import { McpServer } from "@modelcontextprotocol/server";
import { createMcpHandler } from "agents/mcp/server";
import { z } from "zod";
import ENTRIES from "../public_entries.json";
import EMBEDDINGS from "../public_embeddings.json";

interface Entry {
  id: string;
  label: string;
  source: string;
  type: string;
  tags: string[];
  description: string;
  // Real evidence backing this entry, for answering a disputed claim while
  // the account is active -- see ingest/build_graph.py's URL_FIELD_PRIORITY
  // and ingest/common.py's SOURCE_NOTE_BY_TYPE. source_url/captured_at are
  // null when the underlying corpus source genuinely has none (e.g. LinkedIn
  // endorsements never carry a link); source_note explains why in that case.
  source_url: string | null;
  captured_at: string | null;
  provider: string | null;
  source_note: string | null;
}

interface Env {
  AI: Ai;
  MCP_USAGE: AnalyticsEngineDataset;
}

const WORKER_TYPE = "demo";

// Peeks at the JSON-RPC envelope for usage analytics only (method, tool name,
// connecting client) -- never touches the real request, which is handed to
// createMcpHandler unread. Never throws: malformed/absent bodies just mean no
// method-level breakdown for that request, not a failed request.
async function peekRequest(request: Request) {
  try {
    const body = (await request.clone().json()) as {
      method?: string;
      params?: { name?: string; clientInfo?: { name?: string; version?: string } };
    };
    const method = typeof body.method === "string" ? body.method : null;
    return {
      method,
      toolName: method === "tools/call" && typeof body.params?.name === "string" ? body.params.name : null,
      clientName: method === "initialize" ? body.params?.clientInfo?.name ?? null : null,
      clientVersion: method === "initialize" ? body.params?.clientInfo?.version ?? null : null,
    };
  } catch {
    return { method: null, toolName: null, clientName: null, clientVersion: null };
  }
}

// Best-effort success/error classification. MCP tool errors are usually
// HTTP 200 with an `error` or `isError` field inside the JSON-RPC body, not an
// HTTP-level failure -- but only for a non-streamed JSON response; a
// text/event-stream body is left untouched so this never buffers or delays a
// streamed response.
async function classifyStatus(response: Response): Promise<"success" | "error"> {
  if (response.status >= 400) return "error";
  if (response.headers.get("content-type")?.includes("application/json")) {
    try {
      const body = (await response.clone().json()) as { error?: unknown; result?: { isError?: boolean } };
      if (body.error || body.result?.isError === true) return "error";
    } catch {
      // Not parseable JSON -- fall through to HTTP-status-based success.
    }
  }
  return "success";
}

// Metadata only -- no tool arguments, no response content, no query text.
// Matches the privacy discipline already applied to public/private exports
// elsewhere in this repo.
async function logUsage(
  env: Env,
  opts: { customerId: string; startedAt: number; peek: Awaited<ReturnType<typeof peekRequest>>; response: Response },
) {
  try {
    const status = await classifyStatus(opts.response);
    const latencyMs = Date.now() - opts.startedAt;
    // Same fields as the Analytics Engine write below, also surfaced as a
    // structured Workers Logs entry -- unlike Analytics Engine (SQL API only,
    // no dashboard table), Workers Logs' account-level Observability page can
    // show these across every deployed Worker in one filterable table, no API
    // token needed.
    console.log({
      worker_type: WORKER_TYPE,
      customer_id: opts.customerId,
      method: opts.peek.method ?? "",
      tool_name: opts.peek.toolName ?? "",
      client_name: opts.peek.clientName ?? "",
      client_version: opts.peek.clientVersion ?? "",
      status,
      latency_ms: latencyMs,
    });
    env.MCP_USAGE.writeDataPoint({
      indexes: [`${WORKER_TYPE}:${opts.customerId}`],
      blobs: [
        WORKER_TYPE,
        opts.customerId,
        opts.peek.method ?? "",
        opts.peek.toolName ?? "",
        opts.peek.clientName ?? "",
        opts.peek.clientVersion ?? "",
        status,
      ],
      doubles: [latencyMs],
    });
  } catch {
    // Analytics must never break or delay the real response.
  }
}

const EMBEDDING_MODEL = "@cf/baai/bge-base-en-v1.5";
// Empirically tuned against @cf/baai/bge-base-en-v1.5's actual score distribution on
// this corpus (2026-08-09): genuine matches scored 0.70-0.84, unrelated/absent topics
// ("quantum computing", "underwater basket weaving") still scored 0.58-0.61 -- this
// model's cosine similarities don't have a well-calibrated zero baseline, so a naive
// floor like 0.5 let clear noise through as "found: true". 0.65 sits in the real gap.
const SIM_FLOOR = 0.65;
const TOP_N = 10;
// Cap per relation group in related_entries -- some tags are real hubs (e.g.
// tag_python sits on 126+ entries in the full corpus), so an uncapped dump
// would blow past anything a caller actually wants to read. count still
// reports the true total so truncation is visible, not silent.
const RELATED_CAP = 15;
const ALL_TYPES = [
  "course", "project", "certification", "education",
  "endorsement", "position", "profile", "recommendation", "article",
  // Added 2026-08-10 alongside the LinkedIn API rebuild -- all currently
  // empty in the corpus (LinkedIn profile has no data for them yet), so
  // this is forward-declared to avoid list_by_type silently rejecting a
  // valid type the moment one of these domains gets real data, matching
  // ingest/build_public_export.py's ALLOWLIST.
  "organization", "language", "honor", "publication", "patent", "volunteering", "test_score",
  "skill_assessment",
] as const;

let VECTORS: Map<string, Float32Array> | null = null;

function getVectors(): Map<string, Float32Array> {
  if (VECTORS) return VECTORS;
  VECTORS = new Map(
    Object.entries(EMBEDDINGS.vectors as Record<string, string>).map(([id, b64]) => {
      const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
      return [id, new Float32Array(bytes.buffer)];
    }),
  );
  return VECTORS;
}

function cosineSim(a: Float32Array, b: Float32Array): number {
  let dot = 0, na = 0, nb = 0;
  for (let i = 0; i < a.length; i++) {
    dot += a[i] * b[i];
    na += a[i] * a[i];
    nb += b[i] * b[i];
  }
  return dot / (Math.sqrt(na) * Math.sqrt(nb));
}

// tags/provider are already resolved, joinable fields on every entry (see
// ingest/build_public_export.py's node_tags/node_provider) -- no separate
// graph-edge export needed, this is a plain self-join over ENTRIES.
function relatedGroup(entries: Entry[], excludeId: string, predicate: (e: Entry) => boolean) {
  const matches = entries
    .filter((e) => e.id !== excludeId && predicate(e))
    .sort((a, b) => a.label.localeCompare(b.label));
  return {
    count: matches.length,
    entries: matches.slice(0, RELATED_CAP).map((e) => ({ id: e.id, label: e.label, type: e.type })),
  };
}

// Powers skill_evidence: exact tag match (not semantic, unlike query_knowhow),
// uncapped (unlike relatedGroup's RELATED_CAP), grouped by type with a real
// count per type. Each type's entries sort by captured_at ascending (oldest
// first); null-dated entries move to the end and are counted in
// undated_count rather than silently sorted as if their date were known.
type EvidenceEntry = { id: string; label: string; captured_at: string | null };
type TypeGroup = { count: number; undated_count: number; entries: EvidenceEntry[] };

function groupByType(list: Entry[]): Record<string, TypeGroup> {
  const byType: Record<string, TypeGroup> = {};
  for (const e of list) {
    byType[e.type] ??= { count: 0, undated_count: 0, entries: [] };
    const g = byType[e.type];
    g.count++;
    if (!e.captured_at) g.undated_count++;
    g.entries.push({ id: e.id, label: e.label, captured_at: e.captured_at });
  }
  for (const g of Object.values(byType)) {
    g.entries.sort((a, b) => {
      if (a.captured_at && b.captured_at) return a.captured_at.localeCompare(b.captured_at);
      if (a.captured_at) return -1;
      if (b.captured_at) return 1;
      return 0;
    });
  }
  return byType;
}

function groupEvidenceByTag(entries: Entry[], tag: string) {
  const matches = entries.filter((e) => e.tags.includes(tag));
  return { count: matches.length, by_type: groupByType(matches) };
}

function createServer(env: Env) {
  const server = new McpServer({ name: "personalknowhow-demo", version: "0.2.0" });

  server.registerTool(
    "query_knowhow",
    {
      title: "Query know-how",
      annotations: { readOnlyHint: true },
      description:
        "Search this person's real, grounded skills/experience graph for a topic using " +
        "semantic search. Returns only entries with real evidence -- never guesses. Every " +
        "entry here represents something actually done or completed (project, " +
        "certification, position, course, or education) -- this public dataset never " +
        "includes saved-but-not-worked jobs or applications. This is SEMANTIC search " +
        `ranked by relevance and capped at ${TOP_N} results -- it is NOT exhaustive. For ` +
        "'list every X' or 'how many X' questions, use list_by_type instead -- it returns " +
        "the complete, uncapped set with no similarity ranking involved. Clearing the " +
        "similarity floor means 'closest available match', not 'confirmed match' -- read " +
        "each result's actual label/description/type before citing it as evidence for the " +
        "specific topic queried. Each result also carries source_url/captured_at/provider " +
        "(the real evidence behind it, when available) and source_note (explaining why not, " +
        "when the underlying source has no link) -- use these to answer a disputed claim with " +
        "actual backing evidence rather than just the description text. Embeddings can rank " +
        "a topically-adjacent-but-wrong entry " +
        "above the floor (e.g. a course on a different cloud data-warehouse tool, or a " +
        "different framework in the same category) for a term it isn't actually about; if a " +
        "result isn't genuinely on topic, treat the query as unmatched rather than reporting " +
        "it as a match. For 'what else is connected to this' or 'what shares a skill/provider " +
        "with this specific entry' questions, call related_entries with a result's id instead " +
        "of re-querying by topic.",
      inputSchema: { topic: z.string().describe("A skill, technology, or topic to check, e.g. 'django' or 'aws'") },
    },
    async ({ topic }) => {
      const embed = await env.AI.run(EMBEDDING_MODEL, { text: topic });
      const query = new Float32Array((embed as { data: number[][] }).data[0]);
      const vectors = getVectors();

      const scored = (ENTRIES as Entry[])
        // entries.json and embeddings.json can drift (an entry added without a
        // matching embedding regen) -- skip anything with no vector rather than
        // crash the whole request over one missing entry.
        .filter((e) => vectors.has(e.id))
        .map((e) => ({ ...e, score: Math.round(cosineSim(query, vectors.get(e.id)!) * 1000) / 1000 }))
        .filter((e) => e.score >= SIM_FLOOR)
        .sort((a, b) => b.score - a.score)
        .slice(0, TOP_N);

      const result = scored.length > 0
        ? { found: true, count: scored.length, evidence: scored }
        : { found: false, note: "No grounded evidence for this topic in the corpus -- do not assume or guess." };

      return { content: [{ type: "text", text: JSON.stringify(result, null, 2) }] };
    },
  );

  server.registerTool(
    "list_by_type",
    {
      title: "List know-how by type",
      annotations: { readOnlyHint: true },
      description:
        "Returns the COMPLETE, exact set of entries for one type, with no similarity " +
        "ranking, no relevance cutoff, and no cap on count. Use this instead of " +
        "query_knowhow whenever the question requires an exhaustive or countable answer " +
        "('list all my certifications', 'how many courses have I completed'). " +
        "Deterministic ordering (sorted by label) -- repeated calls with the same type " +
        "return the same list in the same order.",
      inputSchema: { type: z.enum(ALL_TYPES).describe("Exact entry type to list in full") },
    },
    async ({ type }) => {
      const matches = (ENTRIES as Entry[])
        .filter((e) => e.type === type)
        .sort((a, b) => a.label.localeCompare(b.label));

      const result = { type, count: matches.length, entries: matches };
      return { content: [{ type: "text", text: JSON.stringify(result, null, 2) }] };
    },
  );

  server.registerTool(
    "related_entries",
    {
      title: "Find related entries",
      annotations: { readOnlyHint: true },
      description:
        "Given an entry id (from a prior query_knowhow or list_by_type result), returns " +
        "other entries that share at least one tag or the same content provider -- the " +
        "only two relationships this corpus currently tracks (there is no 'led to' or " +
        "'used in' relationship here, only shared tag/provider). This is NOT a similarity " +
        "or relevance judgment -- two entries sharing a broad tag (e.g. both tagged " +
        "'data-science') can be quite different in substance; read each related entry's " +
        `own label/type before treating it as meaningful. Each group is capped at ${RELATED_CAP} ` +
        "entries, sorted by label, with the true total count shown separately so you know " +
        "if results were truncated -- call list_by_type on that type if you need the full " +
        "set. Useful for 'what else is connected to X' or 'what did they do that relates " +
        "to this specific course/certification/endorsement' -- questions query_knowhow's " +
        "independent similarity search can't reliably answer, since two entries can be " +
        "genuinely related without their description text reading alike (e.g. a course " +
        "title and an endorsement phrase for the same skill, worded completely differently).",
      inputSchema: { id: z.string().describe("An entry id from a prior query_knowhow or list_by_type result") },
    },
    async ({ id }) => {
      const entries = ENTRIES as Entry[];
      const target = entries.find((e) => e.id === id);
      if (!target) {
        const result = {
          found: false,
          note: "No entry with this id -- ids come from a prior query_knowhow or list_by_type result, never guess one.",
        };
        return { content: [{ type: "text", text: JSON.stringify(result, null, 2) }] };
      }

      const related_by_tag: Record<string, ReturnType<typeof relatedGroup>> = {};
      for (const tag of target.tags) {
        related_by_tag[tag] = relatedGroup(entries, id, (e) => e.tags.includes(tag));
      }
      const related_by_provider = target.provider
        ? relatedGroup(entries, id, (e) => e.provider === target.provider)
        : null;

      const result = {
        id: target.id,
        label: target.label,
        type: target.type,
        tags: target.tags,
        provider: target.provider,
        related_by_tag,
        related_by_provider,
      };
      return { content: [{ type: "text", text: JSON.stringify(result, null, 2) }] };
    },
  );

  server.registerTool(
    "skill_evidence",
    {
      title: "Find skill evidence",
      annotations: { readOnlyHint: true },
      description:
        "Given an exact tag/skill (e.g. 'docker', 'gcp'), returns EVERY entry with that tag, " +
        "uncapped, grouped by type with a real count per type. Unlike related_entries (capped " +
        "at 15, requires a starting entry id) or query_knowhow (semantic, ranked, may over- or " +
        "under-include), this is an EXACT tag match against every entry -- the right tool for " +
        "'how many X have I completed/done' or 'do I have any real evidence for X at all'. " +
        "Tags are exact strings from a prior list_by_type/related_entries/query_knowhow " +
        "result's tags array -- this is NOT semantic search; a tag never assigned during " +
        "ingest returns found:false, try query_knowhow instead. Each type's entries sort by " +
        "captured_at ascending (oldest first); entries with no captured_at are moved to the " +
        "end and counted in undated_count, never silently sorted as if their date were known.",
      inputSchema: { tag: z.string().describe("An exact tag from a prior result's tags array, e.g. 'python', 'docker', 'gcp'") },
    },
    async ({ tag }) => {
      const normalized = tag.trim().toLowerCase().replace(/\s+/g, "-");
      const entries = ENTRIES as Entry[];
      if (!entries.some((e) => e.tags.includes(normalized))) {
        const result = {
          tag: normalized, found: false, count: 0,
          note: "No entries have this tag -- exact match, not semantic search. Check spelling/" +
            "hyphenation, or try query_knowhow if the skill may exist but was never tagged.",
        };
        return { content: [{ type: "text", text: JSON.stringify(result, null, 2) }] };
      }
      const result = { tag: normalized, found: true, ...groupEvidenceByTag(entries, normalized) };
      return { content: [{ type: "text", text: JSON.stringify(result, null, 2) }] };
    },
  );

  return server;
}

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext) {
    const startedAt = Date.now();
    const peek = await peekRequest(request);
    const response = await createMcpHandler(() => createServer(env))(request, env, ctx);
    ctx.waitUntil(logUsage(env, { customerId: "public", startedAt, peek, response }));
    return response;
  },
} satisfies ExportedHandler<Env>;

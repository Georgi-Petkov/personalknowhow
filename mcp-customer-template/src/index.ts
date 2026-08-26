import { McpServer } from "@modelcontextprotocol/server";
import { createMcpHandler } from "agents/mcp/server";
import { z } from "zod";
import ENTRIES from "../entries.json";
import EMBEDDINGS from "../embeddings.json";

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
  evidence_tier: "demonstrated" | "signal_only";
  source_note: string | null;
}

interface Env {
  AI: Ai;
  PRIVATE_MCP_TOKEN: string;
  DB: D1Database;
  MCP_USAGE: AnalyticsEngineDataset;
}

// Set to the customer's hash-based slug (from subscribers.public_slug or
// .private_slug -- never a hand-typed name) at deploy time. This template is
// intentionally generic -- clone it per customer, set this constant, set the
// name in wrangler.toml/package.json to match, then `wrangler deploy`.
const CUSTOMER_LABEL = "CHANGE_ME";
const WORKER_TYPE = "customer_private";
const EMBEDDING_MODEL = "@cf/baai/bge-base-en-v1.5";
// Empirically tuned against @cf/baai/bge-base-en-v1.5's actual score
// distribution: genuine matches scored 0.70-0.84, unrelated/absent topics
// still scored 0.58-0.61 -- this model's cosine similarities don't have a
// well-calibrated zero baseline, so a naive floor like 0.5 lets clear noise
// through as "found: true". 0.65 sits in the real gap.
const SIM_FLOOR = 0.65;
const TOP_N = 10;
// Cap per relation group in related_entries -- some tags are real hubs (e.g.
// tag_python sits on 126+ entries in the full corpus), so an uncapped dump
// would blow past anything a caller actually wants to read. count still
// reports the true total so truncation is visible, not silent.
const RELATED_CAP = 15;
const ALL_TYPES = [
  "course", "project", "certification", "education", "endorsement",
  "position", "profile", "recommendation", "article",
  "career_interest", "job_application",
  "organization", "language", "honor", "publication", "patent", "volunteering", "test_score",
  "skill_assessment",
  "saved_job_alert", "job_seeker_preferences", "saved_answer",
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

// Not ===, to avoid leaking timing information about how many leading
// characters of a guessed token happen to match the real one.
function timingSafeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

// tags/provider are already resolved, joinable fields on every entry -- no
// separate graph-edge export needed, this is a plain self-join over ENTRIES.
// evidence_tier travels with every related entry too -- a signal_only entry
// showing up as "related" is still not proof of ability.
function relatedGroup(entries: Entry[], excludeId: string, predicate: (e: Entry) => boolean) {
  const matches = entries
    .filter((e) => e.id !== excludeId && predicate(e))
    .sort((a, b) => a.label.localeCompare(b.label));
  return {
    count: matches.length,
    entries: matches.slice(0, RELATED_CAP).map((e) => ({
      id: e.id,
      label: e.label,
      type: e.type,
      evidence_tier: e.evidence_tier,
    })),
  };
}

// Powers skill_evidence: exact tag match (not semantic, unlike query_knowhow),
// uncapped (unlike relatedGroup's RELATED_CAP), grouped by evidence_tier then
// type with real counts. by_evidence_tier is always dense (both keys present,
// even at count 0); by_type is sparse (only types with >=1 match). Each
// type's entries sort by captured_at ascending (oldest first); null-dated
// entries move to the end and are counted in undated_count rather than
// silently sorted as if their date were known.
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
  const demonstrated = matches.filter((e) => e.evidence_tier === "demonstrated");
  const signalOnly = matches.filter((e) => e.evidence_tier === "signal_only");
  return {
    count: matches.length,
    by_evidence_tier: {
      demonstrated: { count: demonstrated.length, by_type: groupByType(demonstrated) },
      signal_only: { count: signalOnly.length, by_type: groupByType(signalOnly) },
    },
  };
}

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
// elsewhere in this repo. customerId is CUSTOMER_LABEL, not a hand-typed
// name -- same rule as the D1 gate above (subscribers.public_slug/private_slug).
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

function createServer(env: Env) {
  const server = new McpServer({ name: "personalknowhow-customer", version: "0.1.0" });

  server.registerTool(
    "query_knowhow",
    {
      title: "Query know-how",
      annotations: { readOnlyHint: true },
      description:
        "Search this person's real, grounded skills/experience graph for a topic using " +
        "semantic search. Returns only entries with real evidence -- never guesses. Check " +
        "`evidence_tier` on every result: 'demonstrated' (project/certification/position/" +
        "course/education -- things actually done) vs 'signal_only' (career_interest/" +
        "job_application/etc -- saved or applied to, NOT worked, NOT proof of skill). Never " +
        "cite a signal_only entry as evidence of ability. This is SEMANTIC search ranked by " +
        `relevance and capped at ${TOP_N} results -- it is NOT exhaustive. For 'list every X' ` +
        "or 'how many X' questions, use list_by_type instead -- it returns the complete, " +
        "uncapped set with no similarity ranking involved. Clearing the similarity floor " +
        "means 'closest available match', not 'confirmed match' -- read each result's actual " +
        "label/description/type before citing it as evidence for the specific topic queried. " +
        "Each result also carries source_url/captured_at/provider (the real evidence behind " +
        "it, when available) and source_note (explaining why not, when the underlying source " +
        "has no link) -- use these to answer a disputed claim with actual backing evidence " +
        "rather than just the description text. For 'what else is connected to this' or " +
        "'what shares a skill/provider with this specific entry' questions, call " +
        "related_entries with a result's id instead of re-querying by topic.",
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
        .map((e) => ({
          ...e,
          score: Math.round(cosineSim(query, vectors.get(e.id)!) * 1000) / 1000,
        }))
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
        "Deterministic ordering (sorted by label). `evidence_tier` follows the same rule as " +
        "query_knowhow.",
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
        "set. `evidence_tier` follows the same rule as query_knowhow: never treat a " +
        "signal_only related entry as proof of ability. Useful for 'what else is connected " +
        "to X' or 'what did they do that relates to this specific course/certification/" +
        "endorsement' -- questions query_knowhow's independent similarity search can't " +
        "reliably answer, since two entries can be genuinely related without their " +
        "description text reading alike (e.g. a course title and an endorsement phrase for " +
        "the same skill, worded completely differently).",
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
        evidence_tier: target.evidence_tier,
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
        "uncapped, grouped by evidence_tier ('demonstrated' vs 'signal_only') then by type, " +
        "each group carrying a real count. Unlike related_entries (capped at 15, requires a " +
        "starting entry id) or query_knowhow (semantic, ranked, may over- or under-include), " +
        "this is an EXACT tag match against every entry -- the right tool for 'how many X do I " +
        "have demonstrated evidence for' or 'do I have ANY demonstrated evidence for X, or only " +
        "saved/applied signal'. by_evidence_tier.demonstrated.count === 0 means no real evidence " +
        "exists for this tag, even if signal_only entries do. Tags are exact strings from a " +
        "prior list_by_type/related_entries/query_knowhow result's tags array -- this is NOT " +
        "semantic search; a tag that was never assigned during ingest returns found:false, try " +
        "query_knowhow instead. Each type's entries sort by captured_at ascending (oldest " +
        "first); entries with no captured_at are moved to the end and counted in " +
        "undated_count, never silently sorted as if their date were known.",
      inputSchema: { tag: z.string().describe("An exact tag from a prior result's tags array, e.g. 'python', 'docker', 'gcp'") },
    },
    async ({ tag }) => {
      const normalized = tag.trim().toLowerCase().replace(/\s+/g, "-");
      const entries = ENTRIES as Entry[];
      if (!entries.some((e) => e.tags.includes(normalized))) {
        const result = {
          tag: normalized,
          found: false,
          count: 0,
          note: "No entries have this tag -- exact match, not semantic search. Tags come from a " +
            "prior list_by_type/related_entries/query_knowhow result's tags array; check " +
            "spelling/hyphenation. If the skill genuinely exists in the corpus but was never " +
            "tagged, try query_knowhow instead.",
        };
        return { content: [{ type: "text", text: JSON.stringify(result, null, 2) }] };
      }
      const result = { tag: normalized, found: true, ...groupEvidenceByTag(entries, normalized) };
      return { content: [{ type: "text", text: JSON.stringify(result, null, 2) }] };
    },
  );

  return server;
}

async function handle(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
  // Checked first, before any MCP logic runs. Data is baked into this Worker's
  // bundle at deploy time (static import above) -- deleting the R2 source has no
  // effect on it, so this D1 read is the only thing that can actually revoke
  // access after a "delete my data" request. See site/'s /api/delete-data.
  //
  // Joined through subscribers on email: CUSTOMER_LABEL is a per-tier slug
  // (subscribers.public_slug or .private_slug), never upload_invites.customer_label
  // directly -- one invite row covers two slugs (public+private), so a single
  // customer_label column can't identify which Worker this is. (Found live
  // 2026-08-16: the customer_label-keyed query never matched any row for the
  // real slug-based deploys, meaning this gate silently failed OPEN -- no row
  // found, so neither the deleted_at nor subscription_status check ever fired.)
  const row = await env.DB.prepare(
    `SELECT ui.deleted_at, ui.subscription_status
     FROM upload_invites ui
     JOIN subscribers s ON s.email = ui.email
     WHERE s.public_slug = ? OR s.private_slug = ?`
  ).bind(CUSTOMER_LABEL, CUSTOMER_LABEL).first<{ deleted_at: string | null; subscription_status: string | null }>();
  if (row?.deleted_at) {
    return new Response("This data has been deleted.", { status: 410 });
  }
  // NULL-guarded: invites with no subscription_status at all never went
  // through Stripe and are left untouched. Block-list, not an allow-list of
  // just "active" -- Stripe has legitimate in-progress states ("past_due"
  // while Smart Retries is still attempting a failed renewal charge,
  // "trialing") that should NOT cut access immediately. The retry schedule
  // and dunning reminder emails are Stripe's own (Dashboard: Settings ->
  // Billing -> Subscriptions and emails), not tracked here -- this only
  // blocks once Stripe's own process has concluded the subscription is
  // genuinely over, via the existing customer.subscription.updated/.deleted
  // webhook in site/src/index.ts.
  const BLOCKED_SUBSCRIPTION_STATUSES = new Set(["canceled", "unpaid", "incomplete_expired"]);
  if (row?.subscription_status && BLOCKED_SUBSCRIPTION_STATUSES.has(row.subscription_status)) {
    return new Response("Subscription is not active.", { status: 402 });
  }

  // For a public-tier deployment, remove this block entirely (no
  // PRIVATE_MCP_TOKEN secret, no Authorization check) -- public tier is
  // unauthenticated by design, matching the site's "no token needed" copy.
  const expected = `Bearer ${env.PRIVATE_MCP_TOKEN}`;
  const provided = request.headers.get("Authorization") || "";
  if (!env.PRIVATE_MCP_TOKEN || !timingSafeEqual(provided, expected)) {
    return new Response("Unauthorized", { status: 401 });
  }
  return createMcpHandler(() => createServer(env))(request, env, ctx);
}

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext) {
    const startedAt = Date.now();
    const peek = await peekRequest(request);
    const response = await handle(request, env, ctx);
    ctx.waitUntil(logUsage(env, { customerId: CUSTOMER_LABEL, startedAt, peek, response }));
    return response;
  },
} satisfies ExportedHandler<Env>;

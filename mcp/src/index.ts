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
}

const EMBEDDING_MODEL = "@cf/baai/bge-base-en-v1.5";
// Empirically tuned against @cf/baai/bge-base-en-v1.5's actual score distribution on
// this corpus (2026-08-09): genuine matches scored 0.70-0.84, unrelated/absent topics
// ("quantum computing", "underwater basket weaving") still scored 0.58-0.61 -- this
// model's cosine similarities don't have a well-calibrated zero baseline, so a naive
// floor like 0.5 let clear noise through as "found: true". 0.65 sits in the real gap.
const SIM_FLOOR = 0.65;
const TOP_N = 10;
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
        "it as a match.",
      inputSchema: { topic: z.string().describe("A skill, technology, or topic to check, e.g. 'django' or 'aws'") },
    },
    async ({ topic }) => {
      const embed = await env.AI.run(EMBEDDING_MODEL, { text: topic });
      const query = new Float32Array((embed as { data: number[][] }).data[0]);
      const vectors = getVectors();

      const scored = (ENTRIES as Entry[])
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

  return server;
}

export default {
  fetch(request: Request, env: Env, ctx: ExecutionContext) {
    return createMcpHandler(() => createServer(env))(request, env, ctx);
  },
} satisfies ExportedHandler<Env>;

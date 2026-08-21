import { McpServer } from "@modelcontextprotocol/server";
import { createMcpHandler } from "agents/mcp/server";
import { z } from "zod";
import ENTRIES from "../private_entries.json";
import EMBEDDINGS from "../private_embeddings.json";

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
  PRIVATE_MCP_TOKEN: string;
}

const EMBEDDING_MODEL = "@cf/baai/bge-base-en-v1.5";
// Same empirically-tuned floor as mcp/src/index.ts -- see that file's comment.
const SIM_FLOOR = 0.65;
const TOP_N = 10;
// saved_job_alert/job_seeker_preferences/saved_answer added 2026-08-10 --
// same reasoning as career_interest/job_application: search intent, stated
// preferences, and canned application answers are not proof of skill.
const SIGNAL_ONLY_TYPES = new Set([
  "career_interest", "job_application",
  "saved_job_alert", "job_seeker_preferences", "saved_answer",
]);
const ALL_TYPES = [
  "course", "project", "certification", "education", "endorsement",
  "position", "profile", "recommendation", "article",
  "career_interest", "job_application",
  // Added 2026-08-10 alongside the LinkedIn API rebuild -- matches
  // ingest/build_private_export.py's TYPE_MAP. organization/language/honor/
  // publication/patent/volunteering/test_score are public-tier but listed
  // here too since the private set is a strict superset of the public one;
  // the rest are private-only. All currently empty in the corpus except
  // where noted, forward-declared for the same reason as the public Worker.
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

function createServer(env: Env) {
  const server = new McpServer({ name: "personalknowhow-private", version: "0.1.0" });

  server.registerTool(
    "query_knowhow",
    {
      description:
        "PRIVATE, single-user tool -- includes saved job interests and job applications " +
        "ALONGSIDE real demonstrated experience. Check `evidence_tier` on every result: " +
        "'demonstrated' (project/certification/position/course/education -- things actually " +
        "done) vs 'signal_only' (career_interest/job_application -- roles saved or applied " +
        "to, NOT worked, NOT proof of skill or experience). Never cite a signal_only entry " +
        "as evidence of ability. This is SEMANTIC search ranked by relevance and capped at " +
        `${TOP_N} results -- it is NOT exhaustive and will not reliably surface every entry ` +
        "of a given type (a broad type can have more matches than the cap, and irrelevant-" +
        "sounding entries may rank below the cutoff even if they exist). For 'list every X' " +
        "or 'how many X do I have' questions, use list_by_type instead -- it returns the " +
        "complete, uncapped set with no similarity ranking involved. Clearing the similarity " +
        "floor means 'closest available match', not 'confirmed match' -- read each result's " +
        "actual label/description/type before citing it as evidence for the specific topic " +
        "queried. Each result also carries source_url/captured_at/provider (the real evidence " +
        "behind it, when available) and source_note (explaining why not, when the underlying " +
        "source has no link) -- use these to answer a disputed claim with actual backing " +
        "evidence rather than just the description text. Embeddings can rank a " +
        "topically-adjacent-but-wrong entry above the floor " +
        "(e.g. a course on a different cloud data-warehouse tool, or a different framework in " +
        "the same category) for a term it isn't actually about; if a result isn't genuinely on " +
        "topic, treat the query as unmatched rather than reporting it as a match.",
      inputSchema: { topic: z.string().describe("A skill, technology, or topic to check, e.g. 'django' or 'aws'") },
    },
    async ({ topic }) => {
      const embed = await env.AI.run(EMBEDDING_MODEL, { text: topic });
      const query = new Float32Array((embed as { data: number[][] }).data[0]);
      const vectors = getVectors();

      const scored = (ENTRIES as Entry[])
        .map((e) => ({
          ...e,
          evidence_tier: SIGNAL_ONLY_TYPES.has(e.type) ? "signal_only" : "demonstrated",
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
      description:
        "PRIVATE, single-user tool -- returns the COMPLETE, exact set of entries for one " +
        "type, with no similarity ranking, no relevance cutoff, and no cap on count. Use " +
        "this instead of query_knowhow whenever the question requires an exhaustive or " +
        "countable answer ('list all my job applications', 'how many certifications do I " +
        "have'). Deterministic ordering (sorted by label) -- repeated calls with the same " +
        "type return the same list in the same order. `evidence_tier` follows the same " +
        "rule as query_knowhow: 'demonstrated' for real experience, 'signal_only' for " +
        "career_interest/job_application (saved/applied, not worked).",
      inputSchema: { type: z.enum(ALL_TYPES).describe("Exact entry type to list in full") },
    },
    async ({ type }) => {
      const matches = (ENTRIES as Entry[])
        .filter((e) => e.type === type)
        .map((e) => ({ ...e, evidence_tier: SIGNAL_ONLY_TYPES.has(e.type) ? "signal_only" : "demonstrated" }))
        .sort((a, b) => a.label.localeCompare(b.label));

      const result = { type, count: matches.length, entries: matches };
      return { content: [{ type: "text", text: JSON.stringify(result, null, 2) }] };
    },
  );

  return server;
}

export default {
  fetch(request: Request, env: Env, ctx: ExecutionContext) {
    const expected = `Bearer ${env.PRIVATE_MCP_TOKEN}`;
    const provided = request.headers.get("Authorization") || "";
    if (!env.PRIVATE_MCP_TOKEN || !timingSafeEqual(provided, expected)) {
      return new Response("Unauthorized", { status: 401 });
    }
    return createMcpHandler(() => createServer(env))(request, env, ctx);
  },
} satisfies ExportedHandler<Env>;

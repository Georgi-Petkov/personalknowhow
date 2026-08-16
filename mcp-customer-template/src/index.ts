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
}

interface Env {
  AI: Ai;
  PRIVATE_MCP_TOKEN: string;
  DB: D1Database;
}

// Set to the customer's hash-based slug (from subscribers.public_slug or
// .private_slug -- never a hand-typed name) at deploy time. This template is
// intentionally generic -- clone it per customer, set this constant, set the
// name in wrangler.toml/package.json to match, then `wrangler deploy`.
const CUSTOMER_LABEL = "CHANGE_ME";
const EMBEDDING_MODEL = "@cf/baai/bge-base-en-v1.5";
// Empirically tuned against @cf/baai/bge-base-en-v1.5's actual score
// distribution: genuine matches scored 0.70-0.84, unrelated/absent topics
// still scored 0.58-0.61 -- this model's cosine similarities don't have a
// well-calibrated zero baseline, so a naive floor like 0.5 lets clear noise
// through as "found: true". 0.65 sits in the real gap.
const SIM_FLOOR = 0.65;
const TOP_N = 10;
// career_interest/job_application/saved_job_alert/job_seeker_preferences/
// saved_answer are search intent, stated preferences, or canned application
// answers -- not proof of skill. Only present for a private-tier deployment
// (public-tier entries.json never contains these types at all).
const SIGNAL_ONLY_TYPES = new Set([
  "career_interest", "job_application",
  "saved_job_alert", "job_seeker_preferences", "saved_answer",
]);
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
        "label/description/type before citing it as evidence for the specific topic queried.",
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
        .map((e) => ({ ...e, evidence_tier: SIGNAL_ONLY_TYPES.has(e.type) ? "signal_only" : "demonstrated" }))
        .sort((a, b) => a.label.localeCompare(b.label));

      const result = { type, count: matches.length, entries: matches };
      return { content: [{ type: "text", text: JSON.stringify(result, null, 2) }] };
    },
  );

  return server;
}

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext) {
    // Checked first, before any MCP logic runs. Data is baked into this Worker's
    // bundle at deploy time (static import above) -- deleting the R2 source has no
    // effect on it, so this D1 read is the only thing that can actually revoke
    // access after a "delete my data" request. See site/'s /api/delete-data.
    const row = await env.DB.prepare(
      "SELECT deleted_at, subscription_status FROM upload_invites WHERE customer_label = ?"
    ).bind(CUSTOMER_LABEL).first<{ deleted_at: string | null; subscription_status: string | null }>();
    if (row?.deleted_at) {
      return new Response("This data has been deleted.", { status: 410 });
    }
    // NULL-guarded: invites with no subscription_status at all never went
    // through Stripe and are left untouched -- only rows that DID go through
    // Stripe get enforced on cancellation.
    if (row?.subscription_status && row.subscription_status !== "active") {
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
  },
} satisfies ExportedHandler<Env>;

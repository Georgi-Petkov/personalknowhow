export interface Env {
  ASSETS: Fetcher;
  DB: D1Database;
  STORAGE: R2Bucket;
  STRIPE_WEBHOOK_SECRET: string;
  RESEND_WEBHOOK_SECRET: string;
  SLUG_HMAC_KEY: string;
  SITE_EVENTS: AnalyticsEngineDataset;
}

// Allowlist, not free text -- keeps the dataset from filling up with
// arbitrary strings if the endpoint is ever hit directly instead of through
// a real button. Add an entry here whenever a new data-track attribute is
// added in index.html.
const TRACKABLE_EVENTS = new Set(["cta_waitlist_hero", "cta_demo_hero"]);

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

// Customer-facing subdomains/Worker names are derived from a secret HMAC over the
// email, not the email's local part -- a local-part slug (e.g. "wife" or
// "janedoe") still puts an identity-adjacent string in a public URL. The key
// lives only as a Worker secret, never in the database, so even full DB read
// access can't be used to confirm "is this email a customer" by recomputing the
// hash -- you'd need the key too. Separate purpose strings ("public"/"private")
// so the two tiers' slugs are cryptographically unlinkable from each other.
async function deriveSlug(key: string, email: string, purpose: "public" | "private"): Promise<string> {
  const cryptoKey = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(key),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const sigBytes = await crypto.subtle.sign(
    "HMAC", cryptoKey, new TextEncoder().encode(`${email}:${purpose}`)
  );
  const hex = [...new Uint8Array(sigBytes)].map((b) => b.toString(16).padStart(2, "0")).join("");
  return hex.slice(0, 10);
}

// Plain Web Crypto HMAC verification -- no Stripe SDK dependency needed since we
// only ever verify Payment Link webhooks here, never call the Stripe API. Stripe's
// scheme: header "t=<unix ts>,v1=<hex hmac>", signed payload is "<ts>.<raw body>".
// Timestamp tolerance matches Stripe's own default (5 minutes) to reject replays.
async function verifyStripeSignature(payload: string, header: string, secret: string): Promise<boolean> {
  const parts = Object.fromEntries(header.split(",").map((kv) => kv.split("=")) as [string, string][]);
  const timestamp = parts["t"];
  const signature = parts["v1"];
  if (!timestamp || !signature) return false;

  const ageSeconds = Math.abs(Date.now() / 1000 - Number(timestamp));
  if (!Number.isFinite(ageSeconds) || ageSeconds > 300) return false;

  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const sigBytes = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(`${timestamp}.${payload}`));
  const expected = [...new Uint8Array(sigBytes)].map((b) => b.toString(16).padStart(2, "0")).join("");

  if (expected.length !== signature.length) return false;
  let diff = 0;
  for (let i = 0; i < expected.length; i++) diff |= expected.charCodeAt(i) ^ signature.charCodeAt(i);
  return diff === 0;
}

// Resend webhooks are Svix-signed -- headers svix-id/svix-timestamp/svix-signature,
// HMAC-SHA256 over "<svix-id>.<svix-timestamp>.<raw body>". Unlike Stripe's secret
// above (used as a raw UTF-8 string), the Svix secret is base64-encoded -- strip the
// "whsec_" prefix and base64-decode it into raw key bytes before importing. The
// signature itself is base64, not hex. svix-signature can carry multiple
// space-separated "v1,<sig>" entries (secret rotation) -- accept if any one matches.
// Timestamp tolerance matches verifyStripeSignature's (5 minutes).
async function verifyResendSignature(
  payload: string, svixId: string, svixTimestamp: string, svixSignature: string, secret: string,
): Promise<boolean> {
  if (!svixId || !svixTimestamp || !svixSignature) return false;

  const ageSeconds = Math.abs(Date.now() / 1000 - Number(svixTimestamp));
  if (!Number.isFinite(ageSeconds) || ageSeconds > 300) return false;

  const secretBytes = Uint8Array.from(atob(secret.replace(/^whsec_/, "")), (c) => c.charCodeAt(0));
  const key = await crypto.subtle.importKey(
    "raw", secretBytes, { name: "HMAC", hash: "SHA-256" }, false, ["sign"],
  );
  const sigBytes = await crypto.subtle.sign(
    "HMAC", key, new TextEncoder().encode(`${svixId}.${svixTimestamp}.${payload}`),
  );
  const expected = btoa(String.fromCharCode(...new Uint8Array(sigBytes)));

  return svixSignature.split(" ").some((entry) => {
    const sig = entry.startsWith("v1,") ? entry.slice(3) : entry;
    if (sig.length !== expected.length) return false;
    let diff = 0;
    for (let i = 0; i < sig.length; i++) diff |= sig.charCodeAt(i) ^ expected.charCodeAt(i);
    return diff === 0;
  });
}

// The upload-link email embeds "?token=<uuid>" directly, so a clicked link is a
// second, independent way to recover the invite token beyond the Resend `tags`
// field (data.tags.token, set on send by send_invite.py/send_payment_link.py) --
// useful as a fallback for any email sent before tagging existed. Never throws on
// a malformed/non-URL link; that must never crash the webhook handler.
function parseTokenFromLink(link: unknown): string | null {
  if (typeof link !== "string") return null;
  try {
    return new URL(link).searchParams.get("token");
  } catch {
    return null;
  }
}

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);

    if (url.pathname === "/api/track" && request.method === "POST") {
      // sendBeacon posts as text/plain, not application/json -- read as text
      // and parse manually rather than request.json(), which would reject it.
      let eventName: unknown;
      try {
        eventName = (JSON.parse(await request.text()) as { event?: unknown }).event;
      } catch {
        return new Response(null, { status: 204 });
      }
      if (typeof eventName === "string" && TRACKABLE_EVENTS.has(eventName)) {
        // Mirrored into Workers Logs, not just Analytics Engine -- same reason
        // as mcp/src/index.ts's logUsage: Analytics Engine is SQL-API-only
        // (needs an account-scoped API token to query), while Workers Logs is
        // queryable through the account's existing Observability access with
        // no separate token.
        console.log({ event: eventName });
        ctx.waitUntil(
          (async () => {
            try {
              env.SITE_EVENTS.writeDataPoint({ indexes: [eventName as string], blobs: [eventName as string] });
            } catch {
              // Analytics must never break the page.
            }
          })(),
        );
      }
      return new Response(null, { status: 204 });
    }

    if (url.pathname === "/api/stripe-webhook" && request.method === "POST") {
      const signatureHeader = request.headers.get("Stripe-Signature") ?? "";
      const rawBody = await request.text();

      if (!(await verifyStripeSignature(rawBody, signatureHeader, env.STRIPE_WEBHOOK_SECRET))) {
        return new Response("Invalid signature", { status: 400 });
      }

      let event: { type?: string; data?: { object?: Record<string, unknown> } };
      try {
        event = JSON.parse(rawBody);
      } catch {
        return new Response("Invalid JSON", { status: 400 });
      }

      const obj = event.data?.object ?? {};

      if (event.type === "checkout.session.completed") {
        // client_reference_id carries our invite token -- set on the Payment Link
        // as "?client_reference_id=<token>" when the payment-link email is sent.
        const token = obj["client_reference_id"];
        if (typeof token === "string" && token) {
          const result = await env.DB.prepare(
            "UPDATE upload_invites SET payment_confirmed_at = ?, stripe_customer_id = ?, stripe_subscription_id = ?, subscription_status = 'active' " +
            "WHERE token = ? AND payment_confirmed_at IS NULL"
          ).bind(
            new Date().toISOString(),
            typeof obj["customer"] === "string" ? obj["customer"] : null,
            typeof obj["subscription"] === "string" ? obj["subscription"] : null,
            token,
          ).run();

          // Referral credits: only on the genuine first confirmation for this invite
          // (guarded by the WHERE above, confirmed via changes > 0 -- a redelivered
          // webhook for an already-confirmed invite is a no-op here too). checkout
          // session id is the idempotency key on referral_credits, suffixed per
          // beneficiary since one session earns up to two rows.
          if (result.meta.changes > 0) {
            const invite = await env.DB.prepare(
              "SELECT email, referred_by FROM upload_invites WHERE token = ?"
            ).bind(token).first<{ email: string | null; referred_by: string | null }>();

            if (invite?.referred_by && invite.email) {
              const sessionId = typeof obj["id"] === "string" ? obj["id"] : token;
              const now = new Date().toISOString();
              await env.DB.batch([
                env.DB.prepare(
                  "INSERT OR IGNORE INTO referral_credits (beneficiary_email, reason, checkout_session_id, credited_at) VALUES (?, 'referrer_bonus', ?, ?)"
                ).bind(invite.referred_by, `${sessionId}:referrer`, now),
                env.DB.prepare(
                  "INSERT OR IGNORE INTO referral_credits (beneficiary_email, reason, checkout_session_id, credited_at) VALUES (?, 'referred_new_user', ?, ?)"
                ).bind(invite.email, `${sessionId}:referred`, now),
              ]);
            }
          }
        }
      } else if (event.type === "customer.subscription.updated" || event.type === "customer.subscription.deleted") {
        const subscriptionId = obj["id"];
        const status = event.type === "customer.subscription.deleted" ? "canceled" : obj["status"];
        if (typeof subscriptionId === "string" && subscriptionId) {
          await env.DB.prepare(
            "UPDATE upload_invites SET subscription_status = ? WHERE stripe_subscription_id = ?"
          ).bind(typeof status === "string" ? status : "canceled", subscriptionId).run();
        }
      }

      return Response.json({ received: true });
    }

    if (url.pathname === "/api/resend-webhook" && request.method === "POST") {
      const rawBody = await request.text();
      const svixId = request.headers.get("svix-id") ?? "";
      const svixTimestamp = request.headers.get("svix-timestamp") ?? "";
      const svixSignature = request.headers.get("svix-signature") ?? "";

      if (!(await verifyResendSignature(rawBody, svixId, svixTimestamp, svixSignature, env.RESEND_WEBHOOK_SECRET))) {
        return new Response("Invalid signature", { status: 400 });
      }

      let event: {
        type?: string;
        created_at?: string;
        data?: {
          email_id?: string;
          to?: string[];
          tags?: Record<string, string>;
          click?: { link?: string };
          bounce?: { type?: string; message?: string };
        };
      };
      try {
        event = JSON.parse(rawBody);
      } catch {
        return new Response("Invalid JSON", { status: 400 });
      }

      const data = event.data ?? {};
      // tags is the primary correlation path (present on every event type, not just
      // clicks) -- falls back to parsing the clicked link's own ?token= only for
      // email.clicked events sent before the `tags` field existed on the send call.
      const uploadToken =
        data.tags?.token ?? (event.type === "email.clicked" ? parseTokenFromLink(data.click?.link) : null);

      // svix_id is the dedup key -- Resend retries with backoff on any non-200
      // response, and INSERT OR IGNORE means a redelivered event is a silent no-op
      // instead of a duplicate row, the same pattern referral_credits already uses
      // to dedup on Stripe's checkout session id.
      await env.DB.prepare(
        "INSERT OR IGNORE INTO email_events " +
        "(svix_id, event_type, email_id, recipient, upload_token, clicked_url, bounce_type, bounce_message, event_created_at, received_at, raw_payload) " +
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
      ).bind(
        svixId,
        event.type ?? "unknown",
        data.email_id ?? "",
        data.to?.[0] ?? null,
        uploadToken,
        data.click?.link ?? null,
        data.bounce?.type ?? null,
        data.bounce?.message ?? null,
        event.created_at ?? new Date().toISOString(),
        new Date().toISOString(),
        rawBody,
      ).run();

      return Response.json({ received: true });
    }

    if (url.pathname === "/api/upload" && request.method === "POST") {
      const token = request.headers.get("X-Upload-Token") ?? "";
      if (!token) {
        return Response.json({ error: "Missing upload token." }, { status: 401 });
      }

      const invite = await env.DB.prepare(
        "SELECT used_at, deleted_at, email FROM upload_invites WHERE token = ?"
      ).bind(token).first<{ used_at: string | null; deleted_at: string | null; email: string | null }>();

      if (!invite || invite.used_at || invite.deleted_at) {
        return Response.json({ error: "Invalid or already-used upload link." }, { status: 401 });
      }

      const formData = await request.formData();
      const file = formData.get("file");
      if (!(file instanceof File)) {
        return Response.json({ error: "No file received." }, { status: 400 });
      }
      // Required, not just recorded for show -- this is the 90-day fixed-retention
      // consent (see retention_expiry.py), so an upload with no consent shouldn't
      // silently proceed under the old no-consent assumption.
      if (formData.get("consent") !== "true") {
        return Response.json({ error: "Consent is required to upload." }, { status: 400 });
      }

      // Referral attribution: an email, not a slug -- a slug (public_slug/private_slug)
      // is the live subdomain that serves that person's MCP data, so handing it out as a
      // casually-shared referral code would leak a real data endpoint. An email grants
      // access to nothing. Only counts if it belongs to a real paying/comped customer
      // (not merely a waitlist signup, which subscribers alone would not distinguish) and
      // isn't the uploader's own email (self-referral guard). Invalid/unverifiable input
      // is silently dropped -- never blocks the upload itself.
      let referredBy: string | null = null;
      const referredByRaw = formData.get("referred_by");
      if (typeof referredByRaw === "string" && referredByRaw.trim()) {
        const normalized = referredByRaw.trim().toLowerCase();
        if (normalized !== (invite.email ?? "").trim().toLowerCase()) {
          const referrer = await env.DB.prepare(
            "SELECT email FROM upload_invites WHERE email = ? AND (payment_confirmed_at IS NOT NULL OR subscription_status IN ('active', 'comped'))"
          ).bind(normalized).first<{ email: string }>();
          if (referrer) {
            referredBy = referrer.email;
          }
        }
      }

      const bytes = await file.arrayBuffer();
      if (bytes.byteLength === 0) {
        return Response.json({ error: "The file arrived empty. Try again." }, { status: 400 });
      }
      // A real LinkedIn export is a few MB at most. 50MB is generous headroom while
      // still bounding worst-case processing cost / abuse of the pipeline.
      const MAX_UPLOAD_BYTES = 50 * 1024 * 1024;
      if (bytes.byteLength > MAX_UPLOAD_BYTES) {
        return Response.json({ error: "File is too large (max 50MB) for a LinkedIn export." }, { status: 400 });
      }

      const uploadId = crypto.randomUUID();
      const r2Key = `raw/${uploadId}.zip`;
      await env.STORAGE.put(r2Key, bytes);

      const now = new Date().toISOString();
      await env.DB.prepare(
        "UPDATE upload_invites SET used_at = ?, r2_upload_key = ?, consent_given_at = ?, referred_by = ? WHERE token = ?"
      ).bind(now, r2Key, now, referredBy, token).run();

      return Response.json({ ok: true });
    }

    if (url.pathname === "/api/status" && request.method === "GET") {
      const token = url.searchParams.get("token") ?? "";
      if (!token) {
        return Response.json({ error: "Missing token." }, { status: 400 });
      }

      const invite = await env.DB.prepare(
        "SELECT used_at, deleted_at, mcp_url_public, mcp_url_private, mcp_token_private, payment_confirmed_at, upload_rejected_reason FROM upload_invites WHERE token = ?"
      ).bind(token).first<{
        used_at: string | null;
        deleted_at: string | null;
        mcp_url_public: string | null;
        mcp_url_private: string | null;
        mcp_token_private: string | null;
        payment_confirmed_at: string | null;
        upload_rejected_reason: string | null;
      }>();

      if (!invite) {
        return Response.json({ error: "Unknown link." }, { status: 404 });
      }
      if (invite.upload_rejected_reason) {
        return Response.json({ status: "rejected", reason: invite.upload_rejected_reason });
      }
      if (invite.deleted_at) {
        return Response.json({ status: "deleted" });
      }
      if (invite.mcp_url_public || invite.mcp_url_private) {
        // Two tiers per customer: a shareable, unauthenticated "know-how card"
        // (public) and a bearer-token-gated full-data server (private).
        return Response.json({
          status: "ready",
          mcp_url_public: invite.mcp_url_public,
          mcp_url_private: invite.mcp_url_private,
          mcp_token_private: invite.mcp_token_private,
        });
      }
      if (invite.payment_confirmed_at) {
        return Response.json({ status: "processing" });
      }
      if (invite.used_at) {
        return Response.json({ status: "pending_payment" });
      }
      return Response.json({ status: "pending_upload" });
    }

    if (url.pathname === "/mock-pay" && request.method === "GET") {
      // TEST-MODE STAND-IN for a real Stripe Payment Link + webhook. No money moves
      // here -- this is a plain link sent by email; clicking it is treated as a
      // completed payment. Real version: this becomes a real Stripe Payment Link
      // (the customer never lands back on our domain mid-payment), and confirmation
      // comes from a POST /api/stripe-webhook verifying a signed checkout.session.completed
      // event, not a GET a browser can trigger by itself.
      const token = url.searchParams.get("token") ?? "";
      const page = (title: string, body: string) =>
        new Response(
          `<!doctype html><html><head><meta charset="utf-8"><title>${title} — PersonalKnowHow</title></head><body style="font-family:sans-serif;max-width:520px;margin:80px auto;padding:0 20px;"><h2>${title}</h2><p>${body}</p></body></html>`,
          { headers: { "Content-Type": "text/html; charset=utf-8" } },
        );

      if (!token) {
        return page("Invalid link", "This payment link is missing its token.");
      }

      const invite = await env.DB.prepare(
        "SELECT used_at, deleted_at, payment_confirmed_at FROM upload_invites WHERE token = ?"
      ).bind(token).first<{
        used_at: string | null;
        deleted_at: string | null;
        payment_confirmed_at: string | null;
      }>();

      if (!invite || invite.deleted_at) {
        return page("Invalid link", "This link is no longer valid.");
      }
      if (!invite.used_at) {
        return page("Not ready", "Upload your file before paying.");
      }
      if (!invite.payment_confirmed_at) {
        await env.DB.prepare(
          "UPDATE upload_invites SET payment_confirmed_at = ? WHERE token = ?"
        ).bind(new Date().toISOString(), token).run();
      }

      return page(
        "Payment received",
        "Processing has started. Within 24 hours you'll get an email with your personal MCP connection link, plus instructions to add it: in Claude.ai (web), go to Settings &rarr; Connectors &rarr; Add custom connector and paste in the URL; in Claude Desktop, add the provided config snippet to claude_desktop_config.json and restart.",
      );
    }

    if (url.pathname === "/api/delete-data" && request.method === "POST") {
      const token = request.headers.get("X-Upload-Token") ?? "";
      if (!token) {
        return Response.json({ error: "Missing upload token." }, { status: 401 });
      }

      let body: { email?: unknown };
      try {
        body = await request.json();
      } catch {
        return Response.json({ error: "Invalid JSON body." }, { status: 400 });
      }
      const enteredEmail = String(body.email ?? "").trim().toLowerCase();
      if (!enteredEmail) {
        return Response.json({ error: "Email is required to confirm deletion." }, { status: 400 });
      }

      const invite = await env.DB.prepare(
        "SELECT r2_upload_key, deleted_at, email FROM upload_invites WHERE token = ?"
      ).bind(token).first<{
        r2_upload_key: string | null;
        deleted_at: string | null;
        email: string | null;
      }>();

      if (!invite || invite.deleted_at) {
        return Response.json({ error: "Invalid link." }, { status: 401 });
      }
      if (!invite.email) {
        // No email on file to check against -- fail closed rather than silently
        // accepting anything typed. Fix by setting one on the invite row.
        return Response.json(
          { error: "This invite has no email on file. Deletion can't be confirmed." },
          { status: 400 },
        );
      }
      if (enteredEmail !== invite.email.trim().toLowerCase()) {
        return Response.json({ error: "That email doesn't match." }, { status: 403 });
      }

      // Set deleted_at FIRST -- this is the actual kill switch. Every customer Worker
      // checks this column (joined against subscribers by email, see
      // mcp-customer-template/src/index.ts) before serving anything, so access is
      // cut off the instant this write lands, independent of whether the R2 cleanup
      // below succeeds. mcp_url_public/mcp_url_private are kept (not nulled) so the
      // weekly cleanup digest can still name which deployed Workers need manual
      // teardown. mcp_token_private IS cleared -- the server-side data behind it is
      // already gone, so the bearer token is dead weight with no reason to keep.
      await env.DB.prepare(
        "UPDATE upload_invites SET deleted_at = ?, deleted_by_email = ?, mcp_token_private = NULL, r2_upload_key = NULL WHERE token = ?"
      ).bind(new Date().toISOString(), enteredEmail, token).run();

      // Slugs live on subscribers (keyed by email), not on upload_invites -- one
      // invite row corresponds to a public+private slug pair, not a single
      // customer_label, so both prefixes must be resolved via this join.
      const slugs = await env.DB.prepare(
        "SELECT public_slug, private_slug FROM subscribers WHERE email = ?"
      ).bind(invite.email).first<{ public_slug: string | null; private_slug: string | null }>();

      const keysToDelete = [invite.r2_upload_key];
      for (const slug of [slugs?.public_slug, slugs?.private_slug]) {
        if (slug) {
          keysToDelete.push(`customers/${slug}/entries.json`, `customers/${slug}/embeddings.json`);
        }
      }
      await Promise.all(
        keysToDelete.filter((k): k is string => !!k).map((k) => env.STORAGE.delete(k))
      );

      return Response.json({ ok: true });
    }

    if (url.pathname === "/api/waitlist" && request.method === "POST") {
      let body: { email?: unknown; note?: unknown };
      try {
        body = await request.json();
      } catch {
        return Response.json({ error: "Invalid JSON body." }, { status: 400 });
      }

      const email = String(body.email ?? "").trim().toLowerCase();
      const note = String(body.note ?? "").trim().slice(0, 500);

      if (!EMAIL_RE.test(email)) {
        return Response.json({ error: "Enter a valid email address." }, { status: 400 });
      }

      const publicSlug = await deriveSlug(env.SLUG_HMAC_KEY, email, "public");
      const privateSlug = await deriveSlug(env.SLUG_HMAC_KEY, email, "private");

      await env.DB.prepare(
        "INSERT OR IGNORE INTO subscribers (email, note, created_at, public_slug, private_slug) VALUES (?, ?, ?, ?, ?)"
      ).bind(email, note, new Date().toISOString(), publicSlug, privateSlug).run();

      return Response.json({ ok: true });
    }

    return new Response("Not found", { status: 404 });
  },
} satisfies ExportedHandler<Env>;

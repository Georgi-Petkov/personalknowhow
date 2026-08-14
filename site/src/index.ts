export interface Env {
  ASSETS: Fetcher;
  DB: D1Database;
  STORAGE: R2Bucket;
}

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);

    if (url.pathname === "/api/upload" && request.method === "POST") {
      const token = request.headers.get("X-Upload-Token") ?? "";
      if (!token) {
        return Response.json({ error: "Missing upload token." }, { status: 401 });
      }

      const invite = await env.DB.prepare(
        "SELECT used_at, deleted_at FROM upload_invites WHERE token = ?"
      ).bind(token).first<{ used_at: string | null; deleted_at: string | null }>();

      if (!invite || invite.used_at || invite.deleted_at) {
        return Response.json({ error: "Invalid or already-used upload link." }, { status: 401 });
      }

      const formData = await request.formData();
      const file = formData.get("file");
      if (!(file instanceof File)) {
        return Response.json({ error: "No file received." }, { status: 400 });
      }

      const bytes = await file.arrayBuffer();
      if (bytes.byteLength === 0) {
        return Response.json({ error: "The file arrived empty. Try again." }, { status: 400 });
      }

      const uploadId = crypto.randomUUID();
      const r2Key = `raw/${uploadId}.zip`;
      await env.STORAGE.put(r2Key, bytes);

      await env.DB.prepare(
        "UPDATE upload_invites SET used_at = ?, r2_upload_key = ? WHERE token = ?"
      ).bind(new Date().toISOString(), r2Key, token).run();

      return Response.json({ ok: true });
    }

    if (url.pathname === "/api/status" && request.method === "GET") {
      const token = url.searchParams.get("token") ?? "";
      if (!token) {
        return Response.json({ error: "Missing token." }, { status: 400 });
      }

      const invite = await env.DB.prepare(
        "SELECT used_at, deleted_at, mcp_url, mcp_token FROM upload_invites WHERE token = ?"
      ).bind(token).first<{
        used_at: string | null;
        deleted_at: string | null;
        mcp_url: string | null;
        mcp_token: string | null;
      }>();

      if (!invite) {
        return Response.json({ error: "Unknown link." }, { status: 404 });
      }
      if (invite.deleted_at) {
        return Response.json({ status: "deleted" });
      }
      if (invite.mcp_url) {
        // mcp_token is optional -- a server deployed unauthenticated for testing
        // (matching the public demo's pattern) has none, and that's fine.
        return Response.json({ status: "ready", mcp_url: invite.mcp_url, mcp_token: invite.mcp_token });
      }
      if (invite.used_at) {
        return Response.json({ status: "processing" });
      }
      return Response.json({ status: "pending_upload" });
    }

    if (url.pathname === "/api/delete-data" && request.method === "POST") {
      const token = request.headers.get("X-Upload-Token") ?? "";
      if (!token) {
        return Response.json({ error: "Missing upload token." }, { status: 401 });
      }

      const invite = await env.DB.prepare(
        "SELECT r2_upload_key, customer_label, deleted_at FROM upload_invites WHERE token = ?"
      ).bind(token).first<{ r2_upload_key: string | null; customer_label: string | null; deleted_at: string | null }>();

      if (!invite || invite.deleted_at) {
        return Response.json({ error: "Invalid link." }, { status: 401 });
      }

      const keysToDelete = [invite.r2_upload_key];
      if (invite.customer_label) {
        keysToDelete.push(
          `customers/${invite.customer_label}/entries.json`,
          `customers/${invite.customer_label}/embeddings.json`,
        );
      }
      await Promise.all(
        keysToDelete.filter((k): k is string => !!k).map((k) => env.STORAGE.delete(k))
      );

      // mcp_url/customer_label are kept (not nulled) so the weekly cleanup digest can
      // still name which deployed Worker needs manual teardown. mcp_token IS cleared --
      // the server-side data behind it is already gone, so the bearer token is dead
      // weight with no reason to keep.
      await env.DB.prepare(
        "UPDATE upload_invites SET deleted_at = ?, mcp_token = NULL, r2_upload_key = NULL WHERE token = ?"
      ).bind(new Date().toISOString(), token).run();

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

      await env.DB.prepare(
        "INSERT OR IGNORE INTO subscribers (email, note, created_at) VALUES (?, ?, ?)"
      ).bind(email, note, new Date().toISOString()).run();

      return Response.json({ ok: true });
    }

    return new Response("Not found", { status: 404 });
  },
} satisfies ExportedHandler<Env>;

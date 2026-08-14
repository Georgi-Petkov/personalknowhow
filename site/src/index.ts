export interface Env {
  ASSETS: Fetcher;
  WAITLIST: KVNamespace;
}

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

interface WaitlistEntry {
  email: string;
  note: string;
  timestamp: string;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);

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

      const entry: WaitlistEntry = { email, note, timestamp: new Date().toISOString() };
      await env.WAITLIST.put(email, JSON.stringify(entry));

      return Response.json({ ok: true });
    }

    return new Response("Not found", { status: 404 });
  },
} satisfies ExportedHandler<Env>;

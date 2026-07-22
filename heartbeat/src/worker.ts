/**
 * pkmn-alert cron heartbeat — Cloudflare Worker.
 *
 * ## What this does
 *
 * Fires on Cloudflare's edge cron every 5 minutes and POSTs a
 * ``workflow_dispatch`` to GitHub's REST API, which kicks off the
 * pkmn-alert monitor workflow. This compensates for GitHub Actions'
 * unreliable scheduler on free public repos (currently drifting to a
 * ~95 min median gap when the configured schedule is every 5 minutes).
 *
 * ## Why Cloudflare Workers
 *
 * CF cron triggers run at the edge with a hard SLA — no cold start,
 * no shared scheduler contention with 100M other repos. Free tier is
 * 100k requests/day; we use ~288/day (12/hr × 24hr). We are three
 * orders of magnitude under the free ceiling.
 *
 * ## Why this doesn't cause duplicate runs
 *
 * The pkmn-alert monitor workflow declares a concurrency group that
 * cancels overlapping runs. If a CF firing and the native GH cron
 * fire in the same window, only one execution proceeds. State.json
 * is committed atomically by whichever wins, keeping the dedupe
 * cache consistent.
 *
 * ## Failure modes and observability
 *
 * All non-204 responses log the status + first 400 bytes of the body
 * to CF's console (viewable via ``wrangler tail``). The most common
 * failure is a rotated / expired PAT (401), followed by wrong repo
 * name in the URL (404). Neither would fail silently — you'd see it
 * in tail immediately.
 */

export interface Env {
  /**
   * GitHub fine-grained PAT with ``Actions: read/write`` on the
   * ``pkmn-alert`` repository only. Stored as a Wrangler secret
   * (encrypted at rest, never present in this file or in git).
   *
   * Rotate every 90 days. When rotating, update via:
   *   ``wrangler secret put GH_TOKEN``
   */
  GH_TOKEN: string;
}

// Constants inlined rather than env-varred because these never change
// per-deployment; changing them would mean this heartbeat is pointing
// at a different bot entirely and deserves an explicit code edit.
const GH_OWNER = "akashmandole";
const GH_REPO = "pkmn-alert";
const WORKFLOW_FILE = "monitor.yml";
const BRANCH_REF = "main";

export default {
  /**
   * Cron-triggered entry point. Runs on the schedule declared in
   * ``wrangler.toml``. Any thrown error causes CF to log it and count
   * a failed invocation in the dashboard — we deliberately swallow
   * nothing here so those metrics are honest.
   */
  async scheduled(
    _event: ScheduledEvent,
    env: Env,
    ctx: ExecutionContext,
  ): Promise<void> {
    // ``waitUntil`` extends the worker's lifetime past the return of
    // this handler so the fetch actually completes even if the CF
    // runtime would otherwise shut us down as soon as scheduled()
    // returns.
    ctx.waitUntil(triggerWorkflow(env));
  },

  /**
   * Optional HTTP entry point so you can manually verify the heartbeat
   * from anywhere with curl:
   *
   *   curl -X POST https://<your-worker-subdomain>.workers.dev/fire
   *
   * Returns 200 + JSON on success, 502 + JSON on GitHub-side failure.
   * The root path returns a boring text greeting so bots crawling the
   * worker's public URL get a stable response.
   */
  async fetch(
    request: Request,
    env: Env,
    _ctx: ExecutionContext,
  ): Promise<Response> {
    const url = new URL(request.url);

    if (url.pathname === "/") {
      return new Response(
        "pkmn-alert heartbeat. POST /fire to trigger the monitor workflow manually.\n",
        { status: 200, headers: { "content-type": "text/plain" } },
      );
    }

    if (url.pathname === "/fire") {
      if (request.method !== "POST") {
        return new Response("Method Not Allowed. Use POST /fire.\n", {
          status: 405,
          headers: { "content-type": "text/plain" },
        });
      }
      const result = await triggerWorkflow(env);
      return new Response(JSON.stringify(result, null, 2) + "\n", {
        status: result.ok ? 200 : 502,
        headers: { "content-type": "application/json" },
      });
    }

    return new Response("Not Found\n", {
      status: 404,
      headers: { "content-type": "text/plain" },
    });
  },
};

/**
 * POST a ``workflow_dispatch`` for the monitor workflow. Returns a
 * structured result so callers (cron + manual /fire) can decide what
 * to do — cron just logs, /fire reflects the outcome to the HTTP
 * client.
 *
 * Success from GitHub is HTTP 204 (No Content). Anything else is
 * treated as failure and logged verbatim.
 */
async function triggerWorkflow(env: Env): Promise<{
  ok: boolean;
  status: number;
  detail?: string;
  firedAt: string;
}> {
  const firedAt = new Date().toISOString();

  if (!env.GH_TOKEN) {
    console.error(
      `[${firedAt}] refusing to fire: GH_TOKEN secret is unset. Run 'wrangler secret put GH_TOKEN' to seed it.`,
    );
    return {
      ok: false,
      status: 0,
      detail: "GH_TOKEN secret is unset",
      firedAt,
    };
  }

  const url = `https://api.github.com/repos/${GH_OWNER}/${GH_REPO}/actions/workflows/${WORKFLOW_FILE}/dispatches`;

  let resp: Response;
  try {
    resp = await fetch(url, {
      method: "POST",
      headers: {
        Accept: "application/vnd.github+json",
        Authorization: `Bearer ${env.GH_TOKEN}`,
        // Explicitly pinned so a future GitHub API version bump can't
        // silently change wire semantics under us.
        "X-GitHub-Api-Version": "2022-11-28",
        // GitHub requires a UA on all API requests; something
        // identifiable helps you find these requests in audit logs.
        "User-Agent": "pkmn-alert-heartbeat/1.0 (+https://github.com/akashmandole/pkmn-alert)",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ ref: BRANCH_REF }),
    });
  } catch (err) {
    // Network-level failure (rare; CF has excellent connectivity to
    // api.github.com). Report it so a persistent outage is visible.
    const detail = err instanceof Error ? err.message : String(err);
    console.error(`[${firedAt}] network error calling GitHub: ${detail}`);
    return { ok: false, status: 0, detail, firedAt };
  }

  if (resp.status === 204) {
    console.log(`[${firedAt}] workflow_dispatch fired ok`);
    return { ok: true, status: 204, firedAt };
  }

  // Read the body defensively — GitHub sometimes returns HTML for
  // gateway errors, so cap at 400 chars to keep logs sane.
  let body = "";
  try {
    body = (await resp.text()).slice(0, 400);
  } catch {
    body = "(unreadable body)";
  }
  console.error(
    `[${firedAt}] workflow_dispatch FAILED status=${resp.status} body=${body}`,
  );
  return { ok: false, status: resp.status, detail: body, firedAt };
}

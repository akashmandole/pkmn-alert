# pkmn-alert heartbeat (Cloudflare Worker)

A tiny Cloudflare Worker that fires every 5 minutes on Cloudflare's edge
cron and triggers the `pkmn-alert` monitor workflow via GitHub's
`workflow_dispatch` API.

Companion doc: `canvases/cron-reliability-options.canvas.tsx`.

## Why this exists

GitHub Actions scheduled workflows on free public repos are deprioritized.
The `pkmn-alert` monitor is configured for `*/5 * * * *` but currently
fires every 60–200 minutes, causing missed drops. Cloudflare's cron
triggers run at the edge with a hard SLA and no shared-scheduler
contention — using CF to trigger the workflow via `workflow_dispatch`
sidesteps the deprioritization while keeping the monitor code exactly
where it is.

The workflow's own concurrency group naturally dedupes overlap between
the CF trigger and the native GH cron.

## First-time setup

Total time: ~25 minutes end to end.

### 1. Create the Cloudflare account (5 min)

1. Go to <https://dash.cloudflare.com/sign-up>.
2. Sign up with any email. No credit card required.
3. Verify the email.

The Workers plan is `Free` by default. No plan upgrade needed.

### 2. Create the GitHub fine-grained PAT (5 min)

1. Go to <https://github.com/settings/personal-access-tokens/new>.
2. **Token name:** `pkmn-alert-heartbeat` (any label; this is just for your reference)
3. **Expiration:** `90 days` (or `Custom` if you want longer; rotation reminder is a feature, not a bug)
4. **Repository access:** `Only select repositories` → pick `pkmn-alert`
5. **Repository permissions:** scroll to `Actions` → set to `Read and write`.
6. Every other permission stays at `No access`. Least privilege.
7. `Generate token`. Copy the value shown once — you won't see it again.

The token starts with `github_pat_`. Keep it in your clipboard for step 4.

### 3. Install wrangler locally (2 min)

```bash
cd heartbeat/
npm install
```

That's it — `wrangler` and `@cloudflare/workers-types` are pinned in
`package.json` and land under `node_modules/`. No global install needed.

### 4. Authenticate with Cloudflare + seed the PAT (5 min)

```bash
# Opens a browser to log into your CF account. Grants wrangler a token
# scoped to your account. One-time step.
npx wrangler login

# Prompts for the value. Paste the github_pat_… token from step 2.
# Wrangler encrypts and stores it in Cloudflare's secrets vault; it
# is never written to disk and never appears in git.
npx wrangler secret put GH_TOKEN
```

### 5. Deploy (1 min)

```bash
npx wrangler deploy
```

The output prints your worker's public URL, e.g.
`https://pkmn-alert-heartbeat.<your-cf-subdomain>.workers.dev`.
Cron is now live.

### 6. Verify it's working (5 min)

Three ways to verify, in order of confidence:

**A. Manually fire and confirm GitHub responds:**

```bash
curl -X POST https://pkmn-alert-heartbeat.<your-cf-subdomain>.workers.dev/fire
```

Expected: `{"ok": true, "status": 204, "firedAt": "..."}`.

**B. Watch the worker logs in real time:**

```bash
npx wrangler tail
```

Leave it open. Within 5 minutes you should see a log line like
`[2026-07-22T18:00:00.000Z] workflow_dispatch fired ok`.

**C. Confirm the GitHub workflow ran:**

```bash
gh run list --repo akashmandole/pkmn-alert --workflow monitor --limit 3
```

The most recent run should show `event: workflow_dispatch` (not
`schedule`) and a `createdAt` close to when you fired it.

## Operating notes

### Rotating the PAT

Every 90 days GitHub emails a token-expiry reminder. To rotate:

```bash
# Generate a fresh token via the same flow as step 2 above.
npx wrangler secret put GH_TOKEN
# Paste the new value. Old value is atomically replaced; no downtime.
```

### Changing the cron cadence

Edit `wrangler.toml`'s `crons = [...]` line and redeploy. Cloudflare
Free supports up to 3 triggers per worker. Anything at or above
`*/3 * * * *` is still comfortably under the free-tier request ceiling
(currently ~288 firings/day at `*/5`; free limit is 100,000/day).

### Turning it off

```bash
npx wrangler delete
```

Removes the worker entirely. Cron stops firing immediately.
Alternatively, comment out the `[triggers]` block and redeploy to
keep the code deployed but silent.

### Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `wrangler tail` shows `status=401` | PAT expired or was regenerated | `wrangler secret put GH_TOKEN` with a fresh token |
| `status=404` | Repo renamed / URL constants wrong | Edit `worker.ts` constants at the top |
| `status=422` | Branch `main` was renamed | Update `BRANCH_REF` in `worker.ts` |
| No log lines at all | Cron might be paused in CF dashboard | Check `Cloudflare Dashboard → Workers & Pages → pkmn-alert-heartbeat → Triggers` |
| Log lines but no GH runs | Workflow disabled in GitHub | GitHub disables `workflow_dispatch` for workflows that haven't run in 60 days. Push any commit or re-enable in the Actions tab. |

## What this does NOT solve

Cron firing reliably is only half the equation:

- Reddit occasionally returns HTTP 403 to requests from GitHub-Actions
  IPs (~30% of the time). This heartbeat does not change the IP the
  fetch runs from. Fixing that means moving the fetch itself off
  GitHub Actions (full migration to Lambda/Worker + own state store).
- If the monitor workflow itself is broken (test failure, bad commit),
  the heartbeat will fire it just fine and the workflow will fail
  fast, same as before.

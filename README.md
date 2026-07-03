# pkmn-alert

A small, free, GitHub-Actions-native monitor that pings your phone the
second a Pokemon Center TCG drop goes live.

- **Free**: runs on GitHub Actions' free tier for public repos (unlimited
  minutes). No VPS, no proxies, no paid alerting service.
- **No new accounts**: default delivery is [ntfy.sh](https://ntfy.sh)
  (no signup — pick a secret topic, install the app, done). Gmail SMTP,
  carrier email-to-SMS, iOS Shortcuts webhooks, and native macOS banners
  are all wired up too.
- **Pluggable sources**: Reddit `r/PokemonRestocks` + `r/PKMNTCGDeals`
  (primary), Twitter/X via public Nitter mirrors, any RSS/Atom feed you
  point at it, and an **opt-in** direct queue-it detector that hits
  `pokemoncenter.com` with browser-impersonating TLS.
- **Add a phone in one line**: append a new entry to `subscribers.yaml`,
  commit, done. The next 5-minute tick picks it up.

## Table of contents

1. [How it works](#how-it-works)
2. [Quick start](#quick-start)
3. [Configuring subscribers](#configuring-subscribers)
4. [Configuring sources](#configuring-sources)
5. [About the direct queue detector](#about-the-direct-queue-detector)
6. [Deploying to GitHub Actions](#deploying-to-github-actions)
7. [Local development](#local-development)
8. [Design notes and anti-detection posture](#design-notes-and-anti-detection-posture)
9. [Legal / ToS](#legal--tos)

---

## How it works

```
   Sources                Deduper                Subscribers
   ┌──────────┐        ┌──────────┐        ┌──────────────────┐
   │ reddit   │─┐      │          │        │ me: ntfy+email   │
   │ nitter   │─┼──►   │ state.json ──►    │ friend: sms      │
   │ rss      │─┤      │  (24h    │        │ debug: stdout    │
   │ queueit  │─┘      │  seen)   │        └──────────────────┘
   └──────────┘        └──────────┘                 │
        ▲                                            ▼
        │                                     ┌──────────────┐
        └───────────── cron every 5 min ◄─────┤ GitHub Action│
                                              └──────────────┘
```

Every 5 minutes, GitHub Actions:

1. Pulls the repo (including `state.json`, the dedupe cache).
2. Fetches each enabled source (Reddit JSON, Nitter RSS, ...).
3. Normalises each item into a `DropEvent`.
4. Drops any event whose hash is already in `state.json`.
5. For each surviving event and each subscriber, runs the filter rules
   (region / retailer / kind / keywords / quiet hours / min confidence).
6. Sends via every configured channel on that subscriber.
7. Prunes the dedupe cache (24h TTL, 500 entries max).
8. Commits `state.json` back to the repo with `[skip ci]`.

## Quick start

```bash
git clone <this repo>
cd pokemon_center

# 1. Install deps (base only, no curl_cffi).
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Set up your subscriber list.
cp subscribers.example.yaml subscribers.yaml
# Edit subscribers.yaml — see "Configuring subscribers" below.

# 3. Set env vars for any secrets you referenced (${NTFY_TOPIC}, etc.).
export NTFY_TOPIC="pkmn-drops-$(openssl rand -hex 6)"
echo "Subscribe to https://ntfy.sh/${NTFY_TOPIC} on your phone."

# 4. First run — mark everything currently on Reddit as already-seen so
#    you don't get a flood of historical alerts.
python -m pkmn_alert --seed-only -v

# 5. Real run.
python -m pkmn_alert -v

# 6. Try dry-run to see what would go out without sending.
python -m pkmn_alert --dry-run -vv
```

## Configuring subscribers

`subscribers.yaml` is the *only* file you need to touch to add / remove
phones, emails, or devices. Each subscriber gets a list of `channels`
(where to send) and `filters` (what to send). Example:

```yaml
subscribers:
  - id: me
    enabled: true
    channels:
      - type: ntfy
        topic: "${NTFY_TOPIC}"
      - type: sms_gateway            # free carrier email-to-SMS
        number: "5551234567"
        carrier: "att"
        username: "${GMAIL_USER}"
        password: "${GMAIL_APP_PASSWORD}"
    filters:
      regions: [US]
      retailers: [pokemoncenter]
      kinds: [queue, restock, preorder]
      keywords: [tcg, booster, etb, "pokemon center", queue]
      min_confidence: 0.5
      max_per_day: 20
      quiet_hours: "23:00-06:00"     # muted during these hours except queues
    reminders_minutes: [15]          # follow-up push 15 min after the original
```

### Confidence labels

Every notification title is prefixed with a confidence tag so you can
tell at a glance how much to trust it:

| Prefix | Meaning |
| ------ | ------- |
| `[HIGH]` | Reddit fired AND the direct `queueit` probe saw the queue on `pokemoncenter.com` within the last 30 minutes. Confirmed. |
| `[MED]`  | Only Reddit / another derivative source fired. Probably real but not physically confirmed by us. |
| `[HIGH][REMIND #1]` | This is a **reminder** re-send whose label was re-evaluated at fire time — so a MED alert can naturally become a HIGH reminder if `queueit` confirmed the drop after the original push went out. |

### Reminder follow-ups

Set `reminders_minutes: [15]` (or `[10, 20]`, etc.) on a subscriber to
have the dispatcher queue N extra pushes for every real drop. Each
reminder is stored in `state.json` and fired at the START of the tick
that first crosses its due time. If you set `reminders_minutes: []` (or
omit it), you get one-and-done alerts like before.

Reminders that would fire more than 15 minutes past their intended time
are quietly dropped — better to miss a stale reminder than to get a
"surprise" alert about a drop that ended hours ago.

## Confidence gate: when do we actually probe Pokemon Center?

The `queueit` source doesn't hit pokemoncenter.com every tick. It's
**gated** on cross-source signal so we don't burn our request budget
(and Akamai's tolerance) on quiet ticks.

The gate opens when:

1. At least `gate_min_sources` distinct derivative sources (default: 1)
   have logged a fresh drop-shaped event within `gate_window_minutes`
   (default: 20), **OR**
2. We are inside a **probe burst** window opened by a previous
   confirmed queue detection (default: 30 min).

A "drop-shaped event" for gate purposes means the event's `kind` is one
of `{queue, restock, preorder}` AND the retailer is `{pokemoncenter,
unknown}`. Random deal/news posts or Target/Walmart posts do NOT open
the gate — otherwise we'd probe on every noisy Reddit post.

Tuning knobs live in `sources.yaml` under the queueit source's
`options`:

```yaml
  - id: queueit
    options:
      gate_min_sources: 1        # raise to 2 for stricter signal
      gate_window_minutes: 20    # shrink to 10 for tighter correlation
```

**Effective request rate against Pokemon Center**: on quiet days ~0
requests. During an active drop, one probe within 5 min of the first
Reddit post, then every 5 min for the following 30 min of the burst
window. Typical week ≈ 20–50 requests total, vs. the ~2,000 we'd send
if we probed every tick.

### Supported channels

| `type`         | What it does                                             | Setup cost |
| -------------- | -------------------------------------------------------- | ---------- |
| `ntfy`         | POST to ntfy.sh; instant push to iOS/Android/desktop app | 0 (recommended) |
| `email`        | SMTP send (e.g. Gmail app password)                      | 1× |
| `sms_gateway`  | Email → carrier gateway → SMS on your phone              | 1× (Gmail) |
| `webhook`      | Generic HTTP POST — perfect for iOS Shortcuts            | varies |
| `macos`        | Native `osascript` banner (only when running locally)    | 0 |
| `stdout`       | Print to stdout — good for `--dry-run` testing           | 0 |

### Available filters

| Field            | Meaning                                                          |
| ---------------- | ---------------------------------------------------------------- |
| `regions`        | Allow-list of region codes. Empty = all.                         |
| `retailers`      | Allow-list of retailer ids (`pokemoncenter`, `target`, ...).     |
| `kinds`          | Allow-list of `queue` / `restock` / `preorder` / `deal` / `news`. |
| `keywords`       | Case-insensitive substring match on the event title. Any-of.     |
| `min_confidence` | Drop events below this confidence (only `queueit` uses < 1.0).   |
| `quiet_hours`    | `"HH:MM-HH:MM"` in the configured timezone. `queue` bypasses it. |
| `max_per_day`    | (Cap enforced by dispatcher — future work.)                      |

### Adding a phone later

```yaml
  - id: friend
    enabled: true
    channels:
      - type: sms_gateway
        number: "5559876543"
        carrier: "verizon"
        username: "${GMAIL_USER}"
        password: "${GMAIL_APP_PASSWORD}"
    filters:
      kinds: [queue]      # only wants the highest-signal events
```

Commit. Done. No code changes.

## Configuring sources

See `sources.yaml`. Each source is independent — disabling one has no
effect on the others. Adding a second RSS feed is just another `- id:...`
block with `type: rss`.

The `keyword_hints` on each source are a **coarse pre-filter** so we don't
push obviously-irrelevant items through the pipeline. Subscribers still
apply their own `keywords` filter afterwards.

## About the direct queue detector

The `queueit` source is disabled by default. Here's what you're opting
into if you turn it on:

**What it does.** Every 5 minutes it makes exactly one HTTPS request to
`https://www.pokemoncenter.com/` using `curl_cffi` with `chrome131` /
`chrome124` / `chrome120` TLS impersonation (randomised per run). It
checks three signals:
1. Did the response redirect to a `queue-it.net` URL?
2. Did the response set a `QueueITAccepted` / `QueueITUnlockLink` cookie?
3. Does the response body contain any of the known Queue-it phrases
   (`"you're in the virtual queue"`, `"estimated wait time"`, ...)?

Any single signal fires the alert.

**What it does NOT do.**
- Doesn't try to bypass or auto-join the queue.
- Doesn't execute Queue-it's proof-of-work JavaScript.
- Doesn't run headless Chrome (too easy to fingerprint).
- Doesn't use proxies (GitHub Actions IPs are shared but generally clean).
- Doesn't poll faster than the cron interval.

**Trade-offs.**
- Pokemon Center's Terms of Use prohibit automated access. One request
  per 5 minutes is technically in violation but extremely low-risk
  compared to any commercial scraping stack.
- Akamai may still eventually blacklist the shared GitHub Actions IP
  range you land on. When that happens the source auto-cools-down for
  30 minutes on any 403/412/429 response.
- You'll need `pip install -r requirements-queueit.txt` (adds ~10MB for
  the native curl binary).

**When to turn it on.** If your Reddit + Nitter coverage is missing drops
(rare) or you want a second independent signal for confirmation.

## Deploying to GitHub Actions

1. Push this repo to GitHub as a **public** repo (so Actions minutes are
   free and unlimited). If you want it private, GitHub still gives free
   plans 2,000 min/mo — well within our budget of ~1 min × 12 runs/hr =
   288 min/day. You'll go over on a private repo; use public.

2. In **Settings → Secrets and variables → Actions**, add:

   | Secret               | Example value                          |
   | -------------------- | -------------------------------------- |
   | `NTFY_TOPIC`         | `pkmn-drops-a7Xk29`                    |
   | `GMAIL_USER`         | `you@gmail.com`                        |
   | `GMAIL_APP_PASSWORD` | 16-char app password from Google       |

   Reference them in `subscribers.yaml` as `${NTFY_TOPIC}` etc.

3. Commit `subscribers.yaml` (with `${…}` refs — no secrets in-repo).

4. First deploy: manually trigger the workflow from the Actions tab
   with `seed_only=true`. This marks the currently-visible Reddit posts
   as already-seen so you don't get a burst of historical alerts.

5. From then on the workflow runs on `*/5 * * * *`. `state.json` is
   committed back on each successful run with `[skip ci]`.

**Note:** GitHub will disable scheduled workflows after 60 days of repo
inactivity. Our state-commit-back pattern effectively keeps the repo
active as long as anything at all is happening.

## Local development

```bash
# Offline: run the pytest suite (uses saved fixtures, no network).
pip install pytest
pytest -v

# Online: one-shot, dry-run, most verbose.
python -m pkmn_alert --dry-run -vv

# Only run once, don't send anything, mark everything seen (safe first run).
python -m pkmn_alert --seed-only -v

# Same as CI would run (with 0–60s jitter to look human).
python -m pkmn_alert --jitter-max-s 60 -v
```

`state.json` in the repo root is the entire persistent state. Delete it
to reset.

### A note on local testing

Reddit's RSS endpoint and xcancel.com (our primary Nitter mirror) both
rate-limit *aggressively* per `(User-Agent, IP)` — often after just 2–5
requests from the same box. If you smoke-test more than once or twice in
a session your home IP will get 429'd or 403'd for anywhere from 15 min
to a few hours, and every subsequent local run will look broken.

**This is a local-only problem.** GitHub Actions runners get a fresh IP
from AWS/Azure's pool on every job, so the deployed monitor never sees
this. If a local run returns 0 events with `429`/`403` warnings, the
right response is to wait it out, use the pytest suite for iteration,
and trust the CI run.

## Design notes and anti-detection posture

**Why not just poll pokemoncenter.com directly and skip the community
sources?** Because their anti-bot stack is one of the toughest on the
internet — Akamai Bot Manager (JA3/JA4 TLS scoring, `sensor.js`
fingerprinting, `_abck` session-trust cookie, extension probe of 60
`chrome-extension://` URLs), Imperva/Incapsula as a secondary WAF,
DataDome for behavioural analysis, and hCaptcha on checkout. Even a
"pass the JA4 check with `curl_cffi`" approach can't produce a valid
`_abck` cookie without running Akamai's JS in a real browser. For a
personal-use monitor, aggregating already-observed signals from
communities that specialise in this is dramatically cheaper, more
reliable, and doesn't hammer the site.

**When the queueit source is enabled, what keeps us safe(r)?**
- `curl_cffi` matches Chrome's real TLS ClientHello and HTTP/2 SETTINGS
  frame — same JA3/JA4 as a real user's browser.
- One request per 5-minute tick (well below any per-IP rate limit).
- We randomise the impersonation target across `chrome131` / `chrome124`
  / `chrome120` so every request doesn't paint the exact same
  fingerprint.
- `Accept-Language` matches the impersonated Chrome; we deliberately do
  NOT override `User-Agent` (a Chrome-TLS + non-Chrome-UA mismatch is a
  strong bot signal).
- On any 403/412/429 we set a 30-minute cooldown for this source and
  keep the other sources running.
- Because we don't try to bypass, checkout, or preserve session state,
  we sidestep the vast majority of Akamai's escalating challenges.

**What we intentionally don't do.**
- No proxies. Rotating IPs at our low request rate would look *more*
  suspicious, not less.
- No headless browser. `navigator.webdriver`, the extension probe, and
  missing screen metrics all light up in headless Chrome.
- No storage_state, no session warming. We're a one-shot observer.

## Legal / ToS

- Reddit `.json` endpoints are provided by Reddit for programmatic use;
  we send a descriptive User-Agent per Reddit's Data API rules.
- Nitter re-hosts public tweets; we use only publicly-viewable timelines.
- The direct queue detector, when enabled, technically brushes against
  Pokemon Center's Terms of Use ("no robots, spiders, scrapers"). It is
  disabled by default and rate-limited to 1 request per 5 min.
- This tool is for **personal drop notifications**. Do not use it to
  operate a checkout bot, resell inventory, or aggregate signals for
  paid distribution.

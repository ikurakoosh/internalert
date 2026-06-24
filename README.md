# intern-alert

A tiny, free poller that watches company job feeds, filters to internships
matching your target roles, and pings you (Telegram/Discord) the moment a **new**
one appears. Runs on GitHub Actions — no server, no database.

- **Tier 1 — Public ATS feeds** (Greenhouse / Lever / Ashby): stable, near-zero maintenance.
- **Tier 2 — Big-tech portals** (Amazon / Google / Microsoft / Meta): best-effort
  adapters against each portal's internal API. They can break when a company
  changes its backend; the tool alerts you when that happens.
- **Safety net — LinkedIn saved searches** for the big-tech four, so you're never
  at zero coverage even if a Tier-2 adapter breaks.

---

## Spotify (your #1) — read this

Spotify's careers site (`lifeatspotify.com`) runs on **Lever**, so it's covered by
the stable Tier-1 path: `https://api.lever.co/v0/postings/spotify?mode=json`.

Two things to know:

1. **It's a hard-deadline, fall-only cycle.** Summer 2027 applications are expected
   to open ~**October–November 2026** and close hard around **early February 2027**.
   Late applications are not accepted. Put a calendar reminder for **early October
   2026** to confirm the window has opened. The poller handles detection; the
   reminder makes sure you're watching during the window.
2. **Intern roles are titled by area** ("2027 Summer Internship, Engineering & Data
   Science"), not "Product Manager Intern." So Spotify is configured to alert on
   **every** intern posting (`role_keywords: []` override) — you skim and pick.

---

## Setup (about 20 minutes)

### 1. Put these files in a GitHub repo
Create a repo (private is fine) and add everything in this folder, keeping the
`.github/workflows/poll.yml` path intact.

### 2. Create a Telegram bot (fastest, free, instant push)
1. In Telegram, message **@BotFather** → `/newbot` → follow prompts → copy the **bot token**.
2. Send any message to your new bot.
3. Get your **chat id**: message **@userinfobot**, or open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and read `chat.id`.

(Prefer Discord? Create a channel webhook and use `DISCORD_WEBHOOK_URL` instead.)

### 3. Add repo secrets
Repo → **Settings → Secrets and variables → Actions → New repository secret**:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `DISCORD_WEBHOOK_URL` *(optional)*

### 4. Verify your company tokens
Locally:
```bash
pip install -r requirements.txt
python intern_alert.py --dry-run
```
This prints matches and flags any source that errored. Fix any company whose
fetch failed (usually a wrong `token` — verify via the apply-URL method in
`config.yaml`'s header).

### 5. Seed (so you aren't blasted with the current backlog)
```bash
python intern_alert.py --seed
```
Records what's currently open without alerting. (If you skip this, the first
scheduled run seeds automatically.)

### 6. Turn it on
Commit and push. Go to the **Actions** tab, enable workflows if prompted. It now
runs every ~15 minutes and pushes only new matches. Use **Run workflow**
(workflow_dispatch) to trigger a run manually anytime.

### 7. Set the LinkedIn safety net (big-tech four)
For **Amazon, Google, Microsoft, Meta**: on LinkedIn, search the company +
"intern", open **Jobs**, set filters, and click **Create job alert** (or save the
search). This is laggy/noisy but cannot silently break — it's your guaranteed
backstop for the portals.

---

## Tuning

Edit `config.yaml`:
- `role_keywords` — titles must contain one of these (or `[]` to match all).
- `require_intern_term` / `intern_terms` — keep only internship postings.
- `exclude_keywords` — drop senior/FT noise.
- `allow_anywhere` — off by default (coverage first); set `false` + edit
  `locations` only if you get too many irrelevant geos.
- Per-company `filters:` block overrides the global filters for that company
  (that's how Spotify is set to catch everything).

---

## Capturing a portal endpoint (when a Tier-2 adapter returns nothing)

Big-tech portals load jobs via a background request you can copy in ~60 seconds:

1. Open the company's careers **search results** page in Chrome.
2. **DevTools** (F12) → **Network** tab → filter **Fetch/XHR**.
3. Type a search (e.g. "intern") so the page loads results.
4. Find the request that returns JSON full of jobs → right-click → **Copy → Copy as cURL**.
5. From the cURL, fill the company's `request:` block in `config.yaml`
   (`url`, `method`, `params`/`json_body`, any required `headers`) and set the
   `map:` paths to the JSON fields (`jobs_path`, `id`, `title`, `location`, `url`).
6. `python intern_alert.py --dry-run` to confirm.

If a portal blocks GitHub's datacenter IPs (403/empty from Actions but works
locally), rely on its LinkedIn saved search, or route it through a managed
scraping API. The tool will alert you if all sources fail, so you'll know.

---

## Reliability notes
- **Per-source isolation:** one broken feed never stops the others.
- **Fail-loud:** if every source fails, you get a one-off alert (max 1/hour).
- **GitHub** emails you when a scheduled workflow run fails.
- **Schedules pause after 60 days of repo inactivity** — the periodic state
  commits keep the repo active, so this won't bite you mid-season.
- Scheduled runs are **best-effort**; under load GitHub may delay a run a few
  minutes. Fine for this use case.

## Files
- `intern_alert.py` — the poller (adapters, filter, state, notify).
- `config.yaml` — companies + filters (the only file you regularly edit).
- `.github/workflows/poll.yml` — the 15-minute schedule.
- `requirements.txt` — `requests`, `PyYAML`.
- `state.json` — created on first run; committed back by the workflow.

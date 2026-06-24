#!/usr/bin/env python3
"""
intern_alert.py
===============
Polls company job feeds, filters to internships matching your target roles,
and pushes only NEW postings to Telegram / Discord.

Two source types:
  1. Public ATS feeds  (greenhouse | lever | ashby)  -> stable, documented.
  2. Custom JSON feeds (custom)                       -> for big-tech portals
     (Amazon, Google, Microsoft, Meta). Best-effort: undocumented and may
     change or block. The request + field mapping live in config.yaml so you
     can fix them without touching code. See README "Capturing a portal endpoint".

Design notes
------------
* No database, no server. State (already-seen job IDs) is a small JSON file
  that GitHub Actions commits back to the repo between runs.
* First run "seeds" silently: it records what is currently open WITHOUT
  alerting, so you only get pinged for genuinely new postings afterward.
* Per-source isolation: one broken feed never stops the others.
* Fails loud: if every source fails, you get an alert (rate-limited), so the
  system can't die silently.

Usage
-----
  python intern_alert.py            # normal run (seeds automatically on first run)
  python intern_alert.py --seed     # force re-seed: record current jobs, do NOT alert
  python intern_alert.py --dry-run  # fetch + filter + print matches; no alerts, no state write

Secrets come from environment variables (set as GitHub Actions secrets):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID      (recommended)
  DISCORD_WEBHOOK_URL                        (optional, alternative/additional)
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

# --------------------------------------------------------------------------- #
# Config / constants
# --------------------------------------------------------------------------- #

CONFIG_PATH = Path(__file__).parent / "config.yaml"
STATE_PATH = Path(__file__).parent / "state.json"
TIMEOUT = 25  # seconds per HTTP request

# A browser-like User-Agent reduces trivial bot blocks on the custom portals.
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}

SESSION = requests.Session()
SESSION.headers.update(DEFAULT_HEADERS)


@dataclass(frozen=True)
class Job:
    id: str
    title: str
    company: str
    location: str
    url: str
    ats: str

    @property
    def key(self) -> str:
        """Stable identity across runs."""
        return f"{self.company}:{self.id}"


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def dig(obj, path: str):
    """Walk a dotted path into nested dicts/lists. Returns None if missing.

    Examples:  dig(data, "data.jobs")  dig(item, "categories.location")
    """
    if not path:
        return obj
    cur = obj
    for part in path.split("."):
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
        if cur is None:
            return None
    return cur


def _stringify_location(loc) -> str:
    if loc is None:
        return ""
    if isinstance(loc, list):
        return ", ".join(str(x) for x in loc if x)
    if isinstance(loc, dict):
        # e.g. {"name": "..."} or address-like dicts
        for k in ("name", "location", "city"):
            if loc.get(k):
                return str(loc[k])
        return ", ".join(str(v) for v in loc.values() if v)
    return str(loc)


# --------------------------------------------------------------------------- #
# Fetchers (network) — kept separate from parsers so parsers are unit-testable
# --------------------------------------------------------------------------- #

def fetch_json(url: str, params: dict | None = None) -> dict | list:
    resp = SESSION.get(url, params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def fetch_custom(req: dict) -> dict | list:
    method = (req.get("method") or "GET").upper()
    url = req["url"]
    headers = dict(DEFAULT_HEADERS)
    headers.update(req.get("headers") or {})
    params = req.get("params") or None
    if method == "POST":
        resp = SESSION.post(
            url, headers=headers, params=params,
            json=req.get("json_body"), timeout=TIMEOUT,
        )
    else:
        resp = SESSION.get(url, headers=headers, params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


# --------------------------------------------------------------------------- #
# Parsers (pure functions: payload -> list[Job])
# --------------------------------------------------------------------------- #

def parse_greenhouse(data: dict, company: str) -> list[Job]:
    out = []
    for j in data.get("jobs", []):
        out.append(Job(
            id=str(j.get("id")),
            title=j.get("title", "") or "",
            company=company,
            location=_stringify_location(j.get("location")),
            url=j.get("absolute_url", "") or "",
            ats="greenhouse",
        ))
    return out


def parse_lever(data: list, company: str) -> list[Job]:
    out = []
    for j in data:
        cats = j.get("categories", {}) or {}
        out.append(Job(
            id=str(j.get("id")),
            title=j.get("text", "") or "",
            company=company,
            location=_stringify_location(cats.get("location")),
            url=j.get("hostedUrl", "") or "",
            ats="lever",
        ))
    return out


def parse_ashby(data: dict, company: str) -> list[Job]:
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        url = j.get("jobUrl") or j.get("applyUrl") or ""
        out.append(Job(
            id=str(j.get("id") or url),
            title=j.get("title", "") or "",
            company=company,
            location=_stringify_location(j.get("location")),
            url=url,
            ats="ashby",
        ))
    return out


def parse_custom(data, company: str, mapping: dict) -> list[Job]:
    """Generic parser driven by a field map in config.

    mapping keys: jobs_path, id, title, location, url, url_prefix
    each value (except url_prefix) is a dotted path into a single job item.
    """
    items = dig(data, mapping.get("jobs_path", "")) if mapping.get("jobs_path") else data
    if not isinstance(items, list):
        return []
    prefix = mapping.get("url_prefix", "") or ""
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        raw_url = dig(it, mapping.get("url", "")) or ""
        raw_url = str(raw_url)
        url = raw_url if raw_url.startswith("http") else (prefix + raw_url)
        jid = dig(it, mapping.get("id", ""))
        jid = str(jid) if jid is not None else url
        out.append(Job(
            id=jid,
            title=str(dig(it, mapping.get("title", "title")) or ""),
            company=company,
            location=_stringify_location(dig(it, mapping.get("location", "location"))),
            url=url,
            ats="custom",
        ))
    return out


def jobs_for_company(company: dict) -> list[Job]:
    """Dispatch by ATS type. Raises on network/parse error (caller isolates)."""
    ats = (company.get("ats") or "").lower()
    name = company["name"]
    token = company.get("token", "")

    if ats == "greenhouse":
        data = fetch_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs")
        return parse_greenhouse(data, name)
    if ats == "lever":
        data = fetch_json(f"https://api.lever.co/v0/postings/{token}", {"mode": "json"})
        return parse_lever(data, name)
    if ats == "ashby":
        data = fetch_json(f"https://api.ashbyhq.com/posting-api/job-board/{token}")
        return parse_ashby(data, name)
    if ats == "custom":
        data = fetch_custom(company["request"])
        return parse_custom(data, name, company.get("map", {}))
    raise ValueError(f"Unknown ats type '{ats}' for {name}")


# --------------------------------------------------------------------------- #
# Filtering
# --------------------------------------------------------------------------- #

def matches(job: Job, f: dict) -> bool:
    title = (job.title or "").lower()
    loc = (job.location or "").lower()

    role_kw = [k.lower() for k in f.get("role_keywords", [])]
    if role_kw and not any(k in title for k in role_kw):
        return False

    if f.get("require_intern_term", True):
        terms = [t.lower() for t in f.get("intern_terms", [])]
        if terms and not any(t in title for t in terms):
            return False

    excl = [x.lower() for x in f.get("exclude_keywords", [])]
    if excl and any(x in title for x in excl):
        return False

    if not f.get("allow_anywhere", True):
        locs = [l.lower() for l in f.get("locations", [])]
        if locs and not any(l in loc for l in locs):
            return False

    return True


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #

def _telegram_send(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        },
        timeout=TIMEOUT,
    )


def _discord_send(text: str) -> None:
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        return
    # Discord has no HTML; strip tags for a clean plain-text post.
    plain = (text.replace("<b>", "**").replace("</b>", "**")
                 .replace("</a>", ""))
    while '<a href="' in plain:
        start = plain.index('<a href="') + len('<a href="')
        end = plain.index('"', start)
        href = plain[start:end]
        plain = plain[:plain.index('<a href="')] + href + plain[plain.index(">", end) + 1:]
    requests.post(url, json={"content": plain[:1900]}, timeout=TIMEOUT)


def notify(text: str) -> None:
    """Send to whatever channels are configured. Never raises."""
    for fn in (_telegram_send, _discord_send):
        try:
            fn(text)
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] notify via {fn.__name__} failed: {exc}", file=sys.stderr)


def format_jobs(jobs: list[Job]) -> list[str]:
    """Return Telegram-HTML chunks (each < 4000 chars)."""
    header = f"\U0001F195 <b>New internship match{'es' if len(jobs) != 1 else ''} ({len(jobs)})</b>\n\n"
    lines = []
    for j in jobs:
        title = html.escape(j.title)
        company = html.escape(j.company)
        loc = html.escape(j.location) if j.location else "—"
        url = html.escape(j.url, quote=True)
        lines.append(f"\u2022 <b>{title}</b> — {company}\n  {loc} · <a href=\"{url}\">Apply</a>")

    chunks, buf = [], header
    for line in lines:
        if len(buf) + len(line) + 2 > 3900:
            chunks.append(buf.rstrip())
            buf = ""
        buf += line + "\n\n"
    if buf.strip():
        chunks.append(buf.rstrip())
    return chunks


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #

def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def save_state(state: dict) -> None:
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True))


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def run(seed: bool = False, dry_run: bool = False) -> int:
    config = yaml.safe_load(CONFIG_PATH.read_text())
    filters = config.get("filters", {})
    companies = config.get("companies", [])

    state = load_state()
    seen = set(state.get("seen", []))
    first_run = not STATE_PATH.exists() or not seen

    matched: list[Job] = []
    errors: list[tuple[str, str]] = []
    polled = 0

    for company in companies:
        name = company.get("name", "?")
        try:
            jobs = jobs_for_company(company)
            # Per-company filter overrides merge over the global filters.
            # (e.g. Spotify sets role_keywords: [] to alert on ALL intern roles.)
            company_filters = {**filters, **(company.get("filters") or {})}
            hits = [j for j in jobs if matches(j, company_filters)]
            matched.extend(hits)
            polled += 1
            print(f"[ok] {name}: {len(jobs)} postings, {len(hits)} match")
        except Exception as exc:  # noqa: BLE001
            errors.append((name, str(exc)))
            print(f"[err] {name}: {exc}", file=sys.stderr)

    # Deduplicate within this run.
    by_key = {j.key: j for j in matched}

    if dry_run:
        print(f"\n--- DRY RUN: {len(by_key)} matching postings ---")
        for j in sorted(by_key.values(), key=lambda x: (x.company, x.title)):
            print(f"  {j.company:12} | {j.title}  [{j.location}]  {j.url}")
        if errors:
            print(f"\n{len(errors)} source(s) errored: " + ", ".join(n for n, _ in errors))
        return 0

    new_jobs = [j for k, j in by_key.items() if k not in seen]

    if seed or first_run:
        # Record everything currently open; do not alert on the backlog.
        state["seen"] = sorted(by_key.keys())
        save_state(state)
        msg = (f"\u2705 Alert system armed. Seeded {len(by_key)} current postings "
               f"across {polled} sources. You'll be pinged only for NEW roles from now on.")
        print(msg)
        notify(msg)
        return 0

    if new_jobs:
        new_jobs.sort(key=lambda x: (x.company, x.title))
        for chunk in format_jobs(new_jobs):
            notify(chunk)
        print(f"[notify] sent {len(new_jobs)} new posting(s)")

    seen.update(by_key.keys())
    state["seen"] = sorted(seen)

    # Fail-loud: if EVERYTHING errored, alert (at most once per hour).
    if errors and polled == 0:
        last = state.get("last_error_alert")
        now = datetime.now(timezone.utc)
        recent = last and (now - datetime.fromisoformat(last)).total_seconds() < 3600
        if not recent:
            notify("\u26A0\uFE0F Intern-alert: ALL sources failed this run. "
                   "A token/endpoint may have changed — check the GitHub Actions log. "
                   "Lean on your LinkedIn saved searches until it's fixed.")
            state["last_error_alert"] = now.isoformat()

    save_state(state)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Internship posting alerter")
    ap.add_argument("--seed", action="store_true",
                    help="record current postings without alerting")
    ap.add_argument("--dry-run", action="store_true",
                    help="print matches; no alerts, no state changes")
    args = ap.parse_args()
    return run(seed=args.seed, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())

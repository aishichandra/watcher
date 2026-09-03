#!/usr/bin/env python3
"""Watch Bloomberg's /latest feed and alert Slack about new Bloomberg Exclusives.

Designed for a stateless CI runner: state lives in ``seen_urls.json`` next to this
file, and the caller (the GitHub Actions workflow) is responsible for persisting it
between runs. A URL is only banked once it has actually been fetched, so a failed
article fetch gets retried on the next poll instead of being silently swallowed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scrapling.fetchers import StealthyFetcher

HERE = Path(__file__).resolve().parent

LATEST_URL = "https://www.bloomberg.com/latest"
STATE_FILE = Path(os.environ.get("BBG_STATE_FILE") or HERE / "seen_urls.json")
ARTICLE_RE = re.compile(r"/news/(articles|features|newsletters)/")

SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
PROXY = os.environ.get("BBG_PROXY", "").strip() or None

# /latest sits behind a bot check that a headful browser gets past far more reliably.
# On Linux CI that means running the whole script under `xvfb-run -a`.
HEADLESS_LATEST = os.environ.get("BBG_HEADLESS_LATEST", "0") == "1"
HEADLESS_ARTICLE = os.environ.get("BBG_HEADLESS_ARTICLE", "1") == "1"

MAX_CHECKS = int(os.environ.get("BBG_MAX_CHECKS", "25"))
WORKERS = int(os.environ.get("BBG_WORKERS", "3"))
TIMEOUT_MS = int(os.environ.get("BBG_TIMEOUT_MS", "45000"))
RETRIES = int(os.environ.get("BBG_RETRIES", "2"))
RETENTION_DAYS = int(os.environ.get("BBG_RETENTION_DAYS", "14"))


SOLVE_CLOUDFLARE = os.environ.get("BBG_SOLVE_CLOUDFLARE", "0") == "1"


class ScrapeError(RuntimeError):
    """The listing page came back without anything that looks like a story."""


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc):%H:%M:%S}  {msg}", flush=True)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- fetching ---------------------------------------------------------------
def fetch(url: str, headless: bool = True):
    kwargs = dict(
        headless=headless,
        network_idle=True,
        timeout=TIMEOUT_MS,
        retries=RETRIES,
        solve_cloudflare=SOLVE_CLOUDFLARE,
    )
    if PROXY:
        kwargs["proxy"] = PROXY
    return StealthyFetcher.fetch(url, **kwargs)


def _html(page) -> str:
    html = getattr(page, "html_content", None)
    if html:
        return html
    body = page.body
    return body.decode(errors="ignore") if isinstance(body, (bytes, bytearray)) else (body or "")


def _meta(page, name: str):
    for sel in (f'meta[property="{name}"]', f'meta[name="{name}"]'):
        found = page.css(sel)
        if found:
            return found[0].attrib.get("content")
    return None


# --- state ------------------------------------------------------------------
def load_state() -> dict | None:
    """Map of url -> first-seen ISO timestamp. ``None`` means no state file yet."""
    if not STATE_FILE.exists():
        return None
    try:
        raw = json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, ValueError):
        log(f"! {STATE_FILE.name} is unreadable — treating this run as a priming run")
        return None
    if isinstance(raw, list):  # v1 format: a bare list of URLs
        return {url: _now() for url in raw}
    if isinstance(raw, dict):
        return dict(raw.get("seen", raw))
    return None


def save_state(seen: dict) -> None:
    """Persist state, dropping entries older than the retention window.

    /latest only ever surfaces the past day or so of stories, so pruning at two
    weeks can't resurrect anything as 'new'. Without it the file grows forever.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    kept = {}
    for url, first_seen in seen.items():
        try:
            if datetime.fromisoformat(first_seen) >= cutoff:
                kept[url] = first_seen
        except (TypeError, ValueError):
            kept[url] = _now()  # undated leftover from the v1 format
    payload = {"version": 2, "updated": _now(), "seen": dict(sorted(kept.items()))}
    STATE_FILE.write_text(json.dumps(payload, indent=1) + "\n")


# --- scraping ---------------------------------------------------------------
def scrape_latest(page) -> list[dict]:
    links = page.css("a.Latest_storyLink__80QVD")
    if not links:  # hashed class name changes on every Bloomberg redeploy
        links = page.css('a[href*="/news/"]')

    out, local = [], set()
    for link in links:
        href = link.attrib.get("href")
        if not href:
            continue
        if href.startswith("/"):
            href = "https://www.bloomberg.com" + href
        if href in local:
            continue
        local.add(href)
        headline = link.css("span::text").get() or getattr(link, "text", "") or ""
        out.append({"url": href, "headline": str(headline).strip()})
    return out


def is_bloomberg_exclusive(page) -> bool:
    flat = re.sub(r"[\s_-]+", "", _html(page).lower())
    return "bloombergexclusives" in flat


# --- Slack ------------------------------------------------------------------
def post_slack(payload: dict) -> bool:
    if not SLACK_WEBHOOK_URL:
        log("! SLACK_WEBHOOK_URL is not set — printing instead of posting")
        print(json.dumps(payload, indent=2), flush=True)
        return False

    body = json.dumps(payload).encode()
    for attempt in range(3):
        req = urllib.request.Request(
            SLACK_WEBHOOK_URL, data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp.read()
            return True
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="ignore")[:200]
            log(f"! slack {exc.code}: {detail}")
            if exc.code < 500 and exc.code != 429:
                return False
        except Exception as exc:  # network blip
            log(f"! slack post failed: {exc}")
        time.sleep(2 ** attempt)
    return False


def _pretty_time(iso: str | None) -> str:
    """Bloomberg's article:published_time, as something readable in Slack."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    return f"{dt.astimezone(timezone.utc):%b %-d, %H:%M UTC}"


def alert_exclusive(hit: dict) -> None:
    title = hit.get("title") or hit.get("headline") or hit["url"]
    snippet = (hit.get("snippet") or "").strip()
    published = _pretty_time(hit.get("published"))

    context = " · ".join(p for p in ["Bloomberg Exclusive", published] if p)
    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"🔴 *<{hit['url']}|{title}>*"},
        }
    ]
    if snippet:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": snippet[:600]}}
        )
    blocks.append(
        {"type": "context", "elements": [{"type": "mrkdwn", "text": context}]}
    )

    post_slack({"text": f"🔴 Bloomberg Exclusive: {title}", "blocks": blocks})


def alert_failure(message: str) -> bool:
    return post_slack(
        {
            "text": f"⚠️ Bloomberg watcher failed: {message}",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"⚠️ *Bloomberg watcher failed*\n```{message[:800]}```",
                    },
                }
            ],
        }
    )


# --- the check --------------------------------------------------------------
def check_once(prime: bool = False, dry_run: bool = False) -> list[dict]:
    seen = load_state()
    first_run = seen is None or prime
    seen = {} if seen is None else seen

    page = fetch(LATEST_URL, HEADLESS_LATEST)
    listed = scrape_latest(page)
    log(f"{LATEST_URL} -> {page.status}, {len(listed)} stories")

    if not listed:
        raise ScrapeError(
            f"no story links on {LATEST_URL} (status {page.status}) — "
            "bot check, or the selectors need updating"
        )

    if first_run:
        save_state({**seen, **{i["url"]: _now() for i in listed}})
        log(f"primed {len(listed)} URLs into {STATE_FILE.name} — no alerts on a priming run")
        return []

    new = [i for i in listed if i["url"] not in seen]
    non_articles = {i["url"] for i in new if not ARTICLE_RE.search(i["url"])}
    queue = [i for i in new if i["url"] not in non_articles][:MAX_CHECKS]
    log(f"{len(listed)} listed · {len(new)} new · opening {len(queue)}")

    def check(item: dict) -> dict:
        try:
            article = fetch(item["url"], HEADLESS_ARTICLE)
            return {
                **item,
                "title": _meta(article, "og:title") or item["headline"],
                "snippet": _meta(article, "og:description"),
                "published": _meta(article, "article:published_time"),
                "exclusive": is_bloomberg_exclusive(article),
                "ok": True,
            }
        except Exception as exc:
            log(f"   ! {item['url']} — {exc}")
            return {**item, "ok": False, "exclusive": False}

    checked: list[dict] = []
    if queue:
        with ThreadPoolExecutor(min(WORKERS, len(queue))) as pool:
            checked = list(pool.map(check, queue))

    # Bank only what we actually resolved; failures get retried next poll.
    stamp = _now()
    banked = {**seen}
    for url in non_articles:
        banked.setdefault(url, stamp)
    for c in checked:
        if c["ok"]:
            banked.setdefault(c["url"], stamp)
    save_state(banked)

    failures = [c for c in checked if not c["ok"]]
    if failures:
        log(f"   {len(failures)} article fetch(es) failed — they roll over to the next run")

    hits = [c for c in checked if c["exclusive"]]
    for h in hits:
        log(f"🔴 EXCLUSIVE  {h['title']}\n   {h['url']}")
        if not dry_run:
            alert_exclusive(h)
    if queue and not hits:
        log("   no exclusives among the new stories")
    return hits


def gha_output(**kv) -> None:
    """Hand values back to the workflow (no-op outside GitHub Actions)."""
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        return
    with open(out, "a") as fh:
        for key, value in kv.items():
            fh.write(f"{key}={value}\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--prime",
        action="store_true",
        help="Bank everything currently on /latest without alerting. Use to reset state.",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Do everything except post to Slack.",
    )
    args = ap.parse_args()

    try:
        hits = check_once(prime=args.prime, dry_run=args.dry_run)
    except Exception as exc:
        log(f"poll failed: {type(exc).__name__}: {exc}")
        alerted = False
        if not args.dry_run:
            alerted = alert_failure(f"{type(exc).__name__}: {exc}")
        # `alerted` lets the workflow skip its own failure ping and avoid double-posting.
        gha_output(alerted=str(alerted).lower())
        return 1

    gha_output(exclusives=len(hits))
    return 0


if __name__ == "__main__":
    sys.exit(main())

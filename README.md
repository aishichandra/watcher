# Bloomberg Exclusives watcher

Polls [bloomberg.com/latest](https://www.bloomberg.com/latest) on a schedule, opens
every story it has never seen before, and posts a Slack alert for the ones tagged
**Bloomberg Exclusives**.

This is the notebook watcher in `scrapling_bbg.ipynb` turned into something that runs
unattended: macOS notifications became a Slack webhook, and the `while True` loop became
a GitHub Actions cron.

| | |
|---|---|
| Script | [`bloomberg_watch.py`](bloomberg_watch.py) |
| Workflow | [`.github/workflows/bloomberg-exclusives.yml`](.github/workflows/bloomberg-exclusives.yml) |
| State | `seen_urls.json`, committed back to the repo after each run |
| Cadence | every 5 minutes (GitHub's cron minimum — see Timing below) |

## Setup

This directory has to be the **root of its own repository** — the workflow file only
runs if it sits at `.github/workflows/` at the top level.

```bash
cd blmg_automation
git init && git add . && git commit -m "Bloomberg exclusives watcher"
gh repo create bloomberg-watcher --private --source=. --push
```

Then add the webhook as a repository secret:

```bash
gh secret set SLACK_WEBHOOK_URL   # paste the https://hooks.slack.com/services/... URL
```

Create the webhook itself at <https://api.slack.com/apps> → your app → **Incoming
Webhooks** → *Add New Webhook to Workspace*, and pick the channel you want alerts in.
The webhook URL is a bearer credential — it only belongs in the repo secret, never in a
committed file.

Finally, check **Settings → Actions → General → Workflow permissions** is set to
*Read and write*. The run commits `seen_urls.json` back, and without write access every
run would re-check the same stories.

### First run

Trigger it by hand before trusting the cron:

```bash
gh workflow run bloomberg-exclusives.yml -f dry_run=true   # scrape, print, post nothing
gh run watch
```

If `seen_urls.json` is missing or stale, run it once with `-f prime=true` to bank
everything currently on `/latest` without alerting. Otherwise the first real run
would fire an alert for every exclusive of the past day at once.

## Secrets and settings

| Name | Where | Required | Purpose |
|---|---|---|---|
| `SLACK_WEBHOOK_URL` | repo secret | yes | Where alerts go |
| `BBG_PROXY` | repo secret | no | `http://user:pass@host:port`, if GitHub's IPs get blocked |
| `BBG_MAX_CHECKS` | workflow env | no | Article fetches per run (default 25); the rest roll over |
| `BBG_WORKERS` | workflow env | no | Concurrent browsers (default 3) |
| `BBG_RETENTION_DAYS` | workflow env | no | How long a URL stays in state (default 14) |
| `BBG_HEADLESS_LATEST` | workflow env | no | `1` to drop Xvfb and run `/latest` headless |
| `BBG_SOLVE_CLOUDFLARE` | workflow env | no | `1` only if Bloomberg ever moves behind Cloudflare (see below) |

Leave `BBG_SOLVE_CLOUDFLARE` off. Bloomberg's bot gate is not Cloudflare, and
Scrapling's solver spends a fixed ~60s per fetch looking for a challenge that is never
there — enough to push a full 25-article run past the job timeout.

## Timing

Measured against live Bloomberg: the `/latest` fetch takes ~10s, and articles come in
batches of `BBG_WORKERS` at roughly 65s a batch (each fetch launches its own browser).
A steady-state run with a handful of new stories lands in 2-3 minutes. A worst-case run
that has 25 articles to work through takes ~10 minutes, against a 25-minute job timeout.

The workflow is set to `*/5 * * * *` — GitHub's minimum cron granularity, and the
tightest polling interval this host can offer. In practice a `*/5` schedule fires more
like every 5-15 minutes: GitHub documents scheduled workflows as best-effort and delays
or drops runs when the queue is busy, and `concurrency: cancel-in-progress: false` means
a run that's still going (e.g. a worst-case 10-minute batch) makes the next trigger wait
rather than overlap. If you need alerts closer to real 5-minute latency than GitHub can
promise, run `bloomberg_watch.py` on a schedule on a machine you control instead (cron/
launchd) — see "Running it locally" below.

## Running it locally

```bash
pip install -r requirements.txt
scrapling install
export SLACK_WEBHOOK_URL=...
python bloomberg_watch.py --dry-run
```

On Linux, wrap it in `xvfb-run -a` the way the workflow does. On macOS a real browser
window will open for the `/latest` fetch — that is deliberate.

To run it every 5 minutes locally instead of via GitHub Actions, use `launchd`
(`StartInterval 300` in a plist under `~/Library/LaunchAgents`) or `cron`
(`*/5 * * * * cd /path/to/blmg_automation && python bloomberg_watch.py`). Local runs get
real 5-minute timing and a residential IP (less likely to get bot-gated than GitHub's
datacenter runners), at the cost of only running while the machine is on and awake.

## How it decides something is an exclusive

`/latest` gives headlines and URLs but not the exclusives badge, so every unseen story
gets opened and its HTML flattened (whitespace, hyphens and underscores stripped) and
searched for `bloombergexclusives`. That matches the badge whether Bloomberg ships it as
`Bloomberg Exclusives`, `bloomberg-exclusives`, or a hashed CSS class built from it.

A URL is only written to `seen_urls.json` once it has actually been fetched, so an
article that fails behind a bot check is retried next run rather than silently skipped.

## When it breaks

Both failure modes reach the same Slack channel:

- **The scrape failed** — the script posts the exception text. Most likely a bot check
  (GitHub's runner IPs are datacenter ranges that Bloomberg rate-limits), which is what
  `BBG_PROXY` is for.
- **The run failed** — the workflow posts a link to the run log. Dependency installs,
  the 25-minute timeout, a state push that lost three races.

An alert for *every* story usually means the state file was lost — re-run with
`prime=true`. No alerts at all for a day, with green runs, usually means Bloomberg
renamed the badge; check a known exclusive by hand and adjust `is_bloomberg_exclusive`.

### Selectors

`scrape_latest` looks for `a.Latest_storyLink__80QVD` and falls back to
`a[href*="/news/"]` when that hashed class name changes on a Bloomberg redeploy. The
fallback is deliberately loose — non-article URLs get filtered by `ARTICLE_RE` and are
banked without being opened.

## Caveats

- GitHub's cron is best-effort. A `*/5` schedule really means "roughly every 5 minutes,
  sometimes considerably more." If you need tighter, guaranteed latency than that, this
  is the wrong host — run locally instead (see above).
- Scheduled workflows are disabled automatically after 60 days without repository
  activity. The state commits count as activity, so this stays alive on its own — but
  only while it is succeeding.
- Every run commits to the default branch. The history is noisy by design; that noise is
  what keeps the state durable.

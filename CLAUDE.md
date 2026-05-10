# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> Authoritative context lives in **HANDOFF.md**. Read it before any non-trivial change — it documents the runtime, state machine, deployment chain, known bugs, and TODOs. This file is the index.

---

## What this repo is

A distributed Twitter (X) "matrix" automation system. One **中控 (master)** VPS runs a Streamlit control panel + scheduler daemon and orchestrates many **worker** VPSes that each drive a single X account via Playwright Chromium. Master and workers communicate by SFTP-pushed/pulled JSON files over SSH (paramiko) — no message queue, no API server.

Comments, UI strings, and identifiers are in Chinese. Keep that convention.

## Code layout — what's live vs. legacy

```
推特矩阵系统/
├── master/                  ← V3 matrix master code (deploy: /opt/twitter-matrix/master/)
│   ├── app.py               Streamlit entrypoint + password gate
│   ├── daemon.py            Background scheduler/sync loop (separate systemd unit)
│   ├── database.py          SQLite schema + idempotent _migrate() on every init_db()
│   ├── sync.py              Master ↔ worker file protocol (push_config/tasks, pull state/log)
│   ├── scheduler.py         plan_today_for_worker() — work-window + random rest spacing
│   ├── ssh_client.py        paramiko wrapper with banner probing
│   ├── scoring.py / filter.py   Video quality scoring + filtering for crawled pool
│   ├── crawler/             Site-specific scrapers (jable_crawler, hanime1_crawler) + translator
│   └── pages/               Streamlit multipage (filenames carry emoji — Streamlit naming convention)
├── worker/                  ← V3 matrix worker code (deploy: /opt/twitter-worker/)
│   ├── worker.py            Main loop (systemd); reads config.json/tasks.json/cmd.json, writes state.json/log.txt
│   ├── tweet_engine.py      yt-dlp + m3u8 sniff + Playwright post-tweet flow
│   ├── popup_handler.py     X.com popup rule library + @combobox handling
│   ├── socks_relay.py       gost relay for chromium SOCKS5-auth workaround
│   └── traffic.py           vnstat monthly traffic
├── deploy/                  install_master.sh, upload_to_master.sh
├── tests/                   pytest suite for master/ modules
├── HANDOFF.md               ← canonical project doc (read this first)
│
├── auto_tweet_engine.py     ← V2.0 LEGACY single-machine code at repo root
├── main_app.py              ← V2.0 LEGACY (do not edit unless explicitly asked)
├── database.py              ← V2.0 LEGACY (separate schema from master/database.py)
└── tweet_matrix.db          ← V2.0 LEGACY db
```

**Do not edit the root-level `auto_tweet_engine.py` / `main_app.py` / `database.py`** unless the user explicitly asks. They are the V2.0 single-machine predecessor and are unrelated to the V3 matrix code in `master/` + `worker/`. The matrix system uses `master/database.py` (different schema, in `master/data/matrix.db`).

## Common commands

```bash
# Run tests (from repo root). conftest.py adds master/ to sys.path automatically.
pytest -q                              # full suite
pytest tests/test_scheduler.py -q      # one file
pytest tests/test_scoring.py::test_basic_score -q   # one test

# Local Streamlit (master only — needs deps; password env required)
TWMATRIX_UI_PASSWORD=dev streamlit run master/app.py

# Local scheduler daemon
python master/daemon.py

# Try a crawler standalone (outputs JSON to stdout)
python -m master.crawler.jable_crawler
python -m master.crawler.hanime1_crawler

# Install master deps
pip install -r master/requirements.txt
# Install worker deps (only useful on a worker VPS or for local Playwright tests)
pip install -r worker/requirements.txt && playwright install chromium
```

> The root-level `requirements.txt` is a local draft and **gitignored** (see `.gitignore` line `/requirements.txt`). Real deployment installs from `master/requirements.txt` or `worker/requirements.txt`.

## Deployment chain — read this before pushing

```
Local edit → git push origin main
   → 中控 VPS:   cd /opt/twitter-matrix && git pull && systemctl restart twmatrix-ui twmatrix-daemon
   → Web UI → Workers card → "🔄 更新代码"  (paramiko-driven: pushes worker/*.py + restarts twitter-worker)
```

- China-local → overseas SSH is blocked by GFW. **Do not scp/rsync** to workers; rely on the GitHub-mediated chain above.
- A `git push` only updates master once you run `git pull` on the master VPS; worker code only updates after the Workers-page "更新代码" button is clicked.
- `paramiko<4` is a hard constraint (4.x has SSH banner-timeout regressions).

## Architectural anchors

- **Master ↔ Worker protocol is files, not RPC.** Master writes `config.json` / `tasks.json` / `cmd.json` over SFTP; worker writes `state.json` / `log.txt` / `screenshots/`. Daemon does incremental log pulls via `tail -c +N` keyed on `workers.last_log_offset`. See HANDOFF.md §"中控 ↔ Worker 文件协议".
- **Task state machine:** `pending → scheduled → running → done|failed|human_required`. `human_required` is set by worker when it sees an unknown overlay and snapshots a screenshot; resolving it is a UI action that flips a `cmd.json` resume command.
- **Merge semantics on task push are subtle.** `push_tasks` reads the worker's current `tasks.json` and merges — terminal states (`done`/`failed`/`human_required`) written by the worker must NOT be overwritten by master's view. Past regression.
- **DB writes are mutex-locked** (`_db_write_lock` in `master/database.py`) because the daemon's `ThreadPoolExecutor` syncs many workers concurrently. SQLite is in WAL mode; reads concurrent, writes serialized.
- **Schema migrations** are idempotent column-adds in `_migrate()`, run on every `init_db()`. To add a column, edit `SCHEMA` and add a guarded `ALTER TABLE` line in `_migrate()`.

## Non-obvious gotchas (don't relearn these)

- **Chromium does not support authenticated SOCKS5.** Workers run `gost` as a local relay (`127.0.0.1:11080` no-auth → upstream residential w/ auth). `worker/socks_relay.py` starts/stops it per task. Playwright's `proxy.auth` works for firefox/webkit only.
- **Never press Escape to close X overlays.** X's compose modal also listens for Escape and will close itself. `popup_handler.dismiss_all_overlays` sweeps overlays by selector only; do not add an Escape key press.
- **Disabled-button waits use `:not([disabled])`, not `aria-disabled='false'`.** X's tweet button updates the real `disabled` attribute; aria-disabled lies. Don't use `force=True` clicks to "fix" a button that looks ready — it submits a half-loaded video.
- **Playwright strict mode bites:** every locator that could match >1 element needs `.first`. The "tweetTextarea_0 resolved to 2 elements" error has happened.
- **Video pipeline:** sites like `jable.tv` and `hanime1.me` need m3u8 sniffing via Chromium because yt-dlp refuses them (Piracy list / Cloudflare). The flow is: sniff m3u8 → feed URL to yt-dlp; only fall back to yt-dlp-direct (with `impersonate=chrome-124`) if sniff fails. See `worker/tweet_engine.py`.
- **Adding a new video source = no code change** unless yt-dlp blacklists it. `_sniff_m3u8` tries every URL.
- **`@` parsing for mentions** uses `_is_ident_continuation` (ASCII-only); Chinese chars adjacent to `@` must not extend the handle. Don't "fix" this with `\w+`.
- **Worker code changes require the "🔄 更新代码" button** after merge. `git pull` only updates master; workers won't auto-sync.

## Database & data files

- Live DB: `master/data/matrix.db` (gitignored). Tests use a tmp DB via the `temp_db` fixture in `tests/conftest.py` — never run tests against the live file.
- `master/crawl_test*.json` and `master/hanime1_titles*.json` are crawler debug dumps, also gitignored.
- Screenshots from worker alerts land in `master/data/screenshots/`.

## When in doubt

- HANDOFF.md §五 (已修复 bug) lists past regressions with their fixes — check it before "fixing" something that smells familiar.
- HANDOFF.md §九 (给下一位维护者的建议) is the short list of foot-guns; treat it as binding.
- Credentials (VPS IPs, SSH passwords, UI password, cookies, 2FA secrets, proxy auth) are not in the repo. If a task needs them, ask the user.

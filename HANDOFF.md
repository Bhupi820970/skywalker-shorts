# Skywalker Shorts: handoff

This repo makes movie and anime scene Shorts for the YouTube channel **Skywalker** (@skywalker-k3i). It posts 8 a day at IST slots.

- **GitHub Actions** does everything heavy: planning, rendering, QA, health checks and posting.
- **Box downloader** (one always-on Linux or Mac computer) downloads official scene clips and music with yt-dlp, because GitHub's IPs get bot-checked. It uploads them to the repo's releases.
- The box never renders, and never runs code from issues or AI output.

Written 2026-11-26 (IST). All times are IST.

---

## 1. Box downloader (a loop, NOT cron)

There are **no cron lines** for this repo. The box runs two background loops:

| Loop | What it does |
|---|---|
| `scripts/box_service.sh start` | Every 5 min (`SKY_BOX_TICK`): `box_service.py sync`, which fast-forwards the clone to `origin/main` and keeps the old code if the new code doesn't compile. Then `box_service.py cycle`, capped at 6 h. |
| `scripts/box_watchdog.sh start` | Every 10 min: restarts the service loop if it died. It also adds a shell-start hook to `~/.profile`, `~/.bashrc` and the Linux desktop autostart, so the loops come back after a reboot. |

Commands (run inside the repo clone):

```
bash scripts/box_watchdog.sh start       # start everything (watchdog starts the service)
bash scripts/box_watchdog.sh status      # is it running + service status
bash scripts/box_service.sh logs 100     # last 100 log lines
bash scripts/box_watchdog.sh uninstall   # remove the start hooks AND stop both loops
```

If you'd rather use cron than loops, this one line does the same job as the watchdog:

```
*/10 * * * * bash <REPO>/scripts/box_service.sh start >/dev/null 2>&1
```

Run it on ONE computer only. Two boxes would download and commit the same clips twice.

### What one `cycle` does (`scripts/box_service.py`)
- Reads the clip requests (`config/clip_requests.json`, `config/scene_requests.json`, `config/refill_requests.json`).
- Downloads the official clips with yt-dlp (`scripts/clip_fetch.py`), checks them for logos and overlays (Gemini logo vet), and uploads them to the `sources` / `clips` releases.
- Commits `config/clip_fetch_status.json`.
- Restores any `music/library.json` track that is missing on the `music` release.
- Writes `box_heartbeat.json`.

## 2. Box files (folder `box/`)

| File | Purpose |
|---|---|
| `box/env.sh` | Shared settings for both loop scripts, on Linux or macOS. It reads `box.env` as plain `KEY=value` lines (parsed, never executed) from the box home, or from `~/.config/skywalker/box.env`. The box home defaults to the folder that holds the repo clone. |
| `box/box.env.example` | Template for `box.env`: the gh login folder, the browser profile with the YouTube browser login for yt-dlp, and deno. It holds no secrets. |
| `box/setup_box.sh` | One-time setup: checks the tools, creates a venv in the box home, installs `box/requirements-box.txt`, and copies `box.env`. |
| `box/requirements-box.txt` | Python packages for the box. Rendering uses `requirements.txt` on GitHub. |
| `box/sky_oauth.py` | Gets a new YouTube refresh token for the Skywalker channel and stores `SKY_YT_CLIENT_ID`, `SKY_YT_CLIENT_SECRET` and `SKY_YT_REFRESH_TOKEN` as GitHub secrets. Values go to `gh secret set` over stdin and are never printed. Usage: `python3 box/sky_oauth.py url`, then `python3 box/sky_oauth.py exchange '<localhost URL>'`. It needs the Google "Desktop app" OAuth client JSON at `~/.config/skywalker/sky_oauth_client.json` (or `SKY_OAUTH_CLIENT`). Never commit that JSON. |

Box data folder (the box home, outside git):
- `box_heartbeat.json`: the last cycle, clip and music results.
- `state.json`
- `box_service.log`, `box_watchdog.log`
- `music_cache/`, `work/`
- `box.env`

## 3. GitHub workflows

| Workflow | When it runs | What it does |
|---|---|---|
| `render.yml` | :20 / :50 every hour, and on dispatch | Renders queued items from release clips only (GitHub never downloads from YouTube or X). |
| `auto-retry.yml` | :25 / :55 | Re-dispatches items left in `retry_queued`. |
| `publish.yml` | :10 / :40 | Posts every render that passed the gate, scheduled for its IST slot. It uses the YouTube Data API if the `SKY_YT_*` secrets exist, otherwise Buffer. Idempotent through `results/<id>.json`. |
| `health.yml` | :05 / :35 and :19 / :49 | Checks the next 24 h of slots are covered and assigns ready stock to missed or imminent slots. |
| `slot-guardian.yml` | Every 15 min, plus before each slot | Confirms each official slot has a post. |
| `refill.yml` | 06:11 / 12:11 / 18:11 / 00:11 | Automatic footage refill: writes scene requests for the box. |
| `clips.yml` | 08:47 daily | Gemini picks the official scene clips needed (`config/clip_requests.json`). |
| `discover.yml` | 10:07 daily | Gemini and yt-dlp search for official clips. |
| `source-ingest.yml` | 09:00 daily | Downloads official free trailers and clips from Archive.org, Dailymotion, Vimeo and similar sites. |
| `music-ingest.yml` | Dispatch | Grows `music/library.json` from legal CC and public-domain sources. |
| `stats.yml` | 08:47 daily | Per-video views and likes, saved to `stats/YYYY-MM-DD.json`. |
| `trends.yml` | Every 3rd day, ~06:53 | Gemini studies trending edit Shorts and our stats. |
| `link.yml` | Dispatch | Turns an official YouTube or X clip URL into a queued Short. |
| `buffer-status.yml` | Manual | Shows recent Buffer posts and the daily limit. |
| `yt-auth-check.yml` | Manual | Checks the `SKY_YT_*` secrets (prints only the channel title and handle). |
| `ci.yml` | Push | Tests. |

### GitHub Secrets (names only)
- `SKY_YT_CLIENT_ID`, `SKY_YT_CLIENT_SECRET`, `SKY_YT_REFRESH_TOKEN`: direct YouTube upload.
- `BUFFER_API_KEY`: Buffer fallback.
- `GEMINI_API_KEY`, `GEMINI_API_KEY_2` … `GEMINI_API_KEY_7`
- `YOUTUBE_API_KEY`: stats.
- `YTDLP_COOKIES`: optional, for runner yt-dlp.

Repo variables: `SKY_AUTO_PUBLISH`, `SKY_DIALOGUE_EDIT`.

## 4. Where things live

| What | Where |
|---|---|
| Daily plan / queue | `queue/YYYY-MM-DD.json` |
| Render results and post ids | `results/<slot>-<title>.json` (`yt_video_id` / `buffer_post_id`) |
| Slots | `config/slots.json`: 8 a day, 06:30, 09:00, 11:30, 13:30, 16:00, 18:30, 20:30, 22:30 IST |
| Posting settings | `config/publish.json` (Buffer channel, daily limit, late-post window) |
| Already-posted list (no repeats) | `config/posted_history.json` |
| Clip requests / status | `config/clip_requests.json`, `config/scene_requests.json`, `config/refill_requests.json`, `config/clip_fetch_status.json`, `config/refill_status.json` |
| Backup stock | `config/backup_stock.json`, `config/backup_pool.json`, and release `backup-stock` |
| Health | `config/health_status.json` |
| Releases | `sources` and `clips` (box clips), `scenes`, `music` (library audio), `final` (renders), `backup-stock`, `test-renders` |
| Music library | `music/library.json`, plus `config/music_blocklist.json` and `config/music_denylist.json` |
| Stats / trends | `stats/`, `trends/` |

## 4b. No clip repeats (clip ledger, 2026-11-26)
- `clip_reserve.py` keeps `clip_reservations.json` on branch **`clip-state`** (clip names only, no media).
  Every write goes through the GitHub contents API with the file sha, so it is a compare-and-swap;
  a conflict is retried with backoff. Pushes to `clip-state` trigger no workflow (all push triggers are `main`).
- `scripts/render_item.py` reserves each scene clip the moment it picks it (`download_scene_bundle`),
  so the parallel matrix renders (max-parallel 4) can never share a clip. When the Short renders
  (`done`, or uploaded and waiting on QC) its clips are marked `used` for good; on failure they are
  released. A reservation older than 8 h (killed job) expires. If the ledger cannot be reached the clip
  is skipped, never risked.
- `posted_history.blocked_clips(reserved=True)` (every render, refill count, stock, clip fetch) also
  treats ledger clips and clips of rendered-but-unposted Shorts as gone, so the fresh-clip count is honest.
- Seeded with the 82 clips already in posted (67), rendered or stocked Shorts. Tests: `tests/test_clip_reserve.py`.
- A re-render of the same video id may reuse its own clips; no other video can.

## 5. Setting up a new computer

1. Install `git`, `python3`, `ffmpeg`, `gh` and `deno`. On macOS, also run `brew install coreutils`.
2. Make a folder for the box, e.g. `~/skywalker-box`. Then:
   ```
   cd ~/skywalker-box
   git clone https://github.com/Bhupi820970/skywalker-shorts.git repo
   cd repo
   ```
3. Run `gh auth login` (repo and workflow scopes).
4. Run `bash box/setup_box.sh`, then edit `~/skywalker-box/box.env`. Point `SKY_YTDLP_BROWSER` at a Chrome profile that is logged in to YouTube.
5. Stop the old box first (`bash scripts/box_watchdog.sh uninstall` there). Then run `bash scripts/box_watchdog.sh start` on the new one.

## 6. Known issues (2026-11-26)

- **Clip fetching (fixed on 2026-11-26).** At 20:48 IST, 65 of 66 needed clips failed. The YouTube login and bot check were NOT the cause: the browser login worked, and downloads succeeded. There were two real causes.
  1. **Wrong format pick plus a strict bitrate check.** The downloader preferred VP9 streams, about 1000 kbps at 1080p. The quality gate was tuned for H.264 (1300 kbps needed), so it threw away 48 good official 1080p clips in one cycle, even though 2500–3600 kbps H.264 1080p was available.
     - Fix: pick the highest resolution, then H.264, then the highest bitrate.
     - The bitrate gate now depends on the codec (VP9 needs 0.65×, AV1 0.55×, HEVC 0.7× of H.264).
     - Tested on a clip that was rejected before: it now downloads at 1920x1080 H.264, 2496 kbps, and passes.
  2. **Dead original links.** About 59 of the needed clips point at Crunchyroll uploads, which burn in a logo and subtitles, so our no-logo rule always skips them. A few others were private or removed. They were retried every cycle and counted as fresh failures.
     - Fix: such clips are marked dead for 14 days (`dead_assets` in the box `state.json`), and the box only searches for another official clip of the title.
     - Download errors are now logged with a short reason.
     - YouTube search uses 20 results per query (was 10), with a pause between searches and between requests so the IP isn't throttled.
- **Clip pool rule.** `refill.yml` now runs every 2 h. It asks for new titles as soon as fresh, unposted clips drop to **50 or below** (target about **100**; settings `REFILL_MIN_CLIPS` and `REFILL_TARGET_CLIPS`), on top of the old 7-days-of-slots rule.
  - The box loop (every 5 min) fetches the requests.
  - Errors are retried up to 3 times per clip per day.
  - The watchdog restarts a dead loop every 10 min.
- **Anime titles that only Crunchyroll uploads** can't be used under the no-logo rule. The refill swaps in titles from logo-free official channels (Netflix, Netflix Anime, TOHO animation, Aniplex USA, GKIDS, publishers).
- **What a new box needs for YouTube:**
  - a Chrome/Chromium profile signed in to YouTube (any normal account) and set in `box.env` as `SKY_YTDLP_BROWSER=chrome:<profile folder>`. yt-dlp reads it in place, and nothing from it is ever copied, logged or committed;
  - `deno` (yt-dlp needs it for 1080p+ formats);
  - an up-to-date yt-dlp: run `python3 -m pip install -U yt-dlp` weekly;
  - a home or office internet connection (data-centre IPs get bot-checked).
- **Music is fine:** all 377 library tracks are on the `music` release.
- **Buffer limit:** the Buffer Free plan allows only **2 YouTube Shorts per 24 h**. Direct YouTube upload (`SKY_YT_*` secrets) avoids that limit. If uploads fail with an auth error, re-run `box/sky_oauth.py`.
- **Render default path:** `pipeline.py` defaults `SKY_ROOT` to an old fixed path. Set `SKY_ROOT` when you render locally (`scripts/local_render.sh`).
- **Start hooks:** the watchdog re-adds its start hook at every shell start. Use `box_watchdog.sh uninstall` to stop it for good.

# Skywalker Shorts auto-editor

Pipeline for the YouTube channel Skywalker (@Skywalker-k3i).

## Who does what
- **GitHub Actions (this repo):** downloads sources, edits (scene cuts, beat sync, stabilize, grade), renders templates, QA, 4K upscale, and publishes Release assets.
- **Helper bot:** fills `queue/` with trending topics and hooks, builds and fixes the workflows and code. Hooks and YouTube titles must be written in pure English (no Hindi/Hinglish, no Devanagari); `hook_text.py` rejects anything else at render time.
- **BIBI:** checks frames, uploads through YouTube Studio, watches copyright, and runs analytics every 3 days.

## HARD RULES
- NEVER crop the source frame. The full original aspect ratio must be visible.
- whitebar (default): a tight white caption bar (bold black italic hook, ~30px padding) sits directly on top of the full frame (1080 wide), with no gap. Bar + frame form one block centered vertically on a 1080x1920 canvas; leftover letterbox is a blurred/darkened cover of the same clip (never black bars, never crop).
- Hook language: the white top-bar hook, the YouTube title, and the hook sentence in the description must be pure English only. No Hindi, no Hinglish, no Devanagari (never lines like "sabko hypnotise kar deta hai" or "playground ka hero ban gaya"). Film titles and character names may stay as they are. `hook_text.py` enforces this when a Short is rendered. Body subtitles may follow the trailer audio, including Hindi; do not rewrite those into English just to match the hook.
- depthtext: big 3D words behind the subject. postcard: a logo + name + hook header directly above the clip. Same no-crop rule.
- Nothing is written on the footage itself. Stabilized, no shake.
- Never include studio/distributor logo intros (Netflix N, Warner, Lionsgate, Marvel, etc.), title cards, rating cards, coming-soon/date cards, credits, or near-black empty filler. Only real story/character footage. A small corner watermark that is part of the original frame may stay (do not crop it out).
- Only copyright-safe music (CC BY Kevin MacLeod / NCS) with exact credit in the description. No kids content.
- Anime/movie edits need ENERGY: hype or phonk music, hard beat cuts, drop flash, and the reveal on the drop.

## Queue item schema (queue/<date>.json, a list)
{id, slot, source_url, template, hook, words, title, description, tags, target_dur}

`hook` and `title` are English-only (emoji and `#edit`-style tags in the title are fine). The first sentence of `description` should repeat the English hook.

Usage: `python pipeline.py --template whitebar|depthtext|postcard --out out.mp4 ...` (see `--help`).

## Render workflow (`.github/workflows/render.yml`)
- Triggers: push to `queue/*.json`, `workflow_dispatch` (optional `ids`, `max`, `retry_failed`), and a 3-hourly cron.
- `scripts/plan.py` renders only items that are `status: queued`, have no done result, and whose IST `slot` is within 3h (max 3 per run), so cron can pre-render ~2 hours before the upload slot. Done items are never re-rendered. Committing the whole 7-day queue therefore never fires 28 renders.
- `scripts/render_item.py` self-heals before any bot steps in: (1) normal yt-dlp + official fallbacks, (2) force the `sources` Release fallback mp4, (3) fallback + a different credited music track + alternate scene selection + relaxed *non-hard* thresholds. Hard rules are never relaxed: no-crop, tight whitebar layout, and no logo/title/dark cards. If all three attempts fail, `results/<id>.json` is `retry_queued` (run stays green) and `auto-retry.yml` re-dispatches the item once. Only after that last automatic attempt fails is the result `failed` and the workflow run red (that wakes BIBI). The failure reason is always in `results/<id>.json`.
- `pipeline.py` skips the first ~7s and last ~10s of long trailers, and drops studio/distributor logo intros, title/rating/coming-soon/credit cards, and near-black empty filler. Only real story/character footage stays. A small corner watermark that is part of the original frame is kept (never cropped). QA samples every selected shot and fails the render if any is a logo/title/dark card.
- 4K is a lanczos upscale of the 1080x1920 edit to 2160x3840. It does not invent detail.
- Last-resort local fallback: `scripts/local_render.sh <queue-id>` (venv `/workspace/skywalker/.editvenv`).

## Dialogue, subtitles, uniqueness
- Original trailer audio is kept. When actors speak, dialogue is at ~1.3 volume and the music is ducked to ~0.22 (`sidechaincompress`, with a volume-envelope fallback). Non-speech shots keep music at ~0.65. A beat drop on the reveal is still allowed.
- Lower-thirds are word-accurate subtitles of what is actually said, transcribed with local `faster-whisper` (no paid API, no committed secrets). Generic filler captions are never used. The white top hook bar is unchanged and must stay English-only. Subtitles sit in the bottom black and cover the whole video wherever there is dialogue. Subtitle language follows the trailer audio and may be Hindi; that is separate from the hook.
- Every cut must be unique: a used source range `[s, e]` blocks `[s-6, e+6]`, and visually similar shots (pHash / histogram) are rejected. Auto-QC fails the render if the output still has near-duplicate shots.
- Prefer shots that contain a complete spoken line; cuts are snapped so a word is never sliced in half.

## Music
`music/library.json` is the live edit-song library (target ~250–280 tracks: phonk / drift phonk / Brazilian phonk / slowed+reverb / fast-edit). Each track has `license`, `credit`, and `safety` (`safe` or `claim_risk`). Gemini prefers safe tracks and puts the beat drop on the strongest shot. Credits are appended to the Short description. Podcasts, OST dumps and junk are rejected at ingest (`music-ingest.yml` on GitHub Actions; Internet Archive CC + Pixabay/FMA queries). `music/credits.json` remains the last-resort NCS / Kevin MacLeod fallback.

## Source chain
`sourcing.py` + `scripts/render_item.py` try official hosts in order: GitHub `sources` release → studio/press kits → IMDb / Movieclips / Apple Trailers → DailyMotion / Vimeo / Internet Archive / Bilibili / Facebook / Instagram / X / Reddit / TikTok → Indian free clip sites → YouTube last (optional download-only cookies via `YTDLP_COOKIES` secret — never the Skywalker account). Content is not trailer-only: best/fight/romance/emotional scenes from official clip channels are first-class. No DRM / paid OTT rips.

## Queue + backups
Eight IST slots/day (`config/slots.json`). `scripts/fill_queue.py` (from `trends.yml` / `health.yml`) auto-fills missing slots from `config/topic_catalog.json`. `config/backup_pool.json` holds ≥20 pre-uploaded backup topics. `config/backup_stock.json` tracks a target of 30 pre-rendered, QC-passed Shorts on the `backup-stock` release; health assigns the next unused one when a slot fails, an official time is missing from the queue, or a queued item is still empty ~60 minutes out, and refills (a few per run) whenever ready stock is below 30. A failed render still swaps to an on-demand backup-pool render if stock is empty. `scripts/health_check.py` verifies the next 24h.

## Dry-run / test render
`workflow_dispatch` on `render.yml` with `no_publish=true` renders to the `test-renders` GitHub release and is skipped by Buffer publish.

## Submit an official YouTube or X clip
1. Prefer the box script when the link is on YouTube (GitHub runners often get a "sign in to confirm you are not a bot" block). From the repo: `scripts/fetch_link_to_release.sh <url> <name>` — it downloads the official scene clip only (no render, no cookies), uploads `<name>_scene1_h264.mp4` to the `sources` release, queues the next free official IST slot, and triggers the usual edit / QC / Buffer path.
2. Or run the `link` workflow (`workflow_dispatch` with `link_url`, optional `title` / `hook` / `slot` / `no_publish`), or add a row to `links/requests.json` and run `python3 scripts/submit_link.py --pending`. Trailers and teasers are rejected. If GitHub cannot download the file, run the box script in step 1.

A song is not reused within 3 days. Backup stock and the backup pool skip trailer sources (already-posted videos are never deleted). Official titles should have 3–5 scene clips so a render can merge unique shots before it gives up.


## Auto-discover official scene clips
A daily `discover` workflow (around 10:07 IST, or run it by hand) asks Gemini for trending titles, searches YouTube (`ytsearch`) and official X accounts, and keeps only studio/distributor channels (Netflix, TOHO, Aniplex, Warner, Marvel, Sony, Prime Video, and the same list in `config/source_catalog.json`). Trailers are skipped. Each candidate is sampled and run through the existing logo/watermark detector. If a corner logo or a persistent bug is found, that clip is skipped and another *official* clip of the same title is tried. Fan re-uploads are never used to dodge a logo, and logos are never blurred, cropped, or covered. Up to two accepted links are written into the queue and `links/requests.json` so the normal link mode renders and schedules them into the next free official IST slots.

If GitHub cannot download a YouTube sample ("sign in to confirm you are not a bot"), the job leaves those official URLs in `config/discover_status.json`. On the box, run `python3 scripts/discover_links.py --retry-samples` (short sample only, no render, no cookies).


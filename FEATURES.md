# Feature Backlog

Planned features and improvements for the YouTube RAG system.

---

## Multi-Playlist Support

**Goal:** Scope video ingestion and retrieval to individual playlists, while still supporting cross-playlist search for general questions.

### How it works

- Each video stored in Qdrant gets a `playlist_id` metadata field attached at scrape time.
- The scraper accepts a playlist identifier (YouTube playlist ID or a user-defined slug) as input.
- The chat API detects whether the user's question targets a specific playlist (e.g. the widget is embedded on a playlist-specific page, or the user names a playlist) and filters Qdrant results by `playlist_id`.
- If no playlist context is detected, the query runs against the full collection (all playlists).

### Changes needed

**Scraper service**
- Accept `playlist_id` / `playlist_slug` as a parameter alongside the playlist URL.
- Tag every chunk upserted to Qdrant with `{ playlist_id: "<id>", playlist_slug: "<slug>" }` in the payload.

**n8n workflow**
- Pass `playlist_id` when triggering the scraper.
- Store a playlist registry (id → name/URL mapping) somewhere queryable (Postgres table, or a JSON config file).

**Chat API**
- Accept an optional `playlist_id` in the `/chat` request body.
- When `playlist_id` is present, add a Qdrant filter `{ must: [{ key: "playlist_id", match: { value: "<id>" } }] }` to the search call.
- When absent, run the search with no filter.

**Widget**
- Accept a `playlistId` prop so page owners can scope the embedded chat to a specific playlist.
- Pass it through to the `/chat` API call.

### Open questions

- How should the system handle videos that belong to multiple playlists? Duplicate chunks with different tags, or a `playlist_ids` array field with Qdrant's `any` filter?
- Should there be a playlist-selection UI inside the widget, or is the playlist always determined by the embedding page?

---

## ~~Idempotent Re-runs / Skip Already-Processed Videos~~ ✓ Completed

> Implemented in `n8n/workflows/youtube-rag-ingestion.json`. See `docs/idempotent-reruns.md` for full spec.

**Goal:** When the ingestion workflow runs again (e.g. to pick up new videos added to a playlist), skip videos that are already in Qdrant instead of re-downloading, re-embedding, and overwriting them.

### Current behaviour

The workflow re-processes every video in the playlist on every run. Qdrant's `PUT /points` upsert with deterministic IDs (`videoId_chunkId`) means data is not corrupted, but every re-run wastes embedding API quota and scraper time proportional to the full playlist size.

### Approach

Before calling the scraper for a video, check whether any Qdrant point with `video_id == <id>` already exists (a single scroll/filter request with `limit: 1` is enough). If it does, skip that video entirely.

**n8n workflow changes**
- After "Extract Video IDs", add a "Check if already ingested" HTTP node that queries Qdrant's scroll endpoint with a filter on `video_id`.
- Add an IF node: if points exist → skip; if not → proceed to scraper.

**Benefit for multi-playlist support**
- A video that appears in two playlists will only be scraped once. The second pass just updates the `playlist_ids` field (or skips entirely if the multi-playlist tagging strategy uses separate points).

### Open questions

- On a re-run, should new chunks added due to a re-upload or transcript correction be detected, or is "video seen = skip always" acceptable?

---

## YouTube Playlist Pagination

**Goal:** Ingest all videos in a playlist, not just the first 50 (the YouTube API page limit).

### Current limitation

The "Fetch Playlist Page 1" node calls the YouTube `playlistItems` API once with `maxResults=50`. The response includes a `nextPageToken` when more pages exist, but the workflow ignores it. Playlists with more than 50 videos are silently truncated.

### Interaction with idempotent re-runs

Once the "skip already-processed videos" feature is in place and the workflow runs on a schedule:

- YouTube returns items **newest first**, so new uploads always appear on page 1.
- A scheduled run only needs to check page 1 for new content — pagination is not needed for ongoing operation.
- Pagination is only critical for the **initial back-catalogue scrape** of a large playlist.

> Note: Running the workflow multiple times without a pagination loop does **not** help — you get the same first 50 videos each run, not the next page. A `nextPageToken` must be explicitly passed to reach deeper pages.

### Approach options

**Option A — Full pagination loop (n8n)**
Add a loop after "Fetch Playlist Page 1": if the response contains `nextPageToken`, fetch the next page and continue until `nextPageToken` is absent. Merges all pages before the video loop.

**Option B — One-time init mode**
Add a boolean `full_scan` flag to the Config node. When `true`, follow all pages (used once per new playlist). When `false` (scheduled runs), fetch only page 1 for new videos.

Option B avoids building a complex loop and keeps scheduled runs fast. Recommended given that idempotent re-runs are also planned.

---

## Scheduled Trigger — Automatic New-Video Ingestion

**Goal:** Automatically detect and ingest new videos when they appear in a playlist, without manual intervention.

### How it works

Replace (or supplement) the Manual Trigger node with an n8n Schedule Trigger set to run every 30–60 minutes. On each run:

1. Fetch page 1 of the playlist (newest videos first — YouTube default).
2. For each video, check if it already exists in Qdrant (the idempotent check from the "Skip Already-Processed Videos" feature).
3. If it exists → skip. If not → scrape, embed, and ingest.

Because new uploads always appear at the top of the playlist, a single-page fetch is sufficient for scheduled runs — no pagination needed.

### Dependency on other features

This feature only works efficiently once **idempotent re-runs** are implemented. Without the skip check, every scheduled run re-processes the entire first page (50 videos) every 30–60 minutes.

### n8n changes

- Add a **Schedule Trigger** node (cron: `0 * * * *` for hourly, or `*/30 * * * *` for every 30 min) alongside the existing Manual Trigger.
- Both triggers feed into the same Config node — no other changes needed.
- Keep the Manual Trigger for the initial full back-catalogue scrape (with `full_scan` mode from the pagination feature).

### Recommended rollout order

1. Idempotent re-runs (skip check)
2. Scheduled trigger
3. Pagination / `full_scan` mode for initial ingestion of large back-catalogues

## Source Attribution: Publication Date & Timestamp Links

**Goal:** Surface two complementary pieces of metadata alongside every chat answer — (1) the episode's publication date for staleness detection, and (2) a direct deeplink into the video at the exact moment the answer came from.

> Full implementation plan for the timestamp deeplink feature: `docs/chunk-timestamp-attribution.md`

### Why this matters

- A user asking "what's the current best practice for X?" expects recent guidance. An answer drawn from a 3-year-old episode may be actively misleading. Without a date field the system has no signal to detect this mismatch.
- For long-form content (podcasts, lectures) a bare video URL forces the user to scrub manually. VTT subtitle files downloaded by `yt-dlp` already contain per-second timestamps for every spoken line — this data is currently discarded during parsing. Preserving it enables deeplinks of the form `https://youtube.com/watch?v=VIDEO_ID&t=SECONDS` that jump directly to the relevant moment.

### Two metadata fields

Both fields are additive to the existing Qdrant payload (`video_id`, `title`, `channel`, `url`, `chunk_id`, `text`):

```json
{
  "video_id": "abc123",
  "chunk_id": 4,
  "start_time": 754,
  "published_at": "2023-11-14",
  "title": "Episode 42 — …"
}
```

- **`start_time`** — integer, seconds from video start. Derived from VTT cue timestamps during scraping. `null` when no subtitle file is available. Powers the "Watch at MM:SS" deeplink per source.
- **`published_at`** — ISO-8601 date string (e.g. `"2023-11-14"`). Read from `snippet.publishedAt` in the YouTube playlist API response. Powers staleness detection and episode-level citation.

### Changes needed

**Scraper service** (`start_time` only)
- `_parse_vtt()`: return `List[Tuple[int, str]]` (start_seconds, text) instead of stripping timestamps to plain text.
- `_chunk_transcript()`: track the start time of the first VTT segment in each chunk; attach it to the `Chunk` dataclass as `start_time: Optional[int]`.
- `/transcript` response: include `start_time` in each chunk object.

**n8n workflow**
- "Embed and Ingest Chunks" node: add `start_time` from chunk data to the Qdrant point payload.
- After "Fetch Playlist", map `snippet.publishedAt` → `published_at` for each video and include it in the Qdrant point payload.

**Chat API**
- `Source` model: add `start_time: Optional[int]` and `timestamp_url: Optional[str]` (the deeplink).
- `/chat` endpoint: map `start_time` from Qdrant payload; compute `timestamp_url = url + "&t=" + start_time`.
- Staleness detection: if all top-k results have `published_at` older than a configurable threshold (`STALENESS_THRESHOLD_DAYS`, default `730`), prepend a disclaimer to the LLM context and return `stale_sources: true` in the response body.
- Include `published_at` in the context block sent to the LLM so the model can cite episodes naturally.

**Widget**
- Render a "Watch at MM:SS" link per source when `timestamp_url` is present.
- Show a "Sources may be outdated" badge when `stale_sources: true`.

### Temporal query intent (future enhancement)

Once `published_at` is in place, the chat API can detect explicit recency language in a query ("recently", "latest", "current") and automatically apply a Qdrant date range filter so that semantically distant but recent content is not suppressed by older, higher-scoring hits.

### Open questions

- Should `published_at` also be stored as a Unix timestamp integer (`published_at_ts`) to support Qdrant numeric range filters? Recommend adding it alongside the ISO string from the start.
- Should `timestamp_url` be injected into the LLM context block so the model can cite "as discussed at 12:34 in Episode 42…"? Adds tokens but produces more natural answers.
- Re-ingestion of existing points (no `start_time`): forward-only (new videos only) or one-time backfill via a `force_refresh` flag in the Config node? See `docs/chunk-timestamp-attribution.md` for trade-offs.

## OpenAI-Compatible API & Embeddable Chat

**Goal:** Expose the RAG pipeline as a standard `POST /v1/chat/completions` endpoint so any off-the-shelf chat client can connect to it, and serve a self-contained iframe chat page for embedding on external websites and WordPress.

> Full spec and implementation order: `docs/openai-compatible-embed.md`

### How it works

- The `model` field doubles as a collection selector: `rag:podcasts`, `rag:finance-channel`, etc. Any non-`rag:` value falls back to the default collection.
- The embed page (`GET /embed?collection=...`) is served as inline HTML from chat-api — no new service, no build step.
- A WordPress plugin wraps the embed URL in a shortcode: `[rag_chat collection="podcasts"]`.
- For local development, Open WebUI runs as an optional Docker service (`--profile dev`) and points at the new endpoint.

### Changes needed

**`chat-api/main.py`**
- Add `POST /v1/chat/completions` — accepts standard OpenAI messages, routes collection via `model` field, returns OpenAI-shaped response.
- Add `GET /embed` — returns a self-contained HTML chat page (vanilla JS, no build); takes `?collection` and `?title` query params.

**`docker-compose.yml`**
- Add `open-webui` service under `profiles: [dev]`; points `OPENAI_API_BASE_URL` at `http://chat-api:8000/v1`. Starts only when `docker compose --profile dev up` is run.

**`wordpress-plugin/youtube-rag-chat.php`** _(new file)_
- Single PHP file; registers `[rag_chat collection="..." url="..." height="..."]` shortcode that outputs the iframe. Drop into `wp-content/plugins/`, no dependencies.

### What is NOT changing

- Existing `/chat` endpoint and widget — untouched.
- Qdrant schema, n8n workflow, scraper-service — no changes.
- No new Dockerfiles or Python services.

### Implementation order

1. `POST /v1/chat/completions` in `chat-api/main.py` — unblocks all client testing
2. Open WebUI dev service in `docker-compose.yml` — local chat UI
3. `GET /embed` in `chat-api/main.py` — iframe story
4. `wordpress-plugin/youtube-rag-chat.php` — WordPress plugin

<!-- Add new features below this line -->

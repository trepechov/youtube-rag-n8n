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

## Idempotent Re-runs / Skip Already-Processed Videos

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

## Podcast Publication Date Metadata

**Goal:** Attach the YouTube video's publish date to every Qdrant point so the RAG pipeline can surface temporal context, warn about stale results, and let users trace answers back to a specific episode.

### Why this matters

- A user asking "what's the current best practice for X?" expects recent guidance. An answer drawn from a 3-year-old episode may be actively misleading.
- Without a date field the system has no signal to detect this mismatch.
- The date also doubles as a lightweight source reference — "this was from the episode published 2023-11-14" is enough for a user to locate the podcast.

### Metadata field

Add `published_at` (ISO-8601 date string, e.g. `"2023-11-14"`) to every Qdrant point payload alongside `video_id`, `chunk_id`, and (once implemented) `playlist_id`.

```json
{
  "video_id": "abc123",
  "chunk_id": 4,
  "published_at": "2023-11-14",
  "title": "Episode 42 — …",
  "playlist_id": "PL…"
}
```

### Changes needed

**Scraper service**
- The YouTube `playlistItems` API response already includes `snippet.publishedAt` per video. Pass it through in the transcript response as `published_at`.
- Alternatively, the n8n workflow can read it directly from the playlist page response — it is already present in the "Fetch Playlist" node's output.

**n8n workflow**
- After "Fetch Playlist", map `snippet.publishedAt` → `published_at` for each video.
- Include `published_at` in the "Build Qdrant Point" node's payload alongside existing fields.

**Chat API — staleness detection**
- After Qdrant returns search results, inspect `published_at` on the top-k hits.
- If **all** top results are older than 2 years (relative to the current date), prepend a disclaimer to the LLM context:
  > "Note: the most relevant sources found are more than 2 years old (newest: {date}). The information may be outdated."
- Surface this as a `stale_sources: true` flag in the `/chat` response body so the widget can render a visible warning.

**Chat API — source attribution in answers**
- Include `published_at` and `title` (or `video_id`) in the context block sent to the LLM, so the model can cite them naturally.
- Return the source list (title + date + video URL reconstructed from `video_id`) in the API response under a `sources` field for the widget to display.

**Widget**
- Render the `sources` list beneath each assistant response.
- If `stale_sources: true`, show a "Sources may be outdated" badge.

### Temporal query intent (future enhancement)

As a follow-on, the chat API could detect explicit recency language in the query ("recently", "latest", "current", "in the last year") and automatically apply a Qdrant date range filter (`published_at >= now - 2 years`) so that semantically distant but recent content is not suppressed by older, higher-scoring hits. This is additive and can be implemented independently once the `published_at` field is in place.

### Open questions

- Should `published_at` be stored as a string or a Unix timestamp integer? Qdrant supports range filters on integers, which is cleaner for the date-filter enhancement above. Recommend storing as `published_at_ts` (Unix epoch seconds) in addition to the ISO string.
- What is the right staleness threshold? Two years is the stated default; make it configurable via an env var (`STALENESS_THRESHOLD_DAYS`, default `730`).

<!-- Add new features below this line -->

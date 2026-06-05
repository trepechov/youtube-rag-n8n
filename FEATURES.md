# Feature Backlog

Planned features and improvements for the YouTube RAG system.

---

## Beta v0.1 — Shipped (June 2026)

- Multi-playlist ingestion with full pagination and idempotent re-runs
- Source attribution (`start_time`, `published_at`) in scraper, n8n, and chat API
- OpenAI-compatible `POST /v1/chat/completions` endpoint + Open WebUI integration
- Collection status counter (`GET /stats`)
- Demo password protection via nginx basic auth

---

## ~~Multi-Playlist Ingestion~~ ✓ Completed

> Implemented in `n8n/workflows/youtube-rag-ingestion.json` (feat/multi-playlist branch).
> Retrieval filtering (chat API + widget) tracked as a follow-up below.

**What shipped:**
- n8n Config node accepts a comma-separated `playlist_ids` field (`id:name` pairs or bare IDs).
- A "Loop Playlists" node iterates over each playlist; "Fetch All Videos" paginates through all pages.
- Every chunk stored in Qdrant carries a `playlist_name` payload field.

---

## ~~Idempotent Re-runs / Skip Already-Processed Videos~~ ✓ Completed

> Implemented in `n8n/workflows/youtube-rag-ingestion.json`.

**What shipped:**
- Before calling the scraper, the workflow queries Qdrant for any existing point with `video_id == <id>` (scroll with `limit: 1`).
- Videos already in Qdrant are skipped entirely, saving embedding quota and scraper time.

---

## ~~YouTube Playlist Pagination~~ ✓ Completed

> Implemented in `n8n/workflows/youtube-rag-ingestion.json` ("Fetch All Videos" node).

**What shipped:**
- The "Fetch All Videos" Code node loops through `nextPageToken` until exhausted, collecting all videos before the per-video processing loop.
- No 50-video cap. Works for back-catalogue scrapes of any playlist size.

---

## ~~Collection Status Counter~~ ✓ Completed

> Implemented in `chat-api/main.py`.

**What shipped:**
- `GET /stats?collection=<name>` returns `total_videos`, `total_chunks`, `avg_chunks_per_video`, and Qdrant collection health (`status`).
- `total_videos` is derived from points where `chunk_id == 0` — a reliable proxy that never overcounts.

---

## ~~Source Attribution — Backend~~ ✓ Completed

> Implemented in `scraper-service/main.py`, `n8n/workflows/youtube-rag-ingestion.json`, and `chat-api/main.py`.

**What shipped:**
- **Scraper:** `_parse_vtt()` preserves per-cue start timestamps; `_chunk_transcript()` attaches `start_time` (seconds) to each `Chunk`.
- **n8n workflow:** maps `snippet.publishedAt` → `published_at` per video and includes both `start_time` and `published_at` in the Qdrant point payload.
- **Chat API:** `Source` model includes `start_time`, `published_at`, and `timestamp_url` (`url + "&t=" + start_time`). Sources are sorted newest-first by `published_at`.

**Remaining (widget layer — see below):** deeplink rendering and staleness badge.

---

## ~~OpenAI-Compatible Endpoint~~ ✓ Completed

> Implemented in `chat-api/main.py` and `docker-compose.yml`.

**What shipped:**
- `POST /v1/chat/completions` accepts standard OpenAI messages; routes collection via the `model` field (`rag:<collection-slug>`); returns an OpenAI-shaped response.
- Open WebUI dev service in `docker-compose.yml` (`--profile dev`); points `OPENAI_API_BASE_URL` at the chat-api.

**Remaining (embed + WordPress — see below):** `GET /embed` iframe page and WordPress shortcode plugin.

---

## ~~Demo Password Protection~~ ✓ Completed

> Implemented in `nginx/nginx.conf` and `docker-compose.prod.yml`.

**What shipped:**
- nginx basic auth (`auth_basic`) gates both Open WebUI (`/`) and n8n (`/n8n/`); `/api/` stays public for embedded widgets.
- Open WebUI added to prod compose (internal network, nginx-proxied, no direct port exposure).
- `nginx/.htpasswd` is gitignored; generated locally with one `openssl` command.

---

## Playlist-Scoped Chat

**Goal:** Let the chat API filter Qdrant results to a specific playlist so an embedded widget can answer questions about one playlist without surfacing content from others.

### Changes needed

**Chat API**
- Accept an optional `playlist_name` (or `playlist_id`) in the `/chat` and `/v1/chat/completions` request body.
- When present, add a Qdrant filter `{ must: [{ key: "playlist_name", match: { value: "<name>" } }] }` to the search call.
- When absent, run the search with no filter (full collection).

**Widget**
- Accept a `data-playlist` attribute so page owners can scope the embedded chat to a specific playlist.
- Pass it through to the `/chat` API call.

---

## Scheduled Trigger — Automatic New-Video Ingestion

**Goal:** Automatically detect and ingest new videos when they appear in a playlist, without manual intervention.

### How it works

Replace (or supplement) the Manual Trigger node with an n8n Schedule Trigger set to run every 30–60 minutes. On each run:

1. Fetch page 1 of the playlist (newest videos first — YouTube default).
2. For each video, check if it already exists in Qdrant (idempotent re-run check).
3. If it exists → skip. If not → scrape, embed, and ingest.

Because new uploads always appear at the top of the playlist, a single-page fetch is sufficient for scheduled runs.

### n8n changes

- Add a **Schedule Trigger** node (cron: `0 * * * *` for hourly, or `*/30 * * * *` for every 30 min) alongside the existing Manual Trigger.
- Both triggers feed into the same Config node — no other changes needed.

---

## Source Attribution — Widget Layer

**Goal:** Surface timestamp deeplinks and a staleness warning in the chat widget.

### Changes needed

**Widget**
- Render a "Watch at MM:SS" link per source when `timestamp_url` is present in the `/chat` response.
- Show a "Sources may be outdated" badge when `stale_sources: true`.

**Chat API**
- Add staleness detection: if all top-k results have `published_at` older than `STALENESS_THRESHOLD_DAYS` (default `730`), prepend a disclaimer to the LLM context and return `stale_sources: true` in the response body.

---

## Embeddable iframe & WordPress Plugin

**Goal:** Serve a self-contained iframe chat page and a WordPress shortcode plugin so any site can embed the RAG chat without touching the widget JS directly.

### Changes needed

**`chat-api/main.py`**
- Add `GET /embed` — returns a self-contained HTML chat page (vanilla JS, no build step); takes `?collection` and `?title` query params.

**`wordpress-plugin/youtube-rag-chat.php`** _(new file)_
- Single PHP file; registers `[rag_chat collection="..." url="..." height="..."]` shortcode that outputs the iframe.
- Drop into `wp-content/plugins/`, no dependencies.

<!-- Add new features below this line -->

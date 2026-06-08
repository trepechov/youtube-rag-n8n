# YouTube RAG — n8n Edition

End-to-end system that scrapes YouTube playlists, builds a vector knowledge base, and serves a chat interface for querying podcast content.

---

## Getting started

### What you need before you begin

- Docker + Docker Compose
- [YouTube Data API v3 key](https://console.cloud.google.com/apis/credentials)
- [OpenRouter API key](https://openrouter.ai/keys) — free tier available; covers both embeddings and chat LLM

---

### Step 1 — Clone the repo

```bash
git clone https://github.com/trepechov/youtube-rag-n8n.git
cd youtube-rag-n8n
```

### Step 2 — Add your API keys

```bash
cp .env.example .env
# Open .env and fill in:
#   OPENROUTER_API_KEY  — your OpenRouter key
#   QDRANT_COLLECTION   — a short slug for the podcast you're ingesting
#                         e.g. my-podcast, huberman-lab, my-show
```

### Step 3 — Start the app

```bash
docker compose up -d
```

| Service | URL |
|---------|-----|
| n8n (ingestion UI) | http://localhost:5678 |
| Chat API | http://localhost:8000 |
| Qdrant dashboard | http://localhost:6333/dashboard |

### Step 4 — Ingest a YouTube playlist

1. Open **http://localhost:5678** and import `n8n/workflows/youtube-rag-ingestion.json`
2. Open the **Config** node and set:
   - `playlist_ids` — one or more playlists to ingest, as `id:name` pairs (comma-separated for multiple):
     - Single playlist: `PLrAXtmErZgOfMuxkptxDyMcFpl2tMkhuw:My Show`
     - Multiple playlists: `PL111:My Course,PL222:Interview Prep`
     - Bare ID (no name) still works: `PLrAXtmErZgOfMuxkptxDyMcFpl2tMkhuw`
   - `collection` — the same slug you set in `QDRANT_COLLECTION` (e.g. `my-podcast`)

   Each chunk stored in Qdrant carries a `playlist_name` payload field, which you can filter on:
   ```json
   { "must": [{ "key": "playlist_name", "match": { "value": "My Course" } }] }
   ```
3. Click **Execute Workflow** and wait for it to finish

### Step 5 — Ask a question

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"question": "What topics were discussed?", "collection": "my-podcast"}'
```

You should get back an answer with cited sources from your playlist.

---

## Next steps

**Embed the widget on your website:**

```html
<script
  src="http://localhost:8000/widget/chat-widget.js"
  data-api-url="http://localhost:8000"
  data-collection="my-podcast"
  data-title="Ask the Podcast"
></script>
```

**Start the dev chat UI (Open WebUI):**

```bash
docker compose --profile dev up -d
# Open WebUI available at http://localhost:3000
```

**Deploy to a VPS (with password protection):**

```bash
# 1. Generate the shared password file (once, on the server)
printf "demo:$(openssl passwd -apr1 yourpassword)\n" > nginx/.htpasswd

# 2. Start everything
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

Share `http://your-server-ip` + the password with friends.
- **`/`** → Open WebUI chat (password required)
- **`/n8n/`** → ingestion workflow editor (password required)
- **`/api/`** → RAG API (public — for embedded widgets)

See [ARCHITECTURE.md](ARCHITECTURE.md) for full details on production setup, SSL, and security hardening.

**Sync the transcript cache between environments:**

Transcripts are cached in a Docker volume (`transcript_cache`) so they're never downloaded twice. To move the cache from your local machine to production:

```bash
# 1. On local — export the volume to a tar.gz
./scripts/export-transcript-cache.sh
# Creates: transcript_cache_YYYYMMDD.tar.gz

# 2. Copy it to the server
scp transcript_cache_20260608.tar.gz user@your-server:/path/to/youtube-rag-n8n/

# 3. On the server — merge it into the production volume
./scripts/import-transcript-cache.sh transcript_cache_20260608.tar.gz
```

The import merges into the existing volume. New transcripts are added; existing files with the same video ID are overwritten by the imported version. No service restart needed; the scraper-service picks up the files immediately.

---

## Architecture at a glance

```
YouTube Playlist
      │
      ▼
  n8n Workflow  ──►  Scraper Service (youtube-transcript-api)
      │
      ▼
  OpenRouter Embeddings (free, nvidia/llama-nemotron)
      │
      ▼
   Qdrant (vector DB)
      │
      ▼
  Chat API  ──►  OpenRouter (any LLM, free tier available)
      │
      ▼
  Chat Widget (embeddable JS)
```

## Services

### Core (always running)

| Service | Local Port | Description |
|---------|-----------|-------------|
| n8n | 5678 | Workflow automation — runs the ingestion pipeline |
| Qdrant | 6333 | Vector database |
| Scraper Service | 8001 | youtube-transcript-api extraction (internal only) |
| Chat API | 8000 | RAG query endpoint + embeddable widget |
| PostgreSQL | — | n8n database (internal only) |

### Dev tools (opt-in)

Start with `docker compose --profile dev up -d`.

| Service | Local Port | Description |
|---------|-----------|-------------|
| Open WebUI | 3000 | Chat UI for testing the OpenAI-compatible `/v1` API |

## Workflow: how ingestion works

```
n8n Trigger
  └─► Set Config (playlist ID, collection name)
  └─► YouTube API: fetch all playlist video IDs
  └─► [loop per video]
        └─► Scraper Service: download transcript via youtube-transcript-api
        └─► [loop per chunk]
              └─► OpenRouter: generate embedding (nvidia/llama-nemotron, free)
              └─► Qdrant: upsert vector + payload
```

## RAG collection schema

Each Qdrant point has a 1536-dim vector plus:

```json
{
  "video_id": "dQw4w9WgXcQ",
  "title": "Episode Title",
  "channel": "Channel Name",
  "url": "https://youtube.com/watch?v=dQw4w9WgXcQ",
  "chunk_id": 3,
  "text": "...transcript chunk..."
}
```

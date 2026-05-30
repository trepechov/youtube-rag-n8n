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
# Open .env and fill in OPENROUTER_API_KEY
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
2. Open the **Config** node and set your YouTube playlist ID
3. Click **Execute Workflow** and wait for it to finish

### Step 5 — Ask a question

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"question": "What topics were discussed?", "collection": "podcasts"}'
```

You should get back an answer with cited sources from your playlist.

---

## Next steps

**Embed the widget on your website:**

```html
<script
  src="http://localhost:8000/widget/chat-widget.js"
  data-api-url="http://localhost:8000"
  data-collection="podcasts"
  data-title="Ask the Podcast"
></script>
```

**Start the dev chat UI (Open WebUI):**

```bash
docker compose --profile dev up -d
# Open WebUI available at http://localhost:3000
```

**Deploy to a VPS:**

```bash
docker compose -f docker-compose.prod.yml up -d
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for full details on production setup, SSL, and security hardening.

---

## Architecture at a glance

```
YouTube Playlist
      │
      ▼
  n8n Workflow  ──►  Scraper Service (yt-dlp)
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
| Scraper Service | 8001 | yt-dlp transcript extraction (internal only) |
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
        └─► Scraper Service: download transcript via yt-dlp
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

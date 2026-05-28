# YouTube RAG — n8n Edition

End-to-end system that scrapes YouTube playlists, builds a vector knowledge base, and serves a chat interface for querying podcast content.

## Architecture at a glance

```
YouTube Playlist
      │
      ▼
  n8n Workflow  ──►  Scraper Service (yt-dlp)
      │
      ▼
  Ollama Embeddings (local, free)
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

| Service | Local Port | Description |
|---------|-----------|-------------|
| n8n | 5678 | Workflow automation — runs ingestion pipeline |
| Qdrant | 6333 | Vector database |
| Ollama | 11434 | Local embedding model server (nomic-embed-text) |
| Scraper Service | 8001 | yt-dlp transcript extraction (internal) |
| Chat API | 8000 | RAG query endpoint (public) |
| nginx | 80 | Reverse proxy (production only) |
| PostgreSQL | — | n8n database (internal) |

## Quick Start (local)

### 1. Prerequisites

- Docker + Docker Compose (4GB+ RAM recommended for Ollama)
- YouTube Data API v3 key ([get one here](https://console.cloud.google.com/apis/credentials))
- OpenRouter API key (for chat LLM — [free tier available](https://openrouter.ai/keys))

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env and fill in your API keys
```

### 3. Start everything

```bash
docker compose up -d
```

### 4. Pull the embedding model (one-time setup)

```bash
docker compose exec ollama ollama pull nomic-embed-text
```

This downloads ~274MB and only needs to be done once — the model is persisted in a Docker volume.

Services will be available at:
- n8n: http://localhost:5678
- Chat API: http://localhost:8000
- Qdrant dashboard: http://localhost:6333/dashboard

### 5. Run the ingestion workflow

1. Open n8n at http://localhost:5678
2. Import the workflow from `n8n/workflows/youtube-rag-ingestion.json`
3. In the **Config** node, set your playlist ID (e.g. `PLxxxxxxxxxxxxxx`)
4. No extra credentials needed — Ollama runs locally, YouTube API key is in `.env`
5. Click **Execute Workflow**

### 6. Test the chat API

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"question": "What topics were discussed?", "collection": "podcasts"}'
```

### 7. Embed the chat widget

Add to any website:

```html
<script
  src="http://localhost:8000/widget/chat-widget.js"
  data-api-url="http://localhost:8000"
  data-collection="podcasts"
  data-title="Ask the Podcast"
></script>
```

## Production deployment (VPS)

```bash
# Copy project to VPS, then:
docker compose -f docker-compose.prod.yml up -d
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for full details on VPS setup, SSL, and security hardening.

## Workflow: how ingestion works

```
n8n Trigger
  └─► Set Config (playlist ID, collection name)
  └─► YouTube API: fetch all playlist video IDs
  └─► [loop per video]
        └─► Scraper Service: download transcript via yt-dlp
        └─► [loop per chunk]
              └─► OpenAI: generate embedding (text-embedding-3-small)
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

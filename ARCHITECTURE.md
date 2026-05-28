# Architecture

## System overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        Docker Network                            │
│                                                                  │
│  ┌──────────┐    ┌─────────────────┐    ┌──────────────────┐   │
│  │   n8n    │───►│ scraper-service │    │   chat-api       │   │
│  │ :5678    │    │ :8001 (internal)│    │ :8000            │   │
│  └────┬─────┘    └─────────────────┘    └────────┬─────────┘   │
│       │                                           │              │
│       │  ┌─────────────────┐                     │              │
│       └─►│    Qdrant       │◄────────────────────┘              │
│          │ :6333 (internal)│                                     │
│          └─────────────────┘                                     │
│                                                                  │
│  ┌──────────┐                                                    │
│  │ postgres │  (n8n database, fully internal)                   │
│  └──────────┘                                                    │
└─────────────────────────────────────────────────────────────────┘
         │                              │
    External API calls              Public HTTP
    (YouTube, OpenAI,              (via nginx in prod)
     OpenRouter)
```

## Services

### scraper-service
- **Language**: Python 3.12, FastAPI
- **Purpose**: Downloads YouTube transcripts using yt-dlp, splits into RAG chunks
- **Key endpoint**: `POST /transcript` — accepts a video ID, returns chunked text
- **Not publicly exposed** — only n8n calls it over the internal Docker network
- **Why separate**: yt-dlp is heavy and slow; isolating it keeps n8n clean and lets us scale it independently

### chat-api
- **Language**: Python 3.12, FastAPI
- **Purpose**: RAG query endpoint + serves the embeddable widget
- **Key endpoint**: `POST /chat` — embed question → search Qdrant → call OpenRouter → return answer + sources
- **Publicly exposed** via nginx in production (or direct on port 8000 locally)

### n8n
- **Purpose**: Orchestrates the entire ingestion pipeline
- **Database**: PostgreSQL (required for workflow persistence and credentials storage)
- **Workflow**: `n8n/workflows/youtube-rag-ingestion.json` — import this into n8n

### Qdrant
- **Version**: latest (stable)
- **Vector dimensions**: 1536 (OpenAI `text-embedding-3-small`)
- **Distance metric**: Cosine
- **Storage**: persisted to Docker volume
- **Not publicly exposed** — chat-api and n8n access it over the internal network

## Ingestion pipeline (step by step)

```
1. n8n Manual Trigger
2. Set Config node: playlist_id, collection_name
3. HTTP Request → YouTube Data API v3
   GET /playlistItems?part=snippet&playlistId={id}&maxResults=50&key={key}
   → Returns up to 50 video IDs per page (handle pagination for large playlists)
4. Code node: extract video IDs into array of items
5. Split In Batches: process one video at a time
6. HTTP Request → scraper-service
   POST http://scraper-service:8001/transcript
   Body: { "video_id": "...", "chunk_size": 600 }
   → Returns: { "video_id", "title", "channel", "url", "chunks": [{chunk_id, text}] }
7. Code node: explode chunks array into individual items
8. HTTP Request → OpenAI Embeddings API
   POST https://api.openai.com/v1/embeddings
   Body: { "model": "text-embedding-3-small", "input": "<chunk text>" }
   → Returns: { "data": [{ "embedding": [1536 floats] }] }
9. Code node: build Qdrant upsert payload
   { "points": [{ "id": <uuid>, "vector": [...], "payload": {...} }] }
10. HTTP Request → Qdrant
    PUT http://qdrant:6333/collections/{collection}/points
    → Upserts the vector point
```

## Chat (RAG) pipeline

```
1. User sends question via widget or direct API call
2. chat-api: embed question with OpenAI text-embedding-3-small
3. chat-api: POST /collections/{collection}/points/search to Qdrant
   → Returns top-5 most similar chunks
4. chat-api: build prompt:
   "Context: [chunk1]\n[chunk2]...\nQuestion: {question}"
5. chat-api: POST to OpenRouter /api/v1/chat/completions
   → Returns LLM answer
6. Return { answer, sources[] } to client
```

## API reference

### scraper-service

```
GET  /health
POST /transcript
  Body: { "video_id": str, "chunk_size": int=600, "langs": str[]=["en","en-GB","en-US"] }
  Response: { "video_id", "title", "channel", "url", "chunks": [{"chunk_id", "text"}] }
```

### chat-api

```
GET  /health
POST /chat
  Body: {
    "question": str,
    "collection": str = "podcasts",
    "model": str = env.OPENROUTER_MODEL,
    "top_k": int = 5
  }
  Response: {
    "answer": str,
    "sources": [{ "video_id", "title", "channel", "text", "url", "score" }]
  }

GET  /widget/chat-widget.js   (embeddable widget)
GET  /widget/chat-widget.css
```

## LLM model switching

Set `OPENROUTER_MODEL` in `.env` to switch the chat LLM without code changes or restarts.

```env
# Free (local dev)
OPENROUTER_MODEL=meta-llama/llama-3.1-8b-instruct:free
OPENROUTER_MODEL=google/gemma-2-9b-it:free
OPENROUTER_MODEL=mistralai/mistral-7b-instruct:free

# Paid (production)
OPENROUTER_MODEL=openai/gpt-4o-mini
OPENROUTER_MODEL=anthropic/claude-sonnet-4-6
OPENROUTER_MODEL=meta-llama/llama-3.1-70b-instruct
```

Per-request override: pass `"model": "..."` in the `/chat` request body.

## Embeddings

**Provider**: Ollama (local Docker service) — no API key, no cost.
**Model**: `nomic-embed-text` — 768 dimensions, fast on CPU.
**Endpoint**: `POST http://ollama:11434/v1/embeddings` (OpenAI-compatible format)

The Ollama service shares the same Docker network. The model must be pulled once after first start:

```bash
docker compose exec ollama ollama pull nomic-embed-text
```

**Why not OpenRouter for embeddings?** OpenRouter only supports chat completions — it has no embeddings API. Ollama gives us a local, free, consistent alternative.

**Switching embedding models**: Change `EMBEDDING_MODEL` in `.env`. If you switch, you MUST recreate the Qdrant collection and re-ingest (vectors from different models are incompatible).

| Model | Dimensions | Notes |
|-------|-----------|-------|
| `nomic-embed-text` | 768 | Default, fast CPU |
| `mxbai-embed-large` | 1024 | Higher quality, more RAM |
| `all-minilm` | 384 | Very lightweight |

## Production deployment

### Prerequisites
- VPS with Docker + Docker Compose
- Domain name pointed at VPS
- Ports 80 and 443 open
- At least 4GB RAM (Ollama needs ~2GB for nomic-embed-text)

### Recommended setup

```bash
# 1. Clone repo to VPS
git clone <repo> /opt/youtube-rag
cd /opt/youtube-rag

# 2. Set environment
cp .env.example .env
# Fill in production values

# 3. Set up SSL (Certbot + nginx or Cloudflare proxy)
# If using Cloudflare proxy (recommended): set orange-cloud, port 80 only

# 4. Start production stack
docker compose -f docker-compose.prod.yml up -d

# 5. n8n is accessible at /n8n — secure with N8N_BASIC_AUTH settings in .env
```

### Security hardening checklist

- [ ] Change all default passwords in `.env`
- [ ] Set `N8N_BASIC_AUTH_ACTIVE=true` and strong credentials
- [ ] Restrict Qdrant to internal network only (enforced by docker-compose.prod.yml)
- [ ] Put VPS behind Cloudflare for DDoS protection
- [ ] Rate-limit `/chat` endpoint in nginx config
- [ ] Use `ALLOWED_ORIGINS` env var in chat-api to restrict CORS

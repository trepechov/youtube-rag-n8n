# OpenAI-Compatible API & Embeddable Chat

## Goal

Make the chat-api speak the OpenAI `POST /v1/chat/completions` protocol so that any off-the-shelf chat client can talk to it — without writing or hosting a custom chat UI. The existing `/chat` endpoint stays untouched.

---

## What changes (minimal)

### 1. New endpoint in `chat-api/main.py` (~50 lines)

```
POST /v1/chat/completions
```

Standard OpenAI request body. Collection routing is encoded in the `model` field:

| `model` value | Collection used |
|---|---|
| `rag:podcasts` | `podcasts` |
| `rag:finance-channel` | `finance-channel` |
| `gpt-4o` (or any non-`rag:` value) | default collection from env |

The endpoint extracts the last user message, runs the existing RAG pipeline (embed → Qdrant → LLM), and returns an OpenAI-shaped response. No streaming in v1 (can be added later).

```python
# Route added to chat-api/main.py
POST /v1/chat/completions
  Body: {
    "model": "rag:podcasts",
    "messages": [{"role": "user", "content": "What is X?"}]
  }
  Response: {
    "id": "chatcmpl-...",
    "choices": [{"message": {"role": "assistant", "content": "..."}}],
    "model": "rag:podcasts"
    # x-sources header or extra field carries source list
  }
```

### 2. Embed page served from chat-api (`GET /embed`)

A minimal single-page chat UI served as inline HTML from chat-api itself. No new service, no build step.

```
GET /embed?collection=podcasts&title=Ask+the+Podcast
```

Returns a self-contained HTML page (vanilla JS, ~150 lines) suitable for iframe embedding. Uses the existing `/chat` API internally — no dependency on the OpenAI endpoint.

Embed on any site:

```html
<iframe
  src="https://your-api-domain/embed?collection=podcasts&title=Ask+the+Podcast"
  width="400"
  height="600"
  style="border:none;"
></iframe>
```

### 3. WordPress plugin (`wordpress-plugin/youtube-rag-chat.php`)

Single PHP file. Registers a shortcode that outputs the iframe. No dependencies, no build.

```php
[rag_chat collection="podcasts" url="https://your-api-domain" height="600"]
```

Install: drop the file into `wp-content/plugins/`, activate in WordPress admin.

---

## Local dev testing

Two options — use whichever fits:

### Option A: Open WebUI via Docker profile (recommended)

Zero code. Add this service to `docker-compose.yml`:

```yaml
open-webui:
  image: ghcr.io/open-webui/open-webui:main
  profiles: [dev]
  ports:
    - "3000:8080"
  environment:
    OPENAI_API_BASE_URL: http://chat-api:8000/v1
    OPENAI_API_KEY: any-string
    WEBUI_AUTH: "false"
  depends_on:
    - chat-api
  networks:
    - internal
```

Run the dev stack:

```bash
docker compose --profile dev up
```

Open `http://localhost:3000` → full chat UI. In the model selector, type `rag:podcasts` to scope to a collection.

**Why Open WebUI:** runs as a single Docker image, no config file, points at any OpenAI-compatible URL, has conversation history and source display.

### Option B: Zero-infrastructure (quickest start)

Point any existing tool at `http://localhost:8000/v1`:

| Tool | Type | How |
|---|---|---|
| [Chatbox](https://chatboxai.app) | Desktop app | Settings → Custom API → `http://localhost:8000/v1` |
| [Page Assist](https://github.com/n4ze3m/page-assist) | Browser extension | Add custom provider, same URL |
| HTTPie / curl | CLI | `http POST localhost:8000/v1/chat/completions model=rag:podcasts messages:='[{"role":"user","content":"test"}]'` |

Option B requires only the endpoint change — no docker-compose edit.

---

## Implementation order

| Step | File | Effort |
|---|---|---|
| 1. Add `POST /v1/chat/completions` | `chat-api/main.py` | ~50 lines |
| 2. Add `GET /embed` (inline HTML) | `chat-api/main.py` | ~150 lines HTML as string |
| 3. Add Open WebUI dev service | `docker-compose.yml` | 12 lines |
| 4. WordPress plugin | `wordpress-plugin/youtube-rag-chat.php` | ~30 lines PHP |

Steps 1 and 3 unblock local testing immediately. Steps 2 and 4 deliver the embed story.

---

## What is NOT changing

- Existing `/chat` endpoint — untouched, widget still uses it
- Qdrant schema, n8n workflow, scraper-service — no changes
- No new Dockerfiles, no new Python services
- No frontend build step

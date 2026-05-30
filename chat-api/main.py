import os
import uuid
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI(title="YouTube RAG Chat API", version="1.0.0")

OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]
QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333")
DEFAULT_COLLECTION = os.environ.get("QDRANT_COLLECTION", "podcasts")
DEFAULT_MODEL = os.environ.get("OPENROUTER_MODEL", "meta-llama/llama-3.1-8b-instruct:free")
EMBEDDING_SERVICE_URL = os.environ.get("EMBEDDING_SERVICE_URL", "https://openrouter.ai/api")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "nvidia/llama-nemotron-embed-vl-1b-v2:free")

ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "*").split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

# Serve the widget as static files at /widget/
if os.path.isdir("widget"):
    app.mount("/widget", StaticFiles(directory="widget"), name="widget")


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    question: str
    collection: Optional[str] = None
    model: Optional[str] = None
    top_k: int = 5


class Source(BaseModel):
    video_id: str
    title: str
    channel: str
    text: str
    url: str
    score: float
    start_time: Optional[int] = None
    timestamp_url: Optional[str] = None
    published_at: Optional[str] = None


class ChatResponse(BaseModel):
    answer: str
    sources: list[Source]


# ---------------------------------------------------------------------------
# External API helpers
# ---------------------------------------------------------------------------

async def _get_embedding(text: str) -> list[float]:
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{EMBEDDING_SERVICE_URL}/v1/embeddings",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "HTTP-Referer": "https://youtube-rag",
                "X-Title": "YouTube RAG",
            },
            json={"model": EMBEDDING_MODEL, "input": text},
        )
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]


async def _search_qdrant(collection: str, vector: list[float], top_k: int) -> list[dict]:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{QDRANT_URL}/collections/{collection}/points/search",
            json={"vector": vector, "limit": top_k, "with_payload": True},
        )
        if resp.status_code == 404:
            raise HTTPException(status_code=404, detail=f"Collection '{collection}' not found — run ingestion first")
        resp.raise_for_status()
        return resp.json().get("result", [])


FALLBACK_MODELS = [
    DEFAULT_MODEL,
    "openai/gpt-oss-20b:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "meta-llama/llama-3.2-3b-instruct:free",
    "deepseek/deepseek-v4-flash:free",
    "nvidia/nemotron-nano-9b-v2:free",
]


async def _call_openrouter(question: str, context: str, model: str) -> str:
    system_prompt = (
        "You are a helpful assistant that answers questions based on podcast transcript excerpts. "
        "Always answer in the same language the user used to ask the question. "
        "The transcript excerpts are auto-generated and may lack punctuation or have rough phrasing — "
        "synthesise a clean, fluent, grammatically correct answer from the content; never copy raw transcript text verbatim. "
        "Use only the provided context to answer. If the answer is not in the context, say so honestly. "
        "Cite the episode title when referencing specific content."
    )
    user_message = f"Context from podcast transcripts:\n\n{context}\n\nQuestion: {question}"

    candidates = [model] + [m for m in FALLBACK_MODELS if m != model]
    last_error = ""

    async with httpx.AsyncClient(timeout=60) as client:
        for candidate in candidates:
            resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "HTTP-Referer": "https://youtube-rag",
                    "X-Title": "YouTube RAG",
                },
                json={
                    "model": candidate,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_message},
                    ],
                },
            )
            if resp.status_code == 429 or resp.status_code == 404:
                last_error = resp.text[:200]
                continue
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]

    raise httpx.HTTPStatusError(last_error, request=resp.request, response=resp)


# ---------------------------------------------------------------------------
# OpenAI-compatible models
# ---------------------------------------------------------------------------

class OAIMessage(BaseModel):
    role: str
    content: str

class OAIRequest(BaseModel):
    model: str = "rag:podcasts"
    messages: list[OAIMessage]
    stream: bool = False

class OAIChoice(BaseModel):
    index: int = 0
    message: OAIMessage
    finish_reason: str = "stop"

class OAIResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    model: str
    choices: list[OAIChoice]


def _collection_from_model(model: str) -> str:
    if model.startswith("rag:"):
        slug = model[4:]
        return slug if slug else DEFAULT_COLLECTION
    return DEFAULT_COLLECTION


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/stats")
async def stats(collection: Optional[str] = None):
    coll = collection or DEFAULT_COLLECTION
    async with httpx.AsyncClient(timeout=10) as client:
        info_resp = await client.get(f"{QDRANT_URL}/collections/{coll}")
        if info_resp.status_code == 404:
            raise HTTPException(status_code=404, detail=f"Collection '{coll}' not found — run ingestion first")
        info_resp.raise_for_status()
        info = info_resp.json()["result"]

        # Count distinct videos: each video's first chunk has chunk_id == 0,
        # so this filter gives one point per ingested video.
        count_resp = await client.post(
            f"{QDRANT_URL}/collections/{coll}/points/count",
            json={"filter": {"must": [{"key": "chunk_id", "match": {"value": 0}}]}, "exact": True},
        )
        count_resp.raise_for_status()
        video_count = count_resp.json()["result"]["count"]

    total_chunks = info.get("points_count", 0)
    return {
        "collection": coll,
        "status": info.get("status", "unknown"),
        "total_videos": video_count,
        "total_chunks": total_chunks,
        "avg_chunks_per_video": round(total_chunks / video_count, 1) if video_count else 0,
    }


@app.get("/v1/models")
async def list_models():
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(f"{QDRANT_URL}/collections")
            collections = [c["name"] for c in resp.json().get("result", {}).get("collections", [])]
        except Exception:
            collections = [DEFAULT_COLLECTION]
    return {
        "object": "list",
        "data": [
            {"id": f"rag:{c}", "object": "model", "created": 0, "owned_by": "youtube-rag"}
            for c in collections
        ],
    }


@app.post("/v1/chat/completions", response_model=OAIResponse)
async def oai_chat(req: OAIRequest):
    user_msgs = [m for m in req.messages if m.role == "user"]
    if not user_msgs:
        raise HTTPException(status_code=400, detail="No user message provided")
    question = user_msgs[-1].content
    collection = _collection_from_model(req.model)

    try:
        vector = await _get_embedding(question)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"Embedding failed: {exc.response.text[:200]}")
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail="Embedding service unavailable")

    results = await _search_qdrant(collection, vector, top_k=5)

    if not results:
        answer = "I couldn't find relevant content in the knowledge base. Try ingesting some playlists first."
    else:
        context_parts = []
        source_lines = []
        for r in results:
            p = r["payload"]
            context_parts.append(f"[{p.get('title', 'Unknown')}]\n{p.get('text', '')}")
            url = p.get("url", f"https://youtube.com/watch?v={p.get('video_id', '')}")
            start_time = p.get("start_time")
            link = f"{url}&t={start_time}" if start_time is not None else url
            source_lines.append(f"- [{p.get('title', 'Unknown')}]({link})")
        context = "\n\n---\n\n".join(context_parts)

        try:
            answer = await _call_openrouter(question, context, DEFAULT_MODEL)
        except httpx.HTTPStatusError as exc:
            raise HTTPException(status_code=502, detail=f"LLM call failed: {exc.response.text[:200]}")

        answer = f"{answer}\n\n**Sources:**\n" + "\n".join(source_lines)

    return OAIResponse(
        id=f"chatcmpl-{uuid.uuid4().hex[:12]}",
        model=req.model,
        choices=[OAIChoice(message=OAIMessage(role="assistant", content=answer))],
    )


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    collection = req.collection or DEFAULT_COLLECTION
    model = req.model or DEFAULT_MODEL

    try:
        vector = await _get_embedding(req.question)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"Embedding failed: {exc.response.text[:200]}")
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail="Embedding service unavailable — check OPENROUTER_API_KEY and network connectivity")

    results = await _search_qdrant(collection, vector, req.top_k)

    if not results:
        return ChatResponse(
            answer="I couldn't find relevant content in the knowledge base. Try ingesting some playlists first.",
            sources=[],
        )

    sources: list[Source] = []
    context_parts: list[str] = []

    for r in results:
        p = r["payload"]
        url = p.get("url", f"https://youtube.com/watch?v={p.get('video_id', '')}")
        start_time = p.get("start_time")
        timestamp_url = f"{url}&t={start_time}" if start_time is not None else None
        sources.append(Source(
            video_id=p.get("video_id", ""),
            title=p.get("title", "Unknown"),
            channel=p.get("channel", ""),
            text=p.get("text", ""),
            url=url,
            score=round(r["score"], 4),
            start_time=start_time,
            timestamp_url=timestamp_url,
            published_at=p.get("published_at"),
        ))
        context_parts.append(f"[{p.get('title', 'Unknown')}]\n{p.get('text', '')}")

    context = "\n\n---\n\n".join(context_parts)

    # Sort sources by recency so the most recent episode appears first.
    # Sources without published_at (legacy vectors) fall to the end.
    sources.sort(key=lambda s: s.published_at or "", reverse=True)

    try:
        answer = await _call_openrouter(req.question, context, model)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"LLM call failed: {exc.response.text[:200]}")

    return ChatResponse(answer=answer, sources=sources)

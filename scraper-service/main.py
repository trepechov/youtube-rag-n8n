import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException

logger = logging.getLogger(__name__)
from pydantic import BaseModel
from youtube_transcript_api import (
    YouTubeTranscriptApi,
    NoTranscriptFound,
    TranscriptsDisabled,
    VideoUnavailable,
    CouldNotRetrieveTranscript,
)
from youtube_transcript_api.proxies import GenericProxyConfig

app = FastAPI(title="YouTube Scraper Service", version="2.0.0")


class TranscriptRequest(BaseModel):
    video_id: str
    chunk_size: int = 600
    langs: list[str] = ["bg", "en", "en-GB"]
    title: str = "Unknown Title"
    channel: str = "Unknown Channel"


class Chunk(BaseModel):
    chunk_id: int
    text: str
    start_time: Optional[int] = None


class TranscriptResponse(BaseModel):
    video_id: str
    title: str
    channel: str
    url: str
    chunks: list[Chunk]
    cached: bool = False


# ---------------------------------------------------------------------------
# youtube-transcript-api client (singleton, proxy-aware)
# ---------------------------------------------------------------------------

def _build_api() -> YouTubeTranscriptApi:
    http_proxy  = os.environ.get("HTTP_PROXY_URL", "").strip()
    https_proxy = os.environ.get("HTTPS_PROXY_URL", "").strip() or http_proxy
    if http_proxy or https_proxy:
        return YouTubeTranscriptApi(
            proxy_config=GenericProxyConfig(
                http_url=http_proxy or https_proxy,
                https_url=https_proxy or http_proxy,
            )
        )
    return YouTubeTranscriptApi()

_yt_api = _build_api()

CACHE_DIR = Path(os.environ.get("TRANSCRIPT_CACHE_DIR", "/cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)


_VIDEO_ID_RE = re.compile(r'^[A-Za-z0-9._-]+$')


def _safe_video_id(video_id: str) -> str:
    if not _VIDEO_ID_RE.match(video_id):
        raise ValueError(f"Invalid video_id: {video_id!r}")
    return video_id


def _cache_path(video_id: str) -> Path:
    return CACHE_DIR / f"{_safe_video_id(video_id)}.json"


def _load_from_cache(video_id: str) -> dict | None:
    p = _cache_path(video_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        logger.warning("Corrupt or unreadable cache file %s — discarding", p)
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
        return None


def _save_to_cache(data: dict) -> None:
    target = _cache_path(data["id"])
    try:
        fd, tmp = tempfile.mkstemp(dir=CACHE_DIR, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps(data, ensure_ascii=False))
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            os.unlink(tmp)
            raise
        Path(tmp).replace(target)
    except OSError as exc:
        logger.error("Failed to write cache for %s: %s", data.get("id"), exc)


# ---------------------------------------------------------------------------
# Transcript extraction
# ---------------------------------------------------------------------------

def _download_transcript(
    video_id: str, langs: list[str], title: str, channel: str
) -> dict:
    try:
        transcript_list = _yt_api.list(video_id)
    except TranscriptsDisabled:
        raise RuntimeError("No subtitles found — captions are disabled for this video")
    except VideoUnavailable:
        raise RuntimeError("Video not accessible: VideoUnavailable")
    except CouldNotRetrieveTranscript as exc:
        # IpBlocked / RequestBlocked / AgeRestricted are subclasses — catch all here
        # and surface a clear message so operators know to configure a proxy.
        msg = str(exc)
        if "blocked" in msg.lower() or "ip" in msg.lower():
            raise RuntimeError(
                "YouTube blocked this request. "
                "Set HTTP_PROXY_URL / HTTPS_PROXY_URL on scraper-service "
                "to route via a residential proxy."
            )
        raise RuntimeError(f"Could not retrieve transcript list: {exc}")

    transcript = None

    # 1) Manually-uploaded transcript in any requested language.
    try:
        transcript = transcript_list.find_manually_created_transcript(langs)
    except NoTranscriptFound:
        pass

    # 2) Auto-generated transcript in any requested language.
    if transcript is None:
        try:
            transcript = transcript_list.find_generated_transcript(langs)
        except NoTranscriptFound:
            pass

    # 3) Translate any available transcript into the first requested language.
    if transcript is None and langs:
        for candidate in transcript_list:
            if candidate.is_translatable and any(
                t["language_code"] == langs[0] for t in candidate.translation_languages
            ):
                transcript = candidate.translate(langs[0])
                break

    # 4) Last resort: take the first transcript YouTube offers.
    if transcript is None:
        for candidate in transcript_list:
            transcript = candidate
            break

    if transcript is None:
        raise RuntimeError("No subtitles found — video has no captions in any language")

    try:
        fetched = transcript.fetch()
    except CouldNotRetrieveTranscript as exc:
        raise RuntimeError(f"Could not fetch transcript: {exc}")

    # 1.x returns FetchedTranscriptSnippet objects with .start/.text attributes.
    segments = [(int(snippet.start), snippet.text) for snippet in fetched]

    return {
        "id": video_id,
        "title": title,
        "channel": channel,
        "segments": segments,
    }


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def _chunk_segments(segments: list[tuple[int, str]], chunk_size: int) -> list[Chunk]:
    chunks: list[Chunk] = []
    current_text = ""
    current_start: int = 0
    chunk_id = 0

    for seg_start, seg_text in segments:
        sentences = re.split(r"(?<=[.!?])\s+", seg_text)
        for sentence in sentences:
            if not current_text:
                current_start = seg_start
            if len(current_text) + len(sentence) + (1 if current_text else 0) <= chunk_size:
                current_text = f"{current_text} {sentence}".strip() if current_text else sentence
            else:
                if current_text:
                    chunks.append(Chunk(chunk_id=chunk_id, text=current_text, start_time=current_start))
                    chunk_id += 1
                if len(sentence) > chunk_size:
                    for i in range(0, len(sentence), chunk_size):
                        chunks.append(Chunk(chunk_id=chunk_id, text=sentence[i:i + chunk_size], start_time=seg_start))
                        chunk_id += 1
                    current_text = ""
                else:
                    current_text = sentence
                    current_start = seg_start

    if current_text:
        chunks.append(Chunk(chunk_id=chunk_id, text=current_text, start_time=current_start))

    return chunks


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/transcript", response_model=TranscriptResponse)
def get_transcript(req: TranscriptRequest):
    if req.chunk_size < 1:
        raise HTTPException(status_code=400, detail="chunk_size must be a positive integer")
    try:
        _safe_video_id(req.video_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    cached = True
    data = _load_from_cache(req.video_id)
    if data is None:
        cached = False
        try:
            data = _download_transcript(req.video_id, req.langs, req.title, req.channel)
        except RuntimeError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except Exception as exc:
            logger.exception("Unexpected error fetching transcript for %s", req.video_id)
            raise HTTPException(status_code=500, detail="Internal server error")
        _save_to_cache(data)

    chunks = _chunk_segments(data["segments"], req.chunk_size)

    return TranscriptResponse(
        video_id=data["id"],
        title=data["title"],
        channel=data["channel"],
        url=f"https://www.youtube.com/watch?v={data['id']}",
        chunks=chunks,
        cached=cached,
    )

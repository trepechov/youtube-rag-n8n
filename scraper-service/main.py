import os
import re
import subprocess
import json
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="YouTube Scraper Service", version="1.0.0")


class TranscriptRequest(BaseModel):
    video_id: str
    chunk_size: int = 600
    langs: list[str] = ["bg-orig", "bg", "en", "en-GB"]


class Chunk(BaseModel):
    chunk_id: int
    text: str


class TranscriptResponse(BaseModel):
    video_id: str
    title: str
    channel: str
    url: str
    chunks: list[Chunk]


# ---------------------------------------------------------------------------
# yt-dlp transcript extraction
# ---------------------------------------------------------------------------

def _download_transcript(video_id: str, langs: list[str]) -> dict:
    url = f"https://www.youtube.com/watch?v={video_id}"
    cookies = os.environ.get("COOKIES_FILE", "")

    base_cmd = ["yt-dlp", "--no-warnings"]
    if cookies and Path(cookies).exists():
        base_cmd += ["--cookies", cookies]

    with tempfile.TemporaryDirectory() as tmpdir:
        # Step 1: metadata only (--print conflicts with --write-subs in newer yt-dlp)
        meta_result = subprocess.run(
            base_cmd + ["--dump-single-json", "--skip-download", url],
            capture_output=True, text=True, timeout=60,
        )
        metadata: dict = {}
        if meta_result.returncode == 0 and meta_result.stdout.strip():
            try:
                m = json.loads(meta_result.stdout.strip())
                metadata = {
                    "id": m.get("id", video_id),
                    "title": m.get("title", "Unknown Title"),
                    "channel": m.get("channel") or m.get("uploader", "Unknown Channel"),
                }
            except json.JSONDecodeError:
                pass
        if not metadata:
            metadata = {"id": video_id, "title": "Unknown Title", "channel": "Unknown Channel"}

        # Step 2: download subtitles (no --print here)
        sub_result = subprocess.run(
            base_cmd + [
                "--skip-download",
                "--write-subs",
                "--write-auto-subs",
                "--sub-langs", ",".join(langs),
                "--sub-format", "vtt",
                "--output", f"{tmpdir}/%(id)s",
                url,
            ],
            capture_output=True, text=True, timeout=180,
        )

        # Don't check returncode — yt-dlp may return non-zero if one lang gets
        # rate-limited while others succeeded. Check for files instead.
        vtt_files = sorted(Path(tmpdir).glob("*.vtt"))
        if not vtt_files:
            raise RuntimeError("No subtitles found — video may have no captions")

        transcript_text = _parse_vtt(vtt_files[0].read_text(encoding="utf-8"))

        return {
            "id": metadata["id"],
            "title": metadata["title"],
            "channel": metadata["channel"],
            "transcript": transcript_text,
        }


def _parse_vtt(content: str) -> str:
    seen: set[str] = set()
    lines: list[str] = []

    for line in content.splitlines():
        line = line.strip()
        # Skip timing/metadata lines
        if (
            not line
            or "-->" in line
            or re.match(r"^\d{2}:\d{2}", line)
            or line.startswith("WEBVTT")
            or line.startswith("NOTE")
            or re.match(r"^[0-9]+$", line)
        ):
            continue
        # Strip HTML tags and decode entities
        clean = re.sub(r"<[^>]+>", "", line)
        clean = clean.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&nbsp;", " ")
        clean = clean.strip()
        if clean and clean not in seen:
            lines.append(clean)
            seen.add(clean)

    return " ".join(lines)


def _chunk_text(text: str, chunk_size: int) -> list[str]:
    # Split on sentence boundaries, accumulate until chunk_size
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks: list[str] = []
    current = ""

    for sentence in sentences:
        if len(current) + len(sentence) + 1 <= chunk_size:
            current = f"{current} {sentence}".strip() if current else sentence
        else:
            if current:
                chunks.append(current)
            # If a single sentence exceeds chunk_size, hard-split it
            if len(sentence) > chunk_size:
                for i in range(0, len(sentence), chunk_size):
                    chunks.append(sentence[i : i + chunk_size])
                current = ""
            else:
                current = sentence

    if current:
        chunks.append(current)

    return [c for c in chunks if c.strip()]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/transcript", response_model=TranscriptResponse)
def get_transcript(req: TranscriptRequest):
    try:
        data = _download_transcript(req.video_id, req.langs)
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {exc}")

    chunks = _chunk_text(data["transcript"], req.chunk_size)

    return TranscriptResponse(
        video_id=data["id"],
        title=data["title"],
        channel=data["channel"],
        url=f"https://www.youtube.com/watch?v={data['id']}",
        chunks=[Chunk(chunk_id=i, text=t) for i, t in enumerate(chunks)],
    )

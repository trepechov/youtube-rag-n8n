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
    start_time: Optional[int] = None


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

        # Fast-fail before attempting download: yt-dlp metadata already contains
        # which subtitle tracks exist. Avoids two 180s download attempts on captionless videos.
        if meta_result.returncode == 0 and meta_result.stdout.strip():
            try:
                m = json.loads(meta_result.stdout.strip())
                has_auto = bool(m.get("automatic_captions"))
                has_manual = bool(m.get("subtitles"))
                if not has_auto and not has_manual:
                    raise RuntimeError("No subtitles found — video has no captions")
            except json.JSONDecodeError:
                pass

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

        # Fallback: if specified langs yielded nothing, grab whatever auto-subs exist.
        # Covers videos where auto-generated captions use an unexpected language code.
        if not vtt_files:
            subprocess.run(
                base_cmd + [
                    "--skip-download",
                    "--write-auto-subs",
                    "--sub-langs", "all",
                    "--sub-format", "vtt",
                    "--output", f"{tmpdir}/%(id)s",
                    url,
                ],
                capture_output=True, text=True, timeout=180,
            )
            vtt_files = sorted(Path(tmpdir).glob("*.vtt"))

        if not vtt_files:
            raise RuntimeError("No subtitles found — video may have no captions")

        segments = _parse_vtt(vtt_files[0].read_text(encoding="utf-8"))

        return {
            "id": metadata["id"],
            "title": metadata["title"],
            "channel": metadata["channel"],
            "segments": segments,
        }


def _parse_vtt(content: str) -> list[tuple[int, str]]:
    """Parse VTT into (start_seconds, text) segments, deduplicating rolling-window cues."""
    segments: list[tuple[int, str]] = []
    seen: set[str] = set()
    current_start: int = 0
    in_cue = False

    for raw in content.splitlines():
        line = raw.strip()

        if not line:
            in_cue = False
            continue

        if "-->" in line:
            start_str = line.split("-->")[0].strip()
            try:
                parts = start_str.split(":")
                if len(parts) == 3:
                    h, m, s = parts
                    current_start = int(h) * 3600 + int(m) * 60 + int(float(s.replace(",", ".")))
                elif len(parts) == 2:
                    m, s = parts
                    current_start = int(m) * 60 + int(float(s.replace(",", ".")))
            except (ValueError, IndexError):
                pass
            in_cue = True
            continue

        if (
            line.startswith("WEBVTT")
            or line.startswith("NOTE")
            or re.match(r"^[0-9]+$", line)
            or re.match(r"^\d{2}:\d{2}", line)
        ):
            continue

        if not in_cue:
            continue

        clean = re.sub(r"<[^>]+>", "", line)
        clean = (
            clean.replace("&amp;", "&")
                 .replace("&lt;", "<")
                 .replace("&gt;", ">")
                 .replace("&nbsp;", " ")
                 .strip()
        )
        if clean and clean not in seen:
            seen.add(clean)
            segments.append((current_start, clean))

    return segments


def _chunk_segments(segments: list[tuple[int, str]], chunk_size: int) -> list[Chunk]:
    """Chunk (start_seconds, text) segments into Chunk objects, recording each chunk's start time."""
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
    try:
        data = _download_transcript(req.video_id, req.langs)
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {exc}")

    chunks = _chunk_segments(data["segments"], req.chunk_size)

    return TranscriptResponse(
        video_id=data["id"],
        title=data["title"],
        channel=data["channel"],
        url=f"https://www.youtube.com/watch?v={data['id']}",
        chunks=chunks,
    )

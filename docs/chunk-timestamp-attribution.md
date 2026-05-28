# Video Timestamp Source Attribution

**Status:** Planned  
**Scope:** scraper-service, n8n workflow, chat-api, widget

---

## Problem

When the chat API answers a question it already returns a `sources` list with the video title and URL, but no indication of *where* in the video the answer came from. For long-form content (podcasts, lectures, conference talks) a bare video link forces the user to scrub manually. The VTT subtitle files downloaded by `yt-dlp` contain per-second timestamps for every line of spoken text — this data is currently discarded during parsing.

---

## Approach

Preserve VTT timestamps through the pipeline so that every Qdrant point carries a `start_time` field (seconds from the start of the video). The chat API returns this alongside the existing `url` field, enabling deeplinks of the form `https://youtube.com/watch?v=VIDEO_ID&t=SECONDS` that jump directly to the relevant moment.

No new infrastructure is required. The change is purely additive: one new integer field in the Qdrant payload, one new field in the API response.

---

## Data model change

### Qdrant point payload — before

```json
{
  "video_id": "dQw4w9WgXcQ",
  "title": "Episode 42",
  "channel": "My Podcast",
  "url": "https://youtube.com/watch?v=dQw4w9WgXcQ",
  "chunk_id": 7,
  "text": "...chunk text..."
}
```

### Qdrant point payload — after

```json
{
  "video_id": "dQw4w9WgXcQ",
  "title": "Episode 42",
  "channel": "My Podcast",
  "url": "https://youtube.com/watch?v=dQw4w9WgXcQ",
  "chunk_id": 7,
  "start_time": 754,
  "text": "...chunk text..."
}
```

`start_time` is the start position in seconds of the first VTT segment that contributed text to this chunk. It is `null` (field absent) for videos where no subtitle file was available.

---

## Component changes

### 1. `scraper-service/main.py`

#### `Chunk` dataclass

Add an optional `start_time` field:

```python
@dataclass
class Chunk:
    chunk_id: int
    text: str
    start_time: Optional[int] = None  # seconds from video start; None if no subtitles
```

#### `_parse_vtt()` — return segments instead of plain text

**Current signature:** `_parse_vtt(vtt_content: str) -> str`

**New signature:** `_parse_vtt(vtt_content: str) -> List[Tuple[int, str]]`

Returns a list of `(start_seconds, text)` tuples, one per deduplicated VTT cue. The caller is responsible for joining into chunks.

**Implementation notes:**

VTT files contain cues in this form:

```
00:12:34.560 --> 00:12:37.200
The key insight here is that inflation

00:12:37.100 --> 00:12:40.800
is fundamentally a monetary phenomenon
```

YouTube auto-captions use a rolling-window format where the same words appear in multiple successive cues. The existing `seen` set deduplication handles this for text — the new version applies the same deduplication while pairing each unique text line with the start time of the cue it first appeared in:

```python
def _parse_vtt(vtt_content: str) -> List[Tuple[int, str]]:
    segments: List[Tuple[int, str]] = []
    seen: set[str] = set()
    current_start: Optional[int] = None

    for line in vtt_content.splitlines():
        line = line.strip()
        if "-->" in line:
            # Parse "HH:MM:SS.mmm --> HH:MM:SS.mmm", take start time
            start_str = line.split("-->")[0].strip()
            h, m, s = start_str.replace(",", ".").split(":")
            current_start = int(h) * 3600 + int(m) * 60 + int(float(s))
        elif line and not line.startswith("WEBVTT") and not line.isdigit():
            cleaned = _strip_html(line)
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                segments.append((current_start or 0, cleaned))

    return segments
```

#### `_chunk_transcript()` — track start time per chunk

**Current:** receives plain `text: str`, splits on sentence boundaries, returns `List[Chunk]`.

**New:** receives `List[Tuple[int, str]]` from `_parse_vtt()`, accumulates segments the same way, and records the start time of the first segment added to each chunk.

```python
def _chunk_transcript(segments: List[Tuple[int, str]], chunk_size: int) -> List[Chunk]:
    chunks: List[Chunk] = []
    current_text = ""
    current_start: Optional[int] = None
    chunk_id = 0

    for (seg_start, seg_text) in segments:
        # Sentence-boundary splitting within a segment (existing logic)
        sentences = re.split(r'(?<=[.!?])\s+', seg_text)
        for sentence in sentences:
            if current_start is None:
                current_start = seg_start
            if len(current_text) + len(sentence) + 1 <= chunk_size:
                current_text = (current_text + " " + sentence).strip()
            else:
                if current_text:
                    chunks.append(Chunk(chunk_id=chunk_id, text=current_text, start_time=current_start))
                    chunk_id += 1
                current_text = sentence
                current_start = seg_start

    if current_text:
        chunks.append(Chunk(chunk_id=chunk_id, text=current_text, start_time=current_start))

    return chunks
```

#### `/transcript` endpoint response

The `Chunk` dataclass serialises to the HTTP response automatically. `start_time` will appear alongside `chunk_id` and `text` in the returned JSON:

```json
{
  "video_id": "dQw4w9WgXcQ",
  "title": "Episode 42",
  "chunks": [
    { "chunk_id": 0, "text": "...", "start_time": 0 },
    { "chunk_id": 1, "text": "...", "start_time": 754 },
    { "chunk_id": 2, "text": "...", "start_time": 801 }
  ]
}
```

---

### 2. n8n workflow — "Embed and Ingest Chunks" node

The Code node that builds Qdrant points currently constructs the payload from `chunk.chunk_id`, `chunk.text`, plus video-level fields. Add `start_time`:

```js
// inside the point-building loop
const point = {
  id: deterministicUUID(video_id, chunk.chunk_id),
  vector: embeddings[i],
  payload: {
    video_id,
    title,
    channel,
    url,
    chunk_id: chunk.chunk_id,
    start_time: chunk.start_time ?? null,   // ← new
    text: chunk.text,
  },
};
```

No other workflow nodes change.

---

### 3. `chat-api/main.py`

#### `Source` Pydantic model

```python
class Source(BaseModel):
    video_id: str
    title: str
    channel: str
    text: str
    url: str
    score: float
    start_time: Optional[int] = None        # ← new
    timestamp_url: Optional[str] = None     # ← new: url + &t=N
```

#### `/chat` endpoint — mapping Qdrant results to Source

```python
start_time = hit.payload.get("start_time")
timestamp_url = (
    f"{hit.payload['url']}&t={start_time}"
    if start_time is not None
    else None
)
Source(
    video_id=hit.payload["video_id"],
    title=hit.payload["title"],
    channel=hit.payload["channel"],
    text=hit.payload["text"],
    url=hit.payload["url"],
    score=hit.score,
    start_time=start_time,
    timestamp_url=timestamp_url,
)
```

The existing LLM context string does not need to change for the base feature. As a follow-on, `timestamp_url` can be injected into the context block so the model can cite it naturally in its answer.

---

### 4. Widget

Each source card gains a "Watch at MM:SS" link:

```js
function formatTime(seconds) {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
}

// In source card render:
if (source.timestamp_url) {
  html += `<a href="${source.timestamp_url}" target="_blank">
    Watch at ${formatTime(source.start_time)}
  </a>`;
}
```

---

## Edge cases

| Case | Handling |
|---|---|
| Video has no subtitles | `_parse_vtt()` returns `[]`; scraper falls back to empty transcript (existing behaviour). `start_time` is absent from the Qdrant payload. |
| `start_time` absent on a hit | `Source.start_time` and `Source.timestamp_url` are `null`; widget renders source card without the timestamp link. |
| Single sentence longer than `chunk_size` | Existing hard-split logic applies. The resulting sub-chunks inherit `current_start` from the segment that was being processed. |
| VTT cue with no preceding timestamp line | `current_start` defaults to `0`; the text still lands in a chunk. |
| Auto-caption timing drift | Deeplinks land within a few seconds of the quoted moment — acceptable for podcast/lecture use. Manual captions (when YouTube provides them) are near-frame-accurate. |

---

## Re-ingestion strategy

Existing Qdrant points were stored without `start_time`. Options:

**Option A — Forward-only:** New videos get timestamps; existing videos do not. `start_time` is nullable, so the API and widget handle both gracefully. Zero operational cost.

**Option B — Selective backfill:** Add a `force_refresh` boolean to the n8n Config node. When `true`, the "Check if Ingested" skip logic is bypassed and all videos are re-processed. Run once against the full playlist to backfill timestamps, then revert to normal operation.

Option A is recommended for the initial rollout. Option B is useful once the feature is stable and timestamp accuracy has been validated.

---

## What is NOT changing

- Qdrant collection schema (points, vectors, distance metric) — `start_time` is a payload field, not a vector dimension
- Embedding model or vector dimensions
- Idempotent re-run logic — unchanged
- Playlist fetch, Extract Video IDs, Check if Ingested nodes — unchanged
- `/chat` request schema — no new required fields

---

## Open questions

- **LLM context injection:** Should `timestamp_url` be included in the context block sent to the LLM so the model can reference it in its prose answer, or should citations remain widget-only? Injecting them produces more conversational answers ("as discussed at 12:34 in Episode 42…") but adds tokens.
- **End-time tracking:** Should each chunk also store `end_time`? Useful for highlighting a video range rather than jumping to a single point. Adds minor complexity; YouTube deeplinks only need `t=start` anyway. Defer unless a specific use case requires it.
- **`start_time` in Qdrant filter index:** No Qdrant payload index is needed now. If a future feature filters results to a specific time window ("what was discussed in the first 10 minutes?"), add a numeric payload index on `start_time` at that point.

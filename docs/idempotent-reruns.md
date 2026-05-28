# Idempotent Re-runs: Skip Already-Ingested Videos

**Status:** Completed  
**Scope:** n8n workflow only — no changes to scraper-service or chat-api

---

## Problems this addresses

### 1. "Collection already exists" 409 on re-run (secondary)

Every workflow run sends `PUT /collections/{name}` to Qdrant. The first run creates the collection; every subsequent run receives a 409 `Wrong input: Collection already exists!`.

The current node has `continueOnFail: true` and `ignore400: true`. However, `ignore400` only suppresses exactly HTTP 400 responses — not 409. This causes the node to output an error object downstream and shows the node as failed in the n8n UI, even if the workflow continues.

**Fix:** Replace the `PUT` with a `GET /collections/{name}` check node. If the collection already exists (HTTP 200) an IF node routes directly to "Fetch Playlist". If it does not exist (any non-200 / error) the original `PUT` create node runs first. Both paths then converge at "Fetch Playlist".

---

### 2. Every re-run re-processes every video (main feature)

The workflow loops over all videos in the playlist on every execution. Qdrant point IDs are deterministic (`hash(video_id + chunk_id)`), so upserts are idempotent at the data level — no corruption occurs. However, each re-run wastes:

- YouTube API quota (playlist fetch)
- yt-dlp download time per video
- OpenRouter embedding API calls (most expensive)
- Time proportional to full playlist size

**Fix:** Before calling the scraper, check whether any Qdrant point with `video_id == <id>` already exists. If it does, skip the video entirely.

---

## Workflow changes

### Current flow

```
Manual Trigger
  → Config
  → Create Qdrant Collection   ← PUT, fails with 409 on re-run
  → Fetch Playlist Page 1
  → Extract Video IDs
  → Loop Videos
      └─(each)→ Get Transcript
                → Embed and Ingest Chunks
                → Loop Videos  (feedback)
```

### New flow

```
Manual Trigger
  → Config
  → Check Collection Exists    ← GET /collections/{name}
  → IF collection missing?
      true  → Create Qdrant Collection   ← PUT (only when needed)
      false → (skip)
  → [Merge] Fetch Playlist Page 1
  → Extract Video IDs
  → Loop Videos
      └─(each)→ Check if Ingested       ← NEW: POST /scroll, limit 1
                → IF already ingested?
                    true  → Loop Videos  (skip, feedback)
                    false → Get Transcript
                             → Embed and Ingest Chunks
                             → Loop Videos  (feedback)
```

---

## New nodes

### Check Collection Exists

| Field | Value |
|---|---|
| Type | HTTP Request |
| Method | GET |
| URL | `={{ $('Config').first().json.qdrant_url + '/collections/' + $('Config').first().json.collection }}` |
| `continueOnFail` | true |

Output: HTTP 200 with collection info if it exists; error/non-200 if it does not.

---

### IF: Collection Missing?

| Field | Value |
|---|---|
| Type | IF |
| Condition | `{{ $json.status }}` does not equal `"ok"` |

- **True** (collection absent) → Create Qdrant Collection node  
- **False** (collection exists) → Fetch Playlist Page 1 node  

The existing "Create Qdrant Collection" node output also connects to "Fetch Playlist Page 1" so both branches converge there.

---

### Check if Ingested

Placed between "Loop Videos" (output 1) and "Get Transcript". Implemented as a **Code node** (same pattern as "Embed and Ingest Chunks") to avoid n8n HTTP Request node limitations with dynamic JSON body expressions.

```js
const config = $('Config').first().json;
const video_id = $input.first().json.video_id;

const resp = await this.helpers.httpRequest({
  method: 'POST',
  url: config.qdrant_url + '/collections/' + config.collection + '/points/scroll',
  body: {
    filter: { must: [{ key: 'video_id', match: { value: video_id } }] },
    limit: 1,
    with_payload: false,
    with_vector: false,
  },
  json: true,
});

const exists = resp.result && resp.result.points && resp.result.points.length > 0;
return [{ json: { video_id, already_ingested: exists } }];
```

Output: `{ video_id: "...", already_ingested: true | false }`

---

### IF: Already Ingested?

| Field | Value |
|---|---|
| Type | IF |
| Condition | `{{ $json.already_ingested }}` equals `true` |

- **True** (video exists in Qdrant) → Loop Videos node (skips to next video)  
- **False** (video not ingested) → Get Transcript node (existing path)  

Note: "Get Transcript" reads `video_id` from `$('Loop Videos').first().json.video_id` (not `$json`) since `$json` at that point contains the scroll check output.

---

## Node positions (x, y)

| Node | x | y |
|---|---|---|
| Manual Trigger | 220 | 300 |
| Config | 440 | 300 |
| Check Collection Exists | 660 | 300 |
| IF: Collection Missing? | 880 | 300 |
| Create Qdrant Collection | 1100 | 180 |
| Fetch Playlist Page 1 | 1320 | 300 |
| Extract Video IDs | 1540 | 300 |
| Loop Videos | 1760 | 300 |
| Check if Ingested | 1980 | 300 |
| IF: Already Ingested? | 2200 | 300 |
| Get Transcript | 2420 | 300 |
| Embed and Ingest Chunks | 2640 | 300 |

---

## Connections summary

| From | Output | To |
|---|---|---|
| Manual Trigger | main[0] | Config |
| Config | main[0] | Check Collection Exists |
| Check Collection Exists | main[0] | IF: Collection Missing? |
| IF: Collection Missing? | true (main[0]) | Create Qdrant Collection |
| IF: Collection Missing? | false (main[1]) | Fetch Playlist Page 1 |
| Create Qdrant Collection | main[0] | Fetch Playlist Page 1 |
| Fetch Playlist Page 1 | main[0] | Extract Video IDs |
| Extract Video IDs | main[0] | Loop Videos |
| Loop Videos | main[0] | *(done — no connection)* |
| Loop Videos | main[1] | Check if Ingested |
| Check if Ingested | main[0] | IF: Already Ingested? |
| IF: Already Ingested? | true (main[0]) | Loop Videos |
| IF: Already Ingested? | false (main[1]) | Get Transcript |
| Get Transcript | main[0] | Embed and Ingest Chunks |
| Embed and Ingest Chunks | main[0] | Loop Videos |

---

## What is NOT changing

- `scraper-service/main.py` — no changes
- `chat-api/main.py` — no changes
- Docker Compose files — no changes
- Qdrant data schema (point IDs, payload fields) — no changes

---

## Open questions

- **Re-upload / transcript correction:** If a video's transcript is corrected on YouTube, "video seen = skip always" will miss the update. For v1 this is acceptable — re-processing is an edge case. A future `force_refresh` flag in Config could override the skip.
- **Interaction with multi-playlist feature:** A video appearing in two playlists will only be scraped once (correct). When `playlist_id` tagging is added, the skip check should also verify the `playlist_id` payload field exists on existing points to handle the migration case.

# Implementation Plan: Pagination + Multi-playlist

## Context

`n8n/workflows/youtube-rag-ingestion.json` currently has a `Fetch Playlist Page 1` HTTP node
that calls the YouTube Data API with `maxResults=50` and no `pageToken`. Any playlist with
more than 50 videos is silently truncated. Fixing pagination is the prerequisite for
multi-playlist, so the features are ordered accordingly.

---

## Feature 1: Full playlist pagination

### What changes
Replace two nodes (`Fetch Playlist Page 1` + `Extract Video IDs`) with one Code node
(`Fetch All Videos`) that paginates internally until `nextPageToken` is absent.

### Why a Code node instead of an n8n loop
An n8n loop node (SplitInBatches) cannot carry the accumulating list of video IDs across
iterations without extra state nodes. A single Code node with an internal `while` loop
is simpler: one node, no extra wiring, same output shape.

### Steps

1. ~~**Delete** the `Fetch Playlist Page 1` HTTP node.~~ ✅
2. ~~**Delete** the `Extract Video IDs` Code node.~~ ✅
3. ~~**Add** a new Code node named `Fetch All Videos` between `Ensure Collection` and
   `Loop Videos`, with the following logic:~~  ✅

   ```js
   const config = $('Config').first().json;
   const baseUrl = 'https://www.googleapis.com/youtube/v3/playlistItems';
   const allVideos = [];
   let pageToken = null;

   do {
     const params = new URLSearchParams({
       part: 'snippet',
       playlistId: config.playlist_id,
       maxResults: '50',
       key: config.youtube_api_key,
       ...(pageToken ? { pageToken } : {}),
     });

     const resp = await this.helpers.httpRequest({
       method: 'GET',
       url: `${baseUrl}?${params}`,
       json: true,
     });

     for (const item of (resp.items || [])) {
       const videoId = item.snippet?.resourceId?.videoId;
       if (videoId) {
         allVideos.push({
           video_id:        videoId,
           published_at:    item.snippet.publishedAt || null,
           collection:      config.collection,
           chunk_size:      config.chunk_size,
           embedding_model: config.embedding_model,
         });
       }
     }

     pageToken = resp.nextPageToken || null;
   } while (pageToken);

   return allVideos.map(v => ({ json: v }));
   ```

4. ~~**Wire**: `Ensure Collection` → `Fetch All Videos` → `Loop Videos`
   (same connection that `Extract Video IDs` previously had to `Loop Videos`).~~ ✅

5. **Verify**: re-export the workflow JSON, import into n8n, run against a playlist
   with >50 videos, confirm all videos are present in `Loop Videos` input.

### Output shape (unchanged)
Each item emitted is `{ video_id, published_at, collection, chunk_size, embedding_model }` —
identical to what `Extract Video IDs` was producing, so no downstream node needs changing.

---

## Feature 2: Multiple playlists per run

Builds on Feature 1. Wraps the per-playlist fetch+ingest flow inside a playlist-level loop.
Each playlist carries a human-readable **name** that flows through as metadata and enables
filtering / referencing by playlist in Qdrant queries.

### Config format

`playlist_ids` uses `id:name` pairs, comma-separated:

```
PL111:My Course,PL222:Interview Prep,PL333:Short Clips
```

A bare ID with no colon (e.g. `PL111`) is still valid — the name defaults to the playlist ID.
A single playlist needs no comma.

### What changes

| Node | Action |
|------|--------|
| Config — `playlist_id` field | Rename to `playlist_ids`, value becomes `id:name` CSV e.g. `"PL111:My Course,PL222:Tutorials"` |
| New Code node `Split Playlist IDs` | Parses the CSV into `{ playlist_id, playlist_name }` items |
| New SplitInBatches node `Loop Playlists` | Iterates over the parsed items one at a time |
| `Fetch All Videos` | Reads `playlist_id` and passes `playlist_name` forward on every video item |
| Video items | Gain a `playlist_name` field alongside existing fields |
| `Embed and Ingest Chunks` | Stores `playlist_name` in each chunk's Qdrant payload metadata |
| `Loop Videos` done-output (port 0) | Re-route from `Fetch Final Stats` → `Loop Playlists` |
| `Loop Playlists` done-output (port 0) | Connect to `Fetch Final Stats` |

### Steps

1. **Config node**: change field `playlist_id` → `playlist_ids`, update default value to
   `"REPLACE_WITH_PLAYLIST_ID:My Playlist"` (colon-separated `id:name`; comma-separate for
   multiple playlists).

2. **Add** Code node `Split Playlist IDs` immediately after `Ensure Collection`:

   ```js
   const raw = $('Config').first().json.playlist_ids;
   const entries = raw.split(',').map(s => s.trim()).filter(Boolean);
   return entries.map(entry => {
     const colonIdx = entry.indexOf(':');
     if (colonIdx === -1) return { json: { playlist_id: entry, playlist_name: entry } };
     return { json: {
       playlist_id:   entry.slice(0, colonIdx).trim(),
       playlist_name: entry.slice(colonIdx + 1).trim(),
     }};
   });
   ```

3. **Add** SplitInBatches node `Loop Playlists` (batchSize: 1) after `Split Playlist IDs`.

4. **Wire** the top of the graph:
   `Ensure Collection` → `Split Playlist IDs` → `Loop Playlists`

5. **Update `Fetch All Videos`**: read `playlist_id` and `playlist_name` from the loop item
   and attach `playlist_name` to every video item emitted:

   ```js
   const loopItem   = $('Loop Playlists').first().json;
   const playlistId = loopItem.playlist_id;
   const playlistName = loopItem.playlist_name;
   const config     = $('Config').first().json;
   // ... (existing pagination logic, replacing config.playlist_id with playlistId) ...
   allVideos.push({
     video_id:        videoId,
     published_at:    item.snippet.publishedAt || null,
     collection:      config.collection,
     chunk_size:      config.chunk_size,
     embedding_model: config.embedding_model,
     playlist_name,          // ← new
   });
   ```

6. **Wire** `Loop Playlists` port 1 (loop) → `Fetch All Videos`.

7. **Update `Embed and Ingest Chunks`** (the node that writes to Qdrant): include
   `playlist_name` in the payload metadata so it is stored on every chunk point:

   ```json
   { "playlist_name": "{{ $json.playlist_name }}" }
   ```

   This makes the field filterable via Qdrant's `must: [{ key: "playlist_name", match: { value: "..." } }]`
   filter — enabling per-playlist queries and references.

8. **Re-route `Loop Videos` done-output**:
   - Remove connection: `Loop Videos` port 0 → `Fetch Final Stats`
   - Add connection: `Loop Videos` port 0 → `Loop Playlists` (feeds back to advance the outer loop)

9. **Wire** `Loop Playlists` port 0 (done, no more playlists) → `Fetch Final Stats`.

### Resulting high-level flow

```
Manual Trigger
  → Config
  → Ensure Collection
  → Split Playlist IDs          (emits { playlist_id, playlist_name } per entry)
  → Loop Playlists ──[port 1: each playlist]──→ Fetch All Videos
                                                  → Loop Videos ──[port 1: each video]──→ Check if Ingested
                                                                                            → IF Already Ingested?
                                                                                              [yes] → Loop Videos
                                                                                              [no]  → Get Transcript
                                                                                                        → Has Transcript?
                                                                                                          [yes] → Embed and Ingest Chunks → Loop Videos
                                                                                                          [no]  → Loop Videos
                                                  ← Loop Videos ──[port 0: done]─────────────────────────────────┘
  ← Loop Playlists ──[port 0: done]──────────────────────────────────────────────────────────────────────────────┘
  → Fetch Final Stats
```

### README update
After both features land, update `README.md` / `.env.example`:
- Replace `PLAYLIST_ID` mention with `PLAYLIST_IDS` (`id:name` pairs, comma-separated).
- Show example: `PLAYLIST_IDS=PL111:My Course,PL222:Tutorials`
- Note that a single bare ID still works (no colon or comma needed).
- Document the `playlist_name` Qdrant payload field and how to filter by it.

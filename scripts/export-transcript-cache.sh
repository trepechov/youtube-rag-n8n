#!/usr/bin/env bash
# Exports the transcript_cache Docker volume to a tar.gz file.
# Usage: ./scripts/export-transcript-cache.sh [output-file]
# Default output: transcript_cache_YYYYMMDD.tar.gz

set -euo pipefail

VOLUME="youtube-rag-n8n_transcript_cache"
OUTPUT="${1:-transcript_cache_$(date +%Y%m%d).tar.gz}"

echo "Exporting volume '$VOLUME' → $OUTPUT ..."
docker run --rm \
  -v "${VOLUME}:/cache:ro" \
  -v "$(pwd):/backup" \
  alpine \
  tar czf "/backup/${OUTPUT}" -C /cache .

echo "Done. Transfer with:"
echo "  scp ${OUTPUT} user@prod-server:/path/to/youtube-rag-n8n/"

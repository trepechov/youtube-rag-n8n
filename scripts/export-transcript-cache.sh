#!/usr/bin/env bash
# Exports the transcript_cache Docker volume to a tar.gz file.
# Usage: ./scripts/export-transcript-cache.sh [output-file]
# Default output: transcript_cache_YYYYMMDD.tar.gz
# Override volume name: TRANSCRIPT_CACHE_VOLUME=myproject_transcript_cache ./scripts/export-transcript-cache.sh

set -euo pipefail

VOLUME="${TRANSCRIPT_CACHE_VOLUME:-youtube-rag-n8n_transcript_cache}"
OUTPUT="${1:-transcript_cache_$(date +%Y%m%d).tar.gz}"

# Resolve output to an absolute path so the container volume mount works regardless of
# whether the caller passes a relative or absolute path.
mkdir -p "$(dirname "${OUTPUT}")"
OUTPUT_ABS="$(cd "$(dirname "${OUTPUT}")" && pwd)/$(basename "${OUTPUT}")"
OUTPUT_DIR="$(dirname "${OUTPUT_ABS}")"
OUTPUT_FILE="$(basename "${OUTPUT_ABS}")"

echo "Exporting volume '$VOLUME' → ${OUTPUT_ABS} ..."
docker run --rm \
  -v "${VOLUME}:/cache:ro" \
  -v "${OUTPUT_DIR}:/backup" \
  alpine \
  tar czf "/backup/${OUTPUT_FILE}" -C /cache .

echo "Done. Transfer with:"
echo "  scp ${OUTPUT_ABS} user@prod-server:/path/to/youtube-rag-n8n/"

#!/usr/bin/env bash
# Imports a tar.gz file (from export-transcript-cache.sh) into the transcript_cache volume.
# Usage: ./scripts/import-transcript-cache.sh <file.tar.gz>
# Existing files with the same name are overwritten; other files in the volume are kept.
# Override volume name: TRANSCRIPT_CACHE_VOLUME=myproject_transcript_cache ./scripts/import-transcript-cache.sh <file>

set -euo pipefail

VOLUME="${TRANSCRIPT_CACHE_VOLUME:-youtube-rag-n8n_transcript_cache}"
INPUT="${1:-}"

if [[ -z "$INPUT" ]]; then
  echo "Usage: $0 <file.tar.gz>" >&2
  exit 1
fi

if [[ ! -f "$INPUT" ]]; then
  echo "File not found: $INPUT" >&2
  exit 1
fi

# Resolve to absolute path so it's accessible inside the container
INPUT_ABS="$(cd "$(dirname "$INPUT")" && pwd)/$(basename "$INPUT")"

echo "Importing $INPUT → volume '$VOLUME' ..."
docker run --rm \
  -v "${VOLUME}:/cache" \
  -v "${INPUT_ABS}:/import.tar.gz:ro" \
  alpine \
  tar xzf /import.tar.gz -C /cache

echo "Done. ${VOLUME} now contains the merged cache."

#!/usr/bin/env bash
# Imports a tar.gz file (from export-transcript-cache.sh) into the transcript_cache volume.
# Usage: ./scripts/import-transcript-cache.sh <file.tar.gz>
# Merges into the existing volume — already-cached files are overwritten, others kept.

set -euo pipefail

VOLUME="youtube-rag-n8n_transcript_cache"
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

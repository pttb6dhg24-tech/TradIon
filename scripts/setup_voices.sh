#!/bin/bash
# TradIon — F9: descarga el catálogo curado de voces Piper a models/piper/.
# El catálogo canónico vive en backend/voice_catalog.py: si añades una voz allí,
# añádela también aquí (y viceversa).
#
# Uso:  bash scripts/setup_voices.sh
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -x .venv/bin/python ]; then
    echo "ERROR: no existe .venv — crea el entorno primero (ver README)" >&2
    exit 1
fi

mkdir -p models/piper
.venv/bin/python -m piper.download_voices --data-dir models/piper \
    es_ES-davefx-medium \
    es_ES-carlfm-x_low \
    es_ES-sharvard-medium \
    es_MX-claude-high \
    es_MX-ald-medium \
    en_US-amy-medium \
    en_US-ryan-high \
    en_US-lessac-medium \
    en_GB-alan-medium \
    ko_KR-kss-medium

echo "OK — voces en models/piper/. Las previews y el f0 de referencia se generan"
echo "     automáticamente en el primer arranque del servidor."

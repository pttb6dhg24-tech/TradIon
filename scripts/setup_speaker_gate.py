#!/usr/bin/env python3
"""F11 — Descarga el modelo de embeddings de locutor del Speaker Gate (una vez).

Multiplataforma (Windows/macOS/Linux):
    python scripts/setup_speaker_gate.py

Modelo: CAM++ 3D-Speaker (sherpa-onnx, Apache-2.0, ~28 MB, nativo a 16 kHz).
Después: pip install sherpa-onnx, activar stt.speaker_gate.enabled en settings.yaml
y calibrar con scripts/bench_speaker_gate.py.
"""

import sys
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MODEL_NAME = "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx"
URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/"
       "speaker-recongition-models/" + MODEL_NAME)   # (sic: typo en el tag oficial)
DEST = PROJECT_ROOT / "models" / "speaker" / MODEL_NAME


def main() -> int:
    if DEST.exists() and DEST.stat().st_size > 20_000_000:
        print(f"Ya existe: {DEST} ({DEST.stat().st_size / 1e6:.1f} MB) — nada que hacer")
        return 0
    DEST.parent.mkdir(parents=True, exist_ok=True)
    print(f"Descargando {MODEL_NAME} (~28 MB)...")
    tmp = DEST.with_suffix(".part")
    urllib.request.urlretrieve(URL, tmp)          # noqa: S310 — URL fija del release oficial
    if tmp.stat().st_size < 20_000_000:
        tmp.unlink(missing_ok=True)
        print("ERROR: la descarga parece incompleta; reintenta", file=sys.stderr)
        return 1
    # os.replace (no rename): en Windows rename falla con FileExistsError si quedó
    # un modelo corrupto <=20 MB del guard de arriba — bucle sin salida
    tmp.replace(DEST)
    print(f"OK -> {DEST}")
    print("Siguiente paso: pip install sherpa-onnx  y  scripts/bench_speaker_gate.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

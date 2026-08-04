#!/usr/bin/env python3
"""F11 — Benchmark del Speaker Gate en ESTA máquina (ejecutar en la Victus).

    python scripts/bench_speaker_gate.py [voz1.wav voz2.wav ...]

Sin argumentos: mide ms/embedding y la separación con los previews del catálogo
Piper. OJO — los previews son voces SINTÉTICAS: separan más de lo que separarán
dos voces REALES del mismo idioma/sexo/familia (el caso que el gate existe para
cazar). El veredicto de este modo es solo una COTA INFERIOR de viabilidad.

CON argumentos (recomendado antes de activar el gate): pasa 2+ WAVs de las voces
REALES de la mesa (p. ej. los huella_*.wav de models/tmp durante una sesión, o
grabaciones de 5-10 s de cada comensal) y el benchmark imprime la matriz de
similitudes reales y los umbrales recomendados para settings.yaml.
Documenta los números en docs/DOCUMENTO_MAESTRO.md.
"""

import sys
import time
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.settings import load_settings          # noqa: E402
from backend.speaker_gate import SpeakerGate        # noqa: E402


def _load_16k(path: Path) -> np.ndarray:
    import soundfile as sf
    audio, sr = sf.read(str(path), dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if sr != 16000:
        n = int(audio.size * 16000 / sr)
        audio = np.interp(np.linspace(0, 1, n, endpoint=False),
                          np.linspace(0, 1, audio.size, endpoint=False),
                          audio).astype(np.float32)
    return audio


def _real_voice_mode(gate: "SpeakerGate", paths: list[Path]) -> int:
    """Matriz de similitudes entre voces REALES + recomendación de umbrales."""
    embs = {}
    for p in paths:
        audio = _load_16k(p)
        if audio.size < 16000:
            print(f"  (aviso) {p.name}: menos de 1 s de audio, poco fiable")
        embs[p.stem] = (gate.embed(audio),
                        gate.embed(audio[: audio.size // 2]),
                        gate.embed(audio[audio.size // 2:]))
    print("== Voces REALES: mismo hablante (mitad A vs B de cada WAV) ==")
    peor_mismo = 1.0
    for name, (_, a, b) in embs.items():
        sim = float(a @ b)
        peor_mismo = min(peor_mismo, sim)
        print(f"  {name}: {sim:.3f}")
    print("== Voces REALES: pares de hablantes distintos ==")
    nombres = list(embs)
    peor_ajeno = -1.0
    for i, n1 in enumerate(nombres):
        for n2 in nombres[i + 1:]:
            sim = float(embs[n1][0] @ embs[n2][0])
            peor_ajeno = max(peor_ajeno, sim)
            print(f"  {n1} vs {n2}: {sim:.3f}")
    margen = peor_mismo - peor_ajeno
    print(f"== Margen real mismo-vs-ajeno: {margen:.3f} ==")
    if margen < 0.15:
        print("⛔ Margen insuficiente con TUS voces: NO actives el gate "
              "(o solo en modo zona-gris subiendo reject muy bajo).")
        return 1
    reject = round(peor_ajeno + margen * 0.35, 2)
    accept = round(peor_mismo - margen * 0.35, 2)
    print(f"✅ Umbrales recomendados para settings.yaml -> "
          f"accept: {accept}  reject: {reject}")
    return 0


def main() -> int:
    settings = load_settings()
    settings.setdefault("stt", {}).setdefault("speaker_gate", {})["enabled"] = True
    gate = SpeakerGate(settings)
    if not gate.available:
        print("Gate no disponible: ¿ejecutaste scripts/setup_speaker_gate.py "
              "y pip install sherpa-onnx?", file=sys.stderr)
        return 1

    if len(sys.argv) > 2:
        return _real_voice_mode(gate, [Path(a) for a in sys.argv[1:]])

    previews = PROJECT_ROOT / "models" / "piper" / "previews"
    wavs = sorted(previews.glob("*.wav"))
    if len(wavs) < 3:
        print(f"Faltan previews en {previews} (ejecuta el servidor una vez "
              "o scripts/setup_voices.sh)", file=sys.stderr)
        return 1

    # 1) Coste por duración de clip (lo que pagará cada segmento real)
    base = _load_16k(wavs[0])
    print("== Coste por segmento ==")
    for seconds in (1, 2, 3):
        clip = np.tile(base, 3)[: seconds * 16000]
        t0 = time.perf_counter()
        for _ in range(10):
            gate.embed(clip)
        ms = (time.perf_counter() - t0) * 100          # /10 iteraciones, *1000 ms
        estado = "OK" if ms <= 50 else "LENTO (revisar num_threads)"
        print(f"  clip {seconds} s: {ms:.1f} ms/embedding  [{estado}]")

    # 2) Separación de identidades con el catálogo
    print("== Separación (coseno) ==")
    a = _load_16k(wavs[0])
    mismo = float(gate.embed(a[: a.size // 2]) @ gate.embed(a[a.size // 2:]))
    print(f"  MISMO hablante ({wavs[0].stem} A/B): {mismo:.3f}"
          f"  {'>= accept OK' if mismo >= gate.accept else '¡BAJO! revisar'}")
    embs = {w.stem: gate.embed(_load_16k(w)) for w in wavs[:6]}
    nombres = list(embs)
    peor = 0.0
    for i, n1 in enumerate(nombres):
        for n2 in nombres[i + 1:]:
            sim = float(embs[n1] @ embs[n2])
            peor = max(peor, sim)
            print(f"  {n1:14s} vs {n2:14s}: {sim:.3f}")
    margen = gate.reject - peor
    print(f"== Par ajeno más parecido: {peor:.3f} (margen {margen:+.3f} sobre "
          f"reject={gate.reject}) ==")
    print("⚠️  AVISO: estas son voces SINTÉTICAS del catálogo — separan MÁS que dos")
    print("   voces reales del mismo idioma/sexo/familia (el caso real del gate).")
    print("   Antes de fiarte del REJECT, repite con voces reales de la mesa:")
    print("     python scripts/bench_speaker_gate.py voz1.wav voz2.wav")
    if margen < 0.05:
        print("⛔ Margen sintético ya es estrecho: NO actives el gate sin la "
              "calibración con voces reales.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

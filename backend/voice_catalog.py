"""
TradIon — F9: Librería Biométrica de Voces (catálogo Piper + matcher por f0).

Piezas:
- CATALOG: voces predefinidas agrupadas por idioma/género/tono (archivos .onnx en
  models/piper/, descargados por scripts/setup_voices.sh). Verificado en el M3:
  RTF 0.03-0.11 (sub-segundo real).
- Previews: al primer arranque se sintetiza una muestra corta por voz
  (models/piper/previews/<id>.wav) que sirve para (a) el botón "Escuchar muestra"
  del lobby y (b) medir el f0 de referencia de cada voz (cacheado en JSON).
- Matcher: estima el f0 mediano del usuario a partir del audio del enrollment
  (autocorrelación con puerta de energía — sin dependencias nuevas) y elige la voz
  del catálogo con f0 más cercano EN ESCALA LOGARÍTMICA (percepción de tono).

Honestidad del matcher: f0 = rango tonal, no timbre. Asigna una voz "afín"
(grave/media/aguda como la tuya), no una imitación. La clonación real sigue
disponible con tts.backend: f5tts.
"""
from __future__ import annotations

import io
import json
import logging
import math
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

import numpy as np
import soundfile as sf

from backend.settings import PROJECT_ROOT

logger = logging.getLogger("tradion.voices")


@dataclass(frozen=True)
class VoiceProfile:
    id: str            # id estable expuesto al cliente
    lang: str          # es | ko | en
    label: str         # nombre visible
    gender: str        # M | F
    tone: str          # grave | medio | agudo
    onnx: str          # archivo en models/piper/
    speaker_id: Optional[int] = None   # voces multi-hablante (sharvard)
    # Licencia del CORPUS con el que se entrenó la voz (auditoría 2026-08-05, ver
    # THIRD_PARTY_LICENSES.md). 'commercial' es el campo que decide si la voz puede
    # servirse con licensing.commercial_use activo:
    #   ok        -> corpus de dominio público o permisivo
    #   atribuir  -> permitido citando al autor del corpus
    #   no        -> corpus NO comercial: prohibido cobrar por su uso
    #   incierto  -> licencia no acreditable: se trata como 'no' por prudencia
    license: str = "incierto"
    commercial: str = "incierto"


# Curado a mano desde rhasspy/piper-voices (todas verificadas descargables).
# El f0 de referencia NO se anota aquí: se mide sobre la preview real (cache JSON).
CATALOG: list[VoiceProfile] = [
    # --- Español ---
    VoiceProfile("es-davefx",   "es", "Dave (España)",     "M", "medio",  "es_ES-davefx-medium.onnx",
                 license="CC0-1.0 (corpus Nabu Casa)", commercial="ok"),
    VoiceProfile("es-carlfm",   "es", "Carl (España)",     "M", "grave",  "es_ES-carlfm-x_low.onnx",
                 license="Dominio público", commercial="ok"),
    # speaker_id verificado por f0 MEDIDO: 0 -> 122 Hz (M), 1 -> 200 Hz (F)
    VoiceProfile("es-sharvard-m", "es", "Hugo (España)",   "M", "medio",  "es_ES-sharvard-medium.onnx", speaker_id=0,
                 license="CC BY 3.0 (Edinburgh DataShare)", commercial="atribuir"),
    VoiceProfile("es-sharvard-f", "es", "Elena (España)",  "F", "medio",  "es_ES-sharvard-medium.onnx", speaker_id=1,
                 license="CC BY 3.0 (Edinburgh DataShare)", commercial="atribuir"),
    VoiceProfile("es-claude",   "es", "Claudia (México)",  "F", "agudo",  "es_MX-claude-high.onnx",
                 license="Sin corpus citado en el MODEL_CARD", commercial="incierto"),
    VoiceProfile("es-ald",      "es", "Aldo (México)",     "M", "medio",  "es_MX-ald-medium.onnx",
                 license="Unlicense (dominio público)", commercial="ok"),
    # --- English ---
    VoiceProfile("en-amy",      "en", "Amy (US)",          "F", "medio",  "en_US-amy-medium.onnx",
                 license="Sin licencia localizable", commercial="incierto"),
    VoiceProfile("en-ryan",     "en", "Ryan (US)",         "M", "medio",  "en_US-ryan-high.onnx",
                 license="CC BY-NC-SA 4.0 (RyanSpeech)", commercial="no"),
    VoiceProfile("en-lessac",   "en", "Lessac (US)",       "F", "medio",  "en_US-lessac-medium.onnx",
                 license="Blizzard Challenge 2013 (research only)", commercial="no"),
    VoiceProfile("en-alan",     "en", "Alan (UK)",         "M", "grave",  "en_GB-alan-medium.onnx",
                 license="Documentación contradictoria (Mycroft/mimic3)", commercial="incierto"),
    # --- 한국어 (limitación conocida: una sola voz publicada en piper-voices, y NO comercial:
    #     un despliegue de pago en coreano exige sustituirla) ---
    VoiceProfile("ko-kss",      "ko", "지수 (KSS)",         "F", "medio",  "ko_KR-kss-medium.onnx",
                 license="CC BY-NC-SA 4.0 (Korean Single Speaker)", commercial="no"),
]

PREVIEW_TEXTS = {
    "es": "Hola, así sonará tu voz en la mesa de traducción.",
    "en": "Hi! This is how your voice will sound at the table.",
    "ko": "안녕하세요, 번역 테이블에서 당신의 목소리는 이렇게 들립니다.",
}


# ---------- estimación de f0 (autocorrelación, sin dependencias nuevas) ----------

def estimate_f0(audio: np.ndarray, sample_rate: int,
                fmin: float = 65.0, fmax: float = 400.0) -> Optional[float]:
    """f0 mediano (Hz) de los tramos sonoros, por autocorrelación con puerta de
    energía. Suficiente para clasificar el RANGO tonal (no pretende ser CREPE)."""
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    audio = audio.astype(np.float32)
    frame = int(sample_rate * 0.04)          # 40 ms: >2 periodos a 65 Hz
    hop = int(sample_rate * 0.02)
    if audio.size < frame * 3:
        return None
    rms_all = np.sqrt(np.mean(audio ** 2)) or 1e-9
    lag_min = int(sample_rate / fmax)
    lag_max = int(sample_rate / fmin)
    f0s: list[float] = []
    for start in range(0, audio.size - frame, hop):
        segment = audio[start:start + frame]
        rms = np.sqrt(np.mean(segment ** 2))
        if rms < rms_all * 0.5:              # puerta: solo tramos con voz franca
            continue
        segment = segment - segment.mean()
        acf = np.correlate(segment, segment, mode="full")[frame - 1:]
        if acf[0] <= 0:
            continue
        acf /= acf[0]
        window = acf[lag_min:lag_max]
        if window.size == 0:
            continue
        lag = lag_min + int(np.argmax(window))
        if acf[lag] < 0.45:                  # sin periodicidad clara: tramo sordo
            continue
        f0s.append(sample_rate / lag)
    if len(f0s) < 5:
        return None
    return float(np.median(f0s))


# ---------- catálogo en runtime ----------

class VoiceCatalog:
    """Carga perezosa de voces Piper + previews + f0 de referencia + matcher."""

    def __init__(self, settings: dict[str, Any]) -> None:
        piper_cfg = settings.get("tts", {}).get("piper") or {}
        self.voices_dir = PROJECT_ROOT / str(piper_cfg.get("voices_dir", "models/piper"))
        self.previews_dir = self.voices_dir / "previews"
        self.previews_dir.mkdir(parents=True, exist_ok=True)
        self._f0_cache_path = self.voices_dir / "voice_f0_cache.json"
        self._voices: dict[str, Any] = {}      # onnx filename -> PiperVoice
        self._locks: dict[str, threading.Lock] = {}
        self._load_lock = threading.Lock()     # A5: carga perezosa segura con tts.workers > 1
        self._f0: dict[str, float] = {}
        self.profiles: dict[str, VoiceProfile] = {}
        for profile in CATALOG:
            if (self.voices_dir / profile.onnx).exists():
                self.profiles[profile.id] = profile
            else:
                logger.warning("Voz '%s' ausente (%s): ejecuta scripts/setup_voices.sh",
                               profile.id, profile.onnx)
        if not self.profiles:
            raise RuntimeError(
                f"No hay voces Piper en {self.voices_dir}. Ejecuta: bash scripts/setup_voices.sh"
            )

    # ---- carga y síntesis ----

    def _voice_for(self, profile: VoiceProfile):
        voice = self._voices.get(profile.onnx)
        if voice is None:
            # Double-checked locking: dos hilos del executor de TTS no deben cargar
            # el mismo ONNX a la vez (corrupción/doble RAM) — auditoría A5
            with self._load_lock:
                voice = self._voices.get(profile.onnx)
                if voice is None:
                    from piper import PiperVoice  # import tardío: opcional si backend=f5tts
                    voice = PiperVoice.load(str(self.voices_dir / profile.onnx))
                    self._locks[profile.onnx] = threading.Lock()
                    self._voices[profile.onnx] = voice
                    logger.info("Voz Piper cargada: %s", profile.onnx)
        return voice

    def preload(self, profile: VoiceProfile) -> None:
        """Carga el ONNX de una voz si aún no está en RAM (pre-calentamiento tras el
        enroll). La carga perezosa costaba 2-4 s EN MEDIO de la primera frase de la
        mesa (medido en la Victus: 'Voz Piper cargada' + TTS 4009 ms vs 76 ms después)."""
        self._voice_for(profile)

    def synthesize_pcm(self, profile: VoiceProfile, text: str) -> tuple[np.ndarray, int]:
        """PCM float32 + sample_rate. Lock por modelo (sesiones onnxruntime compartidas)."""
        from piper.config import SynthesisConfig
        voice = self._voice_for(profile)
        config = SynthesisConfig(speaker_id=profile.speaker_id) \
            if profile.speaker_id is not None else None
        with self._locks[profile.onnx]:
            chunks = list(voice.synthesize(text, syn_config=config))
        if not chunks:
            return np.zeros(0, dtype=np.float32), 22050
        sample_rate = chunks[0].sample_rate
        audio = np.concatenate([c.audio_float_array for c in chunks]).astype(np.float32)
        return audio, sample_rate

    # ---- previews + f0 de referencia ----

    def ensure_previews(self) -> None:
        """Primer arranque: genera la muestra de cada voz y mide su f0 (cache JSON)."""
        try:
            self._f0 = json.loads(self._f0_cache_path.read_text())
        except (OSError, ValueError):
            self._f0 = {}
        for profile in self.profiles.values():
            wav_path = self.previews_dir / f"{profile.id}.wav"
            if wav_path.exists() and profile.id in self._f0:
                continue
            text = PREVIEW_TEXTS.get(profile.lang, PREVIEW_TEXTS["es"])
            try:
                audio, sample_rate = self.synthesize_pcm(profile, text)
                sf.write(str(wav_path), audio, sample_rate, format="WAV", subtype="PCM_16")
                f0 = estimate_f0(audio, sample_rate)
                if f0:
                    self._f0[profile.id] = round(f0, 1)
                logger.info("Preview '%s' generada (f0 ref: %s Hz)", profile.id,
                            self._f0.get(profile.id, "?"))
            except Exception:
                logger.exception("No se pudo generar la preview de '%s'", profile.id)
        self._f0_cache_path.write_text(json.dumps(self._f0, indent=1))

    def preview_path(self, voice_id: str) -> Optional[Path]:
        if voice_id not in self.profiles:
            return None
        path = self.previews_dir / f"{voice_id}.wav"
        return path if path.exists() else None

    # ---- API pública ----

    def as_json(self) -> list[dict]:
        return [{**asdict(p), "f0_hz": self._f0.get(p.id)} for p in self.profiles.values()]

    def f0_of(self, voice_id: str) -> Optional[float]:
        return self._f0.get(voice_id)

    def default_for(self, lang: str) -> Optional[VoiceProfile]:
        for profile in self.profiles.values():
            if profile.lang == lang:
                return profile
        return None

    def match_by_f0(self, user_f0: Optional[float], lang: str) -> Optional[VoiceProfile]:
        """Voz del idioma con f0 de referencia más cercano en escala log (percepción
        de tono). Sin f0 medible -> la primera voz del idioma."""
        candidates = [p for p in self.profiles.values() if p.lang == lang]
        if not candidates:
            return None
        if not user_f0:
            return candidates[0]
        scored = [
            (abs(math.log(self._f0.get(p.id, 150.0)) - math.log(user_f0)), p.id, p)
            for p in candidates
        ]
        scored.sort()
        best = scored[0][2]
        logger.info("Matcher f0: usuario %.0f Hz -> '%s' (%s, ref %.0f Hz)",
                    user_f0, best.id, best.label, self._f0.get(best.id, 0.0))
        return best

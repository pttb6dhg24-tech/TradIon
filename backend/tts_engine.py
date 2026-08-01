"""
TradIon — F9: fachada de síntesis con dos backends conmutables (settings tts.backend).

- "piper"  (DEFECTO): Librería Biométrica + motor ultra-rápido. Voces predefinidas del
  catálogo (backend/voice_catalog.py), asignadas por matcher de f0 sobre el enrollment
  o elegidas a mano en el lobby. MEDIDO en el M3: RTF 0.03-0.11 (sub-segundo real);
  la voz "equivalente" en cada idioma destino se resuelve por cercanía de f0.
- "f5tts": clonación Zero-Shot con F5-TTS (Flow Matching) usando la huella vocal del
  enroll. Calidad de imitación real, pero MEDIDO ~3.1 s de coste fijo + 0.5 s por
  segundo de audio en MPS: modo "calidad" no simultáneo.

Contrato común: synthesize(text, lang, client) -> bytes WAV PCM16 @44.1 kHz, ejecutado
SIEMPRE fuera del event loop (run_in_executor) y con anti-clipping (normalización por
pico solo si satura + clamp tras el remuestreo).
"""
from __future__ import annotations

import asyncio
import io
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

import numpy as np
import soundfile as sf
import torch
import torchaudio.functional as F_audio

from backend.settings import load_settings

logger = logging.getLogger("tradion.tts")

OUTPUT_RATE = 44100  # WebKit/iOS digiere mal WAV a 22.05/24 kHz en decodeAudioData


class TTSError(Exception):
    """Error tipado de la etapa de síntesis. Nunca se degrada en silencio (M2)."""


def _to_wav_44k(audio: np.ndarray, sample_rate: int) -> bytes:
    """float32 -> WAV PCM16 @44.1 kHz con la cadena anti-clipping verificada."""
    tensor = torch.from_numpy(np.asarray(audio, dtype=np.float32))
    peak = float(tensor.abs().max()) if tensor.numel() else 0.0
    if peak > 1.0:
        tensor = tensor * (0.95 / peak)   # normaliza SOLO si satura (conserva volumen)
    if sample_rate != OUTPUT_RATE:
        tensor = F_audio.resample(tensor, orig_freq=sample_rate, new_freq=OUTPUT_RATE)
    tensor = torch.clamp(tensor, min=-1.0, max=1.0)  # el sinc del resample re-crea overshoot
    result = tensor.numpy()
    if result.size == 0:
        raise TTSError("Síntesis devolvió audio vacío")
    buffer = io.BytesIO()
    sf.write(buffer, result, OUTPUT_RATE, format="WAV", subtype="PCM_16")
    return buffer.getvalue()


# ============================ Backend Piper (F9) ============================

class PiperBackend:
    """Catálogo de voces + matcher f0. La identidad tonal del hablante se traslada a
    cada idioma destino: se ancla su f0 (medido en el enroll, o el de la voz elegida
    a mano) y se busca la voz más cercana del idioma de destino."""

    name = "piper"

    def __init__(self, settings: dict[str, Any]) -> None:
        from backend.voice_catalog import VoiceCatalog
        self.catalog = VoiceCatalog(settings)
        self.catalog.ensure_previews()
        logger.info("PiperBackend listo: %d voces (%s)", len(self.catalog.profiles),
                    ", ".join(sorted(self.catalog.profiles)))

    def _profile_for(self, lang: str, client: Any) -> Any:
        # Prioridad: (1) voz fijada para ese idioma (asignación del enroll o set_voice
        # manual, ya en voice_by_lang), (2) matcher por el f0 del usuario, (3) defecto
        cached = getattr(client, "voice_by_lang", None)
        if cached is not None and lang in cached:
            return cached[lang]
        anchor_f0 = getattr(client, "user_f0", None) if client is not None else None
        profile = self.catalog.match_by_f0(anchor_f0, lang) or self.catalog.default_for(lang)
        if profile is None:
            raise TTSError(f"El catálogo no tiene voces para el idioma '{lang}'")
        if cached is not None:
            # setdefault atómico (A5): este código corre en el hilo del executor de TTS
            # mientras el event loop puede escribir la misma clave vía set_voice; si el
            # loop ya la puso, gana la elección explícita del usuario
            profile = cached.setdefault(lang, profile)
        return profile

    def synthesize_sync(self, text: str, lang: str, client: Any = None) -> bytes:
        text = text.strip()
        if not text:
            return b""
        profile = self._profile_for(lang, client)
        t0 = time.perf_counter()
        try:
            audio, sample_rate = self.catalog.synthesize_pcm(profile, text)
        except Exception as exc:
            raise TTSError(f"Piper falló ({profile.id}, {len(text)} chars): {exc}") from exc
        wav = _to_wav_44k(audio, sample_rate)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        logger.info("Piper[%s] (%s): %d chars -> %.1f s de audio en %.0f ms",
                    profile.id, getattr(client, "speaker_id", "?"), len(text),
                    audio.size / sample_rate, elapsed_ms)
        return wav

    def shutdown(self) -> None:
        pass


# =========================== Backend F5-TTS (F8) ============================

class F5TTSBackend:
    """Clonación Zero-Shot con la huella vocal del enroll (modo calidad, no simultáneo)."""

    name = "f5tts"

    def __init__(self, settings: dict[str, Any]) -> None:
        cfg = settings["tts"]
        try:
            from f5_tts.api import F5TTS
        except ImportError as exc:
            raise TTSError("F5-TTS no está instalado. Instálalo con:\n  pip install f5-tts") from exc

        if torch.backends.mps.is_available():
            self.device, self.dtype = "mps", torch.float16
        elif torch.cuda.is_available():
            self.device, self.dtype = "cuda", torch.float16
        else:
            self.device, self.dtype = "cpu", torch.float32

        logger.info("Cargando modelo F5-TTS en device=%s con dtype=%s...", self.device, self.dtype)
        try:
            self.model = F5TTS(device=self.device)
            self.model.ema_model.to(self.dtype)
        except Exception as exc:
            raise TTSError(f"Fallo crítico al cargar F5-TTS: {exc}") from exc

        self.nfe_step = int(cfg.get("nfe_step", 8))
        self._lock = threading.Lock()

        # Huella genérica de fallback (por si un cliente llega sin enroll)
        self.generic_ref_path = Path(settings["paths"]["models_dir"]) / "generic_ref.wav"
        if not self.generic_ref_path.exists():
            self.generic_ref_path.parent.mkdir(parents=True, exist_ok=True)
            sf.write(str(self.generic_ref_path), np.zeros(32000, dtype=np.float32), 16000, format="WAV")
        logger.info("F5-TTS cargado y listo para inferencia Zero-Shot.")

    def synthesize_sync(self, text: str, lang: str, client: Any = None) -> bytes:
        text = text.strip()
        if not text:
            return b""
        if not getattr(client, "ref_audio_path", None) or not getattr(client, "ref_text", None):
            logger.warning("(%s) Sin huella vocal: usando referencia de fallback.",
                           getattr(client, "speaker_id", "fallback"))
            ref_path, ref_text = str(self.generic_ref_path), "Esta es una voz de prueba."
        else:
            ref_path, ref_text = client.ref_audio_path, client.ref_text

        t0 = time.perf_counter()
        try:
            with self._lock:  # inferencia no thread-safe en MPS
                wav_out, sr, _ = self.model.infer(
                    ref_file=ref_path, ref_text=ref_text, gen_text=text,
                    nfe_step=self.nfe_step, speed=1.0,
                )
        except Exception as exc:
            raise TTSError(f"Síntesis F5-TTS falló ({len(text)} chars): {exc}") from exc
        wav = _to_wav_44k(np.asarray(wav_out, dtype=np.float32), sr)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        logger.info("F5-TTS (%s): %d chars -> %.0f ms",
                    getattr(client, "speaker_id", "?"), len(text), elapsed_ms)
        return wav

    def shutdown(self) -> None:
        pass


# ============================ Fachada pública ===============================

class TTSEngine:
    """Punto único de síntesis. El backend se elige en settings tts.backend."""

    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        self.settings = settings or load_settings()
        cfg = self.settings["tts"]
        backend_name = str(cfg.get("backend", "piper")).lower()
        if backend_name == "piper":
            self.backend = PiperBackend(self.settings)
        elif backend_name in ("f5tts", "f5-tts"):
            self.backend = F5TTSBackend(self.settings)
        else:
            raise TTSError(f"tts.backend desconocido: {backend_name!r} (usa 'piper' o 'f5tts')")
        self._executor = ThreadPoolExecutor(
            max_workers=int(cfg.get("workers", 1)), thread_name_prefix="tts"
        )
        # Executor PROPIO para el prewarm: si compartiera el único worker de síntesis,
        # un enroll a mitad de conversación encolaría 1-2 cargas de ONNX (2-4 s cada
        # una) POR DELANTE del siguiente chunk de un hablante activo (head-of-line).
        # El double-checked locking de _voice_for hace segura la carga concurrente.
        self._prewarm_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="tts-warm"
        )

    def prewarm_voices(self, client: Any) -> None:
        """Encola la carga de los ONNX ya asignados a un cliente (tras el enroll /
        set_voice / reconexión) en el executor de prewarm. No bloquea el event loop
        ni retrasa la síntesis en vivo de otros hablantes."""
        catalog = self.catalog
        if catalog is None:                      # backend f5tts: no hay voces que cargar
            return
        for profile in list(getattr(client, "voice_by_lang", {}).values()):
            self._prewarm_executor.submit(self._prewarm_one, catalog, profile)

    @staticmethod
    def _prewarm_one(catalog: Any, profile: Any) -> None:
        try:
            catalog.preload(profile)
        except Exception:                        # el prewarm jamás tumba nada: solo avisa
            logger.exception("Prewarm de la voz '%s' falló", getattr(profile, "id", "?"))

    @property
    def catalog(self):
        """Catálogo de voces (None con el backend de clonación)."""
        return getattr(self.backend, "catalog", None)

    def synthesize_sync(self, text: str, lang: str, client: Any = None) -> bytes:
        return self.backend.synthesize_sync(text, lang, client)

    async def synthesize(self, text: str, lang: str, client: Any = None) -> bytes:
        """No bloquea el event loop: SIEMPRE vía run_in_executor."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self.backend.synthesize_sync,
                                          text, lang, client)

    def shutdown(self) -> None:
        self.backend.shutdown()
        self._executor.shutdown(wait=False)
        self._prewarm_executor.shutdown(wait=False)


# ---------- smoke test ----------

def _demo(argv: list[str]) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    engine = TTSEngine()
    pruebas = [("es", "Hola, la mesa vuelve a hablar en tiempo real."),
               ("ko", "안녕하세요, 테이블이 다시 실시간으로 말합니다."),
               ("en", "Hello, the table speaks in real time again.")]
    if argv:
        pruebas = [(argv[1] if len(argv) > 1 else "es", argv[0])]
    for lang, frase in pruebas:
        t0 = time.perf_counter()
        wav = engine.synthesize_sync(frase, lang, None)
        print(f"{lang} [{(time.perf_counter() - t0) * 1000:6.0f} ms] {len(wav)} bytes WAV")
    engine.shutdown()


if __name__ == "__main__":
    import sys
    _demo(sys.argv[1:])

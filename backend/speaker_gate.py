"""F11 — Speaker Gate: verificación de hablante contra la huella vocal del enroll.

La guardia LID v2 (core_engine.transcribe_sync) caza la cross-captura ENTRE
idiomas; este gate caza la del MISMO idioma (dos hispanohablantes en la mesa) y
valida la identidad de CADA segmento: si la firma de locutor del audio no se
parece a la huella del dueño del micrófono, el segmento se descarta ANTES de
gastar GPU en Whisper.

Modelo: sherpa-onnx speaker embedding (CAM++ 3D-Speaker, Apache-2.0, 28 MB,
nativo a 16 kHz — encaja en el pipeline sin re-muestrear). Descarga:
    python scripts/setup_speaker_gate.py
MEDIDO en el M3 (CPU, 2 hilos) con los previews del catálogo Piper:
~29 ms/embedding; mismo hablante 0.79 de similitud coseno; hablantes distintos
0.06-0.31 (incluso mujer-vs-mujer entre idiomas). Doble umbral:
    score >= accept  -> es el dueño: pasa
    score <  reject  -> voz ajena: segmento descartado (telemetría)
    zona gris        -> pasa PERO se loguea (calibración en mesa sin perder voz)
APAGADO por defecto (stt.speaker_gate.enabled) hasta el benchmark en la Victus:
    python scripts/bench_speaker_gate.py
Con <min_speech_s de voz no se decide (el EER se dispara por debajo de ~2 s).
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

import numpy as np

from backend.settings import PROJECT_ROOT

logger = logging.getLogger("tradion.speaker_gate")


class SpeakerGate:
    """Extractor de embeddings + decisión por doble umbral. Fail-open: cualquier
    problema (paquete ausente, modelo sin descargar) lo DESACTIVA con un aviso —
    jamás rompe el arranque ni descarta voz por error de configuración."""

    def __init__(self, settings: dict[str, Any]) -> None:
        cfg = (settings.get("stt") or {}).get("speaker_gate") or {}
        self.enabled = bool(cfg.get("enabled", False))
        # MODO SOMBRA (enforce: false, el defecto): calcula y LOGUEA la similitud de
        # cada segmento contra la huella del enroll pero JAMÁS descarta — una sesión
        # normal se convierte en la calibración con voces reales. Solo con
        # enforce: true el veredicto 'reject' tira segmentos de verdad.
        self.enforce = bool(cfg.get("enforce", False))
        self.accept = float(cfg.get("accept", 0.55))
        self.reject = float(cfg.get("reject", 0.35))
        self.min_samples = int(float(cfg.get("min_speech_s", 1.0)) * 16000)
        self.available = False
        self._extractor = None
        self._lock = threading.Lock()      # sherpa no documenta thread-safety: serializar
        # Executor PROPIO de 1 hilo: ~30 ms/segmento no justifican competir con STT
        self.executor: Optional[ThreadPoolExecutor] = None

        if not self.enabled:
            logger.info("Speaker gate desactivado (stt.speaker_gate.enabled: false)")
            return
        model = Path(str(cfg.get("model", "models/speaker/"
                                 "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx")))
        if not model.is_absolute():
            model = PROJECT_ROOT / model
        try:
            import sherpa_onnx  # import tardío: dependencia opcional
        except ImportError:
            logger.warning("Speaker gate: sherpa-onnx no está instalado "
                           "(pip install sherpa-onnx) — gate DESACTIVADO")
            return
        if not model.exists():
            logger.warning("Speaker gate: falta el modelo %s — ejecuta "
                           "'python scripts/setup_speaker_gate.py' — gate DESACTIVADO", model)
            return
        try:
            config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=str(model), num_threads=int(cfg.get("num_threads", 2)))
            self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
        except Exception:
            logger.exception("Speaker gate: el modelo no cargó — gate DESACTIVADO")
            return
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="spk-gate")
        self.available = True
        logger.info("Speaker gate ACTIVO en modo %s: %s (accept>=%.2f, reject<%.2f, min %.1f s)",
                    "ENFORCE (descarta voz ajena)" if self.enforce
                    else "SOMBRA (solo telemetría, no descarta)",
                    model.name, self.accept, self.reject, self.min_samples / 16000)

    def embed(self, audio_16k_f32: np.ndarray) -> np.ndarray:
        """Embedding L2-normalizado de un clip mono float32 a 16 kHz. Hilo del gate."""
        with self._lock:
            stream = self._extractor.create_stream()
            stream.accept_waveform(16000, np.ascontiguousarray(audio_16k_f32, dtype=np.float32))
            stream.input_finished()
            emb = np.array(self._extractor.compute(stream), dtype=np.float32)
        norm = float(np.linalg.norm(emb))
        return emb / norm if norm > 0 else emb

    def score(self, audio_16k_f32: np.ndarray, ref_embedding: np.ndarray) -> float:
        """Similitud coseno del clip contra la referencia (ya normalizada)."""
        return float(self.embed(audio_16k_f32) @ ref_embedding)

    def decide(self, score: float) -> str:
        """'accept' | 'gray' | 'reject' según el doble umbral."""
        if score >= self.accept:
            return "accept"
        if score < self.reject:
            return "reject"
        return "gray"

    def shutdown(self) -> None:
        if self.executor is not None:
            self.executor.shutdown(wait=False)

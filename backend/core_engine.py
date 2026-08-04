"""
TradIon — F1: motor de escucha (VAD + segmentación + STT).

Reescritura completa según la auditoría del DOCUMENTO_MAESTRO §2, más una segunda
pasada de verificación adversarial sobre esta propia versión:
- C1/A1  Ventanas estrictas de 512 muestras a 16 kHz y normalización int16 -> float32 [-1, 1].
- C2     Cero bloqueo del event loop: inferencia en ThreadPoolExecutors, resultados por
         asyncio.Queue por sesión, y las instancias de VAD se pre-cargan en el arranque
         (crear una sesión no congela a los demás hablantes).
- C3     Cada sesión (hablante) usa su PROPIA instancia de Silero VAD, con reset_states().
- A2     Idioma obligatorio por sesión, validado contra languages.allowed del YAML.
- A3     STT desde np.ndarray en memoria; nunca rutas de archivo.
- A4     beam_size desde el YAML (1 por defecto).
- A5     Segmentador completo: VAD -> pre-roll -> buffer -> cierre por silencio/duración -> STT.
         El tope segment_max_seconds acota la duración TOTAL del audio (voz + pausas cortas).
- F5.2   Parciales en vivo (ventana deslizante): mientras el segmento sigue ABIERTO se
         transcribe periódicamente su cola (window_s) y se emite TranscriptionResult con
         partial=True. El resultado FINAL (partial=False) llega al cerrar el segmento y es
         el único que debe traducirse (la gramática SOV del coreano exige la frase entera).
- M1     Pesos de Whisper en models/ (download_root); Silero llega empaquetado en pip (sin torch.hub).
- M2     Errores tipados (EngineError y subclases); prohibidos los except silenciosos y centinelas.

Smoke test offline:
    python -m backend.core_engine                             # carga modelos (pre-descarga) y termina
    python -m backend.core_engine tests/audio_samples/x.wav es
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import numpy as np
import torch
from faster_whisper import WhisperModel
from silero_vad import load_silero_vad

from backend.settings import load_settings, resolve_dir

logger = logging.getLogger("tradion.core")


class EngineError(Exception):
    """Error base del motor. Nunca se degrada a valores centinela (auditoría M2)."""


class AudioFormatError(EngineError):
    pass


class ModelLoadError(EngineError):
    pass


class TranscriptionError(EngineError):
    pass


@dataclass(frozen=True)
class TranscriptionResult:
    speaker_id: str
    segment_id: int
    language: str
    text: str
    audio_seconds: float
    stt_ms: float                  # métrica de latencia de la etapa STT (§1.9)
    error: Optional[str] = None    # un fallo del pipeline es distinguible de "no dijo nada"
    partial: bool = False          # True = hipótesis en vivo del segmento ABIERTO (no traducir)
    crosstalk: bool = False        # True = el LID demostró voz AJENA en este micro: el
                                   # servidor usa esta evidencia para soltar el floor
    closed_at: float = 0.0         # time.monotonic() del CIERRE del segmento: la evidencia
                                   # crosstalk solo vale contra posesiones ANTERIORES a él
    voiced_rms: float = 0.0        # nivel de voz del segmento (adapta la calibración del micro)

    @property
    def ok(self) -> bool:
        return self.error is None


def _normalize_chunk(data: bytes | bytearray | memoryview | np.ndarray) -> np.ndarray:
    """bytes PCM16LE o ndarray (int16 / float) -> float32 mono en [-1, 1] (auditoría A1)."""
    if isinstance(data, (bytes, bytearray, memoryview)):
        raw = bytes(data)
        if len(raw) % 2 != 0:
            raise AudioFormatError(
                f"Chunk PCM16 de longitud impar ({len(raw)} bytes): el cliente debe enviar "
                "muestras completas de 2 bytes"
            )
        chunk: np.ndarray = np.frombuffer(raw, dtype=np.int16)
    elif isinstance(data, np.ndarray):
        chunk = data
    else:
        raise AudioFormatError(f"Tipo de audio no soportado: {type(data)!r}")

    if chunk.ndim > 1:
        chunk = np.squeeze(chunk)
    if chunk.ndim != 1:
        raise AudioFormatError(f"Se esperaba audio mono 1-D; llegó shape {chunk.shape}")

    if chunk.dtype == np.int16:
        return chunk.astype(np.float32) / 32768.0
    if chunk.dtype in (np.float32, np.float64):
        chunk = chunk.astype(np.float32)
        if chunk.size and float(np.abs(chunk).max()) > 1.5:
            raise AudioFormatError(
                "Audio float fuera de [-1, 1]: parece PCM sin normalizar; envía int16 o normaliza antes"
            )
        return chunk
    raise AudioFormatError(f"dtype de audio no soportado: {chunk.dtype}")


def voiced_rms(samples: np.ndarray, win: int = 512) -> float:
    """Nivel de voz de un clip: mediana del RMS de las ventanas CON señal. Calibra
    los umbrales RELATIVOS del floor v2 (enroll) y su adaptación en conversación."""
    if samples.size < win:
        return float(np.sqrt(np.mean(samples ** 2))) if samples.size else 0.0
    n = samples.size // win
    w = samples[: n * win].reshape(n, win)
    rms = np.sqrt(np.mean(w ** 2, axis=1))
    floor_level = max(0.008, float(np.percentile(rms, 40)))
    voiced = rms[rms > floor_level]
    return float(np.median(voiced)) if voiced.size else float(np.median(rms))


_REPEAT_RUN = re.compile(r"(.)\1{9,}")   # 10+ repeticiones seguidas del mismo carácter


def _looks_degenerate(text: str) -> bool:
    """Bucle de decodificación de Whisper que se cuela por logprob: 'Hmmmmm…' de 198
    caracteres (Victus, seg 41: se tradujo y se SINTETIZARON 13 s de emes) o
    '¿Hola? ¿Hola? ¿Hola? ¿Hola? ¿Hola?'. Tres firmas baratas:
    racha de un mismo carácter, diversidad de caracteres ínfima, o una misma
    palabra dominando la locución (≥5 veces y ≥80% de los tokens — 'sí sí sí sí'
    real de 4 repeticiones sigue pasando)."""
    compact = text.replace(" ", "").lower()
    if _REPEAT_RUN.search(compact):
        return True
    if len(compact) >= 20 and len(set(compact)) <= 3:
        return True
    tokens = re.findall(r"\w+", text.lower())
    if len(tokens) >= 5:
        _, freq = Counter(tokens).most_common(1)[0]
        if freq >= 5 and freq / len(tokens) >= 0.8:
            return True
    return False


class VoiceSession:
    """Estado de UN hablante: VAD propio (C3), segmentador y cola de resultados.

    El audio entra por feed() en chunks de cualquier tamaño (múltiplos de muestra);
    los resultados de STT salen por la cola `results` en cuanto cada segmento
    cerrado termina de transcribirse. Crear sesiones vía CoreEngine.create_session().
    """

    def __init__(self, engine: "CoreEngine", speaker_id: str, language: str, vad_model: Any,
                 seq_start: int = 0) -> None:
        audio_cfg = engine.settings["audio"]
        self.engine = engine
        self.speaker_id = speaker_id
        self.language = language
        self.results: "asyncio.Queue[TranscriptionResult]" = asyncio.Queue()

        # Instancia PROPIA de Silero: el estado recurrente de un hablante jamás toca el de otro (C3)
        self._vad = vad_model
        self._vad.reset_states()

        self._sample_rate = int(audio_cfg["sample_rate"])
        self._window = int(audio_cfg["vad_window_samples"])
        self._win_ms = 1000.0 * self._window / self._sample_rate
        self._threshold = float(audio_cfg["vad_threshold"])
        self._close_silence_ms = float(audio_cfg["segment_close_silence_ms"])
        self._max_segment_ms = float(audio_cfg["segment_max_seconds"]) * 1000.0
        self._min_speech_ms = float(audio_cfg["min_speech_ms"])
        pre_roll_windows = max(1, round(float(audio_cfg["pre_roll_ms"]) / self._win_ms))

        partial_cfg = engine.settings["stt"].get("partials") or {}
        self._partials_enabled = bool(partial_cfg.get("enabled", True))
        self._partial_interval_ms = float(partial_cfg.get("interval_ms", 1200))
        self._partial_min_speech_ms = float(partial_cfg.get("min_speech_ms", 800))
        self._partial_window = int(float(partial_cfg.get("window_s", 6.0)) * self._sample_rate)
        self._last_partial_ms = 0.0
        self._partial_inflight = False
        self._utterance_id = 0

        self._lock = asyncio.Lock()          # serializa feed/flush/close del mismo cliente
        self._pending = np.empty(0, dtype=np.float32)
        self._pre_roll: "deque[np.ndarray]" = deque(maxlen=pre_roll_windows)
        self._segment: list[np.ndarray] = []
        self._in_speech = False
        self._speech_ms = 0.0                # solo ventanas con voz: filtro min_speech_ms
        self._segment_ms = 0.0               # duración TOTAL del segmento: tope duro real
        self._silence_ms = 0.0
        self._segment_seq = int(seq_start)   # monótono ENTRE sesiones del mismo speaker_id
        self._stt_tasks: set[asyncio.Task] = set()
        self._prev_stt: Optional[asyncio.Task] = None
        self._closed = False
        self.ref_embedding: Optional[np.ndarray] = None   # F11: huella de locutor (enroll)

    # ---------- API pública ----------

    async def feed(self, data: bytes | bytearray | memoryview | np.ndarray) -> None:
        """Ingesta un chunk de audio. No bloquea el event loop (C2)."""
        chunk = _normalize_chunk(data)
        async with self._lock:
            if self._closed:                 # comprobado BAJO el lock: sin carrera con close()
                raise EngineError(f"La sesión '{self.speaker_id}' ya está cerrada")
            windows = self._collect_windows(chunk)
            if not windows:
                return
            loop = asyncio.get_running_loop()
            probs = await loop.run_in_executor(self.engine.vad_executor, self._vad_probs, windows)
            for window, prob in zip(windows, probs):
                segment_audio = self._advance(window, prob)
                if segment_audio is not None:
                    self._launch_stt(segment_audio)

    async def flush(self) -> None:
        """Cierra el segmento en curso (p. ej. cuando el cliente deja de enviar)."""
        async with self._lock:
            self._flush_locked()

    async def close(self) -> None:
        """Cierra la sesión, espera los STT en vuelo y resetea el VAD. Idempotente."""
        async with self._lock:
            if not self._closed:
                self._flush_locked()
                self._closed = True
                # Seguro: el lock garantiza que ningún _vad_probs está en vuelo ahora mismo
                self._vad.reset_states()
        if self._stt_tasks:
            # El último segmento (el que flush existe para rescatar) llega a `results`
            # antes de que el caller pueda apagar los executors.
            await asyncio.gather(*self._stt_tasks, return_exceptions=True)

    @property
    def segments_launched(self) -> int:
        return self._segment_seq

    # ---------- internos (siempre bajo self._lock) ----------

    def _flush_locked(self) -> None:
        if self._in_speech and self._pending.size:
            # No perder la cola de la frase (< 1 ventana); ya no necesita pasar por el VAD
            self._segment.append(self._pending)
            self._segment_ms += 1000.0 * self._pending.size / self._sample_rate
        self._pending = np.empty(0, dtype=np.float32)
        segment_audio = self._force_close()
        if segment_audio is not None:
            self._launch_stt(segment_audio)

    def _collect_windows(self, chunk: np.ndarray) -> list[np.ndarray]:
        buffer = np.concatenate((self._pending, chunk)) if self._pending.size else chunk
        n_windows = buffer.size // self._window
        self._pending = buffer[n_windows * self._window:]
        return [buffer[i * self._window:(i + 1) * self._window] for i in range(n_windows)]

    def _vad_probs(self, windows: list[np.ndarray]) -> list[float]:
        # Ventanas ESTRICTAS de `vad_window_samples` (512 a 16 kHz): requisito de silero-vad v5 (C1).
        # Corre en el executor de VAD; el orden secuencial preserva el estado recurrente del hablante.
        return [
            float(self._vad(torch.from_numpy(np.ascontiguousarray(w)), self._sample_rate).item())
            for w in windows
        ]

    def _advance(self, window: np.ndarray, prob: float) -> Optional[np.ndarray]:
        """Máquina de estados del segmentador (A5). Devuelve el audio del segmento al cerrarse."""
        if prob >= self._threshold:
            if not self._in_speech:
                self._in_speech = True
                self._speech_ms = 0.0
                self._segment = list(self._pre_roll)   # pre-roll: no recortar el ataque de la frase
                self._segment_ms = self._win_ms * len(self._segment)
                self._pre_roll.clear()
                self._utterance_id += 1
                self._last_partial_ms = 0.0
            self._segment.append(window)
            self._speech_ms += self._win_ms
            self._segment_ms += self._win_ms
            self._silence_ms = 0.0
            self._maybe_partial()
        else:
            if self._in_speech:
                self._segment.append(window)           # cola de silencio: contexto para Whisper
                self._segment_ms += self._win_ms
                self._silence_ms += self._win_ms
                if self._silence_ms >= self._close_silence_ms:
                    return self._force_close()
            else:
                self._pre_roll.append(window)
                return None
        # Tope DURO sobre la duración total (voz + pausas cortas), no solo la voz:
        # el habla vacilante con pausas < close_silence_ms también debe cerrar a tiempo.
        if self._segment_ms >= self._max_segment_ms:
            return self._force_close()
        return None

    def _force_close(self) -> Optional[np.ndarray]:
        if not self._in_speech:
            return None
        segment_windows = self._segment
        speech_ms = self._speech_ms
        # Métricas del segmento CERRADO para el speaker-gate (F11): voz REAL (sin
        # pre-roll ni silencios) y cola de silencio de cierre — el clip completo
        # llega a ser ~70% relleno y decidir identidad sobre él era injusto
        self._closed_speech_ms = speech_ms
        self._closed_tail_ms = self._silence_ms
        self._segment = []
        self._in_speech = False
        self._speech_ms = 0.0
        self._segment_ms = 0.0
        self._silence_ms = 0.0
        if speech_ms < self._min_speech_ms:
            # Blip descartado, pero se re-siembra el pre-roll con el final del material
            # descartado para no recortar el ataque de una frase real inmediata.
            for w in segment_windows[-(self._pre_roll.maxlen or 1):]:
                if w.size == self._window:
                    self._pre_roll.append(w)
            logger.debug("(%s) blip de %.0f ms descartado", self.speaker_id, speech_ms)
            return None
        return np.concatenate(segment_windows)

    def _maybe_partial(self) -> None:
        """Ventana deslizante: hipótesis parcial del segmento ABIERTO, con coste acotado.
        Máximo una pasada en vuelo por hablante y solo cada interval_ms; se transcribe
        únicamente la cola (window_s) con el modelo pequeño dedicado. Bajo carga (todos
        los workers de finales ocupados) el parcial simplemente se salta: degradar es
        perder una hipótesis desechable, nunca retrasar un final o una calibración."""
        if (not self._partials_enabled
                or self.engine.partial_executor is None
                or self._partial_inflight
                or self.engine.heavy_pending >= self.engine.stt_workers
                or self._speech_ms < self._partial_min_speech_ms
                or self._segment_ms - self._last_partial_ms < self._partial_interval_ms):
            return
        self._partial_inflight = True
        self._last_partial_ms = self._segment_ms
        audio = np.concatenate(self._segment)
        if audio.size > self._partial_window:
            audio = audio[-self._partial_window:]
        task = asyncio.get_running_loop().create_task(
            self._run_partial(audio, self._utterance_id, self._speech_ms)
        )
        self._stt_tasks.add(task)
        task.add_done_callback(self._stt_tasks.discard)

    async def _run_partial(self, audio: np.ndarray, utterance_id: int,
                           speech_ms: float = 0.0) -> None:
        loop = asyncio.get_running_loop()
        # F11 — el gate también cubre los PARCIALES: sin esto, la voz del vecino
        # rechazada en el final se difundía igualmente EN VIVO hasta 15 s con la
        # atribución del dueño del micro, y nada la retractaba (el final vacío no
        # se difunde). De paso se ahorra el STT del modelo de parciales en los
        # rechazos. El suelo de voz es el MISMO que en los finales (min_speech_s
        # sobre la voz REAL acumulada): los 800 ms de partial_min_speech_ms quedan
        # por debajo del 1.0 s que el propio gate exige, y con pre-roll + relleno
        # el embedding decidía —incluido el REJECT destructivo con enforce— en el
        # régimen de error alto (<2 s). Hallazgo adversarial del encendido de
        # enforce; con menos voz el parcial pasa sin gate, como los finales.
        gate = self.engine.speaker_gate
        if (gate.available and self.ref_embedding is not None
                and int(speech_ms * 16) >= gate.min_samples):
            sim = None
            try:
                sim = await loop.run_in_executor(gate.executor, gate.score,
                                                 audio, self.ref_embedding)
            except Exception:
                logger.debug("(%s) speaker-gate parcial falló (fail-open)",
                             self.speaker_id, exc_info=True)
            if sim is not None and gate.decide(sim) == "reject":
                if gate.enforce:
                    self._partial_inflight = False   # ¡liberar SIEMPRE el cerrojo!
                    logger.info("(%s) speaker-gate: parcial DESCARTADO — voz ajena "
                                "(sim %.2f)", self.speaker_id, sim)
                    return
                logger.info("(%s) speaker-gate SOMBRA: parcial sim %.2f — habría "
                            "sido DESCARTADO", self.speaker_id, sim)
        try:
            text, stt_ms = await loop.run_in_executor(
                self.engine.partial_executor, self.engine.transcribe_partial_sync,
                audio, self.language,
            )
        except Exception:
            logger.debug("(%s) STT parcial falló (no fatal)", self.speaker_id, exc_info=True)
            return
        finally:
            self._partial_inflight = False
        if utterance_id != self._utterance_id or not self._in_speech or not text:
            return  # el segmento ya cerró: manda el resultado FINAL, no esta hipótesis
        await self.results.put(TranscriptionResult(
            self.speaker_id, self._segment_seq + 1, self.language, text,
            audio.size / self._sample_rate, stt_ms, partial=True,
        ))

    def _launch_stt(self, audio: np.ndarray) -> None:
        self._segment_seq += 1
        prev = self._prev_stt
        task = asyncio.get_running_loop().create_task(
            self._run_stt(audio, self._segment_seq, prev,
                          speech_ms=getattr(self, "_closed_speech_ms", 0.0),
                          tail_ms=getattr(self, "_closed_tail_ms", 0.0),
                          closed_at=time.monotonic())
        )
        self._prev_stt = task
        self._stt_tasks.add(task)             # referencia viva: evita que el GC cancele la tarea
        task.add_done_callback(self._stt_tasks.discard)

    async def _run_stt(self, audio: np.ndarray, segment_id: int,
                       prev: Optional[asyncio.Task], speech_ms: float = 0.0,
                       tail_ms: float = 0.0, closed_at: float = 0.0) -> None:
        loop = asyncio.get_running_loop()
        seconds = audio.size / self._sample_rate
        seg_voiced_rms = voiced_rms(audio)   # adapta la calibración del micro (floor v2)

        # F11 — Speaker Gate: si la firma de locutor del segmento no se parece a la
        # huella del dueño de ESTE micro, es voz ajena (cross-captura del mismo
        # idioma, que la guardia LID no puede ver): descartar ANTES de gastar GPU.
        # Zona gris: pasa pero se loguea (calibración en mesa sin perder voz).
        # El suelo min_speech_s se compara contra la VOZ REAL del segmento (el clip
        # completo incluye pre-roll + cola de 600 ms de silencio: llegaba a ser ~70%
        # relleno y el gate decidía —incluido el REJECT destructivo— con 300 ms de
        # habla), y al embedding se le recorta esa cola muerta.
        gate = self.engine.speaker_gate
        if (gate.available and self.ref_embedding is not None
                and int(speech_ms * 16) >= gate.min_samples):
            try:
                tail_samples = int(tail_ms * 16)
                gate_clip = (audio[:-tail_samples]
                             if 0 < tail_samples < audio.size else audio)
                sim = await loop.run_in_executor(gate.executor, gate.score,
                                                 gate_clip, self.ref_embedding)
                verdict = gate.decide(sim)
                if not gate.enforce:
                    # MODO SOMBRA: telemetría de TODOS los finales (la calibración con
                    # voces reales sale de estas líneas), sin descartar jamás
                    logger.info("(%s) speaker-gate SOMBRA: seg %d sim %.2f -> %s%s "
                                "(%.1f s voz)", self.speaker_id, segment_id, sim,
                                verdict,
                                " — habría sido DESCARTADO" if verdict == "reject" else "",
                                speech_ms / 1000.0)
                elif verdict == "reject":
                    logger.info("(%s) speaker-gate: seg %d DESCARTADO — voz ajena "
                                "(sim %.2f < %.2f)", self.speaker_id, segment_id,
                                sim, gate.reject)
                    if prev is not None:      # respetar la publicación EN ORDEN
                        with suppress(Exception):
                            await prev
                    await self.results.put(TranscriptionResult(
                        self.speaker_id, segment_id, self.language, "", seconds, 0.0,
                        closed_at=closed_at))
                    return
                elif verdict == "gray":
                    logger.info("(%s) speaker-gate: seg %d zona GRIS (sim %.2f) — pasa",
                                self.speaker_id, segment_id, sim)
            except Exception:
                logger.exception("(%s) speaker-gate falló: el segmento pasa (fail-open)",
                                 self.speaker_id)

        self.engine.heavy_pending += 1
        try:
            text, stt_ms, crosstalk = await loop.run_in_executor(
                self.engine.stt_executor, self.engine.transcribe_sync, audio, self.language
            )
            result = TranscriptionResult(self.speaker_id, segment_id, self.language,
                                         text, seconds, stt_ms, crosstalk=crosstalk,
                                         closed_at=closed_at, voiced_rms=seg_voiced_rms)
            logger.info("(%s) seg %d: %.1f s de audio -> STT %.0f ms -> %r",
                        self.speaker_id, segment_id, seconds, stt_ms, text)
        except Exception as exc:  # se reporta tipado en la cola; jamás silencio (M2)
            logger.exception("(%s) STT falló en el segmento %d", self.speaker_id, segment_id)
            result = TranscriptionResult(self.speaker_id, segment_id, self.language,
                                         "", seconds, 0.0, error=str(exc),
                                         closed_at=closed_at)
        finally:
            self.engine.heavy_pending -= 1
        if prev is not None:
            # Publicación EN ORDEN de segment_id: la inferencia corre en paralelo (workers=2),
            # pero un segmento corto no puede adelantar en la cola a uno largo anterior
            with suppress(Exception):
                await prev
        await self.results.put(result)


class CoreEngine:
    """Carga los modelos UNA vez y sirve N sesiones concurrentes sin bloquear asyncio."""

    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        self.settings = settings or load_settings()
        stt_cfg = self.settings["stt"]
        audio_cfg = self.settings["audio"]
        if int(audio_cfg["sample_rate"]) != 16000:
            raise ModelLoadError(
                "El pipeline está fijado a 16 kHz: silero-vad v5 exige ventanas de 512 muestras a 16 kHz"
            )
        self.allowed_languages = set(self.settings["languages"]["allowed"])
        stt_workers = int(stt_cfg.get("workers", 2))

        # F11 — Speaker Gate (verificación de hablante contra la huella del enroll).
        # Import tardío del módulo: la dependencia sherpa-onnx es opcional y el
        # gate es fail-open (se desactiva solo con un aviso si algo falta)
        from backend.speaker_gate import SpeakerGate
        self.speaker_gate = SpeakerGate(self.settings)

        whisper_dir = resolve_dir(Path(self.settings["paths"]["models_dir"]) / "whisper")
        whisper_kwargs = dict(
            device=stt_cfg.get("device", "cpu"),
            compute_type=stt_cfg.get("compute_type", "int8"),
            cpu_threads=int(stt_cfg.get("cpu_threads", 4)),
            # num_workers=1 serializaría las llamadas concurrentes dentro de CTranslate2:
            # debe ir sincronizado con el tamaño del executor para paralelismo real
            num_workers=stt_workers,
            download_root=str(whisper_dir),        # pesos a models/, nunca ~/.cache (M1)
        )
        try:
            try:
                # Primero SIN red: tras el primer arranque los pesos ya están en
                # models/whisper — esto evita las peticiones a huggingface.co en cada
                # boot y permite arrancar el servidor SIN internet en la sala
                self.whisper = WhisperModel(stt_cfg["model_size"],
                                            local_files_only=True, **whisper_kwargs)
            except Exception:
                self.whisper = WhisperModel(stt_cfg["model_size"], **whisper_kwargs)
        except Exception as exc:
            raise ModelLoadError(f"No se pudo cargar Whisper '{stt_cfg['model_size']}': {exc}") from exc

        self._beam_size = int(stt_cfg.get("beam_size", 1))
        # Confianza mínima del LID para descartar un segmento como cross-captura
        # (otro idioma DE LA SALA hablado junto a este micro). Por debajo del umbral
        # NO se descarta: se re-transcribe forzando el idioma del usuario.
        self._lid_crosstalk_min_prob = float(stt_cfg.get("lid_crosstalk_min_prob", 0.80))
        # Frases-alucinación típicas de Whisper con ruido/silencio (configurable en YAML,
        # comparación sin mayúsculas ni signos): último recurso tras no_speech/logprob
        default_blacklist = ["gracias por ver", "gracias por mirar", "thanks for watching",
                             "suscríbete", "suscribete", "sign up", "구독"]
        self._hallucination_blacklist = {
            str(s).lower() for s in stt_cfg.get("hallucination_blacklist", default_blacklist)
        }
        self.stt_workers = stt_workers
        self.heavy_pending = 0        # STT finales + validaciones enroll en vuelo/encolados
        self.stt_executor = ThreadPoolExecutor(max_workers=stt_workers, thread_name_prefix="stt")
        self.vad_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="vad")
        self._sessions: dict[str, VoiceSession] = {}

        # Modelo DEDICADO a parciales (F5.2): pequeño y con executor propio de 1 worker,
        # para que las hipótesis en vivo jamás retrasen finales ni calibración (la calidad
        # del parcial es desechable por diseño; el final lo transcribe el modelo principal)
        partial_cfg = self.settings["stt"].get("partials") or {}
        self.partial_whisper = None
        self.partial_executor: Optional[ThreadPoolExecutor] = None
        if bool(partial_cfg.get("enabled", True)):
            partial_size = str(partial_cfg.get("model_size", "small"))
            partial_kwargs = dict(
                # Los parciales van a CPU SALVO opt-in explícito en partials.device:
                # si heredaran stt.device=cuda, las hipótesis desechables competirían
                # con los finales por la GPU y sumarían ~0.3 GB de VRAM. Ojo: el
                # compute_type tampoco se hereda (int8_float16 no existe en CPU).
                device=str(partial_cfg.get("device", "cpu")),
                compute_type=str(partial_cfg.get("compute_type", "int8")),
                cpu_threads=int(partial_cfg.get("cpu_threads", 2)),
                num_workers=1,
                download_root=str(whisper_dir),
            )
            try:
                try:
                    self.partial_whisper = WhisperModel(partial_size,
                                                        local_files_only=True, **partial_kwargs)
                except Exception:
                    self.partial_whisper = WhisperModel(partial_size, **partial_kwargs)
                self.partial_executor = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="stt-partial"
                )
            except Exception as exc:
                raise ModelLoadError(
                    f"No se pudo cargar el Whisper de parciales '{partial_size}': {exc}"
                ) from exc

        # Pre-carga de instancias de VAD (~2 MB cada una): unirse a la sala no debe
        # congelar el event loop con torch.jit.load (verificación 2ª pasada)
        max_speakers = int(self.settings.get("room", {}).get("max_speakers", 6))
        self._vad_pool: list[Any] = [load_silero_vad() for _ in range(max_speakers)]

        logger.info("CoreEngine listo: whisper=%s (%s, beam=%d, workers=%d), idiomas=%s, modelos en %s",
                    stt_cfg["model_size"], stt_cfg.get("compute_type", "int8"),
                    self._beam_size, stt_workers, sorted(self.allowed_languages), whisper_dir)

    async def create_session(self, speaker_id: str, language: str,
                             seq_start: int = 0) -> VoiceSession:
        """`seq_start` arranca el contador de segment_id por encima del de la sesión
        anterior del MISMO speaker_id (reconexión con id estable): los clientes
        ordenan parciales/finales por segment_id y un contador que se reinicia a 1
        haría que descartaran todo lo nuevo hasta superar el máximo anterior."""
        if language not in self.allowed_languages:     # idioma SIEMPRE explícito y validado (A2)
            raise EngineError(
                f"Idioma '{language}' no permitido; configura uno de {sorted(self.allowed_languages)}"
            )
        if speaker_id in self._sessions:
            raise EngineError(f"Ya existe una sesión para '{speaker_id}'")
        if self._vad_pool:
            vad_model = self._vad_pool.pop()
        else:  # sala llena por encima de lo previsto: cargar sin bloquear el loop
            loop = asyncio.get_running_loop()
            vad_model = await loop.run_in_executor(self.vad_executor, load_silero_vad)
            if speaker_id in self._sessions:
                # Carrera TOCTOU: otro create_session del MISMO id ganó durante el
                # await (reconexión duplicada del mismo token). Sin este re-check,
                # _sessions[id] se sobreescribía: la sesión pisada jamás se cerraba
                # y su VAD salía del pool PARA SIEMPRE (fuga acumulativa).
                self._vad_pool.append(vad_model)
                raise EngineError(f"Ya existe una sesión para '{speaker_id}'")
        session = VoiceSession(self, speaker_id, language, vad_model,
                               seq_start=int(seq_start))
        self._sessions[speaker_id] = session
        return session

    async def drop_session(self, speaker_id: str) -> None:
        session = self._sessions.pop(speaker_id, None)
        if session is not None:
            await session.close()
            self._vad_pool.append(session._vad)        # reciclar: ya quedó reseteada en close()

    async def set_speaker_reference(self, speaker_id: str,
                                    audio_16k: np.ndarray) -> Optional[list]:
        """F11: calcula y fija la huella de locutor de una sesión (tras el enroll).
        Devuelve el embedding como lista (para persistirlo en el footprint) o None."""
        session = self._sessions.get(speaker_id)
        gate = self.speaker_gate
        if session is None or not gate.available or audio_16k.size < gate.min_samples:
            return None
        loop = asyncio.get_running_loop()
        emb = await loop.run_in_executor(gate.executor, gate.embed, audio_16k)
        session.ref_embedding = emb
        logger.info("(%s) huella de locutor fijada (%d dims)", speaker_id, emb.size)
        return emb.tolist()

    def set_speaker_reference_embedding(self, speaker_id: str, embedding) -> None:
        """F11: restaura una huella ya calculada (reconexión vía footprint)."""
        session = self._sessions.get(speaker_id)
        if session is None or not self.speaker_gate.available or embedding is None:
            return
        emb = np.asarray(embedding, dtype=np.float32)
        norm = float(np.linalg.norm(emb))
        session.ref_embedding = emb / norm if norm > 0 else emb

    def transcribe_sync(self, audio: np.ndarray, expected_language: str,
                        force_language: bool = False) -> tuple[str, float, bool]:
        """Corre dentro del ThreadPoolExecutor. Acepta ndarray float32 16 kHz en memoria (A3).

        GUARDIA LID DE CROSS-CAPTURA (A1 v2, restaurada el 2026-08-04 con salvaguardas):
        el floor token NO cubre el caso del vecino con su dispositivo MUTEADO — su voz
        directa entra por TU micro, tu dispositivo adquiere el canal y su idioma se
        transcribe/traduce en TU sesión (demostrado en la Victus: el usuario EN muteado
        hablaba, la sesión ES capturó 'Hello? Can you hear me?', lo tradujo es->en y se
        lo devolvió en inglés). Diseño de la guardia — las DOS salvaguardas que faltaban
        en el LID original (que descartaba 'ja' p=0.40 y mataba voz válida):
          1) solo descarta si el idioma detectado es OTRO idioma DE LA SALA con
             confianza >= stt.lid_crosstalk_min_prob (0.80 por defecto);
          2) cualquier otra detección (exótica tipo 'ja', dialecto, confianza baja =
             acento confundiendo al LID) NO descarta: se RE-transcribe forzando el
             idioma del usuario, nunca se decodifica en el idioma detectado.
        Coste: el camino normal (detectado == esperado) reutiliza el encoder de la
        pasada LID (~coste de siempre); el camino dudoso paga una decodificación extra.
        `force_language=True` (enroll) salta la guardia por completo.
        El doble-VAD interno queda desactivado (A4): el recorte de silencios ya lo hizo
        el Silero de la sesión. Todos los descartes se LOGUEAN con su causa."""
        t0 = time.perf_counter()
        # Escalera de temperaturas ACOTADA a 2 pasadas: con audio de eco o basura,
        # los umbrales internos (compression_ratio/logprob) disparan el fallback
        # COMPLETO de faster-whisper — hasta 6 decodificaciones del MISMO segmento.
        # Medido en la Victus: STT de 16-18 s por 2-3 s de audio, GPU secuestrada,
        # MT a 1,5 s y el TTS llegando tan tarde que el ducking del receptor le
        # impedía tomar el turno. Si la 2ª pasada sale basura, los filtros la tiran.
        decode_opts = dict(
            beam_size=self._beam_size,
            condition_on_previous_text=False,
            temperature=[0.0, 0.4],
        )
        try:
            segments, info = self.whisper.transcribe(
                audio,
                language=expected_language if force_language else None,
                **decode_opts,
            )
            if not force_language and info.language != expected_language:
                detected, prob = info.language, info.language_probability
                if (detected in self.allowed_languages
                        and prob >= self._lid_crosstalk_min_prob):
                    # Cross-captura demostrable: otro idioma DE LA SALA con confianza
                    # alta. `segments` es perezoso: al no iterarlo, no se pagó decode.
                    # El flag crosstalk=True es EVIDENCIA para el floor: este micro
                    # ganó el canal con la voz del vecino y debe soltarlo.
                    logger.info("LID cross-captura: descartado segmento '%s' (p=%.2f, "
                                "esperado '%s')", detected, prob, expected_language)
                    return "", (time.perf_counter() - t0) * 1000.0, True
                # Detección exótica/dudosa ('ja' p=0.40 = acento): NUNCA decodificar
                # en el idioma detectado (saldría basura) — re-transcribir FORZADO
                logger.info("LID dudoso: '%s' (p=%.2f) -> re-transcripción forzada a "
                            "'%s'", detected, prob, expected_language)
                segments, info = self.whisper.transcribe(
                    audio, language=expected_language, **decode_opts,
                )

            valid_texts = []
            dropped: dict[str, int] = {}
            for seg in segments:
                seg_text = seg.text.strip()
                if getattr(seg, "no_speech_prob", 0.0) > 0.6:
                    dropped["no_speech"] = dropped.get("no_speech", 0) + 1
                    continue
                if getattr(seg, "avg_logprob", 0.0) < -1.0:
                    dropped["logprob"] = dropped.get("logprob", 0) + 1
                    continue
                if seg_text.lower().strip("¡!¿?.") in self._hallucination_blacklist:
                    dropped["blacklist"] = dropped.get("blacklist", 0) + 1
                    continue
                if _looks_degenerate(seg_text):
                    dropped["degenerado"] = dropped.get("degenerado", 0) + 1
                    continue
                valid_texts.append(seg_text)
            if dropped:
                logger.info("Filtros STT (%s): descartes %s", expected_language, dropped)

            text = " ".join(valid_texts).strip()
        except Exception as exc:
            raise TranscriptionError(
                f"STT falló ({expected_language}, {audio.size} muestras): {exc}"
            ) from exc
        return text, (time.perf_counter() - t0) * 1000.0, False

    def transcribe_partial_sync(self, audio: np.ndarray, language: str) -> tuple[str, float]:
        """Pasada parcial (modelo pequeño): coste acotado a UNA decodificación.
        temperature=0.0 desactiva el fallback de temperaturas (hasta 6 re-decodificaciones
        con los defaults, disparado justo por clips truncados a mitad de palabra);
        condition_on_previous_text=False y without_timestamps=True reducen tokens.
        Los segmentos con avg_logprob muy bajo (alucinación típica del clip cortado)
        se descartan en vez de difundirse."""
        t0 = time.perf_counter()
        try:
            segments, _info = self.partial_whisper.transcribe(
                audio, language=language, beam_size=1,
                temperature=0.0,
                condition_on_previous_text=False,
                without_timestamps=True,
            )
            text = " ".join(
                seg.text.strip() for seg in segments if seg.avg_logprob >= -1.0
            ).strip()
        except Exception as exc:
            raise TranscriptionError(f"STT parcial falló ({language}): {exc}") from exc
        return text, (time.perf_counter() - t0) * 1000.0

    def shutdown(self, wait: bool = True) -> None:
        """Llamar DESPUÉS de cerrar todas las sesiones (drop_session espera sus STT)."""
        self.stt_executor.shutdown(wait=wait)
        self.vad_executor.shutdown(wait=wait)
        if self.partial_executor is not None:
            self.partial_executor.shutdown(wait=wait)
        self.speaker_gate.shutdown()


# ---------- smoke test offline ----------

async def _demo(wav_path: Optional[str], language: str) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    engine = CoreEngine()
    if wav_path is None:
        print("Modelos cargados y pre-descargados en models/. Para transcribir un WAV:")
        print("  python -m backend.core_engine tests/audio_samples/ejemplo.wav es|ko|en")
        engine.shutdown()
        return

    import soundfile as sf
    audio, sr = sf.read(wav_path, dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if sr != 16000:
        # Remuestreo lineal: suficiente para el smoke test (el AudioWorklet remuestreará en producción)
        n_out = int(audio.size * 16000 / sr)
        audio = np.interp(
            np.linspace(0.0, 1.0, n_out, endpoint=False),
            np.linspace(0.0, 1.0, audio.size, endpoint=False),
            audio,
        ).astype(np.float32)

    session = await engine.create_session("demo", language)
    chunk = 1600  # 100 ms: simula la llegada por red
    for i in range(0, audio.size, chunk):
        await session.feed(audio[i:i + chunk])
    await engine.drop_session("demo")   # cierra, rescata el último segmento y espera los STT
    n_segments = session.segments_launched

    if n_segments == 0:
        print("El VAD no detectó voz en el archivo (¿es audio con habla real?).")
    for _ in range(n_segments):
        r = session.results.get_nowait()
        estado = "OK" if r.ok else f"ERROR: {r.error}"
        print(f"[seg {r.segment_id}] {r.audio_seconds:.1f}s audio | STT {r.stt_ms:.0f} ms | {estado} | {r.text}")
    engine.shutdown()


if __name__ == "__main__":
    import sys

    _wav = sys.argv[1] if len(sys.argv) > 1 else None
    _lang = sys.argv[2] if len(sys.argv) > 2 else "es"
    asyncio.run(_demo(_wav, _lang))

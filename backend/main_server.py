"""
TradIon — F4: servidor central WSS (topología estrella aprobada, DOCUMENTO_MAESTRO §1).

Pipeline por segmento de voz:
  audio del cliente -> VAD/segmentador (sesión propia, parciales en vivo) -> Whisper ->
  NLLB (una traducción por idioma destino con oyentes) -> F5-TTS Zero-Shot (clona la
  huella vocal del hablante capturada en el enroll, troceado por signos de puntuación
  para bajar la latencia del primer audio) -> N frames WAV binarios SOLO a los sockets
  de ese idioma. Los oyentes del MISMO idioma no reciben TTS (oyen la voz real);
  todos reciben subtítulos y parciales. Supresión cross-mic por dominancia RMS.

Robustez (2ª pasada de verificación adversarial):
- Cada cliente tiene una COLA DE SALIDA acotada con su propia tarea escritora: un móvil
  lento o en segundo plano no congela el pipeline de la sala; si no drena, se le expulsa.
- leave() es idempotente y drena el pump ANTES de cerrarlo: la última frase del que se
  desconecta se subtitula y traduce a la sala (coherente con VoiceSession.close()).
- El shutdown cierra los WebSockets (aiohttp no lo hace solo) para no colgar el Ctrl-C.
- Todo JSON entrante se valida (tipo objeto, idioma permitido) antes de tocar los motores.

PROTOCOLO WebSocket (referencia para el frontend F5):
  Subida (cliente -> servidor):
    1) Texto JSON: {"type":"join","name":"Juan","language":"es","client_token":"<uuid>"}
       (primer mensaje obligatorio; client_token es un id estable por dispositivo/pestaña:
        al reconectar, el servidor expulsa la conexión fantasma anterior del mismo token)
    2) Binario: chunks PCM16LE mono 16 kHz del micrófono (AudioWorklet)
    3) Texto JSON: {"type":"flush"}   (opcional: fuerza el cierre del segmento en curso)
    4) Texto JSON: {"type":"enroll","action":"start","step":N,"expected":"<frase pedida>"}
       — calibración validada por ASR (F5.2): el audio siguiente se BUFFERIZA (no entra al
       pipeline de traducción), se transcribe en streaming con Whisper y, si la similitud
       Levenshtein normalizada contra "expected" alcanza enroll.similarity (0.8), responde
       {"type":"enroll_result","status":"success","step":N,"heard":"..."} al instante.
       {"action":"cancel"} aborta (timeout del cliente). El buffer es el gancho F-bio
       (f0/vector del hablante); hoy se descarta tras validar.
    5) Texto JSON: {"type":"enrolled"} — el cliente completó las 3 frases: la huella vocal
       acumulada (ref_audio_buffer + ref_text) se vuelca a un WAV temporal para F5-TTS y
       SOLO entonces se difunde peer_joined (nadie ve a un hablante sin voz clonable).
    6) Texto JSON: {"type":"move","x","y","angle"} — asiento/orientación en la mesa
       ([-1,1] + radianes): se re-difunde como peer_moved para el audio 3D de los demás.
    7) Texto JSON: {"type":"leave"} — salida limpia: purga inmediata de la sesión en RAM
       Y de la huella vocal (buffer + WAV temporal en disco).
  Bajada (servidor -> cliente):
    - {"type":"joined","speaker_id","room":[{speaker_id,name,language},...]}
    - {"type":"peer_joined"|"peer_left","speaker_id","name","language"}
    - {"type":"partial","speaker_id","name","language","text"} — hipótesis EN VIVO del
      segmento abierto (ventana deslizante del STT); solo UI, jamás se traduce  (a todos)
    - {"type":"subtitle","speaker_id","name","language","text","latency_ms":{"stt":..}}   (a todos)
    - {"type":"translation","speaker_id","name","source_lang","lang","original","text",
       "latency_ms":{"stt":..,"mt":..,"tts":..,"total":..}}          (solo al idioma destino)
    - {"type":"error","message"}
    - Binario TTS: [4 bytes big-endian = longitud N del header][N bytes JSON UTF-8][WAV PCM16]
      header = {"type":"tts","speaker_id","name","source_lang","lang","seq","format":"wav"}
      (el cliente decodifica el WAV con decodeAudioData y lo enruta por speaker_id a su
       AudioBufferSourceNode -> PannerNode; NUNCA MediaStream remoto — bug iOS, §1.3)

Arranque:  python -m backend.main_server
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import ssl
import struct
import tempfile
import time
import unicodedata
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np
from aiohttp import WSCloseCode, WSMsgType, web

from backend.core_engine import AudioFormatError, CoreEngine, EngineError, TranscriptionResult
from backend.settings import PROJECT_ROOT, load_settings
from backend.translation_engine import TextTranslator, TranslationError
from backend.tts_engine import TTSEngine, TTSError

logger = logging.getLogger("tradion.server")

FRONTEND_DIR = PROJECT_ROOT / "frontend"
SEND_TIMEOUT_S = 10.0        # un envío individual atascado más de esto = cliente lento
OUT_QUEUE_MAX = 64           # mensajes pendientes por cliente antes de expulsarlo
PUMP_DRAIN_TIMEOUT_S = 15.0  # margen para enrutar los últimos segmentos al salir

def _dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _normalize_text(s: str) -> str:
    """Solo letras y números, sin mayúsculas: robusto ante puntuación/espaciado de Whisper."""
    s = unicodedata.normalize("NFKC", s).casefold()
    return "".join(ch for ch in s if unicodedata.category(ch)[0] in ("L", "N"))


def _levenshtein(a: str, b: str) -> int:
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _for_compare(s: str) -> str:
    """NFD expande cada sílaba hangul en sus 2-3 jamo: sin esto, un error de UNA vocal
    en coreano costaría 1/10 de similitud mientras el equivalente español cuesta 1/30
    (el umbral 0.8 mediría granularidades distintas por idioma). Las marcas combinantes
    (acentos del español ya descompuestos) se descartan: robustez ante tildes."""
    return "".join(
        ch for ch in unicodedata.normalize("NFD", s) if unicodedata.category(ch) != "Mn"
    )


def _similarity(heard: str, expected: str) -> float:
    a = _for_compare(_normalize_text(heard))
    b = _for_compare(_normalize_text(expected))
    if not a or not b:
        return 0.0
    return 1.0 - _levenshtein(a, b) / max(len(a), len(b))


@dataclass
class EnrollState:
    """Calibración en curso de UN cliente. El buffer es el gancho F-bio (f0/vector)."""
    step: int
    expected: str
    started_at: float
    run: int = 0                  # nonce del intento del cliente: descarta successes tardíos
    buffer: bytearray = field(default_factory=bytearray)
    checking: bool = False
    last_checked_len: int = 0


@dataclass
class Client:
    speaker_id: str
    name: str
    language: str
    ws: web.WebSocketResponse
    session: Any                              # VoiceSession
    token: str = ""                           # id estable del dispositivo (anti-fantasmas)
    enroll: Optional[EnrollState] = None      # calibración de voz en curso (o None)
    ref_audio_buffer: bytearray = field(default_factory=bytearray)
    ref_text: str = ""
    ref_audio_path: Optional[str] = None
    user_f0: Optional[float] = None           # f0 mediano medido en el enroll
    voice_by_lang: dict = field(default_factory=dict)  # caché idioma -> VoiceProfile
    pump_task: Optional[asyncio.Task] = None
    writer_task: Optional[asyncio.Task] = None
    out_queue: "asyncio.Queue[Optional[tuple[str, Any]]]" = field(
        default_factory=lambda: asyncio.Queue(maxsize=OUT_QUEUE_MAX)
    )
    seq: int = 0
    is_enrolled: bool = False
    seat_idx: int = -1                        # plaza hexagonal asignada en el join
    x: float = 0.0
    y: float = 0.0
    angle: float = 0.0


class TradIonServer:
    """Estado de la sala + enrutamiento. Los motores se cargan UNA vez y se comparten."""

    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        self.settings = settings or load_settings()
        self.engine = CoreEngine(self.settings)
        self.translator = TextTranslator(self.settings)
        self.tts = TTSEngine(self.settings)
        self.clients: dict[str, Client] = {}
        self.footprints: dict[str, dict] = {}
        self.max_speakers = int(self.settings.get("room", {}).get("max_speakers", 6))
        self.allowed_languages = set(self.settings["languages"]["allowed"])
        enroll_cfg = self.settings.get("enroll") or {}
        self.enroll_similarity = float(enroll_cfg.get("similarity", 0.8))
        self.enroll_max_s = float(enroll_cfg.get("max_seconds", 15))
        tts_cfg = self.settings.get("tts") or {}
        self.tts_min_chunk = int(tts_cfg.get("chunk_min_chars", 60))
        self._bg_tasks: set[asyncio.Task] = set()
        self.floor_owner: Optional[str] = None
        self.floor_last_active: float = 0.0
        self.floor_acquired_at: float = 0.0
        floor_cfg = self.settings.get("room", {}).get("floor") or {}
        # Histéresis (Schmitt): TOMAR el canal exige voz franca; MANTENERLO es más
        # barato — sin esto, las palabras finales suaves de una frase caían bajo el
        # umbral, el turno se liberaba a mitad de locución y el eco del vecino lo robaba
        self.floor_acquire_rms = float(floor_cfg.get("acquire_rms", 0.025))
        self.floor_hold_rms = float(floor_cfg.get("hold_rms", 0.012))
        self.floor_release_s = float(floor_cfg.get("release_ms", 400)) / 1000.0
        # Anti-inanición: un móvil con ruido de fondo permanente > umbral retenía el
        # canal PARA SIEMPRE y silenciaba a toda la mesa. Tope duro de posesión.
        self.floor_max_hold_s = float(floor_cfg.get("max_hold_s", 30))
        # El tope solo funciona si el expulsado no puede RE-tomar el canal en el
        # siguiente frame (≤128 ms): cuarentena tras una expulsión por max_hold_s
        self.floor_reacquire_cooldown_s = float(floor_cfg.get("reacquire_cooldown_s", 2.0))
        self.floor_cooldown_until: dict[str, float] = {}   # speaker_id -> monotonic

        # B1: las huellas vocales temporales viven en models/tmp (no en el tmp del SO) y
        # se BARREN al arrancar: un crash jamás deja audio biométrico huérfano en disco
        self.tmp_dir = PROJECT_ROOT / "models" / "tmp"
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        swept = 0
        for orphan in self.tmp_dir.glob("huella_*.wav"):
            with suppress(OSError):
                orphan.unlink()
                swept += 1
        if swept:
            logger.info("Barrido de arranque: %d huellas vocales huérfanas eliminadas", swept)

    # ---------- envío ----------

    @staticmethod
    async def _ws_send(ws: web.WebSocketResponse, payload: dict) -> None:
        """Envío directo protegido (solo para sockets aún sin Client, p. ej. antes del join)."""
        if ws.closed:
            return
        try:
            await ws.send_json(payload, dumps=_dumps)
        except (ConnectionResetError, RuntimeError):
            pass

    @staticmethod
    async def _close_ws(ws: web.WebSocketResponse, message: bytes = b"") -> None:
        with suppress(Exception):
            await ws.close(code=WSCloseCode.GOING_AWAY, message=message)
            
    def _enqueue(self, client: Client, kind: str, data: Any) -> None:
        """Encola sin bloquear jamás el pipeline. Cola llena = cliente que no drena -> expulsión."""
        try:
            client.out_queue.put_nowait((kind, data))
        except asyncio.QueueFull:
            logger.warning("(%s) cola de salida llena: cliente lento, cerrando su socket",
                           client.speaker_id)
            # Cerrar el ws despierta su handler; el finally del handler ejecuta leave()
            asyncio.get_running_loop().create_task(self._close_ws(client.ws, b"Slow consumer"))

    def _send_json(self, client: Client, payload: dict) -> None:
        self._enqueue(client, "json", payload)

    def _send_bytes(self, client: Client, data: bytes) -> None:
        self._enqueue(client, "bytes", data)

    def _broadcast(self, payload: dict, exclude: Optional[str] = None) -> None:
        for client in list(self.clients.values()):
            if client.speaker_id != exclude:
                self._send_json(client, payload)

    async def _writer(self, client: Client) -> None:
        """Tarea escritora por cliente: aísla la latencia de SU red del resto de la sala."""
        while True:
            item = await client.out_queue.get()
            if item is None:
                return
            kind, data = item
            try:
                if kind == "json":
                    await asyncio.wait_for(client.ws.send_json(data, dumps=_dumps),
                                           timeout=SEND_TIMEOUT_S)
                else:
                    await asyncio.wait_for(client.ws.send_bytes(data), timeout=SEND_TIMEOUT_S)
            except asyncio.TimeoutError:
                logger.warning("(%s) envío bloqueado > %.0f s: cerrando su socket",
                               client.speaker_id, SEND_TIMEOUT_S)
                asyncio.get_running_loop().create_task(
                    self._close_ws(client.ws, b"Slow consumer"))
                return
            except (ConnectionResetError, RuntimeError):
                return  # socket muerto: el finally de su handler hará leave()

    async def _floor_manager(self) -> None:
        """Tarea de fondo para liberar el turno de palabra (CSMA/CA): por inactividad
        de voz (release_ms) o por tope duro de posesión (max_hold_s, anti-inanición)."""
        try:
            while True:
                await asyncio.sleep(0.1)
                if self.floor_owner is None:
                    continue
                now = time.monotonic()
                reason = None
                if now - self.floor_last_active > self.floor_release_s:
                    reason = "inactividad"
                elif now - self.floor_acquired_at > self.floor_max_hold_s:
                    reason = f"tope de {self.floor_max_hold_s:.0f}s (anti-inanición)"
                    # Sin cuarentena, el mismo móvil ruidoso re-tomaba el canal en el
                    # siguiente frame (~128 ms) y el tope anti-inanición no servía de nada
                    self.floor_cooldown_until[self.floor_owner] = (
                        now + self.floor_reacquire_cooldown_s)
                if reason:
                    old_owner = self.floor_owner
                    self.floor_owner = None
                    self._broadcast({"type": "floor_released", "speaker_id": old_owner})
                    logger.info("Canal liberado (%s) — era de %s", reason, old_owner)
        except asyncio.CancelledError:
            return  # apagado ordenado del servidor

    @staticmethod
    def _tts_frame(header: dict, wav: bytes) -> bytes:
        head = _dumps(header).encode("utf-8")
        return struct.pack(">I", len(head)) + head + wav

    # ---------- ciclo de vida de clientes ----------

    async def join(self, ws: web.WebSocketResponse, data: dict) -> Optional[Client]:
        name = str(data.get("name") or "").strip()[:32] or "Anónimo"
        language = data.get("language")
        # La voz por idioma destino se asigna tras el enroll (matcher f0) y se ajusta
        # con set_voice: en el join ya no se elige nada (la del propio idioma no se oye)
        if not isinstance(language, str) or language not in self.allowed_languages:
            await self._ws_send(ws, {"type": "error",
                                     "message": f"Idioma inválido o no permitido: {language!r}"})
            await ws.close()
            return None
        raw_token = data.get("client_token")
        token = raw_token.strip()[:64] if isinstance(raw_token, str) else ""
        if token:
            # Reconexión del mismo dispositivo: expulsar su conexión fantasma anterior
            # (evita chips duplicados, 'dejó la mesa' espurios y plazas robadas)
            for existing in list(self.clients.values()):
                if existing.token == token:
                    await self.leave(existing)
                    await self._close_ws(existing.ws, b"Reemplazado por reconexion")
        if len(self.clients) >= self.max_speakers:
            await self._ws_send(ws, {"type": "error", "message": "Sala llena"})
            await ws.close()
            return None

        speaker_id = uuid.uuid4().hex[:8]
        try:
            session = await self.engine.create_session(speaker_id, language)
        except EngineError as exc:
            await self._ws_send(ws, {"type": "error", "message": str(exc)})
            await ws.close()
            return None
        # create_session pudo ceder el control (pool de VAD agotado): re-chequear el aforo
        # DESPUÉS del await para que dos joins simultáneos no desborden la sala
        if len(self.clients) >= self.max_speakers:
            await self.engine.drop_session(speaker_id)
            await self._ws_send(ws, {"type": "error", "message": "Sala llena"})
            await ws.close()
            return None

        client = Client(speaker_id=speaker_id, name=name, language=language, ws=ws,
                        session=session, token=token, is_enrolled=False)

        # Asiento hexagonal INICIAL asignado por el servidor: sin esto el roster viajaba
        # con x=y=angle=0.0 y todos los PannerNodes (y el listener) nacían en el ORIGEN
        # -> sin dirección ni distancia, el 3D era mono hasta que alguien arrastraba su
        # ficha. Primera plaza libre del hexágono (los índices se reciclan al salir).
        taken = {c.seat_idx for c in self.clients.values() if c.seat_idx >= 0}
        client.seat_idx = next(i for i in range(self.max_speakers) if i not in taken)
        seat_angle = client.seat_idx * 2.0 * math.pi / max(self.max_speakers, 1) - math.pi / 2.0
        client.x = 0.72 * math.cos(seat_angle)
        client.y = 0.72 * math.sin(seat_angle)
        client.angle = math.atan2(-client.y, -client.x)   # mirando al centro de la mesa

        # Restaurar huella vocal si existía (reconexión por micro-corte)
        if token and token in self.footprints:
            fp = self.footprints[token]
            client.ref_audio_buffer = bytearray(fp["buffer"])
            client.ref_text = fp["text"]
            client.ref_audio_path = fp["path"]
            client.user_f0 = fp["f0"]
            client.voice_by_lang = fp["voices"].copy()
            client.is_enrolled = True
            # El asiento también sobrevive al micro-corte (si el usuario lo arrastró,
            # reaparece donde estaba, no en la plaza hexagonal por defecto)
            if "x" in fp:
                client.x, client.y, client.angle = fp["x"], fp["y"], fp["angle"]
        loop = asyncio.get_running_loop()
        client.pump_task = loop.create_task(self._pump(client))
        client.writer_task = loop.create_task(self._writer(client))
        self.clients[speaker_id] = client

        self._send_json(client, {
            "type": "joined",
            "speaker_id": speaker_id,
            "room": [{"speaker_id": c.speaker_id, "name": c.name, "language": c.language, "x": c.x, "y": c.y, "angle": c.angle}
                     for c in self.clients.values() if getattr(c, 'is_enrolled', False) or c.speaker_id == speaker_id],
            # Estado del turno CSMA/CA: quien entra a mitad de un turno debe saberlo
            # (si no, su cliente no bufferizaba y su primera frase se perdía en silencio)
            "floor_owner": self.floor_owner,
        })
        
        if client.is_enrolled:
            self._broadcast(
                {"type": "peer_joined", "speaker_id": client.speaker_id, "name": client.name, "language": client.language, "x": client.x, "y": client.y, "angle": client.angle},
                exclude=client.speaker_id,
            )
            logger.info("~ %s (%s, %s) — reconectado con perfil restaurado", name, language, speaker_id)
        else:
            logger.info("~ %s (%s, %s) — %d en sala (enrolamiento pendiente)", name, language, speaker_id, len(self.clients))
        return client

    async def leave(self, client: Client) -> None:
        """Liberación de recursos por desconexión. IDEMPOTENTE: puede invocarse desde el
        finally del handler y desde el shutdown a la vez; solo la primera ejecuta algo.

        Orden deliberado: primero drop_session (flush + espera de STT en vuelo -> la cola
        de resultados queda completa), luego el pump drena y enruta esos últimos segmentos
        a la sala, y solo al final se cancela el writer y se anuncia el peer_left.
        La RAM del cliente (buffers de audio, colas, sesión) queda sin referencias -> GC;
        la instancia de VAD vuelve al pool del motor.
        """
        if self.clients.pop(client.speaker_id, None) is None:
            return
        try:
            await self.engine.drop_session(client.speaker_id)
        except Exception:
            logger.exception("(%s) error liberando la sesión", client.speaker_id)
        if client.pump_task is not None:
            client.session.results.put_nowait(None)   # centinela: fin de cola para el pump
            try:
                await asyncio.wait_for(client.pump_task, timeout=PUMP_DRAIN_TIMEOUT_S)
            except asyncio.TimeoutError:
                client.pump_task.cancel()
                with suppress(asyncio.CancelledError):
                    await client.pump_task
            except Exception:
                logger.exception("(%s) el pump terminó con error", client.speaker_id)
        if client.writer_task is not None:
            client.writer_task.cancel()
            with suppress(asyncio.CancelledError):
                await client.writer_task
        # Privacidad: la huella vocal muere con la sesión, SALVO que esté en caché
        # (micro-cortes). Si el usuario pulsó 'Salir', ya se borró de la caché.
        if client.token not in self.footprints:
            client.ref_audio_buffer.clear()
            client.ref_text = ""
            if client.ref_audio_path:
                with suppress(OSError):
                    os.unlink(client.ref_audio_path)
                client.ref_audio_path = None
        self._broadcast({
            "type": "peer_left",
            "speaker_id": client.speaker_id, "name": client.name, "language": client.language,
        })
        if self.floor_owner == client.speaker_id:
            self.floor_owner = None
            self._broadcast({"type": "floor_released", "speaker_id": client.speaker_id})
        self.floor_cooldown_until.pop(client.speaker_id, None)
        logger.info("- %s (%s) — %d en sala", client.name, client.speaker_id, len(self.clients))

    # ---------- pipeline de enrutamiento ----------

    async def _pump(self, client: Client) -> None:
        """Consume los resultados STT del hablante y los enruta. Una tarea por cliente."""
        while True:
            result: Optional[TranscriptionResult] = await client.session.results.get()
            if result is None:                 # centinela de leave(): cola drenada
                return
            try:
                await self._handle_result(client, result)
            except asyncio.CancelledError:
                raise
            except Exception:
                # El pump no debe morir por un segmento problemático: se reporta y se sigue
                logger.exception("(%s) error enrutando el segmento %d",
                                 client.speaker_id, result.segment_id)

    async def _handle_result(self, client: Client, result: TranscriptionResult) -> None:
        if result.partial:
            # Hipótesis en vivo del segmento ABIERTO: solo UI. La traducción espera al
            # final (la gramática SOV del coreano se rompe traduciendo prefijos, §1)
            if result.ok and result.text:
                self._broadcast({
                    "type": "partial",
                    "speaker_id": client.speaker_id, "name": client.name,
                    "language": client.language, "text": result.text,
                    "segment_id": result.segment_id,  # el cliente ordena parciales vs finales
                })
            return
        if not result.ok:
            self._send_json(client, {"type": "error", "message": f"STT: {result.error}"})
            return
        if not result.text:
            return

        self._broadcast({
            "type": "subtitle",
            "speaker_id": client.speaker_id, "name": client.name,
            "language": client.language, "text": result.text,
            "segment_id": result.segment_id,
            "latency_ms": {"stt": round(result.stt_ms)},
        })

        # Idiomas destino = idiomas de los DEMÁS clientes distintos del idioma del hablante.
        # Una sola traducción+síntesis por idioma, difundida a todos sus oyentes.
        targets: dict[str, list[Client]] = {}
        for other in self.clients.values():
            if other.speaker_id != client.speaker_id and other.language != client.language:
                targets.setdefault(other.language, []).append(other)
        if not targets:
            return
        routed = await asyncio.gather(
            *(self._route(client, result, lang, members) for lang, members in targets.items()),
            return_exceptions=True,
        )
        for lang, outcome in zip(targets, routed):
            if isinstance(outcome, BaseException):   # _route ya captura lo suyo; esto es el airbag
                logger.error("(%s->%s) excepción no controlada en la ruta: %r",
                             client.language, lang, outcome)

    async def _route(self, client: Client, result: TranscriptionResult,
                     target_lang: str, members: list[Client]) -> None:
        try:
            t0 = time.perf_counter()
            translated = await self.translator.translate_async(result.text, client.language, target_lang)
            mt_ms = (time.perf_counter() - t0) * 1000.0
            if not translated:
                return
            # La síntesis TTS ocurre en el bucle de chunks de abajo, SIEMPRE vía
            # tts.synthesize() -> run_in_executor: el event loop nunca se bloquea.
        except TranslationError as exc:
            logger.error("(%s->%s) pipeline falló: %s", client.language, target_lang, exc)
            self._send_json(client, {
                "type": "error",
                "message": f"No se pudo traducir a '{target_lang}': {exc}",
            })
            return
        except Exception as exc:  # airbag M2: ningún motor debe romper el contrato en silencio
            logger.exception("(%s->%s) fallo inesperado del pipeline", client.language, target_lang)
            self._send_json(client, {
                "type": "error",
                "message": f"Fallo interno traduciendo a '{target_lang}': {exc}",
            })
            return

        client.seq += 1

        # 1. Enviar el texto completo de una vez a la UI
        # Métricas HONESTAS: aquí solo existen STT y MT (la síntesis va después, por
        # chunks); total = pipeline de texto real, jamás ceros inventados (auditoría C4)
        payload = {
            "type": "translation",
            "speaker_id": client.speaker_id, "name": client.name,
            "source_lang": client.language, "lang": target_lang,
            "original": result.text, "text": translated,
            "latency_ms": {"stt": round(result.stt_ms), "mt": round(mt_ms),
                           "total": round(result.stt_ms + mt_ms)},
        }
        for member in members:
            if member.speaker_id in self.clients:
                self._send_json(member, payload)

        # 2. Chunking para el TTS — MEDIDO en el M3 (sesión 2026-07-27): F5-TTS paga un
        # coste FIJO de ~3,1 s por llamada + ~0,5 s por segundo de audio generado.
        # Trocear por comas multiplica el coste fijo (RTF ~3-4x: la cola solo crece);
        # con trozos >= chunk_min_chars el RTF baja a ~0,8x y la sala es sostenible.
        chunks = self._chunk_for_tts(translated)
            
        for chunk in chunks:
            t1 = time.perf_counter()
            # TTSEngine recibe el chunk, target_lang, y client (que tiene la huella vocal)
            wav = await self.tts.synthesize(chunk, target_lang, client)
            tts_ms = (time.perf_counter() - t1) * 1000.0
            total_ms = result.stt_ms + mt_ms + tts_ms
            
            frame_tts = self._tts_frame({
                "type": "tts", "speaker_id": client.speaker_id, "name": client.name,
                "source_lang": client.language, "lang": target_lang,
                "seq": client.seq, "format": "wav",
            }, wav)
            # El half-duplex vive SOLO en el cliente (ducking del worklet, sample-accurate:
            # sabe exactamente cuándo suena cada buffer). El bloqueo espejo del servidor
            # (tts_playing_until) se eliminó: su ventana estimada desde el envío quedaba
            # desalineada con la cola de reproducción real y comía habla legítima (A3).
            for member in members:
                self._send_bytes(member, frame_tts)
                
            logger.info("%s(%s)->%s: stt %.0f + mt %.0f + tts %.0f = %.0f ms | %r",
                        client.name, client.language, target_lang,
                        result.stt_ms, mt_ms, tts_ms, total_ms, chunk)


    _SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+")

    def _chunk_for_tts(self, text: str) -> list[str]:
        """Corta SOLO en finales de oración y fusiona hasta chunk_min_chars.
        También deduplica trozos adyacentes idénticos (NLLB a veces duplica:
        '- Gracias. - Gracias.' se sintetizaba dos veces)."""
        raw_parts = [s.strip() for s in self._SENTENCE_SPLIT.split(text) if s.strip()]
        # dedup de oraciones adyacentes ANTES de fusionar (NLLB duplica a veces)
        parts: list[str] = []
        for part in raw_parts:
            if not parts or parts[-1] != part:
                parts.append(part)
        if not parts:
            return [text]
        chunks: list[str] = []
        current = ""
        for part in parts:
            current = f"{current} {part}".strip() if current else part
            if len(current) >= self.tts_min_chunk:
                chunks.append(current)
                current = ""
        if current:
            # un resto corto se pega al chunk anterior: no merece su propio coste fijo
            if chunks and len(current) < self.tts_min_chunk // 2:
                chunks[-1] = f"{chunks[-1]} {current}"
            else:
                chunks.append(current)
        return chunks or [text]

    # ---------- calibración de voz validada por ASR (F5.2) ----------

    def _feed_enroll(self, client: Client, data: bytes) -> None:
        enroll = client.enroll
        enroll.buffer += data
        if time.monotonic() - enroll.started_at > self.enroll_max_s:
            logger.info("(%s) enroll expirado en el servidor (paso %d)",
                        client.speaker_id, enroll.step)
            client.enroll = None
            return
        total = len(enroll.buffer)
        # PCM16 a 16 kHz = 32000 bytes/s. Chequear con >=1 s total y >=1 s nuevo, máximo
        # una validación en vuelo por cliente, y SOLO sobre la cola del buffer (6 s):
        # re-transcribir el buffer entero era O(n²) y saturaba el executor (STT de la
        # conversación a 7-9 s en la sesión del 2026-07-27). Bajo carga, se espera.
        if (not enroll.checking and total >= 32000
                and total - enroll.last_checked_len >= 32000
                and self.engine.heavy_pending < self.engine.stt_workers):
            enroll.checking = True
            enroll.last_checked_len = total
            window = bytes(enroll.buffer[-6 * 32000:])
            task = asyncio.get_running_loop().create_task(
                self._check_enroll(client, enroll, window)
            )
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)

    async def _check_enroll(self, client: Client, enroll: EnrollState, pcm: bytes) -> None:
        self.engine.heavy_pending += 1   # la calibración cuenta como tráfico crítico
        try:
            pcm = pcm[: len(pcm) & ~1]   # cliente no conforme: truncar a muestras completas
            audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
            loop = asyncio.get_running_loop()
            # force_language=True (auditoría A2): en la calibración el idioma se conoce
            # con certeza — el LID aquí solo servía para romper el enroll con acentos
            heard, _stt_ms = await loop.run_in_executor(
                self.engine.stt_executor, self.engine.transcribe_sync,
                audio, client.language, True,
            )
        except Exception:
            logger.exception("(%s) fallo transcribiendo la calibración", client.speaker_id)
            return
        finally:
            self.engine.heavy_pending -= 1
            enroll.checking = False
        if client.enroll is not enroll:
            return  # cancelada o reiniciada mientras transcribíamos
        score = _similarity(heard, enroll.expected)
        logger.info("(%s) enroll paso %d: similitud %.2f — %r", client.speaker_id,
                    enroll.step, score, heard)
        if score >= self.enroll_similarity:
            # F-bio: Huella vocal zero-shot
            client.ref_audio_buffer.extend(pcm)
            client.ref_text += (" " if client.ref_text else "") + heard
            client.enroll = None

            self._send_json(client, {"type": "enroll_result", "status": "success",
                                     "step": enroll.step, "run": enroll.run, "heard": heard})

    # ---------- handler WebSocket ----------

    async def websocket_handler(self, request: web.Request) -> web.WebSocketResponse:
        heartbeat = float(self.settings["server"].get("heartbeat_s", 30))
        ws = web.WebSocketResponse(heartbeat=heartbeat, max_msg_size=4 * 1024 * 1024)
        await ws.prepare(request)
        client: Optional[Client] = None
        try:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    if client is None:
                        await self._ws_send(ws, {"type": "error",
                                                 "message": "Envía join antes del audio"})
                        continue
                    if client.enroll is not None:
                        # Audio de calibración: NO entra al pipeline de traducción;
                        # se bufferiza y se valida por ASR en streaming (F5.2)
                        self._feed_enroll(client, msg.data)
                        continue
                    try:
                        # Protocolo CSMA/CA (Floor Token) con latencia acústica.
                        # En lugar de comparar RMS relativos (que el AGC de los móviles anula),
                        # otorgamos el "turno" de forma estricta (First-In First-Out) al primero
                        # que supere el umbral de ruido (0.025). La velocidad del sonido asegura
                        # que el hablante original enviará su paquete ~4ms antes que los "ecos".
                        
                        now_mono = time.monotonic()
                        # Truncar al múltiplo de 2: un frame de longitud IMPAR (proxy que
                        # fragmenta, cliente hostil) hacía que frombuffer(int16) lanzara
                        # ValueError sin capturar y MATABA la conexión entera del cliente
                        pcm = msg.data[: len(msg.data) & ~1]
                        if not pcm:
                            continue
                        audio_np = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
                        rms = float(np.sqrt(np.mean(audio_np**2)))

                        safe_data = pcm

                        if self.floor_owner == client.speaker_id:
                            # Histéresis: mantener el turno solo requiere hold_rms (más bajo
                            # que acquire): las palabras finales suaves ya no sueltan el canal
                            # a mitad de frase. El silencio real sigue sin renovar el turno.
                            if rms > self.floor_hold_rms:
                                self.floor_last_active = now_mono
                        elif now_mono < self.floor_cooldown_until.get(client.speaker_id, 0.0):
                            # Cuarentena post-max_hold_s: TODO el audio del expulsado se
                            # silencia (no solo el que supera acquire_rms) — su ruido suave
                            # seguía entrando a SU pipeline y generaba segmentos basura
                            safe_data = bytes(len(pcm))
                        elif rms > self.floor_acquire_rms:
                            if self.floor_owner is None:
                                # Toma del canal (First-In: la latencia acústica favorece
                                # al hablante real frente a los ecos de los otros móviles)
                                self.floor_owner = client.speaker_id
                                self.floor_acquired_at = now_mono
                                self.floor_last_active = now_mono
                                self._broadcast({"type": "floor_acquired",
                                                 "speaker_id": client.speaker_id})
                                logger.info("Canal tomado por %s", client.speaker_id)
                            else:
                                # Canal ocupado por otro: descartar (crosstalk)
                                safe_data = bytes(len(pcm))
                        elif self.floor_owner is not None:
                            # Sin voz franca y el canal es de otro: silenciar por si acaso
                            safe_data = bytes(len(pcm))

                        await client.session.feed(safe_data)
                    except AudioFormatError as exc:
                        self._send_json(client, {"type": "error", "message": str(exc)})
                    except EngineError as exc:
                        self._send_json(client, {"type": "error", "message": str(exc)})
                        break
                elif msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        await self._ws_send(ws, {"type": "error", "message": "JSON inválido"})
                        continue
                    if not isinstance(data, dict):   # "hola", null o [1] son JSON válidos
                        await self._ws_send(ws, {"type": "error",
                                                 "message": "Se esperaba un objeto JSON"})
                        continue
                    msg_type = data.get("type")
                    if msg_type == "join":
                        if client is not None:
                            self._send_json(client, {"type": "error", "message": "Ya estás unido"})
                            continue
                        client = await self.join(ws, data)
                        if client is None:
                            break
                    elif msg_type == "flush" and client is not None:
                        await client.session.flush()
                    elif msg_type == "enroll" and client is not None:
                        action = data.get("action")
                        if action == "start":
                            expected = data.get("expected")
                            step = data.get("step")
                            if not isinstance(expected, str) or not expected.strip():
                                self._send_json(client, {"type": "error",
                                                         "message": "enroll.expected requerido"})
                            else:
                                await client.session.flush()   # cierra lo que hubiera en curso
                                run = data.get("run")
                                enroll_state = EnrollState(
                                    step=int(step) if isinstance(step, (int, float))
                                    and math.isfinite(step) else 0,
                                    expected=expected.strip()[:200],
                                    started_at=time.monotonic(),
                                    run=int(run) if isinstance(run, (int, float))
                                    and math.isfinite(run) else 0,
                                )
                                client.enroll = enroll_state
                                # B2: expiración REAL aunque el cliente deje de enviar audio
                                # (antes solo se comprobaba al llegar chunks: estado zombi)
                                def _expire(c=client, st=enroll_state):
                                    if c.enroll is st:
                                        c.enroll = None
                                        logger.info("(%s) enroll expirado por temporizador",
                                                    c.speaker_id)
                                asyncio.get_running_loop().call_later(
                                    self.enroll_max_s + 1.0, _expire)
                        elif action in ("cancel", "stop"):
                            client.enroll = None
                        else:
                            self._send_json(client, {"type": "error",
                                                     "message": f"enroll.action inválida: {action!r}"})
                    elif msg_type == "enrolled" and client is not None:
                        if not getattr(client, 'is_enrolled', False):
                            client.is_enrolled = True

                            # F9: matcher biométrico — f0 mediano del audio del enroll
                            if client.ref_audio_buffer:
                                from backend.voice_catalog import estimate_f0
                                pcm = bytes(client.ref_audio_buffer)
                                pcm = pcm[: len(pcm) & ~1]
                                samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
                                loop = asyncio.get_running_loop()
                                client.user_f0 = await loop.run_in_executor(
                                    None, estimate_f0, samples, 16000)
                            # La voz del PROPIO idioma no la oye nadie (los de tu idioma oyen
                            # tu voz real): se asigna una voz afín por f0 para CADA idioma
                            # DESTINO, y el cliente la confirma/cambia antes de entrar a la mesa
                            catalog = self.tts.catalog
                            if catalog is not None:
                                assigned_map = {}
                                for lang in sorted(self.allowed_languages):
                                    if lang == client.language:
                                        continue
                                    match = catalog.match_by_f0(client.user_f0, lang)
                                    if match is not None:
                                        client.voice_by_lang[lang] = match
                                        assigned_map[lang] = {
                                            "voice_id": match.id, "label": match.label,
                                            "gender": match.gender, "tone": match.tone,
                                        }
                                self._send_json(client, {
                                    "type": "voice_assigned",
                                    "voices": assigned_map,
                                    "user_f0": client.user_f0,
                                })

                            # Volcar huella vocal de RAM a WAV temporal asincrónicamente (MPS Optimizaciones)
                            if client.ref_audio_buffer:
                                def _dump_wav():
                                    import tempfile
                                    import soundfile as sf
                                    # B1: en models/tmp (barrida al arrancar), no en el tmp del SO
                                    fd, path = tempfile.mkstemp(
                                        prefix="huella_", suffix=".wav", dir=str(self.tmp_dir))
                                    os.close(fd)
                                    audio_np = np.frombuffer(client.ref_audio_buffer, dtype=np.int16).astype(np.float32) / 32768.0
                                    sf.write(path, audio_np, 16000, format="WAV")
                                    return path
                                loop = asyncio.get_running_loop()
                                client.ref_audio_path = await loop.run_in_executor(None, _dump_wav)
                                logger.info("(%s) Huella vocal Zero-Shot volcada a %s (%.1f s)", 
                                            client.speaker_id, client.ref_audio_path, len(client.ref_audio_buffer)/32000)
                            
                            self._broadcast(
                                {"type": "peer_joined", "speaker_id": client.speaker_id, "name": client.name, "language": client.language, "x": client.x, "y": client.y, "angle": client.angle},
                                exclude=client.speaker_id,
                            )
                            
                            if client.token:
                                self.footprints[client.token] = {
                                    "buffer": bytes(client.ref_audio_buffer),
                                    "text": client.ref_text,
                                    "path": client.ref_audio_path,
                                    "f0": client.user_f0,
                                    "voices": client.voice_by_lang.copy(),
                                    "x": client.x, "y": client.y, "angle": client.angle,
                                }
                            
                            logger.info("+ %s (%s, %s) completó calibración", client.name, client.language, client.speaker_id)
                    elif msg_type == "set_voice" and client is not None:
                        # El usuario elige a mano cómo suena en un idioma destino
                        catalog = self.tts.catalog
                        lang = data.get("lang")
                        voice_id = data.get("voice_id")
                        profile = catalog.profiles.get(voice_id) if catalog and isinstance(voice_id, str) else None
                        if profile is not None and profile.lang == lang:
                            client.voice_by_lang[lang] = profile
                            if client.token and client.token in self.footprints:
                                self.footprints[client.token]["voices"][lang] = profile
                            logger.info("(%s) voz manual para %s: %s",
                                        client.speaker_id, lang, profile.id)
                        else:
                            self._send_json(client, {"type": "error",
                                                     "message": f"Voz inválida: {voice_id!r} para {lang!r}"})
                    elif msg_type == "move" and client is not None:
                        try:
                            nx = float(data.get("x", 0.0))
                            ny = float(data.get("y", 0.0))
                            na = float(data.get("angle", 0.0))
                            # "Infinity"/"NaN" como STRING es JSON válido y float() lo
                            # acepta; json.dumps(allow_nan=True) lo re-emite como
                            # Infinity A PELO -> JSON.parse revienta en TODOS los
                            # clientes (y en cada joined futuro vía footprint).
                            # Solo coordenadas finitas y acotadas a la mesa.
                            if all(map(math.isfinite, (nx, ny, na))):
                                client.x = max(-1.0, min(1.0, nx))
                                client.y = max(-1.0, min(1.0, ny))
                                client.angle = max(-2 * math.pi, min(2 * math.pi, na))
                        except (TypeError, ValueError):
                            pass
                        if client.token and client.token in self.footprints:
                            self.footprints[client.token].update(
                                x=client.x, y=client.y, angle=client.angle)
                        self._broadcast(
                            {"type": "peer_moved", "speaker_id": client.speaker_id, "x": client.x, "y": client.y, "angle": client.angle},
                            exclude=client.speaker_id,
                        )
                    elif msg_type == "leave" and client is not None:
                        # Salida limpia solicitada por el cliente: el finally ejecuta
                        # leave() -> purga inmediata de TODO su estado en RAM
                        if client.token:
                            self.footprints.pop(client.token, None)
                        break
                    else:
                        await self._ws_send(ws, {"type": "error",
                                                 "message": f"Mensaje desconocido: {msg_type!r}"})
                elif msg.type == WSMsgType.ERROR:
                    logger.warning("WS con excepción: %s", ws.exception())
                    break
        finally:
            if client is not None:
                await self.leave(client)
        return ws


# ---------- aplicación aiohttp ----------

async def _index(_request: web.Request) -> web.StreamResponse:
    index_file = FRONTEND_DIR / "index.html"
    if index_file.exists():
        return web.FileResponse(index_file)
    return web.Response(text="TradIon server activo. Frontend pendiente (F5).")


async def _api_voices(request: web.Request) -> web.Response:
    """F9: catálogo de voces para el selector del lobby (vacío con backend f5tts)."""
    server: TradIonServer = request.app["server"]
    catalog = server.tts.catalog
    voices = catalog.as_json() if catalog is not None else []
    return web.json_response({"backend": server.tts.backend.name, "voices": voices},
                             dumps=_dumps)


async def _api_voice_preview(request: web.Request) -> web.StreamResponse:
    """F9: muestra corta pre-sintetizada de una voz ("Escuchar muestra")."""
    server: TradIonServer = request.app["server"]
    catalog = server.tts.catalog
    if catalog is None:
        raise web.HTTPNotFound(text="Backend sin catálogo de voces")
    path = catalog.preview_path(request.match_info["voice_id"])
    if path is None:
        raise web.HTTPNotFound(text="Voz desconocida")
    return web.FileResponse(path, headers={"Cache-Control": "max-age=3600"})



def build_app(server: TradIonServer) -> web.Application:
    app = web.Application()
    app["server"] = server
    ws_path = server.settings["server"].get("ws_path", "/ws")
    app.router.add_get(ws_path, server.websocket_handler)
    app.router.add_get("/", _index)
    app.router.add_get("/api/voices", _api_voices)
    app.router.add_get("/api/voices/{voice_id}/preview.wav", _api_voice_preview)
    if FRONTEND_DIR.is_dir():
        app.router.add_static("/static", FRONTEND_DIR)

    async def _on_startup(_app: web.Application) -> None:
        task = asyncio.create_task(server._floor_manager())
        server._bg_tasks.add(task)
        task.add_done_callback(server._bg_tasks.discard)

    async def _on_shutdown(_app: web.Application) -> None:
        # aiohttp NO cierra los websockets al apagar: sin esto, cada Ctrl-C con clientes
        # conectados espera el shutdown_timeout completo (doc oficial: graceful shutdown)
        for client in list(server.clients.values()):
            await server.leave(client)
            await server._close_ws(client.ws, b"Server shutdown")
        for task in list(server._bg_tasks):
            task.cancel()
        server.engine.shutdown()
        server.translator.shutdown()
        server.tts.shutdown()

    app.on_startup.append(_on_startup)
    app.on_shutdown.append(_on_shutdown)
    return app


def _build_ssl_context(settings: dict[str, Any]) -> ssl.SSLContext:
    cert = Path(settings["server"]["tls_cert"])
    key = Path(settings["server"]["tls_key"])
    if not cert.is_absolute():
        cert = PROJECT_ROOT / cert
    if not key.is_absolute():
        key = PROJECT_ROOT / key
    if not cert.exists() or not key.exists():
        raise SystemExit(
            f"Faltan los certificados TLS ({cert} / {key}). Genéralos con:\n"
            f"  mkcert -cert-file {cert} -key-file {key} "
            "tradion.local localhost 127.0.0.1 $(ipconfig getifaddr en0)\n"
            "getUserMedia en los móviles NO funciona sin HTTPS (DOCUMENTO_MAESTRO §1.4)."
        )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert), str(key))
    return context


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    settings = load_settings()
    logger.info("Cargando motores (Whisper + NLLB + F5-TTS)... puede tardar en el primer arranque")
    server = TradIonServer(settings)
    app = build_app(server)
    ssl_context = _build_ssl_context(settings)
    host = settings["server"].get("host", "0.0.0.0")
    port = int(settings["server"].get("port", 8443))
    ws_path = settings["server"].get("ws_path", "/ws")
    logger.info("TradIon en marcha: https://<IP-del-Mac>:%d  (WebSocket: wss://<IP-del-Mac>:%d%s)",
                port, port, ws_path)
    web.run_app(app, host=host, port=port, ssl_context=ssl_context, shutdown_timeout=5.0)


if __name__ == "__main__":
    main()

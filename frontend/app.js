/* TradIon — F5.2: cliente de la mesa (parciales en vivo + enroll ASR + i18n estructural).
 *
 * Protocolo: el contrato exacto vive en el docstring de backend/main_server.py.
 *   Subida:  JSON {type:"join",name,language,client_token} · binario PCM16LE mono 16 kHz
 *            {type:"flush"} · {type:"enroll",action:"start",step,expected} ·
 *            {type:"enroll",action:"cancel"} · {type:"leave"}
 *   Bajada:  joined / peer_joined / peer_left / partial / subtitle / translation /
 *            enroll_result / error · binario TTS: [4B BE][JSON UTF-8][WAV PCM16]
 *
 * Decisiones F5.2 (Arquitecto):
 *   - STT en tiempo real percibido: el servidor emite 'partial' (ventana deslizante sobre
 *     el segmento abierto). La UI los pinta al instante — la strip para mi voz y burbujas
 *     "en vivo" para los demás. La TRADUCCIÓN solo ocurre sobre segmentos final (el SOV
 *     coreano exige la frase completa). El worklet no cambia: ya emite streaming continuo.
 *   - Enroll estilo Voice Match: 3 frases localizadas, botón "Comenzar a hablar" por paso,
 *     validación ASR en el servidor (Levenshtein >= 0.8) con aprobación inmediata y
 *     timeout de 10 s que reinicia el proceso completo.
 *   - i18n estructural: diccionario embebido + data-i18n en el HTML + updateUI() que
 *     re-traduce TODO el DOM al cambiar el idioma del lobby, sin recargar. Los textos
 *     dinámicos (toasts, syslines, estados, pendientes) pasan por t() en render.
 */
'use strict';

const WS_PATH = '/ws';
const FLAGS = { es: '🇪🇸', ko: '🇰🇷', en: '🇺🇸' };
const FEED_MAX_NODES = 200;
const PENDING_TIMEOUT_MS = 20000;
const PREJOIN_MAX_FAILS = 5;
const ENROLL_STEPS = 1;
const ENROLL_TIMEOUT_MS = 10000;   // margen amplio por paso; al vencer, reinicia el proceso
const LIVE_PARTIAL_TTL_MS = 6000;  // burbuja "en vivo" sin actualizaciones -> se retira
const IS_ANDROID = /android/i.test(navigator.userAgent);
const IS_IOS = /iP(hone|ad|od)/.test(navigator.userAgent)
  || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1); // iPadOS "desktop"

const MIC_CONSTRAINTS = {
  audio: {
    echoCancellation: true,
    noiseSuppression: true,
    autoGainControl: false,
    channelCount: 1,
  },
};

/* ========================== i18n estructural ============================ */

const I18N = {
  es: {
    tagline: 'La mesa que traduce sola',
    name_label: 'Tu nombre',
    name_ph: 'Rodrigo',
    lang_label: 'Hablo y quiero escuchar en…',
    sit: '🪑 Sentarse en la mesa',
    sitting: 'Entrando…',
    notice: '🎧 Con auriculares se oye mejor · ☀️ La pantalla quedará encendida',
    leave: 'Salir',
    leave_title: 'Abandonar la mesa',
    status_connecting: 'Conectando…',
    status_connected: 'Conectado',
    status_reconnecting: 'Reconectando…',
    status_off: 'Desconectado',
    listening: 'Te escucho…',
    me_suffix: '(tú)',
    pending: 'traduciendo…',
    pending_failed: '(traducción no disponible)',
    sys_joined: '{name} se sentó a la mesa {flag}',
    sys_left: '{name} dejó la mesa',
    plan_title: 'Configuración de la mesa',
    plan_hint: 'Arrastra a cada persona a su sitio real alrededor de la mesa: su voz sonará desde esa dirección (audio 3D, mejor con auriculares).',
    plan_done: 'Listo',
    mute_title: 'Silenciar micrófono',
    enroll_title: 'Calibración de voz',
    enroll_hint: 'Lee esta frase en voz alta y natural:',
    enroll_start: 'Comenzar a hablar',
    enroll_listening: '🎙️ Te escuchamos…',
    enroll_step: 'Frase {n} de {total}',
    enroll_retry: 'No te hemos entendido bien. Empezamos de nuevo.',
    enroll_done: 'Voz calibrada ✓',
    enroll_phrase_1: 'Hola, encantado de conocerte',
    enroll_phrase_2: 'El cielo está despejado esta mañana',
    enroll_phrase_3: 'Me apetece un café bien caliente',
    voice_card_title: 'Así sonarás en la mesa',
    voice_card_hint: 'Tu voz real solo la oyen los de tu idioma. Para el resto hablarás con estas voces, elegidas según tu tono. Escúchalas y cámbialas si quieres.',
    voice_enter: 'Entrar a la mesa',
    voice_preview: 'Escuchar muestra de voz',
    err_name: 'Escribe tu nombre para sentarte',
    err_mic: 'No se pudo acceder al micrófono: {msg}',
    err_mic_lost: 'Micrófono perdido; recuperándolo…',
    err_mic_fail: 'No se pudo recuperar el micrófono: {msg}',
    err_mic_paused: 'Micrófono en pausa por el sistema…',
    err_capture: 'La captura de audio falló; recarga la página',
    err_server_down: 'No se pudo entrar a la mesa (servidor no disponible)',
    err_room_full: 'La mesa está llena',
    mute_tip: '🎙️ Pulsa para que te escuchen',
    status_muted: '🔇 Micro silenciado — pulsa el botón para hablar',
    status_turn_other: '⏳ Habla {name} — espera tu turno',
    status_turn_mine: '🎤 Tu turno — te escucho…',
    feed_empty: 'Habla y verás aquí la conversación traducida',
    diag_title: 'Diagnóstico de audio 3D',
    enroll_exit: 'Volver al lobby',
  },
  en: {
    tagline: 'The table that translates on its own',
    name_label: 'Your name',
    name_ph: 'Alex',
    lang_label: 'I speak and want to listen in…',
    sit: '🪑 Take a seat',
    sitting: 'Joining…',
    notice: '🎧 Headphones sound better · ☀️ The screen will stay awake',
    leave: 'Leave',
    leave_title: 'Leave the table',
    status_connecting: 'Connecting…',
    status_connected: 'Connected',
    status_reconnecting: 'Reconnecting…',
    status_off: 'Disconnected',
    listening: 'Listening…',
    me_suffix: '(you)',
    pending: 'translating…',
    pending_failed: '(translation unavailable)',
    sys_joined: '{name} joined the table {flag}',
    sys_left: '{name} left the table',
    plan_title: 'Table setup',
    plan_hint: 'Drag each person to their real seat around the table: their voice will come from that direction (3D audio, best with headphones).',
    plan_done: 'Done',
    mute_title: 'Mute microphone',
    enroll_title: 'Voice calibration',
    enroll_hint: 'Read this phrase aloud, naturally:',
    enroll_start: 'Start speaking',
    enroll_listening: '🎙️ We are listening…',
    enroll_step: 'Phrase {n} of {total}',
    enroll_retry: "We couldn't hear you clearly. Starting over.",
    enroll_done: 'Voice calibrated ✓',
    enroll_phrase_1: 'Hi there, nice to meet you',
    enroll_phrase_2: 'The sky is clear this morning',
    enroll_phrase_3: 'I would love a hot cup of coffee',
    voice_card_title: 'How you will sound',
    voice_card_hint: 'People who share your language hear your real voice. Everyone else will hear you through these voices, matched to your tone. Listen and change them if you like.',
    voice_enter: 'Enter the table',
    voice_preview: 'Play voice sample',
    err_name: 'Enter your name to take a seat',
    err_mic: 'Could not access the microphone: {msg}',
    err_mic_lost: 'Microphone lost; recovering…',
    err_mic_fail: 'Could not recover the microphone: {msg}',
    err_mic_paused: 'Microphone paused by the system…',
    err_capture: 'Audio capture failed; please reload the page',
    err_server_down: 'Could not join the table (server unavailable)',
    err_room_full: 'The table is full',
    mute_tip: '🎙️ Tap to let others hear you',
    status_muted: '🔇 Mic muted — tap the button to talk',
    status_turn_other: '⏳ {name} is speaking — wait for your turn',
    status_turn_mine: "🎤 Your turn — I'm listening…",
    feed_empty: 'Speak and the translated conversation will appear here',
    diag_title: '3D audio diagnostics',
    enroll_exit: 'Back to lobby',
  },
  ko: {
    tagline: '스스로 통역하는 테이블',
    name_label: '이름',
    name_ph: '수지',
    lang_label: '사용할 언어를 선택하세요…',
    sit: '🪑 테이블에 앉기',
    sitting: '입장 중…',
    notice: '🎧 이어폰 사용을 권장합니다 · ☀️ 화면이 계속 켜져 있습니다',
    leave: '나가기',
    leave_title: '테이블에서 나가기',
    status_connecting: '연결 중…',
    status_connected: '연결됨',
    status_reconnecting: '다시 연결 중…',
    status_off: '연결 끊김',
    listening: '듣고 있어요…',
    me_suffix: '(나)',
    pending: '번역 중…',
    pending_failed: '(번역할 수 없음)',
    sys_joined: '{name}님이 테이블에 앉았습니다 {flag}',
    sys_left: '{name}님이 나갔습니다',
    plan_title: '테이블 배치',
    plan_hint: '각 사람을 실제 자리로 드래그하세요. 그 방향에서 목소리가 들립니다 (3D 오디오, 이어폰 권장).',
    plan_done: '완료',
    mute_title: '마이크 음소거',
    enroll_title: '음성 보정',
    enroll_hint: '다음 문장을 자연스럽게 소리 내어 읽어주세요:',
    enroll_start: '시작하기',
    enroll_listening: '🎙️ 듣고 있습니다…',
    enroll_step: '문장 {n} / {total}',
    enroll_retry: '잘 들리지 않았어요. 처음부터 다시 시작합니다.',
    enroll_done: '음성 보정 완료 ✓',
    enroll_phrase_1: '안녕하세요, 만나서 반갑습니다',
    enroll_phrase_2: '오늘 아침 하늘이 맑네요',
    enroll_phrase_3: '따뜻한 커피 한 잔 마시고 싶어요',
    voice_card_title: '테이블에서 이렇게 들립니다',
    voice_card_hint: '같은 언어 사용자는 실제 목소리를 듣습니다. 다른 언어로는 내 톤에 맞춰 선택된 이 목소리로 들립니다. 들어보고 원하면 바꾸세요.',
    voice_enter: '테이블 입장',
    voice_preview: '음성 샘플 듣기',
    err_name: '이름을 입력해 주세요',
    err_mic: '마이크에 접근할 수 없습니다: {msg}',
    err_mic_lost: '마이크 연결이 끊겼습니다. 복구 중…',
    err_mic_fail: '마이크를 복구할 수 없습니다: {msg}',
    err_mic_paused: '시스템이 마이크를 일시 중지했습니다…',
    err_capture: '오디오 캡처에 실패했습니다. 페이지를 새로고침해 주세요',
    err_server_down: '테이블에 입장할 수 없습니다 (서버 연결 불가)',
    err_room_full: '테이블이 가득 찼습니다',
    mute_tip: '🎙️ 탭하면 들립니다',
    status_muted: '🔇 마이크 꺼짐 — 버튼을 누르면 말할 수 있어요',
    status_turn_other: '⏳ {name} 님이 말하는 중 — 차례를 기다려 주세요',
    status_turn_mine: '🎤 당신 차례 — 듣고 있어요…',
    feed_empty: '말하면 번역된 대화가 여기에 표시됩니다',
    diag_title: '3D 오디오 진단',
    enroll_exit: '로비로 돌아가기',
  },
};

// Mensajes conocidos del servidor (llegan en español) -> clave i18n local
const SERVER_MSG_KEYS = { 'Sala llena': 'err_room_full' };

function t(key, params = {}) {
  const dict = I18N[state.lang] || I18N.es;
  let s = dict[key] ?? I18N.es[key] ?? key;
  for (const [k, v] of Object.entries(params)) s = s.replaceAll(`{${k}}`, String(v));
  return s;
}

function translateServerMessage(message) {
  const key = SERVER_MSG_KEYS[message];
  return key ? t(key) : message;
}

/** Re-traduce TODO el DOM estático al idioma actual, sin recargar. */
function updateUI() {
  document.documentElement.lang = state.lang;
  document.querySelectorAll('[data-i18n]').forEach((el) => {
    el.textContent = t(el.dataset.i18n);
  });
  document.querySelectorAll('[data-i18n-placeholder]').forEach((el) => {
    el.placeholder = t(el.dataset.i18nPlaceholder);
  });
  document.querySelectorAll('[data-i18n-title]').forEach((el) => {
    el.title = t(el.dataset.i18nTitle);
    el.setAttribute('aria-label', t(el.dataset.i18nTitle));  // botones de solo-icono
  });
  $('feed').dataset.empty = t('feed_empty');   // estado vacío del feed (CSS :empty)
  ui.setStatus(ui._lastStatus);          // el estado visible también cambia de idioma
  ui.refreshMeStatus();
  if (enrollUi.active) renderEnrollStep();
}

/* ============================== Estado ================================== */

const state = {
  name: '',
  lang: 'es',
  voices: [],           // catálogo servido por /api/voices (para la tarjeta "Así sonarás")
  speakerId: null,
  room: new Map(),
  seated: false,
  joined: false,
  muted: true,
  enrolled: false,
  audioCtx: null,
  micNodes: null,
};

/* ============================== Utilidades ============================== */

const $ = (id) => document.getElementById(id);

function colorFor(speakerId) {
  let h = 0;
  for (const c of String(speakerId)) h = (h * 31 + c.charCodeAt(0)) % 360;
  return `hsl(${h} 70% 65%)`;
}

function toast(message, ms = 4000, kind = 'error') {
  const el = $('toast');
  el.textContent = message;
  el.className = 'toast' + (kind === 'info' ? ' info' : '');  // info: sin alarma roja
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add('hidden'), ms);
}

function clientToken() {
  try {
    let tk = sessionStorage.getItem('tradion_token');
    if (!tk) {
      tk = crypto.randomUUID ? crypto.randomUUID()
                             : `t-${Date.now()}-${Math.random().toString(36).slice(2)}`;
      sessionStorage.setItem('tradion_token', tk);
    }
    return tk;
  } catch {
    if (!clientToken._t) {
      clientToken._t = `t-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    }
    return clientToken._t;
  }
}

/** Compuerta del micrófono: join + mute manual (dúplex completo). Durante la
 * calibración solo se envía audio en la captura activa: lo dicho entre pasos
 * NO debe transcribirse ni difundirse a la sala.
 * IMPORTANTE: el enroll DEBE funcionar aunque el mic esté muteado (Start Muted). */
function micGateOpen() {
  if (!state.joined) return false;
  // Enrollment capture bypasses mute — la calibración necesita audio siempre
  if (enrollUi.active && enrollUi.capturing) return true;
  if (state.muted) return false;
  if (enrollUi.active && !enrollUi.capturing) return false;
  
  // CSMA/CA Floor Token: Si otra persona tiene el turno, nuestro micrófono se silencia.
  // Esto elimina de raíz el eco y el crosstalk captado por móviles cercanos.
  if (net && net.floorOwner && net.floorOwner !== state.speakerId) return false;

  return true;
}

/* ============================ WebSocket robusto ========================== */

class WSClient {
  constructor({ onJSON, onBinary, onState, onFatal }) {
    this.onJSON = onJSON;
    this.onBinary = onBinary;
    this.onState = onState;
    this.onFatal = onFatal;
    this.ws = null;
    this.backoffMs = 1000;
    this._timer = null;
    this._closedByUs = false;
    this._joined = false;
    this._fatalMsg = null;
    this._prejoinFails = 0;
    this.floorOwner = null;
    this.pendingMoves = new Map();   // speaker_id -> move retenido durante un corte
  }

  connect() {
    clearTimeout(this._timer);
    if (this.ws && this.ws.readyState <= WebSocket.OPEN) {
      try { this.ws.onclose = null; this.ws.close(); } catch { /* ya muerto */ }
    }
    this._closedByUs = false;
    this._joined = false;
    this._fatalMsg = null;
    this.onState(this._prejoinFails || this.backoffMs > 1000 ? 'reconnecting' : 'connecting');

    const ws = new WebSocket(`wss://${location.host}${WS_PATH}`);
    ws.binaryType = 'arraybuffer';
    this.ws = ws;

    ws.onopen = () => {
      this.sendJSON({
        type: 'join', name: state.name, language: state.lang,
        client_token: clientToken(),
      });
    };
    ws.onmessage = (ev) => {
      if (typeof ev.data === 'string') {
        try { this.onJSON(JSON.parse(ev.data)); }
        catch (err) { console.error('JSON inválido del servidor', err); }
      } else {
        this.onBinary(ev.data);
      }
    };
    ws.onclose = () => {
      state.joined = false;
      if (this._closedByUs) return;
      if (this._fatalMsg) {
        this.onFatal(this._fatalMsg);
        return;
      }
      if (!this._joined && ++this._prejoinFails >= PREJOIN_MAX_FAILS) {
        this.onFatal(t('err_server_down'));
        return;
      }
      this._scheduleReconnect();
    };
    ws.onerror = () => { /* onclose llega siempre después */ };
  }

  markJoined() {
    this._joined = true;
    this._prejoinFails = 0;
    this.backoffMs = 1000;
    this.onState('connected');
  }

  flagFatal(message) {
    if (!this._joined) this._fatalMsg = message;
  }

  _scheduleReconnect() {
    if (this._closedByUs || !state.seated) return;
    this.onState('reconnecting');
    clearTimeout(this._timer);
    this._timer = setTimeout(() => this.connect(), this.backoffMs);
    this.backoffMs = Math.min(this.backoffMs * 1.7, 10000);
  }

  close() {
    this._closedByUs = true;
    this._prejoinFails = 0;
    this.backoffMs = 1000;
    clearTimeout(this._timer);
    try { this.ws?.close(); } catch { /* ya muerto */ }
  }

  sendJSON(obj) {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(obj));
  }

  /** Los 'move' no pueden perderse en silencio: si el WS está caído (micro-corte),
   *  se retienen y se re-emiten tras el próximo 'joined'. Se retiene SOLO el propio
   *  asiento y solo mientras se sigue sentado: un arrastre de OTRO hecho durante un
   *  corte estaría rancio al reconectar (alguien pudo recolocarlo después con la
   *  mesa viva), y uno posterior a backToLobby no debe viajar a la próxima mesa. */
  sendMove(payload) {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(payload));
    } else if (state.seated && payload.speaker_id === state.speakerId) {
      this.pendingMoves.set(payload.speaker_id, payload);
    }
  }

  sendBinary(buf) {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(buf);
  }
}

/* ========================= Captura de micrófono ========================= */

class LinearResampler {
  constructor(fromRate, toRate) {
    this.ratio = fromRate / toRate;
    this.buf = new Float32Array(0);
    this.pos = 0;
  }
  process(input) {
    const merged = new Float32Array(this.buf.length + input.length);
    merged.set(this.buf); merged.set(input, this.buf.length);
    const out = [];
    let pos = this.pos;
    while (Math.floor(pos) + 1 < merged.length) {
      const i = Math.floor(pos), frac = pos - i;
      const s = merged[i] * (1 - frac) + merged[i + 1] * frac;
      out.push(Math.round(Math.max(-1, Math.min(1, s)) * 32767));
      pos += this.ratio;
    }
    const consumed = Math.min(Math.floor(pos), merged.length);
    this.buf = merged.slice(consumed);
    this.pos = pos - consumed;
    return Int16Array.from(out);
  }
}

let micBuffer = [];   // entradas {buf, t}: onset retenido mientras el floor nos bloquea
// 4 bloques de 2048 muestras @16 kHz = 512 ms de "onset" retenido (el diseño pide ~400 ms;
// el comentario anterior decía 400 ms con 8 bloques, que en realidad eran ~1 s)
const MAX_MIC_BUFFER_CHUNKS = 4;
// Alineado con floor.acquire_rms del servidor (0.025) + margen por el RMS diezmado del
// cliente: un onset retenido que NO pueda adquirir el canal solo genera frases partidas
const MIC_BUFFER_MIN_RMS = 0.028;
const MIC_BUFFER_MAX_AGE_MS = 700;  // voz más vieja que esto ya no es un "onset": se tira

function _pruneMicBuffer() {
  const now = performance.now();
  micBuffer = micBuffer.filter((e) => now - e.t < MIC_BUFFER_MAX_AGE_MS);
}

function _onMicBlock(arrayBuffer) {
  const rms = _rmsOfInt16(arrayBuffer);
  ui.setLevel(rms);

  const open = micGateOpen();

  // Queremos hablar (no estamos muteados) pero el sistema de Turno nos bloquea
  const blockedByFloor = !open && !state.muted && state.joined && net && net.floorOwner && net.floorOwner !== state.speakerId;

  if (open) {
    if (micBuffer.length > 0) {
      // Volcar SOLO onsets frescos: sin la poda por edad, hablar 0.5s durante el turno
      // ajeno dejaba voz retenida que, al liberarse el canal 60s después, ADQUIRÍA el
      // floor para alguien que ya estaba callado (frase fantasma + turno robado)
      _pruneMicBuffer();
      for (const entry of micBuffer) net.sendBinary(entry.buf);
      micBuffer = [];
    }
    net.sendBinary(arrayBuffer);
  } else if (blockedByFloor && rms > MIC_BUFFER_MIN_RMS) {
    // Retener solo bloques con voz clara: el ring buffer guardaba TAMBIÉN el eco del
    // que tenía el turno (justo el audio que el floor quería descartar) y lo
    // reproducía en nuestra sesión al liberarse el canal -> subtítulos duplicados
    micBuffer.push({ buf: arrayBuffer, t: performance.now() });
    _pruneMicBuffer();
    if (micBuffer.length > MAX_MIC_BUFFER_CHUNKS) micBuffer.shift();
  } else if (!blockedByFloor) {
    micBuffer = [];
  }
}

function _rmsOfInt16(arrayBuffer) {
  const a = new Int16Array(arrayBuffer);
  let sum = 0, n = 0;
  for (let i = 0; i < a.length; i += 8) { sum += a[i] * a[i]; n++; }
  return n ? Math.sqrt(sum / n) / 32768 : 0;
}

function _watchMicTrack(track) {
  track.addEventListener('ended', () => {
    toast(t('err_mic_lost'));
    reacquireMic().catch((err) => toast(t('err_mic_fail', { msg: err.message })));
  });
  track.addEventListener('mute', () => toast(t('err_mic_paused'), 2500));
}

async function reacquireMic() {
  if (!state.audioCtx || !state.micNodes) return;
  const stream = await navigator.mediaDevices.getUserMedia(MIC_CONSTRAINTS);
  const source = state.audioCtx.createMediaStreamSource(stream);
  try { state.micNodes.source.disconnect(); } catch { /* ya desconectado */ }
  state.micNodes.stream.getTracks().forEach((tk) => tk.stop());
  source.connect(state.micNodes.node || state.micNodes.proc);
  state.micNodes.stream = stream;
  state.micNodes.source = source;
  _watchMicTrack(stream.getAudioTracks()[0]);
}

function _makeWorkletNode(ctx) {
  const node = new AudioWorkletNode(ctx, 'tradion-capture', {
    numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1],
  });
  node.port.onmessage = (ev) => _onMicBlock(ev.data);
  node.onprocessorerror = (ev) => {
    console.error('Worklet de captura caído; reconstruyendo', ev);
    try {
      const fresh = _makeWorkletNode(ctx);
      state.micNodes.source.disconnect();
      state.micNodes.node.disconnect();
      state.micNodes.source.connect(fresh);
      fresh.connect(state.micNodes.sink);
      state.micNodes.node = fresh;
    } catch (err) {
      console.error(err);
      toast(t('err_capture'));
    }
  };
  return node;
}

async function startMic(ctx) {
  const stream = await navigator.mediaDevices.getUserMedia(MIC_CONSTRAINTS);
  try {
    const source = ctx.createMediaStreamSource(stream);
    const sink = ctx.createGain();
    sink.gain.value = 0;
    sink.connect(ctx.destination);

    if (ctx.audioWorklet) {
      await ctx.audioWorklet.addModule('/static/audio-worklet.js');
      const node = _makeWorkletNode(ctx);
      source.connect(node);
      node.connect(sink);
      state.micNodes = { stream, source, node, sink };
    } else {
      const proc = ctx.createScriptProcessor(4096, 1, 1);
      const resampler = new LinearResampler(ctx.sampleRate, 16000);
      proc.onaudioprocess = (ev) => {
        const block = resampler.process(ev.inputBuffer.getChannelData(0));
        if (block.length) _onMicBlock(block.buffer);
      };
      source.connect(proc);
      proc.connect(sink);
      state.micNodes = { stream, source, proc, sink };
    }
    _watchMicTrack(stream.getAudioTracks()[0]);
  } catch (err) {
    stream.getTracks().forEach((tk) => tk.stop());
    throw err;
  }
}

/* ============== Salida de audio con AEC hardware (anti-eco) ============== */

class EchoSafeOutput {
  constructor(ctx) {
    this.ctx = ctx;
    this.input = ctx.createGain();
    this.audioEl = null;
    this._pcs = null;
    this._dest = null;
    this.mode = 'direct';
  }

  async init() {
    // La ruta <audio srcObject> SOLO aporta en Android/Chromium (agujero del AEC con
    // Web Audio). En iOS Y en Safari de escritorio activa bugs de WebKit (volumen,
    // enrutado): directo a destination, donde el AEC del sistema ya referencia.
    if (!IS_ANDROID) {
      this.input.connect(this.ctx.destination);
      this.mode = 'direct';
      console.info('Ruta de salida TTS: direct (AEC de sistema)');
      return;
    }
    try {
      this._dest = this.ctx.createMediaStreamDestination();
      this.input.connect(this._dest);
      const el = new Audio();
      el.autoplay = true;
      el.playsInline = true;
      el.srcObject = this._dest.stream;
      this.audioEl = el;
      await el.play();
      this.mode = 'element';
    } catch (err) {
      console.warn('Salida <audio> no disponible; degradando a destination', err);
      if (this.audioEl) {
        this.audioEl.pause();
        this.audioEl.srcObject = null;
        this.audioEl = null;
      }
      try { this.input.disconnect(); } catch { /* sin conexiones */ }
      this.input.connect(this.ctx.destination);
      this.mode = 'direct';
    }
    console.info(`Ruta de salida TTS: ${this.mode}`);
  }

  async upgradeLoopback() {
    if (!IS_ANDROID || this.mode !== 'element' || !window.RTCPeerConnection) return;
    try {
      const remote = await this._loopback(this._dest.stream);
      this.audioEl.srcObject = remote;
      await this.audioEl.play().catch(() => {});
      this.mode = 'loopback';
    } catch (err) {
      this._closePcs();
      console.warn('Loopback AEC no disponible; se mantiene la salida <audio>', err);
    }
    console.info(`Ruta de salida TTS: ${this.mode}`);
  }

  async _loopback(stream) {
    const pc1 = new RTCPeerConnection();
    const pc2 = new RTCPeerConnection();
    this._pcs = [pc1, pc2];
    pc1.onicecandidate = (ev) => ev.candidate && pc2.addIceCandidate(ev.candidate).catch(() => {});
    pc2.onicecandidate = (ev) => ev.candidate && pc1.addIceCandidate(ev.candidate).catch(() => {});
    let remote = null;
    pc2.ontrack = (ev) => { remote = ev.streams[0]; };
    const connected = new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('loopback timeout')), 3000);
      pc2.onconnectionstatechange = () => {
        if (pc2.connectionState === 'connected') { clearTimeout(timer); resolve(); }
        else if (pc2.connectionState === 'failed') { clearTimeout(timer); reject(new Error('loopback ICE failed')); }
      };
    });
    for (const track of stream.getTracks()) pc1.addTrack(track, stream);
    // CRÍTICO PARA EL 3D: el HRTF produce audio BINAURAL ESTÉREO, pero Opus en WebRTC
    // negocia MONO por defecto — el loopback aplastaba los dos canales y destruía la
    // espacialización en Android. RFC 7587: el fmtp es declarativo del RECEPTOR, así
    // que basta stereo=1 en la descripción REMOTA de pc1 (la answer); se munge anclado
    // a la línea a=fmtp del payload real de Opus (no a un literal frágil) y SOLO en
    // las descripciones remotas (las locales quedan intactas: sin SDP munging local).
    const forceStereo = (sdp) => {
      const rtpmap = sdp.match(/^a=rtpmap:(\d+) opus\/48000\/2/mi);
      if (!rtpmap) return sdp;
      const pt = rtpmap[1];
      const fmtpLine = new RegExp(`^a=fmtp:${pt} [^\\r\\n]*`, 'mi');
      if (fmtpLine.test(sdp)) {
        return sdp.replace(fmtpLine, (line) => /stereo=/i.test(line)
          ? line.replace(/stereo=\d/i, 'stereo=1')
          : `${line};stereo=1;sprop-stereo=1`);
      }
      return sdp.replace(new RegExp(`^(a=rtpmap:${pt} [^\\r\\n]*)`, 'mi'),
                         `$1\r\na=fmtp:${pt} stereo=1;sprop-stereo=1`);
    };
    const offer = await pc1.createOffer();
    await pc1.setLocalDescription(offer);
    await pc2.setRemoteDescription({ type: 'offer', sdp: forceStereo(offer.sdp) });
    const answer = await pc2.createAnswer();
    await pc2.setLocalDescription(answer);
    await pc1.setRemoteDescription({ type: 'answer', sdp: forceStereo(answer.sdp) });
    await connected;
    if (!remote) throw new Error('loopback sin ontrack');
    return remote;
  }

  _closePcs() {
    this._pcs?.forEach((pc) => { try { pc.close(); } catch { /* ya cerrada */ } });
    this._pcs = null;
  }

  close() {
    this._closePcs();
    if (this.audioEl) {
      this.audioEl.pause();
      this.audioEl.srcObject = null;
      this.audioEl = null;
    }
    try { this.input.disconnect(); } catch { /* sin conexiones */ }
  }
}

/* ===================== Reproducción del TTS entrante ===================== */

function decodeAudio(ctx, arrayBuffer) {
  return new Promise((resolve, reject) => {
    const maybe = ctx.decodeAudioData(arrayBuffer, resolve, reject);
    if (maybe && typeof maybe.then === 'function') maybe.then(resolve, reject);
  });
}

class TTSPlayer {
  constructor(ctx, output) {
    this.ctx = ctx;
    this.output = output;
    this.nextStart = new Map();
    this.gains = new Map();      // speaker_id -> GainNode (gancho para PannerNode en F6)
    this.panners = new Map();    // speaker_id -> PannerNode (F6)
    this._tail = new Map();      // speaker_id -> Promise (orden de LLEGADA garantizado)
    this._dropped = new Set();   // peers expulsados: sus TTS tardíos se descartan
  }

  _gainFor(speakerId) {
    let g = this.gains.get(speakerId);
    if (!g) {
      g = this.ctx.createGain();
      const panner = this.ctx.createPanner();
      panner.panningModel = 'HRTF';

      // SIN conos direccionales: apuntaban al CENTRO de la mesa, pero el oyente está
      // en el BORDE — para los asientos vecinos quedaba fuera del cono (coneOuterGain
      // 0.2 = -14 dB arbitrarios) y ese vaivén de volumen enmascaraba por completo la
      // direccionalidad del HRTF. En una mesa, las fuentes deben ser omnidireccionales:
      // la dirección la da la geometría oyente-fuente, no la "boca" de la fuente.

      // Modelo de distancia SUAVE: con los defaults (inverse, rolloff=1) el comensal de
      // enfrente (~7 m virtuales) sonaba 5x más bajo que el de al lado. rolloff 0.35
      // mantiene una pista sutil de cercanía sin destrozar la inteligibilidad.
      panner.distanceModel = 'inverse';
      panner.refDistance = 2;
      panner.rolloffFactor = 0.35;
      // (sin maxDistance: en el modelo 'inverse' se ignora — solo aplica a 'linear')

      const pos = seats.ensure(speakerId);
      panner.positionX.value = pos.x * SPATIAL_SCALE;
      panner.positionY.value = 0;
      panner.positionZ.value = pos.y * SPATIAL_SCALE;

      this.panners.set(speakerId, panner);

      g.connect(panner);
      panner.connect(this.output.input);
      this.gains.set(speakerId, g);
    }
    return g;
  }

  play(header, wavBuffer) {
    // Un TTS tardío de un peer ya expulsado NO debe resucitar su gain+panner vía
    // _gainFor (sonaría un "fantasma" en posición arbitraria y esos nodos HRTF
    // no se limpiarían jamás: peer_left no se re-emite para ese speaker_id)
    if (this._dropped.has(header.speaker_id)) return;
    const prev = this._tail.get(header.speaker_id) || Promise.resolve();
    const next = prev.then(() => {
      if (this._dropped.has(header.speaker_id)) return;
      return this._decodeAndSchedule(header, wavBuffer);
    });
    this._tail.set(header.speaker_id, next.catch(() => {}));
  }

  async _decodeAndSchedule(header, wavBuffer) {
    let audioBuf;
    try {
      audioBuf = await decodeAudio(this.ctx, wavBuffer);
    } catch (err) {
      console.error('No se pudo decodificar el TTS', header, err);
      return;
    }
    const src = this.ctx.createBufferSource();
    src.buffer = audioBuf;
    src.connect(this._gainFor(header.speaker_id));
    const now = this.ctx.currentTime;
    const start = Math.max(now, this.nextStart.get(header.speaker_id) || 0);
    if (window.duckCount === undefined) window.duckCount = 0;
    src.onended = () => {
      window.duckCount = Math.max(0, window.duckCount - 1);
      if (window.duckCount === 0 && state.micNodes && state.micNodes.node) {
         state.micNodes.node.port.postMessage({ type: 'duck', value: false });
      }
    };
    window.duckCount++;
    if (window.duckCount === 1 && state.micNodes && state.micNodes.node) {
       state.micNodes.node.port.postMessage({ type: 'duck', value: true });
    }

    src.start(start);
    this.nextStart.set(header.speaker_id, start + audioBuf.duration + 0.05);
  }

  /** Levanta el veto de dropSpeaker: el client_token persiste en localStorage, así que
   *  quien sale y vuelve REUTILIZA su speaker_id — sin esto quedaría mudo para siempre. */
  restoreSpeaker(speakerId) {
    this._dropped.delete(speakerId);
  }

  /** Tras un 'joined' de reconexión: purgar nodos de peers que se fueron mientras
   *  estábamos desconectados (su peer_left nunca llegó a este cliente). */
  pruneSpeakers(validIds) {
    for (const id of [...this.gains.keys()])
      if (!validIds.has(id)) this.dropSpeaker(id);
  }

  dropSpeaker(speakerId) {
    this._dropped.add(speakerId);
    this.gains.get(speakerId)?.disconnect();
    this.gains.delete(speakerId);
    this.panners.get(speakerId)?.disconnect();
    this.panners.delete(speakerId);
    this.nextStart.delete(speakerId);
    this._tail.delete(speakerId);
  }
}

/** Escala mundo: [-1,1] del plano -> metros WebAudio. */
const SPATIAL_SCALE = 5;
const SPATIAL_TAU = 0.08;   // setTargetAtTime: ~63% del recorrido en 80 ms

/** Mueve un AudioParam SIN saltos: asignar .value en pleno arrastre produce el
 * "zipper"/clicks que hacían sentir el 3D tosco. */
function _glide(param, value, ctx) {
  param.setTargetAtTime(value, ctx.currentTime, SPATIAL_TAU);
}

/** Recoloca el panner de un hablante (suavizado). Omnidireccional: sin orientación.
 * OJO: usa el binding léxico `player`, NO window.player — con script clásico, un
 * `let` global no crea propiedad de window: window.player era SIEMPRE undefined y
 * el reposicionamiento de panners fue un no-op desde el primer día (por eso el 3D
 * "no seguía" a los avatares: cada fuente quedaba clavada donde nació). */
function positionPanner(speakerId, pos) {
  const p = player?.panners?.get(speakerId);
  if (!p || !state.audioCtx) return;
  _glide(p.positionX, pos.x * SPATIAL_SCALE, state.audioCtx);
  _glide(p.positionZ, pos.y * SPATIAL_SCALE, state.audioCtx);
}

/** F6-fix: el OYENTE debe estar en SU asiento mirando hacia donde mira su avatar.
 * Sin esto, el listener quedaba en el origen mirando a -Z y el 3D "no funcionaba":
 * las direcciones no correspondían con la mesa. */
function updateListener() {
  if (!state.audioCtx || !state.speakerId) return;
  const pos = seats.positions.get(state.speakerId);
  if (!pos) return;
  const listener = state.audioCtx.listener;
  const x = pos.x * SPATIAL_SCALE, z = pos.y * SPATIAL_SCALE;
  const fx = Math.cos(pos.angle), fz = Math.sin(pos.angle);
  if (listener.positionX) {
    // "Primera vez" ligada AL CONTEXTO: el flag debe renacer con cada AudioContext
    // (al re-sentarse hay contexto nuevo; un flag global haría glide desde el origen)
    const first = updateListener._ctx !== state.audioCtx;
    updateListener._ctx = state.audioCtx;
    if (first) {
      // Primera colocación: SNAP (glide desde el origen sería un 'vuelo' de 400 ms)
      listener.positionX.value = x;
      listener.positionZ.value = z;
    } else {
      _glide(listener.positionX, x, state.audioCtx);
      _glide(listener.positionZ, z, state.audioCtx);
    }
    listener.positionY.value = 0;
    // La ORIENTACIÓN se fija en seco, jamás se interpola: el lerp componente a
    // componente de un vector dirección pasa por (0,0,0) en giros de 180° — vector
    // forward inválido según la spec de Web Audio (comportamiento indefinido/NaN)
    listener.forwardX.value = fx;
    listener.forwardY.value = 0;
    listener.forwardZ.value = fz;
    listener.upX.value = 0; listener.upY.value = 1; listener.upZ.value = 0;
  } else {
    // Safari con API antigua de AudioListener (sin AudioParams: sin suavizado posible)
    listener.setPosition(x, 0, z);
    listener.setOrientation(fx, 0, fz, 0, 1, 0);
  }
}

function handleBinary(buf) {
  try {
    const view = new DataView(buf);
    const headerLen = view.getUint32(0);
    const headerBytes = new Uint8Array(buf, 4, headerLen);
    const header = JSON.parse(new TextDecoder().decode(headerBytes));
    if (header.type === 'tts') {
      player.play(header, buf.slice(4 + headerLen));
      ui.markSpeaking(header.speaker_id);
    }
  } catch (err) {
    console.error('Frame binario inválido', err);
  }
}

/* ================= Asientos: plano 2D radial de la mesa ================= */

const seats = {
  positions: new Map(),   // speaker_id -> {x, y} en [-1,1] (F6: PannerNode x/z)
  _dirty: false,
  _draggingId: null,      // ficha bajo MI dedo: los peer_moved sobre ella se ignoran

  ensure(id) {
    if (!this.positions.has(id)) {
      const i = this.positions.size;
      const angle = (i * 2 * Math.PI) / 6 - Math.PI / 2;
      const x = +(0.72 * Math.cos(angle)).toFixed(3);
      const y = +(0.72 * Math.sin(angle)).toFixed(3);
      const faceAngle = Math.atan2(-y, -x);
      this.positions.set(id, { x, y, angle: +faceAngle.toFixed(3) });
    }
    return this.positions.get(id);
  },

  render() {
    const plan = $('plan');
    if (plan.querySelector('.seat.dragging')) {   // no matar el drag en curso
      this._dirty = true;
      return;
    }
    this._dirty = false;
    plan.querySelectorAll('.seat').forEach((s) => s.remove());
    for (const [id, member] of state.room) {
      const pos = this.ensure(id);
      const seat = document.createElement('div');
      seat.className = 'seat';
      seat.dataset.id = id;
      seat.style.background = colorFor(id);
      seat.style.left = `${50 + pos.x * 44}%`;
      seat.style.top = `${50 + pos.y * 44}%`;
      // La ficha NO gira (la 'R' salía boca abajo y las etiquetas caían en
      // posiciones distintas según el ángulo): solo gira la "nariz" dentro
      // de su propio contenedor rotado
      seat.style.transform = 'translate(-50%, -50%)';
      seat.textContent = (member.name || '?')[0].toUpperCase();
      const rot = document.createElement('div');
      rot.className = 'seat-rot';
      rot.style.transform = `rotate(${pos.angle + Math.PI / 2}rad)`;
      const nose = document.createElement('div');
      nose.className = 'seat-nose';
      rot.appendChild(nose);
      seat.appendChild(rot);
      const label = document.createElement('span');
      label.className = 'seat-name';
      label.textContent = id === state.speakerId ? `${member.name} ${t('me_suffix')}` : member.name;
      seat.appendChild(label);
      this._draggable(seat, plan);
      plan.appendChild(seat);
    }
  },

  _draggable(seat, plan) {
    seat.addEventListener('pointerdown', (ev) => {
      ev.preventDefault();
      seat.setPointerCapture(ev.pointerId);
      seat.classList.add('dragging');
      this._draggingId = seat.dataset.id;

      const move = (mv) => {
        const rect = plan.getBoundingClientRect();
        let x = ((mv.clientX - rect.left) / rect.width - 0.5) * 2;
        let y = ((mv.clientY - rect.top) / rect.height - 0.5) * 2;
        const r = Math.hypot(x, y);
        if (r > 1) { x /= r; y /= r; }
        seat.style.left = `${50 + x * 44}%`;
        seat.style.top = `${50 + y * 44}%`;
        const faceAngle = Math.atan2(-y, -x);
        this.positions.set(seat.dataset.id, { x: +x.toFixed(3), y: +y.toFixed(3), angle: +faceAngle.toFixed(3) });
        const rot = seat.querySelector('.seat-rot');
        if (rot) rot.style.transform = `rotate(${faceAngle + Math.PI / 2}rad)`;

        positionPanner(seat.dataset.id, { x, y });   // suavizado: sin zipper al arrastrar
        if (seat.dataset.id === state.speakerId) updateListener();  // el oyente sigue mi dedo
      };
      const up = () => {
        seat.classList.remove('dragging');
        this._draggingId = null;
        seat.removeEventListener('pointermove', move);
        seat.removeEventListener('pointerup', up);
        seat.removeEventListener('pointercancel', up);
        if (this._dirty) this.render();
        const pos = this.positions.get(seat.dataset.id);
        // speaker_id EXPLÍCITO: el plano es colaborativo ("arrastra a cada persona")
        // — sin él, el servidor aplicaba el arrastre de la ficha de A a la posición
        // del REMITENTE: B veía moverse a A pero la mesa entera veía moverse a B
        if (pos) net.sendMove({ type: 'move', speaker_id: seat.dataset.id,
                                x: pos.x, y: pos.y, angle: pos.angle });
        if (seat.dataset.id === state.speakerId) updateListener();  // me moví YO
      };
      seat.addEventListener('pointermove', move);
      seat.addEventListener('pointerup', up);
      seat.addEventListener('pointercancel', up);
    });
  },

  clear() {
    this.positions.clear();
  },
};

/* ================================== UI ================================== */

const ui = {
  bubbles: new Map(),        // "speaker_id\n<original>" -> burbuja pendiente de traducción
  _live: new Map(),          // speaker_id -> burbuja "en vivo" (parciales de otros)
  _liveTimers: new Map(),
  latestSeg: new Map(),      // speaker_id -> mayor segment_id visto (ordena parciales/finales)
  _myTextTimer: null,
  speaking: new Set(),
  speakingTimers: new Map(),
  _levelDecay: null,
  _lastStatus: 'off',

  showTable() {
    $('lobby').classList.add('hidden');
    $('table').classList.remove('hidden');
    $('meTag').textContent = `${FLAGS[state.lang]} ${state.name}`;
    this.updateFloorUI();
  },

  updateFloorUI() {
    if (!state.seated) return;
    // Resaltar el CHIP del dueño del turno (visible para todos) y traerlo a la
    // vista si el scroll horizontal lo dejó fuera — antes el único indicio era
    // un glow sutil en tu propia etiqueta, y solo cuando el turno era TUYO
    document.querySelectorAll('.chip.floor').forEach((c) => c.classList.remove('floor'));
    if (net.floorOwner) {
      const chip = $(`chip-${net.floorOwner}`);
      if (chip) {
        chip.classList.add('floor');
        chip.scrollIntoView({ behavior: 'smooth', inline: 'nearest', block: 'nearest' });
      }
    }
    this.refreshMeStatus();
  },

  showLobby() {
    $('table').classList.add('hidden');
    $('planModal').classList.add('hidden');
    $('enrollModal').classList.add('hidden');
    $('voiceModal').classList.add('hidden');
    $('lobby').classList.remove('hidden');
    const btn = $('sitBtn');
    btn.disabled = false;
    btn.dataset.i18n = 'sit';
    btn.textContent = t('sit');
  },

  resetDom() {
    $('feed').innerHTML = '';
    $('room').innerHTML = '';
    $('meText').textContent = t('listening');
    $('meText').className = 'me-text';
    for (const bubble of this.bubbles.values()) clearTimeout(bubble._pendingTimer);
    this.bubbles.clear();
    for (const timer of this._liveTimers.values()) clearTimeout(timer);
    this._liveTimers.clear();
    this._live.clear();
    for (const timer of this.speakingTimers.values()) clearTimeout(timer);
    this.speakingTimers.clear();
    this.speaking.clear();
    this.latestSeg.clear();
    clearTimeout(this._myTextTimer);
    this.setLevel(0);
  },

  setStatus(status) {
    this._lastStatus = status;
    const el = $('status');
    el.className = 'status ' + status;
    $('statusText').textContent = t(`status_${status}`);
    if (enrollUi.active) {
      // Sin conexión no se puede validar: el botón de calibración espera al 'joined'
      $('enrollStart').disabled = status !== 'connected';
    }
  },

  setLevel(level) {
    const bars = $('vu').children;
    const boost = Math.min(1, level * 6);
    const shape = [0.5, 0.8, 1.0, 0.7, 0.45];
    for (let i = 0; i < bars.length; i++) {
      bars[i].style.transform = `scaleY(${Math.max(0.08, boost * shape[i]).toFixed(2)})`;
    }
    clearTimeout(this._levelDecay);
    this._levelDecay = setTimeout(() => {
      for (const bar of bars) bar.style.transform = 'scaleY(0.08)';
    }, 250);
  },

  setMyText(text, { partial = false } = {}) {
    const el = $('meText');
    el.textContent = text;
    el.className = 'me-text ' + (partial ? 'partial' : 'final');
    // Caduca solo: un parcial huérfano no debe quedarse fijado, y un final ya
    // leído debe ceder el sitio a la línea de estado contextual (mute/turno)
    clearTimeout(this._myTextTimer);
    this._myTextTimer = setTimeout(() => {
      el.className = 'me-text';
      this.refreshMeStatus();
    }, partial ? 7000 : 6000);
  },

  /** Línea de estado contextual del strip: por qué (no) te estamos escuchando.
   *  Sin esto, el floor token descartaba tu voz SIN NINGÚN aviso y "Te escucho…"
   *  se mostraba incluso con el micro silenciado. No pisa transcripciones vivas. */
  refreshMeStatus() {
    if (!state.seated) return;
    const el = $('meText');
    if (el.classList.contains('partial') || el.classList.contains('final')) return;
    if (state.muted) { el.textContent = t('status_muted'); return; }
    if (net?.floorOwner && net.floorOwner !== state.speakerId) {
      const owner = state.room.get(net.floorOwner);
      el.textContent = t('status_turn_other', { name: owner?.name || '…' });
      return;
    }
    if (state.speakerId && net?.floorOwner === state.speakerId) {
      el.textContent = t('status_turn_mine');
      return;
    }
    el.textContent = t('listening');
  },

  renderRoom() {
    const room = $('room');
    room.innerHTML = '';
    for (const [id, member] of state.room) {
      const chip = document.createElement('div');
      chip.className = 'chip';
      chip.id = `chip-${id}`;
      if (this.speaking.has(id)) chip.classList.add('speaking');
      if (net?.floorOwner === id) chip.classList.add('floor');   // dueño del turno
      const avatar = document.createElement('span');
      avatar.className = 'avatar';
      avatar.style.background = colorFor(id);
      avatar.textContent = (member.name || '?')[0].toUpperCase();
      const label = document.createElement('span');
      const suffix = id === state.speakerId ? ` ${t('me_suffix')}` : '';
      label.textContent = `${member.name}${suffix} ${FLAGS[member.language] || ''}`;
      chip.append(avatar, label);
      room.appendChild(chip);
    }
    seats.render();
  },

  markSpeaking(speakerId) {
    this.speaking.add(speakerId);
    $(`chip-${speakerId}`)?.classList.add('speaking');
    clearTimeout(this.speakingTimers.get(speakerId));
    this.speakingTimers.set(speakerId, setTimeout(() => {
      this.speaking.delete(speakerId);
      this.speakingTimers.delete(speakerId);
      $(`chip-${speakerId}`)?.classList.remove('speaking');
    }, 1500));
  },

  sysline(text) {
    const el = document.createElement('div');
    el.className = 'sysline';
    el.textContent = text;
    this._append(el);
  },

  /** Burbuja "en vivo" de un parcial ajeno: se actualiza en sitio y caduca sola. */
  showLivePartial(msg) {
    let bubble = this._live.get(msg.speaker_id);
    if (!bubble || !bubble.isConnected) {
      bubble = document.createElement('div');
      bubble.className = 'bubble live';
      const who = document.createElement('div');
      who.className = 'who';
      const nameEl = document.createElement('span');
      nameEl.className = 'name';
      nameEl.style.color = colorFor(msg.speaker_id);
      nameEl.textContent = msg.name;
      who.append(nameEl, document.createTextNode(FLAGS[msg.language] || ''));
      bubble.appendChild(who);
      const original = document.createElement('div');
      original.className = 'original';
      bubble.appendChild(original);
      this._live.set(msg.speaker_id, bubble);
      this._append(bubble);
    }
    bubble.querySelector('.original').textContent = `${msg.text} …`;
    clearTimeout(this._liveTimers.get(msg.speaker_id));
    this._liveTimers.set(msg.speaker_id, setTimeout(() => {
      this._dropLive(msg.speaker_id);
    }, LIVE_PARTIAL_TTL_MS));
    this.markSpeaking(msg.speaker_id);
    this._scrollFeed();
  },

  _dropLive(speakerId) {
    clearTimeout(this._liveTimers.get(speakerId));
    this._liveTimers.delete(speakerId);
    const bubble = this._live.get(speakerId);
    this._live.delete(speakerId);
    bubble?.remove();
  },

  addSubtitle(msg, { fresh = true } = {}) {
    // Un final TARDÍO de la locución anterior no debe matar la burbuja en vivo
    // de la locución en curso (fresh=false cuando su segment_id es viejo)
    if (fresh) this._dropLive(msg.speaker_id);
    const mine = msg.speaker_id === state.speakerId;
    const bubble = document.createElement('div');
    bubble.className = 'bubble' + (mine ? ' mine' : '');

    const who = document.createElement('div');
    who.className = 'who';
    const nameEl = document.createElement('span');
    nameEl.className = 'name';
    nameEl.style.color = colorFor(msg.speaker_id);
    nameEl.textContent = mine ? `${msg.name} ${t('me_suffix')}` : msg.name;
    who.append(nameEl, document.createTextNode(FLAGS[msg.language] || ''));
    bubble.appendChild(who);

    const original = document.createElement('div');
    original.className = 'original';
    original.textContent = msg.text;
    bubble.appendChild(original);

    if (!mine && msg.language !== state.lang) {
      const pending = document.createElement('div');
      pending.className = 'pending';
      pending.textContent = t('pending');
      bubble.appendChild(pending);
      const key = `${msg.speaker_id}\n${msg.text}`;
      this.bubbles.set(key, bubble);
      bubble._pendingTimer = setTimeout(() => {
        const p = bubble.querySelector('.pending');
        if (p) p.textContent = t('pending_failed');
        this.bubbles.delete(key);
      }, PENDING_TIMEOUT_MS);
    }
    this._append(bubble);
    this.markSpeaking(msg.speaker_id);
  },

  applyTranslation(msg) {
    const key = `${msg.speaker_id}\n${msg.original}`;
    let bubble = this.bubbles.get(key);
    if (!bubble) {
      this.addSubtitle({ speaker_id: msg.speaker_id, name: msg.name,
                         language: msg.source_lang, text: msg.original });
      bubble = this.bubbles.get(key);
      if (!bubble) return;
    }
    clearTimeout(bubble._pendingTimer);
    this.bubbles.delete(key);
    bubble.querySelector('.pending')?.remove();
    const translated = document.createElement('div');
    translated.className = 'translated';
    translated.textContent = msg.text;
    bubble.appendChild(translated);
    if (msg.latency_ms?.total != null) {
      const lat = document.createElement('div');
      lat.className = 'latency';
      lat.textContent = `⚡ ${(msg.latency_ms.total / 1000).toFixed(1)} s`;
      bubble.appendChild(lat);
    }
    this._scrollFeed();
  },

  _append(el) {
    const feed = $('feed');
    feed.appendChild(el);
    while (feed.children.length > FEED_MAX_NODES) {
      const first = feed.firstElementChild;
      for (const [k, v] of this.bubbles) {
        if (v === first) { clearTimeout(v._pendingTimer); this.bubbles.delete(k); }
      }
      for (const [k, v] of this._live) {
        if (v === first) { clearTimeout(this._liveTimers.get(k)); this._liveTimers.delete(k); this._live.delete(k); }
      }
      first.remove();
    }
    this._scrollFeed();
  },
  _scrollState: {
    userScrolling: false,
    resumeTimer: null,
  },

  _bindScrollEvent() {
    const feed = $('feed');
    // Detect scroll events to know if user scrolled up
    feed.addEventListener('scroll', () => {
      // Threshold más alto (150px) para evitar activaciones accidentales
      const isAtBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 150;
      if (!isAtBottom) {
        this._scrollState.userScrolling = true;
        clearTimeout(this._scrollState.resumeTimer);
        this._scrollState.resumeTimer = setTimeout(() => {
          this._scrollState.userScrolling = false;
          this._scrollFeed(true);
        }, 15000); // 15 seconds without scrolling = auto resume
      } else {
        this._scrollState.userScrolling = false;
        clearTimeout(this._scrollState.resumeTimer);
      }
    });
    
    // Also reset the timer if user touches the feed while userScrolling is true
    const resetTimer = () => {
      if (this._scrollState.userScrolling) {
        clearTimeout(this._scrollState.resumeTimer);
        this._scrollState.resumeTimer = setTimeout(() => {
          this._scrollState.userScrolling = false;
          this._scrollFeed(true);
        }, 15000);
      }
    };
    feed.addEventListener('pointerdown', resetTimer);
    feed.addEventListener('touchstart', resetTimer, { passive: true });
  },

  _scrollFeed(force = false) {
    const feed = $('feed');
    if (!force && this._scrollState.userScrolling) return; // Don't interrupt user
    // Fallback robusto en vez de scrollTo() para máxima compatibilidad
    feed.scrollTop = feed.scrollHeight;
  },
};

/** El ducking no debe comerse la calibración: bypass durante la captura del enroll. */
function setDuckBypass(value) {
  state.micNodes?.node?.port.postMessage({ type: 'duck_bypass', value });
}

/**
 * Única fuente de verdad del bypass. El bypass aplica si:
 *  - estoy capturando una frase de enroll (un TTS ajeno no puede silenciar mi calibración), o
 *  - TENGO el turno de palabra: con ducking full-mute, un TTS rezagado de otro idioma
 *    sonando en MI móvil silenciaba mi propio micro >400 ms y el servidor me quitaba
 *    el floor a mitad de frase. Quien posee el canal habla por encima del TTS.
 * Llamar SIEMPRE a esta función (no a setDuckBypass) al cambiar enroll o floor.
 */
function refreshDuckBypass() {
  const ownFloor = !!(state.speakerId && net && net.floorOwner === state.speakerId);
  setDuckBypass(enrollUi.capturing || ownFloor);
}

/* ============ Calibración de voz multi-paso validada por ASR ============ */

let enrollRun = 0;
const enrollUi = { active: false, capturing: false, step: 1, timer: null, run: 0 };

function startEnrollment() {
  enrollRun++;
  enrollUi.active = true;
  enrollUi.capturing = false;
  enrollUi.step = 1;
  enrollUi.run = enrollRun;
  renderEnrollStep();
}

function renderEnrollStep() {
  enrollUi.capturing = false;      // entre pasos, el micrófono no llega a la sala
  $('enrollModal').classList.remove('hidden');
  $('enrollStep').textContent = t('enroll_step', { n: enrollUi.step, total: ENROLL_STEPS });
  $('enrollHint').textContent = t('enroll_hint');
  $('enrollPrompt').textContent = t(`enroll_phrase_${enrollUi.step}`);
  const startBtn = $('enrollStart');
  startBtn.textContent = t('enroll_start');
  startBtn.disabled = ui._lastStatus !== 'connected';
  startBtn.classList.remove('hidden');
  $('enrollListening').classList.add('hidden');
}

function beginEnrollCapture() {
  if (!enrollUi.active) return;
  if (!state.joined || net.ws?.readyState !== WebSocket.OPEN) {
    toast(t('status_reconnecting'));   // sin sesión no hay validación posible
    return;
  }
  $('enrollStart').classList.add('hidden');
  const listening = $('enrollListening');
  listening.textContent = t('enroll_listening');
  listening.classList.remove('hidden');
  // El orden FIFO del WebSocket garantiza que el start llega antes que el audio
  net.sendJSON({
    type: 'enroll', action: 'start', run: enrollRun,
    step: enrollUi.step, expected: t(`enroll_phrase_${enrollUi.step}`),
  });
  enrollUi.capturing = true;
  refreshDuckBypass();   // un TTS ajeno sonando NO puede silenciar mi calibración
  clearTimeout(enrollUi.timer);
  const run = enrollUi.run;
  enrollUi.timer = setTimeout(() => {
    if (!enrollUi.active || run !== enrollRun) return;
    enrollUi.capturing = false;
    refreshDuckBypass();
    net.sendJSON({ type: 'enroll', action: 'cancel' });
    toast(t('enroll_retry'), 5000);
    enrollUi.step = 1;             // directiva: al fallar el timeout, reinicia el PROCESO
    renderEnrollStep();
  }, ENROLL_TIMEOUT_MS);
}

function handleEnrollResult(msg) {
  // step y run deben coincidir con el intento vigente: un success TARDÍO (encolado
  // justo antes del timeout) no debe avanzar el proceso ya reiniciado
  if (!enrollUi.active || msg.status !== 'success') return;
  if (msg.step !== enrollUi.step || (msg.run != null && msg.run !== enrollRun)) return;
  clearTimeout(enrollUi.timer);
  enrollUi.capturing = false;
  refreshDuckBypass();
  if (enrollUi.step < ENROLL_STEPS) {
    enrollUi.step += 1;
    renderEnrollStep();            // el usuario pulsa el botón para la siguiente frase
  } else {
    cancelEnrollment();
    state.enrolled = true;
    net.sendJSON({ type: 'enrolled' });
    if (state.voices.length) showVoiceCard();   // "Así sonarás": confirmar antes de la mesa
    else toast(t('enroll_done'), 2500, 'info');
  }
}

function cancelEnrollment() {
  clearTimeout(enrollUi.timer);
  enrollUi.active = false;
  enrollUi.capturing = false;
  refreshDuckBypass();
  $('enrollModal').classList.add('hidden');
}

/* ========================= Mensajes del servidor ========================= */

function handleMessage(msg) {
  switch (msg.type) {
    case 'joined':
      state.speakerId = msg.speaker_id;
      state.joined = true;
      state.room = new Map(msg.room.map((m) => {
        if (m.x !== undefined) seats.positions.set(m.speaker_id, { x: m.x, y: m.y, angle: m.angle });
        return [m.speaker_id, m];
      }));
      // Sincronizar el turno actual: quien entra a MITAD de un turno debe saberlo —
      // si no, hablaba, el servidor descartaba su audio en silencio y su ring buffer
      // (que solo se llena cuando SABE que está bloqueado) no retenía nada: frase perdida
      net.floorOwner = msg.floor_owner || null;
      refreshDuckBypass();
      for (const m of msg.room) player?.restoreSpeaker(m.speaker_id);  // roster autoritativo
      // Poda: quien se fue MIENTRAS estábamos desconectados nunca nos envió su
      // peer_left — sin esto su asiento y su panner quedaban huérfanos para siempre
      for (const id of [...seats.positions.keys()])
        if (!state.room.has(id)) seats.positions.delete(id);
      player?.pruneSpeakers(new Set(state.room.keys()));
      // El roster manda: cancelar TODO grace timer pendiente — uno armado antes de
      // NUESTRO corte dispararía tras reconectar y borraría (y enmudecería vía
      // _dropped) a un peer que el roster acaba de confirmar como PRESENTE
      if (state.gracePeriods) {
        for (const timerId of state.gracePeriods.values()) clearTimeout(timerId);
        state.gracePeriods.clear();
      }
      // Moves retenidos durante el corte (solo el asiento PROPIO): re-aplicar EN
      // LOCAL (el roster acaba de pisar la posición con la del servidor, más vieja)
      // y re-emitir con sendMove — si el WS vuelve a caerse en este instante,
      // sendJSON lo perdería en silencio; sendMove lo re-retiene solo
      {
        const pend = [...net.pendingMoves.values()];
        net.pendingMoves.clear();
        for (const p of pend) {
          if (!state.room.has(p.speaker_id)) continue;
          seats.positions.set(p.speaker_id, { x: p.x, y: p.y, angle: p.angle });
          positionPanner(p.speaker_id, p);
          net.sendMove(p);
        }
      }
      net.markJoined();
      ui.renderRoom();
      ui.updateFloorUI();                       // chip del turno + strip contextual al día
      updateListener();                         // el oyente 3D nace en su asiento
      if (!state.enrolled) startEnrollment();   // reconexión a mitad de enroll: reinicia
      break;
    case 'peer_joined':
      player?.restoreSpeaker(msg.speaker_id);
      if (state.gracePeriods && state.gracePeriods.has(msg.speaker_id)) {
        clearTimeout(state.gracePeriods.get(msg.speaker_id));
        state.gracePeriods.delete(msg.speaker_id);
        state.room.set(msg.speaker_id, msg);
        // El servidor es autoritativo al reunirse: si alguien arrastró su ficha
        // mientras él estaba en el limbo (move sobre ausente = descartado), la
        // posición del footprint que trae el peer_joined re-sincroniza a todos
        if (msg.x !== undefined) {
          seats.positions.set(msg.speaker_id, { x: msg.x, y: msg.y, angle: msg.angle });
          positionPanner(msg.speaker_id, msg);
        }
        ui.renderRoom();
        break; // Cancelado el "dejó la mesa", reconexión invisible
      }
      state.room.set(msg.speaker_id, msg);
      if (msg.x !== undefined) seats.positions.set(msg.speaker_id, { x: msg.x, y: msg.y, angle: msg.angle });
      ui.renderRoom();
      ui.sysline(t('sys_joined', { name: msg.name, flag: FLAGS[msg.language] || '' }));
      break;
    case 'peer_moved':
      // Mi dedo manda sobre la ficha que ESTOY arrastrando: aplicar un peer_moved
      // concurrente aquí lucharía contra el drag; mi pointerup enviará el valor
      // final y el eco del servidor (peer_moved a TODOS) re-converge la mesa
      if (seats._draggingId === msg.speaker_id) break;
      if (state.room.has(msg.speaker_id)) {
        seats.positions.set(msg.speaker_id, { x: msg.x, y: msg.y, angle: msg.angle });
        positionPanner(msg.speaker_id, msg);   // suavizado; omnidireccional (sin conos)
        if (msg.speaker_id === state.speakerId) updateListener();  // me RECOLOCARON a mí
        seats.render();
      }
      break;
    case 'peer_left':
      // Grace period para evitar spam visual en micro-cortes
      if (!state.gracePeriods) state.gracePeriods = new Map();
      if (state.gracePeriods.has(msg.speaker_id)) clearTimeout(state.gracePeriods.get(msg.speaker_id));
      
      const timer = setTimeout(() => {
        state.gracePeriods.delete(msg.speaker_id);
        state.room.delete(msg.speaker_id);
        seats.positions.delete(msg.speaker_id);
        player?.dropSpeaker(msg.speaker_id);
        ui._dropLive(msg.speaker_id);
        ui.latestSeg.delete(msg.speaker_id);
        ui.renderRoom();
        ui.sysline(t('sys_left', { name: msg.name }));
      }, 10000);
      
      state.gracePeriods.set(msg.speaker_id, timer);
      break;
    case 'partial': {
      const seg = msg.segment_id ?? 0;
      if (seg < (ui.latestSeg.get(msg.speaker_id) || 0)) break;  // parcial rancio
      ui.latestSeg.set(msg.speaker_id, seg);
      if (msg.speaker_id === state.speakerId) ui.setMyText(msg.text, { partial: true });
      else ui.showLivePartial(msg);
      break;
    }
    case 'subtitle': {
      // Un final puede llegar DESPUÉS de los parciales de la locución siguiente
      // (su inferencia tarda segundos): si es viejo, no pisa lo que hay en vivo
      const seg = msg.segment_id ?? Infinity;
      const fresh = seg >= (ui.latestSeg.get(msg.speaker_id) || 0);
      if (fresh && msg.segment_id != null) ui.latestSeg.set(msg.speaker_id, msg.segment_id);
      ui.addSubtitle(msg, { fresh });
      if (fresh && msg.speaker_id === state.speakerId) ui.setMyText(msg.text);
      break;
    }
    case 'translation':
      if (msg.lang === state.lang) ui.applyTranslation(msg);
      break;
    case 'enroll_result':
      handleEnrollResult(msg);
      break;
    case 'floor_acquired':
      net.floorOwner = msg.speaker_id;
      ui.updateFloorUI();
      refreshDuckBypass();           // si el turno es MÍO, el ducking no puede callarme
      break;
    case 'floor_released':
      net.floorOwner = null;
      ui.updateFloorUI();
      refreshDuckBypass();
      break;
    case 'voice_assigned':
      renderVoiceCard(msg.voices || {});
      break;
    case 'error':
      net.flagFatal(translateServerMessage(msg.message));
      toast(translateServerMessage(msg.message));
      break;
    default:
      console.warn('Mensaje desconocido del servidor', msg);
  }
}

/* ============ F9.1: tarjeta "Así sonarás" (voz por idioma DESTINO) =========
 * Lógica del arquitecto: la voz de tu propio idioma no la oye nadie (los de tu
 * idioma oyen tu voz real). Lo que importa es cómo suenas TRADUCIDO: tras la
 * calibración (que mide tu f0), el servidor asigna una voz afín por cada idioma
 * destino y aquí se escuchan las muestras y se cambian antes de entrar. */

async function loadVoiceCatalog() {
  try {
    const res = await fetch('/api/voices');
    if (!res.ok) throw new Error(String(res.status));
    const data = await res.json();
    state.voices = data.voices || [];
  } catch {
    state.voices = [];   // servidor sin catálogo (backend f5tts) o inaccesible
  }
}

let previewAudio = null;

function playPreviewOf(voiceId) {
  previewAudio?.pause();
  previewAudio = new Audio(`/api/voices/${encodeURIComponent(voiceId)}/preview.wav`);
  previewAudio.play().catch(() => toast(t('status_reconnecting')));
}

function _storedVoiceFor(lang) {
  try { return sessionStorage.getItem(`tradion_voice_${lang}`); } catch { return null; }
}

function showVoiceCard() {
  $('voiceList').innerHTML = '';
  $('voiceModal').classList.remove('hidden');   // se rellena al llegar voice_assigned
}

function renderVoiceCard(assigned) {
  const list = $('voiceList');
  list.innerHTML = '';
  for (const [lang, info] of Object.entries(assigned)) {
    const options = state.voices.filter((v) => v.lang === lang);
    const row = document.createElement('div');
    row.className = 'voice-row';
    const flag = document.createElement('span');
    flag.className = 'voice-flag';
    flag.textContent = FLAGS[lang] || lang;
    const select = document.createElement('select');
    for (const voice of options) {
      const option = document.createElement('option');
      option.value = voice.id;
      option.textContent = `${voice.gender === 'F' ? '♀' : '♂'} ${voice.label} · ${voice.tone}`;
      select.appendChild(option);
    }
    // Override recordado de la sesión > asignación automática del matcher
    const stored = _storedVoiceFor(lang);
    const initial = stored && options.some((v) => v.id === stored) ? stored : info.voice_id;
    select.value = initial;
    if (initial !== info.voice_id) {
      net.sendJSON({ type: 'set_voice', lang, voice_id: initial });
    }
    select.addEventListener('change', () => {
      net.sendJSON({ type: 'set_voice', lang, voice_id: select.value });
      try { sessionStorage.setItem(`tradion_voice_${lang}`, select.value); } catch { /* privado */ }
      playPreviewOf(select.value);   // feedback inmediato al cambiar
    });
    const play = document.createElement('button');
    play.type = 'button';
    play.className = 'voice-play';
    play.textContent = '🔊';
    play.title = t('voice_preview');
    play.addEventListener('click', () => playPreviewOf(select.value));
    row.append(flag, select, play);
    list.appendChild(row);
  }
}

/* ============================== Wake Lock =============================== */

let wakeLock = null;

async function requestWakeLock() {
  try {
    wakeLock = await navigator.wakeLock?.request('screen');
  } catch (err) {
    console.warn('Wake Lock no disponible:', err.message);
  }
}

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && state.seated) {
    requestWakeLock();
    state.audioCtx?.resume();
    output?.audioEl?.play().catch(() => {});
    const track = state.micNodes?.stream.getAudioTracks()[0];
    if (track && track.readyState === 'ended') {
      reacquireMic().catch((err) => toast(t('err_mic_fail', { msg: err.message })));
    }
  }
});

/* ==================== Gesto de asiento / salida ========================= */

const net = new WSClient({
  onJSON: handleMessage,
  onBinary: handleBinary,
  onState: (s) => ui.setStatus(s),
  onFatal: (message) => backToLobby(message),
});
let player = null;
let output = null;

async function sit() {
  const name = $('nameInput').value.trim();
  if (!name) {
    toast(t('err_name'));
    $('nameInput').focus();
    return;
  }
  state.name = name;

  const btn = $('sitBtn');
  btn.disabled = true;
  btn.dataset.i18n = 'sitting';   // updateUI() re-traduce el estado REAL del botón
  btn.textContent = t('sitting');
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    state.audioCtx = new Ctx();
    const listener = state.audioCtx.listener;
    if (listener.positionX) {
      listener.positionX.value = 0;
      listener.positionY.value = 0;
      listener.positionZ.value = 0;
      listener.forwardX.value = 0;
      listener.forwardY.value = 0;
      listener.forwardZ.value = -1;
      listener.upX.value = 0;
      listener.upY.value = 1;
      listener.upZ.value = 0;
    } else {
      listener.setPosition(0, 0, 0);
      listener.setOrientation(0, 0, -1, 0, 1, 0);
    }
    await state.audioCtx.resume();
    output = new EchoSafeOutput(state.audioCtx);
    await output.init();                    // el .play() del <audio> consume el gesto AQUÍ
    await startMic(state.audioCtx);
    output.upgradeLoopback();               // Android: tras el permiso, candidatos ICE reales
    await requestWakeLock();
    player = new TTSPlayer(state.audioCtx, output);

    state.seated = true;
    net.connect();
    ui.showTable();
  } catch (err) {
    console.error(err);
    toast(t('err_mic', { msg: err.message }));
    _teardownAudio();
    btn.disabled = false;
    btn.dataset.i18n = 'sit';
    btn.textContent = t('sit');
  }
}

function _teardownAudio() {
  state.micNodes?.stream?.getTracks().forEach((tk) => tk.stop());
  state.micNodes = null;
  output?.close();
  output = null;
  state.audioCtx?.close().catch(() => {});
  state.audioCtx = null;
  player = null;
  wakeLock?.release?.().catch(() => {});
  wakeLock = null;
}

/** Teardown determinista completo: red, audio, DOM y estado (directiva 5 de F5.1). */
function backToLobby(message) {
  cancelEnrollment();
  enrollRun++;
  net.sendJSON({ type: 'leave' });
  net.close();
  state.seated = false;
  state.joined = false;
  state.enrolled = false;
  state.speakerId = null;
  state.muted = true;
  state.room.clear();
  seats.clear();
  micBuffer = [];      // onset retenido de ESTA sesión: no debe volcarse en la siguiente
  net.pendingMoves.clear();   // moves retenidos de ESTA mesa: no viajan a la próxima
  _teardownAudio();
  ui.resetDom();
  const muteBtn = $('muteBtn');
  muteBtn.classList.add('muted');
  muteBtn.textContent = '🎙️';   // el tachado lo dibuja la clase .muted
  $('muteTip').classList.add('hidden');
  ui.setStatus('off');
  ui.showLobby();
  if (message) toast(message, 6000);
}

/* ============================ Enlaces de UI ============================= */

$('langPicker').addEventListener('click', (ev) => {
  const btn = ev.target.closest('.lang');
  if (!btn) return;
  state.lang = btn.dataset.lang;
  for (const b of $('langPicker').children) {
    b.classList.toggle('active', b === btn);
    b.setAttribute('aria-pressed', String(b === btn));
  }
  updateUI();   // i18n instantáneo de TODO el DOM, sin recargar
});

$('voiceEnter').addEventListener('click', () => {
  previewAudio?.pause();
  $('voiceModal').classList.add('hidden');
  // Start Muted: al entrar a la mesa, mic muteado con tooltip tipo Zoom
  state.muted = true;
  const muteBtn = $('muteBtn');
  muteBtn.classList.add('muted');
  muteBtn.setAttribute('aria-pressed', 'true');
  muteBtn.textContent = '🎙️';   // el tachado lo dibuja la clase .muted
  if (state.micNodes?.stream) {
    state.micNodes.stream.getAudioTracks().forEach((track) => { track.enabled = false; });
  }
  $('muteTip').classList.remove('hidden');
});

$('sitBtn').addEventListener('click', sit);
$('leaveBtn').addEventListener('click', () => backToLobby());
$('enrollStart').addEventListener('click', beginEnrollCapture);

$('planBtn').addEventListener('click', () => {
  seats.render();
  $('planModal').classList.remove('hidden');
});
$('planClose').addEventListener('click', () => $('planModal').classList.add('hidden'));
// Salida SIEMPRE disponible de la calibración (antes solo se escapaba por el
// bug de z-index que dejaba la cabecera clicable sobre el modal)
$('enrollExit').addEventListener('click', () => backToLobby());
// Diagnóstico de audio 3D (sampleRate/canales/prueba de oído) en pestaña aparte
$('diagBtn').addEventListener('click', () => window.open('/static/diag.html', '_blank'));

$('muteBtn').addEventListener('click', () => {
  state.muted = !state.muted;
  const btn = $('muteBtn');
  btn.classList.toggle('muted', state.muted);
  btn.setAttribute('aria-pressed', String(state.muted));
  ui.refreshMeStatus();   // el strip explica el estado: silenciado / turno / escuchando
  if (state.micNodes?.stream) {
    state.micNodes.stream.getAudioTracks().forEach((track) => {
      track.enabled = !state.muted;
    });
  }
  if (state.muted) net.sendJSON({ type: 'flush' });
  // Ocultar tooltip al primer unmute
  if (!state.muted) $('muteTip').classList.add('hidden');
});

$('nameInput').addEventListener('keydown', (ev) => {
  if (ev.key === 'Enter') { ev.preventDefault(); ev.target.blur(); }
});
updateUI();
ui.setStatus('off');
ui._bindScrollEvent();
loadVoiceCatalog();

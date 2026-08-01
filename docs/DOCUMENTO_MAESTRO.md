---
tags:
  - tradion
  - traduccion-simultanea
  - whisper
  - nllb
  - f5-tts
  - webaudio
  - websocket
  - proyecto-activo
date: 2026-07-26
updated: 2026-07-27
aliases:
  - TradIon
  - Documento Maestro
  - Traductor Mesh 3D
---

# TradIon — Documento Maestro

> [!info] Nota raíz del grafo
> Única fuente de verdad del proyecto. Toda sesión de trabajo (humana o IA) debe:
> **leer esta nota antes de tocar código** y **añadir una entrada al [[#📔 Diario]] al terminar**.
> El protocolo WebSocket canónico vive en el docstring de `backend/main_server.py`.

**TradIon**: sistema 100 % local y de coste cero para traducción de voz simultánea
**Español ↔ Coreano ↔ Inglés** con clonación de voz Zero-Shot ([[Fase 8 — Clonación F5-TTS]]),
subtítulos parciales en tiempo real, ducking anti-eco y audio espacial 3D
([[Fase 6 — Audio Espacial]]). Servidor: **MacBook Pro M3, 16 GB** (sin CUDA; MPS para F5-TTS).
Clientes: móviles vía PWA en la LAN.

## 🗺️ Mapa del grafo

- [[#⚙️ Arquitectura]] · [[#Cadena WebAudio del cliente]] · [[#Pipeline del servidor]]
- [[#🧠 Decisiones de arquitectura]] · [[#Nota lingüística — SOV]]
- [[#📦 Stack de modelos]] · [[#⛔ Reglas duras]] · [[#⚠️ Riesgos]]
- [[#🔧 Notas de configuración y dispositivos]] · [[#📔 Diario]] · [[#🔗 Fuentes]]
- Fases: [[Fase 1 — Motor de escucha]] · [[Fase 2 — Traducción NLLB]] ·
  [[Fase 4 — Servidor WSS]] · [[Fase 5 — La Mesa (frontend)]] ·
  [[Fase 6 — Audio Espacial]] · [[Fase 8 — Clonación F5-TTS]]

## 📊 Estado de fases

| Fase | Componente | Estado |
|---|---|---|
| [[Fase 1 — Motor de escucha]] | `backend/core_engine.py` — VAD Silero por sesión, segmentador, STT finales (small int8) + **parciales en vivo** con modelo `tiny` dedicado | ✅ |
| [[Fase 2 — Traducción NLLB]] | `backend/translation_engine.py` — NLLB-600M CT2 int8, es/ko/en directo (~250 ms/frase medidos) | ✅ |
| [[Fase 4 — Servidor WSS]] | `backend/main_server.py` — sala, colas por cliente, enroll ASR, huella vocal, `move`/`peer_moved`, supresión cross-mic RMS, chunking predictivo del TTS | ✅ |
| [[Fase 5 — La Mesa (frontend)]] | `frontend/` — lobby i18n (es/en/ko), gesto de asiento iOS, plano radial arrastrable, parciales, mute de hardware flotante | ✅ |
| [[Fase 6 — Audio Espacial]] | PannerNode HRTF por hablante con posición+orientación (conos direccionales), sincronizado por `move` | ✅ |
| [[Fase 8 — Clonación F5-TTS]] | Backend `f5tts` de la fachada TTS — clonación Zero-Shot con la huella del enroll. Medido: ~3,1 s fijos + 0,5 s/s de audio → **modo calidad, no simultáneo** | ✅ (opcional) |
| [[Fase 9 — Librería Biométrica de Voces]] | `backend/voice_catalog.py` + backend `piper` (DEFECTO) — 11 voces es/en/ko, matcher por f0 del enroll, selector + muestra en el lobby. **Medido: 82-105 ms/frase (RTF 0,03-0,11)** | ✅ |
| Diarización por micrófono de mesa | — | 🔮 futuro |
| PWA instalable / pulido | — | 🔮 futuro |

## ⚙️ Arquitectura

### Pipeline del servidor

```
móvil ──PCM16 16kHz──▶ WSS ──▶ [supresión cross-mic RMS]
  ──▶ VoiceSession (VAD Silero propio + segmentador)
        ├─ segmento ABIERTO ─▶ Whisper tiny (parciales, executor dedicado) ─▶ {"partial"}
        └─ segmento FINAL  ─▶ Whisper small ─▶ {"subtitle"}
              └─▶ NLLB CT2 (1 traducción por idioma destino) ─▶ {"translation"}
                    └─▶ troceo por puntuación ─▶ F5-TTS Zero-Shot (huella del hablante)
                          └─▶ N frames binarios [4B BE|JSON|WAV] SOLO al idioma destino
```

Claves: la traducción **solo** ocurre sobre finales ([[#Nota lingüística — SOV]]);
los parciales usan un modelo dedicado y **se saltan bajo carga** (`heavy_pending`) para
jamás retrasar finales ni calibración; el TTS se trocea por signos de puntuación para
minimizar el time-to-first-audio; los frames de un mismo hablante se reproducen en
orden de llegada (cadena de promesas en el cliente).

### Cadena WebAudio del cliente

```
getUserMedia (AEC+NS+AGC) ─▶ AudioWorklet «tradion-capture»
   [re-muestreo lineal a 16 kHz · Int16 · bloques 2048 · DUCKING: silencia
    la captura mientras suena TTS local (contador duck por mensajes del puerto)]
   ─▶ WS binario                          MUTE hardware: track.enabled=false

TTS entrante ─▶ decodeAudioData ─▶ AudioBufferSourceNode (cola por hablante)
   ─▶ GainNode ─▶ PannerNode HRTF (posición x/z ±5 m + orientación con conos 60°/120°)
   ─▶ EchoSafeOutput:
        iOS  ▶ ctx.destination (AEC del sistema)
        Android ▶ MediaStreamDestination ▶ <audio srcObject> (+ loopback RTCPeerConnection
                  tras getUserMedia; éxito medido en connectionState==='connected')
```

El **enroll** (3 frases localizadas, botón por paso, validación Levenshtein por jamo ≥ 0.8)
acumula la **huella vocal** (`ref_audio_buffer` + `ref_text`); al `{"type":"enrolled"}` se
vuelca a un WAV temporal para F5-TTS y solo entonces el hablante aparece en la sala.
La huella **se purga (RAM + disco) en `leave()`**.

### Protocolo WebSocket

Resumen (canónico en el docstring de `main_server.py`):
**Subida**: `join{name,language,client_token}` · binario PCM16 · `flush` ·
`enroll{action,step,expected,run}` · `enrolled` · `move{x,y,angle}` · `leave`.
**Bajada**: `joined{room[x,y,angle]}` · `peer_joined/left/moved` · `partial{segment_id}` ·
`subtitle{segment_id}` · `translation` · `enroll_result{status,step,run,heard}` ·
`error` · binario TTS `[4B BE|JSON header|WAV]`.

## 🧠 Decisiones de arquitectura

1. **Topología estrella** (el Mac procesa todo); "mesh" es solo marca.
2. **Un teléfono = un hablante**: identidad topológica; sin diarización.
3. **WebSocket, no WebRTC**: PCM crudo directo al VAD; el TTS viaja como DATOS
   (bug iOS: PannerNode no espacializa MediaStreams remotos).
4. **HTTPS obligatorio en LAN** (mkcert + CA en cada móvil): `getUserMedia` lo exige.
5. **El servidor no mezcla audio**: cada cliente espacializa con sus PannerNodes.
6. **Anti-eco por capas**: AEC nativo + ruta de salida `EchoSafeOutput` + **ducking**
   en el worklet (silencia el mic mientras suena TTS local) + supresión cross-mic RMS
   en el servidor. Auriculares recomendados, ya no imprescindibles.
7. **Dúplex completo**: el micrófono nunca se auto-mutea; el mute es manual (corta
   `track.enabled`) y la única compuerta automática es la privacidad del enroll.
8. **Estado por cliente**: sesión VAD propia, idioma declarado, huella vocal propia.
9. **Latencia objetivo ≤ 4 s por turno** (p95) — NLLB medido ~250 ms; F5-TTS chunked.
10. **Sin selección de voz**: la clonación Zero-Shot hace que cada uno hable con SU voz.

### Nota lingüística — SOV

El coreano es SOV con verbo/negación/tiempo al final («나는 사과를 먹지 **않았다**»):
traducir prefijos produce falsedades que habría que retractar. Por eso los `partial`
son SOLO subtítulos y NLLB traduce únicamente segmentos cerrados. El reordenamiento
SVO↔SOV lo aprende el transformer (atención cruzada), no reglas manuales.

## 📦 Stack de modelos

| Etapa | Modelo | Licencia | Notas |
|---|---|---|---|
| VAD | Silero VAD (pip) | MIT | instancia por sesión, ventanas de 512 @16 kHz |
| STT finales | faster-whisper `small` int8 | MIT | CPU (CT2 no usa Metal) |
| STT parciales | faster-whisper `tiny` int8 | MIT | executor dedicado, temperature=0, filtro avg_logprob |
| MT | NLLB-200-distilled-600M vía CT2 int8 | **CC-BY-NC-4.0** | directo es/ko/en, sin pivote |
| TTS (defecto) | **Piper** — catálogo de 11 voces (es×6, en×4, ko×1) | MIT (motor); licencia por voz en piper-voices | ONNX CPU, RTF 0,03-0,11; matcher f0; la identidad tonal viaja entre idiomas |
| TTS (modo calidad) | **F5-TTS** (Flow Matching, Zero-Shot) | MIT (código; pesos según checkpoint) | `tts.backend: f5tts`; MPS fp16; huella del enroll; ~4-7 s/frase |
| (retirado) | MeloTTS | MIT | F3; candidato futuro a entrada del catálogo (voz KO extra) |

> [!warning] Licencias
> NLLB es **no comercial** (y los pesos F5-TTS según checkpoint). Válido para proyecto
> personal; para productizar: revisar checkpoint F5 y sustituir NLLB (pivote OPUS-MT).

## ⛔ Reglas duras

> [!danger] Jamás parchear `site-packages` a mano
> Un `pip install` posterior borra los parches y deja paquetes incoherentes (ocurrió el
> 2026-07-26 con MeloTTS). Ajustes de dependencias → script versionado en `scripts/`.

> [!danger] `python-mecab-ko` solo con `scripts/setup_mecab_ko.sh`
> En macOS (FS insensible a mayúsculas) su paquete `mecab/` y el `MeCab/` de
> mecab-python3 son LA MISMA carpeta y se destrozan mutuamente. El script lo instala
> aislado en `.venv/mecab-ko` + `.pth`. (Aplica solo si se revierte a MeloTTS.)

> [!warning] Otras reglas
> - Pesos SIEMPRE en `models/` (`HF_HUB_CACHE` se fija en `backend/__init__.py` ANTES
>   de cualquier import de huggingface_hub — la librería congela la variable al importarse).
> - Configuración SOLO en `config/settings.yaml`; nada hardcodeado.
> - Cada fase con su prueba antes de abrir la siguiente; verificación adversarial
>   (multi-agente) antes de entregar código nuevo.
> - transformers==4.27.4 (pin del retirado MeloTTS): `translation_engine.py` es
>   compatible con 4.27+ (no usa `lang_code_to_id`).

## ⚠️ Riesgos

1. **F5-TTS en MPS**: inferencia serializada por lock; con 3+ oyentes simultáneos medir
   el time-to-first-audio real (el chunking mitiga). fp16 en MPS: vigilar NaNs.
2. **Calidad de huella**: 3 frases ≈ 10-15 s de referencia; acentos/ruido degradan el clon.
3. **Realimentación acústica sin auriculares**: mitigada por ducking + cross-mic + AEC,
   no eliminada — validar en mesa real.
4. **Licencias no comerciales** (NLLB, checkpoint F5): ver [[#📦 Stack de modelos]].
5. **RAM**: Whisper small+tiny + NLLB + F5-TTS + servidor ≈ presupuesto ajustado en 16 GB
   — medir con Activity Monitor con la sala llena.

## 🔧 Notas de configuración y dispositivos

- **Servidor**: MacBook Pro M3, 16 GB, macOS (Darwin 25.5). Sin CUDA; F5-TTS usa MPS.
- **Idiomas activos**: es · ko · en (lobby en alfabeto nativo, UI i18n completa).
- **Ajustes de latencia del segmentador** (afinados a mano): `vad_threshold 0.72`,
  `min_speech_ms 300`, `segment_close_silence_ms 400`.
- **Certificados**: mkcert en `config/certs/` (fuera de git); regenerar si cambia la
  IP/hostname; instalar CA en móviles nuevos (iOS: perfil + confianza total).
- **NLTK** (herencia MeloTTS): datos en `.venv/nltk_data`. **unidic** 785 MB en
  site-packages (retirable si MeloTTS no vuelve).
- El primer arranque descarga pesos a `models/` — sin red falla con error claro.

## 📔 Diario

- **2026-07-26** — Auditoría inicial multi-agente (8 agentes): 16 hallazgos sobre el
  `core_engine.py` original (VAD roto por ventanas de 512, motor síncrono, estado
  compartido…). Estructura del proyecto creada; decisiones §arquitectura cerradas;
  stack elegido con fuentes verificadas. `translation_engine.py` no existía.
- **2026-07-26 (2)** — F1+F2 entregadas (reescritura core + NLLB CT2 int8). 12 hallazgos
  corregidos en verificación adversarial. Benchmark usuario: NLLB ~250 ms/frase.
- **2026-07-26 (3)** — F3 (MeloTTS) + F4 (servidor WSS). 12 hallazgos corregidos
  (HF_HUB_CACHE tardío, segmentos desordenados, shutdown colgado, clientes lentos…).
- **2026-07-26 (4)** — Incidente MeCab resuelto (FS case-insensitive): regla dura +
  `scripts/setup_mecab_ko.sh`. TTS es/ko/en <1 s con warmup.
- **2026-07-26 (5)** — F5 frontend "La Mesa". 11 hallazgos (worklet RangeError crítico
  verificado por simulación, bucle de reconexión, client_token anti-fantasmas…).
- **2026-07-26 (6)** — F5.1: dúplex completo, EchoSafeOutput (iOS directo / Android
  loopback), plano radial arrastrable, enroll v1, teardown determinista. 7 hallazgos.
- **2026-07-27** — F5.2: parciales en vivo (modelo dedicado), enroll ASR multi-paso
  (Levenshtein por jamo), i18n estructural es/en/ko. 14 hallazgos corregidos.
- **2026-07-27 (2) — Integración del usuario + pasada quirúrgica (este doc).**
  El usuario integró: **F5-TTS Zero-Shot** (tts_engine reescrito: MPS fp16, nfe_step=8,
  huella del enroll volcada a WAV), **ducking** en el worklet (silencia mic durante TTS
  local), **audio 3D completo** (PannerNode con orientación/conos + `move`/`peer_moved`),
  supresión cross-mic RMS, chunking predictivo del TTS y ajustes de segmentador.
  Pasada quirúrgica sobre ello: eliminado el selector de voz (obsoleto con Zero-Shot:
  i18n `voice_*`, `state.voice`, campo `voice` del servidor), `import re` a cabecera,
  **purga de la huella vocal (RAM+WAV) en `leave()`**, mute de hardware **flotante y
  masivo** (ya cortaba `track.enabled`), settings `tts: f5tts`, protocolo documentado
  (enrolled/move/peer_moved) y este documento migrado a formato Obsidian.
  Verificado: `import os` ya estaba, `/api/preview` no existe, sticky header ya
  aplicado (sin `overflow` en ancestros), partials/enroll/i18n intactos.

- **2026-07-27 (3) — Primera sesión REAL multi-dispositivo (Mac + iPhone vía túnel
  Cloudflare).** Funciona extremo a extremo: enroll ASR validando en vivo (rechaza
  intentos parciales, acepta con 0.81-1.00; la tolerancia por jamo absorbió errores
  de Whisper como «me apete un café»), huella volcada, **clonación F5-TTS de la voz
  real ES↔EN**. Cuello de botella MEDIDO: F5-TTS en MPS paga **~3,1 s de coste fijo
  por llamada + ~0,5 s por segundo de audio**; el chunking por comas hacía RTF 3-4×
  (latencias de 6-15 s y cola creciente). Arreglo: `_chunk_for_tts` corta solo en
  finales de oración y fusiona hasta `tts.chunk_min_chars` (60) → RTF ~0,8×
  (sostenible), con dedup de oraciones adyacentes (NLLB duplicó «- Gracias.» y se
  sintetizó dos veces). `nfe_step` expuesto en YAML. Pendiente de observar: STT/MT
  se ralentizan bajo carga MPS (1,4-3,9 s) — debería aliviarse al reducir 3-4× las
  llamadas TTS. Nota: el túnel trycloudflare expone la mesa a Internet (cualquiera
  con la URL entra y deja huella de voz): usarlo solo para pruebas puntuales.

- **2026-07-27 (4) — DSP anti-clipping y aclaración de asincronismo.**
  (1) **Petardeo resuelto**: F5-TTS emite float32 que puede superar [-1, 1]; la
  conversión a PCM16 saturaba en escalón. `tts_engine.py` ahora normaliza por pico
  SOLO si satura (×0,95/pico, conserva el volumen normal) y clampea tras el
  remuestreo a 44,1 kHz (el filtro sinc re-crea overshoot). Verificado con señal
  sintética saturante (pico 1,8 → 0,95). (2) **Asincronismo verificado, no
  reescrito**: `tts.synthesize()` YA corría en ThreadPoolExecutor vía
  `run_in_executor` (el event loop no se bloquea); la "sordera" bajo carga es
  contención CPU/GIL entre Whisper/NLLB (CPU) y el overhead Python de F5 —
  mitigada con `tts.workers: 1` (el lock MPS ya serializaba; el 2º hilo solo
  añadía churn de GIL) y con el chunking largo de la entrada anterior.
  Directivas 3-5 (enroll interactivo, mute hardware flotante, i18n) verificadas
  intactas con evidencia en código; sin cambios.

- **2026-07-27 (5) — [[Fase 9 — Librería Biométrica de Voces]]: pivote de TTS.**
  Decisión del arquitecto validada con datos: la difusión local (F5-TTS) no puede
  ser simultánea en hardware personal (~4-7 s/frase medidos); se pivota a
  **catálogo Piper + matcher por f0** manteniendo F5 como modo calidad opcional
  (`tts.backend`). MEDIDO en el M3: **82-105 ms por frase** (RTF 0,03-0,11), 40-70×
  más rápido. Implementado: `voice_catalog.py` (11 voces es/en/ko con f0 de
  referencia MEDIDO sobre sus previews — el f0 detectó y corrigió un swap de
  speakers en la voz dual sharvard), estimador de f0 por autocorrelación (sin
  dependencias nuevas), matcher en escala log que **traslada la identidad tonal a
  cada idioma destino**, fachada TTS de dos backends, endpoints `/api/voices` y
  `/api/voices/{id}/preview.wav`, selector + botón de muestra en el lobby
  (i18n ×3), `voice_assigned` tras el enroll. Limitación honesta: piper-voices
  solo publica UNA voz coreana (kss, F, 274 Hz) — un hombre coreano recibirá voz
  femenina; pitch-shift descartado con datos (1,9 s por 5 s de audio). El enroll
  se conserva íntegro: valida por ASR, mide el f0 y sigue capturando la huella
  para el modo f5tts. `chunk_min_chars: 1` con piper (coste fijo ~50 ms).

- **2026-07-27 (6) — Autopsia de la 2ª sesión real + rediseño de la lógica de voces.**
  Diagnóstico del log: (1) **el ducking silenciaba a cero el micrófono mientras
  sonaba CUALQUIER TTS local** — mató la calibración de Rodrigo (similitud 0.00 «''»
  exactamente durante los TTS de Amy) y troceaba el habla en plena frase (medio-dúplex
  de facto). Arreglo: el ducking ATENÚA (×0,12) en vez de silenciar, y se bypasea
  durante la captura del enroll. (2) **El filtro cross-mic RMS usaba last_rms sin
  timestamp**: un peer que dejaba de emitir (enroll/mute) dejaba su RMS alto congelado
  y silenciaba al otro dispositivo indefinidamente («deja de pillar el audio de uno y
  toma el del otro»). Arreglo: solo se compara contra RMS <0,5 s de antigüedad
  (+umbral 2,5×/0,04). (3) **La validación del enroll re-transcribía el buffer ENTERO
  en cada pasada (O(n²))** y saturaba el executor: STT de la conversación a 7-9 s.
  Arreglo: ventana de 6 s, mínimo 1 s de audio nuevo, espera bajo carga. (4) **El
  listener 3D nunca se posicionaba**: quedaba en el origen mirando a -Z, por eso el
  3D «no funcionaba». Arreglo: `updateListener()` sitúa al oyente en SU asiento con
  su orientación (API moderna + fallback Safari). (5) La ruta `<audio srcObject>` se
  aplicaba también en Safari de macOS: ahora SOLO Android; el resto va directo.
  **Rediseño de voces (lógica del arquitecto)**: la voz del propio idioma no la oye
  nadie → eliminado el selector del lobby; tras calibrar aparece la tarjeta
  **«Así sonarás»** con la voz asignada por f0 PARA CADA IDIOMA DESTINO, muestra ▶️ y
  cambio manual (`set_voice`, overrides recordados por sesión). Mesa **hexagonal**
  (clip-path). Pendiente conocido: doble captura físico en la misma sala (dos mics
  oyen a ambos → duplicados) — mitigado, no eliminado: auriculares o salas separadas.

  **Actualización de Estabilidad en Sesiones Compartidas**:
  - **Anclaje de Idioma STT:** Se eliminó el auto-detector que anulaba el comportamiento si el audio se parecía a otro idioma, forzando estrictamente el `language` de WebSocket en Whisper.
  - **Supresión Eco/Cross-Mic Reforzada:** El umbral RMS/VAD para descartar capturas locales concurrentes con la voz del interlocutor se extendió a 0.8s y se hizo más estricto contra el audio entrante (se evalúa si RMS peer > RMS local × 1.5).
  - **Optimización Whisper:** Aumento de `small` a `medium` en `settings.yaml` dado el excedente de recursos libres tras el despliegue del TTS Piper.

- **2026-07-28 — Resolución completa de la auditoría (plan Fable 5 corregido).**
  Aplicado TODO: **A1** el LID anti-eco solo descarta si detecta OTRO idioma DE LA
  SALA (gallego/catalán ya no matan al castellano) y loguea cada descarte; **A2**
  la calibración fuerza el idioma (`force_language=True`, sin LID); **A3** eliminado
  el half-duplex espejo del servidor (`tts_playing_until`): la única fuente de verdad
  es el ducking sample-accurate del cliente (+ cross-mic RMS con timestamp); **A4**
  `vad_filter=False` (el Silero de sesión ya recorta) + telemetría de descartes por
  causa (no_speech/logprob/blacklist); **A5** double-checked lock en la carga perezosa
  de voces Piper y `setdefault` atómico en `voice_by_lang`; **B1** huellas vocales en
  `models/tmp/` con barrido al arrancar (cero WAVs biométricos huérfanos tras crash);
  **B2** expiración del enroll por temporizador real (`call_later`), no solo al llegar
  audio; **C4** métricas honestas (total = stt+mt reales, sin ceros); **C5** código
  muerto fuera (`voice_pref`, rama enroll inalcanzable, hint "Fase 6"); **C6**
  blacklist anti-alucinación a YAML. Extras no incluidos en el plan original:
  `translation.device` des-hardcodeado (la plantilla Windows con `cuda` ahora surte
  efecto) y README reparado — certificados mkcert CON la IP LAN (antes el "Modo
  Offline" del propio README generaba certificados inválidos), paso de descarga de
  voces que faltaba (con comando Windows sin bash), promesas ajustadas a lo medido
  (1,5-4 s por frase; clonación como modo opcional) y aviso de seguridad del túnel
  público sin autenticación.

- **2026-08-01 — Auditoría experta: 3D espacial + Floor Token + despliegue RTX 3070.**
  Verificación adversarial (3 revisores × 2 pasadas): 15 hallazgos + 2 de la segunda
  pasada (validación de `move`: un cliente que manda `"Infinity"`/`"NaN"` como string
  pasaba `float()` y `json.dumps` lo re-emitía como `Infinity` a pelo → `JSON.parse`
  reventaba en TODOS los clientes y en cada `joined` futuro vía footprint — ahora solo
  coordenadas finitas acotadas; y cuarentena post-max_hold TOTAL: se silencia todo el
  audio del expulsado, no solo el que supera acquire_rms). Todos aplicados.
  **(1) Por qué el 3D "no funcionaba" — cinco causas apiladas, la matemática era correcta:**
  `positionPanner` leía `window.player`, que NO existe (un `let` top-level de script
  clásico no crea propiedad en window) → recolocar paneles era un no-op desde el día 1;
  el **servidor enviaba x=y=angle=0.0** en el roster → todos los panners y el listener
  nacían en el ORIGEN (sin geometría no hay HRTF): ahora `join()` asigna **asiento
  hexagonal inicial** (primera plaza libre, se recicla al salir; el asiento arrastrado
  sobrevive a micro-cortes vía footprint); los **conos** de los panners dejaban al
  oyente del borde FUERA del cono del vecino (fuentes ahora omnidireccionales: en una
  mesa la dirección la da la geometría oyente-fuente); el modelo de distancia default
  (inverse, rolloff 1) aplastaba la direccionalidad (ahora ref 2 / rolloff 0,35);
  y el loopback Android negociaba Opus MONO (munge `stereo=1` sobre la descripción
  REMOTA, RFC 7587: fmtp declara lo que el RECEPTOR acepta). Glide `setTargetAtTime`
  (τ=0,08 s) para posiciones; la orientación se fija directa (interpolar el forward
  a través de (0,0,0) es inválido y el giro de 180° pasaba por ahí).
  **(2) Floor Token (CSMA/CA) endurecido:** cuarentena `reacquire_cooldown_s` (2 s)
  tras expulsión por `max_hold_s` — sin ella el mismo móvil ruidoso re-tomaba el canal
  en el siguiente frame (~128 ms) y el tope anti-inanición no servía; ring buffer del
  cliente con **timestamp y poda a 700 ms** (voz retenida vieja adquiría el floor para
  alguien ya callado) y umbral 0,028 (> acquire_rms: retener lo que no puede adquirir
  solo parte frases); **bypass del ducking mientras POSEO el turno** (un TTS rezagado
  ajeno silenciaba mi mic >400 ms y me robaba el floor a mitad de frase; fuente única
  `refreshDuckBypass()`); frame binario de longitud IMPAR ya no mata la conexión
  (truncado a múltiplo de 2 antes de `frombuffer`); TTS tardío de un peer expulsado
  ya no resucita su panner fantasma (`_dropped` + `restoreSpeaker` al reunirse: el
  client_token reutiliza speaker_id); `micBuffer` purgado en `backToLobby`; código
  muerto `last_rms/last_rms_at` eliminado.
  **(3) RTX 3070 8 GB (settings.windows.yaml):** presupuesto VRAM explícito —
  whisper large-v3-turbo int8_float16 (~1,5 GB) + NLLB-600M int8 (~1,0 GB) + contexto
  CUDA (~0,6 GB) ≈ 3,1 GB con Piper en CPU (margen ~5 GB; con f5tts fp16 +~3 GB).
  `translation.compute_type: auto` pasado a CT2 (faltaba el kwarg: el modelo int8
  corría como int8_float32 en CUDA — kernels fp32, más lento y más VRAM; 'auto'
  resuelve a int8_float16); parciales a CPU **por defecto en código** (ya no heredan
  `stt.device=cuda` ni `compute_type=int8_float16`, que no existe en CPU).

- **2026-08-01 (2) — Autopsia de la 1ª sesión en la RTX 3070: el LID mataba la voz.**
  Log del usuario: `Detected language 'ja' (p=0.40)` → `LID anti-eco: descartado
  segmento` → `''` en cada frase («no funciona nada»). Tres capas: la lógica del filtro
  había DERIVADO del contrato A1 (el default era descartar cualquier idioma exótico:
  japonés ni siquiera es de la sala y descartaba igual), no había umbral de confianza
  (p=0.40 es una moneda al aire en segmentos de 1-2 s), y los segmentos «perdonados»
  se decodificaban en el idioma DETECTADO (catalán/portugués), nunca en el esperado.
  **Resolución (commit `dff20a6`, IA concurrente — mismo diagnóstico por dos vías):
  el LID queda RETIRADO; el idioma va SIEMPRE forzado al declarado (A2).** El
  cross-talk que A1 vigilaba lo arbitra hoy el floor token (audio ajeno a cero en el
  servidor). Riesgo residual documentado en el docstring: un vecino MUTEADO hablando
  junto a tu móvil puede colar su voz como basura en tu idioma (los filtros
  no_speech/logprob cazan la mayoría) — si reaparecen subtítulos basura en mesa
  física, ese es el sitio donde mirar. A1 queda formalmente superado por A2+floor.
  Nota operativa: los logs venían del servidor Windows corriendo `008f34c`; hace
  falta `git pull` + reiniciar el servidor en la Victus para recibir este arreglo.

- **2026-08-01 (3) — Autopsia de la 2ª sesión en la Victus + investigación de plataforma.**
  La sesión FUNCIONÓ de extremo a extremo: pipeline en **519-972 ms por frase** tras
  warmup (objetivo sub-segundo cumplido en la 3070). Cinco defectos reales detectados
  en el log y corregidos: (1) **cada reconexión generaba un speaker_id nuevo** → la
  "reconexión invisible" del cliente (indexada por id) nunca casaba y la mesa veía
  salir/entrar usuarios en cada micro-corte; ahora el id es ESTABLE por dispositivo
  (guardado en el footprint). (2) **La primera frase pagaba la carga perezosa del ONNX
  de Piper (4009 ms medidos)**: prewarm de las voces asignadas al terminar el enroll
  (y en set_voice/reconexión), en el executor de TTS. (3) **NLLB inventaba turnos de
  diálogo** ('What did you say?' → '- ¿Qué dijiste? - No.'): documentado en la
  literatura (HalOmi, arxiv 2305.11746: ≥3% de alucinación en todas las direcciones
  del MISMO modelo 600M; fairseq#4854: entradas cortas → continuación inventada).
  Doble defensa: decoder endurecido con el "standard setting" de NLLB
  (max_decoding_length RELATIVO 3·len+5, no_repeat_ngram_size=3, disable_unk=True) +
  post-proceso que recorta el formato de diálogo cuando la fuente no lo tenía (estilo
  LibreTranslate PR#554); casos del log verificados por simulación, diálogos reales y
  em-dashes de aposición respetados. (4) **'- Thank you.'/'Thank you.' sonaba dos
  veces**: dedup de chunks normalizado sin guion. (5) **El arranque hacía ~8
  peticiones a huggingface.co**: carga local_files_only-primero con fallback online
  (Whisper ×2 y tokenizer NLLB) → arranca sin internet en la sala.
  **Investigación de red (3 agentes, fuentes en el informe):** el corte del WS a los
  ~113 s encaja con el idle/read-timeout de 100-125 s del edge de Cloudflare (NO
  configurable fuera de Enterprise; cloudflared#1282 documenta cierres 1006 incluso
  con tráfico); la URL de trycloudflare **no es privada** (subdominio aleatorio sin
  auth; el README ya lo advertía) y añade 50-200 ms de hairpin estando en la misma
  sala → para la mesa física: **LAN directa + mkcert con la IP de la Victus + regla
  de firewall** (netsh advfirewall ... localport=8443 remoteip=localsubnet).
  **Veredicto del 3D (no es bug nuestro, es plataforma):** HRTF es binaural (spec
  W3C: "Stereo Only") → auriculares obligatorios, por altavoz es físicamente
  imposible; con Bluetooth + micro abierto iOS/Android conmutan A2DP→HFP (MONO,
  8-16 kHz; Apple: "HFP ports will be given a higher priority"); y hay bug conocido
  de iOS Safari (foros Apple 672037/696034, sin respuesta) donde getUserMedia fuerza
  TODA la página a mono. Herramienta nueva: **/static/diag.html** — mide
  sampleRate/canales antes y después de abrir el micro, detecta la firma HFP y
  permite la prueba de oído (tono L/R + órbita HRTF) para separar bug de límite de
  plataforma en cada móvil. Regla práctica para la mesa: auriculares DE CABLE.
  Pendiente F10 (backlog): PIN de sala para cualquier exposición fuera de LAN;
  en Android, valorar sacar el TTS del loopback WebRTC (Chromium colapsa el estéreo
  en APM por diseño — issue 41481053) cuando haya auriculares de cable.
  **Verificación adversarial del lote (2 lentes + refutación): 6 hallazgos, 0 falsos
  positivos, todos corregidos** — los dos ALTA nacían del id estable: (a) el
  peer_left del socket viejo se difundía hasta 15 s DESPUÉS del peer_joined del
  reconectado (leave espera drop_session+drenaje) → el grace expiraba sin cancelación
  y dropSpeaker silenciaba su TTS en toda la mesa para siempre → leave ahora hace pop
  condicional (solo si la entrada es SU instancia) y suprime peer_left/floor si el id
  ya fue re-registrado; (b) _strip_invented_dialog recortaba oraciones REALES cuando
  la fuente era multi-oración y NLLB emitía formato subtítulo → solo se recorta con
  fuente de UNA oración (multi-oración: se limpian los guiones, contenido intacto).
  Más: TOCTOU en create_session (re-check tras el await del VAD: sin él dos joins del
  mismo token podían compartir id y fugar un VAD del pool para siempre), segment_id
  MONÓTONO entre sesiones (seq_start desde footprint.last_seg+64: el contador a 1
  hacía descartar ~50 locuciones de parciales tras reconectar), prewarm en executor
  propio (compartir el único worker de TTS metía 2-4 s de head-of-line a hablantes
  activos) y _norm del dedup solo con guion+espacio ('-5 grados.' es un número).

## 🔗 Fuentes

- Referencia integral: https://github.com/QuentinFuxa/WhisperLiveKit · Topología: https://github.com/niedev/RTranslator
- STT: https://github.com/SYSTRAN/faster-whisper · VAD: https://github.com/snakers4/silero-vad
- MT: https://huggingface.co/facebook/nllb-200-distilled-600M · https://opennmt.net/CTranslate2/guides/transformers.html
- TTS: https://github.com/SWivid/F5-TTS · (retirado) https://github.com/myshell-ai/MeloTTS
- Red/HTTPS: https://github.com/FiloSottile/mkcert · https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/getUserMedia
- Audio 3D/iOS: https://developer.mozilla.org/en-US/docs/Web/API/PannerNode · https://developer.apple.com/forums/thread/696034
- AEC Chromium/WebAudio: https://bugs.chromium.org/p/chromium/issues/detail?id=687574

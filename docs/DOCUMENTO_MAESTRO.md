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

- **2026-08-02 — Auditoría del subsistema de asientos: la mesa es ahora COLABORATIVA
  y convergente.** Pregunta del arquitecto: ¿la posición de la mesa se configura, se
  propaga a todos y es coherente, sin carreras ni pérdidas? Respuesta: NO lo era —
  el defecto raíz es que el plano permitía arrastrar CUALQUIER ficha (el hint lo
  promete: «arrastra a cada persona») pero el protocolo `move` no llevaba
  destinatario: el servidor aplicaba el arrastre al REMITENTE. B veía moverse a A;
  la mesa entera veía moverse a B. Rediseño (verificado por simulación de
  convergencia con 3 clientes): `move` lleva `speaker_id` del destinatario (sin él,
  el propio: compat), el servidor lo aplica a ESE cliente + su footprint y difunde
  `peer_moved` a TODOS — el eco incluido al que arrastró: con `exclude`, dos
  arrastres concurrentes de la misma ficha dejaban al GANADOR del last-write como
  único desincronizado. El cliente ignora ecos de la ficha bajo su dedo
  (`seats._draggingId`) y, si el movido soy YO, recoloco mi listener 3D.
  Anti-pérdidas y anti-carreras adicionales (2ª pasada adversarial: 10 hallazgos de
  2 lentes, refutadores caídos por límite de sesión → verificados manualmente uno a
  uno contra el código): `sendMove` retiene el move si el WS está caído y lo
  re-emite tras el `joined` (solo el asiento PROPIO y solo si sigues sentado: el
  arrastre ajeno retenido estaría rancio y el post-lobby no debe viajar a la mesa
  siguiente); el `joined` de reconexión cancela TODOS los grace timers (uno armado
  antes de mi corte borraba y enmudecía vía `_dropped` a un peer que el roster
  confirmaba presente) y poda asientos/panners de quien se fue durante mi
  desconexión; el `peer_joined` que cancela un grace re-sincroniza la posición desde
  el footprint (un move sobre un ausente se descarta en el servidor: el rejoin
  re-converge a la mesa); docstring del protocolo actualizado (move colaborativo,
  Bajada con `peer_moved` y campos espaciales).

- **2026-08-02 (2) — Autopsia de la 3ª sesión Victus + lección de epistemología del diag.**
  La sesión (6 min, conversación real ES↔EN) confirma en producción los arreglos:
  `MT: formato de diálogo inventado recortado` cazando en vivo (×2), prewarm cargando
  la voz al terminar el enroll (primer TTS 82-189 ms, sin el pico de 4 s), latencias
  482-1131 ms, cierre limpio sin caídas. Tres defectos nuevos del log, corregidos:
  (1) **NLLB omitía oraciones enteras en segmentos multi-frase** («Oh that's a good
  choice. You don't like...» → la primera frase desaparecía; es un modelo de UNA
  oración según su model card): `translate()` trocea la fuente por oraciones y
  traduce CADA una por separado (además el recorte anti-diálogo aplica limpio en
  fuente mono-oración). (2) **El cierre de segmento a 400 ms troceaba frases con
  pausas naturales** («And why you don't eat?» + «cake in the afternoon.» como DOS
  segmentos → MT sin contexto; los fragmentos de la hablante ES: 'Tarde.', 'Del
  tiempo.'): `segment_close_silence_ms` 400→600 y `floor.release_ms` 400→600 con el
  INVARIANTE documentado release_ms ≥ segment_close (si el floor se libera con el
  segmento abierto, otro móvil roba el canal en la pausa y media frase se pone a
  cero). (3) **Falso negativo del diag.html verificado por el usuario**: con
  auriculares Bluetooth el veredicto decía «3D IMPOSIBLE (HFP mono)» y sin embargo
  la voz orbitaba con claridad — el veredicto confundía frecuencia de muestreo con
  número de canales (hay rutas BT con captura SCO y salida estéreo; la
  lateralización depende de los CANALES, no de los kHz). Rediseño: **el oído es el
  veredicto final** — botones de confirmación tras la prueba («se distinguía un
  lado» / «igual por ambos»); las métricas pasan a pistas preliminares en tono
  humilde (HFP ⇒ aviso de CALIDAD de llamada y pérdida de matices HRTF, no
  imposibilidad). Implicación práctica: el Bluetooth puede ser usable para la
  separación L/R de hablantes en según qué móviles — medir por dispositivo con el
  diag; el cable sigue dando la calidad completa. Residual conocido: 'bye' →
  '¡Ahora bien!' (NLLB con entradas de 1 palabra; los topes del decoder acotan la
  longitud, no la semántica — valorar glosario de muletillas si molesta en mesa).

- **2026-08-02 (3) — Auditoría de UI (a petición del arquitecto) + lote completo aplicado.**
  Revisión visual real a 375×812 con mesa simulada (3 comensales, turno, parciales,
  modales), verificada con capturas antes/después. Bugs corregidos: **(1) la cabecera
  sticky (z-index 999) flotaba encendida y clicable POR ENCIMA de los modales
  (z-index 30)** → modales a 1000 y toast a 1100; al taparla se perdía la única
  salida de la calibración, así que **(2) el modal de enroll gana un botón «Volver
  al lobby»** (antes, quien no pasara la calibración quedaba atrapado en el bucle de
  reintentos). **(3) El plano rotaba la ficha entera** (la 'R' boca abajo, la '지'
  tumbada, etiquetas cayendo en posiciones distintas según el ángulo) → solo gira la
  «nariz» en su contenedor `.seat-rot`; inicial y nombre siempre derechos y el
  nombre siempre debajo. Mejoras: **(4) el turno de palabra por fin es visible** —
  chip del dueño resaltado (`.chip.floor`) con auto-scroll a la vista, y el strip
  inferior convertido en **línea de estado contextual** (🔇 silenciado / ⏳ «Habla
  {nombre} — espera tu turno» / 🎤 «Tu turno» / «Te escucho…»), sin pisar
  transcripciones vivas (parcial/final caducan a estado, 7 s/6 s); antes el floor
  descartaba tu voz SIN ningún aviso y «Te escucho…» se mostraba hasta silenciado.
  **(5)** Icono del mute unificado a 🎙️ con barra CSS (el 🔇 se leía como «no oigo a
  los demás»). **(6)** Botón 🎧 en el dock (sustituye al fantasma) que abre
  /static/diag.html. **(7)** Estado vacío del feed («Habla y verás aquí la
  conversación traducida», CSS `:empty`). **(8)** Accesibilidad: `role="dialog"`
  + `aria-modal` + `aria-labelledby` en los 3 modales, `aria-label` automático en
  botones de solo-icono (vía data-i18n-title). **(9)** `toast(…, 'info')` sin alarma
  roja para mensajes informativos («Voz calibrada ✓»), y texto fallback del
  plan_hint corregido («En la su voz…»). 6 claves i18n nuevas ×3 idiomas.

- **2026-08-04 — Autopsia de la 4ª sesión: cross-captura con el hablante MUTEADO.**
  Los tres síntomas reportados («uno se queda con todo el turno», «no se escuchaba
  la voz cuando aparecía el texto», «todo muy lento») eran UNA cadena causal, no
  tres averías. (1) **El agujero que dejó la retirada del LID, demostrado en vivo**
  (el usuario CORRIGIÓ el primer diagnóstico: el Mac SÍ llevaba auriculares — no
  fue TTS al aire, fue su VOZ DIRECTA en la misma habitación): el usuario EN estaba
  **muteado sin saberlo** (sus frases: «Hello? Can you hear me? Can anybody hear
  me?»; su primera adquisición de canal llega 2,5 min tras calibrar) → su
  dispositivo no competía por el floor → el Samsung (sesión ES) capturó su inglés,
  lo adquirió, lo transcribió y lo tradujo es→en devolviéndoselo en inglés. El
  floor token NO puede arbitrar cuando el dispositivo del hablante real está mudo
  — la «imposibilidad de ecos» que justificó dff20a6 era una sobreafirmación (el
  riesgo quedó anotado entonces en el docstring; hoy se materializó). (2) Con ese
  audio ajeno/basura, los umbrales internos de faster-whisper disparaban la
  **escalera COMPLETA de temperaturas — hasta 6 decodificaciones del mismo
  segmento: STT de 16.385 y 18.225 ms medidos** — la GPU secuestrada arrastraba el
  MT a 1,5 s y el primer Piper a 2 s. (3) El TTS llegaba ~6 s tarde; mientras
  sonaba en tu móvil, el ducking (half-duplex deliberado) te silenciaba el micro →
  imposible tomar el turno: la sensación de monopolio era INANICIÓN POR LATENCIA,
  no un bug del floor (el log muestra tomas/liberaciones correctas). (4) La voz
  muda: ambos abrieron **diag.html en otra pestaña en mitad de la sesión**
  (10:54:58 y 10:55:18) — WebKit/Android suspenden el AudioContext y pausan el
  <audio> del loopback al perder el foco y NO los reanudan solos; el texto seguía
  llegando por el WS pero el audio quedaba mudo (bug conocido, WebKit 237878, ya
  citado en la investigación del 2026-08-01).
  **Arreglo principal — GUARDIA LID v2 (A1 restaurada con las salvaguardas que
  faltaban)**: transcripción con LID; descarta SOLO si detecta otro idioma DE LA
  SALA con confianza ≥ `stt.lid_crosstalk_min_prob` (0.80); cualquier detección
  exótica o dudosa ('ja' p=0.40 = acento) NO descarta ni se decodifica en el
  idioma detectado: se re-transcribe FORZANDO el idioma del usuario. Tabla de
  verdad verificada por simulación (cross-captura en→descarta; ja 0.40→forzado;
  es 0.55→forzado; dialecto→forzado; normal→sin coste extra). Además: middleware
  `no-cache` para `/` y `/static/` (los navegadores corrían UI RANCIA tras cada
  git pull — probablemente el Mac no veía el aviso de «micro silenciado» nuevo), y
  hallazgo de la verificación adversarial corregido: **'어' y '음' FUERA de la
  blacklist de interjecciones** ('어' es el «sí» informal coreano — descartarlo
  silenciaba respuestas reales de una palabra).
  **Arreglos:** escalera de temperaturas ACOTADA a [0.0, 0.4] en los finales (peor
  caso ~2 decodificaciones; la basura la descartan igual los filtros);
  **reviveAudio()** — reanimación del AudioContext y del <audio> del loopback en
  visibilitychange/focus/pointerdown/onstatechange (verificado en navegador:
  suspended→running; tolera contexto nulo/cerrado); **MT por lotes** — todas las
  oraciones del segmento en UNA llamada a translate_batch (tope de longitud por la
  oración más larga del lote; el post-proceso anti-diálogo cubre el margen); y
  blacklist de interjecciones (hmm/um/음/어... — 'Hmm' → '¿Qué es eso?' medido).
  Regla operativa PARA LA MESA: auriculares en TODOS los dispositivos, también el
  ordenador; y sigue pendiente en la Victus migrar del túnel a LAN + firewall.

- **2026-08-04 (2) — Evaluación «Krisp» (petición del arquitecto): veredicto y diseño F11.**
  Investigación con fuentes (2 agentes). **Krisp: descartado** — el SDK (el que usa
  Discord, confirmado, incluida su build WASM para navegador) es enterprise con
  acceso solo por ventas, sin precio público ni autoservicio; la única vía gratuita
  es su app de escritorio (micro virtual, 60 min/día) no embebible → incompatible
  con coste-cero y con captura en móviles. **El navegador no salva**: la
  `noiseSuppression` de getUserMedia (ya activa por defecto) es la supresión
  clásica de WebRTC — ruido estacionario, PRESERVA voces (incluida la del vecino);
  el constraint `voiceIsolation` solo funciona en ChromeOS y Safari ni ha
  respondido a la spec; el «Aislar voz» de iOS no está documentado para
  getUserMedia (probar empíricamente, no construir sobre ello). **Insight clave:
  la supresión de ruido NO elimina voces ajenas** — el problema real de TradIon
  (cross-captura) requiere VERIFICACIÓN DE HABLANTE, exactamente lo que hace el
  «voice isolation» personalizado de Teams con un perfil de voz enrolado… y
  TradIon YA tiene la huella vocal del enroll. **Diseño F11 — Speaker Gate (coste
  cero, 100% local):** en el servidor, extraer el embedding de locutor de cada
  segmento (sherpa-onnx, Apache-2.0, modelos ONNX WeSpeaker CAM++/3D-Speaker de
  25-38 MB, **nativos a 16 kHz** — encajan sin re-muestrear) y compararlo por
  coseno contra el embedding de la huella del dueño del micro; doble umbral
  (aceptar / zona gris marcada en UI / rechazar con telemetría), partir de 0.5-0.6
  y calibrar en mesa. Complementa a la guardia LID v2: el LID caza cross-captura
  ENTRE idiomas; el gate caza la del MISMO idioma (dos hispanohablantes) y valida
  la identidad siempre. Precauciones de la literatura: <2 s de voz degrada el EER
  (~+46% relativo de 3.6→2 s) → acumular ≥2 s por decisión; subir el enroll a
  10-15 s o promediar embeddings de segmentos aceptados (adaptación online); los
  embeddings deben calcularse sobre el MISMO tipo de audio que el enroll (si se
  añade denoiser, re-enrolar). Benchmark previo obligatorio en la Victus
  (objetivo <50 ms/segmento, plausible). **Ruido de fondo (no vocal), opcional:**
  RNNoise-WASM de Jitsi (BSD/Apache, patrón producción: worklet a 48 kHz, buffer
  128→480, ANTES del downsample a 16 kHz) en el cliente; DeepFilterNet3
  (MIT/Apache, RTF 0.19 CPU, 40 ms de latencia algorítmica, ojo 16→48→16) en el
  servidor si hiciera falta más. **Maxine/Broadcast: descartado** (licencia de
  evaluación, ~1 GB, y tampoco elimina voces). Señal complementaria barata para la
  zona gris: comparar energía/instante de llegada del mismo evento entre
  dispositivos (el micro del hablante real capta antes y más fuerte).

- **2026-08-04 (3) — F11 implementado + verificación adversarial (6 hallazgos, todos
  corregidos) + autopsia de la 5ª sesión.** El Speaker Gate quedó implementado
  (backend/speaker_gate.py, integración en _run_stt, referencia en el enroll y en el
  footprint, scripts de setup/benchmark, apagado por defecto y fail-open) y validado
  EN LOCAL con el modelo CAM++ real: 11-28 ms/embedding en el M3, mismo hablante
  0.795, peor par ajeno sintético 0.343. La verificación adversarial (2 lentes)
  confirmó 6 defectos, corregidos: **(1+3)** el suelo `min_speech_s` medía la
  duración TOTAL del clip (pre-roll + 600 ms de cola de silencio = hasta ~70%
  relleno): el gate podía RECHAZAR turnos cortos legítimos del dueño → ahora el
  segmentador propaga la VOZ REAL (`speech_ms`) y la cola de silencio (`tail_ms`),
  el suelo compara voz de verdad y al embedding se le recorta la cola muerta;
  **(2+6)** los PARCIALES esquivaban el gate (la voz del intruso se difundía en
  vivo hasta 15 s con atribución falsa y nada la retractaba) → el gate corre
  también antes del STT de parciales (≥800 ms de voz garantizados por
  partial_min_speech_ms), liberando siempre el cerrojo `_partial_inflight`;
  **(4)** el benchmark daba luz verde con margen 0.007 sobre voces SINTÉTICAS →
  veredicto honesto (avisa que los previews son cota inferior y bloquea el GO con
  margen <0.05) + **modo voces REALES** (`bench_speaker_gate.py voz1.wav voz2.wav`:
  matriz de similitudes y umbrales recomendados — probado: recomienda
  accept 0.51/reject 0.30 con es-ald vs es-carlfm); **(5)** `tmp.rename` →
  `tmp.replace` (en Windows, un modelo corrupto previo dejaba el setup en bucle).
  De la 5ª sesión real (log): la guardia LID v2 en producción — cross-captura
  es→en descartada (p=1.00 y 0.94) y decenas de «LID dudoso ('is' 0.98) →
  re-transcripción forzada» salvando el español del micro Bluetooth (banda
  estrecha ≈ «islandés» para el LID; al quitarse los auriculares BT a mitad de
  sesión, transcripciones limpias — confirmación empírica: micro BT NO, escuchar
  por BT sí); tope de temperaturas verificado (la alucinación tardó 2.9 s, no 18);
  y un hueco nuevo cazado y corregido: **'Hmmmm…' ×198 pasó logprob, se tradujo y
  se sintetizaron 13 s de emes** → filtro anti-degeneración (racha de carácter,
  diversidad ínfima, palabra dominante ≥5×/≥80%) con salvaguardas verificadas
  («sí, sí, sí, sí» real pasa). Propuesta F12 «Tela de araña» (profundidad 3D
  visual e intuitiva: anillos concéntricos en el plano, lowpass de «aire» por
  distancia, modos Sutil/Inmersivo) presentada al arquitecto — pendiente de GO.

- **2026-08-04 (4) — Modo SOMBRA del Speaker Gate (propuesta del arquitecto: probar
  en el sistema levantado con logs, no en frío).** Benchmark de la Victus recibido:
  13-30 ms/embedding (aprobado), mismo hablante 0.757, peor par ajeno sintético
  0.271 — hardware y motor validados; falta la calibración con voces reales. En vez
  del baile manual de WAVs, el gate gana `enforce` (defecto `false` = **modo
  sombra**): calcula y LOGUEA la similitud de CADA final contra la huella del
  enroll (`speaker-gate SOMBRA: seg N sim 0.71 -> accept (2.3 s voz)`) y avisa de
  los «habría sido DESCARTADO», pero **jamás tira nada** — una sesión normal de
  conversación ES la calibración con voces reales. La plantilla Windows va con
  `enabled: true + enforce: false` (telemetría en la próxima sesión; fail-open si
  faltara algo); base y Mac siguen apagadas. Flujo de activación: sesión real →
  leer la distribución de similitudes del log (propias vs cross-captura) → ajustar
  accept/reject → `enforce: true`. Verificado por simulación: en sombra ningún
  camino descarta (finales ni parciales) y enforce conserva el comportamiento
  anterior intacto.

- **2026-08-04 (5) — FLOOR v2: el turno deja de ser del micrófono más sensible.**
  Autopsia de la 6ª sesión (Mac + iPhone): el árbitro del canal usaba un umbral
  ABSOLUTO (0.025 para todos) y el iPhone con AGC lo cruzaba con la voz DEL VECINO —
  **6 «Canal tomado» seguidos acabando todos en LID cross-captura**, mientras el
  micro del hablante real moría puesto a cero («I have to be close to my
  microphone…» dicho en la propia sesión). El modo sombra del gate además dio su
  veredicto: con estas dos voces (ambas ~132 Hz) la voz propia (0.43-0.68) y la
  cross-captura (0.52-0.55 en el Mac) SE SOLAPAN → `enforce` prohibido para esta
  pareja; el gate sigue en sombra (un final propio legítimo, 'no sé por qué',
  puntuó 0.32 — enforce lo habría matado). **Rediseño v2**: (a) umbrales RELATIVOS
  al nivel de voz calibrado de cada micro (enroll → `voice_rms`, suelo absoluto y
  tope 3×), (b) CONTIENDA de 240 ms — el canal es de quien suena más fuerte
  relativo a su propia calibración, no del primer frame, (c) la cross-captura
  demostrada por el LID libera el canal y encuarentena 1 s. **Auditoría
  adversarial: 7 hallazgos confirmados, 7 corregidos** — evidencia crosstalk
  RANCIA (llega ~1 s tarde) ya no mata la re-adquisición legítima (frescura:
  `floor_last_acquired` vs `closed_at` del segmento); ventana 120→240 ms (el
  frame del cliente es de 128 ms — la puja del hablante real podía llegar tras la
  resolución); un solo veredicto LID ya no cuesta el canal (CORROBORACIÓN: 2
  seguidos en ≤10 s — un «¿cómo se dice…?» legítimo no penaliza); divisor de la
  puja con SUELO y cap 2.5 (un enroll flojo con vr<0.025 ganaba siempre);
  subcampeón hereda el grant si el mejor cae; `acquire_rel` 0.30→0.20 +
  **adaptación EMA** del `voice_rms` con los finales propios (el enroll se habla
  con el micro cerca; la mesa queda lejos); racha de cross-captura se corta con
  cada final propio. Todo verificado por simulación hallazgo a hallazgo. Firmas
  nuevas en el log: «Canal tomado por X (contienda, norm N, M pujas)» y «Canal
  liberado (cross-captura xN)».

- **2026-08-04 (6) — Auditoría de seguridad del repositorio + README profesional.**
  A petición del arquitecto antes de la siguiente prueba. **Hallazgo crítico: el
  repositorio es PÚBLICO y la clave privada TLS (`config/certs/tradion-key.pem`)
  quedó en el HISTORIAL** — se commiteó en la versión inicial (`7ff4152`), un
  commit posterior la "eliminó" (`e845c85`) pero el borrado no la saca de git: se
  extrae con `git show` (verificado). Alcance: es una clave de certificado hoja de
  mkcert (localhost + IP LAN) — explotable solo con acceso a la LAN y la CA de
  mkcert instalada en los móviles, pero es infraestructura que protege voz
  biométrica. **Acciones del arquitecto (no delegables): (1) ROTAR los
  certificados en la Victus (regenerar con mkcert — la clave filtrada queda
  inservible), (2) hacer el repo PRIVADO en GitHub, (3) opcional si permanece
  público: purgar el historial (`git filter-repo`) + force-push + re-clonar en la
  Victus.** Resto del barrido LIMPIO: ningún fichero sensible rastreado hoy, sin
  huellas vocales ni modelos jamás commiteados, sin patrones de secretos en el
  contenido, la IP del README es un placeholder de ejemplo. Endurecido el
  `.gitignore` (`config/certs/` entero, `*.crt/*.p12`, `.env.*`, `models/tmp/`).
  **README reescrito de cero**: funcionalidades reales al día (turnos calibrados
  por micro, guardia LID, speaker gate en sombra, 3D colaborativo, reconexión
  invisible, diag), diagrama mermaid del pipeline, instalación por hardware,
  flujo de actualización Windows (PowerShell sin `&&` + Copy-Item de la
  plantilla), conectividad LAN-primero con la regla de firewall, recomendaciones
  de audio medidas (cable sí, micro BT no), tabla de configuración, estructura del
  repo, hoja de ruta y aviso reforzado de certificados como secretos.

## 🔗 Fuentes

- Referencia integral: https://github.com/QuentinFuxa/WhisperLiveKit · Topología: https://github.com/niedev/RTranslator
- STT: https://github.com/SYSTRAN/faster-whisper · VAD: https://github.com/snakers4/silero-vad
- MT: https://huggingface.co/facebook/nllb-200-distilled-600M · https://opennmt.net/CTranslate2/guides/transformers.html
- TTS: https://github.com/SWivid/F5-TTS · (retirado) https://github.com/myshell-ai/MeloTTS
- Red/HTTPS: https://github.com/FiloSottile/mkcert · https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/getUserMedia
- Audio 3D/iOS: https://developer.mozilla.org/en-US/docs/Web/API/PannerNode · https://developer.apple.com/forums/thread/696034
- AEC Chromium/WebAudio: https://bugs.chromium.org/p/chromium/issues/detail?id=687574

<p align="center">
  <h1 align="center">TradIon — Traducción Simultánea Local con IA</h1>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Licencia-AGPL--3.0-blue.svg" alt="License AGPL-3.0">
  <img src="https://img.shields.io/badge/Python-3.10+-blue.svg" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/Hardware-Apple_Silicon_|_NVIDIA_GPU-green.svg" alt="Hardware Support">
  <img src="https://img.shields.io/badge/Idiomas-ES_·_KO_·_EN-purple.svg" alt="Idiomas">
  <img src="https://img.shields.io/badge/Privacidad-100%25_Local-orange.svg" alt="100% Local">
</p>

**TradIon** convierte una mesa física en una mesa multilingüe: cada persona habla en su idioma a su propio móvil y escucha a los demás **traducidos, con una voz afín a la de cada hablante y sonando desde el sitio real donde está sentado** (audio 3D). Todo el procesamiento —reconocimiento, traducción y síntesis de voz— ocurre en **un único ordenador de la casa**: sin nubes, sin cuotas, sin instalar apps (los móviles entran por el navegador).

Idiomas soportados: **Español · 한국어 · English** (matriz completa, cualquier dirección).

---

## ✨ Qué hace (funcionalidades)

| Funcionalidad | En qué consiste |
| :--- | :--- |
| **Traducción de voz en tiempo real** | Frase cerrada → traducida y hablada en ~**0,5-1,5 s** (RTX 3070) o ~1,5-4 s (MacBook M3), medidos en sesiones reales. |
| **Subtítulos en vivo** | Ves la transcripción crecer *mientras* la persona habla (modelo dedicado que jamás compite con las traducciones). |
| **Turno de palabra automático** | Un árbitro tipo walkie-talkie (CSMA/CA) decide qué micrófono emite, **calibrado por dispositivo**: cada micro compite relativo a su propio nivel de voz, así un móvil con micrófono sensible no roba el canal. Con histéresis, anti-acaparamiento y autocorrección de robos. |
| **Anti cross-captura** | Si tu micro capta la voz del vecino, una guardia de idioma (LID con salvaguardas) la descarta y devuelve el canal; un verificador de locutor (huella del enroll) registra telemetría por segmento. |
| **Voz afín por idioma** | En la calibración se mide tu tono (f0) y se te asigna la voz del catálogo más parecida **para cada idioma destino** — la escuchas y la cambias antes de entrar. Clonación Zero-Shot real (F5-TTS) disponible como modo calidad opcional. |
| **Audio 3D colaborativo** | Plano de la mesa arrastrable y **compartido**: cualquiera recoloca a cualquiera y la voz de cada persona suena desde su asiento (HRTF). Requiere auriculares (idealmente de cable). |
| **Calibración validada por voz** | El enroll te pide leer frases y las valida por reconocimiento (tolerante a acentos); de ahí salen tu huella vocal, tu tono y el nivel de tu micro. |
| **Reconexiones invisibles** | Un micro-corte de red reconecta en 1-2 s conservando identidad, voz, asiento y subtítulos — la mesa ni se entera. |
| **Interfaz trilingüe** | Toda la UI en es/ko/en, con estado honesto en pantalla: quién tiene el turno, por qué no te oyen, micro silenciado. |
| **Arranque sin internet** | Tras la primera descarga de modelos, el servidor arranca y funciona 100 % offline. |
| **Diagnóstico de audio 3D** | Página `/static/diag.html` para medir en 2 minutos qué puede dar cada móvil + auriculares (estéreo real, firma Bluetooth HFP, prueba de oído). |

## 🧠 Cómo funciona

```mermaid
flowchart LR
    subgraph Movil["📱 Móvil de cada comensal (navegador)"]
        MIC[Micrófono<br/>AudioWorklet 16 kHz] --> WS
        WS[WebSocket seguro] --> OUT[Auriculares<br/>PannerNode 3D]
    end
    subgraph Servidor["🖥️ Servidor central (Mac o PC con NVIDIA)"]
        FLOOR[Turno de palabra<br/>CSMA/CA calibrado] --> VAD[Detector de voz<br/>Silero VAD]
        VAD --> STT[faster-whisper<br/>+ guardia LID]
        STT --> FILT[Filtros anti-alucinación<br/>+ speaker gate]
        FILT --> MT[NLLB-200 CTranslate2<br/>oración a oración]
        MT --> TTS[Piper TTS<br/>voz afín por f0]
    end
    WS --> FLOOR
    TTS --> WS
```

1. **Captura**: tu móvil re-muestrea el micrófono a 16 kHz y lo envía por WebSocket seguro. Empiezas silenciado (como Zoom).
2. **Turno**: el primer micro con voz franca *relativa a su propia calibración* toma el canal; los demás se silencian en el servidor (así tu eco en el móvil de enfrente no genera duplicados). El turno se libera a los 600 ms de silencio.
3. **Segmentación**: un detector neuronal de voz corta tus frases con contexto (pre-roll) y sin trocear pausas naturales.
4. **Transcripción**: `faster-whisper` con una **guardia de idioma**: si detecta con confianza alta otro idioma *de la sala* en tu micro, es la voz del vecino y se descarta (dos seguidas devuelven además el canal); si duda (acentos), re-transcribe forzando tu idioma — nunca pierde tu voz.
5. **Filtros**: confianza, lista negra de alucinaciones, anti-degeneración, y un **verificador de locutor** que compara cada segmento con tu huella del enroll (en modo telemetría hasta calibrarse con las voces reales de tu mesa).
6. **Traducción**: NLLB-200 (int8) **oración a oración**, con topes anti-alucinación y recorte de diálogos inventados.
7. **Síntesis**: Piper genera la voz asignada a tu tono en <150 ms (pre-calentada al calibrar).
8. **Reproducción 3D**: cada oyente recibe subtítulo + traducción a su idioma + audio, que suena desde el asiento real del hablante (mesa hexagonal arrastrable y sincronizada para todos).

**Privacidad por diseño**: la huella vocal vive en RAM y en un WAV temporal que se purga al salir y se barre al arrancar; nada sale de tu red local.

---

## 🛠 Instalación (paso a paso)

Los móviles acceden por navegador — **sin instalar ninguna app**.

### 1. Certificados de red (requisito de los navegadores)
Los navegadores móviles exigen HTTPS para encender el micrófono. Instala `mkcert` a nivel de sistema:
- **Mac:** `brew install mkcert`
- **Windows:** `winget install mkcert`
> ⚠️ Tras instalar, cierra la consola y abre una nueva.

### 2. Clonar y crear el entorno virtual

```bash
git clone https://github.com/pttb6dhg24-tech/TradIon.git
cd TradIon
python -m venv .venv

# Mac (zsh/bash):
source .venv/bin/activate
# Windows (CMD/PowerShell):
.venv\Scripts\activate

pip install -r requirements.txt
```

### 3. Generar los certificados locales

> [!IMPORTANT]
> **Incluye la IP local de tu ordenador** en el certificado: sin ella, los móviles verán un candado inválido y no podrán encender el micrófono. Averíguala con `ipconfig getifaddr en0` (Mac) o `ipconfig` (Windows, campo IPv4).

```bash
mkcert -install
# Sustituye 192.168.1.50 por TU IP local:
mkcert -cert-file config/certs/tradion.pem -key-file config/certs/tradion-key.pem localhost 127.0.0.1 ::1 192.168.1.50
```

Instala además la CA raíz de mkcert en cada móvil (`mkcert -CAROOT` te dice dónde está el `rootCA.pem`).

> [!CAUTION]
> **Los certificados son secretos y NUNCA deben subirse al repositorio** (el `.gitignore` ya veta `config/certs/` entero). Si una clave privada llega a commitearse alguna vez, bórrala **y además regenera los certificados** — eliminar el fichero no lo saca del historial de git.

### 4. Elegir la configuración de tu hardware
Copia la plantilla de tu sistema como configuración activa:
- **Mac (Apple Silicon):** `cp config/settings.mac.yaml config/settings.yaml`
- **Windows (NVIDIA):** `Copy-Item config\settings.windows.yaml config\settings.yaml`

> [!WARNING]
> **Windows (NVIDIA):** para que CUDA encuentre sus librerías:
> ```cmd
> pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
> copy .venv\Lib\site-packages\nvidia\cublas\bin\*.dll .venv\Scripts\
> copy .venv\Lib\site-packages\nvidia\cudnn\bin\*.dll .venv\Scripts\
> ```

### 5. Descargar el catálogo de voces

```bash
# Mac / Linux:
bash scripts/setup_voices.sh

# Windows (mismo resultado, sin bash):
python -m piper.download_voices --data-dir models/piper es_ES-davefx-medium es_ES-carlfm-x_low es_ES-sharvard-medium es_MX-claude-high es_MX-ald-medium en_US-amy-medium en_US-ryan-high en_US-lessac-medium en_GB-alan-medium ko_KR-kss-medium
```

### 6. (Opcional) Verificador de locutor — Speaker Gate

```bash
pip install sherpa-onnx
python scripts/setup_speaker_gate.py
python scripts/bench_speaker_gate.py
```

Arranca **en modo sombra** (solo telemetría en los logs, jamás descarta). Calíbralo con dos voces reales de tu mesa (`python scripts/bench_speaker_gate.py voz1.wav voz2.wav`) antes de plantearte `enforce: true` en `settings.yaml`.

### 7. Arrancar el servidor

```bash
python -m backend.main_server
```

La primera vez descargará los modelos de IA a `models/` (varios minutos). Después, **arranca y funciona sin internet**.

---

## 🔄 Actualizar el servidor (Windows / PowerShell)

Tras cada `git pull`, restaura tu configuración activa desde la plantilla (evita conflictos de merge):

```powershell
git pull
Copy-Item config\settings.windows.yaml config\settings.yaml -Force
```

> 💡 PowerShell 5.1 no acepta `&&` para encadenar comandos: ejecútalos en líneas separadas (o con `;`). Tras actualizar, los móviles deben recargar la página una vez (el servidor ya sirve el frontend con `no-cache`, pero la primera recarga tras una versión antigua puede necesitar cerrar y reabrir la pestaña).

---

## 🌐 Conectividad

### A) Modo recomendado: WiFi local (offline absoluto)
Máxima privacidad, mínima latencia, cero cortes. Solo necesitas que móviles y servidor compartan WiFi.

1. **Windows**: permite el puerto en el firewall (una vez, como Administrador, con el WiFi marcado como red "Privada"):
   ```powershell
   netsh advfirewall firewall add rule name="TradIon 8443" dir=in action=allow protocol=TCP localport=8443 profile=private remoteip=localsubnet
   ```
2. Arranca el servidor y entra desde los móviles en `https://<IP-DE-TU-ORDENADOR>:8443`.

### B) Túnel de Cloudflare (solo pruebas puntuales)

```bash
cloudflared tunnel --url https://localhost:8443 --no-tls-verify
```

> [!CAUTION]
> **El enlace del túnel NO es privado y la mesa no tiene autenticación**: cualquiera con la URL entra, escucha las traducciones y deja muestra de su voz. Además, el túnel corta los WebSockets con timeouts de ~100 s y añade 50-200 ms de latencia. Úsalo solo para pruebas breves con gente de confianza; para uso real, WiFi local.

---

## 🎧 Recomendaciones de audio (importan más que cualquier ajuste)

- **Auriculares de cable** para el 3D completo: el HRTF es binaural y por el altavoz del móvil es físicamente imposible.
- **Bluetooth**: vale para *escuchar*, pero **su micrófono no** — con el micro activo, iOS/Android conmutan al perfil de telefonía (mono, banda estrecha) y el reconocimiento se degrada gravemente (comprobado en sesiones reales). Usa el micro del propio móvil.
- Cada móvil cerca de su dueño; habla claro y deja que la frase termine (el turno se libera a los 600 ms de silencio).
- Ante cualquier duda, abre `https://<IP>:8443/static/diag.html` en el móvil: mide tu ruta de audio real y te dice qué puede dar.

## ⚙️ Configuración

Toda la configuración vive en `config/settings.yaml` (nada hardcodeado). Las claves más útiles:

| Clave | Qué controla |
| :--- | :--- |
| `stt.model_size` / `device` | Modelo Whisper y CPU/CUDA (plantillas ya afinadas por hardware). |
| `audio.segment_close_silence_ms` | Silencio que cierra una frase (600 ms: equilibrio frases completas/latencia). |
| `room.floor.*` | El turno de palabra: umbrales relativos, contienda, anti-acaparamiento. |
| `stt.lid_crosstalk_min_prob` | Confianza mínima para descartar voz ajena por idioma. |
| `stt.speaker_gate.*` | Verificador de locutor (sombra/enforce, umbrales). |
| `tts.backend` | `piper` (rápido, por defecto) o `f5tts` (clonación real, más lento). |

## 🧩 Estructura del repositorio

```
backend/    servidor aiohttp, motores (core_engine, translation, tts, speaker_gate)
frontend/   PWA vanilla: lobby, mesa, audio-worklet, diagnóstico 3D
config/     settings.yaml + plantillas por hardware (certs/ NUNCA se versiona)
scripts/    descarga de voces, speaker gate, benchmarks, MeCab coreano
models/     pesos de IA (se descargan; fuera del repo)
docs/       DOCUMENTO_MAESTRO.md — arquitectura, decisiones y diario de ingeniería
```

## 🗺️ Hoja de ruta

- **F12 — "Tela de araña"**: anillos de distancia en el plano 3D + filtro de "aire" con la lejanía + modos Sutil/Inmersivo.
- **Speaker gate en enforce** tras calibración con las voces reales de cada mesa.
- PIN de sala para exposición fuera de la LAN.

---

## ⚖️ Licencia

TradIon es **software libre**, publicado bajo **[AGPL-3.0-or-later](LICENSE)**.

Copyright © 2026 Kevin Rodrigo Logro Sandoval.

Puedes usarlo, estudiarlo, modificarlo y redistribuirlo, **incluso comercialmente**. La
única condición del copyleft: si distribuyes el programa —o lo ofreces a través de una
red— debes poner el código fuente correspondiente a disposición de esos usuarios, bajo
esta misma licencia. El uso privado y el uso interno de una organización **no generan
ninguna obligación**.

### Licencia comercial (modelo dual)

Si quieres integrar TradIon en un producto propietario o tu política interna no admite la
AGPL, hay disponible una **licencia comercial** que exime del copyleft. El autor conserva
el 100 % de los derechos sobre el código propio, así que puede otorgarla directamente.

📩 **Licencias comerciales y soporte: [4-berro.ruedas@icloud.com](mailto:4-berro.ruedas@icloud.com)**

> [!CAUTION]
> **La licencia de TradIon no cubre los modelos de terceros que descarga.**
>
> Por defecto se usa **NLLB-200** (Meta), bajo **CC-BY-NC-4.0**, que **prohíbe el uso
> comercial** — y esa cláusula restringe *el uso*, no solo la redistribución, así que no
> basta con que los pesos los descargue el usuario. Tres voces de Piper (incluida
> `ko_KR-kss`, la única coreana) tienen también licencias no comerciales.
>
> Antes de cobrar por nada, pon `licensing.commercial_use: true` en
> `config/settings.yaml`: TradIon comprobará su propia configuración al arrancar y te dirá
> exactamente qué componente debes sustituir.
>
> El detalle completo —cada componente, su licencia y su fuente— está en
> **[THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md)**, junto con los sustitutos de
> licencia permisiva ya verificados.

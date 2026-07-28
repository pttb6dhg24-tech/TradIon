<p align="center">
  <h1 align="center">TradIon — Traducción Simultánea Local con IA</h1>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Licencia-MIT-blue.svg" alt="License MIT">
  <img src="https://img.shields.io/badge/Python-3.10+-blue.svg" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/Hardware-M_Series_|_NVIDIA_GPU-green.svg" alt="Hardware Support">
  <img src="https://img.shields.io/badge/Latencia-Real--Time-red.svg" alt="Real Time">
  <img src="https://img.shields.io/badge/Seguridad-100%25_Local-orange.svg" alt="100% Local">
</p>

**TradIon** es un sistema avanzado de traducción de voz simultánea en tiempo real, 100% local y de coste cero. Diseñado específicamente para reuniones presenciales usando teléfonos móviles conectados a una misma red, permite a múltiples usuarios hablar de forma simultánea en distintos idiomas (**Español, Coreano, Inglés**), sin necesidad de instalar ninguna aplicación.

Desarrollado con una arquitectura moderna de baja latencia, procesa el audio en un servidor central (MacBook Pro o PC con NVIDIA) y lo distribuye a los teléfonos de la mesa. Muestra subtítulos parciales al instante mientras hablas, asigna a cada hablante una voz afín a su tono (análisis de f0 en la calibración) para leer sus traducciones — con clonación Zero-Shot real como modo opcional — y espacializa el audio en 3D.

---

## 🚀 Características Principales

- **100% Local y Privado:** Todo el procesamiento (STT, MT, TTS) ocurre en tu máquina. Nada se envía a servidores de terceros ni hay cuotas por uso.
- **Baja Latencia (Real-Time):** subtítulos parciales instantáneos mientras hablas; la voz traducida llega típicamente en **1,5-4 s** tras cerrar la frase (STT+MT+TTS medidos con modelos INT8; la síntesis en sí tarda <150 ms).
- **Voz afín + Clonación opcional:** en la calibración se analiza tu tono (f0) y se te asigna la voz más parecida del catálogo para cada idioma (puedes escucharlas y cambiarlas). El modo **Zero-Shot Voice Cloning** real (F5-TTS, `tts.backend: f5tts`) clona tu voz con las frases de calibración, a cambio de más latencia (~4-7 s por frase).
- **Cross-Mic Suppression:** Supresión inteligente del eco y bucles de audio cruzados, vital en entornos donde múltiples micrófonos de móviles están encendidos en la misma mesa.
- **Audio 3D (Espacial):** Mapeo de la sala en el cliente Web (Web Audio API) que permite escuchar la traducción viniendo físicamente de donde está sentada la otra persona.

## 🧠 Tech Stack

| Componente | Tecnología Principal | Propósito |
| :--- | :--- | :--- |
| **STT (Reconocimiento)** | Faster-Whisper (INT8) | Transcripción de altísima velocidad y precisión. |
| **MT (Traducción)** | NLLB-200 / CTranslate2 | Traducción neuronal simultánea (Meta). |
| **TTS (Síntesis)** | Piper TTS / F5-TTS | Voces ultrarrápidas y clonación Zero-Shot biométrica. |
| **Red & Servidor** | aiohttp + WebSockets | Conectividad asíncrona full-duplex de baja latencia. |

---

## 🛠 Instalación y Configuración (Paso a Paso)

Sigue estos pasos en orden para evitar conflictos. Los móviles de los usuarios accederán escaneando un QR o mediante enlace Web, **sin instalar ninguna App.**

### 1. Requisitos Previos (Certificados de Red)
Los navegadores móviles exigen HTTPS para encender el micrófono. Necesitamos instalar `mkcert` a nivel de sistema operativo para generar candados locales.
- **Mac:** `brew install mkcert`
- **Windows:** `winget install mkcert`
> ⚠️ **Importante:** Tras instalar, cierra la consola y abre una nueva para que tu ordenador reconozca el comando.

### 2. Clonar y Crear Entorno Virtual
Para evitar que las pesadas librerías de Inteligencia Artificial interfieran o rompan tu sistema, encapsularemos el proyecto en un entorno virtual (`.venv`).

```bash
git clone https://github.com/pttb6dhg24-tech/TradIon.git
cd TradIon
python -m venv .venv

# En Mac (zsh/bash):
source .venv/bin/activate
# En Windows (CMD/PowerShell):
.venv\Scripts\activate

pip install -r requirements.txt
```

### 3. Generar Certificados Locales
Con tu entorno virtual activo, genera los certificados (se guardarán automáticamente en la carpeta `config/certs/`).

> [!IMPORTANT]
> **Incluye la IP local de tu ordenador** en el certificado: sin ella, los móviles que entren por WiFi (Modo Offline) verán un certificado inválido y no podrán encender el micrófono. Averíguala con `ipconfig getifaddr en0` (Mac) o `ipconfig` (Windows, campo IPv4).

```bash
mkcert -install
# Sustituye 192.168.1.50 por TU IP local:
mkcert -cert-file config/certs/tradion.pem -key-file config/certs/tradion-key.pem localhost 127.0.0.1 ::1 192.168.1.50
```
*(Si tu router cambia la IP por DHCP, fija una IP estática o regenera el certificado cuando cambie. Instala además la CA raíz de mkcert en cada móvil: `mkcert -CAROOT` te dice dónde está el `rootCA.pem`.)*

### 4. Configurar el Hardware
Entra en la carpeta `config/` y **copia** la plantilla de tu sistema, renombrándola a `settings.yaml`:
- **Mac (M2/M3):** Copia `settings.mac.yaml` a `settings.yaml`. *(Usa el modelo `small` por CPU)*
- **Windows (NVIDIA CUDA):** Copia `settings.windows.yaml` a `settings.yaml`. *(Usa CUDA y el modelo `large-v3-turbo`)*

> [!WARNING]
> **Usuarios de Windows (NVIDIA):** 
> Para que el sistema detecte las librerías matemáticas de la GPU, instala estos paquetes e inyecta las DLLs:
> ```cmd
> pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
> copy .venv\Lib\site-packages\nvidia\cublas\bin\*.dll .venv\Scripts\
> copy .venv\Lib\site-packages\nvidia\cudnn\bin\*.dll .venv\Scripts\
> ```

### 5. Descargar el Catálogo de Voces (TTS)
El motor de voz necesita las voces del catálogo en `models/piper/` (sin ellas el servidor no arranca):
```bash
# Mac / Linux:
bash scripts/setup_voices.sh

# Windows (mismo resultado, sin bash):
python -m piper.download_voices --data-dir models/piper es_ES-davefx-medium es_ES-carlfm-x_low es_ES-sharvard-medium es_MX-claude-high es_MX-ald-medium en_US-amy-medium en_US-ryan-high en_US-lessac-medium en_GB-alan-medium ko_KR-kss-medium
```

### 6. Arrancar el Servidor
Una vez instalados los certificados, elegida la configuración y descargadas las voces, estás listo para encender el motor de IA.
```bash
python -m backend.main_server
```
*(La primera vez tardará varios minutos en descargar los modelos de IA y guardarlos en la carpeta `models/`).*

---

## 🌐 Conectividad: Modo Offline vs Online

### A) Modo Offline Absoluto (Local Wi-Fi)
Puedes llevarte el servidor a medio de la selva sin internet. Solo necesitas un Router Wi-Fi local.
1. Arranca el servidor: `python -m backend.main_server`
2. En el móvil, conéctate al WiFi y entra en: `https://<IP-DE-TU-ORDENADOR>:8443`.
*(Cero latencia de red, privacidad extrema).*

### B) Modo Online (Túneles de Cloudflare)
Para usarlo a través de internet móvil (4G/5G) o cuando la red WiFi de la empresa bloquea conexiones entre ordenadores, exponemos el servidor usando Cloudflare.
1. Arranca el servidor: `python -m backend.main_server`
2. En otra terminal, abre un túnel ciego de Cloudflare conectándolo a tus certificados internos:
   ```bash
   cloudflared tunnel --url https://localhost:8443 --no-tls-verify
   ```
3. Pasa a los usuarios el enlace público generado por Cloudflare.

> [!CAUTION]
> **El enlace del túnel es PÚBLICO y la mesa no tiene autenticación:** cualquiera que consiga la URL puede entrar a la sala, escuchar las traducciones y dejar una muestra de su voz en tu ordenador (se borra al salir o reiniciar, pero existe mientras dura la sesión). Usa el túnel solo para pruebas puntuales con gente de confianza y ciérralo al terminar. Para uso habitual, quédate en el Modo Offline (WiFi local).

---

## ⚖️ Licencia y Uso Comercial (Double Licensing)

El código fuente de este repositorio (Arquitectura TradIon, algoritmos de red y código del servidor) ha sido diseñado y programado desde cero por su autor y se publica bajo licencia **MIT**, permitiendo explícitamente su uso y explotación comercial.

> [!CAUTION]
> **Aviso para uso comercial:**
> Por defecto, el archivo de configuración de este repositorio instruye al sistema a descargar y utilizar el modelo de inteligencia artificial **NLLB-200** (desarrollado por Meta). El modelo NLLB-200 está protegido bajo la licencia **CC-BY-NC-4.0**, lo que **PROHÍBE ESTRICTAMENTE SU USO COMERCIAL**.
> 
> Si eres una empresa y pretendes utilizar el sistema TradIon para un producto de pago, vender servicios, o cualquier actividad con ánimo de lucro, **DEBES modificar el archivo `config/settings.yaml` para sustituir el modelo NLLB de Meta por un modelo de traducción de licencia abierta, o conectar una API comercial (ej. OpenAI, DeepL).** El autor original de TradIon no asume ninguna responsabilidad legal por el uso indebido de los pesos del modelo de Meta por parte de terceros.

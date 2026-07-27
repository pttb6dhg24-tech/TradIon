# TradIon — Traducción Simultánea Local con IA

**TradIon** es un sistema de traducción de voz bidireccional (Español ↔ Coreano) en tiempo real, 100% local y de coste cero, diseñado para funcionar en reuniones presenciales usando teléfonos móviles conectados a una misma red WiFi.

Desarrollado con una arquitectura moderna de baja latencia, procesa el audio en un servidor central (MacBook Pro o PC con NVIDIA) y lo distribuye espacialmente (Audio 3D) a los teléfonos de la mesa, clonando la huella vocal de los hablantes en menos de un segundo (Zero-Shot Voice Cloning).

## 🚀 Características Principales

- **100% Local y Privado:** Todo el procesamiento ocurre en tu máquina. Nada de subir audio a servidores de terceros ni pagar cuotas por minuto.
- **Baja Latencia (Real-Time):** El pipeline está optimizado para traducir párrafos enteros en menos de 1 segundo utilizando modelos cuantizados (INT8).
- **Zero-Shot Voice Cloning (F5-TTS/Piper):** Clona el tono y las características vocales del usuario con solo un par de frases de calibración, leyendo las traducciones con su propia "voz sintética".
- **Cross-Mic Suppression:** Supresión inteligente del eco y bucles de audio cruzados en entornos donde múltiples micrófonos de móviles están encendidos en la misma mesa.
- **Audio 3D (Espacial):** Mapeo de la sala en el cliente Web (Web Audio API) que permite escuchar la traducción viniendo físicamente de donde está sentada la otra persona.

## 🛠 Instalación y Configuración

El proyecto contiene el backend central que deben ejecutar en el ordenador servidor. Los móviles de los usuarios accederán mediante el navegador Web, sin instalar ninguna App.

### 1. Preparar el entorno (Mac o Windows)

Clona este repositorio e instala las dependencias:

```bash
git clone https://github.com/pttb6dhg24-tech/TradIon.git
cd TradIon
python -m venv .venv

# En Mac (zsh/bash):
source .venv/bin/activate
pip install -r requirements.txt

# En Windows (CMD/PowerShell):
.venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configurar el Hardware (¡Importante!)

El sistema funciona excelentemente en ordenadores Mac (Apple Silicon M2/M3) o PCs de Windows con tarjetas gráficas NVIDIA (RTX 3050, 4070, etc). Hemos preparado dos plantillas de configuración.

Entra en la carpeta `config/` y **copia** la plantilla de tu sistema, renombrándola a `settings.yaml`:

- **Si usas Mac (M2/M3):**
  Copia `settings.mac.yaml` a `settings.yaml`.
  *Usa el modelo `small` de Whisper en modo CPU para minimizar la latencia.*

- **Si usas PC Windows (NVIDIA RTX):**
  Copia `settings.windows.yaml` a `settings.yaml`.
  *Usa CUDA y el modelo `large-v3-turbo` para transcripciones perfectas en milisegundos.*

> [!WARNING]
> **Usuarios de Windows (NVIDIA):** 
> Si al iniciar el servidor te aparece un error como `Library cublas64_12.dll is not found`, ejecuta estos comandos en tu consola (con el entorno `.venv` activado) para integrar las librerías matemáticas de la GPU:
> ```cmd
> pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
> copy .venv\Lib\site-packages\nvidia\cublas\bin\*.dll .venv\Scripts\
> copy .venv\Lib\site-packages\nvidia\cudnn\bin\*.dll .venv\Scripts\
> ```

### 3. Certificados de Red
Debido a que el navegador de los móviles requiere **HTTPS** para acceder al micrófono, debes generar certificados TLS usando `mkcert`.
```bash
mkcert -install
mkcert -cert-file config/certs/tradion.pem -key-file config/certs/tradion-key.pem 0.0.0.0 localhost 127.0.0.1
```

### 4. Arrancar
```bash
python -m backend.main_server
```
La primera vez tardará varios minutos en descargar los pesos de los modelos (se guardarán todos de forma segura en la carpeta `models/`). 
Luego, para probarlo, simplemente expón tu puerto local a internet mediante Cloudflare Tunnels y escanea el enlace en tu móvil.

---

## ⚖️ Licencia y Uso Comercial (Double Licensing)

El código fuente de este repositorio (La arquitectura TradIon, los algoritmos de red y el código del servidor) ha sido escrito desde cero y se publica bajo licencia **MIT**, permitiendo su uso comercial.

> [!CAUTION]
> **Aviso para uso comercial:**
> Por defecto, el archivo de configuración de este repositorio instruye al sistema a descargar y utilizar el modelo de inteligencia artificial **NLLB-200** (desarrollado por Meta). El modelo NLLB-200 está protegido bajo la licencia **CC-BY-NC-4.0**, lo que **PROHÍBE ESTRICTAMENTE SU USO COMERCIAL**.
> 
> Si eres una empresa y pretendes utilizar el sistema TradIon para un producto de pago, vender servicios, o cualquier actividad con ánimo de lucro, **DEBES modificar el archivo `config/settings.yaml` para sustituir el modelo NLLB de Meta por un modelo de traducción de licencia abierta, o conectar una API comercial (ej. OpenAI, DeepL).** El autor original de TradIon no asume ninguna responsabilidad legal por el uso indebido de los pesos del modelo de Meta.

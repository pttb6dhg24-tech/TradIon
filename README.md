# TradIon — Traductor Simultáneo Mesh 3D

Sistema **100% local y de coste cero** para traducción de voz bidireccional
**Español ↔ Coreano** en tiempo real, con detección de voz (VAD), aislamiento de
ruido, identificación de hablantes y audio espacializado 3D en el navegador.

> 📖 El estado real del proyecto, las decisiones tomadas y las notas de
> configuración viven en [`docs/DOCUMENTO_MAESTRO.md`](docs/DOCUMENTO_MAESTRO.md).
> **Léelo antes de tocar nada.**

## Estructura del proyecto

```
TradIon/
├── backend/                 # Cerebro Python (servidor local)
│   ├── core_engine.py       # Bloque 1 — VAD + STT (Whisper)
│   ├── translation_engine.py# Bloque 2 — Traducción ES<->KO (PENDIENTE de guardar)
│   ├── tts_engine.py        # Bloque 1b — Síntesis de voz (futuro)
│   └── main_server.py       # Bloque 3 — Servidor de red y orquestador (futuro)
├── frontend/                # Bloque 4 — Cliente web/PWA de los móviles
├── models/                  # Pesos descargados (NUNCA se suben a git)
├── config/
│   └── settings.yaml        # Toda la configuración en UN sitio (nada hardcodeado)
├── docs/
│   └── DOCUMENTO_MAESTRO.md # Estado, decisiones, notas de hardware y roadmap
└── requirements.txt         # Dependencias Python fijadas
```

## Reglas de trabajo (para humanos y para IAs)

1. **Un bloque cada vez.** No se empieza un bloque hasta que el anterior tiene
   su script de prueba (`tests/`) pasando.
2. **Nada hardcodeado.** Rutas, puertos, idiomas y tamaños de modelo se leen de
   `config/settings.yaml`.
3. **Los modelos viven en `models/`.** Toda descarga de pesos debe apuntar ahí
   (parámetro `download_root` / `cache_dir`), nunca a caches ocultas del sistema.
4. **Cada sesión de trabajo actualiza `docs/DOCUMENTO_MAESTRO.md`**: qué se
   hizo, qué se decidió, qué queda roto.

## Arranque rápido (entorno)

```bash
cd ~/Documents/TradIon
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

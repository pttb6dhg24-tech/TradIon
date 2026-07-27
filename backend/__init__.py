"""Paquete backend de TradIon.

IMPORTANTE (regla M1 — pesos en models/, nunca en ~/.cache): HF_HUB_CACHE debe fijarse
ANTES de que cualquier módulo importe huggingface_hub, porque esa librería congela el
valor de la variable al importarse. core_engine importa faster_whisper (que importa
huggingface_hub en cadena) y MeloTTS descarga sus pesos vía hf_hub_download sin
cache_dir explícito: este __init__ se ejecuta antes que el cuerpo de cualquier módulo
backend.* y es el único punto que garantiza el orden correcto.

setdefault respeta un override externo del usuario (export HF_HUB_CACHE=...).
"""
import os
from pathlib import Path

os.environ.setdefault(
    "HF_HUB_CACHE", str(Path(__file__).resolve().parent.parent / "models" / "hf")
)
# Evita el warning/deadlock de tokenizers de HF al hacer fork tras usar el tokenizador
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

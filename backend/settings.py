"""Carga de config/settings.yaml — única fuente de configuración (DOCUMENTO_MAESTRO §4).

Módulo compartido por todos los motores para no duplicar el loader.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.yaml"


def load_settings(path: str | Path | None = None) -> dict[str, Any]:
    settings_path = Path(path) if path else SETTINGS_PATH
    with open(settings_path, "r", encoding="utf-8") as fh:
        settings = yaml.safe_load(fh)
    if not isinstance(settings, dict):
        raise ValueError(f"Configuración inválida o vacía: {settings_path}")
    return settings


def resolve_dir(relative: str | Path) -> Path:
    """Resuelve un directorio del YAML contra la raíz del proyecto y lo crea si falta."""
    path = Path(relative)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    path.mkdir(parents=True, exist_ok=True)
    return path

"""Guardia de licencias: impide cobrar por TradIon con componentes no comerciales.

TradIon es AGPL-3.0, pero los MODELOS que ejecuta son obras de terceros con sus propias
licencias, y varias de las que trae por defecto son NO COMERCIALES:

  - facebook/nllb-200-distilled-600M          CC-BY-NC-4.0
  - voces ko_KR-kss, en_US-ryan, en_US-lessac CC BY-NC-SA / research only

La cláusula NonCommercial de Creative Commons restringe **el uso**, no solo la
redistribución, así que no basta con que los pesos los descargue el usuario: si se cobra
por un producto cuya traducción depende de ellos, el uso queda fuera de la licencia.

Este módulo lo hace explícito en vez de dejarlo en un comentario del YAML:

  licensing:
    commercial_use: false   # defecto: uso personal/investigación, todo funciona
    commercial_use: true    # el arranque FALLA si hay algún componente no comercial

El detalle de cada componente y sus sustitutos permisivos está en THIRD_PARTY_LICENSES.md.
Esto no es asesoramiento jurídico.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("tradion.licensing")


# Modelos de traducción con licencia NO comercial, por prefijo de identificador.
# La conversión a CTranslate2 NO limpia la licencia (es obra derivada), así que se
# compara también contra el ct2_dir.
NONCOMMERCIAL_MT = {
    "facebook/nllb-200": "CC-BY-NC-4.0",
    "facebook/seamless-m4t": "CC-BY-NC-4.0",
    "facebook/hf-seamless-m4t": "CC-BY-NC-4.0",
    "qwen/qwen2.5-3b": "Qwen Research License (non-commercial)",
}

# Sustitutos con licencia comercial verificada (THIRD_PARTY_LICENSES.md §2)
SUSTITUTOS_MT = (
    "Helsinki-NLP/opus-mt-* (Apache-2.0 / CC-BY-4.0), "
    "google/madlad400-3b-mt (Apache-2.0) o facebook/m2m100_418M (MIT)"
)


class LicenseError(RuntimeError):
    """La configuración activa no es compatible con el uso comercial declarado."""


def is_commercial(settings: dict[str, Any]) -> bool:
    return bool((settings.get("licensing") or {}).get("commercial_use", False))


def _mt_violation(settings: dict[str, Any]) -> str | None:
    translation = settings.get("translation") or {}
    campos = " ".join(str(translation.get(k, "")) for k in ("model", "ct2_dir")).lower()
    for prefijo, licencia in NONCOMMERCIAL_MT.items():
        if prefijo in campos:
            return (f"traducción: '{translation.get('model')}' está bajo {licencia}, "
                    f"que prohíbe el uso comercial. Sustitúyelo por {SUSTITUTOS_MT}")
    return None


def audit(settings: dict[str, Any], voices: list[Any] | None = None) -> list[str]:
    """Devuelve la lista de incumplimientos para uso comercial (vacía si todo limpio).

    No lanza: decidir qué hacer con el resultado es de quien llama, porque en modo no
    comercial estos mismos puntos son informativos y perfectamente legales.
    """
    problemas: list[str] = []
    mt = _mt_violation(settings)
    if mt:
        problemas.append(mt)
    for voz in voices or []:
        estado = getattr(voz, "commercial", "incierto")
        if estado == "no":
            problemas.append(f"voz '{voz.id}': {voz.license} — prohíbe el uso comercial")
        elif estado == "incierto":
            problemas.append(f"voz '{voz.id}': {voz.license} — licencia no acreditable, "
                             f"trátala como no comercial hasta poder demostrarla")
    return problemas


def enforce(settings: dict[str, Any], voices: list[Any] | None = None) -> None:
    """Comprueba la configuración al arrancar.

    Con commercial_use=false (defecto) solo informa: el uso personal, de investigación o
    de demostración con NLLB y las voces NC es legítimo. Con commercial_use=true aborta
    el arranque antes de que nadie facture nada indebidamente.
    """
    problemas = audit(settings, voices)
    if not is_commercial(settings):
        if problemas:
            logger.info("Licencias: modo NO comercial (uso personal/investigación). "
                        "%d componente(s) no podrían usarse en un producto de pago; "
                        "pon licensing.commercial_use: true para verlos en detalle.",
                        len(problemas))
        return
    if problemas:
        detalle = "\n  - ".join(problemas)
        raise LicenseError(
            "licensing.commercial_use está en TRUE pero la configuración activa incluye "
            f"componentes que NO permiten uso comercial:\n  - {detalle}\n"
            "Consulta THIRD_PARTY_LICENSES.md para los sustitutos verificados."
        )
    logger.info("Licencias: configuración apta para USO COMERCIAL (ningún componente "
                "no comercial activo).")

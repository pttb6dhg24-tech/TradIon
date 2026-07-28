"""
TradIon — F2: traducción local ES/KO/EN con NLLB-200.

Correcciones sobre la versión recibida (auditadas contra transformers/CTranslate2 actuales):
- `tokenizer.lang_code_to_id` fue ELIMINADO de transformers modernos -> convert_tokens_to_ids.
- `max_length=100` truncaba frases largas en silencio -> max_output_tokens del YAML + aviso al truncar entrada.
- Errores devueltos como "" -> TranslationError tipada (regla M2).
- Descargas a ~/.cache -> cache_dir dentro de models/ (regla M1).
- `tokenizer.src_lang` es estado mutable compartido -> lock; la inferencia corre en un
  ThreadPoolExecutor vía translate_async() para no congelar el event loop (regla C2).
- Nuevo backend 'ct2' (CTranslate2 int8): en el M3 usa ~4x menos RAM y es más rápido en CPU.
  'transformers' queda como alternativa funcional sin paso de conversión.

Smoke test:
    python -m backend.translation_engine     # es<->ko<->en con latencias por frase
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from backend.settings import PROJECT_ROOT, load_settings, resolve_dir

logger = logging.getLogger("tradion.translation")


class TranslationError(Exception):
    """Error tipado de la etapa de traducción. Nunca se degrada a '' (auditoría M2)."""


class TextTranslator:
    """Traductor NLLB many-to-many: cualquier par entre languages.allowed (es/ko/en)."""

    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        self.settings = settings or load_settings()
        cfg = self.settings["translation"]
        langs = self.settings["languages"]

        self.nllb_codes: dict[str, str] = dict(langs["nllb_codes"])
        missing = set(langs["allowed"]) - set(self.nllb_codes)
        if missing:
            raise TranslationError(f"Faltan códigos NLLB en settings.yaml para: {sorted(missing)}")

        self.model_name: str = cfg["model"]
        self.backend: str = cfg.get("backend", "ct2")
        self._beam_size = int(cfg.get("beam_size", 2))
        self._max_input_tokens = int(cfg.get("max_input_tokens", 256))
        self._max_output_tokens = int(cfg.get("max_output_tokens", 256))
        self._device = cfg.get("device", "cpu")

        hf_cache = resolve_dir(Path(self.settings["paths"]["models_dir"]) / "hf")
        from transformers import AutoTokenizer  # import tardío: acelera arrancar sin este motor
        self.tokenizer = AutoTokenizer.from_pretrained(self.model_name, cache_dir=str(hf_cache))

        # Un código FLORES mal escrito en el YAML mapearía a <unk> y produciría traducciones
        # corruptas SIN error (convert_tokens_to_ids no lanza): validar contra el vocabulario ya
        for lang, code in self.nllb_codes.items():
            if self.tokenizer.convert_tokens_to_ids(code) == self.tokenizer.unk_token_id:
                raise TranslationError(
                    f"Código NLLB inválido para '{lang}': {code!r} no está en el vocabulario "
                    "del modelo (los códigos FLORES distinguen mayúsculas, p. ej. kor_Hang)"
                )

        # El lock cubre TODA operación sobre el tokenizer: el setter de src_lang muta el
        # post-procesador Rust en cada llamada y chocaría con encode/decode de otro hilo
        # (RuntimeError 'Already borrowed' intermitente con workers >= 2)
        self._lock = threading.Lock()
        workers = int(cfg.get("workers", 2)) if self.backend == "ct2" else 1
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="mt")

        if self.backend == "ct2":
            self._init_ct2(cfg)
        elif self.backend == "transformers":
            self._init_transformers(hf_cache)
        else:
            raise TranslationError(f"Backend desconocido: {self.backend!r} (usa 'ct2' o 'transformers')")

        logger.info("TextTranslator listo: %s vía %s, beam=%d, idiomas=%s",
                    self.model_name, self.backend, self._beam_size, sorted(self.nllb_codes))

    # ---------- carga ----------

    def _init_ct2(self, cfg: dict[str, Any]) -> None:
        import ctranslate2
        ct2_dir = Path(cfg["ct2_dir"])
        if not ct2_dir.is_absolute():
            ct2_dir = PROJECT_ROOT / ct2_dir
        if not (ct2_dir / "model.bin").exists():
            # HF_HUB_CACHE (no HF_HOME): su layout coincide con el cache_dir del tokenizer,
            # así la conversión reutiliza lo ya descargado en models/hf sin duplicar 2.4 GB
            raise TranslationError(
                f"No existe el modelo CTranslate2 en {ct2_dir}. Conviértelo una vez con:\n"
                f'  HF_HUB_CACHE="{PROJECT_ROOT}/models/hf" ct2-transformers-converter '
                f"--model {self.model_name} --output_dir {ct2_dir} --quantization int8"
            )
        # inter_threads: traducciones en paralelo; intra_threads=0: CTranslate2 decide por CPU.
        # device desde el YAML (estaba hardcodeado a cpu): la plantilla Windows/NVIDIA
        # puede acelerar el MT con device: cuda
        self._ct2 = ctranslate2.Translator(
            str(ct2_dir), device=str(cfg.get("device", "cpu")),
            inter_threads=int(cfg.get("workers", 2)), intra_threads=0,
        )

    def _init_transformers(self, hf_cache: Path) -> None:
        import torch
        from transformers import AutoModelForSeq2SeqLM
        self._torch = torch
        # En el M3 sin CUDA: float32 en CPU (float16 en CPU es más lento y propenso a NaN)
        self._model = AutoModelForSeq2SeqLM.from_pretrained(
            self.model_name, cache_dir=str(hf_cache), torch_dtype=torch.float32
        ).to(self._device)
        self._model.eval()

    # ---------- API ----------

    def translate(self, text: str, src_lang: str, tgt_lang: str) -> str:
        """Síncrona (tests y scripts). En el servidor usa translate_async()."""
        text = text.strip()
        if not text:
            return ""
        src_code, tgt_code = self._resolve_pair(src_lang, tgt_lang)
        if self.backend == "ct2":
            return self._translate_ct2(text, src_code, tgt_code)
        return self._translate_transformers(text, src_code, tgt_code)

    async def translate_async(self, text: str, src_lang: str, tgt_lang: str) -> str:
        """No bloquea el event loop: corre en el pool del traductor (C2)."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self.translate, text, src_lang, tgt_lang)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False)

    # ---------- internos ----------

    def _resolve_pair(self, src_lang: str, tgt_lang: str) -> tuple[str, str]:
        for lang in (src_lang, tgt_lang):
            if lang not in self.nllb_codes:
                raise TranslationError(
                    f"Idioma '{lang}' no configurado; usa uno de {sorted(self.nllb_codes)}"
                )
        if src_lang == tgt_lang:
            raise TranslationError("src_lang y tgt_lang no pueden ser el mismo idioma")
        return self.nllb_codes[src_lang], self.nllb_codes[tgt_lang]

    def _encode_tokens(self, text: str, src_code: str) -> list[str]:
        with self._lock:
            self.tokenizer.src_lang = src_code
            ids = self.tokenizer.encode(text, truncation=True, max_length=self._max_input_tokens)
            tokens = self.tokenizer.convert_ids_to_tokens(ids)
        if len(ids) >= self._max_input_tokens:
            logger.warning("Entrada truncada a %d tokens; divide la frase antes de traducir",
                           self._max_input_tokens)
        return tokens

    def _translate_ct2(self, text: str, src_code: str, tgt_code: str) -> str:
        try:
            tokens = self._encode_tokens(text, src_code)
        except Exception as exc:  # el contrato de translate() es SIEMPRE TranslationError (M2)
            raise TranslationError(f"Tokenización falló ({src_code}): {exc}") from exc
        try:
            results = self._ct2.translate_batch(
                [tokens],
                target_prefix=[[tgt_code]],
                beam_size=self._beam_size,
                max_decoding_length=self._max_output_tokens,
            )
        except Exception as exc:
            raise TranslationError(f"CTranslate2 falló ({src_code}->{tgt_code}): {exc}") from exc
        try:
            hypothesis = results[0].hypotheses[0]
            if hypothesis and hypothesis[0] == tgt_code:
                hypothesis = hypothesis[1:]  # quitar el token de idioma del prefijo
            with self._lock:
                ids = self.tokenizer.convert_tokens_to_ids(hypothesis)
                return self.tokenizer.decode(ids, skip_special_tokens=True).strip()
        except Exception as exc:
            raise TranslationError(f"Decodificación falló ({src_code}->{tgt_code}): {exc}") from exc

    def _translate_transformers(self, text: str, src_code: str, tgt_code: str) -> str:
        torch = self._torch
        try:
            with self._lock:
                self.tokenizer.src_lang = src_code
                inputs = self.tokenizer(
                    text, return_tensors="pt",
                    truncation=True, max_length=self._max_input_tokens,
                ).to(self._device)
                # lang_code_to_id fue eliminado de transformers: el id del idioma destino se
                # obtiene del vocabulario (los códigos FLORES son tokens especiales de NLLB)
                bos_id = self.tokenizer.convert_tokens_to_ids(tgt_code)
                with torch.inference_mode():
                    output = self._model.generate(
                        **inputs,
                        forced_bos_token_id=bos_id,
                        num_beams=self._beam_size,
                        max_new_tokens=self._max_output_tokens,
                    )
                return self.tokenizer.batch_decode(output, skip_special_tokens=True)[0].strip()
        except TranslationError:
            raise
        except Exception as exc:
            raise TranslationError(f"NLLB (transformers) falló ({src_code}->{tgt_code}): {exc}") from exc


# ---------- smoke test ----------

def _demo() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    translator = TextTranslator()
    pruebas = [
        ("es", "ko", "¿Dónde está la estación de tren más cercana?"),
        ("ko", "es", "나는 사과를 먹지 않았다"),
        ("es", "en", "Este sistema funciona sin conexión a internet."),
        ("en", "ko", "Nice to meet you — the food was delicious."),
        ("ko", "en", "내일 아침에 회의가 있어요"),
    ]
    for src, tgt, frase in pruebas:
        t0 = time.perf_counter()
        salida = translator.translate(frase, src, tgt)
        ms = (time.perf_counter() - t0) * 1000.0
        print(f"{src}->{tgt} [{ms:6.0f} ms] {frase!r} -> {salida!r}")


if __name__ == "__main__":
    _demo()

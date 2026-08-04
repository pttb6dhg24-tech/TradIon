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
import re
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
        try:
            # Primero SIN red: tras el primer arranque todo está en models/hf. Esto
            # elimina ~8 peticiones a huggingface.co en cada boot (vistas en el log de
            # la Victus) y permite arrancar el servidor SIN internet en la sala.
            self.tokenizer = AutoTokenizer.from_pretrained(
                self.model_name, cache_dir=str(hf_cache), local_files_only=True)
        except Exception:
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
            # 'auto' elige el mejor kernel del dispositivo REAL: sin esto, un modelo
            # convertido a int8 corría como int8_float32 en CUDA (kernels fp32, más
            # lento y más VRAM). En la 3070 'auto' resuelve a int8_float16.
            compute_type=str(cfg.get("compute_type", "auto")),
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

    # Guion de diálogo al inicio ('- ', '– ', '— ') y separador de turnos tras el
    # cierre de una oración ('...? - No.'): un em-dash de aposición en mitad de frase
    # ('Me gusta — mucho — el plan') NUNCA sigue a un signo de cierre, no se toca.
    _DIALOG_DASH = re.compile(r"^\s*[-–—]\s+")
    _TURN_SPLIT = re.compile(r"\n+|(?<=[.!?…])\s+(?=[-–—]\s)")
    _TURN_DASH = re.compile(r"(?<=[.!?…])\s+[-–—]\s+")
    _SENT_SPLIT = re.compile(r"(?<=[.!?…])\s+")
    _INVENTED_ANSWER = re.compile(r"(?<=\?)\s+(?=[-–—]\s)")

    def translate(self, text: str, src_lang: str, tgt_lang: str) -> str:
        """Síncrona (tests y scripts). En el servidor usa translate_async().

        Se traduce ORACIÓN A ORACIÓN: NLLB es un modelo de una sola oración (model
        card oficial) y con entradas multi-frase OMITE oraciones enteras — visto en
        producción en la Victus: "Oh that's a good choice. You don't like the
        cheesecake...?" perdió la primera frase en la traducción. Además, con fuente
        mono-oración el recorte anti-diálogo aplica limpio en cada pieza."""
        text = text.strip()
        if not text:
            return ""
        src_code, tgt_code = self._resolve_pair(src_lang, tgt_lang)
        pieces = [s.strip() for s in self._SENT_SPLIT.split(text) if s.strip()] or [text]
        if self.backend == "ct2":
            # UNA llamada a translate_batch con todas las oraciones: secuencial
            # duplicaba la latencia por oración extra (medido bajo carga en la
            # Victus: MT a 1,5 s con la GPU ocupada)
            outs = self._translate_ct2_batch(pieces, src_code, tgt_code)
        else:
            outs = [self._translate_transformers(p, src_code, tgt_code) for p in pieces]
        cleaned = [self._strip_invented_dialog(p, o) for p, o in zip(pieces, outs)]
        return " ".join(o for o in cleaned if o).strip()

    def _strip_invented_dialog(self, source: str, translated: str) -> str:
        """NLLB aprendió el formato de subtítulos de su corpus y con frases cortas a
        veces (a) antepone un guion de diálogo y (b) INVENTA un segundo turno entero:
        'What did you say?' -> '- ¿Qué dijiste? - No.' — ese '- No.' no lo dijo nadie
        y el TTS lo pronunciaba en la mesa. Solo si la fuente tiene UNA oración (y
        sin guion propio) se recorta al primer turno: con fuente MULTI-oración la
        salida multi-turno es contenido REAL en formato subtítulo ('Gracias. Hasta
        mañana.' -> '- Thank you. - See you tomorrow.') y recortar destruiría la
        segunda oración — ahí solo se limpian los guiones. El turno inventado también
        aparece SIN guion inicial tras una PREGUNTA ('How are you?' -> '¿Cómo estás?
        - Bien.', Victus 2026-08-04: ese '- Bien.' fantasma se sintetizó y realimentó
        el micro vecino); sin el guion inicial como señal fuerte, el recorte se
        limita al patrón pregunta->respuesta ('? - X'): un '. - ' tras afirmación es
        demasiado a menudo contenido real ('9 a.m. - 6 p.m.', reformateos de fuente
        con coma; hallazgo adversarial) y NO se toca. Cada recorte queda logueado."""
        if not translated or self._DIALOG_DASH.match(source):
            return translated
        src_sentences = [s for s in self._SENT_SPLIT.split(source.strip()) if s.strip()]
        if not self._DIALOG_DASH.match(translated):
            if len(src_sentences) > 1:
                return translated
            first = self._INVENTED_ANSWER.split(translated, maxsplit=1)[0].strip()
            if first != translated:
                logger.info("MT: respuesta inventada tras pregunta recortada: %r -> %r",
                            translated, first)
            return first or translated
        stripped = self._DIALOG_DASH.sub("", translated, count=1)
        if len(src_sentences) > 1:
            cleaned = self._TURN_DASH.sub(" ", stripped).strip()
            return cleaned or translated
        first = self._TURN_SPLIT.split(stripped, maxsplit=1)[0].strip()
        if first != translated:
            logger.info("MT: formato de diálogo inventado recortado: %r -> %r",
                        translated, first)
        return first or translated

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

    def _translate_ct2_batch(self, texts: list[str], src_code: str, tgt_code: str) -> list[str]:
        try:
            token_lists = [self._encode_tokens(t, src_code) for t in texts]
        except Exception as exc:  # el contrato de translate() es SIEMPRE TranslationError (M2)
            raise TranslationError(f"Tokenización falló ({src_code}): {exc}") from exc
        max_src_len = max((len(toks) for toks in token_lists), default=1)
        try:
            results = self._ct2.translate_batch(
                token_lists,
                target_prefix=[[tgt_code]] * len(token_lists),
                beam_size=self._beam_size,
                # Defensas del "standard setting" de NLLB-600M (benchmark HalOmi,
                # arxiv 2305.11746): tope de longitud RELATIVO a la entrada
                # (3·len+5) — la palanca directa contra la sobre-generación con
                # locuciones cortas ('What did you say?' -> turno de diálogo
                # inventado '- No.'), que no es una repetición y a la que
                # no_repeat_ngram_size no llega — más bloqueo de 3-gramas
                # repetidos ('Thank you. Thank you.') y sin token <unk>.
                # El tope es único por lote (CT2 no lo acepta por ejemplo): se usa
                # la oración MÁS LARGA — algo más laxo para las cortas del lote,
                # pero el post-proceso anti-diálogo sigue cubriendo ese hueco.
                max_decoding_length=min(self._max_output_tokens,
                                        3 * max_src_len + 5),
                no_repeat_ngram_size=3,
                disable_unk=True,
            )
        except Exception as exc:
            raise TranslationError(f"CTranslate2 falló ({src_code}->{tgt_code}): {exc}") from exc
        try:
            outs: list[str] = []
            for res in results:
                hypothesis = res.hypotheses[0]
                if hypothesis and hypothesis[0] == tgt_code:
                    hypothesis = hypothesis[1:]  # quitar el token de idioma del prefijo
                with self._lock:
                    ids = self.tokenizer.convert_tokens_to_ids(hypothesis)
                    outs.append(self.tokenizer.decode(ids, skip_special_tokens=True).strip())
            return outs
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

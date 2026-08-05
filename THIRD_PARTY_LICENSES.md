# Licencias de terceros

TradIon se distribuye bajo **AGPL-3.0-or-later** (ver [`LICENSE`](LICENSE)). Este
documento recoge cada componente de terceros que el sistema ejecuta, su licencia y si
permite uso comercial, con la fuente donde se verifica.

**Los pesos de los modelos NO se distribuyen en este repositorio** (`models/` está en
`.gitignore`): los scripts de instalación los descargan de sus fuentes oficiales en la
máquina del usuario. Aun así se documentan aquí, porque algunas licencias restringen el
**uso**, no solo la redistribución.

Última verificación: **5 de agosto de 2026**. Las licencias cambian; re-verifica antes de
cualquier lanzamiento comercial.

> Este documento no es asesoramiento jurídico.

---

## 1. Código (bibliotecas)

| Componente | Licencia | Uso comercial | Fuente |
|---|---|---|---|
| **piper-tts** ≥1.6 (`OHF-Voice/piper1-gpl`) | **GPL-3.0-or-later** | ⚠️ Copyleft | https://github.com/OHF-Voice/piper1-gpl/blob/main/COPYING |
| **espeak-ng** (embebido en piper-tts) | GPL-3.0 | ⚠️ Copyleft | https://github.com/espeak-ng/espeak-ng/blob/master/COPYING |
| faster-whisper | MIT | ✅ | https://github.com/SYSTRAN/faster-whisper/blob/master/LICENSE |
| CTranslate2 | MIT | ✅ | https://github.com/OpenNMT/CTranslate2/blob/master/LICENSE |
| silero-vad | MIT | ✅ | https://github.com/snakers4/silero-vad/blob/master/LICENSE |
| sherpa-onnx | Apache-2.0 | ✅ | https://github.com/k2-fsa/sherpa-onnx/blob/master/LICENSE |
| aiohttp | Apache-2.0 | ✅ | https://github.com/aio-libs/aiohttp/blob/master/LICENSE.txt |
| transformers, sentencepiece, huggingface_hub | Apache-2.0 | ✅ | https://github.com/huggingface/transformers/blob/main/LICENSE |
| torch, soundfile | BSD-3-Clause | ✅ | https://github.com/pytorch/pytorch/blob/main/LICENSE |
| numpy | BSD-3-Clause | ✅ | https://github.com/numpy/numpy/blob/main/LICENSE.txt |
| PyYAML | MIT | ✅ | https://github.com/yaml/pyyaml/blob/main/LICENSE |

### Nota sobre piper-tts y la elección de licencia de TradIon

El paquete `piper-tts` de PyPI **migró** del antiguo `rhasspy/piper` (MIT) a
`OHF-Voice/piper1-gpl`, que es **GPL-3.0-or-later** porque embebe `espeak-ng` para la
fonemización. `backend/voice_catalog.py` importa `piper` en el mismo proceso, de modo que
la obra distribuida es una obra combinada sujeta a copyleft.

Esta es la razón técnica por la que TradIon se publica bajo **AGPL-3.0** y no bajo una
licencia permisiva o *source-available*: la AGPLv3 §13 autoriza expresamente combinar con
obras bajo GPLv3, mientras que BSL, FSL, Elastic v2 o una licencia propietaria serían
incompatibles.

### Compatibilidad con AGPL-3.0 (posición de la FSF)

- **Apache-2.0**: *"compatible with version 3 of the GNU GPL"* (no con GPLv2).
- **MIT/Expat** y **BSD de 3 cláusulas**: *"compatible with the GNU GPL"*.
- **CC BY 4.0**: *"compatible with all versions of the GNU GPL"*.
- **GPL-3.0**: AGPLv3 §13 autoriza expresamente enlazar y combinar.

Fuente: https://www.gnu.org/licenses/license-list.html

---

## 2. Pesos de modelos

| Modelo | Licencia | Uso comercial | Fuente |
|---|---|---|---|
| Whisper large-v3-turbo (y small/tiny) | **MIT** | ✅ | https://huggingface.co/openai/whisper-large-v3-turbo |
| silero-vad (pesos) | MIT | ✅ | pesos dentro del repo, en `src/silero_vad/data/` |
| CAM++ 3D-Speaker | Apache-2.0 | ✅ | https://www.modelscope.cn/models/iic/speech_campplus_sv_zh_en_16k-common_advanced |
| **NLLB-200 distilled 600M** | **CC-BY-NC-4.0** | 🔴 **NO** | https://huggingface.co/facebook/nllb-200-distilled-600M |

OpenAI declara expresamente: *"Whisper's code and model weights are released under the MIT
License"* — la MIT cubre también los pesos, no solo el código.

### 🔴 NLLB-200 bloquea el uso comercial

Meta separa explícitamente código y pesos: *"NLLB code and fairseq(-py) is MIT-licensed…
All models are licensed under CC-BY-NC 4.0"*. La ficha añade que el modelo *"is not
released for production deployment"*.

Dos aclaraciones que suelen malinterpretarse:

1. **Convertirlo a CTranslate2 no lo libera.** Un modelo convertido o cuantizado es
   *Adapted Material* (CC BY-NC 4.0 §2.a.1.B) y hereda la cláusula NoComercial.
2. **Que los pesos los descargue el usuario tampoco.** CC-BY-NC restringe **el uso**, no
   solo la redistribución.

**Sustitutos con licencia comercial verificada:**

| Modelo | Licencia | Notas |
|---|---|---|
| `Helsinki-NLP/opus-mt-*` | Apache-2.0 / CC-BY-4.0 | Ligeros, por par de idiomas, soporte CTranslate2 nativo. Verificar par por par. |
| `google/madlad400-3b-mt` | Apache-2.0 | Conversión CTranslate2 confirmada. |
| `facebook/m2m100_418M` / `1.2B` | MIT | Sustituto más directo de NLLB. |

⚠️ Evitar: `facebook/seamless-m4t-v2-large` (CC-BY-NC-4.0) y
`Qwen/Qwen2.5-3B-Instruct`, cuyo LICENSE dice *"FOR NON-COMMERCIAL PURPOSES ONLY"* pese a
parecer permisivo.

---

## 3. Voces de Piper

Cada voz `.onnx` hereda la licencia del corpus con el que se entrenó.

| Voz | Licencia del corpus | Uso comercial |
|---|---|---|
| `es_ES-carlfm-x_low` | Dominio público | ✅ Sí |
| `es_ES-davefx-medium` | CC0 1.0 (Nabu Casa) | ✅ Probable |
| `es_MX-ald-medium` | Unlicense | ✅ Probable |
| `es_ES-sharvard-medium` | CC BY 3.0 (Edinburgh DataShare) | ⚠️ Sí, **con atribución** |
| `es_MX-claude-high` | Sin dataset citado | ❓ Indeterminable |
| `en_GB-alan-medium` | Documentación contradictoria | ❓ Indeterminable |
| `en_US-amy-medium` | Sin licencia localizable | ❓ Indeterminable |
| `en_US-lessac-medium` | Blizzard Challenge 2013 — *research only* | 🔴 **No** |
| `en_US-ryan-high` | CC BY-NC-SA 4.0 | 🔴 **No** |
| `ko_KR-kss-medium` | CC BY-NC-SA 4.0 | 🔴 **No** |

Fuente de cada voz: su `MODEL_CARD` en https://huggingface.co/rhasspy/piper-voices

**Aviso de producto**: `ko_KR-kss` es actualmente la **única** voz coreana del catálogo. Un
despliegue comercial en coreano requiere sustituirla. Las voces marcadas
«indeterminable» deben tratarse como no utilizables comercialmente hasta poder acreditar
su licencia.

El interruptor `licensing.commercial_use` de `config/settings.yaml` controla este
comportamiento: con `true`, TradIon se niega a cargar los componentes no comerciales.

---

## 4. Obligaciones si algún día empaquetas los pesos

Hoy no aplica (`models/` no se distribuye). Si en el futuro se publica un instalador,
imagen Docker o binario que **incluya** los pesos:

- **MIT** (Whisper, silero): incluir el aviso de copyright y el texto de la licencia.
- **Apache-2.0** (CAM++, sherpa-onnx): incluir copia de la licencia, conservar los avisos
  y el fichero NOTICE, y señalar los ficheros modificados.
- **GPL-3.0** (piper-tts, espeak-ng): ofrecer el código fuente correspondiente.
- **CC BY** (voces con atribución): citar autor y licencia en los créditos.

---

## 5. Obligaciones ajenas a las licencias

Independientes de todo lo anterior y aplicables igualmente:

- **RGPD, art. 9**: el *speaker gate* (F11) genera huellas vocales, que son datos
  biométricos de categoría especial. Requiere base jurídica, información y política de
  retención. El procesamiento 100 % local juega a favor, pero no exime.
- **Reglamento de IA (UE)**: la identificación biométrica tiene obligaciones específicas.
- **Grabación de conversaciones**: consentimiento de todos los participantes.

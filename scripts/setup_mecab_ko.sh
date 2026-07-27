#!/bin/bash
# TradIon — instala python-mecab-ko AISLADO para macOS (DOCUMENTO_MAESTRO §7).
#
# Por qué existe este script:
#   - La voz INGLESA de MeloTTS necesita mecab-python3 (paquete 'MeCab/'), porque
#     melo/text/english.py importa melo/text/japanese.py, que crea MeCab.Tagger()
#     a nivel de módulo.
#   - La voz COREANA usa g2pkk, que en macOS/Linux hace 'import mecab' y necesita
#     python-mecab-ko (paquete 'mecab/').
#   - El sistema de archivos de macOS es INSENSIBLE a mayúsculas: 'MeCab/' y 'mecab/'
#     son la misma carpeta en site-packages. Cualquier pip install/uninstall de uno
#     destroza los archivos del otro (incluida la desinstalación implícita de --upgrade).
#   - Solución: python-mecab-ko vive en .venv/mecab-ko (directorio propio), añadido al
#     sys.path con un .pth. Los IMPORTS de Python sí distinguen mayúsculas, así que
#     'import MeCab' resuelve en site-packages e 'import mecab' en .venv/mecab-ko.
#
# REGLA: jamás instalar python-mecab-ko con pip normal en este venv. Solo con este script.
#
# Uso:  bash scripts/setup_mecab_ko.sh
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -x .venv/bin/python ]; then
    echo "ERROR: no existe .venv — crea el entorno primero (ver README)" >&2
    exit 1
fi

TARGET="$PWD/.venv/mecab-ko"
SITE=$(.venv/bin/python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')

# 1) Purgar restos de instalaciones NORMALES de python-mecab-ko en site-packages:
#    sus dist-info huérfanos hacen que un pip futuro "desinstale" rutas mecab/* que,
#    por la insensibilidad a mayúsculas, vacían MeCab/ (ocurrió el 2026-07-26).
rm -rf "$SITE"/python_mecab_ko-*.dist-info "$SITE"/python_mecab_ko_dic-*.dist-info \
       "$SITE"/_mecab.cpython-*.so "$SITE"/mecab_ko_dic

# 2) Instalar en el directorio aislado (sin --upgrade: evita semántica de desinstalación)
rm -rf "$TARGET"
.venv/bin/pip install --target "$TARGET" python-mecab-ko python-mecab-ko-dic
echo "$TARGET" > "$SITE/zz_mecab_ko.pth"

# 3) Verificar que MeCab (mecab-python3) sigue íntegro; auto-reparar si algo lo pisó
if ! .venv/bin/python -c "import MeCab; MeCab.Tagger()" 2>/dev/null; then
    echo "MeCab dañado: reinstalando mecab-python3..."
    rm -rf "$SITE/MeCab"
    .venv/bin/pip install --force-reinstall --no-deps mecab-python3==1.0.9
fi

# 4) Datos NLTK que la voz INGLESA (g2p_en) y g2pkk necesitan en runtime.
#    Van a .venv/nltk_data (está en la ruta de búsqueda de NLTK y dentro del proyecto).
#    Nota: nltk>=3.9 usa el nombre 'averaged_perceptron_tagger_eng'; se incluye
#    también el nombre antiguo por compatibilidad.
.venv/bin/python -m nltk.downloader -d .venv/nltk_data \
    averaged_perceptron_tagger_eng averaged_perceptron_tagger cmudict

# 5) Verificación final de convivencia
.venv/bin/python - <<'PY'
import MeCab, mecab
assert MeCab.Tagger() is not None
tokens = mecab.MeCab().pos("나는 사과를 먹지 않았다")
print("OK — MeCab (inglés/japonés) y mecab-ko (coreano) conviven aislados.")
print("     mecab-ko:", tokens[:4], "...")
PY

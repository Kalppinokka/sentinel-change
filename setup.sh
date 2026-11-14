#!/usr/bin/env bash
# Luo Python-ympäristön WSL:n kotihakemistoon ja asentaa riippuvuudet.
# Aja projektikansiossa: bash setup.sh
set -e
VENV="$HOME/.venvs/sentinel-change"
cd "$(dirname "$0")"
[ -d "$VENV" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r requirements.txt
"$VENV/bin/pip" list 2>/dev/null | grep -iE "rasterio|pystac|numpy|matplotlib"
echo
echo "Valmis. Ota ympäristö käyttöön:  source $VENV/bin/activate"

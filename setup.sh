#!/usr/bin/env bash
# Idempotent setup: creates venv, installs deps, vendors reverse-SynthID for Gemini mode.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

echo "==> Python venv"
if [ ! -d venv ]; then
    python3 -m venv venv
fi
# shellcheck disable=SC1091
source venv/bin/activate
pip install --upgrade pip >/dev/null
pip install -r requirements.txt

echo "==> Vendoring reverse-SynthID (Gemini mode)"
mkdir -p vendor
if [ ! -d vendor/reverse-SynthID ]; then
    git clone --depth 1 https://github.com/aloshdenny/reverse-SynthID.git vendor/reverse-SynthID
else
    git -C vendor/reverse-SynthID pull --ff-only || true
fi

CB="vendor/reverse-SynthID/artifacts/spectral_codebook_v3.npz"
if [ ! -f "$CB" ]; then
    echo "WARN: $CB yok — Gemini modu çalışmayacak."
fi

echo
echo "✓ Kurulum tamam."
echo "Çalıştırmak için:"
echo "    source venv/bin/activate && python app.py"
echo "Tarayıcıda: http://<vps-ip>:7860"

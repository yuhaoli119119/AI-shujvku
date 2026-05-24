#!/usr/bin/env bash
set -e

echo "=========================================="
echo " Lit AI Collector - PyInstaller Build"
echo "=========================================="
echo ""

pyinstaller LitAICollector.spec --clean --noconfirm

echo ""
echo "[SUCCESS] Build complete!"
echo "Output: dist/LitAICollector/"

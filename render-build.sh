#!/usr/bin/env bash
set -e

echo "🚀 Installing dependencies..."
pip install -r requirements.txt

echo "🌐 Installing Playwright Chromium..."
playwright install --with-deps chromium

echo "✅ Build complete!"

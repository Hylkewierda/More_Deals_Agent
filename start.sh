#!/bin/bash
# More Deals Agent – start script

cd "$(dirname "$0")"

# Check .env
if [ ! -f .env ]; then
  echo "⚠️  Geen .env bestand gevonden."
  echo "   Maak een .env bestand aan met:"
  echo "   echo 'ANTHROPIC_API_KEY=sk-ant-...' > .env"
  exit 1
fi

echo "🚀 More Deals Agent starten op http://localhost:8000"
echo "   (Ctrl+C om te stoppen)"
echo ""

python3 -m uvicorn app:app --host 0.0.0.0 --port 8000 --reload

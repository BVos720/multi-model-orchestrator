#!/usr/bin/env bash
# Automated setup for multi-model-orchestrator (macOS/Linux).
# Usage: ./scripts/setup.sh [--skip-ollama] [--ollama-model qwen2.5-coder:7b]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OLLAMA_MODEL="qwen2.5-coder:32b"
SKIP_OLLAMA=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-ollama) SKIP_OLLAMA=1; shift ;;
    --ollama-model) OLLAMA_MODEL="$2"; shift 2 ;;
    *) echo "unknown flag: $1"; exit 1 ;;
  esac
done

echo "== Disk space =="
df -h "$ROOT" | tail -1

echo "== Python =="
if ! command -v python3 >/dev/null; then
  echo "python3 not found - install it via your OS package manager (brew install python@3.12 / apt install python3.12), then re-run."
  exit 1
fi
python3 --version

echo "== Python environment =="
cd "$ROOT"
[ -d .venv ] || python3 -m venv .venv
./.venv/bin/pip install -e . --quiet
echo "installed orchestrator + dependencies into .venv"

echo "== Claude Code CLI =="
if command -v claude >/dev/null; then
  echo "claude CLI found: $(command -v claude)"
else
  echo "claude CLI not found. Install: npm install -g @anthropic-ai/claude-code, then run 'claude' once to log in."
fi

echo "== .env =="
if [ ! -f .env ]; then
  cp .env.example .env
  echo "created .env - add your GEMINI_API_KEY (free: https://aistudio.google.com/apikey)"
else
  echo ".env already exists, leaving it alone"
fi

if [ "$SKIP_OLLAMA" -eq 0 ]; then
  echo "== Ollama =="
  if ! command -v ollama >/dev/null; then
    echo "Ollama not found - install from https://ollama.com/download (brew install ollama on macOS)"
  else
    echo "ollama found: $(command -v ollama)"
    echo "== Pulling local model: $OLLAMA_MODEL =="
    ollama pull "$OLLAMA_MODEL"
  fi
else
  echo "== Ollama == (skipped)"
fi

echo "== Status =="
./.venv/bin/orchestrator status || true
echo
echo "Setup done. Try:  ./.venv/bin/orchestrator run \"write a fizzbuzz function\""

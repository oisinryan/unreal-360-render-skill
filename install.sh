#!/usr/bin/env sh
# Copies this skill into your Claude Code personal skills folder (~/.claude/skills/unreal-360-render).
set -e
DEST="${1:-$HOME/.claude/skills/unreal-360-render}"
SRC="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$DEST"
for item in SKILL.md README.md LICENSE scripts viewer references evals assets; do
  cp -R "$SRC/$item" "$DEST/"
done
find "$DEST" -name __pycache__ -type d -prune -exec rm -rf {} +
echo "installed to $DEST"

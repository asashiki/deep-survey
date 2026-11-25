#!/bin/sh
set -eu

REPO_DIR="$(cd -P "$(dirname "$0")" && pwd)"
chmod 755 "$REPO_DIR/bin/deep-survey"

mkdir -p "$HOME/.local/bin" "$HOME/.agents/skills" "$HOME/.claude/skills"
ln -sfn "$REPO_DIR/bin/deep-survey" "$HOME/.local/bin/deep-survey"
ln -sfn "$REPO_DIR/skills/deep-survey" "$HOME/.agents/skills/deep-survey"
ln -sfn "$HOME/.agents/skills/deep-survey" "$HOME/.claude/skills/deep-survey"

echo "Installed CLI   -> $HOME/.local/bin/deep-survey"
echo "Installed Skill -> $HOME/.agents/skills/deep-survey (linked to ~/.claude/skills/deep-survey)"
echo "Run 'deep-survey doctor' to verify channels."

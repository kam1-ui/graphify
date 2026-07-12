#!/usr/bin/env bash
# Committed PreToolUse(Bash) guard: refuse any command that would modify the
# system Python. Travels with the clone (wired in .claude/settings.json via
# ${CLAUDE_PROJECT_DIR}) so a fresh `git clone` is protected with zero setup.
# The sanctioned install path is `uv tool install` or a venv — never the
# system interpreter.
set -euo pipefail

cmd="$(jq -r '.tool_input.command // ""')"

deny() {
  jq -cn --arg r "$1" \
    '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:$r}}'
  exit 0
}

# Rule 1 (hard, unconditional): this flag exists only to override the distro's
# managed-Python protection. Never allowed.
if printf '%s' "$cmd" | grep -Eq -- '--break-system-packages'; then
  deny "Blocked by repo guard (.claude/hooks/block-system-pip.sh): --break-system-packages modifies the system Python. Install isolated instead: 'uv tool install <pkg>' for a CLI, or 'python -m venv .venv && .venv/bin/pip install <pkg>' for a project."
fi

# Rule 2 (conservative): a bare system-Python pip install/uninstall with no
# venv active and no venv/uv/--user marker in the command itself.
if printf '%s' "$cmd" | grep -Eq '(^|[^[:alnum:]_])(pip3?|python3?[[:space:]]+-m[[:space:]]+pip)[[:space:]]+(install|uninstall)'; then
  if [ -z "${VIRTUAL_ENV:-}" ] \
     && ! printf '%s' "$cmd" | grep -Eq 'uv[[:space:]]+(pip|tool)|uvx|--user|activate|venv/bin/|VIRTUAL_ENV='; then
    deny "Blocked by repo guard (.claude/hooks/block-system-pip.sh): this pip install/uninstall targets the system Python (no virtualenv active). Use 'uv tool install <pkg>' for a CLI, or 'python -m venv .venv && .venv/bin/pip install <pkg>' for a project. To use an existing venv, activate it in the same command."
  fi
fi

exit 0

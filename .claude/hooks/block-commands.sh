#!/bin/bash
# PreToolUse hook: blocks dangerous bash commands
# Exit 0 = allow, Exit 2 = block (with reason on stdout)

set -euo pipefail

INPUT=$(cat)
COMMAND=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tool_input',{}).get('command',''))" 2>/dev/null || echo "")

if [ -z "$COMMAND" ]; then
  exit 0
fi

# --- Privilege escalation ---
if echo "$COMMAND" | grep -qE '(^|\s|;|&&|\|\|)(sudo|su)\s'; then
  echo "BLOCKED: Privilege escalation (sudo/su) is not allowed"
  exit 2
fi

# --- Catastrophic deletes ---
if echo "$COMMAND" | grep -qE 'rm\s+-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*\s+(/|~|\.)($|\s)'; then
  echo "BLOCKED: Catastrophic delete (rm -rf / or ~ or .) is not allowed"
  exit 2
fi
if echo "$COMMAND" | grep -qE 'rm\s+-[a-zA-Z]*f[a-zA-Z]*r[a-zA-Z]*\s+(/|~|\.)($|\s)'; then
  echo "BLOCKED: Catastrophic delete (rm -fr / or ~ or .) is not allowed"
  exit 2
fi

# --- Destructive git operations ---
if echo "$COMMAND" | grep -qE 'git\s+reset\s+--hard'; then
  echo "BLOCKED: git reset --hard is destructive and not allowed"
  exit 2
fi
if echo "$COMMAND" | grep -qE 'git\s+push\s+.*--force|git\s+push\s+-f\b'; then
  echo "BLOCKED: git push --force is destructive and not allowed"
  exit 2
fi
if echo "$COMMAND" | grep -qE 'git\s+clean\s+-[a-zA-Z]*f'; then
  echo "BLOCKED: git clean -f is destructive and not allowed"
  exit 2
fi

# --- Reading .env via bash ---
if echo "$COMMAND" | grep -qE '(cat|less|more|head|tail|bat|vim|nano|vi|code|open)\s+.*\.env(\s|$|;)'; then
  echo "BLOCKED: Reading .env files via bash is not allowed — they contain secrets"
  exit 2
fi

# --- Dumping environment variables ---
if echo "$COMMAND" | grep -qE '(^|\s|;|&&|\|\|)(env|printenv)(\s*$|\s*;|\s*\||\s*&&|\s*\|\|)'; then
  echo "BLOCKED: Dumping all environment variables is not allowed"
  exit 2
fi

# --- System commands ---
if echo "$COMMAND" | grep -qE '(^|\s|;|&&|\|\|)(shutdown|reboot|halt|poweroff)\s'; then
  echo "BLOCKED: System shutdown/reboot commands are not allowed"
  exit 2
fi

# --- Reverse shells ---
if echo "$COMMAND" | grep -qE '(^|\s|;|&&|\|\|)(nc\s+-l|ncat|socat)\s'; then
  echo "BLOCKED: Reverse shell tools (nc -l, ncat, socat) are not allowed"
  exit 2
fi

# --- Writing .env via redirection ---
if echo "$COMMAND" | grep -qE '>{1,2}\s*\.env|>{1,2}\s*.*\/\.env'; then
  echo "BLOCKED: Writing to .env files via redirection is not allowed"
  exit 2
fi

# --- Killing the server ---
if echo "$COMMAND" | grep -qE '(kill|pkill|killall).*run\.py'; then
  echo "BLOCKED: Killing the ChatHub server (run.py) is not allowed"
  exit 2
fi

exit 0

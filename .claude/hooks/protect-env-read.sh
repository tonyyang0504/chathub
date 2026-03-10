#!/bin/bash
# PreToolUse hook: blocks Read tool on .env files
# Exit 0 = allow, Exit 2 = block (with reason on stdout)

set -euo pipefail

INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tool_input',{}).get('file_path',''))" 2>/dev/null || echo "")

if [ -z "$FILE_PATH" ]; then
  exit 0
fi

BASENAME=$(basename "$FILE_PATH")

# Block .env and .env.* files
if [[ "$BASENAME" == ".env" || "$BASENAME" == .env.* ]]; then
  echo "BLOCKED: Cannot read .env files — they contain secrets (SECRET_KEY, ENCRYPTION_KEY, API keys)"
  exit 2
fi

exit 0

#!/bin/bash
# PreToolUse hook: blocks Edit/Write to .env files (secrets protection)
# Other file protections handled by per-turn review feature
# Exit 0 = allow, Exit 2 = block (with reason on stdout)

set -euo pipefail

INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tool_input',{}).get('file_path',''))" 2>/dev/null || echo "")

if [ -z "$FILE_PATH" ]; then
  exit 0
fi

# Resolve to absolute path (handles ../ traversal)
RESOLVED=$(python3 -c "
import os, sys
p = sys.argv[1]
if not os.path.isabs(p):
    p = os.path.join(os.getcwd(), p)
print(os.path.normpath(p))
" "$FILE_PATH" 2>/dev/null || echo "$FILE_PATH")

BASENAME=$(basename "$RESOLVED")

# Block .env and .env.* files
if [[ "$BASENAME" == ".env" || "$BASENAME" == .env.* ]]; then
  echo "BLOCKED: Cannot modify .env files — they contain secrets (SECRET_KEY, ENCRYPTION_KEY)"
  exit 2
fi

exit 0

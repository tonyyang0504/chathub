#!/bin/bash
# PreToolUse hook: blocks Edit/Write to sensitive files
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

# Block .git/ internals
if [[ "$RESOLVED" == *"/.git/"* || "$RESOLVED" == *"/.git" ]]; then
  echo "BLOCKED: Cannot modify .git/ repository internals"
  exit 2
fi

# Block .claude/ config
if [[ "$RESOLVED" == *"/.claude/"* || "$RESOLVED" == *"/.claude" ]]; then
  echo "BLOCKED: Cannot modify .claude/ configuration files"
  exit 2
fi

# Block data/sessions/ (WhatsApp session data)
if [[ "$RESOLVED" == *"/data/sessions/"* || "$RESOLVED" == *"/data/sessions" ]]; then
  echo "BLOCKED: Cannot modify WhatsApp session data in data/sessions/"
  exit 2
fi

# Block data/backups/
if [[ "$RESOLVED" == *"/data/backups/"* || "$RESOLVED" == *"/data/backups" ]]; then
  echo "BLOCKED: Cannot modify database backups in data/backups/"
  exit 2
fi

# Block database files
if [[ "$BASENAME" == *.db || "$BASENAME" == *.db-journal || "$BASENAME" == *.db-wal || "$BASENAME" == *.db-shm ]]; then
  echo "BLOCKED: Cannot directly modify database files — use migrations or the application"
  exit 2
fi

exit 0

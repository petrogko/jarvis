#!/usr/bin/env bash
# Tripwire warning hook — fires on PostToolUse for Edit/Write/MultiEdit.
#
# Reads the Tool call JSON from stdin. If file_path matches one of
# the canonical 3 membrane files, prints a stderr advisory pointing
# the operator at the security-advisor persona. Never blocks. Never
# fails loud — exits 0 on every input, including malformed JSON.
#
# This is a discipline tripwire, not a hard gate. Branch protection
# + CI + the code-reviewer persona enforce the actual gate.

set -u

payload="$(cat)"

if ! command -v jq >/dev/null 2>&1; then
    exit 0
fi
file_path="$(printf '%s' "$payload" | jq -r '.tool_input.file_path // empty' 2>/dev/null)"
if [ -z "$file_path" ]; then
    exit 0
fi

basename="$(basename "$file_path")"
case "$basename" in
    SECURITY.md|ARCHITECTURE.md|auth.py)
        cat >&2 <<EOF
⚠  Edited $basename — this is a membrane file.
⚠  If this change touched the trust model or auth contract, the
⚠  security-advisor persona should review BEFORE merge. Invoke with:
⚠      Agent(subagent_type='security-advisor')
⚠  This is a tripwire, not a block — discipline lives in CLAUDE.md.
EOF
        ;;
esac

exit 0

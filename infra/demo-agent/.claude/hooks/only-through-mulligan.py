#!/usr/bin/env python3
"""PreToolUse hook for a demo laptop's Claude Code: the only way to the company's systems is
Mulligan's MCP tools. Blocks shell commands and web fetches that reach the databases, GitHub or
Notion directly, and explains why. Reads the hook input on stdin; exit 2 blocks the call."""

import json
import re
import sys

BLOCKED = [
    (r"\bpsql\b|\bpg_dump\b|\bpg_restore\b|postgres(ql)?://", "direct database access"),
    (r":5433\b|:5434\b|\bprod-db\b|\bmulligan-db\b", "direct database access"),
    (r"docker\s+(compose\s+)?exec\b", "shelling into the stack's containers"),
    (r"\bgh\s+(api|issue|pr|repo|release)\b|api\.github\.com", "direct GitHub access"),
    (r"api\.notion\.com", "direct Notion access"),
    (r"\b(curl|wget|http|httpie)\b.*(127\.0\.0\.1|localhost):8000", "Mulligan's REST API"),
    (r"\.env\b", "reading credentials"),
]

event = json.load(sys.stdin)
tool_input = event.get("tool_input") or {}
text = " ".join(str(tool_input.get(k, "")) for k in ("command", "url", "prompt"))
for pattern, what in BLOCKED:
    if re.search(pattern, text, flags=re.IGNORECASE):
        print(
            f"Blocked: {what}. On this machine, work on the company's systems only through the "
            "Mulligan MCP tools (mcp__mulligan__*), which record and can undo every change.",
            file=sys.stderr,
        )
        sys.exit(2)
sys.exit(0)

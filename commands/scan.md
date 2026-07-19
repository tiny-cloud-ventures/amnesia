---
description: Audit Claude Code's memory for contradictions, stale facts, and duplicates
allowed-tools: Bash(python3:*), Read
---

Run the amnesia memory audit and tell the user what it found, in plain language.

1. Run `python3 "${CLAUDE_PLUGIN_ROOT}/amnesia.py" analyze` with a 10-minute Bash timeout — it feeds the whole memory store through the user's own `claude` CLI, so it takes a few minutes. If it fails because the `claude` CLI is missing from PATH, say so and stop.
2. Read `~/.claude/amnesia/analysis.json`.
3. Summarize the findings as short human sentences — "Two memories disagree about which port X runs on", not filenames or JSON. Lead with the count: contradictions first, then stale facts, then duplicates and misfiled memories. If there is nothing to report, say the memory store looks clean and stop.
4. Close with: run `/amnesia:open` to review and fix them one at a time (fixes are one click and reversible — everything goes to trash, never deleted).

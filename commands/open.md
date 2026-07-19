---
description: Open the amnesia memory UI in the browser
allowed-tools: Bash(python3:*), Bash(curl:*), Bash(open:*), Bash(xdg-open:*)
---

Open the amnesia memory UI and hand the user the link. Run each step as a plain foreground command — never append `&` and never use background execution.

1. `python3 "${CLAUDE_PLUGIN_ROOT}/amnesia.py" --detach` — starts the server in its own session so it outlives this one; harmless no-op if it is already running.
2. `curl -s -o /dev/null -w '%{http_code}' --retry 10 --retry-delay 1 --retry-all-errors --max-time 2 http://localhost:8780/` — wait for `200`.
3. `open http://localhost:8780` (macOS) or `xdg-open http://localhost:8780` (Linux).

Then tell the user, in one sentence, that their memory UI is at http://localhost:8780 — nothing else. Do not summarize what amnesia is or list its features.

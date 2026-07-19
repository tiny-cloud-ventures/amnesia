# Changelog

## v0.1.0 — 2026-07-19

First release. Give your agent selective amnesia: see, search, and clean every memory Claude Code has saved about you — and let Claude audit its own memory for contradictions.

**The problem.** Claude Code quietly accumulates memory files under `~/.claude/projects/*/memory/`. Over months the store rots: stale facts, the same rule saved three times, memories from one project leaking into another. Polluted memory measurably makes agents worse than no memory at all — and until now there was no way to even see it.

**What's in the box:**

- **One-glance verdict** — open it and get one sentence: how many memories, across how many projects, and what just indexing them costs in tokens. One button: **Scan**.
- **Claude audits itself** — the scan feeds your whole store through your own `claude` CLI (the subscription you already have — no API key) and flags contradictions, stale facts, duplicates, and misfiled memories.
- **Review, one question at a time** — plain-language flags with one-word answers: Forget, Move it, Combine them, Keep both. Or **Fix all** to apply every confident consolidation in one click.
- **The memory map** — your agent's brain as a live constellation: every memory a dot, colors are projects, lines are real cross-references, contradictions glow red. Hand-rolled canvas physics, zero dependencies. Drag it, click a dot to inspect or forget.
- **Everything reversible** — nothing is ever deleted; forgets, moves, and merges are file moves into `~/.claude/memory-trash/` with undo in the toast, and `MEMORY.md` indexes stay in sync.
- **Claude Code plugin** — `/amnesia:open` starts the UI, `/amnesia:scan` audits without leaving your session.
- **Nothing leaves your machine** — single Python file, stdlib only, binds to localhost, no accounts, no telemetry.

**Install:**

```
/plugin marketplace add tiny-cloud-ventures/amnesia
/plugin install amnesia@amnesia
```

or `uvx --from git+https://github.com/tiny-cloud-ventures/amnesia amnesia`, or just download `amnesia.py` and run it.

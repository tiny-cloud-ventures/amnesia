# memclean

**See, search, and clean every memory Claude Code has saved about you — and let Claude audit its own memory for contradictions.**

Claude Code quietly accumulates memory files per project directory under `~/.claude/projects/*/memory/`. Over months that store rots: facts go stale ("service X is live" — it was retired weeks ago), the same rule gets saved three times in three projects, and memories from one project leak into sessions for another. Research on agent memory calls this *memory contamination*, and polluted memory measurably makes agents worse than no memory at all.

memclean gives you:

- **One UI for all of it** — every memory across every project directory, searchable, grouped by project, with type badges and dates.
- **One-click delete, zero-risk** — deleted memories move to `~/.claude/memory-trash/` (restore = `mv` it back) and the project's `MEMORY.md` index is scrubbed to match.
- **A self-audit** — `memclean analyze` feeds your whole memory store to your own `claude` CLI and gets back a structured report of **contradictions**, **stale/superseded facts**, and **duplicates**, rendered in the UI with click-to-jump links to the offending memories.

No API key. No server. No telemetry. Single file, Python stdlib only. The only thing that ever reads your memories is the Claude account you already use.

## Install

Requires Python 3.9+ and (for `analyze`) the [Claude Code](https://claude.com/claude-code) CLI.

```sh
# run it directly
uvx --from git+https://github.com/OWNER/memclean memclean

# or the zero-tooling way
curl -O https://raw.githubusercontent.com/OWNER/memclean/main/memclean.py
python3 memclean.py
```

## Use

```sh
memclean            # UI at http://localhost:8780
memclean analyze    # audit memories via your claude CLI, then refresh the UI
memclean 9000       # different port
```

Open the UI, search for anything ("postgres", a project name, a date), read the full body of any memory, delete what's wrong. Run `analyze` and the top of the page shows what Claude itself thinks is contradictory, stale, or duplicated — click a file chip to jump to that memory.

## Why this matters

Unscoped, stale memory is negative-value: an agent that "remembers" your old port number or a retired service confidently applies it to today's work. The fix isn't remembering more — it's forgetting and fencing correctly. memclean is the smallest honest tool for that: full visibility, safe deletion, and an LLM audit of the store's internal consistency.

## Roadmap

- Apply mode: approve suggested merges/moves from the analysis, not just deletes
- Scope fencing: flag project-specific facts saved in the global (home) memory dir
- Graph view: entity/supersession graph across projects ("X replaced Y on date Z")

## License

MIT

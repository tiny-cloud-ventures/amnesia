# amnesia

**Give your agent selective amnesia. See, search, and clean every memory Claude Code has saved about you — and let Claude audit its own memory for contradictions.**

Claude Code quietly accumulates memory files per project directory under `~/.claude/projects/*/memory/`. Over months that store rots: facts go stale ("service X is live" — it was retired weeks ago), the same rule gets saved three times in three projects, and memories from one project leak into sessions for another. Research on agent memory calls this *memory contamination*, and polluted memory measurably makes agents worse than no memory at all.

amnesia is one sentence, one button, one question at a time:

1. Open it: *"Your agent remembers 86 things about you, across 21 projects."* One button: **Scan**.
2. The scan feeds your whole store to your own `claude` CLI and flags **contradictions**, **stale facts**, **duplicates**, and **misfiled memories**.
3. **Review** walks you through the flags one at a time, in plain language — *"These can't both be true"*, *"This seems filed in the wrong project"* — with one-word answers: Forget, Move it, Combine them, Keep both.
4. Forgetting is one click and reversible: files move to `~/.claude/memory-trash/`, undo is right there in the toast, and the `MEMORY.md` index stays in sync.

Search and a browse-everything view exist for when you want them — they're one click away, never the default.

No API key. No server. No telemetry. Single file, Python stdlib only. The only thing that ever reads your memories is the Claude account you already use.

## Install

Requires Python 3.9+ and (for `analyze`) the [Claude Code](https://claude.com/claude-code) CLI.

```sh
# run it directly
uvx --from git+https://github.com/tiny-cloud-ventures/amnesia amnesia

# or the zero-tooling way
curl -O https://raw.githubusercontent.com/tiny-cloud-ventures/amnesia/main/amnesia.py
python3 amnesia.py
```

## Use

```sh
amnesia            # UI at http://localhost:8780 — scan, review, done
amnesia analyze    # same audit, from the terminal
amnesia 9000       # different port
```

## Why this matters

Unscoped, stale memory is negative-value: an agent that "remembers" your old port number or a retired service confidently applies it to today's work. The fix isn't remembering more — it's forgetting and fencing correctly. amnesia is the smallest honest tool for that: full visibility, safe deletion, and an LLM audit of the store's internal consistency.

The analyzer also proposes **consolidation ops** — MOVE a memory to the project directory it actually belongs to, MERGE a duplicate into its canonical copy — each applied with one click, trash-backed. Cross-repo consolidation with human approval is the core idea: every mainstream memory system (ChatGPT, Claude.ai, Mem0, Zep) consolidates silently; none lets you review the merge. See `docs/` for the research this design is based on.

## Roadmap

- Bulk select + archive state (soft-disable a memory and see if anything breaks before deleting)
- Token-count column — show what each memory costs your context window every session
- Supersede-don't-delete: rewrite stale facts in place with a `superseded:` marker instead of trashing
- Gated PROMOTE op: hoist a fact to global scope only when observed in 2+ repos, with provenance
- Graph view: entity/supersession graph across projects ("X replaced Y on date Z")

## License

MIT

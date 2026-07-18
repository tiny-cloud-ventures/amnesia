# Survey: memory-management UX in existing AI agent tools (July 2026)

How existing tools present, search, and delete agent memories — and where the gaps are. Compiled to ground amnesia's design.

## Per-tool findings

### Basic Memory (basicmachines)
No UI in the local OSS version — CLI + plain Markdown, designed to be opened in Obsidian. A cloud-only web app has a three-pane layout (project selector + folder tree, date-grouped note list, Markdown editor), Cmd+K full-text search defaulting to all-projects. Removal = filesystem semantics, no trash/undo. No dedup/consolidation. Cross-project *search* exists; cross-project *consolidation* does not.
[GitHub](https://github.com/basicmachines-co/basic-memory) · [Web app docs](https://docs.basicmemory.com/cloud/web-app)

### Beads (steveyegge)
No UI shipped (`bd` CLI, git/Dolt-backed), but a large community UI ecosystem (TUIs, kanban dashboards, DAG visualizers). Its "compaction" — semantic memory decay summarizing old closed tasks — is the closest thing to automated consolidation anywhere in this survey. Per-repo by design; consolidation not reviewable in a UI.
[GitHub](https://github.com/steveyegge/beads) · [community tools](https://github.com/steveyegge/beads/blob/main/docs/community-tools.md)

### RAG Memory MCP / official MCP Knowledge Graph Memory server
No UI from maintainers; JSONL/SQLite manipulated via MCP tool calls or hand-editing. A GUI is an open feature request ([modelcontextprotocol/servers#2393](https://github.com/modelcontextprotocol/servers/issues/2393)); third-party viewers (MemViz, claude-memory-viz) are read-only. No dedup.
[official server](https://github.com/modelcontextprotocol/servers/tree/main/src/memory)

### OpenMemory / Mem0 dashboard — the most complete removal UI surveyed
Web table of memories: content, source app, auto category tags, date, state (active/paused/archived). Debounced live search, filter panel, sortable columns. Per-row archive/pause/delete plus **checkbox multi-select with bulk ops**. Pause/archive act as soft alternatives to delete — but no undo/trash. Mem0's write-time inference dedupes silently; the dashboard exposes **no reviewable consolidation and no contradiction surfacing**. Scoped by user/app, not repo. OpenMemory (the local variant) is being sunset.
[operations docs](https://docs.mem0.ai/core-concepts/memory-operations) · [walkthrough](https://dev.to/anmolbaranwal/how-to-make-your-clients-more-context-aware-with-openmemory-mcp-4h71)

### Zep / Graphiti
Graphiti OSS bundles no UI (use Neo4j Browser); Zep Cloud has a D3 graph explorer ([reference implementation](https://github.com/getzep/zep-graph-visualization)). The killer design choice is in the data model: **contradiction handling is automatic invalidation, not deletion** — a contradicting fact sets `invalid_at` on the old edge, preserving history. Undo-shaped, but data-model-level, never a user affordance.
[Graphiti](https://github.com/getzep/graphiti) · [facts docs](https://help.getzep.com/facts)

### Letta (MemGPT) ADE
Full web/desktop IDE. Core memory = labeled editable text blocks; archival memory = searchable passage list with per-passage delete. **The context-window viewer** — showing what actually loads into the agent and its token cost — is the best cleanup motivator in any tool. Dedup/consolidation is an open feature request ([letta#3116](https://github.com/letta-ai/letta/issues/3116)) — a strong demand signal.
[ADE overview](https://docs.letta.com/guides/ade/overview)

### Claude Code GUI wrappers (opcode)
Has a CLAUDE.md management screen with a **cross-project scanner** — but edit-only, CLAUDE.md-only, and no support for the `~/.claude/projects/*/memory/` auto-memory dirs at all.
[opcode](https://github.com/winfunc/opcode)

## Patterns worth copying

1. **OpenMemory's table + bulk-select + filters** — proven "inbox triage" ergonomics for memory cleanup.
2. **Pause/archive as graduated states before delete** — soft-disable and see if anything breaks.
3. **Zep's invalidate-don't-delete model** — supersession with timestamps gives undo, audit trail, and contradiction history for free.
4. **opcode's cross-project scanner** as the first screen — extended past CLAUDE.md to memory dirs.
5. **Letta's context-window/token-cost view** — showing what a memory *costs per session* motivates cleanup better than any list.

## Gaps nobody covers

- **Consolidation across project directories** — literally no one. "Same fact recorded in 4 project memories → merge or hoist to global" is an empty niche.
- **Contradiction surfacing for human review** — Zep and Mem0 both resolve contradictions silently. Nobody shows "memory A says X, memory B says not-X — pick one."
- **Safe deletion with undo/trash** — absent everywhere as a UI feature.
- **File-based memory management generally** — all the good UIs sit on databases; the markdown world Claude Code lives in has only an editor.

**Competitive verdict:** no existing tool does removal + consolidation across repos. A local web UI scanning `~/.claude/projects/*/memory/`, triage-table UX, contradiction review, and trash-backed deletion is currently uncontested.

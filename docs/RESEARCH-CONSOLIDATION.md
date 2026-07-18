# How mainstream AI systems consolidate memory (July 2026)

The merge/supersede/promote mechanics behind ChatGPT, Claude.ai, Mem0, Letta, and Zep — and what they imply for a review-and-approve consolidation tool.

## ChatGPT
Manage-memories UI is a flat list with per-item delete and "clear all" — no search, no inline edit. Behind the scenes: discrete saved facts (the `bio` tool) plus **opaque batch-regenerated summary layers** distilled from hundreds of conversations, rewritten wholesale on a schedule. OpenAI states ChatGPT can "update, combine, or remove" memories on its own. Documented weakness: the batch summaries accumulate stale facts because nothing detects that plans changed.
[Memory FAQ](https://help.openai.com/en/articles/8590148-memory-faq) · [reverse-engineering](https://www.shloked.com/writing/chatgpt-memory-bitter-lesson)

## Claude.ai
Memories organized by category with an **editable summary as the user-facing artifact** (instruction-based editing), pause vs reset, incognito exclusion. Scoping: **one separate memory per project** plus an overarching memory — hard fencing pitched as a confidentiality guardrail. The consolidated artifact is itself the editable object (vs ChatGPT's hidden layers).
[Help Center](https://support.claude.com/en/articles/11817273-use-claude-s-chat-search-and-memory-to-build-on-previous-context)

## Mem0 — ADD / UPDATE / DELETE / NOOP
Two-phase pipeline: extract candidate facts, then vector-match each against similar existing memories and have an LLM pick an op. Contradiction resolution in the flat store is literally "delete the loser" — history destroyed. The graph variant is softer: conflicts **mark relationships invalid rather than deleting**.
[Mem0 paper](https://arxiv.org/html/2504.19413v1) · [docs](https://docs.mem0.ai/core-concepts/memory-operations/add)

## Letta — sleep-time compute
A background agent with exclusive memory-editing tools (`memory_insert`, `memory_replace`, `memory_rethink`) consolidates while the primary agent is idle: dedupe every run, light consolidation at session end, full reorganization weekly. Community-documented failure mode: **over-consolidation** (losing granularity).
[Sleep-time compute](https://www.letta.com/blog/sleep-time-compute/) · [best practices](https://forum.letta.com/t/sleeptime-agents-for-memory-consolidation-best-practices-guide/154)

## Graphiti / Zep — edge invalidation
Bi-temporal model (`t_valid`/`t_invalid` + `t_created`/`t_expired`). On contradiction, the old edge's `t_invalid` is set to the new edge's `t_valid` — **a temporal boundary, not a deletion**; full history stays queryable. The cleanest supersede-don't-delete design in production OSS.
[Zep paper](https://arxiv.org/html/2501.13956v1) · [Graphiti](https://github.com/getzep/graphiti)

## Cross-scope research
- **MemGuard** (arXiv 2605.28009): contamination prevention via atomic knowledge units in type-isolated stores.
- **A-MemGuard** (arXiv 2510.02373): consensus validation — memories checked against the agent's own history; isolated audits miss ~66% of poisoned entries.
- **LangMem/Foundry promotion**: procedural memories promoted to durable scope, demoted when stale.
- **Claude Code natively**: memory fenced per project dir, `~/.claude/CLAUDE.md` the only manual global layer — cross-repo insights stay stranded where they were learned.

## The converged operation vocabulary

| Operation | Meaning | Used by |
|---|---|---|
| ADD | genuinely new fact | Mem0, ChatGPT |
| UPDATE / merge | rewrite existing memory to absorb new info | Mem0, OpenAI, Letta |
| SUPERSEDE / invalidate | contradiction → old fact timestamped invalid, kept | Zep/Graphiti, Mem0 graph |
| DELETE / evict | hard removal — user request or compliance only | all user UIs |
| NOOP | duplicate/irrelevant | Mem0 |
| RETHINK / reorganize | wholesale block rewrite | Letta, ChatGPT batch |
| PROMOTE / demote | rescope wider/narrower | LangMem/Foundry |
| ARCHIVE / decay | out of hot context | Letta, Beads |

## The gap amnesia targets

Every mainstream system **auto-applies consolidation silently** and offers at most after-the-fact deletion. Nobody does review-and-approve — and ChatGPT's stale batch summaries are exactly the failure human review catches. Design consequences adopted here:

1. **Typed op plan, applied as a reviewable diff** — never auto-applied.
2. **Supersede over silent delete** — contradiction losers get marked/archived, not erased; hard delete stays a human-only op (trash-backed).
3. **PROMOTE is first-class and always gated** — repo memories never merge across repos automatically; promotion wants the same fact observed in ≥2 repos, human approval, and a provenance line so a bad global memory can be traced and demoted.

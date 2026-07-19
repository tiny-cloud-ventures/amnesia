# Contributing to amnesia

Thanks for wanting to help. amnesia is deliberately small — one Python file, stdlib only, zero dependencies — and contributions that keep it that way merge fast.

## Ground rules

- **Zero dependencies is the product.** PRs that add a package, a framework, or a build step will be declined, however good the library is. The whole point is `curl` + `python3` and you're running.
- **One file.** `amnesia.py` is the entire program: server, UI (inline HTML/CSS/JS), analyzer, self-check. Resist the urge to split it.
- **The default view stays calm.** One sentence, one button, one question at a time. Density lives behind links, never on the home screen, and the UI speaks human sentences — no filenames or jargon where a plain sentence exists.
- **Everything reversible.** Any operation that touches a memory file must be trash-backed — move to `~/.claude/memory-trash/`, never unlink — and must keep the project's `MEMORY.md` index in sync.

## Dev loop

```sh
python3 amnesia.py --check       # self-check — must pass before and after your change
python3 amnesia.py 8790          # run on a scratch port
```

To test against fixture memories instead of your real store, point `HOME` somewhere fake — every path (memories, trash, analysis) resolves under it, so destructive flows are safe to exercise:

```sh
mkdir -p /tmp/fakehome/.claude/projects/-my-proj/memory
# drop some .md memory files in there, then:
HOME=/tmp/fakehome python3 amnesia.py 8790
```

For UI changes, headless Chrome gives you a settled screenshot (the virtual-time budget fast-forwards the map physics):

```sh
chrome --headless=new --screenshot=ui.png --window-size=1440,900 \
  --virtual-time-budget=14000 http://localhost:8790
```

## Sending a change

1. Keep the diff as small as it can honestly be.
2. `python3 amnesia.py --check` must pass. If you added non-trivial logic, add one assert for it to `_check()` — the smallest thing that fails if your logic breaks.
3. UI change? Include a before/after screenshot in the PR.
4. Describe what and why in a paragraph. That's it.

## Reporting bugs

Open an issue with what you did, what you expected, and what happened. **Never paste your real memory contents into an issue** — memory files often hold private infrastructure details, tokens, and paths. Reproduce with fixture memories under a fake `HOME` instead.

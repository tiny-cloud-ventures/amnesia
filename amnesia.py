#!/usr/bin/env python3
"""amnesia — view, search, clean, and sanity-check your Claude Code memory.

Local-only, zero dependencies. The analyzer runs on your own `claude` CLI,
so it needs no API key and nothing leaves your machine except via your
existing Claude account.

  amnesia            serve the UI on http://localhost:8780
  amnesia analyze    audit memories for contradictions, stale facts, duplicates
  amnesia --check    run self-tests

Deleted memories move to ~/.claude/memory-trash/<project>/ — restore with mv.
"""
import json, re, shutil, subprocess, sys, threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

ROOT = Path.home() / ".claude" / "projects"
TRASH = Path.home() / ".claude" / "memory-trash"
ANALYSIS = Path.home() / ".claude" / "amnesia" / "analysis.json"


def parse_frontmatter(text):
    meta = {}
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            for line in parts[1].splitlines():
                m = re.match(r"\s*(name|description|type):\s*(.+)", line)
                if m:
                    meta[m.group(1)] = m.group(2).strip()
            return meta, parts[2].strip()
    return meta, text


def memory_files(root):
    for mdir in sorted(root.glob("*/memory")):
        for f in sorted(mdir.glob("*.md")):
            if f.name != "MEMORY.md":
                yield mdir.parent.name, f


def list_memories(root=None):
    out = []
    for project, f in memory_files(root or ROOT):
        meta, body = parse_frontmatter(f.read_text(errors="replace"))
        out.append({
            "project": project,
            "file": f.name,
            "name": meta.get("name", f.stem),
            "description": meta.get("description", ""),
            "type": meta.get("type", ""),
            "body": body,
            "mtime": f.stat().st_mtime,
        })
    return out


def delete_memory(project, filename, root=None, trash=None):
    root = root or ROOT
    trash = trash or TRASH
    # trust boundary: request body names the file; never let it escape the memory dir
    if "/" in project or "/" in filename or ".." in project or ".." in filename:
        raise ValueError("bad path")
    src = root / project / "memory" / filename
    if not src.is_file():
        raise FileNotFoundError(filename)
    dest = trash / project
    dest.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest / filename))
    index = src.parent / "MEMORY.md"
    if index.is_file():
        lines = index.read_text().splitlines(keepends=True)
        index.write_text("".join(l for l in lines if f"({filename})" not in l))


def restore_memory(project, filename, root=None, trash=None):
    root = root or ROOT
    trash = trash or TRASH
    # trust boundary: same validation as delete_memory
    if "/" in project or "/" in filename or ".." in project or ".." in filename:
        raise ValueError("bad path")
    src = trash / project / filename
    if not src.is_file():
        raise FileNotFoundError(filename)
    mdir = root / project / "memory"
    if (mdir / filename).exists():
        raise ValueError("exists: " + filename)
    mdir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(mdir / filename))
    _index_add(mdir, filename)


def _split_ref(ref):
    project, sep, filename = ref.partition("/")
    if not sep or not project or not filename or "/" in filename or ".." in project or ".." in filename:
        raise ValueError("bad ref: " + ref)
    return project, filename


def _index_add(mdir, filename):
    meta, _ = parse_frontmatter((mdir / filename).read_text(errors="replace"))
    line = f"- [{meta.get('name', Path(filename).stem)}]({filename}) — {meta.get('description', '')}\n"
    index = mdir / "MEMORY.md"
    text = index.read_text() if index.is_file() else "## Memory Index\n"
    index.write_text(text.rstrip("\n") + "\n" + line)


def apply_op(op, src, dest, root=None, trash=None):
    root = root or ROOT
    sproj, sfile = _split_ref(src)
    spath = root / sproj / "memory" / sfile
    if not spath.is_file():
        raise FileNotFoundError(src)
    if op == "move":
        if not dest or "/" in dest or ".." in dest:
            raise ValueError("bad dest: " + dest)
        ddir = root / dest / "memory"
        if (ddir / sfile).exists():
            raise ValueError(f"exists: {dest}/{sfile}")
        ddir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(spath), str(ddir / sfile))
        index = spath.parent / "MEMORY.md"
        if index.is_file():
            lines = index.read_text().splitlines(keepends=True)
            index.write_text("".join(l for l in lines if f"({sfile})" not in l))
        _index_add(ddir, sfile)
    elif op == "merge":
        dproj, dfile = _split_ref(dest)
        dpath = root / dproj / "memory" / dfile
        if not dpath.is_file():
            raise FileNotFoundError(dest)
        _, body = parse_frontmatter(spath.read_text(errors="replace"))
        dpath.write_text(dpath.read_text().rstrip("\n") + f"\n\n**Merged from {src} by amnesia:** {body}\n")
        delete_memory(sproj, sfile, root, trash)
    else:
        raise ValueError("unknown op: " + op)


# ---------- analyzer (runs on the user's own `claude` CLI) ----------

ANALYZE_PROMPT = """You are auditing a user's Claude Code memory files for hygiene problems.
Below are all their memory files, each preceded by a header line `=== <project-dir>/<filename> ===`.

Find, being concrete and citing files:
1. contradictions — memories stating incompatible facts (project statuses, ports, hosts, "X replaced Y" vs "Y is live", tool availability). Quote both claims in `detail`.
2. stale — claims that a later memory supersedes; name the superseding file in `detail`.
3. duplicates — the same fact stored in multiple files; say in `detail` which file should be canonical.
4. ops — concrete consolidation operations fixing what you found, each one of:
   - {"op": "move", "from": "<dir>/<file.md>", "to": "<other-dir>", "reason": "<one line>"} — memory saved in the wrong project directory: a fact about repo X saved elsewhere, or a global user preference buried in one project's dir. Move it to the directory whose sessions need it.
   - {"op": "merge", "from": "<dir>/<file.md>", "to": "<dir2>/<file2.md>", "reason": "<one line>"} — duplicate or subset memory folded into the canonical one (from's body is appended to to, then from is trashed).
   Only propose ops you are confident about; when unsure, describe the issue in categories 1-3 instead.

Return ONLY a JSON object, no prose before or after:
{"contradictions": [{"title": "<one line>", "detail": "<2-3 sentences>", "files": ["<dir>/<file.md>", ...]}],
 "stale": [ ...same shape... ],
 "duplicates": [ ...same shape... ],
 "ops": [{"op": "move|merge", "from": "<dir>/<file.md>", "to": "<dir> or <dir>/<file.md>", "reason": "<one line>"}]}

MEMORY FILES:

"""


def build_dump(root=None):
    parts = []
    for project, f in memory_files(root or ROOT):
        parts.append(f"=== {project}/{f.name} ===\n{f.read_text(errors='replace')}")
    return "\n\n".join(parts)


def parse_findings(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON object in model output")
    data = json.loads(m.group(0))
    return {k: data.get(k) or [] for k in ("contradictions", "stale", "duplicates", "ops")}


def analyze(root=None, out=None):
    dump = build_dump(root)
    count = len(re.findall(r"^=== ", dump, re.M))
    if not count:
        sys.exit("no memory files found under " + str(root or ROOT))
    print(f"auditing {count} memories with your claude CLI (can take a few minutes)...")
    try:
        r = subprocess.run(["claude", "-p", "--output-format", "json"],
                           input=ANALYZE_PROMPT + dump,
                           capture_output=True, text=True, timeout=1800)
    except FileNotFoundError:
        sys.exit("`claude` CLI not found — install Claude Code first (https://claude.com/claude-code)")
    if r.returncode != 0:
        sys.exit(f"claude CLI failed: {(r.stderr or r.stdout).strip()[:500]}")
    findings = parse_findings(json.loads(r.stdout).get("result", ""))
    out = out or ANALYSIS
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(findings, indent=2))
    n = sum(len(v) for v in findings.values())
    print(f"{n} findings written to {out} — open the amnesia UI to review")
    return findings


A_STATE = {"running": False, "error": None}


def start_analyze():
    if A_STATE["running"]:
        return
    A_STATE.update(running=True, error=None)

    def run():
        try:
            analyze()
        except BaseException as e:  # analyze() exits via sys.exit on CLI errors
            A_STATE["error"] = str(e) or "analyze failed"
        finally:
            A_STATE["running"] = False

    threading.Thread(target=run, daemon=True).start()


# ---------- web UI ----------

PAGE = """<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>amnesia</title>
<style>
:root { color-scheme: light dark;
  --bg: light-dark(#f6f6f8, #101014);
  --card: light-dark(#ffffff, #1a1a20);
  --border: light-dark(#e4e4e9, #2a2a33);
  --text: light-dark(#1c1c22, #e9e9ee);
  --muted: light-dark(#70707e, #9a9aa8);
  --accent: light-dark(#5b5bd6, #8b8bf5);
  --danger: #e5484d; --ok: #30a46c; --warn: #d9822b; --info: #3d84e0;
}
* { box-sizing: border-box; }
body { font: 15px/1.55 -apple-system, "Segoe UI", sans-serif; margin: 0;
  background: var(--bg); color: var(--text);
  min-height: 100vh; display: flex; flex-direction: column; }
code { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: .85em;
  background: light-dark(#ececf1, #26262e); padding: .1em .35em; border-radius: 5px; }
header { position: sticky; top: 0; z-index: 5; border-bottom: 1px solid var(--border);
  background: color-mix(in srgb, var(--bg) 82%, transparent); backdrop-filter: blur(10px); }
.bar { max-width: 960px; margin: 0 auto; padding: .65rem 1rem;
  display: flex; gap: 1rem; align-items: center; flex-wrap: wrap; }
.wm { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 1.15rem; font-weight: 600; cursor: pointer; }
.wm i { font-style: normal; }
.wm i:nth-of-type(1){opacity:.75} .wm i:nth-of-type(2){opacity:.55}
.wm i:nth-of-type(3){opacity:.35} .wm i:nth-of-type(4){opacity:.18}
#q { flex: 1; min-width: 220px; padding: .45rem .9rem; font-size: .95rem; color: var(--text);
  border: 1px solid var(--border); border-radius: 99px; background: var(--card); outline: none; }
#q:focus { border-color: var(--accent); }
#score { font-size: .8rem; color: var(--ok); white-space: nowrap; }
main, footer { position: relative; z-index: 1; }
main { max-width: 960px; margin: 0 auto; padding: 1rem; }
#cv { position: fixed; inset: 0; width: 100vw; height: 100vh; z-index: 0; display: none; }
body[data-view=home] #cv { display: block; opacity: .4; pointer-events: none; }
body[data-view=map] #cv { display: block; cursor: grab; }
body[data-view=map] footer { display: none; }
.mapbar { position: fixed; top: 3.9rem; left: 0; right: 0; text-align: center;
  color: var(--muted); font-size: .84rem; }
.mapbar a { color: var(--accent); cursor: pointer; margin-left: .8rem; }
.legend { position: fixed; bottom: 1.2rem; left: 0; right: 0; text-align: center;
  font-size: .74rem; color: var(--muted); }
.legend i { display: inline-block; width: 15px; height: 3px; border-radius: 2px;
  vertical-align: middle; margin: 0 .35rem 0 1rem; }
#tip { position: fixed; z-index: 4; background: var(--card); border: 1px solid var(--border);
  border-radius: 8px; padding: .3rem .7rem; font-size: .8rem; pointer-events: none;
  max-width: 320px; box-shadow: 0 4px 18px #0004; }
#tip .proj-tag { margin-left: .4rem; }
#ncard { position: fixed; z-index: 4; left: 50%; transform: translateX(-50%); bottom: 4.2rem;
  width: min(400px, 92vw); background: var(--card); border: 1px solid var(--border);
  border-radius: 14px; padding: 1rem 1.2rem; box-shadow: 0 12px 44px #0006; }
#ncard .t { font-weight: 600; }
#ncard .qbtns { justify-content: flex-start; margin-top: .8rem; }
.home { text-align: center; padding: 3.2rem 1rem 1.5rem; }
.big { font-size: 1.45rem; line-height: 1.45; max-width: 34rem; margin: 0 auto; font-weight: 400; }
.big b { color: var(--accent); }
.sub { color: var(--muted); margin: .9rem auto 1.8rem; max-width: 30rem; }
.sub b { color: var(--text); }
.sub.err { color: var(--danger); }
button.primary { background: var(--accent); color: #fff; border: none; border-radius: 99px;
  padding: .65rem 1.7rem; font-size: 1rem; cursor: pointer; }
button.primary:disabled { opacity: .55; cursor: default; }
button.ghost { background: none; border: 1px solid var(--border); color: var(--muted);
  border-radius: 99px; padding: .6rem 1.4rem; font-size: .95rem; cursor: pointer; }
button.ghost:hover { color: var(--text); }
.links { margin-top: 1.6rem; font-size: .84rem; }
.links a { color: var(--muted); text-decoration: underline; cursor: pointer; margin: 0 .6rem; }
.qwrap { max-width: 620px; margin: 1.8rem auto; }
.qprog { text-align: center; color: var(--muted); font-size: .8rem; margin-bottom: .7rem; }
.qcard { background: var(--card); border: 1px solid var(--border); border-radius: 14px;
  padding: 1.15rem 1.4rem; border-left-width: 4px; }
.qq { font-size: 1.12rem; font-weight: 600; margin: 0 0 .35rem; }
.qdetail { color: var(--muted); font-size: .92rem; margin: 0 0 .6rem; }
.mrow { display: flex; align-items: flex-start; gap: .7rem; border-top: 1px solid var(--border); padding: .55rem 0; }
.mrow details { flex: 1; min-width: 0; }
.mrow summary { cursor: pointer; font-size: .92rem; list-style: none; }
.mrow summary::-webkit-details-marker { display: none; }
.proj-tag { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: .72rem;
  color: var(--muted); margin-left: .45rem; white-space: nowrap; }
.mbody { color: var(--muted); font-size: .85rem; margin-top: .3rem; }
button.forget { border: 1px solid transparent; background: none; color: var(--muted);
  border-radius: 99px; padding: .15rem .8rem; cursor: pointer; font-size: .82rem; flex-shrink: 0; }
button.forget:hover { background: var(--danger); color: #fff; }
.qbtns { display: flex; gap: .7rem; justify-content: center; margin-top: 1.2rem; }
.proj { border: 1px solid var(--border); border-radius: 10px; background: var(--card); margin: .45rem 0; }
.proj > summary { padding: .55rem .95rem; cursor: pointer; font-size: .85rem;
  font-family: ui-monospace, "SF Mono", Menlo, monospace; display: flex; align-items: center; gap: .6rem; }
.proj > summary .n { margin-left: auto; color: var(--muted); font-size: .75rem; }
.card { border-top: 1px solid var(--border); padding: .6rem .95rem; }
.card .top { display: flex; gap: .55rem; align-items: baseline; }
.card .t { font-size: .92rem; flex: 1; }
.badge { font-size: .68rem; padding: .12rem .55rem; border-radius: 99px;
  background: light-dark(#ececf1, #26262e); color: var(--muted); white-space: nowrap; }
.t-project { background: color-mix(in srgb, var(--info) 14%, transparent); color: var(--info); }
.t-user { background: color-mix(in srgb, #8b5cf6 14%, transparent); color: #8b5cf6; }
.t-feedback { background: color-mix(in srgb, var(--warn) 16%, transparent); color: var(--warn); }
.t-reference { background: color-mix(in srgb, #12a594 14%, transparent); color: #12a594; }
.desc { color: var(--muted); font-size: .85rem; margin-top: .1rem; }
details.body { margin-top: .25rem; }
details.body summary { cursor: pointer; font-size: .76rem; color: var(--muted); }
pre { white-space: pre-wrap; background: light-dark(#f1f1f4, #141419); border: 1px solid var(--border);
  padding: .6rem .8rem; border-radius: 8px; font-size: .78rem; overflow-x: auto; }
#count { color: var(--muted); font-size: .82rem; margin: .6rem 0 0; }
#toast { position: fixed; left: 50%; transform: translateX(-50%); bottom: 1.3rem; z-index: 9;
  background: light-dark(#26262e, #e9e9ee); color: light-dark(#fff, #16161a);
  padding: .55rem 1.1rem; border-radius: 99px; font-size: .86rem; box-shadow: 0 6px 24px #0005; }
#toast a { color: light-dark(#a5a5ff, #4a4ad0); cursor: pointer; margin-left: .7rem; text-decoration: underline; }
footer { max-width: 960px; width: 100%; margin: auto auto 0; padding: 1rem; border-top: 1px solid var(--border);
  color: var(--muted); font-size: .78rem; text-align: center; }
footer a { color: var(--accent); text-decoration: none; }
</style>
<header><div class=bar>
  <span class=wm id=wmk>amn<i>e</i><i>s</i><i>i</i><i>a</i></span>
  <input id=q placeholder="Search everything your agent remembers&hellip;">
  <span id=score></span>
</div></header>
<main>
<section id=home></section>
<section id=queue hidden></section>
<section id=browse hidden></section>
<section id=map hidden><div class=mapbar></div><div id=tip hidden></div><div id=ncard hidden></div>
<div class=legend></div></section>
</main>
<canvas id=cv></canvas>
<div id=toast hidden></div>
<footer>nothing is ever lost &mdash; forgotten memories move to <code>~/.claude/memory-trash/</code>
&middot; <a href="https://github.com/tiny-cloud-ventures/amnesia">github</a> &middot; MIT</footer>
<script>
let mems = [], analysis = null, findings = [], byRef = {};
let qtotal = 0, qdone = 0, cleaned = 0, view = 'home', lastDel = null, toastTimer = null;
let status = { running: false, error: null };
const $ = id => document.getElementById(id);
const TYPE_LABEL = { project: 'project', feedback: 'rule', user: 'about you', reference: 'link' };
const KIND_COLOR = { conflict: 'var(--danger)', misfiled: 'var(--info)', twice: 'var(--accent)', stale: 'var(--warn)' };
const KIND_SKIP = { conflict: 'Keep both', misfiled: 'Leave it', twice: 'Leave it', stale: 'Still true' };

function prettyProj(p) {
  const m = p.match(/^-(?:Users|home)-[^-]+(.*)$/);
  return m ? '~' + (m[1] ? m[1].replace('-', '/') : '') : p;
}
function titleOf(m) {
  let d = (m.description || '').trim().replace(/^"+|"+$/g, '');
  if (!d) d = (m.name || m.file.replace(/\\.md$/, '')).replace(/[-_]/g, ' ');
  for (const sep of [' — ', '; ', '. ']) {
    const i = d.indexOf(sep);
    if (i > 12) { d = d.slice(0, i); break; }
  }
  return d.length > 95 ? d.slice(0, 93) + '…' : d;
}
function buildRefs() {
  byRef = {};
  for (const m of mems) byRef[m.project + '/' + m.file] = m;
}
function show(id) {
  view = id;
  document.body.dataset.view = id;
  for (const s of ['home', 'queue', 'browse', 'map']) $(s).hidden = s !== id;
}
function renderScore() {
  $('score').textContent = cleaned > 0 ? '✓ ' + cleaned + ' cleaned' : '';
}

function buildFindings() {
  const out = [];
  if (analysis) {
    for (const it of analysis.contradictions || [])
      out.push({ kind: 'conflict', q: 'These can’t both be true', title: it.title, detail: it.detail, files: it.files || [] });
    for (const o of analysis.ops || []) out.push(o.op === 'move'
      ? { kind: 'misfiled', q: 'This seems filed in the wrong project', detail: o.reason, files: [o.from], op: o }
      : { kind: 'twice', q: 'Your agent remembers this twice', detail: o.reason, files: [o.from, o.to], op: o });
    for (const it of analysis.stale || [])
      out.push({ kind: 'stale', q: 'This looks out of date', title: it.title, detail: it.detail, files: it.files || [] });
    for (const it of analysis.duplicates || [])
      out.push({ kind: 'twice', q: 'Your agent remembers this twice', title: it.title, detail: it.detail, files: it.files || [] });
  } else {
    const byFile = {};
    for (const m of mems) (byFile[m.file] = byFile[m.file] || []).push(m);
    for (const [f, ms] of Object.entries(byFile)) if (ms.length > 1)
      out.push({ kind: 'twice', q: 'Remembered in ' + ms.length + ' different projects',
        detail: 'The same memory lives in several places — usually only one copy is still right.',
        files: ms.map(m => m.project + '/' + m.file) });
    const old = mems.filter(m => (Date.now() / 1000 - m.mtime) > 90 * 86400)
      .sort((a, b) => a.mtime - b.mtime).slice(0, 5);
    for (const m of old)
      out.push({ kind: 'stale', q: 'Untouched for ' + Math.round((Date.now() / 1000 - m.mtime) / 86400) + ' days',
        detail: 'Old memories drift out of date. Does this still describe reality?',
        files: [m.project + '/' + m.file] });
  }
  return out;
}

// ---- the map: every memory a star, colored by project, wired by its links ----
const NAME_RE = /\\[\\[([\\w-]+)\\]\\]/g;
function buildGraph(ms, an) {
  const nodes = ms.map((m, i) => ({ i, m }));
  const byR = {}, byName = {}, byFile = {};
  for (const n of nodes) {
    byR[n.m.project + '/' + n.m.file] = n.i;
    (byName[n.m.name] = byName[n.m.name] || []).push(n.i);
    (byFile[n.m.file] = byFile[n.m.file] || []).push(n.i);
  }
  const seen = new Set(), edges = [];
  const add = (a, b, kind) => {
    if (a == null || b == null || a === b) return;
    const k = Math.min(a, b) + ':' + Math.max(a, b);
    if (!seen.has(k)) { seen.add(k); edges.push({ a, b, kind }); }
  };
  for (const n of nodes)
    for (const mt of n.m.body.matchAll(NAME_RE))
      for (const j of byName[mt[1]] || []) add(n.i, j, 'link');
  for (const idxs of Object.values(byFile))
    for (let x = 1; x < idxs.length; x++) add(idxs[0], idxs[x], 'twin');
  for (const c of (an && an.contradictions) || [])
    for (let x = 1; x < (c.files || []).length; x++) add(byR[c.files[0]], byR[c.files[x]], 'conflict');
  for (const d of (an && an.duplicates) || [])
    for (let x = 1; x < (d.files || []).length; x++) add(byR[d.files[0]], byR[d.files[x]], 'twin');
  for (const o of (an && an.ops) || [])
    if (o.op === 'merge') add(byR[o.from], byR[o.to], 'twin');
  return { nodes, edges };
}

let G = { nodes: [], edges: [] }, hovered = null, dragging = null, dragMoved = 0;
const cv = $('cv'), ctx = cv.getContext('2d');
const DARK = matchMedia('(prefers-color-scheme: dark)');
function edgeColor(kind) {
  return { link: '#8888aa' + (DARK.matches ? '55' : '66'), twin: '#8b8bf588',
    conflict: '#e5484dcc' }[kind];
}
function nodeColor(n) { return 'hsl(' + n.hue + ' 62% ' + (DARK.matches ? 62 : 46) + '%)'; }

function refreshGraph() {
  const old = {};
  for (const n of G.nodes) old[n.m.project + '/' + n.m.file] = n;
  G = buildGraph(mems, analysis);
  const nproj = {};
  for (const m of mems) nproj[m.project] = (nproj[m.project] || 0) + 1;
  const projs = Object.keys(nproj).sort((a, b) => nproj[b] - nproj[a]);
  const w = innerWidth, h = innerHeight, R = Math.min(w, h) * .34;
  for (const n of G.nodes) {
    const pi = projs.indexOf(n.m.project), ang = (pi - 1) / (projs.length - 1 || 1) * 2 * Math.PI - Math.PI / 2;
    n.hue = Math.round(pi * 137.508) % 360;
    n.ax = w / 2 + (pi ? Math.cos(ang) * R : 0); n.ay = h / 2 + (pi ? Math.sin(ang) * R : 0);
    n.r = Math.min(9, 3.2 + Math.sqrt(n.m.body.length) / 14);
    const o = old[n.m.project + '/' + n.m.file];
    if (o) { n.x = o.x; n.y = o.y; n.vx = o.vx; n.vy = o.vy; }
    else {
      n.x = n.ax + (Math.random() - .5) * 90; n.y = n.ay + (Math.random() - .5) * 90;
      n.vx = 0; n.vy = 0;
    }
  }
}

function tick() {
  const ns = G.nodes;
  // ponytail: O(n²) repulsion — fine to ~1000 nodes, grid-bucket it beyond that
  for (let i = 0; i < ns.length; i++) for (let j = i + 1; j < ns.length; j++) {
    const a = ns[i], b = ns[j];
    let dx = b.x - a.x, dy = b.y - a.y, d2 = dx * dx + dy * dy;
    if (d2 < 1) { dx = Math.sin(i) + .1; dy = Math.cos(j); d2 = 1; }
    if (d2 < 22500) {
      const d = Math.sqrt(d2), f = 620 / d2, fx = dx / d * f, fy = dy / d * f;
      a.vx -= fx; a.vy -= fy; b.vx += fx; b.vy += fy;
    }
  }
  for (const e of G.edges) {
    const a = ns[e.a], b = ns[e.b];
    const dx = b.x - a.x, dy = b.y - a.y, d = Math.sqrt(dx * dx + dy * dy) || 1;
    const f = (d - 70) * .004, fx = dx / d * f, fy = dy / d * f;
    a.vx += fx; a.vy += fy; b.vx -= fx; b.vy -= fy;
  }
  const w = innerWidth, h = innerHeight;
  for (const n of ns) {
    n.vx += (n.ax - n.x) * .0045; n.vy += (n.ay - n.y) * .0045;
    if (n.x < 60) n.vx += (60 - n.x) * .02; else if (n.x > w - 60) n.vx -= (n.x - w + 60) * .02;
    if (n.y < 95) n.vy += (95 - n.y) * .02; else if (n.y > h - 85) n.vy -= (n.y - h + 85) * .02;
    n.vx *= .85; n.vy *= .85;
    if (n !== dragging) { n.x += n.vx; n.y += n.vy; }
  }
}

function draw(t) {
  const dpr = devicePixelRatio || 1, w = innerWidth, h = innerHeight;
  if (cv.width !== w * dpr || cv.height !== h * dpr) { cv.width = w * dpr; cv.height = h * dpr; }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  for (const e of G.edges) {
    const a = G.nodes[e.a], b = G.nodes[e.b];
    ctx.strokeStyle = edgeColor(e.kind);
    ctx.lineWidth = e.kind === 'conflict' ? 1.8 : 1;
    ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
  }
  for (const n of G.nodes) {
    const wob = Math.sin(t / 1300 + n.i * 2.1) * 1.3;
    ctx.shadowColor = nodeColor(n); ctx.shadowBlur = n === hovered ? 22 : 10;
    ctx.fillStyle = nodeColor(n);
    ctx.beginPath(); ctx.arc(n.x, n.y + wob, n.r + (n === hovered ? 2.5 : 0), 0, 7); ctx.fill();
  }
  ctx.shadowBlur = 0;
}

function loop(t) {
  requestAnimationFrame(loop);
  if (view !== 'home' && view !== 'map') return;
  tick(); draw(t);
}

function nodeAt(x, y) {
  let best = null, bd = 200;
  for (const n of G.nodes) {
    const d = (n.x - x) ** 2 + (n.y - y) ** 2;
    if (d < bd) { bd = d; best = n; }
  }
  return best;
}
cv.onpointermove = e => {
  if (dragging) {
    dragging.x = e.clientX; dragging.y = e.clientY; dragMoved++;
    $('tip').hidden = true; return;
  }
  hovered = nodeAt(e.clientX, e.clientY);
  cv.style.cursor = hovered ? 'pointer' : 'grab';
  const tip = $('tip');
  if (!hovered) { tip.hidden = true; return; }
  tip.textContent = '';
  tip.append(titleOf(hovered.m));
  const tag = document.createElement('span'); tag.className = 'proj-tag';
  tag.textContent = prettyProj(hovered.m.project); tip.appendChild(tag);
  tip.style.left = Math.min(e.clientX + 14, innerWidth - 340) + 'px';
  tip.style.top = (e.clientY + 16) + 'px';
  tip.hidden = false;
};
cv.onpointerdown = e => {
  dragging = nodeAt(e.clientX, e.clientY); dragMoved = 0;
  if (dragging) cv.setPointerCapture(e.pointerId);
};
cv.onpointerup = () => {
  if (dragging && dragMoved < 4 && view === 'map') openNodeCard(dragging);
  else if (!dragging && view === 'map') $('ncard').hidden = true;
  dragging = null;
};

function openNodeCard(n) {
  const c = $('ncard'); c.textContent = '';
  const top = document.createElement('div'); top.className = 'top';
  const t = document.createElement('span'); t.className = 't'; t.textContent = titleOf(n.m);
  const badge = document.createElement('span');
  badge.className = 'badge t-' + (n.m.type || 'unknown');
  badge.textContent = TYPE_LABEL[n.m.type] || n.m.type || '?';
  top.append(t, badge);
  const proj = document.createElement('div'); proj.className = 'desc';
  proj.textContent = prettyProj(n.m.project);
  const det = document.createElement('details'); det.className = 'body';
  const sum = document.createElement('summary'); sum.textContent = 'details';
  const pre = document.createElement('pre'); pre.textContent = n.m.body;
  det.append(sum, pre);
  const btns = document.createElement('div'); btns.className = 'qbtns';
  const f = document.createElement('button'); f.className = 'forget'; f.textContent = 'forget';
  f.onclick = async () => { if (await deleteMem(n.m)) { refreshGraph(); c.hidden = true; } };
  const x = document.createElement('button'); x.className = 'ghost'; x.textContent = 'close';
  x.onclick = () => { c.hidden = true; };
  btns.append(f, x);
  c.append(top, proj, det, btns);
  c.hidden = false;
}

function renderMap() {
  show('map');
  $('ncard').hidden = true; $('tip').hidden = true;
  const bar = document.querySelector('.mapbar'); bar.textContent = '';
  bar.append('Every dot is one memory — drag them around, click one to look closer.');
  const a = document.createElement('a'); a.textContent = '← back'; a.onclick = renderHome;
  bar.appendChild(a);
  const leg = document.querySelector('.legend'); leg.textContent = '';
  const item = (color, label) => {
    const i = document.createElement('i'); i.style.background = color;
    leg.append(i, ' ' + label);
  };
  leg.append('colors = projects');
  item('#8888aa', 'linked memories'); item('#8b8bf5', 'remembered twice');
  if (analysis && (analysis.contradictions || []).length) item('#e5484d', 'contradiction');
}

async function deleteMem(m) {
  const r = await fetch('/api/delete', { method: 'POST',
    body: JSON.stringify({ project: m.project, file: m.file }) });
  if (!r.ok) { alert('failed: ' + (await r.json()).error); return false; }
  mems = mems.filter(x => x !== m);
  delete byRef[m.project + '/' + m.file];
  cleaned++; renderScore(); toast(m); refreshGraph();
  return true;
}
function toast(m) {
  lastDel = m;
  const t = $('toast'); t.textContent = '';
  t.append('Forgot “' + titleOf(m).slice(0, 42) + '”');
  const u = document.createElement('a'); u.textContent = 'undo';
  u.onclick = async () => {
    const r = await fetch('/api/restore', { method: 'POST',
      body: JSON.stringify({ project: lastDel.project, file: lastDel.file }) });
    if (!r.ok) { alert('undo failed: ' + (await r.json()).error); return; }
    mems.push(lastDel); byRef[lastDel.project + '/' + lastDel.file] = lastDel;
    cleaned--; renderScore(); t.hidden = true; refreshGraph();
    if (view === 'browse') renderBrowse(); else if (view === 'home') renderHome();
  };
  t.appendChild(u); t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 7000);
}

function renderHome() {
  show('home');
  const h = $('home'); h.textContent = '';
  const wrap = document.createElement('div'); wrap.className = 'home';
  const big = document.createElement('p'); big.className = 'big';
  const nm = document.createElement('b'); nm.textContent = mems.length + ' things';
  const np = document.createElement('b');
  np.textContent = new Set(mems.map(m => m.project)).size + ' projects';
  big.append('Your agent remembers ', nm, ' about you, across ', np, '.');
  wrap.appendChild(big);
  const sub = document.createElement('p'); sub.className = 'sub';
  const btn = document.createElement('button'); btn.className = 'primary';
  if (!mems.length) {
    big.textContent = 'No memories found — your agent has a clean slate.';
    sub.textContent = 'Memories appear in ~/.claude/projects/ as you use Claude Code.';
    wrap.appendChild(sub); h.appendChild(wrap); return;
  }
  if (status.running) {
    sub.textContent = 'Reading every memory and cross-checking them… this takes a few minutes.';
    btn.textContent = 'Scanning…'; btn.disabled = true;
  } else if (status.error) {
    sub.className = 'sub err'; sub.textContent = status.error;
    btn.textContent = 'Try again'; btn.onclick = doScan;
  } else if (findings.length) {
    const n = document.createElement('b'); n.textContent = findings.length + ' of them';
    sub.append(n, analysis ? ' need a decision from you.' : ' look worth a glance.');
    btn.textContent = analysis ? 'Review them' : 'Scan my memories';
    btn.onclick = analysis ? startQueue : doScan;
  } else if (analysis) {
    sub.textContent = 'Everything checks out — no conflicts, nothing stale. ✓';
    btn.textContent = 'Rescan'; btn.onclick = doScan;
  } else {
    sub.textContent = 'A scan reads them all and flags anything conflicting, stale, or misfiled.';
    btn.textContent = 'Scan my memories'; btn.onclick = doScan;
  }
  wrap.appendChild(sub); wrap.appendChild(btn);
  const links = document.createElement('div'); links.className = 'links';
  if (!analysis && findings.length && !status.running) {
    const a = document.createElement('a'); a.textContent = 'review ' + findings.length + ' quick flags';
    a.onclick = startQueue; links.appendChild(a);
  }
  if (analysis && findings.length && !status.running) {
    const a = document.createElement('a'); a.textContent = 'rescan'; a.onclick = doScan;
    links.appendChild(a);
  }
  const mp = document.createElement('a'); mp.textContent = 'see the map ✨';
  mp.onclick = renderMap; links.appendChild(mp);
  const b = document.createElement('a'); b.textContent = 'browse everything';
  b.onclick = () => renderBrowse(); links.appendChild(b);
  wrap.appendChild(links);
  h.appendChild(wrap);
}

async function doScan() {
  await fetch('/api/analyze', { method: 'POST' });
  status.running = true; status.error = null; renderHome();
  const poll = setInterval(async () => {
    status = await (await fetch('/api/status')).json();
    if (!status.running) {
      clearInterval(poll);
      if (!status.error) {
        const r = await fetch('/api/analysis');
        analysis = r.ok ? await r.json() : null;
        findings = buildFindings(); qdone = 0; refreshGraph();
      }
      if (view === 'home') renderHome();
    }
  }, 4000);
}

function mrowEl(ref, actions, consume) {
  const row = document.createElement('div'); row.className = 'mrow';
  const m = byRef[ref];
  const det = document.createElement('details');
  const sum = document.createElement('summary');
  if (!m) { sum.textContent = ref; det.appendChild(sum); row.appendChild(det); return row; }
  sum.textContent = titleOf(m);
  const tag = document.createElement('span'); tag.className = 'proj-tag';
  tag.textContent = prettyProj(m.project); sum.appendChild(tag);
  const body = document.createElement('div'); body.className = 'mbody';
  const pre = document.createElement('pre'); pre.textContent = m.body;
  body.appendChild(pre);
  det.append(sum, body);
  row.appendChild(det);
  if (actions) {
    const f = document.createElement('button'); f.className = 'forget'; f.textContent = 'forget';
    f.onclick = async () => { if (await deleteMem(m)) consume(); };
    row.appendChild(f);
  }
  return row;
}

function startQueue() { qtotal = findings.length + qdone; renderQueue(); }
function renderQueue() {
  show('queue');
  const box = $('queue'); box.textContent = '';
  const wrap = document.createElement('div'); wrap.className = 'qwrap';
  if (!findings.length) {
    const done = document.createElement('div'); done.className = 'home';
    const big = document.createElement('p'); big.className = 'big';
    big.textContent = '✨ All done.';
    const sub = document.createElement('p'); sub.className = 'sub';
    sub.textContent = 'You went through ' + qtotal + ' — your agent’s memory is sharper for it.';
    const btn = document.createElement('button'); btn.className = 'primary';
    btn.textContent = 'Back'; btn.onclick = renderHome;
    done.append(big, sub, btn); wrap.appendChild(done);
    box.appendChild(wrap); return;
  }
  const f = findings[0];
  const consume = () => { findings.shift(); qdone++; renderQueue(); };
  const prog = document.createElement('div'); prog.className = 'qprog';
  prog.textContent = (qdone + 1) + ' of ' + qtotal;
  const card = document.createElement('div'); card.className = 'qcard';
  card.style.borderLeftColor = KIND_COLOR[f.kind];
  const q = document.createElement('p'); q.className = 'qq'; q.textContent = f.q;
  card.appendChild(q);
  const dt = document.createElement('p'); dt.className = 'qdetail';
  dt.textContent = (f.title ? f.title + ' — ' : '') + (f.detail || '');
  card.appendChild(dt);
  const perRow = !f.op && f.kind !== 'misfiled';
  for (const ref of f.files) card.appendChild(mrowEl(ref, perRow, consume));
  const btns = document.createElement('div'); btns.className = 'qbtns';
  if (f.op) {
    const go = document.createElement('button'); go.className = 'primary';
    go.textContent = f.op.op === 'move' ? 'Move it' : 'Combine them';
    go.onclick = async () => {
      const r = await fetch('/api/apply', { method: 'POST',
        body: JSON.stringify({ op: f.op.op, from: f.op.from, to: f.op.to }) });
      if (!r.ok) { alert('failed: ' + (await r.json()).error); return; }
      mems = await (await fetch('/api/memories')).json(); buildRefs(); refreshGraph();
      cleaned++; renderScore(); consume();
    };
    btns.appendChild(go);
  }
  const skip = document.createElement('button'); skip.className = 'ghost';
  skip.textContent = KIND_SKIP[f.kind]; skip.onclick = consume;
  btns.appendChild(skip);
  card.appendChild(btns);
  const back = document.createElement('div'); back.className = 'links';
  back.style.textAlign = 'center';
  const a = document.createElement('a'); a.textContent = 'finish later'; a.onclick = renderHome;
  back.appendChild(a);
  wrap.append(prog, card, back);
  box.appendChild(wrap);
}

function cardEl(m) {
  const card = document.createElement('div'); card.className = 'card';
  const top = document.createElement('div'); top.className = 'top';
  const t = document.createElement('span'); t.className = 't'; t.textContent = titleOf(m);
  const badge = document.createElement('span');
  badge.className = 'badge t-' + (m.type || 'unknown');
  badge.textContent = TYPE_LABEL[m.type] || m.type || '?';
  const del = document.createElement('button'); del.className = 'forget'; del.textContent = 'forget';
  del.onclick = async () => { if (await deleteMem(m)) renderBrowse(); };
  top.append(t, badge, del);
  const det = document.createElement('details'); det.className = 'body';
  const sum = document.createElement('summary'); sum.textContent = 'details';
  const desc = document.createElement('div'); desc.className = 'desc'; desc.textContent = m.description;
  const pre = document.createElement('pre'); pre.textContent = m.body;
  det.append(sum, desc, pre);
  card.append(top, det);
  return card;
}
function renderBrowse() {
  show('browse');
  const box = $('browse'); box.textContent = '';
  const q = $('q').value.toLowerCase();
  const shown = mems.filter(m =>
    [m.project, m.file, m.name, m.description, m.type, m.body].join(' ').toLowerCase().includes(q));
  const count = document.createElement('div'); count.id = 'count';
  count.textContent = q ? shown.length + ' matches' : shown.length + ' memories — click a project to look inside';
  box.appendChild(count);
  const groups = {};
  for (const m of shown) (groups[m.project] = groups[m.project] || []).push(m);
  for (const [proj, ms] of Object.entries(groups)) {
    const det = document.createElement('details'); det.className = 'proj'; det.open = !!q;
    const sum = document.createElement('summary'); sum.title = proj;
    const nm = document.createElement('span'); nm.textContent = prettyProj(proj);
    const n = document.createElement('span'); n.className = 'n';
    n.textContent = ms.length + (ms.length === 1 ? ' memory' : ' memories');
    sum.append(nm, n);
    det.appendChild(sum);
    for (const m of ms) det.appendChild(cardEl(m));
    box.appendChild(det);
  }
  const links = document.createElement('div'); links.className = 'links';
  links.style.textAlign = 'center';
  const a = document.createElement('a'); a.textContent = 'back'; a.onclick = () => { $('q').value = ''; renderHome(); };
  links.appendChild(a);
  box.appendChild(links);
}

$('q').oninput = () => { $('q').value ? renderBrowse() : renderHome(); };
$('wmk').onclick = renderHome;
async function load() {
  mems = await (await fetch('/api/memories')).json();
  buildRefs();
  const r = await fetch('/api/analysis');
  analysis = r.ok ? await r.json() : null;
  try { status = await (await fetch('/api/status')).json(); } catch (e) {}
  findings = buildFindings();
  refreshGraph(); requestAnimationFrame(loop);
  renderScore();
  if (location.hash === '#map' && mems.length) renderMap(); else renderHome();
  if (status.running) doScan();
}
load();
</script>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif self.path == "/api/memories":
            self._send(200, list_memories())
        elif self.path == "/api/analysis":
            if ANALYSIS.is_file():
                self._send(200, ANALYSIS.read_bytes())
            else:
                self._send(404, {"error": "no analysis yet"})
        elif self.path == "/api/status":
            self._send(200, A_STATE)
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        try:
            if self.path == "/api/analyze":
                start_analyze()
                return self._send(200, {"ok": True})
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            if self.path == "/api/delete":
                delete_memory(req["project"], req["file"])
            elif self.path == "/api/restore":
                restore_memory(req["project"], req["file"])
            elif self.path == "/api/apply":
                apply_op(req["op"], req["from"], req["to"])
            else:
                return self._send(404, {"error": "not found"})
            self._send(200, {"ok": True})
        except (ValueError, FileNotFoundError, KeyError) as e:
            self._send(400, {"error": str(e)})

    def log_message(self, *a):
        pass


def _check():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root, trash = Path(td) / "projects", Path(td) / "trash"
        mdir = root / "-tmp-proj" / "memory"
        mdir.mkdir(parents=True)
        (mdir / "fact.md").write_text(
            "---\nname: fact\ndescription: a fact\nmetadata:\n  type: project\n---\n\nBody here")
        (mdir / "MEMORY.md").write_text("## Index\n\n- [Fact](fact.md) — a fact\n- [Other](other.md)\n")
        mems = list_memories(root)
        assert len(mems) == 1 and mems[0]["name"] == "fact" and mems[0]["type"] == "project", mems
        dump = build_dump(root)
        assert dump.startswith("=== -tmp-proj/fact.md ===") and "Body here" in dump, dump
        for bad in [("../x", "fact.md"), ("-tmp-proj", "../MEMORY.md")]:
            try:
                delete_memory(*bad, root, trash); assert False, bad
            except ValueError:
                pass
        delete_memory("-tmp-proj", "fact.md", root, trash)
        assert not (mdir / "fact.md").exists()
        assert (trash / "-tmp-proj" / "fact.md").exists()
        idx = (mdir / "MEMORY.md").read_text()
        assert "fact.md" not in idx and "other.md" in idx, idx
        assert list_memories(root) == []
        restore_memory("-tmp-proj", "fact.md", root, trash)
        assert (mdir / "fact.md").is_file() and not (trash / "-tmp-proj" / "fact.md").exists()
        assert "(fact.md)" in (mdir / "MEMORY.md").read_text()
        assert len(list_memories(root)) == 1
        try:
            restore_memory("-tmp-proj", "../evil.md", root, trash); assert False
        except ValueError:
            pass
        try:
            restore_memory("-tmp-proj", "fact.md", root, trash); assert False  # not in trash
        except FileNotFoundError:
            pass
    with tempfile.TemporaryDirectory() as td:
        root, trash = Path(td) / "projects", Path(td) / "trash"
        a, b = root / "-proj-a" / "memory", root / "-proj-b" / "memory"
        a.mkdir(parents=True); b.mkdir(parents=True)
        (a / "misplaced.md").write_text("---\nname: misplaced\ndescription: belongs in b\ntype: project\n---\n\nFact about b.")
        (a / "MEMORY.md").write_text("## Index\n- [Misplaced](misplaced.md) — belongs in b\n")
        (a / "dupe.md").write_text("---\nname: dupe\ndescription: subset\ntype: feedback\n---\n\nExtra nuance.")
        (b / "canon.md").write_text("---\nname: canon\ndescription: canonical\ntype: feedback\n---\n\nThe rule.")
        apply_op("move", "-proj-a/misplaced.md", "-proj-b", root, trash)
        assert (b / "misplaced.md").is_file() and not (a / "misplaced.md").exists()
        assert "misplaced.md" not in (a / "MEMORY.md").read_text()
        assert "(misplaced.md)" in (b / "MEMORY.md").read_text()
        apply_op("merge", "-proj-a/dupe.md", "-proj-b/canon.md", root, trash)
        merged = (b / "canon.md").read_text()
        assert "Extra nuance." in merged and "Merged from -proj-a/dupe.md" in merged, merged
        assert (trash / "-proj-a" / "dupe.md").is_file() and not (a / "dupe.md").exists()
        for bad in [("move", "-proj-b/canon.md", "../evil"), ("merge", "-proj-b/canon.md", "x"),
                    ("rename", "-proj-b/canon.md", "-proj-a")]:
            try:
                apply_op(*bad, root, trash); assert False, bad
            except ValueError:
                pass
    fenced = 'Here you go:\n```json\n{"contradictions": [{"title": "t", "detail": "d", "files": ["a/b.md"]}]}\n```'
    f = parse_findings(fenced)
    assert f["contradictions"][0]["title"] == "t" and f["stale"] == [] and f["ops"] == [], f
    try:
        parse_findings("no json here"); assert False
    except ValueError:
        pass
    print("self-check OK")


def main():
    argv = sys.argv[1:]
    if "--check" in argv:
        _check()
    elif "analyze" in argv:
        analyze()
    else:
        port = int(argv[0]) if argv and argv[0].isdigit() else 8780
        print(f"amnesia on http://localhost:{port} — trash: {TRASH}")
        ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()

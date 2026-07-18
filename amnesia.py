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
import json, re, shutil, subprocess, sys
from http.server import HTTPServer, BaseHTTPRequestHandler
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
  background: var(--bg); color: var(--text); }
code { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: .85em;
  background: light-dark(#ececf1, #26262e); padding: .1em .35em; border-radius: 5px; }
header { position: sticky; top: 0; z-index: 5; border-bottom: 1px solid var(--border);
  background: color-mix(in srgb, var(--bg) 82%, transparent); backdrop-filter: blur(10px); }
.bar { max-width: 960px; margin: 0 auto; padding: .65rem 1rem;
  display: flex; gap: 1rem; align-items: center; flex-wrap: wrap; }
.wm { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 1.15rem; font-weight: 600; }
.wm i { font-style: normal; }
.wm i:nth-of-type(1){opacity:.75} .wm i:nth-of-type(2){opacity:.55}
.wm i:nth-of-type(3){opacity:.35} .wm i:nth-of-type(4){opacity:.18}
#q { flex: 1; min-width: 220px; padding: .45rem .9rem; font-size: .95rem; color: var(--text);
  border: 1px solid var(--border); border-radius: 99px; background: var(--card); outline: none; }
#q:focus { border-color: var(--accent); }
#stats { font-size: .8rem; color: var(--muted); white-space: nowrap; }
main { max-width: 960px; margin: 0 auto; padding: 1rem; }
h2 { font-size: .8rem; font-weight: 600; color: var(--muted); margin: 1.6rem 0 .5rem;
  font-family: ui-monospace, "SF Mono", Menlo, monospace; letter-spacing: .03em; }
h3 { font-size: .95rem; margin: 1.4rem 0 .4rem; }
#count { color: var(--muted); font-size: .82rem; margin: .6rem 0 0; }
.hint { border: 1px dashed var(--border); border-radius: 10px; padding: .9rem 1.1rem;
  margin: 1rem 0; color: var(--muted); font-size: .9rem; background: var(--card); }
.card, .finding { background: var(--card); border: 1px solid var(--border); border-radius: 10px;
  padding: .65rem .9rem; margin: .5rem 0; transition: border-color .15s; }
.card:hover { border-color: light-dark(#c9c9d2, #3a3a45); }
.card .top { display: flex; gap: .55rem; align-items: baseline; }
.card b { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: .88rem; font-weight: 600; }
.badge { font-size: .68rem; padding: .12rem .55rem; border-radius: 99px;
  background: light-dark(#ececf1, #26262e); color: var(--muted); cursor: pointer; }
.t-project { background: color-mix(in srgb, var(--info) 14%, transparent); color: var(--info); }
.t-user { background: color-mix(in srgb, #8b5cf6 14%, transparent); color: #8b5cf6; }
.t-feedback { background: color-mix(in srgb, var(--warn) 16%, transparent); color: var(--warn); }
.t-reference { background: color-mix(in srgb, #12a594 14%, transparent); color: #12a594; }
.card .top button { margin-left: auto; border: 1px solid transparent; background: none;
  color: var(--muted); border-radius: 6px; padding: .1rem .6rem; cursor: pointer; font-size: .8rem; }
.card:hover .top button { color: var(--danger); border-color: color-mix(in srgb, var(--danger) 40%, transparent); }
.card .top button.arm, .card .top button:hover { background: var(--danger); color: #fff; border-color: var(--danger); }
.desc { color: var(--muted); font-size: .88rem; margin-top: .15rem; }
details { margin-top: .3rem; }
summary { cursor: pointer; font-size: .78rem; color: var(--muted); }
pre { white-space: pre-wrap; background: light-dark(#f1f1f4, #141419); border: 1px solid var(--border);
  padding: .6rem .8rem; border-radius: 8px; font-size: .78rem; overflow-x: auto; }
.date { font-size: .72rem; color: var(--muted); }
.finding b { display: block; margin-bottom: .15rem; font-size: .9rem; }
.finding p { margin: .2rem 0; font-size: .88rem; color: var(--muted); }
.chip { display: inline-block; font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: .72rem;
  background: light-dark(#ececf1, #26262e); border-radius: 6px; padding: .08rem .45rem;
  margin: .2rem .25rem 0 0; cursor: pointer; color: var(--text); }
.chip:hover { background: color-mix(in srgb, var(--accent) 18%, transparent); color: var(--accent); }
.k-contradictions { border-left: 3px solid var(--danger); }
.k-stale { border-left: 3px solid var(--warn); }
.k-duplicates { border-left: 3px solid var(--info); }
.k-ops { border-left: 3px solid var(--ok); }
.op-kind { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: .72rem;
  font-weight: 700; margin-right: .35rem; color: var(--ok); }
.finding .apply { float: right; border: 1px solid color-mix(in srgb, var(--ok) 50%, transparent);
  background: none; color: var(--ok); border-radius: 6px; padding: .12rem .7rem; cursor: pointer; font-size: .8rem; }
.finding .apply:hover { background: var(--ok); color: #fff; }
.finding.done { opacity: .45; }
footer { max-width: 960px; margin: 2.5rem auto 0; padding: 1rem; border-top: 1px solid var(--border);
  color: var(--muted); font-size: .78rem; text-align: center; }
footer a { color: var(--accent); text-decoration: none; }
</style>
<header><div class=bar>
  <span class=wm>amn<i>e</i><i>s</i><i>i</i><i>a</i></span>
  <input id=q placeholder="Search memories&hellip;" autofocus>
  <span id=stats></span>
</div></header>
<main>
<div id=count></div>
<div id=analysis></div>
<div id=list></div>
</main>
<footer>deletes are reversible &mdash; files move to <code>~/.claude/memory-trash/</code>
&middot; <a href="https://github.com/tiny-cloud-ventures/amnesia">github</a> &middot; MIT</footer>
<script>
let mems = [], findings = null;
const $ = id => document.getElementById(id);
const TITLES = { contradictions: 'Contradictions', stale: 'Stale / superseded', duplicates: 'Duplicates' };
function prettyProj(p) {
  const m = p.match(/^-(?:Users|home)-[^-]+(.*)$/);
  return m ? '~' + (m[1] ? m[1].replace('-', '/') : '') : p;
}
async function load() {
  mems = await (await fetch('/api/memories')).json();
  const r = await fetch('/api/analysis');
  findings = r.ok ? await r.json() : null;
  $('stats').textContent = mems.length + ' memories · ' +
    new Set(mems.map(m => m.project)).size + ' projects';
  render(); renderAnalysis();
}
function chipEl(label, query) {
  const c = document.createElement('span'); c.className = 'chip'; c.textContent = label;
  c.onclick = () => { $('q').value = query; render(); };
  return c;
}
function findingEl(kind, title, detail) {
  const d = document.createElement('div'); d.className = 'finding k-' + kind;
  const t = document.createElement('b'); t.textContent = title;
  const p = document.createElement('p'); p.textContent = detail;
  d.append(t, p);
  return d;
}
function renderTriage(box) {
  const rows = [];
  const byFile = {};
  for (const m of mems) (byFile[m.file] = byFile[m.file] || []).push(m);
  for (const [f, ms] of Object.entries(byFile)) {
    if (ms.length < 2) continue;
    const d = findingEl('duplicates', f + ' exists in ' + ms.length + ' projects',
      'Same memory file in multiple places — likely cross-repo pollution or a consolidation candidate.');
    for (const m of ms) d.appendChild(chipEl(prettyProj(m.project) + '/' + f, f));
    rows.push(d);
  }
  const old = mems.filter(m => (Date.now() / 1000 - m.mtime) > 90 * 86400)
    .sort((a, b) => a.mtime - b.mtime).slice(0, 6);
  if (old.length) {
    const d = findingEl('stale', 'Untouched for 90+ days',
      'Oldest memories — verify these still describe reality before your agent acts on them.');
    for (const m of old) d.appendChild(chipEl(m.file, m.file));
    rows.push(d);
  }
  if (!rows.length) return false;
  const h = document.createElement('h3'); h.textContent = 'Review first';
  box.appendChild(h);
  for (const d of rows) box.appendChild(d);
  return true;
}
function renderAnalysis() {
  const box = $('analysis'); box.textContent = '';
  if (!findings) {
    renderTriage(box);
    const hint = document.createElement('div'); hint.className = 'hint';
    const c = document.createElement('code'); c.textContent = 'amnesia analyze';
    hint.append('For the full audit, run ', c,
      ' in a terminal — it checks for contradictions, stale facts, duplicates, and misfiled memories.');
    box.appendChild(hint); return;
  }
  const ops = findings.ops || [];
  for (const kind of ['contradictions']) {
    const items = findings[kind] || [];
    if (!items.length) continue;
    const h = document.createElement('h3'); h.textContent = 'Review first — ' + TITLES[kind].toLowerCase() + ' (' + items.length + ')';
    box.appendChild(h);
    for (const it of items) {
      const d = findingEl(kind, it.title, it.detail);
      for (const f of it.files || []) d.appendChild(chipEl(f, f.split('/').pop()));
      box.appendChild(d);
    }
  }
  if (ops.length) {
    const h = document.createElement('h3'); h.textContent = 'Suggested consolidations (' + ops.length + ')';
    box.appendChild(h);
    for (const o of ops) {
      const d = document.createElement('div'); d.className = 'finding k-ops';
      const btn = document.createElement('button'); btn.className = 'apply'; btn.textContent = 'apply';
      btn.onclick = async () => {
        const r = await fetch('/api/apply', { method: 'POST',
          body: JSON.stringify({ op: o.op, from: o.from, to: o.to }) });
        if (r.ok) {
          d.classList.add('done'); btn.disabled = true; btn.textContent = 'applied';
          mems = await (await fetch('/api/memories')).json(); render();
        } else alert('apply failed: ' + (await r.json()).error);
      };
      const t = document.createElement('b');
      const k = document.createElement('span'); k.className = 'op-kind'; k.textContent = o.op.toUpperCase();
      t.append(k, document.createTextNode(o.from + ' → ' + o.to));
      const p = document.createElement('p'); p.textContent = o.reason;
      d.append(btn, t, p);
      d.appendChild(chipEl(o.from, o.from.split('/').pop()));
      box.appendChild(d);
    }
  }
  for (const kind of ['stale', 'duplicates']) {
    const items = findings[kind] || [];
    if (!items.length) continue;
    const h = document.createElement('h3'); h.textContent = TITLES[kind] + ' (' + items.length + ')';
    box.appendChild(h);
    for (const it of items) {
      const d = findingEl(kind, it.title, it.detail);
      for (const f of it.files || []) d.appendChild(chipEl(f, f.split('/').pop()));
      box.appendChild(d);
    }
  }
}
function render() {
  const q = $('q').value.toLowerCase();
  const shown = mems.filter(m =>
    [m.project, m.file, m.name, m.description, m.type, m.body].join(' ').toLowerCase().includes(q));
  $('count').textContent = shown.length + ' of ' + mems.length + ' memories';
  const list = $('list'); list.textContent = '';
  let proj = null;
  for (const m of shown) {
    if (m.project !== proj) {
      proj = m.project;
      const h = document.createElement('h2'); h.textContent = prettyProj(proj); h.title = proj;
      list.appendChild(h);
    }
    const card = document.createElement('div'); card.className = 'card';
    const top = document.createElement('div'); top.className = 'top';
    const name = document.createElement('b'); name.textContent = m.file;
    const badge = document.createElement('span');
    badge.className = 'badge t-' + (m.type || 'unknown'); badge.textContent = m.type || '?';
    badge.onclick = () => { $('q').value = m.type || ''; render(); };
    const date = document.createElement('span'); date.className = 'date';
    date.textContent = new Date(m.mtime * 1000).toLocaleDateString();
    const del = document.createElement('button'); del.textContent = 'delete';
    let armed = false;
    const disarm = () => { armed = false; del.textContent = 'delete'; del.classList.remove('arm'); };
    del.onmouseleave = disarm;
    del.onclick = async () => {
      if (!armed) { armed = true; del.textContent = 'confirm'; del.classList.add('arm'); return; }
      const r = await fetch('/api/delete', { method: 'POST',
        body: JSON.stringify({ project: m.project, file: m.file }) });
      if (r.ok) { mems = mems.filter(x => x !== m); render(); }
      else { disarm(); alert('delete failed: ' + (await r.json()).error); }
    };
    top.append(name, badge, date, del);
    const desc = document.createElement('div'); desc.className = 'desc'; desc.textContent = m.description;
    const det = document.createElement('details');
    const sum = document.createElement('summary'); sum.textContent = 'body';
    const pre = document.createElement('pre'); pre.textContent = m.body;
    det.append(sum, pre);
    card.append(top, desc, det);
    list.appendChild(card);
  }
}
$('q').oninput = render;
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
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            if self.path == "/api/delete":
                delete_memory(req["project"], req["file"])
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
        HTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()

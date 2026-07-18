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
<title>amnesia</title>
<style>
:root { color-scheme: light dark; }
body { font: 15px/1.5 -apple-system, sans-serif; max-width: 900px; margin: 2rem auto; padding: 0 1rem; }
h1 { font-size: 1.3rem; } h1 small { font-weight: normal; opacity: .6; font-size: .8rem; }
h2 { font-size: .85rem; opacity: .6; margin: 1.5rem 0 .5rem; font-family: monospace; }
h3 { font-size: .95rem; margin: 1rem 0 .3rem; }
#q { width: 100%; padding: .5rem .7rem; font-size: 1rem; border: 1px solid #8884; border-radius: 8px; box-sizing: border-box; }
.card, .finding { border: 1px solid #8883; border-radius: 8px; padding: .6rem .8rem; margin: .5rem 0; }
.card .top { display: flex; gap: .5rem; align-items: baseline; }
.card b { font-family: monospace; font-size: .9rem; }
.badge { font-size: .7rem; padding: .05rem .45rem; border-radius: 99px; background: #8882; }
.card .top button { margin-left: auto; border: 1px solid #d33a; background: none; color: #d33;
  border-radius: 6px; padding: .1rem .6rem; cursor: pointer; }
.card .top button:hover { background: #d33; color: #fff; }
.desc { opacity: .8; font-size: .9rem; }
details { margin-top: .3rem; } summary { cursor: pointer; font-size: .8rem; opacity: .6; }
pre { white-space: pre-wrap; background: #8881; padding: .6rem; border-radius: 6px; font-size: .8rem; }
.date { font-size: .75rem; opacity: .5; }
#count, #ahint { opacity: .6; font-size: .85rem; margin: .5rem 0; }
.finding b { display: block; margin-bottom: .2rem; }
.finding p { margin: .2rem 0; font-size: .9rem; opacity: .85; }
.chip { display: inline-block; font-family: monospace; font-size: .75rem; background: #8882;
  border-radius: 6px; padding: .05rem .4rem; margin: .15rem .25rem 0 0; cursor: pointer; }
.chip:hover { background: #8884; }
.k-contradictions { border-left: 3px solid #d33; }
.k-stale { border-left: 3px solid #d90; }
.k-duplicates { border-left: 3px solid #38d; }
.k-ops { border-left: 3px solid #3a5; }
.op-kind { font-family: monospace; font-size: .75rem; font-weight: bold; margin-right: .3rem; }
.finding .apply { float: right; border: 1px solid #3a5a; background: none; color: #3a5;
  border-radius: 6px; padding: .1rem .6rem; cursor: pointer; }
.finding .apply:hover { background: #3a5; color: #fff; }
.finding.done { opacity: .45; }
</style>
<h1>amnesia <small>deletes move to ~/.claude/memory-trash/</small></h1>
<input id=q placeholder="Search name, description, body, project&hellip;" autofocus>
<div id=count></div>
<div id=analysis></div>
<div id=list></div>
<script>
let mems = [], findings = null;
const $ = id => document.getElementById(id);
const TITLES = { contradictions: 'Contradictions', stale: 'Stale / superseded', duplicates: 'Duplicates' };
async function load() {
  mems = await (await fetch('/api/memories')).json();
  const r = await fetch('/api/analysis');
  findings = r.ok ? await r.json() : null;
  render(); renderAnalysis();
}
function renderAnalysis() {
  const box = $('analysis'); box.textContent = '';
  if (!findings) {
    const hint = document.createElement('div'); hint.id = 'ahint';
    hint.textContent = 'No analysis yet — run `amnesia analyze` in a terminal to audit for contradictions, stale facts, and duplicates.';
    box.appendChild(hint); return;
  }
  const ops = findings.ops || [];
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
      const c = document.createElement('span'); c.className = 'chip'; c.textContent = o.from;
      c.onclick = () => { $('q').value = o.from.split('/').pop(); render(); };
      d.appendChild(c);
      box.appendChild(d);
    }
  }
  for (const kind of Object.keys(TITLES)) {
    const items = findings[kind] || [];
    if (!items.length) continue;
    const h = document.createElement('h3'); h.textContent = TITLES[kind] + ' (' + items.length + ')';
    box.appendChild(h);
    for (const it of items) {
      const d = document.createElement('div'); d.className = 'finding k-' + kind;
      const t = document.createElement('b'); t.textContent = it.title;
      const p = document.createElement('p'); p.textContent = it.detail;
      d.append(t, p);
      for (const f of it.files || []) {
        const c = document.createElement('span'); c.className = 'chip'; c.textContent = f;
        c.onclick = () => { $('q').value = f.split('/').pop(); render(); };
        d.appendChild(c);
      }
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
      const h = document.createElement('h2'); h.textContent = proj; list.appendChild(h);
    }
    const card = document.createElement('div'); card.className = 'card';
    const top = document.createElement('div'); top.className = 'top';
    const name = document.createElement('b'); name.textContent = m.file;
    const badge = document.createElement('span'); badge.className = 'badge'; badge.textContent = m.type || '?';
    const date = document.createElement('span'); date.className = 'date';
    date.textContent = new Date(m.mtime * 1000).toLocaleDateString();
    const del = document.createElement('button'); del.textContent = 'delete';
    del.onclick = async () => {
      const r = await fetch('/api/delete', { method: 'POST',
        body: JSON.stringify({ project: m.project, file: m.file }) });
      if (r.ok) { mems = mems.filter(x => x !== m); render(); }
      else alert('delete failed: ' + (await r.json()).error);
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

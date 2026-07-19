#!/usr/bin/env python3
"""amnesia — view, search, clean, and sanity-check your Claude Code memory.

Local-only, zero dependencies. The analyzer runs on your own `claude` CLI,
so it needs no API key and nothing leaves your machine except via your
existing Claude account.

  amnesia            serve the UI on http://localhost:8780
  amnesia analyze    audit memories for contradictions, stale facts, duplicates
  amnesia --check    run self-tests

Forgotten memories move to ~/.claude/memory-trash/<project>/; all UI changes
also get hash-guarded recovery snapshots under ~/.claude/amnesia/history/.
"""
import errno, hashlib, hmac, json, os, re, secrets, shutil, subprocess, sys, threading, time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path.home() / ".claude" / "projects"
TRASH = Path.home() / ".claude" / "memory-trash"
ANALYSIS = Path.home() / ".claude" / "amnesia" / "analysis.json"
STATE = Path.home() / ".claude" / "amnesia" / "state.json"
HISTORY = Path.home() / ".claude" / "amnesia" / "history"
SESSION = secrets.token_urlsafe(24)
STATE_LOCK = threading.Lock()
MUTATION_LOCK = threading.RLock()


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def memory_fingerprint(root=None):
    return hashlib.sha256(build_dump(root).encode()).hexdigest()


def _state(path=None):
    path = path or STATE
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    data["decisions"] = data.get("decisions") or {}
    return data


def save_decision(finding_id, decision, path=None, operation=None):
    if not isinstance(finding_id, str) or not finding_id or len(finding_id) > 500:
        raise ValueError("bad finding id")
    if decision not in {"kept", "forgot", "moved", "merged", "fixed"}:
        raise ValueError("bad decision")
    path = path or STATE
    with STATE_LOCK:
        data = _state(path)
        data["decisions"][finding_id] = {"decision": decision, "at": time.time()}
        if operation:
            data["decisions"][finding_id]["operation"] = operation
        _write_json(path, data)


def undo_decisions(operation, path=None):
    path = path or STATE
    with STATE_LOCK:
        data = _state(path)
        kept = {key: value for key, value in data["decisions"].items()
                if value.get("operation") != operation}
        if len(kept) != len(data["decisions"]):
            data["decisions"] = kept
            _write_json(path, data)


def clear_decisions(path=None):
    path = path or STATE
    with STATE_LOCK:
        data = _state(path)
        if data["decisions"]:
            data["decisions"] = {}
            _write_json(path, data)


def finding_id(kind, item):
    clean = {k: v for k, v in item.items() if not k.startswith("_")}
    raw = json.dumps([kind, clean], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def local_request_host(headers):
    try:
        return urlsplit("//" + headers.get("Host", "")).hostname in {"127.0.0.1", "localhost"}
    except ValueError:
        return False


def post_request_error(headers):
    ctype = headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if ctype != "application/json":
        return 415, "application/json required"
    if not hmac.compare_digest(headers.get("X-Amnesia-Token", ""), SESSION):
        return 403, "invalid session"
    origin, host = headers.get("Origin"), headers.get("Host")
    if not local_request_host(headers):
        return 403, "invalid host"
    if (origin and urlsplit(origin).netloc != host) or headers.get("Sec-Fetch-Site") == "cross-site":
        return 403, "cross-site request blocked"
    return None


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


def delete_memory(project, filename, root=None, trash=None, history=None):
    root = root or ROOT
    trash = trash or TRASH
    # trust boundary: request body names the file; never let it escape the memory dir
    if (not isinstance(project, str) or not isinstance(filename, str) or
            "/" in project or "/" in filename or ".." in project or ".." in filename):
        raise ValueError("bad path")
    src = root / project / "memory" / filename
    if not src.is_file():
        raise FileNotFoundError(filename)
    index = src.parent / "MEMORY.md"
    dest = _available_trash_path(trash / project / filename)
    return _recorded(f"Forgot {project}/{filename}",
                     [("root", src), ("root", index), ("trash", dest)],
                     lambda: _delete_raw(src, index, dest, filename), root, trash, history)


def restore_memory(project, filename, root=None, trash=None, history=None):
    root = root or ROOT
    trash = trash or TRASH
    # trust boundary: same validation as delete_memory
    if (not isinstance(project, str) or not isinstance(filename, str) or
            "/" in project or "/" in filename or ".." in project or ".." in filename):
        raise ValueError("bad path")
    src = trash / project / filename
    if not src.is_file():
        raise FileNotFoundError(filename)
    mdir = root / project / "memory"
    if (mdir / filename).exists():
        raise ValueError("exists: " + filename)
    dest, index = mdir / filename, mdir / "MEMORY.md"

    def restore():
        mdir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
        _index_add(mdir, filename)

    return _recorded(f"Restored {project}/{filename}",
                     [("trash", src), ("root", dest), ("root", index)],
                     restore, root, trash, history)


def _split_ref(ref):
    if not isinstance(ref, str):
        raise ValueError("bad ref")
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


def _available_trash_path(path):
    if not path.exists():
        return path
    return path.with_name(f"{path.stem}-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}{path.suffix}")


def _delete_raw(src, index, dest, filename):
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))
    if index.is_file():
        lines = index.read_text().splitlines(keepends=True)
        index.write_text("".join(line for line in lines if f"({filename})" not in line))


def _file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _require_inside(path, base):
    try:
        relative = path.relative_to(base)
    except ValueError:
        raise ValueError("memory path escapes its store")
    current = base
    if current.is_symlink():
        raise ValueError("memory path contains a symlink")
    for piece in relative.parts:
        current /= piece
        if current.is_symlink():
            raise ValueError("memory path contains a symlink")
    try:
        path.resolve().relative_to(base.resolve())
    except ValueError:
        raise ValueError("memory path escapes its store")


def _recorded(label, paths, action, root, trash, history):
    with MUTATION_LOCK:
        return _recorded_unlocked(label, paths, action, root, trash, history)


def _recorded_unlocked(label, paths, action, root, trash, history):
    bases = {"root": root, "trash": trash}
    for scope, path in paths:
        _require_inside(path, bases[scope])
    if history is None:
        action()
        return None
    seen, changes = set(), []
    for scope, path in paths:
        rel = path.relative_to(bases[scope]).as_posix()
        key = (scope, rel)
        if key not in seen:
            seen.add(key)
            changes.append({"scope": scope, "path": rel})
    op_id = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
    opdir = history / op_id
    opdir.mkdir(parents=True)
    for i, change in enumerate(changes):
        path = bases[change["scope"]] / change["path"]
        parent = path.parent
        change["new_parents"] = []
        while parent != bases[change["scope"]] and not parent.exists():
            change["new_parents"].append(parent.relative_to(bases[change["scope"]]).as_posix())
            parent = parent.parent
        if path.is_file():
            stat = path.stat()
            backup = str(i)
            (opdir / backup).write_bytes(path.read_bytes())
            change["before"] = backup
            change["mode"] = stat.st_mode
            change["mtime_ns"] = stat.st_mtime_ns
        else:
            change["before"] = None
    manifest = {"id": op_id, "label": label, "at": time.time(), "undone": False, "changes": changes}
    try:
        action()
        for change in changes:
            change["after"] = _file_hash(bases[change["scope"]] / change["path"])
        _write_json(opdir / "manifest.json", manifest)
    except BaseException:
        _restore_changes(manifest, root, trash, opdir)
        shutil.rmtree(opdir, ignore_errors=True)
        raise
    return op_id


def _manifest_path(scope, rel, root, trash):
    if scope not in {"root", "trash"} or not isinstance(rel, str):
        raise ValueError("bad recovery record")
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts:
        raise ValueError("bad recovery path")
    base = {"root": root, "trash": trash}[scope]
    path = base / p
    _require_inside(path, base)
    return path


def _restore_changes(manifest, root, trash, opdir):
    created = set()
    for change in manifest["changes"]:
        path = _manifest_path(change["scope"], change["path"], root, trash)
        backup = change.get("before")
        if backup is None:
            if path.is_file():
                path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((opdir / backup).read_bytes())
            os.chmod(path, change["mode"])
            os.utime(path, ns=(change["mtime_ns"], change["mtime_ns"]))
        created.update((change["scope"], rel) for rel in change.get("new_parents", []))
    for scope, rel in sorted(created, key=lambda item: item[1].count("/"), reverse=True):
        path = _manifest_path(scope, rel, root, trash)
        try:
            path.rmdir()
        except OSError:
            pass


def list_history(history=None, limit=20):
    history = history or HISTORY
    out = []
    if not history.is_dir():
        return out
    for path in history.glob("*/manifest.json"):
        try:
            item = json.loads(path.read_text())
            out.append({k: item[k] for k in ("id", "label", "at", "undone")})
        except (KeyError, json.JSONDecodeError, OSError):
            continue
    return sorted(out, key=lambda item: item["at"], reverse=True)[:limit]


def undo_operation(op_id, root=None, trash=None, history=None, state_path=None):
    with MUTATION_LOCK:
        return _undo_operation_unlocked(op_id, root, trash, history, state_path)


def _undo_operation_unlocked(op_id, root=None, trash=None, history=None, state_path=None):
    root, trash, history = root or ROOT, trash or TRASH, history or HISTORY
    if not re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{6}", op_id or ""):
        raise ValueError("bad operation id")
    opdir = history / op_id
    try:
        manifest = json.loads((opdir / "manifest.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        raise FileNotFoundError(op_id)
    if manifest.get("id") != op_id or manifest.get("undone"):
        raise ValueError("already undone")
    for change in manifest["changes"]:
        path = _manifest_path(change["scope"], change["path"], root, trash)
        if _file_hash(path) != change.get("after"):
            raise ValueError(f"cannot undo: {change['path']} changed afterward")
    _restore_changes(manifest, root, trash, opdir)
    manifest["undone"] = True
    manifest["undone_at"] = time.time()
    _write_json(opdir / "manifest.json", manifest)
    if state_path or history == HISTORY:
        undo_decisions(op_id, state_path)


def _op_paths(op, src, dest, root, trash):
    sproj, sfile = _split_ref(src)
    spath = root / sproj / "memory" / sfile
    if not spath.is_file():
        raise FileNotFoundError(src)
    sindex = spath.parent / "MEMORY.md"
    if op == "move":
        if not dest or "/" in dest or ".." in dest:
            raise ValueError("bad dest: " + dest)
        ddir = root / dest / "memory"
        if (ddir / sfile).exists():
            raise ValueError(f"exists: {dest}/{sfile}")
        return [("root", spath), ("root", sindex), ("root", ddir / sfile), ("root", ddir / "MEMORY.md")]
    if op == "merge":
        dproj, dfile = _split_ref(dest)
        dpath = root / dproj / "memory" / dfile
        if not dpath.is_file():
            raise FileNotFoundError(dest)
        if dpath == spath:
            raise ValueError("cannot merge a memory into itself")
        return [("root", spath), ("root", sindex), ("root", dpath),
                ("trash", _available_trash_path(trash / sproj / sfile))]
    raise ValueError("unknown op: " + op)


def apply_op(op, src, dest, root=None, trash=None, history=None):
    root = root or ROOT
    trash = trash or TRASH
    paths = _op_paths(op, src, dest, root, trash)
    label = ("Moved " + src + " → " + dest) if op == "move" else ("Combined " + src + " → " + dest)
    return _recorded(label, paths, lambda: _apply_op_raw(op, src, dest, root, paths),
                     root, trash, history)


def _apply_op_raw(op, src, dest, root, paths):
    sproj, sfile = _split_ref(src)
    spath = root / sproj / "memory" / sfile
    sindex = spath.parent / "MEMORY.md"
    if op == "move":
        ddir = root / dest / "memory"
        ddir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(spath), str(ddir / sfile))
        if sindex.is_file():
            lines = sindex.read_text().splitlines(keepends=True)
            sindex.write_text("".join(line for line in lines if f"({sfile})" not in line))
        _index_add(ddir, sfile)
    else:
        dproj, dfile = _split_ref(dest)
        dpath = root / dproj / "memory" / dfile
        _, body = parse_frontmatter(spath.read_text(errors="replace"))
        dpath.write_text(dpath.read_text().rstrip("\n") + f"\n\n**Merged from {src} by amnesia:** {body}\n")
        _delete_raw(spath, sindex, paths[-1][1], sfile)


def apply_batch(ops, root=None, trash=None, history=None):
    root, trash = root or ROOT, trash or TRASH
    if not isinstance(ops, list) or not ops or len(ops) > 100:
        raise ValueError("bad operations")
    sources, destinations, move_targets = [], [], []
    for item in ops:
        if not isinstance(item, dict):
            raise ValueError("bad operation")
        op, src, dest = item.get("op"), item.get("from"), item.get("to")
        _, sfile = _split_ref(src)
        if op == "move":
            if not isinstance(dest, str) or not dest or "/" in dest or ".." in dest:
                raise ValueError("bad dest")
            target = f"{dest}/{sfile}"
            move_targets.append(target)
            destinations.append(target)
        elif op == "merge":
            _split_ref(dest)
            destinations.append(dest)
        else:
            raise ValueError("unknown op")
        sources.append(src)
    if len(set(sources)) != len(sources) or len(set(move_targets)) != len(move_targets):
        raise ValueError("conflicting batch operations")
    if set(sources) & set(destinations):
        raise ValueError("batch operations depend on each other")
    paths, plans = [], []
    for item in ops:
        item_paths = _op_paths(item.get("op"), item.get("from"), item.get("to"), root, trash)
        paths.extend(item_paths)
        plans.append((item, item_paths))

    def apply():
        for item, item_paths in plans:
            _apply_op_raw(item["op"], item["from"], item["to"], root, item_paths)

    return _recorded(f"Fixed {len(ops)} finding{'s' if len(ops) != 1 else ''}",
                     paths, apply, root, trash, history)


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


def analysis_payload(findings, root=None, fingerprint=None):
    data = {k: findings.get(k) or [] for k in ("contradictions", "stale", "duplicates", "ops")}
    data["_meta"] = {"scanned_at": time.time(), "fingerprint": fingerprint or memory_fingerprint(root)}
    return data


def read_analysis(path=None, root=None):
    path, root = path or ANALYSIS, root or ROOT
    data = json.loads(path.read_text())
    for kind in ("contradictions", "stale", "duplicates", "ops"):
        data[kind] = data.get(kind) or []
        for item in data[kind]:
            item["_id"] = finding_id(kind, item)
    meta = data.get("_meta") or {}
    meta["stale"] = meta.get("fingerprint") != memory_fingerprint(root)
    data["_meta"] = meta
    return data


def analyze(root=None, out=None):
    dump = build_dump(root)
    fingerprint = hashlib.sha256(dump.encode()).hexdigest()
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
    _write_json(out, analysis_payload(findings, root, fingerprint))
    if root is None and out == ANALYSIS:
        clear_decisions()
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
.wm { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 1.15rem; font-weight: 600;
  cursor: pointer; color: var(--text); background: none; border: 0; padding: .2rem; }
.wm i { font-style: normal; }
.wm i:nth-of-type(1){opacity:.75} .wm i:nth-of-type(2){opacity:.55}
.wm i:nth-of-type(3){opacity:.35} .wm i:nth-of-type(4){opacity:.18}
#q { flex: 1; min-width: 220px; padding: .45rem .9rem; font-size: .95rem; color: var(--text);
  border: 1px solid var(--border); border-radius: 99px; background: var(--card); outline: none; }
#q:focus { border-color: var(--accent); }
button:focus-visible, a:focus-visible, input:focus-visible, select:focus-visible,
summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
  overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
#score { font-size: .8rem; color: var(--ok); white-space: nowrap; }
main, footer { position: relative; z-index: 1; }
main { max-width: 960px; margin: 0 auto; padding: 1rem; }
#cv { position: fixed; inset: 0; width: 100vw; height: 100vh; z-index: 0; display: none; }
body[data-view=home] #cv { display: block; opacity: .4; pointer-events: none; }
body[data-view=map] #cv { display: block; cursor: grab; }
body[data-view=map] footer { display: none; }
.mapbar { position: fixed; top: 3.9rem; left: 0; right: 0; text-align: center;
  color: var(--muted); font-size: .84rem; }
.mapbar .link { margin-left: .8rem; }
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
.cost { color: var(--muted); font-size: .88rem; margin: .5rem auto 0; }
.cost b { color: var(--warn); font-weight: 600; }
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
.link { color: var(--muted); text-decoration: underline; cursor: pointer; background: none;
  border: 0; padding: .2rem; font: inherit; }
.links .link { margin: 0 .4rem; }
.link:hover { color: var(--text); }
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
.controls { display: flex; gap: .55rem; flex-wrap: wrap; margin: .7rem 0; }
.controls select { color: var(--text); background: var(--card); border: 1px solid var(--border);
  border-radius: 8px; padding: .35rem .55rem; }
.history { max-width: 620px; margin: 1.8rem auto; }
.history h1 { font-size: 1.25rem; font-weight: 500; }
.history-row { display: flex; align-items: center; gap: .8rem; padding: .7rem 0;
  border-top: 1px solid var(--border); }
.history-row .desc { flex: 1; }
.stale { color: var(--warn); }
#toast { position: fixed; left: 50%; transform: translateX(-50%); bottom: 1.3rem; z-index: 9;
  background: light-dark(#26262e, #e9e9ee); color: light-dark(#fff, #16161a);
  padding: .55rem 1.1rem; border-radius: 99px; font-size: .86rem; box-shadow: 0 6px 24px #0005; }
#toast .link { color: light-dark(#a5a5ff, #4a4ad0); margin-left: .7rem; }
footer { max-width: 960px; width: 100%; margin: auto auto 0; padding: 1rem; border-top: 1px solid var(--border);
  color: var(--muted); font-size: .78rem; text-align: center; }
footer a { color: var(--accent); text-decoration: none; }
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { scroll-behavior: auto !important; transition: none !important; }
}
</style>
<header><div class=bar>
  <button class=wm id=wmk aria-label="Amnesia home">amn<i>e</i><i>s</i><i>i</i><i>a</i></button>
  <label class=sr-only for=q>Search memories</label>
  <input id=q type=search placeholder="Search everything your agent remembers&hellip;">
  <span id=score aria-live=polite></span>
</div></header>
<main>
<section id=home aria-live=polite></section>
<section id=queue hidden aria-live=polite></section>
<section id=browse hidden></section>
<section id=map hidden><div class=mapbar></div><div id=tip hidden></div><div id=ncard hidden></div>
<div class=legend></div></section>
<section id=recovery hidden></section>
</main>
<canvas id=cv role=img aria-label="Visual map of memories; use Browse everything for an accessible list"></canvas>
<div id=toast role=status aria-live=polite hidden></div>
<footer>changes are backed up locally and can be undone from recent changes
&middot; <a href="https://github.com/tiny-cloud-ventures/amnesia">github</a> &middot; MIT</footer>
<script>
let mems = [], analysis = null, findings = [], byRef = Object.create(null);
let qtotal = 0, qdone = 0, cleaned = 0, view = 'home', toastTimer = null;
let appState = { decisions: {} }, browseSort = 'newest', browseProject = '';
let status = { running: false, error: null };
const SESSION = '__AMNESIA_SESSION__';
const $ = id => document.getElementById(id);
const TYPE_LABEL = { project: 'project', feedback: 'rule', user: 'about you', reference: 'link' };
const KIND_COLOR = { conflict: 'var(--danger)', misfiled: 'var(--info)', twice: 'var(--accent)', stale: 'var(--warn)' };
const KIND_SKIP = { conflict: 'Keep both', misfiled: 'Leave it', twice: 'Leave it', stale: 'Still true' };
const post = (path, body = {}) => fetch(path, { method: 'POST',
  headers: { 'Content-Type': 'application/json', 'X-Amnesia-Token': SESSION },
  body: JSON.stringify(body) });
const analysisFresh = () => analysis && !(analysis._meta || {}).stale;
const analysisCount = () => ['contradictions', 'stale', 'duplicates', 'ops']
  .reduce((n, kind) => n + ((analysis && analysis[kind]) || []).length, 0);
const quickId = (kind, files, title = '') => 'quick:' + kind + ':' + files.slice().sort().join('|') + ':' + title;

// ponytail: chars/4 token estimate — real tokenizers disagree by ~10% anyway
const tokOf = m => Math.max(1, Math.round((m.body.length + m.description.length) / 4));
const idxTok = () => Math.round(mems.reduce((s, m) =>
  s + m.name.length + m.file.length + m.description.length + 8, 0) / 4);
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
  byRef = Object.create(null);
  for (const m of mems) byRef[m.project + '/' + m.file] = m;
}
function show(id) {
  view = id;
  document.body.dataset.view = id;
  for (const s of ['home', 'queue', 'browse', 'map', 'recovery']) $(s).hidden = s !== id;
  animate();
}
function renderScore() {
  $('score').textContent = cleaned > 0 ? '✓ ' + cleaned + ' cleaned' : '';
}

function buildFindings() {
  const out = [];
  if (analysisFresh()) {
    for (const it of analysis.contradictions || [])
      out.push({ id: it._id, kind: 'conflict', q: 'These can’t both be true', title: it.title, detail: it.detail, files: it.files || [] });
    for (const o of analysis.ops || []) out.push(o.op === 'move'
      ? { id: o._id, kind: 'misfiled', q: 'This seems filed in the wrong project', detail: o.reason, files: [o.from], op: o }
      : { id: o._id, kind: 'twice', q: 'Your agent remembers this twice', detail: o.reason, files: [o.from, o.to], op: o });
    for (const it of analysis.stale || [])
      out.push({ id: it._id, kind: 'stale', q: 'This looks out of date', title: it.title, detail: it.detail, files: it.files || [] });
    for (const it of analysis.duplicates || [])
      out.push({ id: it._id, kind: 'twice', q: 'Your agent remembers this twice', title: it.title, detail: it.detail, files: it.files || [] });
  } else {
    const exact = Object.create(null);
    for (const m of mems) {
      const key = m.body.trim().replace(/\\s+/g, ' ');
      if (key) (exact[key] = exact[key] || []).push(m);
    }
    for (const ms of Object.values(exact)) if (ms.length > 1) {
      const files = ms.map(m => m.project + '/' + m.file);
      out.push({ id: quickId('twice', files), kind: 'twice',
        q: 'The same memory appears in ' + ms.length + ' places',
        detail: 'These files contain the same memory. Usually only one copy needs to stay active.', files });
    }
    const old = mems.filter(m => (Date.now() / 1000 - m.mtime) > 90 * 86400)
      .sort((a, b) => a.mtime - b.mtime).slice(0, 5);
    for (const m of old) {
      const files = [m.project + '/' + m.file];
      out.push({ id: quickId('stale', files), kind: 'stale', q: 'Untouched for ' + Math.round((Date.now() / 1000 - m.mtime) / 86400) + ' days',
        detail: 'Old memories drift out of date. Does this still describe reality?',
        files });
    }
  }
  return out.filter(f => !appState.decisions[f.id]);
}

// ---- the map: every memory a star, colored by project, wired by its links ----
const NAME_RE = /\\[\\[([\\w-]+)\\]\\]/g;
function buildGraph(ms, an) {
  const nodes = ms.map((m, i) => ({ i, m }));
  const byR = Object.create(null), byName = Object.create(null), byBody = Object.create(null);
  for (const n of nodes) {
    byR[n.m.project + '/' + n.m.file] = n.i;
    (byName[n.m.name] = byName[n.m.name] || []).push(n.i);
    const body = n.m.body.trim().replace(/\\s+/g, ' ');
    if (body) (byBody[body] = byBody[body] || []).push(n.i);
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
  for (const idxs of Object.values(byBody))
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
const REDUCED = matchMedia('(prefers-reduced-motion: reduce)');
let animationFrame = null;
function edgeColor(kind) {
  return { link: '#8888aa' + (DARK.matches ? '55' : '66'), twin: '#8b8bf588',
    conflict: '#e5484dcc' }[kind];
}
function nodeColor(n) { return 'hsl(' + n.hue + ' 62% ' + (DARK.matches ? 62 : 46) + '%)'; }

function refreshGraph() {
  const old = Object.create(null);
  for (const n of G.nodes) old[n.m.project + '/' + n.m.file] = n;
  G = buildGraph(mems, analysisFresh() ? analysis : null);
  const nproj = Object.create(null);
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
  animate();
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
  animationFrame = null;
  if (document.hidden || (view !== 'home' && view !== 'map')) return;
  if (!REDUCED.matches) tick();
  draw(t);
  if (!REDUCED.matches) animationFrame = requestAnimationFrame(loop);
}
function animate() {
  if (!animationFrame && !document.hidden && (view === 'home' || view === 'map'))
    animationFrame = requestAnimationFrame(loop);
}
document.addEventListener('visibilitychange', animate);
REDUCED.addEventListener('change', animate);

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
    $('tip').hidden = true; animate(); return;
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
  animate();
};

function openNodeCard(n) {
  const c = $('ncard'); c.textContent = '';
  const top = document.createElement('div'); top.className = 'top';
  const t = document.createElement('span'); t.className = 't'; t.textContent = titleOf(n.m);
  const badge = document.createElement('span');
  badge.className = 'badge t-' + (n.m.type || 'unknown');
  badge.textContent = TYPE_LABEL[n.m.type] || n.m.type || '?';
  const tk = document.createElement('span'); tk.className = 'badge';
  tk.textContent = '~' + tokOf(n.m) + ' tok';
  top.append(t, badge, tk);
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
  const a = document.createElement('button'); a.className = 'link'; a.textContent = '← back'; a.onclick = renderHome;
  bar.appendChild(a);
  const leg = document.querySelector('.legend'); leg.textContent = '';
  const item = (color, label) => {
    const i = document.createElement('i'); i.style.background = color;
    leg.append(i, ' ' + label);
  };
  leg.append('colors = projects');
  item('#8888aa', 'linked memories'); item('#8b8bf5', 'remembered twice');
  if (analysisFresh() && (analysis.contradictions || []).length) item('#e5484d', 'contradiction');
}

function markAnalysisStale() {
  if (analysis) (analysis._meta = analysis._meta || {}).stale = true;
}
async function reloadMemories() {
  mems = await (await fetch('/api/memories')).json();
  buildRefs(); refreshGraph();
}
async function deleteMem(m, finding = null) {
  const r = await post('/api/delete', { project: m.project, file: m.file,
    finding: finding && finding.id, decision: 'forgot' });
  if (!r.ok) { alert('failed: ' + (await r.json()).error); return false; }
  const result = await r.json();
  mems = mems.filter(x => x !== m);
  delete byRef[m.project + '/' + m.file];
  if (finding) appState.decisions[finding.id] = { decision: 'forgot' };
  markAnalysisStale(); cleaned++; renderScore();
  flash('Forgot “' + titleOf(m).slice(0, 42) + '”', result.operation); refreshGraph();
  return true;
}
async function undoChange(operation) {
  const r = await post('/api/undo', { operation });
  if (!r.ok) { alert('undo failed: ' + (await r.json()).error); return false; }
  await reloadMemories();
  appState = await (await fetch('/api/state')).json();
  const scan = await fetch('/api/analysis');
  analysis = scan.ok ? await scan.json() : null;
  findings = buildFindings();
  cleaned = Math.max(0, cleaned - 1); renderScore();
  flash('Change restored.');
  if (view === 'recovery') renderRecovery();
  else if (view === 'browse') renderBrowse();
  else renderHome();
  return true;
}
function flash(msg, operation = null) {
  const t = $('toast'); t.textContent = msg; t.hidden = false;
  if (operation) {
    const u = document.createElement('button'); u.className = 'link'; u.textContent = 'undo';
    u.onclick = () => undoChange(operation); t.appendChild(u);
  }
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 8000);
}
async function fixAll() {
  const fs = findings.filter(f => f.op), ops = fs.map(f => f.op);
  const preview = ops.map(o => '• ' + (o.op === 'move' ? 'Move ' : 'Combine ') + o.from + ' → ' + o.to).join('\\n');
  if (!ops.length || !confirm('Apply these changes as one undoable batch?\\n\\n' + preview)) return;
  const r = await post('/api/batch', { ops, finding_ids: fs.map(f => f.id) });
  if (!r.ok) { alert('fix all failed: ' + (await r.json()).error); return; }
  const result = await r.json();
  for (const f of fs) appState.decisions[f.id] = { decision: 'fixed' };
  await reloadMemories(); markAnalysisStale(); findings = buildFindings();
  cleaned += ops.length; renderScore();
  flash('Fixed ' + ops.length + ' as one batch.', result.operation);
  renderHome();
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
  if (mems.length) {
    const cost = document.createElement('p'); cost.className = 'cost';
    const ct = document.createElement('b'); ct.textContent = '~' + idxTok().toLocaleString() + ' tokens';
    cost.append('Just indexing them costs ', ct, ' of context.');
    wrap.appendChild(cost);
  }
  const sub = document.createElement('p'); sub.className = 'sub';
  const btn = document.createElement('button'); btn.className = 'primary';
  if (!mems.length) {
    big.textContent = 'No memories found — your agent has a clean slate.';
    sub.textContent = 'Memories appear in ~/.claude/projects/ as you use Claude Code.';
    const links = document.createElement('div'); links.className = 'links';
    const rec = document.createElement('button'); rec.className = 'link'; rec.textContent = 'recent changes';
    rec.onclick = renderRecovery; links.appendChild(rec);
    wrap.append(sub, links); h.appendChild(wrap); return;
  }
  if (status.running) {
    sub.textContent = 'Reading every memory and cross-checking them… this takes a few minutes.';
    btn.textContent = 'Scanning…'; btn.disabled = true;
  } else if (status.error) {
    sub.className = 'sub err'; sub.textContent = status.error;
    btn.textContent = 'Try again'; btn.onclick = doScan;
  } else if (analysis && !analysisFresh()) {
    sub.className = 'sub stale';
    const when = analysis._meta && analysis._meta.scanned_at
      ? ' Last scanned ' + new Date(analysis._meta.scanned_at * 1000).toLocaleString() + '.' : '';
    sub.textContent = 'Your memories changed, so the previous scan is out of date.' + when;
    btn.textContent = 'Scan again'; btn.onclick = doScan;
  } else if (findings.length) {
    const n = document.createElement('b'); n.textContent = findings.length + ' of them';
    sub.append(n, analysisFresh() ? ' need a decision from you.' : ' look worth a glance.');
    btn.textContent = analysisFresh() ? 'Review them' : 'Scan my memories';
    btn.onclick = analysisFresh() ? startQueue : doScan;
  } else if (analysisFresh()) {
    sub.textContent = analysisCount()
      ? 'Review complete — you decided every finding from this scan. ✓'
      : 'Everything checks out — no conflicts, nothing stale. ✓';
    btn.textContent = 'Rescan'; btn.onclick = doScan;
  } else {
    sub.textContent = 'A scan reads them all and flags anything conflicting, stale, or misfiled.';
    btn.textContent = 'Scan my memories'; btn.onclick = doScan;
  }
  wrap.appendChild(sub); wrap.appendChild(btn);
  const links = document.createElement('div'); links.className = 'links';
  if (!analysis && findings.length && !status.running) {
    const a = document.createElement('button'); a.className = 'link'; a.textContent = 'review ' + findings.length + ' quick flags';
    a.onclick = startQueue; links.appendChild(a);
  }
  if (analysisFresh() && findings.length && !status.running) {
    const nops = findings.filter(f => f.op).length;
    if (nops) {
      const fx = document.createElement('button'); fx.className = 'link';
      fx.textContent = nops > 1 ? 'fix all ' + nops + ' automatically' : 'fix it automatically';
      fx.onclick = fixAll; links.appendChild(fx);
    }
    const a = document.createElement('button'); a.className = 'link'; a.textContent = 'rescan'; a.onclick = doScan;
    links.appendChild(a);
  }
  const mp = document.createElement('button'); mp.className = 'link'; mp.textContent = 'see the map ✨';
  mp.onclick = renderMap; links.appendChild(mp);
  const b = document.createElement('button'); b.className = 'link'; b.textContent = 'browse everything';
  b.onclick = () => renderBrowse(); links.appendChild(b);
  const rec = document.createElement('button'); rec.className = 'link'; rec.textContent = 'recent changes';
  rec.onclick = renderRecovery; links.appendChild(rec);
  wrap.appendChild(links);
  h.appendChild(wrap);
}

async function doScan() {
  const started = await post('/api/analyze');
  if (!started.ok) { status.error = (await started.json()).error; renderHome(); return; }
  status.running = true; status.error = null; renderHome();
  const poll = setInterval(async () => {
    status = await (await fetch('/api/status')).json();
    if (!status.running) {
      clearInterval(poll);
      if (!status.error) {
        const r = await fetch('/api/analysis');
        analysis = r.ok ? await r.json() : null;
        appState = await (await fetch('/api/state')).json();
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
    f.onclick = consume;
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
  const consume = async decision => {
    const r = await post('/api/decision', { finding: f.id, decision });
    if (!r.ok) { alert('failed to save decision: ' + (await r.json()).error); return; }
    appState.decisions[f.id] = { decision };
    findings.shift(); qdone++; renderQueue();
  };
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
  const forget = async ref => {
    const m = byRef[ref];
    const fromScan = analysisFresh();
    if (m && await deleteMem(m, f)) {
      findings = buildFindings();
      if (fromScan) renderHome();
      else { qdone++; renderQueue(); }
    }
  };
  for (const ref of f.files) card.appendChild(mrowEl(ref, perRow, () => forget(ref)));
  const btns = document.createElement('div'); btns.className = 'qbtns';
  if (f.op) {
    const go = document.createElement('button'); go.className = 'primary';
    go.textContent = f.op.op === 'move' ? 'Move it' : 'Combine them';
    go.onclick = async () => {
      const r = await post('/api/apply', { op: f.op.op, from: f.op.from, to: f.op.to,
        finding: f.id, decision: f.op.op === 'move' ? 'moved' : 'merged' });
      if (!r.ok) { alert('failed: ' + (await r.json()).error); return; }
      const result = await r.json();
      appState.decisions[f.id] = { decision: f.op.op === 'move' ? 'moved' : 'merged' };
      await reloadMemories(); markAnalysisStale(); findings = buildFindings();
      cleaned++; renderScore(); flash(f.op.op === 'move' ? 'Memory moved.' : 'Memories combined.', result.operation);
      renderHome();
    };
    btns.appendChild(go);
  }
  const skip = document.createElement('button'); skip.className = 'ghost';
  skip.textContent = KIND_SKIP[f.kind]; skip.onclick = () => consume('kept');
  btns.appendChild(skip);
  card.appendChild(btns);
  const back = document.createElement('div'); back.className = 'links';
  back.style.textAlign = 'center';
  const a = document.createElement('button'); a.className = 'link'; a.textContent = 'finish later'; a.onclick = renderHome;
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
  const tk = document.createElement('span'); tk.className = 'badge';
  tk.textContent = '~' + tokOf(m) + ' tok';
  const del = document.createElement('button'); del.className = 'forget'; del.textContent = 'forget';
  del.onclick = async () => { if (await deleteMem(m)) renderBrowse(); };
  top.append(t, badge, tk, del);
  const det = document.createElement('details'); det.className = 'body';
  const sum = document.createElement('summary'); sum.textContent = 'details';
  const desc = document.createElement('div'); desc.className = 'desc';
  desc.textContent = (m.description ? m.description + ' · ' : '') +
    'modified ' + new Date(m.mtime * 1000).toLocaleDateString();
  const pre = document.createElement('pre'); pre.textContent = m.body;
  det.append(sum, desc, pre);
  card.append(top, det);
  return card;
}
function renderBrowse() {
  show('browse');
  const box = $('browse'); box.textContent = '';
  const q = $('q').value.toLowerCase();
  const projects = [...new Set(mems.map(m => m.project))].sort();
  if (browseProject && !projects.includes(browseProject)) browseProject = '';
  const controls = document.createElement('div'); controls.className = 'controls';
  const project = document.createElement('select'); project.setAttribute('aria-label', 'Filter by project');
  project.appendChild(new Option('All projects', ''));
  for (const p of projects) project.appendChild(new Option(prettyProj(p), p));
  project.value = browseProject; project.onchange = () => { browseProject = project.value; renderBrowse(); };
  const sort = document.createElement('select'); sort.setAttribute('aria-label', 'Sort memories');
  for (const [label, value] of [['Newest first', 'newest'], ['Oldest first', 'oldest'],
    ['Largest first', 'largest'], ['By project', 'project']]) sort.appendChild(new Option(label, value));
  sort.value = browseSort; sort.onchange = () => { browseSort = sort.value; renderBrowse(); };
  controls.append(project, sort); box.appendChild(controls);
  const shown = mems.filter(m => (!browseProject || m.project === browseProject) &&
    [m.project, m.file, m.name, m.description, m.type, m.body].join(' ').toLowerCase().includes(q));
  shown.sort({
    newest: (a, b) => b.mtime - a.mtime,
    oldest: (a, b) => a.mtime - b.mtime,
    largest: (a, b) => b.body.length - a.body.length,
    project: (a, b) => (a.project + titleOf(a)).localeCompare(b.project + titleOf(b)),
  }[browseSort]);
  const count = document.createElement('div'); count.id = 'count';
  count.textContent = q ? shown.length + ' matches' : shown.length + ' memories — click a project to look inside';
  box.appendChild(count);
  const groups = Object.create(null);
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
  const a = document.createElement('button'); a.className = 'link'; a.textContent = 'back';
  a.onclick = () => { $('q').value = ''; browseProject = ''; renderHome(); };
  links.appendChild(a);
  box.appendChild(links);
}

async function renderRecovery() {
  show('recovery');
  const box = $('recovery'); box.textContent = '';
  const wrap = document.createElement('div'); wrap.className = 'history';
  const h = document.createElement('h1'); h.textContent = 'Recent changes';
  const sub = document.createElement('p'); sub.className = 'desc';
  sub.textContent = 'Undo is available while the affected files have not been edited again.';
  wrap.append(h, sub);
  const items = await (await fetch('/api/history')).json();
  if (!items.length) {
    const empty = document.createElement('p'); empty.className = 'sub'; empty.textContent = 'No changes yet.';
    wrap.appendChild(empty);
  }
  for (const item of items) {
    const row = document.createElement('div'); row.className = 'history-row';
    const desc = document.createElement('div'); desc.className = 'desc';
    desc.textContent = item.label + ' · ' + new Date(item.at * 1000).toLocaleString();
    const undo = document.createElement('button'); undo.className = 'ghost';
    undo.textContent = item.undone ? 'restored' : 'undo'; undo.disabled = item.undone;
    undo.onclick = () => undoChange(item.id);
    row.append(desc, undo); wrap.appendChild(row);
  }
  const links = document.createElement('div'); links.className = 'links'; links.style.textAlign = 'center';
  const back = document.createElement('button'); back.className = 'link'; back.textContent = 'back'; back.onclick = renderHome;
  links.appendChild(back); wrap.appendChild(links); box.appendChild(wrap);
}

$('q').oninput = () => { $('q').value ? renderBrowse() : renderHome(); };
$('wmk').onclick = renderHome;
async function load() {
  mems = await (await fetch('/api/memories')).json();
  buildRefs();
  try { appState = await (await fetch('/api/state')).json(); } catch (e) {}
  const r = await fetch('/api/analysis');
  analysis = r.ok ? await r.json() : null;
  try { status = await (await fetch('/api/status')).json(); } catch (e) {}
  findings = buildFindings();
  refreshGraph(); animate();
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
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _request_json(self):
        error = post_request_error(self.headers)
        if error:
            self._send(error[0], {"error": error[1]})
            return None
        size = int(self.headers.get("Content-Length", 0))
        if size < 0 or size > 1_000_000:
            self._send(413, {"error": "request too large"})
            return None
        return json.loads(self.rfile.read(size) or b"{}")

    def do_GET(self):
        if not local_request_host(self.headers):
            return self._send(403, {"error": "invalid host"})
        if self.path == "/":
            self._send(200, PAGE.replace("__AMNESIA_SESSION__", SESSION).encode(), "text/html; charset=utf-8")
        elif self.path == "/api/memories":
            self._send(200, list_memories())
        elif self.path == "/api/analysis":
            if ANALYSIS.is_file():
                self._send(200, read_analysis())
            else:
                self._send(404, {"error": "no analysis yet"})
        elif self.path == "/api/status":
            self._send(200, A_STATE)
        elif self.path == "/api/state":
            self._send(200, _state())
        elif self.path == "/api/history":
            self._send(200, list_history())
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        try:
            req = self._request_json()
            if req is None:
                return
            if self.path == "/api/analyze":
                start_analyze()
                return self._send(200, {"ok": True})
            finding = req.get("finding")
            if finding is not None and (not isinstance(finding, str) or not finding or len(finding) > 500):
                raise ValueError("bad finding id")
            if finding and req.get("decision", "fixed") not in {"kept", "forgot", "moved", "merged", "fixed"}:
                raise ValueError("bad decision")
            if self.path == "/api/delete":
                op_id = delete_memory(req["project"], req["file"], history=HISTORY)
            elif self.path == "/api/restore":
                op_id = restore_memory(req["project"], req["file"], history=HISTORY)
            elif self.path == "/api/apply":
                op_id = apply_op(req["op"], req["from"], req["to"], history=HISTORY)
            elif self.path == "/api/batch":
                ids = req.get("finding_ids", [])
                if not isinstance(ids, list) or any(not isinstance(x, str) or not x or len(x) > 500 for x in ids):
                    raise ValueError("bad finding ids")
                op_id = apply_batch(req["ops"], history=HISTORY)
                for item in ids:
                    save_decision(item, "fixed", operation=op_id)
                return self._send(200, {"ok": True, "operation": op_id})
            elif self.path == "/api/undo":
                undo_operation(req["operation"])
                return self._send(200, {"ok": True})
            elif self.path == "/api/decision":
                save_decision(req["finding"], req["decision"])
                return self._send(200, {"ok": True})
            else:
                return self._send(404, {"error": "not found"})
            if req.get("finding"):
                save_decision(req["finding"], req.get("decision", "fixed"), operation=op_id)
            self._send(200, {"ok": True, "operation": op_id})
        except (ValueError, FileNotFoundError, KeyError, TypeError) as e:
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
        outside = Path(td) / "outside.md"
        outside.write_text("do not touch")
        (mdir / "linked.md").symlink_to(outside)
        try:
            delete_memory("-tmp-proj", "linked.md", root, trash); assert False
        except ValueError:
            pass
        assert outside.read_text() == "do not touch"
        (mdir / "linked-inside.md").symlink_to(mdir / "fact.md")
        try:
            delete_memory("-tmp-proj", "linked-inside.md", root, trash); assert False
        except ValueError:
            pass
        assert (mdir / "fact.md").is_file()
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
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        root, trash, history = base / "projects", base / "trash", base / "history"
        state, report = base / "state.json", base / "analysis.json"
        a, b = root / "-proj-a" / "memory", root / "-proj-b" / "memory"
        a.mkdir(parents=True); b.mkdir(parents=True)
        (a / "move.md").write_text("---\nname: move\ndescription: move me\ntype: project\n---\n\nMove body.")
        (a / "merge.md").write_text("---\nname: merge\ndescription: merge me\ntype: project\n---\n\nMerge body.")
        (a / "MEMORY.md").write_text("## Index\n- [Move](move.md)\n- [Merge](merge.md)\n")
        (b / "canon.md").write_text("---\nname: canon\ndescription: keep\ntype: project\n---\n\nCanonical.")
        c = root / "-proj-c" / "memory"
        c.mkdir(parents=True)
        (c / "move.md").write_text("---\nname: other\ndescription: other\ntype: project\n---\n\nOther.")
        (trash / "-proj-a").mkdir(parents=True)
        (trash / "-proj-a" / "merge.md").write_text("older forgotten copy")
        for bad_ops in [
            [{"op": "move", "from": "-proj-a/move.md", "to": "-one"},
             {"op": "move", "from": "-proj-a/move.md", "to": "-two"}],
            [{"op": "move", "from": "-proj-a/move.md", "to": "-same"},
             {"op": "move", "from": "-proj-c/move.md", "to": "-same"}],
        ]:
            untouched = {p.relative_to(base).as_posix(): p.read_bytes() for p in base.rglob("*") if p.is_file()}
            try:
                apply_batch(bad_ops, root, trash, history); assert False, bad_ops
            except ValueError:
                pass
            unchanged = {p.relative_to(base).as_posix(): p.read_bytes() for p in base.rglob("*") if p.is_file()}
            assert unchanged == untouched, (bad_ops, untouched, unchanged)
        before = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*.md")}
        before_trash = {p.relative_to(trash).as_posix(): p.read_bytes() for p in trash.rglob("*.md")}
        op_id = apply_batch([
            {"op": "move", "from": "-proj-a/move.md", "to": "-proj-b"},
            {"op": "merge", "from": "-proj-a/merge.md", "to": "-proj-b/canon.md"},
        ], root, trash, history)
        assert len(list_history(history)) == 1 and list_history(history)[0]["id"] == op_id
        assert (b / "move.md").is_file() and len(list((trash / "-proj-a").glob("merge*.md"))) == 2
        save_decision("batch-finding", "fixed", state, op_id)
        undo_operation(op_id, root, trash, history, state)
        after = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*.md")}
        after_trash = {p.relative_to(trash).as_posix(): p.read_bytes() for p in trash.rglob("*.md")}
        assert after == before and after_trash == before_trash, (before, after, before_trash, after_trash)
        assert list_history(history)[0]["undone"] is True
        assert "batch-finding" not in _state(state)["decisions"]
        guarded = apply_op("move", "-proj-a/move.md", "-proj-b", root, trash, history)
        (b / "move.md").write_text("edited after move")
        try:
            undo_operation(guarded, root, trash, history); assert False
        except ValueError as e:
            assert "changed afterward" in str(e)
        assert (b / "move.md").read_text() == "edited after move"

        sample = {"contradictions": [{"title": "ports", "detail": "two ports", "files": ["a/x.md"]}]}
        _write_json(report, analysis_payload(sample, root))
        fresh = read_analysis(report, root)
        assert fresh["_meta"]["stale"] is False and fresh["_meta"]["scanned_at"]
        fid = fresh["contradictions"][0]["_id"]
        assert fid == finding_id("contradictions", sample["contradictions"][0])
        (b / "canon.md").write_text("changed")
        assert read_analysis(report, root)["_meta"]["stale"] is True
        save_decision(fid, "kept", state)
        assert _state(state)["decisions"][fid]["decision"] == "kept"
        clear_decisions(state)
        assert _state(state)["decisions"] == {}
    fenced = 'Here you go:\n```json\n{"contradictions": [{"title": "t", "detail": "d", "files": ["a/b.md"]}]}\n```'
    f = parse_findings(fenced)
    assert f["contradictions"][0]["title"] == "t" and f["stale"] == [] and f["ops"] == [], f
    try:
        parse_findings("no json here"); assert False
    except ValueError:
        pass
    good_headers = {"Content-Type": "application/json; charset=utf-8", "X-Amnesia-Token": SESSION,
                    "Origin": "http://127.0.0.1:8780", "Host": "127.0.0.1:8780"}
    assert post_request_error(good_headers) is None
    assert post_request_error({**good_headers, "Content-Type": "text/plain"})[0] == 415
    assert post_request_error({**good_headers, "X-Amnesia-Token": "wrong"})[0] == 403
    assert post_request_error({**good_headers, "Origin": "https://evil.example"})[0] == 403
    assert local_request_host(good_headers) and not local_request_host({"Host": "evil.example"})
    print("self-check OK")


def main():
    argv = sys.argv[1:]
    if "--check" in argv:
        _check()
    elif "--detach" in argv:
        # ponytail: agent harnesses kill `&` children at session end; a setsid'd child survives
        port = next((a for a in argv if a.isdigit()), "8780")
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), port],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        print(f"amnesia starting on http://localhost:{port}")
    elif "analyze" in argv:
        analyze()
    else:
        port = int(argv[0]) if argv and argv[0].isdigit() else 8780
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        except OSError as e:
            if e.errno == errno.EADDRINUSE:
                sys.exit(f"amnesia is already running — http://localhost:{port}")
            raise
        print(f"amnesia on http://localhost:{port} — trash: {TRASH}")
        srv.serve_forever()


if __name__ == "__main__":
    main()

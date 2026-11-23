import datetime
import glob
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from patcher import apply_ai_response

# configure paths and optional cdn upload target for your Mac mini M4
REPO_DIR = os.path.expanduser("~/Developer/Android/a")
GRADLE_TASK = "assembleFossDebug"
APK_GLOB = "app/build/outputs/apk/foss/debug/*.apk"
APKS_DIR = os.path.expanduser("~/browser-remote-build/apks")
RCLONE_DEST = os.environ.get("RCLONE_DEST", "")
RCLONE_PUBLIC_URL = os.environ.get("RCLONE_PUBLIC_URL", "").rstrip("/")
PORT = 8787

os.makedirs(APKS_DIR, exist_ok=True)

_initial_apks = sorted(glob.glob(os.path.join(APKS_DIR, "*.apk")), key=os.path.getmtime)
_latest_apk_name = os.path.basename(_initial_apks[-1]) if _initial_apks else ""
_latest_apk_url = (
    f"{RCLONE_PUBLIC_URL}/{_latest_apk_name}"
    if (_latest_apk_name and RCLONE_DEST and RCLONE_PUBLIC_URL)
    else (f"/apk/{_latest_apk_name}" if _latest_apk_name else "")
)

state_lock = threading.Lock()
build_state = {
    "busy": False,
    "phase": "idle",
    "status_text": "ready",
    "log": "",
    "debug_text": "",
    "in_failed_state": False,
    "last_apk": _latest_apk_name,
    "apk_url": _latest_apk_url,
    "last_build_time": "",
}


def run_cmd(cmd, cwd=REPO_DIR, timeout=None):
    return subprocess.run(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
    )


def get_current_branch():
    res = run_cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    return res.stdout.strip() or "main"


def get_dirty_files():
    res = run_cmd(["git", "status", "--porcelain"])
    return [line for line in res.stdout.splitlines() if line.strip()]


def get_commits(limit=25, do_fetch=False):
    branch = get_current_branch()
    if do_fetch:
        try:
            run_cmd(["git", "fetch", "origin", "--quiet"], timeout=8)
        except Exception:
            pass

    remote_ref = f"origin/{branch}"
    has_remote = run_cmd(["git", "rev-parse", "--verify", remote_ref]).returncode == 0

    local_only_hashes = set()
    if has_remote:
        unpushed = run_cmd(["git", "rev-list", f"{remote_ref}..HEAD"])
        if unpushed.returncode == 0:
            local_only_hashes = set(h.strip() for h in unpushed.stdout.splitlines() if h.strip())

    head_res = run_cmd(["git", "rev-parse", "HEAD"])
    head_hash = head_res.stdout.strip()

    log_res = run_cmd(["git", "log", f"-n{limit}", "--format=%H\x1f%h\x1f%s\x1f%cr"])
    commits = []
    if log_res.returncode == 0:
        for line in log_res.stdout.splitlines():
            parts = line.split("\x1f")
            if len(parts) != 4:
                continue
            full_h, short_h, subject, rel_time = parts
            is_local = (full_h in local_only_hashes) if has_remote else True
            commits.append({
                "hash": full_h,
                "short": short_h,
                "subject": subject,
                "time": rel_time,
                "is_remote": not is_local,
                "is_head": full_h == head_hash,
            })
    return branch, commits


def extract_gradle_debug(full_output: str) -> str:
    lines = full_output.splitlines()
    important = []
    for i, line in enumerate(lines):
        if line.startswith("e: ") or "FAILURE:" in line or "* What went wrong:" in line or "Unresolved reference" in line:
            important.append(line)
            if "* What went wrong:" in line:
                important.extend(lines[i + 1 : min(len(lines), i + 8)])
    if important:
        snippet = "\n".join(important)
    else:
        snippet = "\n".join(lines[-60:])
    if len(snippet) > 6000:
        snippet = snippet[-6000:]
    return "gradle build failed with these errors, please fix them:\n\n```text\n" + snippet + "\n```"


def run_apply_and_build(raw_text: str, patch_first: bool = True):
    start_ts = time.time()
    try:
        msg = "building current head..."
        commit_msg = ""

        if patch_first:
            with state_lock:
                in_failed = build_state["in_failed_state"]
                build_state["phase"] = "patching"
                build_state["status_text"] = "checking git state & applying patch..."
                build_state["debug_text"] = ""
                build_state["log"] = ""

            # snapshot any manual Android Studio edits before applying a fresh patch
            if not in_failed and get_dirty_files():
                run_cmd(["git", "add", "-A"])
                run_cmd(["git", "commit", "-m", "wip: local android studio edits"])

            ok, msg, commit_msg, touched = apply_ai_response(REPO_DIR, raw_text)
            if not ok:
                with state_lock:
                    build_state["busy"] = False
                    build_state["phase"] = "error"
                    build_state["status_text"] = "patch failed to match"
                    build_state["debug_text"] = (
                        "the patch could not be applied to my current code. please check the mismatch and send updated blocks:\n\n"
                        + msg
                    )
                    build_state["log"] = msg
                return

            with state_lock:
                build_state["phase"] = "building"
                build_state["status_text"] = f"building {GRADLE_TASK}..."
                build_state["log"] = msg + "\nfiles: " + ", ".join(touched) + "\n\nrunning gradle..."
        else:
            with state_lock:
                build_state["phase"] = "building"
                build_state["status_text"] = f"building {GRADLE_TASK}..."
                build_state["debug_text"] = ""
                build_state["log"] = "running gradle on current head..."

        gradle_res = run_cmd(["./gradlew", GRADLE_TASK])
        full_log = msg + "\n\n" + gradle_res.stdout

        if gradle_res.returncode != 0:
            with state_lock:
                build_state["busy"] = False
                build_state["phase"] = "error"
                build_state["in_failed_state"] = patch_first or build_state["in_failed_state"]
                build_state["status_text"] = "gradle build failed"
                build_state["log"] = full_log[-15000:]
                build_state["debug_text"] = extract_gradle_debug(gradle_res.stdout)
            return

        if patch_first and get_dirty_files():
            run_cmd(["git", "add", "-A"])
            run_cmd(["git", "commit", "-m", commit_msg])

        short_hash = run_cmd(["git", "rev-parse", "--short", "HEAD"]).stdout.strip()

        apk_files = sorted(glob.glob(os.path.join(REPO_DIR, APK_GLOB)), key=os.path.getmtime)
        if not apk_files:
            with state_lock:
                build_state["busy"] = False
                build_state["phase"] = "error"
                build_state["in_failed_state"] = False
                build_state["status_text"] = "build succeeded, but no apk found"
                build_state["log"] = full_log[-12000:]
                build_state["debug_text"] = f"expected apk matching {APK_GLOB}"
            return

        apk_path = apk_files[-1]
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")
        apk_name = f"browser-{stamp}-{short_hash}.apk"
        dest_path = os.path.join(APKS_DIR, apk_name)
        shutil.copy2(apk_path, dest_path)
        size_mb = round(os.path.getsize(dest_path) / (1024 * 1024), 1)

        # keep only the five newest apks on disk
        existing_apks = sorted(glob.glob(os.path.join(APKS_DIR, "*.apk")), key=os.path.getmtime)
        for old_apk in existing_apks[:-5]:
            try:
                os.remove(old_apk)
            except Exception:
                pass

        final_url = f"/apk/{apk_name}"
        upload_note = ""
        if RCLONE_DEST and RCLONE_PUBLIC_URL:
            with state_lock:
                build_state["phase"] = "uploading"
                build_state["status_text"] = f"built {size_mb} mb -> uploading {apk_name} to cdn..."
            up_res = run_cmd([
                "rclone", "copyto", dest_path, f"{RCLONE_DEST}/{apk_name}",
                "--s3-upload-concurrency", "4",
                "--transfers", "4",
            ])
            if up_res.returncode == 0:
                final_url = f"{RCLONE_PUBLIC_URL}/{apk_name}"
                upload_note = " (cdn)"
            else:
                full_log += "\n\nrclone upload failed, falling back to direct download:\n" + up_res.stdout
                upload_note = " (direct fallback)"

        elapsed = round(time.time() - start_ts, 1)
        with state_lock:
            build_state["busy"] = False
            build_state["phase"] = "done"
            build_state["in_failed_state"] = False
            build_state["last_apk"] = apk_name
            build_state["apk_url"] = final_url
            build_state["last_build_time"] = f"{elapsed}s"
            build_state["status_text"] = f"ready in {elapsed}s ({size_mb} mb{upload_note}) -> {apk_name}"
            build_state["log"] = full_log[-10000:]
            build_state["debug_text"] = ""

    except Exception as exc:
        with state_lock:
            build_state["busy"] = False
            build_state["phase"] = "error"
            build_state["status_text"] = f"unexpected error: {exc}"
            build_state["debug_text"] = str(exc)


HTML_PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>browser remote builder</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; font-family: system-ui, -apple-system, sans-serif; }
  body { margin: 0; padding: 12px; background: #000; color: #fff; }
  .grid { display: flex; flex-direction: column; gap: 12px; max-width: 1100px; margin: 0 auto; }
  @media (min-width: 720px) {
    .grid { flex-direction: row; align-items: flex-start; }
    .col { flex: 1; min-width: 0; }
  }
  .card { background: #111; border: 1px solid #2a2a2a; border-radius: 16px; padding: 12px; }
  h2 { margin: 0 0 8px 0; font-size: 14px; font-weight: 600; color: #bbb; text-transform: lowercase; }
  textarea {
    width: 100%; min-height: 150px; background: #000; color: #fff;
    border: 1px solid #333; border-radius: 12px; padding: 10px;
    font-family: ui-monospace, monospace; font-size: 13px; resize: vertical;
  }
  .row { display: flex; gap: 8px; margin-top: 8px; flex-wrap: wrap; }
  button {
    border: 0; border-radius: 12px; padding: 10px 14px;
    font-size: 14px; font-weight: 600; cursor: pointer;
    background: #fff; color: #000;
  }
  button.secondary { background: #222; color: #fff; }
  button.danger { background: #8b1a1a; color: #fff; }
  button:disabled { opacity: 0.45; cursor: not-allowed; }
  .status { font-size: 13px; margin-top: 8px; padding: 8px 10px; border-radius: 10px; background: #1a1a1a; }
  .status.error { background: #361212; color: #ffb4b4; }
  .status.done { background: #122b1a; color: #b6f5c8; }
  pre {
    background: #000; border: 1px solid #2a2a2a; border-radius: 10px;
    padding: 8px; font-size: 11px; overflow-x: auto; max-height: 220px;
    white-space: pre-wrap; word-break: break-word;
  }
  .commit {
    border: 1px solid #242424; border-radius: 12px; padding: 8px 10px;
    margin-top: 6px; cursor: pointer; background: #090909;
  }
  .commit-top { display: flex; align-items: center; gap: 6px; font-size: 12px; }
  .badge {
    font-size: 10px; padding: 2px 6px; border-radius: 999px; font-weight: 700;
  }
  .badge.remote { background: #1c3a24; color: #9be8b0; }
  .badge.local { background: #3b2d12; color: #ffd479; }
  .badge.head { background: #25344d; color: #9ec5ff; }
  .subj { font-size: 13px; margin-top: 4px; word-break: break-word; }
  .actions { display: none; gap: 8px; margin-top: 8px; }
  .commit.open .actions { display: flex; }
</style>
</head>
<body>
<div class="grid">
  <div class="col">
    <div class="card">
      <h2>paste AI Studio response</h2>
      <textarea id="patchText" placeholder="paste full AI Studio response here..."></textarea>
      <div class="row">
        <button id="applyBtn" onclick="applyAndBuild()">apply &amp; build</button>
        <button id="buildOnlyBtn" class="secondary" onclick="buildOnly()">build current</button>
        <button class="secondary" onclick="document.getElementById('patchText').value=''">clear</button>
        <button id="discardBtn" class="danger" onclick="discardDirty()" style="display:none">discard local edits</button>
      </div>
      <div id="statusBox" class="status">loading...</div>
      <div id="apkWrap" class="row" style="display:none;">
        <a id="apkLink" href="#" style="text-decoration:none;width:100%;">
          <button type="button" style="width:100%;background:#1c5d2f;color:#fff;">download apk</button>
        </a>
      </div>
      <div id="dirtyBox" class="status" style="display:none"></div>
      <div id="debugWrap" style="display:none; margin-top:8px;">
        <div class="row" style="justify-content:space-between;align-items:center;">
          <h2 style="margin:0">debug script (copy to AI Studio)</h2>
          <button class="secondary" onclick="copyDebug()">copy debug</button>
        </div>
        <pre id="debugPre"></pre>
      </div>
      <details style="margin-top:8px;">
        <summary style="font-size:12px;color:#888;cursor:pointer;">build log</summary>
        <pre id="logPre"></pre>
      </details>
    </div>
  </div>

  <div class="col">
    <div class="card">
      <div class="row" style="margin-top:0;justify-content:space-between;align-items:center;">
        <h2 style="margin:0">commits (<span id="branchName">...</span>)</h2>
        <button class="secondary" onclick="refreshCommits()">fetch git</button>
      </div>
      <div id="commitList"></div>
    </div>
  </div>
</div>

<script>
let selectedHash = null;

async function api(path, body) {
  const res = await fetch(path, {
    method: body ? 'POST' : 'GET',
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined
  });
  return res.json();
}

function renderStatus(data) {
  const s = data.state;
  const busy = s.busy;
  document.getElementById('applyBtn').disabled = busy;
  document.getElementById('buildOnlyBtn').disabled = busy;
  const sb = document.getElementById('statusBox');
  sb.textContent = s.status_text || 'ready';
  sb.className = 'status ' + (s.phase === 'error' ? 'error' : s.phase === 'done' ? 'done' : '');

  const aw = document.getElementById('apkWrap');
  const al = document.getElementById('apkLink');
  if (s.apk_url && !busy) {
    aw.style.display = 'flex';
    al.href = s.apk_url;
    al.querySelector('button').textContent = 'download ' + s.last_apk;
  } else {
    aw.style.display = 'none';
  }

  const dirty = data.dirty_files || [];
  const db = document.getElementById('dirtyBox');
  const discardBtn = document.getElementById('discardBtn');
  if (dirty.length > 0) {
    db.style.display = 'block';
    db.textContent = 'uncommitted files (' + dirty.length + '): ' + dirty.slice(0, 4).join(', ');
    discardBtn.style.display = busy ? 'none' : 'inline-block';
  } else {
    db.style.display = 'none';
    discardBtn.style.display = 'none';
  }

  const dw = document.getElementById('debugWrap');
  if (s.debug_text) {
    dw.style.display = 'block';
    document.getElementById('debugPre').textContent = s.debug_text;
  } else {
    dw.style.display = 'none';
  }

  document.getElementById('logPre').textContent = s.log || '';
  document.getElementById('branchName').textContent = data.branch || '';

  const list = document.getElementById('commitList');
  list.innerHTML = '';
  for (const c of (data.commits || [])) {
    const div = document.createElement('div');
    div.className = 'commit' + (selectedHash === c.hash ? ' open' : '');
    div.onclick = () => {
      selectedHash = (selectedHash === c.hash) ? null : c.hash;
      renderStatus(data);
    };

    const badges =
      '<span class="badge ' + (c.is_remote ? 'remote' : 'local') + '">' + (c.is_remote ? 'remote' : 'local') + '</span>' +
      (c.is_head ? '<span class="badge head">head</span>' : '');

    div.innerHTML =
      '<div class="commit-top"><code>' + c.short + '</code>' + badges +
      '<span style="color:#777;margin-left:auto;">' + c.time + '</span></div>' +
      '<div class="subj"></div>' +
      '<div class="actions">' +
      '<button class="danger" ' + (busy ? 'disabled' : '') + ' onclick="event.stopPropagation(); hardReset(\\'' + c.hash + '\\', \\'' + c.short + '\\')">hard reset</button>' +
      (!c.is_remote ? '<button ' + (busy ? 'disabled' : '') + ' onclick="event.stopPropagation(); pushCommit(\\'' + c.hash + '\\', \\'' + c.short + '\\')">push to github</button>' : '') +
      '</div>';
    div.querySelector('.subj').textContent = c.subject;
    list.appendChild(div);
  }
}

async function poll() {
  try {
    const data = await api('/api/status');
    renderStatus(data);
  } catch (e) {}
}

async function applyAndBuild() {
  const text = document.getElementById('patchText').value;
  if (!text.trim()) return;
  await api('/api/apply', { text });
  poll();
}

async function buildOnly() {
  await api('/api/build', {});
  poll();
}

async function buildOnly() {
  await api('/api/build', {});
  poll();
}

async function copyDebug() {
  const t = document.getElementById('debugPre').textContent;
  await navigator.clipboard.writeText(t);
  alert('copied debug script');
}

async function hardReset(hash, shortHash) {
  if (!confirm('hard reset working tree to ' + shortHash + '? uncommitted edits will be lost.')) return;
  await api('/api/reset', { hash });
  selectedHash = null;
  poll();
}

async function pushCommit(hash, shortHash) {
  if (!confirm('push up to ' + shortHash + ' to github?')) return;
  await api('/api/push', { hash });
  poll();
}

async function discardDirty() {
  if (!confirm('discard all uncommitted changes on disk?')) return;
  await api('/api/discard', {});
  poll();
}

async function refreshCommits() {
  const data = await api('/api/refresh', {});
  renderStatus(data);
}

poll();
setInterval(poll, 2000);
</script>
</body>
</html>
"""


class RequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass

    def log_message(self, format, *args):
        pass

    def _send_json(self, payload, code=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw)

    def _status_payload(self, do_fetch=False):
        branch, commits = get_commits(do_fetch=do_fetch)
        dirty = get_dirty_files()
        with state_lock:
            state_copy = dict(build_state)
        return {
            "state": state_copy,
            "branch": branch,
            "commits": commits,
            "dirty_files": dirty,
        }

    def do_GET(self):
        if self.path == "/":
            body = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/status":
            self._send_json(self._status_payload(do_fetch=False))
        elif self.path.startswith("/apk/"):
            fname = os.path.basename(self.path[5:].split("?")[0])
            fpath = os.path.join(APKS_DIR, fname)
            if not re.fullmatch(r"[a-zA-Z0-9._-]+\.apk", fname) or not os.path.isfile(fpath):
                self._send_json({"error": "apk not found"}, 404)
                return
            size = os.path.getsize(fpath)
            range_header = self.headers.get("Range")
            start = 0
            end = size - 1
            status = 200

            if range_header and range_header.startswith("bytes="):
                m = re.match(r"bytes=(\d+)-(\d*)", range_header.strip())
                if m:
                    start = int(m.group(1))
                    if m.group(2):
                        end = min(int(m.group(2)), size - 1)
                    if start >= size or start > end:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    status = 206

            length = end - start + 1
            self.send_response(status)
            self.send_header("Content-Type", "application/vnd.android.package-archive")
            self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
            self.send_header("Accept-Ranges", "bytes")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(length))
            self.end_headers()

            try:
                with open(fpath, "rb") as f:
                    f.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = f.read(min(1048576, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path == "/api/refresh":
            self._send_json(self._status_payload(do_fetch=True))
            return

        data = self._read_json()

        with state_lock:
            if build_state["busy"]:
                self._send_json({"error": "build already running"}, 409)
                return

        if self.path == "/api/apply":
            raw_text = data.get("text", "")
            with state_lock:
                build_state["busy"] = True
                build_state["phase"] = "patching"
                build_state["status_text"] = "starting..."
            threading.Thread(target=run_apply_and_build, args=(raw_text, True), daemon=True).start()
            self._send_json({"ok": True})

        elif self.path == "/api/build":
            with state_lock:
                build_state["busy"] = True
                build_state["phase"] = "building"
                build_state["status_text"] = "starting build..."
            threading.Thread(target=run_apply_and_build, args=("", False), daemon=True).start()
            self._send_json({"ok": True})

        elif self.path == "/api/reset":
            target_hash = data.get("hash", "")
            if not re.fullmatch(r"[0-9a-fA-F]{7,40}", target_hash):
                self._send_json({"error": "invalid hash"}, 400)
                return
            res1 = run_cmd(["git", "reset", "--hard", target_hash])
            res2 = run_cmd(["git", "clean", "-fd"])
            with state_lock:
                build_state["in_failed_state"] = False
                build_state["debug_text"] = ""
                build_state["phase"] = "idle"
                build_state["status_text"] = f"hard reset to {target_hash[:7]}"
                build_state["log"] = res1.stdout + "\n" + res2.stdout
            self._send_json({"ok": True})

        elif self.path == "/api/discard":
            res1 = run_cmd(["git", "reset", "--hard", "HEAD"])
            res2 = run_cmd(["git", "clean", "-fd"])
            with state_lock:
                build_state["in_failed_state"] = False
                build_state["debug_text"] = ""
                build_state["phase"] = "idle"
                build_state["status_text"] = "discarded local edits"
                build_state["log"] = res1.stdout + "\n" + res2.stdout
            self._send_json({"ok": True})

        elif self.path == "/api/push":
            target_hash = data.get("hash", "")
            if not re.fullmatch(r"[0-9a-fA-F]{7,40}", target_hash):
                self._send_json({"error": "invalid hash"}, 400)
                return
            branch = get_current_branch()
            res = run_cmd(["git", "push", "origin", f"{target_hash}:refs/heads/{branch}"])
            with state_lock:
                build_state["phase"] = "done" if res.returncode == 0 else "error"
                build_state["status_text"] = (
                    f"pushed {target_hash[:7]} to origin/{branch}"
                    if res.returncode == 0
                    else "git push failed"
                )
                build_state["log"] = res.stdout
                if res.returncode != 0:
                    build_state["debug_text"] = res.stdout
            self._send_json({"ok": res.returncode == 0})
        else:
            self._send_json({"error": "not found"}, 404)


def detect_bind_host():
    try:
        res = subprocess.run(["tailscale", "ip", "-4"], stdout=subprocess.PIPE, text=True, timeout=3)
        ip = res.stdout.strip().splitlines()[0].strip()
        if ip.startswith("100."):
            return ip
    except Exception:
        pass
    return "0.0.0.0"


if __name__ == "__main__":
    host = os.environ.get("BIND_HOST") or detect_bind_host()
    server = ThreadingHTTPServer((host, PORT), RequestHandler)
    print(f"remote builder listening on http://{host}:{PORT}")
    server.serve_forever()

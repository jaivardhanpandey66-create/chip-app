#!/usr/bin/env python3
"""
CHIP 3.0 Web — opencode-style agent engine wrapped in a holographic JARVIS UI.

Now behaves like opencode:
  • streaming responses & tool activity (SSE) — text and tool calls appear live
  • PLAN / BUILD modes (UI Tab) — plan mode only allows read-only tools
  • delegate tool — spawns a sub-agent for parallel research (like Task tool)
  • per-request model override + live model picker (/api/models)
  • sessions, undo history, destructive-command guard

Run:
    python3 chip_web.py                 # → http://127.0.0.1:8000
    python3 chip_web.py --port 9000 --model anthropic/claude-sonnet-4-```
"""

import argparse
import ctypes
import difflib
import fnmatch
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, urlencode
import urllib.request

try:
    from openai import OpenAI
except ImportError:
    print("Missing dependency. Run: pip3 install --user openai")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Config (same key/keyfile convention as chip.py / chip_win.py)
# ---------------------------------------------------------------------------

HOME = os.path.expanduser("~")
IS_WINDOWS = os.name == "nt"
CONFIG_DIR = (os.path.join(os.environ.get("APPDATA", HOME), "chip") if IS_WINDOWS
              else os.path.join(HOME, ".config", "chip"))

MODEL = os.environ.get("CHIP_MODEL", "meta-llama/llama-3.3-70b-instruct")
BASE_URL = "https://openrouter.ai/api/v1"

parser = argparse.ArgumentParser(description="CHIP 3.0 Web")
parser.add_argument("--host", default="127.0.0.1")
parser.add_argument("--port", type=int, default=8000)
parser.add_argument("--model", type=str, default=None)
parser.add_argument("--max-steps", type=int, default=15)
args = parser.parse_args()

if args.model:
    MODEL = args.model
MAX_STEPS = args.max_steps
TEMPERATURE = 0.7
MAX_DELEGATE_DEPTH = 2

HERE = os.path.dirname(os.path.abspath(__file__))
HTML_PATH = os.path.join(HERE, "chip_ui.html")


# ---------------------------------------------------------------------------
# Native acceleration core (libchip_native.so via ctypes; python fallback)
# ---------------------------------------------------------------------------

NATIVE_CORE = None  # True once the lib is loaded


def _load_native():
    """Load libchip_native.so and bind its C ABI. Returns an object or None."""
    libpath = os.path.join(HERE, "libchip_native.so")
    if not os.path.exists(libpath):
        return None
    try:
        lib = ctypes.CDLL(libpath)
    except OSError:
        return None

    lib.nn_cache_init.argtypes = [ctypes.c_size_t, ctypes.c_size_t]
    lib.nn_cache_peek.argtypes = [ctypes.c_char_p]
    lib.nn_cache_peek.restype = ctypes.c_size_t
    lib.nn_cache_read.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t]
    lib.nn_cache_read.restype = ctypes.c_int
    lib.nn_cache_put.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_longlong]
    lib.nn_cache_put.restype = ctypes.c_int
    lib.nn_cache_stats.argtypes = [ctypes.POINTER(ctypes.c_longlong)] * 4

    lib.nn_sess_init.argtypes = [ctypes.c_size_t, ctypes.c_size_t]
    lib.nn_sess_peek.argtypes = [ctypes.c_char_p]
    lib.nn_sess_peek.restype = ctypes.c_size_t
    lib.nn_sess_read.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t]
    lib.nn_sess_read.restype = ctypes.c_int
    lib.nn_sess_put.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t]
    lib.nn_sess_put.restype = ctypes.c_int
    lib.nn_sess_stats.argtypes = [ctypes.POINTER(ctypes.c_longlong)] * 2

    lib.nn_sse_frame.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_size_t)]
    lib.nn_sse_frame.restype = ctypes.c_void_p
    lib.nn_free.argtypes = [ctypes.c_void_p]
    lib.nn_truncate_str.argtypes = [ctypes.c_char_p, ctypes.c_size_t]
    lib.nn_truncate_str.restype = ctypes.c_void_p

    lib.nn_cache_init(256, 8 * 1024 * 1024)
    lib.nn_sess_init(64, 6 * 1024 * 1024)
    return lib


NATIVE = _load_native()
NATIVE_CORE = NATIVE is not None
if not NATIVE:
    print("[chip-web] no libchip_native.so found — running in pure-Python mode "
          "(run ./build.sh to enable native acceleration)")


def truncate_preview(raw: str, limit: int = 120) -> str:
    """Truncate a tool-args preview, native when possible."""
    if NATIVE:
        p = NATIVE.nn_truncate_str(raw.encode("utf-8", "replace"), limit)
        try:
            return ctypes.string_at(p).decode("utf-8", "replace")
        finally:
            NATIVE.nn_free(p)
    return raw[:limit]


# ---------------------------------------------------------------------------
# Rust acceleration core (chip_rs.so via ctypes; python fallback)
#   * SHA-256 for TTS ETag / If-None-Match dedup
#   * token estimation for context-budget trimming
# ---------------------------------------------------------------------------

RS = None


def _load_rs():
    libpath = os.path.join(HERE, "chip_rs.so")
    if not os.path.exists(libpath):
        return None
    try:
        lib = ctypes.CDLL(libpath)
    except OSError:
        return None
    lib.rs_token_estimate.argtypes = [ctypes.c_char_p]
    lib.rs_token_estimate.restype = ctypes.c_size_t
    lib.rs_messages_tokens.argtypes = [
        ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_uint64]
    lib.rs_messages_tokens.restype = ctypes.c_int
    lib.rs_sha256_hex.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p]
    lib.rs_sha256_hex.restype = ctypes.c_int
    lib.rs_version.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    lib.rs_version.restype = ctypes.c_int
    return lib


RS = _load_rs()
RS_CORE = RS is not None
if not RS_CORE:
    print("[chip-web] no chip_rs.so found — Rust core disabled "
          "(run ./build_rs.sh then restart)")

CONTEXT_BUDGET = int(os.environ.get("CHIP_CONTEXT_TOKENS", "32000") or 32000)


def _py_token_estimate(s: str) -> int:
    """Python mirror of the Rust token heuristic (~4 ascii chars/token, 1 wide)."""
    if not s:
        return 0
    ascii_n = sum(1 for c in s if ord(c) < 128)
    return max(1, (ascii_n + 3) // 4 + (len(s) - ascii_n))


def _sha256_hex(data: bytes) -> str:
    """Rust SHA-256 when available, hashlib otherwise."""
    if RS_CORE:
        out = ctypes.create_string_buffer(65)
        try:
            if RS.rs_sha256_hex(data, len(data), out) == 0:
                return out.value.decode("ascii")
        except (ctypes.ArgumentError, ValueError):
            pass
    return hashlib.sha256(data).hexdigest()


def _per_message_tokens(msgs: list) -> list:
    """Token estimate per message, in order. Rust scores the whole batch in
    one call when available; otherwise a per-message Python mirror."""
    if RS_CORE and msgs:
        try:
            blob = json.dumps(msgs).encode("utf-8")
            cap = len(msgs) + 1
            out = (ctypes.c_uint64 * cap)()
            n = RS.rs_messages_tokens(blob, len(blob), out, cap)
            if n == len(msgs):
                return list(out[1:1 + n])
        except (ctypes.ArgumentError, ValueError):
            pass
    return [_py_token_estimate(str(m.get("content") or "")) for m in msgs]


def _trim_to_budget(msgs: list, budget: int) -> list:
    """Keep the system message (index 0) plus the newest messages that fit in
    `budget` tokens. Returns the same list when there's room."""
    if budget <= 0 or len(msgs) <= 1:
        return msgs
    per = _per_message_tokens(msgs)
    if len(per) != len(msgs):
        return msgs
    n = len(msgs)
    acc, start = 0, n
    for i in range(n - 1, 0, -1):
        if acc + per[i] > budget:
            break
        acc += per[i]
        start = i
    if start >= n:
        return msgs
    kept = msgs[:1] + msgs[start:]
    sys.stderr.write(f"[chip-web] context trimmed {len(msgs)}→{len(kept)} messages "
                     f"(kept {acc} tok of budget {budget})\n")
    sys.stderr.flush()
    return kept


DESTRUCTIVE_CMDS = re.compile(
    r"\b(rm\s+-rf|rmdir|rd\b|del\s+/[sqf]|mkfs|format\b|cron|shutdown|reboot|"
    r"taskkill\s+/f|killall|pkill|:(){|diskpart)\b|>\s*/dev/", re.I
)


def get_api_key():
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        return key
    kf = os.path.join(CONFIG_DIR, "key")
    if os.path.exists(kf):
        return open(kf).read().strip()
    return ""


def make_client(model=None):
    key = get_api_key()
    if not key:
        raise RuntimeError("No API key. Set OPENROUTER_API_KEY or save it in %s" % CONFIG_DIR)
    return OpenAI(base_url=BASE_URL, api_key=key, timeout=180), model or MODEL


# ---------------------------------------------------------------------------
# Tools (mirror of chip.py + delegate)
# ---------------------------------------------------------------------------

EDIT_HISTORY = {}
_ctxtls = threading.local()


def _ctxt():
    if not hasattr(_ctxtls, "delegate_depth"):
        _ctxtls.delegate_depth = 0
    return _ctxtls


def _human_size(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{u}" if u == "B" else f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}TB"


def _record_edit(path, old, new):
    EDIT_HISTORY.setdefault(path, []).append((datetime.now().isoformat(), old, new))


def tool_run_command(args):
    cmd = args["command"]
    if DESTRUCTIVE_CMDS.search(cmd):
        return "Blocked in web mode: command looks destructive. Run it yourself in a terminal."
    timeout = min(args.get("timeout", 300), 600)
    try:
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout,
                              cwd=args.get("cwd", os.getcwd()), encoding="utf-8", errors="replace")
        out = proc.stdout.strip() or ""
        err = proc.stderr.strip() or ""
        result = f"exit_code={proc.returncode}\n{out}{('' if not err else '\n[stderr]\n' + err)}".strip()
        return result or "(no output)"
    except subprocess.TimeoutExpired:
        return f"Command timed out after {timeout}s."
    except Exception as e:
        return f"Error running command: {e}"


def tool_read_file(args):
    path = os.path.expanduser(args["path"])
    if not os.path.exists(path):
        return f"File not found: {path}"
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            content = f.read()
        return f"[contents of {path} ({content.count(chr(10)) + 1} lines)]\n{content[:20000]}"
    except Exception as e:
        return f"Error reading: {e}"


def tool_write_file(args):
    path = os.path.expanduser(args["path"])
    content = args["content"]
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if os.path.exists(path):
            _record_edit(path, open(path, encoding="utf-8", errors="replace").read(), content)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error writing {path}: {e}"


def tool_edit_file(args):
    path = os.path.expanduser(args["path"])
    old_text, new_text = args["old_text"], args["new_text"]
    if not os.path.exists(path):
        return f"File not found: {path}"
    try:
        content = open(path, encoding="utf-8", errors="replace").read()
        if old_text not in content:
            return f"old_text not found in {path}. No changes made."
        count = content.count(old_text)
        if count > 1 and not args.get("replaceAll", False):
            return f"old_text found {count} times. Set replaceAll=true or give more context."
        _record_edit(path, content, content.replace(old_text, new_text, 1))
        new_content = content.replace(old_text, new_text, 1 if not args.get("replaceAll") else -1)
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_content)
        diff = list(difflib.unified_diff(content.splitlines(), new_content.splitlines(),
                                        fromfile="a", tofile="b", lineterm=""))
        return f"Edited {path}\n" + "\n".join(diff[:40])
    except Exception as e:
        return f"Error editing {path}: {e}"


def tool_append_file(args):
    path = os.path.expanduser(args["path"])
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(args["content"])
        return f"Appended {len(args['content'])} bytes to {path}"
    except Exception as e:
        return f"Error appending: {e}"


def tool_list_dir(args):
    path = os.path.expanduser(args.get("path", "."))
    if not os.path.isdir(path):
        return f"Not a directory: {path}"
    entries = []
    for name in sorted(os.listdir(path)):
        if not args.get("hidden", False) and name.startswith("."):
            continue
        full = os.path.join(path, name)
        if os.path.isdir(full):
            entries.append(f"[dir]  {name}/")
        else:
            sz = os.path.getsize(full)
            entries.append(f"       {name} ({_human_size(sz)})")
    return f"Contents of {path} ({len(entries)} entries):\n" + "\n".join(entries)


def tool_search_files(args):
    pattern, root = args["pattern"], os.path.expanduser(args.get("path", "."))
    max_results = args.get("max_results", 50)
    if not os.path.isdir(root):
        return f"Not a directory: {root}"
    matches = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fname in filenames:
            if fnmatch.fnmatch(fname, pattern):
                matches.append(os.path.join(dirpath, fname))
                if len(matches) >= max_results:
                    break
        if len(matches) >= max_results:
            break
    if not matches:
        return f"No files matching '{pattern}' in {root}"
    return f"Found {len(matches)} files:\n" + "\n".join(matches)


def tool_grep(args):
    pattern, path = args["pattern"], args.get("path", ".")
    include = args.get("include", "*")
    max_results = args.get("max_results", 50)
    files = []
    if os.path.isfile(path):
        files = [path]
    elif os.path.isdir(path):
        for dirpath, _, filenames in os.walk(path):
            for fn in filenames:
                if fnmatch.fnmatch(fn, include) and not fn.startswith("."):
                    files.append(os.path.join(dirpath, fn))
    else:
        return f"Not found: {path}"
    results = []
    rgx = re.compile(pattern, re.I if args.get("ignore_case", True) else 0)
    for fp in files:
        if len(results) >= max_results:
            break
        try:
            lines = open(fp, encoding="utf-8", errors="replace").readlines()
        except Exception:
            continue
        for i, line in enumerate(lines):
            if rgx.search(line):
                results.append(f"{fp}:{i+1}: {line.rstrip()[:200]}")
                if len(results) >= max_results:
                    break
    if not results:
        return f"No matches for '{pattern}'"
    return f"Found {len(results)} matches:\n" + "\n".join(results)


def tool_web_fetch(args):
    url = args["url"]
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "CHIP/3.0 (web)"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            content = resp.read().decode("utf-8", errors="replace")
        max_chars = min(args.get("max_chars", 8000), 20000)
        if len(content) > max_chars:
            content = content[:max_chars] + f"\n\n... (truncated at {max_chars} chars)"
        return f"[{url}]\n{content}"
    except Exception as e:
        return f"Error fetching {url}: {e}"


def tool_move_file(args):
    src, dst = os.path.expanduser(args["source"]), os.path.expanduser(args["destination"])
    if not os.path.exists(src):
        return f"Source not found: {src}"
    try:
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        shutil.move(src, dst)
        return f"Moved {src} -> {dst}"
    except Exception as e:
        return f"Error moving: {e}"


def tool_copy_file(args):
    src, dst = os.path.expanduser(args["source"]), os.path.expanduser(args["destination"])
    if not os.path.exists(src):
        return f"Source not found: {src}"
    try:
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        if os.path.isdir(src):
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
        return f"Copied {src} -> {dst}"
    except Exception as e:
        return f"Error copying: {e}"


def tool_delete_file(args):
    path = os.path.expanduser(args["path"])
    if not os.path.exists(path):
        return f"Not found: {path}"
    if not args.get("force", False):
        return f"Declined: deletion of {path} requires force=true in web mode."
    try:
        shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
        return f"Deleted {path}"
    except Exception as e:
        return f"Error deleting: {e}"


def tool_undo_edit(args):
    path = os.path.expanduser(args["path"])
    if path not in EDIT_HISTORY or not EDIT_HISTORY[path]:
        return f"No edit history for {path}"
    ts, old, _ = EDIT_HISTORY[path].pop()
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(old)
        return f"Undid edit on {path} (from {ts})"
    except Exception as e:
        return f"Failed to undo: {e}"


def tool_system_info(args):
    info = {
        "hostname": platform.node(),
        "os": f"{platform.system()} {platform.release()}",
        "python": platform.python_version(),
        "arch": platform.machine(),
        "cpu": platform.processor() or "unknown",
    }
    try:
        import psutil
        vm = psutil.virtual_memory()
        info["memory"] = f"{vm.used / 2**30:.1f} / {vm.total / 2**30:.1f} GB ({vm.percent:.0f}%)"
        du = psutil.disk_usage("/")
        info["disk"] = f"{du.used / 2**30:.1f} / {du.total / 2**30:.1f} GB ({du.percent:.0f}%)"
        info["cpu_usage"] = f"{psutil.cpu_percent(interval=0.3):.0f}%"
    except ImportError:
        pass
    return "System Information:\n" + "\n".join(f"  {k}: {v}" for k, v in info.items())


def tool_git(args):
    subcommand = args.get("subcommand", "status")
    valid = {"status", "log", "diff", "add", "commit", "branch", "checkout",
             "stash", "pull", "push", "remote", "blame", "show", "reset", "merge", "rebase"}
    if subcommand not in valid:
        return f"Unknown git subcommand: {subcommand}"
    parts = ["git", subcommand]
    msg = args.get("message", "")
    try:
        if subcommand == "commit":
            if not msg:
                return "git commit requires a 'message'."
            parts = ["git", "commit", "-m", msg]
        elif subcommand == "add":
            paths = args.get("paths", ["."])
            parts += paths if isinstance(paths, list) else [paths]
        elif subcommand == "checkout":
            parts.append(args.get("branch", ""))
        else:
            for k, v in args.items():
                if k not in ("subcommand", "message", "paths", "branch") and v:
                    parts.append(f"--{k}" if len(k) > 1 else f"-{k}")
                    if v is not True:
                        parts.append(str(v))
        proc = subprocess.run(parts, capture_output=True, text=True, timeout=30,
                              encoding="utf-8", errors="replace")
        return (f"{' '.join(parts)}\nexit_code={proc.returncode}\n{proc.stdout.strip()}"
                + ("" if not proc.stderr.strip() else "\n" + proc.stderr.strip())).strip()
    except Exception as e:
        return f"Git error: {e}"


def tool_delegate(args):
    """OpenCode-style: spawn a sub-agent to research a task, return findings."""
    task = (args.get("task") or "").strip()
    if not task:
        return "delegate requires a 'task' string."
    depth = _ctxt().delegate_depth
    if depth >= MAX_DELEGATE_DEPTH:
        return f"Delegate depth limit reached ({MAX_DELEGATE_DEPTH}). Do the work yourself."
    try:
        client, dl_model = make_client()
    except Exception as e:
        return f"delegate failed to init client: {e}"
    _ctxt().delegate_depth = depth + 1
    try:
        sub_role = ("You are a CHIP sub-agent doing focused research with READ-ONLY tools "
                    "(read_file, list_dir, search_files, grep, web_fetch, git status/diff/log, system_info). "
                    "Investigate thoroughly, do NOT modify anything, and report concrete findings with "
                    "file/line references. Be concise: 2-6 bullet points.")
        msgs = [{"role": "system", "content": sub_role}, {"role": "user", "content": task}]
        collected = []
        for ev in agent_generate(client, msgs, mode="build", depth=depth + 1,
                                 max_steps=8, tools=TOOLS_SUB, model=dl_model):
            if ev["type"] in ("delta",):
                pass
            if ev["type"] == "done":
                collected.append(ev["answer"])
            if ev["type"] == "error":
                collected.append(f"sub-agent error: {ev['error']}")
        return "\n\n".join(x for x in collected if x) or "(sub-agent produced no findings)"
    finally:
        _ctxt().delegate_depth = depth


EDIT_CMDS = {"write_file", "edit_file", "append_file", "move_file", "copy_file",
             "delete_file", "run_command", "undo_edit"}
GIT_READONLY = {"status", "log", "diff", "show", "blame", "remote", "branch", "stash"}


def _tool_blocked(name, targs, mode):
    if mode == "plan":
        if name in EDIT_CMDS:
            return f"Blocked in PLAN mode. Use BUILD mode (Tab) to apply changes."
        if name == "git" and (targs.get("subcommand") or "status") not in GIT_READONLY:
            return "Blocked in PLAN mode: that git subcommand mutates. Read-only git only (status/log/diff/show)."
    if name == "run_command" and DESTRUCTIVE_CMDS.search(targs.get("command", "")):
        return "Blocked: command looks destructive."
    return None


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

def _fn(name, description, props, required=None):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": props,
                       "required": required or []}}}


TOOLS = [
    _fn("run_command", "Run a shell (cmd) command. Use for scripts, installs, inspection.",
        {"command": {"type": "string"}, "timeout": {"type": "integer"},
         "cwd": {"type": "string"}}, ["command"]),
    _fn("read_file", "Read the full contents of a text file.",
        {"path": {"type": "string"}}, ["path"]),
    _fn("write_file", "Create or overwrite a file.",
        {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
    _fn("edit_file", "Surgical text replacement in a file. Shows a diff.",
        {"path": {"type": "string"}, "old_text": {"type": "string"},
         "new_text": {"type": "string"}, "replaceAll": {"type": "boolean"}},
        ["path", "old_text", "new_text"]),
    _fn("append_file", "Append content to the end of a file.",
        {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
    _fn("list_dir", "List files and directories.",
        {"path": {"type": "string"}, "hidden": {"type": "boolean"}}),
    _fn("search_files", "Find files by name pattern (glob) recursively.",
        {"pattern": {"type": "string"}, "path": {"type": "string"},
         "max_results": {"type": "integer"}}, ["pattern"]),
    _fn("grep", "Search file contents with regex.",
        {"pattern": {"type": "string"}, "path": {"type": "string"},
         "include": {"type": "string"}, "max_results": {"type": "integer"}}, ["pattern"]),
    _fn("web_fetch", "Fetch content from a URL.",
        {"url": {"type": "string"}, "max_chars": {"type": "integer"}}, ["url"]),
    _fn("move_file", "Move or rename a file/directory.",
        {"source": {"type": "string"}, "destination": {"type": "string"}},
        ["source", "destination"]),
    _fn("copy_file", "Copy a file or directory tree.",
        {"source": {"type": "string"}, "destination": {"type": "string"}},
        ["source", "destination"]),
    _fn("delete_file", "Delete a file/directory. Requires force=true.",
        {"path": {"type": "string"}, "force": {"type": "boolean"}}, ["path"]),
    _fn("system_info", "Get system info: hostname, OS, CPU, memory, disk.",
        {}),
    _fn("git", "Run a git command (status, log, diff, add, commit, checkout, etc).",
        {"subcommand": {"type": "string"}, "message": {"type": "string"},
         "paths": {"type": "array", "items": {"type": "string"}},
         "branch": {"type": "string"}}, ["subcommand"]),
    _fn("undo_edit", "Undo the last edit made to a file.",
        {"path": {"type": "string"}}, ["path"]),
    _fn("delegate", "Spawn a read-only sub-agent to research a task independently and report findings.",
        {"task": {"type": "string", "description": "A focused, self-contained research task."}}, ["task"]),
]

TOOLS_NODELEGATE = [t for t in TOOLS if t["function"]["name"] != "delegate"]
TOOLS_READONLY = [t for t in TOOLS_NODELEGATE
                  if t["function"]["name"] in
                  {"read_file", "list_dir", "search_files", "grep", "web_fetch",
                   "system_info", "git"}]
TOOLS_SUB = list(TOOLS_READONLY)

FUNCS = {
    "run_command": tool_run_command, "read_file": tool_read_file,
    "write_file": tool_write_file, "edit_file": tool_edit_file,
    "append_file": tool_append_file, "list_dir": tool_list_dir,
    "search_files": tool_search_files, "grep": tool_grep,
    "web_fetch": tool_web_fetch, "move_file": tool_move_file,
    "copy_file": tool_copy_file, "delete_file": tool_delete_file,
    "system_info": tool_system_info, "git": tool_git,
    "undo_edit": tool_undo_edit, "delegate": tool_delegate,
}


# ---------------------------------------------------------------------------
# Agent loop (generator → SSE events)
# ---------------------------------------------------------------------------

SESSIONS: dict[str, dict] = {}   # pure-Python fallback store
SESSION_LOCK = threading.Lock()


def _sess_key(name: str) -> bytes:
    return ("sid\x00" + name).encode("utf-8")


def _session_load(session_key: str, mode: str) -> dict:
    """Load a session envelope {mode, messages}. Fresh if absent or mode changed."""
    env = None
    if NATIVE:
        kb = _sess_key(session_key)
        sz = NATIVE.nn_sess_peek(kb)
        if sz:
            buf = ctypes.create_string_buffer(sz)
            if NATIVE.nn_sess_read(kb, buf, sz) == 1:
                try:
                    env = json.loads(buf.raw.decode("utf-8", "replace"))
                except Exception:
                    env = None
    else:
        with SESSION_LOCK:
            env = SESSIONS.get(session_key)
    if not isinstance(env, dict):
        env = {}
    msgs = env.get("messages")
    if not isinstance(msgs, list):
        msgs = []
    if env.get("mode") != mode or not msgs:
        msgs = [{"role": "system", "content": _sys_prompt(mode)}]
        env["mode"] = mode
    env["messages"] = msgs
    env.setdefault("created", datetime.now().isoformat())
    return env


def _session_save(session_key: str, env: dict):
    blob = json.dumps(env).encode("utf-8")
    if NATIVE:
        NATIVE.nn_sess_put(_sess_key(session_key), blob, len(blob))
    else:
        with SESSION_LOCK:
            if len(SESSIONS) > 256:
                SESSIONS.pop(next(iter(SESSIONS)), None)
            SESSIONS[session_key] = env


def _sys_prompt(mode="build"):
    mode_hint = ""
    if mode == "plan":
        mode_hint = ("You are in PLAN MODE. Do NOT modify anything. Investigate with read-only "
                     "tools and produce a clear, step-by-step implementation plan with file paths "
                     "and reasoning. The last message must be the final plan.")
    return ("You are CHIP 3.0, a highly capable, elegantly terse AI agent modeled on "
            "opencode, operating inside the user's %s machine as a holographic assistant. "
            "You can run commands, read/write/edit files, search, fetch the web, manage git, "
            "inspect the system, and delegate research tasks to a sub-agent. Prefer tools over "
            "guessing. For big or ambiguous tasks, use delegate for parallel research. Verify "
            "command results and give a concise summary. Home directory: %s. %s"
            % ("Windows" if IS_WINDOWS else "Linux", HOME, mode_hint))


def agent_generate(client, messages, mode="build", depth=0, max_steps=None,
                   tools=None, temperature=None, model=None):
    """Yield events: delta / tool / done / error. Appends to messages in place."""
    max_steps = max_steps or MAX_STEPS
    tools = tools or TOOLS
    temperature = temperature if temperature is not None else TEMPERATURE
    model = model or MODEL
    used = 0
    while used < max_steps:
        content = ""
        tool_buffer = {}
        try:
            stream = client.chat.completions.create(
                model=model,
                messages=messages, tools=tools, tool_choice="auto",
                temperature=temperature, stream=True)
        except Exception as e:
            yield {"type": "error", "error": str(e)}
            return

        for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta and delta.content:
                content += delta.content
                yield {"type": "delta", "text": delta.content}
            if delta and delta.tool_calls:
                for tc in delta.tool_calls:
                    e = tool_buffer.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                    if tc.id:
                        e["id"] += tc.id
                    if tc.function:
                        if tc.function.name:
                            e["name"] += tc.function.name
                        if tc.function.arguments:
                            e["args"] += tc.function.arguments

        if not tool_buffer:
            messages.append({"role": "assistant", "content": content or ""})
            yield {"type": "done", "answer": content or "(no response)",
                   "steps": used, "delegate_depth": depth}
            return

        tool_calls = [{"id": e["id"] or str(i), "type": "function",
                       "function": {"name": e["name"], "arguments": e["args"]}}
                      for i, e in sorted(tool_buffer.items())]

        messages.append({"role": "assistant", "content": content, "tool_calls": tool_calls})

        for tc in tool_calls:
            name = tc["function"]["name"]
            try:
                targs = json.loads(tc["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                targs = {}
            blocked = _tool_blocked(name, targs, mode) if depth == 0 else None
            if blocked:
                result = blocked
            elif name in FUNCS:
                try:
                    result = FUNCS[name](targs)
                except Exception as ex:
                    result = f"Tool raised {ex!r}"
            else:
                result = f"Unknown tool: {name}"
            used += 1
            yield {"type": "tool", "name": name, "args": targs, "result": str(result)[:900],
                   "index": used, "blocked": blocked is not None}
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": str(result)})

    yield {"type": "done", "answer": content or "(max steps reached)",
           "steps": used, "delegate_depth": depth}


# ---------------------------------------------------------------------------
# Model catalog (OpenRouter, cached)
# ---------------------------------------------------------------------------

_MODEL_CACHE = {"ts": 0.0, "ids": []}
_MODEL_LOCK = threading.Lock()
PREFER = ("claude", "gpt", "qwen", "deepseek", "llama", "gemini", "mistral", "coder")


def fetch_models(client):
    now = time.time()
    if now - _MODEL_CACHE["ts"] < 600 and _MODEL_CACHE["ids"]:
        return _MODEL_CACHE["ids"]
    with _MODEL_LOCK:
        if now - _MODEL_CACHE["ts"] < 600 and _MODEL_CACHE["ids"]:
            return _MODEL_CACHE["ids"]
        ids = []
        try:
            resp = client.models.list()
            raw = [m.id for m in resp.data if "." in str(getattr(m, "id", ""))]
            lower = [x for x in raw if any(p in x.lower() for p in PREFER)
                     and ":free" not in x.lower()]
            rest = [x for x in raw if x not in lower][:40]
            ids = lower + rest
        except Exception:
            ids = [MODEL]
        _MODEL_CACHE["ids"] = ids[:150]
        _MODEL_CACHE["ts"] = now
        return _MODEL_CACHE["ids"]


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

# server-side TTS fallback (Google Translate TTS proxy) — works when the
# browser has no local voices (common on Linux), e.g. Brave/Chromium.
# Caching is backed by the native LRU (libchip_native.so) when available.
_TTS_CACHE = {}          # pure-Python fallback cache
_TTS_LOCK = threading.Lock()
_TTS_MAX_CACHE = 200


def _tts_key(text, lang):
    return (lang + "\x00" + text).encode("utf-8")


def _google_tts(text, lang="en"):
    """Return (data|None, cache_hit: bool)."""
    if not text.strip():
        return None, False

    if NATIVE:
        key = _tts_key(text, lang)
        sz = NATIVE.nn_cache_peek(key)
        if sz:
            buf = ctypes.create_string_buffer(sz)
            if NATIVE.nn_cache_read(key, buf, sz) == 1:
                return buf.raw, True
        data = _fetch_google_tts(text, lang)
        if data:
            NATIVE.nn_cache_put(key, data, len(data), 3600000)
        return data, False

    key = (lang, text)
    with _TTS_LOCK:
        cached = _TTS_CACHE.get(key)
        if cached:
            return cached, True
    data = _fetch_google_tts(text, lang)
    if data:
        with _TTS_LOCK:
            _TTS_CACHE[key] = data
            if len(_TTS_CACHE) > _TTS_MAX_CACHE:
                for old in list(_TTS_CACHE)[:50]:
                    _TTS_CACHE.pop(old, None)
    return data, False


def _fetch_google_tts(text, lang):
    url = "https://translate.google.com/translate_tts?" + urlencode({
        "ie": "UTF-8", "q": text, "tl": lang, "client": "tw-ob"})
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
            "Referer": "https://translate.google.com/"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read() or None
    except Exception:
        return None


def _html():
    if os.path.exists(HTML_PATH):
        with open(HTML_PATH, encoding="utf-8") as f:
            return f.read()
    return "<h1>chip_ui.html not found next to chip_web.py</h1>"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        sys.stderr.write("[chip-web] %s %s\n" % (self.address_string(), fmt % a))

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _chunk(self, data: bytes):
        self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
        self.wfile.flush()

    def _sse_frame(self, kind: str, obj):
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        if NATIVE:
            blen = ctypes.c_size_t()
            p = NATIVE.nn_sse_frame(kind.encode("utf-8"), payload, ctypes.byref(blen))
            try:
                self._chunk(ctypes.string_at(p, blen.value))
            finally:
                NATIVE.nn_free(p)
        else:
            self._chunk(b"event: " + kind.encode("utf-8") + b"\ndata: " + payload + b"\n\n")

    def _send_health(self):
        rsver = None
        if RS_CORE:
            vb = ctypes.create_string_buffer(128)
            if RS.rs_version(vb, 128) >= 0:
                rsver = vb.value.decode("utf-8", "replace")
        if NATIVE:
            items = ctypes.c_longlong(); by = ctypes.c_longlong()
            hits = ctypes.c_longlong(); miss = ctypes.c_longlong()
            s_items = ctypes.c_longlong(); s_bytes = ctypes.c_longlong()
            NATIVE.nn_cache_stats(ctypes.byref(items), ctypes.byref(by),
                                  ctypes.byref(hits), ctypes.byref(miss))
            NATIVE.nn_sess_stats(ctypes.byref(s_items), ctypes.byref(s_bytes))
            self._json({
                "native": True, "rust": RS_CORE, "rust_version": rsver,
                "context_budget": CONTEXT_BUDGET, "model": MODEL,
                "cache": {"items": items.value, "bytes": by.value,
                          "hits": hits.value, "misses": miss.value,
                          "hit_rate": round(100 * hits.value / (hits.value + miss.value), 2)
                          if (hits.value + miss.value) else 0.0},
                "sessions": {"items": s_items.value, "bytes": s_bytes.value},
            })
        else:
            with SESSION_LOCK:
                self._json({
                    "native": False, "rust": RS_CORE, "rust_version": rsver,
                    "context_budget": CONTEXT_BUDGET, "model": MODEL,
                    "cache": {"items": len(_TTS_CACHE), "bytes": sum(len(v) for v in _TTS_CACHE.values()),
                              "hits": 0, "misses": 0, "hit_rate": 0.0},
                    "sessions": {"items": len(SESSIONS), "bytes": 0},
                })

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/chip_ui.html", "/index.html"):
            body = _html().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/models":
            try:
                client, _ = make_client()
                ids = fetch_models(client)
                self._json({"models": ids, "default": MODEL})
            except Exception as e:
                self._json({"error": str(e), "models": [], "default": MODEL}, 500)
            return
        if path == "/api/tts":
            qs = parse_qs(parsed.query)
            text = (qs.get("text") or [""])[0][:220]
            lang = (qs.get("lang") or ["en"])[0][:10]
            if not text:
                self._json({"error": "missing text"}, 400)
                return
            if len(text) > 220:
                text = text[:220]
            etag = '"' + _sha256_hex(_tts_key(text, lang)) + '"'
            if self.headers.get("If-None-Match") in (etag, "*"):
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", "public, max-age=3600")
                self.end_headers()
                return
            data, cache_hit = _google_tts(text, lang)
            if data is None:
                self._json({"error": "TTS service unavailable"}, 502)
                return
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=3600")
            self.send_header("ETag", etag)
            self.send_header("X-Chip-Cache", "hit" if cache_hit else ("miss" if NATIVE else "py"))
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/api/stats":
            self._send_health()
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path.startswith("/api/chat"):
            self._handle_chat(streaming="/stream" in self.path)
            return
        self._json({"error": "not found"}, 404)

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            return json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return {}

    def _handle_chat(self, streaming=False):
        data = self._read_body()
        message = (data.get("message") or "").strip()
        if not message:
            self._json({"error": "empty message"}, 400)
            return
        session_key = (data.get("session") or "default")[:100]
        mode = data.get("mode") if data.get("mode") in ("plan", "build") else "build"
        model = (data.get("model") or "").strip() or MODEL[:]
        try:
            client, model = make_client(model)
        except Exception as e:
            self._json({"error": str(e)}, 500)
            return

        env = _session_load(session_key, mode)
        msgs = env["messages"]
        msgs.append({"role": "user", "content": message})
        msgs = _trim_to_budget(msgs, CONTEXT_BUDGET)
        env["messages"] = msgs

        events = agent_generate(client, msgs, mode=mode, temperature=TEMPERATURE)

        if not streaming:
            steps = []
            answer = ""
            err = None
            for ev in events:
                if ev["type"] == "tool":
                    steps.append({"name": ev["name"], "args": ev["args"],
                                  "result": ev["result"]})
                elif ev["type"] == "done":
                    answer = ev["answer"]
                elif ev["type"] == "error":
                    err = ev["error"]
            _session_save(session_key, env)
            self._json({"answer": answer or "(no response)",
                        "steps": steps,
                        "session": session_key,
                        "error": err} if err else
                       {"answer": answer or "(no response)", "steps": steps,
                        "session": session_key})
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Chip-Native", "1" if NATIVE else "0")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def evt(kind, obj):
            self._sse_frame(kind, obj)

        try:
            evt("meta", {"session": session_key, "model": model, "mode": mode})
            for ev in events:
                if ev["type"] == "delta":
                    evt("delta", {"text": ev["text"]})
                elif ev["type"] == "tool":
                    args_preview = truncate_preview(json.dumps(ev["args"], ensure_ascii=False), 400)
                    evt("tool", {"name": ev["name"], "args": args_preview,
                                 "result": ev["result"], "index": ev["index"],
                                 "blocked": ev["blocked"]})
                elif ev["type"] == "done":
                    evt("done", {"answer": ev["answer"], "steps": ev["steps"]})
                elif ev["type"] == "error":
                    evt("error", {"error": ev["error"]})
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            _session_save(session_key, env)
            try:
                self._chunk(b"")
            except Exception:
                pass


def main():
    print(f"\n  CHIP 3.0 Web (model: {MODEL})  — streaming agent, plan/build, delegate sub-agent")
    print(f"  → http://{args.host}:{args.port}")
    print("  Ctrl+C to stop\n")
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
#!/usr/bin/env python3
"""
codex-bridge.py — Claude Code ↔ Codex Desktop Pet Bridge

Makes the Codex desktop pet (avatar overlay) show live progress when Claude Code
runs tools: the pet enters an "active" animation and displays the current tool
activity ("Calling Bash", "Running command", "Editing files"...), shows a check
mark on completion, then returns to idle — mirroring Codex's native experience.

HOW IT WORKS (reverse-engineered + end-to-end verified)
--------------------------------------------------------
The pet listens on an internal Electron window message-bus, a Unix domain socket
at $TMPDIR/codex-ipc/ipc-<uid>.sock. The wire framing is:

    4-byte little-endian length prefix + UTF-8 JSON body     (max frame 256 MiB)

NOT WebSocket and NOT raw newline JSON — those are why earlier attempts failed
(a raw '{"id' prefix parses as ~1.6 GB > 256 MiB and the server closes; a TLS
ClientHello parses as a valid length and the server hangs waiting for bytes).

Handshake: send an `initialize` request, receive an assigned clientId.

Driving the pet: broadcast `thread-stream-state-changed` snapshots for a
conversationId that the pet already has in its recent-conversations list. A fresh
UUID does NOT work — the pet only renders conversations returned by the app
server's thread/list (source must be interactive/vscode and preview non-empty).
So we use a dedicated, real "Claude Code" session (created once via the
app-server stdio channel) and force the pet to (re)load it with a
`thread-unarchived` broadcast, which triggers refreshRecentConversations.

The conversationState we send MUST be field-complete — we start from a captured
real frame template (codex-bridge-template.json) and mutate only a few fields.
A hand-built partial object makes the overlay throw and show an error badge.

Nothing is persisted by our broadcasts (they update the pet's in-memory store
only). The single persistent artifact is the dedicated session itself.

Usage (invoked by Claude Code hooks via stdin JSON):
  python3 ~/.claude/codex-bridge.py pre_tool_use
  python3 ~/.claude/codex-bridge.py post_tool_use
  python3 ~/.claude/codex-bridge.py stop

Diagnostics:
  python3 ~/.claude/codex-bridge.py --selftest   # connect + handshake, print clientId
  python3 ~/.claude/codex-bridge.py --demo       # drive dedicated session one cycle
"""

import copy
import glob
import json
import os
import shlex
import socket
import sqlite3
import struct
import subprocess
import sys
import time
import urllib.parse
import uuid
from typing import Any, Dict, List, Optional, Tuple

# ── Paths & constants ──────────────────────────────────────────────────────────

# Read-only plugin assets live next to this script (plugin's scripts/ dir).
SCRIPT_DIR    = os.path.dirname(os.path.abspath(__file__))
# Writable runtime state lives in ~/.ccpet (config, compiled binary, durable
# state) — never inside the plugin dir, which is overwritten on plugin update.
RUNTIME_DIR   = os.path.expanduser("~/.ccpet")
TEMPLATE_PATH = os.path.join(SCRIPT_DIR, "codex-bridge-template.json")
CONFIG_PATH   = os.path.join(RUNTIME_DIR, "config.json")
CODEX_BIN     = "/Applications/Codex.app/Contents/Resources/codex"

MAX_FRAME       = 268435456          # 256 MiB — server rejects larger
STREAM_METHOD   = "thread-stream-state-changed"
STREAM_VERSION  = 8                  # per the bus version map for this method
UNARCH_METHOD   = "thread-unarchived"
UNARCH_VERSION  = 1

# Max characters of assistant text to show in the pet bubble (avoid overflow).
MAX_BUBBLE_CHARS = 500
# How much of the transcript tail to read (bytes / lines) when extracting text.
TRANSCRIPT_TAIL_BYTES = 512 * 1024
TRANSCRIPT_TAIL_LINES = 400

# A conversationId known to already exist (source=vscode, preview="Claude Code").
# Created once during design; used as the default dedicated session so the very
# first run needs no API call. ensure_dedicated_session() will re-create if gone.
DEFAULT_DEDICATED_ID = "019f421f-ede6-7210-b10f-568241cc1a1c"

# ── Independent pet (native target) ─────────────────────────────────────────────
# Source is shipped read-only in the plugin; the compiled binary + durable state
# are written to the writable RUNTIME_DIR.
CCPET_SRC       = os.path.join(SCRIPT_DIR, "ccpet.swift")
CCPET_BIN       = os.path.join(RUNTIME_DIR, "ccpet")
CCPET_STATE_DIR = os.path.join(RUNTIME_DIR, "state")

# Cursor: bundle id (for `open -b <id> <folder>` window focus) + workspace
# storage (maps a native-channel session id → workspace folder). Used to
# distinguish the new "Toggle Agent → Claude Code" native channel from the old
# anthropic.claude-code extension, and to focus the correct project window when
# multiple Cursor windows are open. See _cursor_native_session_map / _open_cmd_for.
CURSOR_BUNDLE_ID  = "com.todesktop.230313mzl4w4u92"
CURSOR_WS_STORAGE = os.path.expanduser(
    "~/Library/Application Support/Cursor/User/workspaceStorage")

def _ccpet_sock() -> str:
    t = os.environ.get("TMPDIR", "/tmp").rstrip("/")
    return f"{t}/ccpet/daemon.sock"

def _targets() -> List[str]:
    """Which pet backends to drive. Default: independent native pet only."""
    cfg = _load_config()
    t = cfg.get("targets")
    if isinstance(t, list) and t:
        return t
    return ["native"]


# ── Per-session state (short-lived hook processes coordinate via files) ─────────

def _state_dir(session_id: str) -> str:
    t = os.environ.get("TMPDIR", "/tmp").rstrip("/")
    return f"{t}/codex-bridge-{session_id[:40]}"

def _read(session_id: str, key: str) -> Optional[str]:
    try:
        with open(os.path.join(_state_dir(session_id), key)) as f:
            v = f.read().strip()
            return v or None
    except Exception:
        return None

def _write(session_id: str, key: str, value: str) -> None:
    try:
        os.makedirs(_state_dir(session_id), exist_ok=True)
        with open(os.path.join(_state_dir(session_id), key), "w") as f:
            f.write(value)
    except Exception:
        pass

def _next_revision(session_id: str) -> int:
    cur = _read(session_id, "revision")
    n = (int(cur) if cur and cur.isdigit() else 1000) + 1
    _write(session_id, "revision", str(n))
    return n


# ── Config (dedicated-session mapping) ──────────────────────────────────────────

def _load_config() -> Dict[str, Any]:
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {}

def _save_config(cfg: Dict[str, Any]) -> None:
    try:
        os.makedirs(RUNTIME_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass

def _load_template() -> Optional[Dict[str, Any]]:
    """Full 31-field conversationState captured from a real Codex frame."""
    try:
        with open(TEMPLATE_PATH) as f:
            frame = json.load(f)
        return frame["params"]["change"]["conversationState"]
    except Exception:
        return None


# ── Socket discovery ────────────────────────────────────────────────────────────

def find_socket() -> Optional[str]:
    uid = os.getuid()
    tmpdir = os.environ.get("TMPDIR", "").rstrip("/")
    deadline = time.time() + 1.0
    while True:
        candidates: List[str] = []
        if tmpdir:
            candidates.append(f"{tmpdir}/codex-ipc/ipc-{uid}.sock")
        candidates += glob.glob(f"/var/folders/*/*/T/codex-ipc/ipc-{uid}.sock")
        candidates.append(f"/tmp/codex-ipc/ipc-{uid}.sock")
        for p in candidates:
            if os.path.exists(p):
                return p
        if time.time() >= deadline:
            return None
        time.sleep(0.1)


# ── Wire framing: 4-byte LE length prefix + UTF-8 JSON ──────────────────────────

def _send_frame(sock: socket.socket, obj: Dict[str, Any]) -> None:
    data = json.dumps(obj).encode("utf-8")
    sock.sendall(struct.pack("<I", len(data)) + data)

def _recv_exact(sock: socket.socket, n: int) -> Optional[bytes]:
    buf = b""
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except socket.timeout:
            return None
        if not chunk:
            return None
        buf += chunk
    return buf

def _recv_frame(sock: socket.socket) -> Optional[Dict[str, Any]]:
    header = _recv_exact(sock, 4)
    if not header:
        return None
    (length,) = struct.unpack("<I", header)
    if length == 0 or length > MAX_FRAME:
        return None
    body = _recv_exact(sock, length)
    if body is None:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        return None


def connect_and_init(sock_path: str,
                     client_type: str = "claude-code-bridge") -> Optional[Tuple[socket.socket, str]]:
    """Connect, perform the initialize handshake, return (sock, clientId)."""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(3.0)
        s.connect(sock_path)
        _send_frame(s, {
            "type": "request",
            "requestId": str(uuid.uuid4()),
            "sourceClientId": "initializing-client",
            "version": 0,
            "method": "initialize",
            "params": {"clientType": client_type},
        })
        deadline = time.time() + 3.0
        while time.time() < deadline:
            msg = _recv_frame(s)
            if msg is None:
                break
            if msg.get("method") == "initialize" and msg.get("resultType") == "success":
                client_id = (msg.get("result") or {}).get("clientId")
                if client_id:
                    s.settimeout(2.0)
                    return s, client_id
        s.close()
        return None
    except Exception:
        return None


def _broadcast(sock: socket.socket, client_id: str, method: str,
               version: int, params: Dict[str, Any]) -> None:
    _send_frame(sock, {
        "type": "broadcast",
        "method": method,
        "sourceClientId": client_id,
        "version": version,
        "params": params,
    })


# ── Dedicated "Claude Code" session (via app-server stdio channel) ──────────────

def ensure_dedicated_session(claude_session_id: str) -> Optional[str]:
    """
    Return a Codex conversationId to drive for this Claude Code session, WITHOUT
    ever blocking on a network/LLM call in the hook's critical path.

    Fast paths (return immediately):
      - session already mapped in config;
      - a free pre-created session is available to claim (the DEFAULT, or one
        pre-warmed by a prior background create).

    Slow path (return None this once): no session is ready → spawn a detached
    background process to create one. The current tool call simply shows no pet
    activity; the next tool call finds the freshly-created id in config.
    """
    cfg = _load_config()
    session_map: Dict[str, str] = cfg.get("session_map", {})

    if claude_session_id in session_map:
        _ensure_spare_warm(cfg)          # keep one spare ready for the next new session
        return session_map[claude_session_id]

    claimed = set(session_map.values())

    # Fast path: claim the pre-created default if nobody else has it yet.
    if DEFAULT_DEDICATED_ID not in claimed and _thread_exists(DEFAULT_DEDICATED_ID):
        conv = _claim(cfg, session_map, claude_session_id, DEFAULT_DEDICATED_ID)
        _ensure_spare_warm(_load_config())
        return conv

    # Fast path: claim a pre-warmed spare produced by a prior background create.
    for spare in list(cfg.get("spare_sessions", [])):
        if spare not in claimed and _thread_exists(spare):
            cfg["spare_sessions"] = [s for s in cfg.get("spare_sessions", []) if s != spare]
            conv = _claim(cfg, session_map, claude_session_id, spare)
            _ensure_spare_warm(_load_config())   # replenish the pool
            return conv

    # Slow path: create asynchronously; skip animation for this one tool call.
    _spawn_background_create(claude_session_id)
    return None


def _ensure_spare_warm(cfg: Dict[str, Any]) -> None:
    """Keep at least one usable spare session pre-created in the background."""
    claimed = set(cfg.get("session_map", {}).values())
    spares = [s for s in cfg.get("spare_sessions", []) if s not in claimed]
    if spares:
        return
    # No spare ready → warm one up (debounced, detached, non-blocking).
    _spawn_background_create("__spare__")


def _claim(cfg: Dict[str, Any], session_map: Dict[str, str],
           claude_session_id: str, conv_id: str) -> str:
    session_map[claude_session_id] = conv_id
    cfg["session_map"] = session_map
    _save_config(cfg)
    return conv_id


def _spawn_background_create(claude_session_id: str) -> None:
    """Detach a process that creates a dedicated session and records it in config."""
    cfg = _load_config()
    pending = cfg.get("pending_create", {})
    # Debounce: don't spawn a second creator for the same session within 2 min.
    last = pending.get(claude_session_id, 0)
    if time.time() - last < 120:
        return
    pending[claude_session_id] = int(time.time())
    cfg["pending_create"] = pending
    _save_config(cfg)
    try:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--create-session", claude_session_id],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass


def _run_background_create(claude_session_id: str) -> None:
    """Entry point for the detached creator process."""
    conv_id = _create_dedicated_session()
    cfg = _load_config()
    session_map: Dict[str, str] = cfg.get("session_map", {})
    pending = cfg.get("pending_create", {})
    pending.pop(claude_session_id, None)
    cfg["pending_create"] = pending
    if conv_id:
        # "__spare__" warm-ups always go to the pool; a real session claims it
        # only if still unmapped, otherwise it also becomes a spare.
        if claude_session_id != "__spare__" and claude_session_id not in session_map:
            session_map[claude_session_id] = conv_id
            cfg["session_map"] = session_map
        else:
            spares = cfg.get("spare_sessions", [])
            if conv_id not in spares:
                spares.append(conv_id)
            cfg["spare_sessions"] = spares
    _save_config(cfg)


def _thread_exists(conv_id: str) -> bool:
    """Check the shared state DB for a non-archived thread row."""
    try:
        db = os.path.expanduser("~/.codex/state_5.sqlite")
        out = subprocess.run(
            ["sqlite3", db,
             f"SELECT 1 FROM threads WHERE id='{conv_id}' AND archived=0 LIMIT 1;"],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() == "1"
    except Exception:
        return False


def _create_dedicated_session() -> Optional[str]:
    """
    Create a dedicated "Claude Code" thread the pet will surface.

    Must be done in ONE app-server process: thread/start (source=vscode) then
    turn/start (sets preview = first user message) so the thread persists to
    state_5.sqlite and is returned by thread/list. Splitting these across two
    processes fails — a bare thread/start in an isolated process does not persist.
    Waits for turn/completed, then verifies the row actually landed in the DB.
    """
    cwd = os.path.expanduser("~")
    state: Dict[str, Any] = {"tid": None, "turn_sent": False, "turn_done": False}

    proc = subprocess.Popen(
        [CODEX_BIN, "app-server", "--stdio"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, bufsize=0,
    )
    lines: List[str] = []
    import threading
    threading.Thread(
        target=lambda: [lines.append(l.decode("utf-8", "replace").rstrip())
                        for l in proc.stdout],  # type: ignore[union-attr]
        daemon=True).start()

    def send(o: Dict[str, Any]) -> None:
        proc.stdin.write((json.dumps(o) + "\n").encode())  # type: ignore[union-attr]
        proc.stdin.flush()  # type: ignore[union-attr]

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"clientInfo": {"name": "claude-code-bridge",
                                        "title": "Claude Code", "version": "1.0"},
                         "capabilities": {}}})
        time.sleep(0.8)
        send({"jsonrpc": "2.0", "method": "initialized"})
        time.sleep(0.3)
        send({"jsonrpc": "2.0", "id": 2, "method": "thread/start",
              "params": {"threadSource": "user", "cwd": cwd}})

        deadline = time.time() + 75.0
        while time.time() < deadline and not state["turn_done"]:
            for l in list(lines):
                try:
                    m = json.loads(l)
                except Exception:
                    continue
                if m.get("id") == 2 and "result" in m and not state["tid"]:
                    state["tid"] = (m["result"].get("thread") or {}).get("id")
                    if state["tid"] and not state["turn_sent"]:
                        send({"jsonrpc": "2.0", "id": 3, "method": "turn/start",
                              "params": {"threadId": state["tid"],
                                         "input": [{"type": "text", "text": "Claude Code"}]}})
                        state["turn_sent"] = True
                if m.get("method") == "turn/completed":
                    state["turn_done"] = True
            lines.clear()
            time.sleep(0.2)
    finally:
        try:
            proc.kill()
        except Exception:
            pass

    tid = state["tid"]
    if not tid:
        return None
    # Confirm the thread actually persisted (preview set) before trusting it.
    for _ in range(10):
        if _thread_exists(tid):
            return tid
        time.sleep(0.3)
    return tid if state["turn_done"] else None


# ── conversationState construction (mutate the full real template) ──────────────

def _now_ms() -> int:
    return int(time.time() * 1000)


def _sanitize_str(v: Any) -> str:
    return v if isinstance(v, str) else ("" if v is None else str(v))


def _build_command_item(item_id: str, tool_input: Dict[str, Any],
                        cwd: str, status: str,
                        tool_response: Any = None) -> Dict[str, Any]:
    command = _sanitize_str(tool_input.get("command"))[:2000]
    output = _sanitize_str(tool_response)[:2000] if tool_response is not None else ""
    return {
        "type": "commandExecution",
        "id": item_id,
        "command": command,
        "cwd": cwd,
        "processId": "1",
        "source": "unifiedExecStartup",
        "status": status,
        "commandActions": [{"type": "unknown", "command": command[:200], "path": None}],
        "aggregatedOutput": None if status == "inProgress" else output,
        "exitCode": None if status == "inProgress" else 0,
        "durationMs": None if status == "inProgress" else 0,
    }


def _build_mcp_item(item_id: str, tool_name: str, tool_input: Any,
                    status: str, tool_response: Any = None) -> Dict[str, Any]:
    args = tool_input if isinstance(tool_input, dict) else {}
    result = None
    if status != "inProgress" and tool_response is not None:
        text = _sanitize_str(tool_response)[:2000]
        if text:
            result = {"content": [{"type": "text", "text": text}]}
    return {
        "type": "mcpToolCall",
        "id": item_id,
        "server": "claude-code",
        "tool": tool_name or "tool",
        "arguments": args,
        "status": status,
        "result": result,
        "error": None,
        "durationMs": None if status == "inProgress" else 0,
    }


def _build_item(tool_name: str, tool_input: Any, item_id: str,
                cwd: str, status: str, tool_response: Any = None) -> Dict[str, Any]:
    safe = tool_input if isinstance(tool_input, dict) else {}
    if tool_name == "Bash":
        return _build_command_item(item_id, safe, cwd, status, tool_response)
    return _build_mcp_item(item_id, tool_name, safe, status, tool_response)


def _build_agent_message_item(text: str) -> Dict[str, Any]:
    """An assistant-message item — the overlay renders its text as the bubble body."""
    t = (text or "").strip()
    if len(t) > MAX_BUBBLE_CHARS:
        t = t[:MAX_BUBBLE_CHARS - 1].rstrip() + "…"
    return {"type": "agentMessage", "id": str(uuid.uuid4()),
            "text": t, "phase": None, "memoryCitation": None}


# ── Transcript reading (assistant text + thinking) ──────────────────────────────

def _transcript_path(data: Dict[str, Any]) -> Optional[str]:
    """Prefer the hook-provided path; else locate <session_id>.jsonl under projects."""
    p = data.get("transcript_path")
    if p and os.path.exists(p):
        return p
    session_id = data.get("session_id")
    if not session_id:
        return None
    hits = glob.glob(os.path.expanduser(f"~/.claude/projects/*/{session_id}.jsonl"))
    return hits[0] if hits else None


def _tail_lines(path: str) -> List[str]:
    """Read only the tail of a (possibly huge) transcript file."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            start = max(0, size - TRANSCRIPT_TAIL_BYTES)
            f.seek(start)
            data = f.read()
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines()
        if start > 0 and lines:
            lines = lines[1:]  # drop a possibly-truncated first line
        return lines[-TRANSCRIPT_TAIL_LINES:]
    except Exception:
        return []


def _latest_assistant_text(path: Optional[str]) -> Optional[str]:
    """Most recent assistant record's concatenated text blocks (skips tool_use)."""
    if not path:
        return None
    for line in reversed(_tail_lines(path)):
        try:
            o = json.loads(line)
        except Exception:
            continue
        if o.get("type") != "assistant":
            continue
        content = o.get("message", {}).get("content", [])
        if not isinstance(content, list):
            continue
        parts = [c.get("text", "") for c in content
                 if isinstance(c, dict) and c.get("type") == "text" and c.get("text", "").strip()]
        if parts:
            return "\n".join(parts)
    return None


def _latest_thinking(path: Optional[str]) -> Optional[str]:
    """Most recent thinking block content (tolerates redaction / absence)."""
    if not path:
        return None
    for line in reversed(_tail_lines(path)):
        try:
            o = json.loads(line)
        except Exception:
            continue
        if o.get("type") != "assistant":
            continue
        content = o.get("message", {}).get("content", [])
        if not isinstance(content, list):
            continue
        for c in content:
            if isinstance(c, dict) and c.get("type") == "thinking":
                th = c.get("thinking") or c.get("text")
                if th and th.strip():
                    return th
    return None


def _latest_user_prompt(path: Optional[str]) -> Optional[str]:
    """Most recent real user prompt text (skips tool_result / meta user turns)."""
    if not path:
        return None
    for line in reversed(_tail_lines(path)):
        try:
            o = json.loads(line)
        except Exception:
            continue
        if o.get("type") != "user":
            continue
        msg = o.get("message", {})
        content = msg.get("content")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts = [c.get("text", "") for c in content
                     if isinstance(c, dict) and c.get("type") == "text"]
            text = " ".join(p for p in parts if p.strip())
        text = text.strip()
        # Skip tool-result / command-output / meta turns that aren't real prompts.
        if not text or text.startswith("<") or text.startswith("[Request interrupted"):
            continue
        return " ".join(text.split())   # collapse whitespace
    return None


_SURFACE_LABEL = {
    "cursor": "Claude Code · Cursor",
    "cursor-native": "Claude Code · Cursor (Agent)",
    "vscode": "Claude Code · VS Code",
    "desktop": "Claude Code · Desktop",
    "terminal": "Claude Code · Terminal",
}

def _card_subtitle(surface: str) -> str:
    """Card subtitle = the surface label (its own row, under the title)."""
    return _SURFACE_LABEL.get(surface, "Claude Code")

def _card_title(surface: str, data: Dict[str, Any]) -> str:
    """Card title = the user's latest question (surface moved to subtitle)."""
    prompt = _latest_user_prompt(_transcript_path(data))
    if prompt:
        return prompt[:60] + ("…" if len(prompt) > 60 else "")
    return "Claude Code"


def build_snapshot(template_cs: Dict[str, Any], conv_id: str, revision: int,
                   *, item: Optional[Dict[str, Any]], turn_status: str,
                   runtime_active: bool, has_unread: bool,
                   cwd: str) -> Dict[str, Any]:
    """
    Full-fidelity conversationState: deep-copy the real template and change only
    the identity/title/state fields. Keeping every other field prevents the
    overlay from throwing (which shows an error badge).
    """
    cs = copy.deepcopy(template_cs)
    cs["id"] = conv_id
    cs["sessionId"] = conv_id
    cs["title"] = "Claude Code"
    cs["cwd"] = cwd
    cs["updatedAt"] = _now_ms()
    cs["recencyAt"] = _now_ms()
    cs["resumeState"] = "resumed"
    cs["hasUnreadTurn"] = has_unread
    cs["requests"] = []
    cs["threadRuntimeStatus"] = (
        {"type": "active", "activeFlags": []} if runtime_active else {"type": "idle"}
    )

    turn = copy.deepcopy(cs["turns"][-1])
    turn["turnId"] = str(uuid.uuid4())
    turn["status"] = turn_status
    turn["turnStartedAtMs"] = _now_ms()
    turn["durationMs"] = None if turn_status == "inProgress" else 3000
    turn["finalAssistantStartedAtMs"] = None if turn_status == "inProgress" else _now_ms()
    turn["error"] = None
    turn["hookRuns"] = []
    if isinstance(turn.get("params"), dict):
        turn["params"]["threadId"] = conv_id
        turn["params"]["cwd"] = cwd
    if item is not None:
        turn["items"] = [item]
        turn["commandExecutionStartedAtMsById"] = (
            {item["id"]: _now_ms()} if item.get("type") == "commandExecution" else {}
        )
    else:
        # No item → empty turn; the overlay shows its built-in "Thinking" subtitle.
        turn["items"] = []
        turn["commandExecutionStartedAtMsById"] = {}
    cs["turns"] = [turn]

    return {
        "conversationId": conv_id,
        "hostId": "local",
        "version": STREAM_VERSION,
        "type": STREAM_METHOD,
        "change": {"type": "snapshot", "revision": revision,
                   "conversationState": cs},
    }


def _register(sock: socket.socket, client_id: str, conv_id: str) -> None:
    """Force the pet to (re)load conv_id into its recent list."""
    _broadcast(sock, client_id, UNARCH_METHOD, UNARCH_VERSION,
               {"hostId": "local", "conversationId": conv_id})


# ── Native target: drive the independent pet daemon ─────────────────────────────

def _ccpet_send(msg: Dict[str, Any]) -> bool:
    """Send one newline-JSON message to the pet daemon. Returns success."""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.4)
        s.connect(_ccpet_sock())
        s.sendall((json.dumps(msg) + "\n").encode())
        s.close()
        return True
    except Exception:
        return False


def _ccpet_ensure_daemon() -> None:
    """Compile-if-needed and spawn the pet daemon detached if not reachable."""
    # Fast path: already alive?
    if _ccpet_send({"type": "ping"}):
        return
    # Respect a manual `/pet quit`: don't auto-reopen until `/pet on`.
    try:
        if _load_config().get("autostart_disabled"):
            return
    except Exception:
        pass
    # Debounce spawns via a short-lived marker.
    marker = os.path.join(RUNTIME_DIR, ".spawning")
    try:
        if os.path.exists(marker) and time.time() - os.path.getmtime(marker) < 15:
            return
    except Exception:
        pass
    try:
        os.makedirs(RUNTIME_DIR, exist_ok=True)
        with open(marker, "w") as f:
            f.write(str(time.time()))
    except Exception:
        pass
    # Compile if binary missing or older than source.
    try:
        need_build = (not os.path.exists(CCPET_BIN)) or (
            os.path.exists(CCPET_SRC) and
            os.path.getmtime(CCPET_SRC) > os.path.getmtime(CCPET_BIN))
        if need_build and os.path.exists(CCPET_SRC):
            subprocess.run(["swiftc", "-O", CCPET_SRC, "-o", CCPET_BIN,
                            "-framework", "AppKit"],
                           capture_output=True, timeout=90)
    except Exception:
        pass
    # Spawn detached.
    try:
        if os.path.exists(CCPET_BIN):
            subprocess.Popen([CCPET_BIN],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception:
        pass


def _ccpet_persist(session_id: str, state: str, text: Optional[str]) -> None:
    """Durable fallback so a restarted daemon resyncs even if a send was dropped."""
    try:
        os.makedirs(CCPET_STATE_DIR, exist_ok=True)
        obj = {"session": session_id, "state": state, "text": text or "",
               "ts": int(time.time())}
        with open(os.path.join(CCPET_STATE_DIR, f"{session_id[:60]}.json"), "w") as f:
            json.dump(obj, f)
    except Exception:
        pass


def native_emit(session_id: str, state: str, *, text: Optional[str] = None,
                cwd: str = "", surface: str = "", open_cmd: str = "",
                title: str = "", subtitle: str = "", transcript_path: str = "") -> None:
    """Push a state event to the independent pet; ensure the daemon is up."""
    msg = {"type": "state", "session": session_id, "state": state}
    if text:
        msg["text"] = text[:MAX_BUBBLE_CHARS]
    if cwd:
        msg["cwd"] = cwd
    if surface:
        msg["surface"] = surface
    if open_cmd:
        msg["open_cmd"] = open_cmd
    if title:
        msg["title"] = title[:120]
    if subtitle:
        msg["subtitle"] = subtitle[:60]
    if transcript_path:
        # The daemon uses this file's mtime as a liveness signal: a long turn
        # (generation/thinking/one slow tool) fires no hooks but keeps appending
        # to the transcript, so it must NOT be swept to "Stopped".
        msg["transcript_path"] = transcript_path
    if not _ccpet_send(msg):
        _ccpet_persist(session_id, state, text)
        _ccpet_ensure_daemon()
        _ccpet_send(msg)   # retry once after ensuring


# ── Hook handlers ────────────────────────────────────────────────────────────────

def _session_cwd(data: Dict[str, Any]) -> str:
    return data.get("cwd") or os.path.expanduser("~")


def _register_once(session_id: str, conv_id: str, sock: socket.socket,
                   client_id: str) -> None:
    """Register conv with the pet's recent list on first use this session."""
    if _read(session_id, "registered") != conv_id:
        _register(sock, client_id, conv_id)
        _write(session_id, "registered", conv_id)
        time.sleep(1.5)  # let refreshRecentConversations land


def handle_user_prompt_submit(data: Dict[str, Any], sock: socket.socket,
                              client_id: str, template: Dict[str, Any]) -> None:
    """User asked something → pet enters "Thinking" (running, no tool item yet)."""
    session_id = data.get("session_id") or "default"
    cwd = _session_cwd(data)

    conv_id = ensure_dedicated_session(session_id)
    if not conv_id:
        return
    _register_once(session_id, conv_id, sock, client_id)

    # No item → the overlay falls back to the built-in "Thinking" subtitle.
    snap = build_snapshot(template, conv_id, _next_revision(session_id),
                          item=None, turn_status="inProgress",
                          runtime_active=True, has_unread=False, cwd=cwd)
    _broadcast(sock, client_id, STREAM_METHOD, STREAM_VERSION, snap)



def handle_pre_tool_use(data: Dict[str, Any], sock: socket.socket, client_id: str,
                        template: Dict[str, Any]) -> None:
    session_id = data.get("session_id") or "default"
    tool_name  = data.get("tool_name") or "Unknown"
    tool_input = data.get("tool_input") or {}
    cwd = _session_cwd(data)

    conv_id = ensure_dedicated_session(session_id)
    if not conv_id:
        return

    _register_once(session_id, conv_id, sock, client_id)

    item = _build_item(tool_name, tool_input, str(uuid.uuid4()), cwd, "inProgress")
    snap = build_snapshot(template, conv_id, _next_revision(session_id),
                          item=item, turn_status="inProgress",
                          runtime_active=True, has_unread=False, cwd=cwd)
    _broadcast(sock, client_id, STREAM_METHOD, STREAM_VERSION, snap)


def handle_post_tool_use(data: Dict[str, Any], sock: socket.socket, client_id: str,
                         template: Dict[str, Any]) -> None:
    session_id = data.get("session_id") or "default"
    tool_name  = data.get("tool_name") or "Unknown"
    tool_input = data.get("tool_input") or {}
    cwd = _session_cwd(data)

    conv_id = _read(session_id, "registered")
    if not conv_id:
        return

    # Item completed, but the turn stays inProgress (more tools may follow).
    tool_response = data.get("tool_response")
    item = _build_item(tool_name, tool_input, str(uuid.uuid4()), cwd, "completed",
                       tool_response)
    snap = build_snapshot(template, conv_id, _next_revision(session_id),
                          item=item, turn_status="inProgress",
                          runtime_active=True, has_unread=False, cwd=cwd)
    _broadcast(sock, client_id, STREAM_METHOD, STREAM_VERSION, snap)


def handle_stop(data: Dict[str, Any], sock: socket.socket, client_id: str,
                template: Dict[str, Any]) -> None:
    session_id = data.get("session_id") or "default"
    cwd = _session_cwd(data)

    conv_id = _read(session_id, "registered")
    if not conv_id:
        return

    # Show the assistant's final response text (read from the transcript) as the
    # completed-turn bubble; fall back to a bare check-mark if none is available.
    text = _latest_assistant_text(_transcript_path(data))
    final_item = (_build_agent_message_item(text) if text
                  else _build_mcp_item(str(uuid.uuid4()), "Bash", {}, "completed"))

    # Completed + unread → check-mark ("review") state, showing the reply.
    snap = build_snapshot(template, conv_id, _next_revision(session_id),
                          item=final_item, turn_status="completed",
                          runtime_active=False, has_unread=True, cwd=cwd)
    _broadcast(sock, client_id, STREAM_METHOD, STREAM_VERSION, snap)
    time.sleep(2.0)

    # Clear unread → idle (keep the reply text visible).
    snap = build_snapshot(template, conv_id, _next_revision(session_id),
                          item=final_item, turn_status="completed",
                          runtime_active=False, has_unread=False, cwd=cwd)
    _broadcast(sock, client_id, STREAM_METHOD, STREAM_VERSION, snap)


# ── Diagnostics ───────────────────────────────────────────────────────────────

def _selftest() -> int:
    sock_path = find_socket()
    if not sock_path:
        print("✗ socket not found (is Codex running?)")
        return 1
    print(f"· socket: {sock_path}")
    conn = connect_and_init(sock_path)
    if not conn:
        print("✗ handshake failed")
        return 1
    s, cid = conn
    print(f"✓ handshake ok, clientId = {cid}")
    s.close()
    tmpl = _load_template()
    print("✓ template loaded" if tmpl else "✗ template missing at " + TEMPLATE_PATH)
    return 0 if tmpl else 1


def _demo() -> int:
    sock_path = find_socket()
    tmpl = _load_template()
    if not sock_path or not tmpl:
        print("✗ prerequisites missing"); return 1
    conn = connect_and_init(sock_path)
    if not conn:
        print("✗ handshake failed"); return 1
    s, cid = conn
    conv_id = ensure_dedicated_session("demo-session")
    if not conv_id:
        print("✗ no dedicated session"); return 1
    print(f"· driving dedicated conversation {conv_id}")
    _register(s, cid, conv_id); time.sleep(1.5)
    rev = 8000
    for label, tool, inp in [("Bash", "Bash", {"command": "ls -la"}),
                             ("Read", "Read", {"file_path": "/tmp/x"})]:
        for _ in range(4):
            rev += 1
            item = _build_item(tool, inp, str(uuid.uuid4()),
                               os.path.expanduser("~"), "inProgress")
            snap = build_snapshot(tmpl, conv_id, rev, item=item,
                                  turn_status="inProgress", runtime_active=True,
                                  has_unread=False, cwd=os.path.expanduser("~"))
            _broadcast(s, cid, STREAM_METHOD, STREAM_VERSION, snap)
            print(f"  >>> {label} (running)")
            time.sleep(2.0)
    rev += 1
    done = _build_mcp_item(str(uuid.uuid4()), "Bash", {}, "completed")
    _broadcast(s, cid, STREAM_METHOD, STREAM_VERSION,
               build_snapshot(tmpl, conv_id, rev, item=done, turn_status="completed",
                              runtime_active=False, has_unread=True,
                              cwd=os.path.expanduser("~")))
    print("  >>> completed (check)"); time.sleep(2.0)
    rev += 1
    _broadcast(s, cid, STREAM_METHOD, STREAM_VERSION,
               build_snapshot(tmpl, conv_id, rev, item=done, turn_status="completed",
                              runtime_active=False, has_unread=False,
                              cwd=os.path.expanduser("~")))
    print("  >>> idle"); s.close()
    return 0


# ── Native-target event derivation (surface detection, state, text) ─────────────

def _cursor_native_session_map() -> Dict[str, str]:
    """Map {native-channel session id → workspace folder} from Cursor's storage.

    Cursor 3.11's "Toggle Agent → Claude Code" NATIVE channel records its
    currently-active session id per workspace in state.vscdb under the memento
    key `webviewView.claudeVSCodeSidebarSecondary` (webviewState.sessionID). The
    OLD anthropic.claude-code EXTENSION channel never writes there. This is the
    only reliable discriminator between the two (both spawn the same extension
    binary with identical env / transcripts, so process/env cannot tell them
    apart — verified with labeled sample sessions).

    Caveat: the memento holds only the CURRENT native session per workspace and
    is overwritten by later native sessions. So this must be consulted at hook
    time (session is active → its id is in the memento right now) and the result
    persisted; do not rely on it being present at jump time.
    """
    out: Dict[str, str] = {}
    try:
        dbs = glob.glob(os.path.join(CURSOR_WS_STORAGE, "*", "state.vscdb"))
    except Exception:
        return out
    for db in dbs:
        wsdir = os.path.dirname(db)
        # workspace folder (for cwd → correct window)
        folder = ""
        try:
            with open(os.path.join(wsdir, "workspace.json")) as fh:
                wj = json.load(fh)
            uri = wj.get("folder", "")
            if uri.startswith("file://"):
                folder = urllib.parse.unquote(uri[len("file://"):])
        except Exception:
            folder = ""
        # native session id from the secondary-sidebar memento (read-only open)
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=0.5)
            try:
                row = conn.execute(
                    "SELECT value FROM ItemTable WHERE key="
                    "'memento/webviewView.claudeVSCodeSidebarSecondary'").fetchone()
            finally:
                conn.close()
        except Exception:
            continue
        if not row or not row[0]:
            continue
        try:
            outer = json.loads(row[0])
            inner = json.loads(outer.get("webviewState", "{}"))
            sid = inner.get("sessionID")
        except Exception:
            sid = None
        if sid:
            out[sid] = folder
    return out


def _detect_surface(data: Dict[str, Any]) -> str:
    """Session origin, for click-to-jump. Discriminators verified from real hook env."""
    env = os.environ
    ep = env.get("CLAUDE_CODE_ENTRYPOINT", "").lower()
    bundle = env.get("__CFBundleIdentifier", "").lower()

    # Cursor: unique bundle id (com.todesktop.*) + CURSOR_* env vars.
    if bundle.startswith("com.todesktop") or any(k.startswith("CURSOR_") for k in env):
        return "cursor"
    # VS Code: microsoft bundle id, or IPC hook path under Code/.
    if "com.microsoft.vscode" in bundle or "/Code/" in env.get("VSCODE_IPC_HOOK", ""):
        return "vscode"
    # Desktop app (Claude for desktop / CCD).
    if "desktop" in ep or "claudefordesktop" in bundle:
        return "desktop"
    # Any other editor host reporting the vscode entrypoint.
    if "vscode" in ep:
        return "vscode"
    # Terminal.
    if ep == "cli" or env.get("TERM_PROGRAM") or env.get("TERM_SESSION_ID"):
        return "terminal"
    return ep or "unknown"


def _current_tty() -> str:
    """The controlling tty (e.g. /dev/ttys003) of the terminal running this hook.

    Terminal.app exposes each tab's `tty`, so capturing it lets us focus the
    exact original tab on click (instead of opening a new one). The hook is a
    child of the `claude` process; walk up the ppid chain until a process with a
    real tty is found. Returns "" for editor/desktop sessions (no tty).
    """
    def tty_of(pid: int) -> str:
        try:
            out = subprocess.check_output(["ps", "-o", "tty=", "-p", str(pid)],
                                          stderr=subprocess.DEVNULL).decode().strip()
        except Exception:
            return ""
        # ps prints e.g. "ttys003" (no /dev/) for a real tty, "??" for none.
        return out if out and out != "??" else ""

    def ppid_of(pid: int) -> int:
        try:
            return int(subprocess.check_output(["ps", "-o", "ppid=", "-p", str(pid)],
                                               stderr=subprocess.DEVNULL).decode().strip())
        except Exception:
            return 0

    pid = os.getpid()
    for _ in range(8):
        t = tty_of(pid)
        if t:
            return t if t.startswith("/dev/") else f"/dev/{t}"
        pid = ppid_of(pid)
        if pid <= 1:
            break
    return ""


def _terminal_identity() -> Dict[str, str]:
    """Capture which terminal window/pane this hook runs in (for refocus)."""
    env = os.environ
    info: Dict[str, str] = {}
    tp = env.get("TERM_PROGRAM", "")
    if tp:
        info["term_program"] = tp
    for key in ("ITERM_SESSION_ID", "TERM_SESSION_ID", "WEZTERM_PANE",
                "KITTY_WINDOW_ID", "TMUX", "TMUX_PANE"):
        v = env.get(key)
        if v:
            info[key.lower()] = v
    # tty enables precise Terminal.app tab focus (Terminal exposes `tty` per tab).
    tty = _current_tty()
    if tty:
        info["tty"] = tty
    return info


def _record_surface(session_id: str, data: Dict[str, Any]) -> Dict[str, str]:
    """Persist per-session surface + cwd (+ terminal identity) so the daemon can jump later."""
    surface = _detect_surface(data)
    cwd = _session_cwd(data)
    entry: Dict[str, Any] = {"surface": surface, "cwd": cwd}
    if surface == "terminal":
        entry["term"] = _terminal_identity()
    # Distinguish Cursor's native "Toggle Agent → Claude Code" channel from the
    # old anthropic.claude-code extension. Both look identical to the hook, so we
    # consult Cursor's workspace storage NOW (while this session is active, its id
    # is in the secondary-sidebar memento) and persist the verdict. Never downgrade
    # a previously-recorded cursor-native back to cursor: the memento is overwritten
    # by later native sessions, so a miss on refresh does not mean "not native".
    if surface == "cursor":
        try:
            prior = _load_config().get("surfaces", {}).get(session_id, {})
            if prior.get("surface") == "cursor-native":
                entry["surface"] = "cursor-native"
                if prior.get("cwd"):
                    entry["cwd"] = prior["cwd"]
            else:
                native = _cursor_native_session_map()
                if session_id in native:
                    entry["surface"] = "cursor-native"
                    if native[session_id]:
                        entry["cwd"] = native[session_id]
        except Exception:
            pass
    try:
        cfg = _load_config()
        surfaces = cfg.get("surfaces", {})
        surfaces[session_id] = entry
        cfg["surfaces"] = surfaces
        _save_config(cfg)
    except Exception:
        pass
    return entry


def _terminal_new_tab_cmd(sid: str, cwd: str) -> str:
    """Fallback: resume the session in a fresh Terminal.app window at its cwd."""
    safe_cwd = cwd.replace('"', '\\"')
    inner = f'cd \\"{safe_cwd}\\" && claude --resume {sid}'
    return (f"osascript -e 'tell application \"Terminal\" to do script \"{inner}\"' "
            f"-e 'tell application \"Terminal\" to activate'")


def _iterm_focus_cmd(guid: str, sid: str, cwd: str) -> str:
    """Focus iTerm2's original tab by session GUID; fall back to a new tab.

    macOS gates this behind Automation permission — an unauthorized daemon gets
    an AppleScript error, so we OR in the new-tab fallback at the shell level.
    """
    # AppleScript to select the session whose `id` matches the captured GUID.
    script = (
        'tell application "iTerm" to repeat with w in windows\n'
        '  repeat with t in tabs of w\n'
        '    repeat with s in sessions of t\n'
        f'      if id of s is "{guid}" then\n'
        '        select s\n'
        '        tell t to select\n'
        '        tell w to select\n'
        '        activate\n'
        '        return "ok"\n'
        '      end if\n'
        '    end repeat\n'
        '  end repeat\n'
        'end repeat'
    )
    # If the GUID isn't found (session closed) or automation is denied, osascript
    # prints nothing / errors → run the new-tab fallback.
    focus = "osascript -e " + shlex.quote(script)
    fallback = _terminal_new_tab_cmd(sid, cwd)
    # `[ "$(...)" = ok ] || fallback`: only resume anew when focus didn't land.
    return f'[ "$({focus} 2>/dev/null)" = "ok" ] || ( {fallback} )'


def _terminal_app_focus_cmd(tty: str, sid: str, cwd: str) -> str:
    """Focus Terminal.app's original tab by its tty; fall back to a new tab.

    Terminal.app exposes `tty` on every tab, so we can select the exact tab whose
    tty matches the session's controlling terminal (captured at hook time). Unlike
    iTerm, tab selection here needs no extra Automation grant beyond the one-time
    "control Terminal" prompt. If the tab was closed (tty gone), fall back to a
    fresh resume tab.
    """
    script = (
        'tell application "Terminal"\n'
        f'  set target to "{tty}"\n'
        '  repeat with w in windows\n'
        '    repeat with t in tabs of w\n'
        '      if tty of t is target then\n'
        '        set selected of t to true\n'
        '        set index of w to 1\n'
        '        activate\n'
        '        return "ok"\n'
        '      end if\n'
        '    end repeat\n'
        '  end repeat\n'
        '  return "notfound"\n'
        'end tell'
    )
    focus = "osascript -e " + shlex.quote(script)
    fallback = _terminal_new_tab_cmd(sid, cwd)
    return f'[ "$({focus} 2>/dev/null)" = "ok" ] || ( {fallback} )'


def _resolve_desktop_internal_id(cli_session_id: str) -> Optional[str]:
    """Map a CLI transcript uuid → CCD's internal `local_<uuid>` session id.

    CCD does NOT use `local_<cliSessionId>` — the internal id is a separate
    random uuid, and the same cliSessionId can map to several internal ids
    (stale duplicates). CCD persists per-session metadata as
    `<root>/**/local_<internalUuid>.json` containing {sessionId, cliSessionId,
    lastActivityAt}. Match on cliSessionId and pick the freshest — that's the
    live view the permission notification would navigate to.
    """
    roots = [
        os.path.expanduser("~/Library/Application Support/Claude-3p/claude-code-sessions"),
        os.path.expanduser("~/Library/Application Support/Claude-3p/local-agent-mode-sessions"),
    ]
    best_id: Optional[str] = None
    best_ts = -1.0
    for root in roots:
        for f in glob.glob(os.path.join(root, "**", "local_*.json"), recursive=True):
            try:
                with open(f) as fh:
                    d = json.load(fh)
            except Exception:
                continue
            if d.get("cliSessionId") != cli_session_id:
                continue
            ts = d.get("lastActivityAt") or 0
            try:
                ts = float(ts)
            except Exception:
                ts = 0.0
            if ts > best_ts:
                best_ts = ts
                best_id = d.get("sessionId")
    return best_id


def _open_cmd_for(session_id: str, surface: str, cwd: str,
                  term: Optional[Dict[str, str]] = None) -> str:
    """Shell command that opens/focuses this Claude Code session, per surface."""
    sid = session_id
    # Focus the correct Cursor PROJECT WINDOW by folder. `open -b <bundleid>
    # <folder>` reliably raises the existing window bound to that folder (6/6 in
    # testing; `cursor -r` was only 3/4). This fixes "jumps to the wrong Cursor
    # instance when multiple windows are open".
    if surface in ("cursor", "cursor-native") and cwd:
        focus = f'open -b {CURSOR_BUNDLE_ID} {shlex.quote(cwd)}'
        if surface == "cursor-native":
            # New Toggle-Agent channel: the native chat lives inside that window's
            # agent panel. There is NO Cursor deeplink/CLI to select a specific
            # native composer, so focusing the right window is the best achievable
            # (and it avoids the extension deeplink spawning a spurious editor tab).
            return focus
        # Old extension channel: focus the window, then resume the session in an
        # editor tab via the extension's deeplink (its normal behavior).
        return f'{focus} ; sleep 0.3 ; open "cursor://anthropic.claude-code/open?session={sid}"'
    if surface == "cursor":
        return f'open "cursor://anthropic.claude-code/open?session={sid}"'
    if surface == "cursor-native":
        # No cwd to focus by — fall back to just bringing Cursor forward.
        return f'open -b {CURSOR_BUNDLE_ID}'
    if surface == "vscode":
        return f'open "vscode://anthropic.claude-code/open?session={sid}"'
    if surface == "desktop":
        # Resolve the live internal id from CCD metadata (the freshest
        # `local_<uuid>` whose cliSessionId == our transcript uuid — CCD's
        # internal id is a distinct random uuid, not local_<cli>, and one cli id
        # can have several stale internal ids).
        #
        # Route via the `claude-code-desktop` deeplink host: it resolves the
        # internal id and calls LocalSessions.setFocusedSession — verified by
        # log (`[CCD] LocalSessions.setFocusedSession` + `startShellPty`). The
        # `cowork` host instead hard-reloads the SPA to its home view (the
        # "always jumps to Cowork" bug).
        internal = _resolve_desktop_internal_id(sid) or f"local_{sid}"
        return f'open "claude://claude.ai/claude-code-desktop/{internal}"'
    # terminal / unknown.
    term = term or {}
    iterm_sid = term.get("iterm_session_id", "")
    if term.get("term_program") == "iTerm.app" and iterm_sid:
        # ITERM_SESSION_ID looks like "w0t0p0:<GUID>"; the GUID is iTerm's
        # `id of session`. Focus that exact tab, fall back to a new tab.
        guid = iterm_sid.split(":", 1)[1] if ":" in iterm_sid else iterm_sid
        return _iterm_focus_cmd(guid, sid, cwd)
    # Terminal.app (and other ttys): focus the exact tab by its tty. Terminal.app
    # is TERM_PROGRAM=Apple_Terminal; gate on a captured tty so we only try the
    # AppleScript focus when we actually have one (else new-tab fallback).
    tty = term.get("tty", "")
    if tty and term.get("term_program") in ("Apple_Terminal", "", None):
        return _terminal_app_focus_cmd(tty, sid, cwd)
    # Unknown terminal without a usable locator → new tab.
    return _terminal_new_tab_cmd(sid, cwd)


def native_handle(command: str, data: Dict[str, Any]) -> None:
    """Drive the independent pet directly (no Codex socket needed)."""
    session_id = data.get("session_id") or "default"
    cwd = _session_cwd(data)

    # Fast path for session_end: the session is closing, so all the surface /
    # jump-command / transcript / Cursor-vscdb work below is pointless — and the
    # vscdb reads in _cursor_native_session_map() can be slow while Cursor is
    # shutting down, which shows up as Cursor's "Composer Session End Hooks …
    # taking a bit longer" dialog. Just tell the daemon and return immediately.
    if command == "session_end":
        _ccpet_send({"type": "session_end", "session": session_id})
        return

    # Resolve this session's surface + click-to-open command. Record on first
    # sighting of the session (via any hook) so already-running sessions also get
    # a surface, and refresh on prompt/start when the full env is freshest.
    cfg = _load_config()
    known = cfg.get("surfaces", {}).get(session_id)
    if command in ("user_prompt_submit", "session_start") or not known:
        info = _record_surface(session_id, data)
    else:
        info = known
    surface = info.get("surface", "unknown")
    # Race fix: a native-channel session's first hook (session_start) can fire
    # BEFORE Cursor writes its id into the secondary-sidebar memento, so it gets
    # tagged "cursor" and its jump command would spuriously open an extension tab.
    # On EVERY event, re-check the memento for any still-"cursor" session and
    # upgrade → "cursor-native" the moment it appears (persisting so it sticks).
    # This shrinks the misclassification window to at most one event.
    if surface == "cursor":
        try:
            native = _cursor_native_session_map()
            if session_id in native:
                surface = "cursor-native"
                info = dict(info)
                info["surface"] = "cursor-native"
                if native[session_id]:
                    info["cwd"] = native[session_id]
                try:
                    cfg2 = _load_config()
                    cfg2.setdefault("surfaces", {})[session_id] = info
                    _save_config(cfg2)
                except Exception:
                    pass
        except Exception:
            pass
    open_cmd = _open_cmd_for(session_id, surface, info.get("cwd", cwd),
                             term=info.get("term"))
    title = _card_title(surface, data)
    subtitle = _card_subtitle(surface)

    tpath = _transcript_path(data) or ""

    def emit(state, text=None):
        native_emit(session_id, state, text=text, cwd=cwd,
                    surface=surface, open_cmd=open_cmd, title=title, subtitle=subtitle,
                    transcript_path=tpath)

    if command == "user_prompt_submit":
        emit("thinking")
    elif command == "session_start":
        # Session opened (new session / resume / editor tab / `claude` launched).
        # Wake the pet so it's present (idle) the moment any Claude Code surface
        # opens — using the remembered pet from config. Record the surface above,
        # but do NOT show a card: a card only appears once there's real activity.
        if "native" in _targets():
            _ccpet_ensure_daemon()
    elif command == "pre_tool_use":
        tool = data.get("tool_name") or "tool"
        tool_input = data.get("tool_input") or {}
        # Tools that mean "Claude is waiting on YOU" get the prominent brown
        # attention state + a specific message, not a generic blue tool label.
        attn = _attention_label(tool, tool_input)
        if attn:
            emit("attention", attn)
        else:
            emit("running", _activity_label(tool, tool_input, done=False))
    elif command == "post_tool_use":
        # Show the completed-activity label, but prefer the latest assistant text
        # if the model has started replying (running-phase streaming feel).
        tool = data.get("tool_name") or "tool"
        # AskUserQuestion/ExitPlanMode PostToolUse means the user just answered —
        # let the normal flow (Stop/next prompt) take over; keep showing text.
        latest = _latest_assistant_text(_transcript_path(data))
        emit("running", latest or _activity_label(tool, data.get("tool_input") or {}, done=True))
    elif command in ("notification", "permission_request"):
        # Claude Code needs the user's attention (permission / approval / question
        # / plan review). Distinct "attention" state → brown badge.
        msg = data.get("message") or _permission_message(data) or "Needs your input"
        emit("attention", msg)
    elif command == "stop":
        text = _latest_assistant_text(_transcript_path(data))
        emit("review", text or "")
    # session_end is handled by the fast path at the top of native_handle.


def _attention_label(tool: str, tool_input: Dict[str, Any]) -> Optional[str]:
    """Message for tools that mean 'Claude is waiting on the user'.

    Returns None for ordinary tools (they use the blue running label).
    AskUserQuestion/ExitPlanMode arrive as normal PreToolUse tool calls, so we
    surface them here as the prominent brown attention state instead.
    """
    if tool == "AskUserQuestion":
        questions = tool_input.get("questions") or []
        if questions and isinstance(questions[0], dict):
            q = _sanitize_str(questions[0].get("question"))
            hdr = _sanitize_str(questions[0].get("header"))
            label = q or hdr
            if len(questions) > 1:
                return f"❓ {label[:50]}…（{len(questions)} 个问题）" if label else f"❓ 有 {len(questions)} 个问题等你"
            return f"❓ {label[:70]}" if label else "❓ 有问题等你回答"
        return "❓ 有问题等你回答"
    if tool == "ExitPlanMode":
        return "📋 计划待批准 — 是否执行?"
    return None


def _permission_message(data: Dict[str, Any]) -> Optional[str]:
    """Build a permission-prompt message from a PermissionRequest payload."""
    tool = data.get("tool_name")
    if not tool:
        return None
    tool_input = data.get("tool_input") or {}
    if tool == "Bash":
        cmd = _sanitize_str(tool_input.get("command"))[:60]
        return f"⚠️ 待批准命令: {cmd}" if cmd else "⚠️ 待批准运行命令"
    pretty = str(tool).replace("_", " ")
    return f"⚠️ 待批准: {pretty}"


def _activity_label(tool: str, tool_input: Dict[str, Any], done: bool) -> str:
    """Codex-style activity vocabulary for a Claude Code tool call."""
    def base(name: str) -> str:
        return name
    if tool == "Bash":
        return "Ran command" if done else "Running command"
    if tool == "Read":
        fn = os.path.basename(_sanitize_str(tool_input.get("file_path")))
        return (f"Read {fn}" if done else f"Reading {fn}") if fn else ("Read file" if done else "Reading file")
    if tool in ("Edit", "Write", "NotebookEdit"):
        return "Edited files" if done else "Editing files"
    if tool in ("Glob", "LS"):
        return "Listed files" if done else "Listing files"
    if tool == "Grep":
        q = _sanitize_str(tool_input.get("pattern"))[:40]
        return (f'Searched "{q}"' if done else f'Searching "{q}"') if q else ("Searched files" if done else "Searching files")
    if tool == "WebSearch":
        q = _sanitize_str(tool_input.get("query"))[:40]
        return (f'Searched "{q}"' if done else f'Searching "{q}"') if q else "Searched web"
    if tool == "WebFetch":
        return "Searched web"
    if tool.startswith("mcp__"):
        parts = tool.split("__")
        name = parts[-1].replace("_", " ") if parts else tool
        return f"Called {name}" if done else f"Calling {name}"
    # Generic tool
    pretty = tool.replace("_", " ")
    return f"Called {pretty}" if done else f"Calling {pretty}"


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    if len(sys.argv) < 2:
        return
    command = sys.argv[1]

    if command == "--selftest":
        sys.exit(_selftest())
    if command == "--demo":
        sys.exit(_demo())
    if command == "--create-session":
        # Detached background worker: create a dedicated session, record in config.
        _run_background_create(sys.argv[2] if len(sys.argv) > 2 else "default")
        return

    try:
        raw = sys.stdin.read()
        data: Dict[str, Any] = json.loads(raw) if raw.strip() else {}
    except Exception:
        data = {}

    targets = _targets()

    # Native independent pet (default): fast, no Codex dependency.
    if "native" in targets:
        try:
            native_handle(command, data)
        except Exception:
            pass

    # Codex-pet integration (opt-in): drive the existing Codex overlay too.
    if "codex" in targets:
        try:
            _codex_handle(command, data)
        except Exception:
            pass


def _codex_handle(command: str, data: Dict[str, Any]) -> None:
    """Phase-1 path: drive the Codex desktop pet over its IPC socket."""
    template = _load_template()
    if template is None:
        return
    sock_path = find_socket()
    if not sock_path:
        return
    conn = connect_and_init(sock_path)
    if conn is None:
        return
    sock, client_id = conn
    try:
        if command == "pre_tool_use":
            handle_pre_tool_use(data, sock, client_id, template)
        elif command == "post_tool_use":
            handle_post_tool_use(data, sock, client_id, template)
        elif command == "user_prompt_submit":
            handle_user_prompt_submit(data, sock, client_id, template)
        elif command == "stop":
            handle_stop(data, sock, client_id, template)
    finally:
        try:
            sock.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
ccpet-ctl.py — control the independent Claude Code desktop pet (ccpet).

Usage:
  ccpet-ctl.py list            # list available pets (personal library ~/.codex/pets)
  ccpet-ctl.py use <name>      # switch to pet <name> (fuzzy match)
  ccpet-ctl.py on              # show / wake the pet (spawn daemon if needed)
  ccpet-ctl.py off             # hide the pet
  ccpet-ctl.py quit            # stop the pet daemon entirely
  ccpet-ctl.py status          # show current pet + daemon state
  ccpet-ctl.py grant           # authorize iTerm focus (macOS Automation prompt)
"""

import json
import os
import socket
import subprocess
import sys
import glob

# Read-only plugin assets live next to this script; writable runtime state
# (config, compiled binary) lives in ~/.ccpet — never in the plugin dir.
SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
RUNTIME_DIR = os.path.expanduser("~/.ccpet")
CONFIG_PATH = os.path.join(RUNTIME_DIR, "config.json")
CCPET_BIN   = os.path.join(RUNTIME_DIR, "ccpet")
CCPET_SRC   = os.path.join(SCRIPT_DIR, "ccpet.swift")
PETS_DIR    = os.path.expanduser("~/.codex/pets")


def _sock() -> str:
    t = os.environ.get("TMPDIR", "/tmp").rstrip("/")
    return f"{t}/ccpet/daemon.sock"


def _load_config() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_config(cfg: dict) -> None:
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def _send(msg: dict) -> bool:
    # Retry a couple of times: the daemon may briefly be busy (e.g. reloading a
    # spritesheet) and refuse a connection.
    import time
    for attempt in range(3):
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(1.0)
            s.connect(_sock())
            s.sendall((json.dumps(msg) + "\n").encode())
            s.close()
            return True
        except Exception:
            time.sleep(0.2)
    return False


def _daemon_alive() -> bool:
    return _send({"type": "ping"})


def _spawn_daemon() -> None:
    """Compile if needed, then spawn the daemon detached."""
    try:
        os.makedirs(RUNTIME_DIR, exist_ok=True)   # swiftc output dir must exist
        need_build = (not os.path.exists(CCPET_BIN)) or (
            os.path.exists(CCPET_SRC) and
            os.path.getmtime(CCPET_SRC) > os.path.getmtime(CCPET_BIN))
        if need_build and os.path.exists(CCPET_SRC):
            subprocess.run(["swiftc", "-O", CCPET_SRC, "-o", CCPET_BIN,
                            "-framework", "AppKit"], capture_output=True, timeout=90)
        if os.path.exists(CCPET_BIN):
            subprocess.Popen([CCPET_BIN], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
    except Exception as e:
        print(f"spawn failed: {e}")


def _pets() -> list:
    """List pets in the personal library (dir name + displayName from pet.json)."""
    out = []
    for pj in sorted(glob.glob(os.path.join(PETS_DIR, "*", "pet.json"))):
        name = os.path.basename(os.path.dirname(pj))
        display = name
        try:
            with open(pj) as f:
                display = json.load(f).get("displayName", name)
        except Exception:
            pass
        # Only list pets that actually have a spritesheet.
        if os.path.exists(os.path.join(os.path.dirname(pj), "spritesheet.webp")):
            out.append((name, display))
    return out


def cmd_list() -> None:
    cfg = _load_config()
    cur = cfg.get("pet", "spongebob-star")
    pets = _pets()
    print(f"当前宠物: {cur}")
    print(f"可用宠物 ({len(pets)} 个,来自 ~/.codex/pets):\n")
    for name, display in pets:
        mark = " ← 当前" if name == cur else ""
        print(f"  {name:28s} {display}{mark}")
    print("\n换宠物: /pet use <名字>   开关: /pet on|off")


def cmd_use(query: str) -> None:
    pets = _pets()
    names = [n for n, _ in pets]
    # exact, then prefix, then substring
    match = None
    for n in names:
        if n == query:
            match = n; break
    if not match:
        cand = [n for n in names if n.startswith(query)] or \
               [n for n in names if query.lower() in n.lower()]
        if len(cand) == 1:
            match = cand[0]
        elif len(cand) > 1:
            print(f"'{query}' 匹配多个: {', '.join(cand)}\n请更具体。")
            return
    if not match:
        print(f"没找到宠物 '{query}'。用 /pet list 看可用宠物。")
        return
    cfg = _load_config()
    cfg["pet"] = match
    _save_config(cfg)
    if not _daemon_alive():
        _spawn_daemon()
    else:
        _send({"type": "switch_pet", "pet": match})
    print(f"✓ 已切换到宠物: {match}")


def cmd_on() -> None:
    # Clear the "stay off" flag so hook-driven auto-start works again.
    cfg = _load_config()
    if cfg.get("autostart_disabled"):
        cfg.pop("autostart_disabled", None)
        _save_config(cfg)
    if not _daemon_alive():
        _spawn_daemon()
        print("✓ 已唤醒桌宠(daemon 启动中)")
    else:
        _send({"type": "show"})
        print("✓ 桌宠已显示")


def cmd_off() -> None:
    if _send({"type": "hide"}):
        print("✓ 桌宠已隐藏(daemon 仍在后台,/pet on 恢复)")
    else:
        print("桌宠未在运行。")


def cmd_quit() -> None:
    # Persist a "stay off" flag so hook-driven auto-start won't reopen the pet
    # while Claude Code sessions are still running. Cleared by /pet on.
    cfg = _load_config()
    cfg["autostart_disabled"] = True
    _save_config(cfg)
    if _send({"type": "quit"}):
        print("✓ 桌宠已完全关闭(不会自动重开;/pet on 恢复)")
    else:
        print("桌宠未在运行(已设为不自动开启;/pet on 恢复)")


def cmd_status() -> None:
    cfg = _load_config()
    alive = _daemon_alive()
    print(f"宠物: {cfg.get('pet', 'spongebob-star')}")
    print(f"daemon: {'运行中' if alive else '未运行'}")
    print(f"targets: {cfg.get('targets', ['native'])}")
    print(f"iTerm 精确聚焦授权: {'已授权' if _iterm_authorized() else '未授权(用 /pet grant 开启)'}")


def _iterm_authorized() -> bool:
    """True if AppleScript can drive iTerm (Automation permission granted)."""
    try:
        r = subprocess.run(
            ["osascript", "-e", 'tell application "iTerm" to count windows'],
            capture_output=True, timeout=5, text=True)
        # -1743 / "not allowed" ⇒ not authorized. A numeric count ⇒ authorized.
        return r.returncode == 0 and r.stdout.strip().isdigit()
    except Exception:
        return False


def cmd_grant() -> None:
    """Trigger the macOS Automation permission prompt for iTerm focus-by-GUID."""
    if not _iterm_pkg_present():
        print("未检测到 iTerm2。终端跳转会用 Terminal.app 新标签页(无需授权)。")
        return
    if _iterm_authorized():
        print("✓ iTerm 自动化权限已授权,点击卡片可精确聚焦原 tab。")
        return
    print("正在触发 macOS 自动化授权框(可能弹窗,请点『好』/『允许』)…")
    try:
        subprocess.run(
            ["osascript", "-e", 'tell application "iTerm" to count windows'],
            capture_output=True, timeout=15)
    except Exception:
        pass
    if _iterm_authorized():
        print("✓ 授权成功。终端会话点击卡片将聚焦原 iTerm tab。")
    else:
        print("尚未授权。请在:")
        print("  系统设置 → 隐私与安全性 → 自动化 →(你的终端/ccpet)→ 勾选 iTerm")
        print("授权后再点击卡片即可精确聚焦;未授权时会退化为新标签页 resume。")


def _iterm_pkg_present() -> bool:
    for p in ("/Applications/iTerm.app", os.path.expanduser("~/Applications/iTerm.app")):
        if os.path.exists(p):
            return True
    return False


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] in ("list", "ls", ""):
        cmd_list(); return
    cmd = args[0]
    if cmd in ("use", "switch", "set") and len(args) > 1:
        cmd_use(args[1])
    elif cmd == "on" or cmd == "show" or cmd == "wake":
        cmd_on()
    elif cmd == "off" or cmd == "hide":
        cmd_off()
    elif cmd == "quit" or cmd == "kill" or cmd == "stop":
        cmd_quit()
    elif cmd == "status":
        cmd_status()
    elif cmd == "grant":
        cmd_grant()
    else:
        # `/pet spongebob` shorthand for `/pet use spongebob`
        cmd_use(cmd)


if __name__ == "__main__":
    main()

#!/bin/bash
# ccpet 插件 — 依赖检查 + 安装引导
#
# 用法:   bash install.sh
#
# 这个脚本只做「检查依赖 + 打印下一步」。真正的安装用 Claude Code 的插件机制:
#   1. 在 Claude Code 里:  /plugin marketplace add <本插件目录>
#   2. 然后:               /plugin install ccpet@ccpet-marketplace
# 装好后重开会话,任意端(终端/Cursor/VSCode/Desktop)一开就自动出现桌宠。

set -u

PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ok=0; warn=0

say()  { printf "%s\n" "$*"; }
good() { printf "  ✓ %s\n" "$*"; }
bad()  { printf "  ✗ %s\n" "$*"; warn=$((warn+1)); }

say "════════════ ccpet 插件依赖检查 ════════════"
say ""

# 1. macOS
if [ "$(uname)" = "Darwin" ]; then good "macOS"; else bad "只支持 macOS(当前 $(uname))"; fi

# 2. python3
if command -v python3 >/dev/null 2>&1; then
  good "python3: $(python3 --version 2>&1)"
else
  bad "缺 python3 —— macOS 自带 /usr/bin/python3,装 Xcode Command Line Tools 即可"
fi

# 3. swiftc(编译桌宠)
if command -v swiftc >/dev/null 2>&1; then
  good "swiftc(编译桌宠)"
else
  bad "缺 swiftc —— 运行  xcode-select --install  安装 Command Line Tools"
fi

# 4. 宠物图资源(~/.codex/pets 或 ~/.petdex/pets;没有则从 petdex.dev 拉一个)
have_pets() {
  ls "$HOME/.codex/pets"/*/spritesheet.webp >/dev/null 2>&1 \
    || ls "$HOME/.petdex/pets"/*/spritesheet.webp >/dev/null 2>&1
}
count_pets() {
  { ls -d "$HOME/.codex/pets"/*/spritesheet.webp 2>/dev/null;
    ls -d "$HOME/.petdex/pets"/*/spritesheet.webp 2>/dev/null; } \
    | sed 's#.*/\([^/]*\)/spritesheet.webp#\1#' | sort -u | wc -l | tr -d ' '
}

if have_pets; then
  good "宠物图: $(count_pets) 个(来自 ~/.codex/pets / ~/.petdex/pets)"
else
  # 没有任何宠物图 —— 尝试用 petdex.dev 的 CLI 自动拉一个默认宠物(boba)。
  node_ok=0
  if command -v node >/dev/null 2>&1; then
    major="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
    [ "${major:-0}" -ge 20 ] 2>/dev/null && node_ok=1
  fi
  if [ "$node_ok" -eq 1 ]; then
    say "  · 没找到宠物图 —— 正在从 petdex.dev 拉取默认宠物 boba(npx petdex install boba)…"
    if npx -y petdex@latest install boba >/dev/null 2>&1 && have_pets; then
      good "宠物图: 已从 petdex.dev 安装默认宠物 boba"
      say "    更多宠物见 https://petdex.dev —— 用  npx petdex install <名字>  安装,再  /pet use <名字>  切换"
    else
      bad "从 petdex.dev 自动拉取失败(可能无网络)。可稍后手动运行:  npx petdex@latest install boba"
      say "    或从 https://petdex.dev 挑宠物;也可装 Codex/ChatGPT(自带 ~/.codex/pets 宠物图)"
    fi
  else
    bad "没找到宠物图,且未装 Node.js 20+(petdex 需要)"
    say "    方式一:装 Node.js 20+ 后运行  npx petdex@latest install boba (从 https://petdex.dev 拉宠物)"
    say "    方式二:装 Codex/ChatGPT(自带 ~/.codex/pets 宠物图)"
    say "    方式三:手动放宠物图到 ~/.petdex/pets/<名字>/spritesheet.webp"
  fi
fi

say ""
if [ "$warn" -eq 0 ]; then
  say "依赖齐全 ✓"
else
  say "有 $warn 项缺失(见上)。装好缺失项后再继续。"
fi

# ── 自动安装步骤 ─────────────────────────────────────────────────────────────

CLAUDE_DIR="$HOME/.claude"
SETTINGS="$CLAUDE_DIR/settings.json"

say ""
say "════════════ 自动安装 ════════════"

# 5. 注册 marketplace + 启用插件(写入 ~/.claude/settings.json)
say ""
say "· 注册插件到 Claude Code settings..."
python3 - "$SETTINGS" "$PLUGIN_DIR" <<'PYEOF'
import sys, json, os

settings_path = sys.argv[1]
plugin_dir    = sys.argv[2]

# 读取现有 settings(若不存在则创建)
try:
    with open(settings_path) as f:
        cfg = json.load(f)
except Exception:
    cfg = {}

# 注册 marketplace
markets = cfg.setdefault("extraKnownMarketplaces", {})
markets["ccpet-marketplace"] = {
    "source": {"source": "directory", "path": plugin_dir}
}

# 启用插件
enabled = cfg.setdefault("enabledPlugins", {})
enabled["ccpet@ccpet-marketplace"] = True

os.makedirs(os.path.dirname(settings_path), exist_ok=True)
with open(settings_path, "w") as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)
    f.write("\n")

print("  ✓ settings.json 已更新(marketplace + enabledPlugins)")
PYEOF

# 6. 安装 /pet 用户级命令(~/.claude/commands/pet.md)
#    将插件命令里的 ${CLAUDE_PLUGIN_ROOT} 替换成本机实际路径,
#    使 /pet 命令无需前缀直接可用。
say ""
say "· 安装 /pet 用户命令到 ~/.claude/commands/pet.md ..."
COMMANDS_DIR="$CLAUDE_DIR/commands"
mkdir -p "$COMMANDS_DIR"
# 用 sed 将 ${CLAUDE_PLUGIN_ROOT} 替换为本机 PLUGIN_DIR
sed "s|\${CLAUDE_PLUGIN_ROOT}|$PLUGIN_DIR|g" \
    "$PLUGIN_DIR/commands/pet.md" \
    > "$COMMANDS_DIR/pet.md"
good "/pet 命令已安装(~/.claude/commands/pet.md)"
say "    重开会话后可直接用 /pet 代替 /ccpet:pet"

say ""
say "════════════ 安装完成 ════════════"
say "【重开一个 Claude Code 会话】即可使用。常用命令:"
say "  /pet            列出可用宠物"
say "  /pet use <名字>  换宠物"
say "  /pet grant      授权 iTerm 精确聚焦(可选)"
say "  /pet quit       关闭桌宠(不自动重开,/pet on 恢复)"
say ""
say "可写状态存在 ~/.ccpet/;卸载后 rm -rf ~/.ccpet && rm ~/.claude/commands/pet.md 清理干净。"

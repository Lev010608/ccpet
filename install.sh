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

# 4. 宠物图资源(~/.codex/pets)
PETS="$HOME/.codex/pets"
if [ -d "$PETS" ] && ls "$PETS"/*/spritesheet.webp >/dev/null 2>&1; then
  n=$(ls -d "$PETS"/*/spritesheet.webp 2>/dev/null | wc -l | tr -d ' ')
  good "宠物图: $n 个(来自 $PETS)"
else
  bad "没找到宠物图 —— 需装 ChatGPT/Codex(它会在 ~/.codex/pets/ 放宠物 spritesheet),或手动放入 ~/.codex/pets/<名字>/spritesheet.webp"
fi

say ""
if [ "$warn" -eq 0 ]; then
  say "依赖齐全 ✓"
else
  say "有 $warn 项缺失(见上)。装好缺失项后再继续。"
fi

say ""
say "════════════ 安装步骤 ════════════"
say "在 Claude Code 里依次运行:"
say ""
say "  /plugin marketplace add $PLUGIN_DIR"
say "  /plugin install ccpet@ccpet-marketplace"
say ""
say "装好后【重开一个会话】,桌宠会自动出现。常用命令:"
say "  /pet            列出可用宠物"
say "  /pet use <名字>  换宠物"
say "  /pet grant      授权 iTerm 精确聚焦(可选)"
say "  /pet quit       关闭桌宠(不自动重开,/pet on 恢复)"
say ""
say "可写状态存在 ~/.ccpet/(config、编译的二进制、state);卸载后 rm -rf ~/.ccpet 清理干净。"

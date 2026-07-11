# ccpet — Claude Code 桌宠插件

一个**独立的** macOS 桌面宠物:在自己的透明悬浮窗里实时反映 Claude Code 的活动。不依赖 Codex/ChatGPT 运行(只借用它的宠物精灵图),为 Claude Code 全套适配。

## 它能做什么

- **实时活动**:你提问 → 思考(🧠 转圈);调工具 → 运行中(🏃 转圈);完成 → 卡片显示 AI 回复;需要你批准/回答问题/审批计划 → 醒目棕色卡片(⚠️/❓/📋)。
- **点击卡片跳回会话**:终端(iTerm 精确聚焦原 tab)、Cursor、VS Code、Claude Desktop(Cowork)四端都能跳回正确的活动会话。
- **卡片交互**:悬停出 ✕ 关闭(智能重现);多会话堆叠;拖拽移动;右下角手柄平滑缩放。
- **生命周期自动化**:任意端开会话 → 桌宠自动出现(用上次记住的宠物);所有 Claude Code 会话关闭 → 桌宠自动退出。
- **换宠物**:`/pet use <名字>`,从你的 `~/.codex/pets/` 宠物库里选(27+ 个)。

## 前置依赖

| 依赖 | 用途 | 怎么装 |
|---|---|---|
| **macOS** | 桌宠是原生 AppKit 窗口 | — |
| **swiftc**(Xcode Command Line Tools) | 首次运行编译桌宠 | `xcode-select --install` |
| **python3** | hook 脚本 + 控制脚本 | macOS 自带 `/usr/bin/python3` |
| **宠物精灵图**(`~/.codex/pets/`) | 桌宠的形象资源 | 装 [ChatGPT/Codex](https://openai.com/chatgpt/) 会自动放入;或手动放 `~/.codex/pets/<名字>/spritesheet.webp` |

> ⚠️ **宠物图是硬依赖**:没有 `~/.codex/pets/` 里的 spritesheet,桌宠没有形象。装了 ChatGPT 桌面版即可(它自带一批宠物图)。

## 安装

```bash
# 1. 检查依赖(可选,会打印缺什么)
bash install.sh

# 2. 在 Claude Code 里注册并安装插件
#    /plugin marketplace add /path/to/ccpet-plugin
#    /plugin install ccpet@ccpet-marketplace

# 3. 重开一个会话 —— 桌宠自动出现
```

## 命令

| 命令 | 作用 |
|---|---|
| `/pet` | 列出可用宠物 |
| `/pet use <名字>` | 换宠物(模糊匹配,记住选择) |
| `/pet on` | 唤醒/显示桌宠 |
| `/pet off` | 临时隐藏(会自动重现) |
| `/pet quit` | 完全关闭(**不**自动重开,直到 `/pet on`) |
| `/pet status` | 查看当前宠物 + 运行状态 |
| `/pet grant` | 授权 iTerm 精确聚焦(macOS 自动化权限,可选) |

## 文件位置

- **插件本体**(只读):安装在 Claude Code 的插件缓存目录里。
- **可写状态**(`~/.ccpet/`):`config.json`(宠物选择等)、编译出的 `ccpet` 二进制、`state/`(会话状态)。卸载插件后 `rm -rf ~/.ccpet` 即可彻底清理。

## 已知限制

- **仅 macOS**。
- **Terminal.app 点击跳转会开新 tab**(无法定位原窗口,平台限制);iTerm2 授权后可精确聚焦原 tab。
- **卡片上的"回复/批准"按钮不能真正远程操作** Claude Code(桌宠是独立进程,无公开 IPC),只能聚焦回会话让你自己操作。
- **Desktop 点击跳转依赖 CCD 私有 deeplink**,CCD 大版本更新可能失效。

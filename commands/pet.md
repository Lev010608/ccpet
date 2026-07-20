---
description: 控制 Claude Code 桌宠(列出/切换/开关宠物、授权终端跳转)
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ccpet-ctl.py":*)
hide-from-slash-command-tool: "true"
---

运行桌宠控制命令并把结果原样展示给用户。

用户输入的参数是:`$ARGUMENTS`

执行:

!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ccpet-ctl.py" $ARGUMENTS`

说明(供你理解,不用复述):
- 无参数或 `list` → 列出所有可用宠物(来自 ~/.codex/pets 个人库)
- `use <名字>` 或直接 `<名字>` → 切换宠物(支持模糊匹配)
- `on` → 唤醒/显示桌宠  `off` → 隐藏桌宠  `quit` → 完全关闭(不自动重开)
- `status` → 查看当前宠物和运行状态
- `grant` → 授权 iTerm 精确聚焦(macOS 自动化权限)

把命令输出直接展示给用户即可。

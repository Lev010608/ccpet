# 桌宠 "Stopped" 误标修复 — 混合探活设计

日期: 2026-07-13
状态: 待实现

## Context(为什么做这个)

桌宠把 running/thinking 状态的会话在 90 秒无事件后标成 "Stopped"(灰色 ⛔),对应 `ccpet.swift` 的 `sweepStaleRunning()`(`staleRunningSec = 90.0`)。用户反映在 **Desktop 端经常误标 Stopped**,其他端似乎没有。

**排查结论(带调试日志证据,已确认):**
- **不是 Desktop 漏发事件**:bridge 的 hook 处理完全不分端,`handle_pre_tool_use`/`handle_post_tool_use` 无任何端特定分支。
- **不是端特定问题**:统计各会话相邻保活事件的最大间隔,desktop 会话有 772s / 1181s 的间隔,cursor-native 会话也有 3547s 的间隔——**所有端都会中招**。
- **根因 = 探活信号太弱**:`running` 保活事件**只在调用工具时发**(PreToolUse/PostToolUse)。两次工具之间——模型纯生成长回复、长时间思考、或跑一个很慢的单一工具(大 build/test)——**根本不发任何事件**。Claude Code 的 8 个 hook 都是离散生命周期点,**没有生成期间的周期性心跳**。Desktop 感觉最严重,是因为那边多是长回复少工具的对话且用户盯着卡片看。
- **改超时是治标**:长 build / 长思考仍可能超过任何固定阈值。

**目标**:用更能反映"turn 确实在推进"的信号替代/兜底纯计时,让各端都不再误标 Stopped,同时真被中断(Esc)时仍能正确转 Stopped。

## 方案:多信号"任一为活即保活"

`sweepStaleRunning()` 判断一个 running/thinking 会话是否"真停了",改为综合三个信号,**任一表明活跃就不标 Stopped**:

1. **transcript mtime(主信号,全端通用)**:该会话的 transcript 文件(`~/.claude/projects/<slug>/<sid>.jsonl`)在最近 `staleRunningSec` 秒内被写过 → turn 在推进(每条 assistant 消息 / 工具结果 / thinking 块都会 append)。这是最能反映"生成/思考中"的信号,且不依赖工具调用频率或进程模型。
2. **进程存活(Desktop / 终端兜底)**:`ps` 里存在带 `--resume <sid>` 的 claude 进程 → 该会话在跑。仅对 turn 级进程的端(Desktop/终端)有意义;编辑器 claude 进程常驻,靠信号 1。
3. **放宽计时兜底**:以上都拿不到时,用放宽后的超时(`staleRunningSec` 从 90s 提到 **300s**),避免边缘情况(transcript 路径缺失 + 无法关联进程)永远卡运行态。

**判定伪代码**(在 `sweepStaleRunning` 内,对每个 active 会话):
```
if now - lastEventTs <= staleRunningSec: 保活   // 原有:近期有事件
elif transcriptMtime(sid) 存在 且 now - transcriptMtime <= staleRunningSec: 保活
elif hasLiveResumeProcess(sid): 保活
else: 标 canceled/"Stopped"
```
真被 Esc 中断时:无新事件 + transcript 停止写 + (Desktop)进程退出 → 三信号全灭 → 正确标 Stopped(约 `staleRunningSec` 后)。

## 组件与数据流

### 1. bridge 传 transcript 路径(codex-bridge.py)
- `native_emit(...)` 新增可选参 `transcript_path`;非空时写入 `msg["transcript_path"]`。
- `native_handle` 里的 `emit()` 闭包用 `_transcript_path(data)`(已存在,读 `data["transcript_path"]`,Claude Code 每个 hook 都提供)取路径并传入。
- 影响面:仅新增一个消息字段,向后兼容(daemon 没有该字段时行为不变)。

### 2. daemon 存 transcript 路径 + 用于探活(ccpet.swift)
- 新增 `private var sessionTranscript: [String: String] = [:]`。
- `applyState`(state 消息处理,~line 1096-1127)解析 `obj["transcript_path"]`,非空则存入 `sessionTranscript[session]`。`endSession` 里一并清理。
- `staleRunningSec` 90.0 → 300.0。
- 改 `sweepStaleRunning()`:active 会话在 `now - ts > staleRunningSec` 后,再检查两个兜底信号,任一为活则**刷新 `sessionLastTs[sid] = now` 并跳过**(不标 canceled):
  - `transcriptFresh(sid)`:`FileManager` 取 `sessionTranscript[sid]` 的 mtime,`now - mtime <= staleRunningSec` 为真。
  - `hasLiveResumeProcess(sid)`:跑 `/bin/ps -axo command=`,判断是否有行同时含 `--resume` 和该 `sid`(复用现有 `liveClaudeCodeCount` 的 ps 调用模式;可合并为一次 ps 输出做两件事以省开销)。
- 开销:每 15s sweep 时,对少量 active 会话各 stat 一个文件 + 至多一次 ps。可忽略。

### 3. 边界处理
- transcript 路径缺失(老会话、字段没传)→ 信号 1 跳过,靠信号 2/3。
- ps 出错 → 信号 2 视为"不确定",不据此保活也不据此判死(交给信号 1/3)。
- 已 canceled 的会话不再参与(只 sweep active 态)。

## 不做(YAGNI)
- 不加新 hook / 不改 Claude Code。
- 不做 per-token 心跳(transcript 是消息级,足够)。
- 不改看门狗(`checkClaudeCodeAlive`,那是另一套"全关退出"逻辑)。

## 验证
1. **单测(bridge)**:`_transcript_path` 已有;确认 `native_emit` 带 `transcript_path` 时消息含该字段。
2. **单测(daemon 逻辑)**:构造一个 active 会话,`sessionLastTs` 设为 200s 前,但其 transcript 文件刚 touch → sweep 后应保活(不 canceled);transcript 也 300s 没动且无 resume 进程 → 应标 Stopped。用隔离 TMPDIR daemon + debug_status 验证。
3. **真机(用户)**:开着调试日志,在 Desktop 跑一个长回复 / 长 build 的 turn(>90s 无工具),确认卡片**不再**变 Stopped;然后 Esc 中断一个 turn,确认约 300s 后正确转 Stopped。
4. **回归**:SIGPIPE 存活、幽灵卡片、看门狗、新旧渠道跳转、Terminal 聚焦均不受影响(改动只在 sweep + 一个新消息字段)。

## 涉及文件
- `scripts/codex-bridge.py`:`native_emit`(~790)、`native_handle` 的 `emit`(~1360)。
- `scripts/ccpet.swift`:`sessionTranscript` 新字段、`applyState`(~1096)、`endSession`、`staleRunningSec`(~848)、`sweepStaleRunning`(~1327)。
- 编译 → `~/.ccpet/ccpet`;bridge 无需编译。

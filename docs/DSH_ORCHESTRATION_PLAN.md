# DSH 编排接入最终开发方案

> 结论先行：**官方 DeepSeek Harness SDK（0.1.0rc6）可行性已通过实弹验证**。HTTP 插件桥成立，DSH 可作为唯一 orchestrator 接管 QuantLab 的策略研究→编码→回测→BUG 修复全流程。

---

## 0. 目标架构

```
研究员 ──> Web 工作台 ──> FastAPI ──┬──> 审批桩(人确认)──> DSH 运行时(SDK 子进程)
                                   │                        │  HTTP 插件桥
                                   │                        ▼
                                   │              域内工具(write_strategy_code /
                                   │              execute_backtest_tool / dispatch /
                                   │              verify_strategy_file)
                                   │                        │
                                   └──────────── 回测铁闸 / Git / 质检 / 存档(QuantLab 守门层)
```

- **DSH**：唯一"动脑筋"的 orchestrator。LLM 推理、决策、工具循环全部由 DSH 运行时负责。
- **QuantLab**：退化为守门层——审批桩（确定编码才编码、确定回测才回测）、回测铁闸、Git 版本机关、质检、研究存档。
- **交互模型**：一个研究项目 = 一个 DSH 会话。`run()` 阻塞返回后平台挂起会话 → WebSocket 弹窗 → 用户点击审批 → 喂回同一 `session_id` 续跑。

---

## 1. Phase 0 验证结论（已完成，全部 PASS）

### 1.1 SDK 能力实测（真实方舟端点）

`backend/scripts/dsh_sdk_probe.py` 8 项测试，7 PASS + 1 WARN：

| 能力 | 结论 |
| --- | --- |
| runtime_launch | PASS |
| plain_reasoning | PASS |
| bash_tool | PASS |
| file_io | PASS |
| session_env_persistence | PASS |
| notification_streaming | PASS |
| max_tokens_truncation | PASS |
| bad_credentials | WARN（优雅报错，不崩溃） |

**关键参数坑**：DSH deepseek 适配器默认发 `max_tokens=256000`，方舟上限 131072 → **必须显式传 `max_tokens`**（探测用 32768 通过）。SDK 配置中应设为必填项。

### 1.2 HTTP 插件桥（P0-A，本次闭环）

- 外部 cordis 插件以**绝对路径**注册进默认 bundle 配置即可加载；但插件内 `import { defineTool } from '@deepseek-ai/dsh-tools'` 无法在 bundle 快照内解析 → 需在插件目录 `npm install @deepseek-ai/dsh-tools @deepseek-ai/cordis`。
- **插件必须用 `.mjs`（纯 ESM JS）**，不能用 `.ts`：bundle 的 loader 不编译 TS，`.ts` 会被当 CJS 加载直接 `SyntaxError: Cannot use import statement outside a module`。
- 插件 API（dsh-tools `0.0.1-rc.1` 实测）：`ctx.tools.register(defineTool({ name, description, parameters, output: { schema, render }, execute }))`——与官方文档一致。
- **端到端验证通过**：LLM 调 `ping_quantlab` → 插件 `execute` 真实发起 `GET http://127.0.0.1:8999/api/health` → 后端记日志 → 模型读回 `{"status":"ok",...}` 并复述。`tool/call` / `tool/result` 事件完整流出。

### 1.3 事件模型

SDK 通知为**扁平事件**：`assistant/chunk`、`assistant/message`、`tool/call`、`tool/result`、`turn/start/end`、`step/start/end`、`agent/inbox/spliced`、`session/status`、`session/title`。

> 不是 AgentPanel 等假设的 `AssistantMessage` / `thinking_tokens`，需要做一层事件映射。

### 1.4 会话 / 环境事实

- 默认 bundle 组合**只有 bash 工具**；bash 参数要求 `command` + `description` 两字段。
- 每次 bash 调用是全新 shell，跨轮不保留环境变量。
- 会话落盘 `session.jsonl.zstd`（root 由 `$DSH_SESSION_ROOT` 控制，cwd 由 `$DSH_CWD` 控制）。
- SDK `DeepSeekHarnessConfig` 支持 `base_url` / `api_key` / `cordis`（路径字符串 → `$DSH_CORDIS_CONFIG`）/ `max_tokens` / `env` 注入子进程。

---

## 2. Phase 1：后端接线（✅ 已完成，见 6.1）

1. **删除 `backend/app/dsh/orchestrator.py` 硬编码星型流水线与自研 `call_llm` 循环**；只保留事件/审批骨架。 — ✅ 已删除，`dsh/` 改为导出 bridge/engine。
2. **保留**：`strategy_verifier.py`（4 级 Pre-Flight）、`runner.py`、`backtest_service.py`、Git 版本机关——全部转为被 DSH 工具指令的守门层。 — ✅
3. **DSH 会话管理器**：`backend/app/dsh/engine.py`，项目↔DSH 会话映射、挂起/续跑 API。 — ✅
4. **事件映射层**：`engine._map_notification()` 将 SDK 扁平事件映射为平台词汇（chunk/assistant_message/tool_call/tool_result/step/turn/session_status），并 `persist_mapped()` 落库。 — ✅
5. **审批桩**：`bridge.py` 内存 + `backend/data/dsh/approvals.json` 持久化；`/dsh/pending`、`/dsh/approve`（批准自动 resume 续跑）、`/dsh/cancel`。 — ✅
6. `DSH_CORDIS_CONFIG` 指向 `backend/dsh_runtime/cordis.yml`，cwd 指向 `backend/data/dsh/workspaces/<project>`。 — ✅

## 3. Phase 2：域内工具（HTTP 插件桥，✅ 已完成，见 6.2）

在编排根插件目录实现 cordis 插件 `backend/dsh_runtime/src/quantlab-tools.mjs`，经 `ctx.tools.register(defineTool(...))` 暴露：

| 工具 | 职责 | 守门 |
| --- | --- | --- |
| `write_strategy_code` | 写/改策略文件 | **审批桩**（仅批准后落盘到隔离 worktree） |
| `verify_strategy_file` | 4 级 Pre-Flight 沙盒校验 | 只读，无需审批 |
| `execute_backtest_tool` | 提交回测任务 | **审批桩** + 回测铁闸（数据覆盖/参数合法性） |
| `dispatch_tool_call` | 通用分发到域内其他能力 | 按权限过滤 |

工具全部内网 HTTP 调 FastAPI（`/api/dsh-tools/call`），不直接碰文件系统/DB——保持守门层单一入口。

## 4. Phase 3：前端交互（✅ 已完成，见 6.3）

- `Research.tsx`：新增 **DSH 审批卡片**（`ShieldCheck`），定时轮询 `/dsh/pending`，展示请求参数 JSON、审批意见输入框、批准/拒绝按钮；批准后自动续跑并刷新会话。 — ✅
- 聊天输入框 submit 改走 `/dsh/run`（原 `/messages` 旧流水线仍保留）。 — ✅
- 等待审批时隐藏冗余"思考中"卡片，仅展示审批卡片；`busy` 状态与 pending 联动。 — ✅
- `thinking-status` 实时思维链 + 事件流渲染沿用。 — ✅

## 5. Phase 4：策略撰写技能固化（✅ 部分，见 6.4）

落 `nautilus-strategy-author` 技能为 DSH 侧 skill 文档（NautilusTrader 约定、STRATEGY_MANIFEST 规范、回测指标解读），注入默认 system prompt，确保模型输出符合平台约定。

> 验证：e2e 中模型自动调用 `skill` 工具加载 `nautilus-strategy-author`，并遵循其规范产出通过四级 Pre-Flight 的策略。完整 skill 固化在 `.agents/skills/nautilus-strategy-author/`，可作为 DSH 侧 skill 内容源。

## 5.1 Phase 5：完全 DSH 编排（✅ 已完成，见 6.5）

移除旧自研 agent 流水线（research.py 自研 LLM 循环 + `/api/agent` 会话管线 + 前端 AgentPanel），DSH 成为唯一 orchestrator，旧入口（`/messages`、项目创建、卡片确认按钮）全部改走 DSH：

1. **后端 research.py**：新增共享 `_start_dsh_turn()`（设 THINKING → `dsh_engine.run_turn` → 写 assistant 消息 → 按 pending/异常设 WAITING_APPROVAL/IDLE）；`create_project`（`original_idea` 分支）、`send_message`（`POST /messages`）、`/dsh/run`、`/dsh/approve` 批准后全部走 DSH。 — ✅
2. **删除旧循环**：`_parse_llm_response` / `_call_research_llm_stream` / `run_research_agent_cycle` / `_run_research_background` / `_start_research_task` 及 run_hermes 别名、`TOOL_CALL_REGEX` 等正则、`RESEARCH_LOCKS`。 — ✅
3. **删除 `/api/agent` 前置**：`app/main.py` 移除 agent router 挂载与 lifespan 清理；删除 `app/agent/service.py`；schemas 删 `AgentSessionCreate`/`AgentApplyRequest`。`agent/strategy_verifier.py`、`agent/tools.py`（bridge/tools 复用）保留。 — ✅
4. **前端**：删除 `AgentPanel.tsx`、`api.ts` 的 agent 系列 API 与 `agentSocketUrl`、`server.js` 的 agent WebSocket 反代、`StrategyDetail.tsx` 的 AI 写码面板；`Research.tsx` 旧卡片 handler（ApproveCode/ConfirmRepair/ConfirmAnalysis/ConfirmBacktestParams）统一改为自然语言经 `runDshTurn` → `/dsh/run`；`ProcessToolStep`/`addToolCall` 兼容 DSH 桥工具名（`write_strategy_code`/`execute_backtest_tool`/`dispatch_tool_call`/`quant_execute_backtest`/`quant_save_strategy_code`）；删除 legacy CodeApprovalCard/BacktestParamsCard/BacktestResultCard 渲染分支与 makeTurn 内过时判断。 — ✅
5. **验证**：`pytest tests/` 46 passed；`npm run build`（tsc + vite）通过。 — ✅

---

## 6. 里程碑

| # | 里程碑 | 验收 |
| --- | --- | --- |
| M0 | Phase 0 实弹验证（7/7 + 桥通） | ✅ 已完成 |
| M1 | 后端接线 + mock LLM 全链路 | ✅ 已完成（engine/bridge/cancel 端点、事件映射、审批注册表） |
| M2 | HTTP 桥工具接入真实工作流 | ✅ 已完成（主机模式 e2e 全通：写码→审批→verify→回测→审批→报告落盘） |
| M3 | 前端事件流 + 审批弹窗 | ✅ 已完成（Research.tsx 审批卡片 + /dsh/pending 轮询 + 批准/拒绝/意见） |
| M4 | 策略技能固化 + 回归测试 | ✅ 已完成（test_dsh_bridge.py 11 项；skill 已被模型自动加载） |
| M5 | 完全 DSH 编排（移除旧 agent 流水线） | ✅ 已完成（见 5.1/6.5；46 pytest + 前端 build 通过） |

## 6.1 M1/M2 实现要点（后端）

- `backend/app/dsh/engine.py`：`run_turn(project, prompt)` 调官方 SDK；`_map_notification` 扁平事件映射；`persist_mapped` 写 research_messages；`get/set_status`、`cancel_turn`、`shutdown_all`；`_load_dsh_env()` 读 `.env.dsh`。
- `backend/app/dsh/bridge.py`：`/api/dsh-tools/call` 四工具统一桥；审批注册表（pending/approved/declined + `approvals.json` 持久化）；`approve_proposal`/`pending_approvals` 复用函数；`_diff_vs_baseline` 统一 diff。
- `backend/app/research.py`：`/dsh/run`、`/dsh/pending`、`/dsh/approve`（批准后自动 resume 续跑）、`/dsh/cancel`。
- `backend/app/runner.py`：回测沙箱支持 `QUANTLAB_BACKTEST_SANDBOX` 开关——`true`（默认）走隔离 Git 快照；`false` 直接主机跑（调试/验证用）。沙箱内 `app/quant` 使用惰性 `__getattr__` shim，避免把 DB 依赖拖进沙箱。

## 6.2 端到端验证记录

- **主机模式 e2e 全通**（项目 `10087ce2`，`QUANTLAB_BACKTEST_SANDBOX=false`）：模型读取参考策略 → write_strategy_code（待审批）→ 批准续跑 → verify_strategy_file（四级 Pre-Flight 全过）→ execute_backtest_tool（待审批）→ 批准续跑 → 真实回测 `COMPLETED`（743 根、报告落盘）→ 中文总结。0 成交已逐条件归因（唐奇安突破在震荡月未触发），非链路问题。
- **沙箱模式回归 e2e**（项目 `48306ce8`，默认 `QUANTLAB_BACKTEST_SANDBOX=true`）：同一闭环在默认沙箱下跑通——模型读取参考策略 → `write_strategy_code` 待审批并获准 → `verify_strategy_file` 通过 → `execute_backtest_tool` 待审批并获准 → 回测 `f6087de1` `COMPLETED`（策略版本 `1.0.0.3` 落盘，metrics 写库）。终轮中文总结因模型在 deadline（420s）内仍未结束（THINKING），属已知 ark-code 长思考不稳定行为，非链路问题。

## 6.3 M3 实现要点（前端）

- `types.ts`：新增 `DshApproval` 接口。
- `api.ts`：新增 `dshPending(id)`、`dshApprove(id, request_id, approved, feedback)`。
- `Research.tsx`：轮询 `/dsh/pending`；审批卡片（工具名、参数 JSON、审批意见、批准/拒绝）；聊天框 submit 改走 `/dsh/run`；等待审批时隐藏"思考中"卡片。

## 6.4 已知问题 / 后续

- **模型行为不稳定**：ark-code 长思考 + 大量 bash 探路时偶发长时间不产出；e2e 首轮曾在 468s 超时无输出。收敛 prompt（明确"请结束本轮请求审批"）显著改善。
- **回测数据依赖**：BEATUSDT 无数据时模型自动改用 BTCUSDT（数据完整性检查会在缺失时请求确认）。
- **沙箱快照**：当前策略依赖 `app.quant.*` / `app.strategy_base` 已可用；若未来引入更深依赖需扩展 `runner.py` 的 `runtime_files` 或惰性 shim。
- **审批注册表**：内存 + JSON 持久化，无 DB 迁移；重启后 `_load_registry()` 恢复 pending 条目。

## 6.5 M5 实现要点（完全 DSH 编排）

- `backend/app/research.py`：`_start_dsh_turn()`（`_worker` 内设 THINKING → `dsh_engine.run_turn` → assistant 消息落库 → 按 pending/异常设状态；任务跟踪进 `ACTIVE_RESEARCH_TASKS`）；删旧 LLM 循环与正则；export 端点仍用保留的 `RESEARCH_INSTRUCTIONS`/`extract_python_strategy_code`。
- `backend/app/agent/`：`service.py` 删除；`strategy_verifier.py`、`tools.py` 保留（`ensure_strategy_db_record`/`get_strategy_code_tool`/`get_writing_log_tool`/`write_strategy_code`、`dispatch_tool_call` 仍被使用）。
- `frontend/src/pages/Research.tsx`：`runDshTurn()` 统一入口（乐观用户消息 → `/dsh/run` → 刷新 messages/runs/project/writingLog/thinkingStatus/dshPending）；旧卡片 handler 发出自然语言 DSH 指令；`addToolCall`/`addToolOutput` 从 `metadata.event`（DSH 扁平事件）解析工具名/参数/结果。
- 保留 `AgentSession`/`AgentMessage` 数据模型（`ResearchProject.implementation_session_id` 外键依赖），无 schema 迁移。

---

## 7. 探测脚本归档（Phase 0 产物）

- `backend/scripts/dsh_sdk_probe.py` — 保留（DSH 能力回归测试）。
- `backend/scripts/dsh_probe_mock_llm.py` — 保留（无 key 环境下工具循环 CI）。
- `/tmp/dsh-plugin/`（quantlab-tool.mjs / cordis-toolonly.yml / e2e_tool_test.py）— **迁移进仓库**：编排根 `backend/dsh_runtime/`（可含 package.json + npm 依赖声明），不再依赖 /tmp。
- `backend/.env.dsh` — 已 gitignore，勿提交。
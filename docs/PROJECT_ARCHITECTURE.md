# QuantLab 项目架构与业务流程说明

> 文档基线：当前工作区代码（2026-08-15）  
> 适用对象：产品、量化研究、前端、后端、策略开发、测试与运维人员

## 1. 项目概述

QuantLab 是一个以 NautilusTrader 为执行内核的量化策略研究与回测管理平台。它把策略想法研讨、规格确认、AI 辅助实现、代码审查、Git 版本发布、历史数据准备、隔离回测、结果分析和研究归档串联成可追溯的研究闭环。

平台的核心设计目标如下：

- **研究过程可追踪**：研究对话、策略规格、实现会话、回测和结论都关联到研究项目。
- **策略接口标准化**：策略通过 `STRATEGY_MANIFEST` 描述参数、周期、运行模式和图表指标。
- **回测可复现**：回测锁定策略版本的 Git commit，并从该提交导出独立源码快照。
- **执行相互隔离**：每个回测在独立 Python 子进程中运行；AI 改码在独立 Git worktree 中进行。
- **数据不造假**：回测只读取 Nautilus `ParquetDataCatalog` 中已有的 Instrument 和 Bar，并在执行前校验覆盖范围。
- **结果可审计**：数据库保存任务元数据和摘要，文件系统保存输入、日志、报告、行情、指标和完整结果。

当前版本是面向单机或可信内网的研究平台。它已有完整的核心研究链路，但尚未实现用户账户/租户鉴权、分布式任务队列和参数优化功能。

## 2. 系统边界

### 2.1 系统提供的能力

1. 策略文件创建、在线编辑、语法校验与元数据维护。
2. 基于 Git commit 的策略发布和业务版本管理。
3. Binance 现货及 U 本位永续 K 线下载，并写入 Nautilus Catalog。
4. 单标的策略与组合策略的真实 NautilusTrader 回测。
5. 回测进度、取消、失败诊断、绩效指标和交互图表展示。
6. 基于 DeepSeek Harness (DSH) 进行全流程策略研讨、逻辑设计、Pre-Flight 沙盒验证、回测与归因分析。
7. 通过策略 Agent 在隔离工作区中实现或修复策略。
8. LLM、Git 远端与运行参数配置。

### 2.2 外部依赖

| 外部系统 | 用途 | 交互方式 |
| --- | --- | --- |
| PostgreSQL 16 | 持久化业务实体、状态、配置和 Agent 消息 | SQLAlchemy async + asyncpg |
| Redis 7 | 已纳入部署与配置，但当前业务代码尚未实际使用 | 预留 |
| Binance Vision / Exchange API | 获取交易标的元数据与历史 K 线归档 | HTTPS |
| NautilusTrader | Catalog、回测引擎、订单撮合、账户与绩效报告 | Python SDK |
| OpenAI 兼容 LLM 服务 | DeepSeek / DSH 量化研究与策略写码服务 | HTTP `/v1/chat/completions` API |
| Git 远端（可选） | 推送策略专用仓库 | Git + AskPass |

## 3. 总体架构

```mermaid
flowchart LR
    U["研究员 / 策略开发者"] --> FE["React + Vite Web 工作台"]
    FE -->|"REST / JSON"| API["FastAPI 应用"]
    FE <-->|"WebSocket"| AG["Agent 会话服务"]

    API --> PG[("PostgreSQL")]
    API --> FS[("本地文件系统")]
    API --> GR[("策略专用 Git 仓库")]
    API --> DSH["DeepSeek Harness 核心引擎"]
    AG --> DSH
    AG --> WT[("隔离 Git worktree")]

    API --> DL["Binance 数据下载器"]
    DL --> BV["Binance Vision / Exchange API"]
    DL --> CAT[("Nautilus ParquetDataCatalog")]

    API --> RUN["异步回测编排器"]
    RUN --> SNAP[("Git commit 源码快照")]
    RUN --> WK["独立 Python Worker"]
    WK --> NT["NautilusTrader BacktestNode"]
    NT --> CAT
    WK --> ART[("回测产物目录")]
    RUN --> PG
```

### 3.1 分层职责

| 层次 | 主要目录/模块 | 职责 |
| --- | --- | --- |
| 表现层 | `frontend/src` | 页面路由、表单、状态轮询、图表、Agent 实时会话 |
| API/应用层 | `backend/app/main.py`、各 Router | 参数校验、业务规则、状态流转、资源编排 |
| 领域层 | `models.py`、`strategy_contract.py`、`research.py`、`dsh/*` | 策略版本、研究生命周期、回测任务、DSH 编排与契约规则 |
| 执行层 | `runner.py`、`backtests/*`、`strategy_verifier.py` | Git 快照导出、进程隔离、Pre-Flight 沙盒、结果采集 |
| 集成层 | `data_downloads.py`、`agent/service.py`、`llm_config.py`、`git_config.py` | Binance、DeepSeek / DSH、Git 远端集成 |
| 持久化层 | PostgreSQL、Catalog、回测目录、策略 Git 仓库 | 结构化业务数据、行情、代码版本及大体积报告 |

## 4. 技术架构

### 4.1 前端

- React + TypeScript，Vite 负责开发和构建。
- React Router 管理客户端路由。
- Lightweight Charts 展示 K 线和策略指标。
- Recharts 展示权益、回撤、收益分布等统计图。
- React Markdown + GFM 展示研究和 Agent 文本。
- Lucide React 提供图标。
- REST 请求集中封装在 `frontend/src/api.ts`；Agent 流式交互使用 WebSocket。

主要页面：

| 路由 | 页面 | 说明 |
| --- | --- | --- |
| `/` | Dashboard | 平台总览 |
| `/research` | Research | 研究项目与完整研究闭环 |
| `/strategies` | Strategies | 策略文件和已发布策略列表 |
| `/strategies/:name` | StrategyDetail | 代码、设置、版本、回测、Agent |
| `/backtests` | Backtests | 回测任务管理 |
| `/backtests/new` | NewBacktest | 创建回测 |
| `/backtests/:id` | Result | 指标、图表和回测详情 |
| `/data` | DataDownloads | Binance 数据下载与 Catalog 写入 |
| `/settings` | SettingsPage | LLM 和 Git 配置 |
| `/optimize` | Placeholder | 参数优化尚未实现 |

前端没有引入全局状态框架，状态主要由页面组件持有，并通过 API 重新获取。刷新页面后可恢复的业务状态均来自后端；浏览器侧 `client_id` 用于区分研究和 Agent 会话，但它不是安全身份认证机制。

### 4.2 后端

- Python 3.12–3.14。
- FastAPI 提供 REST API、生命周期钩子和 WebSocket。
- Pydantic Settings 读取 `.env`，Pydantic 模型完成请求校验。
- SQLAlchemy 2 异步 ORM + asyncpg 访问 PostgreSQL。
- Alembic 管理数据库结构演进。
- asyncio 负责任务编排；回测本体使用独立子进程。
- pandas、NumPy、PyArrow 用于指标计算和 Parquet 报告。
- cryptography 用于加密 LLM/Git 凭据。

应用启动顺序：

1. `backend/start.sh` 执行 `alembic upgrade head`。Alembic 独占数据库结构管理，应用启动不再执行 `Base.metadata.create_all` 兜底——否则新增模型会在迁移之前被自动建表，导致 `alembic_version` 与实际结构错位。
2. Uvicorn 加载 FastAPI 应用。
3. lifespan 创建回测产物根目录。
4. 将服务重启前仍为 `QUEUED/RUNNING/ANALYZING` 的任务标记为失败。
5. 尝试注册内置示例策略。
6. 修复 Agent 会话工作区路径并清理过期 worktree。

### 4.3 数据与存储

系统采用四种存储，各自承担不同职责：

| 存储 | 保存内容 | 原因 |
| --- | --- | --- |
| PostgreSQL | 策略、版本、回测元数据、研究项目、规格、消息、配置 | 事务、查询与状态关联 |
| 策略专用 Git 仓库 | `backend/app/strategies/*.py` 的发布历史 | 精确锁定可执行代码 |
| Nautilus Catalog | Instrument 与 Bar Parquet 数据 | Nautilus 原生高效查询 |
| 回测/Agent 文件目录 | 源码快照、输入、日志、报告、worktree | 大文件、临时工作区和审计材料 |

默认路径均相对后端运行目录解析，因此生产环境建议全部配置为绝对路径。

## 5. 后端模块说明

### 5.1 API 入口与策略/回测管理

`backend/app/main.py` 负责：

- 应用生命周期与 CORS。
- 策略注册、查询、更新、删除。
- 策略版本发布与删除保护。
- 回测创建、查询、图表读取、取消与删除。
- 汇总挂载研究、Agent、数据、Git 和 LLM 子路由。

删除规则体现了数据完整性约束：运行中的回测不能删除；被回测引用的策略版本不能删除；策略的唯一版本不能单独删除。删除整个策略时，会先拒绝仍有活动任务的策略，再删除其回测记录和对应产物目录。

### 5.2 策略契约

每个策略模块必须导出：

- `STRATEGY_MANIFEST: StrategyManifest`
- `calculate_indicators(dataframe, parameters) -> DataFrame`
- Manifest 中声明的策略类与配置类

Manifest 包含：

| 字段 | 含义 |
| --- | --- |
| `slug/name/version` | 稳定标识、显示名称、业务版本 |
| `description/category` | 策略说明与分类 |
| `strategy_path/config_path` | Nautilus 可导入类路径 |
| `parameters` | Web 参数标题、类型、默认值和边界 |
| `timeframes/primary_timeframe` | 所需全部周期和主周期 |
| `plot_config` | 主图/副图指标序列配置 |
| `mode` | `SINGLE_INSTRUMENT` 或 `PORTFOLIO` |
| `supports_short/requires_funding` | 策略能力和数据需求声明 |

`SINGLE_INSTRUMENT` 为每个标的构造一个策略实例，注入 `instrument_id` 和主 `bar_type`。`PORTFOLIO` 只构造一个实例，一次注入全部 `instrument_ids` 和 `bar_types`；若配置类支持 `data_bar_types`，还会注入所有标的/周期组合。

加载 Manifest 时会校验绘图契约；创建回测时会补齐默认参数、转换类型并校验上下界。指标函数必须保持 DataFrame 行数不变，并生成 `plot_config` 引用的所有数值列。

### 5.3 策略文件与 Git 版本

策略存在两个概念层：

- **工作区策略文件**：`backend/app/strategies/*.py`，可编辑、可处于未提交草稿状态。
- **已发布策略版本**：数据库中的 `StrategyVersion`，绑定 Git commit、Manifest 哈希和契约快照。

策略文件 API 将访问严格限制在策略目录，并以正则限制文件名，防止路径穿越。保存时先执行 Python `compile` 语法检查。

发布流程：

```mermaid
sequenceDiagram
    actor User as 用户
    participant UI as 策略详情页
    participant API as FastAPI
    participant Work as 工作区策略文件
    participant Git as 策略专用 Git 仓库
    participant DB as PostgreSQL

    User->>UI: 编辑并保存策略
    UI->>API: PUT /strategy-files/{name}
    API->>API: Python 语法检查
    API->>Work: 写入文件
    User->>UI: 发布新版本
    UI->>API: POST /strategies/.../versions
    API->>API: 加载并校验 Manifest
    API->>Git: 同步单个策略文件并 commit
    API->>Git: 可选推送远端
    API->>DB: 保存业务版本、commit、ref、Manifest hash、契约快照
    DB-->>UI: 返回已发布版本
```

策略专用仓库默认为 `data/strategy-repository`。它与 QuantLab 主项目仓库分离，只管理策略源码，使策略发布不会被平台其他未提交修改阻塞。若 Manifest 版本号冲突，版本 API 会在当前最高合法 `x.y.z` 上自动增加 patch。

### 5.4 数据下载与 Catalog

数据模块支持 Binance `spot` 和 `um`，下载流程如下：

1. 从 Exchange Info 获取当前可交易标的及精度/过滤器信息。
2. 将请求日期按月切分，优先下载月度 ZIP；必要时回退到每日 ZIP。
3. 下载并校验 `.CHECKSUM`（存在时）。
4. 解析 CSV，裁剪到请求日期范围，并转换为 Nautilus `Bar`。
5. 根据 Binance tick size / step size 构造 `CurrencyPair` 或 `CryptoPerpetual` Instrument。
6. 写入 `ParquetDataCatalog`，维护下载清单以支持增量模式。

下载任务由进程内线程池执行，状态保存在内存字典中，最多保留 1000 条日志。服务重启后任务状态不会恢复；但已成功写入 Catalog 的数据仍保留。并发数由 `DATA_DOWNLOAD_CONCURRENCY` 控制，代码上限为 16。

### 5.5 回测执行

```mermaid
sequenceDiagram
    actor User as 用户
    participant API as FastAPI
    participant DB as PostgreSQL
    participant Runner as 回测编排器
    participant Git as 策略 Git 仓库
    participant Worker as 独立 Python Worker
    participant Catalog as ParquetDataCatalog
    participant NT as Nautilus BacktestNode
    participant Art as 产物目录

    User->>API: POST /api/backtests
    API->>API: 校验版本、参数、Manifest hash
    API->>DB: 创建 QUEUED 任务
    API-->>User: 立即返回任务 ID
    API->>Runner: asyncio 后台执行
    Runner->>DB: RUNNING / 10%
    Runner->>Art: 写 payload.json
    Runner->>Git: git archive 指定 commit
    Git->>Art: 导出 source/ 快照
    Runner->>Worker: 创建子进程
    Worker->>Catalog: 校验精确 BarType 和时间覆盖
    Worker->>NT: 构建并运行 BacktestNode
    NT-->>Worker: 结果、订单、成交、持仓、账户
    Worker->>Art: Parquet/JSON/日志/指标
    Worker-->>Runner: worker-result.json
    Runner->>DB: ANALYZING -> COMPLETED 或 FAILED
```

关键实现细节：

- 创建任务前，工作区 Manifest hash 必须等于版本记录的 hash，避免用户误把未发布代码用于旧版本。
- Runner 通过 `git archive` 导出锁定 commit；当前平台的 builder/worker/analytics/contract 会复制到快照中，保证策略代码固定，同时使用当前平台运行时协议。
- Catalog 路径在父进程中转为绝对路径，避免子进程工作目录变化导致解析错误。
- Worker 使用 Nautilus 内部精确文件选择逻辑校验每个 BarType 的请求区间，避免相同标的不同周期相互混淆。
- `execution_model=FAST` 会关闭 `bar_adaptive_high_low_ordering`；其他模式开启。当前三种名称并未配置三套独立撮合模型。
- Venue 固定使用 HEDGING、MARGIN、USDT 基础币种和 Maker/Taker Fee Model。
- 超过 `BACKTEST_TIMEOUT_SECONDS` 后杀死子进程并标记失败。
- API 的“取消”当前只更新数据库状态，未向已运行的子进程发送终止信号；这是需要特别注意的现有限制。

### 5.6 回测结果与图表

回测完成后，数据库保存适合列表和详情页快速读取的 `metrics` 与 `result`；完整报告存放在 `ARTIFACT_ROOT/<run-id>/`：

| 文件 | 内容 |
| --- | --- |
| `payload.json` | 冻结后的任务输入和策略 revision |
| `source/` | 指定 Git commit 导出的源码快照 |
| `backtest.log` | 子进程标准输出/错误 |
| `worker-result.json` | Worker 返回给父进程的结果 |
| `orders.parquet` 等 | 订单、成交、持仓、账户报告 |
| `bars.parquet` | 回测引擎缓存中策略实际看到的 Bar |
| `indicators.parquet` | 按策略契约计算的图表指标 |
| `equity.parquet`、`returns.parquet` | 权益、回撤和收益序列 |
| `backtest_result.json` | Nautilus 原生回测结果 |
| `analyzer_statistics.json` | 分析器统计 |
| `report_manifest.json` | 报告文件、行数和列清单 |
| `plot_config.json` | 策略指标绘图配置 |

图表 API 从产物目录加载，不查询实时行情，因此页面展示的是策略实际消费的数据。接口支持按标的、时间范围、周期和最大数量裁剪。

### 5.7 Agent 策略开发

Agent 服务为每个会话创建隔离 Git worktree，只复制/暴露策略开发所需上下文。前端通过 WebSocket 发送提示、切换权限模式、取消、压缩上下文和响应工具审批。

权限模式包括 `plan`、`default`、`acceptEdits` 和 `bypassPermissions`。会话及事件写入 PostgreSQL，活动 WebSocket、任务、SDK Client 和审批 Future 保存在进程内。

修改应用流程：

1. Agent 在 worktree 内编辑和验证策略。
2. 用户查看 diff 及增删行统计。
3. 用户拒绝则丢弃本次变更；接受则把策略文件复制回正式工作区。
4. 研究项目进入 `CODE_REVIEW`。
5. 接受修改不等于发布，仍需用户确认发布策略版本。

worktree 默认保留 7 天，启动时清理过期目录。服务重启会丢失实时连接与正在执行的 SDK 任务，但数据库消息仍可查询。

### 5.8 研究闭环 (DeepSeek Harness)

研究模块由 **DeepSeek Harness (DSH)** 原生架构驱动，通过星型拓扑多 Agent 协同与 4 级 Pre-Flight 运行期沙盒，实现策略研讨、编码、回测与归因分析的自动化闭环。

```mermaid
stateDiagram-v2
    [*] --> DISCUSSING: 创建研究项目
    DISCUSSING --> DISCUSSING: 策略构想与指标研讨
    DISCUSSING --> CODE_APPROVAL: 输出 Markdown 方案并由用户审批
    CODE_APPROVAL --> WRITING: 用户批准编码，启动 4 级 Pre-Flight 沙盒写码
    WRITING --> CODE_REVIEW: 4 级沙盒全部通过，保存策略代码
    CODE_REVIEW --> BACKTEST_PROPOSED: 用户要求回测，生成回测参数方案卡片
    BACKTEST_PROPOSED --> BACKTESTING: 用户确认参数并启动回测
    BACKTESTING --> RESULT_REVIEW: 回测执行成功并生成归因分析报告
    BACKTESTING --> CODE_REVIEW: 回测报错触发自动自愈修复
    RESULT_REVIEW --> ARCHIVED: 保存结论并归档
    RESULT_REVIEW --> DISCUSSING: 新一轮策略假设研讨
```

一次典型业务流程：

1. 用户创建研究项目并提交原始想法。
2. DSH Quant Lead 与用户多轮研讨，明确量化假设、标的周期、进出场信号、风控规则与回测区间。
3. DSH 在回复正文中输出完整的 Markdown 策略设计方案，并在末尾发起 `propose_code_approval` 交互式审批卡片。
4. 用户在界面上点击“批准方案”后，系统调用 `write_strategy_code` 工具，启动 4 级 Pre-Flight 运行期沙盒（AST 结构、参数契约、向量化指标计算、NautilusTrader 运行期生命周期）。
5. 沙盒验证通过后，策略代码自动持久化并注册到数据库 `Strategy` 与 `StrategyVersion`。
6. 当用户明确要求回测时，系统调用 `propose_backtest_params` 弹出交互式回测配置卡片。
7. 用户确认后调用 `execute_backtest`，后台通过独立 Worker 进程安全执行 NautilusTrader 回测。
8. 回测完成后，DSH 自动生成绩效指标与归因分析报告供用户审查。
9. 用户归档项目或进入下一轮策略迭代。

## 6. 数据模型

```mermaid
erDiagram
    STRATEGY ||--o{ STRATEGY_VERSION : has
    STRATEGY_VERSION ||--o{ BACKTEST_RUN : executes
    RESEARCH_PROJECT o|--o{ BACKTEST_RUN : contains
    RESEARCH_PROJECT o|--o{ RESEARCH_MESSAGE : discusses
    RESEARCH_PROJECT o|--o{ STRATEGY_SPECIFICATION : versions
    RESEARCH_PROJECT o|--o{ AGENT_SESSION : develops
    AGENT_SESSION ||--o{ AGENT_MESSAGE : records
    STRATEGY o|--o{ RESEARCH_PROJECT : publishes
```

主要实体：

| 实体 | 关键字段 | 说明 |
| --- | --- | --- |
| `Strategy` | slug、name、status | 策略业务主档 |
| `StrategyVersion` | version、entrypoint、git_commit、manifest_hash | 不可变发布快照 |
| `BacktestRun` | status、stage、config、metrics、result | 回测任务及摘要 |
| `ResearchProject` | status、conversation_id、关联策略/回测、结论 | 研究聚合根 |
| `ResearchMessage` | role、type、metadata | 研讨、决策、分析和迭代记录 |
| `StrategySpecification` | version、status、content | 可确认、可被替代的结构化规格 |
| `ResearchDecision` | question、options、recommendation、status、answer、origin | 研讨阶段待用户拍板的策略设计决策 |
| `AgentSession` | strategy、permission、workspace、status | 隔离开发会话 |
| `AgentMessage` | role、event_type、content | Agent 消息与工具事件 |
| `LlmConfiguration` | base_url、model、加密 key、权限默认值 | LLM 服务配置，单行记录 |
| `GitConfiguration` | remote、username、加密 password、auto_push | 策略远端配置，单行记录 |

业务状态存为数据库 Enum。`StrategyVersion -> BacktestRun`、研究关联等使用外键，但部分关系只声明字段、未配置 ORM relationship；服务层负责显式查询和状态联动。

## 7. API 分组

| 前缀 | 主要能力 |
| --- | --- |
| `/api/health` | 健康检查 |
| `/api/strategies` | 策略主档与版本管理 |
| `/api/strategy-files` | 策略文件、Git 状态和提交 |
| `/api/backtests` | 回测创建、查询、取消、删除与图表 |
| `/api/data` | 标的查询、下载任务创建与进度 |
| `/api/research` | 研究项目、消息、决策审批、规格、实现、发布、回测、分析、结论与迭代 |
| `/api/agent` | Agent 会话、消息、diff、应用/拒绝、WebSocket |
| `/api/settings/llm` | LLM 配置保存与连通性测试 |
| `/api/settings/git` | Git 远端配置保存与连通性测试 |

完整请求/响应模型以 FastAPI 生成的 `/docs` 和 `/openapi.json` 为准。

## 8. 配置说明

后端配置来自 `backend/.env`，字段名大小写不敏感。关键变量如下：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+asyncpg://quantlab:quantlab@localhost:5432/quantlab` | PostgreSQL 连接串 |
| `REDIS_URL` | `redis://localhost:6380/0` | Redis 预留连接串 |
| `CORS_ORIGINS` | `["http://localhost:5173"]` | 允许的前端来源 |
| `DATA_ROOT` | `../data` | 通用数据和 Agent 根目录 |
| `ARTIFACT_ROOT` | `../data/backtests` | 回测产物根目录 |
| `CATALOG_PATH` | `../catalog` | 默认 Nautilus Catalog |
| `BACKTEST_TIMEOUT_SECONDS` | `3600` | 单次回测超时 |
| `INSTRUMENT_ID_TEMPLATE` | `{symbol}-PERP.{venue}` | 简写标的转 InstrumentId 模板 |
| `STRATEGY_REPO_PATH` | 项目根目录 | Agent worktree 使用的主仓库 |
| `STRATEGY_GIT_REPO_PATH` | `data/strategy-repository` | 策略发布专用仓库 |
| `LLM_SECRET_ENCRYPTION_KEY` | 开发占位值 | LLM/Git 密钥加密根密钥，生产必须更换 |
| `AGENT_MAX_CONCURRENCY` | `5` | Agent 同时执行上限 |
| `AGENT_WORKSPACE_RETENTION_DAYS` | `7` | worktree 保留天数 |
| `DATA_DOWNLOAD_CONCURRENCY` | `8` | 数据归档并行下载数 |
| `VITE_API_URL` | `http://localhost:8000/api` | 前端 API 根地址 |

## 9. 本地开发与部署

### 9.1 启动

```bash
# 1. PostgreSQL 和 Redis
docker compose up -d

# 2. 后端
cd backend
cp .env.example .env
uv sync
./start.sh

# 3. 前端（另一个终端）
cd frontend
npm install
npm run dev
```

默认地址：前端 `http://localhost:5173`，后端 `http://localhost:8000`，Swagger `http://localhost:8000/docs`。

在创建回测前必须准备有效 Catalog。可通过数据管理页面生成，也可由外部程序写入；InstrumentId、BarType、周期和日期范围必须与回测配置一致。

### 9.2 测试与构建

```bash
cd backend
uv run pytest
uv run ruff check .

cd frontend
npm run build
npm run lint
```

后端测试覆盖策略契约、Schema、策略文件、Git 版本、数据下载、回测配置、图表、研究和 Agent 服务。涉及数据库、Git、Nautilus 或外部 API 的路径应继续优先采用临时目录和依赖替身，避免污染真实策略仓库与 Catalog。

### 9.3 生产化建议

当前仓库的 Compose 只启动 PostgreSQL 和 Redis，前后端需单独部署。生产环境至少应：

- 使用固定版本构建前端静态资源和后端镜像，关闭 Uvicorn `--reload`。
- 将数据库、Catalog、策略仓库、回测产物挂载到持久卷并备份。
- 将所有相对数据路径改为绝对路径。
- 更换 `LLM_SECRET_ENCRYPTION_KEY`，通过 Secret 管理器提供 API Key 和 Git 密码。
- 限制 CORS，置于 HTTPS 反向代理后，并补充用户认证、授权和审计。
- 为 PostgreSQL 设置连接池、监控与迁移发布流程。
- 将长任务迁移到可恢复的队列/Worker；当前 asyncio、线程池和内存状态只适合单实例。
- 给 Catalog 下载和回测目录设置容量、配额和清理策略。

## 10. 可靠性、安全性与一致性设计

已有保护：

- Pydantic 对日期、杠杆、参数范围、标的数量和文件名进行校验。
- 策略文件和回测产物路径执行父目录检查，降低路径穿越风险。
- LLM API Key 与 Git 密码加密后入库，响应只返回是否已配置。
- Git AskPass 临时脚本执行后删除，凭据不写入 remote URL。
- 回测使用独立子进程并设置超时，异常堆栈写入任务错误和日志。
- Git commit + Manifest hash 双重约束策略代码与业务契约。
- 服务重启时将中断的回测显式标记失败，避免永久卡在运行态。

仍需关注：

1. **没有鉴权和租户隔离**：`client_id` 仅为客户端标识，不能替代身份认证。
2. **长任务不持久**：回测 asyncio Task、下载任务、WebSocket 和 Agent 活动对象均在单进程内。
3. **取消不完整**：回测取消未终止子进程，可能出现数据库已取消但后台仍占用资源的情况。
4. **多实例不安全**：任务状态、信号量和连接表不共享；Redis 目前未接入。
5. **密钥轮换困难**：由单一配置密钥派生加密密钥，更换时需设计旧密文迁移。
6. **同步阻塞点**：部分 Git/文件操作位于请求进程内，高并发时需转线程或任务队列。
7. **外部数据变化**：标的列表来自实时 Exchange Info，历史退市标的可能需要额外元数据来源。
8. **自动建表与 Alembic 并存**：生产环境建议仅由迁移控制 Schema，避免隐藏结构漂移。

## 11. 已知限制与后续演进

### 11.1 当前明确未实现或未完成

- 参数优化页面只有占位内容。
- Redis 虽部署并加入依赖，尚未承担队列、缓存、锁或发布订阅职责。
- funding 字段和 `requires_funding` 已进入契约/请求，但当前 builder 未加载资金费率数据。
- 下载任务仅存在于进程内，无法在重启后查询旧任务状态。
- 回测没有真正的排队 Worker；API 实例直接创建后台 Task 和子进程。
- 缺少登录、角色权限、项目级访问控制、限流和正式审计日志。
- 当前启动脚本为开发模式，没有后端容器和生产部署编排。

### 11.2 推荐演进顺序

1. 先完善回测取消、任务恢复、幂等和资源上限。
2. 将 Redis 接入持久任务队列，拆分 API 与回测/下载/Agent Worker。
3. 增加身份认证、RBAC、研究项目所有权和凭据分级管理。
4. 增加 Catalog 元数据索引、覆盖范围查询和数据质量检查。
5. 补齐资金费率、滑点/延迟模型和可配置撮合模型。
6. 在稳定任务基础上实现参数优化、实验矩阵与结果对比。
7. 增加 OpenTelemetry、结构化日志、任务指标、告警和产物生命周期管理。

## 12. 维护约定

- 修改策略契约时，同步更新策略模板、builder、analytics、前端类型和契约测试。
- 修改研究状态时，同步检查 `research.py`、`runner.py`、启动恢复逻辑、前端状态映射和 Alembic Enum 迁移。
- 新增回测产物时，更新 `report_manifest.json`、图表加载器和清理逻辑。
- 数据库结构变化必须新增 Alembic migration，不修改已发布 migration。
- 不要把工作区策略文件的“当前内容”当作已发布版本；可复现性以数据库记录的 Git commit 为准。
- 删除或清理产物前先核对数据库引用；研究项目、回测和策略版本共同构成审计链。
- 本文描述实现现状；架构行为变化后应与代码在同一次提交中更新本文。

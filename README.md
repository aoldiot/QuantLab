# QuantLab

QuantLab 是一个基于 NautilusTrader 的策略研究与回测管理平台。当前版本包含标准策略契约、策略管理、真实 BacktestNode 回测、异步任务状态、ReportProvider 结果收集和深色量化终端 UI。

## 启动方式

### 方式一：Docker Compose 全栈一键启动（推荐）

```bash
docker compose up -d --build
```

该命令将一键构建并启动全部 4 个容器服务：
- **前端工作台**：`http://localhost:5173`（Nginx 反向代理 + SPA）
- **后端 API 与文档**：`http://localhost:8000/docs`
- **PostgreSQL 16**：`localhost:5432`（本地持久化目录 `./data/postgres`）
- **Redis 7**：`localhost:6380`（本地持久化目录 `./data/redis`）

### 方式二：仅启动中间件（本地开发调试）

```bash
docker compose up -d postgres redis
```

## 启动后端

```bash
cd backend
cp .env.example .env
uv sync
./start.sh
```

首次启动会自动建表，并注册标准 ATR 单标的策略与动量轮动组合策略。API 文档位于 `http://localhost:8000/docs`。

## 启动前端

```bash
cd frontend
npm install
npm run dev
```

打开 `http://localhost:5173`。默认 API 地址为 `http://localhost:8000/api`。

## 准备 Nautilus Catalog

真实回测不会下载或伪造数据。请将 Instrument 和 Bar 写入 Nautilus `ParquetDataCatalog`，并在 `.env` 设置：

```env
CATALOG_PATH=/absolute/path/to/catalog
INSTRUMENT_ID_TEMPLATE={symbol}-PERP.{venue}
```

网页中也可以为单次任务指定 Catalog 路径。Catalog、Instrument 或周期不匹配时任务会失败并显示真实错误日志。

## 标准策略契约

策略模块必须导出 `STRATEGY_MANIFEST`，由它声明：

- 策略和配置类路径
- Web 可填写的业务参数及范围
- 主周期和所有数据周期
- 策略运行模式：`SINGLE_INSTRUMENT` 或 `PORTFOLIO`
- 是否支持做空、是否需要资金费率

`SINGLE_INSTRUMENT` 模式会为每个标的创建一个策略对象，平台自动注入单个 `instrument_id` 和 `bar_type`。参考实现位于 `backend/app/strategies/atr_trend.py`。

`PORTFOLIO` 模式只创建一个策略对象，平台把整个币池作为 `instrument_ids` 和 `bar_types` 一次性交给它，适合统一选币、排序、调仓和组合风控。即使选择 100 个标的，一个回测任务仍然只有一个子进程和一个组合策略对象。参考实现位于 `backend/app/strategies/momentum_rotation.py`。

每个回测在独立 Python 子进程中运行 `BacktestNode`，明细报告保存到 `data/backtests/<run-id>/`。

## Git + 数据库策略版本

策略代码以 Git commit 为唯一代码版本，PostgreSQL 保存业务版本号、commit SHA、分支、Manifest 哈希和契约快照。

发布新版本的流程：

1. 打开策略详情并编辑代码。
2. 保存文件；后端会先执行 Python 语法检查。
3. 点击“发布新版本”。平台自动提交策略目录的 Git 修改，并记录 commit 与 Manifest 快照。

可以在 `STRATEGY_MANIFEST.version` 中手动指定更高版本号；若该版本已存在，平台会基于当前最高的 `x.y.z` 版本自动递增补丁号。

策略管理和策略开发已经合并。策略列表同时显示未发布草稿和正式策略；点击策略进入详情页，可以在“代码 / 设置 / 版本 / 回测”之间切换。新建策略会直接创建草稿文件并进入详情页，第一次发布时自动注册数据库策略，此后发布自动创建新版本。

在线代码功能只允许访问 `backend/app/strategies/`，不能通过文件名读取或修改项目其他目录。新建时可以选择单标的或 PORTFOLIO 模板。

创建回测时，任务会锁定策略版本的 commit SHA。执行器通过 `git archive` 将该提交导出到任务的 `data/backtests/<run-id>/source/`，然后从这份只读代码快照启动 NautilusTrader。后续修改当前工作区不会改变旧回测使用的策略代码。

## 策略 Agent 与 DeepSeek Harness (DSH)

系统设置页保存全局 LLM API 配置（支持 DeepSeek 等 OpenAI 兼容接口）。API Key 使用 `LLM_SECRET_ENCRYPTION_KEY` 加密存储。

策略研究模块基于 **DeepSeek Harness (DSH)** 自主架构运行，实现星型拓扑多 Agent 协作（Quant Lead、Researcher、Developer 与 Reviewer）。系统包含完整的 4 级 Pre-Flight 运行期沙盒验证（语法结构、契约规范、向量化指标计算及 NautilusTrader 运行期生命周期测试），确保全流程高质量自主研发与回测。

策略详情页可启动隔离的 Git worktree Agent 会话，支持 Plan、审批执行、自动编辑和完全自动模式。会话与工具事件写入 PostgreSQL，工作区保存在 `data/agent/worktrees/`，修改经 Diff 确认后才应用到正式策略文件。数据库结构使用 Alembic 管理，`start.sh` 会先执行 `alembic upgrade head`。

已经被回测引用的策略版本不能删除，只能将整个策略设置为“停用”。

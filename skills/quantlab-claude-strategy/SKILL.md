---
name: quantlab-claude-strategy
description: 驱动 QuantLab 内置的 Claude Agent SDK 生成、编写、修改与自愈修复 NautilusTrader 量化策略，并执行 4 级 Pre-Flight 运行期沙盒校验。
---

# QuantLab Claude Strategy Writer Skill

本 Skill 专供 Hermes 在 QuantLab 研究闭环中调用。当策略设计完成且**用户已明确批准编写代码**时，Hermes 通过本 Skill 调用 QuantLab 系统的 **Claude Agent SDK**（具备多轮推理、代码生成、4 级运行期沙盒检测与自动自愈能力）来编写或修复策略。

---

## 核心职责划分（严格遵循）

- **Hermes 的职责**：策略构想研讨、交易规则设计、发起用户编码审批、调用本 Skill 驱动 Claude 写码、发起回测与归因分析。
- **Claude Agent SDK 的职责**：在隔离沙盒中生成标准的 NautilusTrader 策略源码（`StrategyConfig`、`Strategy`、`calculate_indicators`、`STRATEGY_MANIFEST`），并自动通过 4 级 Pre-Flight 校验。
- **【严禁行为】**：Hermes 严禁使用通用 `write_file` 工具直接手写策略代码，必须始终通过本 Skill 委托给 Claude 编写！

---

## 使用方法

Hermes 可以通过内置的 `terminal` 工具运行本 Skill 配套的 Python 驱动脚本：

```bash
python skills/quantlab-claude-strategy/scripts/invoke_claude_writer.py \
  --strategy-name "<策略小写标识符>" \
  --instructions "<策略设计说明与交易规则>" \
  --project-id "<当前研究项目ID（可选）>"
```

### 常用参数说明

| 参数 | 必填 | 说明 |
| :--- | :--- | :--- |
| `--strategy-name` | 是 | 策略英文小写标识符（如 `btc_ema_atr`、`macd_triple_filter_trend`） |
| `--instructions` | 是 | 详细的策略编写需求、指标公式、入场出场条件、止损止盈及图表配置说明 |
| `--specification` | 否 | 结构化策略规格 JSON 字符串或 JSON 文件路径 |
| `--project-id` | 否 | 关联的 QuantLab 研究项目 ID，便于系统同步进度日志 |
| `--is-fix` | 否 | 标记为报错修复模式（默认 `False`） |
| `--error-context` | 否 | 回测或运行时报错的完整堆栈信息（在修复模式下提供） |
| `--api-url` | 否 | QuantLab 后端 API 地址（默认 `http://127.0.0.1:8000`） |

---

## 调用示例

### 1. 新建策略编写示例

```bash
python skills/quantlab-claude-strategy/scripts/invoke_claude_writer.py \
  --strategy-name "macd_triple_filter_trend" \
  --instructions "编写MACD三重过滤趋势跟随策略。包含EMA方向过滤、ATR波动率过滤、Choppiness过滤。双均线金叉开多死叉开空。图表主图显示close/fast_ma/slow_ma，副图显示ATR和Choppiness。"
```

### 2. 回测报错修复示例

```bash
python skills/quantlab-claude-strategy/scripts/invoke_claude_writer.py \
  --strategy-name "macd_triple_filter_trend" \
  --is-fix \
  --instructions "修复回测运行时报错" \
  --error-context "KeyError: 'choppiness' in calculate_indicators"
```

---

## 4 级 Pre-Flight 运行期沙盒保障

本 Skill 调用的 Claude Agent SDK 会在完成写码后全自动执行 4 级验证，若未通过会自动在当前会话中触发**多轮自闭环自愈修复**：

1. **L1 静态语法与 AST 检查**：必须包含四大标准导出结构。
2. **L2 契约结构与参数类型检查**：`STRATEGY_MANIFEST.parameters` 与 `StrategyConfig` 字段严格对应，`plot_config` 为双层嵌套字典。
3. **L3 指标计算与 NaN 检测**：`calculate_indicators` 保持 DataFrame 行数不变，覆盖所有 `plot_config` 指标列。
4. **L4 Nautilus 运行时生命周期模拟**：实例化策略与配置对象，检查 `on_start`、`on_bar` 订阅与订单逻辑。

---

## 脚本输出格式

脚本将输出 JSON 格式的标准结果，格式如下：

```json
{
  "ok": true,
  "strategy_name": "macd_triple_filter_trend",
  "file_path": ".../backend/app/strategies/macd_triple_filter_trend.py",
  "verification": {
    "ok": true,
    "summary": "全部 4 级校验通过"
  },
  "code_preview": "import pandas as pd ...",
  "message": "策略代码已成功编写并通过 4 级 Pre-Flight 沙盒验证"
}
```

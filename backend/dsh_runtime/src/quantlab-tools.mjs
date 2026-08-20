import { createHash } from 'node:crypto'
import { defineTool } from '@deepseek-ai/dsh-tools'

const BRIDGE_URL = (process.env.DSH_BRIDGE_URL || 'http://127.0.0.1:8000/api').replace(/\/+$/, '')
const BRIDGE_TOKEN = process.env.DSH_BRIDGE_TOKEN || ''
const PROJECT_ID = process.env.DSH_PROJECT_ID || ''

async function bridge(path, body) {
  const res = await fetch(`${BRIDGE_URL}${path}`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(BRIDGE_TOKEN ? { Authorization: `Bearer ${BRIDGE_TOKEN}` } : {}),
    },
    body: JSON.stringify(body),
  })
  const text = await res.text()
  let parsed
  try {
    parsed = JSON.parse(text)
  } catch {
    parsed = { status: 'error', error: text.slice(0, 500) }
  }
  return { ...parsed, http_status: res.status }
}

function proposalKey(tool, args) {
  const canonical = JSON.stringify(args, Object.keys(args).sort())
  return createHash('sha1').update(`${tool}:${canonical}`).digest('hex')
}

function renderText(_args, value) {
  return [{ type: 'text', text: typeof value === 'string' ? value : JSON.stringify(value, null, 2) }]
}

function argsFor(args) {
  return { ...(args || {}), ...(PROJECT_ID && !args.project_id ? { project_id: PROJECT_ID } : {}) }
}

const awaitingHint = 'If the status is "awaiting_approval", stop your turn immediately and clearly present the proposal to the user for approval. Do not retry the same tool call until the user approves.'

export const name = 'quantlab-tools'
export const inject = ['tools', 'systemPrompt']

export function apply(ctx) {
  const researchInstructions = process.env.DSH_RESEARCH_INSTRUCTIONS || ''
  if (researchInstructions) {
    ctx.systemPrompt.section({
      name: 'quantlab:research-instructions',
      order: 10,
      text: researchInstructions,
    })
  }

  ctx.tools.register(defineTool({
    name: 'propose_backtest_params',
    description: 'Present an editable backtest parameter proposal card to the user. This does not start a backtest and does not require approval. After calling it, stop and wait for the user to review or edit the card.',
    parameters: {
      strategy_name: { type: 'string', required: true, description: 'Strategy identifier to backtest' },
      symbols: { type: 'array', required: true, items: { type: 'string' }, description: 'Symbols to backtest, e.g. ["BTCUSDT"]' },
      timeframes: { type: 'array', required: true, items: { type: 'string' }, description: 'Timeframes, e.g. ["1h"]' },
      start_date: { type: 'string', required: true, description: 'Start date YYYY-MM-DD' },
      end_date: { type: 'string', required: true, description: 'End date YYYY-MM-DD' },
      initial_balance: { type: 'number', description: 'Initial balance, default 10000.0' },
      leverage: { type: 'number', description: 'Leverage, default 1.0' },
      execution_model: { type: 'string', description: 'Execution model, default CONSERVATIVE' },
      parameters: { type: 'object', additionalProperties: true, description: 'Strategy parameter overrides' },
    },
    output: {
      schema: { type: 'string' },
      render: renderText,
    },
    async execute(args) {
      return JSON.stringify({
        ok: true,
        status: 'proposal_ready',
        backtest_params: args || {},
        message: '回测参数方案已生成，等待用户确认。',
      })
    },
  }))

  ctx.tools.register(defineTool({
    name: 'write_strategy_code',
    description: `Write or update a NautilusTrader strategy Python source file for the current research project. Writes into the project's isolated workspace and runs the 4-level Pre-Flight sandbox verification. Requires user approval before the file is written. ${awaitingHint}`,
    parameters: {
      strategy_name: { type: 'string', required: true, description: 'Snake_case strategy identifier, e.g. btc_ema_atr_trend' },
      code: { type: 'string', required: true, description: 'Complete Python source code for the strategy file' },
      project_id: { type: 'string', description: 'Research project id (usually injected automatically)' },
      request_id: { type: 'string', description: 'Approval request id returned by a previous awaiting_approval response; pass it back unchanged when retrying after user approval' },
    },
    output: {
      schema: { type: 'string' },
      render: renderText,
    },
    async execute(args) {
      const a = argsFor(args)
      return JSON.stringify(await bridge('/dsh-tools/call', {
        project_id: a.project_id,
        tool: 'write_strategy_code',
        proposal_key: proposalKey('write_strategy_code', { strategy_name: a.strategy_name, code: a.code }),
        request_id: a.request_id,
        arguments: { strategy_name: a.strategy_name, code: a.code },
      }))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'verify_strategy_file',
    description: 'Run the read-only 4-level Pre-Flight sandbox verification (L1 syntax, L2 manifest contract, L3 indicator coverage, L4 Nautilus instantiation) on the strategy file currently in the project workspace. No approval needed.',
    parameters: {
      strategy_name: { type: 'string', required: true, description: 'Strategy identifier to verify' },
      project_id: { type: 'string', description: 'Research project id (usually injected automatically)' },
    },
    output: {
      schema: { type: 'string' },
      render: renderText,
    },
    async execute(args) {
      const a = argsFor(args)
      return JSON.stringify(await bridge('/dsh-tools/call', {
        project_id: a.project_id,
        tool: 'verify_strategy_file',
        arguments: { strategy_name: a.strategy_name },
      }))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'execute_backtest_tool',
    description: `Submit an isolated NautilusTrader backtest run for the given strategy in the project workspace. Requires user approval before the run is created and started. ${awaitingHint}`,
    parameters: {
      strategy_name: { type: 'string', required: true, description: 'Strategy identifier to backtest' },
      symbols: { type: 'array', required: true, items: { type: 'string' }, description: 'Symbols to backtest, e.g. ["BTCUSDT"]' },
      timeframes: { type: 'array', required: true, items: { type: 'string' }, description: 'Timeframes, e.g. ["1h"]' },
      start_date: { type: 'string', required: true, description: 'Start date YYYY-MM-DD' },
      end_date: { type: 'string', required: true, description: 'End date YYYY-MM-DD' },
      initial_balance: { type: 'number', description: 'Initial balance, default 10000.0' },
      leverage: { type: 'number', description: 'Leverage, default 1.0' },
      parameters: { type: 'object', additionalProperties: true, description: 'Strategy parameter overrides' },
      project_id: { type: 'string', description: 'Research project id (usually injected automatically)' },
      request_id: { type: 'string', description: 'Approval request id returned by a previous awaiting_approval response; pass it back unchanged when retrying after user approval' },
    },
    output: {
      schema: { type: 'string' },
      render: renderText,
    },
    async execute(args) {
      const a = argsFor(args)
      const callArgs = {
        strategy_name: a.strategy_name,
        symbols: a.symbols,
        timeframes: a.timeframes,
        start_date: a.start_date,
        end_date: a.end_date,
        initial_balance: a.initial_balance,
        leverage: a.leverage,
        parameters: a.parameters,
      }
      return JSON.stringify(await bridge('/dsh-tools/call', {
        project_id: a.project_id,
        tool: 'execute_backtest_tool',
        proposal_key: proposalKey('execute_backtest_tool', callArgs),
        request_id: a.request_id,
        arguments: callArgs,
      }))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'dispatch_tool_call',
    description: 'Dispatch a bounded read-only QuantLab research tool. Prefer platform/context tools over inspecting source. Research may use market data, factor experiments and cited web research; implementation verification belongs to the implementation phase.',
    parameters: {
      tool_name: {
        type: 'string', required: true,
        enum: ['quant_get_capabilities', 'quant_get_research_context', 'quant_get_strategy_context', 'quant_web_research', 'quant_market_data_query', 'quant_factor_analysis', 'quant_run_experiment', 'quant_parameter_sweep', 'quant_robustness_test', 'quant_get_strategy', 'quant_preflight_verify'],
        description: 'Name of the analysis tool to run',
      },
      arguments: { type: 'object', additionalProperties: true, required: true, description: 'Arguments for the named tool, e.g. {"symbol":"BTCUSDT","timeframe":"1h","factor_name":"ema_spread"}' },
      project_id: { type: 'string', description: 'Research project id (usually injected automatically)' },
    },
    output: {
      schema: { type: 'string' },
      render: renderText,
    },
    async execute(args) {
      const a = argsFor(args)
      return JSON.stringify(await bridge('/dsh-tools/call', {
        project_id: a.project_id,
        tool: 'dispatch_tool_call',
        arguments: { tool_name: a.tool_name, arguments: a.arguments || {} },
      }))
    },
  }))
}

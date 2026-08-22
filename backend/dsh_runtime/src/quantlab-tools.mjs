import { createHash } from 'node:crypto'
import { defineTool } from '@deepseek-ai/dsh-tools'

const BRIDGE_URL = (process.env.DSH_BRIDGE_URL || 'http://127.0.0.1:8000/api').replace(/\/+$/, '')
const BRIDGE_TOKEN = process.env.DSH_BRIDGE_TOKEN || ''
const PROJECT_ID = process.env.DSH_PROJECT_ID || ''
const PHASE = (process.env.DSH_RESEARCH_PHASE || '').toUpperCase()
const PHASE_ALIASES = {
  IMPLEMENTED: 'IMPLEMENTATION',
  BACKTEST_RETRY: 'BACKTEST',
  AWAITING_IMPLEMENTATION_APPROVAL: 'RESEARCH',
  AWAITING_BACKTEST_APPROVAL: 'BACKTEST',
}
const EFFECTIVE_PHASE = PHASE_ALIASES[PHASE] || PHASE
const DISPATCH_TOOLS_BY_PHASE = {
  RESEARCH: [
    'quant_get_capabilities', 'quant_get_research_context', 'quant_get_strategy_context',
    'quant_web_research', 'quant_market_data_query', 'quant_factor_analysis', 'quant_run_experiment',
  ],
  REPAIR: ['quant_get_strategy_context', 'quant_get_strategy', 'quant_preflight_verify'],
  FIX_ERROR: ['quant_get_strategy_context', 'quant_get_strategy', 'quant_preflight_verify'],
  BACKTEST: ['quant_get_research_context', 'quant_get_strategy_context', 'quant_get_strategy'],
  RESULT_REVIEW: ['quant_get_research_context', 'quant_get_strategy_context', 'quant_get_strategy', 'quant_robustness_test'],
}
const ALL_DISPATCH_TOOLS = [...new Set(Object.values(DISPATCH_TOOLS_BY_PHASE).flat())]

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

const awaitingHint = 'The backend normally executes this action directly. If an installation explicitly enables approvals and returns "awaiting_approval", stop and present it to the user.'

export const name = 'quantlab-tools'
export const inject = ['tools', 'systemPrompt']

export function apply(ctx) {
  const researchInstructions = process.env.DSH_RESEARCH_INSTRUCTIONS || ''
  if (researchInstructions) {
    ctx.systemPrompt.section({
      name: 'quantlab:phase-instructions',
      order: 10,
      text: researchInstructions,
    })
  }

  // Define tools
  const proposeBacktestParamsTool = defineTool({
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
      execution_model: { type: 'string', enum: ['CONSERVATIVE'], description: 'Execution model, default CONSERVATIVE' },
      venue: { type: 'string', description: 'Venue, default BINANCE' },
      market_type: { type: 'string', enum: ['spot', 'um'], description: 'Market type, default um' },
      check_data_integrity: { type: 'boolean', description: 'Check data integrity before backtesting, default true' },
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
  })

  const writeStrategyCodeTool = defineTool({
    name: 'write_strategy_code',
    description: `Verify and publish a NautilusTrader strategy. Technical writes execute directly; iterate until verification passes. ${awaitingHint}`,
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
  })

  const readStrategyCandidateTool = defineTool({
    name: 'read_strategy_candidate',
    description: 'Read the complete Python source currently staged in this project isolated candidate workspace. No approval needed and never reads the shared published strategy directory.',
    parameters: {
      strategy_name: { type: 'string', required: true, description: 'Snake_case strategy identifier' },
      project_id: { type: 'string', description: 'Research project id (usually injected automatically)' },
    },
    output: { schema: { type: 'string' }, render: renderText },
    async execute(args) {
      const a = argsFor(args)
      return JSON.stringify(await bridge('/dsh-tools/call', {
        project_id: a.project_id,
        tool: 'read_strategy_candidate',
        arguments: { strategy_name: a.strategy_name },
      }))
    },
  })

  const stageStrategyCandidateTool = defineTool({
    name: 'stage_strategy_candidate',
    description: 'Atomically stage a complete initial strategy source in the isolated project candidate workspace and immediately run 4-level Pre-Flight. No publication and no approval. If validation fails, keep the candidate and repair it with patch_strategy_candidate.',
    parameters: {
      strategy_name: { type: 'string', required: true, description: 'Snake_case strategy identifier' },
      code: { type: 'string', required: true, description: 'Complete Python source code' },
      project_id: { type: 'string', description: 'Research project id (usually injected automatically)' },
    },
    output: { schema: { type: 'string' }, render: renderText },
    async execute(args) {
      const a = argsFor(args)
      return JSON.stringify(await bridge('/dsh-tools/call', {
        project_id: a.project_id,
        tool: 'stage_strategy_candidate',
        arguments: { strategy_name: a.strategy_name, code: a.code },
      }))
    },
  })

  const patchStrategyCandidateTool = defineTool({
    name: 'patch_strategy_candidate',
    description: 'Apply minimal exact old-to-new replacements to the staged candidate and rerun Pre-Flight. Every old fragment must match exactly once; otherwise the whole patch is rejected without changing the file.',
    parameters: {
      strategy_name: { type: 'string', required: true, description: 'Snake_case strategy identifier' },
      edits: {
        type: 'array', required: true, description: 'One or more exact local replacements',
        items: {
          type: 'object',
          additionalProperties: false,
          properties: {
            old: { type: 'string', required: true, description: 'Exact existing source fragment' },
            new: { type: 'string', required: true, description: 'Replacement source fragment' },
          },
        },
      },
      project_id: { type: 'string', description: 'Research project id (usually injected automatically)' },
    },
    output: { schema: { type: 'string' }, render: renderText },
    async execute(args) {
      const a = argsFor(args)
      return JSON.stringify(await bridge('/dsh-tools/call', {
        project_id: a.project_id,
        tool: 'patch_strategy_candidate',
        arguments: { strategy_name: a.strategy_name, edits: a.edits },
      }))
    },
  })

  const verifyStrategyFileTool = defineTool({
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
  })

  const executeBacktestTool = defineTool({
    name: 'execute_backtest_tool',
    description: `Submit and start an isolated NautilusTrader backtest directly. ${awaitingHint}`,
    parameters: {
      strategy_name: { type: 'string', required: true, description: 'Strategy identifier to backtest' },
      symbols: { type: 'array', required: true, items: { type: 'string' }, description: 'Symbols to backtest, e.g. ["BTCUSDT"]' },
      timeframes: { type: 'array', required: true, items: { type: 'string' }, description: 'Timeframes, e.g. ["1h"]' },
      start_date: { type: 'string', required: true, description: 'Start date YYYY-MM-DD' },
      end_date: { type: 'string', required: true, description: 'End date YYYY-MM-DD' },
      initial_balance: { type: 'number', description: 'Initial balance, default 10000.0' },
      leverage: { type: 'number', description: 'Leverage, default 1.0' },
      venue: { type: 'string', description: 'Venue, default BINANCE' },
      market_type: { type: 'string', enum: ['spot', 'um'], description: 'Market type, default um' },
      execution_model: { type: 'string', enum: ['CONSERVATIVE'], description: 'Execution model, fixed to CONSERVATIVE' },
      check_data_integrity: { type: 'boolean', description: 'Check data integrity before backtesting, default true' },
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
        venue: a.venue,
        market_type: a.market_type,
        execution_model: a.execution_model,
        parameters: a.parameters,
        check_data_integrity: a.check_data_integrity !== false,
      }
      return JSON.stringify(await bridge('/dsh-tools/call', {
        project_id: a.project_id,
        tool: 'execute_backtest_tool',
        proposal_key: proposalKey('execute_backtest_tool', callArgs),
        request_id: a.request_id,
        arguments: callArgs,
      }))
    },
  })

  const dispatchToolCall = defineTool({
    name: 'dispatch_tool_call',
    description: 'Dispatch a bounded read-only QuantLab domain tool. Research tools provide market data and factor analysis; review tools provide attribution and robustness metrics.',
    parameters: {
      tool_name: {
        type: 'string', required: true,
        // The connectivity probe intentionally has no business phase. Tool
        // definitions are still compiled before phase-gated registration, so
        // an empty enum would abort the complete Cordis plugin tree at boot.
        enum: DISPATCH_TOOLS_BY_PHASE[EFFECTIVE_PHASE] || ALL_DISPATCH_TOOLS,
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
  })

  // Phase-aware tool registration gating
  if (EFFECTIVE_PHASE === 'RESEARCH') {
    ctx.tools.register(dispatchToolCall)
  } else if (EFFECTIVE_PHASE === 'REPAIR' || EFFECTIVE_PHASE === 'FIX_ERROR') {
    ctx.tools.register(readStrategyCandidateTool)
    ctx.tools.register(patchStrategyCandidateTool)
    ctx.tools.register(verifyStrategyFileTool)
    ctx.tools.register(writeStrategyCodeTool)
    ctx.tools.register(dispatchToolCall)
  } else if (EFFECTIVE_PHASE === 'IMPLEMENTATION') {
    ctx.tools.register(stageStrategyCandidateTool)
    ctx.tools.register(readStrategyCandidateTool)
    ctx.tools.register(patchStrategyCandidateTool)
    ctx.tools.register(writeStrategyCodeTool)
    ctx.tools.register(verifyStrategyFileTool)
  } else if (EFFECTIVE_PHASE === 'BACKTEST') {
    ctx.tools.register(dispatchToolCall)
    ctx.tools.register(proposeBacktestParamsTool)
    ctx.tools.register(executeBacktestTool)
  } else if (EFFECTIVE_PHASE === 'RESULT_REVIEW') {
    ctx.tools.register(dispatchToolCall)
  }
}

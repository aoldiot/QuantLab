import {useEffect,useMemo,useRef,useState} from 'react'
import {
  AlertCircle,
  AlertTriangle,
  Archive,
  ArrowRight,
  Bot,
  BrainCircuit,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  ChevronUp,
  Clock3,
  Code2,
  Cpu,
  Database,
  ExternalLink,
  FileCode,
  FlaskConical,
  LineChart,
  Loader2,
  MessageSquarePlus,
  Play,
  Plus,
  RotateCcw,
  Send,
  Settings2,
  ShieldCheck,
  Sliders,
  Sparkles,
  Square,
  Terminal,
  Trash2,
  User,
  Wrench,
  X,
} from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {Link,useLocation,useNavigate} from 'react-router-dom'
import CodeEditor from '../CodeEditor'
import {api} from '../api'
import {Status} from '../components'
import type {
  DshAction,
  DshApproval,
  DshLiveEvent,
  ResearchMessage,
  ResearchProject,
  ResearchRun,
  ResearchThinkingStatus,
  ResearchWritingLog,
  Strategy,
} from '../types'
import {getClientId,generateUUID} from '../utils'

const clientId=getClientId

const RESEARCH_PHASE_LABELS:Record<string,string>={
  RESEARCH:'策略研究',
  AWAITING_IMPLEMENTATION_APPROVAL:'待编码审批',
  IMPLEMENTATION:'策略实现',
  AWAITING_BACKTEST_APPROVAL:'待回测审批',
  BACKTEST:'回测执行',
  RESULT_REVIEW:'结果分析',
  ANALYSIS:'结果分析',
}

function BacktestParamsModal({
  isOpen,
  onClose,
  initialParams,
  project,
  currentStrategyName,
  onConfirmAndRun,
}: {
  isOpen: boolean
  onClose: () => void
  initialParams: Record<string, any>
  project: ResearchProject
  currentStrategyName?: string
  onConfirmAndRun: (updatedParams: Record<string, any>) => void
}) {
  const navigate = useNavigate()
  const [strategyName, setStrategyName] = useState('')
  const [symbols, setSymbols] = useState<string[]>([])
  const [symbolsInput, setSymbolsInput] = useState('')
  const [timeframes, setTimeframes] = useState<string[]>(['15m'])
  const [startDate, setStartDate] = useState('2024-01-01')
  const [endDate, setEndDate] = useState('2024-06-30')
  const [initialBalance, setInitialBalance] = useState(10000)
  const [leverage, setLeverage] = useState(1.0)
  const [executionModel, setExecutionModel] = useState('CONSERVATIVE')
  const [checkDataIntegrity, setCheckDataIntegrity] = useState(true)
  const [parameters, setParameters] = useState<Record<string, any>>({})

  const [newParamKey, setNewParamKey] = useState('')
  const [newParamValue, setNewParamValue] = useState('')

  const [checkingCatalog, setCheckingCatalog] = useState(false)
  const [catalogCheckResult, setCatalogCheckResult] = useState<{
    checked: boolean
    ok: boolean
    summary: string
  } | null>(null)

  useEffect(() => {
    if (!isOpen) return
    const sName = currentStrategyName || initialParams.strategy_name || 'strategy'
    setStrategyName(sName)
    const rawSymbols = Array.isArray(initialParams.symbols)
      ? initialParams.symbols
      : [initialParams.symbols || 'BTCUSDT']
    setSymbols(rawSymbols)
    setSymbolsInput(rawSymbols.join(', '))

    const rawTfs = Array.isArray(initialParams.timeframes)
      ? initialParams.timeframes
      : [initialParams.timeframes || '15m']
    setTimeframes(rawTfs)

    setStartDate(initialParams.start_date || '2024-01-01')
    setEndDate(initialParams.end_date || '2024-06-30')
    setInitialBalance(Number(initialParams.initial_balance ?? 10000))
    setLeverage(Number(initialParams.leverage ?? 1.0))
    setExecutionModel(initialParams.execution_model || 'CONSERVATIVE')
    setCheckDataIntegrity(initialParams.check_data_integrity !== false)
    setParameters(initialParams.parameters ? { ...initialParams.parameters } : {})
    setCatalogCheckResult(null)
  }, [isOpen, initialParams])

  if (!isOpen) return null

  const POPULAR_SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'DOGEUSDT', 'XRPUSDT']
  const POPULAR_TIMEFRAMES = ['1m', '5m', '15m', '30m', '1h', '4h', '1d']

  function handleToggleSymbol(sym: string) {
    let next: string[]
    if (symbols.includes(sym)) {
      next = symbols.filter(s => s !== sym)
    } else {
      next = [...symbols, sym]
    }
    if (next.length === 0) next = [sym]
    setSymbols(next)
    setSymbolsInput(next.join(', '))
    setCatalogCheckResult(null)
  }

  function handleSymbolsInputChange(val: string) {
    setSymbolsInput(val)
    const parsed = val.split(/[,，\s]+/).map(s => s.trim().toUpperCase()).filter(Boolean)
    setSymbols(parsed.length ? parsed : ['BTCUSDT'])
    setCatalogCheckResult(null)
  }

  function handleToggleTimeframe(tf: string) {
    let next: string[]
    if (timeframes.includes(tf)) {
      next = timeframes.filter(t => t !== tf)
    } else {
      next = [...timeframes, tf]
    }
    if (next.length === 0) next = [tf]
    setTimeframes(next)
    setCatalogCheckResult(null)
  }

  function handleParamChange(key: string, val: string) {
    setParameters(prev => {
      const copy = { ...prev }
      if (val === '') {
        delete copy[key]
      } else if (!isNaN(Number(val)) && val.trim() !== '') {
        copy[key] = Number(val)
      } else if (val.toLowerCase() === 'true') {
        copy[key] = true
      } else if (val.toLowerCase() === 'false') {
        copy[key] = false
      } else {
        copy[key] = val
      }
      return copy
    })
  }

  function handleAddParam() {
    if (!newParamKey.trim()) return
    const key = newParamKey.trim()
    let val: any = newParamValue.trim()
    if (!isNaN(Number(val)) && val !== '') {
      val = Number(val)
    } else if (val.toLowerCase() === 'true') {
      val = true
    } else if (val.toLowerCase() === 'false') {
      val = false
    }
    setParameters(prev => ({ ...prev, [key]: val }))
    setNewParamKey('')
    setNewParamValue('')
  }

  function handleDeleteParam(key: string) {
    setParameters(prev => {
      const copy = { ...prev }
      delete copy[key]
      return copy
    })
  }

  async function handleCheckCatalog() {
    setCheckingCatalog(true)
    try {
      const res = await api.checkBacktestCatalog({
        symbols,
        timeframes,
        start_date: startDate,
        end_date: endDate,
      })
      setCatalogCheckResult({
        checked: true,
        ok: res.ok,
        summary: res.summary_text || (res.ok ? '所有标的数据完整' : '检测到缺少行情数据'),
      })
    } catch (e) {
      setCatalogCheckResult({
        checked: true,
        ok: false,
        summary: `数据检查异常: ${(e as Error).message}`,
      })
    } finally {
      setCheckingCatalog(false)
    }
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    const finalParams = {
      strategy_name: strategyName,
      symbols: symbols.length ? symbols : ['BTCUSDT'],
      timeframes: timeframes.length ? timeframes : ['15m'],
      start_date: startDate,
      end_date: endDate,
      initial_balance: initialBalance,
      leverage,
      execution_model: executionModel,
      check_data_integrity: checkDataIntegrity,
      parameters,
    }
    onConfirmAndRun(finalParams)
    onClose()
  }

  function handleOpenAdvancedNew() {
    navigate('/backtests/new', {
      state: {
        strategySlug: strategyName,
        researchProjectId: project.id,
        copiedConfig: {
          strategy_name: strategyName,
          symbols,
          timeframes,
          start_date: startDate,
          end_date: endDate,
          initial_balance: initialBalance,
          leverage,
          execution_model: executionModel,
          strategy_parameters: parameters,
          check_data_integrity: checkDataIntegrity,
          research_project_id: project.id,
        },
      },
    })
    onClose()
  }

  return (
    <div className="modal-backdrop">
      <section className="modal backtest-edit-modal">
        <button className="modal-close" onClick={onClose}><X size={16} /></button>
        <div className="backtest-modal-header">
          <div className="modal-title-with-badge">
            <Sliders size={20} className="modal-title-icon" />
            <h2>配置并确认策略回测方案</h2>
            <span className="strategy-slug-badge">{strategyName}</span>
          </div>
          <p className="muted modal-subtitle">用户可在此自主微调标的、周期、回测区间及策略参数，确定后将交由 DeepSeek Harness 调度 QuantLab 执行回测。</p>
        </div>

        <form onSubmit={handleSubmit} className="backtest-form-stack">
          {/* Section 1: 交易标的与K线周期 */}
          <div className="form-section-card">
            <div className="section-head">
              <span className="section-num">1</span>
              <b>交易标的与数据周期</b>
            </div>

            <div className="form-field-group">
              <label>
                <span>交易标的 (Symbols)</span>
                <input
                  type="text"
                  value={symbolsInput}
                  onChange={e => handleSymbolsInputChange(e.target.value)}
                  placeholder="例如：BTCUSDT, ETHUSDT"
                  required
                />
              </label>
              <div className="quick-chips-row">
                <span className="chips-label">快速选择：</span>
                {POPULAR_SYMBOLS.map(sym => (
                  <button
                    key={sym}
                    type="button"
                    className={`quick-chip-btn ${symbols.includes(sym) ? 'active' : ''}`}
                    onClick={() => handleToggleSymbol(sym)}
                  >
                    {sym} {symbols.includes(sym) && <Check size={11} />}
                  </button>
                ))}
              </div>
            </div>

            <div className="form-field-group">
              <label>
                <span>K线周期 (Timeframes)</span>
              </label>
              <div className="quick-chips-row timeframes">
                {POPULAR_TIMEFRAMES.map(tf => (
                  <button
                    key={tf}
                    type="button"
                    className={`quick-chip-btn tf-chip ${timeframes.includes(tf) ? 'active' : ''}`}
                    onClick={() => handleToggleTimeframe(tf)}
                  >
                    {tf} {timeframes.includes(tf) && <Check size={11} />}
                  </button>
                ))}
              </div>
            </div>
          </div>

          {/* Section 2: 回测区间与资金杠杆 */}
          <div className="form-section-card">
            <div className="section-head">
              <span className="section-num">2</span>
              <b>回测区间与资金杠杆</b>
            </div>

            <div className="form-grid-2">
              <label>
                <span>回测开始日期 (Start Date)</span>
                <input
                  type="date"
                  value={startDate}
                  onChange={e => { setStartDate(e.target.value); setCatalogCheckResult(null) }}
                  required
                />
              </label>
              <label>
                <span>回测结束日期 (End Date)</span>
                <input
                  type="date"
                  value={endDate}
                  onChange={e => { setEndDate(e.target.value); setCatalogCheckResult(null) }}
                  required
                />
              </label>
            </div>

            <div className="form-grid-3">
              <label>
                <span>初始资金 (USDT)</span>
                <input
                  type="number"
                  min="100"
                  step="100"
                  value={initialBalance}
                  onChange={e => setInitialBalance(Number(e.target.value))}
                  required
                />
              </label>
              <label>
                <span>杠杆倍数 (Leverage)</span>
                <input
                  type="number"
                  min="1"
                  max="100"
                  step="0.5"
                  value={leverage}
                  onChange={e => setLeverage(Number(e.target.value))}
                  required
                />
              </label>
              <label>
                <span>执行撮合模型</span>
                <select
                  value={executionModel}
                  onChange={e => setExecutionModel(e.target.value)}
                >
                  <option value="CONSERVATIVE">CONSERVATIVE (保守)</option>
                  <option value="REALISTIC">REALISTIC (真实)</option>
                  <option value="AGGRESSIVE">AGGRESSIVE (激进)</option>
                </select>
              </label>
            </div>

            {/* Catalog Check Action */}
            <div className="integrity-checkbox-bar">
              <label className="checkbox-toggle-label">
                <input
                  type="checkbox"
                  checked={checkDataIntegrity}
                  onChange={e => setCheckDataIntegrity(e.target.checked)}
                  style={{ width: 16, height: 16, accentColor: 'var(--cyan)' }}
                />
                <span>检查本地行情完整性（可选，回测启动前校验历史 K 线数据）</span>
              </label>

              <button
                type="button"
                className="button mini secondary catalog-btn"
                onClick={handleCheckCatalog}
                disabled={checkingCatalog}
                title="立即校验当前所选标的与时间范围的本地行情是否完整"
              >
                {checkingCatalog ? <Loader2 size={12} className="spin" /> : <Database size={12} />}
                校验完整性
              </button>
            </div>
            {catalogCheckResult && (
              <div className={`catalog-result-tag ${catalogCheckResult.ok ? 'ok' : 'warn'}`}>
                {catalogCheckResult.ok ? <CheckCircle2 size={13} /> : <AlertTriangle size={13} />}
                <span>{catalogCheckResult.summary}</span>
              </div>
            )}
          </div>

          {/* Section 3: 策略自定义参数 */}
          <div className="form-section-card">
            <div className="section-head">
              <span className="section-num">3</span>
              <b>策略自定义参数字典 (Parameters)</b>
            </div>

            <div className="dynamic-params-table">
              {Object.keys(parameters).length === 0 ? (
                <div className="empty-params-note">暂无自定义策略参数，将使用策略默认 Manifest 默认值</div>
              ) : (
                Object.entries(parameters).map(([key, val]) => (
                  <div key={key} className="dynamic-param-row">
                    <span className="param-key-label">{key}</span>
                    <input
                      type="text"
                      className="param-val-input"
                      value={String(val)}
                      onChange={e => handleParamChange(key, e.target.value)}
                    />
                    <button
                      type="button"
                      className="param-del-btn"
                      title="删除此参数"
                      onClick={() => handleDeleteParam(key)}
                    >
                      <Trash2 size={13} />
                    </button>
                  </div>
                ))
              )}

              {/* Add new param row */}
              <div className="add-param-row">
                <input
                  type="text"
                  placeholder="参数名 (如 fast_period)"
                  value={newParamKey}
                  onChange={e => setNewParamKey(e.target.value)}
                />
                <input
                  type="text"
                  placeholder="参数值 (如 12)"
                  value={newParamValue}
                  onChange={e => setNewParamValue(e.target.value)}
                  onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); handleAddParam(); } }}
                />
                <button
                  type="button"
                  className="button mini secondary add-btn"
                  onClick={handleAddParam}
                  disabled={!newParamKey.trim()}
                >
                  <Plus size={12} /> 添加
                </button>
              </div>
            </div>
          </div>

          {/* Modal Actions */}
          <div className="modal-actions backtest-modal-actions">
            <button
              type="button"
              className="button secondary adv-config-btn"
              onClick={handleOpenAdvancedNew}
              title="在完整回测管理页面中微调更多高级设置"
            >
              <ExternalLink size={13} />
              在回测管理中高级配置
            </button>
            <div className="modal-actions-right">
              <button type="button" className="button" onClick={onClose}>
                取消
              </button>
              <button type="submit" className="button primary execute-confirm-btn">
                <Play size={14} />
                确认修改并执行回测
              </button>
            </div>
          </div>
        </form>
      </section>
    </div>
  )
}

type BacktestParamsProposal = {
  params: Record<string, any>
  cleanContent: string
}

function extractBacktestParamsProposal(msg: ResearchMessage): BacktestParamsProposal | null {
  const metadataParams = msg.metadata?.backtest_params
    || (msg.message_type === 'backtest_params' ? msg.metadata?.arguments : null)
  if (metadataParams && typeof metadataParams === 'object' && !Array.isArray(metadataParams)) {
    return {
      params: metadataParams,
      cleanContent: (msg.content || '')
        .replace(/```(?:backtest_params|json:backtest_params)\s*[\s\S]*?```/gi, '')
        .trim(),
    }
  }

  const content = msg.content || ''
  const fencedBlock = /```(?:backtest_params|json:backtest_params)\s*\r?\n?([\s\S]*?)```/i.exec(content)
  if (!fencedBlock) return null

  try {
    const params = JSON.parse(fencedBlock[1].trim())
    if (!params || typeof params !== 'object' || Array.isArray(params)) return null
    return {
      params,
      cleanContent: content.replace(fencedBlock[0], '').trim(),
    }
  } catch {
    // Keep malformed machine blocks visible so the user can diagnose the model output.
    return null
  }
}

function BacktestParamsCard({
  params,
  onOpenModal,
}: {
  params: Record<string, any>
  onOpenModal: (params: Record<string, any>) => void
}) {
  const strategyName = params.strategy_name || 'strategy'
  const symbols = Array.isArray(params.symbols) ? params.symbols : [params.symbols || 'BTCUSDT']
  const timeframes = Array.isArray(params.timeframes) ? params.timeframes : [params.timeframes || '15m']
  const startDate = params.start_date || '2024-01-01'
  const endDate = params.end_date || '2024-06-30'
  const capital = params.initial_balance ?? 10000
  const leverage = params.leverage ?? 1.0
  const customParams = params.parameters || {}

  const openModal = () => onOpenModal(params)

  return (
    <article
      className="backtest-params-card"
      role="button"
      tabIndex={0}
      aria-label={`配置策略 ${strategyName} 的回测参数`}
      onClick={openModal}
      onKeyDown={event => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault()
          openModal()
        }
      }}
    >
      <div className="params-card-header">
        <div className="params-title">
          <Sliders size={16} className="params-icon" />
          <span><b>策略回测参数方案</b> ({strategyName})</span>
        </div>
        <span className="badge pending">点击配置 / 待确认</span>
      </div>

      <div className="params-grid">
        <div className="param-item">
          <span className="param-label">交易标的</span>
          <b className="param-value">{symbols.join(', ')}</b>
        </div>
        <div className="param-item">
          <span className="param-label">K线周期</span>
          <b className="param-value">{timeframes.join(', ')}</b>
        </div>
        <div className="param-item">
          <span className="param-label">回测区间</span>
          <b className="param-value">{startDate} ~ {endDate}</b>
        </div>
        <div className="param-item">
          <span className="param-label">资金与杠杆</span>
          <b className="param-value">${Number(capital).toLocaleString()} USDT · {leverage}x 杠杆</b>
        </div>
        {Object.keys(customParams).length > 0 && (
          <div className="param-item wide">
            <span className="param-label">策略参数设置</span>
            <div className="param-tags">
              {Object.entries(customParams).map(([key, value]) => (
                <span key={key} className="param-tag">{key}: <b>{String(value)}</b></span>
              ))}
            </div>
          </div>
        )}
      </div>

      <div className="params-card-footer">
        <span className="params-hint">确认前可修改全部参数，不会直接启动回测</span>
        <span className="button mini primary config-modal-btn">
          <Sliders size={12} /> 查看与修改参数
        </span>
      </div>
    </article>
  )
}

function UserMessageBubble({ msg }: { msg: ResearchMessage }) {
  const [expanded, setExpanded] = useState(false)
  const content = msg.content || ''
  const isLong = content.length > 120 || content.split('\n').filter(Boolean).length > 3

  let previewContent = content
  if (isLong && !expanded) {
    const lines = content.split('\n').filter(Boolean)
    if (lines.length > 2) {
      previewContent = lines.slice(0, 2).join('\n')
      if (previewContent.length > 110) {
        previewContent = previewContent.slice(0, 110) + '...'
      } else {
        previewContent += '...'
      }
    } else if (content.length > 110) {
      previewContent = content.slice(0, 110) + '...'
    }
  }

  return (
    <article className="chat-message user">
      <div className="message-avatar">
        <User size={16} />
      </div>
      <div className="message-content">
        <div className="message-author">
          <b>你</b>
          <time>{new Date(msg.created_at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}</time>
        </div>
        <div className="message-markdown user-markdown">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>
            {expanded ? content : previewContent}
          </ReactMarkdown>
          {isLong && (
            <button
              type="button"
              className="user-msg-toggle-btn"
              onClick={() => setExpanded(!expanded)}
            >
              {expanded ? '收起' : '展开全文'}
            </button>
          )}
        </div>
      </div>
    </article>
  )
}

interface ProcessItem {
  id: string
  type: 'thinking' | 'tool'
  thought?: string
  toolName?: string
  args?: Record<string, any>
  result?: Record<string, any>
  isSuccess?: boolean
  isCallRunning?: boolean
  isWritingActive?: boolean
  runForCall?: ResearchRun | null
  isActionResult?: boolean
  rawCallMsg?: ResearchMessage
  rawOutputMsg?: ResearchMessage
}

interface MessageTurn {
  id: string
  userMessage?: ResearchMessage
  processItems: ProcessItem[]
  responseMessages: ResearchMessage[]
}

function groupMessagesIntoTurns(
  messages: ResearchMessage[],
  runs: ResearchRun[],
  writingLog: ResearchWritingLog | null
): MessageTurn[] {
  const turns: MessageTurn[] = []
  let currentTurn: MessageTurn = {
    id: 'turn-initial',
    processItems: [],
    responseMessages: [],
  }

  function dshToolMeta(msg: ResearchMessage): { toolName: string; args: Record<string, any>; result?: any; think?: string } {
    const ev = msg.metadata?.event
    const tool = ev?.tool || {}
    const dsh = ev as { kind?: string; result?: any; call_id?: string } | null
    return {
      toolName: tool?.name || msg.metadata?.tool_name || '',
      args: tool?.arguments || msg.metadata?.arguments || {},
      result: dsh?.kind === 'tool_result' ? dsh.result ?? msg.metadata?.result : msg.metadata?.result,
      think: msg.metadata?.reasoning_content || ev?.data?.reasoning_content,
    }
  }

  function addToolCall(msg: ResearchMessage) {
    const { toolName, args, think } = dshToolMeta(msg)
    if (think && think.trim()) {
      currentTurn.processItems.push({
        id: `${msg.id}-thought`,
        type: 'thinking',
        thought: think,
      })
    }

    const isBacktestCall = toolName === 'execute_backtest' || toolName === 'execute_backtest_tool' || toolName === 'quant_execute_backtest'
    const isWritingCall = toolName === 'write_strategy_code' || toolName === 'write_strategy_with_claude' || toolName === 'quant_save_strategy_code'
    const runForCall = isBacktestCall
      ? runs.find(run => {
          const config = run.config || {}
          if (args.strategy_name && config.strategy_name === args.strategy_name) return true
          const sameDates = Boolean(
            args.start_date && args.end_date
            && config.start_date === args.start_date
            && config.end_date === args.end_date
          )
          const requestedSymbols = Array.isArray(args.symbols) ? args.symbols.map(String).sort() : []
          const runSymbols = Array.isArray(config.symbols) ? config.symbols.map(String).sort() : []
          return sameDates
            && requestedSymbols.length > 0
            && requestedSymbols.join('|') === runSymbols.join('|')
        })
      : null
    const isCallRunning = !!(runForCall && ['QUEUED', 'RUNNING', 'ANALYZING'].includes(runForCall.status))
    const isWritingActive = isWritingCall && (writingLog?.status === 'RUNNING')

    currentTurn.processItems.push({
      id: msg.id,
      type: 'tool',
      toolName: toolName || '未命名工具',
      args,
      isCallRunning,
      isWritingActive,
      runForCall,
      rawCallMsg: msg,
    })
  }

  function addToolOutput(msg: ResearchMessage) {
    const { toolName, args, result: dshResult } = dshToolMeta(msg)
    const resultCallId = msg.metadata?.event?.call_id
    const res = dshResult && typeof dshResult === 'object' && Object.keys(dshResult).length > 0
      ? dshResult
      : (msg.metadata?.result || {})
    const isSuccess = res.ok !== false && res.status !== 'FAILED' && (res.exit_code == null || res.exit_code === 0)

    const lastMatchingTool = [...currentTurn.processItems].reverse().find(
      item => item.type === 'tool' &&
        (!item.result || Object.keys(item.result).length === 0) &&
        (
          (resultCallId && item.rawCallMsg?.metadata?.event?.call_id === resultCallId) ||
          (!resultCallId && (!item.toolName || item.toolName === toolName || !toolName))
        )
    )

    if (lastMatchingTool) {
      lastMatchingTool.result = res
      lastMatchingTool.isSuccess = isSuccess
      lastMatchingTool.rawOutputMsg = msg
    } else {
      currentTurn.processItems.push({
        id: msg.id,
        type: 'tool',
        toolName: toolName || '工具结果',
        args: args || msg.metadata?.arguments || {},
        result: res,
        isSuccess,
        rawOutputMsg: msg,
      })
    }
  }

  for (const msg of messages) {
    if (msg.role === 'user') {
      if (currentTurn.userMessage || currentTurn.processItems.length > 0 || currentTurn.responseMessages.length > 0) {
        turns.push(currentTurn)
      }
      currentTurn = {
        id: `turn-${msg.id}`,
        userMessage: msg,
        processItems: [],
        responseMessages: [],
      }
      continue
    }

    if (msg.message_type === 'tool_call') {
      addToolCall(msg)
      continue
    }

    // Legacy proposal message types (code_approval / backtest_params / backtest_result)
    // are no longer produced by the DSH runtime and render as plain messages below.

    // 2. Generic tool execution outputs belong to processItems
    if (msg.message_type === 'tool_output') {
      addToolOutput(msg)
      continue
    }

    if (msg.role === 'tool') {
      addToolOutput(msg)
      continue
    }

    if (msg.role === 'assistant') {
      const isBacktestApproval = msg.metadata?.event_type === 'approval_execution' && msg.metadata?.tool === 'execute_backtest_tool'
      const isBacktestFixedAction = (msg.metadata?.event_type === 'fixed_action_executed' || msg.metadata?.event_type === 'fixed_action') && (msg.metadata?.action === 'RUN_BACKTEST' || msg.metadata?.tool === 'execute_backtest_tool')
      const runIdMatch = msg.content.match(/"run_id"\s*:\s*"([^"]+)"/) || msg.content.match(/回测任务(?:：|:|`|\s)*([a-f0-9\-]{36})/i)
      const hasBacktestMeta = Boolean(msg.metadata?.run_id || (msg.metadata?.result as any)?.run_id)
      const isBacktestMsg = isBacktestApproval || isBacktestFixedAction || hasBacktestMeta || Boolean(runIdMatch)

      if (isBacktestMsg) {
        const approvedRunId = msg.metadata?.run_id || (msg.metadata?.result as any)?.run_id || runIdMatch?.[1]
        const approvedRun = approvedRunId ? runs.find(run => run.id === approvedRunId) : null
        if (approvedRun && !currentTurn.processItems.some(item => item.runForCall?.id === approvedRun.id)) {
          currentTurn.processItems.push({
            id: `${msg.id}-approved-backtest`,
            type: 'tool',
            toolName: 'execute_backtest_tool',
            args: msg.metadata?.arguments || approvedRun.config || {},
            result: {
              ok: msg.metadata?.ok !== false && approvedRun.status !== 'FAILED',
              run_id: approvedRun.id,
              metrics: approvedRun.metrics,
              status: approvedRun.status,
            },
            isSuccess: msg.metadata?.ok !== false && approvedRun.status !== 'FAILED',
            isCallRunning: ['QUEUED', 'RUNNING', 'ANALYZING'].includes(approvedRun.status),
            runForCall: approvedRun,
            isActionResult: true,
          })
        } else if (!approvedRun && msg.metadata?.ok === false) {
          const errorMatch = msg.content.match(/"error(?:_message)?"\s*:\s*"([^"]+)"/)
          const errorMsg = errorMatch?.[1] || (msg.metadata?.result as any)?.error || (msg.metadata?.result as any)?.error_message || msg.content
          currentTurn.processItems.push({
            id: `${msg.id}-approved-backtest-failed`,
            type: 'tool',
            toolName: 'execute_backtest_tool',
            args: msg.metadata?.arguments || {},
            result: {
              ok: false,
              error: errorMsg,
              error_message: errorMsg,
              strategy_name: msg.metadata?.arguments?.strategy_name,
            },
            isSuccess: false,
            isActionResult: true,
          })
        }
      } else if (msg.metadata?.event_type === 'approval_execution' && (msg.metadata?.tool === 'write_strategy_code' || msg.metadata?.tool === 'write_strategy_with_claude' || msg.metadata?.tool === 'quant_save_strategy_code')) {
        let parsedResult: any = msg.metadata?.result
        if (!parsedResult) {
          const jsonMatch = msg.content.match(/```json\s*([\s\S]*?)\s*```/)
          if (jsonMatch) {
            try {
              parsedResult = JSON.parse(jsonMatch[1])
            } catch {}
          }
        }
        const isOk = msg.metadata?.ok !== false && parsedResult?.ok !== false
        const stratName = parsedResult?.strategy_name || msg.metadata?.arguments?.strategy_name || ''
        const errorMsg = parsedResult?.error || parsedResult?.verification?.error_message || parsedResult?.verification?.summary || (isOk ? undefined : 'Pre-Flight 校验未通过')
        const suggestion = parsedResult?.verification?.suggestion || ''

        currentTurn.processItems.push({
          id: `${msg.id}-approved-write`,
          type: 'tool',
          toolName: 'write_strategy_code',
          args: msg.metadata?.arguments || { strategy_name: stratName },
          result: parsedResult || {
            ok: isOk,
            status: isOk ? 'written' : 'verification_failed',
            error: errorMsg,
            error_message: errorMsg,
            suggestion,
            strategy_name: stratName,
          },
          isSuccess: isOk,
          isActionResult: true,
        })
      }
      if (msg.metadata?.reasoning_content?.trim()) {
        currentTurn.processItems.push({
          id: `${msg.id}-thought`,
          type: 'thinking',
          thought: msg.metadata.reasoning_content,
        })
      }
      const isDuplicate = currentTurn.responseMessages.some(
        m => m.id === msg.id || (m.content && m.content.trim() === msg.content.trim())
      )
      if (!isDuplicate) {
        currentTurn.responseMessages.push(msg)
      }
      continue
    }
  }

  if (currentTurn.userMessage || currentTurn.processItems.length > 0 || currentTurn.responseMessages.length > 0) {
    turns.push(currentTurn)
  }

  return turns
}

function ProcessThinkingStep({ thought }: { thought: string }) {
  const [expanded, setExpanded] = useState(false)
  if (!thought || !thought.trim()) return null

  return (
    <div className="process-step-item thinking">
      <div
        className="process-step-head"
        onClick={() => setExpanded(prev => !prev)}
        title={expanded ? '点击收起思考过程' : '点击展开思考过程'}
      >
        <div className="step-head-title">
          <BrainCircuit size={13} className="step-icon text-cyan" />
          <span className="step-label">DeepSeek Harness 思考与推理 (Reasoning Process)</span>
        </div>
        <div className="step-head-actions">
          <span className="step-toggle-hint">{expanded ? '收起' : '展开'}</span>
          {expanded ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
        </div>
      </div>
      {expanded && (
        <div className="process-step-content thinking-text">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>
            {thought}
          </ReactMarkdown>
        </div>
      )}
    </div>
  )
}

function BacktestRunResultCard({
  run,
  args,
  strategyName,
  busy,
  handleConfirmAnalysis,
  handleOpenParamsModal,
}: {
  run: ResearchRun
  args: Record<string, any>
  strategyName: string
  busy: boolean
  handleConfirmAnalysis: (metrics?: Record<string, any>, stratName?: string) => void
  handleOpenParamsModal: (params: Record<string, any>) => void
}) {
  const metrics = run.metrics || {}
  const totalReturn = metrics.total_return
  const sharpe = metrics.sharpe_ratio ?? metrics.sharpe
  const maxDrawdown = metrics.max_drawdown
  const winRate = metrics.win_rate
  const totalTrades = metrics.total_trades ?? metrics.trades
  const resolvedStrategyName = args.strategy_name || run.config?.strategy_name || strategyName || 'strategy'
  const rerunParams = {
    ...args,
    strategy_name: resolvedStrategyName,
    symbols: args.symbols || run.config?.symbols,
    timeframes: args.timeframes || run.config?.timeframes,
    start_date: args.start_date || run.config?.start_date,
    end_date: args.end_date || run.config?.end_date,
    initial_balance: args.initial_balance ?? run.config?.initial_balance,
    leverage: args.leverage ?? run.config?.leverage,
    parameters: args.parameters || run.config?.strategy_parameters || {},
  }

  return (
    <div className="backtest-result-card-wrap" role="status" aria-live="polite">
      <div className="backtest-main-result-card">
        <div className="result-card-header">
          <div className="result-card-title">
            <CheckCircle2 size={18} className="text-green" />
            <span><b>策略回测完成</b> ({resolvedStrategyName})</span>
          </div>
          <span className="badge ok">报告已就绪</span>
        </div>

        <div className="backtest-metrics-grid">
          <div className="metric-box">
            <span className="label">总收益率</span>
            <b className={`value ${(totalReturn ?? 0) >= 0 ? 'pos' : 'neg'}`}>
              {totalReturn != null ? `${Number(totalReturn).toFixed(2)}%` : '—'}
            </b>
          </div>
          <div className="metric-box">
            <span className="label">夏普比率</span>
            <b className="value">{sharpe != null ? Number(sharpe).toFixed(2) : '—'}</b>
          </div>
          <div className="metric-box">
            <span className="label">最大回撤</span>
            <b className="value neg">{maxDrawdown != null ? `${Number(maxDrawdown).toFixed(2)}%` : '—'}</b>
          </div>
          <div className="metric-box">
            <span className="label">胜率</span>
            <b className="value">{winRate != null ? `${Number(winRate).toFixed(1)}%` : '—'}</b>
          </div>
          <div className="metric-box">
            <span className="label">总交易数</span>
            <b className="value">{totalTrades ?? '—'}</b>
          </div>
          <div className="metric-box action">
            <Link className="button mini primary detail-link-btn" to={`/backtests/${run.id}`} target="_blank">
              完整详情 <ExternalLink size={11} />
            </Link>
          </div>
        </div>

        <div className="backtest-analysis-prompt-card">
          <div className="analysis-prompt-info">
            <Sparkles size={14} className="text-cyan" />
            <span>回测报告已生成，可继续进行绩效归因或调整参数。</span>
          </div>
          <div className="analysis-prompt-actions">
            <button
              type="button"
              className="button mini primary analysis-btn"
              disabled={busy}
              onClick={() => handleConfirmAnalysis(metrics, resolvedStrategyName)}
            >
              <Sparkles size={12} /> 深度归因分析
            </button>
            <button
              type="button"
              className="button mini secondary"
              onClick={() => handleOpenParamsModal(rerunParams)}
            >
              <Sliders size={12} /> 调整参数重新回测
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

function BacktestFailureCard({
  run,
  busy,
  onRepair,
  onAdjust,
}: {
  run: ResearchRun
  busy: boolean
  onRepair: (run: ResearchRun) => void
  onAdjust: (params: Record<string, any>) => void
}) {
  return (
    <article className="backtest-failure-card" role="alert" aria-live="polite">
      <div className="failure-card-icon"><AlertCircle size={18} /></div>
      <div className="failure-card-content">
        <div className="failure-card-head">
          <div>
            <b>回测执行失败</b>
            <span>{run.name}</span>
          </div>
          <span className="badge err">{run.stage || '执行异常'}</span>
        </div>
        <p>{run.error_message || '回测引擎未返回具体错误，请打开日志进一步检查。'}</p>
        <div className="failure-card-actions">
          <button type="button" className="button mini primary" disabled={busy} onClick={() => onRepair(run)}>
            {busy ? <Loader2 size={12} className="spin" /> : <Wrench size={12} />} 修复策略代码
          </button>
          <button type="button" className="button mini secondary" onClick={() => onAdjust(run.config || {})}>
            <Sliders size={12} /> 调整回测参数
          </button>
          <Link className="button mini secondary" to={`/backtests/${run.id}`} target="_blank">
            查看日志 <ExternalLink size={11} />
          </Link>
        </div>
      </div>
    </article>
  )
}

function BacktestRunningCard({ run }: { run: ResearchRun }) {
  const stratName = run.config?.strategy_name || run.name || '策略'
  const progressPct = Math.max(5, run.progress || 10)
  return (
    <div className="backtest-result-card-wrap" role="status" aria-live="polite">
      <div className="backtest-main-result-card running" style={{ border: '1px solid rgba(6, 182, 212, 0.35)', background: 'linear-gradient(135deg, rgba(6, 182, 212, 0.05), rgba(15, 23, 42, 0.6))' }}>
        <div className="result-card-header">
          <div className="result-card-title">
            <Loader2 size={18} className="spin text-cyan" />
            <span><b>NautilusTrader 正在回测中</b> ({stratName})</span>
          </div>
          <span className="badge running">{run.stage || '执行中'} · {run.progress}%</span>
        </div>

        <div style={{ margin: '14px 0 10px' }}>
          <div className="tool-progress-track" style={{ height: '8px', borderRadius: '999px', background: 'rgba(255,255,255,0.06)', overflow: 'hidden' }}>
            <div
              className="tool-progress-fill"
              style={{
                height: '100%',
                width: `${progressPct}%`,
                background: 'linear-gradient(90deg, #06b6d4, #3b82f6)',
                transition: 'width 0.3s ease',
              }}
            />
          </div>
        </div>

        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', fontSize: '12px', color: '#94a3b8', marginTop: '6px' }}>
          <span>{run.stage || '正在加载行情并运行回测引擎...'} ({progressPct}%)</span>
          <Link
            className="button mini secondary"
            to={`/backtests/${run.id}`}
            target="_blank"
            style={{ display: 'inline-flex', alignItems: 'center', gap: '4px' }}
          >
            查看实时日志 <ExternalLink size={11} />
          </Link>
        </div>
      </div>
    </div>
  )
}

function TurnActionCards({
  processItems,
  hasResponseProposal,
  strategyName,
  busy,
  handleConfirmAnalysis,
  handleConfirmRepair,
  handleOpenParamsModal,
}: {
  processItems: ProcessItem[]
  hasResponseProposal: boolean
  strategyName: string
  busy: boolean
  handleConfirmAnalysis: (metrics?: Record<string, any>, stratName?: string) => void
  handleConfirmRepair: (errorMessage?: string, stratName?: string) => void
  handleOpenParamsModal: (params: Record<string, any>) => void
}) {
  const toolItems = processItems.filter(item => item.type === 'tool')
  const proposalItems = hasResponseProposal
    ? []
    : toolItems.filter(item => item.toolName === 'propose_backtest_params')

  const backtestItems = toolItems.filter(item =>
    item.toolName === 'execute_backtest' ||
    item.toolName === 'execute_backtest_tool' ||
    item.toolName === 'quant_execute_backtest' ||
    Boolean(item.runForCall)
  )

  const runningItems = Array.from(
    backtestItems
      .filter(item => item.runForCall && ['QUEUED', 'RUNNING', 'ANALYZING'].includes(item.runForCall.status))
      .reduce((items, item) => items.set(item.runForCall!.id, item), new Map<string, ProcessItem>())
      .values()
  )

  const completedItems = Array.from(
    backtestItems
      .filter(item => item.runForCall?.status === 'COMPLETED')
      .reduce((items, item) => items.set(item.runForCall!.id, item), new Map<string, ProcessItem>())
      .values()
  )

  const failedItems = Array.from(
    backtestItems
      .filter(item => item.runForCall?.status === 'FAILED')
      .reduce((items, item) => items.set(item.runForCall!.id, item), new Map<string, ProcessItem>())
      .values()
  )

  if (proposalItems.length === 0 && runningItems.length === 0 && completedItems.length === 0 && failedItems.length === 0) return null

  return (
    <div className="turn-action-cards" aria-label="本轮任务结果">
      {proposalItems.map(item => (
        <BacktestParamsCard
          key={`${item.id}-proposal`}
          params={item.args || {}}
          onOpenModal={handleOpenParamsModal}
        />
      ))}
      {runningItems.map(item => (
        <BacktestRunningCard
          key={`${item.runForCall!.id}-running`}
          run={item.runForCall!}
        />
      ))}
      {completedItems.map(item => (
        <BacktestRunResultCard
          key={`${item.runForCall!.id}-result`}
          run={item.runForCall!}
          args={item.args || {}}
          strategyName={strategyName}
          busy={busy}
          handleConfirmAnalysis={handleConfirmAnalysis}
          handleOpenParamsModal={handleOpenParamsModal}
        />
      ))}
      {failedItems.map(item => (
        <BacktestFailureCard
          key={`${item.runForCall!.id}-failed`}
          run={item.runForCall!}
          busy={busy}
          onRepair={r => handleConfirmRepair(r.error_message || '', r.config?.strategy_name || r.name)}
          onAdjust={handleOpenParamsModal}
        />
      ))}
    </div>
  )
}

function ProcessToolStep({
  item,
  project,
  strategyName,
  writingLog,
  busy,
  handleConfirmAnalysis,
  handleConfirmRepair,
  handleOpenParamsModal,
  handleOpenWritingLog,
  loadStrategy,
  setDrawerTab,
  setDrawerOpen,
}: {
  item: ProcessItem
  project: ResearchProject | null
  strategyName: string
  writingLog: ResearchWritingLog | null
  busy: boolean
  handleConfirmAnalysis: (metrics?: Record<string, any>, stratName?: string) => void
  handleConfirmRepair: (errorMessage?: string, stratName?: string) => void
  handleOpenParamsModal: (params: Record<string, any>) => void
  handleOpenWritingLog: () => void
  loadStrategy: (projId: string) => void
  setDrawerTab: (tab: 'code' | 'backtests' | 'writer_log') => void
  setDrawerOpen: (open: boolean) => void
}) {
  const [expanded, setExpanded] = useState(false)
  const toolName = item.toolName || '工具调用'
  const args = item.args || {}
  const res = item.result || {}
  const isBacktest = toolName === 'execute_backtest' || toolName === 'execute_backtest_tool' || toolName === 'quant_execute_backtest'
  const isWriting = toolName === 'write_strategy_code' || toolName === 'write_strategy_with_claude' || toolName === 'quant_save_strategy_code'
  const isTerminal = toolName === 'terminal'
  const isSkill = toolName === 'skill_view'
  const isProcess = toolName === 'process'

  const hasResult = !!item.result
  const isSuccess = item.isSuccess ?? (res.ok !== false && res.status !== 'FAILED' && (res.exit_code == null || res.exit_code === 0))
  const isRunning = item.isCallRunning || item.isWritingActive

  let titleText = `调用工具: ${toolName}`
  if (isWriting) {
    titleText = `编写策略「${args.strategy_name || '策略'}」`
  } else if (isBacktest) {
    titleText = `NautilusTrader 执行回测 (${args.strategy_name || '策略'})`
  } else if (isTerminal) {
    const cmdStr = args.command ? (args.command.length > 45 ? args.command.slice(0, 45) + '...' : args.command) : 'run'
    titleText = `终端指令: $ ${cmdStr}`
  } else if (isSkill) {
    titleText = `加载技能: ${args.name || ''}`
  } else if (isProcess) {
    titleText = `后台进程: ${args.action || 'wait'}`
  }

  let statusBadge = '已调用'
  let badgeClass = 'default'
  if (isRunning) {
    statusBadge = item.isWritingActive
      ? `写码中 ${writingLog?.progress ?? 30}%`
      : `回测中 ${item.runForCall?.progress ?? 10}%`
    badgeClass = 'running'
  } else if (hasResult) {
    if (isSuccess) {
      if (isBacktest && res.metrics?.total_return != null) {
        statusBadge = `成功 · 收益 ${Number(res.metrics.total_return).toFixed(2)}%`
      } else if (isWriting) {
        statusBadge = '成功 · 4级沙盒通过'
      } else {
        statusBadge = '成功'
      }
      badgeClass = 'ok'
    } else {
      statusBadge = '失败'
      badgeClass = 'err'
    }
  }

  return (
    <div className={`process-step-item tool ${badgeClass}`}>
      <div
        className="process-step-head"
        onClick={() => setExpanded(prev => !prev)}
        title={expanded ? '点击收起工具详情' : '点击展开工具详情'}
      >
        <div className="step-head-title">
          {isWriting ? (
            <Code2 size={13} className="step-icon text-indigo" />
          ) : isBacktest ? (
            <FlaskConical size={13} className="step-icon text-cyan" />
          ) : isTerminal ? (
            <Terminal size={13} className="step-icon text-sky" />
          ) : isSkill ? (
            <Sparkles size={13} className="step-icon text-purple" />
          ) : isProcess ? (
            <Cpu size={13} className="step-icon text-amber" />
          ) : (
            <Wrench size={13} className="step-icon text-muted" />
          )}
          <span className="step-label">{titleText}</span>
        </div>
        <div className="step-head-actions">
          <span className={`badge mini ${badgeClass}`}>{statusBadge}</span>
          {expanded ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
        </div>
      </div>

      {!expanded && isRunning && (
        <div className="tool-mini-progress-track">
          <div
            className={`tool-mini-progress-fill ${item.isWritingActive ? 'writer' : ''}`}
            style={{ width: `${Math.max(6, item.isWritingActive ? (writingLog?.progress ?? 30) : (item.runForCall?.progress ?? 10))}%` }}
          />
        </div>
      )}

      {expanded && (
        <div className="process-step-content tool-details-content">
          {isRunning && item.runForCall && (
            <div className="tool-live-progress-container">
              <div className="tool-progress-info">
                <div className="tool-progress-stage">
                  <Loader2 size={13} className="spin text-cyan" />
                  <span>{item.runForCall.stage || '正在执行回测...'}</span>
                </div>
                <span className="tool-progress-num">{item.runForCall.progress}%</span>
              </div>
              <div className="tool-progress-track">
                <div
                  className="tool-progress-fill"
                  style={{ width: `${Math.max(6, item.runForCall.progress)}%` }}
                />
              </div>
            </div>
          )}

          {isWriting && (
            <div className="tool-live-writing-box">
              <div className="tool-progress-info">
                <div className="tool-progress-stage">
                  {item.isWritingActive ? (
                    <Loader2 size={13} className="spin text-cyan" />
                  ) : writingLog?.status === 'FAILED' || (!isSuccess && hasResult) ? (
                    <X size={13} className="text-rose-400" />
                  ) : (
                    <Check size={13} className="text-emerald-400" />
                  )}
                  <span>
                    {writingLog?.stage || (
                      item.isWritingActive
                        ? '正在编写策略代码...'
                        : isSuccess
                        ? '代码编写完成并通过沙盒校验'
                        : '代码沙盒校验未通过'
                    )}
                  </span>
                </div>
                <span className="tool-progress-num">{writingLog?.progress ?? 100}%</span>
              </div>
              <div className="tool-progress-track">
                <div
                  className="tool-progress-fill writer"
                  style={{ width: `${Math.max(8, writingLog?.progress ?? 100)}%` }}
                />
              </div>
              <div className="tool-verification-steps-row" style={{ display: 'flex', gap: '6px', flexWrap: 'wrap', marginTop: '8px', marginBottom: '8px' }}>
                {[
                  { level: 'L1', title: 'AST 语法与结构' },
                  { level: 'L2', title: '契约与类加载' },
                  { level: 'L3', title: '指标沙盒计算' },
                  { level: 'L4', title: 'Nautilus 运行时' },
                ].map((st) => {
                  const stepResult = writingLog?.steps?.find((s: any) => s.level === st.level)
                  const isDone = stepResult?.ok === true
                  const isFail = stepResult?.ok === false
                  return (
                    <div
                      key={st.level}
                      style={{
                        display: 'inline-flex',
                        alignItems: 'center',
                        gap: '4px',
                        fontSize: '11px',
                        padding: '2px 8px',
                        borderRadius: '4px',
                        backgroundColor: isDone ? 'rgba(34, 197, 94, 0.12)' : isFail ? 'rgba(239, 68, 68, 0.15)' : 'rgba(255, 255, 255, 0.05)',
                        color: isDone ? '#4ade80' : isFail ? '#f87171' : '#94a3b8',
                        border: `1px solid ${isDone ? 'rgba(34, 197, 94, 0.3)' : isFail ? 'rgba(239, 68, 68, 0.4)' : 'rgba(255, 255, 255, 0.08)'}`
                      }}
                      title={stepResult?.message || st.title}
                    >
                      {isDone ? <Check size={11} className="text-emerald-400" /> : isFail ? <X size={11} className="text-rose-400" /> : <span style={{ opacity: 0.6 }}>•</span>}
                      <span>{st.level} {st.title}</span>
                    </div>
                  )
                })}
              </div>
              {writingLog?.logs && (
                <pre className="tool-mini-terminal">
                  {writingLog.logs.split('\n').filter(Boolean).slice(-5).join('\n')}
                </pre>
              )}
              <div className="tool-writing-foot">
                <button
                  type="button"
                  className="view-log-link"
                  onClick={e => {
                    e.stopPropagation()
                    handleOpenWritingLog()
                  }}
                >
                  <Terminal size={11} /> 实时终端日志 &gt;
                </button>
              </div>
            </div>
          )}

          {isBacktest && isSuccess && (res.metrics || item.runForCall?.metrics) && (
            <>
              <div className="backtest-metrics-card">
                <div className="metric-box">
                  <span className="label">总收益率</span>
                  <b className={`value ${((res.metrics?.total_return ?? item.runForCall?.metrics?.total_return) ?? 0) >= 0 ? 'pos' : 'neg'}`}>
                    {(res.metrics?.total_return ?? item.runForCall?.metrics?.total_return) != null ? `${Number(res.metrics?.total_return ?? item.runForCall?.metrics?.total_return).toFixed(2)}%` : '—'}
                  </b>
                </div>
                <div className="metric-box">
                  <span className="label">夏普比率</span>
                  <b className="value">
                    {((res.metrics?.sharpe_ratio ?? res.metrics?.sharpe) ?? (item.runForCall?.metrics?.sharpe_ratio ?? item.runForCall?.metrics?.sharpe)) != null
                      ? Number((res.metrics?.sharpe_ratio ?? res.metrics?.sharpe) ?? (item.runForCall?.metrics?.sharpe_ratio ?? item.runForCall?.metrics?.sharpe)).toFixed(2)
                      : '—'}
                  </b>
                </div>
                <div className="metric-box">
                  <span className="label">最大回撤</span>
                  <b className="value neg">
                    {(res.metrics?.max_drawdown ?? item.runForCall?.metrics?.max_drawdown) != null ? `${Number(res.metrics?.max_drawdown ?? item.runForCall?.metrics?.max_drawdown).toFixed(2)}%` : '—'}
                  </b>
                </div>
                <div className="metric-box">
                  <span className="label">胜率</span>
                  <b className="value">
                    {(res.metrics?.win_rate ?? item.runForCall?.metrics?.win_rate) != null ? `${Number(res.metrics?.win_rate ?? item.runForCall?.metrics?.win_rate).toFixed(1)}%` : '—'}
                  </b>
                </div>
                <div className="metric-box">
                  <span className="label">总交易数</span>
                  <b className="value">{(res.metrics?.total_trades ?? res.metrics?.trades) ?? (item.runForCall?.metrics?.total_trades ?? item.runForCall?.metrics?.trades) ?? '—'}</b>
                </div>
                {(res.run_id || item.runForCall?.id) && (
                  <div className="metric-box action">
                    <Link className="button mini" to={`/backtests/${res.run_id || item.runForCall?.id}`} target="_blank">
                      完整详情 <ExternalLink size={11} />
                    </Link>
                  </div>
                )}
              </div>

              <div className="backtest-analysis-prompt-card">
                <div className="analysis-prompt-info">
                  <Sparkles size={14} className="text-cyan" />
                  <span>回测已成功生成报告。是否需要对本次回测绩效及交易进行深度归因分析？</span>
                </div>
                <div className="analysis-prompt-actions">
                  <button
                    type="button"
                    className="button mini primary analysis-btn"
                    disabled={busy}
                    onClick={() => handleConfirmAnalysis(res.metrics || item.runForCall?.metrics, res.strategy_name || item.runForCall?.config?.strategy_name || strategyName || '')}
                  >
                    <Sparkles size={12} /> 确认进行回测深度分析
                  </button>
                  <button
                    type="button"
                    className="button mini secondary"
                    onClick={() => {
                      handleOpenParamsModal(res.arguments || res.backtest_params || item.runForCall?.config || {})
                    }}
                  >
                    <Sliders size={12} /> 调整参数重新回测
                  </button>
                </div>
              </div>
            </>
          )}

          {isBacktest && !isSuccess && (hasResult || item.runForCall?.status === 'FAILED') && (
            <div className="backtest-error-box">
              <div className="error-box-header">
                <AlertCircle size={15} className="err-icon" />
                <b>回测执行失败报错：</b>
              </div>
              <p className="err-msg-text">{res.error_message || res.error || item.runForCall?.error_message || '执行过程出现异常'}</p>
              <div className="error-prompt-tip">
                ⚠️ 策略回测运行报错。是否确认让 DeepSeek Harness 进行代码修复？（【系统安全限制】：本次仅修复策略代码，修复后不会自动重新回测）
              </div>
              <div className="err-action-row">
                <button
                  type="button"
                  className="button mini primary fix-btn"
                  disabled={busy}
                  onClick={() => handleConfirmRepair(res.error_message || res.error || item.runForCall?.error_message, res.strategy_name || args.strategy_name || strategyName || '')}
                >
                  <Wrench size={12} /> 确认修复策略代码
                </button>
                <button
                  type="button"
                  className="button mini secondary"
                  onClick={() => {
                    handleOpenParamsModal(res.arguments || res.backtest_params || item.runForCall?.config || {})
                  }}
                >
                  <Sliders size={12} /> 重新调整回测参数
                </button>
                {(res.run_id || item.runForCall?.id) && (
                  <Link className="button mini secondary" to={`/backtests/${res.run_id || item.runForCall?.id}`} target="_blank">
                    查看日志 <ExternalLink size={11} />
                  </Link>
                )}
              </div>
            </div>
          )}

          {isWriting && hasResult && !isSuccess && (
            <div className="backtest-error-box">
              <div className="error-box-header">
                <AlertCircle size={15} className="err-icon" />
                <b>策略 Pre-Flight 校验未通过：</b>
              </div>
              <p className="err-msg-text">
                {res.error_message || res.error || res.verification?.error_message || res.verification?.summary || '代码未通过 4 级沙盒契约校验'}
              </p>
              {res.verification?.suggestion && (
                <div className="error-prompt-tip">
                  💡 修复建议：{res.verification.suggestion}
                </div>
              )}
              <div className="error-prompt-tip">
                ⚠️ 策略代码未通过契约或沙盒校验。是否确认启动 DeepSeek Harness 专属修复模式（REPAIR Cordis）进行代码定向修复？
              </div>
              <div className="err-action-row">
                <button
                  type="button"
                  className="button mini primary fix-btn"
                  disabled={busy}
                  onClick={() =>
                    handleConfirmRepair(
                      res.error_message || res.error || res.verification?.error_message || 'Pre-Flight 校验未通过',
                      res.strategy_name || args.strategy_name || strategyName || ''
                    )
                  }
                >
                  <Wrench size={12} /> 确认修复策略代码
                </button>
                <button
                  type="button"
                  className="button mini secondary"
                  onClick={e => {
                    e.stopPropagation()
                    handleOpenWritingLog()
                  }}
                >
                  <Terminal size={12} /> 写码日志
                </button>
                <button
                  type="button"
                  className="button mini secondary"
                  onClick={e => {
                    e.stopPropagation()
                    if (project) loadStrategy(project.id)
                    setDrawerTab('code')
                    setDrawerOpen(true)
                  }}
                >
                  <Code2 size={12} /> 查看策略源码
                </button>
              </div>
            </div>
          )}

          {isWriting && hasResult && isSuccess && (
            <div className="code-success-bar">
              <span>{res.message || '代码已保存至 strategies 目录并通过 Pre-Flight 校验'}</span>
              <div className="code-bar-actions">
                <button
                  type="button"
                  className="button mini secondary"
                  onClick={e => {
                    e.stopPropagation()
                    handleOpenWritingLog()
                  }}
                >
                  <Terminal size={12} /> 写码日志
                </button>
                <button
                  type="button"
                  className="button mini primary"
                  onClick={e => {
                    e.stopPropagation()
                    if (project) loadStrategy(project.id)
                    setDrawerTab('code')
                    setDrawerOpen(true)
                  }}
                >
                  <Code2 size={12} /> 查看策略代码
                </button>
              </div>
            </div>
          )}

          {isTerminal && args.command && (
            <div className="terminal-cmd-preview">
              <code>$ {args.command}</code>
            </div>
          )}
          {isTerminal && res && typeof res === 'object' && res.output && (
            <pre className="tool-terminal-box">
              {String(res.output)}
            </pre>
          )}

          {!isTerminal && !isWriting && args && Object.keys(args).length > 0 && (
            <pre className="tool-details">
              {JSON.stringify(args, null, 2)}
            </pre>
          )}

          {!isTerminal && !isBacktest && !isWriting && res && Object.keys(res).length > 0 && (
            <pre className="tool-details">
              {typeof res === 'string' ? res : JSON.stringify(res, null, 2)}
            </pre>
          )}
        </div>
      )}
    </div>
  )
}

function HermesProcessBox({
  processItems,
  project,
  strategyName,
  writingLog,
  busy,
  handleConfirmAnalysis,
  handleConfirmRepair,
  handleOpenParamsModal,
  handleOpenWritingLog,
  loadStrategy,
  setDrawerTab,
  setDrawerOpen,
}: {
  processItems: ProcessItem[]
  project: ResearchProject | null
  strategyName: string
  writingLog: ResearchWritingLog | null
  busy: boolean
  handleConfirmAnalysis: (metrics?: Record<string, any>, stratName?: string) => void
  handleConfirmRepair: (errorMessage?: string, stratName?: string) => void
  handleOpenParamsModal: (params: Record<string, any>) => void
  handleOpenWritingLog: () => void
  loadStrategy: (projId: string) => void
  setDrawerTab: (tab: 'code' | 'backtests' | 'writer_log') => void
  setDrawerOpen: (open: boolean) => void
}) {
  const [expanded, setExpanded] = useState(false)
  const visibleProcessItems = processItems.filter(item => !item.isActionResult)
  if (visibleProcessItems.length === 0) return null

  const toolItems = visibleProcessItems.filter(p => p.type === 'tool')
  const thinkingItems = visibleProcessItems.filter(p => p.type === 'thinking')
  const toolCount = toolItems.length
  const hasThinking = thinkingItems.length > 0
  const hasRunning = toolItems.some(p => p.isCallRunning || p.isWritingActive)
  const hasError = toolItems.some(p => p.isSuccess === false)

  let title = 'DeepSeek Harness 思考与执行过程'
  if (toolCount === 0 && hasThinking) {
    title = 'DeepSeek Harness 思考过程'
  } else if (!hasThinking && toolCount > 0) {
    title = 'DeepSeek Harness 工具调度过程'
  }

  return (
    <div className={`hermes-process-box ${expanded ? 'expanded' : 'collapsed'}`}>
      <button
        type="button"
        className="hermes-process-header"
        onClick={() => setExpanded(prev => !prev)}
        title={expanded ? '点击收起过程详情' : '点击展开过程详情'}
      >
        <div className="process-header-left">
          <BrainCircuit size={14} className="process-header-icon text-cyan" />
          <span className="process-header-title">{title}</span>
          <div className="process-pills-row">
            {hasThinking && (
              <span className="process-pill">深度推理</span>
            )}
            {toolCount > 0 && (
              <span className="process-pill cyan">{toolCount} 次工具调用</span>
            )}
            {hasRunning ? (
              <span className="process-pill yellow spin-icon">
                <Loader2 size={10} className="spin" /> 执行中
              </span>
            ) : hasError ? (
              <span className="process-pill red">工具异常</span>
            ) : (
              <span className="process-pill green">已完成</span>
            )}
          </div>
        </div>
        <div className="process-header-right">
          <span className="process-toggle-hint">{expanded ? '收起详情' : '展开过程'}</span>
          {expanded ? <ChevronUp size={13} /> : <ChevronDown size={13} />}
        </div>
      </button>

      {expanded && (
        <div className="hermes-process-body">
          <div className="process-timeline">
            {visibleProcessItems.map((item, idx) => {
              if (item.type === 'thinking' && item.thought) {
                return (
                  <ProcessThinkingStep
                    key={item.id || `think-${idx}`}
                    thought={item.thought}
                  />
                )
              }
              if (item.type === 'tool') {
                return (
                  <ProcessToolStep
                    key={item.id || `tool-${idx}`}
                    item={item}
                    project={project}
                    strategyName={strategyName}
                    writingLog={writingLog}
                    busy={busy}
                    handleConfirmAnalysis={handleConfirmAnalysis}
                    handleConfirmRepair={handleConfirmRepair}
                    handleOpenParamsModal={handleOpenParamsModal}
                    handleOpenWritingLog={handleOpenWritingLog}
                    loadStrategy={loadStrategy}
                    setDrawerTab={setDrawerTab}
                    setDrawerOpen={setDrawerOpen}
                  />
                )
              }
              return null
            })}
          </div>
        </div>
      )}
    </div>
  )
}

function ThinkingAccordion({thought}: {thought: string}) {
  const [expanded, setExpanded] = useState(false)
  if (!thought || !thought.trim()) return null

  return (
    <div className="process-step-item thinking standalone-thinking">
      <div
        className="process-step-head"
        onClick={() => setExpanded(!expanded)}
        title={expanded ? '点击收起思考过程' : '点击展开思考过程'}
      >
        <div className="step-head-title">
          <BrainCircuit size={13} className="step-icon text-cyan" />
          <span className="step-label">DeepSeek Harness 深度思考过程 (Thinking Process)</span>
        </div>
        <div className="step-head-actions">
          <span className="step-toggle-hint">{expanded ? '收起' : '展开'}</span>
          {expanded ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
        </div>
      </div>
      {expanded && (
        <div className="process-step-content thinking-text">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{thought}</ReactMarkdown>
        </div>
      )}
    </div>
  )
}

function DshLiveExecution({events}: {events: DshLiveEvent[]}) {
  const toolCalls = Array.from(
    events
      .filter(event => event.kind === 'tool_call')
      .reduce((calls, event) => calls.set(event.call_id || `${event.turn_id}-${event.seq}`, event), new Map<string, DshLiveEvent>())
      .values()
  ).slice(-8)
  const textDeltas = events.filter(event => event.kind === 'chunk' && event.chunk_type === 'text-delta' && event.text)
  const fallbackText = [...events].reverse().find(
    event => (event.kind === 'assistant_message' || (event.kind === 'chunk' && event.chunk_type === 'block-end')) && event.text
  )?.text || ''
  const streamedText = textDeltas.length > 0 ? textDeltas.map(event => event.text).join('') : fallbackText
  const reasoningDeltas = events.filter(event => event.kind === 'reasoning_chunk' && event.text)
  const fallbackReasoning = [...events].reverse().find(event => event.kind === 'assistant_message' && event.reasoning)?.reasoning || ''
  const reasoningText = reasoningDeltas.length > 0 ? reasoningDeltas.map(event => event.text).join('') : fallbackReasoning

  return (
    <div className="dsh-live-execution" aria-live="polite" aria-label="DeepSeek Harness 实时执行事件">
      <div className="dsh-live-execution-head">
        <Terminal size={12} />
        <span>DSH SDK 实时执行流</span>
        <span className="dsh-live-event-count">
          {toolCalls.length > 0 ? `${toolCalls.length} 次工具调用 · ` : ''}{events.length} 个聚合事件
        </span>
      </div>

      {reasoningText && (
        <details className="dsh-live-reasoning">
          <summary>
            <BrainCircuit size={12} />
            <span>模型推理流</span>
            <span>展开查看</span>
          </summary>
          <div className="dsh-live-reasoning-content">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{reasoningText}</ReactMarkdown>
          </div>
        </details>
      )}

      {toolCalls.length > 0 && (
        <div className="dsh-live-tool-list">
          {toolCalls.map(event => {
            const toolName = event.tool?.name || '未命名工具'
            const toolArgs = event.tool?.arguments || event.tool?.input || event.tool?.args || {}
            const resultEvent = event.call_id
              ? events.find(candidate => candidate.kind === 'tool_result' && candidate.call_id === event.call_id)
              : events.find(candidate => candidate.kind === 'tool_result' && candidate.seq > event.seq && candidate.tool?.name === event.tool?.name)
            const resultFailed = Boolean(resultEvent?.result?.is_error || resultEvent?.result?.error)
            return (
              <details className="dsh-live-tool-row" key={`${event.turn_id}-${event.seq}`}>
                <summary>
                  <Wrench size={12} />
                  <code>{toolName}</code>
                  <span className={`dsh-live-tool-status ${resultFailed ? 'failed' : resultEvent ? 'done' : 'running'}`}>
                    {resultFailed
                      ? <><AlertCircle size={10} /> 执行失败</>
                      : resultEvent
                        ? <><Check size={10} /> 已返回</>
                        : <><Loader2 size={10} className="spin" /> 执行中</>}
                  </span>
                </summary>
                <div className="dsh-live-tool-detail">
                  <span>调用参数</span>
                  <pre>{JSON.stringify(toolArgs, null, 2)}</pre>
                  {resultEvent && (
                    <>
                      <span>执行结果</span>
                      <pre>{JSON.stringify(resultEvent.result ?? {}, null, 2)}</pre>
                    </>
                  )}
                </div>
              </details>
            )
          })}
        </div>
      )}

      {streamedText ? (
        <div className="dsh-live-output">
          <span className="dsh-live-output-label">模型输出（实时）</span>
          <div className="dsh-live-output-content">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{streamedText}</ReactMarkdown>
          </div>
        </div>
      ) : toolCalls.length === 0 ? (
        <div className="dsh-live-waiting">
          <Loader2 size={11} className="spin" />
          <span>已连接 Harness，等待首个 SDK 执行事件…</span>
        </div>
      ) : null}
    </div>
  )
}

const SUGGESTIONS=[
  {
    title:'BTC 15m EMA 动量突破 + ATR 动态止损',
    prompt:'我想设计一个基于 15m BTCUSDT 的双 EMA 均线突破策略，采用 ATR 动态跟踪止损与波动率仓位管理，请为我设计完整的量化方案并准备编写代码。',
  },
  {
    title:'ETH/BTC 均值回归与布林带自适应网格',
    prompt:'我想设计一个针对 ETHUSDT 的布林带突破过滤与 RSI 超买超卖均值回归策略，请提出完整的入场出场条件与参数设计方案。',
  },
  {
    title:'主流币多因子动量轮动策略',
    prompt:'我想设计一个在 BTC, ETH, SOL 之间根据 1h 收益率动量与成交量加权轮动持仓的 Portfolio 策略，请给出量化逻辑。',
  },
]

export default function Research(){
  const location=useLocation(),navigate=useNavigate()
  const navState=location.state as {projectId?:string;autoPrompt?:string}|null

  const[projects,setProjects]=useState<ResearchProject[]>([])
  const[strategies,setStrategies]=useState<Strategy[]>([])
  const[project,setProject]=useState<ResearchProject|null>(null)
  const[messages,setMessages]=useState<ResearchMessage[]>([])
  const[runs,setRuns]=useState<ResearchRun[]>([])
  const[strategyCode,setStrategyCode]=useState<string>('')
  const[strategyName,setStrategyName]=useState<string>('')

  const[input,setInput]=useState('')
  const[busy,setBusy]=useState(false)
  const[actionBusy,setActionBusy]=useState<DshAction|null>(null)
  const[generatingBacktestParams,setGeneratingBacktestParams]=useState(false)
  const[cancelBusy,setCancelBusy]=useState(false)
  const[error,setError]=useState('')
  const[creating,setCreating]=useState(false)
  const[creatingBusy,setCreatingBusy]=useState(false)
  const[newSessionMode,setNewSessionMode]=useState<'blank'|'continue'>('blank')
  const[sourceProjectId,setSourceProjectId]=useState('')
  const[newTitle,setNewTitle]=useState('')
  const[newIdea,setNewIdea]=useState('')
  const[expandedSessionGroups,setExpandedSessionGroups]=useState<Record<string,boolean>>({})

  // Backtest Parameter Modal state
  const[paramsModalOpen,setParamsModalOpen]=useState(false)
  const[activeModalParams,setActiveModalParams]=useState<Record<string,any>>({})

  // Strategy writing log state
  const[writingLog,setWritingLog]=useState<ResearchWritingLog|null>(null)

  // Real-time DSH execution stage and SDK event stream
  const[thinkingStatus,setThinkingStatus]=useState<ResearchThinkingStatus|null>(null)
  const[liveDshEvents,setLiveDshEvents]=useState<DshLiveEvent[]>([])

  // DSH interactive approvals (pending write/backtest proposals awaiting user decision)
  const[dshPending,setDshPending]=useState<DshApproval[]>([])
  const[approveBusyId,setApproveBusyId]=useState<string|null>(null)
  const[approveFeedback,setApproveFeedback]=useState<Record<string,string>>({})

  // Right drawer state
  const[drawerOpen,setDrawerOpen]=useState(false)
  const[drawerTab,setDrawerTab]=useState<'code'|'backtests'|'writer_log'>('code')
  const[expandedTools,setExpandedTools]=useState<Record<string,boolean>>({})

  const timeline=useRef<HTMLDivElement|null>(null)
  const textareaRef=useRef<HTMLTextAreaElement|null>(null)
  const isComposingRef=useRef(false)
  const autoScrollRef=useRef(true)
  const[showScrollBottom,setShowScrollBottom]=useState(false)

  function scrollToBottom(smooth = true){
    if(timeline.current){
      timeline.current.scrollTo({
        top: timeline.current.scrollHeight,
        behavior: smooth ? 'smooth' : 'auto',
      })
      autoScrollRef.current = true
      setShowScrollBottom(false)
    }
  }

  function handleTimelineScroll(){
    const el = timeline.current
    if(!el)return
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight
    const isAtBottom = distanceFromBottom < 80
    autoScrollRef.current = isAtBottom
    setShowScrollBottom(!isAtBottom)
  }

  async function reloadList(){
    try{
      const[value,strategyRows]=await Promise.all([
        api.researchProjects(),
        api.strategies().catch(()=>[]),
      ])
      setProjects(value)
      setStrategies(strategyRows)
      return value
    }catch(e){
      setError((e as Error).message)
      return []
    }
  }

  const strategyById=useMemo(
    ()=>new Map(strategies.map(item=>[item.id,item])),
    [strategies],
  )

  const sessionGroups=useMemo(()=>{
    const groups=new Map<string,{key:string;label:string;slug?:string;sessions:ResearchProject[]}>()
    for(const item of projects){
      const key=item.strategy_id?`strategy:${item.strategy_id}`:'unlinked'
      if(!groups.has(key)){
        const linked=item.strategy_id?strategyById.get(item.strategy_id):null
        groups.set(key,{
          key,
          label:item.strategy_id?(linked?.name||item.title):'尚未关联策略',
          slug:linked?.slug,
          sessions:[],
        })
      }
      groups.get(key)!.sessions.push(item)
    }
    return Array.from(groups.values())
  },[projects,strategyById])

  const continuationSources=useMemo(()=>{
    const seen=new Set<string>()
    return projects.filter(item=>{
      if(!item.strategy_id||seen.has(item.strategy_id))return false
      seen.add(item.strategy_id)
      return true
    })
  },[projects])

  function openCreateModal(){
    const currentSource=project?.strategy_id
      ? continuationSources.find(item=>item.strategy_id===project.strategy_id)
      : continuationSources[0]
    setNewSessionMode(currentSource?'continue':'blank')
    setSourceProjectId(currentSource?.id||'')
    setCreating(true)
  }

  async function open(item:ResearchProject){
    setError('')
    setStrategyCode('')
    setStrategyName('')
    setWritingLog(null)
    setThinkingStatus(null)
    setLiveDshEvents([])
    try{
      const[m,r,fresh,wLog,tStatus,liveEvents]=await Promise.all([
        api.researchMessages(item.id),
        api.researchRuns(item.id),
        api.researchProject(item.id),
        api.researchWritingLog(item.id).catch(()=>null),
        api.researchThinkingStatus(item.id).catch(()=>null),
        api.dshLiveEvents(item.id).catch(()=>null),
      ])
      setMessages(m)
      setRuns(r)
      setProject(fresh)
      if(wLog)setWritingLog(wLog)
      if(tStatus)setThinkingStatus(tStatus)
      if(liveEvents)setLiveDshEvents(liveEvents.events)
      if(fresh.is_busy){
        setBusy(true)
      }else{
        setBusy(false)
      }
      loadStrategy(item.id)
      autoScrollRef.current = true
      setShowScrollBottom(false)
      setTimeout(() => scrollToBottom(false), 50)
    }catch(e){
      const err = e as Error
      setError(err.message)
      if (err.message.includes('不存在') || err.message.includes('404')) {
        setProject(null)
        setMessages([])
        setRuns([])
        setWritingLog(null)
        setThinkingStatus(null)
        setStrategyCode('')
        setStrategyName('')
        reloadList()
      }
    }
  }

  async function runFixedAction(
    action:DshAction,
    options:{content?:string;run_id?:string;arguments?:Record<string,any>}={},
  ){
    if(!project||busy)return
    const labels:Record<DshAction,string>={
      WRITE_STRATEGY:'编写策略',
      GENERATE_BACKTEST_PARAMS:'生成回测参数',
      RUN_BACKTEST:'执行回测',
      FIX_ERROR:'修复报错',
      ANALYZE_BACKTEST:'回测分析',
    }
    // The API validates fixed-action content at 4,000 characters. Backtest
    // error logs can be much larger, so preserve a useful prefix and keep the
    // full diagnostic payload out of the user-message field.
    const actionContent=(options.content||labels[action]).length>4000
      ? `${(options.content||labels[action]).slice(0,3975)}\n（报错内容过长，已截断）`
      : (options.content||labels[action])
    const tempId=generateUUID()
    const optimisticMsg:ResearchMessage={
      id:tempId,
      role:'user',
      content:actionContent,
      message_type:'message',
      metadata:{is_dsh_run:true,event_type:'fixed_action',action},
      created_at:new Date().toISOString(),
    }
    setMessages(prev=>[...prev,optimisticMsg])
    setLiveDshEvents([])
    setBusy(true)
    setActionBusy(action)
    setError('')
    autoScrollRef.current=true
    setTimeout(()=>scrollToBottom(true),30)
    try{
      await api.runDshAction(project.id,{action,...options,content:actionContent})
      const[m,r,fresh,wLog,tStatus,dshP,liveEvents]=await Promise.all([
        api.researchMessages(project.id),
        api.researchRuns(project.id),
        api.researchProject(project.id),
        api.researchWritingLog(project.id).catch(()=>null),
        api.researchThinkingStatus(project.id).catch(()=>null),
        api.dshPending(project.id).catch(()=>[]),
        api.dshLiveEvents(project.id).catch(()=>null),
      ])
      setMessages(m)
      setRuns(r)
      setProject(fresh)
      if(wLog)setWritingLog(wLog)
      if(tStatus)setThinkingStatus(tStatus)
      if(liveEvents)setLiveDshEvents(liveEvents.events)
      setDshPending(dshP)
      setBusy(Boolean(fresh.is_busy||dshP.length>0))
      setTimeout(()=>scrollToBottom(false),50)
    }catch(e){
      setMessages(prev=>prev.filter(msg=>msg.id!==tempId))
      setError((e as Error).message)
      setBusy(false)
    }finally{
      setActionBusy(null)
    }
  }

  function handleConfirmRepair(errorMessage?: string, stratName?: string, runId?:string){
    const failedRun=runId?runs.find(run=>run.id===runId):runs.find(run=>run.status==='FAILED')
    void runFixedAction('FIX_ERROR',{
      run_id: failedRun?.id,
      content: errorMessage ? `修复策略报错：${errorMessage}` : undefined,
      arguments: { strategy_name: stratName || strategyName, error_message: errorMessage },
    })
  }

  function handleConfirmAnalysis(_metrics?: Record<string, any>, _stratName?: string, runId?:string){
    const completedRun=runId?runs.find(run=>run.id===runId):runs.find(run=>run.status==='COMPLETED')
    void runFixedAction('ANALYZE_BACKTEST',{run_id:completedRun?.id})
  }

  function handleOpenParamsModal(params:Record<string,any>){
    setActiveModalParams(params)
    setParamsModalOpen(true)
  }

  async function handleOpenWritingLog(){
    if(project){
      try{
        const wLog=await api.researchWritingLog(project.id)
        setWritingLog(wLog)
      }catch{}
    }
    setDrawerTab('writer_log')
    setDrawerOpen(true)
  }

  function handleConfirmBacktestParams(params:Record<string,any>){
    if(!project||busy)return
    setActiveModalParams(params)
    void runFixedAction('RUN_BACKTEST',{
      content:'使用已确认参数执行回测',
      arguments:params,
    })
  }

  async function loadStrategy(projId:string){
    try{
      const res=await api.researchStrategy(projId)
      if(res.ok&&res.code){
        setStrategyCode(res.code)
        if(res.strategy_name)setStrategyName(res.strategy_name)
      }
    }catch{
      // Preserve existing state on network jitter
    }
  }

  useEffect(()=>{
    reloadList().then(async list=>{
      if(!list.length)return
      const target=list.find(p=>p.id===navState?.projectId)??list[0]
      if(target){
        await open(target)
        if(navState?.autoPrompt){
          setInput(navState.autoPrompt)
          navigate('/research',{replace:true,state:null})
        }
      }
    })
  },[])

  useEffect(()=>{
    if(autoScrollRef.current && timeline.current){
      timeline.current.scrollTo({top:timeline.current.scrollHeight,behavior:'smooth'})
    }
  },[messages,busy,dshPending.length,runs[0]?.id,runs[0]?.status])

  // Continuous periodic polling for research messages, writing logs, thinking status, and backtest runs
  useEffect(()=>{
    if(!project?.id)return

    const hasActiveRun=runs.some(r=>['QUEUED','RUNNING','ANALYZING'].includes(r.status))
    const isWriting=writingLog?.status==='RUNNING'
    const isThinking=thinkingStatus?.status==='THINKING'||thinkingStatus?.status==='TOOL_RUNNING'||thinkingStatus?.status==='GENERATING'
    const isBusy=busy||project.is_busy||hasActiveRun||isWriting||isThinking
    const pollInterval=isBusy ? 1000 : 2500

    const timer=window.setInterval(async ()=>{
      try{
        const[m,r,fresh,wLog,tStatus,dshP,liveEvents]=await Promise.all([
          api.researchMessages(project.id),
          api.researchRuns(project.id),
          api.researchProject(project.id),
          api.researchWritingLog(project.id).catch(()=>null),
          api.researchThinkingStatus(project.id).catch(()=>null),
          api.dshPending(project.id).catch(()=>[]),
          api.dshLiveEvents(project.id).catch(()=>null),
        ])
        setMessages(prev => {
          if(
            prev.length === m.length &&
            prev.length > 0 &&
            prev[prev.length - 1]?.id === m[m.length - 1]?.id &&
            prev[prev.length - 1]?.content === m[m.length - 1]?.content &&
            prev[prev.length - 1]?.message_type === m[m.length - 1]?.message_type
          ){
            return prev
          }
          return m
        })
        setRuns(r)
        setProject(fresh)
        if(wLog)setWritingLog(wLog)
        if(tStatus)setThinkingStatus(tStatus)
        if(liveEvents)setLiveDshEvents(liveEvents.events)
        setDshPending(dshP)
        const runsActive=r.some(run=>['QUEUED','RUNNING','ANALYZING'].includes(run.status))
        const writerActive=wLog?.status==='RUNNING'
        const thinkingActive=tStatus?.status==='THINKING'||tStatus?.status==='TOOL_RUNNING'||tStatus?.status==='GENERATING'
        const hasPending=dshP.length>0
        if(fresh.is_busy||runsActive||writerActive||thinkingActive){
          setBusy(true)
        }else{
          setBusy(hasPending)
          if(!hasPending)loadStrategy(project.id)
        }
      }catch(err: any){
        const msg = String(err?.message || '')
        if (msg.includes('不存在') || msg.includes('404')) {
          setProject(null)
          setMessages([])
          setRuns([])
          setWritingLog(null)
          setThinkingStatus(null)
          setLiveDshEvents([])
          setStrategyCode('')
          reloadList()
        }
      }
    }, pollInterval)
    return()=>window.clearInterval(timer)
  },[project?.id,project?.is_busy,busy,runs.map(r=>r.status).join(','),writingLog?.status,thinkingStatus?.status,dshPending.map(p=>p.request_id).join(',')])

  async function handleCreate(e:React.FormEvent){
    e.preventDefault()
    if(!newTitle.trim()||creatingBusy)return
    if(newSessionMode==='continue'&&!sourceProjectId){
      setError('请选择要续接的已有策略')
      return
    }
    setCreatingBusy(true)
    setError('')
    try{
      const created=await api.createResearch(
        newTitle.trim(),
        newIdea.trim(),
        clientId(),
        newSessionMode==='continue'?sourceProjectId:undefined,
      )
      setCreating(false)
      setCreatingBusy(false)
      setNewSessionMode('blank')
      setSourceProjectId('')
      setNewTitle('')
      setNewIdea('')
      await reloadList()
      await open(created)
    }catch(e){
      setError((e as Error).message)
      setCreatingBusy(false)
    }
  }

  async function runDshTurn(prompt: string) {
    if (!project || !prompt.trim() || busy) return
    const text = prompt.trim()
    const tempId = generateUUID()
    const optimisticMsg: ResearchMessage = {
      id: tempId,
      role: 'user',
      content: text,
      message_type: 'message',
      metadata: { is_dsh_run: true },
      created_at: new Date().toISOString(),
    }
    setMessages(prev => [...prev, optimisticMsg])
    setLiveDshEvents([])
    setInput('')
    setBusy(true)
    setError('')
    autoScrollRef.current = true
    setShowScrollBottom(false)
    setTimeout(() => scrollToBottom(true), 30)
    try {
      await api.runDshPipeline(project.id, text)
      const [m, r, fresh, wLog, tStatus, dshP, liveEvents] = await Promise.all([
        api.researchMessages(project.id),
        api.researchRuns(project.id),
        api.researchProject(project.id),
        api.researchWritingLog(project.id).catch(() => null),
        api.researchThinkingStatus(project.id).catch(() => null),
        api.dshPending(project.id).catch(() => []),
        api.dshLiveEvents(project.id).catch(() => null),
      ])
      setMessages(m)
      setRuns(r)
      setProject(fresh)
      if (wLog) setWritingLog(wLog)
      if (tStatus) setThinkingStatus(tStatus)
      if (liveEvents) setLiveDshEvents(liveEvents.events)
      setDshPending(dshP)
      setBusy(Boolean(fresh.is_busy || dshP.length > 0))
    } catch (err) {
      setError((err as Error).message)
      setBusy(false)
    }
  }

  async function handleDshRun(e?: React.FormEvent) {
    if (e) e.preventDefault()
    await runDshTurn(input)
  }

  async function handleDshCancel() {
    if (!project || cancelBusy) return
    setCancelBusy(true)
    setError('')
    try {
      await api.cancelDshPipeline(project.id)
      const [m, fresh, tStatus, liveEvents] = await Promise.all([
        api.researchMessages(project.id),
        api.researchProject(project.id),
        api.researchThinkingStatus(project.id).catch(() => null),
        api.dshLiveEvents(project.id).catch(() => null),
      ])
      setMessages(m)
      setProject(fresh)
      if (tStatus) setThinkingStatus(tStatus)
      if (liveEvents) setLiveDshEvents(liveEvents.events)
      setBusy(false)
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setCancelBusy(false)
    }
  }

  async function handleDshApprove(approval: DshApproval, approved: boolean) {
    if (!project || approveBusyId) return
    setApproveBusyId(approval.request_id)
    setError('')
    if (approved) setLiveDshEvents([])
    try {
      await api.dshApprove(project.id, approval.request_id, approved, approveFeedback[approval.request_id] || '')
      setApproveFeedback(prev => { const n = { ...prev }; delete n[approval.request_id]; return n })
      const [m, r, fresh, wLog, tStatus, dshP, liveEvents] = await Promise.all([
        api.researchMessages(project.id),
        api.researchRuns(project.id),
        api.researchProject(project.id),
        api.researchWritingLog(project.id).catch(() => null),
        api.researchThinkingStatus(project.id).catch(() => null),
        api.dshPending(project.id).catch(() => []),
        api.dshLiveEvents(project.id).catch(() => null),
      ])
      setMessages(m)
      setRuns(r)
      setProject(fresh)
      if (wLog) setWritingLog(wLog)
      if (tStatus) setThinkingStatus(tStatus)
      if (liveEvents) setLiveDshEvents(liveEvents.events)
      setDshPending(dshP)
      setBusy(Boolean(fresh.is_busy || dshP.length > 0))
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setApproveBusyId(null)
    }
  }

  function getAgentDisplayName(msg: ResearchMessage) {
    if (msg.role === 'user') return '你'
    const role = msg.metadata?.agent_role
    if (role === 'lead') return 'DSH Quant Lead (总主控)'
    if (role === 'researcher') return 'Researcher (量化研究员)'
    if (role === 'developer') return 'Developer (策略开发员)'
    if (role === 'reviewer') return 'Reviewer (独立审核员)'
    if (role === 'tool' || msg.role === 'tool') return 'QuantLab 确定性量化引擎'
    return 'DSH Quant Lead'
  }

  function handleKeyDown(e:React.KeyboardEvent<HTMLTextAreaElement>){
    if(e.key==='Enter'&&!e.shiftKey){
      // 中文/日文等 IME 输入法正在选词输入中，不触发发送
      if(e.nativeEvent.isComposing || isComposingRef.current || e.keyCode === 229){
        return
      }
      e.preventDefault()
      handleDshRun()
    }
  }

  async function handleArchive(){
    if(!project)return
    setBusy(true)
    try{
      const fresh=project.status==='ARCHIVED'?await api.reopenResearch(project.id):await api.archiveResearch(project.id)
      setProject(fresh)
      await reloadList()
    }catch(e){
      setError((e as Error).message)
    }finally{
      setBusy(false)
    }
  }

  async function handleDelete(projId:string,e:React.MouseEvent){
    e.stopPropagation()
    if(!window.confirm('确定要删除此研究项目吗？历史对话记录将一并清除。'))return
    try{
      await api.deleteResearch(projId)
      const list=await reloadList()
      if(project?.id===projId){
        if(list.length>0){
          await open(list[0])
        }else{
          setProject(null)
          setMessages([])
          setRuns([])
          setWritingLog(null)
          setThinkingStatus(null)
          setStrategyCode('')
        }
      }
    }catch(e){
      setError((e as Error).message)
    }
  }

  function toggleTool(msgId:string){
    setExpandedTools(prev=>({...prev,[msgId]:!prev[msgId]}))
  }

  // Active run & latest failed run detection for monitoring & alerting
  const activeRun = runs.find(r => ['QUEUED', 'RUNNING', 'ANALYZING'].includes(r.status))
  const latestFailedRun = runs.length > 0 && runs[0].status === 'FAILED' ? runs[0] : null
  const latestCompletedRun = runs.find(r => r.status === 'COMPLETED') || null
  const strategyReady = Boolean(project?.strategy_id && strategyCode)
  const canStopDsh = busy && dshPending.length === 0 && !activeRun

  function handleQuickAction(action:DshAction){
    if(action==='WRITE_STRATEGY'){
      void runFixedAction(action)
      return
    }
    if(action==='RUN_BACKTEST'){
      const reusableParams=Object.keys(activeModalParams).length>0
        ? activeModalParams
        : (runs[0]?.config||{})
      if(Object.keys(reusableParams).length>0){
        handleOpenParamsModal({
          ...reusableParams,
          strategy_name:reusableParams.strategy_name||strategyName,
          parameters:reusableParams.parameters||reusableParams.strategy_parameters||{},
        })
      }else{
        void runFixedAction(action)
      }
      return
    }
    if(action==='FIX_ERROR'){
      void runFixedAction(action,{run_id:latestFailedRun?.id})
      return
    }
    void runFixedAction(action,{run_id:latestCompletedRun?.id})
  }

  async function handleGenerateBacktestParams(){
    setGeneratingBacktestParams(true)
    try{
      await runFixedAction('GENERATE_BACKTEST_PARAMS',{
        content:'生成回测参数',
      })
    }finally{
      setGeneratingBacktestParams(false)
    }
  }

  // Compute grouped turns for Hermes thinking & tool execution bundling
  const turns = useMemo(
    () => groupMessagesIntoTurns(messages, runs, writingLog),
    [messages, runs, writingLog]
  )

  return(
    <div className="research-page research-page-v2">
      {error&&<div className="form-error research-error-toast"><AlertCircle size={15}/><span>{error}</span><button onClick={()=>setError('')}><X size={14}/></button></div>}

      <div className="research-layout">
        {/* Left Sidebar: Session List */}
        <div className="research-sidebar">
          <div className="sidebar-header">
            <div className="sidebar-title">
              <BrainCircuit className="sidebar-icon"/>
              <span>量化研究会话</span>
              {projects.length>0&&<span className="count-pill">{projects.length}</span>}
            </div>
            <div>
              <button className="button primary new-session-btn" onClick={openCreateModal}>
                <Plus size={14}/>新建
              </button>
            </div>
          </div>

          <div className="session-list">
            {sessionGroups.map(group=>{
              const expanded=expandedSessionGroups[group.key]!==false
              return(
                <section className="session-group" key={group.key}>
                  <button
                    type="button"
                    className="session-group-header"
                    onClick={()=>setExpandedSessionGroups(prev=>({...prev,[group.key]:!expanded}))}
                    aria-expanded={expanded}
                  >
                    {expanded?<ChevronDown size={13}/>:<ChevronRight size={13}/>}
                    <span className="session-group-label">{group.label}</span>
                    {group.slug&&<code>{group.slug}</code>}
                    <span className="session-group-count">{group.sessions.length}</span>
                  </button>
                  {expanded&&(
                    <div className="session-group-items">
                      {group.sessions.map(item=>(
                        <div
                          key={item.id}
                          className={`session-item ${item.id===project?.id?'active':''}`}
                          onClick={()=>open(item)}
                        >
                          <div className="session-item-body">
                            <span className="session-item-title">{item.title}</span>
                            <div className="session-item-meta">
                              <time>{new Date(item.updated_at).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})}</time>
                              <span className="session-phase-tag">{RESEARCH_PHASE_LABELS[item.research_phase||'RESEARCH']||'策略研究'}</span>
                              {item.status==='ARCHIVED'&&<span className="status-tag archived">已归档</span>}
                            </div>
                          </div>
                          <button
                            className="session-delete-btn"
                            title="删除研究"
                            onClick={e=>handleDelete(item.id,e)}
                          >
                            <Trash2 size={13}/>
                          </button>
                        </div>
                      ))}
                    </div>
                  )}
                </section>
              )
            })}
            {!projects.length&&<div className="sidebar-empty">点击上方按钮创建第一个研究主题</div>}
          </div>
        </div>

        {/* Center: Main Chat Stream */}
        <div className="research-main">
          {project?(
            <>
              <div className="chat-header">
                <div className="chat-header-info">
                  <h2>{project.title}</h2>
                  <span className="chat-header-meta">
                    创建于 {new Date(project.created_at).toLocaleDateString('zh-CN')} · DeepSeek Harness 主控
                  </span>
                </div>
                <div className="chat-header-actions">
                  <button
                    className={`button tool-btn ${drawerOpen&&drawerTab==='code'?'active':''}`}
                    onClick={()=>{
                      if(project)loadStrategy(project.id)
                      setDrawerTab('code')
                      setDrawerOpen(open=>drawerTab==='code'?!open:true)
                    }}
                  >
                    <Code2 size={14}/>策略源码 {strategyCode&&<span className="dot-ok"/>}
                  </button>
                  <button
                    className={`button tool-btn ${drawerOpen&&drawerTab==='writer_log'?'active':''}`}
                    onClick={()=>{
                      handleOpenWritingLog()
                      if(drawerTab==='writer_log')setDrawerOpen(open=>!open)
                    }}
                  >
                    <Terminal size={14}/>写码日志 {writingLog?.status==='RUNNING'&&<Loader2 size={11} className="spin text-cyan"/>}
                  </button>
                  <button
                    className={`button tool-btn ${drawerOpen&&drawerTab==='backtests'?'active':''}`}
                    onClick={()=>{
                      setDrawerTab('backtests')
                      setDrawerOpen(open=>drawerTab==='backtests'?!open:true)
                    }}
                  >
                    <FlaskConical size={14}/>回测列表 ({runs.length})
                  </button>
                  <button className="button icon-btn" title={project.status==='ARCHIVED'?'重新打开':'归档研究'} onClick={handleArchive}>
                    {project.status==='ARCHIVED'?<RotateCcw size={14}/>:<Archive size={14}/>}
                  </button>
                </div>
              </div>

              {/* Real-time Active Backtest Status Banner */}
              {activeRun && (
                <div className="active-backtest-banner">
                  <div className="banner-info">
                    <Loader2 size={15} className="spin banner-icon" />
                    <span className="banner-title">回测运行监控：</span>
                    <b className="banner-name">{activeRun.name}</b>
                    <span className="banner-stage">{activeRun.stage} ({activeRun.progress}%)</span>
                  </div>
                  <div className="banner-actions">
                    <button
                      className="button mini secondary"
                      onClick={() => {
                        setDrawerTab('backtests')
                        setDrawerOpen(true)
                      }}
                    >
                      查看实时进度与日志
                    </button>
                  </div>
                </div>
              )}

              <div className="chat-timeline" ref={timeline} onScroll={handleTimelineScroll}>
                {/* Welcome Card if brand new */}
                {messages.length===0&&(
                  <div className="chat-welcome-card">
                    <div className="welcome-avatar"><Bot size={28}/></div>
                    <h3>你好，我是 QuantLab DSH 量化研发主控（Quant Lead）</h3>
                    <p>基于 DeepSeek Harness (DSH) 星型多 Agent 协作系统（Quant Lead、Researcher、Developer、Reviewer）与 QuantLab 确定性工具库，全流程协助您完成假设检验、因子分析、策略编写、沙盒自愈、独立审查与回测验证。</p>
                    <div className="suggestion-grid">
                      {SUGGESTIONS.map((s,i)=>(
                        <button
                          key={i}
                          className="suggestion-card"
                          onClick={()=>{
                            setInput(s.prompt)
                            textareaRef.current?.focus()
                          }}
                        >
                          <Sparkles size={14} className="sugg-icon"/>
                          <b>{s.title}</b>
                        </button>
                      ))}
                    </div>
                  </div>
                )}

                {/* Message Stream grouped by turns */}
                {turns.map(turn => {
                  return (
                    <div key={turn.id} className="chat-turn-group">
                      {/* User message if present */}
                      {turn.userMessage && (
                        <UserMessageBubble msg={turn.userMessage} />
                      )}

                      {/* Hermes / DSH Thinking & Tool Process Box */}
                      {turn.processItems.length > 0 && (
                        <HermesProcessBox
                          processItems={turn.processItems}
                          project={project}
                          strategyName={strategyName}
                          writingLog={writingLog}
                          busy={busy}
                          handleConfirmAnalysis={handleConfirmAnalysis}
                          handleConfirmRepair={handleConfirmRepair}
                          handleOpenParamsModal={handleOpenParamsModal}
                          handleOpenWritingLog={handleOpenWritingLog}
                          loadStrategy={loadStrategy}
                          setDrawerTab={setDrawerTab}
                          setDrawerOpen={setDrawerOpen}
                        />
                      )}

                      {/* Assistant Responses & Proposal Cards for this turn */}
                      {turn.responseMessages.map(msg => {
                        const backtestProposal = extractBacktestParamsProposal(msg)
                        if (backtestProposal && project) {
                          return (
                            <div key={msg.id} className="chat-msg-wrap">
                              {backtestProposal.cleanContent && (
                                <article className={`chat-message ${msg.role}`}>
                                  <div className="message-avatar"><Bot size={16} /></div>
                                  <div className="message-content">
                                    <div className="message-author">
                                      <b>{getAgentDisplayName(msg)}</b>
                                      <time>{new Date(msg.created_at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}</time>
                                    </div>
                                    <div className="message-markdown">
                                      <ReactMarkdown remarkPlugins={[remarkGfm]}>{backtestProposal.cleanContent}</ReactMarkdown>
                                    </div>
                                  </div>
                                </article>
                              )}
                              <BacktestParamsCard
                                params={backtestProposal.params}
                                onOpenModal={handleOpenParamsModal}
                              />
                            </div>
                          )
                        }

                        // Regular assistant or system message
                        return (
                          <article key={msg.id} className={`chat-message ${msg.role}`}>
                            <div className="message-avatar">
                              {msg.role === 'user' ? <User size={16} /> : <Bot size={16} />}
                            </div>
                            <div className="message-content">
                              <div className="message-author">
                                <b>{getAgentDisplayName(msg)}</b>
                                <time>{new Date(msg.created_at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}</time>
                              </div>
                              <div className="message-markdown">
                                <ReactMarkdown remarkPlugins={[remarkGfm]}>
                                  {msg.content}
                                </ReactMarkdown>
                              </div>
                              {strategyReady && dshPending.length === 0 && msg.role === 'assistant' && msg.content.includes('请点击「批准并开始编写代码」') && (
                                <div className="approval-resolved-note" role="status">
                                  <Check size={13} />
                                  <span>此审批已完成；策略代码已写入并通过 L1–L4 校验，无需再次批准。</span>
                                </div>
                              )}
                            </div>
                          </article>
                        )
                      })}

                      <TurnActionCards
                        processItems={turn.processItems}
                        hasResponseProposal={turn.responseMessages.some(msg => Boolean(extractBacktestParamsProposal(msg)))}
                        strategyName={strategyName}
                        busy={busy}
                        handleConfirmAnalysis={handleConfirmAnalysis}
                        handleConfirmRepair={(errMsg, stratName) => handleConfirmRepair(errMsg, stratName)}
                        handleOpenParamsModal={handleOpenParamsModal}
                      />
                    </div>
                  )
                })}

                {/* DSH Interactive Approval Card — user gatekeeper step */}
                {dshPending.length > 0 && (
                  <div className="dsh-approval-stack">
                    {dshPending.map(appr => {
                      const isWrite = appr.tool === 'write_strategy_code'
                      const isBacktest = appr.tool === 'execute_backtest_tool'
                      const args = appr.arguments || {}
                      const fb = approveFeedback[appr.request_id] || ''
                      const pendingKey = `${appr.request_id}-${appr.status}-${appr.created_at}`
                      return (
                        <div key={pendingKey} className="chat-msg-wrap">
                          <article className="chat-message tool dsh-approval-wrap">
                            <div className="message-avatar">
                              <ShieldCheck size={16} />
                            </div>
                            <div className="message-content">
                              <div className="message-author">
                                <b>DSH 执行门禁 · 待你审批</b>
                                <time>{new Date(appr.created_at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}</time>
                              </div>
                              <div className="dsh-approval-card">
                                <div className="dsh-approval-head">
                                  <div className="dsh-approval-badge">
                                    <Sparkles size={14} />
                                    <span>{isBacktest ? '回测执行' : isWrite ? '策略写码' : appr.tool}</span>
                                  </div>
                                  <code className="dsh-approval-rid">{appr.request_id.slice(0, 12)}…</code>
                                </div>
                                <div className="dsh-approval-tip">
                                  <AlertCircle size={13} />
                                  <span>
                                    {isBacktest
                                      ? `DSH 请求创建回测：${args.strategy_name || ''}`
                                      : isWrite
                                        ? `DSH 请求写入/修改策略：${args.strategy_name || ''}`
                                        : `DSH 请求调用工具：${appr.tool}`}
                                  </span>
                                </div>
                                {Object.keys(args).length > 0 && (
                                  <details className="dsh-approval-details">
                                    <summary>查看请求参数</summary>
                                    <pre>{JSON.stringify(args, null, 2)}</pre>
                                  </details>
                                )}
                                <textarea
                                  className="dsh-approval-feedback"
                                  placeholder="审批意见（可选）：例如「改个参数默认值即可」"
                                  rows={2}
                                  value={fb}
                                  disabled={approveBusyId === appr.request_id}
                                  onChange={e => setApproveFeedback(prev => ({ ...prev, [appr.request_id]: e.target.value }))}
                                />
                                <div className="dsh-approval-actions">
                                  <button
                                    type="button"
                                    className="button mini danger"
                                    disabled={approveBusyId === appr.request_id}
                                    onClick={() => handleDshApprove(appr, false)}
                                  >
                                    拒绝
                                  </button>
                                  <button
                                    type="button"
                                    className="button mini primary"
                                    disabled={approveBusyId === appr.request_id}
                                    onClick={() => handleDshApprove(appr, true)}
                                  >
                                    {approveBusyId === appr.request_id ? (
                                      <><Loader2 size={12} className="spin" /> 处理中…</>
                                    ) : (
                                      <><Check size={12} /> 批准并继续</>
                                    )}
                                  </button>
                                </div>
                              </div>
                            </div>
                          </article>
                        </div>
                      )
                    })}
                  </div>
                )}

                {/* Live Backtest Progress or Strategy Writing Live Progress or Live Thinking card */}
                {busy && dshPending.length === 0 && (
                  writingLog?.status === 'RUNNING' ? (
                    <div className="chat-writer-live-card">
                      <div className="live-card-avatar writer">
                        <Code2 size={18} className="live-avatar-icon" />
                      </div>
                      <div className="live-card-body">
                        <div className="live-card-head">
                          <div className="live-title-group">
                            <b>DeepSeek Harness 正在编写策略代码</b>
                            {writingLog.strategy_name && <span className="live-strat-name">{writingLog.strategy_name}.py</span>}
                          </div>
                          <span className="live-percent-badge writer">{writingLog.progress}%</span>
                        </div>

                        <div className="live-progress-track">
                          <div
                            className="live-progress-fill writer"
                            style={{ width: `${Math.max(8, writingLog.progress)}%` }}
                          />
                        </div>

                        <div className="live-stage-desc">
                          <Loader2 size={13} className="spin text-cyan" />
                          <span>当前阶段：<b>{writingLog.stage || '正在编写代码与指标…'}</b></span>
                        </div>

                        {writingLog.logs && (
                          <pre className="live-terminal-stream">
                            {writingLog.logs.split('\n').filter(Boolean).slice(-6).join('\n')}
                          </pre>
                        )}

                        <div className="live-card-footer-actions">
                          <button
                            type="button"
                            className="button mini secondary"
                            onClick={handleOpenWritingLog}
                          >
                            <Terminal size={12} /> 打开完整终端日志
                          </button>
                        </div>
                      </div>
                    </div>
                  ) : activeRun ? (
                    <div className="chat-backtest-live-card">
                      <div className="live-card-avatar">
                        <FlaskConical size={18} className="live-avatar-icon" />
                      </div>
                      <div className="live-card-body">
                        <div className="live-card-head">
                          <div className="live-title-group">
                            <b>正在执行 NautilusTrader 量化回测</b>
                            <span className="live-strat-name">{activeRun.name}</span>
                          </div>
                          <span className="live-percent-badge">{activeRun.progress}%</span>
                        </div>

                        <div className="live-progress-track">
                          <div
                            className="live-progress-fill"
                            style={{ width: `${Math.max(6, activeRun.progress)}%` }}
                          />
                        </div>

                        <div className="live-stage-desc">
                          <Loader2 size={13} className="spin text-cyan" />
                          <span>当前阶段：<b>{activeRun.stage || '执行计算中…'}</b></span>
                        </div>

                        <div className="live-steps-row">
                          <span className={`step-pill ${activeRun.progress >= 10 ? 'done' : 'current'}`}>
                            1. 环境准备
                          </span>
                          <span className={`step-pill ${activeRun.progress >= 30 ? 'done' : activeRun.progress >= 10 ? 'current' : 'pending'}`}>
                            2. 行情校验
                          </span>
                          <span className={`step-pill ${activeRun.progress >= 85 ? 'done' : activeRun.progress >= 30 ? 'current' : 'pending'}`}>
                            3. 策略撮合
                          </span>
                          <span className={`step-pill ${activeRun.progress >= 95 ? 'done' : activeRun.progress >= 85 ? 'current' : 'pending'}`}>
                            4. 绩效报告
                          </span>
                        </div>
                      </div>
                    </div>
                  ) : (
                    <div className="chat-thinking-live-card">
                      <div className="live-card-avatar thinking">
                        <BrainCircuit size={18} className="live-avatar-icon brain-pulse" />
                      </div>
                      <div className="live-card-body">
                        <div className="live-card-head">
                          <div className="live-title-group">
                            <b>DeepSeek Harness 量化主控正在执行研究任务</b>
                            <span className="live-thinking-state-badge">
                              <Loader2 size={11} className="live-status-spinner" aria-hidden="true" />
                              {thinkingStatus?.status === 'TOOL_RUNNING'
                                ? '工具调度中'
                                : thinkingStatus?.status === 'GENERATING'
                                  ? '生成回复中'
                                  : '任务分析中'}
                            </span>
                          </div>
                        </div>

                        <div className="dsh-run-budget" aria-label="DSH 当前阶段与执行预算">
                          <span>{thinkingStatus?.phase === 'IMPLEMENTATION' ? '策略开发' : thinkingStatus?.phase === 'BACKTEST' ? '正式回测' : '策略研究'}</span>
                          <span>{liveDshEvents.filter(event => event.kind === 'tool_call').length} / {thinkingStatus?.phase === 'RESEARCH' || !thinkingStatus?.phase ? 5 : '—'} 次工具</span>
                          {thinkingStatus?.metrics?.elapsed_ms != null && <span>{Math.round(thinkingStatus.metrics.elapsed_ms / 1000)} 秒</span>}
                        </div>

                        <div className="live-stage-desc thinking">
                          <Sparkles size={13} className="text-cyan sparkle-spin" />
                          <span>{thinkingStatus?.step || '正在等待 Harness 返回执行阶段…'}</span>
                        </div>

                        <DshLiveExecution events={liveDshEvents} />
                      </div>
                    </div>
                  )
                )}

                {latestFailedRun && !busy && dshPending.length === 0 && (
                  <BacktestFailureCard
                    run={latestFailedRun}
                    busy={busy}
                    onRepair={run => handleConfirmRepair(run.error_message || '', run.name, run.id)}
                    onAdjust={handleOpenParamsModal}
                  />
                )}
              </div>

              {/* Floating scroll to bottom button */}
              {showScrollBottom && (
                <button
                  type="button"
                  className="chat-scroll-bottom-btn"
                  onClick={() => scrollToBottom(true)}
                  title="回到最新消息"
                >
                  <ChevronDown size={14} />
                  <span>回到底部</span>
                </button>
              )}

              {/* Chat Composer */}
              <div className="chat-composer-wrap">
                {/* Fixed high-frequency actions */}
                <div className="quick-prompt-bar" role="toolbar" aria-label="策略工作流快捷操作">
                  <button
                    type="button"
                    className="quick-chip"
                    disabled={busy||project.status==='ARCHIVED'}
                    onClick={()=>handleQuickAction('WRITE_STRATEGY')}
                    title="跳过意图判断，直接进入策略编写"
                  >
                    {actionBusy==='WRITE_STRATEGY'?<Loader2 size={13} className="spin"/>:<Code2 size={13}/>} 编写策略
                  </button>
                  <button
                    type="button"
                    className="quick-chip"
                    disabled={busy||!project.strategy_id||project.status==='ARCHIVED'}
                    onClick={handleGenerateBacktestParams}
                    title={project.strategy_id?'根据当前策略生成可编辑回测参数，不查询标的列表':'请先完成策略编写'}
                  >
                    {generatingBacktestParams?<Loader2 size={13} className="spin"/>:<Sliders size={13}/>} 生成回测参数
                  </button>
                  <button
                    type="button"
                    className="quick-chip"
                    disabled={busy||!project.strategy_id||project.status==='ARCHIVED'}
                    onClick={()=>handleQuickAction('RUN_BACKTEST')}
                    title={project.strategy_id?'配置确认后直接生成回测审批卡':'请先完成策略编写'}
                  >
                    {actionBusy==='RUN_BACKTEST'&&!generatingBacktestParams?<Loader2 size={13} className="spin"/>:<Play size={13}/>} 执行回测
                  </button>
                  <button
                    type="button"
                    className="quick-chip"
                    disabled={busy||!latestFailedRun||project.status==='ARCHIVED'}
                    onClick={()=>handleQuickAction('FIX_ERROR')}
                    title={latestFailedRun?'直接修复最近一次失败回测':'当前没有失败回测可修复'}
                  >
                    {actionBusy==='FIX_ERROR'?<Loader2 size={13} className="spin"/>:<Wrench size={13}/>} 修复报错
                  </button>
                  <button
                    type="button"
                    className="quick-chip"
                    disabled={busy||!latestCompletedRun||project.status==='ARCHIVED'}
                    onClick={()=>handleQuickAction('ANALYZE_BACKTEST')}
                    title={latestCompletedRun?'直接分析最近一次成功回测':'当前没有已完成回测可分析'}
                  >
                    {actionBusy==='ANALYZE_BACKTEST'?<Loader2 size={13} className="spin"/>:<LineChart size={13}/>} 回测分析
                  </button>
                </div>

                <form className="chat-composer" onSubmit={handleDshRun}>
                  <textarea
                    ref={textareaRef}
                    rows={2}
                    placeholder="输入你的策略设想、改进建议或回测要求，DSH 量化主控将调度研究闭环（Enter 发送，Shift+Enter 换行）…"
                    value={input}
                    onChange={e=>setInput(e.target.value)}
                    onKeyDown={handleKeyDown}
                    onCompositionStart={()=>{isComposingRef.current=true}}
                    onCompositionEnd={()=>{isComposingRef.current=false}}
                    disabled={busy||project.status==='ARCHIVED'}
                  />
                  {canStopDsh ? (
                    <button
                      type="button"
                      className="send-btn stop"
                      disabled={cancelBusy}
                      onClick={handleDshCancel}
                      aria-label="强制停止当前 LLM 任务"
                      title="强制停止当前 LLM 任务"
                    >
                      {cancelBusy ? <Loader2 size={15} className="spin" /> : <Square size={14} fill="currentColor" />}
                      <span>{cancelBusy ? '停止中' : '停止'}</span>
                    </button>
                  ) : (
                    <button
                      type="submit"
                      className="send-btn"
                      disabled={busy||!input.trim()||project.status==='ARCHIVED'}
                      aria-label="发送消息"
                    >
                      <Send size={15}/>
                      <span>发送</span>
                    </button>
                  )}
                </form>
              </div>
            </>
          ):(
            <div className="empty-workspace">
              <BrainCircuit size={48} className="empty-icon"/>
              <h2>未选择研究会话</h2>
              <p>请在左侧选择一个已有会话，或点击新建开启新的量化策略研讨。</p>
              <button className="button primary" onClick={()=>setCreating(true)}>
                <Plus size={14}/>新建研究会话
              </button>
            </div>
          )}
        </div>

        {/* Right Companion Drawer: Code, Writer Logs & Backtests */}
        {drawerOpen&&project&&(
          <div className="research-drawer">
            <div className="drawer-header">
              <div className="drawer-title-group">
                {drawerTab === 'code' ? (
                  <>
                    <Code2 size={15} className="drawer-title-icon" />
                    <b>策略源码</b>
                  </>
                ) : drawerTab === 'writer_log' ? (
                  <>
                    <Terminal size={15} className="drawer-title-icon" />
                    <b>写码日志</b>
                  </>
                ) : (
                  <>
                    <FlaskConical size={15} className="drawer-title-icon" />
                    <b>回测列表 ({runs.length})</b>
                  </>
                )}
              </div>
              <button className="drawer-close" onClick={()=>setDrawerOpen(false)} title="关闭抽屉">
                <X size={15}/>
              </button>
            </div>

            <div className="drawer-content">
              {drawerTab==='code'&&(
                <div className="drawer-code-view">
                  <div className="code-view-bar">
                    <span className="code-filename">
                      <FileCode size={14}/>
                      {strategyName ? `${strategyName}.py` : '策略源码 (未生成)'}
                    </span>
                    {strategyName&&(
                      <Link
                        className="button mini"
                        to={`/strategies/${strategyName}?research=${project.id}`}
                        target="_blank"
                      >
                        在编辑器打开 <ExternalLink size={11}/>
                      </Link>
                    )}
                  </div>
                  {strategyCode?(
                    <CodeEditor value={strategyCode} onChange={()=>{}} readOnly/>
                  ):(
                    <div className="drawer-empty">
                      <Code2 size={32}/>
                      <p>尚未生成代码，请在对话中让 DeepSeek Harness 编写策略。</p>
                    </div>
                  )}
                </div>
              )}

              {drawerTab==='writer_log'&&(
                <div className="drawer-writer-log-view">
                  <div className="code-view-bar">
                    <div className="writer-log-head-left">
                      <Terminal size={14}/>
                      <span className="code-filename">
                        写码日志 {writingLog?.strategy_name ? `(${writingLog.strategy_name}.py)` : ''}
                      </span>
                      {writingLog&&(
                        <span className={`badge ${writingLog.status==='RUNNING'?'running':writingLog.status==='COMPLETED'?'ok':writingLog.status==='FAILED'?'err':'pending'}`}>
                          {writingLog.status==='RUNNING'?`生成中 ${writingLog.progress}%`:writingLog.status==='COMPLETED'?'编写完成':'就绪'}
                        </span>
                      )}
                    </div>
                    <button
                      className="button mini secondary"
                      onClick={async()=>{
                        if(project){
                          const fresh=await api.researchWritingLog(project.id)
                          setWritingLog(fresh)
                        }
                      }}
                    >
                      <RotateCcw size={12}/> 刷新日志
                    </button>
                  </div>
                  <div className="writer-terminal-box">
                    <pre className="terminal-log-content">
                      {writingLog?.logs || '暂无写码日志，在对话中让 DeepSeek Harness 编写策略时将在此实时流式输出。'}
                    </pre>
                  </div>
                </div>
              )}

              {drawerTab==='backtests'&&(
                <div className="drawer-backtest-list">
                  {runs.map(run=>(
                    <div key={run.id} className="drawer-run-card">
                      <div className="run-card-header">
                        <b>{run.name}</b>
                        <Status value={run.status}/>
                      </div>
                      <div className="run-card-meta">
                        <span>{run.stage} · {run.progress}%</span>
                        <time>{new Date(run.created_at).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})}</time>
                      </div>
                      {run.metrics&&(
                        <div className="run-card-metrics">
                          <span>夏普: <b>{(run.metrics.sharpe_ratio ?? run.metrics.sharpe)!=null?Number(run.metrics.sharpe_ratio ?? run.metrics.sharpe).toFixed(2):'—'}</b></span>
                          <span>回撤: <b className="neg">{run.metrics.max_drawdown!=null?`${Number(run.metrics.max_drawdown).toFixed(1)}%`:'—'}</b></span>
                          <span>收益: <b className={(run.metrics.total_return??0)>=0?'pos':'neg'}>{run.metrics.total_return!=null?`${Number(run.metrics.total_return).toFixed(1)}%`:'—'}</b></span>
                        </div>
                      )}
                      <div className="run-card-actions">
                        <Link className="button mini" to={`/backtests/${run.id}`} target="_blank">
                          查看图表与日志 <ExternalLink size={11}/>
                        </Link>
                      </div>
                    </div>
                  ))}
                  {!runs.length&&(
                    <div className="drawer-empty">
                      <FlaskConical size={32}/>
                      <p>当前研究尚无回测记录，可在对话中让 DeepSeek Harness 执行回测。</p>
                    </div>
                  )}
                </div>
              )}
            </div>
          </div>
        )}
      </div>

      {/* Backtest Parameters Edit Modal */}
      {project && (
        <BacktestParamsModal
          isOpen={paramsModalOpen}
          onClose={()=>setParamsModalOpen(false)}
          initialParams={activeModalParams}
          project={project}
          currentStrategyName={strategyName}
          onConfirmAndRun={handleConfirmBacktestParams}
        />
      )}

      {/* New Research Modal */}
      {creating&&(
        <div className="modal-backdrop">
          <section className="modal create-research-modal">
            <button className="modal-close" onClick={()=>setCreating(false)}><X size={16}/></button>
            <h2>新建量化研究会话</h2>
            <p className="muted">选择从零研究，或在不复制冗长历史的情况下续接已有策略。</p>
            <form onSubmit={handleCreate} className="stack-form">
              <div className="new-session-mode" role="radiogroup" aria-label="会话创建方式">
                <label className={newSessionMode==='blank'?'active':''}>
                  <input
                    type="radio"
                    name="new-session-mode"
                    value="blank"
                    checked={newSessionMode==='blank'}
                    onChange={()=>setNewSessionMode('blank')}
                  />
                  <span><b>新建策略会话</b><small>从零开始研究一个新策略</small></span>
                </label>
                <label className={newSessionMode==='continue'?'active':''}>
                  <input
                    type="radio"
                    name="new-session-mode"
                    value="continue"
                    checked={newSessionMode==='continue'}
                    disabled={continuationSources.length===0}
                    onChange={()=>{
                      setNewSessionMode('continue')
                      if(!sourceProjectId)setSourceProjectId(continuationSources[0]?.id||'')
                    }}
                  />
                  <span><b>续接已有策略</b><small>共享策略与最近结果，使用新上下文</small></span>
                </label>
              </div>
              {newSessionMode==='continue'&&(
                <label>
                  选择已有策略
                  <select value={sourceProjectId} onChange={e=>setSourceProjectId(e.target.value)} required>
                    <option value="">请选择策略</option>
                    {continuationSources.map(item=>{
                      const linked=item.strategy_id?strategyById.get(item.strategy_id):null
                      return <option key={item.id} value={item.id}>{linked?.name||item.title} · 最近会话：{item.title}</option>
                    })}
                  </select>
                  <small className="field-help">系统只生成结构化交接摘要，不会复制旧会话消息。</small>
                </label>
              )}
              <label>
                本轮会话名称
                <input
                  type="text"
                  placeholder={newSessionMode==='continue'?'例如：第二轮 · 调整过滤条件与回测':'例如：BTC 15m 均线动量突破'}
                  value={newTitle}
                  onChange={e=>setNewTitle(e.target.value)}
                  required
                  autoFocus
                />
              </label>
              <label>
                {newSessionMode==='continue'?'本轮目标（可选）':'初始策略想法（可选）'}
                <textarea
                  rows={3}
                  placeholder={newSessionMode==='continue'?'例如：重点优化震荡行情下的假突破问题':'可描述交易逻辑、指标构想和适用市场…'}
                  value={newIdea}
                  onChange={e=>setNewIdea(e.target.value)}
                />
              </label>
              <div className="modal-actions">
                <button type="button" className="button" onClick={()=>setCreating(false)}>取消</button>
                <button type="submit" className="button primary" disabled={creatingBusy||!newTitle.trim()||(newSessionMode==='continue'&&!sourceProjectId)}>
                  {creatingBusy?<Loader2 size={14} className="spin"/>:(newSessionMode==='continue'?'创建续接会话':'创建并开启研讨')}
                </button>
              </div>
            </form>
          </section>
        </div>
      )}
    </div>
  )
}

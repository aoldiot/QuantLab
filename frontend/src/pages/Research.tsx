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
  FileDown,
  FileJson,
  FileText,
  FlaskConical,
  LineChart,
  Loader2,
  MessageSquarePlus,
  Play,
  Plus,
  RotateCcw,
  Send,
  Settings2,
  Sliders,
  Sparkles,
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
  CodeApprovalData,
  ResearchMessage,
  ResearchProject,
  ResearchRun,
  ResearchThinkingStatus,
  ResearchWritingLog,
} from '../types'
import {getClientId,generateUUID} from '../utils'

const clientId=getClientId

function BacktestParamsModal({
  isOpen,
  onClose,
  initialParams,
  project,
  onConfirmAndRun,
}: {
  isOpen: boolean
  onClose: () => void
  initialParams: Record<string, any>
  project: ResearchProject
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
    const sName = initialParams.strategy_name || 'strategy'
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

function BacktestParamsCard({
  params,
  project,
  onOpenModal,
}: {
  params: Record<string, any>
  project: ResearchProject
  onOpenModal: (params: Record<string, any>) => void
}) {
  const strategyName = params.strategy_name || 'strategy'
  const symbols = Array.isArray(params.symbols)
    ? params.symbols
    : [params.symbols || 'BTCUSDT']
  const startDate = params.start_date || '2024-01-01'
  const endDate = params.end_date || '2024-06-30'
  const capital = params.initial_balance ?? 10000
  const leverage = params.leverage ?? 1.0
  const timeframes = Array.isArray(params.timeframes)
    ? params.timeframes
    : [params.timeframes || '15m']
  const customParams = params.parameters || {}

  return (
    <div className="backtest-params-card" onClick={() => onOpenModal(params)}>
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
              {Object.entries(customParams).map(([k, v]) => (
                <span key={k} className="param-tag">
                  {k}: <b>{String(v)}</b>
                </span>
              ))}
            </div>
          </div>
        )}
      </div>

      <div className="params-card-footer">
        <span className="params-hint">💡 点击卡片可弹窗修改所有参数并确认回测</span>
        <button
          type="button"
          className="button mini primary config-modal-btn"
          onClick={e => {
            e.stopPropagation()
            onOpenModal(params)
          }}
        >
          <Sliders size={12} />
          查看与修改参数
        </button>
      </div>
    </div>
  )
}

function BacktestResultCard({
  msg,
  project,
  strategyName,
  busy,
  handleConfirmAnalysis,
  handleConfirmRepair,
  handleOpenParamsModal,
}: {
  msg: ResearchMessage
  project: ResearchProject
  strategyName: string
  busy: boolean
  handleConfirmAnalysis: (metrics?: Record<string, any>, stratName?: string) => void
  handleConfirmRepair: (errMsg?: string, stratName?: string) => void
  handleOpenParamsModal: (params: Record<string, any>) => void
}) {
  const res = msg.metadata?.result || msg.metadata?.backtest_result || {}
  const isSuccess = res.ok !== false && res.status !== 'FAILED'
  const metrics = res.metrics || {}
  const strat = res.strategy_name || strategyName || 'strategy'

  if (!isSuccess) {
    return (
      <div className="backtest-result-card-wrap">
        <div className="backtest-error-box in-dialog">
          <div className="error-box-header">
            <AlertCircle size={16} className="err-icon" />
            <b>策略「{strat}」回测执行失败</b>
          </div>
          <p className="err-msg-text">{res.error_message || '回测执行异常，请查看详细日志或进行代码修复'}</p>
          <div className="error-prompt-tip">
            ⚠️ 策略回测运行报错。是否确认让 DeepSeek Harness 进行代码修复？（【系统安全限制】：修复后不会自动重新回测）
          </div>
          <div className="err-action-row">
            <button
              type="button"
              className="button mini primary fix-btn"
              disabled={busy}
              onClick={() => handleConfirmRepair(res.error_message, strat)}
            >
              <Wrench size={12} /> 确认修复策略代码
            </button>
            <button
              type="button"
              className="button mini secondary"
              onClick={() => handleOpenParamsModal(res.arguments || res.backtest_params || { strategy_name: strat })}
            >
              <Sliders size={12} /> 重新调整回测参数
            </button>
            {res.run_id && (
              <Link className="button mini secondary" to={`/backtests/${res.run_id}`} target="_blank">
                查看详细日志 <ExternalLink size={11} />
              </Link>
            )}
          </div>
        </div>
      </div>
    )
  }

  const totalReturn = metrics.total_return != null ? `${Number(metrics.total_return).toFixed(2)}%` : '—'
  const sharpe =
    (metrics.sharpe_ratio ?? metrics.sharpe) != null
      ? Number(metrics.sharpe_ratio ?? metrics.sharpe).toFixed(2)
      : '—'
  const maxDd = metrics.max_drawdown != null ? `${Number(metrics.max_drawdown).toFixed(2)}%` : '—'
  const winRate = metrics.win_rate != null ? `${Number(metrics.win_rate).toFixed(1)}%` : '—'
  const totalTrades = metrics.total_trades ?? metrics.trades ?? '—'

  return (
    <div className="backtest-result-card-wrap">
      <div className="backtest-main-result-card">
        <div className="result-card-header">
          <div className="result-card-title">
            <CheckCircle2 size={18} className="text-green" />
            <span><b>策略回测完成</b> ({strat})</span>
          </div>
          <span className="badge ok">回测报告已就绪</span>
        </div>

        <div className="backtest-metrics-grid">
          <div className="metric-box">
            <span className="label">总收益率</span>
            <b className={`value ${(metrics.total_return ?? 0) >= 0 ? 'pos' : 'neg'}`}>{totalReturn}</b>
          </div>
          <div className="metric-box">
            <span className="label">夏普比率</span>
            <b className="value">{sharpe}</b>
          </div>
          <div className="metric-box">
            <span className="label">最大回撤</span>
            <b className="value neg">{maxDd}</b>
          </div>
          <div className="metric-box">
            <span className="label">胜率</span>
            <b className="value">{winRate}</b>
          </div>
          <div className="metric-box">
            <span className="label">总交易数</span>
            <b className="value">{totalTrades}</b>
          </div>
          {res.run_id && (
            <div className="metric-box action">
              <Link className="button mini primary detail-link-btn" to={`/backtests/${res.run_id}`} target="_blank">
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
              onClick={() => handleConfirmAnalysis(metrics, strat)}
            >
              <Sparkles size={12} /> 确认进行回测深度分析
            </button>
            <button
              type="button"
              className="button mini secondary"
              onClick={() => handleOpenParamsModal(res.arguments || res.backtest_params || { strategy_name: strat })}
            >
              <Sliders size={12} /> 调整参数重新回测
            </button>
          </div>
        </div>
      </div>
    </div>
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
  activeRun: ResearchRun | undefined,
  writingLog: ResearchWritingLog | null,
  project: ResearchProject | null
): MessageTurn[] {
  const turns: MessageTurn[] = []
  let currentTurn: MessageTurn = {
    id: 'turn-initial',
    processItems: [],
    responseMessages: [],
  }

  function addToolCall(msg: ResearchMessage) {
    const toolName = msg.metadata?.tool_name || '工具调用'
    const args = msg.metadata?.arguments || {}
    const reasoning = msg.metadata?.reasoning_content
    if (reasoning && reasoning.trim()) {
      currentTurn.processItems.push({
        id: `${msg.id}-thought`,
        type: 'thinking',
        thought: reasoning,
      })
    }

    const isBacktestCall = toolName === 'execute_backtest'
    const isWritingCall = toolName === 'write_strategy_code' || toolName === 'write_strategy_with_claude'
    const runForCall = isBacktestCall
      ? (runs.find(r => r.name.includes(args.strategy_name || '') || r.id === project?.latest_backtest_id) || activeRun)
      : null
    const isCallRunning = !!(runForCall && ['QUEUED', 'RUNNING', 'ANALYZING'].includes(runForCall.status))
    const isWritingActive = isWritingCall && (writingLog?.status === 'RUNNING')

    currentTurn.processItems.push({
      id: msg.id,
      type: 'tool',
      toolName,
      args,
      isCallRunning,
      isWritingActive,
      runForCall,
      rawCallMsg: msg,
    })
  }

  function addToolOutput(msg: ResearchMessage) {
    const toolName = msg.metadata?.tool_name || ''
    const res = msg.metadata?.result || {}
    const isSuccess = res.ok !== false && res.status !== 'FAILED' && (res.exit_code == null || res.exit_code === 0)

    const lastMatchingTool = [...currentTurn.processItems].reverse().find(
      item => item.type === 'tool' && (!item.result || Object.keys(item.result).length === 0) && (!item.toolName || item.toolName === toolName || !toolName)
    )

    if (lastMatchingTool) {
      lastMatchingTool.result = res
      lastMatchingTool.isSuccess = isSuccess
      lastMatchingTool.rawOutputMsg = msg
    } else {
      currentTurn.processItems.push({
        id: msg.id,
        type: 'tool',
        toolName,
        args: msg.metadata?.arguments || {},
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

    // 1. Proposals (code_approval and backtest_params) must be extracted to responseMessages
    // regardless of whether their role is 'assistant' or 'tool'
    if (msg.message_type === 'code_approval' || msg.metadata?.code_approval) {
      if (msg.role === 'tool' || msg.message_type === 'tool_output') {
        addToolOutput(msg)
      }
      if (msg.metadata?.reasoning_content?.trim()) {
        currentTurn.processItems.push({
          id: `${msg.id}-thought`,
          type: 'thinking',
          thought: msg.metadata.reasoning_content,
        })
      }
      const alreadyHasApproval = currentTurn.responseMessages.some(
        m => m.message_type === 'code_approval' || m.metadata?.code_approval
      )
      if (!alreadyHasApproval) {
        currentTurn.responseMessages.push(msg)
      }
      continue
    }

    if (msg.message_type === 'backtest_params' || msg.metadata?.backtest_params) {
      if (msg.role === 'tool' || msg.message_type === 'tool_output') {
        addToolOutput(msg)
      }
      if (msg.metadata?.reasoning_content?.trim()) {
        currentTurn.processItems.push({
          id: `${msg.id}-thought`,
          type: 'thinking',
          thought: msg.metadata.reasoning_content,
        })
      }
      const alreadyHasParams = currentTurn.responseMessages.some(
        m => m.message_type === 'backtest_params' || m.metadata?.backtest_params
      )
      if (!alreadyHasParams) {
        currentTurn.responseMessages.push(msg)
      }
      continue
    }

    if (
      msg.message_type === 'backtest_result' ||
      msg.metadata?.tool_name === 'execute_backtest' ||
      msg.metadata?.backtest_result
    ) {
      if (msg.role === 'tool' || msg.message_type === 'tool_output') {
        addToolOutput(msg)
      }
      if (msg.metadata?.reasoning_content?.trim()) {
        currentTurn.processItems.push({
          id: `${msg.id}-thought`,
          type: 'thinking',
          thought: msg.metadata.reasoning_content,
        })
      }
      const alreadyHasResult = currentTurn.responseMessages.some(
        m =>
          m.message_type === 'backtest_result' ||
          m.metadata?.tool_name === 'execute_backtest' ||
          m.metadata?.backtest_result
      )
      if (!alreadyHasResult) {
        currentTurn.responseMessages.push(msg)
      }
      continue
    }

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
  const isBacktest = toolName === 'execute_backtest'
  const isWriting = toolName === 'write_strategy_code' || toolName === 'write_strategy_with_claude'
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

          {isBacktest && isSuccess && res.metrics && (
            <>
              <div className="backtest-metrics-card">
                <div className="metric-box">
                  <span className="label">总收益率</span>
                  <b className={`value ${(res.metrics.total_return ?? 0) >= 0 ? 'pos' : 'neg'}`}>
                    {res.metrics.total_return != null ? `${Number(res.metrics.total_return).toFixed(2)}%` : '—'}
                  </b>
                </div>
                <div className="metric-box">
                  <span className="label">夏普比率</span>
                  <b className="value">
                    {(res.metrics.sharpe_ratio ?? res.metrics.sharpe) != null
                      ? Number(res.metrics.sharpe_ratio ?? res.metrics.sharpe).toFixed(2)
                      : '—'}
                  </b>
                </div>
                <div className="metric-box">
                  <span className="label">最大回撤</span>
                  <b className="value neg">
                    {res.metrics.max_drawdown != null ? `${Number(res.metrics.max_drawdown).toFixed(2)}%` : '—'}
                  </b>
                </div>
                <div className="metric-box">
                  <span className="label">胜率</span>
                  <b className="value">
                    {res.metrics.win_rate != null ? `${Number(res.metrics.win_rate).toFixed(1)}%` : '—'}
                  </b>
                </div>
                <div className="metric-box">
                  <span className="label">总交易数</span>
                  <b className="value">{res.metrics.total_trades ?? res.metrics.trades ?? '—'}</b>
                </div>
                {res.run_id && (
                  <div className="metric-box action">
                    <Link className="button mini" to={`/backtests/${res.run_id}`} target="_blank">
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
                    onClick={() => handleConfirmAnalysis(res.metrics, res.strategy_name || strategyName || '')}
                  >
                    <Sparkles size={12} /> 确认进行回测深度分析
                  </button>
                  <button
                    type="button"
                    className="button mini secondary"
                    onClick={() => {
                      handleOpenParamsModal(res.arguments || res.backtest_params || {})
                    }}
                  >
                    <Sliders size={12} /> 调整参数重新回测
                  </button>
                </div>
              </div>
            </>
          )}

          {isBacktest && !isSuccess && hasResult && (
            <div className="backtest-error-box">
              <div className="error-box-header">
                <AlertCircle size={15} className="err-icon" />
                <b>回测执行失败报错：</b>
              </div>
              <p className="err-msg-text">{res.error_message || '执行过程出现异常'}</p>
              <div className="error-prompt-tip">
                ⚠️ 策略回测运行报错。是否确认让 DeepSeek Harness 进行代码修复？（【系统安全限制】：本次仅修复策略代码，修复后不会自动重新回测）
              </div>
              <div className="err-action-row">
                <button
                  type="button"
                  className="button mini primary fix-btn"
                  disabled={busy}
                  onClick={() => handleConfirmRepair(res.error_message, res.strategy_name || strategyName || '')}
                >
                  <Wrench size={12} /> 确认修复策略代码
                </button>
                <button
                  type="button"
                  className="button mini secondary"
                  onClick={() => {
                    handleOpenParamsModal(res.arguments || res.backtest_params || {})
                  }}
                >
                  <Sliders size={12} /> 重新调整回测参数
                </button>
                {res.run_id && (
                  <Link className="button mini secondary" to={`/backtests/${res.run_id}`} target="_blank">
                    查看日志 <ExternalLink size={11} />
                  </Link>
                )}
              </div>
            </div>
          )}

          {isWriting && hasResult && (
            <div className="code-success-bar">
              <span>{res.message || (isSuccess ? '代码已保存至 strategies 目录' : '代码编写失败')}</span>
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
                {isSuccess && (
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
                )}
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
  if (!processItems || processItems.length === 0) return null

  const toolItems = processItems.filter(p => p.type === 'tool')
  const thinkingItems = processItems.filter(p => p.type === 'thinking')
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
            {processItems.map((item, idx) => {
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

function CodeApprovalCard({
  data,
  onApprove,
  onModify,
  disabled,
}: {
  data: CodeApprovalData
  onApprove: (data: CodeApprovalData) => void
  onModify: (data: CodeApprovalData) => void
  disabled?: boolean
}) {
  const [approved, setApproved] = useState(false)

  return (
    <div className="code-approval-card">
      <div className="code-approval-header">
        <div className="code-approval-title-wrap">
          <div className="code-approval-badge">
            <Sparkles size={14} />
            <span>待用户审批</span>
          </div>
          <h4>策略编码方案已就绪 · 请确认是否开始写码</h4>
        </div>
        {data.strategy_name && (
          <div className="code-approval-slug">
            <code>{data.strategy_name}</code>
          </div>
        )}
      </div>

      <div className="code-approval-body">
        {data.strategy_summary && (
          <div className="code-approval-summary">
            <p>{data.strategy_summary}</p>
          </div>
        )}

        {data.key_rules && data.key_rules.length > 0 && (
          <div className="code-approval-rules">
            <div className="code-approval-rules-title">核心逻辑与规则清单：</div>
            <ul className="code-approval-rules-list">
              {data.key_rules.map((rule, idx) => (
                <li key={idx} className="code-approval-rule-item">
                  <CheckCircle2 size={14} className="rule-check-icon" />
                  <span>{rule}</span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {data.parameter_specs && Object.keys(data.parameter_specs).length > 0 && (
          <div className="code-approval-params">
            <div className="code-approval-params-title">预设参数列表：</div>
            <div className="code-approval-param-tags">
              {Object.entries(data.parameter_specs).map(([k, v]) => (
                <span key={k} className="code-approval-param-tag">
                  <b>{k}</b>: {String(v)}
                </span>
              ))}
            </div>
          </div>
        )}
      </div>

      <div className="code-approval-footer">
        <div className="code-approval-tip">
          <AlertCircle size={13} />
          <span>请核对上述策略设计逻辑。批准后 DeepSeek Harness 将开始编写策略代码。</span>
        </div>
        <div className="code-approval-actions">
          <button
            type="button"
            className="button mini secondary code-approval-btn modify"
            disabled={disabled || approved}
            onClick={() => onModify(data)}
          >
            修改策略设计
          </button>
          <button
            type="button"
            className="button mini primary code-approval-btn approve"
            disabled={disabled || approved}
            onClick={() => {
              setApproved(true)
              onApprove(data)
            }}
          >
            <Play size={14} />
            <span>批准并开始编写代码</span>
          </button>
        </div>
      </div>
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
  const[project,setProject]=useState<ResearchProject|null>(null)
  const[messages,setMessages]=useState<ResearchMessage[]>([])
  const[runs,setRuns]=useState<ResearchRun[]>([])
  const[strategyCode,setStrategyCode]=useState<string>('')
  const[strategyName,setStrategyName]=useState<string>('')

  const[input,setInput]=useState('')
  const[busy,setBusy]=useState(false)
  const[error,setError]=useState('')
  const[creating,setCreating]=useState(false)
  const[newTitle,setNewTitle]=useState('')
  const[newIdea,setNewIdea]=useState('')

  // Backtest Parameter Modal state
  const[paramsModalOpen,setParamsModalOpen]=useState(false)
  const[activeModalParams,setActiveModalParams]=useState<Record<string,any>>({})

  // Strategy writing log state
  const[writingLog,setWritingLog]=useState<ResearchWritingLog|null>(null)

  // Real-time Hermes thinking status
  const[thinkingStatus,setThinkingStatus]=useState<ResearchThinkingStatus|null>(null)

  // Right drawer state
  const[drawerOpen,setDrawerOpen]=useState(false)
  const[drawerTab,setDrawerTab]=useState<'code'|'backtests'|'writer_log'>('code')
  const[expandedTools,setExpandedTools]=useState<Record<string,boolean>>({})

  // Export log state
  const[exportMenuOpen,setExportMenuOpen]=useState(false)
  const[exportingLog,setExportingLog]=useState(false)

  const handleExportLog = async (fmt: 'markdown' | 'json') => {
    if (!project) return
    setExportingLog(true)
    setExportMenuOpen(false)
    try {
      await api.downloadResearchExport(project.id, fmt)
    } catch (err: any) {
      alert(err?.message || '导出日志失败')
    } finally {
      setExportingLog(false)
    }
  }

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
      const value=await api.researchProjects()
      setProjects(value)
      return value
    }catch(e){
      setError((e as Error).message)
      return []
    }
  }

  async function open(item:ResearchProject){
    setError('')
    setStrategyCode('')
    setStrategyName('')
    setWritingLog(null)
    setThinkingStatus(null)
    try{
      const[m,r,fresh,wLog,tStatus]=await Promise.all([
        api.researchMessages(item.id),
        api.researchRuns(item.id),
        api.researchProject(item.id),
        api.researchWritingLog(item.id).catch(()=>null),
        api.researchThinkingStatus(item.id).catch(()=>null),
      ])
      setMessages(m)
      setRuns(r)
      setProject(fresh)
      if(wLog)setWritingLog(wLog)
      if(tStatus)setThinkingStatus(tStatus)
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

  function handleApproveCode(data:CodeApprovalData){
    if(!project||busy)return
    const stratName =
      (data.strategy_name && data.strategy_name !== 'strategy' && data.strategy_name !== 'custom_strategy')
        ? data.strategy_name
        : (strategyName || 'custom_strategy')
    const rulesSummary = data.key_rules && data.key_rules.length > 0 ? `\n核心规则要点：\n${data.key_rules.map(r => `- ${r}`).join('\n')}` : ''
    const paramsSummary = data.parameter_specs && Object.keys(data.parameter_specs).length > 0 ? `\n预设参数配置：${JSON.stringify(data.parameter_specs, null, 2)}` : ''
    const approvePrompt =
      `【已批准策略「${stratName}」的设计方案，请立即调用 write_strategy_code 工具编写策略代码】：\n` +
      `- strategy_name: "${stratName}"\n` +
      `- instructions: "请编写策略 ${stratName} 的完整代码文件 backend/app/strategies/${stratName}.py，严格遵循 NautilusTrader 开发规范并通过 4 级 Pre-Flight 验证。${rulesSummary}${paramsSummary}"`

    const tempId=generateUUID()
    const optimisticMsg:ResearchMessage={
      id:tempId,
      role:'user',
      content:approvePrompt,
      message_type:'message',
      metadata:{},
      created_at:new Date().toISOString(),
    }
    setMessages(prev=>[...prev,optimisticMsg])
    setBusy(true)
    setError('')
    autoScrollRef.current = true
    setShowScrollBottom(false)
    setTimeout(() => scrollToBottom(true), 30)
    api
      .sendResearchMessage(project.id,approvePrompt)
      .then(async()=>{
        const[m,r,fresh,wLog,tStatus]=await Promise.all([
          api.researchMessages(project.id),
          api.researchRuns(project.id),
          api.researchProject(project.id),
          api.researchWritingLog(project.id).catch(()=>null),
          api.researchThinkingStatus(project.id).catch(()=>null),
        ])
        setMessages(m)
        setRuns(r)
        setProject(fresh)
        if(wLog)setWritingLog(wLog)
        if(tStatus)setThinkingStatus(tStatus)
      })
      .catch(e=>{
        setError((e as Error).message)
        setBusy(false)
      })
  }

  function handleModifyCode(data:CodeApprovalData){
    const stratName =
      (data.strategy_name && data.strategy_name !== 'strategy' && data.strategy_name !== 'custom_strategy')
        ? data.strategy_name
        : (strategyName || 'custom_strategy')
    setInput(`关于策略「${stratName}」的设计方案，我想调整以下逻辑：\n- `)
    textareaRef.current?.focus()
  }

  function handleConfirmRepair(errorMessage?: string, stratName?: string){
    if(!project||busy)return
    const sName =
      (stratName && stratName !== 'strategy' && stratName !== 'custom_strategy')
        ? stratName
        : (strategyName || 'custom_strategy')
    const errText = errorMessage || '回测执行异常'
    const repairPrompt =
      `针对策略「${sName}」回测运行报错：\n${errText}\n\n`+
      `请深入分析报错原因，对策略「${sName}」进行1次代码修复并保存策略文件。\n\n`+
      `【系统安全限制（强制）】：\n`+
      `1. 本次操作只修改并保存策略代码；\n`+
      `2. 严禁在修复后自动调用 execute_backtest 重新回测，严禁擅自生成回测参数卡片；\n`+
      `3. 修复完成后请简要总结修改点，并等待用户进一步确认。`

    const tempId=generateUUID()
    const optimisticMsg:ResearchMessage={
      id:tempId,
      role:'user',
      content:repairPrompt,
      message_type:'message',
      metadata:{},
      created_at:new Date().toISOString(),
    }
    setMessages(prev=>[...prev,optimisticMsg])
    setInput('')
    setBusy(true)
    setError('')
    autoScrollRef.current = true
    setShowScrollBottom(false)
    setTimeout(() => scrollToBottom(true), 30)
    api
      .sendResearchMessage(project.id,repairPrompt)
      .then(async()=>{
        const[m,r,fresh,wLog,tStatus]=await Promise.all([
          api.researchMessages(project.id),
          api.researchRuns(project.id),
          api.researchProject(project.id),
          api.researchWritingLog(project.id).catch(()=>null),
          api.researchThinkingStatus(project.id).catch(()=>null),
        ])
        setMessages(m)
        setRuns(r)
        setProject(fresh)
        if(wLog)setWritingLog(wLog)
        if(tStatus)setThinkingStatus(tStatus)
      })
      .catch(e=>{
        setError((e as Error).message)
        setBusy(false)
      })
  }

  function handleConfirmAnalysis(metrics?: Record<string, any>, stratName?: string){
    if(!project||busy)return
    const sName =
      (stratName && stratName !== 'strategy' && stratName !== 'custom_strategy')
        ? stratName
        : (strategyName || 'custom_strategy')
    const ret = metrics?.total_return != null ? `${Number(metrics.total_return).toFixed(2)}%` : '—'
    const sharpe =
      (metrics?.sharpe_ratio ?? metrics?.sharpe) != null
        ? Number(metrics?.sharpe_ratio ?? metrics?.sharpe).toFixed(2)
        : '—'
    const dd = metrics?.max_drawdown != null ? `${Number(metrics.max_drawdown).toFixed(2)}%` : '—'
    const winRate = metrics?.win_rate != null ? `${Number(metrics.win_rate).toFixed(1)}%` : '—'
    const totalTrades = metrics?.total_trades ?? metrics?.trades ?? '—'

    const analysisPrompt =
      `针对策略「${sName}」本次回测结果（总收益率: ${ret}, 夏普比率: ${sharpe}, 最大回撤: ${dd}, 胜率: ${winRate}, 总交易数: ${totalTrades}），请进行1次深度回测归因分析。\n\n`+
      `请剖析：\n`+
      `1. 收益与亏损的核心来源及行情特征适应性；\n`+
      `2. 胜率、盈亏比与极端亏损原因分析；\n`+
      `3. 策略逻辑的潜在改进方向。\n\n`+
      `【系统安全限制（强制）】：\n`+
      `本次仅进行原因与指标归因分析，严禁修改策略代码，严禁调用 execute_backtest 重新回测。`

    const tempId=generateUUID()
    const optimisticMsg:ResearchMessage={
      id:tempId,
      role:'user',
      content:analysisPrompt,
      message_type:'message',
      metadata:{},
      created_at:new Date().toISOString(),
    }
    setMessages(prev=>[...prev,optimisticMsg])
    setInput('')
    setBusy(true)
    setError('')
    autoScrollRef.current = true
    setShowScrollBottom(false)
    setTimeout(() => scrollToBottom(true), 30)
    api
      .sendResearchMessage(project.id,analysisPrompt)
      .then(async()=>{
        const[m,r,fresh,wLog,tStatus]=await Promise.all([
          api.researchMessages(project.id),
          api.researchRuns(project.id),
          api.researchProject(project.id),
          api.researchWritingLog(project.id).catch(()=>null),
          api.researchThinkingStatus(project.id).catch(()=>null),
        ])
        setMessages(m)
        setRuns(r)
        setProject(fresh)
        if(wLog)setWritingLog(wLog)
        if(tStatus)setThinkingStatus(tStatus)
      })
      .catch(e=>{
        setError((e as Error).message)
        setBusy(false)
      })
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
    const symbolsArr = Array.isArray(params.symbols) ? params.symbols : [params.symbols || 'BTCUSDT']
    const confirmPrompt=
      `【回测参数已确认，请立即启动 QuantLab 官方回测引擎】：\n`+
      `【系统强制指令】：严禁使用 terminal/bash 终端运行命令！你必须通过 QuantLab 官方回测系统执行回测。请立即在回复中输出以下标准的 tool_call 机器块启动回测：\n\n`+
      `\`\`\`tool_call\n`+
      `{\n`+
      `  "name": "execute_backtest",\n`+
      `  "arguments": {\n`+
      `    "strategy_name": "${params.strategy_name||'strategy'}",\n`+
      `    "symbols": ${JSON.stringify(symbolsArr)},\n`+
      `    "start_date": "${params.start_date||'2024-01-01'}",\n`+
      `    "end_date": "${params.end_date||'2024-06-30'}",\n`+
      `    "initial_balance": ${params.initial_balance??10000.0},\n`+
      `    "leverage": ${params.leverage??1.0},\n`+
      `    "parameters": ${JSON.stringify(params.parameters||{})}\n`+
      `  }\n`+
      `}\n`+
      `\`\`\`\n\n`+
      `请立即输出上述 tool_call 块启动 QuantLab 回测。`

    const tempId=generateUUID()
    const optimisticMsg:ResearchMessage={
      id:tempId,
      role:'user',
      content:confirmPrompt,
      message_type:'message',
      metadata:{},
      created_at:new Date().toISOString(),
    }
    setMessages(prev=>[...prev,optimisticMsg])
    setInput('')
    setBusy(true)
    setError('')
    autoScrollRef.current = true
    setShowScrollBottom(false)
    setTimeout(() => scrollToBottom(true), 30)
    api
      .sendResearchMessage(project.id,confirmPrompt)
      .then(async()=>{
        const[m,r,fresh,wLog,tStatus]=await Promise.all([
          api.researchMessages(project.id),
          api.researchRuns(project.id),
          api.researchProject(project.id),
          api.researchWritingLog(project.id).catch(()=>null),
          api.researchThinkingStatus(project.id).catch(()=>null),
        ])
        setMessages(m)
        setRuns(r)
        setProject(fresh)
        if(wLog)setWritingLog(wLog)
        if(tStatus)setThinkingStatus(tStatus)
        setBusy(true)
      })
      .catch(e=>{
        setError((e as Error).message)
        setBusy(false)
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
  },[messages,busy])

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
        const[m,r,fresh,wLog,tStatus]=await Promise.all([
          api.researchMessages(project.id),
          api.researchRuns(project.id),
          api.researchProject(project.id),
          api.researchWritingLog(project.id).catch(()=>null),
          api.researchThinkingStatus(project.id).catch(()=>null),
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
        const runsActive=r.some(run=>['QUEUED','RUNNING','ANALYZING'].includes(run.status))
        const writerActive=wLog?.status==='RUNNING'
        const thinkingActive=tStatus?.status==='THINKING'||tStatus?.status==='TOOL_RUNNING'||tStatus?.status==='GENERATING'
        if(fresh.is_busy||runsActive||writerActive||thinkingActive){
          setBusy(true)
        }else{
          setBusy(false)
          loadStrategy(project.id)
        }
      }catch(err: any){
        const msg = String(err?.message || '')
        if (msg.includes('不存在') || msg.includes('404')) {
          setProject(null)
          setMessages([])
          setRuns([])
          setWritingLog(null)
          setThinkingStatus(null)
          setStrategyCode('')
          reloadList()
        }
      }
    }, pollInterval)
    return()=>window.clearInterval(timer)
  },[project?.id,project?.is_busy,busy,runs.map(r=>r.status).join(','),writingLog?.status,thinkingStatus?.status])

  async function handleCreate(e:React.FormEvent){
    e.preventDefault()
    if(!newTitle.trim()||busy)return
    setBusy(true)
    setError('')
    try{
      const created=await api.createResearch(newTitle.trim(),newIdea.trim(),clientId())
      setCreating(false)
      setNewTitle('')
      setNewIdea('')
      await reloadList()
      await open(created)
    }catch(e){
      setError((e as Error).message)
      setBusy(false)
    }
  }

  async function handleSend(e?:React.FormEvent){
    if(e)e.preventDefault()
    const text=input.trim()
    if(!project||!text||busy)return
    const tempId=generateUUID()
    const optimisticMsg:ResearchMessage={
      id:tempId,
      role:'user',
      content:text,
      message_type:'message',
      metadata:{},
      created_at:new Date().toISOString(),
    }
    setMessages(prev=>[...prev,optimisticMsg])
    setInput('')
    setBusy(true)
    setError('')
    autoScrollRef.current = true
    setShowScrollBottom(false)
    setTimeout(() => scrollToBottom(true), 30)
    try{
      await api.sendResearchMessage(project.id,text)
      const[m,r,fresh,wLog,tStatus]=await Promise.all([
        api.researchMessages(project.id),
        api.researchRuns(project.id),
        api.researchProject(project.id),
        api.researchWritingLog(project.id).catch(()=>null),
        api.researchThinkingStatus(project.id).catch(()=>null),
      ])
      setMessages(m)
      setRuns(r)
      setProject(fresh)
      if(wLog)setWritingLog(wLog)
      if(tStatus)setThinkingStatus(tStatus)
      setBusy(true)
    }catch(e){
      setError((e as Error).message)
      setBusy(false)
    }
  }

  async function handleDshRun(e?: React.FormEvent) {
    if (e) e.preventDefault()
    const text = input.trim()
    if (!project || !text || busy) return
    const tempId = generateUUID()
    const optimisticMsg: ResearchMessage = {
      id: tempId,
      role: 'user',
      content: `⚡ [DSH 多 Agent 星型闭环] ${text}`,
      message_type: 'message',
      metadata: { is_dsh_run: true },
      created_at: new Date().toISOString(),
    }
    setMessages(prev => [...prev, optimisticMsg])
    setInput('')
    setBusy(true)
    setError('')
    autoScrollRef.current = true
    setShowScrollBottom(false)
    setTimeout(() => scrollToBottom(true), 30)
    try {
      await api.runDshPipeline(project.id, text)
      const [m, r, fresh, wLog, tStatus] = await Promise.all([
        api.researchMessages(project.id),
        api.researchRuns(project.id),
        api.researchProject(project.id),
        api.researchWritingLog(project.id).catch(() => null),
        api.researchThinkingStatus(project.id).catch(() => null),
      ])
      setMessages(m)
      setRuns(r)
      setProject(fresh)
      if (wLog) setWritingLog(wLog)
      if (tStatus) setThinkingStatus(tStatus)
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(false)
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
      handleSend()
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

  // Compute grouped turns for Hermes thinking & tool execution bundling
  const turns = useMemo(
    () => groupMessagesIntoTurns(messages, runs, activeRun, writingLog, project),
    [messages, runs, activeRun, writingLog, project]
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
            <button className="button primary new-session-btn" onClick={()=>setCreating(true)}>
              <Plus size={14}/>新建研究
            </button>
          </div>

          <div className="session-list">
            {projects.map(item=>(
              <div
                key={item.id}
                className={`session-item ${item.id===project?.id?'active':''}`}
                onClick={()=>open(item)}
              >
                <div className="session-item-body">
                  <span className="session-item-title">{item.title}</span>
                  <div className="session-item-meta">
                    <time>{new Date(item.updated_at).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})}</time>
                    <span className={`status-tag ${item.status.toLowerCase()}`}>
                      {item.status==='ARCHIVED'?'已归档':'进行中'}
                    </span>
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
                  <div className="header-export-container" style={{ position: 'relative' }}>
                    <button
                      className="button tool-btn header-export-btn"
                      title="导出策略研究记录与 DSH 调试日志"
                      onClick={() => setExportMenuOpen((o) => !o)}
                      disabled={exportingLog}
                    >
                      {exportingLog ? <Loader2 size={14} className="spin text-cyan" /> : <FileDown size={14} />}
                      导出日志
                      <ChevronDown size={11} />
                    </button>
                    {exportMenuOpen && (
                      <div className="header-export-dropdown">
                        <button
                          type="button"
                          className="header-export-item"
                          onClick={() => handleExportLog('markdown')}
                        >
                          <FileText size={13} className="text-cyan" />
                          <span>导出 Markdown 报告 (.md)</span>
                        </button>
                        <button
                          type="button"
                          className="header-export-item"
                          onClick={() => handleExportLog('json')}
                        >
                          <FileJson size={13} className="text-amber" />
                          <span>导出 JSON 原始数据 (.json)</span>
                        </button>
                      </div>
                    )}
                  </div>
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

              {/* Recent Backtest Error Alert Banner */}
              {latestFailedRun && (
                <div className="failed-backtest-alert-banner">
                  <div className="alert-left">
                    <AlertCircle size={16} className="alert-icon" />
                    <div className="alert-text">
                      <b>回测执行报错 ({latestFailedRun.name})</b>
                      <span>{latestFailedRun.error_message || '未知错误，请检查日志与数据'}</span>
                    </div>
                  </div>
                  <div className="alert-actions">
                    <button
                      className="button mini danger"
                      disabled={busy}
                      onClick={() => handleConfirmRepair(latestFailedRun.error_message || '', latestFailedRun.name || '')}
                    >
                      <Wrench size={12} /> 确认修复策略代码
                    </button>
                    <button
                      className="button mini secondary"
                      onClick={() => {
                        if (activeModalParams && Object.keys(activeModalParams).length > 0) {
                          setParamsModalOpen(true)
                        } else if (latestFailedRun.config) {
                          setActiveModalParams(latestFailedRun.config)
                          setParamsModalOpen(true)
                        }
                      }}
                    >
                      <Sliders size={12} /> 重新配置参数
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
                        if (msg.message_type === 'code_approval' || msg.metadata?.code_approval) {
                          const approvalData: CodeApprovalData =
                            msg.metadata?.code_approval || msg.metadata?.result?.approval_data || msg.metadata?.arguments || {}
                          let cleanContent = msg.content
                            ? msg.content.replace(/```(?:code_approval|json:code_approval)[\s\S]*?```/gi, '').trim()
                            : ''
                          if (cleanContent.startsWith('{') && cleanContent.endsWith('}')) {
                            cleanContent = ''
                          }
                          return (
                            <div key={msg.id} className="chat-msg-wrap">
                              {cleanContent && (
                                <article className={`chat-message ${msg.role === 'tool' ? 'assistant' : msg.role}`}>
                                  <div className="message-avatar">
                                    <Bot size={16} />
                                  </div>
                                  <div className="message-content">
                                    <div className="message-author">
                                      <b>{getAgentDisplayName(msg)}</b>
                                      <time>{new Date(msg.created_at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}</time>
                                    </div>
                                    <div className="message-markdown">
                                      <ReactMarkdown remarkPlugins={[remarkGfm]}>
                                        {cleanContent}
                                      </ReactMarkdown>
                                    </div>
                                  </div>
                                </article>
                              )}
                              <CodeApprovalCard
                                data={approvalData}
                                onApprove={handleApproveCode}
                                onModify={handleModifyCode}
                                disabled={busy}
                              />
                            </div>
                          )
                        }

                        if (msg.message_type === 'backtest_params' || msg.metadata?.backtest_params) {
                          const bp =
                            msg.metadata?.backtest_params || msg.metadata?.result?.backtest_params || msg.metadata?.arguments || {}
                          let cleanContent = msg.content
                            ? msg.content.replace(/```(?:backtest_params|json:backtest_params)[\s\S]*?```/gi, '').trim()
                            : ''
                          if (cleanContent.startsWith('{') && cleanContent.endsWith('}')) {
                            cleanContent = ''
                          }
                          return (
                            <div key={msg.id} className="chat-msg-wrap">
                              {cleanContent && (
                                <article className={`chat-message ${msg.role === 'tool' ? 'assistant' : msg.role}`}>
                                  <div className="message-avatar">
                                    <Bot size={16} />
                                  </div>
                                  <div className="message-content">
                                    <div className="message-author">
                                      <b>{getAgentDisplayName(msg)}</b>
                                      <time>{new Date(msg.created_at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}</time>
                                    </div>
                                    <div className="message-markdown">
                                      <ReactMarkdown remarkPlugins={[remarkGfm]}>
                                        {cleanContent}
                                      </ReactMarkdown>
                                    </div>
                                  </div>
                                </article>
                              )}
                              {project && (
                                <BacktestParamsCard
                                  params={bp}
                                  project={project}
                                  onOpenModal={handleOpenParamsModal}
                                />
                              )}
                            </div>
                          )
                        }

                        if (
                          msg.message_type === 'backtest_result' ||
                          msg.metadata?.tool_name === 'execute_backtest' ||
                          msg.metadata?.backtest_result
                        ) {
                          return (
                            <div key={msg.id} className="chat-msg-wrap">
                              {project && (
                                <BacktestResultCard
                                  msg={msg}
                                  project={project}
                                  strategyName={strategyName}
                                  busy={busy}
                                  handleConfirmAnalysis={handleConfirmAnalysis}
                                  handleConfirmRepair={handleConfirmRepair}
                                  handleOpenParamsModal={handleOpenParamsModal}
                                />
                              )}
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
                            </div>
                          </article>
                        )
                      })}
                    </div>
                  )
                })}

                {/* Live Backtest Progress or Strategy Writing Live Progress or Live Thinking card */}
                {busy && (
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
                            <b>DeepSeek Harness 量化主控正在深度思考与推理</b>
                            <span className="live-thinking-state-badge">
                              <Loader2 size={11} className="spin" />
                              {thinkingStatus?.status === 'TOOL_RUNNING' ? '工具调度中' : '量化推理中'}
                            </span>
                          </div>
                        </div>

                        <div className="live-stage-desc thinking">
                          <Sparkles size={13} className="text-cyan sparkle-spin" />
                          <span>{thinkingStatus?.step || '正在深度研讨量化假设、指标计算与策略规则…'}</span>
                        </div>

                        {thinkingStatus?.thought && (
                          <div className="live-thinking-stream-box">
                            <div className="thinking-stream-label">
                              <BrainCircuit size={12} />
                              <span>实时思维链流：</span>
                            </div>
                            <div className="thinking-stream-content">
                              <ReactMarkdown remarkPlugins={[remarkGfm]}>
                                {thinkingStatus.thought}
                              </ReactMarkdown>
                            </div>
                          </div>
                        )}
                      </div>
                    </div>
                  )
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
                {/* Prompt Suggestions */}
                <div className="quick-prompt-bar">
                  <button
                    className="quick-chip"
                    onClick={()=>setInput('方案很清晰，请开始编写策略代码。')}
                  >
                    🚀 编写策略代码
                  </button>
                  <button
                    className="quick-chip"
                    onClick={()=>setInput('请为该策略生成回测参数。')}
                  >
                    📈 生成回测参数
                  </button>
                  <button
                    className="quick-chip"
                    onClick={()=>setInput('针对策略回测报错，请进行1次策略代码修复（只修改代码，禁止自动回测）。')}
                  >
                    🛠️ 修复策略代码
                  </button>
                  <button
                    className="quick-chip"
                    onClick={()=>setInput('请对本次回测结果进行1次深度归因分析，详细剖析盈利/亏损原因（只分析原因，禁止修改代码和回测）。')}
                  >
                    📊 回测结果分析
                  </button>
                </div>

                <form className="chat-composer" onSubmit={handleSend}>
                  <textarea
                    ref={textareaRef}
                    rows={2}
                    placeholder="输入你的策略设想、改进建议或回测要求（Enter 发送，Shift+Enter 换行）…"
                    value={input}
                    onChange={e=>setInput(e.target.value)}
                    onKeyDown={handleKeyDown}
                    onCompositionStart={()=>{isComposingRef.current=true}}
                    onCompositionEnd={()=>{isComposingRef.current=false}}
                    disabled={busy||project.status==='ARCHIVED'}
                  />
                  <button
                    type="submit"
                    className="send-btn"
                    disabled={busy||!input.trim()||project.status==='ARCHIVED'}
                  >
                    {busy?<Loader2 size={16} className="spin"/>:<Send size={16}/>}
                  </button>
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
          onConfirmAndRun={handleConfirmBacktestParams}
        />
      )}

      {/* New Research Modal */}
      {creating&&(
        <div className="modal-backdrop">
          <section className="modal create-research-modal">
            <button className="modal-close" onClick={()=>setCreating(false)}><X size={16}/></button>
            <h2>新建策略研究</h2>
            <p className="muted">开启一个全新的对话会话，由 DeepSeek Harness 协同完成策略构想、写码与回测。</p>
            <form onSubmit={handleCreate} className="stack-form">
              <label>
                研究主题名称
                <input
                  type="text"
                  placeholder="例如：BTC 15m 均线动量突破与 ATR 止损"
                  value={newTitle}
                  onChange={e=>setNewTitle(e.target.value)}
                  required
                  autoFocus
                />
              </label>
              <label>
                初始策略想法（可选）
                <textarea
                  rows={3}
                  placeholder="可在此描述你的交易逻辑、指标构想、预期适用市场等…"
                  value={newIdea}
                  onChange={e=>setNewIdea(e.target.value)}
                />
              </label>
              <div className="modal-actions">
                <button type="button" className="button" onClick={()=>setCreating(false)}>取消</button>
                <button type="submit" className="button primary" disabled={busy||!newTitle.trim()}>
                  {busy?<Loader2 size={14} className="spin"/>:'创建并开启研讨'}
                </button>
              </div>
            </form>
          </section>
        </div>
      )}
    </div>
  )
}

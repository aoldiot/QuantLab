import {
  CalendarDays,
  CheckCircle2,
  Download,
  HardDrive,
  History,
  Info,
  LoaderCircle,
  RefreshCw,
  TerminalSquare,
  Trash2,
} from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api'

const intervals = ['1m', '3m', '5m', '15m', '30m', '1h', '2h', '4h', '6h', '8h', '12h', '1d', '3d', '1w', '1M']
const yesterday = () => {
  const d = new Date()
  d.setUTCDate(d.getUTCDate() - 1)
  return d.toISOString().slice(0, 10)
}
const monthAgo = () => {
  const d = new Date()
  d.setUTCMonth(d.getUTCMonth() - 1)
  return d.toISOString().slice(0, 10)
}

const STORAGE_FORM_KEY = 'quantlab_download_form_v1'
const STORAGE_ACTIVE_TASK_KEY = 'quantlab_active_download_id_v1'

interface TaskLog {
  time: string
  level: string
  message: string
}

interface Task {
  id: string
  status: string
  stage: string
  progress: number
  rows: number
  completed_files: number
  total_files: number
  downloaded_files: number
  skipped_files: number
  missing_files: number
  catalog_path?: string
  error?: string
  created_at?: string
  updated_at?: string
  request?: {
    market_type?: string
    mode?: string
    intervals?: string[]
    start_date?: string
    end_date?: string
    symbols?: string[]
  }
  logs: TaskLog[]
}

function loadSavedForm() {
  try {
    const raw = localStorage.getItem(STORAGE_FORM_KEY)
    if (!raw) return null
    return JSON.parse(raw)
  } catch {
    return null
  }
}

export default function DataDownloads() {
  const savedForm = useMemo(() => loadSavedForm(), [])

  const [market, setMarket] = useState(savedForm?.market ?? 'um')
  const [mode, setMode] = useState(savedForm?.mode ?? 'incremental')
  const [selected, setSelected] = useState<string[]>(savedForm?.selected ?? ['1m'])
  const [start, setStart] = useState(savedForm?.start ?? monthAgo())
  const [end, setEnd] = useState(savedForm?.end ?? yesterday())
  const [symbols, setSymbols] = useState(savedForm?.symbols ?? 'BTCUSDT, ETHUSDT')
  const [catalogPath, setCatalogPath] = useState(savedForm?.catalogPath ?? '')

  const [task, setTask] = useState<Task | null>(null)
  const [taskList, setTaskList] = useState<Task[]>([])
  const [busySymbols, setBusySymbols] = useState(false)
  const [error, setError] = useState('')
  const logStreamRef = useRef<HTMLDivElement | null>(null)

  // Save form settings to localStorage
  useEffect(() => {
    try {
      localStorage.setItem(
        STORAGE_FORM_KEY,
        JSON.stringify({ market, mode, selected, start, end, symbols, catalogPath }),
      )
    } catch {
      // Ignore localStorage errors
    }
  }, [market, mode, selected, start, end, symbols, catalogPath])

  // Refresh task list and restore latest/active task on mount
  const refreshTasks = async () => {
    try {
      const list = await api.dataDownloads()
      setTaskList(list)
      const savedTaskId = localStorage.getItem(STORAGE_ACTIVE_TASK_KEY)
      if (savedTaskId) {
        const found = list.find((t) => t.id === savedTaskId)
        if (found) {
          const detail = await api.dataDownload(found.id)
          setTask(detail)
          return
        }
      }
      if (list.length > 0) {
        const detail = await api.dataDownload(list[0].id)
        setTask(detail)
        localStorage.setItem(STORAGE_ACTIVE_TASK_KEY, detail.id)
      }
    } catch (e) {
      console.warn('获取下载任务历史失败', e)
    }
  }

  useEffect(() => {
    refreshTasks()
  }, [])

  const running = Boolean(task && ['queued', 'running'].includes(task.status))

  // Polling for running task
  useEffect(() => {
    if (!running || !task) return
    const timer = window.setInterval(async () => {
      try {
        const updated = await api.dataDownload(task.id)
        setTask(updated)
        // If task completed, refresh list
        if (['completed', 'failed'].includes(updated.status)) {
          api.dataDownloads().then(setTaskList).catch(() => {})
        }
      } catch (e) {
        setError(e instanceof Error ? e.message : '查询任务状态失败')
      }
    }, 1000)
    return () => clearInterval(timer)
  }, [running, task?.id])

  // Auto-scroll logs to bottom
  useEffect(() => {
    if (logStreamRef.current) {
      logStreamRef.current.scrollTop = logStreamRef.current.scrollHeight
    }
  }, [task?.logs.length])

  const symbolList = useMemo(
    () => symbols.split(/[,;\s]+/).map((x: string) => x.trim()).filter(Boolean),
    [symbols],
  )

  function toggle(value: string) {
    setSelected((current) => (current.includes(value) ? current.filter((x) => x !== value) : [...current, value]))
  }

  async function loadSymbols() {
    setBusySymbols(true)
    setError('')
    try {
      const rows = await api.dataSymbols(market)
      setSymbols(rows.filter((x) => x.quote === 'USDT').map((x) => x.symbol).join(', '))
    } catch (e) {
      setError(e instanceof Error ? e.message : '获取币种失败')
    } finally {
      setBusySymbols(false)
    }
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setError('')
    if (!selected.length) {
      setError('请至少选择一个 K 线周期')
      return
    }
    try {
      const created = await api.createDataDownload({
        market_type: market,
        mode,
        intervals: selected,
        start_date: start,
        end_date: end,
        symbols: symbolList,
        catalog_path: catalogPath || null,
      })
      setTask(created)
      localStorage.setItem(STORAGE_ACTIVE_TASK_KEY, created.id)
      api.dataDownloads().then(setTaskList).catch(() => {})
    } catch (e) {
      setError(e instanceof Error ? e.message : '任务提交失败')
    }
  }

  async function selectTask(taskId: string) {
    try {
      const detail = await api.dataDownload(taskId)
      setTask(detail)
      localStorage.setItem(STORAGE_ACTIVE_TASK_KEY, detail.id)
    } catch (e) {
      setError(e instanceof Error ? e.message : '读取任务详情失败')
    }
  }

  async function removeTask(taskId: string, e: React.MouseEvent) {
    e.stopPropagation()
    try {
      await api.deleteDataDownload(taskId)
      const list = taskList.filter((t) => t.id !== taskId)
      setTaskList(list)
      if (task?.id === taskId) {
        if (list.length > 0) {
          const detail = await api.dataDownload(list[0].id)
          setTask(detail)
          localStorage.setItem(STORAGE_ACTIVE_TASK_KEY, detail.id)
        } else {
          setTask(null)
          localStorage.removeItem(STORAGE_ACTIVE_TASK_KEY)
        }
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : '删除任务失败')
    }
  }

  return (
    <>
      <div className="data-download-layout">
        <form className="download-panel" onSubmit={submit}>
          <div className="download-title">
            <span>
              <Download />
              新建下载任务
            </span>
            <small>数据源 data.binance.vision</small>
          </div>
          <div className="download-grid">
            <label>
              交易所
              <select disabled>
                <option>Binance（币安）</option>
              </select>
            </label>
            <label>
              交易类型
              <select value={market} onChange={(e) => setMarket(e.target.value)}>
                <option value="um">U 本位永续合约</option>
                <option value="spot">现货</option>
              </select>
            </label>
            <label>
              下载模式
              <select value={mode} onChange={(e) => setMode(e.target.value)}>
                <option value="incremental">增量下载（跳过已完成归档）</option>
                <option value="force">强制重新下载</option>
              </select>
            </label>
          </div>
          <fieldset>
            <legend>K 线级别（可多选）</legend>
            <div className="interval-grid">
              {intervals.map((value) => (
                <label key={value}>
                  <input type="checkbox" checked={selected.includes(value)} onChange={() => toggle(value)} />
                  <span>{value}</span>
                </label>
              ))}
            </div>
          </fieldset>
          <div className="download-grid two">
            <label>
              开始日期（UTC）
              <input type="date" value={start} max={end} onChange={(e) => setStart(e.target.value)} />
            </label>
            <label>
              结束日期（UTC）
              <input type="date" value={end} min={start} max={yesterday()} onChange={(e) => setEnd(e.target.value)} />
            </label>
          </div>
          <label className="download-label">
            <span>
              交易品种（逗号、分号或空格分隔）
              <button type="button" className="text-button" onClick={loadSymbols} disabled={busySymbols}>
                <RefreshCw className={busySymbols ? 'spin' : ''} />
                {busySymbols ? '获取中' : '获取全部 USDT 币种'}
              </button>
            </span>
            <textarea value={symbols} onChange={(e) => setSymbols(e.target.value)} placeholder="BTCUSDT, ETHUSDT" />
            <small>
              {market === 'um'
                ? 'Catalog Instrument ID 示例：BTCUSDT-PERP.BINANCE'
                : 'Catalog Instrument ID 示例：BTCUSDT.BINANCE'}{' '}
              · 当前 {symbolList.length} 个品种
            </small>
          </label>
          <label className="download-label">
            Catalog 本地路径
            <input
              value={catalogPath}
              onChange={(e) => setCatalogPath(e.target.value)}
              placeholder="留空使用后端 CATALOG_PATH"
            />
            <small>
              <HardDrive />
              目录不存在时自动创建；增量记录保存在 catalog 内
            </small>
          </label>
          {error && <div className="form-error">{error}</div>}
          <div className="download-note">
            <Info />
            完整月份优先使用 monthly 归档，首尾非完整月份使用 daily 归档；页面已自动保存配置与下载进度。
          </div>
          <div className="download-actions">
            <button className="button" type="button" disabled>
              <CalendarDays />
              定时任务（即将支持）
            </button>
            <button className="button primary" disabled={running || !symbolList.length}>
              {running ? <LoaderCircle className="spin" /> : <Download />}
              {running ? '任务执行中' : '提交数据下载任务'}
            </button>
          </div>
        </form>

        <section className="log-column">
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', margin: '3px 0 15px' }}>
            <h3 style={{ margin: 0 }}>
              <i />
              实时数据下载日志（UTC）
            </h3>
            {taskList.length > 0 && (
              <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                <History size={14} style={{ color: 'var(--cyan)' }} />
                <select
                  value={task?.id ?? ''}
                  onChange={(e) => selectTask(e.target.value)}
                  style={{
                    background: '#080f19',
                    border: '1px solid #26384d',
                    borderRadius: 6,
                    color: '#dce6f1',
                    fontSize: 11,
                    padding: '4px 8px',
                    maxWidth: 220,
                  }}
                >
                  {taskList.map((t) => (
                    <option key={t.id} value={t.id}>
                      {t.status === 'running'
                        ? '🟡 运行中'
                        : t.status === 'completed'
                          ? '🟢 已完成'
                          : t.status === 'queued'
                            ? '⏳ 等待中'
                            : '🔴 失败'}{' '}
                      · {t.request?.market_type?.toUpperCase() ?? 'UM'} ({t.progress}%)
                    </option>
                  ))}
                </select>
                {task && (
                  <button
                    type="button"
                    className="text-button"
                    style={{ color: '#ff7c87', padding: '2px 4px' }}
                    onClick={(e) => removeTask(task.id, e)}
                    title="删除当前任务记录"
                  >
                    <Trash2 size={13} />
                  </button>
                )}
              </div>
            )}
          </div>

          <div className="log-panel">
            {!task ? (
              <div className="log-empty">
                <TerminalSquare />
                <span>暂无运行日志，请配置参数后提交下载任务…</span>
              </div>
            ) : (
              <>
                <div className="task-progress">
                  <div>
                    <b>{task.stage}</b>
                    <span>{task.progress}%</span>
                  </div>
                  <div className="big-progress">
                    <i style={{ width: `${task.progress}%` }} />
                  </div>
                  <small>
                    {task.completed_files}/{task.total_files} 文件 · {task.rows.toLocaleString()} 根 K 线
                  </small>
                </div>
                <div className="log-stream" ref={logStreamRef}>
                  {task.logs.map((line, index) => (
                    <div className={line.level} key={index}>
                      <time>{line.time}</time>
                      <span>{line.message}</span>
                    </div>
                  ))}
                </div>
                {task.status === 'completed' && (
                  <div className="task-result success">
                    <CheckCircle2 />
                    <div>
                      <b>Catalog 写入完成</b>
                      <small>
                        {task.catalog_path}
                        <br />
                        下载 {task.downloaded_files} · 跳过 {task.skipped_files} · 远端缺失/跳过 {task.missing_files}
                      </small>
                    </div>
                  </div>
                )}
                {task.status === 'failed' && (
                  <div className="task-result failed">
                    <Info />
                    <div>
                      <b>任务失败或已中断</b>
                      <small>{task.error || '未捕获的错误'}</small>
                    </div>
                  </div>
                )}
              </>
            )}
          </div>
        </section>
      </div>
    </>
  )
}

import {
  ArrowDownUp,
  BarChart2,
  Calendar,
  CalendarDays,
  CalendarRange,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Clock,
  Coins,
  Copy,
  Database,
  Download,
  ExternalLink,
  Eye,
  FileSpreadsheet,
  Filter,
  FilterX,
  HardDrive,
  History,
  Info,
  Layers,
  LoaderCircle,
  Plus,
  RefreshCw,
  Search,
  Sparkles,
  TerminalSquare,
  Trash2,
  X,
  Zap,
} from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api'
import type {
  CatalogCoverageBucket,
  CatalogCoverageSymbolDetail,
  CatalogSummary,
  CatalogSymbolItem,
  CatalogTimeframeItem,
} from '../types'

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
const STORAGE_ACTIVE_TAB_KEY = 'quantlab_data_active_tab_v1'

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

function formatBytes(bytes: number): string {
  if (!bytes || bytes <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  const i = Math.floor(Math.log(bytes) / Math.log(1024))
  return `${(bytes / Math.pow(1024, i)).toFixed(i === 0 ? 0 : 1)} ${units[i]}`
}

function calculateDaysSpan(start?: string | null, end?: string | null): number {
  if (!start || !end) return 0
  const d1 = new Date(start).getTime()
  const d2 = new Date(end).getTime()
  if (isNaN(d1) || isNaN(d2)) return 0
  return Math.max(1, Math.round(Math.abs(d2 - d1) / (1000 * 60 * 60 * 24)) + 1)
}

export default function DataDownloads() {
  const savedForm = useMemo(() => loadSavedForm(), [])

  // Tab State
  const [activeTab, setActiveTab] = useState<'catalog' | 'download'>(() => {
    return (localStorage.getItem(STORAGE_ACTIVE_TAB_KEY) as 'catalog' | 'download') || 'catalog'
  })

  // Catalog Explorer State
  const [catalogLoading, setCatalogLoading] = useState(false)
  const [catalogSummary, setCatalogSummary] = useState<CatalogSummary | null>(null)
  const [catalogError, setCatalogError] = useState('')
  const [searchQuery, setSearchQuery] = useState('')
  const [debouncedSearch, setDebouncedSearch] = useState('')
  const [marketFilter, setMarketFilter] = useState<'all' | 'um' | 'spot'>('all')
  const [intervalFilter, setIntervalFilter] = useState<string>('all')
  const [durationFilter, setDurationFilter] = useState<string>('all')
  const [sortBy, setSortBy] = useState<'symbol' | 'bars' | 'size' | 'start' | 'end'>('symbol')
  const [sortOrder, setSortOrder] = useState<'asc' | 'desc'>('asc')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [expandedSymbols, setExpandedSymbols] = useState<Set<string>>(new Set())
  const [deleteTarget, setDeleteTarget] = useState<{ symbol: string; instrument_id: string; interval?: string } | null>(null)
  const [deleting, setDeleting] = useState(false)

  // Coverage Modal State
  const [bucketModalTarget, setBucketModalTarget] = useState<CatalogCoverageBucket | null>(null)
  const [bucketModalSearch, setBucketModalSearch] = useState('')
  const [copiedSymbols, setCopiedSymbols] = useState(false)

  // Download Form State
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

  // Debounce search query
  useEffect(() => {
    const timer = setTimeout(() => {
      setDebouncedSearch(searchQuery)
      setPage(1)
    }, 250)
    return () => clearTimeout(timer)
  }, [searchQuery])

  // Switch Tab helper
  const handleTabChange = (tab: 'catalog' | 'download') => {
    setActiveTab(tab)
    try {
      localStorage.setItem(STORAGE_ACTIVE_TAB_KEY, tab)
    } catch {
      // Ignore
    }
  }

  // Load Catalog Summary with pagination
  const fetchCatalog = useCallback(
    async (targetPage = page, targetPageSize = pageSize) => {
      setCatalogLoading(true)
      setCatalogError('')
      try {
        const summary = await api.dataCatalogSummary({
          page: targetPage,
          page_size: targetPageSize,
          query: debouncedSearch.trim() || undefined,
          market_type: marketFilter !== 'all' ? marketFilter : undefined,
          interval: intervalFilter !== 'all' ? intervalFilter : undefined,
          duration_bucket: durationFilter !== 'all' ? durationFilter : undefined,
          sort_by: sortBy,
          sort_order: sortOrder,
          catalog_path: catalogPath.trim() || undefined,
        })
        setCatalogSummary(summary)
        // If modal is open, keep its data synced with updated stats
        if (bucketModalTarget && summary.coverage_stats) {
          const updatedBucket = summary.coverage_stats.find((b) => b.key === bucketModalTarget.key)
          if (updatedBucket) {
            setBucketModalTarget(updatedBucket)
          }
        }
      } catch (err) {
        setCatalogError(err instanceof Error ? err.message : '读取 Catalog 数据资产失败')
      } finally {
        setCatalogLoading(false)
      }
    },
    [page, pageSize, debouncedSearch, marketFilter, intervalFilter, durationFilter, sortBy, sortOrder, catalogPath, bucketModalTarget?.key],
  )

  useEffect(() => {
    fetchCatalog(page, pageSize)
  }, [page, pageSize, debouncedSearch, marketFilter, intervalFilter, durationFilter, sortBy, sortOrder, catalogPath])

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
        // If task completed, refresh list and catalog
        if (['completed', 'failed'].includes(updated.status)) {
          api.dataDownloads().then(setTaskList).catch(() => {})
          fetchCatalog()
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

  // Pre-fill download form from catalog item and switch to download tab
  const handleRefillDownload = (item: CatalogSymbolItem, interval?: string) => {
    setMarket(item.market_type)
    setSymbols(item.symbol)
    if (interval) {
      setSelected([interval])
    } else if (item.timeframes.length > 0) {
      setSelected(item.timeframes.map((tf) => tf.interval))
    }
    if (item.end_date) {
      const d = new Date(item.end_date)
      d.setUTCDate(d.getUTCDate() + 1)
      const nextDay = d.toISOString().slice(0, 10)
      if (nextDay <= yesterday()) {
        setStart(nextDay)
      } else {
        setStart(item.start_date || monthAgo())
      }
    }
    setEnd(yesterday())
    setMode('incremental')
    handleTabChange('download')
  }

  const handleCopySymbols = (symbolsList: string[]) => {
    if (!symbolsList || symbolsList.length === 0) return
    const text = symbolsList.join(', ')
    navigator.clipboard.writeText(text).then(() => {
      setCopiedSymbols(true)
      setTimeout(() => setCopiedSymbols(false), 2000)
    }).catch(() => {
      // Fallback
    })
  }

  const handleBatchRefillDownload = (symbolsList: string[], marketType: string = 'um') => {
    if (!symbolsList || symbolsList.length === 0) return
    setMarket(marketType)
    setSymbols(symbolsList.join(', '))
    setMode('incremental')
    setBucketModalTarget(null)
    handleTabChange('download')
  }

  const toggleExpandSymbol = (id: string) => {
    setExpandedSymbols((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  // Handle Delete Symbol / Interval from Catalog
  const confirmDelete = async () => {
    if (!deleteTarget) return
    setDeleting(true)
    try {
      await api.deleteCatalogSymbol(deleteTarget.instrument_id, deleteTarget.interval, catalogPath.trim() || undefined)
      await fetchCatalog(page, pageSize)
      setDeleteTarget(null)
    } catch (err) {
      setCatalogError(err instanceof Error ? err.message : '删除数据失败')
    } finally {
      setDeleting(false)
    }
  }

  const catalogItems = catalogSummary?.items ?? []

  return (
    <>
      <header>
        <div>
          <h1>数据管理</h1>
          <p>Binance 历史行情采集、Catalog 本地数据资产盘点与 K 线时间跨度检索</p>
        </div>
        <div className="actions">
          {activeTab === 'catalog' && (
            <button className="button" onClick={() => fetchCatalog(page, pageSize)} disabled={catalogLoading}>
              <RefreshCw className={catalogLoading ? 'spin' : ''} size={14} />
              刷新资产
            </button>
          )}
          <button className="button primary" onClick={() => handleTabChange('download')}>
            <Download size={14} />
            新建下载任务
          </button>
        </div>
      </header>

      {/* Top Nav Tabs */}
      <div className="data-page-tabs">
        <button
          type="button"
          className={`data-tab-btn ${activeTab === 'catalog' ? 'active' : ''}`}
          onClick={() => handleTabChange('catalog')}
        >
          <Database size={16} />
          <span>数据资产管理</span>
          {catalogSummary && catalogSummary.total_symbols > 0 && (
            <span className="data-tab-badge">{catalogSummary.total_symbols}</span>
          )}
        </button>
        <button
          type="button"
          className={`data-tab-btn ${activeTab === 'download' ? 'active' : ''}`}
          onClick={() => handleTabChange('download')}
        >
          <Download size={16} />
          <span>数据采集与下载</span>
          {running && (
            <span className="data-tab-badge is-running">
              <LoaderCircle size={11} className="spin" />
              {task?.progress}%
            </span>
          )}
        </button>
      </div>

      {/* TAB 1: 数据资产管理 (Catalog Explorer) */}
      {activeTab === 'catalog' && (
        <div className="catalog-management-view">
          {/* Top Metric Cards */}
          <div className="catalog-metrics-grid">
            <div className="catalog-metric-card">
              <div className="metric-icon cyan">
                <Coins size={20} />
              </div>
              <div className="metric-info">
                <span>标的总数</span>
                <strong>{catalogSummary?.total_symbols ?? 0} 个</strong>
                <small>
                  全量 Catalog 包含 {catalogSummary?.all_symbols_count ?? 0} 个标的
                </small>
              </div>
            </div>

            <div className="catalog-metric-card">
              <div className="metric-icon green">
                <BarChart2 size={20} />
              </div>
              <div className="metric-info">
                <span>K 线总量</span>
                <strong>{(catalogSummary?.total_bars ?? 0).toLocaleString()} 根</strong>
                <small>Parquet 紧凑列式存储</small>
              </div>
            </div>

            <div className="catalog-metric-card">
              <div className="metric-icon amber">
                <HardDrive size={20} />
              </div>
              <div className="metric-info">
                <span>Catalog 占用体积</span>
                <strong>{formatBytes(catalogSummary?.total_size_bytes ?? 0)}</strong>
                <small title={catalogSummary?.catalog_path}>
                  {catalogSummary?.catalog_path
                    ? catalogSummary.catalog_path.split('/').slice(-2).join('/')
                    : '本地数据目录'}
                </small>
              </div>
            </div>

            <div className="catalog-metric-card">
              <div className="metric-icon purple">
                <Layers size={20} />
              </div>
              <div className="metric-info">
                <span>已覆盖周期</span>
                <div className="available-tfs-row">
                  {catalogSummary?.available_timeframes && catalogSummary.available_timeframes.length > 0 ? (
                    catalogSummary.available_timeframes.map((tf) => (
                      <span key={tf} className="tf-mini-badge">
                        {tf}
                      </span>
                    ))
                  ) : (
                    <small>暂无数据</small>
                  )}
                </div>
              </div>
            </div>
          </div>

          {/* Coverage Duration Statistics Panel */}
          {catalogSummary && (
            <div className="coverage-stats-section">
              <div className="coverage-stats-header">
                <div className="coverage-stats-title">
                  <Clock size={16} />
                  <span>标的历史覆盖时长统计</span>
                  <small>
                    (共统计 {catalogSummary.all_symbols_count} 个本地标的，点击卡片快速筛选或点击「查看币种」)
                  </small>
                </div>
                {durationFilter !== 'all' && (
                  <div className="coverage-filter-active-pill">
                    <span>
                      已筛选时长：
                      <strong>
                        {catalogSummary.coverage_stats?.find((b) => b.key === durationFilter)?.label ?? durationFilter}
                      </strong>
                    </span>
                    <button
                      type="button"
                      onClick={() => {
                        setDurationFilter('all')
                        setPage(1)
                      }}
                      title="清除时长筛选"
                    >
                      <X size={13} />
                    </button>
                  </div>
                )}
              </div>

              {/* Multi-segment distribution progress bar */}
              {catalogSummary.all_symbols_count > 0 && (
                <div className="coverage-dist-bar-container">
                  <div className="coverage-dist-bar">
                    {catalogSummary.coverage_stats?.map((bucket) => {
                      if (bucket.percentage <= 0) return null
                      return (
                        <div
                          key={bucket.key}
                          className={`dist-bar-seg bucket-${bucket.key}`}
                          style={{ width: `${bucket.percentage}%` }}
                          title={`${bucket.label} (${bucket.desc}): ${bucket.count} 个币种 (${bucket.percentage}%)`}
                          onClick={() => {
                            setDurationFilter((prev) => (prev === bucket.key ? 'all' : bucket.key))
                            setPage(1)
                          }}
                        />
                      )
                    })}
                  </div>
                </div>
              )}

              {/* 5 Coverage Bucket Cards */}
              <div className="coverage-cards-grid">
                {(catalogSummary.coverage_stats ?? []).map((bucket) => {
                  const isActive = durationFilter === bucket.key
                  return (
                    <div
                      key={bucket.key}
                      className={`coverage-card ${isActive ? 'active' : ''}`}
                      onClick={() => {
                        setDurationFilter((prev) => (prev === bucket.key ? 'all' : bucket.key))
                        setPage(1)
                      }}
                    >
                      <div className="coverage-card-head">
                        <div className="bucket-dot-label">
                          <span className={`bucket-dot bucket-${bucket.key}`} />
                          <span>{bucket.label}</span>
                        </div>
                        <span className="bucket-desc-tag">{bucket.desc}</span>
                      </div>

                      <div className="coverage-card-body">
                        <div className="bucket-count-wrapper">
                          <strong>{bucket.count}</strong>
                          <small>个币种</small>
                        </div>
                        <span className="bucket-pct-badge">{bucket.percentage}%</span>
                      </div>

                      <div className="coverage-card-foot">
                        <span className="bucket-size-text">{formatBytes(bucket.total_size_bytes)}</span>
                        <button
                          type="button"
                          className="bucket-inspect-btn"
                          disabled={bucket.count === 0}
                          onClick={(e) => {
                            e.stopPropagation()
                            setBucketModalTarget(bucket)
                            setBucketModalSearch('')
                          }}
                          title={`查看【${bucket.label}】覆盖的全部 ${bucket.count} 个币种`}
                        >
                          <Eye size={12} />
                          <span>查看币种</span>
                        </button>
                      </div>
                    </div>
                  )
                })}
              </div>
            </div>
          )}

          {/* Filter & Search Bar */}
          <div className="catalog-toolbar">
            <div className="toolbar-search">
              <Search size={15} />
              <input
                type="text"
                placeholder="搜索标的名称或 Instrument ID (如 BTCUSDT)..."
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
              />
              {searchQuery && (
                <button type="button" className="clear-btn" onClick={() => setSearchQuery('')}>
                  ×
                </button>
              )}
            </div>

            <div className="toolbar-filters">
              <label>
                <span>交易类型</span>
                <select
                  value={marketFilter}
                  onChange={(e) => {
                    setMarketFilter(e.target.value as any)
                    setPage(1)
                  }}
                >
                  <option value="all">全部类型</option>
                  <option value="um">U 本位永续 (UM)</option>
                  <option value="spot">现货 (Spot)</option>
                </select>
              </label>

              <label>
                <span>K 线周期</span>
                <select
                  value={intervalFilter}
                  onChange={(e) => {
                    setIntervalFilter(e.target.value)
                    setPage(1)
                  }}
                >
                  <option value="all">全部周期</option>
                  {intervals.map((tf) => (
                    <option key={tf} value={tf}>
                      {tf}
                    </option>
                  ))}
                </select>
              </label>

              <label>
                <span>覆盖时长</span>
                <select
                  value={durationFilter}
                  onChange={(e) => {
                    setDurationFilter(e.target.value)
                    setPage(1)
                  }}
                >
                  <option value="all">全部时长</option>
                  <option value="gte_3y">≥ 3 年 (1095天+)</option>
                  <option value="1y_3y">1 - 3 年 (365-1094天)</option>
                  <option value="6m_1y">6 个月 - 1 年 (180-364天)</option>
                  <option value="1m_6m">1 - 6 个月 (30-179天)</option>
                  <option value="lt_1m">&lt; 1 个月 (&lt;30天)</option>
                </select>
              </label>

              <label>
                <span>排序方式</span>
                <select
                  value={sortBy}
                  onChange={(e) => {
                    setSortBy(e.target.value as any)
                    setPage(1)
                  }}
                >
                  <option value="symbol">按标的名称</option>
                  <option value="bars">按 K 线总数</option>
                  <option value="size">按占用体积</option>
                  <option value="start">按起始时间</option>
                  <option value="end">按最新截止时间</option>
                </select>
              </label>

              <button
                type="button"
                className="sort-direction-btn"
                title={sortOrder === 'asc' ? '当前：升序 (点击切换降序)' : '当前：降序 (点击切换升序)'}
                onClick={() => {
                  setSortOrder((prev) => (prev === 'asc' ? 'desc' : 'asc'))
                  setPage(1)
                }}
              >
                <ArrowDownUp size={14} />
                <span>{sortOrder === 'asc' ? '升序' : '降序'}</span>
              </button>
            </div>
          </div>

          {catalogError && <div className="form-error" style={{ marginBottom: 16 }}>{catalogError}</div>}

          {/* Catalog Data Table */}
          <div className="catalog-table-container">
            {catalogLoading && !catalogSummary ? (
              <div className="catalog-empty">
                <LoaderCircle className="spin" size={32} />
                <span>正在快速检索 Catalog 数据资产...</span>
              </div>
            ) : catalogItems.length === 0 ? (
              <div className="catalog-empty">
                <Database size={40} />
                <h3>未找到匹配的行情数据</h3>
                <p>
                  {searchQuery || marketFilter !== 'all' || intervalFilter !== 'all'
                    ? '没有符合当前筛选条件的标的，请调整搜索条件或过滤项。'
                    : '本地 Catalog 尚无 K 线数据，请先前往数据采集页面下载所需品种的历史行情。'}
                </p>
                <button
                  type="button"
                  className="button primary"
                  onClick={() => handleTabChange('download')}
                  style={{ marginTop: 12 }}
                >
                  <Download size={14} />
                  前往下载行情数据
                </button>
              </div>
            ) : (
              <>
                <table className="catalog-table">
                  <thead>
                    <tr>
                      <th style={{ width: 36 }}></th>
                      <th>标的名称 / 编号</th>
                      <th>类型</th>
                      <th>覆盖时间范围 (UTC)</th>
                      <th>K 线周期</th>
                      <th style={{ textAlign: 'right' }}>总 K 线条数</th>
                      <th style={{ textAlign: 'right' }}>存储体积</th>
                      <th style={{ textAlign: 'center', width: 140 }}>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {catalogItems.map((item) => {
                      const isExpanded = expandedSymbols.has(item.instrument_id)
                      const daysSpan = calculateDaysSpan(item.start_date, item.end_date)
                      return (
                        <SymbolRows
                          key={item.instrument_id}
                          item={item}
                          isExpanded={isExpanded}
                          daysSpan={daysSpan}
                          onToggleExpand={() => toggleExpandSymbol(item.instrument_id)}
                          onRefillDownload={(interval) => handleRefillDownload(item, interval)}
                          onDeleteSymbol={() => setDeleteTarget({ symbol: item.symbol, instrument_id: item.instrument_id })}
                          onDeleteInterval={(interval) =>
                            setDeleteTarget({
                              symbol: item.symbol,
                              instrument_id: item.instrument_id,
                              interval,
                            })
                          }
                        />
                      )
                    })}
                  </tbody>
                </table>

                {/* Pagination Controls */}
                {catalogSummary && catalogSummary.total_symbols > 0 && (
                  <div className="catalog-pagination">
                    <div className="pagination-info">
                      <span>
                        显示第 <strong>{(catalogSummary.page - 1) * catalogSummary.page_size + 1}</strong> -{' '}
                        <strong>{Math.min(catalogSummary.page * catalogSummary.page_size, catalogSummary.total_symbols)}</strong> 条，共{' '}
                        <strong>{catalogSummary.total_symbols}</strong> 条标的
                      </span>
                      <div className="page-size-selector">
                        <span>每页条数：</span>
                        <select
                          value={pageSize}
                          onChange={(e) => {
                            setPageSize(Number(e.target.value))
                            setPage(1)
                          }}
                        >
                          <option value={10}>10 条 / 页</option>
                          <option value={20}>20 条 / 页</option>
                          <option value={50}>50 条 / 页</option>
                          <option value={100}>100 条 / 页</option>
                        </select>
                      </div>
                    </div>

                    {catalogSummary.total_pages > 1 && (
                      <div className="pagination-controls">
                        <button
                          type="button"
                          className="page-btn"
                          disabled={catalogSummary.page <= 1 || catalogLoading}
                          onClick={() => setPage(1)}
                          title="首页"
                        >
                          首页
                        </button>
                        <button
                          type="button"
                          className="page-btn"
                          disabled={catalogSummary.page <= 1 || catalogLoading}
                          onClick={() => setPage((p) => Math.max(1, p - 1))}
                          title="上一页"
                        >
                          上一页
                        </button>
                        {renderPaginationButtons(catalogSummary.page, catalogSummary.total_pages, (p) => setPage(p))}
                        <button
                          type="button"
                          className="page-btn"
                          disabled={catalogSummary.page >= catalogSummary.total_pages || catalogLoading}
                          onClick={() => setPage((p) => Math.min(catalogSummary.total_pages, p + 1))}
                          title="下一页"
                        >
                          下一页
                        </button>
                        <button
                          type="button"
                          className="page-btn"
                          disabled={catalogSummary.page >= catalogSummary.total_pages || catalogLoading}
                          onClick={() => setPage(catalogSummary.total_pages)}
                          title="尾页"
                        >
                          尾页
                        </button>
                      </div>
                    )}
                  </div>
                )}
              </>
            )}
          </div>
        </div>
      )}

      {/* TAB 2: 数据下载与任务 (Data Download) */}
      {activeTab === 'download' && (
        <div className="data-download-layout">
          <form className="download-panel" onSubmit={submit}>
            <div className="download-title">
              <span>
                <Download />
                新建下载任务
              </span>
              <small>数据源 data.binance.vision (免 Key 直连)</small>
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
                  <option value="um">U 本位永续合约 (UM)</option>
                  <option value="spot">现货 (Spot)</option>
                </select>
              </label>
              <label>
                下载模式
                <select value={mode} onChange={(e) => setMode(e.target.value)}>
                  <option value="incremental">增量下载（跳过已下载归档）</option>
                  <option value="force">强制重新下载覆盖</option>
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
                · 当前选定 {symbolList.length} 个品种
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
              流式多线程并发下载、校验与入库，实时推送任务百分比与 K 线行数；下载完成后可随时在「数据资产管理」Tab 查验。
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
                      <div style={{ flex: 1 }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                          <b>Catalog 写入完成</b>
                          <button
                            type="button"
                            className="button"
                            style={{ padding: '4px 10px', fontSize: 11 }}
                            onClick={() => {
                              fetchCatalog()
                              handleTabChange('catalog')
                            }}
                          >
                            <Database size={12} />
                            查看数据资产
                          </button>
                        </div>
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
      )}

      {/* Coverage Symbols Detail Modal */}
      {bucketModalTarget && (
        <div className="modal-backdrop" onClick={() => setBucketModalTarget(null)}>
          <div className="modal coverage-symbols-modal" onClick={(e) => e.stopPropagation()}>
            <div className="coverage-symbols-modal-head">
              <div className="modal-head-title-row">
                <span className={`bucket-dot bucket-${bucketModalTarget.key}`} style={{ width: 10, height: 10 }} />
                <h3>
                  覆盖时间段：{bucketModalTarget.label} ({bucketModalTarget.desc})
                </h3>
                <span className="data-tab-badge">{bucketModalTarget.count} 个币种</span>
              </div>
              <button
                type="button"
                className="modal-close-btn"
                onClick={() => setBucketModalTarget(null)}
                aria-label="关闭"
              >
                <X size={18} />
              </button>
            </div>

            <div className="coverage-modal-summary-strip">
              <div>
                <span>时间段占比:</span>
                <strong>{bucketModalTarget.percentage}%</strong>
              </div>
              <div>
                <span>总 K 线条数:</span>
                <strong>{bucketModalTarget.total_bars.toLocaleString()} 根</strong>
              </div>
              <div>
                <span>数据存储占用:</span>
                <strong>{formatBytes(bucketModalTarget.total_size_bytes)}</strong>
              </div>
            </div>

            <div className="coverage-modal-toolbar">
              <div className="modal-search-box">
                <Search size={14} />
                <input
                  type="text"
                  placeholder="在当前时间段搜索币种..."
                  value={bucketModalSearch}
                  onChange={(e) => setBucketModalSearch(e.target.value)}
                  autoFocus
                />
                {bucketModalSearch && (
                  <button type="button" className="clear-btn" onClick={() => setBucketModalSearch('')}>
                    ×
                  </button>
                )}
              </div>

              <div className="modal-toolbar-actions">
                <button
                  type="button"
                  className="modal-btn copy"
                  onClick={() => handleCopySymbols(bucketModalTarget.symbols)}
                  title="一键复制当前时间段内所有币种代码"
                >
                  {copiedSymbols ? <Check size={13} style={{ color: 'var(--green)' }} /> : <Copy size={13} />}
                  <span>{copiedSymbols ? '已复制列表!' : '复制全部币种'}</span>
                </button>

                <button
                  type="button"
                  className="modal-btn filter-action"
                  onClick={() => {
                    setDurationFilter(bucketModalTarget.key)
                    setPage(1)
                    setBucketModalTarget(null)
                  }}
                  title="在资产管理主表格中筛选展示这些币种"
                >
                  <Filter size={13} />
                  <span>在表格中筛选</span>
                </button>

                <button
                  type="button"
                  className="modal-btn refill-action"
                  onClick={() => handleBatchRefillDownload(bucketModalTarget.symbols)}
                  title="将这些币种一键填入采集下载表单"
                >
                  <Download size={13} />
                  <span>批量增量补齐</span>
                </button>
              </div>
            </div>

            <div className="coverage-symbols-scroll-list">
              {bucketModalTarget.symbol_details.filter(
                (s) =>
                  !bucketModalSearch.trim() ||
                  s.symbol.toLowerCase().includes(bucketModalSearch.trim().toLowerCase()) ||
                  s.instrument_id.toLowerCase().includes(bucketModalSearch.trim().toLowerCase()),
              ).length === 0 ? (
                <div className="catalog-empty" style={{ padding: '30px 10px' }}>
                  <Search size={24} />
                  <p>未找到匹配 “{bucketModalSearch}” 的币种</p>
                </div>
              ) : (
                bucketModalTarget.symbol_details
                  .filter(
                    (s) =>
                      !bucketModalSearch.trim() ||
                      s.symbol.toLowerCase().includes(bucketModalSearch.trim().toLowerCase()) ||
                      s.instrument_id.toLowerCase().includes(bucketModalSearch.trim().toLowerCase()),
                  )
                  .map((sym) => (
                    <div key={sym.instrument_id} className="symbol-detail-item">
                      <div className="symbol-detail-left">
                        <div>
                          <div className="symbol-detail-name">{sym.symbol}</div>
                          <div className="symbol-detail-inst">{sym.instrument_id}</div>
                        </div>
                        <span className={`market-type-badge ${sym.market_type}`}>
                          {sym.market_type === 'um' ? '永续' : '现货'}
                        </span>
                      </div>

                      <div className="symbol-detail-mid">
                        <div className="symbol-date-span-row">
                          <span>{sym.start_date || '—'}</span>
                          <span className="arrow-sep">→</span>
                          <span>{sym.end_date || '—'}</span>
                          <span className="symbol-days-tag">共 {sym.days_span} 天</span>
                        </div>
                        <div className="symbol-meta-row">
                          <span>K 线: {sym.total_bars.toLocaleString()} 根</span>
                          <span>·</span>
                          <span>体积: {formatBytes(sym.total_size_bytes)}</span>
                          <span>·</span>
                          <span>周期: {sym.timeframes.join(', ') || '无'}</span>
                        </div>
                      </div>

                      <div className="symbol-detail-right">
                        <button
                          type="button"
                          className="button"
                          style={{ padding: '3px 8px', fontSize: 11 }}
                          onClick={() => {
                            setSearchQuery(sym.symbol)
                            setDurationFilter('all')
                            setPage(1)
                            setBucketModalTarget(null)
                          }}
                          title="在主表格中查找此标的"
                        >
                          <Search size={11} />
                          定位
                        </button>
                        <button
                          type="button"
                          className="button primary"
                          style={{ padding: '3px 8px', fontSize: 11 }}
                          onClick={() => {
                            setMarket(sym.market_type)
                            setSymbols(sym.symbol)
                            setMode('incremental')
                            setBucketModalTarget(null)
                            handleTabChange('download')
                          }}
                          title="为该标的采集/增量补齐数据"
                        >
                          <Download size={11} />
                          补齐
                        </button>
                      </div>
                    </div>
                  ))
              )}
            </div>
          </div>
        </div>
      )}

      {/* Delete Confirmation Modal */}
      {deleteTarget && (
        <div className="modal-backdrop" onClick={() => setDeleteTarget(null)}>
          <div className="modal confirm-modal" onClick={(e) => e.stopPropagation()}>
            <h2>确认删除数据</h2>
            <p>
              确定要从本地 Catalog 中删除标的 <strong>{deleteTarget.symbol}</strong>
              {deleteTarget.interval ? ` 的 [${deleteTarget.interval}] 周期数据` : ' 的全部 K 线与标的定义数据'} 吗？
              <br />
              <small style={{ color: 'var(--red)', display: 'block', marginTop: 8 }}>
                此操作将物理删除本地对应的 Parquet 文件及 Manifest 记录，不可撤销。
              </small>
            </p>
            <div className="modal-actions">
              <button className="button" type="button" onClick={() => setDeleteTarget(null)} disabled={deleting}>
                取消
              </button>
              <button className="button danger" type="button" onClick={confirmDelete} disabled={deleting}>
                {deleting ? <LoaderCircle className="spin" size={14} /> : <Trash2 size={14} />}
                {deleting ? '删除中...' : '确认删除'}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  )
}

function SymbolRows({
  item,
  isExpanded,
  daysSpan,
  onToggleExpand,
  onRefillDownload,
  onDeleteSymbol,
  onDeleteInterval,
}: {
  item: CatalogSymbolItem
  isExpanded: boolean
  daysSpan: number
  onToggleExpand: () => void
  onRefillDownload: (interval?: string) => void
  onDeleteSymbol: () => void
  onDeleteInterval: (interval: string) => void
}) {
  return (
    <>
      <tr className={`catalog-row ${isExpanded ? 'expanded' : ''}`} onClick={onToggleExpand}>
        <td style={{ textAlign: 'center', cursor: 'pointer' }}>
          <button type="button" className="expand-chevron-btn" aria-label="展开周期明细">
            {isExpanded ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
          </button>
        </td>
        <td>
          <div className="symbol-name-col">
            <strong className="symbol-title">{item.symbol}</strong>
            <small className="instrument-code">{item.instrument_id}</small>
          </div>
        </td>
        <td>
          <span className={`market-type-badge ${item.market_type}`}>
            {item.market_type === 'um' ? '永续合约' : '现货'}
          </span>
        </td>
        <td>
          {item.start_date && item.end_date ? (
            <div className="date-range-col">
              <div className="date-span-text">
                <span>{item.start_date}</span>
                <span className="arrow-sep">→</span>
                <span>{item.end_date}</span>
              </div>
              <small className="days-badge">共 {daysSpan} 天数据</small>
            </div>
          ) : (
            <span className="muted-text">暂无时间记录</span>
          )}
        </td>
        <td>
          <div className="timeframes-pill-list">
            {item.timeframes.map((tf) => (
              <span key={tf.interval} className="tf-pill" title={`${tf.bars.toLocaleString()} 根 (${tf.start_date || ''} ~ ${tf.end_date || ''})`}>
                {tf.interval}
              </span>
            ))}
          </div>
        </td>
        <td style={{ textAlign: 'right' }}>
          <span className="bars-count-text">{item.total_bars.toLocaleString()}</span>
          <small className="bars-unit"> 根</small>
        </td>
        <td style={{ textAlign: 'right' }}>
          <span className="size-text">{formatBytes(item.total_size_bytes)}</span>
          <small className="file-count-text"> ({item.file_count} 文件)</small>
        </td>
        <td style={{ textAlign: 'center' }} onClick={(e) => e.stopPropagation()}>
          <div className="row-actions">
            <button
              type="button"
              className="action-icon-btn refill"
              onClick={() => onRefillDownload()}
              title="补充 / 增量下载此标的数据"
            >
              <Download size={14} />
              <span>补充</span>
            </button>
            <button
              type="button"
              className="action-icon-btn danger"
              onClick={onDeleteSymbol}
              title="删除此标的所有数据"
            >
              <Trash2 size={14} />
            </button>
          </div>
        </td>
      </tr>

      {/* Expanded Breakdown Rows */}
      {isExpanded && (
        <tr className="expanded-details-row">
          <td colSpan={8}>
            <div className="expanded-details-panel">
              <div className="details-header">
                <span>
                  <Layers size={14} />
                  <strong>{item.symbol}</strong> 各周期详细覆盖统计 ({item.timeframes.length} 个周期)
                </span>
                <button
                  type="button"
                  className="text-button"
                  style={{ fontSize: 11 }}
                  onClick={() => onRefillDownload()}
                >
                  <Plus size={13} />
                  为该标的采集更多周期
                </button>
              </div>
              <div className="timeframe-cards-grid">
                {item.timeframes.map((tf) => (
                  <div className="tf-detail-card" key={tf.interval}>
                    <div className="tf-card-top">
                      <span className="tf-card-interval">{tf.interval}</span>
                      <button
                        type="button"
                        className="tf-del-btn"
                        title={`删除 [${tf.interval}] 周期数据`}
                        onClick={() => onDeleteInterval(tf.interval)}
                      >
                        <Trash2 size={12} />
                      </button>
                    </div>
                    <div className="tf-card-body">
                      <div className="tf-card-row">
                        <span>K 线条数:</span>
                        <strong>{tf.bars.toLocaleString()} 根</strong>
                      </div>
                      <div className="tf-card-row">
                        <span>时间范围:</span>
                        <small>{tf.start_date || '—'} ~ {tf.end_date || '—'}</small>
                      </div>
                      <div className="tf-card-row">
                        <span>存储大小:</span>
                        <span>{formatBytes(tf.size_bytes)}</span>
                      </div>
                    </div>
                    <div className="tf-card-foot">
                      <button
                        type="button"
                        className="tf-refill-btn"
                        onClick={() => onRefillDownload(tf.interval)}
                      >
                        <Download size={12} />
                        增量补齐 {tf.interval}
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </td>
        </tr>
      )}
    </>
  )
}

function renderPaginationButtons(current: number, total: number, onSelect: (page: number) => void) {
  if (total <= 1) return null
  const pages: (number | string)[] = []
  if (total <= 7) {
    for (let i = 1; i <= total; i++) pages.push(i)
  } else {
    pages.push(1)
    if (current > 3) pages.push('ellipsis-start')
    const start = Math.max(2, current - 1)
    const end = Math.min(total - 1, current + 1)
    for (let i = start; i <= end; i++) {
      pages.push(i)
    }
    if (current < total - 2) pages.push('ellipsis-end')
    pages.push(total)
  }

  return pages.map((p, idx) => {
    if (typeof p === 'string') {
      return <span key={`ell-${idx}`} className="page-ellipsis">…</span>
    }
    return (
      <button
        key={`page-${p}`}
        type="button"
        className={`page-btn ${p === current ? 'active' : ''}`}
        onClick={() => onSelect(p as number)}
      >
        {p}
      </button>
    )
  })
}


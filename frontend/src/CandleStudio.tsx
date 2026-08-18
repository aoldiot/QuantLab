import {useEffect, useMemo, useRef, useState} from 'react'
import {
  AreaSeries,
  BaselineSeries,
  CandlestickSeries,
  ColorType,
  createChart,
  createSeriesMarkers,
  CrosshairMode,
  HistogramSeries,
  LineSeries,
  type IChartApi,
  type LogicalRange,
  type MouseEventParams,
  type UTCTimestamp,
} from 'lightweight-charts'
import {ChevronLeft, ChevronRight, Eye, EyeOff, History, Layers3, PanelLeftClose, PanelRightClose} from 'lucide-react'
import {api} from './api'
import type {ChartBar, ChartData, ChartFill, IndicatorPoint, PlotSeriesSpec, Run} from './types'

type Trade = {entry: ChartFill; exit: ChartFill; pnl: number; direction: 'LONG' | 'SHORT'}
type ChartSync = {charts: Set<IChartApi>; syncing: boolean; range: LogicalRange | null; focusIndex: number | null}
const percent = (value: number) => `${value >= 0 ? '+' : ''}${value.toFixed(2)}%`

const indicatorNameMap: Record<string, string> = {
  kc_mid: 'KC 中轨',
  bb_upper: '布林上轨',
  bb_lower: '布林下轨',
  bb_mid: '布林中轨',
  kc_upper: 'KC 上轨',
  kc_lower: 'KC 下轨',
  bb_width: '布林带宽',
  kc_width: 'KC 带宽',
  squeeze_on: '挤压开启',
  squeeze_off: '挤压释放',
  squeeze_momentum: '动量偏离',
  atr: 'ATR 波动率',
  vol_ma: '均量线',
  long_stop_ref: '多头止损轨',
  short_stop_ref: '空头止损轨',
  close: '收盘均线',
  open: '开盘价',
  high: '最高价',
  low: '最低价',
  volume: '成交量',
  sma: 'SMA 均线',
  ema: 'EMA 均线',
  rsi: 'RSI 强弱',
  macd: 'MACD 差离',
  macd_signal: 'MACD 信号',
  macd_hist: 'MACD 柱',
}

function getIndicatorLabel(column: string, spec?: PlotSeriesSpec): string {
  if (spec?.name) return spec.name
  return indicatorNameMap[column] || column
}

function formatNum(val: number | null | undefined): string {
  if (val == null || !Number.isFinite(val)) return '—'
  if (Math.abs(val) >= 1000) return val.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})
  if (Math.abs(val) >= 1) return val.toFixed(3)
  return val.toFixed(4)
}

function pairTrades(fills: ChartFill[]) {
  const result: Trade[] = []
  for (let i = 0; i + 1 < fills.length; i += 2) {
    const entry = fills[i], exit = fills[i + 1], direction = entry.side === 'BUY' ? 'LONG' : 'SHORT'
    const raw = ((exit.price - entry.price) / entry.price) * 100
    result.push({entry, exit, direction, pnl: direction === 'LONG' ? raw : -raw})
  }
  return result
}

function findNearestBar(bars: ChartBar[], time: number): ChartBar | undefined {
  let matched: ChartBar | undefined
  for (let i = 0; i < bars.length; i++) {
    if (bars[i].time <= time) matched = bars[i]
    else break
  }
  return matched || bars[0]
}

export default function CandleStudio({run}: {run: Run}) {
  const [data, setData] = useState<ChartData>()
  const [symbol, setSymbol] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [left, setLeft] = useState(true)
  const [right, setRight] = useState(true)

  const [enabledMainSeries, setEnabledMainSeries] = useState<Set<string>>(new Set())
  const [enabledPanes, setEnabledPanes] = useState<Set<string>>(new Set())

  const [symbolQuery, setSymbolQuery] = useState('')
  const [tradeSort, setTradeSort] = useState<'time-desc' | 'time-asc' | 'pnl-desc' | 'pnl-asc'>('time-desc')
  const [selectedTrade, setSelectedTrade] = useState<Trade | null>(null)

  useEffect(() => {
    let active = true
    setLoading(true)
    api.chart(run.id, symbol || undefined)
      .then(value => {
        if (active) {
          setData(value)
          setSymbol(value.symbol)
          setError('')
        }
      })
      .catch(reason => active && setError((reason as Error).message))
      .finally(() => active && setLoading(false))
    return () => {
      active = false
    }
  }, [run.id, symbol])

  useEffect(() => {
    if (data?.plot_config) {
      setEnabledMainSeries(new Set(Object.keys(data.plot_config.main_plot ?? {})))
      setEnabledPanes(new Set(Object.keys(data.plot_config.subplots ?? {})))
    }
  }, [data?.symbol, data?.plot_config])

  const history = useMemo(() => pairTrades(data?.fills ?? []), [data])
  const sortedHistory = useMemo(
    () =>
      [...history].sort((a, b) =>
        tradeSort === 'time-desc'
          ? b.exit.time - a.exit.time
          : tradeSort === 'time-asc'
          ? a.exit.time - b.exit.time
          : tradeSort === 'pnl-desc'
          ? b.pnl - a.pnl
          : a.pnl - b.pnl
      ),
    [history, tradeSort]
  )
  const filteredSymbols = useMemo(
    () => data?.symbols.filter(item => item.toLowerCase().includes(symbolQuery.trim().toLowerCase())) ?? [],
    [data, symbolQuery]
  )
  const returns = useMemo(
    () => Object.fromEntries((run.result?.contribution ?? []).map(item => [item.symbol, item.value])),
    [run]
  )

  const toggleMainSeries = (column: string) => {
    setEnabledMainSeries(current => {
      const next = new Set(current)
      if (next.has(column)) next.delete(column)
      else next.add(column)
      return next
    })
  }

  const togglePane = (name: string) => {
    setEnabledPanes(current => {
      const next = new Set(current)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })
  }

  if (loading && !data) return <div className="candle-loading">读取 K 线与成交记录…</div>
  if (error && !data)
    return (
      <section className="card candle-error">
        <h3>K 线数据不可用</h3>
        <p>{error}</p>
        <small>历史任务需包含 bars.parquet 与 fills.parquet；新回测会自动采集。</small>
      </section>
    )
  if (!data) return null

  const mainPlotConfig = data.plot_config?.main_plot ?? {}
  const subplotsConfig = data.plot_config?.subplots ?? {}
  const visiblePanes = Object.entries(subplotsConfig).filter(([name]) => enabledPanes.has(name))
  const studioHeight = 552 + visiblePanes.length * 180

  const hasMainPlots = Object.keys(mainPlotConfig).length > 0
  const hasSubplots = Object.keys(subplotsConfig).length > 0

  return (
    <section className={`candle-studio ${left ? 'has-left' : ''} ${right ? 'has-right' : ''}`} style={{height: studioHeight}}>
      <header className="candle-toolbar">
        <div>
          <b>{data.symbol}</b>
          <span>{String(data.timeframe ?? run.config.timeframes?.[0] ?? '回测周期')}</span>
          {data.truncated && <em>最近 5,000 根</em>}
        </div>

        {/* Distinct Main Plot and Subplot Indicator Legend Groups */}
        <div className="indicator-toolbar-sections">
          {hasMainPlots && (
            <div className="indicator-section">
              <span className="indicator-section-tag main-tag" title="主图叠加指标图例">
                <Layers3 size={11} />主图
              </span>
              <div className="legend-chips-container">
                {Object.entries(mainPlotConfig).map(([column, spec]) => {
                  const isActive = enabledMainSeries.has(column)
                  const label = getIndicatorLabel(column, spec)
                  return (
                    <button
                      key={column}
                      className={`indicator-legend-chip ${isActive ? 'active' : 'inactive'}`}
                      onClick={() => toggleMainSeries(column)}
                      title={`点击切换主图指标 [${label}] 显隐`}
                    >
                      <span className="legend-color-dot" style={{backgroundColor: spec.color || '#43a5ff'}} />
                      <span className="chip-label">{label}</span>
                      {isActive ? <Eye size={12} /> : <EyeOff size={12} />}
                    </button>
                  )
                })}
              </div>
            </div>
          )}

          {hasMainPlots && hasSubplots && <div className="section-divider" />}

          {hasSubplots && (
            <div className="indicator-section">
              <span className="indicator-section-tag sub-tag" title="副图技术指标图例">
                副图
              </span>
              <div className="legend-chips-container">
                {Object.entries(subplotsConfig).map(([name, seriesMap]) => {
                  const isActive = enabledPanes.has(name)
                  const seriesList = Object.values(seriesMap)
                  return (
                    <button
                      key={name}
                      className={`indicator-legend-chip ${isActive ? 'active' : 'inactive'}`}
                      onClick={() => togglePane(name)}
                      title={`点击切换副图窗口 [${name}] 显隐`}
                    >
                      <span className="multi-color-dots">
                        {seriesList.slice(0, 3).map((s, idx) => (
                          <i key={idx} style={{backgroundColor: s.color || '#27d0dd'}} />
                        ))}
                      </span>
                      <span className="chip-label">{name}</span>
                      {isActive ? <Eye size={12} /> : <EyeOff size={12} />}
                    </button>
                  )
                })}
              </div>
            </div>
          )}
        </div>

        <div>
          <button title="显示/隐藏标的列表" onClick={() => setLeft(!left)}>
            <PanelLeftClose />
          </button>
          <button title="显示/隐藏交易历史" onClick={() => setRight(!right)}>
            <PanelRightClose />
          </button>
        </div>
      </header>

      {left && (
        <aside className="symbol-float">
          <div className="float-title">
            <span>标的</span>
            <button onClick={() => setLeft(false)}>
              <ChevronLeft />
            </button>
          </div>
          <div className="symbol-search">
            <input value={symbolQuery} onChange={event => setSymbolQuery(event.target.value)} placeholder="搜索标的…" />
          </div>
          <div className="symbol-list">
            {filteredSymbols.map(item => (
              <button className={item === data.symbol ? 'active' : ''} onClick={() => setSymbol(item)} key={item}>
                <span>{item.replace('.BINANCE', '')}</span>
                <strong className={(returns[item] ?? 0) >= 0 ? 'gain' : 'loss'}>
                  {returns[item] == null ? '—' : percent(returns[item])}
                </strong>
              </button>
            ))}
            {!filteredSymbols.length && <p className="list-empty">没有匹配标的</p>}
          </div>
        </aside>
      )}

      <div className="chart-stage">
        <PriceChart
          data={data}
          panes={visiblePanes}
          selectedTrade={selectedTrade}
          enabledMainSeries={enabledMainSeries}
          onToggleMainSeries={toggleMainSeries}
          onTogglePane={togglePane}
        />
      </div>

      {right && (
        <aside className="history-float">
          <div className="float-title">
            <span>
              <History />交易历史 <small>{history.length}</small>
            </span>
            <button onClick={() => setRight(false)}>
              <ChevronRight />
            </button>
          </div>
          <div className="trade-sort">
            <select value={tradeSort} onChange={event => setTradeSort(event.target.value as typeof tradeSort)}>
              <option value="time-desc">时间：最新优先</option>
              <option value="time-asc">时间：最早优先</option>
              <option value="pnl-desc">收益：从高到低</option>
              <option value="pnl-asc">收益：从低到高</option>
            </select>
          </div>
          <div className="history-list">
            {sortedHistory.map((trade, index) => {
              const selected = selectedTrade?.entry.time === trade.entry.time && selectedTrade?.exit.time === trade.exit.time
              return (
                <button
                  className={`trade-record ${selected ? 'selected' : ''}`}
                  key={`${trade.exit.time}-${index}`}
                  onClick={() => setSelectedTrade(trade)}
                  title="定位到开仓 K 线"
                >
                  <div>
                    <b className={trade.direction === 'LONG' ? 'long' : 'short'}>{trade.direction === 'LONG' ? '多' : '空'}</b>
                    <span>{new Date(trade.exit.time * 1000).toLocaleString()}</span>
                  </div>
                  <strong className={trade.pnl >= 0 ? 'gain' : 'loss'}>{percent(trade.pnl)}</strong>
                  <small>
                    {trade.entry.price.toLocaleString()} → {trade.exit.price.toLocaleString()}
                  </small>
                </button>
              )
            })}
          </div>
        </aside>
      )}
    </section>
  )
}

function registerChart(sync: ChartSync, chart: IChartApi) {
  sync.charts.add(chart)
  if (sync.range) chart.timeScale().setVisibleLogicalRange(sync.range)
  const onRange = (range: LogicalRange | null) => {
    if (!range || sync.syncing) return
    sync.range = range
    sync.syncing = true
    for (const other of sync.charts) {
      if (other !== chart) other.timeScale().setVisibleLogicalRange(range)
    }
    sync.syncing = false
  }
  chart.timeScale().subscribeVisibleLogicalRangeChange(onRange)
  return () => {
    chart.timeScale().unsubscribeVisibleLogicalRangeChange(onRange)
    sync.charts.delete(chart)
  }
}

function addPlotSeries(chart: IChartApi, spec: PlotSeriesSpec, points: IndicatorPoint[], column?: string) {
  if (!points || points.length === 0) return null
  const title = getIndicatorLabel(column || '', spec)
  const common = {
    title,
    color: spec.color ?? '#43a5ff',
    lineWidth: (spec.lineWidth ?? 2) as 1 | 2 | 3 | 4,
    priceLineVisible: false,
    lastValueVisible: true,
  }
  if (spec.type === 'histogram') {
    const series = chart.addSeries(HistogramSeries, {...common})
    series.setData(
      points.map(point => ({
        time: point.time as UTCTimestamp,
        value: point.value,
        color: point.value >= 0 ? (spec.color ?? '#20c997') : '#ef5b6c',
      }))
    )
    return series
  }
  if (spec.type === 'area') {
    const series = chart.addSeries(AreaSeries, {
      ...common,
      lineColor: spec.color ?? '#43a5ff',
      topColor: `${spec.color ?? '#43a5ff'}66`,
      bottomColor: `${spec.color ?? '#43a5ff'}08`,
    })
    series.setData(points.map(point => ({time: point.time as UTCTimestamp, value: point.value})))
    return series
  }
  if (spec.type === 'baseline') {
    const series = chart.addSeries(BaselineSeries, {
      ...common,
      topLineColor: spec.color ?? '#20c997',
      bottomLineColor: '#ef5b6c',
    })
    series.setData(points.map(point => ({time: point.time as UTCTimestamp, value: point.value})))
    return series
  }
  const series = chart.addSeries(LineSeries, {...common})
  series.setData(points.map(point => ({time: point.time as UTCTimestamp, value: point.value})))
  return series
}

function PriceChart({
  data,
  panes,
  selectedTrade,
  enabledMainSeries,
  onToggleMainSeries,
  onTogglePane,
}: {
  data: ChartData
  panes: [string, Record<string, PlotSeriesSpec>][]
  selectedTrade: Trade | null
  enabledMainSeries: Set<string>
  onToggleMainSeries: (column: string) => void
  onTogglePane: (name: string) => void
}) {
  const mainRef = useRef<HTMLDivElement>(null)
  const syncRef = useRef<ChartSync>({charts: new Set(), syncing: false, range: null, focusIndex: null})
  const [pulse, setPulse] = useState<{entry: {x: number; y: number}; exit: {x: number; y: number}} | null>(null)
  const selectedRef = useRef<Trade | null>(selectedTrade)
  const pulseUpdaterRef = useRef<() => void>(() => {})

  const lastBar = data.bars[data.bars.length - 1]
  const [hoverBar, setHoverBar] = useState<ChartBar | null>(lastBar || null)
  const [hoverIndicators, setHoverIndicators] = useState<Record<string, number | null>>({})

  // Compute latest values for indicators
  const latestIndicatorValues = useMemo(() => {
    const res: Record<string, number | null> = {}
    for (const [col, points] of Object.entries(data.indicator_series)) {
      if (points && points.length > 0) res[col] = points[points.length - 1].value
      else res[col] = null
    }
    return res
  }, [data.indicator_series])

  useEffect(() => {
    selectedRef.current = selectedTrade
    const sync = syncRef.current
    if (!selectedTrade || !data.bars.length) {
      setPulse(null)
      return
    }
    let index = data.bars.findIndex(bar => bar.time >= selectedTrade.entry.time)
    if (index < 0) index = data.bars.length - 1
    const from = Math.max(0, index - 25)
    const to = Math.min(data.bars.length - 1 + 10, index + 35)
    sync.focusIndex = index
    sync.syncing = true
    for (const chart of sync.charts) {
      chart.timeScale().setVisibleLogicalRange({from, to})
    }
    sync.syncing = false
    requestAnimationFrame(() => {
      pulseUpdaterRef.current()
      requestAnimationFrame(pulseUpdaterRef.current)
    })
  }, [selectedTrade, data])

  useEffect(() => {
    if (!mainRef.current) return
    const chart = createChart(mainRef.current, {
      ...chartOptions,
      width: mainRef.current.clientWidth || 800,
      height: 500,
    })
    const unregister = registerChart(syncRef.current, chart)
    const candles = chart.addSeries(CandlestickSeries, {
      upColor: '#20c997',
      downColor: '#ef5b6c',
      wickUpColor: '#20c997',
      wickDownColor: '#ef5b6c',
      borderVisible: false,
    })
    candles.setData(data.bars.map(bar => ({...bar, time: bar.time as UTCTimestamp})))

    const volume = chart.addSeries(HistogramSeries, {priceFormat: {type: 'volume'}, priceScaleId: 'vol'})
    volume.priceScale().applyOptions({scaleMargins: {top: 0.82, bottom: 0}})
    volume.setData(
      data.bars.map(bar => ({
        time: bar.time as UTCTimestamp,
        value: bar.volume,
        color: bar.close >= bar.open ? '#1f8f7055' : '#a8425055',
      }))
    )

    // Render active main plot indicators
    for (const [column, spec] of Object.entries(data.plot_config?.main_plot ?? {})) {
      if (enabledMainSeries.has(column)) {
        addPlotSeries(chart, spec, data.indicator_series[column] ?? [], column)
      }
    }

    createSeriesMarkers(
      candles,
      pairTrades(data.fills)
        .flatMap(trade => [
          {
            time: trade.entry.time as UTCTimestamp,
            position: trade.direction === 'LONG' ? ('belowBar' as const) : ('aboveBar' as const),
            color: trade.direction === 'LONG' ? '#20c997' : '#ef5b6c',
            shape: trade.direction === 'LONG' ? ('arrowUp' as const) : ('arrowDown' as const),
            text: trade.direction === 'LONG' ? '开多' : '开空',
          },
          {
            time: trade.exit.time as UTCTimestamp,
            position: trade.direction === 'LONG' ? ('aboveBar' as const) : ('belowBar' as const),
            color: trade.pnl >= 0 ? '#20c997' : '#ef5b6c',
            shape: 'circle' as const,
            text: `平 ${percent(trade.pnl)}`,
          },
        ])
        .sort((a, b) => Number(a.time) - Number(b.time))
    )

    if (syncRef.current.range) chart.timeScale().setVisibleLogicalRange(syncRef.current.range)
    else chart.timeScale().fitContent()

    // Crosshair movement handler for live overlay values
    const onCrosshairMove = (param: MouseEventParams) => {
      if (!param.time || !data.bars.length) {
        setHoverBar(lastBar || null)
        setHoverIndicators(latestIndicatorValues)
        return
      }
      const timeNum = Number(param.time)
      const currentBar = data.bars.find(b => b.time === timeNum) || findNearestBar(data.bars, timeNum)
      setHoverBar(currentBar || lastBar || null)

      const indVals: Record<string, number | null> = {}
      for (const [col, points] of Object.entries(data.indicator_series)) {
        const pt = points.find(p => p.time === timeNum)
        indVals[col] = pt ? pt.value : null
      }
      setHoverIndicators(indVals)
    }

    chart.subscribeCrosshairMove(onCrosshairMove)

    const updatePulse = () => {
      const trade = selectedRef.current
      if (!trade || !data.bars.length) {
        setPulse(null)
        return
      }
      const entryBar = findNearestBar(data.bars, trade.entry.time)
      const exitBar = findNearestBar(data.bars, trade.exit.time)
      const entryTime = entryBar ? entryBar.time : trade.entry.time
      const exitTime = exitBar ? exitBar.time : trade.exit.time

      const ex = chart.timeScale().timeToCoordinate(entryTime as UTCTimestamp)
      const xx = chart.timeScale().timeToCoordinate(exitTime as UTCTimestamp)
      const ey = candles.priceToCoordinate(trade.entry.price)
      const xy = candles.priceToCoordinate(trade.exit.price)

      if (
        ex != null &&
        xx != null &&
        ey != null &&
        xy != null &&
        Number.isFinite(ex) &&
        Number.isFinite(xx) &&
        Number.isFinite(ey) &&
        Number.isFinite(xy)
      ) {
        setPulse({entry: {x: ex, y: ey}, exit: {x: xx, y: xy}})
      } else {
        setPulse(null)
      }
    }

    pulseUpdaterRef.current = updatePulse
    chart.timeScale().subscribeVisibleLogicalRangeChange(updatePulse)
    requestAnimationFrame(updatePulse)

    const observer = new ResizeObserver(entries => {
      const width = entries[0]?.contentRect.width ?? mainRef.current?.clientWidth ?? 0
      if (width > 0) {
        chart.applyOptions({width})
        requestAnimationFrame(updatePulse)
      }
    })
    observer.observe(mainRef.current)

    return () => {
      observer.disconnect()
      chart.unsubscribeCrosshairMove(onCrosshairMove)
      chart.timeScale().unsubscribeVisibleLogicalRangeChange(updatePulse)
      pulseUpdaterRef.current = () => {}
      unregister()
      chart.remove()
    }
  }, [data, enabledMainSeries, lastBar, latestIndicatorValues])

  const curBar = hoverBar || lastBar
  const isUp = curBar ? curBar.close >= curBar.open : true
  const barChange = curBar && curBar.open > 0 ? ((curBar.close - curBar.open) / curBar.open) * 100 : 0

  return (
    <>
      <div className="main-chart-wrapper">
        <div className="main-chart" ref={mainRef} />

        {/* TradingView-style Interactive Floating Legend Overlay */}
        <div className="chart-floating-overlay">
          {curBar && (
            <div className="ohlcv-legend-row">
              <b>{new Date(curBar.time * 1000).toLocaleString('zh-CN', {month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit'})}</b>
              <span>开: <b className="ohlcv-val">{formatNum(curBar.open)}</b></span>
              <span>高: <b className="ohlcv-val">{formatNum(curBar.high)}</b></span>
              <span>低: <b className="ohlcv-val">{formatNum(curBar.low)}</b></span>
              <span>收: <b className={`ohlcv-val ${isUp ? 'gain' : 'loss'}`}>{formatNum(curBar.close)}</b></span>
              <span className={isUp ? 'gain' : 'loss'}>{percent(barChange)}</span>
              <span>量: <b className="ohlcv-val">{formatNum(curBar.volume)}</b></span>
            </div>
          )}

          {/* Main Plot Indicators Legend List with live values */}
          <div className="main-legend-items-row">
            {Object.entries(data.plot_config?.main_plot ?? {}).map(([col, spec]) => {
              const active = enabledMainSeries.has(col)
              const val = hoverIndicators[col] ?? latestIndicatorValues[col]
              const label = getIndicatorLabel(col, spec)
              return (
                <button
                  key={col}
                  className={`overlay-legend-item ${active ? 'active' : 'dimmed'}`}
                  onClick={() => onToggleMainSeries(col)}
                  title={`点击切换主图指标 [${label}]`}
                >
                  <span className="legend-color-dot" style={{backgroundColor: spec.color || '#43a5ff'}} />
                  <span>{label}:</span>
                  <span className="item-val">{active ? formatNum(val) : '隐藏'}</span>
                  {active ? <Eye size={10} /> : <EyeOff size={10} />}
                </button>
              )
            })}
          </div>
        </div>

        {pulse && (
          <div className="trade-pulse-layer">
            <i className="trade-pulse entry-pulse" style={{left: pulse.entry.x, top: pulse.entry.y}} />
            <i className="trade-pulse exit-pulse" style={{left: pulse.exit.x, top: pulse.exit.y}} />
          </div>
        )}
      </div>

      {panes.map(([name, series]) => (
        <IndicatorChart
          key={name}
          data={data}
          name={name}
          series={series}
          sync={syncRef.current}
          onClose={() => onTogglePane(name)}
        />
      ))}
    </>
  )
}

const chartOptions = {
  layout: {
    background: {type: ColorType.Solid, color: '#08111a'},
    textColor: '#8192a6',
    attributionLogo: false,
  },
  grid: {
    vertLines: {color: '#142230'},
    horzLines: {color: '#142230'},
  },
  rightPriceScale: {borderColor: '#263748'},
  timeScale: {
    borderColor: '#263748',
    timeVisible: true,
    secondsVisible: false,
  },
  crosshair: {
    mode: CrosshairMode.Normal,
    vertLine: {color: '#46627a'},
    horzLine: {color: '#46627a'},
  },
}

function IndicatorChart({
  data,
  name,
  series,
  sync,
  onClose,
}: {
  data: ChartData
  name: string
  series: Record<string, PlotSeriesSpec>
  sync: ChartSync
  onClose: () => void
}) {
  const ref = useRef<HTMLDivElement>(null)
  const [hoverVals, setHoverVals] = useState<Record<string, number | null>>({})

  // Compute latest values for indicators in this pane
  const latestVals = useMemo(() => {
    const res: Record<string, number | null> = {}
    for (const column of Object.keys(series)) {
      const points = data.indicator_series[column]
      if (points && points.length > 0) res[column] = points[points.length - 1].value
      else res[column] = null
    }
    return res
  }, [data.indicator_series, series])

  useEffect(() => {
    if (!ref.current) return
    const chart = createChart(ref.current, {
      ...chartOptions,
      width: ref.current.clientWidth || 800,
      height: 180,
    })
    const unregister = registerChart(sync, chart)
    for (const [column, spec] of Object.entries(series)) {
      addPlotSeries(chart, spec, data.indicator_series[column] ?? [], column)
    }
    if (sync.range) chart.timeScale().setVisibleLogicalRange(sync.range)
    else chart.timeScale().fitContent()

    const onCrosshairMove = (param: MouseEventParams) => {
      if (!param.time) {
        setHoverVals(latestVals)
        return
      }
      const timeNum = Number(param.time)
      const vals: Record<string, number | null> = {}
      for (const column of Object.keys(series)) {
        const points = data.indicator_series[column] ?? []
        const pt = points.find(p => p.time === timeNum)
        vals[column] = pt ? pt.value : null
      }
      setHoverVals(vals)
    }

    chart.subscribeCrosshairMove(onCrosshairMove)

    const observer = new ResizeObserver(entries => {
      const width = entries[0]?.contentRect.width ?? ref.current?.clientWidth ?? 0
      if (width > 0) chart.applyOptions({width})
    })
    observer.observe(ref.current)

    return () => {
      observer.disconnect()
      chart.unsubscribeCrosshairMove(onCrosshairMove)
      unregister()
      chart.remove()
    }
  }, [data, name, series, sync, latestVals])

  return (
    <div className="sub-chart">
      <div className="sub-chart-header">
        <div className="sub-chart-title-group">
          <span className="sub-chart-name">{name}</span>
          <div className="sub-chart-legend-list">
            {Object.entries(series).map(([col, spec]) => {
              const val = hoverVals[col] ?? latestVals[col]
              const label = getIndicatorLabel(col, spec)
              return (
                <span key={col} className="sub-legend-item">
                  <span className="legend-color-dot" style={{backgroundColor: spec.color || '#43a5ff'}} />
                  <span>{label}:</span>
                  <b>{formatNum(val)}</b>
                </span>
              )
            })}
          </div>
        </div>
        <button className="sub-chart-close-btn" onClick={onClose} title={`隐藏副图 [${name}]`}>
          <EyeOff size={12} />
        </button>
      </div>
      <div ref={ref} />
    </div>
  )
}

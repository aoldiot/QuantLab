import {useEffect,useMemo,useRef,useState} from 'react'
import {AreaSeries,BaselineSeries,CandlestickSeries,ColorType,createChart,createSeriesMarkers,CrosshairMode,HistogramSeries,LineSeries,type IChartApi,type ISeriesApi,type LogicalRange,type SeriesType,type UTCTimestamp} from 'lightweight-charts'
import {ChevronLeft,ChevronRight,History,Layers3,PanelLeftClose,PanelRightClose} from 'lucide-react'
import {api} from './api'
import type {ChartData,ChartFill,IndicatorPoint,PlotSeriesSpec,Run} from './types'

type Trade={entry:ChartFill;exit:ChartFill;pnl:number;direction:'LONG'|'SHORT'}
type ChartSync={charts:Set<IChartApi>;syncing:boolean;range:LogicalRange|null;focusIndex:number|null}
const percent=(value:number)=>`${value>=0?'+':''}${value.toFixed(2)}%`
function pairTrades(fills:ChartFill[]){const result:Trade[]=[];for(let i=0;i+1<fills.length;i+=2){const entry=fills[i],exit=fills[i+1],direction=entry.side==='BUY'?'LONG':'SHORT';const raw=(exit.price-entry.price)/entry.price*100;result.push({entry,exit,direction,pnl:direction==='LONG'?raw:-raw})}return result}

export default function CandleStudio({run}:{run:Run}){
  const [data,setData]=useState<ChartData>(),[symbol,setSymbol]=useState(''),[error,setError]=useState(''),[loading,setLoading]=useState(true)
  const [left,setLeft]=useState(true),[right,setRight]=useState(true),[enabledPanes,setEnabledPanes]=useState<Set<string>>(new Set())
  const [symbolQuery,setSymbolQuery]=useState(''),[tradeSort,setTradeSort]=useState<'time-desc'|'time-asc'|'pnl-desc'|'pnl-asc'>('time-desc')
  const [selectedTrade,setSelectedTrade]=useState<Trade|null>(null)
  useEffect(()=>{let active=true;setLoading(true);api.chart(run.id,symbol||undefined).then(value=>{if(active){setData(value);setSymbol(value.symbol);setError('')}}).catch(reason=>active&&setError((reason as Error).message)).finally(()=>active&&setLoading(false));return()=>{active=false}},[run.id,symbol])
  useEffect(()=>{if(data)setEnabledPanes(new Set(Object.keys(data.plot_config?.subplots??{})))},[data?.symbol,data?.plot_config])
  const history=useMemo(()=>pairTrades(data?.fills??[]),[data])
  const sortedHistory=useMemo(()=>[...history].sort((a,b)=>tradeSort==='time-desc'?b.exit.time-a.exit.time:tradeSort==='time-asc'?a.exit.time-b.exit.time:tradeSort==='pnl-desc'?b.pnl-a.pnl:a.pnl-b.pnl),[history,tradeSort])
  const filteredSymbols=useMemo(()=>data?.symbols.filter(item=>item.toLowerCase().includes(symbolQuery.trim().toLowerCase()))??[],[data,symbolQuery])
  const returns=useMemo(()=>Object.fromEntries((run.result?.contribution??[]).map(item=>[item.symbol,item.value])),[run])
  if(loading&&!data)return <div className="candle-loading">读取 K 线与成交记录…</div>
  if(error&&!data)return <section className="card candle-error"><h3>K 线数据不可用</h3><p>{error}</p><small>历史任务需包含 bars.parquet 与 fills.parquet；新回测会自动采集。</small></section>
  if(!data)return null
  const visiblePanes=Object.entries(data.plot_config?.subplots??{}).filter(([name])=>enabledPanes.has(name))
  const studioHeight=552+visiblePanes.length*180
  return <section className={`candle-studio ${left?'has-left':''} ${right?'has-right':''}`} style={{height:studioHeight}}>
    <header className="candle-toolbar"><div><b>{data.symbol}</b><span>{String(data.timeframe??run.config.timeframes?.[0]??'回测周期')}</span>{data.truncated&&<em>最近 5,000 根</em>}</div><div className="indicator-switches"><Layers3/><button className="on">主图指标</button>{Object.keys(data.plot_config?.subplots??{}).map(name=><button key={name} className={enabledPanes.has(name)?'on':''} onClick={()=>setEnabledPanes(current=>{const next=new Set(current);next.has(name)?next.delete(name):next.add(name);return next})}>{name}</button>)}</div><div><button title="显示/隐藏标的列表" onClick={()=>setLeft(!left)}><PanelLeftClose/></button><button title="显示/隐藏交易历史" onClick={()=>setRight(!right)}><PanelRightClose/></button></div></header>
    {left&&<aside className="symbol-float"><div className="float-title"><span>标的</span><button onClick={()=>setLeft(false)}><ChevronLeft/></button></div><div className="symbol-search"><input value={symbolQuery} onChange={event=>setSymbolQuery(event.target.value)} placeholder="搜索标的…"/></div><div className="symbol-list">{filteredSymbols.map(item=><button className={item===data.symbol?'active':''} onClick={()=>setSymbol(item)} key={item}><span>{item.replace('.BINANCE','')}</span><strong className={(returns[item]??0)>=0?'gain':'loss'}>{returns[item]==null?'—':percent(returns[item])}</strong></button>)}{!filteredSymbols.length&&<p className="list-empty">没有匹配标的</p>}</div></aside>}
    <div className="chart-stage"><PriceChart data={data} panes={visiblePanes} selectedTrade={selectedTrade}/></div>
    {right&&<aside className="history-float"><div className="float-title"><span><History/>交易历史 <small>{history.length}</small></span><button onClick={()=>setRight(false)}><ChevronRight/></button></div><div className="trade-sort"><select value={tradeSort} onChange={event=>setTradeSort(event.target.value as typeof tradeSort)}><option value="time-desc">时间：最新优先</option><option value="time-asc">时间：最早优先</option><option value="pnl-desc">收益：从高到低</option><option value="pnl-asc">收益：从低到高</option></select></div><div className="history-list">{sortedHistory.map((trade,index)=>{const selected=selectedTrade?.entry.time===trade.entry.time&&selectedTrade?.exit.time===trade.exit.time;return <button className={`trade-record ${selected?'selected':''}`} key={`${trade.exit.time}-${index}`} onClick={()=>setSelectedTrade(trade)} title="定位到开仓 K 线"><div><b className={trade.direction==='LONG'?'long':'short'}>{trade.direction==='LONG'?'多':'空'}</b><span>{new Date(trade.exit.time*1000).toLocaleString()}</span></div><strong className={trade.pnl>=0?'gain':'loss'}>{percent(trade.pnl)}</strong><small>{trade.entry.price.toLocaleString()} → {trade.exit.price.toLocaleString()}</small></button>})}</div></aside>}
  </section>
}

function registerChart(sync:ChartSync,chart:IChartApi){
  sync.charts.add(chart)
  if(sync.range)chart.timeScale().setVisibleLogicalRange(sync.range)
  const onRange=(range:LogicalRange|null)=>{if(!range||sync.syncing)return;sync.range=range;sync.syncing=true;for(const other of sync.charts)if(other!==chart)other.timeScale().setVisibleLogicalRange(range);sync.syncing=false}
  chart.timeScale().subscribeVisibleLogicalRangeChange(onRange)
  return()=>{chart.timeScale().unsubscribeVisibleLogicalRangeChange(onRange);sync.charts.delete(chart)}
}

function addPlotSeries(chart:IChartApi,spec:PlotSeriesSpec,points:IndicatorPoint[]){
  const common={title:spec.name,color:spec.color??'#43a5ff',lineWidth:(spec.lineWidth??2) as 1|2|3|4,priceLineVisible:false,lastValueVisible:true}
  if(spec.type==='histogram'){
    const series=chart.addSeries(HistogramSeries,{...common})
    series.setData(points.map(point=>({time:point.time as UTCTimestamp,value:point.value,color:point.value>=0?(spec.color??'#20c997'):'#ef5b6c'})))
    return
  }
  if(spec.type==='area'){
    const series=chart.addSeries(AreaSeries,{...common,lineColor:spec.color??'#43a5ff',topColor:`${spec.color??'#43a5ff'}66`,bottomColor:`${spec.color??'#43a5ff'}08`})
    series.setData(points.map(point=>({time:point.time as UTCTimestamp,value:point.value})))
    return
  }
  if(spec.type==='baseline'){
    const series=chart.addSeries(BaselineSeries,{...common,topLineColor:spec.color??'#20c997',bottomLineColor:'#ef5b6c'})
    series.setData(points.map(point=>({time:point.time as UTCTimestamp,value:point.value})))
    return
  }
  const series=chart.addSeries(LineSeries,{...common})
  series.setData(points.map(point=>({time:point.time as UTCTimestamp,value:point.value})))
}

function PriceChart({data,panes,selectedTrade}:{data:ChartData;panes:[string,Record<string,PlotSeriesSpec>][];selectedTrade:Trade|null}){
  const mainRef=useRef<HTMLDivElement>(null)
  const syncRef=useRef<ChartSync>({charts:new Set(),syncing:false,range:null,focusIndex:null})
  const [pulse,setPulse]=useState<{entry:{x:number;y:number};exit:{x:number;y:number}}|null>(null)
  const selectedRef=useRef<Trade|null>(selectedTrade)
  const pulseUpdaterRef=useRef<()=>void>(()=>{})
  useEffect(()=>{selectedRef.current=selectedTrade;const sync=syncRef.current;if(!selectedTrade||!data.bars.length){setPulse(null);return}let index=data.bars.findIndex(bar=>bar.time>=selectedTrade.entry.time);if(index<0)index=data.bars.length-1;const center=data.bars[index].time;const interval=data.bars.length>1?Math.max(1,data.bars[1].time-data.bars[0].time):3600;const from=Math.max(data.bars[0].time,center-45*interval) as UTCTimestamp;const to=Math.min(data.bars[data.bars.length-1].time,center+45*interval) as UTCTimestamp;sync.focusIndex=index;sync.syncing=true;for(const chart of sync.charts)chart.timeScale().setVisibleRange({from,to});sync.syncing=false;requestAnimationFrame(()=>{pulseUpdaterRef.current();requestAnimationFrame(pulseUpdaterRef.current)})},[selectedTrade,data])
  useEffect(()=>{if(!mainRef.current)return;const chart=createChart(mainRef.current,{...chartOptions,width:mainRef.current.clientWidth,height:500});const unregister=registerChart(syncRef.current,chart);const candles=chart.addSeries(CandlestickSeries,{upColor:'#20c997',downColor:'#ef5b6c',wickUpColor:'#20c997',wickDownColor:'#ef5b6c',borderVisible:false});candles.setData(data.bars.map(bar=>({...bar,time:bar.time as UTCTimestamp})));const volume=chart.addSeries(HistogramSeries,{priceFormat:{type:'volume'},priceScaleId:'vol'});volume.priceScale().applyOptions({scaleMargins:{top:.82,bottom:0}});volume.setData(data.bars.map(bar=>({time:bar.time as UTCTimestamp,value:bar.volume,color:bar.close>=bar.open?'#1f8f7055':'#a8425055'})));for(const [column,spec] of Object.entries(data.plot_config?.main_plot??{}))addPlotSeries(chart,spec,data.indicator_series[column]??[]);createSeriesMarkers(candles,pairTrades(data.fills).flatMap(trade=>[{time:trade.entry.time as UTCTimestamp,position:trade.direction==='LONG'?'belowBar' as const:'aboveBar' as const,color:trade.direction==='LONG'?'#20c997':'#ef5b6c',shape:trade.direction==='LONG'?'arrowUp' as const:'arrowDown' as const,text:trade.direction==='LONG'?'开多':'开空'},{time:trade.exit.time as UTCTimestamp,position:trade.direction==='LONG'?'aboveBar' as const:'belowBar' as const,color:trade.pnl>=0?'#20c997':'#ef5b6c',shape:'circle' as const,text:`平 ${percent(trade.pnl)}`}]).sort((a,b)=>Number(a.time)-Number(b.time)));if(syncRef.current.range)chart.timeScale().setVisibleLogicalRange(syncRef.current.range);else chart.timeScale().fitContent();const updatePulse=()=>{const trade=selectedRef.current;if(!trade){setPulse(null);return}const ex=chart.timeScale().timeToCoordinate(trade.entry.time as UTCTimestamp),xx=chart.timeScale().timeToCoordinate(trade.exit.time as UTCTimestamp),ey=candles.priceToCoordinate(trade.entry.price),xy=candles.priceToCoordinate(trade.exit.price);if(ex!=null&&xx!=null&&ey!=null&&xy!=null)setPulse({entry:{x:ex,y:ey},exit:{x:xx,y:xy}});else setPulse(null)};pulseUpdaterRef.current=updatePulse;chart.timeScale().subscribeVisibleLogicalRangeChange(updatePulse);requestAnimationFrame(updatePulse);const observer=new ResizeObserver(entries=>{chart.applyOptions({width:entries[0]?.contentRect.width??mainRef.current?.clientWidth??0});requestAnimationFrame(updatePulse)});observer.observe(mainRef.current);return()=>{observer.disconnect();chart.timeScale().unsubscribeVisibleLogicalRangeChange(updatePulse);pulseUpdaterRef.current=()=>{};unregister();chart.remove()}},[data]);return <><div className="main-chart" ref={mainRef}>{pulse&&<><i className="trade-pulse entry-pulse" style={{left:pulse.entry.x,top:pulse.entry.y}}/><i className="trade-pulse exit-pulse" style={{left:pulse.exit.x,top:pulse.exit.y}}/></>}</div>{panes.map(([name,series])=><IndicatorChart key={name} data={data} name={name} series={series} sync={syncRef.current}/>)}</>
}

const chartOptions={layout:{background:{type:ColorType.Solid,color:'#08111a'},textColor:'#8192a6',attributionLogo:false},grid:{vertLines:{color:'#142230'},horzLines:{color:'#142230'}},rightPriceScale:{borderColor:'#263748'},timeScale:{borderColor:'#263748',timeVisible:true,secondsVisible:false},crosshair:{mode:CrosshairMode.Normal,vertLine:{color:'#46627a'},horzLine:{color:'#46627a'}}}

function IndicatorChart({data,name,series,sync}:{data:ChartData;name:string;series:Record<string,PlotSeriesSpec>;sync:ChartSync}){
  const ref=useRef<HTMLDivElement>(null)
  useEffect(()=>{if(!ref.current)return;const chart=createChart(ref.current,{...chartOptions,width:ref.current.clientWidth,height:180});const unregister=registerChart(sync,chart);for(const [column,spec] of Object.entries(series))addPlotSeries(chart,spec,data.indicator_series[column]??[]);if(sync.range)chart.timeScale().setVisibleLogicalRange(sync.range);else chart.timeScale().fitContent();const observer=new ResizeObserver(entries=>chart.applyOptions({width:entries[0]?.contentRect.width??ref.current?.clientWidth??0}));observer.observe(ref.current);return()=>{observer.disconnect();unregister();chart.remove()}},[data,name,series,sync]);return <div className="sub-chart"><span>{name}</span><div ref={ref}/></div>
}

import {useEffect,useRef,useState} from 'react'
import {AlertTriangle,ArrowLeft,CandlestickChart,Check,Copy,Download,Play,RefreshCw,RotateCw,Search,ShieldCheck,Terminal,Trash2,Wrench,X} from 'lucide-react'
import {Link,useNavigate,useParams} from 'react-router-dom'
import {Area,AreaChart,Bar,BarChart,CartesianGrid,Line,LineChart,ResponsiveContainer,Tooltip,XAxis,YAxis} from 'recharts'
import {api} from '../api'
import {Card,Metric} from '../components'
import type {CatalogCheckResponse,Run} from '../types'
import {getClientId} from '../utils'
import CandleStudio from '../CandleStudio'

type Tab='overview'|'candles'|'parameters'|'logs'
const terminal=new Set(['COMPLETED','FAILED','CANCELED'])
const labels:Record<string,string>={strategy_version_id:'策略版本 ID',strategy_parameters:'策略参数',venue:'市场',symbols:'交易品种',timeframes:'周期',start_date:'开始日期',end_date:'结束日期',initial_balance:'初始资金',leverage:'杠杆',execution_model:'成交模型',funding:'资金费率',catalog_path:'Catalog 路径',chunk_size:'分块大小',strategy_revision:'代码版本',check_data_integrity:'检查数据完整性'}

export default function Result(){
  const{id}=useParams(),navigate=useNavigate(),[run,setRun]=useState<Run>(),[tab,setTab]=useState<Tab>('overview'),[strategyId,setStrategyId]=useState(''),[error,setError]=useState(''),[repairing,setRepairing]=useState(false),[repairError,setRepairError]=useState(''),[actionBusy,setActionBusy]=useState(false)
  useEffect(()=>{let active=true,timer:number|undefined;const load=async()=>{try{const value=await api.run(id!);if(!active)return;setRun(value);setError('');if(!terminal.has(value.status))timer=window.setTimeout(load,1200)}catch(reason){if(active)setError((reason as Error).message)}};load();return()=>{active=false;if(timer)clearTimeout(timer)}},[id])
  useEffect(()=>{if(!run)return;api.strategies().then(async strategies=>{for(const strategy of strategies){const versions=await api.versions(strategy.id);if(versions.some(v=>v.id===String(run.config.strategy_version_id))){setStrategyId(strategy.id);break}}}).catch(()=>{})},[run])
  if(error)return <Card className="failure"><h2>回测详情加载失败</h2><p>{error}</p></Card>
  if(!run)return <div className="loading">加载回测任务…</div>
  const copyRun=()=>navigate('/backtests/new',{state:{copiedConfig:run.config,strategyId}})
  const repair=()=>{if(!run.research_project_id)return;navigate('/research',{state:{projectId:run.research_project_id,autoPrompt:`回测任务「${run.name}」运行失败，错误日志：${run.error_message||'未知错误'}。请深入分析报错原因并进行1次策略代码修复（【系统安全限制】：只修改代码，禁止自动执行回测）。`}})}
  const confirmRun=async(ignoreMissing:boolean)=>{if(actionBusy)return;setActionBusy(true);setError('');try{const updated=await api.confirmRun(run.id,{ignore_missing_data:ignoreMissing});setRun(updated)}catch(e){setError((e as Error).message)}finally{setActionBusy(false)}}
  const cancelRun=async()=>{if(actionBusy)return;setActionBusy(true);setError('');try{const updated=await api.cancelRun(run.id);setRun(updated)}catch(e){setError((e as Error).message)}finally{setActionBusy(false)}}
  const value=(item:unknown)=>item==null||item===''?'—':Array.isArray(item)?item.join(' / '):typeof item==='object'?JSON.stringify(item,null,2):String(item)
  return <><div className="strategy-title backtest-title"><Link className="detail-back" to="/backtests" aria-label="返回回测管理" title="返回回测管理"><ArrowLeft/></Link><div><h1>{run.name}</h1><small>{run.config.start_date} — {run.config.end_date}</small></div></div>
    <div className="detail-tabs">{([['overview','回测总览'],['candles','K 线'],['parameters','回测参数'],['logs','运行日志']] as [Tab,string][]).map(([key,label])=><button className={tab===key?'active':''} onClick={()=>setTab(key)} key={key}>{key==='candles'&&<CandlestickChart/>}{key==='logs'&&<Terminal/>}{label}</button>)}</div>
    {tab==='overview'&&<Overview run={run} onRepair={run.research_project_id?repair:undefined} repairing={repairing} repairError={repairError} onConfirm={confirmRun} onCancel={cancelRun} actionBusy={actionBusy} onViewLogs={()=>setTab('logs')}/>}
    {tab==='candles'&&<CandleStudio run={run}/>}
    {tab==='parameters'&&<section className="card parameter-page"><div className="section-title"><div><h3>本次回测配置</h3><p>以下参数为任务创建时锁定的完整配置。</p></div><button className="button primary" onClick={copyRun}><Copy/>复制参数重新回测</button></div><dl className="parameter-list">{Object.entries(run.config).filter(([key])=>key!=='strategy_revision'&&key!=='catalog_check'&&key!=='waiting_confirmation').map(([key,item])=><div key={key}><dt>{labels[key]??key}</dt><dd><pre>{value(item)}</pre></dd></div>)}</dl></section>}
    {tab==='logs'&&<LogViewer runId={run.id} runStatus={run.status}/>}
  </>
}

function DataCheckConfirmationCard({run,onConfirm,onCancel,actionBusy,onViewLogs}:{run:Run;onConfirm:(ignoreMissing:boolean)=>void;onCancel:()=>void;actionBusy:boolean;onViewLogs:()=>void}){
  const checkResult=run.config?.catalog_check as CatalogCheckResponse|undefined
  const hasMissing=checkResult?.has_missing??false
  const details=checkResult?.details??[]
  return (
    <div className={`data-check-card ${hasMissing?'warning-state':''}`}>
      <div className="data-check-header">
        <div className="data-check-icon">
          {hasMissing?<AlertTriangle/>:<ShieldCheck/>}
        </div>
        <div className="data-check-title">
          <h2>数据完整性检查已完成</h2>
          <p>{checkResult?.summary_text||'标的数据覆盖度验证已完成'}</p>
        </div>
      </div>
      {details.length>0&&(
        <div className="data-check-details">
          <table className="data-check-table">
            <thead>
              <tr>
                <th>标的品种</th>
                <th>周期</th>
                <th>状态</th>
                <th>详情说明</th>
              </tr>
            </thead>
            <tbody>
              {details.map((item,idx)=>(
                <tr key={`${item.symbol}-${item.timeframe}-${idx}`}>
                  <td><strong>{item.symbol}</strong></td>
                  <td>{item.timeframe}</td>
                  <td>
                    <span className={`data-check-pill ${item.status==='OK'?'ok':item.status==='PARTIAL_RANGE'?'partial':'missing'}`}>
                      {item.status==='OK'?'完整':item.status==='PARTIAL_RANGE'?'范围不足':'缺失'}
                    </span>
                  </td>
                  <td>{item.message}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="data-check-actions">
        <div className="data-check-prompt">
          {hasMissing?'检测到部分标的缺少行情数据。是否忽略缺失数据继续执行回测？':'所有标的行情数据均已完备，是否立即开始回测？'}
        </div>
        <div className="data-check-btn-group">
          <button type="button" className="button" onClick={onViewLogs}>
            <Terminal size={14}/>查看日志
          </button>
          <button type="button" className="button" disabled={actionBusy} onClick={onCancel}>
            <X size={14}/>取消回测
          </button>
          <button type="button" className="button primary" disabled={actionBusy} onClick={()=>onConfirm(hasMissing)}>
            <Play size={14}/>{actionBusy?'正在启动…':hasMissing?'忽略缺失并启动回测':'立即开始回测'}
          </button>
        </div>
      </div>
    </div>
  )
}

function Overview({run,onRepair,repairing,repairError,onConfirm,onCancel,actionBusy,onViewLogs}:{run:Run;onRepair?:()=>void;repairing:boolean;repairError:string;onConfirm:(ignoreMissing:boolean)=>void;onCancel:()=>void;actionBusy:boolean;onViewLogs:()=>void}){
  const isWaitingConfirmation=Boolean(run.config?.waiting_confirmation)||run.stage==='数据检查完成，等待确认'
  if(isWaitingConfirmation){
    return <DataCheckConfirmationCard run={run} onConfirm={onConfirm} onCancel={onCancel} actionBusy={actionBusy} onViewLogs={onViewLogs}/>
  }
  if(run.status==='FAILED')return <Card className="failure"><div className="failure-heading"><div><h2>{run.stage}</h2><p>回测未生成结果。可查看完整运行日志排查原因，或由 Claude 诊断并修复代码。</p></div><div style={{display:'flex',gap:8}}><button type="button" className="button" onClick={onViewLogs}><Terminal size={15}/>查看回测日志</button>{onRepair&&<button className="button primary repair-strategy-button" disabled={repairing} onClick={onRepair}><Wrench/>{repairing?'正在创建诊断任务…':'策略修复'}</button>}</div></div>{repairError&&<div className="form-error">{repairError}</div>}<pre>{run.error_message}</pre></Card>
  if(!run.result)return <Card className="running"><RefreshCw className={terminal.has(run.status)?'':'spin'}/><h2>{run.stage}</h2><div className="big-progress"><i style={{width:run.progress+'%'}}/></div><strong>{run.progress}%</strong><p>{run.status==='CANCELED'?'该历史任务已取消。':run.stage.includes('检查数据完整性')?'正在逐项验证行情数据覆盖率…':'完成后页面将显示 NautilusTrader 生成的结果。'}</p><button type="button" className="quick-log-btn" onClick={onViewLogs}><Terminal/>查看实时日志</button></Card>
  const m=run.metrics!,show=(value:number|null|undefined,suffix='')=>value==null?'—':`${value}${suffix}`
  const fallback=run.result.equity.map((value,index)=>({timestamp:run.result!.timestamps[index]??String(index),value}))
  const charts=run.result.charts
  const equity=charts?.equity??fallback,drawdown=charts?.drawdown??run.result.drawdown.map((value,index)=>({timestamp:run.result!.timestamps[index]??String(index),value}))
  const monthNames=['1月','2月','3月','4月','5月','6月','7月','8月','9月','10月','11月','12月']
  const years=[...new Set(charts?.monthly_returns.map(item=>item.year)??[])]
  const tooltip={background:'#111a25',border:'1px solid #2a3b50'}
  const tick=(value:string)=>value?.slice(0,10)
  return <><div className="metrics"><Metric label="总收益" value={show(m.total_return,'%')}/><Metric label="最大回撤" value={show(m.max_drawdown,'%')}/><Metric label="Sharpe" value={show(m.sharpe)}/><Metric label="胜率" value={show(m.win_rate,'%')}/><Metric label="Profit Factor" value={show(m.profit_factor)}/><Metric label="交易次数" value={show(m.trades)}/></div>
    <div className="analysis-grid">
      <Card title="权益曲线" className="chart chart-wide"><ResponsiveContainer width="100%" height={300}><AreaChart data={equity}><CartesianGrid stroke="#213041" vertical={false}/><XAxis dataKey="timestamp" tickFormatter={tick} minTickGap={40} stroke="#718096"/><YAxis stroke="#718096" domain={['auto','auto']}/><Tooltip contentStyle={tooltip}/><Area type="monotone" dataKey="value" name="权益" stroke="#18c8d8" fill="#18c8d833" strokeWidth={2}/></AreaChart></ResponsiveContainer></Card>
      <Card title="回撤曲线" className="chart"><ResponsiveContainer width="100%" height={250}><AreaChart data={drawdown}><CartesianGrid stroke="#213041" vertical={false}/><XAxis dataKey="timestamp" tickFormatter={tick} minTickGap={40} stroke="#718096"/><YAxis stroke="#718096" unit="%"/><Tooltip contentStyle={tooltip}/><Area type="monotone" dataKey="value" name="回撤" stroke="#ff5c68" fill="#ff5c6830"/></AreaChart></ResponsiveContainer></Card>
      <Card title="滚动 Sharpe（30日）" className="chart"><ResponsiveContainer width="100%" height={250}><LineChart data={charts?.rolling_sharpe??[]}><CartesianGrid stroke="#213041" vertical={false}/><XAxis dataKey="timestamp" tickFormatter={tick} minTickGap={40} stroke="#718096"/><YAxis stroke="#718096"/><Tooltip contentStyle={tooltip}/><Line type="monotone" dataKey="value" name="Sharpe" stroke="#f3b743" dot={false}/></LineChart></ResponsiveContainer></Card>
      <Card title="月度收益热力图" className="chart chart-wide monthly-return-card"><div className="returns-heatmap"><div/><>{monthNames.map(name=><b key={name}>{name}</b>)}</>{years.map(year=><div className="heat-row" key={year}><strong>{year}</strong>{monthNames.map((_,month)=>{const point=charts?.monthly_returns.find(item=>item.year===year&&item.month===month+1),value=point?.value;return <span key={month} className={value==null?'empty':value>=0?'gain':'loss'} title={value==null?'无数据':`${value}%`}>{value==null?'—':value.toFixed(2)+'%'}</span>})}</div>)}</div></Card>
      <Card title="收益分布" className="chart"><ResponsiveContainer width="100%" height={250}><BarChart data={charts?.returns_distribution.map(item=>({...item,label:`${item.from.toFixed(2)}~${item.to.toFixed(2)}%`}))??[]}><CartesianGrid stroke="#213041" vertical={false}/><XAxis dataKey="label" hide/><YAxis stroke="#718096"/><Tooltip contentStyle={tooltip}/><Bar dataKey="count" name="次数" fill="#18c8d8"/></BarChart></ResponsiveContainer></Card>
      <Card title="年度收益" className="chart"><ResponsiveContainer width="100%" height={250}><BarChart data={charts?.yearly_returns??[]}><CartesianGrid stroke="#213041" vertical={false}/><XAxis dataKey="year" stroke="#718096"/><YAxis stroke="#718096" unit="%"/><Tooltip contentStyle={tooltip}/><Bar dataKey="value" name="收益率" fill="#2bd486"/></BarChart></ResponsiveContainer></Card>
      <Card title="品种贡献" className="chart"><ResponsiveContainer width="100%" height={250}><BarChart data={run.result.contribution} layout="vertical"><XAxis type="number" hide/><YAxis dataKey="symbol" type="category" stroke="#aeb9c7" width={110}/><Tooltip contentStyle={tooltip}/><Bar dataKey="value" name="已实现盈亏" fill="#18c8d8" radius={[0,4,4,0]}/></BarChart></ResponsiveContainer></Card>
      <Card title="原生数据收集"><div className="report-summary">{run.result.reports?Object.entries(run.result.reports).map(([name,item])=><p key={name}><span>{name}</span><b>{item.rows.toLocaleString()} 行</b></p>):<p>历史任务未包含报告清单</p>}<p><span>BacktestResult</span><b>{run.result.native?'已保存':'历史任务未保存'}</b></p><p><span>Analyzer Statistics</span><b>{run.result.statistics?'已保存':'历史任务未保存'}</b></p></div></Card>
      <Card title="运行信息"><div className="run-info"><p><span>任务 ID</span><code>{run.id}</code></p><p><span>创建时间</span>{new Date(run.created_at).toLocaleString()}</p><p><span>执行器</span>NautilusTrader BacktestNode</p><p><span>数据性质</span>真实历史 K 线 · 模拟成交</p><p><span>迭代次数</span>{String(run.result.native?.iterations??'—')}</p><p><span>运行耗时</span>{String(run.result.native?.elapsed_time??'—')} 秒</p></div></Card>
    </div></>
}

function LogViewer({runId,runStatus}:{runId:string;runStatus:string}){
  const[logs,setLogs]=useState('')
  const[loading,setLoading]=useState(true)
  const[autoScroll,setAutoScroll]=useState(true)
  const[search,setSearch]=useState('')
  const[copied,setCopied]=useState(false)
  const bodyRef=useRef<HTMLDivElement>(null)

  const fetchLogs=async()=>{
    try{
      const res=await api.runLogs(runId)
      setLogs(res.logs||'')
    }catch{/**/}finally{
      setLoading(false)
    }
  }

  useEffect(()=>{
    fetchLogs()
    if(terminal.has(runStatus))return
    const timer=setInterval(fetchLogs,1500)
    return()=>clearInterval(timer)
  },[runId,runStatus])

  useEffect(()=>{
    if(autoScroll&&bodyRef.current){
      bodyRef.current.scrollTop=bodyRef.current.scrollHeight
    }
  },[logs,autoScroll])

  const copyLogs=()=>{
    if(!logs)return
    navigator.clipboard.writeText(logs)
    setCopied(true)
    setTimeout(()=>setCopied(false),2000)
  }

  const downloadLogs=()=>{
    if(!logs)return
    const blob=new Blob([logs],{type:'text/plain;charset=utf-8'})
    const url=URL.createObjectURL(blob)
    const a=document.createElement('a')
    a.href=url
    a.download=`backtest-${runId}.log`
    a.click()
    URL.revokeObjectURL(url)
  }

  const lines=logs.split('\n')
  const filteredLines=search?lines.filter(l=>l.toLowerCase().includes(search.toLowerCase())):lines
  const lineCount=lines.filter(l=>l.trim().length>0).length

  const renderLine=(text:string,idx:number)=>{
    const timeMatch=text.match(/^\[(\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2})\]/)
    let rest=text
    let timeStr=''
    if(timeMatch){
      timeStr=timeMatch[0]
      rest=text.slice(timeStr.length)
    }
    let levelTag:React.ReactNode=null
    if(rest.includes('[INFO]')){
      levelTag=<span className="log-info-tag">[INFO]</span>
      rest=rest.replace('[INFO]','')
    }else if(rest.includes('[WARN]')){
      levelTag=<span className="log-warn-tag">[WARN]</span>
      rest=rest.replace('[WARN]','')
    }else if(rest.includes('[ERROR]')){
      levelTag=<span className="log-error-tag">[ERROR]</span>
      rest=rest.replace('[ERROR]','')
    }

    return (
      <div className="log-line" key={idx}>
        <span className="log-lineno">{idx+1}</span>
        <div className="log-text">
          {timeStr&&<span className="log-time-tag">{timeStr} </span>}
          {levelTag}
          {rest}
        </div>
      </div>
    )
  }

  return (
    <section className="log-viewer-section">
      <div className="log-terminal">
        <div className="log-terminal-header">
          <div className="log-header-left">
            <span className={`log-status-dot ${runStatus==='RUNNING'?'running':runStatus==='COMPLETED'?'completed':runStatus==='FAILED'?'failed':''}`}/>
            <div className="log-terminal-title">
              <Terminal/>回测执行日志
            </div>
            <span className="log-line-count">{lineCount} 行</span>
          </div>
          <div className="log-header-right">
            <div className="log-search-box">
              <Search/>
              <input
                type="text"
                placeholder="搜索日志内容…"
                value={search}
                onChange={e=>setSearch(e.target.value)}
              />
            </div>
            <button
              type="button"
              className={`log-action-btn ${autoScroll?'active':''}`}
              onClick={()=>setAutoScroll(!autoScroll)}
              title="自动滚动到最新日志"
            >
              <RotateCw size={12}/>自动滚动
            </button>
            <button
              type="button"
              className="log-action-btn"
              onClick={copyLogs}
              title="复制全部日志到剪贴板"
            >
              {copied?<Check size={12}/>:<Copy size={12}/>}
              {copied?'已复制':'复制'}
            </button>
            <button
              type="button"
              className="log-action-btn"
              onClick={downloadLogs}
              title="下载日志文件"
            >
              <Download size={12}/>下载
            </button>
            <button
              type="button"
              className="log-action-btn"
              onClick={fetchLogs}
              title="手动刷新"
            >
              <RefreshCw size={12}/>
            </button>
          </div>
        </div>
        <div className="log-terminal-body" ref={bodyRef}>
          {loading&&!logs?(
            <div className="log-empty">正在加载日志…</div>
          ):filteredLines.length===0?(
            <div className="log-empty">{search?'未匹配到相关日志':'暂无回测日志'}</div>
          ):(
            filteredLines.map((line,idx)=>renderLine(line,idx))
          )}
        </div>
      </div>
    </section>
  )
}

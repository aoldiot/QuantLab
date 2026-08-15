import {useEffect,useState} from 'react'
import {ArrowLeft,CandlestickChart,Copy,RefreshCw,Wrench} from 'lucide-react'
import {Link,useNavigate,useParams} from 'react-router-dom'
import {Area,AreaChart,Bar,BarChart,CartesianGrid,Line,LineChart,ResponsiveContainer,Tooltip,XAxis,YAxis} from 'recharts'
import {api} from '../api'
import {Card,Metric} from '../components'
import type {Run} from '../types'
import CandleStudio from '../CandleStudio'

type Tab='overview'|'candles'|'parameters'
const terminal=new Set(['COMPLETED','FAILED','CANCELED'])
const labels:Record<string,string>={strategy_version_id:'策略版本 ID',strategy_parameters:'策略参数',venue:'市场',symbols:'交易品种',timeframes:'周期',start_date:'开始日期',end_date:'结束日期',initial_balance:'初始资金',leverage:'杠杆',execution_model:'成交模型',funding:'资金费率',catalog_path:'Catalog 路径',chunk_size:'分块大小',strategy_revision:'代码版本'}

export default function Result(){
  const{id}=useParams(),navigate=useNavigate(),[run,setRun]=useState<Run>(),[tab,setTab]=useState<Tab>('overview'),[strategyId,setStrategyId]=useState(''),[error,setError]=useState(''),[repairing,setRepairing]=useState(false),[repairError,setRepairError]=useState('')
  useEffect(()=>{let active=true,timer:number|undefined;const load=async()=>{try{const value=await api.run(id!);if(!active)return;setRun(value);setError('');if(!terminal.has(value.status))timer=window.setTimeout(load,1200)}catch(reason){if(active)setError((reason as Error).message)}};load();return()=>{active=false;if(timer)clearTimeout(timer)}},[id])
  useEffect(()=>{if(!run)return;api.strategies().then(async strategies=>{for(const strategy of strategies){const versions=await api.versions(strategy.id);if(versions.some(v=>v.id===String(run.config.strategy_version_id))){setStrategyId(strategy.id);break}}}).catch(()=>{})},[run])
  if(error)return <Card className="failure"><h2>回测详情加载失败</h2><p>{error}</p></Card>
  if(!run)return <div className="loading">加载回测任务…</div>
  const copyRun=()=>navigate('/backtests/new',{state:{copiedConfig:run.config,strategyId}})
  const repair=async()=>{if(!run.research_project_id||repairing)return;setRepairing(true);setRepairError('');try{let client_id=localStorage.getItem('quantlab_client_id');if(!client_id){client_id=crypto.randomUUID();localStorage.setItem('quantlab_client_id',client_id)}const created=await api.repairResearchRun(run.research_project_id,run.id,client_id);navigate('/research',{state:{repairProjectId:run.research_project_id,repairSessionId:created.session.id,repairPrompt:created.prompt}})}catch(reason){setRepairError((reason as Error).message);setRepairing(false)}}
  const value=(item:unknown)=>item==null||item===''?'—':Array.isArray(item)?item.join(' / '):typeof item==='object'?JSON.stringify(item,null,2):String(item)
  return <><div className="strategy-title backtest-title"><Link className="detail-back" to="/backtests" aria-label="返回回测管理" title="返回回测管理"><ArrowLeft/></Link><div><h1>{run.name}</h1><small>{run.config.start_date} — {run.config.end_date}</small></div></div>
    <div className="detail-tabs">{([['overview','回测总览'],['candles','K 线'],['parameters','回测参数']] as [Tab,string][]).map(([key,label])=><button className={tab===key?'active':''} onClick={()=>setTab(key)} key={key}>{key==='candles'&&<CandlestickChart/>}{label}</button>)}</div>
    {tab==='overview'&&<Overview run={run} onRepair={run.research_project_id?repair:undefined} repairing={repairing} repairError={repairError}/>}
    {tab==='candles'&&<CandleStudio run={run}/>}
    {tab==='parameters'&&<section className="card parameter-page"><div className="section-title"><div><h3>本次回测配置</h3><p>以下参数为任务创建时锁定的完整配置。</p></div><button className="button primary" onClick={copyRun}><Copy/>复制参数重新回测</button></div><dl className="parameter-list">{Object.entries(run.config).filter(([key])=>key!=='strategy_revision').map(([key,item])=><div key={key}><dt>{labels[key]??key}</dt><dd><pre>{value(item)}</pre></dd></div>)}</dl></section>}
  </>
}

function Overview({run,onRepair,repairing,repairError}:{run:Run;onRepair?:()=>void;repairing:boolean;repairError:string}){
  if(run.status==='FAILED')return <Card className="failure"><div className="failure-heading"><div><h2>{run.stage}</h2><p>回测未生成结果。Claude 会先判断责任归属，只有策略问题才会修改代码。</p></div>{onRepair&&<button className="button primary repair-strategy-button" disabled={repairing} onClick={onRepair}><Wrench/>{repairing?'正在创建诊断任务…':'策略修复'}</button>}</div>{repairError&&<div className="form-error">{repairError}</div>}<pre>{run.error_message}</pre></Card>
  if(!run.result)return <Card className="running"><RefreshCw className={terminal.has(run.status)?'':'spin'}/><h2>{run.stage}</h2><div className="big-progress"><i style={{width:run.progress+'%'}}/></div><strong>{run.progress}%</strong><p>{run.status==='CANCELED'?'该历史任务已取消。':'完成后页面将显示 NautilusTrader 生成的结果。'}</p></Card>
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

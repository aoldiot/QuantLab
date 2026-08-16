import {useEffect,useMemo,useState} from 'react'
import {ArrowDown,ArrowUp,ChevronRight,Plus,Trash2} from 'lucide-react'
import {Link,useNavigate} from 'react-router-dom'
import {api} from '../api'
import {ConfirmDialog,Status} from '../components'
import type {Run,Strategy} from '../types'

type SortKey='name'|'strategy'|'market'|'status'|'progress'|'return'|'sharpe'|'created'

export default function Backtests(){
  const[runs,setRuns]=useState<Run[]>([]),[strategies,setStrategies]=useState<Strategy[]>([]),[strategyNames,setStrategyNames]=useState<Record<string,string>>({})
  const[error,setError]=useState(''),[deleting,setDeleting]=useState(''),[pendingDelete,setPendingDelete]=useState<Run|null>(null),[strategyFilter,setStrategyFilter]=useState('全部'),[statusFilter,setStatusFilter]=useState('全部'),[from,setFrom]=useState(''),[to,setTo]=useState(''),[sort,setSort]=useState<SortKey>('created'),[direction,setDirection]=useState<'asc'|'desc'>('desc')
  const navigate=useNavigate()
  useEffect(()=>{let active=true;const load=()=>Promise.all([api.runs(),api.strategies()]).then(async([value,all])=>{const pairs=await Promise.all(all.map(async strategy=>[strategy,await api.versions(strategy.id)] as const)),names:Record<string,string>={};pairs.forEach(([strategy,versions])=>versions.forEach(version=>{names[version.id]=strategy.name}));if(active){setRuns(value);setStrategies(all);setStrategyNames(names);setError('')}}).catch(reason=>{if(active)setError((reason as Error).message)});load();const timer=setInterval(load,1500);return()=>{active=false;clearInterval(timer)}},[])
  const statuses=useMemo(()=>['全部',...Array.from(new Set(runs.map(run=>run.status)))],[runs])
  const visible=useMemo(()=>{const value=(run:Run,key:SortKey):string|number=>{switch(key){case'name':return run.name;case'strategy':return strategyNames[String(run.config.strategy_version_id)]??'';case'market':return `${run.config.venue??''} ${run.config.timeframes?.join(' ')??''}`;case'status':return run.status;case'progress':return run.progress;case'return':return run.metrics?.total_return??Number.NEGATIVE_INFINITY;case'sharpe':return run.metrics?.sharpe??Number.NEGATIVE_INFINITY;case'created':return new Date(run.created_at).getTime()}};return runs.filter(run=>{const day=run.created_at.slice(0,10),name=strategyNames[String(run.config.strategy_version_id)]??'已删除策略';return(strategyFilter==='全部'||name===strategyFilter)&&(statusFilter==='全部'||run.status===statusFilter)&&(!from||day>=from)&&(!to||day<=to)}).sort((a,b)=>{const av=value(a,sort),bv=value(b,sort),result=typeof av==='number'&&typeof bv==='number'?av-bv:String(av).localeCompare(String(bv),'zh-CN');return direction==='asc'?result:-result})},[runs,strategyNames,strategyFilter,statusFilter,from,to,sort,direction])
  const changeSort=(key:SortKey)=>{if(sort===key)setDirection(value=>value==='asc'?'desc':'asc');else{setSort(key);setDirection('asc')}}
  const heading=(label:string,key:SortKey)=><button className="sort-heading" onClick={()=>changeSort(key)}>{label}{sort===key&&(direction==='asc'?<ArrowUp/>:<ArrowDown/>)}</button>
  const open=(id:string)=>navigate('/backtests/'+id)
  const remove=async()=>{const run=pendingDelete;if(!run)return;setDeleting(run.id);setError('');try{await api.deleteRun(run.id);setRuns(current=>current.filter(item=>item.id!==run.id));setPendingDelete(null)}catch(reason){setError((reason as Error).message)}finally{setDeleting('')}}
  return <>
    {error&&<div className="form-error">历史回测加载失败：{error}</div>}
    <div className="backtest-filters">
      <label>策略<select value={strategyFilter} onChange={e=>setStrategyFilter(e.target.value)}><option>全部</option>{strategies.map(item=><option key={item.id}>{item.name}</option>)}</select></label>
      <label>状态<select value={statusFilter} onChange={e=>setStatusFilter(e.target.value)}>{statuses.map(value=><option key={value}>{value}</option>)}</select></label>
      <label>开始日期<input type="date" value={from} max={to||undefined} onChange={e=>setFrom(e.target.value)}/></label>
      <label>结束日期<input type="date" value={to} min={from||undefined} onChange={e=>setTo(e.target.value)}/></label>
      <button onClick={()=>{setStrategyFilter('全部');setStatusFilter('全部');setFrom('');setTo('')}}>重置筛选</button>
      <span className="filter-count">{visible.length} / {runs.length}</span>
      <Link className="button primary new-backtest-btn" to="/backtests/new"><Plus size={16}/>新建回测</Link>
    </div>
    <div className="table-card backtest-history"><table><thead><tr><th>{heading('任务名称','name')}</th><th>{heading('回测策略','strategy')}</th><th>{heading('市场与周期','market')}</th><th>{heading('状态','status')}</th><th>{heading('进度','progress')}</th><th>{heading('总收益','return')}</th><th>{heading('Sharpe','sharpe')}</th><th>{heading('创建时间','created')}</th><th aria-label="操作"/></tr></thead><tbody>
      {visible.length===0?<tr><td colSpan={9} className="empty-row">没有符合当前筛选条件的回测任务。</td></tr>:visible.map(run=><tr key={run.id} className="clickable-run" tabIndex={0} onClick={()=>open(run.id)} onKeyDown={event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();open(run.id)}}} aria-label={`查看回测详情：${run.name}`}>
        <td><strong>{run.name}</strong><small title={Array.isArray(run.config.symbols)?run.config.symbols.join(', '):undefined}>{Array.isArray(run.config.symbols)?`${run.config.symbols.length} 个标的`:'—'}</small></td><td>{strategyNames[String(run.config.strategy_version_id)]??'已删除策略'}</td><td>{run.config.venue} · {run.config.timeframes?.join(' / ')}</td><td><Status value={run.status}/></td><td><div className="progress"><i style={{width:run.progress+'%'}}/></div><small>{run.stage}</small></td><td className={(run.metrics?.total_return??0)>=0?'positive':'negative'}>{run.metrics?`${run.metrics.total_return}%`:'—'}</td><td>{run.metrics?.sharpe??'—'}</td><td>{new Date(run.created_at).toLocaleString()}</td><td><div className="run-actions"><span className="detail-link">查看详情<ChevronRight/></span><button type="button" className="delete-run" disabled={deleting===run.id||['QUEUED','RUNNING','ANALYZING'].includes(run.status)} onClick={event=>{event.stopPropagation();setPendingDelete(run)}} title={['QUEUED','RUNNING','ANALYZING'].includes(run.status)?'运行中的任务不能删除':'删除回测'} aria-label={`删除回测：${run.name}`}><Trash2/></button></div></td>
      </tr>)}
    </tbody></table></div><ConfirmDialog open={!!pendingDelete} title="删除回测" message={`确定删除回测“${pendingDelete?.name??''}”吗？\n该回测记录、运行日志及全部报告文件都会永久删除，此操作无法撤销。`} confirmLabel="永久删除" danger busy={!!deleting} onCancel={()=>setPendingDelete(null)} onConfirm={remove}/>
  </>
}

import {useEffect,useMemo,useState} from 'react'
import {Check,Info,Play,Save,ShieldCheck} from 'lucide-react'
import {useLocation,useNavigate} from 'react-router-dom'
import {api} from '../api'
import {Card} from '../components'
import type {Strategy} from '../types'

export default function NewBacktest(){
  const nav=useNavigate(),location=useLocation()
  const navState=location.state as {copiedConfig?:Record<string,any>;strategyId?:string;strategySlug?:string;researchProjectId?:string}|null
  const copied=navState?.copiedConfig
  const copiedStrategy=navState?.strategyId
  const copiedSlug=navState?.strategySlug
  const researchProjectId=navState?.researchProjectId||copied?.research_project_id||null

  const[strategies,setStrategies]=useState<Strategy[]>([])
  const[selected,setSelected]=useState('')
  const[busy,setBusy]=useState(false)
  const[error,setError]=useState('')

  useEffect(()=>{
    api.strategies().then(x=>{
      setStrategies(x)
      let found=''
      if(copiedStrategy&&x.some(s=>s.id===copiedStrategy)){
        found=copiedStrategy
      }else if(copiedSlug&&x.some(s=>s.slug===copiedSlug)){
        found=x.find(s=>s.slug===copiedSlug)!.id
      }
      setSelected(found || x[0]?.id || '')
    })
  },[copiedStrategy,copiedSlug])
  const strategy=useMemo(()=>strategies.find(x=>x.id===selected),[strategies,selected])

  async function submit(e:React.FormEvent<HTMLFormElement>){
    e.preventDefault()
    if(!strategy||busy)return
    setBusy(true)
    setError('')
    const f=new FormData(e.currentTarget)
    const params:Record<string,unknown>={}
    Object.entries(strategy.parameter_schema).forEach(([key,spec])=>{
      const raw=f.get('param_'+key)
      params[key]=spec.type==='boolean'?raw==='on':spec.type==='integer'?Number(raw):spec.type==='number'?Number(raw):raw
    })
    const checkIntegrity=f.get('check_data_integrity')==='on'
    const payload={
      name:f.get('name'),
      strategy_version_id:strategy.latest_version_id,
      strategy_parameters:params,
      venue:copied?.venue??'BINANCE',
      symbols:String(f.get('symbols')).split(',').map(x=>x.trim()).filter(Boolean),
      timeframes:strategy.data_requirements.timeframes,
      start_date:String(f.get('start')),
      end_date:String(f.get('end')),
      initial_balance:Number(f.get('capital')),
      leverage:Number(f.get('leverage')),
      execution_model:f.get('model'),
      funding:strategy.data_requirements.funding,
      catalog_path:(f.get('catalog_path') as string)||null,
      ignore_missing_data:true,
      check_data_integrity:checkIntegrity,
      research_project_id:researchProjectId,
    }

    try{
      const run=await api.createRun(payload)
      nav('/backtests/'+run.id)
    }catch(e){
      setError(e instanceof Error?e.message:'创建回测任务失败')
      setBusy(false)
    }
  }

  const oldParams=copied?.strategy_parameters??{}
  return <>
    <div className="steps">{['选择策略','策略参数','交易范围','账户设置','执行模型','确认'].map((x,i)=><div className={i<5?'done':''} key={x}><span>{i<4?<Check/>:i+1}</span>{x}</div>)}</div>
    <form onSubmit={submit} className="config-layout">
      <div>
        <Card title="基础配置">
          <div className="form-grid">
            <label>任务名称<input name="name" defaultValue={copied?'复制回测 · 参数调整':'策略回测 · 标准验证'}/></label>
            <label>策略版本<select value={selected} onChange={e=>setSelected(e.target.value)}>{strategies.map(x=><option value={x.id} key={x.id}>{x.name} v{x.version}</option>)}</select></label>
            <label>开始日期<input name="start" type="date" defaultValue={copied?.start_date??'2024-01-01'}/></label>
            <label>结束日期<input name="end" type="date" defaultValue={copied?.end_date??'2025-12-31'}/></label>
          </div>
        </Card>
        <Card title="策略参数">
          <div className="form-grid">
            {strategy&&Object.entries(strategy.parameter_schema).map(([key,spec])=><label key={key}>{spec.title}{spec.type==='boolean'?<input name={'param_'+key} type="checkbox" defaultChecked={Boolean(oldParams[key]??spec.default)}/>:<input name={'param_'+key} type="number" step={spec.type==='integer'?'1':'any'} min={spec.min} max={spec.max} defaultValue={String(oldParams[key]??spec.default)}/>}</label>)}
          </div>
        </Card>
        <Card title="交易范围与账户">
          <div className="form-grid">
            <label className="wide">Nautilus Catalog 路径<input name="catalog_path" defaultValue={copied?.catalog_path??''} placeholder="留空使用后端 CATALOG_PATH"/><small>必须是已写入 Instrument 和 Bar 的 ParquetDataCatalog</small></label>
            <label className="wide">交易品种<input name="symbols" defaultValue={copied?.symbols?.join(', ')??'BTCUSDT, ETHUSDT, SOLUSDT'}/><small>{strategy?.data_requirements.mode==='PORTFOLIO'?'整个币池交给同一个组合策略统一排序和调仓':'每个标的创建一个独立策略实例'}</small></label>
            <label>初始资金<input name="capital" type="number" defaultValue={copied?.initial_balance??10000}/><em>USDT</em></label>
            <label>杠杆<input name="leverage" type="number" defaultValue={copied?.leverage??4}/><em>x</em></label>
            <label className="wide checkbox-field" style={{display:'flex',flexDirection:'row',alignItems:'center',gap:10,marginTop:6,cursor:'pointer'}}>
              <input type="checkbox" name="check_data_integrity" defaultChecked={copied?.check_data_integrity??true} style={{width:16,height:16,accentColor:'var(--cyan)'}}/>
              <span style={{fontWeight:500,color:'#e2ecf5'}}>检查数据完整性</span>
              <small style={{color:'var(--muted)',fontSize:12}}>（勾选后将在回测开始前验证 Parquet 行情覆盖度并显示进度；不勾选则跳过检查直接开始回测）</small>
            </label>
          </div>
        </Card>
        <Card title="执行模型">
          <div className="model-select">
            {[['FAST','快速','固定 OHLC 路径'],['STANDARD','标准','自适应 K 线路径'],['CONSERVATIVE','保守','自适应 K 线路径']].map(([v,t,s])=><label key={v}><input type="radio" name="model" value={v} defaultChecked={v===(copied?.execution_model??'CONSERVATIVE')}/><span><b>{t}</b><small>{s}</small></span></label>)}
          </div>
        </Card>
        {error&&<div className="form-error">{error}</div>}
        <div className="form-actions">
          <button type="button" className="button" onClick={()=>nav(-1)}>取消</button>
          <button type="button" className="button"><Save size={16}/>保存草稿</button>
          <button disabled={busy||!strategy} className="button primary"><Play size={17}/>{busy?'正在创建…':'创建并启动回测'}</button>
        </div>
      </div>
      <aside className="summary">
        <Card title="回测摘要">
          <dl>
            <div><dt>策略</dt><dd>{strategy?.name??'加载中'}</dd></div>
            <div><dt>周期</dt><dd>{strategy?.data_requirements.timeframes.join(' · ')}</dd></div>
            <div><dt>执行器</dt><dd>BacktestNode</dd></div>
          </dl>
        </Card>
        <Card title="配置检查">
          <ul className="checks">
            <li><Check/>策略契约已加载</li>
            <li><Check/>参数由 Manifest 动态生成</li>
            <li><Check/>支持数据完整性自检与实时日志监控</li>
          </ul>
          <div className="notice"><ShieldCheck/>复制配置不会修改原回测，提交后会创建独立的新任务。</div>
        </Card>
        <div className="tip"><Info/>所有字段都可以在提交前调整。</div>
      </aside>
    </form>
  </>
}


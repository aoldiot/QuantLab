import {useEffect,useMemo,useRef,useState} from 'react'
import {ArrowLeft,Check,Code2,FileCode2,History,Play,RotateCcw,Trash2,X} from 'lucide-react'
import {Link,useParams} from 'react-router-dom'
import {api} from '../api'
import CodeEditor from '../CodeEditor'
import {ConfirmDialog,Status} from '../components'
import {loadCategories} from '../categories'
import type {Run,Strategy,StrategyFile,StrategyVersion} from '../types'

type Tab='code'|'settings'|'versions'|'backtests'
export default function StrategyDetail(){
  const{name=''}=useParams(),[file,setFile]=useState<StrategyFile|null>(null),[strategy,setStrategy]=useState<Strategy|null>(null)
  const[versions,setVersions]=useState<StrategyVersion[]>([]),[runs,setRuns]=useState<Run[]>([]),[categoryOptions,setCategoryOptions]=useState(loadCategories),[pendingVersion,setPendingVersion]=useState<StrategyVersion|null>(null)
  const[previewVersion,setPreviewVersion]=useState<StrategyVersion|null>(null)
  const[content,setContent]=useState(''),[saved,setSaved]=useState(''),[tab,setTab]=useState<Tab>('code'),[busy,setBusy]=useState(false),[publishOpen,setPublishOpen]=useState(false),[error,setError]=useState(''),[toast,setToast]=useState('')
  const autoSaveVersion=useRef(0)
  const dirty=content!==saved

  const latestVersion=useMemo(()=>versions.find(v=>v.is_latest)||versions[0]||null,[versions])
  const codeChanged=useMemo(()=>{
    if(!versions.length)return true
    if(!latestVersion?.code)return true
    return content.trim()!==latestVersion.code.trim()
  },[versions.length,latestVersion,content])

  async function load(){
    autoSaveVersion.current++
    const [f,all,allRuns,allFiles]=await Promise.all([api.strategyFile(name),api.strategies(),api.runs(),api.strategyFiles()])
    const s=all.find(x=>x.module===f.module)??null
    setFile(f)
    setContent(f.content??'')
    setSaved(f.content??'')
    setStrategy(s)
    setRuns(allRuns)
    setCategoryOptions(Array.from(new Set([...loadCategories(),...all.map(item=>item.category),...allFiles.map(item=>item.draft_category??'')])).filter(Boolean))
    setVersions(s?await api.versions(s.id):[])
  }
  useEffect(()=>{load().catch(e=>setError(e.message))},[name])
  useEffect(()=>{if(!toast)return;const timer=setTimeout(()=>setToast(''),2600);return()=>clearTimeout(timer)},[toast])
  useEffect(()=>{
    if(!file||content===saved)return
    const version=++autoSaveVersion.current
    const timer=window.setTimeout(async()=>{
      try{
        const f=await api.saveStrategyFile(file.name,content)
        if(version!==autoSaveVersion.current)return
        setFile(f)
        setSaved(content)
        setError('')
      }catch(e){
        if(version===autoSaveVersion.current)setError(`自动保存失败：${(e as Error).message}`)
      }
    },800)
    return()=>window.clearTimeout(timer)
  },[content,file?.name,saved])

  async function publish(e:React.FormEvent<HTMLFormElement>){
    e.preventDefault()
    if(!file)return
    if(dirty){setError('请先保存代码');return}
    const description=String(new FormData(e.currentTarget).get('description')).trim()
    setBusy(true)
    setError('')
    try{
      if(strategy)await api.createVersion(strategy.id,file.module,description)
      else await api.createStrategy(file.module,description)
      setPublishOpen(false)
      await load()
      setTab('versions')
      setToast(strategy?'新版本发布成功':'首次发布成功')
    }catch(e){
      setError((e as Error).message)
    }finally{
      setBusy(false)
    }
  }

  async function updateSettings(e:React.FormEvent<HTMLFormElement>){
    e.preventDefault()
    if(!file)return
    const f=new FormData(e.currentTarget),description=String(f.get('description')),category=String(f.get('category'))
    try{
      if(strategy)await api.updateStrategy(strategy.id,{name:f.get('display_name'),category,description})
      else await api.updateStrategyFileMetadata(file.name,description,category)
      await load()
      setToast('策略资料已更新')
    }catch(e){
      setError((e as Error).message)
    }
  }

  async function removeVersion(){
    if(!strategy||!pendingVersion)return
    setBusy(true)
    try{
      await api.deleteVersion(strategy.id,pendingVersion.id)
      setPendingVersion(null)
      await load()
    }catch(e){
      setError((e as Error).message)
    }finally{
      setBusy(false)
    }
  }

  async function restoreVersion(v:StrategyVersion){
    if(!strategy)return
    setBusy(true)
    setError('')
    try{
      await api.restoreVersion(strategy.id,v.id)
      setPreviewVersion(null)
      await load()
      setTab('code')
      setToast(`已成功还原至 v${v.version} 代码`)
    }catch(e){
      setError((e as Error).message)
    }finally{
      setBusy(false)
    }
  }

  const versionIds=new Set(versions.map(v=>v.id)),strategyRuns=runs.filter(r=>versionIds.has(String(r.config.strategy_version_id??'')))
  return <><div className="strategy-title"><Link className="detail-back" to="/strategies" aria-label="返回策略列表" title="返回策略列表"><ArrowLeft/></Link><h1>{strategy?.name??file?.name??name}</h1></div>
    {error&&<div className="form-error strategy-error">{error}</div>}{toast&&<div className="toast">{toast}</div>}
    <div className="detail-tabs">{([['code','代码'],['settings','设置'],['versions','版本'],['backtests','回测']] as [Tab,string][]).map(([key,label])=><button className={tab===key?'active':''} onClick={()=>setTab(key)} key={key}>{label}{key==='versions'&&` (${versions.length})`}</button>)}</div>
    {tab==='code'&&<div className="detail-code-layout"><div className="detail-editor"><div className="editor-tabs"><span><FileCode2/>{file?.filename}{dirty&&<i>●</i>}</span><div className="editor-actions"><button className="button primary" title={!codeChanged?'代码未发生改变，无需重复发布':dirty?'代码正在自动保存':''} disabled={!file||busy||dirty||!codeChanged} onClick={()=>setPublishOpen(true)}><Play/>{strategy?'发布新版本':'首次发布'}</button></div></div><CodeEditor value={content} onChange={setContent}/><div className="editor-status"><span>Python</span><span>{content.split('\n').length} 行</span><span>{dirty?'自动保存中…':codeChanged?'有待发布修改':'已是最新版本'}</span></div></div></div>}
    {tab==='settings'&&<section className="card detail-section"><form className="stack-form settings-form" onSubmit={updateSettings}>{strategy&&<label>显示名称<input name="display_name" defaultValue={strategy.name}/></label>}<label>策略分类<select name="category" defaultValue={strategy?.category??file?.draft_category??'趋势'}>{categoryOptions.map(value=><option key={value}>{value}</option>)}</select></label><label className="wide">说明<textarea name="description" required defaultValue={strategy?.description??file?.draft_description??''}/></label><button className="button primary">保存设置</button></form></section>}
    {tab==='versions'&&<section className="card detail-section"><div className="section-title"><div><h3>正式版本历史</h3><p>每个版本保存在数据库中，记录发布时间、说明与源码快照，可随时查看历史代码或一键还原。</p></div></div>{versions.length?<div className="version-list compact-versions">{versions.map(v=><article key={v.id}><div><div className="version-title"><b>v{v.version}</b>{v.is_latest&&<span className="current-task-tag">最新版本</span>}</div><small title={v.description}>{v.description||'暂无版本描述'}</small></div><time>{v.created_at?new Date(v.created_at).toLocaleString('zh-CN'):'—'}</time><div className="version-actions-btns" style={{display:'flex',gap:6}}>{v.code&&<button type="button" className="button" style={{height:28,padding:'0 8px',fontSize:11.5}} onClick={()=>setPreviewVersion(v)}><Code2 size={13}/>查看代码</button>}{v.code&&!v.is_latest&&<button type="button" className="button" style={{height:28,padding:'0 8px',fontSize:11.5}} disabled={busy} onClick={()=>restoreVersion(v)}><RotateCcw size={13}/>还原此版本</button>}<button disabled={versions.length===1||busy} title="删除版本" onClick={()=>setPendingVersion(v)}><Trash2 size={14}/></button></div></article>)}</div>:<div className="empty-inline"><h3>还没有正式版本</h3><p>编写代码后在代码页点击“首次发布”。</p></div>}</section>}
    {tab==='backtests'&&<section className="table-card detail-section"><table><thead><tr><th>任务</th><th>状态</th><th>创建时间</th><th></th></tr></thead><tbody>{strategyRuns.length?strategyRuns.map(r=><tr key={r.id}><td>{r.name}</td><td><Status value={r.status}/></td><td>{new Date(r.created_at).toLocaleString()}</td><td><Link to={'/backtests/'+r.id}>查看</Link></td></tr>):<tr><td colSpan={4} className="empty-row">暂无该策略的回测记录</td></tr>}</tbody></table></section>}
    {publishOpen&&<div className="modal-backdrop"><section className="modal publish-modal"><button className="modal-close" onClick={()=>setPublishOpen(false)}>×</button><h2>{strategy?'发布新版本':'首次发布'}</h2><p className="muted">说明本次版本的更新内容、参数修改或逻辑优化，发布后将在数据库中创建只读快照。</p><form className="stack-form" onSubmit={publish}><label>版本描述<textarea name="description" required maxLength={500} autoFocus placeholder="例如：增加组合仓位上限与回撤保护"/></label><div className="publish-actions"><button type="button" className="button" onClick={()=>setPublishOpen(false)}>取消</button><button className="button primary" disabled={busy}><Play/>{busy?'发布中…':'确认发布'}</button></div></form></section></div>}
    {previewVersion&&<div className="modal-backdrop"><section className="modal" style={{maxWidth:820,width:'90vw',maxHeight:'88vh',display:'flex',flexDirection:'column'}}><div style={{display:'flex',alignItems:'center',justifyContent:'space-between',marginBottom:12}}><div style={{display:'flex',alignItems:'center',gap:8}}><h2>v{previewVersion.version} 源码快照</h2>{previewVersion.is_latest&&<span className="current-task-tag">最新版本</span>}</div><button className="modal-close" onClick={()=>setPreviewVersion(null)}>×</button></div><p className="muted" style={{margin:'0 0 10px'}}>{previewVersion.description||'无版本描述'} · 发布时间：{previewVersion.created_at?new Date(previewVersion.created_at).toLocaleString('zh-CN'):'—'}</p><div style={{flex:1,minHeight:340,maxHeight:'55vh',overflow:'hidden',border:'1px solid rgba(255,255,255,0.08)',borderRadius:8}}><CodeEditor value={previewVersion.code||''} onChange={()=>{}} readOnly/></div><div style={{display:'flex',justifyContent:'flex-end',gap:10,marginTop:14}}><button type="button" className="button" onClick={()=>setPreviewVersion(null)}>关闭</button>{!previewVersion.is_latest&&<button type="button" className="button primary" disabled={busy} onClick={()=>restoreVersion(previewVersion)}><RotateCcw size={14}/>还原该版本到工作区</button>}</div></section></div>}
    <ConfirmDialog open={!!pendingVersion} title="删除策略版本" message={`确定删除 v${pendingVersion?.version??''}？此操作无法撤销。`} confirmLabel="删除版本" danger busy={busy} onCancel={()=>setPendingVersion(null)} onConfirm={removeVersion}/>
  </>
}

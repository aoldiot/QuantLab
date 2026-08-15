import {useEffect,useMemo,useRef,useState} from 'react'
import {ArrowRight,ArrowUp,Beaker,BrainCircuit,Check,CheckCircle2,ChevronDown,ChevronLeft,ChevronRight,ChevronUp,Clock3,Code2,ExternalLink,FileJson,FlaskConical,GitBranch,ListFilter,Loader2,MessageSquarePlus,Plus,RotateCcw,Target,X} from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {Link,useLocation,useNavigate} from 'react-router-dom'
import AgentPanel from '../AgentPanel'
import CodeEditor from '../CodeEditor'
import {api} from '../api'
import {CatalogMissingDialog,Status} from '../components'
import type {CatalogCheckResponse,ResearchDecision,ResearchMessage,ResearchProject,ResearchRun,Run,Strategy,StrategyFile} from '../types'
import {getClientId,generateUUID} from '../utils'

const clientId=getClientId


const stageNames:Record<string,string>={DISCUSSING:'策略研讨',SPEC_REVIEW:'规格确认',IMPLEMENTING:'策略开发',CODE_REVIEW:'代码审核',READY_FOR_BACKTEST:'等待回测',BACKTESTING:'回测中',READY_FOR_ANALYSIS:'等待分析',ANALYZING:'结果分析',RESULT_REVIEW:'结果研讨',ARCHIVED:'已归档'}
const journeyStages=[
  {name:'研讨',description:'建立可证伪假设'},
  {name:'定义',description:'冻结策略规格'},
  {name:'实现',description:'开发并审核代码'},
  {name:'验证',description:'运行回测实验'},
  {name:'结论',description:'判断证据与去向'},
]
const stageByStatus:Record<string,number>={DISCUSSING:0,SPEC_REVIEW:1,IMPLEMENTING:2,CODE_REVIEW:2,READY_FOR_BACKTEST:3,BACKTESTING:3,READY_FOR_ANALYSIS:3,ANALYZING:4,RESULT_REVIEW:4,ARCHIVED:4}

function jsonAsMarkdown(value:unknown,depth=2):string{
  if(Array.isArray(value))return value.map(item=>typeof item==='object'?jsonAsMarkdown(item,depth+1):`- ${String(item)}`).join('\n')
  if(value&&typeof value==='object')return Object.entries(value as Record<string,unknown>).map(([key,item])=>{
    const heading='#'.repeat(Math.min(depth,4))
    if(item&&typeof item==='object')return `${heading} ${key}\n\n${jsonAsMarkdown(item,depth+1)}`
    return `**${key}：** ${String(item)}`
  }).join('\n\n')
  return String(value??'')
}

function readableMessage(message:ResearchMessage){
  if(message.role==='user')return message.content
  const text=message.content.trim().replace(/^```json\s*/i,'').replace(/\s*```$/,'')
  if(!text.startsWith('{')&&!text.startsWith('['))return message.content
  try{return jsonAsMarkdown(JSON.parse(text))}catch{return message.content}
}

export default function Research(){
  const location=useLocation(),navigate=useNavigate()
  const repairRequest=location.state as {repairProjectId?:string;repairSessionId?:string;repairPrompt?:string}|null
  const[projects,setProjects]=useState<ResearchProject[]>([]),[project,setProject]=useState<ResearchProject|null>(null),[messages,setMessages]=useState<ResearchMessage[]>([]),[runs,setRuns]=useState<ResearchRun[]>([]),[strategies,setStrategies]=useState<Strategy[]>([]),[decisions,setDecisions]=useState<ResearchDecision[]>([])
  const[input,setInput]=useState(''),[busy,setBusy]=useState(false),[error,setError]=useState(''),[creating,setCreating]=useState(false),[specText,setSpecText]=useState(''),[editingSpec,setEditingSpec]=useState(false),[agentOpen,setAgentOpen]=useState(false),[backtestOpen,setBacktestOpen]=useState(false),[implementationPrompt,setImplementationPrompt]=useState('')
  const[catalogCheck,setCatalogCheck]=useState<CatalogCheckResponse|null>(null)
  const[pendingBacktestData,setPendingBacktestData]=useState<Record<string,any>|null>(null)
  const[preview,setPreview]=useState<{module:string;name:string;parameter_schema:Record<string,any>;data_requirements:Record<string,any>}|null>(null)
  const[runDetail,setRunDetail]=useState<Run|null>(null)
  const[runDetails,setRunDetails]=useState<Record<string,Run>>({})
  const[implementationFile,setImplementationFile]=useState<StrategyFile|null>(null)
  const[questionsOpen,setQuestionsOpen]=useState(true),[viewStage,setViewStage]=useState(0)
  const[decisionDrafts,setDecisionDrafts]=useState<Record<string,string>>({}),[decisionBusy,setDecisionBusy]=useState(''),[decisionIndex,setDecisionIndex]=useState(0)
  const[settledOpen,setSettledOpen]=useState(false)
  const timeline=useRef<HTMLDivElement|null>(null)
  const strategyName=String(project?.specification?.content.strategy_name??'')
  const strategy=useMemo(()=>strategies.find(item=>item.module===`app.strategies.${strategyName}`),[strategies,strategyName])
  const developmentComplete=Boolean(implementationFile?.content&&(implementationFile.content.length>50||['CODE_REVIEW','READY_FOR_BACKTEST','BACKTESTING','READY_FOR_ANALYSIS','ANALYZING','RESULT_REVIEW','ARCHIVED'].includes(project?.status??'')))
  const userMessages=useMemo(()=>messages.filter(m=>m.role==='user'&&m.message_type==='message'),[messages])
  const completedRun=runs.find(run=>run.status==='COMPLETED')
  const activeRun=runs.find(run=>['QUEUED','RUNNING','ANALYZING'].includes(run.status))
  const journeyIndex=project?stageByStatus[project.status]??0:0
  const analysisMessages=messages.filter(message=>message.message_type==='analysis')
  const pendingDecisions=useMemo(()=>decisions.filter(item=>item.status==='PENDING'),[decisions])
  const settledDecisions=useMemo(()=>decisions.filter(item=>item.status!=='PENDING'),[decisions])
  const activeDecision=pendingDecisions[Math.min(decisionIndex,Math.max(0,pendingDecisions.length-1))]
  const decisionsRef=useRef<HTMLDivElement|null>(null)
  async function reloadList(){const value=await api.researchProjects(clientId());setProjects(value);return value}
  async function open(item:ResearchProject){setError('');const[m,r,s,fresh,d]=await Promise.all([api.researchMessages(item.id),api.researchRuns(item.id),api.strategies(),api.researchProject(item.id),api.researchDecisions(item.id)]);setMessages(m);setRuns(r);setStrategies(s);setProject(fresh);setDecisions(d);setSpecText(fresh.specification?JSON.stringify(fresh.specification.content,null,2):'')}
  useEffect(()=>{reloadList().then(async value=>{const target=value.find(item=>item.id===repairRequest?.repairProjectId)??value[0];if(!target)return;await open(target);if(repairRequest?.repairProjectId===target.id&&repairRequest.repairSessionId&&repairRequest.repairPrompt){setViewStage(2);setImplementationPrompt(repairRequest.repairPrompt);setAgentOpen(true);navigate('/research',{replace:true,state:null})}}).catch(e=>setError(e.message))},[])
  useEffect(()=>{timeline.current?.scrollTo({top:timeline.current.scrollHeight,behavior:'smooth'})},[messages,busy])
  useEffect(()=>{if(!project||!runs.some(run=>['QUEUED','RUNNING','ANALYZING'].includes(run.status)))return;const timer=window.setInterval(()=>{api.researchRuns(project.id).then(setRuns).catch(()=>{})},2000);return()=>window.clearInterval(timer)},[project?.id,runs.map(run=>run.status).join(',')])
  useEffect(()=>{setViewStage(journeyIndex)},[project?.id,journeyIndex])
  useEffect(()=>{if(!strategyName){setImplementationFile(null);return}api.strategyFile(strategyName).then(setImplementationFile).catch(()=>setImplementationFile(null))},[strategyName,project?.implementation_session_id,strategy?.status,project?.updated_at])
  useEffect(()=>{let active=true;if(!runs.length){setRunDetails({});return}Promise.all(runs.map(run=>api.run(run.id).catch(()=>null))).then(values=>{if(!active)return;const next:Record<string,Run>={};values.forEach(value=>{if(value)next[value.id]=value});setRunDetails(next)});return()=>{active=false}},[runs.map(run=>`${run.id}:${run.status}:${run.progress}`).join('|')])

  const scrollToMessage=(id:string)=>{
    const el=document.getElementById(`msg-${id}`)
    if(el){
      el.scrollIntoView({behavior:'smooth',block:'center'})
      el.classList.add('message-highlight')
      setTimeout(()=>el.classList.remove('message-highlight'),2000)
    }
  }

  async function create(e:React.FormEvent<HTMLFormElement>){e.preventDefault();setBusy(true);setError('');const f=new FormData(e.currentTarget);try{const item=await api.createResearch(clientId(),String(f.get('title')));setCreating(false);await reloadList();await open(item)}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
  async function send(e:React.FormEvent){e.preventDefault();const text=input.trim();if(!project||!text||busy)return;setMessages(old=>[...old,{id:generateUUID(),role:'user',content:text,message_type:'message',metadata:{},created_at:new Date().toISOString()}]);setInput('');setBusy(true);setError('');try{await api.sendResearchMessage(project.id,text);await open(project);await reloadList()}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
  async function generateSpec(){if(!project)return;setBusy(true);setError('');try{const fresh=await api.generateSpecification(project.id);setProject(fresh);setSpecText(JSON.stringify(fresh.specification?.content??{},null,2));await Promise.all([reloadList(),open(fresh)])}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
  async function resolveDecision(decision:ResearchDecision,answer:string){if(!project||!answer.trim()||decisionBusy)return;setDecisionBusy(decision.id);setError('');try{await api.resolveResearchDecision(project.id,decision.id,answer.trim());setDecisionDrafts(old=>{const next={...old};delete next[decision.id];return next});const[d,m]=await Promise.all([api.researchDecisions(project.id),api.researchMessages(project.id)]);setDecisions(d);setMessages(m)}catch(e){setError((e as Error).message)}finally{setDecisionBusy('')}}
  async function dismissDecision(decision:ResearchDecision){if(!project||decisionBusy)return;setDecisionBusy(decision.id);setError('');try{await api.dismissResearchDecision(project.id,decision.id);setDecisions(await api.researchDecisions(project.id))}catch(e){setError((e as Error).message)}finally{setDecisionBusy('')}}
  const focusDecisions=()=>{setViewStage(0);requestAnimationFrame(()=>decisionsRef.current?.scrollIntoView({behavior:'smooth',block:'end'}))}
  async function saveSpec(){if(!project?.specification)return;try{const content=JSON.parse(specText);const fresh=await api.updateSpecification(project.id,project.specification.id,content);setProject(fresh);setEditingSpec(false)}catch(e){setError(e instanceof SyntaxError?'规格JSON格式错误':(e as Error).message)}}
  async function approve(){if(!project?.specification)return;setBusy(true);try{const fresh=await api.approveSpecification(project.id,project.specification.id);setProject(fresh);await reloadList()}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
  async function implement(force:boolean=false){if(!project)return;setBusy(true);setError('');try{const created=await api.createResearchImplementation(project.id,clientId(),force);setImplementationPrompt(created.prompt);const fresh=await api.researchProject(project.id);setProject(fresh);setAgentOpen(true);await reloadList()}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
  function openImplementation(){setImplementationPrompt('');setAgentOpen(true)}
  async function showBacktest(){if(!project)return;setBusy(true);setError('');try{setPreview(await api.researchStrategyPreview(project.id));setBacktestOpen(true)}catch(e){setError((e as Error).message)}finally{setBusy(false)}}

  async function executeResearchBacktest(payload:Record<string,any>){
    if(!project)return
    setBusy(true)
    setError('')
    try{
      await api.createResearchRun(project.id,payload)
      setBacktestOpen(false)
      await open(project)
    }catch(e){
      setError((e as Error).message)
    }finally{
      setBusy(false)
    }
  }

  async function startBacktest(e:React.FormEvent<HTMLFormElement>){
    e.preventDefault()
    if(!project||!preview)return
    const f=new FormData(e.currentTarget),params:Record<string,unknown>={}
    Object.entries(preview.parameter_schema).forEach(([key,spec])=>{
      const raw=f.get('param_'+key)
      params[key]=spec.type==='boolean'?raw==='on':spec.type==='integer'||spec.type==='number'?Number(raw):raw
    })
    setBusy(true)
    setError('')
    try{
      const symbols=String(f.get('symbols')).split(',').map(x=>x.trim()).filter(Boolean)
      const timeframes=preview.data_requirements.timeframes
      const startDate=String(f.get('start'))
      const endDate=String(f.get('end'))
      const catalogPath=(f.get('catalog_path') as string)||null
      const published=await api.publishResearchStrategy(project.id)
      const payload={
        name:f.get('name'),
        strategy_version_id:published.latest_version_id,
        strategy_parameters:params,
        venue:'BINANCE',
        symbols,
        timeframes,
        start_date:startDate,
        end_date:endDate,
        initial_balance:Number(f.get('capital')),
        leverage:Number(f.get('leverage')),
        execution_model:f.get('model'),
        funding:Boolean(preview.data_requirements.funding),
        catalog_path:catalogPath,
        ignore_missing_data:false,
      }

      const check=await api.checkBacktestCatalog({
        symbols,
        timeframes,
        start_date:startDate,
        end_date:endDate,
        venue:'BINANCE',
        catalog_path:catalogPath,
      })

      if(check.has_missing){
        setCatalogCheck(check)
        setPendingBacktestData(payload)
        setBusy(false)
        return
      }

      await executeResearchBacktest(payload)
    }catch(e){
      setError((e as Error).message)
      setBusy(false)
    }
  }

  async function confirmProceedWithMissing(){
    if(!pendingBacktestData)return
    await executeResearchBacktest({...pendingBacktestData,ignore_missing_data:true})
    setCatalogCheck(null)
    setPendingBacktestData(null)
  }

  async function analyze(runId:string){if(!project)return;setBusy(true);setViewStage(4);setError('');try{await api.analyzeResearchRun(project.id,runId);await open(project);await reloadList();setViewStage(4)}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
  async function startNextIteration(){if(!project)return;setBusy(true);setError('');try{const fresh=await api.iterateResearch(project.id,{target:'DISCUSSING',reason:'基于上一轮实验结果与研究结论，开启下一轮研讨。请保留上一轮记录，并重新审视假设、反例和下一项高信息量实验。'});await open(fresh);await reloadList();setViewStage(0)}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
  async function showRun(runId:string){try{setRunDetail(await api.run(runId))}catch(e){setError((e as Error).message)}}
  const currentTask=useMemo(()=>{
    if(!project)return null
    if(journeyIndex===0&&!messages.length)return {title:'描述你的市场假设',detail:'从交易逻辑、观察到的市场现象或希望推翻的假设开始。',meta:'由你开始 · Hermes 协助梳理',label:'填写研究想法',action:()=>setInput('我观察到一个可能存在的市场规律：'),checks:['提出可验证的市场观察','明确适用市场与时间尺度']}
    if(journeyIndex===0&&pendingDecisions.length)return {title:`先完成 ${pendingDecisions.length} 项策略决策`,detail:'Hermes 提出了需要你拍板的策略设计选项。逐项决策后才能生成策略规格。',meta:`${pendingDecisions.length} 项待你拍板 · 决策后进入定义阶段`,label:'前往决策',action:focusDecisions,checks:['每项决策都不依赖回测结果','与研究无关的问题可以忽略']}
    if(journeyIndex===0)return {title:'把讨论沉淀为策略规格',detail:'已有研究对话，可以生成第一版结构化规格；后续仍可继续修改。',meta:`已完成 ${userMessages.length} 轮输入 · Hermes 生成`,label:'生成策略规格',action:generateSpec,checks:['核心假设已有讨论','生成后检查风险与退出条件']}
    if(journeyIndex===1&&project.specification?.status==='DRAFT')return {title:'审查并确认策略规格',detail:'确认假设、信号、退出与风险约束，再将规格交给实现阶段。',meta:`规格 V${project.specification.version} · 等待你确认`,label:'确认规格',action:approve,checks:['检查核心假设与适用范围','确认风险约束和数据要求']}
    if(journeyIndex===1)return {title:'将已确认规格交给 Claude',detail:'规格已经冻结，下一步根据规格创建策略代码与测试。',meta:`规格 V${project.specification?.version??1} 已确认 · Claude 执行`,label:'开始策略开发',action:()=>implement(false),checks:['规格已确认','即将创建独立开发会话']}
    if(journeyIndex===2&&!developmentComplete)return {title:'完成策略实现与检查',detail:'Claude 正在独立开发会话中实现规格。查看进度并处理需要确认的问题。',meta:'Claude 负责 · 完成后返回工作台',label:'查看开发进度',action:openImplementation,checks:['策略文件待生成','实现完成后检查代码与参数']}
    if(journeyIndex===2)return {title:'用策略回测验证实现',detail:'策略代码已经开发完成。下一步发布当前版本并创建正式回测实验。',meta:`${strategy?.name||strategyName} · NautilusTrader 验证`,label:'策略回测',action:showBacktest,checks:['策略文件已就绪','回测将冻结当前策略版本']}
    if(journeyIndex===3&&activeRun)return {title:'等待回测实验完成',detail:`${activeRun.name} 正在运行，完成后可查看指标并交给 Hermes 分析。`,meta:`${activeRun.stage} · ${activeRun.progress}%`,label:'查看运行详情',action:()=>showRun(activeRun.id),checks:['NautilusTrader 正在执行','无需离开页面，进度会自动刷新']}
    if(journeyIndex===3&&completedRun)return {title:'把实验结果转化为研究结论',detail:'回测已经完成。让 Hermes 对照原始假设分析证据、风险和反例。',meta:`${completedRun.name} 已完成 · Hermes 分析`,label:'分析实验结果',action:()=>analyze(completedRun.id),checks:['实验结果已生成','下一步判断支持、否定或证据不足']}
    if(journeyIndex===3)return {title:'创建第一组验证实验',detail:'配置标的、区间和资金参数，验证策略是否符合原始假设。',meta:'NautilusTrader 验证',label:'配置回测',action:showBacktest,checks:['策略版本已准备','建议先运行基准参数']}
    return {title:'基于结论开启下一轮研讨',detail:'保留本轮策略、回测与分析记录，回到研讨阶段重新审视假设并定义下一项实验。',meta:'新一轮研究 · Hermes 协助研讨',label:'创建下一轮实验',action:startNextIteration,checks:['复核收益与风险是否支持假设','带着本轮局限进入下一轮研讨']}
  },[project,journeyIndex,messages.length,userMessages.length,strategy,developmentComplete,activeRun,completedRun,pendingDecisions.length])
  const suggestions = ['多周期 EMA 动量突破与 ATR 跟踪止损', '布林带与 RSI 多重过滤均值回归策略', 'ATR 波动率自适应动态网格', '资金费率中性套利与基差动量']
  return <div className="research-page">{error&&<div className="form-error research-error">{error}</div>}
    <div className="research-workspace"><aside className="research-list"><div className="research-list-title"><BrainCircuit/><span>研究项目</span>{projects.length>0&&<span className="project-count-badge">{projects.length}</span>}</div><div className="research-list-action"><button className="button primary research-create-btn" onClick={()=>setCreating(true)}><Plus size={14}/>新建研究</button></div>{projects.map(item=><button className={item.id===project?.id?'active':''} onClick={()=>open(item)} key={item.id}><b>{item.title}</b><span>{stageNames[item.status]??item.status}</span><time>{new Date(item.updated_at).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})}</time></button>)}{!projects.length&&<div className="research-empty">从一个策略想法开始研究</div>}</aside>
      <main className="research-stage-pages"><div className="research-stage">{project?<><div className="research-journey" role="navigation" aria-label="研究阶段">{journeyStages.map((stage,index)=><button type="button" key={stage.name} disabled={index>journeyIndex} onClick={()=>setViewStage(index)} className={`journey-step ${index<journeyIndex?'complete':index===journeyIndex?'active':'upcoming'} ${viewStage===index?'selected':''}`} aria-current={viewStage===index?'page':undefined}><span className="journey-marker">{index<journeyIndex?<Check size={13}/>:index+1}</span><span className="journey-copy"><b>{stage.name}</b><small>{stage.description}</small></span></button>)}</div>{viewStage===journeyIndex&&currentTask&&<div className="research-current-task-bar"><div className="current-task-content"><span className="current-task-tag">当前任务</span><b className="current-task-title">{currentTask.title}</b><span className="current-task-sep">·</span><span className="current-task-detail" title={currentTask.detail}>{currentTask.detail}</span></div><button type="button" className="current-task-btn" disabled={busy} onClick={currentTask.action}>{busy?<><Loader2 size={13} className="spin"/> 处理中…</>:<>{currentTask.label}<ArrowRight size={13}/></>}</button></div>}</>:<span>尚未选择研究</span>}</div>
        {!project?<div className="research-welcome stage-empty"><div className="welcome-icon"><BrainCircuit/></div><span>AI 驱动的量化策略研究工作台</span><h2>从一个值得验证的市场假设开始</h2><p>先新建一个研究主题，再进入研讨、定义、实现、验证与结论五个独立工作页面。</p><button className="button primary" onClick={()=>setCreating(true)}><MessageSquarePlus/>新建研究主题</button></div>:<div className="stage-page-body">
          {viewStage===0&&(
            <div className="stage-discussion">
              <div className="discussion-main">
                {userMessages.length>0&&(
                  <div className={`research-questions-dock ${questionsOpen?'open':'closed'}`}>
                    <div className="dock-head" onClick={()=>setQuestionsOpen(!questionsOpen)}>
                      <span className="dock-title"><ListFilter size={13}/><span>对话大纲</span><b className="dock-count">{userMessages.length}</b></span>
                      <button type="button" className="dock-toggle-btn" onClick={(e)=>{e.stopPropagation();setQuestionsOpen(!questionsOpen)}}>{questionsOpen?<ChevronUp size={14}/>:<ChevronDown size={14}/>}</button>
                    </div>
                    {questionsOpen&&(
                      <div className="dock-list">
                        {userMessages.map((msg,idx)=>(
                          <button key={msg.id} type="button" className="dock-item" onClick={()=>scrollToMessage(msg.id)}>
                            <span className="dock-idx">Q{idx+1}</span>
                            <span className="dock-text">{msg.content}</span>
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                )}
                <div className="research-timeline" ref={timeline}>
                  {messages.length?(
                    messages.filter((message)=>message.message_type!=='analysis').map((message)=>(
                      message.message_type==='decision'?(
                        <article id={`msg-${message.id}`} className="research-decision-log" key={message.id}>
                          <GitBranch size={13}/>
                          <span>{message.content.replace(/^决策：/,'')}</span>
                          <time>{new Date(message.created_at).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'})}</time>
                        </article>
                      ):(
                        <article id={`msg-${message.id}`} className={'research-message '+message.role} key={message.id}>
                          <header>
                            <div className="message-author">
                              <span className="author-avatar">{message.role==='user'?'你':'H'}</span>
                              <b>{message.role==='user'?'你':'Hermes'}</b>
                              {message.role!=='user'&&<small>首席量化研究员</small>}
                            </div>
                            <time>{new Date(message.created_at).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'})}</time>
                          </header>
                          <div className="research-markdown">
                            <ReactMarkdown remarkPlugins={[remarkGfm]}>{readableMessage(message)}</ReactMarkdown>
                          </div>
                        </article>
                      )
                    ))
                  ):(
                    <div className="research-welcome">
                      <div className="welcome-icon"><BrainCircuit/></div>
                      <h2>输入具体的策略想法开始研讨</h2>
                      <p>描述交易逻辑、市场观察或希望验证的假设，Hermes 将帮助你寻找反例。</p>
                      <div className="welcome-prompts">
                        {suggestions.map((s)=>(
                          <button key={s} type="button" className="welcome-prompt-chip" onClick={()=>setInput(`我想研究一个${s}，请帮我梳理交易逻辑、因子构建与假设可证伪性。`)}>{s}</button>
                        ))}
                      </div>
                    </div>
                  )}
                  {busy&&(
                    <div className="research-thinking">
                      <div className="thinking-avatar">H</div>
                      <div><b>Hermes 正在研究</b><span>正在梳理假设、特征因子与潜在反例…</span></div>
                      <Loader2/>
                    </div>
                  )}
                </div>
                {(activeDecision||settledDecisions.length>0)&&(
                  <section className="decision-bottom-dock" ref={decisionsRef} aria-label="策略决策">
                    {activeDecision&&(
                      <article className="decision-card-inline">
                        <header className="decision-inline-header">
                          <span className="decision-inline-title">
                            <GitBranch size={13}/>
                            <b>待你拍板的决策</b>
                            {pendingDecisions.length>1&&<span className="decision-page-badge">{Math.min(decisionIndex+1,pendingDecisions.length)} / {pendingDecisions.length}</span>}
                          </span>
                          <div className="decision-inline-controls">
                            {pendingDecisions.length>1&&(
                              <div className="decision-nav-btns">
                                <button type="button" title="上一个决策" disabled={decisionIndex===0} onClick={()=>setDecisionIndex((old)=>Math.max(0,old-1))}><ChevronLeft size={13}/></button>
                                <button type="button" title="下一个决策" disabled={decisionIndex>=pendingDecisions.length-1} onClick={()=>setDecisionIndex((old)=>Math.min(pendingDecisions.length-1,old+1))}><ChevronRight size={13}/></button>
                              </div>
                            )}
                            {settledDecisions.length>0&&(
                              <button type="button" className="decision-settled-toggle" onClick={()=>setSettledOpen(!settledOpen)}>
                                <CheckCircle2 size={12}/>已决策 {settledDecisions.length} 项{settledOpen?<ChevronDown size={12}/>:<ChevronUp size={12}/>}
                              </button>
                            )}
                          </div>
                        </header>
                        <div className="decision-inline-body">
                          <div className="decision-q-row">
                            <h4>{activeDecision.question}</h4>
                            {activeDecision.impact&&<p className="decision-impact">{activeDecision.impact}</p>}
                          </div>
                          {activeDecision.options.length>0&&(
                            <div className="decision-options">
                              {activeDecision.options.map((option)=>(
                                <button
                                  type="button"
                                  key={option}
                                  className={option===activeDecision.recommendation?'recommended':''}
                                  disabled={Boolean(decisionBusy)}
                                  onClick={()=>resolveDecision(activeDecision,option)}
                                >
                                  {option}
                                  {option===activeDecision.recommendation&&<span className="decision-recommended-tag">推荐</span>}
                                </button>
                              ))}
                            </div>
                          )}
                          <div className="decision-custom">
                            <input
                              value={decisionDrafts[activeDecision.id]??''}
                              placeholder={activeDecision.options.length?'或输入你自己的决策…':'输入你的决策…'}
                              onChange={(e)=>setDecisionDrafts((old)=>({...old,[activeDecision.id]:e.target.value}))}
                              onKeyDown={(e)=>{if(e.key==='Enter'&&!e.nativeEvent.isComposing){e.preventDefault();resolveDecision(activeDecision,decisionDrafts[activeDecision.id]??'')}}}
                            />
                            <button
                              type="button"
                              className="decision-confirm"
                              disabled={!(decisionDrafts[activeDecision.id]??'').trim()||Boolean(decisionBusy)}
                              onClick={()=>resolveDecision(activeDecision,decisionDrafts[activeDecision.id]??'')}
                            >
                              {decisionBusy===activeDecision.id?<Loader2 size={13} className="spin"/>:<Check size={13}/>}确认
                            </button>
                            <button
                              type="button"
                              className="decision-dismiss"
                              disabled={Boolean(decisionBusy)}
                              onClick={()=>dismissDecision(activeDecision)}
                            >
                              忽略
                            </button>
                          </div>
                        </div>
                        {settledOpen&&settledDecisions.length>0&&(
                          <div className="decision-settled-drawer">
                            <ul className="decision-settled-list">
                              {settledDecisions.map((decision)=>(
                                <li key={decision.id}>
                                  <span className="settled-q">{decision.question}</span>
                                  {decision.status==='DISMISSED'?(
                                    <span className="settled-a dismissed">已忽略</span>
                                  ):(
                                    <span className="settled-a">{decision.answer}</span>
                                  )}
                                </li>
                              ))}
                            </ul>
                          </div>
                        )}
                      </article>
                    )}
                    {!activeDecision&&settledDecisions.length>0&&(
                      <div className="decision-settled-bar">
                        <button type="button" className="decision-settled-btn" onClick={()=>setSettledOpen(!settledOpen)}>
                          <CheckCircle2 size={13}/>
                          <span>已拍板全部 {settledDecisions.length} 项决策</span>
                          {settledOpen?<ChevronDown size={13}/>:<ChevronUp size={13}/>}
                        </button>
                        {settledOpen&&(
                          <ul className="decision-settled-list">
                            {settledDecisions.map((decision)=>(
                              <li key={decision.id}>
                                <span className="settled-q">{decision.question}</span>
                                {decision.status==='DISMISSED'?(
                                  <span className="settled-a dismissed">已忽略</span>
                                ):(
                                  <span className="settled-a">{decision.answer}</span>
                                )}
                              </li>
                            ))}
                          </ul>
                        )}
                      </div>
                    )}
                  </section>
                )}
                <form className="research-composer" onSubmit={send}>
                  <textarea
                    value={input}
                    onChange={(e)=>setInput(e.target.value)}
                    onKeyDown={(e)=>{if(e.key==='Enter'&&!e.shiftKey&&!e.nativeEvent.isComposing){e.preventDefault();e.currentTarget.form?.requestSubmit()}}}
                    placeholder="继续讨论策略逻辑、数据、风险或潜在反例…"
                  />
                  <div className="composer-footer">
                    <span>Enter 发送 · Shift+Enter 换行</span>
                    <button title="发送消息" disabled={busy||!input.trim()}><ArrowUp/></button>
                  </div>
                </form>
              </div>
            </div>
          )}
          {viewStage===1&&<div className="stage-content definition-page">{project.specification?<><div className="stage-content-toolbar"><div><Status value={project.specification.status}/><span>规格 V{project.specification.version}</span></div>{project.specification.status==='DRAFT'&&!editingSpec&&<button onClick={()=>setEditingSpec(true)}>编辑完整规格</button>}</div>{editingSpec?<div className="definition-editor"><textarea className="spec-editor" value={specText} onChange={e=>setSpecText(e.target.value)}/><div><button onClick={()=>setEditingSpec(false)}>取消</button><button className="button primary" onClick={saveSpec}>保存修改</button></div></div>:<div className="spec-document"><ReactMarkdown remarkPlugins={[remarkGfm]}>{jsonAsMarkdown(project.specification.content)}</ReactMarkdown></div>}</>:<div className="stage-data-empty"><FileJson/><h3>尚未生成策略规格</h3><p>先在研讨页面明确假设，再生成结构化规格。</p><button onClick={()=>setViewStage(0)}>返回研讨页面</button></div>}</div>}
          {viewStage===2&&<div className="stage-content implementation-page">{project.implementation_session_id?<div className="research-code-workspace"><div className="research-dev-status"><div className={developmentComplete?'complete':'working'}>{developmentComplete?<CheckCircle2/>:<Loader2/>}<span><b>{developmentComplete?'策略开发已完成':'策略正在开发'}</b><small>{developmentComplete?'代码已生成并注册，可进入验证阶段':'Claude 会话已创建，等待策略文件生成并注册'}</small></span></div><div className="implementation-actions">{developmentComplete&&<button className="button primary" onClick={showBacktest}><FlaskConical size={14}/>配置基准回测</button>}<button className="button" onClick={openImplementation}><Code2 size={14}/>查看 Claude 开发会话</button><button className="button" disabled={busy} onClick={()=>implement(true)}><RotateCcw size={14}/>重新开发</button>{strategyName&&<Link className="button" to={`/strategies/${strategyName}?research=${project.id}`}>打开完整编辑器 <ExternalLink size={14}/></Link>}</div></div><div className="detail-editor research-readonly-editor"><div className="editor-tabs"><span><Code2/>{implementationFile?.filename??`${strategyName||'strategy'}.py`}</span><div className="editor-actions"><Status value={developmentComplete?'READY':'IMPLEMENTING'}/></div></div>{implementationFile?.content?<CodeEditor value={implementationFile.content} onChange={()=>{}} readOnly/>:<div className="editor-empty"><Loader2 className={developmentComplete?'':'spin'}/><p>{developmentComplete?'正在读取策略代码…':'Claude 完成实现后，代码将在这里显示'}</p></div>}<div className="editor-status"><span>Python</span><span>{implementationFile?.content?.split('\n').length??0} 行</span><span>{developmentComplete?'开发完成 · 已注册':'开发进行中'}</span></div></div></div>:<div className="stage-data-empty"><Code2/><h3>策略尚未进入实现</h3><p>{project.specification?.status==='APPROVED'?'策略规格已确认，可直接创建 Claude 会话进行策略代码开发。':'请先在定义页面确认策略规格，再创建 Claude 开发会话。'}</p>{project.specification?.status==='APPROVED'?<button className="button primary" disabled={busy} onClick={()=>implement(false)}><Code2 size={14}/>开始策略开发</button>:<button onClick={()=>setViewStage(1)}>查看策略定义</button>}</div>}</div>}
          {viewStage===3&&<div className="stage-content validation-page">{busy&&<div className="research-thinking" style={{maxWidth:1080,margin:'0 auto 16px'}}><div className="thinking-avatar">H</div><div><b>Hermes 正在分析回测结果</b><span>对照原始假设深度分析证据、收益分布、极端回撤与潜在反例…</span></div><Loader2/></div>}{runs.length?<div className="table-card backtest-history research-validation-history"><table><thead><tr><th>任务名称</th><th>回测策略</th><th>市场与周期</th><th>状态</th><th>进度</th><th>总收益</th><th>Sharpe</th><th>创建时间</th><th aria-label="操作"/></tr></thead><tbody>{runs.map(run=>{const detail=runDetails[run.id],config=detail?.config??{};return <tr key={run.id} className="clickable-run" tabIndex={0} onClick={()=>showRun(run.id)} onKeyDown={event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();showRun(run.id)}}} aria-label={`查看回测详情：${run.name}`}><td><strong>{run.name}</strong><small>{Array.isArray(config.symbols)?config.symbols.join(' / '):'—'}</small></td><td>{strategy?.name??project.title}</td><td>{String(config.venue??'—')} · {Array.isArray(config.timeframes)?config.timeframes.join(' / '):'—'}</td><td><Status value={run.status}/></td><td><div className="progress"><i style={{width:`${run.progress}%`}}/></div><small>{run.stage} · {run.progress}%</small></td><td className={(run.metrics?.total_return??0)>=0?'positive':'negative'}>{run.metrics&&run.metrics.total_return!=null?`${run.metrics.total_return}%`:'—'}</td><td>{String(run.metrics?.sharpe_ratio??run.metrics?.sharpe??'—')}</td><td>{new Date(run.created_at).toLocaleString('zh-CN')}</td><td><span className="detail-link">查看详情<ChevronRight/></span></td></tr>})}</tbody></table></div>:<div className="stage-data-empty"><FlaskConical/><h3>尚未创建验证实验</h3><p>策略实现并注册后，可以在这里创建第一组基准回测。</p>{journeyIndex>=2&&viewStage!==journeyIndex&&<button onClick={showBacktest}>配置基准回测</button>}</div>}</div>}
          {viewStage===4&&<div className="stage-content conclusion-page">{busy&&<div className="research-thinking" style={{maxWidth:980,margin:'0 auto 20px'}}><div className="thinking-avatar">H</div><div><b>Hermes 正在分析回测结果</b><span>对照原始假设深度分析证据、收益分布、极端回撤与潜在反例…</span></div><Loader2/></div>}{analysisMessages.length?<div className="analysis-list">{analysisMessages.map(message=><article key={message.id}><header><BrainCircuit/><div><b>Hermes 实验分析</b><time>{new Date(message.created_at).toLocaleString('zh-CN')}</time></div></header><div className="research-markdown"><ReactMarkdown remarkPlugins={[remarkGfm]}>{readableMessage(message)}</ReactMarkdown></div></article>)}</div>:(!busy&&<div className="stage-data-empty"><BrainCircuit/><h3>尚未形成研究结论</h3><p>完成回测后，将结果交给 Hermes 分析，结论会集中显示在这里。</p>{completedRun&&<button onClick={()=>analyze(completedRun.id)}>分析最近一次实验</button>}</div>)}</div>}
        </div>}</main></div>
    {creating&&<div className="modal-backdrop"><section className="modal"><button className="modal-close" onClick={()=>setCreating(false)}><X/></button><h2>新建研究主题</h2><p className="muted">这里只创建会话，进入后再输入具体研究内容。</p><form className="stack-form" onSubmit={create}><label>主题名称<input name="title" required autoFocus placeholder="例如：MA + MACD 趋势策略"/></label><button className="button primary" disabled={busy}>{busy?'正在创建…':'下一步：创建会话'}</button></form></section></div>}
    {agentOpen&&project&&strategyName&&<div className="research-agent-overlay"><AgentPanel key={project.implementation_session_id??'agent'} strategyName={strategyName} sessionId={project.implementation_session_id??undefined} initialPrompt={implementationPrompt||undefined} onClose={()=>{setAgentOpen(false);setImplementationPrompt('')}} onApplied={async()=>{setAgentOpen(false);setImplementationPrompt('');await open(project)}}/></div>}
    {backtestOpen&&preview&&<div className="modal-backdrop"><section className="modal research-backtest-modal"><button className="modal-close" onClick={()=>setBacktestOpen(false)}><X/></button><h2>策略回测配置</h2><p className="muted">确认后自动发布当前策略版本，并创建正式 NautilusTrader 回测。</p><form className="stack-form" onSubmit={startBacktest}><div className="form-grid"><label>任务名称<input name="name" defaultValue={`${project?.title} · 基准回测`} required/></label><label>交易标的<input name="symbols" defaultValue="BTCUSDT, ETHUSDT, SOLUSDT" required/></label><label>开始日期<input type="date" name="start" defaultValue="2024-01-01" required/></label><label>结束日期<input type="date" name="end" defaultValue="2025-12-31" required/></label><label>初始资金<input type="number" name="capital" defaultValue="10000" required/></label><label>杠杆<input type="number" name="leverage" defaultValue="4" required/></label><label className="wide">Catalog 路径<input name="catalog_path" placeholder="留空使用系统默认 CATALOG_PATH"/></label></div><h3>策略参数</h3><div className="form-grid">{Object.entries(preview.parameter_schema).map(([key,spec])=><label key={key}>{spec.title??key}{spec.type==='boolean'?<input name={'param_'+key} type="checkbox" defaultChecked={Boolean(spec.default)}/>:<input name={'param_'+key} type="number" step={spec.type==='integer'?'1':'any'} min={spec.min} max={spec.max} defaultValue={String(spec.default)} required/>}</label>)}</div><h3>执行模型</h3><div className="model-select">{[['FAST','快速'],['STANDARD','标准'],['CONSERVATIVE','保守']].map(([value,label])=><label key={value}><input type="radio" name="model" value={value} defaultChecked={value==='CONSERVATIVE'}/><span><b>{label}</b><small>{value}</small></span></label>)}</div><button className="button primary" disabled={busy}>{busy?'正在发布并创建…':'下一步：发布版本并开始回测'}</button></form></section></div>}{runDetail&&<div className="modal-backdrop"><section className="modal research-result-modal"><button className="modal-close" onClick={()=>setRunDetail(null)}><X/></button><div className="result-modal-head"><div><Status value={runDetail.status}/><h2>{runDetail.name}</h2><p>{runDetail.stage} · {runDetail.progress}%</p></div><Link className="button" to={`/backtests/${runDetail.id}?research=${project?.id}`}>打开完整详情 <ExternalLink/></Link></div><div className="research-run-progress large"><i style={{width:`${runDetail.progress}%`}}/></div>{runDetail.error_message&&<div className="form-error">{runDetail.error_message}</div>}<div className="result-metric-grid">{Object.entries(runDetail.metrics??{}).slice(0,12).map(([key,value])=><div key={key}><small>{key}</small><b>{String(value??'—')}</b></div>)}</div><h3>回测配置</h3><pre className="result-config">{JSON.stringify(runDetail.config,null,2)}</pre>{runDetail.status==='COMPLETED'&&<button className="button primary" disabled={busy} onClick={()=>{setRunDetail(null);analyze(runDetail.id)}}>下一步：交给 Hermes 分析结果</button>}</section></div>}
    <CatalogMissingDialog
      open={Boolean(catalogCheck)}
      checkResult={catalogCheck}
      busy={busy}
      onCancel={()=>{setCatalogCheck(null);setPendingBacktestData(null)}}
      onConfirm={confirmProceedWithMissing}
    />
  </div>
}


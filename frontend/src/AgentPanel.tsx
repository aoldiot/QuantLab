import {useEffect,useRef,useState} from 'react'
import {ArrowUp,Bolt,Check,ChevronLeft,ChevronRight,FileCode2,GitCompare,Hand,History,MessageSquarePlus,Minimize2,Pencil,Square,Undo2,X} from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {agentSocketUrl,api} from './api'
import type {AgentSession,PermissionMode} from './types'

type ChatLine={role:'user'|'assistant'|'system';text:string;kind?:'tool'}
type ChangeSummary={diff:string;files:{path:string;additions:number;deletions:number}[];additions:number;deletions:number}
const modes:{value:PermissionMode;label:string;description:string;icon:typeof Hand}[]=[
  {value:'default',label:'手动审批',description:'基础读写自动允许，敏感操作请求批准',icon:Hand},
  {value:'acceptEdits',label:'自动编辑',description:'自动修改代码，其他操作仍按规则审批',icon:Pencil},
  {value:'plan',label:'规划',description:'只分析代码并给出方案，不修改文件',icon:FileCode2},
  {value:'bypassPermissions',label:'完全自动',description:'在平台安全边界内自动执行允许的操作',icon:Bolt},
]
const clientId=()=>{let value=localStorage.getItem('quantlab_client_id');if(!value){value=crypto.randomUUID();localStorage.setItem('quantlab_client_id',value)}return value}
function collectText(value:unknown):string[]{if(typeof value==='string')return[];if(Array.isArray(value))return value.flatMap(collectText);if(value&&typeof value==='object'){const item=value as Record<string,unknown>;const own=typeof item.text==='string'?[item.text]:[];return own.concat(Object.entries(item).filter(([key])=>key!=='text').flatMap(([,child])=>collectText(child)))}return[]}
const toolNames:Record<string,string>={Read:'读取文件',Glob:'查找文件',Grep:'搜索代码',Edit:'编辑文件',Write:'写入文件',Bash:'执行命令',Skill:'调用策略技能',Agent:'启动子任务',AskUserQuestion:'询问用户',WebSearch:'联网搜索',WebFetch:'读取网页'}
const fieldNames:Record<string,string>={command:'命令',file_path:'文件路径',path:'路径',pattern:'搜索规则',query:'查询内容',old_string:'原内容',new_string:'新内容',content:'内容',description:'说明'}
function isHandoffPrompt(text:string):boolean{if(!text)return false;return text.includes('QuantLab策略规格')||text.includes('请严格按照已确认的')||text.startsWith('请严格按照已确认的QuantLab策略规格')||(text.includes('strategy_name')&&text.includes('hypothesis')&&text.includes('market'))}
function approvalInput(value:unknown):string{if(!value||typeof value!=='object')return String(value??'');return JSON.stringify(Object.fromEntries(Object.entries(value as Record<string,unknown>).map(([key,item])=>[fieldNames[key]??key,item])),null,2)}
function toolLines(value:unknown):ChatLine[]{if(!Array.isArray(value))return[];return value.flatMap(item=>{if(!item||typeof item!=='object')return[];const call=item as Record<string,unknown>;if(typeof call.name!=='string')return[];const input=call.input&&typeof call.input==='object'?call.input as Record<string,unknown>:{};const target=String(input.file_path??input.path??input.command??input.pattern??input.query??'').trim();return[{role:'system',kind:'tool',text:`${toolNames[call.name]??call.name}${target?` · ${target}`:''}`}]})}

export default function AgentPanel({strategyName,onClose,onApplied,initialPrompt,sessionId}:{strategyName:string;onClose:()=>void;onApplied:()=>Promise<void>;initialPrompt?:string;sessionId?:string}){
  const[session,setSession]=useState<AgentSession|null>(null),[mode,setMode]=useState<PermissionMode>('default'),[modeOpen,setModeOpen]=useState(false),[ready,setReady]=useState(false),[lines,setLines]=useState<ChatLine[]>([]),[input,setInput]=useState(''),[status,setStatus]=useState('IDLE'),[context,setContext]=useState({percentage:0,totalTokens:0,maxTokens:0}),[changes,setChanges]=useState<ChangeSummary|null>(null),[showDiff,setShowDiff]=useState(false),[error,setError]=useState(''),[approval,setApproval]=useState<{request_id:string;tool:string;input:unknown}|null>(null),[width,setWidth]=useState(()=>Math.max(560,Math.round(window.innerWidth/2)))
  const[sessions,setSessions]=useState<AgentSession[]>([]),[historyCollapsed,setHistoryCollapsed]=useState(false),[sessionPreviews,setSessionPreviews]=useState<Record<string,string>>({}),[thinkingTokens,setThinkingTokens]=useState(0)
  const socket=useRef<WebSocket|null>(null)
  const reconnectTimer=useRef<number|null>(null)
  const errorTimer=useRef<number|null>(null)
  const reconnectCount=useRef(0)
  const mounted=useRef(true)
  const composing=useRef(false)
  const lastSentPromptSessionId=useRef<string|null>(null)
  const initialPromptStarted=useRef(false)
  const initialPromptCompleted=useRef(false)
  const conversationRef=useRef<HTMLDivElement|null>(null)
  const running=status==='RUNNING'||status==='QUEUED'
  const selectedMode=modes.find(item=>item.value===mode)??modes[0]

  function closeCurrentSocket(){
    if(reconnectTimer.current!==null){window.clearTimeout(reconnectTimer.current);reconnectTimer.current=null}
    if(errorTimer.current!==null){window.clearTimeout(errorTimer.current);errorTimer.current=null}
    reconnectCount.current=0;
    if(socket.current){
      const prev=socket.current;
      socket.current=null;
      try{prev.onclose=null;prev.onerror=null;prev.close(1000)}catch{}
    }
  }

  function connect(created:AgentSession){
    closeCurrentSocket();
    setReady(false);setSession(created);setMode(created.permission_mode);setStatus(created.status)
    const ws=new WebSocket(agentSocketUrl(created.id));socket.current=ws
    ws.onopen=async()=>{
      setReady(true);
      reconnectCount.current=0;
      if(errorTimer.current!==null){window.clearTimeout(errorTimer.current);errorTimer.current=null}
      setError('');
      try{
        const[saved,pending]=await Promise.all([api.agentMessages(created.id),api.agentDiff(created.id)]);
        if(socket.current!==ws)return;
        setLines(messageLines(saved));
        setThinkingTokens(savedThinking(saved));
        setContext(savedContext(saved));
        setChanges(pending.files.length?pending:null)
      }catch{/* WebSocket 已连接，历史恢复失败不阻断当前任务 */}
      if(initialPrompt&&lastSentPromptSessionId.current!==created.id&&socket.current===ws){
        lastSentPromptSessionId.current=created.id;
        ws.send(JSON.stringify({type:'message',content:initialPrompt}))
      }
    }
    ws.onclose=(event:CloseEvent)=>{
      setReady(false);
      if(socket.current!==ws||!mounted.current||event.code===1000||event.code===1001||event.code===1005||event.wasClean)return;
      if(errorTimer.current===null){
        errorTimer.current=window.setTimeout(()=>{
          if(mounted.current&&socket.current===ws)setError('Agent 连接已断开，正在自动重连；后台任务会继续执行。')
        },3500)
      }
      reconnectCount.current+=1;
      const delay=Math.min(4000,1000*reconnectCount.current);
      reconnectTimer.current=window.setTimeout(()=>{
        if(mounted.current&&socket.current===ws)connect(created)
      },delay)
    }
    ws.onerror=()=>{
      if(socket.current===ws&&mounted.current&&reconnectCount.current>2){
        setError('Agent 连接失败，请检查网络或重新打开面板重试。')
      }
    }
    ws.onmessage=e=>{const data=JSON.parse(e.data);if(data.type==='status'){setStatus(data.status);if(data.status==='RUNNING')initialPromptStarted.current=true;if(data.status==='IDLE'&&initialPromptStarted.current)initialPromptCompleted.current=true}if(data.type==='queued'){initialPromptStarted.current=true;setThinkingTokens(0);setStatus('QUEUED')}if(data.type==='context_usage')setContext({percentage:Number(data.usage.percentage??0),totalTokens:Number(data.usage.totalTokens??0),maxTokens:Number(data.usage.maxTokens??0)});if(data.type==='error')setError(data.message||'Agent 执行失败，请重试或查看后端日志。');if(data.type==='backtest_error')setError(data.message);if(data.type==='approval_required')setApproval(data);if(data.type==='changes_ready'){initialPromptCompleted.current=true;setChanges(data)}if(data.type==='backtest_created')setLines(old=>[...old,{role:'system',text:`已创建正式回测记录 ${data.name}（${data.run_id}）`}]);if(data.type==='sdk_event'){if(data.event?.subtype==='thinking_tokens')setThinkingTokens(Number(data.event?.data?.estimated_tokens??0));if(data.event?.subtype==='error_during_execution'){const message=Array.isArray(data.event?.errors)&&data.event.errors.length?data.event.errors.join('；'):'Claude 执行被中断或模型调用失败';const isInterrupted=message.includes('interrupted')||message.includes('interruption')||message.includes('被中断');if(!isInterrupted){setError(message);setLines(old=>[...old,{role:'system',text:message}])}}if(data.event?.message_type==='AssistantMessage'){initialPromptCompleted.current=true;const tools=toolLines(data.event.content);const text=[...new Set(collectText(data.event.content))].join('\n');if(tools.length||text)setLines(old=>[...old,...tools,...(text?[{role:'assistant' as const,text}]:[])])}}}
  }
  function messageLines(saved:Awaited<ReturnType<typeof api.agentMessages>>){return saved.flatMap(message=>{const isUser=message.role==='user',isAssistant=message.content.message_type==='AssistantMessage';if(isUser){const text=[...new Set(collectText(message.content))].join('\n');if(!text||isHandoffPrompt(text))return[];return [{role:'user' as const,text}]}if(!isAssistant)return[];const tools=toolLines(message.content.content),text=[...new Set(collectText(message.content.content))].join('\n');return[...tools,...(text?[{role:'assistant' as const,text}]:[])]})}
  function savedThinking(saved:Awaited<ReturnType<typeof api.agentMessages>>){const value=[...saved].reverse().find(message=>message.event_type==='thinking_tokens')?.content;return Number(value?.data&&typeof value.data==='object'?(value.data as Record<string,unknown>).estimated_tokens:0)}
  function savedContext(saved:Awaited<ReturnType<typeof api.agentMessages>>){const value=[...saved].reverse().find(message=>message.event_type==='context_usage')?.content??{};return{percentage:Number(value.percentage??0),totalTokens:Number(value.totalTokens??0),maxTokens:Number(value.maxTokens??0)}}
  async function openSession(current:AgentSession){closeCurrentSocket();setError('');setApproval(null);setShowDiff(false);try{const[saved,pending]=await Promise.all([api.agentMessages(current.id),api.agentDiff(current.id)]);const restored=messageLines(saved);setLines(restored);setThinkingTokens(savedThinking(saved));setChanges(pending.files.length?pending:null);setContext(savedContext(saved));setSessionPreviews(old=>({...old,[current.id]:restored.find(line=>line.role==='user'&&!isHandoffPrompt(line.text))?.text??'策略开发'}));connect(current)}catch(e){setError((e as Error).message)}}
  useEffect(()=>{let active=true;mounted.current=true;api.agentSessions(clientId(),strategyName).then(async all=>{if(!active)return;setSessions(all);const previews=await Promise.all(all.map(async item=>{const restored=messageLines(await api.agentMessages(item.id));return[item.id,restored.find(line=>line.role==='user'&&!isHandoffPrompt(line.text))?.text??'策略开发'] as const}));if(!active)return;setSessionPreviews(Object.fromEntries(previews));const target=(sessionId&&all.find(item=>item.id===sessionId))||all[0];if(target)await openSession(target)}).catch(e=>setError((e as Error).message));return()=>{active=false;mounted.current=false;closeCurrentSocket()}},[strategyName,sessionId])
  useEffect(()=>{const frame=requestAnimationFrame(()=>{const element=conversationRef.current;if(element)element.scrollTo({top:element.scrollHeight,behavior:'smooth'})});return()=>cancelAnimationFrame(frame)},[lines,changes,status])
  useEffect(()=>{const textarea=document.querySelector<HTMLTextAreaElement>('.agent-composer textarea');if(!textarea)return;textarea.placeholder=ready?'输入策略需求，Enter 发送，Shift+Enter 换行':'正在连接 Agent…';const start=()=>{composing.current=true},end=()=>{composing.current=false},keydown=(event:KeyboardEvent)=>{if(event.key!=='Enter'||event.shiftKey)return;event.stopPropagation();if(event.isComposing||composing.current)return;event.preventDefault();textarea.form?.requestSubmit()};textarea.addEventListener('compositionstart',start);textarea.addEventListener('compositionend',end);textarea.addEventListener('keydown',keydown);return()=>{textarea.removeEventListener('compositionstart',start);textarea.removeEventListener('compositionend',end);textarea.removeEventListener('keydown',keydown)}},[session,ready])
  async function start(){setError('');try{const created=await api.createAgentSession(clientId(),strategyName,mode);setSessions(old=>[created,...old]);connect(created)}catch(e){setError((e as Error).message)}}
  async function newSession(){if(running)return;closeCurrentSocket();setError('');setLines([]);setChanges(null);setContext({percentage:0,totalTokens:0,maxTokens:0});try{const created=await api.createAgentSession(clientId(),strategyName,mode);setSessions(old=>[created,...old]);connect(created)}catch(e){setError((e as Error).message)}}
  async function compact(){if(!session||running)return;try{const ws=await waitForSocket();ws.send(JSON.stringify({type:'compact'}));setLines(old=>[...old,{role:'system',text:'正在压缩会话上下文…'}])}catch(e){setError((e as Error).message)}}
  function beginResize(e:React.PointerEvent){e.preventDefault();const startX=e.clientX,startWidth=width;const move=(event:PointerEvent)=>setWidth(Math.min(window.innerWidth-260,Math.max(380,startWidth+startX-event.clientX)));const stop=()=>{document.removeEventListener('pointermove',move);document.removeEventListener('pointerup',stop)};document.addEventListener('pointermove',move);document.addEventListener('pointerup',stop)}
  async function waitForSocket():Promise<WebSocket>{const ws=socket.current;if(!ws)throw new Error('Agent 尚未连接');if(ws.readyState===WebSocket.OPEN)return ws;if(ws.readyState!==WebSocket.CONNECTING)throw new Error('Agent 连接已断开');return new Promise((resolve,reject)=>{const timer=window.setTimeout(()=>reject(new Error('Agent 连接超时')),8000);ws.addEventListener('open',()=>{clearTimeout(timer);resolve(ws)},{once:true});ws.addEventListener('error',()=>{clearTimeout(timer);reject(new Error('Agent 连接失败'))},{once:true})})}
  async function send(e:React.FormEvent){e.preventDefault();const message=input.trim();if(!message||running)return;try{const ws=await waitForSocket();ws.send(JSON.stringify({type:'message',content:message}));setLines(old=>[...old,{role:'user',text:message}]);setInput('');setChanges(null);setShowDiff(false);setError('')}catch(e){setError((e as Error).message)}}
  function changeMode(next:PermissionMode){setMode(next);setModeOpen(false);socket.current?.send(JSON.stringify({type:'set_mode',mode:next}))}
  async function apply(){if(!session)return;await api.applyAgent(session.id);await onApplied();setChanges(null);setShowDiff(false);setLines(old=>[...old,{role:'system',text:'代码修改已应用到正式策略文件。请检查后发布新版本。'}])}
  async function reject(){if(!session)return;await api.rejectAgent(session.id);setChanges(null);setShowDiff(false);setLines(old=>[...old,{role:'system',text:'已撤销本次 Agent 生成的代码修改。'}])}
  async function stop(){if(!session)return;socket.current?.send(JSON.stringify({type:'cancel'}));await api.cancelAgent(session.id);setStatus('CANCELED')}
  function decide(approved:boolean){socket.current?.send(JSON.stringify({type:'approval',request_id:approval?.request_id,approved}));setApproval(null)}
  const modePicker=<div className="composer-mode"><button type="button" onClick={()=>setModeOpen(value=>!value)}><selectedMode.icon/>{selectedMode.label}</button>{modeOpen&&<div className="mode-menu">{modes.map(item=><button type="button" className={item.value===mode?'active':''} onClick={()=>changeMode(item.value)} key={item.value}><item.icon/><span><b>{item.label}</b><small>{item.description}</small></span>{item.value===mode&&<Check/>}</button>)}</div>}</div>
  const history=<nav className={'agent-history '+(historyCollapsed?'collapsed':'')}><div className="agent-history-head"><span><History/>历史会话</span><button title={historyCollapsed?'展开历史会话':'折叠历史会话'} onClick={()=>setHistoryCollapsed(value=>!value)}>{historyCollapsed?<ChevronRight/>:<ChevronLeft/>}</button></div>{!historyCollapsed&&<div className="agent-history-list">{sessions.length?sessions.map(item=><button className={item.id===session?.id?'active':''} disabled={running&&item.id!==session?.id} onClick={()=>item.id!==session?.id&&openSession(item)} key={item.id}><b>{sessionPreviews[item.id]??`会话 ${item.id.slice(0,6)}`}</b><time>{new Date(item.updated_at).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})}</time></button>):<p>暂无历史会话</p>}</div>}</nav>

  return <aside className="ai-panel agent-panel" style={{width}}><div className="agent-resize-handle" onPointerDown={beginResize}/><header><div><b>策略 Agent</b><span className={'agent-state '+status.toLowerCase()}>{status}</span></div><button onClick={onClose}><X/></button></header>{error&&<div className="ai-error">{error}</div>}<div className="agent-workspace">{history}<main className="agent-chat"><div className="ai-conversation" ref={conversationRef}>{!session?<div className="ai-empty"><p>启动隔离 Agent 工作区，通过自然语言编写当前策略。</p><div className="agent-start-actions">{modePicker}<button className="button primary" onClick={start}>启动 Agent</button></div></div>:<>{lines.length?lines.map((line,index)=><div key={index} className={'chat-line '+line.role+(line.kind?' '+line.kind:'')}>{line.kind==='tool'?<><Bolt/>{line.text}</>:line.role==='assistant'?<ReactMarkdown remarkPlugins={[remarkGfm]}>{line.text}</ReactMarkdown>:line.text}</div>):<div className="ai-empty"><p>描述策略逻辑、风控或需要修复的问题。</p></div>}{running&&<div className="agent-thinking-live"><span className="agent-thinking-pulse"/><div><b>Claude 正在分析与执行</b><small>{thinkingTokens?`已进行约 ${thinkingTokens.toLocaleString()} thinking tokens 的推理`:'正在建立上下文…'}</small></div></div>}{changes&&<section className="change-review-card"><div className="change-review-head"><FileCode2/><div><b>已编辑 {changes.files.length} 个文件</b><span><i>+{changes.additions}</i> <em>-{changes.deletions}</em></span></div><div><button onClick={reject}><Undo2/>撤销</button><button onClick={()=>setShowDiff(value=>!value)}><GitCompare/>审核</button><button className="apply-button" onClick={apply}><Check/>应用</button></div></div>{changes.files.map(file=><div className="change-file" key={file.path}><span>{file.path}</span><b>+{file.additions} <em>-{file.deletions}</em></b></div>)}{showDiff&&<pre className="agent-diff">{changes.diff}</pre>}</section>}</>}</div>{session&&<><div className="agent-commands"><button disabled={running} onClick={newSession}><MessageSquarePlus/>新建会话</button><button disabled={running} onClick={compact}><Minimize2/>压缩会话</button></div><div className="agent-input-stack">{approval&&<div className="agent-approval"><b>是否允许“{toolNames[approval.tool]??approval.tool}”？</b><pre>{approvalInput(approval.input)}</pre><div><button onClick={()=>decide(false)}>拒绝</button><button className="allow" onClick={()=>decide(true)}>允许</button></div></div>}<form className="agent-composer" onSubmit={send}><textarea value={input} onChange={e=>setInput(e.target.value)} onKeyDown={e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();e.currentTarget.form?.requestSubmit()}}} placeholder={ready?'输入策略需求，Enter 发送，Shift+Enter 换行':'正在连接 Agent…'}/><div className="composer-footer">{modePicker}<div className="composer-right"><div className="context-ring" style={{'--context':`${Math.min(100,context.percentage)}%`} as React.CSSProperties} title={`上下文 ${context.percentage.toFixed(1)}% · ${context.totalTokens.toLocaleString()} / ${context.maxTokens.toLocaleString()} tokens`}><span>{Math.round(context.percentage)}</span></div><button type={running?'button':'submit'} onClick={running?stop:undefined} className={'composer-action '+(running?'stop':'send')} disabled={!running&&(!input.trim()||!ready)} aria-label={running?'停止':'发送'}>{running?<Square/>:<ArrowUp/>}</button></div></div></form></div></>}</main></div></aside>
}

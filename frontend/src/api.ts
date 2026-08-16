import type {AgentSession,AgentStoredMessage,ChartData,GitConfiguration,LlmConfiguration,PermissionMode,ResearchDecision,ResearchMessage,ResearchProject,ResearchRun,Run,Strategy,StrategyFile,StrategyGitStatus,StrategyVersion} from './types'
export const AUTH_TOKEN_KEY = 'quantlab_token'
export const AUTH_USER_KEY = 'quantlab_user'
const BASE=import.meta.env.VITE_API_URL??'http://localhost:8000/api'
export const agentSocketUrl=(sessionId:string)=>{
  const token = localStorage.getItem(AUTH_TOKEN_KEY)
  const tokenParam = token ? `?token=${encodeURIComponent(token)}` : ''
  if(BASE.startsWith('http'))return `${BASE.replace(/^http/,'ws')}/agent/ws/${sessionId}${tokenParam}`
  const proto=window.location.protocol==='https:'?'wss:':'ws:'
  return `${proto}//${window.location.host}${BASE}/agent/ws/${sessionId}${tokenParam}`
}
function errorText(detail:unknown):string{if(typeof detail==='string')return detail;if(Array.isArray(detail))return detail.map(x=>{if(typeof x==='object'&&x){const e=x as {loc?:unknown[];msg?:string};return `${e.loc?.slice(1).join('.')||'参数'}：${e.msg||'格式错误'}`}return String(x)}).join('；');if(detail&&typeof detail==='object')return JSON.stringify(detail);return '请求失败'}
async function request<T>(path:string,init?:RequestInit):Promise<T>{
  const token = localStorage.getItem(AUTH_TOKEN_KEY)
  const authHeaders: Record<string, string> = {}
  if (token) {
    authHeaders['Authorization'] = `Bearer ${token}`
  }
  const r=await fetch(BASE+path,{
    ...init,
    headers:{
      'Content-Type':'application/json',
      ...authHeaders,
      ...(init?.headers??{})
    }
  });
  if(!r.ok){
    let body:{detail?:unknown}={}
    try{body=await r.json()}catch{/**/}
    if (r.status === 401 && path !== '/auth/login') {
      localStorage.removeItem(AUTH_TOKEN_KEY)
      localStorage.removeItem(AUTH_USER_KEY)
      if (typeof window !== 'undefined' && window.location.pathname !== '/login') {
        window.location.href = '/login'
      }
    }
    throw new Error(errorText(body.detail) || (r.status === 401 ? '未登录或登录已过期' : '请求失败'))
  }
  if(r.status===204)return undefined as T;
  return r.json()
}
export const api={
  login:(username:string,password:string)=>request<{access_token:string;token_type:string;username:string}>('/auth/login',{method:'POST',body:JSON.stringify({username,password})}),
  me:()=>request<{username:string;authenticated:boolean}>('/auth/me'),
  logout:()=>request<{ok:boolean;message:string}>('/auth/logout',{method:'POST'}),
  strategies:()=>request<Strategy[]>('/strategies'),
  strategy:(id:string)=>request<Strategy>('/strategies/'+id),
  createStrategy:(module:string,versionDescription:string)=>request<Strategy>('/strategies',{method:'POST',body:JSON.stringify({module,version_description:versionDescription})}),
  updateStrategy:(id:string,data:unknown)=>request<Strategy>('/strategies/'+id,{method:'PATCH',body:JSON.stringify(data)}),
  deleteStrategy:(id:string)=>request<void>('/strategies/'+id,{method:'DELETE'}),
  versions:(id:string)=>request<StrategyVersion[]>('/strategies/'+id+'/versions'),
  createVersion:(id:string,module:string,description:string)=>request<StrategyVersion>('/strategies/'+id+'/versions',{method:'POST',body:JSON.stringify({module,description})}),
  deleteVersion:(strategyId:string,versionId:string)=>request<void>(`/strategies/${strategyId}/versions/${versionId}`,{method:'DELETE'}),
  restoreVersion:(strategyId:string,versionId:string)=>request<{ok:boolean;message:string}>(`/strategies/${strategyId}/versions/${versionId}/restore`,{method:'POST'}),
  strategyFiles:()=>request<StrategyFile[]>('/strategy-files'),
  strategyFile:(name:string)=>request<StrategyFile>('/strategy-files/'+name),
  createStrategyFile:(name:string,mode:string,description:string,category:string)=>request<StrategyFile>('/strategy-files',{method:'POST',body:JSON.stringify({name,mode,description,category})}),
  saveStrategyFile:(name:string,content:string)=>request<StrategyFile>('/strategy-files/'+name,{method:'PUT',body:JSON.stringify({content})}),
  updateStrategyFileMetadata:(name:string,description:string,category:string)=>request<StrategyFile>(`/strategy-files/${name}/metadata`,{method:'PATCH',body:JSON.stringify({description,category})}),
  deleteStrategyFile:(name:string)=>request<void>('/strategy-files/'+name,{method:'DELETE'}),
  strategyGitStatus:()=>request<StrategyGitStatus>('/strategy-files/git/status'),
  commitStrategyFiles:(message:string)=>request<{commit:string;message:string}>('/strategy-files/git/commit',{method:'POST',body:JSON.stringify({message})}),
  llmConfig:()=>request<LlmConfiguration>('/settings/llm'),
  saveLlmConfig:(data:unknown)=>request<LlmConfiguration>('/settings/llm',{method:'PUT',body:JSON.stringify(data)}),
  testLlmConfig:(deep=false)=>request<{ok:boolean;message:string}>(`/settings/llm/test?deep=${deep}`,{method:'POST'}),
  testHermesConfig:()=>request<{ok:boolean;message:string}>('/settings/llm/test-hermes',{method:'POST'}),
  gitConfig:()=>request<GitConfiguration>('/settings/git'),
  saveGitConfig:(data:unknown)=>request<GitConfiguration>('/settings/git',{method:'PUT',body:JSON.stringify(data)}),
  testGitConfig:()=>request<{ok:boolean;message:string}>('/settings/git/test',{method:'POST'}),
  backupGit:()=>request<{ok:boolean;message:string;files_count:number;commit?:string}>('/settings/git/backup',{method:'POST'}),
  createAgentSession:(client_id:string,strategy_name:string,permission_mode:PermissionMode)=>request<AgentSession>('/agent/sessions',{method:'POST',body:JSON.stringify({client_id:client_id||'default_client',strategy_name,permission_mode})}),
  agentSessions:(clientId?:string,strategyName?:string)=>{
    const qs=new URLSearchParams()
    if(clientId)qs.set('client_id',clientId)
    if(strategyName)qs.set('strategy_name',strategyName)
    const qStr=qs.toString()
    return request<AgentSession[]>(`/agent/sessions${qStr?'?'+qStr:''}`)
  },
  agentMessages:(sessionId:string)=>request<AgentStoredMessage[]>(`/agent/sessions/${sessionId}/messages`),
  agentDiff:(sessionId:string)=>request<{diff:string;files:{path:string;additions:number;deletions:number}[];additions:number;deletions:number}>(`/agent/sessions/${sessionId}/diff`),
  applyAgent:(sessionId:string)=>request<{applied:boolean;requires_publish_confirmation:boolean}>(`/agent/sessions/${sessionId}/apply`,{method:'POST',body:JSON.stringify({create_version:false})}),
  rejectAgent:(sessionId:string)=>request<{rejected:boolean}>(`/agent/sessions/${sessionId}/reject`,{method:'POST'}),
  cancelAgent:(sessionId:string)=>request<AgentSession>(`/agent/sessions/${sessionId}/cancel`,{method:'POST'}),
  researchProjects:(clientId?:string)=>{
    const qs=clientId?`?client_id=${encodeURIComponent(clientId)}`:''
    return request<ResearchProject[]>(`/research${qs}`)
  },
  createResearch:(title:string,client_id?:string)=>request<ResearchProject>('/research',{method:'POST',body:JSON.stringify({client_id:client_id||'default_client',title})}),
  researchProject:(id:string)=>request<ResearchProject>('/research/'+id),
  researchMessages:(id:string)=>request<ResearchMessage[]>(`/research/${id}/messages`),
  sendResearchMessage:(id:string,content:string)=>request<{role:string;content:string;decisions:ResearchDecision[]}>(`/research/${id}/messages`,{method:'POST',body:JSON.stringify({content})}),
  researchDecisions:(id:string)=>request<ResearchDecision[]>(`/research/${id}/decisions`),
  resolveResearchDecision:(id:string,decisionId:string,answer:string)=>request<ResearchDecision>(`/research/${id}/decisions/${decisionId}/resolve`,{method:'POST',body:JSON.stringify({answer})}),
  dismissResearchDecision:(id:string,decisionId:string)=>request<ResearchDecision>(`/research/${id}/decisions/${decisionId}/dismiss`,{method:'POST'}),
  generateSpecification:(id:string)=>request<ResearchProject>(`/research/${id}/specification/generate`,{method:'POST'}),
  updateSpecification:(id:string,specId:string,content:Record<string,any>)=>request<ResearchProject>(`/research/${id}/specification/${specId}`,{method:'PUT',body:JSON.stringify({content})}),
  approveSpecification:(id:string,specId:string)=>request<ResearchProject>(`/research/${id}/specification/${specId}/approve`,{method:'POST'}),
  createResearchImplementation:(id:string,client_id?:string,force:boolean=false)=>request<{session:AgentSession;strategy_name:string;prompt:string}>(`/research/${id}/implementation`,{method:'POST',body:JSON.stringify({client_id:client_id||'default_client',permission_mode:'acceptEdits',force})}),
  researchStrategyPreview:(id:string)=>request<{module:string;name:string;parameter_schema:Record<string,any>;data_requirements:Record<string,any>}>(`/research/${id}/strategy-preview`),
  publishResearchStrategy:(id:string)=>request<Strategy>(`/research/${id}/publish`,{method:'POST'}),
  researchRuns:(id:string)=>request<ResearchRun[]>(`/research/${id}/backtests`),
  createResearchRun:(id:string,data:unknown)=>request<{id:string;status:string;name:string}>(`/research/${id}/backtests`,{method:'POST',body:JSON.stringify(data)}),
  analyzeResearchRun:(id:string,runId:string)=>request<{role:string;content:string;run_id:string}>(`/research/${id}/backtests/${runId}/analyze`,{method:'POST'}),
  repairResearchRun:(id:string,runId:string,client_id?:string)=>request<{session:AgentSession;strategy_name:string;prompt:string}>(`/research/${id}/backtests/${runId}/repair`,{method:'POST',body:JSON.stringify({client_id:client_id||'default_client',permission_mode:'acceptEdits'})}),
  saveResearchConclusion:(id:string,data:{verdict:'SUPPORTED'|'REJECTED'|'INCONCLUSIVE';summary:string;next_step:string})=>request<ResearchProject>(`/research/${id}/conclusion`,{method:'PUT',body:JSON.stringify(data)}),
  archiveResearch:(id:string)=>request<ResearchProject>(`/research/${id}/archive`,{method:'POST'}),
  reopenResearch:(id:string)=>request<ResearchProject>(`/research/${id}/reopen`,{method:'POST'}),
  iterateResearch:(id:string,data:{target:'DISCUSSING'|'SPEC_REVIEW'|'READY_FOR_BACKTEST';reason:string})=>request<ResearchProject>(`/research/${id}/iterate`,{method:'POST',body:JSON.stringify(data)}),
  dataSymbols:(marketType:string)=>request<{symbol:string;base:string;quote:string}[]>(`/data/symbols?market_type=${marketType}`),
  dataCatalogSummary:(params?:Record<string,string|number|undefined|null>)=>{
    const qs=params?new URLSearchParams(Object.entries(params).filter(([_,v])=>v!==undefined&&v!==null&&v!=='').map(([k,v])=>[k,String(v)])).toString():''
    return request<import('./types').CatalogSummary>(`/data/catalog/summary${qs?'?'+qs:''}`)
  },
  deleteCatalogSymbol:(instrumentId:string,interval?:string,catalogPath?:string)=>{
    const qs=new URLSearchParams()
    if(interval)qs.set('interval',interval)
    if(catalogPath)qs.set('catalog_path',catalogPath)
    const qStr=qs.toString()
    return request<{ok:boolean;deleted:boolean}>(`/data/catalog/symbols/${encodeURIComponent(instrumentId)}${qStr?'?'+qStr:''}`,{method:'DELETE'})
  },
  dataDownloads:()=>request<any[]>('/data/downloads'),
  dataDownloadLatest:()=>request<any>('/data/downloads/latest'),
  createDataDownload:(data:unknown)=>request<any>('/data/downloads',{method:'POST',body:JSON.stringify(data)}),
  dataDownload:(id:string)=>request<any>('/data/downloads/'+id),
  deleteDataDownload:(id:string)=>request<{ok:boolean}>('/data/downloads/'+id,{method:'DELETE'}),
  runs:()=>request<Run[]>('/backtests'),run:(id:string)=>request<Run>('/backtests/'+id),chart:(id:string,symbol?:string)=>request<ChartData>(`/backtests/${id}/chart${symbol?'?symbol='+encodeURIComponent(symbol):''}`),createRun:(data:unknown)=>request<Run>('/backtests',{method:'POST',body:JSON.stringify(data)}),deleteRun:(id:string)=>request<void>('/backtests/'+id,{method:'DELETE'}),
  runLogs:(id:string)=>request<import('./types').BacktestLogsResponse>('/backtests/'+id+'/logs'),
  confirmRun:(id:string,data?:{ignore_missing_data?:boolean})=>request<Run>('/backtests/'+id+'/confirm',{method:'POST',body:JSON.stringify(data??{})}),
  cancelRun:(id:string)=>request<Run>('/backtests/'+id+'/cancel',{method:'POST'}),
  checkBacktestCatalog:(data:{symbols:string[];timeframes:string[];start_date:string;end_date:string;venue?:string;catalog_path?:string|null})=>request<import('./types').CatalogCheckResponse>('/backtests/check-catalog',{method:'POST',body:JSON.stringify(data)}),
}


import type {AgentSession,AgentStoredMessage,ChartData,GitConfiguration,LlmConfiguration,PermissionMode,ResearchDecision,ResearchMessage,ResearchProject,ResearchRun,Run,Strategy,StrategyFile,StrategyGitStatus,StrategyVersion} from './types'
export const AUTH_TOKEN_KEY = 'quantlab_token'
export const AUTH_USER_KEY = 'quantlab_user'
export function getApiBaseUrl(): string {
  if (import.meta.env.VITE_API_URL) {
    return import.meta.env.VITE_API_URL
  }
  if (typeof window !== 'undefined' && window.location.hostname) {
    return `${window.location.protocol}//${window.location.hostname}:8000/api`
  }
  return 'http://localhost:8000/api'
}

export const agentSocketUrl = (sessionId: string) => {
  const token = localStorage.getItem(AUTH_TOKEN_KEY)
  const tokenParam = token ? `?token=${encodeURIComponent(token)}` : ''
  const base = getApiBaseUrl()
  if (base.startsWith('http://')) {
    return `${base.replace(/^http:\/\//, 'ws://')}/agent/ws/${sessionId}${tokenParam}`
  }
  if (base.startsWith('https://')) {
    return `${base.replace(/^https:\/\//, 'wss://')}/agent/ws/${sessionId}${tokenParam}`
  }
  const proto = typeof window !== 'undefined' && window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const host = typeof window !== 'undefined' ? window.location.host : 'localhost:8000'
  return `${proto}//${host}${base}/agent/ws/${sessionId}${tokenParam}`
}
function errorText(detail:unknown):string{if(typeof detail==='string')return detail;if(Array.isArray(detail))return detail.map(x=>{if(typeof x==='object'&&x){const e=x as {loc?:unknown[];msg?:string};return `${e.loc?.slice(1).join('.')||'参数'}：${e.msg||'格式错误'}`}return String(x)}).join('；');if(detail&&typeof detail==='object')return JSON.stringify(detail);return '请求失败'}
async function request<T>(path:string,init?:RequestInit):Promise<T>{
  const token = localStorage.getItem(AUTH_TOKEN_KEY)
  const authHeaders: Record<string, string> = {}
  if (token) {
    authHeaders['Authorization'] = `Bearer ${token}`
  }
  const base = getApiBaseUrl()
  const r=await fetch(base+path,{
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
  testDshConfig:()=>request<{ok:boolean;message:string;reasoning?:string}>('/settings/llm/test-dsh',{method:'POST'}),
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
  createResearch:(title:string,original_idea?:string,client_id?:string)=>request<ResearchProject>('/research',{method:'POST',body:JSON.stringify({client_id:client_id||'default_client',title,original_idea:original_idea||''})}),
  researchProject:(id:string)=>request<ResearchProject>('/research/'+id),
  researchMessages:(id:string)=>request<ResearchMessage[]>(`/research/${id}/messages`),
  sendResearchMessage:(id:string,content:string)=>request<ResearchMessage[]>(`/research/${id}/messages`,{method:'POST',body:JSON.stringify({content})}),
  runDshPipeline:(id:string,content:string)=>request<{ok:boolean;strategy_name?:string;final_summary?:string;candidate?:any;review?:any;backtest?:any;robustness?:any}>(`/research/${id}/dsh/run`,{method:'POST',body:JSON.stringify({content})}),
  researchStrategy:(id:string,strategyName?:string)=>{
    const qs=strategyName?`?strategy_name=${encodeURIComponent(strategyName)}`:''
    return request<{ok:boolean;strategy_name?:string;code?:string;error?:string}>(`/research/${id}/strategy${qs}`)
  },
  researchWritingLog:(id:string)=>request<import('./types').ResearchWritingLog>(`/research/${id}/writing-log`),
  researchThinkingStatus:(id:string)=>request<import('./types').ResearchThinkingStatus>(`/research/${id}/thinking-status`),
  researchRuns:(id:string)=>request<ResearchRun[]>(`/research/${id}/backtests`),
  archiveResearch:(id:string)=>request<ResearchProject>(`/research/${id}/archive`,{method:'POST'}),
  reopenResearch:(id:string)=>request<ResearchProject>(`/research/${id}/reopen`,{method:'POST'}),
  deleteResearch:(id:string)=>request<{ok:boolean}>(`/research/${id}`,{method:'DELETE'}),
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
  dashboardStats:()=>request<import('./types').DashboardStats>('/dashboard/stats'),
}


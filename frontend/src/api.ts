import type {ChartData,GitConfiguration,LlmConfiguration,ResearchDecision,ResearchMessage,ResearchProject,ResearchRun,Run,Strategy,StrategyFile,StrategyGitStatus,StrategyVersion} from './types'
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
  testDshConfig:()=>request<{ok:boolean;message:string;reasoning?:string}>('/settings/llm/test-dsh',{method:'POST'}),
  gitConfig:()=>request<GitConfiguration>('/settings/git'),
  saveGitConfig:(data:unknown)=>request<GitConfiguration>('/settings/git',{method:'PUT',body:JSON.stringify(data)}),
  testGitConfig:()=>request<{ok:boolean;message:string}>('/settings/git/test',{method:'POST'}),
  backupGit:()=>request<{ok:boolean;message:string;files_count:number;commit?:string}>('/settings/git/backup',{method:'POST'}),
  researchProjects:(clientId?:string)=>{
    const qs=clientId?`?client_id=${encodeURIComponent(clientId)}`:''
    return request<ResearchProject[]>(`/research${qs}`)
  },
  createResearch:(title:string,original_idea?:string,client_id?:string,source_project_id?:string)=>request<ResearchProject>('/research',{method:'POST',body:JSON.stringify({client_id:client_id||'default_client',title,original_idea:original_idea||'',source_project_id:source_project_id||null})}),
  researchProject:(id:string)=>request<ResearchProject>('/research/'+id),
  researchMessages:(id:string)=>request<ResearchMessage[]>(`/research/${id}/messages`),
  sendResearchMessage:(id:string,content:string)=>request<ResearchMessage[]>(`/research/${id}/messages`,{method:'POST',body:JSON.stringify({content})}),
  runDshPipeline:(id:string,content:string)=>request<{ok:boolean;strategy_name?:string;final_summary?:string;candidate?:any;review?:any;backtest?:any;robustness?:any}>(`/research/${id}/dsh/run`,{method:'POST',body:JSON.stringify({content})}),
  runDshAction:(id:string,data:import('./types').DshActionRequest)=>request<{ok:boolean;kicked_off:boolean;action:import('./types').DshAction;phase:string;message?:string;proposal?:Record<string,any>}>(`/research/${id}/dsh/action`,{method:'POST',body:JSON.stringify(data)}),
  cancelDshPipeline:(id:string)=>request<{ok:boolean;message:string}>(`/research/${id}/dsh/cancel`,{method:'POST'}),
  dshPending:(id:string)=>request<import('./types').DshApproval[]>(`/research/${id}/dsh/pending`),
  dshApprove:(id:string,request_id:string,approved:boolean,feedback?:string)=>request<{ok:boolean;request_id:string;status:string;feedback:string}>(`/research/${id}/dsh/approve`,{method:'POST',body:JSON.stringify({request_id,approved,feedback:feedback||''})}),
  researchStrategy:(id:string,strategyName?:string)=>{
    const qs=strategyName?`?strategy_name=${encodeURIComponent(strategyName)}`:''
    return request<{ok:boolean;strategy_name?:string;code?:string;error?:string}>(`/research/${id}/strategy${qs}`)
  },
  researchWritingLog:(id:string)=>request<import('./types').ResearchWritingLog>(`/research/${id}/writing-log`),
  researchThinkingStatus:(id:string)=>request<import('./types').ResearchThinkingStatus>(`/research/${id}/thinking-status`),
  dshLiveEvents:(id:string)=>request<import('./types').DshLiveEventsResponse>(`/research/${id}/dsh/events`),
  researchRuns:(id:string)=>request<ResearchRun[]>(`/research/${id}/backtests`),
  researchTasks:(id:string)=>request<Array<{id:string;worker_type:'RESEARCH'|'CODING'|'BACKTEST'|'ANALYSIS';task_type:string;status:string;attempt:number;max_attempts:number;session_id?:string;error_code?:string;error_message?:string}>>(`/research/${id}/tasks`),
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
  exportResearchUrl:(id:string,format:'markdown'|'json'='markdown')=>{
    const token = localStorage.getItem(AUTH_TOKEN_KEY)
    const tokenParam = token ? `&token=${encodeURIComponent(token)}` : ''
    const base = getApiBaseUrl()
    return `${base}/research/${id}/export?format=${format}${tokenParam}`
  },
  downloadResearchExport:async(id:string,format:'markdown'|'json'='markdown',filename?:string)=>{
    const token = localStorage.getItem(AUTH_TOKEN_KEY)
    const authHeaders: Record<string, string> = {}
    if (token) {
      authHeaders['Authorization'] = `Bearer ${token}`
    }
    const base = getApiBaseUrl()
    const r = await fetch(`${base}/research/${id}/export?format=${format}`, {
      headers: authHeaders,
    })
    if (!r.ok) {
      throw new Error('导出日志失败')
    }
    const blob = await r.blob()
    const url = window.URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    // Try getting filename from Content-Disposition header
    const disposition = r.headers.get('Content-Disposition')
    let downloadName = filename
    if (!downloadName && disposition && disposition.includes('filename=')) {
      const match = disposition.match(/filename="?([^";]+)"?/)
      if (match && match[1]) {
        downloadName = match[1]
      }
    }
    if (!downloadName) {
      downloadName = `quantlab_research_${id}_${Date.now()}.${format === 'markdown' ? 'md' : 'json'}`
    }
    a.download = downloadName
    document.body.appendChild(a)
    a.click()
    window.URL.revokeObjectURL(url)
    document.body.removeChild(a)
  },
  checkBacktestCatalog:(data:{symbols:string[];timeframes:string[];start_date:string;end_date:string;venue?:string;market_type?:'spot'|'um';catalog_path?:string|null})=>request<import('./types').CatalogCheckResponse>('/backtests/check-catalog',{method:'POST',body:JSON.stringify(data)}),
  dashboardStats:()=>request<import('./types').DashboardStats>('/dashboard/stats'),
}

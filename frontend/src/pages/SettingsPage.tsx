import {useEffect,useState} from 'react'
import {BrainCircuit,CheckCircle2,CloudUpload,GitBranch,KeyRound,PlugZap,Save,UploadCloud} from 'lucide-react'
import {api} from '../api'
import type {GitConfiguration,LlmConfiguration,PermissionMode} from '../types'

export default function SettingsPage(){
  const[config,setConfig]=useState<LlmConfiguration>({configured:false}),[gitConfig,setGitConfig]=useState<GitConfiguration>({configured:false}),[error,setError]=useState(''),[notice,setNotice]=useState(''),[busy,setBusy]=useState(false),[tab,setTab]=useState<'llm'|'git'>('llm')
  useEffect(()=>{Promise.all([api.llmConfig(),api.gitConfig()]).then(([llm,git])=>{setConfig(llm);setGitConfig(git)}).catch(e=>setError(e.message))},[])
  async function save(e:React.FormEvent<HTMLFormElement>){
    e.preventDefault()
    setBusy(true)
    setError('')
    const f=new FormData(e.currentTarget)
    try{
      const result=await api.saveLlmConfig({
        base_url:f.get('base_url'),
        api_key:f.get('api_key')||null,
        auth_type:f.get('auth_type'),
        model:f.get('model'),
        small_fast_model:f.get('small_fast_model')||null,
        timeout_seconds:Number(f.get('timeout_seconds')),
        max_turns:Number(f.get('max_turns')),
        default_permission_mode:f.get('default_permission_mode') as PermissionMode,
        hermes_base_url:f.get('hermes_base_url'),
        hermes_api_key:f.get('hermes_api_key')||null,
        hermes_model:f.get('hermes_model'),
        hermes_timeout_seconds:Number(f.get('hermes_timeout_seconds')),
      })
      setConfig(result)
      setNotice('LLM 及 Hermes 配置已加密保存')
    }catch(e){
      setError((e as Error).message)
    }finally{
      setBusy(false)
    }
  }
  async function test(deep:boolean){
    setBusy(true)
    setError('')
    try{
      const r=await api.testLlmConfig(deep)
      setNotice((deep?'Claude Agent 深度测试：':'Claude 连接测试：')+r.message)
      setConfig(await api.llmConfig())
    }catch(e){
      setError((e as Error).message)
    }finally{
      setBusy(false)
    }
  }
  async function testHermes(){
    setBusy(true)
    setError('')
    try{
      const r=await api.testHermesConfig()
      setNotice('Hermes 研讨连接测试：'+r.message)
      setConfig(await api.llmConfig())
    }catch(e){
      setError((e as Error).message)
    }finally{
      setBusy(false)
    }
  }
  async function saveGit(e:React.FormEvent<HTMLFormElement>){
    e.preventDefault()
    setBusy(true)
    setError('')
    const f=new FormData(e.currentTarget)
    try{
      const result=await api.saveGitConfig({
        remote_url:f.get('remote_url'),
        username:f.get('username'),
        password:f.get('password')||null,
        auto_push:false,
      })
      setGitConfig(result)
      setNotice('远程 Git 备份配置已加密保存')
    }catch(e){
      setError((e as Error).message)
    }finally{
      setBusy(false)
    }
  }
  async function backupGitNow(){
    setBusy(true)
    setError('')
    try{
      const result=await api.backupGit()
      setNotice(result.message)
      setGitConfig(await api.gitConfig())
    }catch(e){
      setError((e as Error).message)
      setGitConfig(await api.gitConfig())
    }finally{
      setBusy(false)
    }
  }
  function switchTab(next:'llm'|'git'){
    setTab(next)
    setNotice('')
    setError('')
  }
  return <div className="settings-shell">
    {error&&<div className="form-error">{error}</div>}
    {notice&&<div className="save-notice"><CheckCircle2/>{notice}</div>}
    <div className="settings-tabs" role="tablist" aria-label="设置分类">
      <button type="button" role="tab" aria-selected={tab==='llm'} className={tab==='llm'?'active':''} onClick={()=>switchTab('llm')}><KeyRound/>LLM 配置</button>
      <button type="button" role="tab" aria-selected={tab==='git'} className={tab==='git'?'active':''} onClick={()=>switchTab('git')}><GitBranch/>策略 Git 远程备份</button>
    </div>
    <section className="card settings-page" role="tabpanel">
      {tab==='llm'?<LlmSettings config={config} busy={busy} save={save} test={test} testHermes={testHermes}/>:<GitSettings config={gitConfig} busy={busy} save={saveGit} backup={backupGitNow}/>}
    </section>
  </div>
}

function LlmSettings({config,busy,save,test,testHermes}:{config:LlmConfiguration;busy:boolean;save:(e:React.FormEvent<HTMLFormElement>)=>void;test:(deep:boolean)=>void;testHermes:()=>void}){
  return <form className="stack-form llm-settings-form" onSubmit={save}>
    <div className="llm-subcard">
      <div className="section-title">
        <div>
          <h3><KeyRound/>Claude Agent 配置</h3>
          <p>用于代码生成、代码重构与策略文件落地。API Key 加密保存且不返回前端。</p>
        </div>
        {config.configured&&<span className={config.last_test_ok?'config-ok':(config.last_test_ok===false?'config-error':'config-pending')}>{config.last_test_ok?'Claude 连接正常':(config.last_test_ok===false?'Claude 连接异常':'Claude 等待测试')}</span>}
      </div>
      <div className="settings-grid">
        <label className="wide">Anthropic API Base URL<input name="base_url" required defaultValue={config.base_url??''} placeholder="https://api.anthropic.com"/></label>
        <label>认证方式<select name="auth_type" defaultValue={config.auth_type??'api_key'}><option value="api_key">x-api-key</option><option value="auth_token">Authorization Bearer</option></select></label>
        <label>API Key<input name="api_key" type="password" placeholder={config.api_key_masked??'请输入 API Key'}/></label>
        <label>模型<input name="model" required defaultValue={config.model??''} placeholder="claude-sonnet-4-6"/></label>
        <label>小模型（可选）<input name="small_fast_model" defaultValue={config.small_fast_model??''}/></label>
        <label>超时（秒）<input name="timeout_seconds" type="number" min="10" max="1800" defaultValue={config.timeout_seconds??120}/></label>
        <label>最大 Agent 轮次<input name="max_turns" type="number" min="1" max="200" defaultValue={config.max_turns??30}/></label>
        <label className="wide">默认权限模式<select name="default_permission_mode" defaultValue={config.default_permission_mode??'default'}><option value="plan">Plan</option><option value="default">审批执行</option><option value="acceptEdits">自动编辑</option><option value="bypassPermissions">完全自动</option></select></label>
      </div>
      <div className="subcard-actions">
        <button type="button" className="button" disabled={busy||!config.configured} onClick={()=>test(false)}><PlugZap/>测试 Claude 连接</button>
        <button type="button" className="button" disabled={busy||!config.configured} onClick={()=>test(true)}><PlugZap/>深度测试 Claude Agent</button>
      </div>
    </div>

    <div className="llm-subcard">
      <div className="section-title">
        <div>
          <h3><BrainCircuit/>Hermes 研究配置</h3>
          <p>用于策略研讨对话、规格生成与回测结果深度分析。由前端统一维护，无需固定在环境变量。</p>
        </div>
        {config.hermes_configured?<span className={config.hermes_last_test_ok?'config-ok':(config.hermes_last_test_ok===false?'config-error':'config-pending')}>{config.hermes_last_test_ok?'Hermes 连接正常':(config.hermes_last_test_ok===false?'Hermes 连接异常':'Hermes 等待测试')}</span>:<span className="config-pending">未配置 Hermes</span>}
      </div>
      <div className="settings-grid">
        <label className="wide">Hermes API Base URL<input name="hermes_base_url" defaultValue={config.hermes_base_url??''} placeholder="http://127.0.0.1:8642/v1"/></label>
        <label>Hermes API Key（可选）<input name="hermes_api_key" type="password" placeholder={config.hermes_api_key_masked??'留空或输入 Bearer Token'}/></label>
        <label>Hermes 模型<input name="hermes_model" defaultValue={config.hermes_model??''} placeholder="hermes-agent"/></label>
        <label className="wide">请求超时（秒）<input name="hermes_timeout_seconds" type="number" min="10" max="3600" defaultValue={config.hermes_timeout_seconds??600}/></label>
      </div>
      <div className="subcard-actions">
        <button type="button" className="button" disabled={busy||!config.hermes_configured} onClick={testHermes}><PlugZap/>测试 Hermes 连接</button>
      </div>
    </div>

    <div className="wide settings-actions-footer">
      <button className="button primary" disabled={busy}><Save/>保存配置</button>
    </div>
  </form>
}

function GitSettings({config,busy,save,backup}:{config:GitConfiguration;busy:boolean;save:(e:React.FormEvent<HTMLFormElement>)=>void;backup:()=>void}){
  return <><div className="section-title"><div><h3><GitBranch/>策略 Git 远程备份</h3><p>策略版本由数据库统一管理。您可以在此配置个人的 GitHub / GitLab 远程仓库，随时将全部策略文件手动备份到云端。</p></div>{config.configured&&<span className={config.last_backup_ok?'config-ok':(config.last_backup_ok===false?'config-error':'config-pending')}>{config.last_backup_ok?'备份连接正常':(config.last_backup_ok===false?'备份遇到异常':'等待首次备份')}</span>}</div>{config.last_backup_at&&<div className="research-status-banner" style={{marginBottom:18,padding:'10px 14px',borderRadius:8,fontSize:13,background:'rgba(255,255,255,0.03)',border:'1px solid rgba(255,255,255,0.08)',display:'flex',justifyContent:'space-between',alignItems:'center'}}><span><b>上次备份时间：</b>{new Date(config.last_backup_at).toLocaleString('zh-CN')}</span><span style={{color:config.last_backup_ok?'#4ade80':'#f87171'}}>{config.last_backup_message||(config.last_backup_ok?'备份成功':'备份失败')}</span></div>}<form className="stack-form settings-grid" onSubmit={save}><label className="wide">远程 HTTPS 地址<input name="remote_url" type="url" required defaultValue={config.remote_url??''} placeholder="https://github.com/your-org/my-strategies.git"/></label><label>Git 账号<input name="username" required defaultValue={config.username??''} placeholder="例如：octocat"/></label><label>密码或个人访问令牌 (Token)<input name="password" type="password" placeholder={config.password_masked??'请输入 Personal Access Token'}/></label><div className="wide settings-actions"><button className="button primary" disabled={busy}><Save/>保存备份配置</button><button type="button" className="button" disabled={busy||!config.configured} onClick={backup}><CloudUpload/>立即备份全部策略到远程</button></div></form></>
}

import {useEffect,useState} from 'react'
import {
  Activity,
  ArrowRight,
  BarChart2,
  CheckCircle2,
  Code2,
  Compass,
  Cpu,
  Database,
  ExternalLink,
  FileSpreadsheet,
  FlaskConical,
  Layers3,
  LineChart,
  PlayCircle,
  Plus,
  RefreshCw,
  Server,
  ShieldCheck,
  Sparkles,
  TrendingUp,
  Workflow,
  Zap,
} from 'lucide-react'
import {Link,useNavigate} from 'react-router-dom'
import {api} from '../api'
import {Status} from '../components'
import type {DashboardStats} from '../types'
import '../dashboard.css'

interface WorkflowStep {
  id: number
  stepNumber: string
  title: string
  role: 'lead' | 'researcher' | 'developer' | 'reviewer' | 'engine'
  roleLabel: string
  shortDesc: string
  fullDesc: string
  inputs: string[]
  actions: string[]
  tools: string[]
  outputs: string[]
  icon: React.ElementType
  linkTo: string
  actionLabel: string
}

const WORKFLOW_STEPS: WorkflowStep[] = [
  {
    id: 0,
    stepNumber: '01',
    title: '需求拆解与假设规划',
    role: 'lead',
    roleLabel: 'Quant Lead 主控',
    shortDesc: '拆解策略想法，定义研究假设、标的周期与参数边界',
    fullDesc: '量化主控根据用户交易想法或市场信号，进行结构化需求拆解，建立量化研究假设，规划标的池、时间周期并调度多 Agent 协同工作。',
    inputs: ['用户交易想法与需求', '目标市场与标的范围', '预期风控目标与回测周期'],
    actions: ['需求结构化解析', '量化假设形式化', '多 Agent 任务分解与派发'],
    tools: ['quant_lead_planner', 'agent_task_dispatch'],
    outputs: ['Research Specification 规格书', '多阶段研究计划'],
    icon: Compass,
    linkTo: '/research',
    actionLabel: '前往策略研究',
  },
  {
    id: 1,
    stepNumber: '02',
    title: '因子挖掘与向量化实验',
    role: 'researcher',
    roleLabel: 'Researcher 研究员',
    shortDesc: '历史数据探索、Alpha 因子计算与 IC 统计检验',
    fullDesc: '研究员基于 Parquet 数据湖中的真实历史行情，进行技术与 Alpha 因子计算、IC / Rank IC 统计显著性检验及向量化快速实验，确定策略有效性。',
    inputs: ['Parquet 行情数据', '因子逻辑与参数定义', '量化假设清单'],
    actions: ['Alpha 因子计算与标准化', 'IC / Rank IC 显著性统计检验', '向量化快速回测实验'],
    tools: ['quant_market_data_query', 'quant_factor_analysis', 'quant_run_experiment'],
    outputs: ['Strategy Candidate 候选规格', '因子统计检验报告'],
    icon: LineChart,
    linkTo: '/research',
    actionLabel: '进入因子研究',
  },
  {
    id: 2,
    stepNumber: '03',
    title: '策略编写与沙盒校验',
    role: 'developer',
    roleLabel: 'Developer 开发者',
    shortDesc: '编写 NautilusTrader 策略代码并通过 4 级 Pre-Flight 沙盒校验',
    fullDesc: '开发者将 Candidate 规格转化为标准的 NautilusTrader 事件驱动策略代码，并在隔离沙盒中通过语法、导入、实例化与运行期计算 4 级确定性校验。',
    inputs: ['Candidate 策略规格', '指标与图表契约定义', '参数规范 ParameterSpec'],
    actions: ['NautilusTrader 策略代码生成', '指标流式计算函数实现', '4 级 Pre-Flight 运行期沙盒校验'],
    tools: ['quant_save_strategy_code', 'preflight_sandbox_verifier'],
    outputs: ['策略源码 (app/strategies/<slug>.py)', '沙盒验证通过凭证'],
    icon: Code2,
    linkTo: '/strategies',
    actionLabel: '查看策略代码',
  },
  {
    id: 3,
    stepNumber: '04',
    title: '独立审查与逻辑审计',
    role: 'reviewer',
    roleLabel: 'Reviewer 审核员',
    shortDesc: '逻辑一致性审查、未来函数排查与过拟合风险审计',
    fullDesc: '审核员以严苛的量化合规标准，独立比对策略代码与研究规格一致性，深度排查未来函数（Lookahead Bias）、参数硬编码与风控漏洞。',
    inputs: ['策略源码', 'Candidate 规格书', 'Pre-Flight 验证结果'],
    actions: ['未来函数与前瞻偏差排查', '过拟合与数据泄露审查', '契约与风控边界一致性校验'],
    tools: ['quant_code_audit', 'logic_consistency_checker'],
    outputs: ['独立审查报告 (APPROVED / REJECTED)', '风险提示与优化建议'],
    icon: ShieldCheck,
    linkTo: '/research',
    actionLabel: '查看审查详情',
  },
  {
    id: 4,
    stepNumber: '05',
    title: '事件驱动回测与稳健性',
    role: 'engine',
    roleLabel: 'Backtest Engine 撮合引擎',
    shortDesc: 'NautilusTrader 真实撮合回测、Walk-Forward 与蒙特卡洛压力测试',
    fullDesc: '调用底层 NautilusTrader 高性能事件驱动撮合引擎，进行微秒级撮合回测，并执行 Walk-Forward 样本外向前推进与 Monte Carlo 蒙特卡洛压力测试。',
    inputs: ['已验证策略版本', '完整历史行情与 OrderBook 数据', '真实手续费与滑点模型'],
    actions: ['高精度事件驱动撮合回测', 'Walk-Forward 样本外向前推进', 'Monte Carlo 随机抽样与极端压力测试'],
    tools: ['quant_execute_backtest', 'quant_robustness_test'],
    outputs: ['高精度收益/回撤/夏普报告', '资产权益曲线与逐笔交易', '稳健性评分与压力测试图'],
    icon: Cpu,
    linkTo: '/backtests',
    actionLabel: '查看回测任务',
  },
  {
    id: 5,
    stepNumber: '06',
    title: '综合决策与报告归档',
    role: 'lead',
    roleLabel: 'Quant Lead 主控',
    shortDesc: '汇总全流程多维成果，生成量化研报并归档发布策略',
    fullDesc: 'Quant Lead 综合 Researcher 因子成果、Developer 沙盒代码、Reviewer 审核结论及引擎正式回测数据，生成专业客观的 Markdown 研报并完成版本发布。',
    inputs: ['因子检验数据', '正式回测绩效指标', '稳健性评估结果', '审核意见'],
    actions: ['多维度量化综合研报生成', '策略版本发布与 Git 提交', '策略入库与监控配置'],
    tools: ['quant_synthesis_report', 'git_version_commit'],
    outputs: ['量化策略全景综合研报', '正式发布策略版本 (StrategyVersion)'],
    icon: FileSpreadsheet,
    linkTo: '/research',
    actionLabel: '浏览研究成果',
  },
]

function formatBytes(bytes: number): string {
  if (!bytes || bytes === 0) return '0 B'
  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return `${(bytes / Math.pow(k, i)).toFixed(1)} ${sizes[i]}`
}

function formatBars(count: number): string {
  if (!count || count === 0) return '0'
  if (count >= 1_000_000) return `${(count / 1_000_000).toFixed(2)} M`
  if (count >= 1_000) return `${(count / 1_000).toFixed(1)} K`
  return count.toLocaleString()
}

export default function Dashboard() {
  const [stats, setStats] = useState<DashboardStats | null>(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState('')
  const [activeStep, setActiveStep] = useState<number>(0)
  const [lastUpdated, setLastUpdated] = useState<string>('')
  const navigate = useNavigate()

  const loadData = async (isManual = false) => {
    if (isManual) setRefreshing(true)
    try {
      const data = await api.dashboardStats()
      setStats(data)
      setError('')
      setLastUpdated(new Date().toLocaleTimeString())
    } catch (err) {
      setError((err as Error).message || '加载仪表盘统计失败')
    } finally {
      setLoading(false)
      if (isManual) setRefreshing(false)
    }
  }

  useEffect(() => {
    loadData()
    const timer = setInterval(() => loadData(), 8000)
    return () => clearInterval(timer)
  }, [])

  const currentStep = WORKFLOW_STEPS[activeStep] ?? WORKFLOW_STEPS[0]
  const isEngineBusy = stats?.system.engine_status === 'BUSY' || (stats?.backtests.running_runs ?? 0) > 0

  return (
    <div className="dashboard-container">
      {/* 1. Header with live status & quick actions */}
      <div className="dashboard-header">
        <div className="dashboard-title-area">
          <div className="dashboard-title-row">
            <h1>量化研究控制台</h1>
            <div className={`system-status-chip ${isEngineBusy ? 'busy' : stats?.system.llm_configured ? '' : 'warn'}`}>
              <span className="status-dot" />
              <span>
                {isEngineBusy
                  ? `回测引擎执行中 (${stats?.backtests.running_runs} 个任务)`
                  : stats?.system.llm_configured
                  ? 'AI 引擎与数据库在线'
                  : 'AI 配置待完善'}
              </span>
            </div>
          </div>
          <p className="dashboard-subtitle">
            基于 NautilusTrader 事件驱动架构与多 Agent 协同研发体系 · 实时监控与流程全景
          </p>
        </div>

        <div className="dashboard-header-actions">
          {lastUpdated && <span className="muted" style={{fontSize: 12}}>更新于 {lastUpdated}</span>}
          <button
            type="button"
            className={`refresh-btn ${refreshing ? 'spinning' : ''}`}
            onClick={() => loadData(true)}
            title="刷新统计数据"
          >
            <RefreshCw size={14} />
            <span>刷新</span>
          </button>
          <Link className="button primary" to="/backtests/new">
            <Plus size={15} />
            <span>新建回测</span>
          </Link>
        </div>
      </div>

      {error && <div className="form-error">统计数据加载异常：{error}</div>}

      {/* 2. KPI Metrics Grid */}
      <div className="dashboard-kpi-grid">
        {/* Strategy Stats Card */}
        <div className="kpi-card cyan" onClick={() => navigate('/strategies')}>
          <div className="kpi-top">
            <div className="kpi-label-group">
              <span className="kpi-label">策略资产总数</span>
            </div>
            <div className="kpi-icon-wrap">
              <Layers3 size={20} />
            </div>
          </div>
          <div className="kpi-value-row">
            <span className="kpi-main-number">
              {loading ? '—' : stats?.strategies.total_strategies ?? 0}
            </span>
            <span className="kpi-badge">
              {stats?.strategies.registered_strategies ?? 0} 个已注册
            </span>
          </div>
          <div className="kpi-footer">
            <div className="kpi-footer-tags">
              {stats?.strategies.categories &&
                Object.entries(stats.strategies.categories).slice(0, 3).map(([cat, count]) => (
                  <span key={cat} className="kpi-tag">{cat}: {count}</span>
                ))}
            </div>
            <span className="kpi-arrow-link">
              管理 <ArrowRight size={12} />
            </span>
          </div>
        </div>

        {/* Backtests Stats Card */}
        <div className="kpi-card green" onClick={() => navigate('/backtests')}>
          <div className="kpi-top">
            <div className="kpi-label-group">
              <span className="kpi-label">回测任务总数</span>
            </div>
            <div className="kpi-icon-wrap">
              <FlaskConical size={20} />
            </div>
          </div>
          <div className="kpi-value-row">
            <span className="kpi-main-number">
              {loading ? '—' : stats?.backtests.total_runs ?? 0}
            </span>
            {(stats?.backtests.running_runs ?? 0) > 0 ? (
              <span className="kpi-badge active-pulse">
                <span className="status-dot" />
                {stats?.backtests.running_runs} 运行中
              </span>
            ) : (
              <span className="kpi-badge">
                {stats?.backtests.completed_runs ?? 0} 个已完成
              </span>
            )}
          </div>
          <div className="kpi-footer">
            <span>
              胜率: {stats?.backtests.win_rate !== null && stats?.backtests.win_rate !== undefined ? `${stats.backtests.win_rate}%` : '—'}
              {' · '}
              均收益: {stats?.backtests.avg_return !== null && stats?.backtests.avg_return !== undefined ? `${stats.backtests.avg_return}%` : '—'}
            </span>
            <span className="kpi-arrow-link">
              任务中心 <ArrowRight size={12} />
            </span>
          </div>
        </div>

        {/* Research Projects Card */}
        <div className="kpi-card purple" onClick={() => navigate('/research')}>
          <div className="kpi-top">
            <div className="kpi-label-group">
              <span className="kpi-label">策略研究项目</span>
            </div>
            <div className="kpi-icon-wrap">
              <Sparkles size={20} />
            </div>
          </div>
          <div className="kpi-value-row">
            <span className="kpi-main-number">
              {loading ? '—' : stats?.research.total_projects ?? 0}
            </span>
            <span className="kpi-badge">
              {stats?.research.active_projects ?? 0} 个进行中
            </span>
          </div>
          <div className="kpi-footer">
            <span>
              已归档成果: {stats?.research.archived_projects ?? 0} 项
            </span>
            <span className="kpi-arrow-link">
              研究工作台 <ArrowRight size={12} />
            </span>
          </div>
        </div>

        {/* Market Data Catalog Card */}
        <div className="kpi-card amber" onClick={() => navigate('/data')}>
          <div className="kpi-top">
            <div className="kpi-label-group">
              <span className="kpi-label">行情数据湖资产</span>
            </div>
            <div className="kpi-icon-wrap">
              <Database size={20} />
            </div>
          </div>
          <div className="kpi-value-row">
            <span className="kpi-main-number">
              {loading ? '—' : stats?.catalog.total_symbols ?? 0}
            </span>
            <span className="kpi-badge">
              {loading ? '—' : `${formatBars(stats?.catalog.total_bars ?? 0)} 根 K线`}
            </span>
          </div>
          <div className="kpi-footer">
            <span>
              占用存储: {loading ? '—' : formatBytes(stats?.catalog.total_size_bytes ?? 0)}
            </span>
            <span className="kpi-arrow-link">
              数据管理 <ArrowRight size={12} />
            </span>
          </div>
        </div>
      </div>

      {/* 3. Research Workflow Flowchart Section */}
      <section className="flowchart-section">
        <div className="flowchart-header">
          <div className="flowchart-title-wrap">
            <h2>
              <Workflow size={20} />
              量化研究全生命周期流程图
            </h2>
            <p>基于 Star-Topology 多 Agent 协同、4 级代码沙盒与 NautilusTrader 事件撮合真实验证体系</p>
          </div>
          <div className="flowchart-actions">
            <Link className="button" to="/research" style={{fontSize: 12}}>
              <Sparkles size={14} />
              开启新策略研究
            </Link>
          </div>
        </div>

        {/* Step Nodes Track */}
        <div className="flowchart-grid">
          {WORKFLOW_STEPS.map((s, idx) => {
            const Icon = s.icon
            const isSelected = activeStep === s.id
            return (
              <div
                key={s.id}
                className={`flowchart-node ${isSelected ? 'active' : ''}`}
                onClick={() => setActiveStep(s.id)}
              >
                <div className="node-header">
                  <span className="node-step-badge">{s.stepNumber}</span>
                  <span className={`node-role-pill ${s.role}`}>{s.roleLabel.split(' ')[0]}</span>
                </div>
                <div className="node-icon-title">
                  <div className="node-icon">
                    <Icon size={16} />
                  </div>
                  <span className="node-title">{s.title}</span>
                </div>
                <p className="node-desc">{s.shortDesc}</p>
                <div className="node-tools-summary">
                  {s.tools.slice(0, 2).map(t => (
                    <span key={t} className="node-tool-tag">{t}</span>
                  ))}
                </div>
                {idx < WORKFLOW_STEPS.length - 1 && <div className="node-connector" />}
              </div>
            )
          })}
        </div>

        {/* Step Details Inspector Drawer */}
        <div className="flowchart-inspector">
          {/* Col 1: Overview & Role */}
          <div className="inspector-section">
            <span className="inspector-label">
              <Activity size={13} />
              当前阶段 · {currentStep.stepNumber}
            </span>
            <div className="inspector-title-row">
              <h3 className="inspector-title">{currentStep.title}</h3>
              <span className={`node-role-pill ${currentStep.role}`}>{currentStep.roleLabel}</span>
            </div>
            <p className="inspector-body">{currentStep.fullDesc}</p>
          </div>

          {/* Col 2: Inputs & Actions */}
          <div className="inspector-section">
            <span className="inspector-label">
              <Zap size={13} />
              核心输入与执行动作
            </span>
            <div className="inspector-chips">
              {currentStep.inputs.map((inp, idx) => (
                <div key={idx} className="inspector-chip">
                  <span style={{color: 'var(--muted)'}}>入:</span> {inp}
                </div>
              ))}
              {currentStep.actions.map((act, idx) => (
                <div key={idx} className="inspector-chip">
                  <span style={{color: 'var(--cyan)'}}>做:</span> {act}
                </div>
              ))}
            </div>
          </div>

          {/* Col 3: Tools & Outputs */}
          <div className="inspector-section">
            <span className="inspector-label">
              <Code2 size={13} />
              确定性工具与产出物
            </span>
            <div className="inspector-chips">
              {currentStep.tools.map((tl, idx) => (
                <div key={idx} className="inspector-chip">
                  <code>{tl}()</code>
                </div>
              ))}
              {currentStep.outputs.map((out, idx) => (
                <div key={idx} className="inspector-chip" style={{borderColor: 'rgba(43,212,134,0.3)'}}>
                  <CheckCircle2 size={12} color="var(--green)" /> {out}
                </div>
              ))}
            </div>
          </div>

          {/* Col 4: Quick Action */}
          <div className="inspector-action-col">
            <Link className="button primary" to={currentStep.linkTo} style={{width: '100%', justifyContent: 'center'}}>
              <span>{currentStep.actionLabel}</span>
              <ArrowRight size={14} />
            </Link>
            <small style={{color: 'var(--muted)', fontSize: 11, textAlign: 'center'}}>
              点击上方阶段卡片可查看对应流程细节
            </small>
          </div>
        </div>
      </section>

      {/* 4. Backtest Status & Recent Runs Monitoring + Quick Actions Matrix */}
      <div className="dashboard-lower-grid">
        {/* Left: Recent Backtests & Status Overview */}
        <div className="recent-backtests-panel">
          <div className="panel-header">
            <h3>
              <FlaskConical size={18} />
              回测运行监控与最新动态
            </h3>
            <Link className="panel-link" to="/backtests">
              查看全部 ({stats?.backtests.total_runs ?? 0}) <ExternalLink size={12} />
            </Link>
          </div>

          {/* Status Distribution Progress Bar */}
          {stats && stats.backtests.total_runs > 0 && (
            <>
              <div className="backtest-distribution-bar">
                {stats.backtests.completed_runs > 0 && (
                  <div
                    className="dist-bar-seg completed"
                    style={{width: `${(stats.backtests.completed_runs / stats.backtests.total_runs) * 100}%`}}
                    title={`已完成: ${stats.backtests.completed_runs}`}
                  />
                )}
                {stats.backtests.running_runs > 0 && (
                  <div
                    className="dist-bar-seg running"
                    style={{width: `${(stats.backtests.running_runs / stats.backtests.total_runs) * 100}%`}}
                    title={`运行中: ${stats.backtests.running_runs}`}
                  />
                )}
                {stats.backtests.failed_runs > 0 && (
                  <div
                    className="dist-bar-seg failed"
                    style={{width: `${(stats.backtests.failed_runs / stats.backtests.total_runs) * 100}%`}}
                    title={`失败: ${stats.backtests.failed_runs}`}
                  />
                )}
                {stats.backtests.canceled_runs > 0 && (
                  <div
                    className="dist-bar-seg canceled"
                    style={{width: `${(stats.backtests.canceled_runs / stats.backtests.total_runs) * 100}%`}}
                    title={`已取消: ${stats.backtests.canceled_runs}`}
                  />
                )}
              </div>

              <div className="dist-legend">
                <span className="dist-legend-item">
                  <span className="dist-legend-dot completed" />
                  已完成: <b>{stats.backtests.completed_runs}</b>
                </span>
                <span className="dist-legend-item">
                  <span className="dist-legend-dot running" />
                  运行中/分析中: <b>{stats.backtests.running_runs}</b>
                </span>
                <span className="dist-legend-item">
                  <span className="dist-legend-dot failed" />
                  执行失败: <b>{stats.backtests.failed_runs}</b>
                </span>
                {stats.backtests.canceled_runs > 0 && (
                  <span className="dist-legend-item">
                    <span className="dist-legend-dot canceled" />
                    已取消: <b>{stats.backtests.canceled_runs}</b>
                  </span>
                )}
                {stats.backtests.avg_sharpe !== null && (
                  <span className="dist-legend-item" style={{marginLeft: 'auto', color: 'var(--cyan)'}}>
                    平均夏普: <b>{stats.backtests.avg_sharpe}</b>
                  </span>
                )}
              </div>
            </>
          )}

          {/* Recent Runs Mini Table */}
          {stats?.backtests.recent_runs && stats.backtests.recent_runs.length > 0 ? (
            <table className="dashboard-mini-table">
              <thead>
                <tr>
                  <th>任务名称 / 标的</th>
                  <th>关联策略</th>
                  <th>状态</th>
                  <th>总收益率</th>
                  <th>Sharpe</th>
                  <th>时间</th>
                </tr>
              </thead>
              <tbody>
                {stats.backtests.recent_runs.map(run => {
                  const ret = run.total_return
                  const pnlClass = ret === null || ret === undefined ? 'neutral' : ret > 0 ? 'positive' : ret < 0 ? 'negative' : 'neutral'
                  return (
                    <tr
                      key={run.id}
                      className="clickable-row"
                      onClick={() => navigate(`/backtests/${run.id}`)}
                    >
                      <td>
                        <div className="run-title-cell">
                          <strong>{run.name}</strong>
                          <small>
                            {run.venue || 'BINANCE'} · {run.symbols.length ? run.symbols.join(', ') : '默认标的'} ({run.timeframes.join('/') || '1h'})
                          </small>
                        </div>
                      </td>
                      <td>
                        <span style={{color: '#bcd0e4'}}>{run.strategy_name || '—'}</span>
                      </td>
                      <td>
                        <Status value={run.status} />
                      </td>
                      <td>
                        <span className={`pnl-text ${pnlClass}`}>
                          {ret !== null && ret !== undefined ? `${ret > 0 ? '+' : ''}${ret}%` : '—'}
                        </span>
                      </td>
                      <td>
                        <span style={{fontFamily: 'monospace', color: '#a6bcd0'}}>
                          {run.sharpe !== null && run.sharpe !== undefined ? run.sharpe : '—'}
                        </span>
                      </td>
                      <td style={{color: 'var(--muted)', fontSize: 11}}>
                        {new Date(run.created_at).toLocaleDateString()} {new Date(run.created_at).toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'})}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          ) : (
            <div style={{padding: '30px 20px', textAlign: 'center', color: 'var(--muted)'}}>
              <FlaskConical size={32} style={{margin: '0 auto 10px', opacity: 0.4}} />
              <p style={{margin: 0, fontSize: 13}}>暂无回测运行记录，点击右上角「新建回测」提交首个任务</p>
            </div>
          )}
        </div>

        {/* Right: Quick Actions & Architecture Matrix */}
        <div className="quick-matrix-panel">
          <div className="quick-actions-card">
            <div className="panel-header" style={{marginBottom: 10}}>
              <h3>
                <PlayCircle size={18} />
                快速导航与操作
              </h3>
            </div>
            <div className="quick-links-grid">
              <Link className="quick-nav-item" to="/backtests/new">
                <div className="quick-nav-left">
                  <div className="quick-nav-icon">
                    <TrendingUp size={16} />
                  </div>
                  <div className="quick-nav-info">
                    <b>新建事件驱动回测</b>
                    <small>选择策略与标的，发起 NautilusTrader 回测</small>
                  </div>
                </div>
                <ArrowRight size={14} className="arrow" />
              </Link>

              <Link className="quick-nav-item" to="/research">
                <div className="quick-nav-left">
                  <div className="quick-nav-icon">
                    <Sparkles size={16} />
                  </div>
                  <div className="quick-nav-info">
                    <b>策略研究工作台</b>
                    <small>多 Agent 对话探索因子与自动生成策略代码</small>
                  </div>
                </div>
                <ArrowRight size={14} className="arrow" />
              </Link>

              <Link className="quick-nav-item" to="/strategies">
                <div className="quick-nav-left">
                  <div className="quick-nav-icon">
                    <Layers3 size={16} />
                  </div>
                  <div className="quick-nav-info">
                    <b>策略代码与版本管理</b>
                    <small>在线编辑策略、查看指标契约与 Git 提交</small>
                  </div>
                </div>
                <ArrowRight size={14} className="arrow" />
              </Link>

              <Link className="quick-nav-item" to="/data">
                <div className="quick-nav-left">
                  <div className="quick-nav-icon">
                    <Database size={16} />
                  </div>
                  <div className="quick-nav-info">
                    <b>Parquet 行情数据湖</b>
                    <small>下载 Binance K 线数据并管理 Catalog</small>
                  </div>
                </div>
                <ArrowRight size={14} className="arrow" />
              </Link>
            </div>
          </div>

          {/* System Architecture Topology Banner */}
          <div className="architecture-banner">
            <span style={{fontSize: 12, fontWeight: 700, color: '#8fa4b8', display: 'flex', alignItems: 'center', gap: 6}}>
              <Server size={14} />
              系统执行拓扑架构
            </span>
            <div className="topology-flow">
              <span className="topo-node">React 前端</span>
              <span className="topo-sep">→</span>
              <span className="topo-node">FastAPI 网关</span>
              <span className="topo-sep">→</span>
              <span className="topo-node">沙盒验证器</span>
              <span className="topo-sep">→</span>
              <span className="topo-node">Nautilus 撮合引擎</span>
            </div>
            <small style={{color: 'var(--muted)', fontSize: 11, lineHeight: 1.5}}>
              全站回测与分析均基于真实事件驱动引擎确定性计算，杜绝未来函数与虚假撮合。
            </small>
          </div>
        </div>
      </div>
    </div>
  )
}

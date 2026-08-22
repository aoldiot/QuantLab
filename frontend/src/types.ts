export type ParameterSpec={title:string;type:string;default:unknown;min?:number;max?:number}
export type DataRequirements={timeframes:string[];primary_timeframe:string;multi_symbol:boolean;mode:'SINGLE_INSTRUMENT'|'PORTFOLIO'}
export type Strategy={id:string;name:string;slug:string;description:string;category:string;status:'DRAFT'|'READY'|'DISABLED';latest_version_id:string;version:string;version_count:number;module:string;created_at:string|null;updated_at:string|null;parameter_schema:Record<string,ParameterSpec>;data_requirements:DataRequirements}
export type StrategyVersion={id:string;strategy_id:string;version:string;description:string;entrypoint:string;code?:string;code_hash?:string|null;parameter_schema:Record<string,ParameterSpec>;data_requirements:DataRequirements;is_latest:boolean;git_commit:string|null;git_ref:string|null;manifest_hash:string|null;created_at:string|null}
export type StrategyFile={name:string;filename:string;module:string;code_hash?:string;git_status?:string;draft_description?:string|null;draft_category?:string|null;created_at?:number;updated_at?:number;content?:string}
export type StrategyGitStatus={dirty:boolean;files:string[];head:string;branch:string}
export type SeriesPoint={timestamp:string;value:number}
export type RunCharts={equity:SeriesPoint[];drawdown:SeriesPoint[];monthly_returns:{year:number;month:number;value:number}[];yearly_returns:{year:number;value:number}[];returns_distribution:{from:number;to:number;count:number}[];rolling_sharpe:SeriesPoint[]}
export type RunResult={equity:number[];drawdown:number[];timestamps:string[];contribution:{symbol:string;value:number}[];stats_pnls:Record<string,unknown>;stats_returns:Record<string,unknown>;native?:Record<string,unknown>;statistics?:Record<string,Record<string,unknown>>;series?:Record<string,SeriesPoint[]>;reports?:Record<string,{rows:number;columns:string[];file:string}>;charts?:RunCharts;funding?:{net_cost:number;settlements:number;snapshot:{enabled?:boolean;rate_per_8h?:number}}}
export type Run={id:string;name:string;status:string;stage:string;progress:number;config:Record<string,any>;metrics:Record<string,number|null>|null;result:RunResult|null;error_message:string|null;research_project_id:string|null;created_at:string}
export type ChartBar={time:number;open:number;high:number;low:number;close:number;volume:number}
export type ChartFill={time:number;price:number;quantity:number;side:string}
export type PlotSeriesSpec={name?:string;type?:'line'|'histogram'|'area'|'baseline';color?:string;lineWidth?:number}
export type PlotConfig={main_plot:Record<string,PlotSeriesSpec>;subplots:Record<string,Record<string,PlotSeriesSpec>>}
export type IndicatorPoint={time:number;value:number}
export type ChartData={symbol:string;symbols:string[];bars:ChartBar[];fills:ChartFill[];truncated:boolean;timeframe?:string;plot_config:PlotConfig;indicator_series:Record<string,IndicatorPoint[]>}
export type PermissionMode='plan'|'default'|'acceptEdits'|'bypassPermissions'
export type LlmConfiguration={configured:boolean;base_url?:string;api_key_masked?:string;auth_type?:'api_key'|'auth_token';model?:string;small_fast_model?:string|null;timeout_seconds?:number;max_turns?:number;default_permission_mode?:PermissionMode;last_test_ok?:boolean|null;last_test_message?:string|null;last_tested_at?:string|null;updated_at?:string}
export type GitConfiguration={configured:boolean;repository_path?:string;remote_url?:string;username?:string;password_masked?:string;auto_push?:boolean;last_backup_at?:string|null;last_backup_ok?:boolean|null;last_backup_message?:string|null;updated_at?:string}
export type ResearchSpecification={id:string;version:number;status:'DRAFT'|'APPROVED'|'SUPERSEDED';content:Record<string,any>;created_at:string;approved_at:string|null}
export type ResearchConclusion={verdict:'SUPPORTED'|'REJECTED'|'INCONCLUSIVE';summary:string;next_step:string}
export type ResearchProject={id:string;client_id:string;title:string;original_idea:string;status:string;research_phase?:'RESEARCH'|'AWAITING_IMPLEMENTATION_APPROVAL'|'IMPLEMENTATION'|'AWAITING_BACKTEST_APPROVAL'|'BACKTEST'|'RESULT_REVIEW'|'ANALYSIS';strategy_id:string|null;implementation_session_id:string|null;latest_backtest_id:string|null;conclusion:ResearchConclusion|null;archived_at:string|null;created_at:string;updated_at:string;specification:ResearchSpecification|null;is_busy?:boolean}
export type ResearchMessage={id:string;role:'user'|'assistant'|'system'|'tool';content:string;message_type:string;metadata:Record<string,any>;created_at:string}
export type ResearchDecision={id:string;question:string;options:string[];recommendation:string|null;impact:string|null;status:'PENDING'|'RESOLVED'|'DISMISSED';answer:string|null;origin:'DISCUSSION'|'SPECIFICATION';source_message_id:string|null;created_at:string;resolved_at:string|null}
export type ResearchRun={id:string;name:string;status:string;stage:string;progress:number;metrics:Record<string,number|null>|null;error_message?:string|null;config?:Record<string,any>|null;created_at:string}
export interface CatalogTimeframeItem{interval:string;spec:string;bar_type:string;bars:number;size_bytes:number;file_count:number;start_time:string|null;end_time:string|null;start_date:string|null;end_date:string|null}
export interface CatalogSymbolItem{symbol:string;instrument_id:string;market_type:string;market_type_label:string;base_currency:string;quote_currency:string;total_bars:number;total_size_bytes:number;file_count:number;start_time:string|null;end_time:string|null;start_date:string|null;end_date:string|null;days_span?:number;timeframes:CatalogTimeframeItem[];updated_at?:string|null}
export interface CatalogCoverageSymbolDetail{symbol:string;instrument_id:string;market_type:string;market_type_label:string;start_date:string|null;end_date:string|null;days_span:number;total_bars:number;total_size_bytes:number;timeframes:string[]}
export interface CatalogCoverageBucket{key:string;label:string;min_days:number;max_days:number|null;desc:string;count:number;percentage:number;total_bars:number;total_size_bytes:number;symbols:string[];symbol_details:CatalogCoverageSymbolDetail[]}
export interface CatalogSummary{catalog_path:string;total_symbols:number;total_bars:number;total_size_bytes:number;all_symbols_count:number;all_bars_count:number;all_size_bytes:number;available_timeframes:string[];coverage_stats?:CatalogCoverageBucket[];page:number;page_size:number;total_pages:number;items:CatalogSymbolItem[]}
export interface CatalogMissingDetail{symbol:string;instrument_id:string;timeframe:string;status:'MISSING_INSTRUMENT'|'MISSING_DATA'|'PARTIAL_RANGE'|'OK';message:string}
export interface CatalogCheckResponse{ok:boolean;has_missing:boolean;catalog_exists:boolean;catalog_path:string;missing_symbols:string[];details:CatalogMissingDetail[];summary_text:string}
export interface BacktestLogsResponse{id:string;status:string;stage:string;progress:number;logs:string;error_message?:string|null}
export interface VerificationStep{level:string;name:string;ok:boolean;message:string;details?:Record<string,unknown>}
export interface ResearchWritingLog{status:'IDLE'|'RUNNING'|'COMPLETED'|'FAILED';stage:string;progress:number;strategy_name:string;logs:string;updated_at?:string;steps?:VerificationStep[]}
export interface ResearchThinkingStatus{status:'IDLE'|'THINKING'|'WAITING_APPROVAL'|'TOOL_RUNNING'|'GENERATING'|'FAILED';step:string;thought:string;updated_at?:string;phase?:string;error?:string;metrics?:{phase?:string;elapsed_ms?:number;tool_call_count?:number;max_step?:number;recovered_empty_response?:boolean}}
export interface DshLiveEvent{seq:number;turn_id:string;received_at:string;type:string;kind?:string;chunk_type?:string;text?:string;reasoning?:string;stream_key?:string;turn?:number;step?:number|Record<string,any>;tool?:{name?:string;arguments?:any;arguments_raw?:string;input?:Record<string,any>;args?:Record<string,any>};result?:any;call_id?:string;status?:string;reason?:string}
export interface DshLiveEventsResponse{events:DshLiveEvent[];status:{project_id:string;status:string;stage:string;progress:number;error:string;updated_at:string}}
export interface DshApproval{request_id:string;project_id:string;tool:string;proposal_key?:string;arguments:Record<string,any>;status:'pending'|'approved'|'declined';feedback:string;created_at:string;summary?:string;message?:string}
export type DshAction='WRITE_STRATEGY'|'GENERATE_BACKTEST_PARAMS'|'RUN_BACKTEST'|'FIX_ERROR'|'ANALYZE_BACKTEST'
export interface DshActionRequest{action:DshAction;content?:string;run_id?:string;arguments?:Record<string,any>}
export interface BacktestCreateParams{name:string;strategy_version_id:string;strategy_parameters:Record<string,unknown>;venue:string;market_type:'spot'|'um';symbols:string[];timeframes:string[];start_date:string;end_date:string;initial_balance:number;leverage:number;execution_model:string;catalog_path?:string|null;chunk_size?:number|null;ignore_missing_data?:boolean;check_data_integrity?:boolean}

export interface DashboardRecentRun {
  id: string
  name: string
  status: string
  stage: string
  progress: number
  strategy_name?: string | null
  venue?: string | null
  timeframes: string[]
  symbols: string[]
  total_return?: number | null
  sharpe?: number | null
  created_at: string
}

export interface DashboardStrategyStats {
  total_strategies: number
  registered_strategies: number
  draft_strategies: number
  categories: Record<string, number>
}

export interface DashboardBacktestStats {
  total_runs: number
  running_runs: number
  completed_runs: number
  failed_runs: number
  canceled_runs: number
  win_rate?: number | null
  avg_return?: number | null
  avg_sharpe?: number | null
  recent_runs: DashboardRecentRun[]
}

export interface DashboardResearchStats {
  total_projects: number
  active_projects: number
  archived_projects: number
}

export interface DashboardCatalogStats {
  total_symbols: number
  total_bars: number
  total_size_bytes: number
  available_timeframes: string[]
}

export interface DashboardSystemStats {
  llm_configured: boolean
  db_ok: boolean
  engine_status: string
}

export interface DashboardStats {
  strategies: DashboardStrategyStats
  backtests: DashboardBacktestStats
  research: DashboardResearchStats
  catalog: DashboardCatalogStats
  system: DashboardSystemStats
}

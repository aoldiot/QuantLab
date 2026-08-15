import {AlertTriangle} from 'lucide-react'
import type {ReactNode} from 'react'
import type {CatalogCheckResponse} from './types'

export function Header({title,subtitle,actions}:{title:string;subtitle?:string;actions?:ReactNode}){return <header><div><h1>{title}</h1>{subtitle&&<p>{subtitle}</p>}</div><div className="actions">{actions}</div></header>}
export function Card({title,children,className=''}:{title?:string;children:ReactNode;className?:string}){return <section className={'card '+className}>{title&&<h3>{title}</h3>}{children}</section>}
export function Status({value}:{value:string}){const map:Record<string,string>={COMPLETED:'已完成',RUNNING:'运行中',ANALYZING:'分析中',QUEUED:'排队中',FAILED:'失败',CANCELED:'已取消',READY:'可回测',DRAFT:'草稿',APPROVED:'已确认',SUPERSEDED:'已替代',DISABLED:'已停用'};return <span className={'status '+value.toLowerCase()}>{map[value]??value}</span>}
export function Metric({label,value,note}:{label:string;value:string;note?:string}){return <Card className="metric"><span>{label}</span><strong>{value}</strong>{note&&<small>{note}</small>}</Card>}
export function ConfirmDialog({open,title,message,confirmLabel='确认',danger=false,busy=false,onCancel,onConfirm}:{open:boolean;title:string;message:string;confirmLabel?:string;danger?:boolean;busy?:boolean;onCancel:()=>void;onConfirm:()=>void}){if(!open)return null;return <div className="modal-backdrop" role="presentation" onMouseDown={event=>{if(event.target===event.currentTarget&&!busy)onCancel()}}><section className="modal confirm-modal" role="dialog" aria-modal="true" aria-labelledby="confirm-title"><h2 id="confirm-title">{title}</h2><p>{message}</p><div className="modal-actions"><button type="button" className="button" disabled={busy} onClick={onCancel}>取消</button><button type="button" className={'button '+(danger?'danger':'primary')} disabled={busy} onClick={onConfirm}>{busy?'处理中…':confirmLabel}</button></div></section></div>}

export function CatalogMissingDialog({open,checkResult,busy=false,onCancel,onConfirm}:{open:boolean;checkResult:CatalogCheckResponse|null;busy?:boolean;onCancel:()=>void;onConfirm:()=>void}){
  if(!open||!checkResult)return null
  const missingDetails=checkResult.details.filter(d=>d.status!=='OK')
  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={e=>{if(e.target===e.currentTarget&&!busy)onCancel()}}>
      <section className="modal catalog-missing-modal" role="dialog" aria-modal="true">
        <div className="catalog-missing-header">
          <div className="warning-icon-badge">
            <AlertTriangle size={24}/>
          </div>
          <div>
            <h2>缺少 Catalog 行情数据</h2>
            <p className="muted">检测到以下 {checkResult.missing_symbols.length} 个标的缺少回测所需的 Catalog 数据（或请求日期范围未覆盖）：</p>
          </div>
        </div>

        <div className="missing-symbols-box">
          <div className="missing-symbols-title">缺失数据详情</div>
          <div className="missing-symbols-list">
            {missingDetails.map((item,idx)=>(
              <div key={`${item.symbol}-${item.timeframe}-${idx}`} className="missing-symbol-item">
                <div className="symbol-tag">
                  <span className="symbol-name">{item.symbol}</span>
                  <span className="timeframe-tag">{item.timeframe}</span>
                </div>
                <div className="missing-reason">
                  <span className={`status-pill ${item.status.toLowerCase()}`}>
                    {item.status==='MISSING_INSTRUMENT'?'未注册/无数据':item.status==='PARTIAL_RANGE'?'日期未覆盖':'缺少周期'}
                  </span>
                  <span className="reason-text">{item.message}</span>
                </div>
              </div>
            ))}
          </div>
        </div>

        <div className="catalog-missing-notice">
          <strong>是否忽略数据问题继续回测？</strong>
          <p>如继续，NautilusTrader 将仅使用已有的行情数据执行回测；如需完整结果，建议先前往【数据管理】补充下载缺失数据。</p>
        </div>

        <div className="modal-actions">
          <button type="button" className="button" disabled={busy} onClick={onCancel}>
            取消 / 去补充数据
          </button>
          <button type="button" className="button warning-proceed-btn" disabled={busy} onClick={onConfirm}>
            {busy?'正在启动…':'忽略缺失并继续回测'}
          </button>
        </div>
      </section>
    </div>
  )
}


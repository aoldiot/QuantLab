import {useRef,type ReactNode} from 'react'

const token=/#[^\n]*|(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')|\b(?:class|def|return|if|else|elif|for|while|in|from|import|as|try|except|raise|with|True|False|None|and|or|not|self|super)\b|\b\d+(?:\.\d+)?\b/g

function Line({text}:{text:string}){
  const parts:ReactNode[]=[];let last=0,index=0
  for(const match of text.matchAll(token)){const start=match.index??0;if(start>last)parts.push(<span key={index++}>{text.slice(last,start)}</span>);const value=match[0],kind=value.startsWith('#')?'comment':value.startsWith('"')||value.startsWith("'")?'string':/^\d/.test(value)?'number':'keyword';parts.push(<span className={'syntax-'+kind} key={index++}>{value}</span>);last=start+value.length}
  if(last<text.length)parts.push(<span key={index++}>{text.slice(last)}</span>)
  return <div>{parts.length?parts:' '}</div>
}

export default function CodeEditor({value,onChange,readOnly=false}:{value:string;onChange:(value:string)=>void;readOnly?:boolean}){
  const highlight=useRef<HTMLDivElement>(null),gutter=useRef<HTMLDivElement>(null)
  function sync(target:HTMLTextAreaElement){if(highlight.current){highlight.current.scrollTop=target.scrollTop;highlight.current.scrollLeft=target.scrollLeft}if(gutter.current)gutter.current.scrollTop=target.scrollTop}
  return <div className="syntax-editor"><div className="line-numbers" ref={gutter}>{value.split('\n').map((_,i)=><div key={i}>{i+1}</div>)}</div><div className="syntax-highlight" ref={highlight}>{value.split('\n').map((line,i)=><Line text={line} key={i}/>)}</div><textarea value={value} readOnly={readOnly} aria-label={readOnly?'只读策略代码':'策略代码'} onChange={e=>onChange(e.target.value)} onScroll={e=>sync(e.currentTarget)} spellCheck={false}/></div>
}

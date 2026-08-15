export const DEFAULT_CATEGORIES=['趋势','套利','动量','均值回归']
export const RESERVED_CATEGORIES=['全部','归档']
const KEY='quantlab.strategy-categories'

export function loadCategories():string[]{
  try{
    const value=JSON.parse(localStorage.getItem(KEY)??'null')
    if(Array.isArray(value))return Array.from(new Set(value.filter(item=>typeof item==='string'&&item.trim()&&!RESERVED_CATEGORIES.includes(item.trim())).map(item=>item.trim())))
  }catch{/** Ignore invalid legacy browser data. */}
  return DEFAULT_CATEGORIES
}

export function saveCategories(categories:string[]):string[]{
  const value=Array.from(new Set(categories.map(item=>item.trim()).filter(item=>item&&!RESERVED_CATEGORIES.includes(item))))
  localStorage.setItem(KEY,JSON.stringify(value))
  return value
}

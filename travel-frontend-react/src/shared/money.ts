/** 金额的币种由服务端快照给出；不在展示层猜汇率或合并异币。 */
export function formatMoney(amount: number, currency = 'CNY'): string {
  return new Intl.NumberFormat('zh-CN', { style: 'currency', currency, currencyDisplay: 'code', maximumFractionDigits: 2 }).format(amount)
}

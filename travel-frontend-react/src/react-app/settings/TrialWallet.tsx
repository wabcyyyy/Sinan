import { useEffect, useState } from 'react'
import { getTrialWallet } from '../../api/sinan'
import type * as Contracts from '../../types/generated/contracts'
import { isDemoMode } from '../../demoMode'

export function TrialWallet() {
  const [wallet, setWallet] = useState<Contracts.TrialWalletVO | null>(null)
  const [error, setError] = useState('')
  useEffect(() => { getTrialWallet().then(setWallet).catch((err: unknown) => setError(err instanceof Error ? err.message : '余额读取失败')) }, [])
  if (isDemoMode()) return <section className="trial-panel" aria-label="试用额度"><h2>试用额度</h2><p>产品展示使用固定样例，无真实账号或余额，不消耗模型额度。</p></section>
  return <section className="trial-panel" aria-label="试用额度">
    <h2>试用额度</h2>
    {error && <p role="alert">{error}</p>}
    {wallet && <><p>可用 ¥{wallet.balanceCny.toFixed(4)} · 已消耗 ¥{wallet.spentCny.toFixed(4)}</p>
      <p>注册一次性赠送 6 元，不自动补充。澄清、生成、编辑按模型用量与平台配置单价消耗额度；自带网关使用自己的额度。未知用量会暂时冻结预留额，管理员核实后处理。</p>
      <a href="/admin">试用管理（仅管理员）</a></>}
  </section>
}

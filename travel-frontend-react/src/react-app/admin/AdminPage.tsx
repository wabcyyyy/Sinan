import { useEffect, useState } from 'react'
import { getUserInfo, getTrialUsers, getUserWallet, issueTrialInvites, grantTrialCredit, setTrialUserStatus, reconcileTrialCharge } from '../../api/sinan'
import type * as Contracts from '../../types/generated/contracts'

export function AdminPage() {
  const [users, setUsers] = useState<Contracts.TrialUsersVO | null>(null)
  const [wallet, setWallet] = useState<Contracts.TrialWalletVO | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [codes, setCodes] = useState<string[]>([])
  const [page, setPage] = useState(1)
  const [amount, setAmount] = useState('6')
  const [reason, setReason] = useState('')
  const [grantId, setGrantId] = useState(() => crypto.randomUUID())
  const [isAdmin, setIsAdmin] = useState(false)
  useEffect(() => {
    let alive = true
    getUserInfo().then(async (user) => {
      if (user.role !== 'admin') throw new Error('此页面仅管理员可访问')
      if (alive) setIsAdmin(true)
      const list = await getTrialUsers(page)
      if (alive) setUsers(list)
    }).catch((err: unknown) => { if (alive) setError(err instanceof Error ? err.message : '加载失败') })
    return () => { alive = false }
  }, [page])
  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true); setError('')
    try { await fn() } catch (err) { setError(err instanceof Error ? err.message : '操作失败') }
    finally { setBusy(false) }
  }
  const selectUser = (id: number) => act(async () => { setWallet(null); setWallet(await getUserWallet(id)) })
  return <div className="page-content trial-admin">
    <h1>试用管理</h1>
    {error && <p role="alert">{error}</p>}
    {isAdmin && <>
      <section className="trial-panel"><h2>邀请码</h2>
        <button className="button" disabled={busy} onClick={() => act(async () => setCodes((await issueTrialInvites({ count: 1 })).codes))}>生成一次性邀请码</button>
        <p>明码仅本次显示，请复制后私下发给试用者。</p><output>{codes.join('\n')}</output>
      </section>
      <section className="trial-panel"><h2>用户</h2>
        <table><thead><tr><th>账号</th><th>状态</th><th>操作</th></tr></thead><tbody>
          {users?.records.map((user) => <tr key={user.id}><td>{user.username}</td><td>{user.status === 0 ? '已禁用' : '正常'}</td><td>
            <button className="button" disabled={busy} onClick={() => selectUser(user.id)}>查看余额与费用</button>
            <button className="button" disabled={busy || user.role === 'admin'} onClick={() => act(async () => {
              await setTrialUserStatus(user.id, user.status === 0 ? 1 : 0); setUsers(await getTrialUsers(page))
            })}>{user.status === 0 ? '启用' : '禁用'}</button>
          </td></tr>)}
        </tbody></table>
        <button className="button" disabled={busy || page === 1} onClick={() => setPage(page - 1)}>上一页</button>
        <button className="button" disabled={busy || page * 20 >= (users?.total ?? 0)} onClick={() => setPage(page + 1)}>下一页</button>
      </section>
      {wallet && <section className="trial-panel"><h2>用户 #{wallet.userId} 的额度</h2>
        <p>可用 ¥{wallet.balanceCny.toFixed(4)} · 已消耗 ¥{wallet.spentCny.toFixed(4)}</p>
        <label>赠额（元）<input type="number" min="0.01" max="100" step="0.01" value={amount} onChange={(e) => setAmount(e.target.value)} /></label>
        <label>操作原因<input value={reason} maxLength={200} onChange={(e) => setReason(e.target.value)} /></label>
        <button className="button" disabled={busy || !reason.trim() || !(Number(amount) > 0)} onClick={() => act(async () => {
          setWallet(await grantTrialCredit(wallet.userId, { amountCny: Number(amount), reason: reason.trim(), requestId: grantId }))
          setGrantId(crypto.randomUUID())
          setReason('')
        })}>确认赠额</button>
        <p>费用按配置单价计算，非供应商发票。reserved/unknown 为已冻结且尚未结算；仅在核实供应商记录后对账，填入该次实际消耗。</p>
        <table><thead><tr><th>时间 / 模型</th><th>状态</th><th>消耗 / 冻结</th><th>操作</th></tr></thead><tbody>
          {wallet.entries.map((entry) => <tr key={entry.id}><td>{entry.createdAt}<br />{entry.model || '赠额'}<details><summary>用量 / 操作记录</summary><pre>{JSON.stringify(entry.detail, null, 2)}</pre></details></td><td>{entry.status}</td><td>¥{entry.amountCny.toFixed(6)} / ¥{entry.reservedCny.toFixed(6)}</td><td>
            {entry.kind === 'call' && entry.status === 'unknown' && <button className="button" disabled={busy || !reason.trim()} onClick={() => act(async () => {
              await reconcileTrialCharge(entry.id, { amountCny: Number(amount), reason: reason.trim() }); setWallet(await getUserWallet(wallet.userId))
            })}>按上方金额与原因对账</button>}
          </td></tr>)}
        </tbody></table>
      </section>}
    </>}
  </div>
}

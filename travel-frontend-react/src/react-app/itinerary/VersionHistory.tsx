import { useState } from 'react'
import { listItineraryVersions, restoreItineraryVersion } from '../../api/sinan'
import type * as Contracts from '../../types/generated/contracts'
import type { ItineraryDetail } from '../../types/itinerary'

export function VersionHistory({ trip, disabled, onRestored }: { trip: ItineraryDetail; disabled: boolean; onRestored: (trip: ItineraryDetail) => void }) {
  const [versions, setVersions] = useState<Contracts.ItineraryVersionVO[] | null>(null)
  const [selected, setSelected] = useState<Contracts.ItineraryVersionVO | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const load = async () => {
    setBusy(true); setError(''); setSelected(null)
    try { setVersions(await listItineraryVersions(trip.id)) }
    catch (err) { setError(err instanceof Error ? err.message : '版本读取失败') }
    finally { setBusy(false) }
  }
  const restore = async () => {
    if (!selected || trip.planningRevision === undefined) return
    setBusy(true); setError('')
    try { onRestored(await restoreItineraryVersion(trip.id, selected.id, trip.planningRevision)); setSelected(null); setVersions(null) }
    catch (err) { setError(err instanceof Error ? err.message : '恢复失败') }
    finally { setBusy(false) }
  }
  return <section className="scenario-panel">
    <button className="button button-secondary" type="button" disabled={busy || disabled} onClick={() => versions ? setVersions(null) : void load()} aria-expanded={Boolean(versions)}>查看与恢复历史版本</button>
    {error && <p role="alert">{error}</p>}
    {versions && <div className="scenario-content"><h3>改动可以回头</h3><p>恢复会替换整个行程，恢复前的当前版本会自动保存。历史版本不代表营业或预约信息仍有效。</p>
      {!versions.length && <p>还没有历史版本；首次确认修改时会保存修改前后的版本。</p>}
      <ol className="version-list">{versions.map(version => <li key={version.id}><span>V{version.versionNo} · {version.summary || version.operation}<small>{version.createdAt?.replace('T', ' ')}</small></span><button className="button button-secondary" type="button" disabled={busy || disabled} onClick={() => setSelected(version)}>选择恢复</button></li>)}</ol>
      {selected && <div role="alert" className="scenario-hint"><p>确认恢复到 V{selected.versionNo}「{selected.summary}」？当前尚未应用的草稿将失效。</p><button className="button button-primary" type="button" disabled={busy || disabled || trip.planningRevision === undefined} onClick={() => void restore()}>{busy ? '恢复中…' : '确认恢复此版本'}</button><button className="button button-secondary" type="button" disabled={busy} onClick={() => setSelected(null)}>取消</button></div>}
    </div>}
  </section>
}

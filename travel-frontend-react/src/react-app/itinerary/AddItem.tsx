import { useState } from 'react'
import { createUserItem } from '../../api/sinan'
import type { ItineraryDetail } from '../../types/itinerary'
import { emptyArrangement, UserArrangementForm } from '../shared/UserArrangementForm'

export function AddItem({ trip, dayNo, disabled, onChanged }: { trip: ItineraryDetail; dayNo: number; disabled: boolean; onChanged: (trip: ItineraryDetail) => void }) {
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  return <section className="item-actions scenario-content" aria-label="添加自己的安排">
    {!open ? <button type="button" className="button button-secondary" disabled={disabled} onClick={() => setOpen(true)}>添加自己的安排</button> : <>
      <h3>把自己的安排加到这一天</h3>
      <UserArrangementForm initial={emptyArrangement(dayNo)} currency={trip.budgetCurrency || 'CNY'} days={trip.dayList.map(d => d.dayNo)} busy={busy || disabled} create onCancel={() => { setOpen(false); setError('') }} onSave={async value => {
        setBusy(true); setError('')
        try { onChanged(await createUserItem(trip.id, { ...value, action: 'create', planningRevision: trip.planningRevision!, latitude: null, longitude: null })); setOpen(false) }
        catch (err) { setError(err instanceof Error ? err.message : '添加失败，请重试') }
        finally { setBusy(false) }
      }} />
    </>}
    {error && <p role="alert">{error}</p>}
  </section>
}

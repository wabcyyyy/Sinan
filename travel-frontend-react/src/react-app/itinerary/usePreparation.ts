import { useCallback, useEffect, useRef, useState } from 'react'
import { getPreparation, putPreparation } from '../../api/sinan'
import type { PreparationData, PreparationVO } from '../../types/generated/contracts'

export const emptyPreparation = (): PreparationData => ({ places: [], bookings: [], tasks: [], confirmations: [], completedItemIds: [] })

export function usePreparation(id: number, enabled: boolean) {
  const [state, setState] = useState<PreparationVO | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const writing = useRef(false)
  const sequence = useRef(0)
  const reload = useCallback(async () => {
    const seq = ++sequence.current
    if (!enabled) { setState(null); return }
    setError('')
    try { const next = await getPreparation(id); if (seq === sequence.current) setState(next) }
    catch (err) { if (seq === sequence.current) setError(err instanceof Error ? err.message : '旅行资料读取失败') }
  }, [id, enabled])
  useEffect(() => { setState(null); void reload(); return () => { sequence.current++ } }, [reload])
  const save = async (data: PreparationData) => {
    if (!state || !enabled || writing.current) return false
    const seq = sequence.current
    writing.current = true; setBusy(true); setError('')
    try {
      const next = await putPreparation(id, { revision: state.revision, data })
      if (seq === sequence.current) setState(next)
      return seq === sequence.current
    } catch (err) { if (seq === sequence.current) setError(err instanceof Error ? err.message : '保存失败'); return false }
    finally { writing.current = false; setBusy(false) }
  }
  return { state, data: state?.data ?? emptyPreparation(), busy, error, reload, save }
}

export type PreparationController = ReturnType<typeof usePreparation>

export function externalLink(value: string | null | undefined) {
  try { const url = new URL(value || ''); return ['https:', 'http:'].includes(url.protocol) ? url.href : undefined } catch { return undefined }
}

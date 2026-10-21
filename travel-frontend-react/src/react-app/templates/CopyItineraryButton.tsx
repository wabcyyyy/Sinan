import { useRef, useState } from 'react'
import { copyPublicItinerary, copySharedItinerary, isUnauthorized } from '../../api/sinan'
import { loginRedirect, navigate } from '../router'
import { Icon } from '../shared/Icon'

export function CopyItineraryButton({ templateId, shareToken, signedIn = false }: { templateId?: number; shareToken?: string; signedIn?: boolean }) {
  const lock = useRef(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const copy = async () => {
    if (lock.current) return
    if (!signedIn) { navigate(loginRedirect()); return }
    lock.current = true
    setBusy(true)
    setError('')
    try {
      const result = shareToken ? await copySharedItinerary(shareToken) : await copyPublicItinerary(templateId!)
      navigate(`/trips/${result.itineraryId}`)
    } catch (err) {
      if (isUnauthorized(err)) navigate(loginRedirect())
      else setError(err instanceof Error ? err.message : '复制未完成，请稍后重试')
    } finally { lock.current = false; setBusy(false) }
  }
  return <div className="public-copy"><button className="button button-primary" type="button" disabled={busy} onClick={() => void copy()}>{busy ? '正在复制…' : signedIn ? '复制到我的行程' : '登录后复制微调'}<Icon name="arrow" size={16} /></button>{error && <p role="alert">{error}</p>}</div>
}

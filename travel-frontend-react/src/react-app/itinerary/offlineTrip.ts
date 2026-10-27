import type { PreparationData } from '../../types/generated/contracts'
import type { ItineraryDetail } from '../../types/itinerary'
import { isDemoMode } from '../../demoMode'

const DB_NAME = 'sinan-offline-trips-v1'
const STORE = 'packs'
const OWNER_KEY = 'sinan-offline-owner-v1'
const MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000

export interface OfflineOwner { id: number; username: string }
export interface OfflineItem {
  id?: number; poiName: string; itemType: string; startTime?: string | null; endTime?: string | null
  address?: string | null; remark?: string | null; latitude?: number | null; longitude?: number | null
}
export interface OfflineTrip {
  key: string; ownerId: number; ownerName: string; tripId: number; title: string; city: string
  revision: number; savedAt: string; expiresAt: string; startDate?: string | null
  timeZone?: string
  days: Array<{ dayNo: number; travelDate?: string | null; theme?: string | null; items: OfflineItem[] }>
  bookings: PreparationData['bookings']; tasks: PreparationData['tasks']; confirmations: PreparationData['confirmations']
  completedItemIds: number[]
}

export function setOfflineOwner(owner: OfflineOwner) {
  if (!isDemoMode()) localStorage.setItem(OWNER_KEY, JSON.stringify(owner))
}

export function getOfflineOwner(): OfflineOwner | null {
  if (isDemoMode()) return null
  try {
    const owner = JSON.parse(localStorage.getItem(OWNER_KEY) || 'null') as OfflineOwner | null
    return owner && Number.isInteger(owner.id) && owner.id > 0 && typeof owner.username === 'string' ? owner : null
  } catch { return null }
}

export function clearOfflineOwner() { localStorage.removeItem(OWNER_KEY) }

function database(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1)
    req.onupgradeneeded = () => { req.result.createObjectStore(STORE, { keyPath: 'key' }) }
    req.onsuccess = () => resolve(req.result)
    req.onerror = () => reject(req.error || new Error('本地存储不可用'))
  })
}

async function transact<T>(mode: IDBTransactionMode, run: (store: IDBObjectStore, done: (value: T) => void) => void): Promise<T> {
  const db = await database()
  return new Promise((resolve, reject) => {
    const transaction = db.transaction(STORE, mode)
    let result: T
    transaction.onerror = () => reject(transaction.error || new Error('本地存储失败'))
    transaction.oncomplete = () => { db.close(); resolve(result) }
    run(transaction.objectStore(STORE), value => { result = value })
  })
}

export async function readOfflineTrip(ownerId: number, tripId: number): Promise<OfflineTrip | null> {
  return transact('readonly', (store, done) => { const req = store.get(`${ownerId}:${tripId}`); req.onsuccess = () => done(req.result || null) })
}

export async function listOfflineTrips(ownerId: number): Promise<OfflineTrip[]> {
  return transact('readonly', (store, done) => { const req = store.getAll(); req.onsuccess = () => done((req.result as OfflineTrip[]).filter(p => p.ownerId === ownerId)) })
}

export async function removeOfflineTrip(ownerId: number, tripId: number) {
  await transact<void>('readwrite', (store, done) => { store.delete(`${ownerId}:${tripId}`); done() })
}

export function expired(pack: OfflineTrip) { return Date.now() > Date.parse(pack.expiresAt) }

export async function saveOfflineTrip(trip: ItineraryDetail, data: PreparationData): Promise<OfflineTrip> {
  const owner = getOfflineOwner()
  if (!owner || isDemoMode() || trip.id <= 0 || trip.status !== 2 || trip.planningRevision === undefined) throw new Error('请登录并打开已保存的真实行程')
  const now = new Date()
  const pack: OfflineTrip = {
    key: `${owner.id}:${trip.id}`, ownerId: owner.id, ownerName: owner.username, tripId: trip.id,
    title: trip.title, city: trip.city, revision: trip.planningRevision, savedAt: now.toISOString(),
    timeZone: trip.dayList[0]?.constraintPlanning?.destination.timeZone || 'Asia/Shanghai',
    expiresAt: new Date(now.getTime() + MAX_AGE_MS).toISOString(), startDate: trip.startDate,
    days: trip.dayList.map(day => ({ dayNo: day.dayNo, travelDate: day.travelDate, theme: day.theme,
      items: day.items.map(item => ({ id: item.id, poiName: item.poiName, itemType: item.itemType, startTime: item.startTime,
        endTime: item.endTime, address: item.address, remark: item.remark, latitude: item.latitude, longitude: item.longitude })) })),
    bookings: structuredClone(data.bookings), tasks: structuredClone(data.tasks),
    confirmations: structuredClone(data.confirmations), completedItemIds: [...data.completedItemIds],
  }
  await cacheOfflineShell()
  await transact<void>('readwrite', (store, done) => { store.put(pack); done() })
  return pack
}

async function cacheOfflineShell() {
  if (!('serviceWorker' in navigator)) throw new Error('当前浏览器不支持离线页面')
  const registration = await navigator.serviceWorker.register('/sw.js', { scope: '/' })
  await navigator.serviceWorker.ready
  const worker = registration.active
  if (!worker) throw new Error('离线页面准备失败，请刷新后重试')
  const assets = [...new Set(performance.getEntriesByType('resource').map(entry => entry.name)
    .filter(url => { try { const parsed = new URL(url); return parsed.origin === location.origin && ['/assets/', '/src/', '/node_modules/', '/@vite/'].some(prefix => parsed.pathname.startsWith(prefix)) } catch { return false } })
    .map(url => new URL(url).pathname + new URL(url).search))]
  await new Promise<void>((resolve, reject) => {
    const channel = new MessageChannel()
    const timer = setTimeout(() => reject(new Error('离线资源保存超时，请重试')), 15000)
    channel.port1.onmessage = event => { clearTimeout(timer); event.data?.ok ? resolve() : reject(new Error(event.data?.error || '离线页面保存失败')) }
    worker.postMessage({ type: 'CACHE_OFFLINE_SHELL', assets }, [channel.port2])
  })
}

// @vitest-environment happy-dom
// mantis: Regression coverage for first-pull recovery in the preview build.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../lib/api.js', () => ({ api: vi.fn(), setRemoteAuth: vi.fn() }))
vi.mock('./useUI.js', () => ({ useUI: { getState: () => ({ toast: vi.fn(), stopRest: vi.fn(), abandonWork: vi.fn() }) } }))

const entries = new Map()
const storage = {
  getItem: key => entries.get(String(key)) ?? null,
  setItem: (key, value) => entries.set(String(key), String(value)),
  removeItem: key => entries.delete(String(key)),
  clear: () => entries.clear(),
}
const clone = value => JSON.parse(JSON.stringify(value))
const user = { id: '1', name: 'Timo' }
const sync = { status: 'ok', offline: false, pending: false, auth: false, lastError: null, lastSynced: 0 }
let api
let DEF
let useStore

beforeEach(async () => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-10-17T10:00:00Z'))
  vi.stubEnv('VITE_MANTIS_OG', '1')
  vi.stubGlobal('localStorage', storage)
  localStorage.clear()
  vi.resetModules()
  ;({ api } = await import('../lib/api.js'))
  ;({ DEF, useStore } = await import('./useStore.js'))
  api.mockReset()
  useStore.setState({ S: clone(DEF), user, ready: true, config: { coach: false }, sync: { ...sync } })
})

afterEach(() => {
  vi.clearAllTimers()
  vi.useRealTimers()
  localStorage.clear()
  vi.unstubAllGlobals()
  vi.unstubAllEnvs()
})

describe('Mantis first-pull recovery', () => {
  it('loads the server snapshot on the next poll after the initial GET returns 503', async () => {
    api.mockRejectedValueOnce(Object.assign(new Error('server unavailable'), { status: 503 }))
    await useStore.getState().pullState()
    expect(useStore.getState().sync.lastError).not.toBeNull()

    const remote = { ...clone(DEF), workouts: [{ id: 'w-7', name: 'Recovered workout', entries: [] }] }
    api.mockResolvedValueOnce({ state: remote, rev: 4, wid: 'r4' })
    await vi.advanceTimersByTimeAsync(30_000)

    expect(api.mock.calls.filter(([path]) => path === '/api/data')).toHaveLength(2)
    expect(useStore.getState().S.workouts).toEqual(remote.workouts)
    expect(useStore.getState().sync.lastError).toBeNull()
  })
})

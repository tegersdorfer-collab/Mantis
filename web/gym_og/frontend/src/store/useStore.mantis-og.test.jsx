// @vitest-environment happy-dom
// mantis: Regression coverage for the token-backed read-only preview contract.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../lib/api.js', () => ({ api: vi.fn(), setRemoteAuth: vi.fn() }))
vi.mock('./useUI.js', () => ({ useUI: { getState: () => ({ toast: vi.fn(), stopRest: vi.fn(), abandonWork: vi.fn() }) } }))

vi.stubEnv('VITE_MANTIS_OG', '1')
const { api } = await import('../lib/api.js')
const { DEF, useStore } = await import('./useStore.js')

const clone = value => JSON.parse(JSON.stringify(value))
const USER = { id: 'mantis-user', name: 'Timo' }
const sync = { status: 'ok', offline: false, pending: false, auth: false, lastError: null, lastSynced: 0 }
const pause = () => new Promise(resolve => setTimeout(resolve, 10))
const entries = new Map()
const storage = {
  getItem: key => entries.get(String(key)) ?? null,
  setItem: (key, value) => entries.set(String(key), String(value)),
  removeItem: key => entries.delete(String(key)),
  clear: () => entries.clear(),
}

beforeEach(() => {
  vi.stubGlobal('localStorage', storage)
  localStorage.clear()
  api.mockReset()
  useStore.setState({ S: clone(DEF), user: USER, ready: true, config: { coach: false }, sync: { ...sync } })
})

afterEach(() => {
  vi.useRealTimers()
  localStorage.clear()
  useStore.setState({ S: clone(DEF), user: null, ready: false, config: null, sync: { ...sync } })
  vi.unstubAllGlobals()
})

describe('Mantis read-only preview', () => {
  it('replaces the whole local state with the server snapshot, including local edit markers', async () => {
    const local = {
      ...clone(DEF), restSec: 30, _ts: 500,
      workouts: [{ id: 'w-1', name: 'Local renamed', entries: [{ id: 'm-1', sets: [{ w: 40, r: 8 }] }] }, { id: 'w-deleted' }],
      routines: [{ id: 'r-1', name: 'Local routine', ex: [] }],
      customEx: [{ id: 'm-1', n: 'Local exercise' }],
      edited: { restSec: 400, routines: { 'r-1': 450 } },
      deleted: [{ type: 'workouts', id: 'w-deleted', at: 475 }],
    }
    const remote = {
      restSec: 90,
      workouts: [{ id: 'w-1', name: 'Server workout', entries: [{ id: 'm-1', sets: [{ w: 50, r: 8 }] }] }],
      routines: [{ id: 'r-1', name: 'Server routine', ex: [] }],
      customEx: [{ id: 'm-1', n: 'Server exercise' }],
      bodyweight: [], dayPlan: {}, week: {}, unit: 'kg',
    }
    useStore.setState({ S: local, readOnly: true, sync: { ...sync, pending: true } })
    localStorage.setItem('gym_sync', JSON.stringify({ rev: 1, ts: 400, wid: 'old' }))
    localStorage.setItem('gym_dirty', '1')
    api.mockResolvedValueOnce({ state: remote, rev: 2, wid: 'new' })

    await useStore.getState().pullState()

    const { S } = useStore.getState()
    expect(S).toMatchObject({
      restSec: 90,
      workouts: [{ id: 'w-1', name: 'Server workout', entries: [{ sets: [{ w: 50 }] }] }],
      routines: [{ id: 'r-1', name: 'Server routine' }],
      customEx: [{ id: 'm-1', n: 'Server exercise' }],
    })
    expect(S.workouts).toHaveLength(1)
    expect(S).not.toHaveProperty('edited')
    expect(S).not.toHaveProperty('deleted')
    expect(localStorage.getItem('gym_dirty')).toBeNull()
    expect(useStore.getState().sync.pending).toBe(false)
    expect(useStore.getState().S.restSec).toBe(90)
    expect(api.mock.calls.map(([path, options]) => [path, options?.method || 'GET'])).toEqual([['/api/data', 'GET']])
  })

  it('discards a stale stash after reload when the API user ID is numeric', async () => {
    const stale = {
      ...clone(DEF), restSec: 30,
      workouts: [{ id: 'w-2', name: 'Deleted on server', entries: [] }],
    }
    const remote = {
      ...clone(DEF), restSec: 90, workouts: [], routines: [], customEx: [],
      bodyweight: [], dayPlan: {}, week: {}, unit: 'kg',
    }
    useStore.setState({ S: stale, user: null, ready: false, config: null, sync: { ...sync } })
    localStorage.setItem('gym_owner', '1')
    localStorage.setItem('gym_state_v1', JSON.stringify(stale))
    localStorage.setItem('gym_stash', JSON.stringify({
      'http://localhost:3000|1': {
        server: 'http://localhost:3000', uid: '1', name: 'Timo', at: 1, state: stale,
      },
    }))
    api.mockImplementation(async (path, options = {}) => {
      if (path === '/api/config') return { allow_guest: false, coach: false }
      if (path === '/api/me') return { user: { id: 1, name: 'Timo', admin: false } }
      if (path === '/api/data' && !options.method) return { state: remote, rev: 3, wid: 'r3' }
      if (path === '/api/data/rev') return { rev: 3, wid: 'r3' }
      throw new Error(`unexpected ${options.method || 'GET'} ${path}`)
    })

    await useStore.getState().boot()
    window.dispatchEvent(new Event('online'))
    await pause()

    expect(useStore.getState().user.id).toBe('1')
    expect(useStore.getState().S.restSec).toBe(90)
    expect(useStore.getState().S.workouts).toEqual([])
    expect(localStorage.getItem('gym_stash')).toBeNull()
    expect(api).toHaveBeenCalledWith('/api/data/rev')
  })

  it('does not full-pull or retry local preview edits until the server revision changes', async () => {
    api.mockResolvedValueOnce({ state: { ...clone(DEF), restSec: 90 }, rev: 7, wid: 'r7' })
    await useStore.getState().pullState()
    api.mockClear()
    useStore.getState().update(state => { state.restSec = 40 })
    api.mockResolvedValueOnce({ rev: 7, wid: 'r7' })

    window.dispatchEvent(new Event('online'))
    await pause()

    expect(api.mock.calls.map(([path, options]) => [path, options?.method || 'GET'])).toEqual([['/api/data/rev', 'GET']])
    expect(useStore.getState().unsyncedChanges()).toMatchObject({ owed: false })
    expect(useStore.getState().sync.pending).toBe(false)
  })

  it('pulls the server day plan when the local calendar day changes, even with the same revision', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date(2026, 6, 1, 23, 59))
    api.mockResolvedValueOnce({ state: { ...clone(DEF), dayPlan: { '2026-07-01': 'r-1' } }, rev: 8, wid: 'r8' })
    await useStore.getState().pullState()
    api.mockClear()
    api.mockResolvedValueOnce({ state: { ...clone(DEF), dayPlan: { '2026-07-02': 'r-2' } }, rev: 8, wid: 'r8' })
    vi.setSystemTime(new Date(2026, 6, 2, 0, 1))

    window.dispatchEvent(new Event('online'))
    await vi.advanceTimersByTimeAsync(1)

    expect(api.mock.calls.map(([path, options]) => [path, options?.method || 'GET'])).toEqual([['/api/data', 'GET']])
    expect(useStore.getState().S.dayPlan).toEqual({ '2026-07-02': 'r-2' })
  })

  it('ignores and removes a stored guest flag in the Mantis build', async () => {
    localStorage.setItem('gym_guest', '1')
    useStore.setState({ user: null, ready: false, config: null })
    api.mockImplementation(async path => {
      if (path === '/api/config') return { allow_guest: true, coach: false }
      if (path === '/api/me') throw Object.assign(new Error('unauthorized'), { status: 401 })
      throw new Error('unexpected ' + path)
    })

    await useStore.getState().boot()

    expect(useStore.getState().isGuest()).toBe(false)
    expect(localStorage.getItem('gym_guest')).toBeNull()
    expect(useStore.getState().ready).toBe(true)
  })

  it('retries an owed logout without stopping boot before /api/me', async () => {
    localStorage.setItem('gym_logout_owed', '1')
    useStore.setState({ user: null, ready: false, config: null })
    api.mockImplementation(async path => {
      if (path === '/api/logout') throw new TypeError('offline')
      if (path === '/api/config') return { allow_guest: false, coach: false }
      if (path === '/api/me') return { user: USER }
      if (path === '/api/data') return { state: { ...clone(DEF), workouts: [] }, rev: 1, wid: 'r1' }
      throw new Error('unexpected ' + path)
    })

    await useStore.getState().boot()

    expect(api).toHaveBeenCalledWith('/api/me')
    expect(useStore.getState().ready).toBe(true)
  })

  it('deletes the server session and redirects after sign out', async () => {
    const oldLocation = Object.getOwnPropertyDescriptor(window, 'location')
    const assign = vi.fn()
    Object.defineProperty(window, 'location', { configurable: true, value: { assign } })
    api.mockResolvedValueOnce({ ok: true })
    useStore.setState({ S: clone(DEF), user: USER, ready: true, readOnly: true, sync: { ...sync } })

    await useStore.getState().signOut()

    expect(api).toHaveBeenCalledWith('/api/logout', { method: 'POST', body: '{}' })
    expect(assign).toHaveBeenCalledWith('/?next=%2Fog%2F')
    Object.defineProperty(window, 'location', oldLocation)
  })
})

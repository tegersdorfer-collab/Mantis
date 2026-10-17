// @vitest-environment happy-dom
// mantis: Keep the read-only disclosure present and remove retry wording from this build.
import React, { act } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createRoot } from 'react-dom/client'

const state = vi.hoisted(() => ({
  S: {}, user: { id: 1 }, sync: { status: 'pending' }, ready: true,
  isGuest: () => false, needsMobileOnboarding: false,
}))
vi.mock('../store/useStore.js', () => ({ useStore: selector => selector(state) }))
vi.mock('../store/useUI.js', () => ({ useUI: { getState: () => ({}) } }))
vi.mock('react-router-dom', () => ({ useNavigate: () => () => {} }))
vi.mock('../components/Icon.jsx', () => ({ default: () => null }))
vi.mock('./ServerSync.jsx', () => ({
  connectionView: () => ({ tone: 'wait', icon: 'reset', line: 'Waiting to sync', banner: 'Not synced yet. Tap to retry.', action: 'retry' }),
  actionLabel: () => 'Retry', syncNowWithToast: vi.fn(), pairAgain: vi.fn(), connectServer: vi.fn(),
  signInAgain: vi.fn(), useOnline: () => true,
}))

vi.stubEnv('VITE_MANTIS_OG', '1')
const { default: SyncBanner } = await import('./SyncBanner.jsx')

let root, host
afterEach(() => {
  if (root) act(() => root.unmount())
  host?.remove()
  root = host = null
  Object.assign(state, {
    S: {}, user: { id: 1 }, sync: { status: 'pending' }, ready: true,
    isGuest: () => false, needsMobileOnboarding: false,
  })
})

it('keeps the read-only preview notice visible across sync states without a retry action', () => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
  act(() => root.render(<SyncBanner />))

  expect(host.textContent).toContain('Vorschau – Änderungen werden nicht gespeichert')
  expect(host.textContent).not.toContain('Waiting to sync')
  expect([...host.querySelectorAll('button')].some(button => button.textContent.includes('Retry'))).toBe(false)

  state.sync = { status: 'ok' }
  act(() => root.render(<SyncBanner />))
  expect(host.textContent).toContain('Vorschau – Änderungen werden nicht gespeichert')
  expect([...host.querySelectorAll('button')].some(button => button.textContent.includes('Retry'))).toBe(false)

  state.sync = { status: 'error' }
  state.user = null
  state.ready = false
  act(() => root.render(<SyncBanner />))
  expect(host.textContent).toContain('Vorschau – Änderungen werden nicht gespeichert')
  expect([...host.querySelectorAll('button')].some(button => button.textContent.includes('Retry'))).toBe(false)
})

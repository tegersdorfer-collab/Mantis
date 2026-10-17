// @vitest-environment happy-dom
import { describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ passkeyLogin: vi.fn() }))
vi.mock('../store/useStore.js', () => ({
  useStore: Object.assign(() => ({}), { getState: () => ({ config: { password_login: false } }) }),
}))
vi.mock('../store/useUI.js', () => ({ useUI: { getState: () => ({ toast: vi.fn() }) } }))
vi.mock('../lib/api.js', () => ({
  passkeyLogin: mocks.passkeyLogin,
  passkeyError: vi.fn(),
  webauthnOK: () => true,
}))
vi.mock('../lib/mobile.js', () => ({ MOBILE: false, shareExport: vi.fn() }))
vi.mock('../lib/media-sync.js', () => ({ syncMedia: vi.fn() }))
vi.mock('../lib/demo.js', () => ({ DEMO: false }))
vi.mock('../sheets.jsx', () => ({ askAddDeviceData: vi.fn(), menuSheet: vi.fn() }))
vi.mock('../views/MobileOnboarding.jsx', () => ({ ConnectSheet: () => null, CfAccessSheet: () => null }))
vi.mock('./PasswordAuth.jsx', () => ({ passwordOn: () => false, openPasswordSignIn: vi.fn() }))

vi.stubEnv('VITE_MANTIS_OG', '1')
const { signInAgain } = await import('./ServerSync.jsx')

describe('Mantis openGym expired session', () => {
  it('returns to the existing token login instead of opening passkey sign-in', () => {
    const oldLocation = Object.getOwnPropertyDescriptor(window, 'location')
    const assign = vi.fn()
    Object.defineProperty(window, 'location', { configurable: true, value: { assign } })

    signInAgain()

    expect(assign).toHaveBeenCalledWith('/?next=%2Fog%2F')
    expect(mocks.passkeyLogin).not.toHaveBeenCalled()
    Object.defineProperty(window, 'location', oldLocation)
  })
})

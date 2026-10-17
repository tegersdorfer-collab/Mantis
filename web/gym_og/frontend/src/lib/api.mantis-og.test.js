// @vitest-environment happy-dom
// mantis: Cover the Mantis token redirect and the session DELETE response contract.
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.stubEnv('VITE_MANTIS_OG', '1')
const { api } = await import('./api.js')

describe('Mantis openGym token login redirect', () => {
  afterEach(() => vi.unstubAllGlobals())

  it.each(['/api/me', '/api/config'])('redirects a 401 from %s to the existing token login', async path => {
    const oldLocation = Object.getOwnPropertyDescriptor(window, 'location')
    const assign = vi.fn()
    Object.defineProperty(window, 'location', { configurable: true, value: { assign } })
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: false, status: 401, json: async () => ({ error: 'Authentication required' }),
    })))

    await expect(api(path)).rejects.toMatchObject({ status: 401 })

    expect(assign).toHaveBeenCalledWith('/login?next=%2F')
    Object.defineProperty(window, 'location', oldLocation)
  })

  it('accepts the 204 response from the shared session deletion endpoint', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, status: 204, json: async () => null })))

    await expect(api('/og/api/logout', { method: 'POST', body: '{}' })).resolves.toEqual({ ok: true })
  })
})

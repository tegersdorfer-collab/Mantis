// mantis: The root-served Mantis build keeps talking to /og/api, whatever the page path is.
import { afterEach, describe, expect, it, vi } from 'vitest'

afterEach(() => { vi.unstubAllEnvs(); vi.unstubAllGlobals(); vi.resetModules() })

describe('Mantis API base', () => {
  it('uses VITE_MANTIS_API_BASE for every API URL, independent of the asset base', async () => {
    vi.stubEnv('VITE_MANTIS_OG', '1')
    vi.stubEnv('VITE_MANTIS_API_BASE', '/og')
    const { apiBase } = await import('./app-base.js')
    expect(apiBase({ pathname: '/' })).toBe('/og/')
    expect(apiBase({ pathname: '/index.html' })).toBe('/og/')

    const fetch = vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ ok: true }) }))
    vi.stubGlobal('fetch', fetch)
    const sendBeacon = vi.fn(() => true)
    vi.stubGlobal('navigator', { sendBeacon })
    const { api, beacon } = await import('./api.js')
    await api('/api/data')
    expect(fetch.mock.calls[0][0]).toBe('/og/api/data')
    beacon('/api/activity', {})
    expect(sendBeacon.mock.calls[0][0]).toBe('/og/api/activity')
  })

  it('falls back to the page path without the Mantis variable', async () => {
    vi.stubEnv('VITE_MANTIS_API_BASE', '')
    const { apiBase } = await import('./app-base.js')
    expect(apiBase({ pathname: '/myGym/' })).toBe('/myGym/')
  })
})

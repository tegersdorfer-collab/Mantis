import { afterEach, describe, expect, it, vi } from 'vitest'

afterEach(() => {
  vi.unstubAllEnvs()
  vi.resetModules()
})

describe('Mantis openGym build', () => {
  it('keeps the built-in exercise database empty', async () => {
    vi.stubEnv('VITE_MANTIS_OG', '1')
    vi.resetModules()
    const [{ EXDB }, { ALL_EQUIPMENT }] = await Promise.all([
      import('./exercises.js'),
      import('./equipment.js'),
    ])

    expect(EXDB).toEqual([])
    expect(ALL_EQUIPMENT).toEqual(['clubbell', 'macebell', 'bench', 'pull-up bar'])
  })
})

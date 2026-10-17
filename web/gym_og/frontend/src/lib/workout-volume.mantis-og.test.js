// mantis: Check that mapped per-bell loads use the upstream volume calculation.
import { describe, expect, it } from 'vitest'
import { workoutVolume } from './history.js'

describe('Mantis dumbbell volume mapping', () => {
  it('counts one 20 kg dumbbell as 320 kg-reps per pair, versus 160 for a single bell', () => {
    const set = { w: 20, r: 8, done: true }

    expect(workoutVolume({ entries: [{ id: '0292', target: { dbLoad: 'each', mantisPair: true }, sets: [set] }] })).toBe(320)
    expect(workoutVolume({ entries: [{ id: '0292', target: { dbLoad: 'each' }, sets: [set] }] })).toBe(160)
  })
})

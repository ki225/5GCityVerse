import { describe, expect, it } from 'vitest'
import { formalJourneyStep } from './LearningLab'
describe('formal intent journey', () => {
  it('starts with a map draft and tracks the accepted formal operation', () => {
    expect(formalJourneyStep(undefined)).toBe(1)
    expect(formalJourneyStep(undefined, 'queued')).toBe(3)
    expect(formalJourneyStep(undefined, 'running', true)).toBe(2)
  })
  it('keeps needs-definition, failure and recovery visible as outcomes', () => {
    for (const status of ['needs_definition', 'failed', 'recovery_required', 'completed']) expect(formalJourneyStep(undefined, status)).toBe(4)
  })
  it('tracks only the round matched by the caller to the intent request identity', () => {
    expect(formalJourneyStep('registering', 'running')).toBe(3)
    expect(formalJourneyStep('completed', 'completed')).toBe(4)
  })
})

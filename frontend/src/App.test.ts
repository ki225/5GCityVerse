import { describe, expect, it } from 'vitest'
import { isAiWorkspaceEnabled, workspaceHeightStyle } from './App'

describe('AI workspace access', () => {
  it('keeps AI Decisions unavailable for no slicing and static slicing', () => {
    expect(isAiWorkspaceEnabled('none', null)).toBe(false)
    expect(isAiWorkspaceEnabled('static', null)).toBe(false)
  })

  it('enables AI Decisions only for AI dynamic slicing', () => {
    expect(isAiWorkspaceEnabled('ai', null)).toBe(true)
  })

  it('uses the strategy locked for the submitted round', () => {
    expect(isAiWorkspaceEnabled('none', 'ai')).toBe(true)
    expect(isAiWorkspaceEnabled('ai', 'static')).toBe(false)
  })
})

describe('workspace height contract', () => {
  it('binds the right workspace to the observed topology-card height', () => {
    expect(workspaceHeightStyle(760.2)).toEqual({ '--topology-card-height': '761px' })
    expect(workspaceHeightStyle(0)).toEqual({ '--topology-card-height': 'auto' })
  })
})

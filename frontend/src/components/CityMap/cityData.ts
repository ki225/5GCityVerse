import type { CityNode, SliceType } from '../../types'

// City node definitions
// Layout: 700 × 480 viewBox
export const CITY_NODES: CityNode[] = [
  // Districts
  { id: 'mall',       label: '商場',       x: 120, y: 90,  type: 'district', activeSlices: ['eMBB'] },
  { id: 'factory',    label: '智慧工廠',   x: 560, y: 90,  type: 'district', activeSlices: ['URLLC'] },
  { id: 'hospital',   label: '醫院',       x: 120, y: 320, type: 'district', activeSlices: ['URLLC'] },
  { id: 'residential',label: '居民區',     x: 340, y: 360, type: 'district', activeSlices: ['eMBB', 'mMTC'] },
  { id: 'highway',    label: '國道', x: 560, y: 320, type: 'district', activeSlices: ['V2X'] },
  // gNB (radio)
  { id: 'gnb1', label: 'gNB', x: 340, y: 190, type: 'gnb', activeSlices: [] },
  // UPF (scale target)
  { id: 'upf',  label: 'UPF',   x: 340, y: 240, type: 'upf',  activeSlices: [] },
  // 5GC
  { id: 'core', label: '5GC Core', x: 340, y: 130, type: 'core', activeSlices: [] },
]

// Static links between nodes
export const CITY_LINKS: { from: string; to: string }[] = [
  { from: 'mall',       to: 'gnb1' },
  { from: 'hospital',   to: 'gnb1' },
  { from: 'factory',    to: 'gnb1' },
  { from: 'highway',    to: 'gnb1' },
  { from: 'residential',to: 'gnb1' },
  { from: 'gnb1',       to: 'upf' },
  { from: 'upf',        to: 'core' },
]

// Colors per slice
export const SLICE_COLOR: Record<SliceType, string> = {
  eMBB:  '#3b82f6',
  URLLC: '#ef4444',
  mMTC:  '#22c55e',
  V2X:   '#f97316',
}

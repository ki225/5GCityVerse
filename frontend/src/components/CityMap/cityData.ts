import type { CityNode, PacketFlow, SliceType } from '../../types'

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
  { id: 'gnb1', label: 'gNB-1', x: 230, y: 190, type: 'gnb', activeSlices: [] },
  { id: 'gnb2', label: 'gNB-2', x: 460, y: 190, type: 'gnb', activeSlices: [] },
  // UPF (scale target)
  { id: 'upf',  label: 'UPF',   x: 340, y: 240, type: 'upf',  activeSlices: [] },
  // 5GC
  { id: 'core', label: '5GC Core', x: 340, y: 130, type: 'core', activeSlices: [] },
]

// Static links between nodes
export const CITY_LINKS: { from: string; to: string }[] = [
  { from: 'mall',       to: 'gnb1' },
  { from: 'hospital',   to: 'gnb1' },
  { from: 'factory',    to: 'gnb2' },
  { from: 'highway',    to: 'gnb2' },
  { from: 'residential',to: 'gnb1' },
  { from: 'residential',to: 'gnb2' },
  { from: 'gnb1',       to: 'upf' },
  { from: 'gnb2',       to: 'upf' },
  { from: 'upf',        to: 'core' },
]

// Colors per slice
export const SLICE_COLOR: Record<SliceType, string> = {
  eMBB:  '#3b82f6',
  URLLC: '#ef4444',
  mMTC:  '#22c55e',
  V2X:   '#f97316',
}

// Default packet flows per event with multi-dimensional characteristics
export const EVENT_FLOWS: Record<string, PacketFlow[]> = {
  concert: [
    // eMBB: High bandwidth, low latency, no flicker (普通視頻串流)
    {
      id: 'f1',
      sourceNodeId: 'mall',
      targetNodeId: 'gnb1',
      sliceType: 'eMBB',
      active: true,
      bandwidthMbps: 800,      // 高帶寬
      latencyMs: 30,            // 低延遲
      fiveQi: 9,                // 低優先級 (視頻)
      packetLossPercent: 0,     // 無丟包
      upfCongestionPercent: 45, // 中等擁塞
    },
    {
      id: 'f2',
      sourceNodeId: 'residential',
      targetNodeId: 'gnb1',
      sliceType: 'eMBB',
      active: true,
      bandwidthMbps: 600,
      latencyMs: 35,
      fiveQi: 9,
      packetLossPercent: 0,
      upfCongestionPercent: 50,
    },
    {
      id: 'f3',
      sourceNodeId: 'gnb1',
      targetNodeId: 'upf',
      sliceType: 'eMBB',
      active: true,
      bandwidthMbps: 1400,      // 聚合流量
      latencyMs: 15,
      fiveQi: 9,
      packetLossPercent: 0.1,   // 極低丟包
      upfCongestionPercent: 75, // 高擁塞
    },
    {
      id: 'f4',
      sourceNodeId: 'upf',
      targetNodeId: 'core',
      sliceType: 'eMBB',
      active: true,
      bandwidthMbps: 1400,
      latencyMs: 10,
      fiveQi: 9,
      packetLossPercent: 0,
      upfCongestionPercent: 60,
    },
  ],

  medical: [
    // URLLC: 低帶寬, 超低延遲, 高優先級閃爍 (緊急醫療)
    {
      id: 'm1',
      sourceNodeId: 'hospital',
      targetNodeId: 'gnb1',
      sliceType: 'URLLC',
      active: true,
      bandwidthMbps: 15,        // 低帶寬
      latencyMs: 8,             // 超低延遲
      fiveQi: 1,                // 最高優先級 URLLC
      packetLossPercent: 0,     // 零丟包保證
      upfCongestionPercent: 15, // 優先級保護，低擁塞
    },
    {
      id: 'm2',
      sourceNodeId: 'gnb1',
      targetNodeId: 'upf',
      sliceType: 'URLLC',
      active: true,
      bandwidthMbps: 20,
      latencyMs: 5,
      fiveQi: 1,
      packetLossPercent: 0,
      upfCongestionPercent: 10,
    },
  ],

  typhoon: [
    // mMTC: 高流量密集度, 多個 UE, 中等延遲 (IoT 感測器爆發)
    {
      id: 't1',
      sourceNodeId: 'residential',
      targetNodeId: 'gnb1',
      sliceType: 'mMTC',
      active: true,
      bandwidthMbps: 300,       // 中等帶寬（多UE聚合）
      latencyMs: 80,            // 中等延遲
      fiveQi: 3,                // mMTC 優先級
      packetLossPercent: 2,     // 允許小丟包
      upfCongestionPercent: 55,
    },
    {
      id: 't2',
      sourceNodeId: 'hospital',
      targetNodeId: 'gnb1',
      sliceType: 'URLLC',
      active: true,
      bandwidthMbps: 18,        // 醫療快速響應
      latencyMs: 6,
      fiveQi: 1,
      packetLossPercent: 0,
      upfCongestionPercent: 12,
    },
    {
      id: 't3',
      sourceNodeId: 'gnb1',
      targetNodeId: 'upf',
      sliceType: 'mMTC',
      active: true,
      bandwidthMbps: 350,
      latencyMs: 50,
      fiveQi: 3,
      packetLossPercent: 1.5,
      upfCongestionPercent: 65,
    },
    {
      id: 't4',
      sourceNodeId: 'gnb1',
      targetNodeId: 'upf',
      sliceType: 'URLLC',
      active: true,
      bandwidthMbps: 25,
      latencyMs: 4,
      fiveQi: 1,
      packetLossPercent: 0,
      upfCongestionPercent: 15,
    },
  ],

  accident: [
    // V2X: 實時導航, 低延遲, 高優先級 (車-到-車通信)
    {
      id: 'a1',
      sourceNodeId: 'highway',
      targetNodeId: 'gnb2',
      sliceType: 'V2X',
      active: true,
      bandwidthMbps: 25,        // V2X 帶寬適中
      latencyMs: 20,            // 很低延遲（導航關鍵）
      fiveQi: 2,                // V2X 高優先級
      packetLossPercent: 0,     // 導航必須可靠
      upfCongestionPercent: 20,
    },
    {
      id: 'a2',
      sourceNodeId: 'gnb2',
      targetNodeId: 'upf',
      sliceType: 'V2X',
      active: true,
      bandwidthMbps: 30,
      latencyMs: 12,
      fiveQi: 2,
      packetLossPercent: 0,
      upfCongestionPercent: 18,
    },
  ],

  iot_surge: [
    // mMTC 主導: 大量小流量 (工廠 IoT)
    {
      id: 'i1',
      sourceNodeId: 'factory',
      targetNodeId: 'gnb2',
      sliceType: 'mMTC',
      active: true,
      bandwidthMbps: 250,
      latencyMs: 100,
      fiveQi: 3,
      packetLossPercent: 3,
      upfCongestionPercent: 50,
    },
    {
      id: 'i2',
      sourceNodeId: 'residential',
      targetNodeId: 'gnb1',
      sliceType: 'mMTC',
      active: true,
      bandwidthMbps: 200,
      latencyMs: 120,
      fiveQi: 3,
      packetLossPercent: 2,
      upfCongestionPercent: 48,
    },
    {
      id: 'i3',
      sourceNodeId: 'gnb1',
      targetNodeId: 'upf',
      sliceType: 'mMTC',
      active: true,
      bandwidthMbps: 280,
      latencyMs: 90,
      fiveQi: 3,
      packetLossPercent: 2.5,
      upfCongestionPercent: 70,
    },
    {
      id: 'i4',
      sourceNodeId: 'gnb2',
      targetNodeId: 'upf',
      sliceType: 'mMTC',
      active: true,
      bandwidthMbps: 320,
      latencyMs: 110,
      fiveQi: 3,
      packetLossPercent: 3,
      upfCongestionPercent: 72,
    },
  ],
}

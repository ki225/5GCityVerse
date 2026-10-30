import { describe, expect, it } from 'vitest'
import { canvasPixelRatio } from './resolution'

describe('畫布解析度與記憶體上限', () => {
  it('一般螢幕也使用 2 倍取樣，Retina 可使用 3 倍', () => {
    expect(canvasPixelRatio(1040, 680, 1)).toBe(2)
    expect(canvasPixelRatio(800, 500, 3)).toBe(3)
    expect(canvasPixelRatio(800, 500, 4)).toBe(3)
  })
  it('大型畫布的每個 backing surface 不超過四百萬像素', () => {
    for (const [width, height] of [[3840, 2160], [7680, 4320]]) {
      const ratio = canvasPixelRatio(width, height, 3)
      expect(width * height * ratio * ratio).toBeLessThanOrEqual(4_000_001)
    }
  })
})

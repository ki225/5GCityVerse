import palette from './colors.json'

/** Single color source for DOM styles, Tailwind and canvas renderers. */
export const colors = palette

/** Install before rendering React so plain CSS and utility colors agree. */
export function initializeColors(root: HTMLElement = document.documentElement) {
  function install(value: Record<string, unknown>, prefix: string) {
    for (const [key, color] of Object.entries(value)) {
      const name = `${prefix}-${key}`
      if (typeof color === 'string') root.style.setProperty(name, color)
      else if (color && typeof color === 'object') install(color as Record<string, unknown>, name)
    }
  }
  install(colors, '--color')
}

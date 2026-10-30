import { readFileSync } from 'node:fs'
const palette = JSON.parse(readFileSync(new URL('./src/theme/colors.json', import.meta.url), 'utf8'))
/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        ...palette,
        ...palette.slice,
        city: {
          bg: palette.surface.app,
          panel: palette.surface.panel,
          border: palette.slate[200],
          accent: palette.primary[600],
        },
      },
      animation: {
        'pulse-slow': 'pulse 3s cubic-bezier(0.4, 0, 0.6, 1) infinite',
        'flow': 'flow 2s linear infinite',
      },
      keyframes: {
        flow: {
          '0%': { strokeDashoffset: '100' },
          '100%': { strokeDashoffset: '0' },
        },
      },
    },
  },
  plugins: [],
}

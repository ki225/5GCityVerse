/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        embb: '#3b82f6',      // blue-500
        urllc: '#ef4444',     // red-500
        mmtc: '#22c55e',      // green-500
        v2x: '#f97316',       // orange-500
        city: {
          bg: '#0a0f1e',
          panel: '#0f172a',
          border: '#1e293b',
          accent: '#38bdf8',
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

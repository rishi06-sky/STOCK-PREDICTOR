import type { Config } from 'tailwindcss';

/**
 * Terminal-inspired palette: a dark ground with restrained accents, so the
 * colour that does appear (a signal, a P&L figure) carries meaning.
 */
const config: Config = {
  content: ['./src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        ground: { DEFAULT: '#0b0f14', raised: '#121821', overlay: '#1a222d' },
        line: { DEFAULT: '#1f2937', bright: '#2d3a4d' },
        ink: { DEFAULT: '#e6edf3', muted: '#8b98a9', faint: '#5c6b7d' },
        bull: { DEFAULT: '#26a96c', soft: '#15352a' },
        bear: { DEFAULT: '#e5484d', soft: '#3b1a1d' },
        flat: { DEFAULT: '#8b98a9', soft: '#1a222d' },
        accent: { DEFAULT: '#3b82f6', soft: '#132338' },
        warn: { DEFAULT: '#d29922', soft: '#332711' },
      },
      fontFamily: {
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'Consolas', 'monospace'],
        sans: ['ui-sans-serif', 'system-ui', '-apple-system', 'Segoe UI', 'sans-serif'],
      },
      fontSize: { '2xs': ['0.6875rem', { lineHeight: '1rem' }] },
    },
  },
  plugins: [],
};
export default config;

import type { Config } from 'tailwindcss';

/**
 * Instrument-panel palette: graphite neutrals with one accent, so the colour
 * that does appear (a signal, a P&L figure, a warning) carries meaning.
 *
 * Every colour is a CSS variable holding RGB channels (see globals.css), so
 * the light and dark themes swap in one place and Tailwind's `/alpha`
 * modifiers keep working.
 *
 * Shape rule, applied everywhere: surfaces `rounded-lg`, controls
 * `rounded-md`, chips `rounded`.
 */
const token = (name: string) => `rgb(var(--${name}) / <alpha-value>)`;

const config: Config = {
  content: ['./src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        ground: { DEFAULT: token('ground'), raised: token('ground-raised'), overlay: token('ground-overlay') },
        line: { DEFAULT: token('line'), bright: token('line-bright') },
        ink: { DEFAULT: token('ink'), muted: token('ink-muted'), faint: token('ink-faint') },
        bull: { DEFAULT: token('bull'), soft: token('bull-soft') },
        bear: { DEFAULT: token('bear'), soft: token('bear-soft') },
        flat: { DEFAULT: token('ink-muted'), soft: token('ground-overlay') },
        accent: { DEFAULT: token('accent'), soft: token('accent-soft'), ink: token('accent-ink') },
        warn: { DEFAULT: token('warn'), soft: token('warn-soft') },
      },
      fontFamily: {
        sans: ['var(--font-geist-sans)', 'ui-sans-serif', 'system-ui', 'sans-serif'],
        mono: ['var(--font-geist-mono)', 'ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace'],
      },
      fontSize: { '2xs': ['0.6875rem', { lineHeight: '1rem' }] },
    },
  },
  plugins: [],
};
export default config;

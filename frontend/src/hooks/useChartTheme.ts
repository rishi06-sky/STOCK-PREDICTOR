'use client';

import { useEffect, useState } from 'react';

/**
 * Chart colours resolved from the theme tokens in globals.css.
 *
 * Recharts writes colours into SVG presentation attributes, where CSS
 * variables are not reliably resolved, so the tokens are read once and again
 * whenever the colour scheme flips. The defaults match the dark theme, which
 * is also what the server renders.
 */
export interface ChartTheme {
  grid: string;
  axis: string;
  tick: string;
  surface: string;
  muted: string;
  accent: string;
  bull: string;
  bear: string;
  warn: string;
  volume: string;
}

const DARK: ChartTheme = {
  grid: 'rgb(38 40 45)',
  axis: 'rgb(38 40 45)',
  tick: 'rgb(134 139 148)',
  surface: 'rgb(21 22 25)',
  muted: 'rgb(160 164 172)',
  accent: 'rgb(118 150 240)',
  bull: 'rgb(70 186 140)',
  bear: 'rgb(238 98 104)',
  warn: 'rgb(222 170 72)',
  volume: 'rgb(54 57 64)',
};

function read(): ChartTheme {
  const style = getComputedStyle(document.documentElement);
  const color = (name: string, fallback: string) => {
    const value = style.getPropertyValue(`--${name}`).trim();
    return value ? `rgb(${value})` : fallback;
  };
  return {
    grid: color('line', DARK.grid),
    axis: color('line', DARK.axis),
    tick: color('ink-faint', DARK.tick),
    surface: color('ground-raised', DARK.surface),
    muted: color('ink-muted', DARK.muted),
    accent: color('accent', DARK.accent),
    bull: color('bull', DARK.bull),
    bear: color('bear', DARK.bear),
    warn: color('warn', DARK.warn),
    volume: color('line-bright', DARK.volume),
  };
}

export function useChartTheme(): ChartTheme {
  const [theme, setTheme] = useState<ChartTheme>(DARK);

  useEffect(() => {
    const query = window.matchMedia('(prefers-color-scheme: light)');
    const update = () => setTheme(read());
    update();
    query.addEventListener('change', update);
    return () => query.removeEventListener('change', update);
  }, []);

  return theme;
}

/** Shared tooltip styling so every chart reads the same. */
export function tooltipStyle(theme: ChartTheme) {
  return {
    contentStyle: {
      background: theme.surface,
      border: `1px solid ${theme.grid}`,
      borderRadius: 8,
      fontSize: 12,
      fontFamily: 'var(--font-geist-mono), ui-monospace, monospace',
    },
    labelStyle: { color: theme.muted },
    itemStyle: { color: theme.muted },
    cursor: { stroke: theme.volume },
  };
}

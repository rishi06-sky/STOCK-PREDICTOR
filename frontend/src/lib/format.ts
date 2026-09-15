/** Presentation helpers. Nothing here invents precision the data lacks. */

export function formatNumber(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '--';
  return value.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

/**
 * Format an amount in its own currency.
 *
 * The default matches the backend's `base_currency`, which the seeded universe
 * is built around -- but pass the currency the API supplied wherever there is
 * one, so a price is never relabelled as something it is not.
 */
export function formatCurrency(
  value: number | null | undefined,
  currency: string | null | undefined = 'INR',
  digits = 2,
): string {
  currency = currency || 'INR';
  if (value === null || value === undefined || Number.isNaN(value)) return '--';
  try {
    return value.toLocaleString(undefined, {
      style: 'currency',
      currency,
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    });
  } catch {
    return formatNumber(value, digits);
  }
}

export function formatPercent(
  value: number | null | undefined,
  digits = 2,
  { alreadyPercent = false } = {},
): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '--';
  const pct = alreadyPercent ? value : value * 100;
  return `${pct >= 0 ? '+' : ''}${pct.toFixed(digits)}%`;
}

export function formatCompact(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '--';
  return Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 })
    .format(value);
}

export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return '--';
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000;
  if (seconds < 0) return 'just now';
  if (seconds < 60) return `${Math.floor(seconds)}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return '--';
  return new Date(iso).toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: '2-digit',
    hour: '2-digit', minute: '2-digit',
  });
}

export const directionClass = (value: number | null | undefined): string => {
  if (value === null || value === undefined || Number.isNaN(value)) return 'text-ink-muted';
  if (value > 0) return 'text-bull';
  if (value < 0) return 'text-bear';
  return 'text-flat';
};

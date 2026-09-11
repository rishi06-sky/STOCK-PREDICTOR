/** Shared presentational primitives. */
import type { ReactNode } from 'react';
import type { DataQuality, SignalKind } from '@/lib/api';
import { relativeTime } from '@/lib/format';

/**
 * Panel carries `min-w-0`: a grid or flex child defaults to `min-width: auto`,
 * which stops a wide table from shrinking and pushes the whole page wider than
 * the viewport. With `min-w-0` the table scrolls inside its own container.
 */
export function Panel({
  title, actions, children, className = '', bodyClassName = 'p-4',
}: {
  title?: string;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
}) {
  return (
    <section className={`panel min-w-0 ${className}`}>
      {(title || actions) && (
        <header className="panel-header">
          {title && <h2 className="panel-title">{title}</h2>}
          {actions}
        </header>
      )}
      <div className={bodyClassName}>{children}</div>
    </section>
  );
}

export function Stat({
  label, value, sub, tone = 'neutral',
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  tone?: 'neutral' | 'bull' | 'bear';
}) {
  const toneClass =
    tone === 'bull' ? 'text-bull' : tone === 'bear' ? 'text-bear' : 'text-ink';
  return (
    <div>
      <div className="stat-label">{label}</div>
      <div className={`stat-value text-lg mt-0.5 ${toneClass}`}>{value}</div>
      {sub && <div className="text-2xs text-ink-faint mt-0.5">{sub}</div>}
    </div>
  );
}

const SIGNAL_STYLES: Record<SignalKind, string> = {
  STRONG_BUY: 'bg-bull text-white',
  BUY: 'bg-bull-soft text-bull border border-bull/40',
  HOLD: 'bg-flat-soft text-flat border border-flat/30',
  NO_ACTION: 'bg-flat-soft text-ink-faint border border-line',
  SELL: 'bg-bear-soft text-bear border border-bear/40',
  STRONG_SELL: 'bg-bear text-white',
};

export function SignalBadge({ signal }: { signal: SignalKind }) {
  return (
    <span className={`chip ${SIGNAL_STYLES[signal] ?? SIGNAL_STYLES.HOLD}`}>
      {signal.replace('_', ' ')}
    </span>
  );
}

/**
 * Freshness is shown next to every price. A DELAYED or EOD figure is not a
 * live price, and SYNTHETIC is test data that must never be traded on.
 */
export function QualityBadge({
  quality, asOf, stale,
}: {
  quality: DataQuality;
  asOf?: string;
  stale?: boolean;
}) {
  const style =
    quality === 'LIVE' ? 'bg-bull-soft text-bull'
      : quality === 'SYNTHETIC' ? 'bg-warn-soft text-warn'
        : stale ? 'bg-bear-soft text-bear'
          : 'bg-ground-overlay text-ink-muted';
  const label = quality === 'SYNTHETIC' ? 'SYNTHETIC (TEST)' : quality;
  return (
    <span className={`chip ${style}`} title={asOf ? `as of ${asOf}` : undefined}>
      {label}
      {asOf && <span className="text-ink-faint">· {relativeTime(asOf)}</span>}
      {stale && <span className="font-semibold">· STALE</span>}
    </span>
  );
}

const RISK_STYLES: Record<string, string> = {
  LOW: 'bg-bull-soft text-bull',
  MODERATE: 'bg-ground-overlay text-ink-muted',
  HIGH: 'bg-warn-soft text-warn',
  VERY_HIGH: 'bg-bear-soft text-bear',
};

export function RiskBadge({ level }: { level: string }) {
  return (
    <span className={`chip ${RISK_STYLES[level] ?? RISK_STYLES.MODERATE}`}>
      {level.replace('_', ' ')}
    </span>
  );
}

export function StatusDot({ status }: { status: string }) {
  const color =
    status === 'HEALTHY' ? 'bg-bull'
      : status === 'DEGRADED' ? 'bg-warn'
        : 'bg-bear';
  return <span className={`inline-block h-2 w-2 rounded-full ${color}`} />;
}

export function Confidence({ value }: { value: number }) {
  const pct = Math.round(value * 100);
  const bar = value >= 0.7 ? 'bg-bull' : value >= 0.55 ? 'bg-accent' : 'bg-flat';
  return (
    <div className="flex items-center gap-2">
      <div className="h-1.5 w-16 rounded-full bg-ground-overlay overflow-hidden">
        <div className={`h-full ${bar}`} style={{ width: `${pct}%` }} />
      </div>
      <span className="font-mono tabular-nums text-xs">{pct}%</span>
    </div>
  );
}

export function Empty({ message, hint }: { message: string; hint?: string }) {
  return (
    <div className="py-10 text-center">
      <p className="text-sm text-ink-muted">{message}</p>
      {hint && <p className="mt-1 text-2xs text-ink-faint">{hint}</p>}
    </div>
  );
}

export function Loading({ label = 'Loading' }: { label?: string }) {
  return (
    <div className="py-10 text-center text-sm text-ink-faint animate-pulse">
      {label}…
    </div>
  );
}

export function ErrorBox({ message }: { message: string }) {
  return (
    <div className="rounded-md border border-bear/40 bg-bear-soft px-3 py-2 text-sm text-bear">
      {message}
    </div>
  );
}

/** Shown wherever model output is presented, so nothing reads as a promise. */
export function Disclaimer({ children }: { children?: ReactNode }) {
  return (
    <p className="text-2xs leading-relaxed text-ink-faint">
      {children ?? (
        <>
          Model-generated estimates carrying real uncertainty. Not investment
          advice, and not a prediction of any guaranteed outcome.
        </>
      )}
    </p>
  );
}

/** Shared presentational primitives. */
import type { ReactNode } from 'react';
import {
  ArrowDownRight, ArrowUpRight, Minus, Tray, WarningCircle,
} from '@phosphor-icons/react/ssr';
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

/** Title block at the top of each page: one heading, one line of context. */
export function PageHeader({
  title, description, meta, actions,
}: {
  title: ReactNode;
  description?: ReactNode;
  meta?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3 pb-1">
      <div className="min-w-0">
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
          {meta && <span className="text-xs text-ink-faint">{meta}</span>}
        </div>
        {description && (
          <p className="mt-1 max-w-[65ch] text-sm leading-relaxed text-ink-muted">{description}</p>
        )}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

export function Stat({
  label, value, sub, tone = 'neutral', size = 'md',
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  tone?: 'neutral' | 'bull' | 'bear';
  size?: 'md' | 'lg';
}) {
  const toneClass =
    tone === 'bull' ? 'text-bull' : tone === 'bear' ? 'text-bear' : 'text-ink';
  return (
    <div className="min-w-0">
      <div className="stat-label">{label}</div>
      <div className={`stat-value mt-1 ${size === 'lg' ? 'text-2xl' : 'text-lg'} ${toneClass}`}>
        {value}
      </div>
      {sub && <div className="mt-0.5 text-xs tabular-nums text-ink-faint">{sub}</div>}
    </div>
  );
}

const SIGNAL_STYLES: Record<SignalKind, string> = {
  STRONG_BUY: 'bg-bull text-ground-raised',
  BUY: 'bg-bull-soft text-bull',
  HOLD: 'bg-ground-overlay text-ink-muted',
  NO_ACTION: 'bg-ground-overlay text-ink-faint',
  SELL: 'bg-bear-soft text-bear',
  STRONG_SELL: 'bg-bear text-ground-raised',
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
      {asOf && <span className="font-normal opacity-75">{relativeTime(asOf)}</span>}
      {stale && <span className="font-semibold">STALE</span>}
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

/** A real status indicator, never decoration: healthy, degraded or down. */
export function StatusDot({ status }: { status: string }) {
  const color =
    status === 'HEALTHY' ? 'bg-bull'
      : status === 'DEGRADED' ? 'bg-warn'
        : 'bg-bear';
  return (
    <span
      role="img"
      aria-label={status.toLowerCase()}
      className={`inline-block h-2 w-2 shrink-0 rounded-full ${color}`}
    />
  );
}

/**
 * Confidence is direction-neutral, so it is drawn in the accent rather than
 * bull/bear colours: a confident SELL must not show a green bar. Readings
 * below the floor the engine treats as weak are dimmed.
 */
export function Confidence({ value }: { value: number }) {
  const pct = Math.round(value * 100);
  const bar = value >= 0.55 ? 'bg-accent' : 'bg-ink-faint';
  return (
    <div className="flex items-center gap-2" title={`${pct}% model confidence`}>
      <div className="h-1 w-14 overflow-hidden rounded-full bg-ground-overlay">
        <div className={`h-full rounded-full ${bar}`} style={{ width: `${pct}%` }} />
      </div>
      <span className="font-mono text-xs tabular-nums">{pct}%</span>
    </div>
  );
}

/** Direction glyph for rationale factors and news sentiment. */
export function DirectionIcon({
  direction, className = '',
}: {
  direction: 'up' | 'down' | 'neutral';
  className?: string;
}) {
  const props = { size: 14, weight: 'bold' as const, className: `shrink-0 ${className}` };
  if (direction === 'up') return <ArrowUpRight {...props} className={`${props.className} text-bull`} aria-label="bullish" />;
  if (direction === 'down') return <ArrowDownRight {...props} className={`${props.className} text-bear`} aria-label="bearish" />;
  return <Minus {...props} className={`${props.className} text-ink-faint`} aria-label="neutral" />;
}

export function Empty({ message, hint }: { message: string; hint?: string }) {
  return (
    <div className="flex flex-col items-center px-4 py-10 text-center">
      <Tray size={22} className="mb-2 text-ink-faint" aria-hidden />
      <p className="text-sm text-ink-muted">{message}</p>
      {hint && <p className="mt-1 max-w-[52ch] text-xs leading-relaxed text-ink-faint">{hint}</p>}
    </div>
  );
}

/**
 * Loading placeholder shaped like the rows it stands in for, so the layout
 * does not jump when data arrives.
 */
export function Loading({ label = 'Loading', rows = 4 }: { label?: string; rows?: number }) {
  return (
    <div className="space-y-3 p-4" role="status" aria-live="polite">
      <span className="sr-only">{label}</span>
      {Array.from({ length: rows }, (_, index) => (
        <div key={index} className="flex items-center gap-4">
          <div className="skeleton h-3 w-24" />
          <div className="skeleton h-3 flex-1" style={{ maxWidth: `${70 - index * 9}%` }} />
          <div className="skeleton ml-auto h-3 w-14" />
        </div>
      ))}
    </div>
  );
}

export function ErrorBox({ message }: { message: string }) {
  return (
    <div
      role="alert"
      className="flex items-start gap-2 rounded-md border border-bear/40 bg-bear-soft px-3 py-2 text-sm text-bear"
    >
      <WarningCircle size={16} weight="bold" className="mt-0.5 shrink-0" aria-hidden />
      <span>{message}</span>
    </div>
  );
}

/** A warning line, used for portfolio and risk notes. */
export function WarningLine({ children, className = '' }: { children: ReactNode; className?: string }) {
  return (
    <li className={`flex items-start gap-1.5 text-warn ${className}`}>
      <WarningCircle size={14} weight="bold" className="mt-[3px] shrink-0" aria-hidden />
      <span>{children}</span>
    </li>
  );
}

/** Shown wherever model output is presented, so nothing reads as a promise. */
export function Disclaimer({ children }: { children?: ReactNode }) {
  return (
    <p className="max-w-[90ch] text-xs leading-relaxed text-ink-faint">
      {children ?? (
        <>
          Model-generated estimates carrying real uncertainty. Not investment
          advice, and not a prediction of any guaranteed outcome.
        </>
      )}
    </p>
  );
}

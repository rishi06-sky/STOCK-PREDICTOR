'use client';

import Link from 'next/link';
import { useState } from 'react';
import useSWR from 'swr';
import {
  Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts';
import { api, fetcher, type Page, type Portfolio } from '@/lib/api';
import {
  directionClass, formatCurrency, formatNumber, formatPercent, relativeTime,
} from '@/lib/format';
import {
  Disclaimer, Empty, ErrorBox, Loading, PageHeader, Panel, Stat, WarningLine,
} from '@/components/ui';
import { tooltipStyle, useChartTheme } from '@/hooks/useChartTheme';

interface Trade {
  id: number; symbol: string; side: string; quantity: number; price: number;
  reference_price: number; commission: number; slippage_cost: number;
  realized_pnl: number | null; exit_reason: string | null; mode: string;
  executed_at: string;
}

interface Performance {
  performance: Record<string, unknown>;
  equity_curve: { date: string; equity: number }[];
  correlation: {
    highest_pairs?: { a: string; b: string; correlation: number }[];
    average_absolute_correlation?: number;
    diversification_note?: string;
    note?: string;
  };
}

const toneOf = (value: number): 'bull' | 'bear' | 'neutral' =>
  value > 0 ? 'bull' : value < 0 ? 'bear' : 'neutral';

export default function PortfolioPage() {
  const portfolio = useSWR<Portfolio>('/portfolio', fetcher, { refreshInterval: 30000 });
  const trades = useSWR<Page<Trade>>('/portfolio/trades?limit=40', fetcher);
  const performance = useSWR<Performance>('/portfolio/performance', fetcher);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const pf = portfolio.data;
  const chart = useChartTheme();

  async function runCycle() {
    setBusy(true);
    setMessage(null);
    try {
      const result = await api.post<{ opened: unknown[]; closed: unknown[]; halt_reason?: string }>(
        '/portfolio/paper/run',
      );
      setMessage(
        result.halt_reason
          ? `Halted: ${result.halt_reason}`
          : `Opened ${result.opened.length}, closed ${result.closed.length}.`,
      );
      portfolio.mutate();
      trades.mutate();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'Paper cycle failed');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-5">
      <PageHeader
        title="Portfolio"
        meta={pf && <span className="chip bg-ground-overlay text-ink-muted">{pf.mode}</span>}
        description="Paper trading uses the same signal and risk engines as live mode."
        actions={
          <button className="btn-primary" onClick={runCycle} disabled={busy}>
            {busy ? 'Running…' : 'Run paper cycle'}
          </button>
        }
      />

      {message && <p role="status" className="text-sm text-ink-muted">{message}</p>}
      {portfolio.error && <ErrorBox message={String(portfolio.error)} />}
      {portfolio.isLoading && <Panel><Loading /></Panel>}

      {pf && (
        <>
          <section
            aria-label="Portfolio summary"
            className="grid grid-cols-2 gap-px overflow-hidden rounded-lg border border-line bg-line lg:grid-cols-6"
          >
            {[
              { label: 'Equity', value: formatCurrency(pf.equity, pf.currency, 0) },
              {
                label: 'Total P&L', value: formatCurrency(pf.total_pnl, pf.currency, 0),
                sub: formatPercent(pf.total_pnl_pct), tone: toneOf(pf.total_pnl),
              },
              {
                label: 'Unrealised', value: formatCurrency(pf.unrealized_pnl, pf.currency, 0),
                tone: toneOf(pf.unrealized_pnl),
              },
              {
                label: 'Realised', value: formatCurrency(pf.realized_pnl, pf.currency, 0),
                tone: toneOf(pf.realized_pnl),
              },
              { label: 'Cash', value: formatCurrency(pf.cash, pf.currency, 0) },
              {
                label: 'Exposure', value: formatPercent(pf.exposure_pct, 1).replace('+', ''),
                sub: `drawdown ${formatPercent(pf.drawdown_pct, 1)}`,
              },
            ].map((stat) => (
              <div key={stat.label} className="min-w-0 bg-ground-raised px-4 py-3.5">
                <Stat label={stat.label} value={stat.value} sub={stat.sub} tone={stat.tone} />
              </div>
            ))}
          </section>

          {pf.warnings.length > 0 && (
            <ul className="space-y-1 rounded-lg border border-warn/30 bg-warn-soft px-4 py-3 text-sm">
              {pf.warnings.map((warning) => (
                <WarningLine key={warning}>{warning}</WarningLine>
              ))}
            </ul>
          )}

          <div className="grid grid-cols-1 gap-5 lg:grid-cols-3">
            <Panel title="Equity curve, live" className="lg:col-span-2" bodyClassName="p-4">
              {performance.data && performance.data.equity_curve.length > 1 ? (
                <div className="h-64">
                  <ResponsiveContainer width="100%" height="100%">
                    <AreaChart data={performance.data.equity_curve}>
                      <defs>
                        <linearGradient id="equityFill" x1="0" y1="0" x2="0" y2="1">
                          <stop offset="0%" stopColor={chart.accent} stopOpacity={0.28} />
                          <stop offset="100%" stopColor={chart.accent} stopOpacity={0} />
                        </linearGradient>
                      </defs>
                      <CartesianGrid stroke={chart.grid} vertical={false} />
                      <XAxis
                        dataKey="date" tick={{ fontSize: 11, fill: chart.tick }} stroke={chart.axis}
                        tickLine={false} minTickGap={32}
                      />
                      <YAxis
                        tick={{ fontSize: 11, fill: chart.tick }} stroke={chart.axis} axisLine={false} tickLine={false}
                        domain={['auto', 'auto']} width={70}
                        tickFormatter={(v) => Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 2 }).format(v)}
                      />
                      <Tooltip {...tooltipStyle(chart)} />
                      <Area
                        type="monotone" dataKey="equity" stroke={chart.accent}
                        strokeWidth={1.5} fill="url(#equityFill)"
                      />
                    </AreaChart>
                  </ResponsiveContainer>
                </div>
              ) : (
                <Empty
                  message="Not enough snapshots yet"
                  hint="A daily snapshot is recorded by the scheduler; the curve appears once there are at least two."
                />
              )}
              <p className="mt-2 text-xs text-ink-faint">
                Live performance from recorded portfolio snapshots. This is separate
                from backtest results.
              </p>
            </Panel>

            <Panel title="Allocation">
              {Object.keys(pf.sector_allocation).length === 0 && (
                <Empty message="No open positions" />
              )}
              <ul className="space-y-3">
                {Object.entries(pf.sector_allocation).map(([sector, weight]) => (
                  <li key={sector}>
                    <div className="flex items-baseline justify-between text-sm">
                      <span className="truncate">{sector}</span>
                      <span className="font-mono tabular-nums text-ink-muted">
                        {(weight * 100).toFixed(1)}%
                      </span>
                    </div>
                    <div className="mt-1.5 h-1 overflow-hidden rounded-full bg-ground-overlay">
                      <div
                        className={`h-full rounded-full ${weight > 0.3 ? 'bg-warn' : 'bg-accent'}`}
                        style={{ width: `${Math.min(weight * 100, 100)}%` }}
                      />
                    </div>
                  </li>
                ))}
              </ul>
              {performance.data?.correlation?.average_absolute_correlation !== undefined && (
                <p className="mt-4 border-t border-line pt-3 text-xs leading-relaxed text-ink-faint">
                  Average absolute correlation{' '}
                  <span className="font-mono tabular-nums text-ink-muted">
                    {performance.data.correlation.average_absolute_correlation.toFixed(2)}
                  </span>
                  : {performance.data.correlation.diversification_note}
                </p>
              )}
            </Panel>
          </div>

          <Panel title="Open positions" bodyClassName="p-0">
            {pf.positions.length === 0 && <Empty message="No open positions" />}
            {pf.positions.length > 0 && (
              <div className="w-full max-w-full overflow-x-auto">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Symbol</th><th>Sector</th>
                      <th className="text-right">Qty</th>
                      <th className="text-right">Avg cost</th>
                      <th className="text-right">Price</th>
                      <th className="text-right">Value</th>
                      <th className="text-right">P&L</th>
                      <th className="text-right">P&L %</th>
                      <th className="text-right">Weight</th>
                      <th className="text-right">Stop</th>
                      <th className="text-right">Target</th>
                      <th>Opened</th>
                    </tr>
                  </thead>
                  <tbody>
                    {pf.positions.map((position) => (
                      <tr key={position.security_id}>
                        <td>
                          <Link href={`/stock/${position.symbol}`} className="font-medium hover:text-accent">
                            {position.symbol}
                          </Link>
                          {position.price_is_stale && (
                            <span className="chip ml-1.5 bg-bear-soft text-bear">STALE</span>
                          )}
                          {!position.counted_in_totals && (
                            <span
                              className="chip ml-1.5 bg-bear-soft text-bear"
                              title={`Priced in ${position.currency || 'an unknown currency'}; not included in the portfolio totals because no FX rate is available.`}
                            >
                              NOT IN TOTAL
                            </span>
                          )}
                        </td>
                        <td className="text-ink-muted">{position.sector ?? '--'}</td>
                        <td className="text-right font-mono tabular-nums">{formatNumber(position.quantity, 2)}</td>
                        <td className="text-right font-mono tabular-nums">{formatNumber(position.average_cost)}</td>
                        <td className="text-right font-mono tabular-nums">{formatCurrency(position.current_price, position.currency || pf?.currency)}</td>
                        <td className="text-right font-mono tabular-nums">{formatCurrency(position.market_value, position.currency || pf?.currency, 0)}</td>
                        <td className={`text-right font-mono tabular-nums ${directionClass(position.unrealized_pnl)}`}>
                          {formatNumber(position.unrealized_pnl, 2)}
                        </td>
                        <td className={`text-right font-mono tabular-nums ${directionClass(position.unrealized_pnl_pct)}`}>
                          {formatPercent(position.unrealized_pnl_pct)}
                        </td>
                        <td className="text-right font-mono tabular-nums text-ink-muted">
                          {(position.weight * 100).toFixed(1)}%
                        </td>
                        <td className="text-right font-mono tabular-nums text-bear">
                          {formatNumber(position.stop_loss)}
                        </td>
                        <td className="text-right font-mono tabular-nums text-bull">
                          {formatNumber(position.take_profit)}
                        </td>
                        <td className="text-xs text-ink-faint">{relativeTime(position.opened_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Panel>

          <Panel title="Trade history" bodyClassName="p-0">
            {!trades.data && <Loading />}
            {trades.data?.items.length === 0 && <Empty message="No trades executed yet" />}
            {!!trades.data?.items.length && (
              <div className="w-full max-w-full overflow-x-auto">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Executed</th><th>Symbol</th><th>Side</th>
                      <th className="text-right">Qty</th>
                      <th className="text-right">Fill</th>
                      <th className="text-right">Reference</th>
                      <th className="text-right">Commission</th>
                      <th className="text-right">Slippage</th>
                      <th className="text-right">Realised P&L</th>
                      <th>Reason</th><th>Mode</th>
                    </tr>
                  </thead>
                  <tbody>
                    {trades.data.items.map((trade) => (
                      <tr key={trade.id}>
                        <td className="text-xs text-ink-faint">{relativeTime(trade.executed_at)}</td>
                        <td className="font-medium">{trade.symbol}</td>
                        <td>
                          <span className={`chip ${trade.side === 'BUY' ? 'bg-bull-soft text-bull' : 'bg-bear-soft text-bear'}`}>
                            {trade.side}
                          </span>
                        </td>
                        <td className="text-right font-mono tabular-nums">{formatNumber(trade.quantity, 2)}</td>
                        <td className="text-right font-mono tabular-nums">{formatCurrency(trade.price, pf?.currency)}</td>
                        <td className="text-right font-mono tabular-nums text-ink-muted">
                          {formatCurrency(trade.reference_price, pf?.currency)}
                        </td>
                        <td className="text-right font-mono tabular-nums text-ink-muted">
                          {formatNumber(trade.commission)}
                        </td>
                        <td className="text-right font-mono tabular-nums text-ink-muted">
                          {formatNumber(trade.slippage_cost)}
                        </td>
                        <td className={`text-right font-mono tabular-nums ${directionClass(trade.realized_pnl)}`}>
                          {trade.realized_pnl === null ? '--' : formatNumber(trade.realized_pnl, 2)}
                        </td>
                        <td className="text-xs text-ink-muted">{trade.exit_reason ?? '--'}</td>
                        <td className="text-xs text-ink-faint">{trade.mode}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <div className="border-t border-line px-4 py-3">
              <Disclaimer>
                Fill prices include simulated slippage and commission. Paper results
                are a rehearsal, not a promise of live execution quality.
              </Disclaimer>
            </div>
          </Panel>
        </>
      )}
    </div>
  );
}

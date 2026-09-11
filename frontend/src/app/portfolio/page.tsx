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
import { Disclaimer, Empty, ErrorBox, Loading, Panel, Stat } from '@/components/ui';

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

export default function PortfolioPage() {
  const portfolio = useSWR<Portfolio>('/portfolio', fetcher, { refreshInterval: 30000 });
  const trades = useSWR<Page<Trade>>('/portfolio/trades?limit=40', fetcher);
  const performance = useSWR<Performance>('/portfolio/performance', fetcher);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const pf = portfolio.data;

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
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">
            Portfolio {pf && <span className="text-2xs text-ink-faint">· {pf.mode}</span>}
          </h1>
          <p className="text-2xs text-ink-faint">
            Paper trading uses the same signal and risk engines as live mode.
          </p>
        </div>
        <button className="btn-primary" onClick={runCycle} disabled={busy}>
          {busy ? 'Running…' : 'Run paper cycle'}
        </button>
      </div>

      {message && <div className="text-2xs text-ink-muted">{message}</div>}
      {portfolio.error && <ErrorBox message={String(portfolio.error)} />}
      {portfolio.isLoading && <Panel><Loading /></Panel>}

      {pf && (
        <>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-6">
            <Panel bodyClassName="p-3">
              <Stat label="Equity" value={formatCurrency(pf.equity, pf.currency, 0)} />
            </Panel>
            <Panel bodyClassName="p-3">
              <Stat
                label="Total P&L" value={formatCurrency(pf.total_pnl, pf.currency, 0)}
                sub={formatPercent(pf.total_pnl_pct)}
                tone={pf.total_pnl > 0 ? 'bull' : pf.total_pnl < 0 ? 'bear' : 'neutral'}
              />
            </Panel>
            <Panel bodyClassName="p-3">
              <Stat
                label="Unrealised" value={formatCurrency(pf.unrealized_pnl, pf.currency, 0)}
                tone={pf.unrealized_pnl > 0 ? 'bull' : pf.unrealized_pnl < 0 ? 'bear' : 'neutral'}
              />
            </Panel>
            <Panel bodyClassName="p-3">
              <Stat
                label="Realised" value={formatCurrency(pf.realized_pnl, pf.currency, 0)}
                tone={pf.realized_pnl > 0 ? 'bull' : pf.realized_pnl < 0 ? 'bear' : 'neutral'}
              />
            </Panel>
            <Panel bodyClassName="p-3">
              <Stat label="Cash" value={formatCurrency(pf.cash, pf.currency, 0)} />
            </Panel>
            <Panel bodyClassName="p-3">
              <Stat
                label="Exposure" value={formatPercent(pf.exposure_pct, 1).replace('+', '')}
                sub={`drawdown ${formatPercent(pf.drawdown_pct, 1)}`}
              />
            </Panel>
          </div>

          {pf.warnings.length > 0 && (
            <Panel title="Warnings">
              <ul className="space-y-1">
                {pf.warnings.map((warning) => (
                  <li key={warning} className="text-sm text-warn">⚠ {warning}</li>
                ))}
              </ul>
            </Panel>
          )}

          <div className="grid gap-4 lg:grid-cols-3">
            <Panel title="Equity curve (LIVE)" className="lg:col-span-2" bodyClassName="p-3">
              {performance.data && performance.data.equity_curve.length > 1 ? (
                <div className="h-64">
                  <ResponsiveContainer width="100%" height="100%">
                    <AreaChart data={performance.data.equity_curve}>
                      <defs>
                        <linearGradient id="equityFill" x1="0" y1="0" x2="0" y2="1">
                          <stop offset="0%" stopColor="#3b82f6" stopOpacity={0.35} />
                          <stop offset="100%" stopColor="#3b82f6" stopOpacity={0} />
                        </linearGradient>
                      </defs>
                      <CartesianGrid stroke="#1f2937" vertical={false} />
                      <XAxis dataKey="date" tick={{ fontSize: 10, fill: '#5c6b7d' }} stroke="#1f2937" />
                      <YAxis
                        tick={{ fontSize: 10, fill: '#5c6b7d' }} stroke="#1f2937"
                        domain={['auto', 'auto']} width={70}
                        tickFormatter={(v) => Intl.NumberFormat(undefined, { notation: 'compact' }).format(v)}
                      />
                      <Tooltip
                        contentStyle={{
                          background: '#121821', border: '1px solid #1f2937',
                          borderRadius: 6, fontSize: 12,
                        }}
                        labelStyle={{ color: '#8b98a9' }}
                      />
                      <Area
                        type="monotone" dataKey="equity" stroke="#3b82f6"
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
              <p className="mt-2 text-2xs text-ink-faint">
                LIVE performance from recorded portfolio snapshots — distinct from
                backtest results.
              </p>
            </Panel>

            <Panel title="Allocation">
              {Object.keys(pf.sector_allocation).length === 0 && (
                <Empty message="No open positions" />
              )}
              <ul className="space-y-2">
                {Object.entries(pf.sector_allocation).map(([sector, weight]) => (
                  <li key={sector}>
                    <div className="flex items-baseline justify-between text-sm">
                      <span className="truncate">{sector}</span>
                      <span className="font-mono tabular-nums text-ink-muted">
                        {(weight * 100).toFixed(1)}%
                      </span>
                    </div>
                    <div className="mt-1 h-1.5 rounded-full bg-ground-overlay overflow-hidden">
                      <div
                        className={`h-full ${weight > 0.3 ? 'bg-warn' : 'bg-accent'}`}
                        style={{ width: `${Math.min(weight * 100, 100)}%` }}
                      />
                    </div>
                  </li>
                ))}
              </ul>
              {performance.data?.correlation?.average_absolute_correlation !== undefined && (
                <p className="mt-3 text-2xs text-ink-faint">
                  Average absolute correlation{' '}
                  {performance.data.correlation.average_absolute_correlation.toFixed(2)} —{' '}
                  {performance.data.correlation.diversification_note}
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
                            <span className="ml-1 text-2xs text-bear">STALE</span>
                          )}
                        </td>
                        <td className="text-ink-muted">{position.sector ?? '--'}</td>
                        <td className="text-right font-mono tabular-nums">{formatNumber(position.quantity, 2)}</td>
                        <td className="text-right font-mono tabular-nums">{formatNumber(position.average_cost)}</td>
                        <td className="text-right font-mono tabular-nums">{formatNumber(position.current_price)}</td>
                        <td className="text-right font-mono tabular-nums">{formatNumber(position.market_value, 0)}</td>
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
                        <td className="text-2xs text-ink-faint">{relativeTime(position.opened_at)}</td>
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
                        <td className="text-2xs text-ink-faint">{relativeTime(trade.executed_at)}</td>
                        <td className="font-medium">{trade.symbol}</td>
                        <td>
                          <span className={`chip ${trade.side === 'BUY' ? 'bg-bull-soft text-bull' : 'bg-bear-soft text-bear'}`}>
                            {trade.side}
                          </span>
                        </td>
                        <td className="text-right font-mono tabular-nums">{formatNumber(trade.quantity, 2)}</td>
                        <td className="text-right font-mono tabular-nums">{formatNumber(trade.price)}</td>
                        <td className="text-right font-mono tabular-nums text-ink-muted">
                          {formatNumber(trade.reference_price)}
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
                        <td className="text-2xs text-ink-muted">{trade.exit_reason ?? '--'}</td>
                        <td className="text-2xs text-ink-faint">{trade.mode}</td>
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

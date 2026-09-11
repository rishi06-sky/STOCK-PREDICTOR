'use client';

import { useState } from 'react';
import useSWR from 'swr';
import {
  CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts';
import { fetcher } from '@/lib/api';
import { directionClass, formatCurrency, formatNumber, formatPercent, relativeTime } from '@/lib/format';
import { Disclaimer, Empty, Loading, Panel, Stat } from '@/components/ui';

interface Backtest {
  id: number; name: string; start_date: string; end_date: string;
  initial_capital: number; data_quality: string;
  total_return: number | null; cagr: number | null; sharpe_ratio: number | null;
  sortino_ratio: number | null; max_drawdown: number | null; win_rate: number | null;
  profit_factor: number | null; total_trades: number | null; volatility: number | null;
  benchmark_return: number | null; completed_at: string | null;
}

interface BacktestDetail {
  backtest: Backtest;
  equity_curve: { date: string; equity: number }[] | null;
  config: Record<string, unknown> | null;
  trades: {
    entry_date: string; exit_date: string; entry_price: number;
    exit_price: number | null; net_pnl: number | null; return_pct: number | null;
    holding_days: number | null; exit_reason: string | null;
  }[];
  disclaimer: string;
}

export default function BacktestsPage() {
  const list = useSWR<Backtest[]>('/backtests', fetcher);
  const [selected, setSelected] = useState<number | null>(null);
  const detail = useSWR<BacktestDetail>(selected ? `/backtests/${selected}` : null, fetcher);

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-lg font-semibold tracking-tight">Backtests</h1>
        <p className="text-2xs text-ink-faint">
          Simulated historical results. Backtest performance is not live performance.
        </p>
      </div>

      {list.isLoading && <Panel><Loading /></Panel>}
      {list.data?.length === 0 && (
        <Panel>
          <Empty
            message="No backtests have been run"
            hint="Start one via POST /api/v1/backtests. It replays recorded model predictions over the chosen period."
          />
        </Panel>
      )}

      {!!list.data?.length && (
        <Panel title="Runs" bodyClassName="p-0">
          <div className="w-full max-w-full overflow-x-auto">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Name</th><th>Period</th><th>Data</th>
                  <th className="text-right">Return</th>
                  <th className="text-right">CAGR</th>
                  <th className="text-right">Sharpe</th>
                  <th className="text-right">Sortino</th>
                  <th className="text-right">Max DD</th>
                  <th className="text-right">Win rate</th>
                  <th className="text-right">Profit factor</th>
                  <th className="text-right">Trades</th>
                  <th>Completed</th><th></th>
                </tr>
              </thead>
              <tbody>
                {list.data.map((backtest) => (
                  <tr key={backtest.id}>
                    <td className="font-medium">{backtest.name}</td>
                    <td className="text-2xs text-ink-muted">
                      {backtest.start_date} → {backtest.end_date}
                    </td>
                    <td>
                      <span
                        className={`chip ${
                          backtest.data_quality === 'SYNTHETIC'
                            ? 'bg-warn-soft text-warn'
                            : 'bg-ground-overlay text-ink-muted'
                        }`}
                      >
                        {backtest.data_quality}
                      </span>
                    </td>
                    <td className={`text-right font-mono tabular-nums ${directionClass(backtest.total_return)}`}>
                      {formatPercent(backtest.total_return)}
                    </td>
                    <td className={`text-right font-mono tabular-nums ${directionClass(backtest.cagr)}`}>
                      {formatPercent(backtest.cagr)}
                    </td>
                    <td className="text-right font-mono tabular-nums">{formatNumber(backtest.sharpe_ratio)}</td>
                    <td className="text-right font-mono tabular-nums">{formatNumber(backtest.sortino_ratio)}</td>
                    <td className="text-right font-mono tabular-nums text-bear">
                      {formatPercent(backtest.max_drawdown)}
                    </td>
                    <td className="text-right font-mono tabular-nums">
                      {backtest.win_rate === null ? '--' : `${(backtest.win_rate * 100).toFixed(1)}%`}
                    </td>
                    <td className="text-right font-mono tabular-nums">{formatNumber(backtest.profit_factor)}</td>
                    <td className="text-right font-mono tabular-nums">{backtest.total_trades ?? '--'}</td>
                    <td className="text-2xs text-ink-faint">
                      {backtest.completed_at ? relativeTime(backtest.completed_at) : 'running…'}
                    </td>
                    <td>
                      <button
                        className="text-2xs text-accent hover:underline"
                        onClick={() => setSelected(backtest.id === selected ? null : backtest.id)}
                      >
                        {selected === backtest.id ? 'Hide' : 'Open'}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
      )}

      {selected && detail.data && (
        <>
          <Panel title="Equity curve (BACKTEST)" bodyClassName="p-3">
            {detail.data.equity_curve && detail.data.equity_curve.length > 1 ? (
              <div className="h-72">
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={detail.data.equity_curve}>
                    <CartesianGrid stroke="#1f2937" vertical={false} />
                    <XAxis dataKey="date" tick={{ fontSize: 10, fill: '#5c6b7d' }} stroke="#1f2937" />
                    <YAxis
                      tick={{ fontSize: 10, fill: '#5c6b7d' }} stroke="#1f2937" width={70}
                      domain={['auto', 'auto']}
                      tickFormatter={(v) => Intl.NumberFormat(undefined, { notation: 'compact' }).format(v)}
                    />
                    <Tooltip
                      contentStyle={{
                        background: '#121821', border: '1px solid #1f2937',
                        borderRadius: 6, fontSize: 12,
                      }}
                    />
                    <Line type="monotone" dataKey="equity" stroke="#3b82f6" dot={false} strokeWidth={1.5} />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            ) : (
              <Empty message="No equity curve recorded for this run" />
            )}
            <p className="mt-2 text-2xs text-warn">{detail.data.disclaimer}</p>
          </Panel>

          <Panel title="Trades" bodyClassName="p-0">
            {detail.data.trades.length === 0 && <Empty message="No trades in this run" />}
            {detail.data.trades.length > 0 && (
              <div className="max-h-96 overflow-auto">
                <table className="data-table">
                  <thead className="sticky top-0 bg-ground-raised">
                    <tr>
                      <th>Entry</th><th>Exit</th>
                      <th className="text-right">Entry px</th>
                      <th className="text-right">Exit px</th>
                      <th className="text-right">Net P&L</th>
                      <th className="text-right">Return</th>
                      <th className="text-right">Days</th>
                      <th>Reason</th>
                    </tr>
                  </thead>
                  <tbody>
                    {detail.data.trades.slice(0, 200).map((trade, index) => (
                      <tr key={index}>
                        <td className="text-2xs">{trade.entry_date}</td>
                        <td className="text-2xs">{trade.exit_date}</td>
                        <td className="text-right font-mono tabular-nums">{formatNumber(trade.entry_price)}</td>
                        <td className="text-right font-mono tabular-nums">{formatNumber(trade.exit_price)}</td>
                        <td className={`text-right font-mono tabular-nums ${directionClass(trade.net_pnl)}`}>
                          {formatNumber(trade.net_pnl)}
                        </td>
                        <td className={`text-right font-mono tabular-nums ${directionClass(trade.return_pct)}`}>
                          {formatPercent(trade.return_pct)}
                        </td>
                        <td className="text-right font-mono tabular-nums">{trade.holding_days ?? '--'}</td>
                        <td className="text-2xs text-ink-muted">{trade.exit_reason ?? '--'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <div className="border-t border-line px-4 py-3">
              <Disclaimer>
                Fills are modelled at the next bar&apos;s open with commission and adverse
                slippage applied. Where a bar spans both stop and target, the stop is
                assumed to fill first.
              </Disclaimer>
            </div>
          </Panel>
        </>
      )}
    </div>
  );
}

'use client';

import Link from 'next/link';
import { useMemo, useState } from 'react';
import useSWR from 'swr';
import { fetcher, type Page, type Signal } from '@/lib/api';
import { directionClass, formatCurrency, formatNumber, formatPercent, relativeTime } from '@/lib/format';
import {
  Confidence, Disclaimer, Empty, ErrorBox, Loading, Panel, QualityBadge,
  RiskBadge, SignalBadge,
} from '@/components/ui';

type SortKey = 'opportunity_score' | 'confidence' | 'expected_return' | 'symbol';

export default function OpportunitiesPage() {
  const [direction, setDirection] = useState<'all' | 'long' | 'short'>('all');
  const [risk, setRisk] = useState('');
  const [exchange, setExchange] = useState('');
  const [minConfidence, setMinConfidence] = useState(0);
  const [sortKey, setSortKey] = useState<SortKey>('opportunity_score');
  const [query, setQuery] = useState('');

  const params = new URLSearchParams({ limit: '100', direction });
  if (risk) params.set('risk_level', risk);
  if (exchange) params.set('exchange', exchange);
  if (minConfidence > 0) params.set('min_confidence', String(minConfidence));

  const { data, error, isLoading } = useSWR<Page<Signal>>(
    `/opportunities?${params}`, fetcher, { refreshInterval: 60000 },
  );

  const rows = useMemo(() => {
    const items = (data?.items ?? []).filter((signal) => {
      if (!query.trim()) return true;
      const needle = query.trim().toLowerCase();
      return (
        signal.symbol.toLowerCase().includes(needle) ||
        signal.name.toLowerCase().includes(needle) ||
        (signal.sector ?? '').toLowerCase().includes(needle)
      );
    });
    return [...items].sort((a, b) => {
      if (sortKey === 'symbol') return a.symbol.localeCompare(b.symbol);
      return (b[sortKey] ?? 0) - (a[sortKey] ?? 0);
    });
  }, [data, query, sortKey]);

  return (
    <div className="space-y-4">
      <div className="flex items-baseline justify-between gap-4">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">Opportunities</h1>
          <p className="text-2xs text-ink-faint">
            Active signals ranked by risk-adjusted opportunity score.
          </p>
        </div>
        <span className="text-2xs text-ink-faint">
          {data ? `${rows.length} of ${data.total}` : ''}
        </span>
      </div>

      <Panel bodyClassName="p-3">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-5">
          <div>
            <label className="stat-label" htmlFor="search">Search</label>
            <input
              id="search" className="input mt-1" placeholder="symbol, name or sector"
              value={query} onChange={(e) => setQuery(e.target.value)}
            />
          </div>
          <div>
            <label className="stat-label" htmlFor="direction">Direction</label>
            <select
              id="direction" className="input mt-1" value={direction}
              onChange={(e) => setDirection(e.target.value as typeof direction)}
            >
              <option value="all">All</option>
              <option value="long">Long only</option>
              <option value="short">Short only</option>
            </select>
          </div>
          <div>
            <label className="stat-label" htmlFor="risk">Risk</label>
            <select id="risk" className="input mt-1" value={risk} onChange={(e) => setRisk(e.target.value)}>
              <option value="">Any</option>
              <option value="LOW">Low</option>
              <option value="MODERATE">Moderate</option>
              <option value="HIGH">High</option>
              <option value="VERY_HIGH">Very high</option>
            </select>
          </div>
          <div>
            <label className="stat-label" htmlFor="exchange">Exchange</label>
            <select
              id="exchange" className="input mt-1" value={exchange}
              onChange={(e) => setExchange(e.target.value)}
            >
              <option value="">All</option>
              <option value="NSE">NSE</option>
              <option value="BSE">BSE</option>
            </select>
          </div>
          <div>
            <label className="stat-label" htmlFor="confidence">
              Min confidence: {(minConfidence * 100).toFixed(0)}%
            </label>
            <input
              id="confidence" type="range" min={0} max={0.95} step={0.05}
              value={minConfidence} className="mt-3 w-full accent-accent"
              onChange={(e) => setMinConfidence(Number(e.target.value))}
            />
          </div>
        </div>
      </Panel>

      <Panel bodyClassName="p-0">
        {isLoading && <Loading />}
        {error && <div className="p-4"><ErrorBox message={String(error)} /></div>}
        {data && rows.length === 0 && (
          <Empty
            message="No signals match these filters"
            hint="The engine returns nothing rather than forcing a low-confidence call."
          />
        )}
        {rows.length > 0 && (
          <div className="w-full max-w-full overflow-x-auto">
            <table className="data-table">
              <thead>
                <tr>
                  <th>#</th>
                  <th>
                    <button onClick={() => setSortKey('symbol')} className="hover:text-ink">Symbol</button>
                  </th>
                  <th>Signal</th>
                  <th>
                    <button onClick={() => setSortKey('confidence')} className="hover:text-ink">Confidence</button>
                  </th>
                  <th>
                    <button onClick={() => setSortKey('opportunity_score')} className="hover:text-ink">Score</button>
                  </th>
                  <th className="text-right">Price</th>
                  <th className="text-right">Entry zone</th>
                  <th className="text-right">Stop</th>
                  <th className="text-right">Target</th>
                  <th className="text-right">R:R</th>
                  <th>
                    <button onClick={() => setSortKey('expected_return')} className="hover:text-ink">Exp. return</button>
                  </th>
                  <th>Risk</th>
                  <th>Horizon</th>
                  <th>Data</th>
                  <th>Age</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((signal, index) => (
                  <tr key={signal.id}>
                    <td className="font-mono text-ink-faint">{index + 1}</td>
                    <td>
                      <Link href={`/stock/${signal.symbol}`} className="font-medium hover:text-accent">
                        {signal.symbol}
                      </Link>
                      <div className="text-2xs text-ink-faint">{signal.sector ?? signal.exchange}</div>
                    </td>
                    <td><SignalBadge signal={signal.signal} /></td>
                    <td><Confidence value={signal.confidence} /></td>
                    <td className="font-mono tabular-nums">
                      {signal.opportunity_score?.toFixed(1) ?? '--'}
                    </td>
                    <td className="text-right font-mono tabular-nums">
                      {formatCurrency(signal.reference_price)}
                    </td>
                    <td className="text-right font-mono tabular-nums text-ink-muted">
                      {signal.entry_low && signal.entry_high
                        ? `${formatNumber(signal.entry_low)}–${formatNumber(signal.entry_high)}`
                        : '--'}
                    </td>
                    <td className="text-right font-mono tabular-nums text-bear">
                      {formatNumber(signal.stop_loss)}
                    </td>
                    <td className="text-right font-mono tabular-nums text-bull">
                      {formatNumber(signal.take_profit)}
                    </td>
                    <td className="text-right font-mono tabular-nums">
                      {signal.reward_to_risk ? `${signal.reward_to_risk.toFixed(1)}:1` : '--'}
                    </td>
                    <td className={`font-mono tabular-nums ${directionClass(signal.expected_return)}`}>
                      {formatPercent(signal.expected_return)}
                    </td>
                    <td><RiskBadge level={signal.risk_level} /></td>
                    <td className="font-mono tabular-nums text-ink-muted">{signal.horizon_days}d</td>
                    <td><QualityBadge quality={signal.data_quality} stale={false} /></td>
                    <td className="text-2xs text-ink-faint">{relativeTime(signal.generated_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <div className="border-t border-line px-4 py-3">
          <Disclaimer>
            Expected return is an estimate derived from the model&apos;s edge scaled by
            volatility, with wide error bars. Entry, stop and target are volatility-derived
            suggestions, not price forecasts.
          </Disclaimer>
        </div>
      </Panel>
    </div>
  );
}

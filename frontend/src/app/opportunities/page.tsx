'use client';

import Link from 'next/link';
import { useMemo, useState } from 'react';
import useSWR from 'swr';
import { CaretDown } from '@phosphor-icons/react/ssr';
import { fetcher, type Page, type Signal } from '@/lib/api';
import { directionClass, formatCurrency, formatNumber, formatPercent, relativeTime } from '@/lib/format';
import {
  Confidence, Disclaimer, Empty, ErrorBox, Loading, PageHeader, Panel, QualityBadge,
  RiskBadge, SignalBadge,
} from '@/components/ui';

type SortKey = 'opportunity_score' | 'confidence' | 'expected_return' | 'symbol';

function SortButton({
  active, onClick, children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={`inline-flex items-center gap-1 transition-colors hover:text-ink ${active ? 'text-ink' : ''}`}
    >
      {children}
      <CaretDown size={10} weight="bold" aria-hidden className={active ? 'opacity-100' : 'opacity-0'} />
    </button>
  );
}

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
    <div className="space-y-5">
      <PageHeader
        title="Opportunities"
        description="Active signals ranked by risk-adjusted opportunity score."
        meta={data ? `${rows.length} of ${data.total}` : undefined}
      />

      <Panel bodyClassName="p-4">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-5">
          <div>
            <label className="stat-label" htmlFor="search">Search</label>
            <input
              id="search" className="input mt-1.5" placeholder="symbol, name or sector"
              value={query} onChange={(e) => setQuery(e.target.value)}
            />
          </div>
          <div>
            <label className="stat-label" htmlFor="direction">Direction</label>
            <select
              id="direction" className="input mt-1.5" value={direction}
              onChange={(e) => setDirection(e.target.value as typeof direction)}
            >
              <option value="all">All</option>
              <option value="long">Long only</option>
              <option value="short">Short only</option>
            </select>
          </div>
          <div>
            <label className="stat-label" htmlFor="risk">Risk</label>
            <select id="risk" className="input mt-1.5" value={risk} onChange={(e) => setRisk(e.target.value)}>
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
              id="exchange" className="input mt-1.5" value={exchange}
              onChange={(e) => setExchange(e.target.value)}
            >
              <option value="">All</option>
              <option value="NSE">NSE</option>
              <option value="BSE">BSE</option>
            </select>
          </div>
          <div>
            <label className="stat-label" htmlFor="confidence">
              Min confidence{' '}
              <span className="font-mono tabular-nums text-ink">{(minConfidence * 100).toFixed(0)}%</span>
            </label>
            <input
              id="confidence" type="range" min={0} max={0.95} step={0.05}
              value={minConfidence} className="mt-4 w-full accent-accent"
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
                    <SortButton active={sortKey === 'symbol'} onClick={() => setSortKey('symbol')}>Symbol</SortButton>
                  </th>
                  <th>Signal</th>
                  <th>
                    <SortButton active={sortKey === 'confidence'} onClick={() => setSortKey('confidence')}>Confidence</SortButton>
                  </th>
                  <th>
                    <SortButton active={sortKey === 'opportunity_score'} onClick={() => setSortKey('opportunity_score')}>Score</SortButton>
                  </th>
                  <th className="text-right">Price</th>
                  <th className="text-right">Entry zone</th>
                  <th className="text-right">Stop</th>
                  <th className="text-right">Target</th>
                  <th className="text-right">R:R</th>
                  <th>
                    <SortButton active={sortKey === 'expected_return'} onClick={() => setSortKey('expected_return')}>Exp. return</SortButton>
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
                      <div className="text-xs text-ink-faint">{signal.sector ?? signal.exchange}</div>
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
                        ? `${formatNumber(signal.entry_low)} to ${formatNumber(signal.entry_high)}`
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
                    <td className="text-xs text-ink-faint">{relativeTime(signal.generated_at)}</td>
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

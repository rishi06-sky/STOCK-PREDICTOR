'use client';

import { useParams } from 'next/navigation';
import { useMemo, useState } from 'react';
import useSWR from 'swr';
import {
  Bar, CartesianGrid, ComposedChart, Legend, Line, ResponsiveContainer,
  Tooltip, XAxis, YAxis,
} from 'recharts';
import { fetcher, type DataQuality, type Rationale, type SignalKind } from '@/lib/api';
import { directionClass, formatCompact, formatCurrency, formatDateTime, formatNumber, formatPercent, humanize, relativeTime } from '@/lib/format';
import {
  Confidence, DirectionIcon, Disclaimer, Empty, ErrorBox, Loading, Panel, QualityBadge,
  RiskBadge, SignalBadge, Stat,
} from '@/components/ui';
import { tooltipStyle, useChartTheme } from '@/hooks/useChartTheme';

interface Analysis {
  security: {
    id: number; symbol: string; name: string; exchange: string;
    sector: string | null; industry: string | null; currency: string; asset_type: string;
  };
  market_open: boolean;
  quote: {
    price: number; change: number | null; change_pct: number | null;
    quality: DataQuality; source_timestamp: string; provider: string;
  } | null;
  technical: Record<string, number | null>;
  fundamentals: {
    score: number | null; coverage: number; reliable: boolean;
    metrics_used: string[]; metrics_missing: string[]; notes: string[];
    peer_comparisons: {
      metric: string; value: number | null; peer_median: number | null;
      percentile: number | null; peer_count: number; better_than_peers: boolean | null;
    }[];
  };
  signal: {
    signal: SignalKind; confidence: number; expected_return: number | null;
    risk_level: string; horizon_days: number; reference_price: number;
    entry_low: number | null; entry_high: number | null;
    stop_loss: number | null; take_profit: number | null;
    reward_to_risk: number | null; regime: string | null;
    data_quality: DataQuality; price_as_of: string; generated_at: string;
    expires_at: string; model_version: string | null; rationale: Rationale[];
  } | null;
  sentiment: {
    score: number; confidence: number; count: number; label: string;
    window_days?: number; note?: string;
  };
  news: {
    headline: string; source: string; url: string | null;
    published_at: string; retrieved_at: string; is_breaking: boolean;
    sentiment: { label: string; score: number; confidence: number } | null;
  }[];
  history_bars: number;
}

interface Candle {
  trade_date: string; open: number; high: number; low: number;
  close: number; volume: number; quality: string;
}

const RANGES = [
  { label: '1M', days: 30 }, { label: '3M', days: 90 },
  { label: '6M', days: 180 }, { label: '1Y', days: 365 },
  { label: '2Y', days: 730 },
];

const CONTRIBUTION_DIRECTION = {
  bullish: 'up', bearish: 'down', neutral: 'neutral',
} as const;

const SENTIMENT_DIRECTION = {
  POSITIVE: 'up', NEGATIVE: 'down',
} as const;

export default function StockPage() {
  const params = useParams<{ symbol: string }>();
  const symbol = String(params.symbol).toUpperCase();
  const [range, setRange] = useState(180);
  const [showSMA, setShowSMA] = useState(true);
  const [showVolume, setShowVolume] = useState(true);
  const chart = useChartTheme();

  const analysis = useSWR<Analysis>(`/securities/${symbol}/analysis`, fetcher, {
    refreshInterval: 60000,
  });
  const history = useSWR<Candle[]>(`/market/history/${symbol}?days=${range}`, fetcher);

  // Moving averages are computed here rather than fetched: the chart is the
  // only consumer, and it keeps the payload small.
  const chartData = useMemo(() => {
    const bars = history.data ?? [];
    return bars.map((bar, index) => {
      const windowFor = (n: number) =>
        index + 1 >= n
          ? bars.slice(index + 1 - n, index + 1).reduce((sum, b) => sum + b.close, 0) / n
          : null;
      return {
        date: bar.trade_date,
        close: bar.close,
        volume: bar.volume,
        sma20: windowFor(20),
        sma50: windowFor(50),
      };
    });
  }, [history.data]);

  if (analysis.error) {
    return <ErrorBox message={`Could not load ${symbol}: ${analysis.error}`} />;
  }
  if (analysis.isLoading || !analysis.data) {
    return (
      <div className="space-y-5">
        <Panel><Loading label={`Loading ${symbol}`} rows={2} /></Panel>
        <div className="skeleton h-80 w-full rounded-lg" />
      </div>
    );
  }

  const { security, quote, technical, signal, fundamentals, sentiment, news } = analysis.data;

  return (
    <div className="space-y-5">
      {/* ------------------------------------------------------------ header */}
      <Panel bodyClassName="p-5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="mr-1 text-2xl font-semibold tracking-tight">{security.symbol}</h1>
              <span className="chip bg-ground-overlay text-ink-muted">{security.exchange}</span>
              {security.sector && (
                <span className="chip bg-ground-overlay text-ink-muted">{security.sector}</span>
              )}
              <span
                className={`chip ${
                  analysis.data.market_open ? 'bg-bull-soft text-bull' : 'bg-ground-overlay text-ink-muted'
                }`}
              >
                {analysis.data.market_open ? 'MARKET OPEN' : 'MARKET CLOSED'}
              </span>
            </div>
            <p className="mt-1.5 text-sm text-ink-muted">{security.name}</p>
            {security.industry && (
              <p className="mt-0.5 text-xs text-ink-faint">{security.industry}</p>
            )}
          </div>

          {quote && (
            <div className="text-right">
              <div className="font-mono text-3xl tabular-nums tracking-tight">{formatCurrency(quote.price, security.currency)}</div>
              <div className={`font-mono text-sm tabular-nums ${directionClass(quote.change_pct)}`}>
                {formatNumber(quote.change)} ({formatPercent(quote.change_pct, 2, { alreadyPercent: true })})
              </div>
              <div className="mt-2 flex items-center justify-end gap-2">
                <span className="text-xs text-ink-faint">via {quote.provider}</span>
                <QualityBadge quality={quote.quality} asOf={quote.source_timestamp} />
              </div>
            </div>
          )}
        </div>
      </Panel>

      {/* ------------------------------------------------------------- chart */}
      <Panel
        title="Price history"
        bodyClassName="p-4"
        actions={
          <div className="flex flex-wrap items-center justify-end gap-x-4 gap-y-2">
            <label className="flex cursor-pointer items-center gap-1.5 text-xs text-ink-muted">
              <input type="checkbox" className="h-3.5 w-3.5 accent-accent" checked={showSMA}
                onChange={(e) => setShowSMA(e.target.checked)} />
              SMA
            </label>
            <label className="flex cursor-pointer items-center gap-1.5 text-xs text-ink-muted">
              <input type="checkbox" className="h-3.5 w-3.5 accent-accent" checked={showVolume}
                onChange={(e) => setShowVolume(e.target.checked)} />
              Volume
            </label>
            <div role="group" aria-label="Range" className="flex rounded-md border border-line p-0.5">
              {RANGES.map((option) => (
                <button
                  key={option.label}
                  type="button"
                  aria-pressed={range === option.days}
                  onClick={() => setRange(option.days)}
                  className={`rounded px-2 py-0.5 font-mono text-xs transition-colors ${
                    range === option.days
                      ? 'bg-ground-overlay text-ink'
                      : 'text-ink-muted hover:text-ink'
                  }`}
                >
                  {option.label}
                </button>
              ))}
            </div>
          </div>
        }
      >
        {history.isLoading && <div className="skeleton h-80 w-full" />}
        {chartData.length === 0 && !history.isLoading && (
          <Empty message="No price history stored for this security" />
        )}
        {chartData.length > 0 && (
          <div className="h-80">
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={chartData}>
                <CartesianGrid stroke={chart.grid} vertical={false} />
                <XAxis
                  dataKey="date" tick={{ fontSize: 11, fill: chart.tick }} stroke={chart.axis}
                  tickLine={false} minTickGap={48}
                />
                <YAxis
                  yAxisId="price" domain={['auto', 'auto']} width={68}
                  tick={{ fontSize: 11, fill: chart.tick }} axisLine={false} tickLine={false}
                />
                {showVolume && (
                  <YAxis
                    yAxisId="volume" orientation="right" width={48} domain={[0, (max: number) => max * 4]}
                    tick={{ fontSize: 11, fill: chart.tick }} axisLine={false} tickLine={false}
                    tickFormatter={(v) => formatCompact(v)}
                  />
                )}
                <Tooltip
                  {...tooltipStyle(chart)}
                  formatter={(value: number, name: string) => [
                    name === 'volume' ? formatCompact(value) : formatNumber(value),
                    name,
                  ]}
                />
                <Legend wrapperStyle={{ fontSize: 12, color: chart.muted, paddingTop: 8 }} iconSize={10} />
                {showVolume && (
                  <Bar yAxisId="volume" dataKey="volume" fill={chart.volume} fillOpacity={0.6} name="volume" />
                )}
                <Line yAxisId="price" type="monotone" dataKey="close" stroke={chart.accent}
                  dot={false} strokeWidth={1.6} name="close" />
                {showSMA && (
                  <>
                    <Line yAxisId="price" type="monotone" dataKey="sma20" stroke={chart.muted}
                      dot={false} strokeWidth={1} name="SMA 20" connectNulls />
                    <Line yAxisId="price" type="monotone" dataKey="sma50" stroke={chart.warn}
                      dot={false} strokeWidth={1} name="SMA 50" connectNulls />
                  </>
                )}
                {signal?.stop_loss && (
                  <Line yAxisId="price" dataKey={() => signal.stop_loss} stroke={chart.bear}
                    dot={false} strokeDasharray="4 4" strokeWidth={1} name="stop" />
                )}
                {signal?.take_profit && (
                  <Line yAxisId="price" dataKey={() => signal.take_profit} stroke={chart.bull}
                    dot={false} strokeDasharray="4 4" strokeWidth={1} name="target" />
                )}
              </ComposedChart>
            </ResponsiveContainer>
          </div>
        )}
      </Panel>

      <div className="grid grid-cols-1 gap-5 lg:grid-cols-3">
        {/* ------------------------------------------------------- signal */}
        <Panel title="Signal" className="lg:col-span-2">
          {!signal && (
            <Empty
              message="No active signal for this security"
              hint="The engine emits nothing when confidence is below the floor or no validated model covers this horizon."
            />
          )}
          {signal && (
            <>
              <div className="flex flex-wrap items-center gap-3">
                <SignalBadge signal={signal.signal} />
                <Confidence value={signal.confidence} />
                <RiskBadge level={signal.risk_level} />
                <QualityBadge quality={signal.data_quality} asOf={signal.price_as_of} />
              </div>
              <dl className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-xs">
                <div className="flex gap-1.5"><dt className="text-ink-faint">Horizon</dt><dd className="font-mono tabular-nums">{signal.horizon_days}d</dd></div>
                <div className="flex gap-1.5"><dt className="text-ink-faint">Model</dt><dd className="font-mono">{signal.model_version ?? '--'}</dd></div>
                {signal.regime && (
                  <div className="flex gap-1.5"><dt className="text-ink-faint">Regime</dt><dd className="font-mono">{signal.regime}</dd></div>
                )}
              </dl>

              <div className="mt-5 grid grid-cols-2 gap-x-4 gap-y-5 border-t border-line pt-4 sm:grid-cols-4">
                <Stat
                  label="Entry zone"
                  value={
                    signal.entry_low && signal.entry_high
                      ? `${formatNumber(signal.entry_low)} to ${formatNumber(signal.entry_high)}`
                      : '--'
                  }
                />
                <Stat label="Stop loss" value={formatNumber(signal.stop_loss)} tone="bear" />
                <Stat label="Take profit" value={formatNumber(signal.take_profit)} tone="bull" />
                <Stat
                  label="Reward : risk"
                  value={signal.reward_to_risk ? `${signal.reward_to_risk.toFixed(2)}:1` : '--'}
                  sub={`expected ${formatPercent(signal.expected_return)}`}
                />
              </div>

              <h3 className="mb-2 mt-6 text-xs font-medium text-ink-muted">Why this signal</h3>
              <ul className="space-y-2">
                {signal.rationale.map((item, index) => (
                  <li key={index} className="flex items-start gap-2 text-sm">
                    <DirectionIcon
                      direction={CONTRIBUTION_DIRECTION[item.contribution] ?? 'neutral'}
                      className="mt-[3px]"
                    />
                    <span>
                      <span className="text-ink-muted">{item.factor}:</span> {item.detail}
                    </span>
                  </li>
                ))}
              </ul>

              <p className="mt-5 text-xs text-ink-faint">
                Generated {relativeTime(signal.generated_at)}, expires{' '}
                {formatDateTime(signal.expires_at)}
              </p>
              <div className="mt-2"><Disclaimer /></div>
            </>
          )}
        </Panel>

        {/* ---------------------------------------------------- technical */}
        <Panel title="Technical indicators">
          {Object.keys(technical).length === 0 && (
            <Empty message="Not enough history to compute indicators" />
          )}
          {Object.keys(technical).length > 0 && (
            <dl className="grid grid-cols-2 gap-x-5 gap-y-2.5 text-sm">
              {[
                ['RSI (14)', technical.rsi], ['MACD', technical.macd],
                ['MACD signal', technical.macd_signal], ['ADX (14)', technical.adx],
                ['ATR (14)', technical.atr], ['SMA 20', technical.sma_20],
                ['SMA 50', technical.sma_50], ['SMA 200', technical.sma_200],
                ['EMA 20', technical.ema_20], ['VWAP (20)', technical.vwap_20],
                ['BB upper', technical.bb_upper], ['BB lower', technical.bb_lower],
                ['Support', technical.support], ['Resistance', technical.resistance],
                ['Realised vol', technical.realized_volatility],
              ].map(([label, value]) => (
                <div key={String(label)} className="flex justify-between gap-2">
                  <dt className="truncate text-ink-muted">{label}</dt>
                  <dd className="font-mono tabular-nums">{formatNumber(value as number)}</dd>
                </div>
              ))}
            </dl>
          )}
        </Panel>
      </div>

      <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
        {/* ------------------------------------------------- fundamentals */}
        <Panel title="Fundamentals">
          {fundamentals.score === null ? (
            <Empty
              message="No fundamentals available for this security"
              hint="Fundamentals require a provider that supplies them; nothing is estimated or filled in."
            />
          ) : (
            <>
              <div className="flex items-center gap-4">
                <Stat
                  label="Peer-relative score" value={`${fundamentals.score.toFixed(0)}/100`}
                  sub={`coverage ${(fundamentals.coverage * 100).toFixed(0)}%`}
                />
                {!fundamentals.reliable && (
                  <span className="chip bg-warn-soft text-warn">LOW COVERAGE, INDICATIVE ONLY</span>
                )}
              </div>
              <div className="-mx-4 mt-3 overflow-x-auto px-4">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Metric</th><th className="text-right">Value</th>
                    <th className="text-right">Peer median</th>
                    <th className="text-right">Percentile</th>
                  </tr>
                </thead>
                <tbody>
                  {fundamentals.peer_comparisons
                    .filter((c) => c.value !== null)
                    .map((comparison) => (
                      <tr key={comparison.metric}>
                        <td className="text-ink-muted">{humanize(comparison.metric)}</td>
                        <td className="text-right font-mono tabular-nums">
                          {formatNumber(comparison.value)}
                        </td>
                        <td className="text-right font-mono tabular-nums text-ink-muted">
                          {formatNumber(comparison.peer_median)}
                        </td>
                        <td
                          className={`text-right font-mono tabular-nums ${
                            comparison.better_than_peers ? 'text-bull' : 'text-ink-muted'
                          }`}
                        >
                          {comparison.percentile === null ? '--' : comparison.percentile.toFixed(0)}
                        </td>
                      </tr>
                    ))}
                </tbody>
              </table>
              </div>
              {fundamentals.notes.length > 0 && (
                <ul className="mt-3 space-y-0.5 text-xs text-ink-faint">
                  {fundamentals.notes.map((note) => (
                    <li key={note}>{note}</li>
                  ))}
                </ul>
              )}
            </>
          )}
        </Panel>

        {/* --------------------------------------------------------- news */}
        <Panel
          title="News & sentiment"
          bodyClassName="p-0"
          actions={
            <span className="text-right text-xs text-ink-faint">
              {sentiment.count > 0 ? (
                <>
                  <span className="text-ink-muted">{humanize(sentiment.label)}</span>{' '}
                  <span className="font-mono tabular-nums">{sentiment.score.toFixed(2)}</span>, confidence{' '}
                  <span className="font-mono tabular-nums">{sentiment.confidence.toFixed(2)}</span>
                </>
              ) : 'No scored articles'}
            </span>
          }
        >
          {news.length === 0 && (
            <Empty message="No news stored for this security" />
          )}
          <ul className="divide-y divide-line/60">
            {news.map((article, index) => (
              <li key={index} className="px-4 py-3">
                <div className="flex items-start gap-2">
                  {article.sentiment && (
                    <DirectionIcon
                      direction={
                        SENTIMENT_DIRECTION[article.sentiment.label as keyof typeof SENTIMENT_DIRECTION]
                          ?? 'neutral'
                      }
                      className="mt-[3px]"
                    />
                  )}
                  <div className="min-w-0">
                    {article.url ? (
                      <a
                        href={article.url} target="_blank" rel="noopener noreferrer"
                        className="text-sm transition-colors hover:text-accent"
                      >
                        {article.headline}
                      </a>
                    ) : (
                      <p className="text-sm">{article.headline}</p>
                    )}
                    <p className="mt-1 flex flex-wrap items-center gap-x-2 text-xs text-ink-faint">
                      {article.is_breaking && (
                        <span className="chip bg-warn-soft text-warn">BREAKING</span>
                      )}
                      <span>{article.source}</span>
                      <span>
                        published {relativeTime(article.published_at)}, retrieved{' '}
                        {relativeTime(article.retrieved_at)}
                      </span>
                    </p>
                  </div>
                </div>
              </li>
            ))}
          </ul>
          {sentiment.note && (
            <p className="border-t border-line px-4 py-3 text-xs text-ink-faint">
              {sentiment.note}
            </p>
          )}
        </Panel>
      </div>
    </div>
  );
}

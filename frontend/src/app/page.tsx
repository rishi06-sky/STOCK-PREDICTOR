'use client';

import Link from 'next/link';
import useSWR from 'swr';
import {
  fetcher, type Alert, type Health, type MarketStatus, type Page,
  type Portfolio, type Quote, type Signal,
} from '@/lib/api';
import { directionClass, formatCurrency, formatNumber, formatPercent, humanize, relativeTime } from '@/lib/format';
import {
  Confidence, Disclaimer, Empty, Loading, Panel, QualityBadge, RiskBadge,
  SignalBadge, Stat, StatusDot, WarningLine,
} from '@/components/ui';
import { useLiveUpdates } from '@/hooks/useLiveUpdates';

export default function DashboardPage() {
  const opportunities = useSWR<Page<Signal>>('/opportunities?limit=8', fetcher, {
    refreshInterval: 60000,
  });
  const portfolio = useSWR<Portfolio>('/portfolio', fetcher, { refreshInterval: 60000 });
  const alerts = useSWR<Page<Alert>>('/alerts?limit=6', fetcher, { refreshInterval: 30000 });
  const health = useSWR<Health>('/health', fetcher, { refreshInterval: 30000 });
  const indices = useSWR<Quote[]>('/market/indices', fetcher, { refreshInterval: 60000 });
  const movers = useSWR<Quote[]>('/market/movers?limit=6', fetcher, { refreshInterval: 60000 });
  const markets = useSWR<MarketStatus[]>('/market/status', fetcher, { refreshInterval: 60000 });

  // A pipeline or alert event means the server has new data; revalidate rather
  // than trusting the socket payload as the source of truth.
  useLiveUpdates((event) => {
    if (event.type === 'alert' || event.type === 'signal') {
      alerts.mutate();
      opportunities.mutate();
    }
  });

  const pf = portfolio.data;

  return (
    <>
    <h1 className="sr-only">Dashboard</h1>
    <div className="space-y-5">

      {/* ---------------------------------------------------- market strip */}
      {!indices.data && <Panel bodyClassName="p-0"><Loading label="Loading indices" rows={2} /></Panel>}
      {indices.data?.length === 0 && (
        <Panel>
          <Empty
            message="No index data yet"
            hint="Run the ingestion pipeline to populate benchmark history."
          />
        </Panel>
      )}
      {!!indices.data?.length && (
        <section
          aria-label="Benchmark indices"
          className="grid grid-cols-1 gap-px overflow-hidden rounded-lg border border-line bg-line sm:grid-cols-2 lg:grid-cols-4"
        >
          {indices.data.map((index) => (
            <div key={index.security_id} className="min-w-0 bg-ground-raised px-4 py-3.5">
              <div className="flex items-baseline justify-between gap-2">
                <span className="truncate text-xs text-ink-muted">{index.symbol}</span>
                <span className={`font-mono text-xs tabular-nums ${directionClass(index.change_pct)}`}>
                  {formatPercent(index.change_pct, 2, { alreadyPercent: true })}
                </span>
              </div>
              <div className="mt-1 flex flex-wrap items-center justify-between gap-2">
                <span className="stat-value text-xl">{formatNumber(index.price)}</span>
                <QualityBadge quality={index.quality} asOf={index.source_timestamp} stale={index.is_stale} />
              </div>
            </div>
          ))}
        </section>
      )}

      <div className="grid grid-cols-1 gap-5 lg:grid-cols-3">
        {/* ------------------------------------------------ opportunities */}
        <Panel
          className="flex flex-col lg:col-span-2"
          title="Top opportunities"
          bodyClassName="flex flex-1 flex-col p-0"
          actions={
            <Link href="/opportunities" className="link">
              View all
            </Link>
          }
        >
          {opportunities.isLoading && <Loading />}
          {opportunities.error && (
            <Empty message="Could not load opportunities" hint={String(opportunities.error)} />
          )}
          {opportunities.data?.items.length === 0 && (
            <Empty
              message="No actionable signals right now"
              hint="The engine emits nothing when confidence is below the floor or no validated model is live."
            />
          )}
          {!!opportunities.data?.items.length && (
            <div className="w-full max-w-full overflow-x-auto">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>#</th><th>Symbol</th><th>Signal</th><th>Confidence</th>
                    <th className="text-right">Price</th>
                    <th className="text-right">Exp. return</th>
                    <th>Risk</th><th className="text-right">R:R</th><th>Data</th>
                  </tr>
                </thead>
                <tbody>
                  {opportunities.data.items.map((signal, index) => (
                    <tr key={signal.id}>
                      <td className="text-ink-faint font-mono">{index + 1}</td>
                      <td>
                        <Link
                          href={`/stock/${signal.symbol}`}
                          className="font-medium hover:text-accent"
                        >
                          {signal.symbol}
                        </Link>
                        <div className="text-xs text-ink-faint">{signal.exchange}</div>
                      </td>
                      <td><SignalBadge signal={signal.signal} /></td>
                      <td><Confidence value={signal.confidence} /></td>
                      <td className="text-right font-mono tabular-nums">
                        {formatCurrency(signal.reference_price)}
                      </td>
                      <td className={`text-right font-mono tabular-nums ${directionClass(signal.expected_return)}`}>
                        {formatPercent(signal.expected_return)}
                      </td>
                      <td><RiskBadge level={signal.risk_level} /></td>
                      <td className="text-right font-mono tabular-nums">
                        {signal.reward_to_risk ? `${signal.reward_to_risk.toFixed(1)}:1` : '--'}
                      </td>
                      <td><QualityBadge quality={signal.data_quality} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <div className="mt-auto border-t border-line px-4 py-3"><Disclaimer /></div>
        </Panel>

        {/* ---------------------------------------------------- portfolio */}
        <div className="space-y-5">
          <Panel
            title="Paper portfolio"
            actions={
              <Link href="/portfolio" className="link">
                Details
              </Link>
            }
          >
            {portfolio.isLoading && <Loading />}
            {pf && (
              <>
                <div className="grid grid-cols-2 gap-x-4 gap-y-5">
                  <Stat
                    label="Equity"
                    size="lg"
                    value={formatCurrency(pf.equity, pf.currency, 0)}
                    sub={`${pf.open_positions} open position${pf.open_positions === 1 ? '' : 's'}`}
                  />
                  <Stat
                    label="Total P&L"
                    size="lg"
                    value={formatCurrency(pf.total_pnl, pf.currency, 0)}
                    sub={formatPercent(pf.total_pnl_pct)}
                    tone={pf.total_pnl > 0 ? 'bull' : pf.total_pnl < 0 ? 'bear' : 'neutral'}
                  />
                  <Stat label="Cash" value={formatCurrency(pf.cash, pf.currency, 0)} />
                  <Stat
                    label="Exposure"
                    value={formatPercent(pf.exposure_pct, 1).replace('+', '')}
                    sub={`drawdown ${formatPercent(pf.drawdown_pct, 1)}`}
                  />
                </div>
                {pf.warnings.length > 0 && (
                  <ul className="mt-4 space-y-1 border-t border-line pt-3 text-xs">
                    {pf.warnings.slice(0, 3).map((warning) => (
                      <WarningLine key={warning}>{warning}</WarningLine>
                    ))}
                  </ul>
                )}
              </>
            )}
          </Panel>

          <Panel title="System health" bodyClassName="p-0">
            {health.data && (
              <ul className="divide-y divide-line/60">
                {health.data.components.map((component) => (
                  <li key={component.component} className="flex items-center gap-2.5 px-4 py-2.5">
                    <StatusDot status={component.status} />
                    <span className="text-sm">{humanize(component.component)}</span>
                    <span className="ml-auto max-w-[55%] truncate text-right text-xs text-ink-faint">
                      {component.detail}
                    </span>
                  </li>
                ))}
              </ul>
            )}
            {!health.data && <Loading rows={5} />}
          </Panel>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-5 lg:grid-cols-3">
        {/* -------------------------------------------------------- movers */}
        <Panel title="Market movers" bodyClassName="p-0" className="lg:col-span-2">
          {!movers.data && <Loading />}
          {movers.data?.length === 0 && <Empty message="No quote data yet" />}
          {!!movers.data?.length && (
            <div className="w-full max-w-full overflow-x-auto">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Symbol</th>
                    <th className="text-right">Price</th>
                    <th className="text-right">Change</th>
                    <th className="text-right">Volume</th>
                    <th>Data</th>
                  </tr>
                </thead>
                <tbody>
                  {movers.data.map((quote) => (
                    <tr key={quote.security_id}>
                      <td>
                        <Link
                          href={`/stock/${quote.symbol}${quote.exchange ? `?exchange=${quote.exchange}` : ''}`}
                          className="font-medium hover:text-accent"
                        >
                          {quote.symbol}
                        </Link>
                        <div className="text-xs text-ink-faint">{quote.exchange}</div>
                      </td>
                      <td className="text-right font-mono tabular-nums">
                        {formatCurrency(quote.price, quote.currency)}
                      </td>
                      <td className={`text-right font-mono tabular-nums ${directionClass(quote.change_pct)}`}>
                        {formatPercent(quote.change_pct, 2, { alreadyPercent: true })}
                      </td>
                      <td className="text-right font-mono tabular-nums text-ink-muted">
                        {quote.volume ? quote.volume.toLocaleString() : '--'}
                      </td>
                      <td>
                        <QualityBadge
                          quality={quote.quality}
                          asOf={quote.source_timestamp}
                          stale={quote.is_stale}
                        />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>

        {/* -------------------------------------------------------- alerts */}
        <Panel
          title="Recent alerts"
          bodyClassName="p-0"
          actions={<Link href="/alerts" className="link">View all</Link>}
        >
          {!alerts.data && <Loading />}
          {alerts.data?.items.length === 0 && <Empty message="No alerts yet" />}
          <ul className="divide-y divide-line/60">
            {alerts.data?.items.map((alert) => (
              <li key={alert.id} className="px-4 py-2.5">
                <div className="flex items-start gap-2">
                  <span
                    role="img"
                    aria-label={alert.severity.toLowerCase()}
                    className={`mt-[7px] h-1.5 w-1.5 shrink-0 rounded-full ${
                      alert.severity === 'CRITICAL' ? 'bg-bear'
                        : alert.severity === 'WARNING' ? 'bg-warn' : 'bg-accent'
                    }`}
                  />
                  <div className="min-w-0">
                    <p className="truncate text-sm">{alert.title}</p>
                    <p className="mt-0.5 text-xs text-ink-faint">{relativeTime(alert.created_at)}</p>
                  </div>
                </div>
              </li>
            ))}
          </ul>
        </Panel>
      </div>

      {/* -------------------------------------------------- market sessions */}
      <Panel title="Market sessions" bodyClassName="p-0">
        <div className="w-full max-w-full overflow-x-auto">
          <table className="data-table">
            <thead>
              <tr>
                <th>Exchange</th><th>Market</th><th>Status</th>
                <th>Local time</th><th>Next open</th>
              </tr>
            </thead>
            <tbody>
              {markets.data?.map((market) => (
                <tr key={market.exchange}>
                  <td className="font-medium">{market.exchange}</td>
                  <td className="text-ink-muted">{market.market}</td>
                  <td>
                    <span className={`chip ${market.is_open ? 'bg-bull-soft text-bull' : 'bg-ground-overlay text-ink-muted'}`}>
                      {market.is_open ? 'OPEN' : 'CLOSED'}
                    </span>
                    <span className="ml-2 text-xs text-ink-faint">{market.reason}</span>
                  </td>
                  <td className="font-mono tabular-nums text-ink-muted">
                    {new Date(market.local_time).toLocaleTimeString()}
                  </td>
                  <td className="font-mono tabular-nums text-ink-muted">
                    {market.next_open ? new Date(market.next_open).toLocaleString() : '--'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
    </div>
    </>
  );
}

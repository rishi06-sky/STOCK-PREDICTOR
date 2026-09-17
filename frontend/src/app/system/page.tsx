'use client';

import { useState } from 'react';
import useSWR from 'swr';
import { api, fetcher, type Health } from '@/lib/api';
import { formatDateTime, relativeTime } from '@/lib/format';
import { Empty, ErrorBox, Loading, Panel, StatusDot } from '@/components/ui';

interface SystemEvent {
  id: number; component: string; event_type: string; severity: string;
  message: string; details: Record<string, unknown> | null; created_at: string;
}

interface TradingStatus {
  allowed: boolean; mode: string; blockers: string[];
  kill_switch_engaged: boolean; paper_starting_cash: number;
  commission_bps: number; slippage_bps: number;
}

const SEVERITY_COLOR: Record<string, string> = {
  CRITICAL: 'text-bear', ERROR: 'text-bear',
  WARNING: 'text-warn', INFO: 'text-ink-muted', DEBUG: 'text-ink-faint',
};

export default function SystemPage() {
  const health = useSWR<Health>('/health/full', fetcher, { refreshInterval: 30000 });
  const events = useSWR<SystemEvent[]>('/system/events?limit=60', fetcher, {
    refreshInterval: 30000,
  });
  const trading = useSWR<TradingStatus>('/system/trading-status', fetcher, {
    refreshInterval: 15000,
  });
  const regimes = useSWR<Record<string, unknown>[]>('/system/regime', fetcher);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  async function toggleKillSwitch(engage: boolean) {
    if (engage && !window.confirm(
      'Engage the kill switch? This blocks every new order immediately, in paper and live mode.',
    )) return;
    setBusy(true);
    setMessage(null);
    try {
      const result = await api.post<{ message: string }>(
        `/system/kill-switch?engage=${engage}`,
      );
      setMessage(result.message);
      trading.mutate();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'Request failed');
    } finally {
      setBusy(false);
    }
  }

  async function runPipeline() {
    setBusy(true);
    setMessage(null);
    try {
      await api.post('/system/pipeline/run');
      setMessage('Pipeline cycle started in the background.');
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'Request failed');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">System health</h1>
          <p className="text-2xs text-ink-faint">
            Components report their own status with a reason; unknown is treated
            as unhealthy, never assumed healthy.
          </p>
        </div>
        <div className="flex gap-2">
          <button className="btn-ghost" onClick={runPipeline} disabled={busy}>
            Run pipeline now
          </button>
          {trading.data?.kill_switch_engaged ? (
            <button className="btn-primary" onClick={() => toggleKillSwitch(false)} disabled={busy}>
              Release kill switch
            </button>
          ) : (
            <button
              className="btn border border-bear/50 bg-bear-soft text-bear hover:bg-bear/20"
              onClick={() => toggleKillSwitch(true)}
              disabled={busy}
            >
              Engage kill switch
            </button>
          )}
        </div>
      </div>

      {message && <div className="text-2xs text-ink-muted">{message}</div>}

      {trading.data?.kill_switch_engaged && (
        <ErrorBox message="KILL SWITCH ENGAGED — all new orders are blocked in paper and live mode." />
      )}

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <Panel title="Components" className="lg:col-span-2" bodyClassName="p-0">
          {health.isLoading && <Loading />}
          {health.data && (
            <ul className="divide-y divide-line/60">
              {health.data.components.map((component) => (
                <li key={component.component} className="px-4 py-3">
                  <div className="flex items-center gap-2">
                    <StatusDot status={component.status} />
                    <span className="text-sm font-medium">
                      {component.component.replace(/_/g, ' ')}
                    </span>
                    <span className="ml-auto text-2xs text-ink-faint">{component.status}</span>
                  </div>
                  <p className="mt-1 pl-4 text-2xs text-ink-muted">{component.detail}</p>
                </li>
              ))}
            </ul>
          )}
        </Panel>

        <div className="space-y-4">
          <Panel title="Trading mode">
            {trading.data && (
              <dl className="space-y-2 text-sm">
                <div className="flex justify-between">
                  <dt className="text-ink-muted">Mode</dt>
                  <dd className="font-mono uppercase">{trading.data.mode}</dd>
                </div>
                <div className="flex justify-between">
                  <dt className="text-ink-muted">Live trading</dt>
                  <dd className={trading.data.allowed ? 'text-bear' : 'text-bull'}>
                    {trading.data.allowed ? 'ENABLED' : 'disabled'}
                  </dd>
                </div>
                <div className="flex justify-between">
                  <dt className="text-ink-muted">Kill switch</dt>
                  <dd className={trading.data.kill_switch_engaged ? 'text-bear' : 'text-ink-muted'}>
                    {trading.data.kill_switch_engaged ? 'ENGAGED' : 'released'}
                  </dd>
                </div>
                <div className="flex justify-between">
                  <dt className="text-ink-muted">Commission</dt>
                  <dd className="font-mono">{trading.data.commission_bps} bps</dd>
                </div>
                <div className="flex justify-between">
                  <dt className="text-ink-muted">Slippage</dt>
                  <dd className="font-mono">{trading.data.slippage_bps} bps</dd>
                </div>
                {trading.data.blockers.length > 0 && (
                  <div className="pt-2 border-t border-line">
                    <dt className="stat-label mb-1">Live trading blocked by</dt>
                    <ul className="space-y-0.5">
                      {trading.data.blockers.map((blocker) => (
                        <li key={blocker} className="text-2xs text-ink-faint">· {blocker}</li>
                      ))}
                    </ul>
                  </div>
                )}
              </dl>
            )}
          </Panel>

          <Panel title="Market regime">
            {!regimes.data?.length && <Empty message="No regime computed yet" />}
            <ul className="space-y-2">
              {regimes.data?.map((regime) => (
                <li key={String(regime.exchange)} className="text-sm">
                  <div className="flex items-center justify-between">
                    <span className="font-medium">{String(regime.exchange)}</span>
                    <span className="font-mono text-2xs">
                      {String(regime.regime)} / {String(regime.volatility_regime)}
                    </span>
                  </div>
                  <div className="text-2xs text-ink-faint">
                    as of {String(regime.trade_date)}
                  </div>
                </li>
              ))}
            </ul>
          </Panel>
        </div>
      </div>

      <Panel title="Recent system events" bodyClassName="p-0">
        {events.isLoading && <Loading />}
        {events.data?.length === 0 && <Empty message="No events recorded" />}
        {!!events.data?.length && (
          <div className="w-full max-w-full overflow-x-auto">
            <table className="data-table">
              <thead>
                <tr><th>When</th><th>Component</th><th>Event</th><th>Severity</th><th>Message</th></tr>
              </thead>
              <tbody>
                {events.data.map((event) => (
                  <tr key={event.id}>
                    <td className="text-2xs text-ink-faint" title={formatDateTime(event.created_at)}>
                      {relativeTime(event.created_at)}
                    </td>
                    <td className="text-ink-muted">{event.component}</td>
                    <td className="font-mono text-2xs">{event.event_type}</td>
                    <td className={`text-2xs font-medium ${SEVERITY_COLOR[event.severity] ?? ''}`}>
                      {event.severity}
                    </td>
                    <td className="max-w-md truncate text-2xs text-ink-muted" title={event.message}>
                      {event.message}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  );
}

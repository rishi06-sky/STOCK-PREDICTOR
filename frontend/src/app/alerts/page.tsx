'use client';

import { useState } from 'react';
import useSWR from 'swr';
import { api, fetcher, type Alert, type Page } from '@/lib/api';
import { formatDateTime, relativeTime } from '@/lib/format';
import { Empty, Loading, Panel } from '@/components/ui';

const SEVERITY_STYLES: Record<string, string> = {
  CRITICAL: 'bg-bear-soft text-bear border-bear/40',
  WARNING: 'bg-warn-soft text-warn border-warn/40',
  INFO: 'bg-accent-soft text-accent border-accent/40',
};

export default function AlertsPage() {
  const [unreadOnly, setUnreadOnly] = useState(false);
  const { data, isLoading, mutate } = useSWR<Page<Alert>>(
    `/alerts?limit=100${unreadOnly ? '&unread_only=true' : ''}`,
    fetcher,
    { refreshInterval: 30000 },
  );

  async function markRead(id: number) {
    await api.post(`/alerts/${id}/read`);
    mutate();
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">Alerts</h1>
          <p className="text-2xs text-ink-faint">
            Duplicate alerts are suppressed within the dedupe window rather than re-sent.
          </p>
        </div>
        <label className="flex items-center gap-2 text-2xs text-ink-muted">
          <input
            type="checkbox" className="accent-accent" checked={unreadOnly}
            onChange={(e) => setUnreadOnly(e.target.checked)}
          />
          Unread only
        </label>
      </div>

      {isLoading && <Panel><Loading /></Panel>}
      {data?.items.length === 0 && (
        <Panel>
          <Empty
            message="No alerts"
            hint="Alerts are raised for actionable signals, stop/target hits, large moves and system events."
          />
        </Panel>
      )}

      <div className="space-y-2">
        {data?.items.map((alert) => (
          <article
            key={alert.id}
            className={`panel p-4 ${alert.read_at ? 'opacity-60' : ''}`}
          >
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <span className={`chip border ${SEVERITY_STYLES[alert.severity] ?? SEVERITY_STYLES.INFO}`}>
                    {alert.severity}
                  </span>
                  <span className="chip bg-ground-overlay text-ink-muted">
                    {alert.alert_type.replace(/_/g, ' ')}
                  </span>
                  <span className="text-2xs text-ink-faint">
                    {relativeTime(alert.created_at)} · {formatDateTime(alert.created_at)}
                  </span>
                  {alert.status === 'FAILED' && (
                    <span className="chip bg-bear-soft text-bear">DELIVERY FAILED</span>
                  )}
                </div>
                <h2 className="mt-1.5 text-sm font-medium">{alert.title}</h2>
                <pre className="mt-1 whitespace-pre-wrap font-sans text-2xs leading-relaxed text-ink-muted">
                  {alert.body}
                </pre>
              </div>
              {!alert.read_at && (
                <button className="btn-ghost shrink-0" onClick={() => markRead(alert.id)}>
                  Mark read
                </button>
              )}
            </div>
          </article>
        ))}
      </div>
    </div>
  );
}

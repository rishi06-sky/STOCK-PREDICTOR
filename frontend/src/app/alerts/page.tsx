'use client';

import { useState } from 'react';
import useSWR from 'swr';
import { api, fetcher, type Alert, type Page } from '@/lib/api';
import { formatDateTime, relativeTime } from '@/lib/format';
import { Empty, Loading, PageHeader, Panel } from '@/components/ui';

const SEVERITY_STYLES: Record<string, string> = {
  CRITICAL: 'bg-bear-soft text-bear',
  WARNING: 'bg-warn-soft text-warn',
  INFO: 'bg-accent-soft text-accent',
};

/** A coloured rule on the left edge carries severity at a glance. */
const SEVERITY_EDGE: Record<string, string> = {
  CRITICAL: 'before:bg-bear',
  WARNING: 'before:bg-warn',
  INFO: 'before:bg-accent/60',
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
    <div className="space-y-5">
      <PageHeader
        title="Alerts"
        description="Duplicate alerts are suppressed within the dedupe window rather than re-sent."
        actions={
          <label className="flex cursor-pointer items-center gap-2 text-sm text-ink-muted">
            <input
              type="checkbox" className="h-3.5 w-3.5 accent-accent" checked={unreadOnly}
              onChange={(e) => setUnreadOnly(e.target.checked)}
            />
            Unread only
          </label>
        }
      />

      {isLoading && <Panel><Loading /></Panel>}
      {data?.items.length === 0 && (
        <Panel>
          <Empty
            message="No alerts"
            hint="Alerts are raised for actionable signals, stop/target hits, large moves and system events."
          />
        </Panel>
      )}

      {!!data?.items.length && (
        <Panel bodyClassName="p-0">
          <ul className="divide-y divide-line">
            {data.items.map((alert) => (
              <li
                key={alert.id}
                className={`relative px-5 py-4 before:absolute before:inset-y-3 before:left-0 before:w-0.5 before:rounded-full ${
                  alert.read_at ? 'before:bg-transparent' : SEVERITY_EDGE[alert.severity] ?? SEVERITY_EDGE.INFO
                }`}
              >
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className={`min-w-0 flex-1 ${alert.read_at ? 'opacity-60' : ''}`}>
                    <div className="flex flex-wrap items-center gap-2">
                      <span className={`chip ${SEVERITY_STYLES[alert.severity] ?? SEVERITY_STYLES.INFO}`}>
                        {alert.severity}
                      </span>
                      <span className="chip bg-ground-overlay text-ink-muted">
                        {alert.alert_type.replace(/_/g, ' ')}
                      </span>
                      {alert.status === 'FAILED' && (
                        <span className="chip bg-bear-soft text-bear">DELIVERY FAILED</span>
                      )}
                      <time
                        dateTime={alert.created_at}
                        title={formatDateTime(alert.created_at)}
                        className="text-xs text-ink-faint"
                      >
                        {relativeTime(alert.created_at)}
                      </time>
                    </div>
                    <h2 className="mt-2 text-sm font-medium">{alert.title}</h2>
                    <pre className="mt-1 max-w-[90ch] whitespace-pre-wrap font-sans text-xs leading-relaxed text-ink-muted">
                      {alert.body}
                    </pre>
                  </div>
                  {!alert.read_at && (
                    <button className="btn-ghost shrink-0" onClick={() => markRead(alert.id)}>
                      Mark read
                    </button>
                  )}
                </div>
              </li>
            ))}
          </ul>
        </Panel>
      )}
    </div>
  );
}

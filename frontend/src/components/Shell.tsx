'use client';

import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { useEffect, useState } from 'react';
import useSWR from 'swr';
import { clearTokens, fetcher, getToken, type Health, type MarketStatus } from '@/lib/api';
import { useLiveUpdates } from '@/hooks/useLiveUpdates';
import { StatusDot } from '@/components/ui';

const NAV = [
  { href: '/', label: 'Dashboard' },
  { href: '/opportunities', label: 'Opportunities' },
  { href: '/portfolio', label: 'Portfolio' },
  { href: '/alerts', label: 'Alerts' },
  { href: '/backtests', label: 'Backtests' },
  { href: '/models', label: 'Models' },
  { href: '/system', label: 'System' },
];

export function Shell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [authed, setAuthed] = useState<boolean | null>(null);
  const { connected } = useLiveUpdates();

  useEffect(() => {
    const token = getToken();
    setAuthed(Boolean(token));
    if (!token && pathname !== '/login') router.replace('/login');
  }, [pathname, router]);

  const { data: health } = useSWR<Health>(
    authed ? '/health' : null, fetcher, { refreshInterval: 30000 },
  );
  const { data: markets } = useSWR<MarketStatus[]>(
    authed ? '/market/status' : null, fetcher, { refreshInterval: 60000 },
  );

  if (pathname === '/login') return <>{children}</>;
  if (authed === null) {
    return <div className="p-8 text-sm text-ink-faint">Checking session…</div>;
  }

  return (
    <div className="min-h-screen flex flex-col">
      <header className="sticky top-0 z-20 border-b border-line bg-ground/95 backdrop-blur">
        <div className="flex flex-wrap items-center gap-x-6 gap-y-2 px-4 py-2.5">
          <Link href="/" className="flex items-center gap-2 shrink-0">
            <span className="h-2 w-2 rounded-sm bg-accent" />
            <span className="font-semibold tracking-tight">Stock Intelligence</span>
          </Link>

          <nav className="flex flex-wrap items-center gap-1 order-3 w-full lg:order-2 lg:w-auto">
            {NAV.map((item) => {
              const active =
                item.href === '/' ? pathname === '/' : pathname.startsWith(item.href);
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  className={`rounded px-2.5 py-1 text-sm transition-colors ${
                    active
                      ? 'bg-ground-overlay text-ink'
                      : 'text-ink-muted hover:text-ink'
                  }`}
                >
                  {item.label}
                </Link>
              );
            })}
          </nav>

          <div className="ml-auto flex items-center gap-3 order-2 lg:order-3">
            <div className="hidden md:flex items-center gap-2.5 text-2xs">
              {markets?.map((market) => (
                <span key={market.exchange} className="flex items-center gap-1">
                  <span
                    className={`h-1.5 w-1.5 rounded-full ${
                      market.is_open ? 'bg-bull' : 'bg-ink-faint'
                    }`}
                  />
                  <span className="text-ink-muted">{market.exchange}</span>
                </span>
              ))}
            </div>

            <span
              className="flex items-center gap-1 text-2xs text-ink-faint"
              title={connected ? 'Live updates connected' : 'Live updates reconnecting'}
            >
              <span
                className={`h-1.5 w-1.5 rounded-full ${
                  connected ? 'bg-bull' : 'bg-warn animate-pulse'
                }`}
              />
              {connected ? 'live' : 'offline'}
            </span>

            {health && (
              <Link href="/system" className="flex items-center gap-1 text-2xs text-ink-muted">
                <StatusDot status={health.status} />
                {health.status.toLowerCase()}
              </Link>
            )}

            <button
              type="button"
              onClick={() => {
                clearTokens();
                router.replace('/login');
              }}
              className="text-2xs text-ink-faint hover:text-ink"
            >
              Sign out
            </button>
          </div>
        </div>
      </header>

      <main className="flex-1 px-4 py-5">{children}</main>

      <footer className="border-t border-line px-4 py-3">
        <p className="text-2xs text-ink-faint">
          Signals are model estimates with real uncertainty — not investment advice,
          and never a guarantee. Check the data-freshness tag beside every price:
          DELAYED and EOD figures are not live, and SYNTHETIC is test data.
        </p>
      </footer>
    </div>
  );
}

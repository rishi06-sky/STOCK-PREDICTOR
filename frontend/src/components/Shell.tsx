'use client';

import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { useEffect, useState } from 'react';
import useSWR from 'swr';
import { SignOut } from '@phosphor-icons/react/ssr';
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

function NavLinks({ pathname }: { pathname: string }) {
  return (
    <>
      {NAV.map((item) => {
        const active =
          item.href === '/' ? pathname === '/' : pathname.startsWith(item.href);
        return (
          <Link
            key={item.href}
            href={item.href}
            aria-current={active ? 'page' : undefined}
            className={`relative flex h-full shrink-0 items-center px-2.5 text-sm transition-colors ${
              active ? 'text-ink' : 'text-ink-muted hover:text-ink'
            }`}
          >
            {item.label}
            <span
              aria-hidden
              className={`absolute inset-x-2.5 bottom-0 h-0.5 rounded-full bg-accent transition-opacity duration-200 ${
                active ? 'opacity-100' : 'opacity-0'
              }`}
            />
          </Link>
        );
      })}
    </>
  );
}

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
    return (
      <div className="min-h-[100dvh]" role="status">
        <span className="sr-only">Checking session</span>
        <div className="h-14 border-b border-line" />
        <div className="mx-auto max-w-[1600px] space-y-4 px-4 py-6">
          <div className="skeleton h-6 w-48" />
          <div className="skeleton h-40 w-full" />
        </div>
      </div>
    );
  }

  return (
    <div className="flex min-h-[100dvh] flex-col">
      <header className="sticky top-0 z-20 border-b border-line bg-ground/90 backdrop-blur-md">
        <div className="mx-auto flex h-14 max-w-[1600px] items-stretch gap-6 px-4">
          <Link href="/" className="flex shrink-0 items-center gap-2">
            <span aria-hidden className="h-3 w-3 rounded-[3px] bg-accent" />
            <span className="text-[15px] font-semibold tracking-tight">Stock Intelligence</span>
          </Link>

          <nav aria-label="Primary" className="hidden items-stretch lg:flex">
            <NavLinks pathname={pathname} />
          </nav>

          <div className="ml-auto flex items-center gap-4 text-xs">
            <div className="hidden items-center gap-3 md:flex">
              {markets?.map((market) => (
                <span
                  key={market.exchange}
                  className="flex items-center gap-1.5 text-ink-muted"
                  title={`${market.exchange} ${market.is_open ? 'open' : 'closed'}: ${market.reason}`}
                >
                  <span
                    aria-hidden
                    className={`h-1.5 w-1.5 rounded-full ${market.is_open ? 'bg-bull' : 'bg-ink-faint'}`}
                  />
                  {market.exchange}
                  <span className="text-ink-faint">{market.is_open ? 'open' : 'closed'}</span>
                </span>
              ))}
            </div>

            <span
              className="hidden items-center gap-1.5 text-ink-muted sm:flex"
              title={connected ? 'Live updates connected' : 'Live updates reconnecting'}
            >
              <span
                aria-hidden
                className={`h-1.5 w-1.5 rounded-full ${
                  connected ? 'bg-bull' : 'bg-warn motion-safe:animate-pulse'
                }`}
              />
              {connected ? 'Live' : 'Reconnecting'}
            </span>

            {health && (
              <Link
                href="/system"
                className="flex items-center gap-1.5 text-ink-muted transition-colors hover:text-ink"
                title="System health"
              >
                <StatusDot status={health.status} />
                <span className="capitalize">{health.status.toLowerCase()}</span>
              </Link>
            )}

            <button
              type="button"
              onClick={() => {
                clearTokens();
                router.replace('/login');
              }}
              className="flex items-center gap-1.5 rounded-md py-1 text-ink-faint transition-colors hover:text-ink"
            >
              <SignOut size={14} aria-hidden />
              <span className="hidden sm:inline">Sign out</span>
              <span className="sr-only sm:hidden">Sign out</span>
            </button>
          </div>
        </div>

        {/* Below lg the nav moves to its own row and scrolls sideways. */}
        <nav
          aria-label="Primary"
          className="no-scrollbar flex h-10 items-stretch overflow-x-auto border-t border-line px-1.5 lg:hidden"
        >
          <NavLinks pathname={pathname} />
        </nav>
      </header>

      <main className="mx-auto w-full max-w-[1600px] flex-1 px-4 py-6">{children}</main>

      <footer className="border-t border-line">
        <p className="mx-auto max-w-[1600px] px-4 py-4 text-xs leading-relaxed text-ink-faint">
          Signals are model estimates with real uncertainty. They are not investment
          advice and never a guarantee. Check the freshness tag beside every price:
          DELAYED and EOD figures are not live, and SYNTHETIC is test data.
        </p>
      </footer>
    </div>
  );
}

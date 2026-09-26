'use client';

import { useRouter } from 'next/navigation';
import { useState } from 'react';
import { ApiError, login, register } from '@/lib/api';
import { ChartLineUp, Scales, ShieldCheck } from '@phosphor-icons/react/ssr';
import { ErrorBox } from '@/components/ui';

/** Drawn from how the platform behaves, not marketing claims. */
const PRINCIPLES = [
  {
    icon: ChartLineUp,
    title: 'Tested out of sample',
    body: 'Walk-forward validation, reported against the majority-class baseline.',
  },
  {
    icon: Scales,
    title: 'Risk checked',
    body: 'Every order passes the kill switch and risk engine first.',
  },
  {
    icon: ShieldCheck,
    title: 'Fails safe',
    body: 'Stale data or no validated model means no signal.',
  },
];

export default function LoginPage() {
  const router = useRouter();
  const [mode, setMode] = useState<'login' | 'register'>('login');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [fullName, setFullName] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (mode === 'register') {
        await register(email, password, fullName || undefined);
      }
      await login(email, password);
      router.replace('/');
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not reach the API server.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="grid min-h-[100dvh] lg:grid-cols-[minmax(0,1.1fr)_minmax(0,1fr)]">
      {/* ------------------------------------------------------ context */}
      <section className="flex flex-col justify-between gap-10 border-b border-line bg-ground-raised px-6 py-8 sm:px-10 lg:border-b-0 lg:border-r lg:px-14 lg:py-12">
        <div className="flex items-center gap-2">
          <span aria-hidden className="h-3 w-3 rounded-[3px] bg-accent" />
          <span className="text-[15px] font-semibold tracking-tight">Stock Intelligence</span>
        </div>

        <div className="max-w-xl">
          <h1 className="text-3xl font-semibold leading-[1.1] tracking-tight md:text-4xl">
            Research and paper trading for NSE and BSE.
          </h1>
          <p className="mt-4 max-w-[52ch] text-base leading-relaxed text-ink-muted">
            Model signals with their uncertainty shown, checked by a risk engine before
            any simulated order.
          </p>

          <ul className="mt-10 hidden gap-6 sm:grid sm:grid-cols-3">
            {PRINCIPLES.map(({ icon: Icon, title, body }) => (
              <li key={title}>
                <Icon size={20} className="text-accent" aria-hidden />
                <p className="mt-3 text-sm font-medium">{title}</p>
                <p className="mt-1 text-xs leading-relaxed text-ink-faint">{body}</p>
              </li>
            ))}
          </ul>
        </div>

        <p className="hidden text-xs text-ink-faint lg:block">
          Not investment advice. Live trading is off unless explicitly enabled.
        </p>
      </section>

      {/* --------------------------------------------------------- form */}
      <section className="flex items-start justify-center px-6 py-10 sm:px-10 lg:items-center">
        <div className="w-full max-w-sm">
          <h2 className="text-xl font-semibold tracking-tight">
            {mode === 'login' ? 'Sign in' : 'Create an account'}
          </h2>
          <p className="mt-1 text-sm text-ink-muted">
            {mode === 'login'
              ? 'Use the account you registered on this server.'
              : 'The first account created becomes the administrator.'}
          </p>

          <form onSubmit={submit} className="mt-8 space-y-5">
            {mode === 'register' && (
              <div className="grid gap-2">
                <label className="text-sm font-medium" htmlFor="name">Full name</label>
                <input
                  id="name" className="input" value={fullName}
                  onChange={(e) => setFullName(e.target.value)} autoComplete="name"
                />
              </div>
            )}
            <div className="grid gap-2">
              <label className="text-sm font-medium" htmlFor="email">Email</label>
              <input
                id="email" type="email" required className="input" value={email}
                onChange={(e) => setEmail(e.target.value)} autoComplete="username"
              />
            </div>
            <div className="grid gap-2">
              <label className="text-sm font-medium" htmlFor="password">Password</label>
              <input
                id="password" type="password" required minLength={10} className="input"
                value={password} onChange={(e) => setPassword(e.target.value)}
                autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
                aria-describedby={mode === 'register' ? 'password-help' : undefined}
              />
              {mode === 'register' && (
                <p id="password-help" className="text-xs text-ink-faint">At least 10 characters.</p>
              )}
            </div>

            {error && <ErrorBox message={error} />}

            <button type="submit" className="btn-primary w-full py-2" disabled={busy}>
              {busy ? 'Working…' : mode === 'login' ? 'Sign in' : 'Create account'}
            </button>
          </form>

          <p className="mt-6 text-center text-sm text-ink-muted">
            {mode === 'login' ? 'No account yet?' : 'Already registered?'}{' '}
            <button
              type="button"
              onClick={() => { setMode(mode === 'login' ? 'register' : 'login'); setError(null); }}
              className="font-medium text-accent underline-offset-4 hover:underline"
            >
              {mode === 'login' ? 'Create one' : 'Sign in'}
            </button>
          </p>
        </div>
      </section>
    </div>
  );
}

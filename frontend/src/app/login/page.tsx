'use client';

import { useRouter } from 'next/navigation';
import { useState } from 'react';
import { ApiError, login, register } from '@/lib/api';
import { ErrorBox } from '@/components/ui';

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
    <div className="min-h-screen grid place-items-center px-4">
      <div className="w-full max-w-sm">
        <div className="mb-6 text-center">
          <div className="mx-auto mb-3 h-2.5 w-2.5 rounded-sm bg-accent" />
          <h1 className="text-xl font-semibold tracking-tight">Stock Intelligence</h1>
          <p className="mt-1 text-2xs text-ink-faint">
            NSE · BSE — research and paper trading
          </p>
        </div>

        <form onSubmit={submit} className="panel p-5 space-y-3">
          {mode === 'register' && (
            <div>
              <label className="stat-label" htmlFor="name">Full name</label>
              <input
                id="name" className="input mt-1" value={fullName}
                onChange={(e) => setFullName(e.target.value)} autoComplete="name"
              />
            </div>
          )}
          <div>
            <label className="stat-label" htmlFor="email">Email</label>
            <input
              id="email" type="email" required className="input mt-1" value={email}
              onChange={(e) => setEmail(e.target.value)} autoComplete="username"
            />
          </div>
          <div>
            <label className="stat-label" htmlFor="password">Password</label>
            <input
              id="password" type="password" required minLength={10} className="input mt-1"
              value={password} onChange={(e) => setPassword(e.target.value)}
              autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
            />
            {mode === 'register' && (
              <p className="mt-1 text-2xs text-ink-faint">At least 10 characters.</p>
            )}
          </div>

          {error && <ErrorBox message={error} />}

          <button type="submit" className="btn-primary w-full" disabled={busy}>
            {busy ? 'Working…' : mode === 'login' ? 'Sign in' : 'Create account'}
          </button>

          <button
            type="button"
            onClick={() => { setMode(mode === 'login' ? 'register' : 'login'); setError(null); }}
            className="w-full text-2xs text-ink-faint hover:text-ink"
          >
            {mode === 'login' ? 'No account? Create one' : 'Already registered? Sign in'}
          </button>
        </form>

        <p className="mt-4 text-center text-2xs text-ink-faint">
          The first account created becomes the administrator.
        </p>
      </div>
    </div>
  );
}

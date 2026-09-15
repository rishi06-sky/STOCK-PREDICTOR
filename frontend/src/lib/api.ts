/**
 * API client.
 *
 * The access token lives in memory plus sessionStorage rather than a
 * long-lived cookie, so a closed tab ends the session. Every response shape
 * mirrors the backend schemas in app/schemas/common.py.
 */
export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE ?? 'http://localhost:8000/api/v1';

const TOKEN_KEY = 'si_access_token';
const REFRESH_KEY = 'si_refresh_token';

export function getToken(): string | null {
  if (typeof window === 'undefined') return null;
  return window.sessionStorage.getItem(TOKEN_KEY);
}

export function setTokens(access: string, refresh: string): void {
  window.sessionStorage.setItem(TOKEN_KEY, access);
  window.sessionStorage.setItem(REFRESH_KEY, refresh);
}

export function clearTokens(): void {
  window.sessionStorage.removeItem(TOKEN_KEY);
  window.sessionStorage.removeItem(REFRESH_KEY);
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code?: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = getToken();
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...((init.headers as Record<string, string>) ?? {}),
  };
  if (token) headers.Authorization = `Bearer ${token}`;

  const response = await fetch(`${API_BASE}${path}`, { ...init, headers });

  if (!response.ok) {
    let detail = `request failed (${response.status})`;
    let code: string | undefined;
    try {
      const body = await response.json();
      detail = body.detail ?? detail;
      code = body.code;
    } catch {
      /* a non-JSON error body is not worth failing over */
    }
    if (response.status === 401) clearTokens();
    throw new ApiError(detail, response.status, code);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'POST', body: body ? JSON.stringify(body) : undefined }),
  del: <T>(path: string) => request<T>(path, { method: 'DELETE' }),
};

export const fetcher = <T>(path: string) => api.get<T>(path);

export async function login(email: string, password: string) {
  const tokens = await api.post<{ access_token: string; refresh_token: string }>(
    '/auth/login',
    { email, password },
  );
  setTokens(tokens.access_token, tokens.refresh_token);
  return tokens;
}

export async function register(email: string, password: string, fullName?: string) {
  return api.post('/auth/register', { email, password, full_name: fullName });
}

// ------------------------------------------------------------------- types
export type SignalKind =
  | 'STRONG_BUY' | 'BUY' | 'HOLD' | 'SELL' | 'STRONG_SELL' | 'NO_ACTION';

/** LIVE / DELAYED / EOD / SYNTHETIC. Surfaced everywhere a price is shown. */
export type DataQuality = 'LIVE' | 'DELAYED' | 'EOD' | 'HISTORICAL' | 'SYNTHETIC';

export interface Rationale {
  factor: string;
  detail: string;
  contribution: 'bullish' | 'bearish' | 'neutral';
}

export interface Signal {
  id: number;
  security_id: number;
  symbol: string;
  name: string;
  exchange: string | null;
  sector: string | null;
  signal: SignalKind;
  status: string;
  confidence: number;
  expected_return: number | null;
  risk_level: 'LOW' | 'MODERATE' | 'HIGH' | 'VERY_HIGH';
  horizon_days: number;
  reference_price: number;
  entry_low: number | null;
  entry_high: number | null;
  stop_loss: number | null;
  take_profit: number | null;
  reward_to_risk: number | null;
  opportunity_score: number | null;
  regime: string | null;
  data_quality: DataQuality;
  price_as_of: string;
  generated_at: string;
  expires_at: string;
  model_version: string | null;
  rationale: Rationale[];
}

export interface Quote {
  security_id: number;
  symbol: string;
  exchange: string | null;
  /** Listing currency, supplied per quote so prices are never assumed. */
  currency: string | null;
  price: number;
  previous_close: number | null;
  change: number | null;
  change_pct: number | null;
  day_open: number | null;
  day_high: number | null;
  day_low: number | null;
  volume: number | null;
  provider: string;
  quality: DataQuality;
  source_timestamp: string;
  age_seconds: number;
  is_stale: boolean;
}

export interface Position {
  security_id: number;
  symbol: string;
  name: string;
  sector: string | null;
  quantity: number;
  average_cost: number;
  current_price: number;
  market_value: number;
  unrealized_pnl: number;
  unrealized_pnl_pct: number;
  weight: number;
  stop_loss: number | null;
  take_profit: number | null;
  price_is_stale: boolean;
  opened_at: string;
}

export interface Portfolio {
  portfolio_id: number;
  name: string;
  mode: string;
  currency: string;
  cash: number;
  positions_value: number;
  equity: number;
  starting_cash: number;
  total_pnl: number;
  total_pnl_pct: number;
  unrealized_pnl: number;
  realized_pnl: number;
  exposure_pct: number;
  drawdown_pct: number;
  peak_equity: number;
  open_positions: number;
  positions: Position[];
  sector_allocation: Record<string, number>;
  warnings: string[];
}

export interface MarketStatus {
  exchange: string;
  market: string;
  is_open: boolean;
  reason: string;
  local_time: string;
  next_open: string | null;
  next_close: string | null;
  holidays_known: boolean;
}

export interface HealthComponent {
  component: string;
  status: 'HEALTHY' | 'DEGRADED' | 'UNHEALTHY';
  detail: string;
  metrics: Record<string, unknown>;
}

export interface Health {
  status: 'HEALTHY' | 'DEGRADED' | 'UNHEALTHY';
  checked_at: string;
  environment: string;
  components: HealthComponent[];
}

export interface Alert {
  id: number;
  alert_type: string;
  severity: string;
  status: string;
  title: string;
  body: string;
  payload: Record<string, unknown> | null;
  security_id: number | null;
  created_at: string;
  sent_at: string | null;
  read_at: string | null;
}

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

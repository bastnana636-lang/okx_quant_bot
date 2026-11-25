import type { CandlesResponse, ConfigResponse, DashboardView, LogEntry, MarketsResponse } from "./types";

const POST_HEADERS = {
  "Content-Type": "application/json",
  "X-Requested-With": "OKX-Dashboard",
};

async function parseJson<T>(res: Response): Promise<T> {
  const data = await res.json();
  if (!res.ok && data && typeof data.message === "string") {
    throw new Error(data.message);
  }
  if (!res.ok) {
    throw new Error(`HTTP ${res.status}`);
  }
  return data as T;
}

export async function fetchView(): Promise<DashboardView> {
  const res = await fetch("/api/view", { cache: "no-store" });
  return parseJson<DashboardView>(res);
}

export async function fetchMarkets(): Promise<MarketsResponse> {
  const res = await fetch("/api/markets", { cache: "no-store" });
  return parseJson<MarketsResponse>(res);
}

export async function fetchCandles(pair: string, interval?: string, limit = 200): Promise<CandlesResponse> {
  const params = new URLSearchParams({ pair, limit: String(limit) });
  if (interval) params.set("interval", interval);
  const res = await fetch(`/api/candles?${params.toString()}`, { cache: "no-store" });
  return parseJson<CandlesResponse>(res);
}

export async function fetchConfig(): Promise<ConfigResponse> {
  const res = await fetch("/api/config", { cache: "no-store" });
  return parseJson<ConfigResponse>(res);
}

export async function fetchLogs(lines = 250): Promise<{ lines: LogEntry[]; total: number }> {
  const res = await fetch(`/api/logs?lines=${lines}`, { cache: "no-store" });
  return parseJson<{ lines: LogEntry[]; total: number }>(res);
}

export async function fetchControlLog(action: "start" | "stop"): Promise<{
  action: string;
  lines: string[];
  control: { in_progress: boolean; action: string | null; pid: number | null; exit_code: number | null };
}> {
  const res = await fetch(`/api/control-log?action=${action}`, { cache: "no-store" });
  return parseJson(res);
}

export async function postRestart(): Promise<{ ok: boolean; message: string }> {
  const res = await fetch("/api/restart", { method: "POST", headers: POST_HEADERS, body: "{}" });
  return res.json();
}

export async function postStop(): Promise<{ ok: boolean; message: string }> {
  const res = await fetch("/api/stop", { method: "POST", headers: POST_HEADERS, body: "{}" });
  return res.json();
}

export async function postClosePosition(controller: string): Promise<{ ok: boolean; message: string }> {
  const res = await fetch("/api/close-position", {
    method: "POST",
    headers: POST_HEADERS,
    body: JSON.stringify({ controller }),
  });
  return parseJson(res);
}

export async function postManualClose(payload: {
  pair: string;
  side: string;
  amount: number;
  breakeven: number;
  pnl: number;
}): Promise<{ ok: boolean; message: string }> {
  const res = await fetch("/api/manual-close", {
    method: "POST",
    headers: POST_HEADERS,
    body: JSON.stringify(payload),
  });
  return parseJson(res);
}

export async function postDeleteManualClose(pair: string, side: string): Promise<{ ok: boolean; message: string }> {
  const res = await fetch("/api/manual-close/delete", {
    method: "POST",
    headers: POST_HEADERS,
    body: JSON.stringify({ pair, side }),
  });
  return parseJson(res);
}

export async function postSaveConfig(
  payload: {
    pairs: Array<{
      file: string;
      leverage: string;
      total_amount_quote: string;
      overrides?: Record<string, string>;
    }>;
    shared: Record<string, string>;
    enabled_files?: string[];
  },
  andRestart = false
): Promise<{ ok: boolean; message: string; files?: string[] }> {
  const endpoint = andRestart ? "/api/config/save-and-restart" : "/api/config";
  const res = await fetch(endpoint, {
    method: "POST",
    headers: POST_HEADERS,
    body: JSON.stringify(payload),
  });
  return parseJson(res);
}

export async function postAddPair(payload: {
  trading_pair: string;
  leverage: string;
  total_amount_quote: string;
  enable: boolean;
}): Promise<{ ok: boolean; message: string }> {
  const res = await fetch("/api/config/add-pair", {
    method: "POST",
    headers: POST_HEADERS,
    body: JSON.stringify(payload),
  });
  return parseJson(res);
}

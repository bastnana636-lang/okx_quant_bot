export type TabKey = "overview" | "markets" | "positions" | "config" | "logs";

export interface HistoryPoint {
  t: number;
  bot_pnl: number;
  assets: number;
}

export interface StrategyInfo {
  key: string;
  name: string;
  badge: string;
  interval: string;
  mean_window: number;
  time_limit: number;
  signal_mode?: string;
  side_mode?: string;
  description: string;
}

export interface PerfRow {
  controller: string;
  pair: string;
  symbol: string;
  realized: number;
  unrealized: number;
  global: number;
  global_pct: string;
  volume: number;
  z_score: number | null;
  momentum: number | null;
  rsi9?: number | null;
  er4h?: number | null;
  signal: number;
  reason: string;
  signal_mode: string;
  leverage: number;
  total_amount_quote: number;
  candles_interval: string;
  entry_z_score: number;
  max_entry_z_score: number;
  momentum_threshold: number;
  long_only: boolean;
  manual_kill_switch: boolean;
}

export interface PositionItem {
  connector: string;
  pair: string;
  symbol: string;
  side: "BUY" | "SELL";
  amount: number;
  value: number;
  breakeven: number;
  unrealized: number;
  realized: number;
  fees: number;
  leverage: number;
  controller_id: string;
  source: string;
}

export interface OrderItem {
  connector: string;
  pair: string;
  symbol: string;
  side: string;
  price: number;
  amount: number;
  age: string;
}

export interface ExecutorItem {
  controller: string;
  symbol: string;
  pair: string;
  side: string;
  status: string;
  net_pnl_pct: number;
  net_pnl: number;
  volume: number;
  is_trading: boolean;
  close_type: string;
  age: number;
}

export interface ManualCloseRecord {
  pair: string;
  side: string;
  amount: number;
  breakeven: number;
  pnl: number;
}

export interface LogEntry {
  id: number;
  raw: string;
  level: "INFO" | "WARNING" | "ERROR" | "DEBUG";
  timestamp: string;
}

export interface DashboardView {
  error: string | null;
  updated_at: number;
  updated_str: string;
  snapshot_age_s: number | null;
  uptime_s: number;
  uptime_str: string;
  running: boolean;
  stale: boolean;
  strategy: string;
  strategy_info: StrategyInfo;
  side_mode: string;
  control: {
    in_progress: boolean;
    action: string | null;
    pid: number | null;
    exit_code?: number | null;
  };
  usdt_bal: number;
  usdt_bal_str: string;
  balances: Record<string, number>;
  realized: number;
  unrealized: number;
  global_pnl: number;
  global_pct: string;
  total_volume: number;
  session_peak: number;
  session_min: number;
  history: HistoryPoint[];
  perf_rows: PerfRow[];
  positions: PositionItem[];
  orders: OrderItem[];
  executors: ExecutorItem[];
  signals: Record<string, { z: number | null; momentum: number | null; reason: string; signal: number }>;
  manual_closes: ManualCloseRecord[];
  manual_close_note: string;
  warnings: string[];
  logs: LogEntry[];
}

export interface TickerItem {
  pair: string;
  symbol: string;
  inst_id: string;
  is_strategy_pair: boolean;
  last: number | null;
  open24h: number | null;
  change_pct: number | null;
  bid: number | null;
  ask: number | null;
  spread_pct: number | null;
  spread_bps: number | null;
  high24h: number | null;
  low24h: number | null;
  vol_base: number | null;
  vol_quote: number | null;
}

export interface MarketsResponse {
  error: string | null;
  stale: boolean;
  meta: string;
  fetched_at: number;
  tickers: TickerItem[];
}

export interface CandlePoint {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface LinePoint {
  time: number;
  value: number;
}

export interface CandlesResponse {
  pair: string;
  interval: string;
  source: string;
  error: string | null;
  candles: CandlePoint[];
  overlays: {
    mean: LinePoint[];
    upper_band: LinePoint[];
    lower_band: LinePoint[];
    z_score: LinePoint[];
    momentum_pct: LinePoint[];
    atr: number | null;
    stop_pct: number | null;
    atr_stop_long: number | null;
    atr_stop_short: number | null;
  };
  settings: {
    signal_mode: string;
    candles_interval: string;
    mean_window: number;
    entry_z_score: number;
    max_entry_z_score: number;
    exit_z_score: number;
    atr_length: number;
    atr_stop_multiplier: number;
    min_stop_loss: number;
    max_stop_loss: number;
    momentum_lookback: number;
    momentum_threshold: number;
  };
  active_position: PositionItem | null;
}

export interface FieldSpec {
  key: string;
  label: string;
  label_en: string;
  group: "pair" | "mode" | "human" | "momentum" | "reversion" | "risk" | "execution" | "other";
  kind: "int" | "decimal" | "choice" | "bool";
  unit: string;
  step: string;
  description: string;
  default: string;
  choices?: string[];
  min?: number | null;
  max?: number | null;
  bounds?: Record<string, number>;
}

export interface PairConfigEntry {
  file: string;
  id: string;
  trading_pair: string;
  enabled: boolean;
  leverage: string;
  total_amount_quote: string;
  values: Record<string, string>;
  overrides: Record<string, string>;
  has_overrides: boolean;
}

export interface StrategyPreset {
  key: string;
  name: string;
  badge: string;
  description: string;
  shared: Record<string, string>;
}

export interface ConfigResponse {
  pairs: PairConfigEntry[];
  all_pairs: PairConfigEntry[];
  shared: Record<string, string>;
  shared_mixed: Record<string, boolean>;
  defaults: Record<string, string | number>;
  strategy: StrategyInfo;
  field_specs: FieldSpec[];
  presets: StrategyPreset[];
  side_mode: string;
}

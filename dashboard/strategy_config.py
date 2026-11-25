"""Read, validate, and update controller YAML files and active script selection.

Keeps Python standard-library compatibility (zero third-party YAML deps) while
exposing the full PMMSimpleConfig / MeanReversionSettings schema, per-pair
overrides, pair enable/disable management in conf_okx_multi.yml, and presets.
"""

from __future__ import annotations

import os
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(os.environ.get("OKX_TRADER_ROOT", Path(__file__).resolve().parents[1])).resolve()
SCRIPT_FILE = ROOT / "conf" / "scripts" / "conf_okx_multi.yml"
CONTROLLERS_DIR = ROOT / "conf" / "controllers"

FILE_NAME_RE = re.compile(r"^conf_okx_pmm_[a-z0-9]+\.yml$")
PAIR_NAME_RE = re.compile(r"^[A-Z0-9]{1,12}-USDT$")
KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*?)\s*$")
INTERVALS = ("1m", "3m", "5m", "15m", "30m", "1h")
SIGNAL_MODES = ("reversion", "momentum", "human_swing")

STRATEGY_INFO = {
    "key": "human_swing_11",
    "name": "5m Human 双向波段策略",
    "badge": "5m Human 波段 · 11 币种",
    "interval": "5m",
    "mean_window": 48,
    "time_limit": 86400,
    "signal_mode": "human_swing",
    "description": "4H结构共振 + BTC共振 + 11币种广度过滤；5m回踩EMA12/36配合MACD与RSI9开仓，15m ATR(2.0x)止损，自适应追踪止盈与+4.5%目标止盈。",
}

# Legacy tuple format kept compatible with existing imports & unit tests:
# (key, label, kind, bounds)
PAIR_FIELDS = (
    ("leverage", "杠杆", "int", (1, 20)),
    ("total_amount_quote", "开单金额 USDT", "decimal", {"gt": 0}),
)

SHARED_FIELDS = (
    ("signal_mode", "信号模式", "choice", SIGNAL_MODES),
    ("long_only", "仅做多", "bool", None),
    ("candles_interval", "K 线周期", "choice", INTERVALS),
    ("require_closed_candle_confirmation", "收盘确认信号", "bool", None),
    ("manual_kill_switch", "手动停机", "bool", None),
    ("require_btc_4h", "BTC 4H 共振", "bool", None),
    ("min_breadth_frac", "最小市场广度", "decimal", {"ge": 0, "le": 1}),
    ("er4h_soft_thresh", "4H 效率软降仓阈值", "decimal", {"ge": 0, "le": 1}),
    ("er4h_soft_scale", "4H 低效率降仓系数", "decimal", {"gt": 0, "le": 1}),
    ("max_dist_e144_atr15", "EMA144 最大偏离(ATR)", "decimal", {"ge": 0}),
    ("swing_tp_pct", "波段目标止盈比例", "decimal", {"gt": 0, "lt": 1}),
    ("adaptive_trail", "自适应 ATR 追踪止盈", "bool", None),
    ("trail_atr_mult", "追踪激活 ATR 倍数", "decimal", {"gt": 0}),
    ("trail_trig_min", "追踪激活下限", "decimal", {"gt": 0, "lt": 1}),
    ("trail_trig_max", "追踪激活上限", "decimal", {"gt": 0, "lt": 1}),
    ("trail_pb_ratio", "追踪回撤比例系数", "decimal", {"gt": 0, "lt": 1}),
    ("trail_pb_min", "追踪回撤下限", "decimal", {"gt": 0, "lt": 1}),
    ("trail_pb_max", "追踪回撤上限", "decimal", {"gt": 0, "lt": 1}),
    ("max_positions", "最大并发持仓数", "int", (1, 20)),
    ("margin_ratio", "单笔最大保证金占比", "decimal", {"gt": 0, "le": 1}),
    ("max_margin_cap", "单笔保证金硬上限", "decimal", {"gt": 0}),
    ("unprot_2nd_scale", "未护航第2笔缩放", "decimal", {"gt": 0, "le": 1}),
    ("unprot_3rd_scale", "未护航第3笔缩放", "decimal", {"gt": 0, "le": 1}),
    ("daily_loss_limit", "单日回撤熔断", "decimal", {"ge": 0, "lt": 1}),
    ("momentum_lookback", "动量回看周期", "int", (2, 500)),
    ("momentum_threshold", "动量入场阈值", "decimal", {"ge": 0, "lt": 1}),
    ("risk_frac", "单笔风险比例", "decimal", {"gt": 0, "lt": 1}),
    ("mean_window", "均值窗口", "int", (10, 500)),
    ("entry_z_score", "入场 Z", "decimal", {"gt": 0}),
    ("max_entry_z_score", "最大入场 Z", "decimal", {"gt": 0}),
    ("exit_z_score", "出场 Z", "decimal", {"ge": 0}),
    ("min_std_pct", "最小波动率", "decimal", {"gt": 0, "lt": 1}),
    ("atr_length", "ATR 长度", "int", (2, 100)),
    ("atr_stop_multiplier", "ATR 止损倍数", "decimal", {"gt": 0}),
    ("min_stop_loss", "止损下限", "decimal", {"gt": 0, "lt": 1}),
    ("max_stop_loss", "止损上限", "decimal", {"gt": 0, "lt": 1}),
    ("round_trip_cost_pct", "往返成本", "decimal", {"ge": 0, "lt": 1}),
    ("min_net_edge_pct", "最小净边际", "decimal", {"gt": 0, "lt": 1}),
    ("min_reward_risk", "最小收益风险比", "decimal", {"gt": 0}),
    ("quote_offset_pct", "报价偏移", "decimal", {"ge": 0, "lt": 1}),
    ("max_bid_ask_spread_pct", "最大点差", "decimal", {"gt": 0, "lt": 1}),
    ("executor_refresh_time", "挂单刷新秒", "int", (1, None)),
    ("cooldown_time", "交易冷静期秒", "int", (1, None)),
    ("stop_loss_cooldown_time", "止损冷静期秒", "int", (1, None)),
    ("time_limit", "持仓时限秒", "int", (60, None)),
)

# Fields added after initial 20-key schema; older YAMLs omit them and rely on PMMSimpleConfig defaults.
OPTIONAL_SHARED_DEFAULTS = {
    "signal_mode": "reversion",
    "long_only": "false",
    "require_closed_candle_confirmation": "true",
    "momentum_lookback": "16",
    "momentum_threshold": "0.008",
    "risk_frac": "0.02",
    "require_btc_4h": "true",
    "min_breadth_frac": "0.60",
    "er4h_soft_thresh": "0.22",
    "er4h_soft_scale": "0.50",
    "max_dist_e144_atr15": "1.2",
    "swing_tp_pct": "0.045",
    "adaptive_trail": "true",
    "trail_atr_mult": "3.2",
    "trail_trig_min": "0.012",
    "trail_trig_max": "0.018",
    "trail_pb_ratio": "0.375",
    "trail_pb_min": "0.0045",
    "trail_pb_max": "0.0065",
    "max_positions": "3",
    "margin_ratio": "0.20",
    "max_margin_cap": "2500",
    "unprot_2nd_scale": "0.60",
    "unprot_3rd_scale": "0.40",
    "daily_loss_limit": "0.02",
}

CORE_SHARED_KEYS = {
    item[0] for item in SHARED_FIELDS if item[0] not in OPTIONAL_SHARED_DEFAULTS
}

PAIR_KEYS = {item[0] for item in PAIR_FIELDS}
SHARED_KEYS = {item[0] for item in SHARED_FIELDS}
EDITABLE_KEYS = PAIR_KEYS | SHARED_KEYS

# Rich bilingual schema metadata for the React Strategy Config Studio.
FIELD_META = {
    "leverage": {
        "label_en": "Leverage",
        "group": "pair",
        "unit": "x",
        "step": "1",
        "description": "合约杠杆倍数（1–20x），Human 策略主流币 15x、山寨币 10x。",
    },
    "total_amount_quote": {
        "label_en": "Max Notional",
        "group": "pair",
        "unit": "USDT",
        "step": "5",
        "description": "单笔最大名义持仓上限（USDT，非保证金占用）。",
    },
    "signal_mode": {
        "label_en": "Signal Mode",
        "group": "mode",
        "unit": "",
        "description": "human_swing = 4H共振+广度波段；reversion = 均值回归；momentum = 动量突破。",
    },
    "require_btc_4h": {
        "label_en": "BTC 4H Resonance",
        "group": "human",
        "unit": "",
        "description": "开启后要求 BTC 已收盘 4H K线同步满足顺势 EMA20/EMA50 结构共振。",
    },
    "min_breadth_frac": {
        "label_en": "Min Breadth",
        "group": "human",
        "unit": "ratio",
        "step": "0.05",
        "description": "全市场 11 币种 1H/4H 均线同侧最小广度占比（默认 0.60 = 60%）。",
    },
    "er4h_soft_thresh": {
        "label_en": "4H ER Soft Thresh",
        "group": "human",
        "unit": "ratio",
        "step": "0.02",
        "description": "已收盘 4H 72h 路径效率低于该阈值时触发软降仓（默认 0.22，0=关闭）。",
    },
    "er4h_soft_scale": {
        "label_en": "4H ER Soft Scale",
        "group": "human",
        "unit": "x",
        "step": "0.05",
        "description": "4H 路径效率偏低时的风险缩减系数（默认 0.50 = 半仓）。",
    },
    "max_dist_e144_atr15": {
        "label_en": "EMA144 Max Stretch",
        "group": "human",
        "unit": "ATR",
        "step": "0.1",
        "description": "防过度拉伸追尾：距 EMA144 偏离超过该 15m ATR 倍数则拦截（默认 1.2）。",
    },
    "swing_tp_pct": {
        "label_en": "Swing TP Target",
        "group": "human",
        "unit": "ratio",
        "step": "0.005",
        "description": "波段固定目标限价止盈比例（默认 0.045 = +4.5%）。",
    },
    "adaptive_trail": {
        "label_en": "Adaptive ATR Trail",
        "group": "human",
        "unit": "",
        "description": "按入场 15m ATR 波动率自适应计算追踪止盈激活点与回撤幅度。",
    },
    "trail_atr_mult": {
        "label_en": "Trail ATR Mult",
        "group": "human",
        "unit": "x",
        "step": "0.1",
        "description": "自适应追踪激活阈值的 15m ATR 倍数（默认 3.2）。",
    },
    "trail_trig_min": {
        "label_en": "Trail Trig Min",
        "group": "human",
        "unit": "ratio",
        "step": "0.001",
        "description": "自适应追踪止盈激活盈利下限（默认 0.012 = 1.2%）。",
    },
    "trail_trig_max": {
        "label_en": "Trail Trig Max",
        "group": "human",
        "unit": "ratio",
        "step": "0.001",
        "description": "自适应追踪止盈激活盈利上限（默认 0.018 = 1.8%）。",
    },
    "trail_pb_ratio": {
        "label_en": "Trail Pullback Ratio",
        "group": "human",
        "unit": "ratio",
        "step": "0.025",
        "description": "回撤幅度相对激活阈值的比例（默认 0.375）。",
    },
    "trail_pb_min": {
        "label_en": "Trail Pullback Min",
        "group": "human",
        "unit": "ratio",
        "step": "0.0005",
        "description": "追踪止盈回撤幅度下限（默认 0.0045 = 0.45%）。",
    },
    "trail_pb_max": {
        "label_en": "Trail Pullback Max",
        "group": "human",
        "unit": "ratio",
        "step": "0.0005",
        "description": "追踪止盈回撤幅度上限（默认 0.0065 = 0.65%）。",
    },
    "max_positions": {
        "label_en": "Max Concurrent Pos",
        "group": "human",
        "unit": "pos",
        "step": "1",
        "description": "全账户最多同时持有的并发合约仓位数（默认 3）。",
    },
    "margin_ratio": {
        "label_en": "Max Margin Ratio",
        "group": "human",
        "unit": "ratio",
        "step": "0.05",
        "description": "单笔保证金占当前总权益最大比例（默认 0.20 = 20%）。",
    },
    "max_margin_cap": {
        "label_en": "Max Margin Cap",
        "group": "human",
        "unit": "USDT",
        "step": "100",
        "description": "单笔开仓保证金绝对上限（默认 2500 USDT）。",
    },
    "unprot_2nd_scale": {
        "label_en": "2nd Unprot Scale",
        "group": "human",
        "unit": "x",
        "step": "0.1",
        "description": "已有1笔同向未浮盈保护单时，第2笔风险缩减系数（默认 0.60）。",
    },
    "unprot_3rd_scale": {
        "label_en": "3rd Unprot Scale",
        "group": "human",
        "unit": "x",
        "step": "0.1",
        "description": "已有>=2笔同向未浮盈保护单时，第3笔风险缩减系数（默认 0.40）。",
    },
    "daily_loss_limit": {
        "label_en": "Daily Loss Limit",
        "group": "human",
        "unit": "ratio",
        "step": "0.005",
        "description": "单日净值回撤触发熔断停开新仓比例（默认 0.02 = 2%）。",
    },
    "long_only": {
        "label_en": "Long Only",
        "group": "mode",
        "unit": "",
        "description": "默认关闭（双向交易）；开启后仅允许开多单。",
    },
    "candles_interval": {
        "label_en": "Candles Interval",
        "group": "mode",
        "unit": "",
        "description": "策略信号与指标计算的 K 线时间周期。",
    },
    "require_closed_candle_confirmation": {
        "label_en": "Closed Candle Confirm",
        "group": "mode",
        "unit": "",
        "description": "均值回归需等待最近一根已收盘 K 线与实时 Z-Score 方向一致才开仓。",
    },
    "manual_kill_switch": {
        "label_en": "Manual Kill Switch",
        "group": "mode",
        "unit": "",
        "description": "开启后暂停新开仓（仍会管理已有风控）。",
    },
    "momentum_lookback": {
        "label_en": "Momentum Lookback",
        "group": "momentum",
        "unit": "bars",
        "step": "1",
        "description": "动量模式计算涨跌幅回看的已收盘 K 线根数。",
    },
    "momentum_threshold": {
        "label_en": "Momentum Threshold",
        "group": "momentum",
        "unit": "ratio",
        "step": "0.001",
        "description": "触发动量开仓的最小区间收益率（例如 0.008 = 0.8%）。",
    },
    "risk_frac": {
        "label_en": "Risk Fraction",
        "group": "momentum",
        "unit": "ratio",
        "step": "0.005",
        "description": "动量模式单笔止损占名义上限的目标风险比例（如 0.02 = 2%）。",
    },
    "mean_window": {
        "label_en": "Mean Window",
        "group": "reversion",
        "unit": "bars",
        "step": "1",
        "description": "均值回归滚动均值与标准差窗口长度（根）。",
    },
    "entry_z_score": {
        "label_en": "Entry Z-Score",
        "group": "reversion",
        "unit": "σ",
        "step": "0.1",
        "description": "触发开仓的最小偏离标准差倍数 |Z|。",
    },
    "max_entry_z_score": {
        "label_en": "Max Entry Z-Score",
        "group": "reversion",
        "unit": "σ",
        "step": "0.1",
        "description": "防止极端插针暴走的最大入场 |Z| 上限，须大于入场 Z。",
    },
    "exit_z_score": {
        "label_en": "Exit Z-Score",
        "group": "reversion",
        "unit": "σ",
        "step": "0.05",
        "description": "回归目标 Z 阈值，须小于入场 Z。",
    },
    "min_std_pct": {
        "label_en": "Min Dispersion",
        "group": "reversion",
        "unit": "ratio",
        "step": "0.0005",
        "description": "最小相对标准差，过滤极度横盘无波动行情。",
    },
    "atr_length": {
        "label_en": "ATR Length",
        "group": "risk",
        "unit": "bars",
        "step": "1",
        "description": "真实波幅 ATR 的计算周期长度。",
    },
    "atr_stop_multiplier": {
        "label_en": "ATR Stop Mult",
        "group": "risk",
        "unit": "x",
        "step": "0.1",
        "description": "动态止损（或动量追踪回撤）对应的 ATR 倍数。",
    },
    "min_stop_loss": {
        "label_en": "Min Stop Loss",
        "group": "risk",
        "unit": "ratio",
        "step": "0.001",
        "description": "止损比例硬下限（如 0.004 = 0.4%）。",
    },
    "max_stop_loss": {
        "label_en": "Max Stop Loss",
        "group": "risk",
        "unit": "ratio",
        "step": "0.002",
        "description": "止损比例硬上限，波动率过高超限则放弃入场。",
    },
    "round_trip_cost_pct": {
        "label_en": "Round-Trip Cost",
        "group": "risk",
        "unit": "ratio",
        "step": "0.0002",
        "description": "预估双边手续费与摩擦成本（如 0.0012 = 0.12%）。",
    },
    "min_net_edge_pct": {
        "label_en": "Min Net Edge",
        "group": "risk",
        "unit": "ratio",
        "step": "0.0002",
        "description": "扣除往返成本后的最小预期净收益空间。",
    },
    "min_reward_risk": {
        "label_en": "Min Reward/Risk",
        "group": "risk",
        "unit": "R",
        "step": "0.1",
        "description": "均值回归建仓所需的最小净盈亏比。",
    },
    "quote_offset_pct": {
        "label_en": "Quote Offset",
        "group": "execution",
        "unit": "ratio",
        "step": "0.0001",
        "description": "Post-Only 挂单相对中间价的偏离比例。",
    },
    "max_bid_ask_spread_pct": {
        "label_en": "Max Spread",
        "group": "execution",
        "unit": "ratio",
        "step": "0.0005",
        "description": "允许下单的最大盘口买卖价差比例。",
    },
    "executor_refresh_time": {
        "label_en": "Order Refresh",
        "group": "execution",
        "unit": "s",
        "step": "10",
        "description": "未成交限价开仓单自动撤单刷新周期（秒），须小于持仓时限。",
    },
    "cooldown_time": {
        "label_en": "Cooldown",
        "group": "execution",
        "unit": "s",
        "step": "60",
        "description": "常规平仓后的冷却时间（秒）。",
    },
    "stop_loss_cooldown_time": {
        "label_en": "SL Cooldown",
        "group": "execution",
        "unit": "s",
        "step": "60",
        "description": "触发止损后的加长冷却时间（秒），须 ≥ 常规冷却。",
    },
    "time_limit": {
        "label_en": "Time Limit",
        "group": "execution",
        "unit": "s",
        "step": "300",
        "description": "单笔持仓最长持有时限（秒），到期强制平仓。",
    },
}

PRESETS = (
    {
        "key": "human_swing_11",
        "name": "Human 4H共振双向波段 (当前实盘)",
        "badge": "5m Human 波段",
        "description": "完全对齐 okx_human_trader：5m入场+15m ATR(2.0x)止损+4H/BTC/广度共振+自适应追踪止盈与+4.5%目标止盈。",
        "shared": {
            "signal_mode": "human_swing",
            "long_only": "false",
            "candles_interval": "5m",
            "require_closed_candle_confirmation": "true",
            "manual_kill_switch": "false",
            "require_btc_4h": "true",
            "min_breadth_frac": "0.60",
            "er4h_soft_thresh": "0.22",
            "er4h_soft_scale": "0.50",
            "max_dist_e144_atr15": "1.2",
            "swing_tp_pct": "0.045",
            "adaptive_trail": "true",
            "trail_atr_mult": "3.2",
            "trail_trig_min": "0.012",
            "trail_trig_max": "0.018",
            "trail_pb_ratio": "0.375",
            "trail_pb_min": "0.0045",
            "trail_pb_max": "0.0065",
            "max_positions": "3",
            "margin_ratio": "0.20",
            "max_margin_cap": "2500",
            "unprot_2nd_scale": "0.60",
            "unprot_3rd_scale": "0.40",
            "daily_loss_limit": "0.02",
            "momentum_lookback": "16",
            "momentum_threshold": "0.008",
            "risk_frac": "0.015",
            "mean_window": "48",
            "entry_z_score": "2",
            "max_entry_z_score": "3.2",
            "exit_z_score": "0.2",
            "min_std_pct": "0.001",
            "atr_length": "14",
            "atr_stop_multiplier": "2.0",
            "min_stop_loss": "0.008",
            "max_stop_loss": "0.15",
            "round_trip_cost_pct": "0.0007",
            "min_net_edge_pct": "0.0018",
            "min_reward_risk": "0.5",
            "quote_offset_pct": "0.0001",
            "max_bid_ask_spread_pct": "0.002",
            "executor_refresh_time": "300",
            "cooldown_time": "3600",
            "stop_loss_cooldown_time": "3600",
            "time_limit": "86400",
        },
    },
    {
        "key": "momentum_1h_both",
        "name": "1h 动量双向波段",
        "badge": "1h 双向动量",
        "description": "1小时 K 线，16 根动量 >0.8% 顺势触发，默认双向，3x ATR 追踪止损，最长持仓 7 天。",
        "shared": {
            "signal_mode": "momentum",
            "long_only": "false",
            "candles_interval": "1h",
            "require_closed_candle_confirmation": "true",
            "manual_kill_switch": "false",
            "momentum_lookback": "16",
            "momentum_threshold": "0.008",
            "risk_frac": "0.02",
            "mean_window": "16",
            "entry_z_score": "2",
            "max_entry_z_score": "3.2",
            "exit_z_score": "0.2",
            "min_std_pct": "0.001",
            "atr_length": "14",
            "atr_stop_multiplier": "3",
            "min_stop_loss": "0.004",
            "max_stop_loss": "0.06",
            "round_trip_cost_pct": "0.0012",
            "min_net_edge_pct": "0.0018",
            "min_reward_risk": "0.5",
            "quote_offset_pct": "0.0002",
            "max_bid_ask_spread_pct": "0.002",
            "executor_refresh_time": "60",
            "cooldown_time": "3600",
            "stop_loss_cooldown_time": "3600",
            "time_limit": "604800",
        },
    },
    {
        "key": "reversion_5m_classic",
        "name": "5m 经典双向均值回归",
        "badge": "5m 回归",
        "description": "5分钟 K 线，48 根滚动均值，2.0σ–3.5σ 双向捕捉回归机会，1.5x ATR 止损。",
        "shared": {
            "signal_mode": "reversion",
            "long_only": "false",
            "candles_interval": "5m",
            "require_closed_candle_confirmation": "true",
            "manual_kill_switch": "false",
            "momentum_lookback": "16",
            "momentum_threshold": "0.008",
            "risk_frac": "0.02",
            "mean_window": "48",
            "entry_z_score": "2.0",
            "max_entry_z_score": "3.5",
            "exit_z_score": "0.4",
            "min_std_pct": "0.001",
            "atr_length": "14",
            "atr_stop_multiplier": "1.5",
            "min_stop_loss": "0.006",
            "max_stop_loss": "0.02",
            "round_trip_cost_pct": "0.0012",
            "min_net_edge_pct": "0.0018",
            "min_reward_risk": "1.0",
            "quote_offset_pct": "0.0002",
            "max_bid_ask_spread_pct": "0.002",
            "executor_refresh_time": "60",
            "cooldown_time": "300",
            "stop_loss_cooldown_time": "900",
            "time_limit": "3600",
        },
    },
    {
        "key": "reversion_15m_conservative",
        "name": "15m 稳健均值回归",
        "badge": "15m 稳健",
        "description": "15分钟过滤短线噪音，更高入场 Z-Score (2.2σ) 与更长冷静期，降低震荡磨损。",
        "shared": {
            "signal_mode": "reversion",
            "long_only": "false",
            "candles_interval": "15m",
            "require_closed_candle_confirmation": "true",
            "manual_kill_switch": "false",
            "momentum_lookback": "16",
            "momentum_threshold": "0.008",
            "risk_frac": "0.015",
            "mean_window": "40",
            "entry_z_score": "2.2",
            "max_entry_z_score": "3.6",
            "exit_z_score": "0.3",
            "min_std_pct": "0.0015",
            "atr_length": "14",
            "atr_stop_multiplier": "1.8",
            "min_stop_loss": "0.006",
            "max_stop_loss": "0.025",
            "round_trip_cost_pct": "0.0012",
            "min_net_edge_pct": "0.002",
            "min_reward_risk": "1.2",
            "quote_offset_pct": "0.0002",
            "max_bid_ask_spread_pct": "0.002",
            "executor_refresh_time": "90",
            "cooldown_time": "600",
            "stop_loss_cooldown_time": "1800",
            "time_limit": "10800",
        },
    },
    {
        "key": "momentum_15m_both",
        "name": "15m 双向动量波段",
        "badge": "15m 双向动量",
        "description": "15分钟 K 线捕捉日内双向突破，1.2% 动量阈值 + 2.5x ATR 追踪止损。",
        "shared": {
            "signal_mode": "momentum",
            "long_only": "false",
            "candles_interval": "15m",
            "require_closed_candle_confirmation": "true",
            "manual_kill_switch": "false",
            "momentum_lookback": "12",
            "momentum_threshold": "0.012",
            "risk_frac": "0.015",
            "mean_window": "24",
            "entry_z_score": "2.0",
            "max_entry_z_score": "3.5",
            "exit_z_score": "0.3",
            "min_std_pct": "0.001",
            "atr_length": "14",
            "atr_stop_multiplier": "2.5",
            "min_stop_loss": "0.005",
            "max_stop_loss": "0.04",
            "round_trip_cost_pct": "0.0012",
            "min_net_edge_pct": "0.0018",
            "min_reward_risk": "0.8",
            "quote_offset_pct": "0.0002",
            "max_bid_ask_spread_pct": "0.002",
            "executor_refresh_time": "60",
            "cooldown_time": "900",
            "stop_loss_cooldown_time": "1800",
            "time_limit": "86400",
        },
    },
)


class ConfigError(ValueError):
    """User-facing configuration error."""


def active_controller_names(script_file: Path = SCRIPT_FILE) -> list[str]:
    if not script_file.is_file():
        raise ConfigError(f"找不到策略清单 {script_file}")
    names = []
    for line in script_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("- ") and stripped.endswith(".yml"):
            name = stripped[2:].strip()
            if not FILE_NAME_RE.fullmatch(name):
                raise ConfigError(f"策略清单包含不支持的控制器文件 {name}")
            names.append(name)
    if not names:
        raise ConfigError("策略清单里没有控制器")
    if len(names) != len(set(names)):
        raise ConfigError("策略清单里有重复的控制器")
    return names


def all_controller_names(script_file: Path = SCRIPT_FILE, controllers_dir: Path = CONTROLLERS_DIR) -> list[str]:
    """Return active controller filenames in script order, followed by inactive valid controllers."""
    active = active_controller_names(script_file)
    active_set = set(active)
    extras = []
    if controllers_dir.is_dir():
        for path in sorted(controllers_dir.glob("conf_okx_pmm_*.yml")):
            if FILE_NAME_RE.fullmatch(path.name) and path.name not in active_set:
                extras.append(path.name)
    return active + extras


def update_script_controllers(enabled_files: list[str], script_file: Path = SCRIPT_FILE,
                              controllers_dir: Path = CONTROLLERS_DIR) -> list[str]:
    """Update controllers_config list in conf_okx_multi.yml cleanly."""
    if not script_file.is_file():
        raise ConfigError(f"找不到策略清单 {script_file}")
    cleaned: list[str] = []
    for raw_name in enabled_files:
        name = str(raw_name).strip()
        if not FILE_NAME_RE.fullmatch(name):
            raise ConfigError(f"未知的控制器文件 {name}")
        if not (controllers_dir / name).is_file():
            raise ConfigError(f"找不到控制器配置 {name}")
        if name in cleaned:
            raise ConfigError(f"策略清单中有重复的控制器 {name}")
        cleaned.append(name)
    if not cleaned:
        raise ConfigError("至少需要启用 1 个交易对控制器")

    original = script_file.read_text(encoding="utf-8")
    lines = original.splitlines()
    out_lines: list[str] = []
    in_controllers = False
    wrote_list = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("controllers_config:"):
            out_lines.append("controllers_config:")
            for name in cleaned:
                out_lines.append(f"  - {name}")
            in_controllers = True
            wrote_list = True
            continue
        if in_controllers:
            if stripped.startswith("- ") or stripped == "":
                continue
            if not line.startswith(" ") and ":" in line:
                in_controllers = False
                out_lines.append(line)
            continue
        out_lines.append(line)
    if not wrote_list:
        out_lines.append("controllers_config:")
        for name in cleaned:
            out_lines.append(f"  - {name}")
    rendered = "\n".join(out_lines) + "\n"
    if rendered != original:
        _atomic_write(script_file, rendered)
    return cleaned


def parse_flat_yaml(text: str) -> dict[str, str]:
    data = {}
    for line in text.splitlines():
        match = KEY_RE.match(line)
        if match and not line.lstrip().startswith("#"):
            data[match.group(1)] = match.group(2)
    return data


def render_updates(text: str, updates: dict[str, str]) -> str:
    remaining = dict(updates)
    lines = []
    for line in text.splitlines():
        match = KEY_RE.match(line)
        if match and match.group(1) in remaining and not line.lstrip().startswith("#"):
            key = match.group(1)
            val = remaining.pop(key)
            if val == "":
                lines.append(f"{key}: null")
            else:
                lines.append(f"{key}: {val}")
        else:
            lines.append(line)
    for key, value in remaining.items():
        if value != "":
            lines.append(f"{key}: {value}")
    body = "\n".join(lines)
    if text.endswith("\n") or not text:
        body += "\n"
    return body


def _parse_controller_file(path: Path) -> tuple[dict[str, str], set[str]]:
    raw = parse_flat_yaml(path.read_text(encoding="utf-8"))
    present_keys = set(raw.keys())
    required = PAIR_KEYS | CORE_SHARED_KEYS | {"trading_pair"}
    missing = [key for key in required if key not in raw]
    if missing:
        raise ConfigError(f"{path.name} 缺少字段: {', '.join(missing)}")
    for key, default in OPTIONAL_SHARED_DEFAULTS.items():
        if key not in raw or raw[key] in ("null", "None", "", None):
            raw[key] = default
    return raw, present_keys


def _field_specs_payload() -> list[dict]:
    specs = []
    for key, label, kind, bounds in tuple(PAIR_FIELDS) + tuple(SHARED_FIELDS):
        meta = FIELD_META.get(key, {})
        item = {
            "key": key,
            "label": label,
            "label_en": meta.get("label_en", key),
            "group": meta.get("group", "other"),
            "kind": kind,
            "unit": meta.get("unit", ""),
            "step": meta.get("step", ""),
            "description": meta.get("description", ""),
            "default": OPTIONAL_SHARED_DEFAULTS.get(key, ""),
        }
        if kind == "choice":
            item["choices"] = list(bounds)
        elif kind == "int":
            item["min"] = bounds[0]
            item["max"] = bounds[1]
        elif kind == "decimal":
            item["bounds"] = bounds
        specs.append(item)
    return specs


def load_config(
    script_file: Path = SCRIPT_FILE,
    controllers_dir: Path = CONTROLLERS_DIR,
    include_inactive: bool = False,
) -> dict:
    active_names = active_controller_names(script_file)
    active_set = set(active_names)
    names = all_controller_names(script_file, controllers_dir) if include_inactive else active_names

    parsed_active = []
    parsed_all = []
    for name in names:
        path = controllers_dir / name
        if not path.is_file():
            raise ConfigError(f"找不到控制器配置 {name}")
        raw, present_keys = _parse_controller_file(path)
        entry = (name, raw, present_keys, name in active_set)
        parsed_all.append(entry)
        if name in active_set:
            parsed_active.append(entry)

    shared = {key: parsed_active[0][1][key] for key in SHARED_KEYS}
    mixed = {key: False for key in SHARED_KEYS}
    for _, raw, _, _ in parsed_active[1:]:
        for key in SHARED_KEYS:
            if raw[key] != shared[key]:
                mixed[key] = True

    def _pair_entry(name: str, raw: dict[str, str], present_keys: set[str], enabled: bool) -> dict:
        overrides = {
            key: raw[key]
            for key in SHARED_KEYS
            if raw[key] != shared[key]
        }
        values = {key: raw[key] for key in EDITABLE_KEYS}
        return {
            "file": name,
            "id": raw.get("id", name[:-4]),
            "trading_pair": raw["trading_pair"],
            "enabled": enabled,
            "leverage": raw["leverage"],
            "total_amount_quote": raw["total_amount_quote"],
            "values": values,
            "overrides": overrides,
            "has_overrides": bool(overrides),
        }

    pairs = [_pair_entry(n, r, p, e) for n, r, p, e in parsed_active]
    all_pairs = [_pair_entry(n, r, p, e) for n, r, p, e in parsed_all]

    result = {
        "pairs": pairs,
        "all_pairs": all_pairs,
        "shared": shared,
        "shared_mixed": mixed,
        "defaults": {"leverage": 3, **OPTIONAL_SHARED_DEFAULTS},
        "strategy": get_strategy_info_from_parsed(shared, parsed_active),
        "field_specs": _field_specs_payload(),
        "presets": list(PRESETS),
        "side_mode": _side_mode_from_parsed(parsed_active),
    }
    return result


def _side_mode_from_parsed(parsed_active: list[tuple[str, dict[str, str], set[str], bool]]) -> str:
    flags = [_long_only_enabled(raw) for _, raw, _, _ in parsed_active]
    if all(flags):
        return "long_only"
    if not any(flags):
        return "both"
    return "mixed"


def apply_config(
    payload: dict,
    script_file: Path = SCRIPT_FILE,
    controllers_dir: Path = CONTROLLERS_DIR,
) -> dict:
    if not isinstance(payload, dict):
        raise ConfigError("请求体必须是 JSON 对象")

    # Optional: update enabled controllers list in conf_okx_multi.yml first when requested.
    enabled_files = payload.get("enabled_files")
    if enabled_files is not None:
        if not isinstance(enabled_files, list):
            raise ConfigError("enabled_files 必须是数组")
        update_script_controllers(enabled_files, script_file, controllers_dir)

    current_names = active_controller_names(script_file)
    pairs = payload.get("pairs")
    shared = payload.get("shared")
    if not isinstance(pairs, list) or not isinstance(shared, dict):
        raise ConfigError("需要 pairs 和 shared")

    incoming = []
    for item in pairs:
        if not isinstance(item, dict):
            raise ConfigError("pairs 中的每一项都必须是对象")
        name = str(item.get("file", ""))
        if not FILE_NAME_RE.fullmatch(name):
            raise ConfigError(f"未知的控制器文件 {name}")
        if enabled_files is None and name not in current_names:
            raise ConfigError(f"未知的控制器文件 {name}")
        if not (controllers_dir / name).is_file():
            raise ConfigError(f"未知的控制器文件 {name}")
        incoming.append(name)

    if enabled_files is None and sorted(incoming) != sorted(current_names):
        raise ConfigError("必须同时提交当前已启用的全部币种")
    if enabled_files is not None:
        missing_active = set(current_names) - set(incoming)
        if missing_active:
            raise ConfigError(f"已启用的控制器缺少配置提交: {', '.join(sorted(missing_active))}")

    originals: dict[str, tuple[dict[str, str], set[str]]] = {}
    for name in incoming:
        originals[name] = _parse_controller_file(controllers_dir / name)

    # Support legacy callers that omit OPTIONAL_SHARED_DEFAULTS from shared dict.
    shared_with_defaults = dict(shared)
    for opt_key, opt_default in OPTIONAL_SHARED_DEFAULTS.items():
        if opt_key not in shared_with_defaults:
            shared_with_defaults[opt_key] = opt_default

    shared_values = _normalize_fields(shared_with_defaults, SHARED_FIELDS)
    normalized_pairs = []
    for item in pairs:
        name = str(item["file"])
        pair_values = _normalize_fields(item, PAIR_FIELDS)
        raw_overrides = item.get("overrides") or {}
        if not isinstance(raw_overrides, dict):
            raise ConfigError(f"{name}: overrides 必须是对象")

        # Validate per-pair overrides for any shared key
        override_specs = [spec for spec in SHARED_FIELDS if spec[0] in raw_overrides]
        override_values = _normalize_fields(raw_overrides, override_specs) if override_specs else {}

        merged_shared = {**shared_values, **override_values}
        merged_all = {**merged_shared, **pair_values}
        _validate_relationships(name, merged_all)
        normalized_pairs.append((name, pair_values, merged_shared, override_values))

    written = []
    for name, pair_values, merged_shared, override_values in normalized_pairs:
        path = controllers_dir / name
        original_text = path.read_text(encoding="utf-8")
        orig_raw, present_keys = originals[name]

        updates: dict[str, str] = {}
        for key, val in {**merged_shared, **pair_values}.items():
            # Preserve legacy files that didn't explicitly store optional keys unless changed or overridden.
            if (
                key in OPTIONAL_SHARED_DEFAULTS
                and key not in present_keys
                and key not in override_values
                and _values_equal(val, OPTIONAL_SHARED_DEFAULTS[key])
            ):
                continue
            updates[key] = _preserve_spelling(orig_raw.get(key) if key in present_keys else None, val)

        updated = render_updates(original_text, updates)
        if updated != original_text:
            _atomic_write(path, updated)
        written.append(name)
    return {"files": written, "enabled_files": current_names}


def add_pair_config(
    trading_pair: str,
    leverage: str | int = 3,
    total_amount_quote: str | float = 50,
    enable: bool = True,
    script_file: Path = SCRIPT_FILE,
    controllers_dir: Path = CONTROLLERS_DIR,
) -> dict:
    """Create a new controller YAML cloned from the first active controller."""
    pair = str(trading_pair or "").strip().upper()
    if not PAIR_NAME_RE.fullmatch(pair):
        raise ConfigError("交易对格式无效，须为类似 LINK-USDT 的大写合约名")
    base = pair.split("-", 1)[0].lower()
    file_name = f"conf_okx_pmm_{base}.yml"
    if not FILE_NAME_RE.fullmatch(file_name):
        raise ConfigError("交易对文件名不合法")
    target_path = controllers_dir / file_name
    if target_path.exists():
        raise ConfigError(f"{pair} 控制器配置文件 {file_name} 已存在")

    active = active_controller_names(script_file)
    template_path = controllers_dir / active[0]
    template_text = template_path.read_text(encoding="utf-8")
    template_raw = parse_flat_yaml(template_text)

    pair_fields = _normalize_fields(
        {
            "leverage": leverage,
            "total_amount_quote": total_amount_quote,
        },
        PAIR_FIELDS,
    )
    updates = {
        "id": f"okx_pmm_{base}",
        "trading_pair": pair,
        "long_only": "false",
        **pair_fields,
    }
    # Preserve key order from template
    new_text = render_updates(template_text, updates)
    # Check duplicate trading_pair across existing controllers
    for existing in controllers_dir.glob("conf_okx_pmm_*.yml"):
        raw_existing = parse_flat_yaml(existing.read_text(encoding="utf-8"))
        if raw_existing.get("trading_pair", "").upper() == pair:
            raise ConfigError(f"交易对 {pair} 已存在于 {existing.name}")
    _atomic_write(target_path, new_text)
    if enable:
        update_script_controllers(active + [file_name], script_file, controllers_dir)
    return {"file": file_name, "trading_pair": pair, "enabled": enable, "id": f"okx_pmm_{base}",
            "template_interval": template_raw.get("candles_interval", "1h")}


def _values_equal(left: str, right: str) -> bool:
    if left == right:
        return True
    try:
        return Decimal(left) == Decimal(right)
    except (InvalidOperation, ValueError):
        return False


def _preserve_spelling(previous: str | None, normalized: str) -> str:
    if previous is None:
        return normalized
    if previous == normalized:
        return previous
    try:
        if Decimal(previous) == Decimal(normalized):
            return previous
    except (InvalidOperation, ValueError):
        pass
    return normalized


def _normalize_fields(source: dict, specs) -> dict[str, str]:
    values = {}
    for key, label, kind, bounds in specs:
        if key not in source:
            raise ConfigError(f"缺少 {label}")
        values[key] = _normalize_value(label, source[key], kind, bounds)
    return values


def _normalize_value(label: str, raw, kind: str, bounds) -> str:
    if isinstance(raw, bool) and kind == "bool":
        return "true" if raw else "false"
    text = str(raw).strip()
    if kind == "bool":
        lowered = text.lower()
        if lowered in {"true", "yes", "1"}:
            return "true"
        if lowered in {"false", "no", "0"}:
            return "false"
        raise ConfigError(f"{label} 只能是 true 或 false")
    if kind == "choice":
        if text not in bounds:
            raise ConfigError(f"{label} 只能是 {', '.join(bounds)}")
        return text
    if kind == "int":
        if not re.fullmatch(r"-?\d+", text):
            raise ConfigError(f"{label} 必须是整数")
        value = int(text)
        lo, hi = bounds
        if lo is not None and value < lo:
            raise ConfigError(f"{label} 不能小于 {lo}")
        if hi is not None and value > hi:
            raise ConfigError(f"{label} 不能大于 {hi}")
        return str(value)
    if kind == "decimal":
        try:
            value = Decimal(text)
        except (InvalidOperation, ValueError):
            raise ConfigError(f"{label} 不是有效数字")
        if not value.is_finite():
            raise ConfigError(f"{label} 不是有效数字")
        for op, limit in bounds.items():
            limit = Decimal(str(limit))
            if op == "gt" and not value > limit:
                raise ConfigError(f"{label} 必须大于 {limit}")
            if op == "ge" and not value >= limit:
                raise ConfigError(f"{label} 不能小于 {limit}")
            if op == "lt" and not value < limit:
                raise ConfigError(f"{label} 必须小于 {limit}")
            if op == "le" and not value <= limit:
                raise ConfigError(f"{label} 不能大于 {limit}")
        rendered = format(value, "f")
        if "." in rendered:
            rendered = rendered.rstrip("0").rstrip(".")
        return rendered or "0"
    raise ConfigError(f"未知字段类型 {kind}")


def _validate_relationships(name: str, values: dict[str, str]) -> None:
    exit_z = Decimal(values["exit_z_score"])
    entry_z = Decimal(values["entry_z_score"])
    max_z = Decimal(values["max_entry_z_score"])
    if not exit_z < entry_z < max_z:
        raise ConfigError(f"{name}: 需要 出场 Z < 入场 Z < 最大入场 Z")
    if Decimal(values["min_stop_loss"]) > Decimal(values["max_stop_loss"]):
        raise ConfigError(f"{name}: 止损下限不能大于止损上限")
    if Decimal(values["trail_trig_min"]) > Decimal(values["trail_trig_max"]):
        raise ConfigError(f"{name}: 追踪激活下限不能大于上限")
    if Decimal(values["trail_pb_min"]) > Decimal(values["trail_pb_max"]):
        raise ConfigError(f"{name}: 追踪回撤下限不能大于上限")
    if int(values["stop_loss_cooldown_time"]) < int(values["cooldown_time"]):
        raise ConfigError(f"{name}: 止损冷静期不能短于交易冷静期")
    if int(values["executor_refresh_time"]) >= int(values["time_limit"]):
        raise ConfigError(f"{name}: 挂单刷新必须短于持仓时限")


def _atomic_write(path: Path, text: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _long_only_enabled(raw: dict) -> bool:
    flag = str(raw.get("long_only", "true")).strip().lower()
    return flag not in ("false", "no", "0", "null", "none")


def read_side_mode(script_file: Path = SCRIPT_FILE, controllers_dir: Path = CONTROLLERS_DIR) -> str:
    """Return long_only, both, or mixed from the active controller files."""
    names = active_controller_names(script_file)
    flags = []
    for name in names:
        path = controllers_dir / name
        if not path.is_file():
            raise ConfigError(f"找不到控制器配置 {name}")
        flags.append(_long_only_enabled(parse_flat_yaml(path.read_text(encoding="utf-8"))))
    if all(flags):
        return "long_only"
    if not any(flags):
        return "both"
    return "mixed"


def set_side_mode(
    long_only: bool,
    script_file: Path = SCRIPT_FILE,
    controllers_dir: Path = CONTROLLERS_DIR,
) -> list[str]:
    """Write the same long_only value into every active controller file."""
    value = "true" if long_only else "false"
    written = []
    for name in active_controller_names(script_file):
        path = controllers_dir / name
        if not path.is_file():
            raise ConfigError(f"找不到控制器配置 {name}")
        original = path.read_text(encoding="utf-8")
        updated = render_updates(original, {"long_only": value})
        if updated != original:
            _atomic_write(path, updated)
        written.append(name)
    return written


def get_strategy_info_from_parsed(shared: dict, parsed_active: list) -> dict:
    info = dict(STRATEGY_INFO)
    try:
        modes = {raw.get("signal_mode", "reversion") for _, raw, _, _ in parsed_active}
        interval = shared.get("candles_interval") or info["interval"]
        mean_window = int(shared.get("mean_window") or info["mean_window"])
        time_limit = int(shared.get("time_limit") or info["time_limit"])
        lookback = int(shared.get("momentum_lookback") or 16)
        mom_thresh = float(shared.get("momentum_threshold") or 0.008)
        atr_mult = shared.get("atr_stop_multiplier") or "3"
        mode_str = next(iter(modes)) if len(modes) == 1 else "mixed"
        side_mode = _side_mode_from_parsed(parsed_active)

        info["interval"] = str(interval)
        info["mean_window"] = mean_window
        info["time_limit"] = time_limit
        info["signal_mode"] = mode_str
        info["side_mode"] = side_mode
        if mode_str == "human_swing":
            info["key"] = "human_swing_11"
            info["name"] = f"{interval} Human 双向波段"
            info["badge"] = f"{interval} Human 波段 · {len(parsed_active)} 币种"
            info["description"] = (
                f"4H结构共振 + BTC共振 + {len(parsed_active)}币种广度 ≥ {float(shared.get('min_breadth_frac', 0.6))*100:.0f}%；"
                f"15m ATR({atr_mult}x) 止损，目标 +{float(shared.get('swing_tp_pct', 0.045))*100:.1f}%，"
                f"单笔风险 {float(shared.get('risk_frac', 0.015))*100:.1f}%。"
            )
        elif mode_str == "momentum":
            info["key"] = f"momentum_{interval}"
            info["name"] = f"{interval} 动量策略"
            info["badge"] = f"{interval} 动量策略"
            info["description"] = (
                f"{interval} K 线，回看 {lookback} 根涨跌幅 > {mom_thresh * 100:.2f}% 顺势触发，"
                f"{atr_mult}x ATR 动态止损与追踪止盈，持仓时限 {time_limit} 秒。"
            )
        elif mode_str == "reversion":
            info["key"] = f"reversion_{interval}"
            info["name"] = f"{interval} 均值回归"
            info["badge"] = f"{interval} 均值回归"
            info["description"] = (
                f"{interval} K 线，{mean_window} 根均值窗口，"
                f"Z-Score [{shared.get('entry_z_score', '2.0')}, {shared.get('max_entry_z_score', '3.5')}] 入场，"
                f"{atr_mult}x ATR 止损，持仓时限 {time_limit} 秒。"
            )
        else:
            info["key"] = "mixed_strategy"
            info["name"] = f"混合策略矩阵 ({len(parsed_active)} 币种)"
            info["badge"] = "多策略混合"
            info["description"] = "各交易对分别运行动量趋势或均值回归控制器，具体参数以单币种配置为准。"
    except Exception:
        pass
    return info


def get_strategy_info(controllers_dir: Path = CONTROLLERS_DIR) -> dict:
    """Return active strategy metadata dynamically calculated from active configuration."""
    try:
        cfg = load_config(controllers_dir=controllers_dir)
        return cfg["strategy"]
    except Exception:
        return dict(STRATEGY_INFO)


def get_presets_info(controllers_dir: Path = CONTROLLERS_DIR) -> dict:
    """Return active strategy info and curated presets."""
    strat = get_strategy_info(controllers_dir=controllers_dir)
    return {
        "active_preset": strat.get("key", "momentum_1h_long"),
        "active_meta": strat,
        "presets": list(PRESETS),
    }

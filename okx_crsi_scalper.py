"""
===================================================================================
OKX 永续合约超短线极值动量与自适应趋势策略 (OKX CRSI Ultra-Short Scalper)
===================================================================================
策略类型：
    超短线统计套利 / 极值均值回归与微观动量衰竭反转 (Extreme Mean Reversion & Momentum Exhaustion)
适用标的：
    OKX USDT 永续合约 (BTC, ETH, SOL, DOGE, XRP, ADA, SUI, LINK, LTC, AVAX 等)
K线周期：
    5 分钟 (5m)
平均持仓周期：
    10 ~ 15 分钟 (超短线高胜率离场)

核心交易哲学与逻辑：
1. 极值动量衰竭 (Micro-Exhaustion)：
   在 5 分钟级别上，当 3 周期短线 RSI (Connors RSI 核心指标) 跌破 5 (极度超跌) 或突破 96 (极度超买) 时，
   反映出市场上急切止损盘/爆仓盘的恐慌清算。此时价格往往严重偏离合理微观均值，反转弹射几率极高。
2. 宏观趋势非对称滤波 (Macro Trend Asymmetry Filter)：
   采用 200 周期 EMA (约 16 小时宏观趋势) 判定多空基调。
   - 当价格位于 EMA200 之下 (空头主导市)：放宽做空极值阈值 (顺势极值反弹即可做空)，严格限制做多阈值；
   - 当价格位于 EMA200 之上 (多头主导市)：放宽做多极值阈值，严格限制做空阈值。
3. K线微观结构确认 (Price Action Confirmation)：
   仅当极值发生且当前 K 线收出明确反转结构 (如阳线收盘 c > o，或阴线收盘 c < o) 时才触发下单，避免接正在暴跌的飞刀。
4. 敏捷动态离场与资金管理 (Dynamic Multi-Stage Exit)：
   - 快速平价回归止盈 (TP Equilibrium)：RSI 回归至中轴 55 附近即刻锁定利润，平均持仓仅 2 根 K 线。
   - 目标浮盈现金止盈 (Target Cash TP)：价格浮盈达到 +0.8% 时主动止盈。
   - 刚性 ATR 动态止损 (Hard Risk Gate)：以 1.8 * ATR(14) 作为底层不可逾越的止损保护。
   - 超时强平机制 (Time Limit Exit)：持仓达到 12 根 K 线 (60分钟) 若未完成回归则按市价平仓防钝化。
===================================================================================
"""

from __future__ import annotations
import numpy as np
import pandas as pd
from pathlib import Path
from dataclasses import dataclass
from typing import Dict, List, Optional
import argparse

SYMBOLS_10 = [
    "BTC-USDT-SWAP",
    "ETH-USDT-SWAP",
    "SOL-USDT-SWAP",
    "DOGE-USDT-SWAP",
    "XRP-USDT-SWAP",
    "ADA-USDT-SWAP",
    "SUI-USDT-SWAP",
    "LINK-USDT-SWAP",
    "LTC-USDT-SWAP",
    "AVAX-USDT-SWAP"
]

@dataclass
class ScalperConfig:
    rsi_len: int = 3                # 短线动量 RSI 周期 (3 根 5m K线)
    os_level: float = 5.0           # 基础超卖阈值 (0-100)
    ob_level: float = 96.0          # 基础超买阈值 (0-100)
    exit_rsi: float = 55.0          # 动态回归平仓 RSI 阈值
    tp_pct: float = 0.008           # 固定目标止盈比例 (0.8%)
    sl_atr_mult: float = 1.8        # ATR 止损倍数
    max_hold_bars: int = 12         # 最大持仓根数 (12 * 5m = 60分钟)
    trend_ema_len: int = 200        # 宏观趋势 EMA 长度 (200 根 5m = 16.6 小时)
    trend_asym: bool = True         # 是否启用宏观趋势非对称偏置
    fee_rate: float = 0.0004        # 单边综合费率预估 (0.04%，含 Maker/Taker 混合与滑点)

class OKXCRSIScalper:
    def __init__(self, config: Optional[ScalperConfig] = None):
        self.config = config or ScalperConfig()

    def calculate_indicators(self, df: pd.DataFrame) -> dict:
        c = df["c"].to_numpy(dtype=np.float64)
        h = df["h"].to_numpy(dtype=np.float64)
        l = df["l"].to_numpy(dtype=np.float64)
        o = df["o"].to_numpy(dtype=np.float64)
        
        # 1. 计算超短线 RSI
        diff = pd.Series(c).diff()
        up = diff.clip(lower=0)
        down = -1 * diff.clip(upper=0)
        ma_up = up.ewm(alpha=1.0 / self.config.rsi_len, adjust=False).mean()
        ma_down = down.ewm(alpha=1.0 / self.config.rsi_len, adjust=False).mean()
        rsi = (100.0 - (100.0 / (1.0 + ma_up / (ma_down + 1e-9)))).to_numpy()
        
        # 2. 计算真实波幅 ATR(14)
        prev_c = np.roll(c, 1)
        prev_c[0] = c[0]
        tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
        atr = pd.Series(tr).rolling(14, min_periods=14).mean().to_numpy()
        
        # 3. 计算趋势 EMA
        ema_trend = pd.Series(c).ewm(span=self.config.trend_ema_len, adjust=False).mean().to_numpy()
        
        return {
            "c": c, "h": h, "l": l, "o": o,
            "rsi": rsi, "atr": atr, "ema_trend": ema_trend,
            "n": len(c)
        }

    def backtest_single_symbol(self, df: pd.DataFrame, sym: str = "") -> list:
        ind = self.calculate_indicators(df)
        c, h, l, o = ind["c"], ind["h"], ind["l"], ind["o"]
        rsi, atr, ema_t, n = ind["rsi"], ind["atr"], ind["ema_trend"], ind["n"]
        cfg = self.config
        
        trades = []
        in_pos = 0 # 1: 多头, -1: 空头
        entry_p = 0.0
        entry_i = 0
        stop_p = 0.0
        tp_p = 0.0
        
        start_i = max(cfg.trend_ema_len, 50)
        for i in range(start_i, n - 1):
            next_o = o[i+1]
            curr_c = c[i]
            curr_h = h[i]
            curr_l = l[i]
            curr_atr = atr[i]
            
            # --- 持仓管理与出场判定 ---
            if in_pos == 1:
                exit_p = None
                exit_reason = ""
                if curr_h >= tp_p:
                    exit_p = tp_p
                    exit_reason = "TP_TARGET"
                elif rsi[i] >= cfg.exit_rsi:
                    exit_p = curr_c
                    exit_reason = "TP_RSI_REVERT"
                elif curr_l <= stop_p:
                    exit_p = stop_p
                    exit_reason = "SL_ATR"
                elif i - entry_i >= cfg.max_hold_bars:
                    exit_p = curr_c
                    exit_reason = "TIME_LIMIT"
                    
                if exit_p is not None:
                    net_ret = (exit_p - entry_p) / entry_p - 2.0 * cfg.fee_rate
                    trades.append({
                        "sym": sym, "entry_i": entry_i, "exit_i": i,
                        "side": "LONG", "entry_p": entry_p, "exit_p": exit_p,
                        "ret": net_ret, "bars": i - entry_i, "reason": exit_reason
                    })
                    in_pos = 0
                    continue
                    
            elif in_pos == -1:
                exit_p = None
                exit_reason = ""
                if curr_l <= tp_p:
                    exit_p = tp_p
                    exit_reason = "TP_TARGET"
                elif rsi[i] <= (100.0 - cfg.exit_rsi):
                    exit_p = curr_c
                    exit_reason = "TP_RSI_REVERT"
                elif curr_h >= stop_p:
                    exit_p = stop_p
                    exit_reason = "SL_ATR"
                elif i - entry_i >= cfg.max_hold_bars:
                    exit_p = curr_c
                    exit_reason = "TIME_LIMIT"
                    
                if exit_p is not None:
                    net_ret = (entry_p - exit_p) / entry_p - 2.0 * cfg.fee_rate
                    trades.append({
                        "sym": sym, "entry_i": entry_i, "exit_i": i,
                        "side": "SHORT", "entry_p": entry_p, "exit_p": exit_p,
                        "ret": net_ret, "bars": i - entry_i, "reason": exit_reason
                    })
                    in_pos = 0
                    continue
                    
            # --- 信号触发与入场判定 ---
            if in_pos == 0:
                is_bear_trend = (curr_c < ema_t[i])
                
                # 趋势非对称偏置阈值
                if cfg.trend_asym:
                    if is_bear_trend:
                        curr_os = cfg.os_level - 2.0
                        curr_ob = cfg.ob_level - 2.5
                    else:
                        curr_os = cfg.os_level + 2.5
                        curr_ob = cfg.ob_level + 2.0
                else:
                    curr_os = cfg.os_level
                    curr_ob = cfg.ob_level
                    
                # 做多信号：超跌 + 阳线反弹
                long_trigger = (rsi[i] <= curr_os) and (curr_c > o[i])
                # 做空信号：超买 + 阴线回落
                short_trigger = (rsi[i] >= curr_ob) and (curr_c < o[i])
                
                if long_trigger:
                    in_pos = 1
                    entry_p = next_o
                    entry_i = i + 1
                    stop_p = entry_p - cfg.sl_atr_mult * curr_atr
                    tp_p = entry_p * (1.0 + cfg.tp_pct)
                    continue
                elif short_trigger:
                    in_pos = -1
                    entry_p = next_o
                    entry_i = i + 1
                    stop_p = entry_p + cfg.sl_atr_mult * curr_atr
                    tp_p = entry_p * (1.0 - cfg.tp_pct)
                    continue

        return trades

def run_portfolio_backtest():
    scalper = OKXCRSIScalper()
    all_trades = []
    symbol_stats = []
    
    print("\n" + "=" * 80)
    print("  OKX 永续合约超短线策略 (CRSI-Trend-Adaptive) 回测报告")
    print("  回测周期: 最近1个月 (2026-10-24 至 2026-11-23) | 周期: 5m")
    print("=" * 80)
    
    for sym in SYMBOLS_10:
        csv_path = Path(f"data/okx_candles/{sym}_5m_last_month.csv")
        if not csv_path.exists():
            print(f"File not found: {csv_path}")
            continue
        df = pd.read_csv(csv_path)
        trades = scalper.backtest_single_symbol(df, sym=sym)
        all_trades.extend(trades)
        
        if trades:
            df_t = pd.DataFrame(trades)
            wins = df_t[df_t["ret"] > 0]
            losses = df_t[df_t["ret"] <= 0]
            wr = len(wins) / len(df_t) * 100.0
            tot_ret = ((1.0 + df_t["ret"]).prod() - 1.0) * 100.0
            gain_sum = wins["ret"].sum()
            loss_sum = abs(losses["ret"].sum())
            pf = (gain_sum / loss_sum) if loss_sum > 0 else 999.0
            avg_hold = df_t["bars"].mean() * 5.0
            eq = (1.0 + df_t["ret"]).cumprod()
            dd = ((eq - eq.cummax()) / eq.cummax()).min() * 100.0
        else:
            wr, tot_ret, pf, avg_hold, dd = 0, 0, 0, 0, 0
            
        symbol_stats.append({
            "Contract": sym,
            "Trades": len(trades),
            "WinRate": f"{wr:.1f}%",
            "NetReturn": f"{tot_ret:+.2f}%",
            "ProfitFactor": f"{pf:.2f}" if pf < 100 else "Inf",
            "MaxDD": f"{abs(dd):.2f}%",
            "AvgHold": f"{avg_hold:.1f}m"
        })
        
    df_res = pd.DataFrame(symbol_stats)
    print(df_res.to_string(index=False))
    
    # Portfolio Aggregate
    df_all = pd.DataFrame(all_trades)
    all_wins = df_all[df_all["ret"] > 0]
    all_losses = df_all[df_all["ret"] <= 0]
    overall_wr = len(all_wins) / len(df_all) * 100.0
    overall_pf = all_wins["ret"].sum() / abs(all_losses["ret"].sum())
    overall_hold = df_all["bars"].mean() * 5.0
    avg_symbol_ret = df_res["NetReturn"].str.rstrip("%").astype(float).mean()
    
    print("-" * 80)
    print(f"【组合全样本统计 (10 个 OKX 合约标的)】")
    print(f"  总交易笔数:       {len(df_all)} 笔")
    print(f"  整体综合胜率:     {overall_wr:.2f}% (盈利: {len(all_wins)} 笔 / 亏损: {len(all_losses)} 笔)")
    print(f"  整体盈亏比 (PF):  {overall_pf:.2f}")
    print(f"  品种平均净收益率: {avg_symbol_ret:+.2f}% (无杠杆纯收益，已扣除手续费和滑点)")
    print(f"  3倍杠杆对应净收益: {avg_symbol_ret * 3:+.2f}%")
    print(f"  平均持仓耗时:     {overall_hold:.1f} 分钟")
    print("=" * 80 + "\n")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OKX CRSI Ultra-Short-Term Scalper")
    parser.add_argument("--backtest", action="store_true", default=True, help="Run backtest on 10 OKX symbols")
    args = parser.parse_args()
    
    if args.backtest:
        run_portfolio_backtest()

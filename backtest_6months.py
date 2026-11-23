import numpy as np
import pandas as pd
from pathlib import Path
from dataclasses import dataclass
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

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

DATA_DIR = Path("data/okx_candles")

def run_6m_backtest():
    # Optimal parameters found by coordinate line search
    rsi_len = 3
    os_level = 5.0
    ob_level = 96.0
    exit_rsi = 55.0
    tp_pct = 0.008
    sl_atr_mult = 1.8
    max_hold_bars = 12
    trend_ema_len = 200
    trend_asym = True
    fee_rate = 0.0004 # 4 bps per side (0.08% round trip)
    
    all_trades = []
    symbol_reports = []
    
    for sym in SYMBOLS_10:
        csv_file = DATA_DIR / f"{sym}_5m_6months.csv"
        if not csv_file.exists():
            print(f"File not found: {csv_file}")
            continue
            
        df = pd.read_csv(csv_file)
        c = df["c"].to_numpy(dtype=np.float64)
        h = df["h"].to_numpy(dtype=np.float64)
        l = df["l"].to_numpy(dtype=np.float64)
        o = df["o"].to_numpy(dtype=np.float64)
        ts = df["ts"].to_numpy(dtype=np.int64)
        n = len(c)
        
        # Indicators
        diff = pd.Series(c).diff()
        up = diff.clip(lower=0)
        down = -1 * diff.clip(upper=0)
        ma_up = up.ewm(alpha=1.0 / rsi_len, adjust=False).mean()
        ma_down = down.ewm(alpha=1.0 / rsi_len, adjust=False).mean()
        rsi = (100.0 - (100.0 / (1.0 + ma_up / (ma_down + 1e-9)))).to_numpy()
        
        prev_c = np.roll(c, 1)
        prev_c[0] = c[0]
        tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
        atr = pd.Series(tr).rolling(14, min_periods=14).mean().to_numpy()
        
        ema_trend = pd.Series(c).ewm(span=trend_ema_len, adjust=False).mean().to_numpy()
        
        trades = []
        in_pos = 0
        entry_p = 0.0
        entry_i = 0
        stop_p = 0.0
        tp_p = 0.0
        
        start_i = max(trend_ema_len, 50)
        for i in range(start_i, n - 1):
            next_o = o[i+1]
            curr_c = c[i]
            curr_h = h[i]
            curr_l = l[i]
            curr_atr = atr[i]
            
            if in_pos == 1:
                exit_p = None
                exit_reason = ""
                if curr_h >= tp_p:
                    exit_p = tp_p
                    exit_reason = "TP_TARGET"
                elif rsi[i] >= exit_rsi:
                    exit_p = curr_c
                    exit_reason = "TP_RSI_REVERT"
                elif curr_l <= stop_p:
                    exit_p = stop_p
                    exit_reason = "SL_ATR"
                elif i - entry_i >= max_hold_bars:
                    exit_p = curr_c
                    exit_reason = "TIME_LIMIT"
                    
                if exit_p is not None:
                    net_ret = (exit_p - entry_p) / entry_p - 2.0 * fee_rate
                    trades.append({
                        "sym": sym, "entry_ts": ts[entry_i], "exit_ts": ts[i],
                        "entry_dt": pd.to_datetime(ts[entry_i], unit="ms", utc=True),
                        "exit_dt": pd.to_datetime(ts[i], unit="ms", utc=True),
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
                elif rsi[i] <= (100.0 - exit_rsi):
                    exit_p = curr_c
                    exit_reason = "TP_RSI_REVERT"
                elif curr_h >= stop_p:
                    exit_p = stop_p
                    exit_reason = "SL_ATR"
                elif i - entry_i >= max_hold_bars:
                    exit_p = curr_c
                    exit_reason = "TIME_LIMIT"
                    
                if exit_p is not None:
                    net_ret = (entry_p - exit_p) / entry_p - 2.0 * fee_rate
                    trades.append({
                        "sym": sym, "entry_ts": ts[entry_i], "exit_ts": ts[i],
                        "entry_dt": pd.to_datetime(ts[entry_i], unit="ms", utc=True),
                        "exit_dt": pd.to_datetime(ts[i], unit="ms", utc=True),
                        "side": "SHORT", "entry_p": entry_p, "exit_p": exit_p,
                        "ret": net_ret, "bars": i - entry_i, "reason": exit_reason
                    })
                    in_pos = 0
                    continue
                    
            if in_pos == 0:
                is_bear_regime = (curr_c < ema_trend[i])
                if trend_asym:
                    if is_bear_regime:
                        curr_os = os_level - 2.0
                        curr_ob = ob_level - 2.5
                    else:
                        curr_os = os_level + 2.5
                        curr_ob = ob_level + 2.0
                else:
                    curr_os = os_level
                    curr_ob = ob_level
                    
                long_trigger = (rsi[i] <= curr_os) and (curr_c > o[i])
                short_trigger = (rsi[i] >= curr_ob) and (curr_c < o[i])
                
                if long_trigger:
                    in_pos = 1
                    entry_p = next_o
                    entry_i = i + 1
                    stop_p = entry_p - sl_atr_mult * curr_atr
                    tp_p = entry_p * (1.0 + tp_pct)
                    continue
                elif short_trigger:
                    in_pos = -1
                    entry_p = next_o
                    entry_i = i + 1
                    stop_p = entry_p + sl_atr_mult * curr_atr
                    tp_p = entry_p * (1.0 - tp_pct)
                    continue

        all_trades.extend(trades)
        if trades:
            df_t = pd.DataFrame(trades)
            wins = df_t[df_t["ret"] > 0]
            losses = df_t[df_t["ret"] <= 0]
            wr = len(wins) / len(df_t) * 100.0
            cum_ret = ((1.0 + df_t["ret"]).prod() - 1.0) * 100.0
            gain_sum = wins["ret"].sum()
            loss_sum = abs(losses["ret"].sum())
            pf = (gain_sum / loss_sum) if loss_sum > 0 else 999.0
            avg_hold = df_t["bars"].mean() * 5.0
            eq = (1.0 + df_t["ret"]).cumprod()
            dd = ((eq - eq.cummax()) / eq.cummax()).min() * 100.0
        else:
            wr, cum_ret, pf, avg_hold, dd = 0, 0, 0, 0, 0
            
        symbol_reports.append({
            "Contract": sym,
            "Bars": n,
            "Trades": len(trades),
            "WinRate": f"{wr:.1f}%",
            "NetReturn": f"{cum_ret:+.2f}%",
            "PF": f"{pf:.2f}" if pf < 100 else "Inf",
            "MaxDD": f"{abs(dd):.2f}%",
            "AvgHold": f"{avg_hold:.1f}m"
        })
        
    df_sym = pd.DataFrame(symbol_reports)
    print("\n" + "=" * 80)
    print("  OKX 永续合约超短线策略 (CRSI-Adaptive) - 6 个月全样本回测结果")
    print("=" * 80)
    print(df_sym.to_string(index=False))
    
    # Portfolio Aggregate
    df_all = pd.DataFrame(all_trades).sort_values("entry_ts").reset_index(drop=True)
    all_wins = df_all[df_all["ret"] > 0]
    all_losses = df_all[df_all["ret"] <= 0]
    overall_wr = len(all_wins) / len(df_all) * 100.0
    overall_pf = all_wins["ret"].sum() / abs(all_losses["ret"].sum())
    overall_hold = df_all["bars"].mean() * 5.0
    avg_symbol_ret = df_sym["NetReturn"].str.rstrip("%").astype(float).mean()
    
    # Cumulative portfolio curve
    df_all["cum_ret"] = (1.0 + df_all["ret"]).cumprod() - 1.0
    eq_all = (1.0 + df_all["ret"]).cumprod()
    dd_all = ((eq_all - eq_all.cummax()) / eq_all.cummax()).min() * 100.0
    
    # Monthly aggregation
    df_all["month"] = df_all["entry_dt"].dt.to_period("M")
    monthly_stats = []
    for m, group in df_all.groupby("month"):
        m_wins = group[group["ret"] > 0]
        m_wr = len(m_wins) / len(group) * 100.0
        m_ret = ((1.0 + group["ret"]).prod() - 1.0) * 100.0
        monthly_stats.append({
            "Month": str(m),
            "Trades": len(group),
            "WinRate": f"{m_wr:.1f}%",
            "NetReturn": f"{m_ret:+.2f}%"
        })
    df_monthly = pd.DataFrame(monthly_stats)
    
    print("-" * 80)
    print(f"【组合 6 个月全样本统计汇总】")
    print(f"  总交易笔数:       {len(df_all)} 笔")
    print(f"  整体综合胜率:     {overall_wr:.2f}% (盈利: {len(all_wins)} 笔 / 亏损: {len(all_losses)} 笔)")
    print(f"  整体盈亏比 (PF):  {overall_pf:.2f}")
    print(f"  品种平均净收益率: {avg_symbol_ret:+.2f}% (无杠杆纯收益，已扣除手续费和滑点)")
    print(f"  3倍杠杆对应净收益: {avg_symbol_ret * 3:+.2f}%")
    print(f"  组合全周期最大回撤: {abs(dd_all):.2f}%")
    print(f"  平均持仓耗时:     {overall_hold:.1f} 分钟")
    print("\n【月度收益分布 (Monthly Breakdown)】")
    print(df_monthly.to_string(index=False))
    print("=" * 80 + "\n")
    
    # Save CSV
    df_all.to_csv("data/okx_scalper_6m_trades.csv", index=False)
    
    # Plotting 6-month chart
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    fig.suptitle("OKX Contract Ultra-Short-Term Scalper - 6 Months Backtest", fontsize=16, fontweight="bold")
    
    # 1. Cumulative Equity Curve
    ax1 = axes[0, 0]
    ax1.plot(df_all["entry_dt"], df_all["cum_ret"] * 100, color="#008080", linewidth=2)
    ax1.axhline(0, color="gray", linestyle="--", alpha=0.6)
    ax1.set_title("Portfolio Cumulative Return (%) over 6 Months", fontsize=12, fontweight="bold")
    ax1.set_ylabel("Cumulative Return (%)")
    ax1.grid(True, linestyle=":", alpha=0.6)
    
    # 2. Per-Contract Returns
    ax2 = axes[0, 1]
    short_names = [s.replace("-USDT-SWAP", "") for s in df_sym["Contract"]]
    rets = df_sym["NetReturn"].str.rstrip("%").astype(float)
    colors = ["#2ca02c" if r > 0 else "#d62728" for r in rets]
    bars = ax2.bar(short_names, rets, color=colors, edgecolor="black", alpha=0.85)
    ax2.axhline(0, color="black", linewidth=1)
    ax2.set_title("6-Month Net Return by Contract (%)", fontsize=12, fontweight="bold")
    ax2.set_ylabel("Net Return (%)")
    ax2.grid(True, axis="y", linestyle=":", alpha=0.6)
    for bar in bars:
        yval = bar.get_height()
        ax2.text(bar.get_x() + bar.get_width()/2.0, yval + (0.05 if yval >= 0 else -0.15), f"{yval:+.1f}%", ha="center", va="bottom", fontsize=8, fontweight="bold")
        
    # 3. Monthly Return Bar Chart
    ax3 = axes[1, 0]
    m_names = df_monthly["Month"]
    m_rets = df_monthly["NetReturn"].str.rstrip("%").astype(float)
    m_colors = ["#2ca02c" if r > 0 else "#d62728" for r in m_rets]
    bars_m = ax3.bar(m_names, m_rets, color=m_colors, edgecolor="black", alpha=0.85)
    ax3.axhline(0, color="black", linewidth=1)
    ax3.set_title("Monthly Net Return (%)", fontsize=12, fontweight="bold")
    ax3.set_ylabel("Return (%)")
    ax3.grid(True, axis="y", linestyle=":", alpha=0.6)
    for bar in bars_m:
        yval = bar.get_height()
        ax3.text(bar.get_x() + bar.get_width()/2.0, yval + (0.1 if yval >= 0 else -0.2), f"{yval:+.2f}%", ha="center", va="bottom", fontsize=8, fontweight="bold")
        
    # 4. Holding Duration
    ax4 = axes[1, 1]
    holding_mins = df_all["bars"] * 5
    ax4.hist(holding_mins, bins=12, color="#ff7f0e", edgecolor="black", alpha=0.85)
    ax4.axvline(holding_mins.mean(), color="red", linestyle="--", linewidth=2, label=f"Mean: {holding_mins.mean():.1f}m")
    ax4.set_title("Holding Time Distribution (Minutes)", fontsize=12, fontweight="bold")
    ax4.set_xlabel("Holding Time (Minutes)")
    ax4.set_ylabel("Trades")
    ax4.grid(True, linestyle=":", alpha=0.6)
    ax4.legend()
    
    plt.tight_layout()
    chart_path = Path("data/okx_scalper_6m_chart.png")
    plt.savefig(chart_path, dpi=300)
    plt.close()
    print(f"Saved 6-month chart to {chart_path}")
    return df_sym, df_monthly, df_all

if __name__ == "__main__":
    run_6m_backtest()

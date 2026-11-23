import numpy as np
import pandas as pd
from pathlib import Path
import json
import time

from scalping_engine import load_all_symbols, SYMBOLS_10, evaluate_trades, BacktestResult

class OKXScalperSimulator:
    def __init__(self, data_dict=None):
        if data_dict is None:
            self.data = load_all_symbols()
        else:
            self.data = data_dict
            
        # Cache raw numpy arrays for performance
        self.cached_arrays = {}
        for sym, df in self.data.items():
            self.cached_arrays[sym] = {
                "c": df["c"].to_numpy(dtype=np.float64),
                "h": df["h"].to_numpy(dtype=np.float64),
                "l": df["l"].to_numpy(dtype=np.float64),
                "o": df["o"].to_numpy(dtype=np.float64),
                "v": df["vol"].to_numpy(dtype=np.float64),
                "n": len(df)
            }

    def run_single_symbol(
        self,
        sym: str,
        rsi_len: int = 3,
        os_level: float = 6.0,
        ob_level: float = 94.0,
        exit_rsi: float = 50.0,
        tp_pct: float = 0.010,
        sl_atr_mult: float = 2.0,
        max_hold_bars: int = 12,
        trend_ema_len: int = 144,
        trend_asym: bool = True,
        fee_rate: float = 0.0004 # 0.04% per side (Maker entry / Taker SL / Maker TP avg)
    ) -> list:
        arr = self.cached_arrays[sym]
        c, h, l, o, v, n = arr["c"], arr["h"], arr["l"], arr["o"], arr["v"], arr["n"]
        
        # 1. RSI
        diff = pd.Series(c).diff()
        up = diff.clip(lower=0)
        down = -1 * diff.clip(upper=0)
        ma_up = up.ewm(alpha=1.0 / rsi_len, adjust=False).mean()
        ma_down = down.ewm(alpha=1.0 / rsi_len, adjust=False).mean()
        rsi = (100.0 - (100.0 / (1.0 + ma_up / (ma_down + 1e-9)))).to_numpy()
        
        # 2. ATR(14)
        prev_c = np.roll(c, 1)
        prev_c[0] = c[0]
        tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
        atr = pd.Series(tr).rolling(14, min_periods=14).mean().to_numpy()
        
        # 3. Macro Trend EMA
        ema_trend = pd.Series(c).ewm(span=trend_ema_len, adjust=False).mean().to_numpy()
        
        trades = []
        in_pos = 0 # 1: Long, -1: Short
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
            
            # Position Exit Evaluation
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
                        "sym": sym, "entry_i": entry_i, "exit_i": i,
                        "side": "SHORT", "entry_p": entry_p, "exit_p": exit_p,
                        "ret": net_ret, "bars": i - entry_i, "reason": exit_reason
                    })
                    in_pos = 0
                    continue
                    
            # Signal Entry Evaluation
            if in_pos == 0:
                is_bear_regime = (curr_c < ema_trend[i])
                
                # Asymmetric thresholds
                if trend_asym:
                    if is_bear_regime:
                        # In bear market, shorts trigger easier, longs require deeper oversold
                        curr_os = os_level - 2.0
                        curr_ob = ob_level - 2.5
                    else:
                        # In bull market, longs trigger easier, shorts require deeper overbought
                        curr_os = os_level + 2.5
                        curr_ob = ob_level + 2.0
                else:
                    curr_os = os_level
                    curr_ob = ob_level
                    
                # Long Condition: RSI extreme oversold + bullish reversal tick
                long_trigger = (rsi[i] <= curr_os) and (curr_c > o[i])
                # Short Condition: RSI extreme overbought + bearish reversal tick
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

        return trades

    def evaluate_portfolio(self, params: dict):
        all_trades = []
        sym_res = {}
        
        for sym in SYMBOLS_10:
            trades = self.run_single_symbol(sym, **params)
            all_trades.extend(trades)
            sym_res[sym] = evaluate_trades(trades)
            
        overall = evaluate_trades(all_trades)
        
        # Portfolio equal-weight return: average total_ret across all 10 symbols
        avg_ret = np.mean([r.total_ret for r in sym_res.values()])
        
        # Objective score calculation
        # Must be profitable, have decent trades, high PF, low drawdown
        trades_count = overall.trades_count
        pf = overall.profit_factor
        wr = overall.win_rate
        max_dd = overall.max_drawdown
        
        if trades_count < 15 or avg_ret <= 0 or pf < 1.05:
            score = -100.0 + avg_ret
        else:
            # Score favors positive return, high profit factor, balanced trade count, and low drawdown
            trade_factor = np.log10(trades_count + 1)
            score = avg_ret * min(pf, 4.0) * trade_factor / (1.0 + max_dd / 10.0)
            
        return {
            "score": score,
            "avg_ret": avg_ret,
            "overall_trades": trades_count,
            "overall_wr": wr,
            "overall_pf": pf,
            "overall_max_dd": max_dd,
            "symbols": sym_res,
            "all_trades": all_trades
        }

def run_coordinate_line_search(sim: OKXScalperSimulator, max_iterations: int = 2):
    """
    Executes Coordinate Line Search along each parameter dimension sequentially.
    """
    # 1. Initial parameter seed
    current_params = {
        "rsi_len": 3,
        "os_level": 6.0,
        "ob_level": 94.0,
        "exit_rsi": 50.0,
        "tp_pct": 0.010,
        "sl_atr_mult": 2.0,
        "max_hold_bars": 12,
        "trend_ema_len": 144,
        "trend_asym": True
    }
    
    # 2. Define search intervals for each coordinate dimension
    search_dimensions = [
        ("rsi_len", [2, 3, 4]),
        ("os_level", [4.0, 5.0, 6.0, 7.0, 8.0, 9.0]),
        ("ob_level", [92.0, 93.0, 94.0, 95.0, 96.0]),
        ("exit_rsi", [45.0, 48.0, 50.0, 52.0, 55.0]),
        ("tp_pct", [0.006, 0.008, 0.010, 0.012, 0.015]),
        ("sl_atr_mult", [1.5, 1.8, 2.0, 2.2, 2.5]),
        ("max_hold_bars", [8, 10, 12, 16, 20]),
        ("trend_ema_len", [72, 100, 144, 200, 288]),
        ("trend_asym", [True, False])
    ]
    
    print("=== Starting Coordinate Line Search Optimization ===")
    initial_eval = sim.evaluate_portfolio(current_params)
    print(f"Initial Baseline: Score={initial_eval['score']:.4f}, AvgRet={initial_eval['avg_ret']:+.2f}%, PF={initial_eval['overall_pf']:.2f}, WR={initial_eval['overall_wr']:.1f}%, Trades={initial_eval['overall_trades']}")
    print("-" * 75)
    
    best_params = current_params.copy()
    best_score = initial_eval["score"]
    
    history = []
    
    for iteration in range(1, max_iterations + 1):
        print(f"\n>>> ITERATION {iteration} / {max_iterations} <<<")
        iteration_improved = False
        
        for param_name, search_grid in search_dimensions:
            dim_start_val = best_params[param_name]
            best_dim_val = dim_start_val
            best_dim_score = best_score
            best_dim_eval = None
            
            print(f"\n[Line Search on: '{param_name}'] Current={dim_start_val}, Grid={search_grid}")
            
            for candidate_val in search_grid:
                test_params = best_params.copy()
                test_params[param_name] = candidate_val
                eval_res = sim.evaluate_portfolio(test_params)
                
                score = eval_res["score"]
                status = " "
                if score > best_dim_score:
                    status = "*"
                    best_dim_score = score
                    best_dim_val = candidate_val
                    best_dim_eval = eval_res
                    
                print(f"  {status} {param_name}={candidate_val:<5} -> Score: {score:7.4f} | AvgRet: {eval_res['avg_ret']:+6.2f}% | PF: {eval_res['overall_pf']:5.2f} | WR: {eval_res['overall_wr']:5.1f}% | Trades: {eval_res['overall_trades']}")
                
            if best_dim_val != dim_start_val and best_dim_score > best_score:
                print(f"==> Dimension '{param_name}' updated: {dim_start_val} -> {best_dim_val} (Score: {best_score:.4f} -> {best_dim_score:.4f})")
                best_params[param_name] = best_dim_val
                best_score = best_dim_score
                iteration_improved = True
                history.append({
                    "iteration": iteration,
                    "updated_param": param_name,
                    "old_val": dim_start_val,
                    "new_val": best_dim_val,
                    "new_score": best_score,
                    "eval": {
                        "avg_ret": best_dim_eval["avg_ret"],
                        "pf": best_dim_eval["overall_pf"],
                        "wr": best_dim_eval["overall_wr"],
                        "trades": best_dim_eval["overall_trades"]
                    }
                })
            else:
                print(f"==> Dimension '{param_name}' retained optimal value: {dim_start_val}")
                
        if not iteration_improved:
            print("\nConvergence reached: no parameter changed in this cycle.")
            break
            
    final_eval = sim.evaluate_portfolio(best_params)
    print("\n" + "=" * 75)
    print("=== Line Search Complete: Optimal Parameter Set Found ===")
    print(json.dumps(best_params, indent=2))
    print(f"\nPortfolio Performance Summary Across 10 Symbols:")
    print(f"  Avg Return per Symbol: {final_eval['avg_ret']:+.2f}%")
    print(f"  Overall Win Rate:      {final_eval['overall_wr']:.2f}%")
    print(f"  Overall Profit Factor: {final_eval['overall_pf']:.2f}")
    print(f"  Overall Total Trades:  {final_eval['overall_trades']}")
    print(f"  Overall Max Drawdown:  {final_eval['overall_max_dd']:.2f}%")
    print("=" * 75)
    
    return best_params, final_eval, history

if __name__ == "__main__":
    sim = OKXScalperSimulator()
    best_p, final_res, hist = run_coordinate_line_search(sim, max_iterations=2)

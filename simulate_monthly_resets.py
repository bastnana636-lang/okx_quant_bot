import pandas as pd
import numpy as np
from pathlib import Path
from typing import Dict, List, Optional
from okx_human_trader import load_and_align_candles, Position, SYMBOLS, next_funding_time

def run_monthly_reset_sim(
    initial_capital_per_month: float = 10000.0,
    execution: str = 'maker',
    funding_rate: float = 0.0001,
    maintenance_margin_rate: float = 0.005,
    regime_filter: bool = True,
    min_trend_spread_atr: float = 1.25,
    min_trend_t: float = 0.2,
    cooldown_bars: int = 12,
    daily_loss_limit: float = 0.02
):
    print("Loading 2-year candle dataset...")
    common_ts, data = load_and_align_candles('2years')
    ts_series = pd.Series(common_ts)
    n_bars = len(common_ts)
    
    # 确定所有月份区间
    df_ts = pd.DataFrame({'dt': common_ts})
    df_ts['month'] = df_ts['dt'].dt.tz_localize(None).dt.to_period('M')
    unique_months = df_ts['month'].unique()
    print(f"Total months in dataset: {len(unique_months)} ({unique_months[0]} to {unique_months[-1]})")

    fee_rate = 0.0002 if execution == 'maker' else 0.0005
    slippage = 0.0000 if execution == 'maker' else 0.0002
    margin_ratio = 0.20
    max_margin_cap = 2500.0
    btc_eth_lev = 15.0
    alt_lev = 10.0
    swing_tp = 0.035
    swing_sl_mult = 2.0
    swing_trail_trig = 0.012
    swing_trail_pb = 0.005
    max_hold_bars = 288
    funding_interval_hours = 8
    trend_t_window = 288

    # ==========================================
    # 模式一：每月月初本金固定重置为 10,000 USDT (每月独立运行)
    # ==========================================
    monthly_results_mode1 = []

    for m in unique_months:
        m_indices = df_ts[df_ts['month'] == m].index
        start_bar = m_indices[0]
        end_bar = m_indices[-1]
        
        # 预热检查：若索引小于288则从288开始
        if start_bar < 288:
            start_bar = 288
        if start_bar >= end_bar:
            continue
            
        cash = initial_capital_per_month
        active_positions: Dict[str, Position] = {}
        completed_trades = []
        equity_curve = []
        trade_id_counter = 1
        last_exit_bar = {}
        day_key = None
        day_start_equity = initial_capital_per_month
        
        for i in range(start_bar, end_bar):
            curr_dt = common_ts[i]
            next_dt = common_ts[i+1]
            bar_end_dt = curr_dt + pd.Timedelta(minutes=5)
            
            # 1. 检查持仓
            for sym, pos in list(active_positions.items()):
                sd = data[sym]
                c = sd['c'][i]
                h = sd['h'][i]
                l = sd['l'][i]
                hold_bars = i - pos.entry_bar + 1
                hold_mins = hold_bars * 5

                while pos.next_funding_dt is not None and pos.next_funding_dt <= curr_dt:
                    funding_cost = pos.nominal_size * funding_rate
                    pos.funding_paid += funding_cost if pos.side == 'LONG' else -funding_cost
                    pos.next_funding_dt = pos.next_funding_dt + pd.Timedelta(hours=funding_interval_hours)
                
                exit_price = None
                exit_reason = ""
                liquidated = False
                
                if pos.side == 'LONG':
                    liq_hit = (l <= pos.liq_price)
                    sl_hit = (l <= pos.stop_loss)
                    tp_hit = (h >= pos.take_profit)
                    
                    prior_peak = pos.peak
                    gain_prior = (prior_peak - pos.entry_price) / pos.entry_price
                    trail_hit = False
                    if gain_prior >= swing_trail_trig:
                        trail_px = prior_peak * (1.0 - swing_trail_pb)
                        if l <= trail_px:
                            trail_hit = True
                            trail_exit = max(trail_px, pos.stop_loss)
                    if not trail_hit and h > pos.peak:
                        gain_new = (h - pos.entry_price) / pos.entry_price
                        if gain_new >= swing_trail_trig:
                            trail_px = h * (1.0 - swing_trail_pb)
                            if c <= trail_px:
                                trail_hit = True
                                trail_exit = max(trail_px, pos.stop_loss)
                                
                    if liq_hit:
                        liquidated = True
                        exit_price = pos.liq_price
                        exit_reason = f"LIQUIDATION"
                    elif tp_hit and not sl_hit:
                        exit_price = pos.take_profit
                        exit_reason = f"TP_TARGET (+{pos.tp_pct*100:.1f}%)"
                    elif trail_hit:
                        exit_price = trail_exit * (1.0 - slippage)
                        exit_reason = "TRAILING_STOP"
                    elif sl_hit:
                        exit_price = pos.stop_loss * (1.0 - slippage)
                        exit_reason = f"SL_ATR ({swing_sl_mult:.1f}*ATR)"
                    elif hold_bars >= max_hold_bars:
                        exit_price = c * (1.0 - slippage)
                        exit_reason = f"TIME_LIMIT_1D ({hold_mins}m)"
                        
                    if exit_price is not None:
                        gross_pnl = (exit_price - pos.entry_price) * pos.quantity
                        entry_fee = pos.nominal_size * fee_rate
                        exit_nominal = exit_price * pos.quantity
                        exit_fee = exit_nominal * fee_rate
                        total_fees = round(entry_fee + exit_fee, 2)
                        net_pnl = -round(pos.margin, 2) if liquidated else round(gross_pnl - total_fees - pos.funding_paid, 2)
                        
                        cash += (pos.margin + net_pnl)
                        del active_positions[sym]
                        last_exit_bar[sym] = i
                        completed_trades.append({
                            'trade_id': pos.trade_id,
                            'symbol': sym,
                            'side': pos.side,
                            'net_pnl': net_pnl,
                            'fees': total_fees,
                            'funding': pos.funding_paid,
                            'exit_reason': exit_reason
                        })
                    else:
                        if h > pos.peak: pos.peak = h
                        if l < pos.trough: pos.trough = l
                        
                elif pos.side == 'SHORT':
                    liq_hit = (h >= pos.liq_price)
                    sl_hit = (h >= pos.stop_loss)
                    tp_hit = (l <= pos.take_profit)
                    
                    prior_trough = pos.trough
                    gain_prior = (pos.entry_price - prior_trough) / pos.entry_price
                    trail_hit = False
                    if gain_prior >= swing_trail_trig:
                        trail_px = prior_trough * (1.0 + swing_trail_pb)
                        if h >= trail_px:
                            trail_hit = True
                            trail_exit = min(trail_px, pos.stop_loss)
                    if not trail_hit and l < pos.trough:
                        gain_new = (pos.entry_price - l) / pos.entry_price
                        if gain_new >= swing_trail_trig:
                            trail_px = l * (1.0 + swing_trail_pb)
                            if c >= trail_px:
                                trail_hit = True
                                trail_exit = min(trail_px, pos.stop_loss)
                                
                    if liq_hit:
                        liquidated = True
                        exit_price = pos.liq_price
                        exit_reason = f"LIQUIDATION"
                    elif tp_hit and not sl_hit:
                        exit_price = pos.take_profit
                        exit_reason = f"TP_TARGET (+{pos.tp_pct*100:.1f}%)"
                    elif trail_hit:
                        exit_price = trail_exit * (1.0 + slippage)
                        exit_reason = "TRAILING_STOP"
                    elif sl_hit:
                        exit_price = pos.stop_loss * (1.0 + slippage)
                        exit_reason = f"SL_ATR ({swing_sl_mult:.1f}*ATR)"
                    elif hold_bars >= max_hold_bars:
                        exit_price = c * (1.0 + slippage)
                        exit_reason = f"TIME_LIMIT_1D ({hold_mins}m)"
                        
                    if exit_price is not None:
                        gross_pnl = (pos.entry_price - exit_price) * pos.quantity
                        entry_fee = pos.nominal_size * fee_rate
                        exit_nominal = exit_price * pos.quantity
                        exit_fee = exit_nominal * fee_rate
                        total_fees = round(entry_fee + exit_fee, 2)
                        net_pnl = -round(pos.margin, 2) if liquidated else round(gross_pnl - total_fees - pos.funding_paid, 2)
                        
                        cash += (pos.margin + net_pnl)
                        del active_positions[sym]
                        last_exit_bar[sym] = i
                        completed_trades.append({
                            'trade_id': pos.trade_id,
                            'symbol': sym,
                            'side': pos.side,
                            'net_pnl': net_pnl,
                            'fees': total_fees,
                            'funding': pos.funding_paid,
                            'exit_reason': exit_reason
                        })
                    else:
                        if h > pos.peak: pos.peak = h
                        if l < pos.trough: pos.trough = l

            # 2. 盯市权益与回撤
            unrealized_total = sum(
                ((data[s]['c'][i] - p.entry_price) * p.quantity if p.side == 'LONG'
                else (p.entry_price - data[s]['c'][i]) * p.quantity) - p.funding_paid
                for s, p in active_positions.items()
            )
            current_mtm_equity = round(cash + sum(p.margin for p in active_positions.values()) + unrealized_total, 2)
            equity_curve.append(current_mtm_equity)

            bar_day = pd.Timestamp(curr_dt).date()
            if bar_day != day_key:
                day_key = bar_day
                day_start_equity = current_mtm_equity
            
            # 3. 寻找开仓
            if len(active_positions) < 3 and cash > 500:
                for sym in SYMBOLS:
                    if sym in active_positions:
                        continue
                    if len(active_positions) >= 3:
                        break
                        
                    sd = data[sym]
                    c_i = sd['c'][i]
                    o_i = sd['o'][i]
                    h_i = sd['h'][i]
                    l_i = sd['l'][i]
                    rsi9_i = sd['rsi9'][i]
                    macd_h = sd['macd_hist'][i]
                    atr_i = sd['atr14'][i]
                    ema12_i = sd['ema12'][i]
                    ema36_i = sd['ema36'][i]
                    ema144_i = sd['ema144'][i]
                    ema288_i = sd['ema288'][i]
                    
                    side = None
                    tp_val = swing_tp
                    sl_mult = swing_sl_mult
                    
                    is_macro_bear = (c_i < ema144_i) and (ema144_i < ema288_i)
                    tested_res = (h_i >= ema12_i * 0.998) or (h_i >= ema36_i * 0.998)
                    closed_weak = (c_i < o_i) and (c_i < ema12_i)
                    momentum_bear = (macd_h < 0) and (rsi9_i >= 50.0 and rsi9_i <= 68.0)

                    is_macro_bull = (c_i > ema144_i) and (ema144_i > ema288_i)
                    tested_sup = (l_i <= ema12_i * 1.002) or (l_i <= ema36_i * 1.002)
                    closed_strong = (c_i > o_i) and (c_i > ema12_i)
                    momentum_bull = (macd_h > 0) and (rsi9_i >= 32.0 and rsi9_i <= 50.0)

                    if is_macro_bear and tested_res and closed_weak and momentum_bear:
                        side = 'SHORT'
                    elif is_macro_bull and tested_sup and closed_strong and momentum_bull:
                        side = 'LONG'
                            
                    if side is not None and regime_filter:
                        trend_spread_atr = abs(ema144_i - ema288_i) / max(atr_i, 1e-9)
                        tw = min(trend_t_window, i)
                        drift = (c_i - sd['c'][i - tw]) / max(atr_i * np.sqrt(tw), 1e-9)
                        trend_t = drift if side == 'LONG' else -drift
                        if trend_spread_atr < min_trend_spread_atr or trend_t < min_trend_t:
                            side = None

                    if side is None:
                        continue

                    if cooldown_bars > 0 and (i - last_exit_bar.get(sym, -(10**9))) < cooldown_bars:
                        continue
                    if daily_loss_limit > 0 and current_mtm_equity <= day_start_equity * (1.0 - daily_loss_limit):
                        continue
                        
                    lev = btc_eth_lev if ('BTC' in sym or 'ETH' in sym) else alt_lev
                    margin = min(current_mtm_equity * margin_ratio, max_margin_cap)
                    if margin > cash:
                        margin = cash * 0.90
                    if margin < 100:
                        continue
                        
                    cash -= margin
                    nominal = margin * lev
                    next_open = sd['o'][i+1]
                    
                    if side == 'LONG':
                        fill_price = next_open * (1.0 + slippage)
                        qty = nominal / fill_price
                        sl = fill_price - sl_mult * atr_i
                        tp = fill_price * (1.0 + tp_val)
                        liq = fill_price * (1.0 - 1.0 / lev + maintenance_margin_rate)
                        active_positions[sym] = Position(
                            trade_id=trade_id_counter,
                            symbol=sym,
                            side='LONG',
                            mode='SWING',
                            entry_bar=i+1,
                            entry_time=next_dt,
                            entry_price=fill_price,
                            leverage=lev,
                            margin=margin,
                            nominal_size=nominal,
                            quantity=qty,
                            stop_loss=sl,
                            take_profit=tp,
                            tp_pct=tp_val,
                            entry_reason='',
                            peak=fill_price,
                            trough=fill_price,
                            liq_price=liq,
                            next_funding_dt=next_funding_time(next_dt, funding_interval_hours)
                        )
                        trade_id_counter += 1
                    elif side == 'SHORT':
                        fill_price = next_open * (1.0 - slippage)
                        qty = nominal / fill_price
                        sl = fill_price + sl_mult * atr_i
                        tp = fill_price * (1.0 - tp_val)
                        liq = fill_price * (1.0 + 1.0 / lev - maintenance_margin_rate)
                        active_positions[sym] = Position(
                            trade_id=trade_id_counter,
                            symbol=sym,
                            side='SHORT',
                            mode='SWING',
                            entry_bar=i+1,
                            entry_time=next_dt,
                            entry_price=fill_price,
                            leverage=lev,
                            margin=margin,
                            nominal_size=nominal,
                            quantity=qty,
                            stop_loss=sl,
                            take_profit=tp,
                            tp_pct=tp_val,
                            entry_reason='',
                            peak=fill_price,
                            trough=fill_price,
                            liq_price=liq,
                            next_funding_dt=next_funding_time(next_dt, funding_interval_hours)
                        )
                        trade_id_counter += 1

        # 月末平仓剩余持仓
        final_bar_idx = end_bar
        for sym, pos in list(active_positions.items()):
            sd = data[sym]
            c = sd['c'][final_bar_idx]
            gross_pnl = (c - pos.entry_price) * pos.quantity if pos.side == 'LONG' else (pos.entry_price - c) * pos.quantity
            entry_fee = pos.nominal_size * fee_rate
            exit_nominal = c * pos.quantity
            exit_fee = exit_nominal * fee_rate
            total_fees = round(entry_fee + exit_fee, 2)
            net_pnl = round(gross_pnl - total_fees - pos.funding_paid, 2)
            cash += (pos.margin + net_pnl)
            completed_trades.append({
                'trade_id': pos.trade_id,
                'symbol': sym,
                'side': pos.side,
                'net_pnl': net_pnl,
                'fees': total_fees,
                'funding': pos.funding_paid,
                'exit_reason': 'MONTH_END'
            })
            del active_positions[sym]

        df_m_trades = pd.DataFrame(completed_trades)
        final_equity = round(cash, 2)
        net_profit = round(final_equity - initial_capital_per_month, 2)
        roi_pct = round((net_profit / initial_capital_per_month) * 100.0, 2)
        
        # 月内最大回撤
        s_eq = pd.Series(equity_curve)
        if len(s_eq) > 0:
            m_peak = s_eq.cummax()
            m_dd = (s_eq - m_peak) / m_peak
            m_max_dd_pct = round(abs(m_dd.min()) * 100.0, 2)
            m_max_dd_usd = round((m_peak - s_eq).max(), 2)
        else:
            m_max_dd_pct = 0.0
            m_max_dd_usd = 0.0

        n_trades = len(df_m_trades)
        wins = len(df_m_trades[df_m_trades['net_pnl'] > 0]) if n_trades > 0 else 0
        losses = len(df_m_trades[df_m_trades['net_pnl'] <= 0]) if n_trades > 0 else 0
        wr = round((wins / n_trades) * 100.0, 1) if n_trades > 0 else 0.0
        fees = round(df_m_trades['fees'].sum(), 2) if n_trades > 0 else 0.0
        funding = round(df_m_trades['funding'].sum(), 2) if n_trades > 0 else 0.0

        monthly_results_mode1.append({
            'month': str(m),
            'start_date': common_ts[start_bar].strftime('%Y-%m-%d'),
            'end_date': common_ts[end_bar].strftime('%Y-%m-%d'),
            'initial_capital': initial_capital_per_month,
            'final_equity': final_equity,
            'net_pnl': net_profit,
            'roi_pct': roi_pct,
            'trades': n_trades,
            'wins': wins,
            'losses': losses,
            'win_rate': wr,
            'fees': fees,
            'funding': funding,
            'max_dd_pct': m_max_dd_pct,
            'max_dd_usd': m_max_dd_usd
        })

    df_mode1 = pd.DataFrame(monthly_results_mode1)
    df_mode1.to_csv('data/monthly_pnl_mode1_fixed_10k.csv', index=False, encoding='utf-8-sig')
    print("Mode 1 (Fixed 10,000 USDT at beginning of EACH month) completed:")
    print(df_mode1[['month', 'net_pnl', 'roi_pct', 'win_rate', 'trades', 'final_equity']].to_string())

    # ==========================================
    # 模式二：每年年初重置为 10,000 USDT (Year 1: 2024-11~2025-11, Year 2: 2025-11~2026-11)
    # ==========================================
    # 我们也可以运行 Year 1 和 Year 2，按月拆解其盈亏
    from okx_human_trader import run_human_trading_simulation
    print("\nRunning Year 1 (prev_year) continuous simulation...")
    _, df_y1_trades, df_y1_eq = run_human_trading_simulation(period='prev_year', execution='maker', initial_capital=10000.0)
    df_y1_trades['dt'] = pd.to_datetime(df_y1_trades['entry_time'].str.replace(' UTC', ''))
    df_y1_trades['month'] = df_y1_trades['dt'].dt.to_period('M')

    print("Running Year 2 (1year) continuous simulation...")
    _, df_y2_trades, df_y2_eq = run_human_trading_simulation(period='1year', execution='maker', initial_capital=10000.0)
    df_y2_trades['dt'] = pd.to_datetime(df_y2_trades['entry_time'].str.replace(' UTC', ''))
    df_y2_trades['month'] = df_y2_trades['dt'].dt.to_period('M')

    mode2_records = []
    # Year 1 months
    for m, g in df_y1_trades.groupby('month'):
        w = g[g['net_pnl'] > 0]
        mode2_records.append({
            'year': 'Year 1 (2024-2025)',
            'month': str(m),
            'trades': len(g),
            'wins': len(w),
            'win_rate': round(len(w)/len(g)*100, 1),
            'net_pnl': round(g['net_pnl'].sum(), 2),
            'fees': round(g['total_fees'].sum(), 2),
            'account_equity_end': round(g['account_equity'].iloc[-1], 2)
        })
    # Year 2 months
    for m, g in df_y2_trades.groupby('month'):
        w = g[g['net_pnl'] > 0]
        mode2_records.append({
            'year': 'Year 2 (2025-2026)',
            'month': str(m),
            'trades': len(g),
            'wins': len(w),
            'win_rate': round(len(w)/len(g)*100, 1),
            'net_pnl': round(g['net_pnl'].sum(), 2),
            'fees': round(g['total_fees'].sum(), 2),
            'account_equity_end': round(g['account_equity'].iloc[-1], 2)
        })
    df_mode2 = pd.DataFrame(mode2_records)
    df_mode2.to_csv('data/monthly_pnl_mode2_yearly_10k.csv', index=False, encoding='utf-8-sig')
    print("\nMode 2 (Reset to 10,000 USDT at beginning of EACH YEAR) completed:")
    print(df_mode2[['year', 'month', 'net_pnl', 'win_rate', 'trades', 'account_equity_end']].to_string())

if __name__ == '__main__':
    run_monthly_reset_sim()

import pandas as pd
import numpy as np
from pathlib import Path
from okx_human_trader import load_and_align_candles, Position, SYMBOLS, next_funding_time

common_ts, data = load_and_align_candles('1year')
ts_series = pd.Series(common_ts)

def run_range_sim(start_str, end_str, initial_capital=10000.0,
                  execution='maker', funding_rate=0.0001,
                  maintenance_margin_rate=0.005, regime_filter=True,
                  min_trend_spread_atr=1.25, min_trend_t=0.2,
                  cooldown_bars=12, daily_loss_limit=0.02):
    
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
    
    start_dt = pd.to_datetime(start_str, utc=True)
    end_dt = pd.to_datetime(end_str, utc=True)
    
    start_bar = (ts_series >= start_dt).idxmax()
    end_bar = (ts_series <= end_dt)[::-1].idxmax()
    
    cash = initial_capital
    active_positions = {}
    completed_trades = []
    equity_curve = []
    trade_id_counter = 1
    last_exit_bar = {}
    day_key = None
    day_start_equity = initial_capital
    
    for i in range(start_bar, end_bar):
        curr_dt = common_ts[i]
        next_dt = common_ts[i+1]
        bar_end_dt = curr_dt + pd.Timedelta(minutes=5)
        
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
            exit_reason = ''
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
                    exit_reason = f'LIQUIDATION (强平 {pos.liq_price:.4f})'
                elif tp_hit and not sl_hit:
                    exit_price = pos.take_profit
                    exit_reason = f'TP_TARGET (+{pos.tp_pct*100:.1f}%)'
                elif trail_hit:
                    exit_price = trail_exit * (1.0 - slippage)
                    exit_reason = 'TRAILING_STOP'
                elif sl_hit:
                    exit_price = pos.stop_loss * (1.0 - slippage)
                    exit_reason = f'SL_ATR ({swing_sl_mult:.1f}*ATR)'
                elif hold_bars >= max_hold_bars:
                    exit_price = c * (1.0 - slippage)
                    exit_reason = f'TIME_LIMIT_1D ({hold_mins}m)'
                    
                if exit_price is not None:
                    gross_pnl = (exit_price - pos.entry_price) * pos.quantity
                    entry_fee = pos.nominal_size * fee_rate
                    exit_nominal = exit_price * pos.quantity
                    exit_fee = exit_nominal * fee_rate
                    total_fees = round(entry_fee + exit_fee, 2)
                    if liquidated:
                        net_pnl = -round(pos.margin, 2)
                    else:
                        net_pnl = round(gross_pnl - total_fees - pos.funding_paid, 2)
                    
                    cash += (pos.margin + net_pnl)
                    del active_positions[sym]
                    last_exit_bar[sym] = i
                    
                    unrealized_rem = sum(
                        ((data[s]['c'][i] - p.entry_price) * p.quantity if p.side == 'LONG'
                        else (p.entry_price - data[s]['c'][i]) * p.quantity) - p.funding_paid
                        for s, p in active_positions.items()
                    )
                    total_equity = round(cash + sum(p.margin for p in active_positions.values()) + unrealized_rem, 2)
                    price_ret = (exit_price - pos.entry_price) / pos.entry_price
                    roe_pct = round((net_pnl / pos.margin) * 100.0, 2)
                    
                    completed_trades.append({
                        'trade_id': pos.trade_id,
                        'symbol': sym,
                        'side': pos.side,
                        'mode': pos.mode,
                        'leverage': f'{pos.leverage:.0f}x',
                        'margin': round(pos.margin, 2),
                        'nominal_size': round(pos.nominal_size, 2),
                        'entry_time': pos.entry_time.strftime('%Y-%m-%d %H:%M UTC'),
                        'entry_price': round(pos.entry_price, 4),
                        'exit_time': bar_end_dt.strftime('%Y-%m-%d %H:%M UTC'),
                        'exit_price': round(exit_price, 4),
                        'stop_loss': round(pos.stop_loss, 4),
                        'take_profit': round(pos.take_profit, 4),
                        'hold_bars': hold_bars,
                        'hold_mins': hold_mins,
                        'total_fees': total_fees,
                        'price_ret_pct': round(price_ret * 100.0, 3),
                        'net_pnl': net_pnl,
                        'roe_pct': roe_pct,
                        'funding_paid': round(pos.funding_paid, 2),
                        'liq_price': round(pos.liq_price, 4),
                        'account_equity': total_equity,
                        'exit_reason': exit_reason,
                        'entry_reason': pos.entry_reason
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
                    exit_reason = f'LIQUIDATION (强平 {pos.liq_price:.4f})'
                elif tp_hit and not sl_hit:
                    exit_price = pos.take_profit
                    exit_reason = f'TP_TARGET (+{pos.tp_pct*100:.1f}%)'
                elif trail_hit:
                    exit_price = trail_exit * (1.0 + slippage)
                    exit_reason = 'TRAILING_STOP'
                elif sl_hit:
                    exit_price = pos.stop_loss * (1.0 + slippage)
                    exit_reason = f'SL_ATR ({swing_sl_mult:.1f}*ATR)'
                elif hold_bars >= max_hold_bars:
                    exit_price = c * (1.0 + slippage)
                    exit_reason = f'TIME_LIMIT_1D ({hold_mins}m)'
                    
                if exit_price is not None:
                    gross_pnl = (pos.entry_price - exit_price) * pos.quantity
                    entry_fee = pos.nominal_size * fee_rate
                    exit_nominal = exit_price * pos.quantity
                    exit_fee = exit_nominal * fee_rate
                    total_fees = round(entry_fee + exit_fee, 2)
                    if liquidated:
                        net_pnl = -round(pos.margin, 2)
                    else:
                        net_pnl = round(gross_pnl - total_fees - pos.funding_paid, 2)
                    
                    cash += (pos.margin + net_pnl)
                    del active_positions[sym]
                    last_exit_bar[sym] = i
                    
                    unrealized_rem = sum(
                        ((data[s]['c'][i] - p.entry_price) * p.quantity if p.side == 'LONG'
                        else (p.entry_price - data[s]['c'][i]) * p.quantity) - p.funding_paid
                        for s, p in active_positions.items()
                    )
                    total_equity = round(cash + sum(p.margin for p in active_positions.values()) + unrealized_rem, 2)
                    price_ret = (pos.entry_price - exit_price) / pos.entry_price
                    roe_pct = round((net_pnl / pos.margin) * 100.0, 2)
                    
                    completed_trades.append({
                        'trade_id': pos.trade_id,
                        'symbol': sym,
                        'side': pos.side,
                        'mode': pos.mode,
                        'leverage': f'{pos.leverage:.0f}x',
                        'margin': round(pos.margin, 2),
                        'nominal_size': round(pos.nominal_size, 2),
                        'entry_time': pos.entry_time.strftime('%Y-%m-%d %H:%M UTC'),
                        'entry_price': round(pos.entry_price, 4),
                        'exit_time': bar_end_dt.strftime('%Y-%m-%d %H:%M UTC'),
                        'exit_price': round(exit_price, 4),
                        'stop_loss': round(pos.stop_loss, 4),
                        'take_profit': round(pos.take_profit, 4),
                        'hold_bars': hold_bars,
                        'hold_mins': hold_mins,
                        'total_fees': total_fees,
                        'price_ret_pct': round(price_ret * 100.0, 3),
                        'net_pnl': net_pnl,
                        'roe_pct': roe_pct,
                        'funding_paid': round(pos.funding_paid, 2),
                        'liq_price': round(pos.liq_price, 4),
                        'account_equity': total_equity,
                        'exit_reason': exit_reason,
                        'entry_reason': pos.entry_reason
                    })
                else:
                    if h > pos.peak: pos.peak = h
                    if l < pos.trough: pos.trough = l

        unrealized_total = sum(
            ((data[s]['c'][i] - p.entry_price) * p.quantity if p.side == 'LONG'
            else (p.entry_price - data[s]['c'][i]) * p.quantity) - p.funding_paid
            for s, p in active_positions.items()
        )
        current_mtm_equity = round(cash + sum(p.margin for p in active_positions.values()) + unrealized_total, 2)
        equity_curve.append({
            'dt': curr_dt,
            'equity': current_mtm_equity,
            'cash': round(cash, 2),
            'active_positions': len(active_positions)
        })

        bar_day = pd.Timestamp(curr_dt).date()
        if bar_day != day_key:
            day_key = bar_day
            day_start_equity = current_mtm_equity
        
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
                mode = None
                reason_desc = ''
                tp_val = 0.0
                sl_mult = 2.0
                
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
                    mode = 'SWING'
                    reason_desc = f'[顺势波段] 12h/24h下行趋势回踩1h/3h均线承压滞涨(MACD绿柱,RSI9={rsi9_i:.1f})'
                    tp_val = swing_tp
                    sl_mult = swing_sl_mult
                elif is_macro_bull and tested_sup and closed_strong and momentum_bull:
                    side = 'LONG'
                    mode = 'SWING'
                    reason_desc = f'[顺势波段] 12h/24h上行趋势回踩1h/3h均线企稳收阳(MACD红柱,RSI9={rsi9_i:.1f})'
                    tp_val = swing_tp
                    sl_mult = swing_sl_mult
                        
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
                        mode=mode,
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
                        entry_reason=reason_desc,
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
                        mode=mode,
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
                        entry_reason=reason_desc,
                        peak=fill_price,
                        trough=fill_price,
                        liq_price=liq,
                        next_funding_dt=next_funding_time(next_dt, funding_interval_hours)
                    )
                    trade_id_counter += 1

    final_bar_idx = end_bar
    final_dt = common_ts[final_bar_idx]
    final_bar_end_dt = final_dt + pd.Timedelta(minutes=5)
    for sym, pos in list(active_positions.items()):
        sd = data[sym]
        c = sd['c'][final_bar_idx]
        hold_bars = final_bar_idx - pos.entry_bar + 1
        hold_mins = hold_bars * 5
        if pos.side == 'LONG':
            exit_price = c * (1.0 - slippage)
            gross_pnl = (exit_price - pos.entry_price) * pos.quantity
        else:
            exit_price = c * (1.0 + slippage)
            gross_pnl = (pos.entry_price - exit_price) * pos.quantity
            
        entry_fee = pos.nominal_size * fee_rate
        exit_nominal = exit_price * pos.quantity
        exit_fee = exit_nominal * fee_rate
        total_fees = round(entry_fee + exit_fee, 2)
        net_pnl = round(gross_pnl - total_fees - pos.funding_paid, 2)
        cash += (pos.margin + net_pnl)
        del active_positions[sym]
        total_equity = round(cash + sum(p.margin for p in active_positions.values()), 2)
        price_ret = (exit_price - pos.entry_price) / pos.entry_price if pos.side == 'LONG' else (pos.entry_price - exit_price) / pos.entry_price
        roe_pct = round((net_pnl / pos.margin) * 100.0, 2)
        
        completed_trades.append({
            'trade_id': pos.trade_id,
            'symbol': sym,
            'side': pos.side,
            'mode': pos.mode,
            'leverage': f'{pos.leverage:.0f}x',
            'margin': round(pos.margin, 2),
            'nominal_size': round(pos.nominal_size, 2),
            'entry_time': pos.entry_time.strftime('%Y-%m-%d %H:%M UTC'),
            'entry_price': round(pos.entry_price, 4),
            'exit_time': final_bar_end_dt.strftime('%Y-%m-%d %H:%M UTC'),
            'exit_price': round(exit_price, 4),
            'stop_loss': round(pos.stop_loss, 4),
            'take_profit': round(pos.take_profit, 4),
            'hold_bars': hold_bars,
            'hold_mins': hold_mins,
            'total_fees': total_fees,
            'price_ret_pct': round(price_ret * 100.0, 3),
            'net_pnl': net_pnl,
            'roe_pct': roe_pct,
            'funding_paid': round(pos.funding_paid, 2),
            'liq_price': round(pos.liq_price, 4),
            'account_equity': total_equity,
            'exit_reason': 'SIMULATION_END',
            'entry_reason': pos.entry_reason
        })

    final_cash_rounded = round(cash, 2)
    equity_curve.append({
        'dt': final_dt,
        'equity': final_cash_rounded,
        'cash': final_cash_rounded,
        'active_positions': 0
    })

    return final_cash_rounded, pd.DataFrame(completed_trades), pd.DataFrame(equity_curve)

def build_full_daily_report(df_trades, df_equity, start_date, end_date, initial_cap=10000.0):
    dates = pd.date_range(start_date, end_date, freq='D')
    
    df_trades = df_trades.copy()
    if len(df_trades) > 0:
        df_trades['exit_date'] = pd.to_datetime(df_trades['exit_time'].str.replace(' UTC', '')).dt.date
    else:
        df_trades['exit_date'] = []
        
    df_equity = df_equity.copy()
    df_equity['date'] = pd.to_datetime(df_equity['dt']).dt.date
    
    daily_records = []
    prev_eq = initial_cap
    
    for d in dates:
        d_date = d.date()
        day_trades = df_trades[df_trades['exit_date'] == d_date] if len(df_trades) > 0 else pd.DataFrame()
        n_trades = len(day_trades)
        realized_pnl = round(day_trades['net_pnl'].sum(), 2) if n_trades > 0 else 0.0
        wins = len(day_trades[day_trades['net_pnl'] > 0]) if n_trades > 0 else 0
        losses = len(day_trades[day_trades['net_pnl'] <= 0]) if n_trades > 0 else 0
        win_rate = round((wins / n_trades) * 100, 1) if n_trades > 0 else 0.0
        
        # Details of symbols traded
        if n_trades > 0:
            syms = ",".join(day_trades['symbol'].str.replace('-USDT-SWAP','').unique())
            top_winner = day_trades['net_pnl'].max()
            top_loser = day_trades['net_pnl'].min()
        else:
            syms = "-"
            top_winner = 0.0
            top_loser = 0.0
        
        # MTM equity at end of this day
        eq_subset = df_equity[df_equity['date'] <= d_date]
        if len(eq_subset) > 0:
            end_eq = eq_subset.iloc[-1]['equity']
        else:
            end_eq = prev_eq
            
        mtm_pnl = round(end_eq - prev_eq, 2)
        ret_pct = round((mtm_pnl / prev_eq) * 100, 2) if prev_eq > 0 else 0.0
        
        daily_records.append({
            '日期': str(d_date),
            '平仓笔数': n_trades,
            '盈利笔数': wins,
            '亏损笔数': losses,
            '当日胜率': f'{win_rate}%' if n_trades > 0 else '-',
            '平仓净盈亏(USDT)': realized_pnl,
            '盯市净值增量(USDT)': mtm_pnl,
            '当日收盘总权益(USDT)': end_eq,
            '当日收益率': f'{ret_pct:+.2f}%',
            '交易标的': syms
        })
        prev_eq = end_eq
        
    return pd.DataFrame(daily_records)

# 1. Isolated 2026-04
print("Computing 2026-04 isolated backtest...")
_, df_apr_iso, eq_apr_iso = run_range_sim('2026-04-01 00:00:00', '2026-04-30 23:55:00', 10000.0)
daily_apr_iso = build_full_daily_report(df_apr_iso, eq_apr_iso, '2026-04-01', '2026-04-30', 10000.0)
daily_apr_iso.to_csv('data/backtest_daily_pnl_2026_04_isolated.csv', index=False, encoding='utf-8-sig')

# 2. Isolated 2026-10
print("Computing 2026-10 isolated backtest...")
_, df_oct_iso, eq_oct_iso = run_range_sim('2026-10-01 00:00:00', '2026-10-31 23:55:00', 10000.0)
daily_oct_iso = build_full_daily_report(df_oct_iso, eq_oct_iso, '2026-10-01', '2026-10-31', 10000.0)
daily_oct_iso.to_csv('data/backtest_daily_pnl_2026_10_isolated.csv', index=False, encoding='utf-8-sig')

print("Finished generating daily PnL CSVs.")

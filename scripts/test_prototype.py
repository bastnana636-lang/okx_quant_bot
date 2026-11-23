import pandas as pd
import numpy as np
from okx_human_trader import load_and_align_candles, SYMBOLS
from dataclasses import dataclass
from typing import Dict, List

@dataclass
class Position:
    trade_id: int
    symbol: str
    side: str
    mode: str
    entry_bar: int
    entry_time: pd.Timestamp
    entry_price: float
    leverage: float
    margin: float
    nominal_size: float
    quantity: float
    stop_loss: float
    take_profit: float
    tp_pct: float
    entry_reason: str
    peak: float
    trough: float
    trough_bar: int
    peak_bar: int

common_ts, data = load_and_align_candles()
n_bars = len(common_ts)

def test_sim(include_bull_swing=True, rigorous_trailing=True):
    initial_cap = 10000.0
    cash = initial_cap
    active_positions: Dict[str, Position] = {}
    completed_trades = []
    equity_curve = []
    trade_id_counter = 1
    start_bar = 288
    max_positions = 3
    margin_ratio = 0.20
    max_margin_cap = 2500.0
    btc_eth_lev = 15.0
    alt_lev = 10.0
    fee_rate = 0.0005
    slippage = 0.0002
    max_hold_bars = 288
    swing_tp = 0.035
    swing_sl_mult = 2.0
    swing_trail_trig = 0.012
    swing_trail_pb = 0.005
    scalp_tp = 0.008
    scalp_sl_mult = 2.0
    scalp_exit_rsi = 60.0

    for i in range(start_bar, n_bars - 1):
        curr_dt = common_ts[i]
        next_dt = common_ts[i+1]

        # 1. Check exits
        for sym, pos in list(active_positions.items()):
            sd = data[sym]
            c = sd['c'][i]
            h = sd['h'][i]
            l = sd['l'][i]
            o = sd['o'][i]
            rsi3 = sd['rsi3'][i]
            hold_bars = i - pos.entry_bar + 1
            hold_mins = hold_bars * 5

            exit_price = None
            exit_reason = ""

            if pos.side == 'LONG':
                sl_hit = (l <= pos.stop_loss)
                tp_hit = (h >= pos.take_profit)

                if pos.mode == 'SWING':
                    trail_hit = False
                    trail_exit = 0.0
                    new_peak = False
                    if h > pos.peak:
                        pos.peak = h
                        pos.peak_bar = i
                        new_peak = True

                    gain = (pos.peak - pos.entry_price) / pos.entry_price
                    if gain >= swing_trail_trig:
                        trail_px = pos.peak * (1.0 - swing_trail_pb)
                        if rigorous_trailing:
                            if new_peak:
                                if c <= trail_px:
                                    trail_hit = True
                                    trail_exit = max(trail_px, pos.stop_loss)
                            else:
                                if l <= trail_px:
                                    trail_hit = True
                                    trail_exit = max(trail_px, pos.stop_loss)
                        else:
                            if l <= trail_px:
                                trail_hit = True
                                trail_exit = max(trail_px, pos.stop_loss)

                    if tp_hit and not sl_hit:
                        exit_price = pos.take_profit
                        exit_reason = f"TP_TARGET (+{pos.tp_pct*100:.1f}%)"
                    elif trail_hit and not sl_hit:
                        exit_price = trail_exit * (1.0 - slippage)
                        exit_reason = "TRAILING_STOP"
                    elif sl_hit:
                        exit_price = pos.stop_loss * (1.0 - slippage)
                        exit_reason = f"SL_ATR ({swing_sl_mult:.1f}*ATR)"
                    elif hold_bars >= max_hold_bars:
                        exit_price = c * (1.0 - slippage)
                        exit_reason = f"TIME_LIMIT_1D ({hold_mins}m)"
                else: # CLIMAX
                    if tp_hit and not sl_hit:
                        exit_price = pos.take_profit
                        exit_reason = f"TP_TARGET (+{pos.tp_pct*100:.1f}%)"
                    elif sl_hit:
                        exit_price = pos.stop_loss * (1.0 - slippage)
                        exit_reason = f"SL_ATR ({scalp_sl_mult:.1f}*ATR)"
                    elif rsi3 >= scalp_exit_rsi:
                        exit_price = c * (1.0 - slippage)
                        exit_reason = f"TP_RSI_REVERT (RSI>={scalp_exit_rsi:.0f})"
                    elif hold_bars >= max_hold_bars:
                        exit_price = c * (1.0 - slippage)
                        exit_reason = f"TIME_LIMIT_1D ({hold_mins}m)"

                if exit_price is not None:
                    gross_pnl = (exit_price - pos.entry_price) * pos.quantity
                    entry_fee = pos.nominal_size * fee_rate
                    exit_nominal = exit_price * pos.quantity
                    exit_fee = exit_nominal * fee_rate
                    total_fees = entry_fee + exit_fee
                    net_pnl = gross_pnl - total_fees
                    cash += (pos.margin + net_pnl)
                    del active_positions[sym]
                    total_eq = cash + sum(p.margin for p in active_positions.values())
                    exit_dt = common_ts[i] + pd.Timedelta(minutes=5)
                    completed_trades.append({
                        'trade_id': pos.trade_id,
                        'symbol': sym,
                        'side': pos.side,
                        'mode': pos.mode,
                        'leverage': f"{pos.leverage:.0f}x",
                        'margin': round(pos.margin, 2),
                        'nominal_size': round(pos.nominal_size, 2),
                        'entry_time': pos.entry_time.strftime('%Y-%m-%d %H:%M UTC'),
                        'entry_price': round(pos.entry_price, 4),
                        'exit_time': exit_dt.strftime('%Y-%m-%d %H:%M UTC'),
                        'exit_price': round(exit_price, 4),
                        'stop_loss': round(pos.stop_loss, 4),
                        'take_profit': round(pos.take_profit, 4),
                        'hold_bars': hold_bars,
                        'hold_mins': hold_mins,
                        'total_fees': round(total_fees, 2),
                        'price_ret_pct': round((exit_price - pos.entry_price) / pos.entry_price * 100.0, 3),
                        'net_pnl': round(net_pnl, 2),
                        'roe_pct': round(net_pnl / pos.margin * 100.0, 2),
                        'account_equity': round(total_eq, 2),
                        'exit_reason': exit_reason,
                        'entry_reason': pos.entry_reason
                    })

            elif pos.side == 'SHORT':
                sl_hit = (h >= pos.stop_loss)
                tp_hit = (l <= pos.take_profit)

                if pos.mode == 'SWING':
                    trail_hit = False
                    trail_exit = 0.0
                    new_trough = False
                    if l < pos.trough:
                        pos.trough = l
                        pos.trough_bar = i
                        new_trough = True

                    gain = (pos.entry_price - pos.trough) / pos.entry_price
                    if gain >= swing_trail_trig:
                        trail_px = pos.trough * (1.0 + swing_trail_pb)
                        if rigorous_trailing:
                            if new_trough:
                                if c >= trail_px:
                                    trail_hit = True
                                    trail_exit = min(trail_px, pos.stop_loss)
                            else:
                                if h >= trail_px:
                                    trail_hit = True
                                    trail_exit = min(trail_px, pos.stop_loss)
                        else:
                            if h >= trail_px:
                                trail_hit = True
                                trail_exit = min(trail_px, pos.stop_loss)

                    if tp_hit and not sl_hit:
                        exit_price = pos.take_profit
                        exit_reason = f"TP_TARGET (+{pos.tp_pct*100:.1f}%)"
                    elif trail_hit and not sl_hit:
                        exit_price = trail_exit * (1.0 + slippage)
                        exit_reason = "TRAILING_STOP"
                    elif sl_hit:
                        exit_price = pos.stop_loss * (1.0 + slippage)
                        exit_reason = f"SL_ATR ({swing_sl_mult:.1f}*ATR)"
                    elif hold_bars >= max_hold_bars:
                        exit_price = c * (1.0 + slippage)
                        exit_reason = f"TIME_LIMIT_1D ({hold_mins}m)"
                else: # CLIMAX
                    if tp_hit and not sl_hit:
                        exit_price = pos.take_profit
                        exit_reason = f"TP_TARGET (+{pos.tp_pct*100:.1f}%)"
                    elif sl_hit:
                        exit_price = pos.stop_loss * (1.0 + slippage)
                        exit_reason = f"SL_ATR ({scalp_sl_mult:.1f}*ATR)"
                    elif rsi3 <= (100.0 - scalp_exit_rsi):
                        exit_price = c * (1.0 + slippage)
                        exit_reason = f"TP_RSI_REVERT (RSI<={100-scalp_exit_rsi:.0f})"
                    elif hold_bars >= max_hold_bars:
                        exit_price = c * (1.0 + slippage)
                        exit_reason = f"TIME_LIMIT_1D ({hold_mins}m)"

                if exit_price is not None:
                    gross_pnl = (pos.entry_price - exit_price) * pos.quantity
                    entry_fee = pos.nominal_size * fee_rate
                    exit_nominal = exit_price * pos.quantity
                    exit_fee = exit_nominal * fee_rate
                    total_fees = entry_fee + exit_fee
                    net_pnl = gross_pnl - total_fees
                    cash += (pos.margin + net_pnl)
                    del active_positions[sym]
                    total_eq = cash + sum(p.margin for p in active_positions.values())
                    exit_dt = common_ts[i] + pd.Timedelta(minutes=5)
                    completed_trades.append({
                        'trade_id': pos.trade_id,
                        'symbol': sym,
                        'side': pos.side,
                        'mode': pos.mode,
                        'leverage': f"{pos.leverage:.0f}x",
                        'margin': round(pos.margin, 2),
                        'nominal_size': round(pos.nominal_size, 2),
                        'entry_time': pos.entry_time.strftime('%Y-%m-%d %H:%M UTC'),
                        'entry_price': round(pos.entry_price, 4),
                        'exit_time': exit_dt.strftime('%Y-%m-%d %H:%M UTC'),
                        'exit_price': round(exit_price, 4),
                        'stop_loss': round(pos.stop_loss, 4),
                        'take_profit': round(pos.take_profit, 4),
                        'hold_bars': hold_bars,
                        'hold_mins': hold_mins,
                        'total_fees': round(total_fees, 2),
                        'price_ret_pct': round((pos.entry_price - exit_price) / pos.entry_price * 100.0, 3),
                        'net_pnl': round(net_pnl, 2),
                        'roe_pct': round(net_pnl / pos.margin * 100.0, 2),
                        'account_equity': round(total_eq, 2),
                        'exit_reason': exit_reason,
                        'entry_reason': pos.entry_reason
                    })

        # Mark-to-market equity with unrealized PnL
        unrealized = 0.0
        for p in active_positions.values():
            cur_c = data[p.symbol]['c'][i]
            if p.side == 'LONG':
                unrealized += (cur_c - p.entry_price) * p.quantity
            else:
                unrealized += (p.entry_price - cur_c) * p.quantity
        mtm_equity = cash + sum(p.margin for p in active_positions.values()) + unrealized
        equity_curve.append({'dt': curr_dt, 'equity': mtm_equity, 'cash': cash, 'active': len(active_positions)})

        # 2. Check entries
        if len(active_positions) < max_positions and cash > 500:
            candidates = []
            for sym in SYMBOLS:
                if sym in active_positions:
                    continue
                sd = data[sym]
                c_i = sd['c'][i]
                o_i = sd['o'][i]
                h_i = sd['h'][i]
                l_i = sd['l'][i]
                rsi3_i = sd['rsi3'][i]
                rsi9_i = sd['rsi9'][i]
                macd_h = sd['macd_hist'][i]
                atr_i = sd['atr14'][i]
                ema12_i = sd['ema12'][i]
                ema36_i = sd['ema36'][i]
                ema144_i = sd['ema144'][i]
                ema288_i = sd['ema288'][i]
                ema200_i = sd['ema200'][i]

                side = None
                mode = None
                reason_desc = ''
                tp_val = 0.0
                sl_mult = 2.0
                score = 0.0

                is_bear = (c_i < ema200_i)
                os_thresh = 3.0 if is_bear else 7.5
                ob_thresh = 93.5 if is_bear else 98.0

                if rsi3_i <= os_thresh and c_i > o_i:
                    side = 'LONG'
                    mode = 'CLIMAX'
                    trend_tag = 'EMA200下方逆势极端恐慌抛售衰竭' if is_bear else 'EMA200多头趋势回踩支撑企稳'
                    reason_desc = f'[极值反抽] {trend_tag}(RSI3={rsi3_i:.1f})+收阳确认'
                    tp_val = scalp_tp
                    sl_mult = scalp_sl_mult
                    score = 100.0 - rsi3_i
                elif rsi3_i >= ob_thresh and c_i < o_i:
                    side = 'SHORT'
                    mode = 'CLIMAX'
                    trend_tag = 'EMA200空头趋势顺势极值反弹诱多' if is_bear else 'EMA200上方极端冲高买盘枯竭'
                    reason_desc = f'[极值做空] {trend_tag}(RSI3={rsi3_i:.1f})+收阴滞涨'
                    tp_val = scalp_tp
                    sl_mult = scalp_sl_mult
                    score = rsi3_i
                else:
                    # Trend Swing
                    is_macro_bear = (c_i < ema144_i) and (ema144_i < ema288_i)
                    tested_res = (h_i >= ema12_i * 0.998) or (h_i >= ema36_i * 0.998)
                    closed_weak = (c_i < o_i) and (c_i < ema12_i)
                    momentum_bear = (macd_h < 0) and (rsi9_i >= 50.0 and rsi9_i <= 68.0)

                    if is_macro_bear and tested_res and closed_weak and momentum_bear:
                        side = 'SHORT'
                        mode = 'SWING'
                        reason_desc = f'[顺势波段] 12h/24h下行趋势回踩1h/3h均线承压滞涨(MACD绿柱,RSI9={rsi9_i:.1f})'
                        tp_val = swing_tp
                        sl_mult = swing_sl_mult
                        score = 50.0 + abs(macd_h)

                    elif include_bull_swing:
                        is_macro_bull = (c_i > ema144_i) and (ema144_i > ema288_i)
                        tested_sup = (l_i <= ema12_i * 1.002) or (l_i <= ema36_i * 1.002)
                        closed_strong = (c_i > o_i) and (c_i > ema12_i)
                        momentum_bull = (macd_h > 0) and (rsi9_i >= 32.0 and rsi9_i <= 50.0)
                        if is_macro_bull and tested_sup and closed_strong and momentum_bull:
                            side = 'LONG'
                            mode = 'SWING'
                            reason_desc = f'[顺势波段] 12h/24h上行趋势回踩1h/3h均线企稳收阳(MACD红柱,RSI9={rsi9_i:.1f})'
                            tp_val = swing_tp
                            sl_mult = swing_sl_mult
                            score = 50.0 + abs(macd_h)

                if side is not None:
                    candidates.append({
                        'sym': sym, 'side': side, 'mode': mode, 'score': score,
                        'reason_desc': reason_desc, 'tp_val': tp_val, 'sl_mult': sl_mult,
                        'atr_i': atr_i
                    })

            candidates.sort(key=lambda x: x['score'], reverse=True)

            for cand in candidates:
                if len(active_positions) >= max_positions:
                    break
                sym = cand['sym']
                side = cand['side']
                mode = cand['mode']
                sd = data[sym]
                atr_i = cand['atr_i']
                lev = btc_eth_lev if ('BTC' in sym or 'ETH' in sym) else alt_lev
                margin = min(mtm_equity * margin_ratio, max_margin_cap)
                if margin > cash:
                    margin = cash * 0.90
                if margin < 100:
                    continue

                cash -= margin
                nominal = margin * lev
                next_open = sd['o'][i+1]
                fill_price = next_open * (1.0 + slippage) if side == 'LONG' else next_open * (1.0 - slippage)
                qty = nominal / fill_price
                sl = (fill_price - cand['sl_mult'] * atr_i) if side == 'LONG' else (fill_price + cand['sl_mult'] * atr_i)
                tp = (fill_price * (1.0 + cand['tp_val'])) if side == 'LONG' else (fill_price * (1.0 - cand['tp_val']))

                active_positions[sym] = Position(
                    trade_id=trade_id_counter,
                    symbol=sym,
                    side=side,
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
                    tp_pct=cand['tp_val'],
                    entry_reason=cand['reason_desc'],
                    peak=fill_price,
                    trough=fill_price,
                    peak_bar=i+1,
                    trough_bar=i+1
                )
                trade_id_counter += 1

    df_trades = pd.DataFrame(completed_trades)
    df_eq = pd.DataFrame(equity_curve)
    pnl = df_trades['net_pnl'].sum()
    wr = (df_trades['net_pnl'] > 0).mean() * 100
    final_eq = df_trades['account_equity'].iloc[-1]
    print(f"Config: bull_swing={include_bull_swing}, rigorous_trailing={rigorous_trailing} -> Trades: {len(df_trades)}, Net PnL: ${pnl:.2f}, WR: {wr:.1f}%, Final Eq: ${final_eq:.2f}")
    return df_trades, df_eq

if __name__ == '__main__':
    test_sim(include_bull_swing=True, rigorous_trailing=True)
    test_sim(include_bull_swing=False, rigorous_trailing=True)
    test_sim(include_bull_swing=False, rigorous_trailing=False)

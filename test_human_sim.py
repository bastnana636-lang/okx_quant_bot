import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass
from typing import List, Dict, Optional

DATA_DIR = Path('data/okx_candles')
SYMBOLS = [
    'BTC-USDT-SWAP', 'ETH-USDT-SWAP', 'SOL-USDT-SWAP', 'DOGE-USDT-SWAP', 'XRP-USDT-SWAP',
    'ADA-USDT-SWAP', 'SUI-USDT-SWAP', 'LINK-USDT-SWAP', 'LTC-USDT-SWAP', 'AVAX-USDT-SWAP'
]

def load_all_data():
    dfs = {}
    for s in SYMBOLS:
        df = pd.read_csv(DATA_DIR / f'{s}_5m_last_month.csv')
        df['dt'] = pd.to_datetime(df['dt'])
        dfs[s] = df.sort_values('dt').reset_index(drop=True)

    ts_set = set(dfs[SYMBOLS[0]]['dt'])
    for s in SYMBOLS[1:]:
        ts_set = ts_set.intersection(set(dfs[s]['dt']))
    common_ts = sorted(list(ts_set))

    market_data = {}
    for s in SYMBOLS:
        df = dfs[s].set_index('dt').loc[common_ts].reset_index()
        c = df['c'].to_numpy(dtype=np.float64)
        h = df['h'].to_numpy(dtype=np.float64)
        l = df['l'].to_numpy(dtype=np.float64)
        o = df['o'].to_numpy(dtype=np.float64)
        v = df['vol'].to_numpy(dtype=np.float64)
        dt = df['dt'].tolist()
        
        # RSI 3
        diff = pd.Series(c).diff()
        up = diff.clip(lower=0)
        down = -1 * diff.clip(upper=0)
        ma_up = up.ewm(alpha=1.0/3, adjust=False).mean()
        ma_down = down.ewm(alpha=1.0/3, adjust=False).mean()
        rsi = (100.0 - (100.0 / (1.0 + ma_up / (ma_down + 1e-9)))).to_numpy()
        
        # ATR 14
        prev_c = np.roll(c, 1)
        prev_c[0] = c[0]
        tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
        atr = pd.Series(tr).rolling(14, min_periods=14).mean().to_numpy()
        
        # EMA 200
        ema200 = pd.Series(c).ewm(span=200, adjust=False).mean().to_numpy()
        
        market_data[s] = {
            'c': c, 'h': h, 'l': l, 'o': o, 'v': v, 'dt': dt,
            'rsi': rsi, 'atr': atr, 'ema200': ema200
        }
    return common_ts, market_data

@dataclass
class Position:
    trade_id: int
    symbol: str
    side: str # 'LONG' or 'SHORT'
    entry_bar: int
    entry_time: str
    entry_price: float
    leverage: float
    margin: float
    nominal_size: float
    quantity: float
    stop_loss: float
    take_profit: float
    entry_reason: str

def run_simulation(
    initial_capital: float = 10000.0,
    max_positions: int = 2,
    margin_ratio: float = 0.15,
    tp_pct: float = 0.008,
    sl_atr_mult: float = 1.8,
    max_hold_bars: int = 12,
    exit_rsi: float = 55.0,
    base_os: float = 5.0,
    base_ob: float = 96.0,
    fee_rate: float = 0.0005,
    slippage: float = 0.0002
):
    common_ts, data = load_all_data()
    n_bars = len(common_ts)
    
    cash = initial_capital
    active_positions: Dict[str, Position] = {}
    completed_trades = []
    trade_id_counter = 1
    
    start_bar = 200
    
    for i in range(start_bar, n_bars - 1):
        curr_dt = common_ts[i]
        next_dt = common_ts[i+1]
        
        # 1. Update and check active positions during bar i
        closed_symbols = []
        for sym, pos in active_positions.items():
            sym_data = data[sym]
            c = sym_data['c'][i]
            h = sym_data['h'][i]
            l = sym_data['l'][i]
            cur_rsi = sym_data['rsi'][i]
            
            exit_price = None
            exit_reason = ""
            
            if pos.side == 'LONG':
                sl_hit = (l <= pos.stop_loss)
                tp_hit = (h >= pos.take_profit)
                
                # Check target TP first if reached
                if tp_hit and not sl_hit:
                    exit_price = pos.take_profit
                    exit_reason = "TP_TARGET"
                elif sl_hit and not tp_hit:
                    exit_price = pos.stop_loss * (1.0 - slippage)
                    exit_reason = "SL_ATR"
                elif sl_hit and tp_hit:
                    # Conservative
                    exit_price = pos.stop_loss * (1.0 - slippage)
                    exit_reason = "SL_ATR"
                elif cur_rsi >= exit_rsi:
                    exit_price = c * (1.0 - slippage)
                    exit_reason = "TP_RSI_REVERT"
                elif i - pos.entry_bar >= max_hold_bars:
                    exit_price = c * (1.0 - slippage)
                    exit_reason = "TIME_LIMIT"
                    
                if exit_price is not None:
                    gross_pnl = (exit_price - pos.entry_price) * pos.quantity
                    entry_fee = pos.nominal_size * fee_rate
                    exit_nominal = exit_price * pos.quantity
                    exit_fee = exit_nominal * fee_rate
                    net_pnl = gross_pnl - (entry_fee + exit_fee)
                    
                    cash += (pos.margin + net_pnl)
                    ret_pct = net_pnl / pos.margin
                    price_ret = (exit_price - pos.entry_price) / pos.entry_price
                    
                    completed_trades.append({
                        'trade_id': pos.trade_id,
                        'symbol': sym,
                        'side': pos.side,
                        'leverage': pos.leverage,
                        'margin': pos.margin,
                        'nominal_size': pos.nominal_size,
                        'entry_time': pos.entry_time,
                        'entry_price': pos.entry_price,
                        'exit_time': str(curr_dt),
                        'exit_price': exit_price,
                        'stop_loss': pos.stop_loss,
                        'take_profit': pos.take_profit,
                        'hold_bars': i - pos.entry_bar,
                        'hold_mins': (i - pos.entry_bar) * 5,
                        'exit_reason': exit_reason,
                        'entry_reason': pos.entry_reason,
                        'price_ret_pct': price_ret * 100.0,
                        'net_pnl': net_pnl,
                        'roe_pct': ret_pct * 100.0,
                        'balance_after': cash
                    })
                    closed_symbols.append(sym)
                    
            elif pos.side == 'SHORT':
                sl_hit = (h >= pos.stop_loss)
                tp_hit = (l <= pos.take_profit)
                
                if tp_hit and not sl_hit:
                    exit_price = pos.take_profit
                    exit_reason = "TP_TARGET"
                elif sl_hit and not tp_hit:
                    exit_price = pos.stop_loss * (1.0 + slippage)
                    exit_reason = "SL_ATR"
                elif sl_hit and tp_hit:
                    exit_price = pos.stop_loss * (1.0 + slippage)
                    exit_reason = "SL_ATR"
                elif cur_rsi <= (100.0 - exit_rsi):
                    exit_price = c * (1.0 + slippage)
                    exit_reason = "TP_RSI_REVERT"
                elif i - pos.entry_bar >= max_hold_bars:
                    exit_price = c * (1.0 + slippage)
                    exit_reason = "TIME_LIMIT"
                    
                if exit_price is not None:
                    gross_pnl = (pos.entry_price - exit_price) * pos.quantity
                    entry_fee = pos.nominal_size * fee_rate
                    exit_nominal = exit_price * pos.quantity
                    exit_fee = exit_nominal * fee_rate
                    net_pnl = gross_pnl - (entry_fee + exit_fee)
                    
                    cash += (pos.margin + net_pnl)
                    ret_pct = net_pnl / pos.margin
                    price_ret = (pos.entry_price - exit_price) / pos.entry_price
                    
                    completed_trades.append({
                        'trade_id': pos.trade_id,
                        'symbol': sym,
                        'side': pos.side,
                        'leverage': pos.leverage,
                        'margin': pos.margin,
                        'nominal_size': pos.nominal_size,
                        'entry_time': pos.entry_time,
                        'entry_price': pos.entry_price,
                        'exit_time': str(curr_dt),
                        'exit_price': exit_price,
                        'stop_loss': pos.stop_loss,
                        'take_profit': pos.take_profit,
                        'hold_bars': i - pos.entry_bar,
                        'hold_mins': (i - pos.entry_bar) * 5,
                        'exit_reason': exit_reason,
                        'entry_reason': pos.entry_reason,
                        'price_ret_pct': price_ret * 100.0,
                        'net_pnl': net_pnl,
                        'roe_pct': ret_pct * 100.0,
                        'balance_after': cash
                    })
                    closed_symbols.append(sym)
                    
        for sym in closed_symbols:
            del active_positions[sym]
            
        # 2. Check for new entry signals at the close of bar i
        if len(active_positions) < max_positions and cash > 500:
            for sym in SYMBOLS:
                if sym in active_positions:
                    continue
                if len(active_positions) >= max_positions:
                    break
                    
                sym_data = data[sym]
                c_i = sym_data['c'][i]
                o_i = sym_data['o'][i]
                rsi_i = sym_data['rsi'][i]
                atr_i = sym_data['atr'][i]
                ema_i = sym_data['ema200'][i]
                
                is_bear = (c_i < ema_i)
                if is_bear:
                    os_thresh = base_os - 2.0  # 3.0
                    ob_thresh = base_ob - 2.5  # 93.5
                else:
                    os_thresh = base_os + 2.5  # 7.5
                    ob_thresh = base_ob + 2.0  # 98.0
                    
                long_trigger = (rsi_i <= os_thresh) and (c_i > o_i)
                short_trigger = (rsi_i >= ob_thresh) and (c_i < o_i)
                
                if not long_trigger and not short_trigger:
                    continue
                    
                lev = 10.0 if ('BTC' in sym or 'ETH' in sym) else 7.0
                margin = min(cash * margin_ratio, 2000.0)
                if margin < 100:
                    continue
                    
                cash -= margin
                nominal = margin * lev
                
                next_open = sym_data['o'][i+1]
                
                if long_trigger:
                    fill_price = next_open * (1.0 + slippage)
                    qty = nominal / fill_price
                    sl = fill_price - sl_atr_mult * atr_i
                    tp = fill_price * (1.0 + tp_pct)
                    reason_desc = f"EMA200{'下方极值恐慌抛压清算' if is_bear else '多头趋势回踩支撑'}(RSI3={rsi_i:.1f})+阳线收盘企稳"
                    active_positions[sym] = Position(
                        trade_id=trade_id_counter,
                        symbol=sym,
                        side='LONG',
                        entry_bar=i+1,
                        entry_time=str(next_dt),
                        entry_price=fill_price,
                        leverage=lev,
                        margin=margin,
                        nominal_size=nominal,
                        quantity=qty,
                        stop_loss=sl,
                        take_profit=tp,
                        entry_reason=reason_desc
                    )
                    trade_id_counter += 1
                elif short_trigger:
                    fill_price = next_open * (1.0 - slippage)
                    qty = nominal / fill_price
                    sl = fill_price + sl_atr_mult * atr_i
                    tp = fill_price * (1.0 - tp_pct)
                    reason_desc = f"EMA200{'空头趋势顺势极值反弹' if is_bear else '上方阻力区极端买盘衰竭'}(RSI3={rsi_i:.1f})+阴线冲高回落"
                    active_positions[sym] = Position(
                        trade_id=trade_id_counter,
                        symbol=sym,
                        side='SHORT',
                        entry_bar=i+1,
                        entry_time=str(next_dt),
                        entry_price=fill_price,
                        leverage=lev,
                        margin=margin,
                        nominal_size=nominal,
                        quantity=qty,
                        stop_loss=sl,
                        take_profit=tp,
                        entry_reason=reason_desc
                    )
                    trade_id_counter += 1

    df_trades = pd.DataFrame(completed_trades)
    return cash, df_trades

if __name__ == '__main__':
    final_cash, df = run_simulation()
    print(f"Final Cash: ${final_cash:.2f}")
    print(f"Total Trades: {len(df)}")
    if len(df) > 0:
        wins = df[df['net_pnl'] > 0]
        losses = df[df['net_pnl'] <= 0]
        wr = len(wins) / len(df) * 100
        total_pnl = df['net_pnl'].sum()
        pf = wins['net_pnl'].sum() / abs(losses['net_pnl'].sum()) if len(losses) > 0 else 999
        print(f"Win Rate: {wr:.2f}% ({len(wins)} W / {len(losses)} L)")
        print(f"Total Net PnL: ${total_pnl:.2f} ({(total_pnl/10000)*100:+.2f}%)")
        print(f"Profit Factor: {pf:.2f}")

from dataclasses import replace
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from controllers.market_making.mean_reversion import (
    MeanReversionSettings, ReversionSnapshot, calculate_barriers, calculate_snapshot, reversion_side,
)

D = Decimal
NOW = 1800000000


def candles(last=97.5):
    close = 100 + np.sin(np.arange(48) * 0.7) * 1.2
    close[-1] = last
    return pd.DataFrame({
        'timestamp': NOW - (48 - np.arange(48)) * 300,
        'open': close, 'high': close + 0.15, 'low': close - 0.15, 'close': close,
    })


@pytest.mark.parametrize('z,side', [('-3.6', 0), ('-3.5', 1), ('-2', 1), ('-1.99', 0),
                                     ('0', 0), ('1.99', 0), ('2', -1), ('3.5', -1), ('3.6', 0)])
def test_only_fade_moderate_deviations(z, side):
    assert reversion_side(D(z), MeanReversionSettings()) == side


@pytest.mark.parametrize('price,side', [('97.5', 1), ('102.5', -1)])
def test_closed_and_live_prices_must_agree(price, side):
    frame = candles(float(price))
    result = calculate_snapshot(frame, NOW, D(price), MeanReversionSettings())
    assert result.signal == side
    assert calculate_snapshot(frame, NOW, D('100'), MeanReversionSettings()).signal == 0
    assert calculate_snapshot(frame, NOW, D('110'), MeanReversionSettings()).signal == 0


def test_unfinished_and_future_candles_cannot_change_signal():
    frame = candles()
    expected = calculate_snapshot(frame, NOW + 100, D('97.5'), MeanReversionSettings())
    frame.loc[len(frame)] = [NOW, 5000, 9000, 1, 5000]
    frame.loc[len(frame)] = [NOW + 300, 9999, 9999, 1, 9999]
    assert calculate_snapshot(frame, NOW + 100, D('97.5'), MeanReversionSettings()) == expected


@pytest.mark.parametrize('case', ['missing', 'duplicate', 'nan', 'infinite', 'zero', 'range', 'flat', 'warmup', 'stale'])
def test_bad_data_blocks_entries(case):
    frame = candles()
    now = NOW
    if case == 'missing':
        frame.loc[10, 'timestamp'] -= 300
    elif case == 'duplicate':
        frame.loc[10, 'timestamp'] = frame.loc[9, 'timestamp']
    elif case in ('nan', 'infinite', 'zero'):
        frame.loc[20, 'close'] = {'nan': np.nan, 'infinite': np.inf, 'zero': 0}[case]
    elif case == 'range':
        frame.loc[20, 'high'] = 1
    elif case == 'flat':
        frame[['open', 'high', 'low', 'close']] = 100
    elif case == 'warmup':
        frame = frame.iloc[1:]
    else:
        now += 601
    with pytest.raises(ValueError):
        calculate_snapshot(frame, now, D('97.5'), MeanReversionSettings())


@pytest.mark.parametrize('mid', ['0', '-1', 'NaN', 'Infinity'])
def test_bad_mid_price_fails_closed(mid):
    with pytest.raises(ValueError):
        calculate_snapshot(candles(), NOW, D(mid), MeanReversionSettings())


def snapshot(side=1, std='1', atr='0.4'):
    return ReversionSnapshot(NOW - 300, D('100'), D(std), D(atr), D('-2.5'), D('-2.5'), side)


@pytest.mark.parametrize('side,entry,target', [(1, '97.5', '99.85'), (-1, '102.5', '100.15')])
def test_profit_target_is_near_the_frozen_mean(side, entry, target):
    result = calculate_barriers(snapshot(side), D(entry), MeanReversionSettings())
    assert result.target_price == D(target)
    assert result.take_profit == side * (D(target) - D(entry)) / D(entry)
    assert D('0.006') <= result.stop_loss <= D('0.02')


@pytest.mark.parametrize('case', ['cost', 'risk', 'volatile', 'already_reverted', 'no_signal'])
def test_entries_need_net_edge_and_bounded_risk(case):
    config = MeanReversionSettings()
    state, entry = snapshot(), D('97.5')
    if case == 'cost':
        config = MeanReversionSettings(round_trip_cost_pct='0.03')
    elif case == 'risk':
        config = MeanReversionSettings(min_reward_risk='10')
    elif case == 'volatile':
        state = snapshot(atr='2')
    elif case == 'already_reverted':
        entry = D('100')
    else:
        state = replace(state, signal=0)
    with pytest.raises(ValueError):
        calculate_barriers(state, entry, config)


@pytest.mark.parametrize('invalid', [
    {'mean_window': 2}, {'entry_z_score': 4}, {'exit_z_score': 2},
    {'min_stop_loss': '.03'}, {'min_std_pct': 0}, {'round_trip_cost_pct': '-1'},
    {'entry_z_score': 'NaN'}, {'stop_loss_cooldown_time': 10},
    {'time_limit': 60, 'executor_refresh_time': 60}, {'candles_interval': '7m'},
])
def test_parameter_validation(invalid):
    with pytest.raises(ValidationError):
        MeanReversionSettings(**invalid)


def test_live_price_spike_triggers_without_closed_confirmation():
    frame = candles(100.0)
    snapshot_default = calculate_snapshot(frame, NOW, D('97.5'), MeanReversionSettings())
    assert snapshot_default.signal == 1
    assert snapshot_default.closed_z == snapshot_default.closed_z_score
    assert snapshot_default.live_z == snapshot_default.z_score

    snapshot_strict = calculate_snapshot(
        frame, NOW, D('97.5'), MeanReversionSettings(require_closed_candle_confirmation=True)
    )
    assert snapshot_strict.signal == 0

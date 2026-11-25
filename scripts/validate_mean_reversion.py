"""Read-only startup check for the active controller configs and executor schema."""

import argparse
from pathlib import Path

import yaml

from controllers.market_making.human_swing import human_swing_notional, human_swing_snapshot  # noqa: F401
from controllers.market_making.momentum import momentum_notional, momentum_snapshot  # noqa: F401
from controllers.market_making.pmm_simple import PMMSimpleConfig, PMMSimpleController
from hummingbot.strategy_v2.executors.position_executor.data_types import TripleBarrierConfig
from hummingbot.strategy_v2.executors.position_executor.position_executor import PositionExecutor  # noqa: F401


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="conf_okx_multi.yml")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    script = yaml.safe_load((root / "conf/scripts" / args.config).read_text())
    configs = [PMMSimpleConfig(**yaml.safe_load((root / "conf/controllers" / name).read_text()))
               for name in script["controllers_config"]]
    if not configs:
        raise ValueError("No controllers configured")
    if len({config.id for config in configs}) != len(configs):
        raise ValueError("Duplicate controller ids")
    if len({config.trading_pair for config in configs}) != len(configs):
        raise ValueError("Only one controller per trading pair is supported")
    for config in configs:
        assert config.get_controller_class() is PMMSimpleController
        barriers = TripleBarrierConfig(
            take_profit_quote=config.take_profit_quote if config.signal_mode == "reversion" else None,
            take_profit=config.swing_tp_pct if config.signal_mode == "human_swing" else None,
        )
        assert barriers is not None
        print(
            f"{config.trading_pair}: mode={config.signal_mode}; lev={config.leverage}x; "
            f"cap={config.total_amount_quote} USDT; risk_frac={config.risk_frac}"
        )
    print("Strategy imports and configuration validated. No exchange orders were submitted.")


if __name__ == "__main__":
    main()

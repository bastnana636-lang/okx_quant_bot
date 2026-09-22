"""Read-only startup check for the active mean-reversion config and executor schema."""

import argparse
from pathlib import Path

import yaml

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
        raise ValueError("Only one mean-reversion controller per trading pair is supported")
    for config in configs:
        assert config.get_controller_class() is PMMSimpleController
        barriers = TripleBarrierConfig(take_profit_quote=config.take_profit_quote)
        assert barriers.take_profit_quote == config.take_profit_quote
        print(f"{config.trading_pair}: notional <= {config.total_amount_quote} USDT; "
              f"leverage {config.leverage}; cash TP {config.take_profit_quote} USDT per position")
    print("Strategy imports and configuration validated. No exchange orders were submitted.")


if __name__ == "__main__":
    main()

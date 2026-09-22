import asyncio

import aiohttp

PAIRS = {
    "BTC-USDT-SWAP": "BTC-USDT",
    "ETH-USDT-SWAP": "ETH-USDT",
    "SOL-USDT-SWAP": "SOL-USDT",
    "XRP-USDT-SWAP": "XRP-USDT",
    "DOGE-USDT-SWAP": "DOGE-USDT",
}
LEVELS = 4
FAR_SELL_SPREAD = 0.004


async def main() -> None:
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(trust_env=True) as session:
        async with session.get(
            "https://www.okx.com/api/v5/public/instruments?instType=SWAP",
            timeout=timeout,
        ) as resp:
            instruments = {item["instId"]: item for item in (await resp.json()).get("data", [])}
        async with session.get(
            "https://www.okx.com/api/v5/market/tickers?instType=SWAP",
            timeout=timeout,
        ) as resp:
            tickers = {item["instId"]: item for item in (await resp.json()).get("data", [])}

    print("pair\tlast\tminSz\tmin_notional\tneed_per_pair")
    total = 0.0
    for inst_id, name in PAIRS.items():
        inst = instruments[inst_id]
        last = float(tickers[inst_id]["last"])
        min_sz = float(inst["minSz"])
        ct_val = float(inst.get("ctVal") or 1)
        min_base = min_sz * ct_val
        min_notional = min_base * last * (1 + FAR_SELL_SPREAD)
        need = min_notional * LEVELS * 1.15
        total += need
        print(
            f"{name}\tlast={last}\tminSz={min_sz}\tctVal={ct_val}\t"
            f"minBase={min_base}\tminNotional={min_notional:.2f}\tneed={need:.1f}"
        )
    print(f"total_five_pairs\t{total:.0f}")


if __name__ == "__main__":
    asyncio.run(main())

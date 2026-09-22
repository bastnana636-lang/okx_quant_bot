import asyncio

import aiohttp


async def main() -> None:
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(trust_env=True) as session:
        async with session.get(
            "https://www.okx.com/api/v5/public/instruments?instType=SWAP", timeout=timeout
        ) as resp:
            instruments = await resp.json()
        async with session.get(
            "https://www.okx.com/api/v5/market/tickers?instType=SWAP", timeout=timeout
        ) as resp:
            tickers = await resp.json()
        async with session.get(
            "https://www.okx.com/api/v5/public/instruments?instType=SPOT", timeout=timeout
        ) as resp:
            spots = await resp.json()

    swaps = [
        item for item in instruments.get("data", [])
        if item.get("settleCcy") == "USDT" and item.get("state") == "live"
    ]
    spot_pairs = [
        item for item in spots.get("data", [])
        if item.get("quoteCcy") == "USDT" and item.get("state") == "live"
    ]
    quote_volume = {}
    for ticker in tickers.get("data", []):
        inst_id = ticker.get("instId", "")
        if not inst_id.endswith("-USDT-SWAP"):
            continue
        last = float(ticker.get("last") or 0)
        base_vol = float(ticker.get("volCcy24h") or 0)
        quote_volume[inst_id] = base_vol * last

    ranked = sorted(swaps, key=lambda item: quote_volume.get(item["instId"], 0), reverse=True)
    print(f"live_usdt_swaps={len(swaps)}")
    print(f"live_usdt_spots={len(spot_pairs)}")
    print("top15_quote_volume")
    for item in ranked[:15]:
        inst_id = item["instId"]
        print(f"{inst_id}\tminSz={item.get('minSz')}\tquote24h={int(quote_volume.get(inst_id, 0))}")


if __name__ == "__main__":
    asyncio.run(main())

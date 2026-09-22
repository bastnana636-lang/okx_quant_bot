"""Read-only startup check, streamed into the container by make wait-ready.

The CLI's start command only waits for the strategy process. Request fresh engine
snapshots until connectors and the mean-reversion controllers have initialized.
No orders are submitted or cancelled, including on timeout.
"""
import argparse
import math
import time


def pending_dependencies(snapshot):
    engine = snapshot.get("engine") or {}
    pending = []
    if not engine.get("strategy_running") or not engine.get("clock_running"):
        pending.append("strategy/clock initializing")
    connectors = engine.get("connectors") or {}
    if not connectors:
        pending.append("connectors initializing")
    for name, connector in connectors.items():
        if not connector.get("ready"):
            pending.append(f"{name}: market connector initializing")
    text = snapshot.get("format_status") or ""
    if not text.strip():
        pending.append("strategy status unavailable")
    elif "Market connectors are not ready" in text:
        pending.append("strategy waiting for market connectors")
    else:
        controller = "controller"
        for line in text.splitlines():
            if line.strip().startswith("Controller:"):
                controller = line.strip()
            if "waiting_for_data" in line or "leverage_not_ready" in line:
                pending.append(f"{controller}: {line.strip()}")
    return pending


def wait_for_ready(bot, refresh, config, timeout):
    """Require a fresh snapshot from the same running PID and requested config."""
    if not math.isfinite(timeout) or timeout <= 0:
        print("Readiness timeout must be a positive, finite number.", flush=True)
        return 4
    deadline = time.monotonic() + timeout
    pid = bot.read_pid()
    pending = ["waiting for a fresh engine snapshot"]
    last_message = None
    print(f"Waiting up to {timeout:g}s for market connectors and strategy data...", flush=True)
    while time.monotonic() < deadline:
        if pid is None or bot.read_pid() != pid or not bot.is_engine_pid(pid):
            print("Bot stopped or changed during startup. Run make status and make logs.", flush=True)
            return 3
        meta = bot.read_meta() or {}
        if meta.get("file") != config:
            print(f"Running config is {meta.get('file')!r}, expected {config!r}.", flush=True)
            return 4
        previous = (bot.read_status() or {}).get("updated_at", 0)
        refresh(timeout=min(5.0, max(0.0, deadline - time.monotonic())))
        snapshot = bot.read_status() or {}
        if (snapshot.get("pid") != pid or not snapshot.get("running")
                or snapshot.get("updated_at", 0) <= previous
                or snapshot.get("updated_at", 0) < meta.get("started_at", 0)):
            pending = ["waiting for a fresh snapshot from the current bot"]
        else:
            pending = pending_dependencies(snapshot)
            if not pending and bot.read_pid() == pid and bot.is_engine_pid(pid):
                connectors = (snapshot.get("engine") or {}).get("connectors") or {}
                pairs = sum(len(c.get("trading_pairs", [])) for c in connectors.values())
                print(f"Ready: {config}; {len(connectors)} connector(s), {pairs} trading pair(s).", flush=True)
                return 0
        message = "; ".join(pending)
        if message != last_message:
            print(f"Initializing: {message}", flush=True)
            last_message = message
        time.sleep(min(2.0, max(0.0, deadline - time.monotonic())))
    print(f"Timed out after {timeout:g}s: {'; '.join(pending)}.\n"
          "The bot may still be running; this check does not stop or restart it.\n"
          "Run make status and make logs for details; retry with make wait-ready.", flush=True)
    return 5


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()
    from hummingbot.cli import bot
    from hummingbot.cli.commands.status import _request_fresh_snapshot

    return wait_for_ready(bot, _request_fresh_snapshot, args.config, args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())

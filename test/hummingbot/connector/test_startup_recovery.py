"""Regression tests for interrupted startup requests; no exchange requests or orders."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import aiohttp

from hummingbot.connector.perpetual_derivative_py_base import PerpetualDerivativePyBase
from hummingbot.core.data_type.order_book_tracker import OrderBookTracker


class StartupRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def connector(self):
        return SimpleNamespace(
            trading_pairs=["BTC-USDT", "ETH-USDT"],
            _orderbook_ds=SimpleNamespace(get_funding_info=AsyncMock()),
            _perpetual_trading=MagicMock(),
            _set_trading_pair_leverage=AsyncMock(),
            _sleep=AsyncMock(),
            logger=MagicMock(),
        )

    async def test_order_book_recovers_without_restarting_completed_pairs(self):
        first_book, second_book = MagicMock(), MagicMock()
        tracker = OrderBookTracker(MagicMock(), ["BTC-USDT", "ETH-USDT"])
        tracker._initial_order_book_for_trading_pair = AsyncMock(
            side_effect=[first_book, aiohttp.ClientConnectionError("TLS reset"), second_book]
        )
        tracker._track_single_book = AsyncMock()

        async def backoff(delay):
            self.assertFalse(tracker.ready)
            if delay == 5:
                self.assertEqual({"BTC-USDT": first_book}, tracker.order_books)
                self.assertEqual(["BTC-USDT"], list(tracker._tracking_tasks))

        tracker._sleep = AsyncMock(side_effect=backoff)
        try:
            await OrderBookTracker._init_order_books(tracker)
            self.assertTrue(tracker.ready)
            self.assertEqual({"BTC-USDT": first_book, "ETH-USDT": second_book}, tracker.order_books)
            self.assertEqual([call("BTC-USDT"), call("ETH-USDT"), call("ETH-USDT")],
                             tracker._initial_order_book_for_trading_pair.await_args_list)
            await asyncio.gather(*tracker._tracking_tasks.values())
            self.assertEqual(2, tracker._track_single_book.await_count)
        finally:
            tracker.stop()

    async def test_order_book_can_be_cancelled_during_retry(self):
        tracker = OrderBookTracker(MagicMock(), ["BTC-USDT"])
        tracker._initial_order_book_for_trading_pair = AsyncMock(side_effect=aiohttp.ClientConnectionError())
        tracker._sleep = AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await OrderBookTracker._init_order_books(tracker)
        self.assertFalse(tracker.ready)
        self.assertEqual({}, tracker._tracking_tasks)

    async def test_funding_recovers_without_reinitializing_completed_pairs(self):
        connector = self.connector()
        first_info, second_info = object(), object()
        connector._orderbook_ds.get_funding_info.side_effect = [
            first_info, aiohttp.ClientConnectionError("TLS reset"), second_info
        ]
        await PerpetualDerivativePyBase._init_funding_info(connector)
        self.assertEqual([call("BTC-USDT"), call("ETH-USDT"), call("ETH-USDT")],
                         connector._orderbook_ds.get_funding_info.await_args_list)
        self.assertEqual([call(first_info), call(second_info)],
                         connector._perpetual_trading.initialize_funding_info.call_args_list)
        connector._sleep.assert_awaited_once_with(5)

    async def test_funding_can_be_cancelled_during_retry(self):
        connector = self.connector()
        connector._orderbook_ds.get_funding_info.side_effect = aiohttp.ClientConnectionError()
        connector._sleep.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await PerpetualDerivativePyBase._init_funding_info(connector)
        connector._perpetual_trading.initialize_funding_info.assert_not_called()

    async def test_leverage_transport_failure_retries_and_confirms_only_on_success(self):
        connector = self.connector()
        connector._set_trading_pair_leverage.side_effect = [aiohttp.ClientConnectionError(), (True, "")]

        async def backoff(delay):
            connector._perpetual_trading.set_leverage.assert_not_called()

        connector._sleep.side_effect = backoff
        await PerpetualDerivativePyBase._execute_set_leverage(connector, "BTC-USDT", 3)
        connector._perpetual_trading.set_leverage.assert_called_once_with("BTC-USDT", 3)
        self.assertEqual(2, connector._set_trading_pair_leverage.await_count)

    async def test_leverage_retry_is_bounded_and_does_not_mark_failure_as_success(self):
        connector = self.connector()
        connector._set_trading_pair_leverage.side_effect = asyncio.TimeoutError()
        await PerpetualDerivativePyBase._execute_set_leverage(connector, "BTC-USDT", 3)
        self.assertEqual(3, connector._set_trading_pair_leverage.await_count)
        connector._perpetual_trading.set_leverage.assert_not_called()
        connector.logger.return_value.network.assert_called_once()

    async def test_leverage_exchange_rejection_is_not_retried(self):
        connector = self.connector()
        connector._set_trading_pair_leverage.return_value = (False, "leverage rejected")
        await PerpetualDerivativePyBase._execute_set_leverage(connector, "BTC-USDT", 3)
        connector._set_trading_pair_leverage.assert_awaited_once()
        connector._sleep.assert_not_awaited()
        connector._perpetual_trading.set_leverage.assert_not_called()

    async def test_leverage_cancellation_is_not_retried(self):
        connector = self.connector()
        connector._set_trading_pair_leverage.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await PerpetualDerivativePyBase._execute_set_leverage(connector, "BTC-USDT", 3)
        connector._sleep.assert_not_awaited()
        connector._perpetual_trading.set_leverage.assert_not_called()

import asyncio
import json
import logging
import math
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ib_insync import IB, Stock, StopOrder, util

ET = ZoneInfo("America/New_York")
log = logging.getLogger(__name__)


def setup_logging():
    fmt = "%(asctime)s %(levelname)-8s %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.FileHandler("bot.log"),
            logging.StreamHandler(),
        ],
    )


def load_config():
    path = Path("config.json")
    if not path.exists():
        raise FileNotFoundError("config.json not found")
    with open(path) as f:
        return json.load(f)


def load_tickers():
    path = Path("tickers.txt")
    if not path.exists():
        raise FileNotFoundError("tickers.txt not found")
    tickers = [line.strip().upper() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not tickers:
        raise ValueError("tickers.txt is empty")
    if len(tickers) > 15:
        log.warning(f"Trimming ticker list from {len(tickers)} to 15")
        tickers = tickers[:15]
    return tickers


def get_account_value(ib: IB, account: str = "") -> float:
    vals = ib.accountValues(account)
    for v in vals:
        if v.tag == "NetLiquidation" and v.currency == "USD":
            return float(v.value)
    for v in vals:
        if v.tag == "NetLiquidation":
            log.warning(f"Using {v.currency} NetLiquidation for position sizing")
            return float(v.value)
    raise RuntimeError("Cannot read account value from IBKR")


def valid_price(p) -> bool:
    return p is not None and not math.isnan(p) and p > 0


class TickerManager:
    def __init__(self, ib: IB, symbol: str, cfg: dict):
        self.ib = ib
        self.symbol = symbol
        self.cfg = cfg
        self.contract = Stock(symbol, "SMART", "USD")

        self.state = "idle"
        self.entry_trade = None
        self.stop_trade = None
        self.current_stop: float = None
        self.shares: int = 0

        self.ticker_obj = None

        # 9:30 candle tracking
        self.c_open: float = None
        self.c_high: float = -float("inf")
        self.c_low: float = float("inf")

        # trailing stop window
        self.trail_tf: int = cfg.get("trailing_timeframe_minutes", 5)
        self.trail_window_low: float = float("inf")
        self.trail_window_start: datetime = None

        self.cancel_handle = None

    async def start(self):
        try:
            await self.ib.qualifyContractsAsync(self.contract)
        except Exception as e:
            log.error(f"[{self.symbol}] Contract qualify failed: {e}")
            self.state = "done"
            return

        self.state = "watching"

        now = datetime.now(ET)
        if self.cfg.get("test_mode"):
            # Test mode: trigger candle tracking 2 minutes from now
            open_time = now + timedelta(minutes=2)
            log.info(f"[{self.symbol}] TEST MODE — candle tracking starts at {open_time.strftime('%H:%M:%S')} ET")
        else:
            open_time = now.replace(hour=9, minute=30, second=0, microsecond=0)

        deadline = open_time + timedelta(minutes=self.cfg["cancel_after_minutes"])

        if not self.cfg.get("test_mode") and now > deadline:
            log.info(f"[{self.symbol}] Past cancel window — skipping today")
            self.state = "done"
            return

        delay = max(0.0, (open_time - now).total_seconds())
        log.info(f"[{self.symbol}] Candle tracking starts in {delay:.0f}s")

        asyncio.get_event_loop().call_later(
            delay,
            lambda: asyncio.ensure_future(self._begin_candle_tracking()),
        )

    async def _begin_candle_tracking(self):
        if self.state != "watching":
            return

        self.c_open = None
        self.c_high = -float("inf")
        self.c_low = float("inf")

        self.ticker_obj = self.ib.reqMktData(self.contract, "", False, False)
        self.ticker_obj.updateEvent += self._on_tick_candle
        log.info(f"[{self.symbol}] Tracking 9:30 candle via live ticks")

        # 9:30 candle closes at 9:31 — wait 61 seconds (1s buffer after close)
        asyncio.get_event_loop().call_later(
            61,
            lambda: asyncio.ensure_future(self._finalize_opening_candle()),
        )

    def _on_tick_candle(self, ticker):
        price = ticker.last
        if not valid_price(price):
            return
        if self.c_open is None:
            self.c_open = price
        if price > self.c_high:
            self.c_high = price
        if price < self.c_low:
            self.c_low = price

    async def _finalize_opening_candle(self):
        if self.state != "watching":
            return

        # Unregister candle tick handler
        if self.ticker_obj:
            self.ticker_obj.updateEvent -= self._on_tick_candle

        if self.c_open is None or self.c_high == -float("inf"):
            log.error(
                f"[{self.symbol}] No tick data received for 9:30 candle. "
                "Make sure your IBKR account has a US market data subscription."
            )
            self._cancel_mktdata()
            self.state = "done"
            return

        candle_high = round(self.c_high, 2)
        candle_low = round(self.c_low, 2)
        log.info(f"[{self.symbol}] 9:30 candle | H={candle_high} L={candle_low}")

        await self._place_entry(candle_high, candle_low)

    async def _place_entry(self, entry_price: float, stop_price: float):
        if entry_price <= stop_price:
            log.warning(f"[{self.symbol}] High ({entry_price}) <= Low ({stop_price}) — skipping")
            self._cancel_mktdata()
            self.state = "done"
            return

        try:
            account_value = get_account_value(self.ib, self.cfg.get("account", ""))
        except RuntimeError as e:
            log.error(f"[{self.symbol}] {e}")
            self._cancel_mktdata()
            self.state = "done"
            return

        risk_amount = account_value * (self.cfg["risk_percent"] / 100)
        risk_per_share = entry_price - stop_price
        self.shares = max(1, math.floor(risk_amount / risk_per_share))
        self.current_stop = stop_price

        log.info(
            f"[{self.symbol}] ENTRY ORDER | stop-buy @ {entry_price} | "
            f"stop-loss @ {stop_price} | shares={self.shares} | "
            f"risk=${risk_amount:.2f} | risk/share=${risk_per_share:.2f}"
        )

        buy_order = StopOrder("BUY", self.shares, entry_price)
        buy_order.tif = "DAY"
        self.entry_trade = self.ib.placeOrder(self.contract, buy_order)
        self.entry_trade.filledEvent += self._on_entry_filled
        self.state = "order_placed"

        cancel_delay = self.cfg["cancel_after_minutes"] * 60
        loop = asyncio.get_event_loop()
        self.cancel_handle = loop.call_later(
            cancel_delay,
            lambda: asyncio.ensure_future(self._cancel_unfilled()),
        )

    async def _cancel_unfilled(self):
        if self.state != "order_placed":
            return
        log.info(f"[{self.symbol}] {self.cfg['cancel_after_minutes']} min elapsed — cancelling unfilled buy")
        self.ib.cancelOrder(self.entry_trade.order)
        self._cancel_mktdata()
        self.state = "done"

    def _on_entry_filled(self, trade):
        if self.cancel_handle:
            self.cancel_handle.cancel()
        fill_px = trade.orderStatus.avgFillPrice
        log.info(f"[{self.symbol}] BUY FILLED @ {fill_px} | placing stop @ {self.current_stop}")
        asyncio.ensure_future(self._activate_stop_and_trail())

    async def _activate_stop_and_trail(self):
        self.state = "in_position"

        stop_order = StopOrder("SELL", self.shares, self.current_stop)
        stop_order.tif = "DAY"
        self.stop_trade = self.ib.placeOrder(self.contract, stop_order)
        self.stop_trade.filledEvent += lambda t: self._on_stop_filled()

        # Reuse existing ticker subscription for trailing stop tracking
        self.trail_window_low = float("inf")
        self.trail_window_start = None

        if self.ticker_obj:
            self.ticker_obj.updateEvent += self._on_tick_trail
        else:
            self.ticker_obj = self.ib.reqMktData(self.contract, "", False, False)
            self.ticker_obj.updateEvent += self._on_tick_trail

        log.info(f"[{self.symbol}] Trailing stop active | tracking {self.trail_tf}-min candle lows")

    def _on_tick_trail(self, ticker):
        if self.state != "in_position":
            return

        price = ticker.last
        if not valid_price(price):
            return

        now = datetime.now(ET)
        candle_minute = (now.minute // self.trail_tf) * self.trail_tf
        candle_start = now.replace(minute=candle_minute, second=0, microsecond=0)

        if self.trail_window_start is None:
            self.trail_window_start = candle_start
            self.trail_window_low = price
            return

        if candle_start > self.trail_window_start:
            # Previous 5-min candle just completed
            completed_low = round(self.trail_window_low, 2)
            if completed_low > self.current_stop:
                log.info(f"[{self.symbol}] TRAIL STOP {self.current_stop} -> {completed_low}")
                self.current_stop = completed_low
                self.stop_trade.order.auxPrice = completed_low
                self.ib.placeOrder(self.contract, self.stop_trade.order)
            # Start new window
            self.trail_window_start = candle_start
            self.trail_window_low = price
        else:
            if price < self.trail_window_low:
                self.trail_window_low = price

    def _on_stop_filled(self):
        log.info(f"[{self.symbol}] STOP FILLED — position closed")
        self._cancel_mktdata()
        self.state = "done"

    def _cancel_mktdata(self):
        if self.ticker_obj:
            try:
                self.ib.cancelMktData(self.contract)
            except Exception:
                pass
            self.ticker_obj = None

    def cleanup(self):
        self._cancel_mktdata()
        if self.cancel_handle:
            self.cancel_handle.cancel()


async def main():
    setup_logging()
    cfg = load_config()
    tickers = load_tickers()

    log.info("=" * 60)
    log.info("IBKR Intraday Bot Starting")
    log.info(f"Tickers       : {tickers}")
    log.info(f"Risk per trade: {cfg['risk_percent']}%")
    log.info(f"Cancel after  : {cfg['cancel_after_minutes']} minutes")
    log.info(f"Trail TF      : {cfg.get('trailing_timeframe_minutes', 5)} minutes")
    log.info(f"Port          : {cfg['port']}")
    log.info("=" * 60)

    ib = IB()
    try:
        await ib.connectAsync(
            cfg.get("host", "127.0.0.1"),
            cfg.get("port", 7497),
            clientId=cfg.get("client_id", 1),
        )
    except Exception as e:
        log.error(f"Connection failed: {e}")
        log.error("Make sure TWS or IB Gateway is running with API enabled.")
        return

    log.info("Connected to IBKR")

    managers = [TickerManager(ib, sym, cfg) for sym in tickers]

    try:
        await asyncio.gather(*[m.start() for m in managers])
        log.info("All tickers active. Press Ctrl+C to stop.")

        while True:
            await asyncio.sleep(30)
            now = datetime.now(ET)
            if now.hour >= 16:
                log.info("4:00 PM ET — shutting down")
                break
            if all(m.state == "done" for m in managers):
                log.info("All tickers resolved — shutting down")
                break

    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Stopped by user")
    finally:
        for m in managers:
            m.cleanup()
        ib.disconnect()
        log.info("Disconnected from IBKR")


if __name__ == "__main__":
    util.run(main())

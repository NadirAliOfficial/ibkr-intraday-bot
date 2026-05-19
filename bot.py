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
        raise FileNotFoundError("config.json not found — check the README")
    with open(path) as f:
        return json.load(f)


def load_tickers():
    path = Path("tickers.txt")
    if not path.exists():
        raise FileNotFoundError("tickers.txt not found — add your tickers before running")
    tickers = [line.strip().upper() for line in path.read_text().splitlines() if line.strip()]
    if not tickers:
        raise ValueError("tickers.txt is empty — add at least one ticker")
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
            log.warning(f"USD NetLiquidation not found — using {v.currency} value for sizing")
            return float(v.value)
    raise RuntimeError("Cannot read account value from IBKR. Check your connection and account.")


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

        self.bars_5m = None
        self.cancel_handle = None
        self.first_candle_handled = False

    async def start(self):
        try:
            await self.ib.qualifyContractsAsync(self.contract)
        except Exception as e:
            log.error(f"[{self.symbol}] Failed to qualify contract: {e}")
            self.state = "done"
            return

        self.state = "watching"

        # Schedule the candle pull for 9:31:10 ET — after the first 1-min bar has closed.
        # Requesting at open causes "HMDS query returned no data" because IBKR has no RTH
        # data yet. Waiting until 9:31:10 guarantees the 9:30 bar exists.
        now = datetime.now(ET)
        target = now.replace(hour=9, minute=31, second=10, microsecond=0)
        delay = max(0.0, (target - now).total_seconds())
        log.info(f"[{self.symbol}] Candle check scheduled in {delay:.0f}s")
        asyncio.get_event_loop().call_later(
            delay,
            lambda: asyncio.ensure_future(self._fetch_opening_candle()),
        )

    def _parse_bar_dt(self, bar) -> datetime:
        dt = bar.date
        if isinstance(dt, str):
            dt = datetime.strptime(dt, "%Y%m%d %H:%M:%S")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ET)
        return dt

    async def _fetch_opening_candle(self):
        if self.first_candle_handled or self.state != "watching":
            return

        bars = []
        for data_type in ("TRADES", "MIDPOINT"):
            try:
                bars = await self.ib.reqHistoricalDataAsync(
                    self.contract,
                    endDateTime="",
                    durationStr="120 S",
                    barSizeSetting="1 min",
                    whatToShow=data_type,
                    useRTH=True,
                    formatDate=1,
                    keepUpToDate=False,
                )
            except Exception as e:
                log.warning(f"[{self.symbol}] {data_type} request error: {e}")
                bars = []
            if bars:
                break

        if not bars:
            log.error(
                f"[{self.symbol}] No market data available — check your IBKR market data "
                "subscription at Account Management > Market Data Subscriptions"
            )
            self.state = "done"
            return

        # Find the 9:30 candle
        opening_candle = None
        for bar in bars:
            bar_dt = self._parse_bar_dt(bar)
            if bar_dt.hour == 9 and bar_dt.minute == 30:
                opening_candle = bar
                break

        if opening_candle is None:
            log.warning(f"[{self.symbol}] 9:30 candle not found in returned data — no trade placed")
            self.state = "done"
            return

        now = datetime.now(ET)
        bar_dt = self._parse_bar_dt(opening_candle)
        deadline = bar_dt + timedelta(minutes=self.cfg["cancel_after_minutes"])
        if now > deadline:
            log.info(f"[{self.symbol}] Past cancellation window — skipping today")
            self.state = "done"
            return

        self.first_candle_handled = True
        log.info(
            f"[{self.symbol}] 9:30 candle | H={opening_candle.high} L={opening_candle.low}"
        )
        await self._place_entry(opening_candle)

    async def _place_entry(self, candle):
        entry_price = round(candle.high, 2)
        stop_price = round(candle.low, 2)

        if entry_price <= stop_price:
            log.warning(
                f"[{self.symbol}] Candle high ({entry_price}) <= low ({stop_price}) — skipping"
            )
            self.state = "done"
            return

        try:
            account_value = get_account_value(self.ib, self.cfg.get("account", ""))
        except RuntimeError as e:
            log.error(f"[{self.symbol}] {e}")
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
        log.info(
            f"[{self.symbol}] {self.cfg['cancel_after_minutes']} min elapsed — cancelling unfilled buy"
        )
        self.ib.cancelOrder(self.entry_trade.order)
        self.state = "done"

    def _on_entry_filled(self, trade):
        if self.cancel_handle:
            self.cancel_handle.cancel()
        fill_px = trade.orderStatus.avgFillPrice
        log.info(
            f"[{self.symbol}] BUY FILLED @ {fill_px} | "
            f"placing stop-loss @ {self.current_stop}"
        )
        asyncio.ensure_future(self._activate_stop_and_trail())

    async def _activate_stop_and_trail(self):
        self.state = "in_position"

        stop_order = StopOrder("SELL", self.shares, self.current_stop)
        stop_order.tif = "DAY"
        self.stop_trade = self.ib.placeOrder(self.contract, stop_order)
        self.stop_trade.filledEvent += lambda t: self._on_stop_filled()

        for data_type in ("TRADES", "MIDPOINT"):
            try:
                self.bars_5m = await self.ib.reqHistoricalDataAsync(
                    self.contract,
                    endDateTime="",
                    durationStr="1 D",
                    barSizeSetting="5 mins",
                    whatToShow=data_type,
                    useRTH=True,
                    formatDate=1,
                    keepUpToDate=True,
                )
            except Exception:
                self.bars_5m = None
            if self.bars_5m is not None:
                break

        if self.bars_5m is None:
            log.error(f"[{self.symbol}] Could not subscribe to 5-min bars — trailing stop inactive")
            return

        self.bars_5m.updateEvent += self._on_5m_update
        log.info(f"[{self.symbol}] Trailing stop active | watching 5-min bars")

    def _on_5m_update(self, bars, has_new_bar: bool):
        if not has_new_bar or self.state != "in_position":
            return
        if len(bars) < 2:
            return

        completed = bars[-2]
        candidate = round(completed.low, 2)

        if candidate > self.current_stop:
            log.info(
                f"[{self.symbol}] TRAIL STOP {self.current_stop} -> {candidate}"
            )
            self.current_stop = candidate
            self.stop_trade.order.auxPrice = candidate
            self.ib.placeOrder(self.contract, self.stop_trade.order)

    def _on_stop_filled(self):
        log.info(f"[{self.symbol}] STOP FILLED — position closed")
        self.state = "done"
        self._stop_5m_feed()

    def _stop_5m_feed(self):
        if self.bars_5m:
            self.ib.cancelHistoricalData(self.bars_5m)
            self.bars_5m = None

    def cleanup(self):
        self._stop_5m_feed()
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
        log.error("Make sure TWS or IB Gateway is running with API enabled on the correct port.")
        return

    log.info("Connected to IBKR")

    managers = [TickerManager(ib, sym, cfg) for sym in tickers]

    try:
        await asyncio.gather(*[m.start() for m in managers])
        log.info("All tickers active. Waiting for market open. Press Ctrl+C to stop.")

        while True:
            await asyncio.sleep(30)
            now = datetime.now(ET)
            if now.hour >= 16:
                log.info("4:00 PM ET reached — shutting down")
                break
            active = [m.symbol for m in managers if m.state not in ("done",)]
            if not active:
                log.info("All positions resolved — shutting down")
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

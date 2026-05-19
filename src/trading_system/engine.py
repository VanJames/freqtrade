from __future__ import annotations

import asyncio
from logging import getLogger

from trading_system.cache import StateCache
from trading_system.config import Settings
from trading_system.execution import ExecutionEngine
from trading_system.exchange import CcxtOkxExchange, DryRunExchange, ExchangeClient
from trading_system.foresight import ForesightProvider
from trading_system.models import HedgeLock, PositionSide, Regime, SignalType, TradeSignal
from trading_system.llm_regime import LLMRegimeReviewer, RegimeReviewInput
from trading_system.portfolio import AlphaFilter, GridPlanner
from trading_system.position_manager import PositionManager
from trading_system.regime import MarketRegimeClassifier
from trading_system.risk import RiskManager
from trading_system.store import StateStore
from trading_system.strategy import StrategyEngine
from trading_system.volatility import build_volatility_policy, needs_llm_review

logger = getLogger(__name__)


class OKXQuantEngine:
    def __init__(
        self,
        settings: Settings,
        exchange: ExchangeClient | None = None,
        store: StateStore | None = None,
        cache: StateCache | None = None,
        foresight: ForesightProvider | None = None,
    ) -> None:
        self.settings = settings
        self.exchange = exchange or (
            DryRunExchange(settings.symbols) if settings.dry_run else CcxtOkxExchange(settings.okx_config())
        )
        self.store = store
        self.cache = cache
        self.foresight = foresight or ForesightProvider()
        self.regime_classifier = MarketRegimeClassifier()
        self.strategy = StrategyEngine(
            min_stop_loss_pct=settings.min_stop_loss_pct,
            high_vol_min_stop_loss_pct=settings.high_vol_min_stop_loss_pct,
            extreme_vol_min_stop_loss_pct=settings.extreme_vol_min_stop_loss_pct,
            max_stop_loss_pct=settings.max_stop_loss_pct,
            shock_reward_risk=settings.shock_reward_risk,
            trend_reward_risk=settings.trend_reward_risk,
            min_take_profit_pct=settings.min_take_profit_pct,
        )
        self.risk = RiskManager(settings)
        self.execution = ExecutionEngine(
            self.exchange,
            twap_callback=store.record_twap_fill if store else None,
        )
        self.alpha_filter = AlphaFilter()
        self.grid_planner = GridPlanner()
        self.position_manager = PositionManager()
        self.llm_reviewer = LLMRegimeReviewer(
            model=settings.llm_regime_model,
            provider=settings.llm_regime_provider,
            base_url=settings.llm_regime_base_url or None,
            api_key_env=settings.llm_regime_api_key_env or None,
            enabled=settings.llm_regime_review_enabled,
            cache_ttl_seconds=settings.llm_regime_review_cache_ttl_seconds,
            min_interval_seconds=settings.llm_regime_review_min_interval_seconds,
        )
        self.hedge_locks: dict[str, HedgeLock] = {}
        self.klines = {
            symbol: {"5m": [], "1h": [], "4h": []}
            for symbol in settings.symbols
        }
        self.running = False

    async def initialize(self, init_store: bool = True) -> None:
        await self.exchange.initialize()
        if self.store and init_store:
            await self.store.initialize()
            await self._restore_latest_snapshot()
        if self.cache:
            await self.cache.ping()
        for symbol in self.settings.symbols:
            await self.exchange.set_leverage(symbol, self.settings.trend_symbol_leverage_limit)
            for timeframe, limit in {"5m": 150, "1h": 120, "4h": 100}.items():
                self.klines[symbol][timeframe] = await self.exchange.fetch_ohlcv(symbol, timeframe, limit)
        equity = await self.exchange.fetch_balance_equity()
        self.risk.update_equity(equity)
        logger.info("engine initialized dry_run=%s symbols=%s", self.settings.dry_run, self.settings.symbols)

    async def run(self) -> None:
        self.running = True
        tasks = [asyncio.create_task(self._symbol_loop(symbol)) for symbol in self.settings.symbols]
        tasks.append(asyncio.create_task(self._monitor_loop()))
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await self.shutdown()

    async def run_once(self) -> None:
        all_signals: list[TradeSignal] = []
        for symbol in self.settings.symbols:
            all_signals.extend(await self._build_symbol_signals(symbol))
        filtered = self.alpha_filter.filter(
            all_signals,
            {symbol: self.klines[symbol]["1h"] for symbol in self.settings.symbols},
        )
        await self._execute_signals(filtered)
        await self._save_snapshot()
        await self._save_cache()

    async def _symbol_loop(self, symbol: str) -> None:
        while self.running and not self.risk.fused():
            try:
                await self._tick_symbol(symbol)
            except Exception:
                logger.exception("symbol loop failed symbol=%s", symbol)
                await asyncio.sleep(5)

    async def _tick_symbol(self, symbol: str) -> None:
        await self._execute_signals(await self._build_symbol_signals(symbol))

    async def _build_symbol_signals(self, symbol: str) -> list[TradeSignal]:
        for timeframe in ("5m", "1h"):
            latest = await self.exchange.watch_ohlcv(symbol, timeframe)
            self._update_cache(symbol, timeframe, latest[0])
        if len(self.klines[symbol]["4h"]) < 100:
            self.klines[symbol]["4h"] = await self.exchange.fetch_ohlcv(symbol, "4h", 100)

        self.risk.inspect_spike(symbol, self.klines[symbol]["5m"])
        previous = self.regime_classifier.regimes.get(symbol)
        regime, features = self.regime_classifier.classify(
            symbol,
            self.klines[symbol]["1h"],
            self.klines[symbol]["4h"],
            self.foresight.get(symbol),
        )
        review_features = {
            "adx": features.adx,
            "plus_di": features.plus_di,
            "minus_di": features.minus_di,
            "atr_1h": features.atr_1h,
            "atr_pct": features.atr_1h / features.close_1h if features.close_1h else 0.0,
            "ema20_1h": features.ema20_1h,
            "ema60_1h": features.ema60_1h,
            "range_high_4h": features.range_high_4h,
            "range_low_4h": features.range_low_4h,
            "range_amplitude_4h": features.range_amplitude_4h,
            "close_1h": features.close_1h,
        }
        volatility_policy = build_volatility_policy(
            price=features.close_1h,
            atr_1h=features.atr_1h,
            base_min_stop_loss_pct=self.settings.min_stop_loss_pct,
            high_vol_min_stop_loss_pct=self.settings.high_vol_min_stop_loss_pct,
            extreme_vol_min_stop_loss_pct=self.settings.extreme_vol_min_stop_loss_pct,
            ret_24h=features.ret_24h,
            range_24h=features.range_24h,
            range_72h=features.range_72h,
        )
        review_features["volatility_tier"] = volatility_policy.tier
        review = (
            await self.llm_reviewer.review(
                RegimeReviewInput(symbol=symbol, rule_regime=regime, features=review_features)
            )
            if needs_llm_review(volatility_policy, regime)
            else self.llm_reviewer.default_review(regime)
        )
        regime = review.proposed_regime
        if not review.allow_trade:
            return []
        positions = await self.exchange.fetch_positions(symbol)
        release_signals = self.strategy.hedge_release_signals(
            symbol,
            self.hedge_locks.get(symbol),
            self.klines[symbol]["5m"],
            positions,
        )
        if release_signals:
            return release_signals
        exit_signals = self.position_manager.exit_signals(
            symbol,
            features.close_1h,
            features.atr_1h or features.atr_4h,
            positions,
        )
        if exit_signals:
            return exit_signals
        signals = self.strategy.build_signals(
            symbol,
            regime,
            previous or regime,
            features,
            self.klines[symbol]["5m"],
            positions,
        )
        if not signals:
            return []
        for signal in signals:
            if signal.signal_type == SignalType.HEDGE_TRANSITION:
                grid_side = PositionSide.SHORT if signal.position_side == PositionSide.LONG else PositionSide.LONG
                self.hedge_locks[symbol] = HedgeLock(
                    symbol=symbol,
                    grid_side=grid_side,
                    hedge_side=signal.position_side,
                    contracts=float(signal.metadata["hedge_contracts"]),
                )
        return signals

    async def _execute_signals(self, signals: list[TradeSignal]) -> None:
        if not signals:
            return
        equity = await self.exchange.fetch_balance_equity()
        if self.risk.update_equity(equity):
            logger.critical("risk fuse active; skipping orders")
            await self.exchange.close_all_positions()
            return

        for signal in signals:
            funding = await self.exchange.fetch_funding_rate(signal.symbol)
            decision = self.risk.assess(signal, equity, funding)
            if not decision.allowed:
                logger.warning("signal rejected symbol=%s reason=%s", signal.symbol, decision.reason)
                continue
            if signal.signal_type == SignalType.ENTER_GRID:
                orders = []
                atr_value = abs(signal.price - signal.stop_loss) / 1.5
                for price, amount in self.grid_planner.orders_for_signal(signal, decision.size, atr_value):
                    orders.append(await self.execution.execute_limit(signal, amount, price))
                order = orders[0]
            else:
                order = await self.execution.execute(signal, decision)
            if signal.signal_type == SignalType.RELEASE_HEDGE:
                lock = self.hedge_locks.get(signal.symbol)
                if lock:
                    lock.active = False
            if signal.signal_type in {SignalType.ENTER_TREND, SignalType.ENTER_GRID}:
                self.position_manager.register_entry(signal, abs(signal.price - signal.stop_loss))
            if signal.signal_type in {SignalType.ENTER_TREND, SignalType.ENTER_GRID}:
                self.risk.reserve_risk(
                    signal.position_side,
                    self.risk.signal_risk_multiplier(signal),
                )
            if self.store:
                await self.store.record_order(order, signal.regime)

    def _update_cache(self, symbol: str, timeframe: str, row: list[float]) -> None:
        cache = self.klines[symbol][timeframe]
        if cache and int(cache[-1][0]) == int(row[0]):
            cache[-1] = row
        else:
            cache.append(row)
        if len(cache) > 200:
            del cache[:-200]

    async def _monitor_loop(self) -> None:
        while self.running and not self.risk.fused():
            try:
                equity = await self.exchange.fetch_balance_equity()
                if self.risk.update_equity(equity):
                    await self.exchange.close_all_positions()
                await self._save_snapshot()
                await self._save_cache()
                await asyncio.sleep(60)
            except Exception:
                logger.exception("monitor loop failed")
                await asyncio.sleep(5)

    async def _save_snapshot(self) -> None:
        if not self.store:
            return
        equity = await self.exchange.fetch_balance_equity()
        memory = {
            "regimes": {symbol: regime.value for symbol, regime in self.regime_classifier.regimes.items()},
            "direction_risk": {side.value: value for side, value in self.risk.direction_risk.items()},
            "symbol_locks": {symbol: until.isoformat() for symbol, until in self.risk.symbol_locks.items()},
            "hedge_locks": {
                symbol: {
                    "grid_side": lock.grid_side.value,
                    "hedge_side": lock.hedge_side.value,
                    "contracts": lock.contracts,
                    "created_at": lock.created_at.isoformat(),
                    "active": lock.active,
                }
                for symbol, lock in self.hedge_locks.items()
            },
        }
        active_hedging = any(
            task for task in self.execution.background_tasks if not task.done()
        )
        await self.store.save_snapshot(equity, active_hedging, memory)

    async def _restore_latest_snapshot(self) -> None:
        if not self.store:
            return
        snapshot = await self.store.load_latest_snapshot()
        if not snapshot:
            return
        for symbol, value in snapshot.get("regimes", {}).items():
            self.regime_classifier.regimes[symbol] = Regime(value)
        for side, value in snapshot.get("direction_risk", {}).items():
            self.risk.direction_risk[PositionSide(side)] = float(value)
        for symbol, raw in snapshot.get("hedge_locks", {}).items():
            self.hedge_locks[symbol] = HedgeLock(
                symbol=symbol,
                grid_side=PositionSide(raw["grid_side"]),
                hedge_side=PositionSide(raw["hedge_side"]),
                contracts=float(raw["contracts"]),
                active=bool(raw.get("active", True)),
            )
        logger.info("restored latest account snapshot")

    async def _save_cache(self) -> None:
        if not self.cache:
            return
        await self.cache.set_positions(await self.exchange.fetch_positions())
        await self.cache.set_hedge_locks(self.hedge_locks)

    async def shutdown(self) -> None:
        self.running = False
        await self.execution.drain()
        if self.store:
            await self.store.close()
        if self.cache:
            await self.cache.close()
        await self.exchange.close()

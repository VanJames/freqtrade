from __future__ import annotations

import asyncio
from collections.abc import Hashable
from datetime import datetime, timezone
from logging import getLogger
import time
from typing import Any

import pandas as pd

from trading_system.cache import StateCache
from trading_system.config import Settings
from trading_system.email_notification import OrderEmailNotifier, SmtpConfig
from trading_system.execution import ExecutionEngine
from trading_system.exchange import CcxtOkxExchange, DryRunExchange, ExchangeClient
from trading_system.foresight import ForesightProvider
from trading_system.indicators import macd, ohlcv_frame
from trading_system.hotcoin import HotcoinExchange
from trading_system.liquidation_plugin import LiquidationDirectionPlugin
from trading_system.models import (
    HedgeLock,
    OrderResult,
    PositionSide,
    Regime,
    Side,
    SignalType,
    TradeSignal,
)
from trading_system.llm_regime import LLMRegimeReviewer, RegimeReviewInput
from trading_system.portfolio import AlphaFilter, GridPlanner
from trading_system.position_manager import PositionManager
from trading_system.regime import MarketRegimeClassifier
from trading_system.risk import RiskManager
from trading_system.runtime_config import apply_runtime_config
from trading_system.store import StateStore
from trading_system.strategy import StrategyEngine
from trading_system.volatility import build_volatility_policy, needs_llm_review

logger = getLogger(__name__)
ENGINE_DIAGNOSTICS_VERSION = "live-diagnostics-20260520"
TRANSIENT_ENTRY_SUMMARIES = {
    "waiting_live_check",
    "waiting_market_data",
    "waiting_positions",
    "waiting_llm_review",
}


def build_exchange(settings: Settings) -> ExchangeClient:
    if settings.dry_run:
        return DryRunExchange(settings.symbols)
    exchange_id = settings.exchange_id.lower().strip()
    if exchange_id == "hotcoin":
        return HotcoinExchange(settings)
    if exchange_id != "okx":
        raise ValueError(
            f"unsupported exchange_id={settings.exchange_id!r}; expected okx or hotcoin"
        )
    return CcxtOkxExchange(
        settings.okx_config(),
        positions_cache_ttl_seconds=settings.positions_cache_ttl_seconds,
    )


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
        self.exchange = exchange or build_exchange(settings)
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
            trailing_gap_pct=settings.trailing_gap_pct,
            min_trailing_activate_r=settings.min_trailing_activate_r,
            enable_trend_short=settings.enable_trend_short,
            trend_long_risk_multiplier=settings.trend_long_risk_multiplier,
            trend_short_risk_multiplier=settings.trend_short_risk_multiplier,
            defensive_risk_multiplier=settings.defensive_risk_multiplier,
            shock_trend_risk_multiplier=settings.shock_trend_risk_multiplier,
            shock_trend_down_risk_multiplier=settings.shock_trend_down_risk_multiplier,
            enable_shock_trend_scout=settings.enable_shock_trend_scout,
            shock_trend_scout_risk_multiplier=settings.shock_trend_scout_risk_multiplier,
            enable_liquidity_sweep_reversal=settings.enable_liquidity_sweep_reversal,
            liquidity_sweep_risk_multiplier=settings.liquidity_sweep_risk_multiplier,
            liquidity_sweep_require_confirmation=settings.liquidity_sweep_require_confirmation,
            enable_two_candle_momentum=settings.enable_two_candle_momentum,
            enable_daily_macd_breakout=settings.enable_daily_macd_breakout,
        )
        self.risk = RiskManager(settings)
        self.email_notifier = OrderEmailNotifier(SmtpConfig.from_settings(settings))
        self.execution = ExecutionEngine(
            self.exchange,
            twap_callback=store.record_twap_fill if store else None,
            email_notifier=self.email_notifier,
        )
        self.alpha_filter = AlphaFilter()
        self.grid_planner = GridPlanner()
        self.position_manager = PositionManager(
            min_stop_loss_pct=settings.min_stop_loss_pct,
            max_stop_loss_pct=settings.max_stop_loss_pct,
            trailing_gap_pct=settings.trailing_gap_pct,
            min_trailing_activate_r=settings.min_trailing_activate_r,
            recovered_risk_multiplier=settings.defensive_risk_multiplier,
        )
        self.llm_reviewer = LLMRegimeReviewer(
            model=settings.llm_regime_model,
            provider=settings.llm_regime_provider,
            base_url=settings.llm_regime_base_url or None,
            api_key=settings.llm_regime_api_key or None,
            api_key_env=settings.llm_regime_api_key_env or None,
            enabled=settings.llm_regime_review_enabled,
            cache_ttl_seconds=settings.llm_regime_review_cache_ttl_seconds,
            min_interval_seconds=settings.llm_regime_review_min_interval_seconds,
        )
        self.liquidation_plugin = LiquidationDirectionPlugin(settings)
        self.hedge_locks: dict[str, HedgeLock] = {}
        self.klines = {
            symbol: {"5m": [], "1h": [], "4h": [], "1d": []}
            for symbol in settings.symbols
        }
        self._applied_leverage_limit = settings.trend_symbol_leverage_limit
        self.symbol_status: dict[str, dict[str, object]] = {}
        self._diagnostic_log_state: dict[tuple[str, str], tuple[str, float]] = {}
        self._exit_lock = asyncio.Lock()
        self._pending_exit_keys: set[tuple[str, PositionSide]] = set()
        self._order_guard_lock = asyncio.Lock()
        self._pending_entry_keys: set[tuple[str, PositionSide]] = set()
        self._entry_order_cooldowns: dict[tuple[str, PositionSide], float] = {}
        self._exit_order_cooldowns: dict[tuple[str, PositionSide], float] = {}
        self._risk_rejection_email_cooldowns: dict[
            tuple[str, PositionSide, str], float
        ] = {}
        self._last_entry_submitted_at: datetime | None = None
        self._last_order_store_checked_at = 0.0
        self._adaptive_log_state: dict[str, str] = {}
        self.position_monitor_status: dict[str, object] = {
            "interval_seconds": settings.position_monitor_interval_seconds,
            "last_checked_at": None,
            "last_symbol": None,
            "last_price": None,
            "checks": 0,
            "exit_signals": 0,
        }
        self.running = False

    async def initialize(self, init_store: bool = True) -> None:
        try:
            await self.exchange.initialize()
            if self.store and init_store:
                await self.store.initialize()
                await self._apply_runtime_settings(update_exchange_leverage=False)
                await self._restore_latest_snapshot()
                await self._reconcile_direction_risk_with_live_positions()
            if self.cache:
                await self.cache.ping()
            for symbol in self.settings.symbols:
                await self.exchange.set_leverage(
                    symbol, self.settings.trend_symbol_leverage_limit
                )
                for timeframe, limit in {
                    "5m": 300,
                    "1h": 120,
                    "4h": 100,
                    "1d": 80,
                }.items():
                    self.klines[symbol][timeframe] = await self.exchange.fetch_ohlcv(
                        symbol, timeframe, limit
                    )
                latest_price = (
                    float(self.klines[symbol]["5m"][-1][4])
                    if self.klines[symbol]["5m"]
                    else 0.0
                )
                self._set_entry_status(
                    symbol,
                    price=latest_price,
                    regime=Regime.UNKNOWN,
                    action="engine_started",
                    summary="waiting_live_check",
                    blockers=[
                        {
                            "code": "live_symbol_check",
                            "passed": False,
                            "value": "等待下一轮实盘检查",
                        }
                    ],
                )
            equity = await self.exchange.fetch_balance_equity()
            self.risk.update_equity(equity)
            logger.info(
                "engine initialized dry_run=%s symbols=%s diagnostics_version=%s",
                self.settings.dry_run,
                self.settings.symbols,
                ENGINE_DIAGNOSTICS_VERSION,
            )
        except Exception:
            logger.exception("engine initialization failed")
            await self.shutdown()
            raise

    async def run(self) -> None:
        self.running = True
        tasks = [
            asyncio.create_task(self._symbol_loop(symbol))
            for symbol in self.settings.symbols
        ]
        tasks.append(asyncio.create_task(self._position_monitor_loop()))
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
            except Exception as exc:
                self._set_entry_status(
                    symbol,
                    price=self._latest_price(symbol),
                    regime=self.regime_classifier.regimes.get(symbol, Regime.UNKNOWN),
                    action="symbol_loop_error",
                    summary="symbol_loop_error",
                    blockers=[
                        {
                            "code": "symbol_loop_error",
                            "passed": False,
                            "value": f"{type(exc).__name__}: {exc}",
                        }
                    ],
                )
                logger.exception("symbol loop failed symbol=%s", symbol)
                await asyncio.sleep(5)

    async def _tick_symbol(self, symbol: str) -> None:
        await self._execute_signals(await self._build_symbol_signals(symbol))

    async def _build_symbol_signals(self, symbol: str) -> list[TradeSignal]:
        self._set_entry_status(
            symbol,
            price=self._latest_price(symbol),
            regime=self.regime_classifier.regimes.get(symbol, Regime.UNKNOWN),
            action="live_5m_wait",
            summary="waiting_market_data",
            blockers=[{"code": "live_5m_ohlcv", "passed": False}],
        )
        latest = await self._await_exchange_step(
            symbol,
            "live_5m_ohlcv",
            self.exchange.watch_ohlcv(symbol, "5m"),
            self.settings.live_ohlcv_timeout_seconds,
        )
        if not latest:
            self._set_entry_status(
                symbol,
                price=self._latest_price(symbol),
                regime=self.regime_classifier.regimes.get(symbol, Regime.UNKNOWN),
                action="live_5m_wait",
                summary="waiting_market_data",
                blockers=[{"code": "live_5m_ohlcv", "passed": False, "value": "empty"}],
            )
            return []
        self._update_cache(symbol, "5m", latest[0])
        self._set_entry_status(
            symbol,
            price=self._latest_price(symbol),
            regime=self.regime_classifier.regimes.get(symbol, Regime.UNKNOWN),
            action="refresh_higher_timeframes",
            summary="waiting_market_data",
            blockers=[{"code": "snapshot_1h_ohlcv", "passed": False}],
        )
        snapshot_1h = await self._await_exchange_step(
            symbol,
            "snapshot_1h_ohlcv",
            self.exchange.fetch_ohlcv(symbol, "1h", 120),
            self.settings.exchange_request_timeout_seconds,
        )
        self._replace_timeframe_cache(symbol, "1h", snapshot_1h)
        if len(self.klines[symbol]["4h"]) < 100 or self._timeframe_stale(
            symbol, "4h", 14_400_000
        ):
            snapshot_4h = await self._await_exchange_step(
                symbol,
                "snapshot_4h_ohlcv",
                self.exchange.fetch_ohlcv(symbol, "4h", 100),
                self.settings.exchange_request_timeout_seconds,
            )
            self._replace_timeframe_cache(symbol, "4h", snapshot_4h)
        if len(self.klines[symbol]["1d"]) < 35 or self._timeframe_stale(
            symbol, "1d", 86_400_000
        ):
            snapshot_1d = await self._await_exchange_step(
                symbol,
                "snapshot_1d_ohlcv",
                self.exchange.fetch_ohlcv(symbol, "1d", 80),
                self.settings.exchange_request_timeout_seconds,
            )
            self._replace_timeframe_cache(symbol, "1d", snapshot_1d)
        self._sync_higher_timeframes_from_5m(symbol)

        self.risk.inspect_spike(symbol, self.klines[symbol]["5m"])
        previous = self.regime_classifier.regimes.get(symbol)
        regime, features = self.regime_classifier.classify(
            symbol,
            self.klines[symbol]["1h"],
            self.klines[symbol]["4h"],
            self.klines[symbol]["5m"],
            self.foresight.get(symbol),
        )
        review_features = {
            "adx": features.adx,
            "plus_di": features.plus_di,
            "minus_di": features.minus_di,
            "atr_1h": features.atr_1h,
            "atr_pct": features.atr_1h / features.close_1h
            if features.close_1h
            else 0.0,
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
        adaptive_no_trade_hours = (
            await self._hours_since_latest_entry()
            if self.settings.enable_adaptive_strategy_switch
            else 0.0
        )
        adaptive_review_required = (
            self.settings.enable_adaptive_strategy_switch
            and adaptive_no_trade_hours >= float(self.settings.adaptive_no_trade_hours)
            and features.range_24h >= float(self.settings.adaptive_min_range_24h_pct)
        )
        review_features["adaptive_no_trade_hours"] = round(adaptive_no_trade_hours, 2)
        review_features["adaptive_review_required"] = adaptive_review_required
        review_features["range_24h"] = features.range_24h
        review_features["ret_24h"] = features.ret_24h
        review_features["ret_72h"] = features.ret_72h
        llm_review_required = (
            needs_llm_review(volatility_policy, regime) or adaptive_review_required
        )
        if llm_review_required:
            self._set_entry_status(
                symbol,
                price=self._latest_price(symbol),
                regime=regime,
                action="llm_review",
                summary="waiting_llm_review",
                blockers=[{"code": "llm_review_pending", "passed": False}],
            )
            logger.debug(
                "llm review requested symbol=%s regime=%s volatility_tier=%s adaptive=%s",
                symbol,
                regime.value,
                volatility_policy.tier,
                adaptive_review_required,
            )
        review = (
            await self.llm_reviewer.review(
                RegimeReviewInput(
                    symbol=symbol, rule_regime=regime, features=review_features
                )
            )
            if llm_review_required
            else self.llm_reviewer.default_review(regime)
        )
        if llm_review_required:
            self._log_llm_review(symbol, review)
        regime = review.proposed_regime
        latest_price = (
            float(self.klines[symbol]["5m"][-1][4])
            if self.klines[symbol]["5m"]
            else features.close_1h
        )
        status = self.symbol_status.setdefault(symbol, {})
        status.update(
            {
                "price": latest_price,
                "regime": regime.value,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "llm_review": {
                    "action": review.action,
                    "allow_trade": review.allow_trade,
                    "confidence": review.confidence,
                    "reasons": review.reasons,
                    "missing_evidence": review.missing_evidence,
                },
            }
        )
        if not review.allow_trade:
            diagnostics = {
                "action": "llm_review",
                "summary": "llm_rejected",
                "requirements": [
                    {
                        "code": "llm_review_allow_trade",
                        "passed": False,
                        "value": ",".join(review.reasons) or review.action,
                    }
                ],
                "blockers": [
                    {
                        "code": "llm_review_allow_trade",
                        "passed": False,
                        "value": ",".join(review.reasons) or review.action,
                    }
                ],
                "metrics": {
                    "regime": regime.value,
                    "price": latest_price,
                    "llm_confidence": review.confidence,
                },
            }
            self._set_completed_entry_diagnostics(
                symbol, diagnostics, price=latest_price, regime=regime
            )
            self._log_entry_diagnostics(symbol, diagnostics)
            return []
        self._set_entry_status(
            symbol,
            price=latest_price,
            regime=regime,
            action="fetch_positions",
            summary="waiting_positions",
            blockers=[{"code": "fetch_positions", "passed": False}],
        )
        positions = await self._await_exchange_step(
            symbol,
            "fetch_positions",
            self.exchange.fetch_positions(symbol, refresh=True),
            self.settings.exchange_request_timeout_seconds,
        )
        recovered_positions = self.position_manager.recover_missing_states(
            symbol,
            positions,
            price=latest_price,
            atr_value=features.atr_1h or features.atr_4h,
            regime=regime,
            signal_hints=self._position_signal_hints(),
        )
        if recovered_positions:
            self.symbol_status[symbol]["recovered_positions"] = recovered_positions
            for item in recovered_positions:
                logger.warning(
                    "recovered live position symbol=%s side=%s contracts=%.8f entry=%.8f "
                    "protective_stop=%.8f regime=%s",
                    item["symbol"],
                    item["side"],
                    item["contracts"],
                    item["entry_price"],
                    item["stop_loss"],
                    item["regime"],
                )
        diagnostics = self.strategy.entry_diagnostics(
            symbol,
            regime,
            features,
            self.klines[symbol]["5m"],
            positions,
            self.klines[symbol]["1h"],
            self.klines[symbol]["4h"],
            self._daily_candles_with_live(symbol),
        )
        self._set_completed_entry_diagnostics(
            symbol, diagnostics, price=latest_price, regime=regime
        )
        self._log_entry_diagnostics(symbol, diagnostics)
        release_signals = self.strategy.hedge_release_signals(
            symbol,
            self.hedge_locks.get(symbol),
            self.klines[symbol]["5m"],
            positions,
        )
        if release_signals:
            self.symbol_status[symbol]["last_signal"] = self._signal_status(
                release_signals[0]
            )
            diagnostics = {
                **diagnostics,
                "summary": "release_hedge_signal_ready",
                "signal_reason": release_signals[0].reason,
            }
            self._set_completed_entry_diagnostics(
                symbol, diagnostics, price=latest_price, regime=regime
            )
            return release_signals
        daily_macd_exit_signals = self._daily_macd_momentum_exit_signals(
            symbol, latest_price, positions
        )
        if daily_macd_exit_signals:
            self.symbol_status[symbol]["last_signal"] = self._signal_status(
                daily_macd_exit_signals[0]
            )
            diagnostics = {
                **diagnostics,
                "summary": "daily_macd_momentum_exit_ready",
                "signal_reason": daily_macd_exit_signals[0].reason,
            }
            self._set_completed_entry_diagnostics(
                symbol, diagnostics, price=latest_price, regime=regime
            )
            return daily_macd_exit_signals
        exit_signals = self.position_manager.exit_signals(
            symbol,
            latest_price,
            features.atr_1h or features.atr_4h,
            positions,
        )
        if exit_signals:
            self.symbol_status[symbol]["last_signal"] = self._signal_status(
                exit_signals[0]
            )
            diagnostics = {
                **diagnostics,
                "summary": "exit_signal_ready",
                "signal_reason": exit_signals[0].reason,
            }
            self._set_completed_entry_diagnostics(
                symbol, diagnostics, price=latest_price, regime=regime
            )
            return exit_signals
        strategy_context = await self._adaptive_strategy_context(symbol, regime, features)
        strategy_context.update(
            await self._liquidation_plugin_context(symbol, latest_price)
        )
        signals = self.strategy.build_signals(
            symbol,
            regime,
            previous or regime,
            features,
            self.klines[symbol]["5m"],
            positions,
            self.klines[symbol]["1h"],
            self.klines[symbol]["4h"],
            self._daily_candles_with_live(symbol),
            context=strategy_context,
        )
        if not signals:
            return []
        self.symbol_status[symbol]["last_signal"] = self._signal_status(signals[0])
        diagnostics = {
            **diagnostics,
            "summary": "signal_ready",
            "signal_reason": signals[0].reason,
        }
        self._set_completed_entry_diagnostics(
            symbol, diagnostics, price=latest_price, regime=regime
        )
        for signal in signals:
            if signal.signal_type == SignalType.HEDGE_TRANSITION:
                grid_side = (
                    PositionSide.SHORT
                    if signal.position_side == PositionSide.LONG
                    else PositionSide.LONG
                )
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
        await self._apply_runtime_settings()
        equity = await self.exchange.fetch_balance_equity()
        if self.risk.update_equity(equity):
            logger.critical("risk fuse active; skipping orders")
            await self.exchange.close_all_positions()
            return

        for signal in signals:
            exit_key: tuple[str, PositionSide] | None = None
            entry_key: tuple[str, PositionSide] | None = None
            order_attempted = False
            cooldown_needed = False
            signal_is_entry = signal.signal_type in {
                SignalType.ENTER_TREND,
                SignalType.ENTER_GRID,
            }
            if signal.signal_type in {SignalType.EXIT, SignalType.RELEASE_HEDGE}:
                exit_key = (signal.symbol, signal.position_side)
                async with self._exit_lock:
                    if exit_key in self._pending_exit_keys or self._cooldown_active(
                        self._exit_order_cooldowns,
                        exit_key,
                    ):
                        logger.info(
                            "duplicate exit skipped symbol=%s side=%s reason=%s",
                            signal.symbol,
                            signal.position_side.value,
                            signal.reason,
                        )
                        continue
                    self._pending_exit_keys.add(exit_key)
            if signal_is_entry:
                entry_key = (signal.symbol, signal.position_side)
                async with self._order_guard_lock:
                    if entry_key in self._pending_entry_keys or self._cooldown_active(
                        self._entry_order_cooldowns,
                        entry_key,
                    ):
                        logger.info(
                            "duplicate entry skipped symbol=%s side=%s reason=%s",
                            signal.symbol,
                            signal.position_side.value,
                            signal.reason,
                        )
                        continue
                    self._pending_entry_keys.add(entry_key)
            try:
                funding = await self.exchange.fetch_funding_rate(signal.symbol)
                decision = self.risk.assess(signal, equity, funding)
                if not decision.allowed:
                    if signal_is_entry:
                        cooldown_needed = True
                        email_key = (
                            signal.symbol,
                            signal.position_side,
                            decision.reason,
                        )
                        if not self._cooldown_active(
                            self._risk_rejection_email_cooldowns, email_key
                        ):
                            await self.email_notifier.notify_order_signal(
                                signal,
                                order_price=signal.price,
                                amount=decision.size,
                                status="风控拒单",
                                note=f"未真实下单: {decision.reason}; account_equity={equity:.4f}",
                            )
                            self._start_cooldown(
                                self._risk_rejection_email_cooldowns,
                                email_key,
                                self.settings.risk_rejection_email_cooldown_seconds,
                            )
                        else:
                            logger.info(
                                "risk rejection email suppressed symbol=%s side=%s reason=%s",
                                signal.symbol,
                                signal.position_side.value,
                                decision.reason,
                            )
                    logger.warning(
                        "signal rejected symbol=%s regime=%s side=%s reason=%s signal_reason=%s "
                        "price=%.8f stop=%.8f take_profit=%s funding=%.6f",
                        signal.symbol,
                        signal.regime.value,
                        signal.position_side.value,
                        decision.reason,
                        signal.reason,
                        signal.price,
                        signal.stop_loss,
                        signal.take_profit,
                        funding,
                    )
                    status = self.symbol_status.setdefault(signal.symbol, {})
                    status["last_risk_rejection"] = {
                        "reason": decision.reason,
                        "checked_at": datetime.now(timezone.utc).isoformat(),
                        "signal_reason": signal.reason,
                    }
                    previous_diagnostics = status.get("entry_diagnostics")
                    if isinstance(previous_diagnostics, dict):
                        blocker = {"code": f"risk_{decision.reason}", "passed": False}
                        requirements = [
                            *previous_diagnostics.get("requirements", []),
                            blocker,
                        ]
                        blockers = [*previous_diagnostics.get("blockers", []), blocker]
                        diagnostics = {
                            **previous_diagnostics,
                            "summary": "risk_rejected",
                            "requirements": requirements,
                            "blockers": blockers,
                            "risk_rejection": decision.reason,
                        }
                        status["entry_diagnostics"] = diagnostics
                        status["last_completed_entry_diagnostics"] = diagnostics
                    continue
                if signal_is_entry and await self._has_live_position(signal):
                    logger.info(
                        "entry skipped because live position already exists symbol=%s side=%s reason=%s",
                        signal.symbol,
                        signal.position_side.value,
                        signal.reason,
                    )
                    continue
                if signal.signal_type == SignalType.ENTER_GRID:
                    orders = []
                    atr_value = abs(signal.price - signal.stop_loss) / 1.5
                    for price, amount in self.grid_planner.orders_for_signal(
                        signal, decision.size, atr_value
                    ):
                        order_attempted = True
                        orders.append(
                            await self.execution.execute_limit(signal, amount, price)
                        )
                    order = orders[0]
                else:
                    order_attempted = True
                    order = await self.execution.execute(signal, decision)
                if signal.signal_type in {
                    SignalType.ENTER_TREND,
                    SignalType.ENTER_GRID,
                }:
                    self._last_entry_submitted_at = datetime.now(timezone.utc)
                    self.position_manager.register_entry(
                        signal, abs(signal.price - signal.stop_loss)
                    )
                if signal.signal_type in {
                    SignalType.ENTER_TREND,
                    SignalType.ENTER_GRID,
                }:
                    self.risk.reserve_risk(
                        signal.position_side,
                        self.risk.signal_risk_multiplier(signal),
                    )
                synced_order = await self._sync_submitted_order(order, signal)
                if synced_order:
                    order = synced_order
                if (
                    signal.signal_type == SignalType.RELEASE_HEDGE
                    and self._order_has_fill(order)
                ):
                    lock = self.hedge_locks.get(signal.symbol)
                    if lock:
                        lock.active = False
                if signal.signal_type in {SignalType.EXIT, SignalType.RELEASE_HEDGE}:
                    if self._order_has_fill(order):
                        self.risk.release_risk(
                            signal.position_side,
                            float(signal.metadata.get("risk_multiplier", 1.0)),
                        )
                        self.position_manager.trailing.pop(
                            (signal.symbol, signal.position_side), None
                        )
                    else:
                        logger.warning(
                            "exit order not filled; keeping trailing state symbol=%s order_id=%s status=%s reason=%s",
                            signal.symbol,
                            order.id,
                            order.status,
                            signal.reason,
                        )
                status = self.symbol_status.setdefault(signal.symbol, {})
                status["last_order"] = {
                    "id": order.id,
                    "status": order.status,
                    "side": order.side.value,
                    "position_side": order.position_side.value,
                    "amount": order.amount,
                    "price": order.price,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "signal_reason": signal.reason,
                }
                logger.info(
                    "order submitted symbol=%s order_id=%s status=%s regime=%s side=%s amount=%.8f "
                    "price=%.8f signal_reason=%s",
                    signal.symbol,
                    order.id,
                    order.status,
                    signal.regime.value,
                    signal.position_side.value,
                    order.amount,
                    order.price,
                    signal.reason,
                )
                previous_diagnostics = status.get("entry_diagnostics")
                if isinstance(previous_diagnostics, dict):
                    diagnostics = {
                        **previous_diagnostics,
                        "summary": "order_submitted",
                        "order_id": order.id,
                        "signal_reason": signal.reason,
                    }
                    status["entry_diagnostics"] = diagnostics
                    status["last_completed_entry_diagnostics"] = diagnostics
                if self.store:
                    await self.store.record_order(order, signal.regime, signal)
            finally:
                if exit_key is not None:
                    async with self._exit_lock:
                        if order_attempted:
                            self._start_cooldown(
                                self._exit_order_cooldowns,
                                exit_key,
                                self.settings.live_exit_order_cooldown_seconds,
                            )
                        self._pending_exit_keys.discard(exit_key)
                if entry_key is not None:
                    async with self._order_guard_lock:
                        if order_attempted or cooldown_needed:
                            self._start_cooldown(
                                self._entry_order_cooldowns,
                                entry_key,
                                self.settings.live_entry_order_cooldown_seconds,
                            )
                        self._pending_entry_keys.discard(entry_key)

    async def _has_live_position(self, signal: TradeSignal) -> bool:
        try:
            positions = await self._await_exchange_step(
                signal.symbol,
                "pre_order_fetch_positions",
                self.exchange.fetch_positions(signal.symbol, refresh=True),
                self.settings.exchange_request_timeout_seconds,
            )
        except Exception:
            logger.debug(
                "pre-order live position check failed symbol=%s side=%s",
                signal.symbol,
                signal.position_side.value,
                exc_info=True,
            )
            return False
        return any(
            position.side == signal.position_side and abs(position.contracts) > 0
            for position in positions
        )

    async def _adaptive_strategy_context(
        self,
        symbol: str,
        regime: Regime,
        features,
    ) -> dict[str, object]:
        if not self.settings.enable_adaptive_strategy_switch:
            return {}
        no_trade_hours = await self._hours_since_latest_entry()
        range_24h = float(getattr(features, "range_24h", 0.0) or 0.0)
        ret_24h = float(getattr(features, "ret_24h", 0.0) or 0.0)
        enough_inactivity = no_trade_hours >= float(
            self.settings.adaptive_no_trade_hours
        )
        enough_motion = range_24h >= float(self.settings.adaptive_min_range_24h_pct)
        continuation_short = (
            enough_inactivity
            and enough_motion
            and regime in {Regime.SHOCK_TREND_DOWN, Regime.TREND_SHORT}
            and ret_24h < 0
        )
        context = {
            "adaptive_strategy_enabled": enough_inactivity and enough_motion,
            "adaptive_continuation_short": continuation_short,
            "adaptive_no_trade_hours": round(no_trade_hours, 2),
            "adaptive_range_24h": round(range_24h, 6),
            "adaptive_reason": (
                "no_trade_with_downside_volatility"
                if continuation_short
                else "inactive_but_no_strategy_match"
                if enough_inactivity
                else "core_strategy_active"
            ),
        }
        status = self.symbol_status.setdefault(symbol, {})
        status["adaptive_strategy"] = context
        state_key = f"{context['adaptive_reason']}:{continuation_short}"
        if self._adaptive_log_state.get(symbol) != state_key:
            self._adaptive_log_state[symbol] = state_key
            logger.info(
                "adaptive strategy review symbol=%s regime=%s no_trade_hours=%.2f range_24h=%.4f "
                "ret_24h=%.4f continuation_short=%s reason=%s",
                symbol,
                regime.value,
                no_trade_hours,
                range_24h,
                ret_24h,
                continuation_short,
                context["adaptive_reason"],
            )
        return context

    async def _liquidation_plugin_context(
        self, symbol: str, latest_price: float
    ) -> dict[str, object]:
        if not self.settings.enable_liquidation_plugin:
            return {}
        try:
            result = await self.liquidation_plugin.evaluate(
                symbol,
                current_price=latest_price,
                candles_15m=self._resample_5m_cache(symbol, "15m", limit=120),
                candles_1h=self.klines[symbol]["1h"],
                candles_4h=self.klines[symbol]["4h"],
                candles_1d=self._daily_candles_with_live(symbol),
            )
        except Exception as exc:
            logger.exception("liquidation plugin failed symbol=%s", symbol)
            plugin = {
                "status": "failure",
                "weight": self.settings.liquidation_plugin_weight,
                "reason": f"{type(exc).__name__}: {exc}",
                "matched_scenario": "none",
            }
            self.symbol_status.setdefault(symbol, {})["liquidation_plugin"] = plugin
            return {"liquidation_plugin": plugin}
        context = result.as_context()
        plugin = context["liquidation_plugin"]
        self.symbol_status.setdefault(symbol, {})["liquidation_plugin"] = plugin
        return context

    async def _hours_since_latest_entry(self) -> float:
        now = datetime.now(timezone.utc)
        if self.store and time.monotonic() - self._last_order_store_checked_at >= 300:
            self._last_order_store_checked_at = time.monotonic()
            try:
                latest = await self.store.latest_order_created_at()
            except Exception:
                logger.debug("latest order lookup failed", exc_info=True)
            else:
                if latest is not None and (
                    self._last_entry_submitted_at is None
                    or latest > self._last_entry_submitted_at
                ):
                    self._last_entry_submitted_at = latest
        if self._last_entry_submitted_at is None:
            return float(self.settings.adaptive_no_trade_hours)
        latest = self._last_entry_submitted_at
        if latest.tzinfo is None:
            latest = latest.replace(tzinfo=timezone.utc)
        return max(0.0, (now - latest).total_seconds() / 3600)

    @staticmethod
    def _cooldown_active(
        cooldowns: dict[Hashable, float],
        key: Hashable,
    ) -> bool:
        until = cooldowns.get(key)
        if until is None:
            return False
        if time.monotonic() < until:
            return True
        cooldowns.pop(key, None)
        return False

    @staticmethod
    def _start_cooldown(
        cooldowns: dict[Hashable, float],
        key: Hashable,
        seconds: float,
    ) -> None:
        cooldowns[key] = time.monotonic() + max(0.0, float(seconds))

    async def _position_monitor_loop(self) -> None:
        while self.running and not self.risk.fused():
            try:
                await self._monitor_positions_once()
                interval = max(0.5, self.settings.position_monitor_interval_seconds)
                self.position_monitor_status["interval_seconds"] = interval
                await asyncio.sleep(interval)
            except Exception:
                logger.exception("position monitor loop failed")
                await asyncio.sleep(5)

    async def _monitor_positions_once(self) -> None:
        for symbol in self.settings.symbols:
            self.position_monitor_status["checks"] = (
                int(self.position_monitor_status.get("checks") or 0) + 1
            )
            self.position_monitor_status["last_checked_at"] = datetime.now(
                timezone.utc
            ).isoformat()
            self.position_monitor_status["last_symbol"] = symbol
            positions = await self._await_exchange_step(
                symbol,
                "monitor_fetch_positions",
                self.exchange.fetch_positions(symbol, refresh=True),
                self.settings.exchange_request_timeout_seconds,
            )
            if not positions:
                self.position_manager.recover_missing_states(
                    symbol,
                    [],
                    price=self._latest_price(symbol),
                    atr_value=self._latest_atr(symbol),
                    regime=self.regime_classifier.regimes.get(symbol, Regime.UNKNOWN),
                )
                continue
            price = await self._monitor_price(symbol)
            self.position_monitor_status["last_price"] = price
            atr_value = self._latest_atr(symbol)
            recovered = self.position_manager.recover_missing_states(
                symbol,
                positions,
                price=price,
                atr_value=atr_value,
                regime=self.regime_classifier.regimes.get(symbol, Regime.UNKNOWN),
                signal_hints=self._position_signal_hints(),
            )
            if recovered:
                self.symbol_status.setdefault(symbol, {})[
                    "recovered_positions"
                ] = recovered
            signals = self.position_manager.exit_signals(
                symbol, price, atr_value, positions
            )
            if signals:
                self.position_monitor_status["exit_signals"] = int(
                    self.position_monitor_status.get("exit_signals") or 0
                ) + len(signals)
                status = self.symbol_status.setdefault(symbol, {})
                status["last_signal"] = self._signal_status(signals[0])
                logger.info(
                    "position monitor exit ready symbol=%s price=%.8f reason=%s",
                    symbol,
                    price,
                    signals[0].reason,
                )
                await self._execute_signals(signals)

    async def _monitor_price(self, symbol: str) -> float:
        book = await self._await_exchange_step(
            symbol,
            "monitor_order_book",
            self.exchange.fetch_order_book(symbol),
            self.settings.exchange_request_timeout_seconds,
        )
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        bid = float(bids[0][0]) if bids else 0.0
        ask = float(asks[0][0]) if asks else 0.0
        if bid > 0 and ask > 0:
            price = (bid + ask) / 2
        else:
            price = bid or ask or self._latest_price(symbol)
        self.symbol_status.setdefault(symbol, {})["price"] = price
        return price

    def _latest_atr(self, symbol: str) -> float:
        candles = self.klines.get(symbol, {}).get("1h", [])
        if len(candles) < 2:
            price = self._latest_price(symbol)
            return max(price * self.settings.min_stop_loss_pct, 0.0)
        latest = candles[-1]
        high = float(latest[2])
        low = float(latest[3])
        close = float(latest[4])
        return max(high - low, close * self.settings.min_stop_loss_pct)

    def _update_cache(self, symbol: str, timeframe: str, row: list[float]) -> None:
        cache = self.klines[symbol][timeframe]
        if cache and int(cache[-1][0]) == int(row[0]):
            cache[-1] = row
        else:
            cache.append(row)
        max_items = 3_000 if timeframe == "5m" else 200
        if len(cache) > max_items:
            del cache[:-max_items]

    def _replace_timeframe_cache(
        self, symbol: str, timeframe: str, rows: list[list[float]]
    ) -> None:
        if not rows:
            return
        duration_ms = self._timeframe_duration_ms(timeframe)
        closed_rows = self._closed_ohlcv_rows(rows, duration_ms)
        if closed_rows:
            self.klines[symbol][timeframe] = closed_rows[-200:]

    def _sync_higher_timeframes_from_5m(self, symbol: str) -> None:
        derived_1h = self._resample_5m_cache(symbol, "1h", limit=120)
        if len(derived_1h) >= 80:
            self.klines[symbol]["1h"] = derived_1h
        derived_4h = self._resample_5m_cache(symbol, "4h", limit=100)
        if len(derived_4h) >= 50:
            self.klines[symbol]["4h"] = derived_4h

    def _daily_candles_with_live(self, symbol: str) -> list[list[float]]:
        rows_1d = list(self.klines.get(symbol, {}).get("1d", []))[-79:]
        rows_5m = self.klines.get(symbol, {}).get("5m", [])
        if not rows_5m:
            return rows_1d
        latest_ts = int(rows_5m[-1][0])
        day_start_ms = (latest_ts // 86_400_000) * 86_400_000
        current_rows = [row for row in rows_5m if int(row[0]) >= day_start_ms]
        if not current_rows:
            return rows_1d
        current_day = [
            float(current_rows[-1][0]),
            float(current_rows[0][1]),
            max(float(row[2]) for row in current_rows),
            min(float(row[3]) for row in current_rows),
            float(current_rows[-1][4]),
            sum(float(row[5]) for row in current_rows),
        ]
        if rows_1d and (int(rows_1d[-1][0]) // 86_400_000) == (latest_ts // 86_400_000):
            rows_1d = rows_1d[:-1]
        return rows_1d + [current_day]

    def _daily_macd_momentum_exit_signals(
        self, symbol: str, price: float, positions
    ) -> list[TradeSignal]:
        signals: list[TradeSignal] = []
        for position in positions:
            key = (symbol, position.side)
            state = self.position_manager.trailing.get(key)
            if (
                not state
                or not state.delay_trailing_until_momentum_exit
                or not self._daily_macd_momentum_exit(symbol, position.side)
            ):
                continue
            side = Side.SELL if position.side == PositionSide.LONG else Side.BUY
            signals.append(
                self.position_manager._exit_signal(
                    symbol,
                    side,
                    position.side,
                    price,
                    position.contracts,
                    "daily_macd_momentum_exit",
                    state.risk_multiplier,
                    state.entry_price,
                )
            )
        return signals

    def _daily_macd_momentum_exit(self, symbol: str, side: PositionSide) -> bool:
        rows = self._daily_candles_with_live(symbol)
        if len(rows) < 35:
            return False
        frame = ohlcv_frame(rows)
        _, _, histogram = macd(frame.close.astype(float))
        current_hist = float(histogram.iloc[-1])
        previous_hist = float(histogram.iloc[-2])
        if side == PositionSide.LONG:
            return current_hist > 0 and current_hist < previous_hist
        return current_hist < 0 and current_hist < previous_hist

    def _three_4h_reverse_candles(self, symbol: str, side: PositionSide) -> bool:
        rows = self.klines.get(symbol, {}).get("4h", [])
        if len(rows) < 3:
            return False
        frame = ohlcv_frame(rows[-3:])
        if side == PositionSide.LONG:
            return bool((frame.close < frame.open).all())
        return bool((frame.close > frame.open).all())

    def _resample_5m_cache(
        self, symbol: str, timeframe: str, *, limit: int
    ) -> list[list[float]]:
        rows = self.klines.get(symbol, {}).get("5m", [])
        if len(rows) < 12:
            return []
        df = ohlcv_frame(rows)
        if df.empty:
            return []
        df["dt"] = pd.to_datetime(df.ts, unit="ms", utc=True)
        df = df.set_index("dt")
        frame = (
            df.resample(timeframe)
            .agg(
                {
                    "ts": "last",
                    "open": "first",
                    "high": "max",
                    "low": "min",
                    "close": "last",
                    "volume": "sum",
                }
            )
            .dropna()
        )
        if frame.empty:
            return []
        rows_out = frame[
            ["ts", "open", "high", "low", "close", "volume"]
        ].values.tolist()
        duration_ms = self._timeframe_duration_ms(timeframe)
        return self._closed_ohlcv_rows(rows_out, duration_ms)[-limit:]

    @staticmethod
    def _timeframe_duration_ms(timeframe: str) -> int:
        return {
            "5m": 300_000,
            "1h": 3_600_000,
            "4h": 14_400_000,
            "1d": 86_400_000,
        }.get(timeframe, 60_000)

    @staticmethod
    def _closed_ohlcv_rows(
        rows: list[list[float]],
        duration_ms: int,
        *,
        now_ms: int | None = None,
    ) -> list[list[float]]:
        now_ms = (
            now_ms
            if now_ms is not None
            else int(datetime.now(timezone.utc).timestamp() * 1000)
        )
        return [row for row in rows if int(row[0]) + duration_ms <= now_ms]

    def _set_entry_status(
        self,
        symbol: str,
        *,
        price: float,
        regime: Regime,
        action: str,
        summary: str,
        blockers: list[dict[str, object]],
    ) -> None:
        checked_at = datetime.now(timezone.utc).isoformat()
        requirements = [dict(item) for item in blockers]
        diagnostics = {
            "action": action,
            "summary": summary,
            "requirements": requirements,
            "blockers": blockers,
            "metrics": {"regime": regime.value, "price": price},
            "checked_at": checked_at,
        }
        status = self.symbol_status.setdefault(symbol, {})
        status.update(
            {
                "price": price,
                "regime": regime.value,
                "checked_at": checked_at,
                "entry_diagnostics": diagnostics,
            }
        )
        self._log_diagnostic_status(
            symbol, action, summary, regime.value, price, blockers
        )

    def _set_completed_entry_diagnostics(
        self,
        symbol: str,
        diagnostics: dict[str, Any],
        *,
        price: float | None = None,
        regime: Regime | None = None,
    ) -> None:
        checked_at = datetime.now(timezone.utc).isoformat()
        metrics = (
            diagnostics.get("metrics")
            if isinstance(diagnostics.get("metrics"), dict)
            else {}
        )
        if price is None:
            price = self._float_metric(metrics.get("price"), self._latest_price(symbol))
        if regime is not None:
            regime_value = regime.value
        else:
            regime_value = str(
                metrics.get("regime")
                or self.regime_classifier.regimes.get(symbol, Regime.UNKNOWN).value
            )
        completed = {**diagnostics, "checked_at": checked_at}
        status = self.symbol_status.setdefault(symbol, {})
        status.update(
            {
                "price": price,
                "regime": regime_value,
                "checked_at": checked_at,
                "entry_diagnostics": completed,
                "last_completed_entry_diagnostics": completed,
            }
        )

    async def _await_exchange_step(
        self,
        symbol: str,
        stage: str,
        awaitable,
        timeout_seconds: float,
    ):
        try:
            return await asyncio.wait_for(awaitable, timeout=max(0.01, timeout_seconds))
        except TimeoutError:
            self._set_entry_status(
                symbol,
                price=self._latest_price(symbol),
                regime=self.regime_classifier.regimes.get(symbol, Regime.UNKNOWN),
                action=stage,
                summary="symbol_loop_error",
                blockers=[
                    {
                        "code": f"{stage}_timeout",
                        "passed": False,
                        "value": f">{timeout_seconds:.0f}s",
                    }
                ],
            )
            logger.warning(
                "exchange step timeout symbol=%s stage=%s timeout=%.1fs",
                symbol,
                stage,
                timeout_seconds,
            )
            raise

    def _log_entry_diagnostics(self, symbol: str, diagnostics: dict[str, Any]) -> None:
        metrics = (
            diagnostics.get("metrics")
            if isinstance(diagnostics.get("metrics"), dict)
            else {}
        )
        blockers = (
            diagnostics.get("blockers")
            if isinstance(diagnostics.get("blockers"), list)
            else []
        )
        price = self._float_metric(metrics.get("price"), self._latest_price(symbol))
        regime = str(
            metrics.get("regime")
            or self.regime_classifier.regimes.get(symbol, Regime.UNKNOWN).value
        )
        extra = {
            "score": metrics.get("opportunity_score"),
            "min_score": metrics.get("min_score"),
            "rsi_5m": metrics.get("rsi_5m"),
            "close_position_72h": metrics.get("close_position_72h"),
        }
        self._log_diagnostic_status(
            symbol,
            f"entry:{diagnostics.get('action', 'unknown')}",
            str(diagnostics.get("summary") or ""),
            regime,
            price,
            blockers,
            extra,
        )

    def _log_llm_review(self, symbol: str, review: Any) -> None:
        reasons = [str(reason) for reason in getattr(review, "reasons", [])]
        quiet_review = "llm_rate_limited" in reasons or (
            "llm_cached" in reasons
            and getattr(review, "action", "") == "KEEP"
            and bool(getattr(review, "allow_trade", True))
        )
        log = logger.debug if quiet_review else logger.info
        reason_text = ",".join(reasons)
        if len(reason_text) > 300:
            reason_text = f"{reason_text[:297]}..."
        log(
            "llm review completed symbol=%s action=%s proposed_regime=%s allow_trade=%s "
            "confidence=%.2f reasons=%s",
            symbol,
            review.action,
            review.proposed_regime.value,
            review.allow_trade,
            review.confidence,
            reason_text,
        )

    def _log_diagnostic_status(
        self,
        symbol: str,
        action: str,
        summary: str,
        regime: str,
        price: float,
        blockers: list[dict[str, object]],
        extra: dict[str, object] | None = None,
    ) -> None:
        blocker_text = self._format_blockers(blockers)
        extra_text = self._format_extra(extra or {})
        fingerprint = f"{summary}|{regime}|{self._format_blocker_codes(blockers)}"
        key = (symbol, action)
        now = time.monotonic()
        previous = self._diagnostic_log_state.get(key)
        log_interval = max(0.1, self.settings.diagnostics_log_interval_seconds)
        if previous and previous[0] == fingerprint and now - previous[1] < log_interval:
            return
        self._diagnostic_log_state[key] = (fingerprint, now)
        logger.info(
            "entry diagnostic symbol=%s action=%s summary=%s regime=%s price=%.8f blockers=%s%s",
            symbol,
            action,
            summary,
            regime,
            price,
            blocker_text or "-",
            f" {extra_text}" if extra_text else "",
        )

    @staticmethod
    def _format_blockers(blockers: list[dict[str, object]]) -> str:
        values = []
        for item in blockers:
            code = str(item.get("code") or "unknown")
            if "value" in item:
                values.append(f"{code}:{item.get('value')}")
            else:
                values.append(code)
        return ",".join(values)

    @staticmethod
    def _format_blocker_codes(blockers: list[dict[str, object]]) -> str:
        return ",".join(str(item.get("code") or "unknown") for item in blockers)

    @staticmethod
    def _format_extra(values: dict[str, object]) -> str:
        parts = [f"{key}={value}" for key, value in values.items() if value is not None]
        return " ".join(parts)

    @staticmethod
    def _float_metric(value: object, fallback: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return fallback

    def _latest_price(self, symbol: str) -> float:
        candles = self.klines.get(symbol, {}).get("5m", [])
        return float(candles[-1][4]) if candles else 0.0

    def _timeframe_stale(
        self, symbol: str, timeframe: str, stale_after_ms: int
    ) -> bool:
        candles = self.klines.get(symbol, {}).get(timeframe, [])
        live_5m = self.klines.get(symbol, {}).get("5m", [])
        if not candles or not live_5m:
            return True
        return int(live_5m[-1][0]) - int(candles[-1][0]) >= stale_after_ms

    async def _monitor_loop(self) -> None:
        while self.running and not self.risk.fused():
            try:
                await self._apply_runtime_settings()
                await self._reconcile_direction_risk_with_live_positions()
                equity = await self.exchange.fetch_balance_equity()
                if self.risk.update_equity(equity):
                    await self.exchange.close_all_positions()
                await self._save_snapshot()
                await self._sync_recent_orders()
                await self._save_cache()
                await asyncio.sleep(60)
            except Exception:
                logger.exception("monitor loop failed")
                await asyncio.sleep(5)

    async def _save_snapshot(self) -> None:
        if not self.store:
            return
        equity = await self.exchange.fetch_balance_equity()
        snapshot_time = datetime.now(timezone.utc)
        regimes = {
            symbol: regime.value
            for symbol, regime in self.regime_classifier.regimes.items()
        }
        regimes.update(
            {
                symbol: str(status.get("regime"))
                for symbol, status in self.symbol_status.items()
                if status.get("regime") is not None
            }
        )
        display_entry_diagnostics = {}
        current_entry_diagnostics = {}
        for symbol, status in self.symbol_status.items():
            current = status.get("entry_diagnostics")
            if current is not None:
                current_entry_diagnostics[symbol] = current
            display = self._display_entry_diagnostics(status)
            if display is not None:
                display_entry_diagnostics[symbol] = display
        positions_snapshot = await self._positions_snapshot()
        memory = {
            "engine_version": ENGINE_DIAGNOSTICS_VERSION,
            "exchange_id": self.settings.exchange_id,
            "market_data_source": "okx"
            if self.settings.exchange_id == "hotcoin"
            else self.settings.exchange_id,
            "snapshot_saved_at": snapshot_time.isoformat(),
            "regimes": regimes,
            "direction_risk": {
                side.value: value for side, value in self.risk.direction_risk.items()
            },
            "risk": {
                "risk_percent": self.settings.risk_percent,
                "same_direction_risk_limit": self.settings.same_direction_risk_limit,
                "daily_drawdown_limit": self.settings.daily_drawdown_limit,
                "shock_leverage_limit": self.settings.shock_leverage_limit,
                "trend_symbol_leverage_limit": self.settings.trend_symbol_leverage_limit,
                "max_signal_risk_multiplier": self.settings.max_signal_risk_multiplier,
                "confirmation_position_sizing": self.settings.confirmation_position_sizing,
                "confirmation_max_risk_multiplier": self.settings.confirmation_max_risk_multiplier,
                "position_monitor_interval_seconds": self.settings.position_monitor_interval_seconds,
                "positions_cache_ttl_seconds": self.settings.positions_cache_ttl_seconds,
                "live_entry_order_cooldown_seconds": self.settings.live_entry_order_cooldown_seconds,
                "live_exit_order_cooldown_seconds": self.settings.live_exit_order_cooldown_seconds,
                "min_live_equity_to_order": self.settings.min_live_equity_to_order,
                "llm_regime_review_enabled": self.settings.llm_regime_review_enabled,
                "llm_regime_provider": self.settings.llm_regime_provider,
                "llm_regime_model": self.settings.llm_regime_model,
                "llm_regime_review_cache_ttl_seconds": self.settings.llm_regime_review_cache_ttl_seconds,
                "llm_regime_review_min_interval_seconds": self.settings.llm_regime_review_min_interval_seconds,
            },
            "prices": {
                symbol: status.get("price")
                for symbol, status in self.symbol_status.items()
            },
            "regime_checked_at": {
                symbol: status.get("checked_at")
                for symbol, status in self.symbol_status.items()
            },
            "entry_diagnostics": display_entry_diagnostics,
            "current_entry_diagnostics": current_entry_diagnostics,
            "liquidation_plugin": {
                symbol: status.get("liquidation_plugin")
                for symbol, status in self.symbol_status.items()
                if status.get("liquidation_plugin") is not None
            },
            "last_signal": {
                symbol: status.get("last_signal")
                for symbol, status in self.symbol_status.items()
                if status.get("last_signal") is not None
            },
            "last_risk_rejection": {
                symbol: status.get("last_risk_rejection")
                for symbol, status in self.symbol_status.items()
                if status.get("last_risk_rejection") is not None
            },
            "last_order": {
                symbol: status.get("last_order")
                for symbol, status in self.symbol_status.items()
                if status.get("last_order") is not None
            },
            "position_monitor": self.position_monitor_status,
            "positions": positions_snapshot,
            "trailing_states": self.position_manager.trailing_snapshot(),
            "recovered_positions": {
                f"{symbol}:{side.value}": value
                for (
                    symbol,
                    side,
                ), value in self.position_manager.recovered_positions.items()
            },
            "symbol_locks": {
                symbol: until.isoformat()
                for symbol, until in self.risk.symbol_locks.items()
            },
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

    async def _positions_snapshot(self) -> list[dict[str, object]]:
        try:
            positions = await self.exchange.fetch_positions()
        except Exception as exc:
            logger.warning("positions snapshot failed: %s", exc)
            return []
        return [
            {
                "symbol": position.symbol,
                "side": position.side.value,
                "contracts": position.contracts,
                "entry_price": position.entry_price,
                "unrealized_pnl": position.unrealized_pnl,
            }
            for position in positions
        ]

    async def _sync_submitted_order(
        self,
        order: OrderResult,
        signal: TradeSignal,
    ) -> OrderResult | None:
        if signal.signal_type not in {SignalType.EXIT, SignalType.RELEASE_HEDGE}:
            return None
        await asyncio.sleep(1)
        try:
            return await self.exchange.fetch_order(order.id, order.symbol)
        except Exception:
            logger.exception(
                "order sync failed order_id=%s symbol=%s", order.id, order.symbol
            )
            return None

    @staticmethod
    def _order_has_fill(order: OrderResult) -> bool:
        return order.filled > 0 or str(order.status).lower() in {"closed", "filled"}

    async def _sync_recent_orders(self) -> None:
        if not self.store:
            return
        try:
            refs = await self.store.recent_order_refs(limit=20)
        except Exception:
            logger.exception("load recent orders for sync failed")
            return
        for order_id, symbol in refs:
            try:
                order = await self.exchange.fetch_order(order_id, symbol)
                await self.store.update_order_result(order)
            except Exception:
                logger.debug(
                    "recent order sync skipped order_id=%s symbol=%s",
                    order_id,
                    symbol,
                    exc_info=True,
                )

    def _display_entry_diagnostics(
        self, status: dict[str, object]
    ) -> dict[str, Any] | None:
        current = status.get("entry_diagnostics")
        completed = status.get("last_completed_entry_diagnostics")
        if isinstance(current, dict) and not self._entry_diagnostics_transient(current):
            return current
        if isinstance(completed, dict):
            return completed
        return current if isinstance(current, dict) else None

    @staticmethod
    def _entry_diagnostics_transient(diagnostics: dict[str, Any]) -> bool:
        return str(diagnostics.get("summary") or "") in TRANSIENT_ENTRY_SUMMARIES

    @staticmethod
    def _signal_status(signal: TradeSignal) -> dict[str, object]:
        return {
            "type": signal.signal_type.value,
            "side": signal.side.value,
            "position_side": signal.position_side.value,
            "regime": signal.regime.value,
            "price": signal.price,
            "stop_loss": signal.stop_loss,
            "take_profit": signal.take_profit,
            "reason": signal.reason,
            "created_at": signal.created_at.isoformat(),
            "trailing_gap_pct": signal.metadata.get("trailing_gap_pct"),
            "min_trailing_activate_r": signal.metadata.get("min_trailing_activate_r"),
            "breakeven_activate_r": signal.metadata.get("breakeven_activate_r"),
            "breakeven_buffer_pct": signal.metadata.get("breakeven_buffer_pct"),
            "risk_multiplier": signal.metadata.get("risk_multiplier"),
        }

    def _position_signal_hints(
        self,
    ) -> dict[tuple[str, PositionSide], dict[str, object]]:
        hints: dict[tuple[str, PositionSide], dict[str, object]] = {}
        for symbol, status in self.symbol_status.items():
            signal = status.get("last_signal")
            if not isinstance(signal, dict) or signal.get("type") not in {
                SignalType.ENTER_TREND.value,
                SignalType.ENTER_GRID.value,
            }:
                continue
            try:
                side = PositionSide(str(signal.get("position_side")))
            except ValueError:
                continue
            hints[(symbol, side)] = signal
        return hints

    async def _restore_latest_snapshot(self) -> None:
        if not self.store:
            return
        snapshot = await self.store.load_latest_snapshot()
        if not snapshot:
            return
        snapshot_exchange = str(snapshot.get("exchange_id") or "okx")
        same_exchange = snapshot_exchange == self.settings.exchange_id
        for symbol, value in snapshot.get("regimes", {}).items():
            self.regime_classifier.regimes[symbol] = Regime(value)
        if same_exchange:
            for side, value in snapshot.get("direction_risk", {}).items():
                self.risk.direction_risk[PositionSide(side)] = float(value)
            self.position_manager.restore_trailing_snapshot(
                snapshot.get("trailing_states", {})
            )
            for symbol, signal in snapshot.get("last_signal", {}).items():
                self.symbol_status.setdefault(symbol, {})["last_signal"] = signal
            for symbol, order in snapshot.get("last_order", {}).items():
                self.symbol_status.setdefault(symbol, {})["last_order"] = order
            for symbol, raw in snapshot.get("hedge_locks", {}).items():
                self.hedge_locks[symbol] = HedgeLock(
                    symbol=symbol,
                    grid_side=PositionSide(raw["grid_side"]),
                    hedge_side=PositionSide(raw["hedge_side"]),
                    contracts=float(raw["contracts"]),
                    active=bool(raw.get("active", True)),
                )
        else:
            logger.info(
                "skipped position state restore from different exchange snapshot=%s current=%s",
                snapshot_exchange,
                self.settings.exchange_id,
            )
        logger.info("restored latest account snapshot")

    async def _reconcile_direction_risk_with_live_positions(self) -> None:
        try:
            positions = await self.exchange.fetch_positions(refresh=True)
        except Exception:
            logger.debug(
                "direction risk reconcile skipped; fetch positions failed",
                exc_info=True,
            )
            return

        live_sides = {
            position.side for position in positions if abs(position.contracts) > 0
        }
        for side in (PositionSide.LONG, PositionSide.SHORT):
            current = self.risk.direction_risk.get(side, 0.0)
            if current > 0 and side not in live_sides:
                self.risk.direction_risk[side] = 0.0
                logger.warning(
                    "cleared stale direction risk side=%s previous=%.6f live_positions=%s",
                    side.value,
                    current,
                    [
                        {
                            "symbol": position.symbol,
                            "side": position.side.value,
                            "contracts": position.contracts,
                        }
                        for position in positions
                        if abs(position.contracts) > 0
                    ],
                )

    async def _save_cache(self) -> None:
        if not self.cache:
            return
        await self.cache.set_positions(await self.exchange.fetch_positions())
        await self.cache.set_hedge_locks(self.hedge_locks)

    async def _apply_runtime_settings(
        self, update_exchange_leverage: bool = True
    ) -> None:
        if not self.store:
            return
        values = await self.store.load_runtime_settings()
        if not values:
            return
        previous_leverage = self.settings.trend_symbol_leverage_limit
        previous_llm = (
            self.llm_reviewer.provider,
            self.llm_reviewer.model,
            self.llm_reviewer.base_url,
            bool(self.llm_reviewer.api_key_value),
            self.llm_reviewer.enabled,
            self.llm_reviewer.cache_ttl_seconds,
            self.llm_reviewer.min_interval_seconds,
        )
        apply_runtime_config(self.settings, values)
        self.email_notifier = OrderEmailNotifier(
            SmtpConfig.from_settings(self.settings)
        )
        self.execution.email_notifier = self.email_notifier
        self._apply_llm_runtime_settings(previous_llm)
        if (
            update_exchange_leverage
            and self.settings.trend_symbol_leverage_limit != previous_leverage
        ):
            for symbol in self.settings.symbols:
                await self.exchange.set_leverage(
                    symbol, self.settings.trend_symbol_leverage_limit
                )
            self._applied_leverage_limit = self.settings.trend_symbol_leverage_limit
            logger.info(
                "runtime leverage update requested leverage=%s",
                self.settings.trend_symbol_leverage_limit,
            )

    def _apply_llm_runtime_settings(self, previous: tuple[object, ...]) -> None:
        provider = self.settings.llm_regime_provider.lower()
        base_url = self.settings.llm_regime_base_url or (
            "https://api.deepseek.com" if provider == "deepseek" else None
        )
        api_key_env = self.settings.llm_regime_api_key_env or (
            "DEEPSEEK_API_KEY" if provider == "deepseek" else "OPENAI_API_KEY"
        )
        self.llm_reviewer.provider = provider
        self.llm_reviewer.model = self.settings.llm_regime_model
        self.llm_reviewer.base_url = base_url
        self.llm_reviewer.api_key_value = self.settings.llm_regime_api_key
        self.llm_reviewer.api_key_env = api_key_env
        self.llm_reviewer.enabled = self.settings.llm_regime_review_enabled
        self.llm_reviewer.cache_ttl_seconds = (
            self.settings.llm_regime_review_cache_ttl_seconds
        )
        self.llm_reviewer.min_interval_seconds = (
            self.settings.llm_regime_review_min_interval_seconds
        )
        current = (
            self.llm_reviewer.provider,
            self.llm_reviewer.model,
            self.llm_reviewer.base_url,
            bool(self.llm_reviewer.api_key_value),
            self.llm_reviewer.enabled,
            self.llm_reviewer.cache_ttl_seconds,
            self.llm_reviewer.min_interval_seconds,
        )
        if current != previous:
            self.llm_reviewer._review_cache.clear()
            logger.info(
                "runtime llm settings updated enabled=%s provider=%s model=%s cache_ttl=%s min_interval=%s",
                self.llm_reviewer.enabled,
                self.llm_reviewer.provider,
                self.llm_reviewer.model,
                self.llm_reviewer.cache_ttl_seconds,
                self.llm_reviewer.min_interval_seconds,
            )

    async def shutdown(self) -> None:
        self.running = False
        await self.execution.drain()
        if self.store:
            await self.store.close()
        if self.cache:
            await self.cache.close()
        try:
            await self.exchange.close()
        except Exception:
            logger.exception("exchange close failed")

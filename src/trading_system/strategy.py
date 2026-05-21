from __future__ import annotations

from typing import Any

from trading_system.indicators import atr, crossed_above, crossed_below, ema, macd, ohlcv_frame, rsi
from trading_system.models import HedgeLock, MarketFeatures, Position, PositionSide, Regime, Side, SignalType, TradeSignal
from trading_system.opportunity import opportunity_metadata, score_opportunity, sol_structure_stop, sol_trade_allowed
from trading_system.volatility import build_volatility_policy


class StrategyEngine:
    def __init__(
        self,
        min_stop_loss_pct: float = 0.002,
        high_vol_min_stop_loss_pct: float = 0.004,
        extreme_vol_min_stop_loss_pct: float = 0.005,
        max_stop_loss_pct: float = 0.012,
        shock_reward_risk: float = 1.15,
        trend_reward_risk: float = 1.8,
        min_take_profit_pct: float = 0.004,
        trailing_gap_pct: float = 0.0025,
        min_trailing_activate_r: float = 1.0,
        enable_trend_short: bool = True,
        trend_long_risk_multiplier: float = 0.35,
        trend_short_risk_multiplier: float = 0.5,
        defensive_risk_multiplier: float = 0.7,
        shock_trend_risk_multiplier: float = 0.1,
        shock_trend_down_risk_multiplier: float = 0.1,
    ) -> None:
        self.min_stop_loss_pct = min_stop_loss_pct
        self.high_vol_min_stop_loss_pct = high_vol_min_stop_loss_pct
        self.extreme_vol_min_stop_loss_pct = extreme_vol_min_stop_loss_pct
        self.max_stop_loss_pct = max_stop_loss_pct
        self.shock_reward_risk = shock_reward_risk
        self.trend_reward_risk = trend_reward_risk
        self.min_take_profit_pct = min_take_profit_pct
        self.trailing_gap_pct = trailing_gap_pct
        self.min_trailing_activate_r = min_trailing_activate_r
        self.enable_trend_short = enable_trend_short
        self.trend_long_risk_multiplier = trend_long_risk_multiplier
        self.trend_short_risk_multiplier = trend_short_risk_multiplier
        self.defensive_risk_multiplier = defensive_risk_multiplier
        self.shock_trend_risk_multiplier = shock_trend_risk_multiplier
        self.shock_trend_down_risk_multiplier = shock_trend_down_risk_multiplier

    def build_signals(
        self,
        symbol: str,
        regime: Regime,
        previous_regime: Regime,
        features: MarketFeatures,
        candles_5m: list[list[float]],
        positions: list[Position],
    ) -> list[TradeSignal]:
        if regime == Regime.UNKNOWN:
            return []

        signals: list[TradeSignal] = []
        signals.extend(self._transition_hedges(symbol, regime, previous_regime, features.close_1h, positions))
        if signals:
            return signals

        signal = self._user_4h_signal(symbol, regime, features, candles_5m)
        return [signal] if signal else []

    def entry_diagnostics(
        self,
        symbol: str,
        regime: Regime,
        features: MarketFeatures,
        candles_5m: list[list[float]],
        positions: list[Position],
    ) -> dict[str, Any]:
        if regime == Regime.UNKNOWN:
            return self._diagnostics(
                "none",
                [{"code": "regime_known", "passed": False, "value": "UNKNOWN"}],
                {"regime": regime.value, "positions": len(positions)},
            )

        df = ohlcv_frame(candles_5m)
        if len(df) < 35 or features.atr_1h <= 0:
            return self._diagnostics(
                "data_wait",
                [
                    {"code": "enough_5m_candles", "passed": len(df) >= 35, "value": len(df)},
                    {"code": "atr_ready", "passed": features.atr_1h > 0, "value": round(features.atr_1h, 8)},
                ],
                {"regime": regime.value, "positions": len(positions)},
            )

        price = float(df.close.iloc[-1])
        current = df.iloc[-1]
        macd_line, signal_line, _ = macd(df.close)
        ema20_5m = ema(df.close, 20)
        rsi_5m = rsi(df.close, 14)
        last_rsi = float(rsi_5m.iloc[-1])
        ema_now = float(ema20_5m.iloc[-1])
        ema_prev = float(ema20_5m.iloc[-4])
        crossed_up = crossed_above(macd_line, signal_line)
        crossed_down = crossed_below(macd_line, signal_line)
        recent_pullback_down = recent_down_candle(df)
        recent_pullback_up = recent_up_candle(df)
        trend_up_aligned = self._multi_timeframe_aligned(df, PositionSide.LONG)
        trend_down_aligned = self._multi_timeframe_aligned(df, PositionSide.SHORT)
        volatility_policy = self._volatility_policy(price, features, df)
        trend_long_near_breakout = (
            price >= features.range_high_4h - volatility_policy.breakout_retrace_atr * features.atr_1h
        )
        trend_short_near_breakout = (
            price <= features.range_low_4h + volatility_policy.breakout_retrace_atr * features.atr_1h
        )
        long_momentum_positive = bool(macd_line.iloc[-1] >= signal_line.iloc[-1])
        short_momentum_positive = bool(macd_line.iloc[-1] <= signal_line.iloc[-1])
        metrics = {
            "regime": regime.value,
            "price": round(price, 8),
            "rsi_5m": round(last_rsi, 2),
            "close_position_72h": round(features.close_position_72h, 4),
            "ret_24h": round(features.ret_24h, 5),
            "ret_72h": round(features.ret_72h, 5),
            "volatility_tier": volatility_policy.tier,
            "positions": len(positions),
        }

        if regime == Regime.SHOCK:
            midpoint = (features.range_high_4h + features.range_low_4h) / 2
            if price < midpoint:
                return self._diagnostics(
                    "shock_long",
                    [
                        {"code": "price_below_midpoint", "passed": True, "value": round(price - midpoint, 8)},
                        {"code": "macd_cross_up", "passed": crossed_up},
                    ],
                    metrics,
                )
            return self._diagnostics(
                "shock_short",
                [
                    {"code": "price_above_midpoint", "passed": price > midpoint, "value": round(price - midpoint, 8)},
                    {"code": "macd_cross_down", "passed": crossed_down},
                ],
                metrics,
            )

        if regime == Regime.TREND_LONG:
            rsi_quality = 40 <= last_rsi <= 66
            opportunity = score_opportunity(
                symbol=symbol,
                regime=regime,
                side=PositionSide.LONG,
                checks={
                    "regime_direction": True,
                    "multi_timeframe": trend_up_aligned,
                    "one_hour_trend": features.ema20_1h >= features.ema60_1h,
                    "four_hour_trend": trend_long_near_breakout,
                    "pullback": recent_pullback_down,
                    "confirmation_candle": float(current.close) > float(current.open),
                    "momentum_cross": crossed_up,
                    "momentum_positive": long_momentum_positive,
                    "price_location": price >= ema_now * 0.998,
                    "ema_slope": ema_now >= ema_prev,
                    "rsi_quality": rsi_quality,
                    "not_chasing": last_rsi <= 70,
                },
                penalties={
                    "extreme_volatility": volatility_policy.tier == "EXTREME",
                    "counter_ema": features.ema20_1h < features.ema60_1h,
                },
                reward_risk=self.trend_reward_risk,
                volatility_tier=volatility_policy.tier,
                min_score=88,
            )
            sol_allowed = self._sol_allowed(symbol, PositionSide.LONG, regime, opportunity.score, features)
            return self._diagnostics(
                "trend_long",
                [
                    {"code": "opportunity_score", "passed": opportunity.allow_trade, "value": opportunity.score},
                    {"code": "multi_timeframe", "passed": (not volatility_policy.require_multi_timeframe or trend_up_aligned)},
                    {"code": "near_breakout", "passed": (not volatility_policy.require_near_breakout or trend_long_near_breakout)},
                    {"code": "pullback_down", "passed": recent_pullback_down},
                    {"code": "bullish_candle", "passed": float(current.close) > float(current.open)},
                    {"code": "macd_cross_up", "passed": crossed_up},
                    {"code": "price_above_ema20_5m", "passed": price >= ema_now * 0.998},
                    {"code": "rsi_40_66", "passed": rsi_quality, "value": round(last_rsi, 2)},
                    {"code": "sol_filter", "passed": sol_allowed},
                ],
                {**metrics, "opportunity_score": opportunity.score},
            )

        if regime == Regime.TREND_SHORT:
            rsi_quality = 34 <= last_rsi <= 60
            opportunity = score_opportunity(
                symbol=symbol,
                regime=regime,
                side=PositionSide.SHORT,
                checks={
                    "regime_direction": True,
                    "multi_timeframe": trend_down_aligned,
                    "one_hour_trend": features.ema20_1h <= features.ema60_1h,
                    "four_hour_trend": trend_short_near_breakout and features.last_4h_close < features.prev_4h_close,
                    "pullback": recent_pullback_up,
                    "confirmation_candle": float(current.close) < float(current.open),
                    "momentum_cross": crossed_down,
                    "momentum_positive": short_momentum_positive,
                    "price_location": price <= ema_now * 1.002,
                    "ema_slope": ema_now <= ema_prev,
                    "rsi_quality": rsi_quality,
                    "not_chasing": last_rsi >= 24,
                },
                penalties={
                    "extreme_volatility": volatility_policy.tier == "EXTREME",
                    "counter_ema": features.ema20_1h > features.ema60_1h,
                    "overextended": price < features.range_low_4h * 0.985,
                },
                reward_risk=self.trend_reward_risk,
                volatility_tier=volatility_policy.tier,
                min_score=88,
            )
            sol_allowed = self._sol_allowed(symbol, PositionSide.SHORT, regime, opportunity.score, features)
            return self._diagnostics(
                "trend_short",
                [
                    {"code": "trend_short_enabled", "passed": self.enable_trend_short},
                    {"code": "opportunity_score", "passed": opportunity.allow_trade, "value": opportunity.score},
                    {"code": "multi_timeframe", "passed": (not volatility_policy.require_multi_timeframe or trend_down_aligned)},
                    {"code": "near_breakout", "passed": (not volatility_policy.require_near_breakout or trend_short_near_breakout)},
                    {"code": "one_hour_bearish", "passed": features.ema20_1h <= features.ema60_1h},
                    {"code": "four_hour_bearish", "passed": features.last_4h_close < features.prev_4h_close},
                    {"code": "near_4h_low", "passed": features.last_4h_close <= features.range_low_4h * 1.01 and price < features.range_low_4h * 1.003},
                    {"code": "pullback_up", "passed": recent_pullback_up},
                    {"code": "bearish_candle", "passed": float(current.close) < float(current.open)},
                    {"code": "macd_cross_down", "passed": crossed_down},
                    {"code": "price_below_ema20_5m", "passed": price <= ema_now * 1.002},
                    {"code": "rsi_34_60", "passed": rsi_quality, "value": round(last_rsi, 2)},
                    {"code": "sol_filter", "passed": sol_allowed},
                ],
                {**metrics, "opportunity_score": opportunity.score},
            )

        return self._shock_trend_diagnostics(
            symbol=symbol,
            regime=regime,
            features=features,
            df=df,
            price=price,
            current=current,
            crossed_up=crossed_up,
            crossed_down=crossed_down,
            recent_pullback_down=recent_pullback_down,
            recent_pullback_up=recent_pullback_up,
            trend_up_aligned=trend_up_aligned,
            trend_down_aligned=trend_down_aligned,
            long_momentum_positive=long_momentum_positive,
            short_momentum_positive=short_momentum_positive,
            ema_now=ema_now,
            ema_prev=ema_prev,
            last_rsi=last_rsi,
            volatility_policy=volatility_policy,
            metrics=metrics,
        )

    def _diagnostics(
        self,
        action: str,
        requirements: list[dict[str, Any]],
        metrics: dict[str, Any],
    ) -> dict[str, Any]:
        normalized_requirements: list[dict[str, Any]] = []
        for item in requirements:
            passed = bool(item.get("passed"))
            normalized = {
                "code": str(item.get("code", "unknown")),
                "passed": passed,
            }
            if "value" in item:
                normalized["value"] = self._json_value(item["value"])
            normalized_requirements.append(normalized)
        blockers = [item for item in normalized_requirements if not item["passed"]]
        return {
            "action": action,
            "summary": "entry_conditions_met" if not blockers else "waiting_for_conditions",
            "requirements": normalized_requirements,
            "blockers": blockers,
            "metrics": {str(key): self._json_value(value) for key, value in metrics.items()},
        }

    def _shock_trend_diagnostics(
        self,
        symbol: str,
        regime: Regime,
        features: MarketFeatures,
        df,
        price: float,
        current,
        crossed_up: bool,
        crossed_down: bool,
        recent_pullback_down: bool,
        recent_pullback_up: bool,
        trend_up_aligned: bool,
        trend_down_aligned: bool,
        long_momentum_positive: bool,
        short_momentum_positive: bool,
        ema_now: float,
        ema_prev: float,
        last_rsi: float,
        volatility_policy,
        metrics: dict[str, Any],
    ) -> dict[str, Any]:
        if regime == Regime.SHOCK_TREND_UP:
            rsi_quality = 42 <= last_rsi <= 66
            opportunity = score_opportunity(
                symbol=symbol,
                regime=regime,
                side=PositionSide.LONG,
                checks={
                    "regime_direction": True,
                    "multi_timeframe": trend_up_aligned,
                    "one_hour_trend": features.ema20_1h >= features.ema60_1h,
                    "four_hour_trend": features.last_4h_close > features.prev_4h_close,
                    "pullback": recent_pullback_down,
                    "confirmation_candle": float(current.close) > float(current.open),
                    "momentum_cross": crossed_up,
                    "momentum_positive": long_momentum_positive,
                    "price_location": price >= ema_now * 0.998 and price >= features.ema20_1h * 0.997,
                    "ema_slope": ema_now >= ema_prev,
                    "rsi_quality": rsi_quality,
                    "not_chasing": last_rsi <= 68,
                    "range_position": features.close_position_72h <= 0.92,
                },
                penalties={
                    "directional_conflict": self._directional_breakout_active(features)
                    and not (features.ret_72h > 0 and features.ret_24h > -0.01),
                    "rsi_extreme": last_rsi > 72,
                    "counter_ema": features.ema20_1h < features.ema60_1h,
                    "overextended": features.close_position_72h > 0.96,
                },
                reward_risk=self.trend_reward_risk,
                volatility_tier=volatility_policy.tier,
                min_score=88,
            )
            sol_allowed = self._sol_allowed(symbol, PositionSide.LONG, regime, opportunity.score, features)
            return self._diagnostics(
                "shock_trend_up",
                [
                    {"code": "opportunity_score", "passed": opportunity.allow_trade, "value": opportunity.score},
                    {"code": "multi_timeframe", "passed": trend_up_aligned},
                    {"code": "one_hour_bullish", "passed": features.ema20_1h >= features.ema60_1h},
                    {"code": "four_hour_bullish", "passed": features.last_4h_close > features.prev_4h_close},
                    {"code": "pullback_down", "passed": recent_pullback_down},
                    {"code": "bullish_candle", "passed": float(current.close) > float(current.open)},
                    {"code": "macd_cross_up", "passed": crossed_up},
                    {"code": "price_above_ema20_5m", "passed": price >= ema_now * 0.998},
                    {"code": "price_above_ema20_1h", "passed": price >= features.ema20_1h * 0.997},
                    {"code": "ema_slope_up", "passed": ema_now >= ema_prev},
                    {"code": "rsi_42_66", "passed": rsi_quality, "value": round(last_rsi, 2)},
                    {"code": "range_position_not_chasing", "passed": features.close_position_72h <= 0.92, "value": round(features.close_position_72h, 4)},
                    {"code": "sol_filter", "passed": sol_allowed},
                ],
                {
                    **metrics,
                    "opportunity_score": opportunity.score,
                    "opportunity_grade": opportunity.grade,
                    "min_score": 88,
                    "ema20_1h": round(features.ema20_1h, 8),
                    "ema60_1h": round(features.ema60_1h, 8),
                    "last_4h_close": round(features.last_4h_close, 8),
                    "prev_4h_close": round(features.prev_4h_close, 8),
                    "recent_5h_position": round(self._recent_range_position(df, price, bars=60), 4),
                },
            )

        if regime == Regime.SHOCK_TREND_DOWN:
            rsi_quality = 35 <= last_rsi <= 58
            range_ok = features.close_position_72h >= (0.18 if symbol.startswith("SOL/") else 0.08)
            momentum_ok = (
                features.ret_24h <= -0.003 or features.close_position_72h >= 0.30
                if symbol.startswith("SOL/")
                else True
            )
            opportunity = score_opportunity(
                symbol=symbol,
                regime=regime,
                side=PositionSide.SHORT,
                checks={
                    "regime_direction": True,
                    "multi_timeframe": trend_down_aligned,
                    "one_hour_trend": features.ema20_1h <= features.ema60_1h,
                    "four_hour_trend": features.last_4h_close < features.prev_4h_close,
                    "pullback": recent_pullback_up,
                    "confirmation_candle": float(current.close) < float(current.open),
                    "momentum_cross": crossed_down,
                    "momentum_positive": short_momentum_positive,
                    "price_location": price <= ema_now * 1.002 and price <= features.ema20_1h * 1.003,
                    "ema_slope": ema_now <= ema_prev,
                    "rsi_quality": rsi_quality,
                    "not_chasing": last_rsi >= 24,
                    "range_position": range_ok,
                },
                penalties={
                    "directional_conflict": self._directional_breakout_active(features)
                    and not (features.ret_72h < 0 and features.ret_24h < 0.01),
                    "rsi_extreme": last_rsi < 20,
                    "counter_ema": features.ema20_1h > features.ema60_1h,
                    "overextended": features.close_position_72h < (0.18 if symbol.startswith("SOL/") else 0.04),
                },
                reward_risk=self.trend_reward_risk,
                volatility_tier=volatility_policy.tier,
                min_score=96,
            )
            sol_allowed = self._sol_allowed(symbol, PositionSide.SHORT, regime, opportunity.score, features)
            return self._diagnostics(
                "shock_trend_down",
                [
                    {"code": "opportunity_score", "passed": opportunity.allow_trade, "value": opportunity.score},
                    {"code": "multi_timeframe", "passed": trend_down_aligned},
                    {"code": "one_hour_bearish", "passed": features.ema20_1h <= features.ema60_1h},
                    {"code": "price_below_ema20_1h", "passed": price <= features.ema20_1h * 1.003},
                    {"code": "four_hour_bearish", "passed": features.last_4h_close < features.prev_4h_close},
                    {"code": "pullback_up", "passed": recent_pullback_up},
                    {"code": "bearish_candle", "passed": float(current.close) < float(current.open)},
                    {"code": "macd_cross_down", "passed": crossed_down},
                    {"code": "price_below_ema20_5m", "passed": price <= ema_now * 1.002},
                    {"code": "ema_slope_down", "passed": ema_now <= ema_prev},
                    {"code": "rsi_35_58", "passed": rsi_quality, "value": round(last_rsi, 2)},
                    {"code": "range_position_short_room", "passed": range_ok, "value": round(features.close_position_72h, 4)},
                    {"code": "down_momentum", "passed": momentum_ok, "value": round(features.ret_24h, 5)},
                    {"code": "sol_filter", "passed": sol_allowed},
                ],
                {
                    **metrics,
                    "opportunity_score": opportunity.score,
                    "opportunity_grade": opportunity.grade,
                    "min_score": 96,
                    "ema20_1h": round(features.ema20_1h, 8),
                    "ema60_1h": round(features.ema60_1h, 8),
                    "last_4h_close": round(features.last_4h_close, 8),
                    "prev_4h_close": round(features.prev_4h_close, 8),
                },
            )

        return self._diagnostics(
            "none",
            [{"code": "supported_regime", "passed": False, "value": regime.value}],
            metrics,
        )

    @staticmethod
    def _json_value(value: Any) -> Any:
        if hasattr(value, "item"):
            value = value.item()
        if isinstance(value, float):
            return round(value, 8)
        if isinstance(value, (str, int, bool)) or value is None:
            return value
        return str(value)

    def hedge_release_signals(
        self,
        symbol: str,
        lock: HedgeLock | None,
        candles_5m: list[list[float]],
        positions: list[Position],
    ) -> list[TradeSignal]:
        if not lock or not lock.active:
            return []
        df = ohlcv_frame(candles_5m)
        if len(df) < 20:
            return []
        rsi_5m = rsi(df.close, 14)
        price = float(df.close.iloc[-1])
        release = False
        if lock.hedge_side == PositionSide.LONG:
            release = bool(rsi_5m.iloc[-2] > 70 and rsi_5m.iloc[-1] < rsi_5m.iloc[-2])
        elif lock.hedge_side == PositionSide.SHORT:
            release = bool(rsi_5m.iloc[-2] < 30 and rsi_5m.iloc[-1] > rsi_5m.iloc[-2])
        if not release:
            return []

        signals: list[TradeSignal] = []
        for pos in positions:
            if pos.side not in {lock.grid_side, lock.hedge_side} or pos.contracts <= 0:
                continue
            side = Side.SELL if pos.side == PositionSide.LONG else Side.BUY
            signals.append(
                TradeSignal(
                    symbol=symbol,
                    signal_type=SignalType.RELEASE_HEDGE,
                    side=side,
                    position_side=pos.side,
                    regime=Regime.UNKNOWN,
                    price=price,
                    stop_loss=price,
                    reason="hedge_release_5m_pullback",
                    metadata={"contracts": pos.contracts, "reduce_only": True},
                )
            )
        return signals

    def _transition_hedges(
        self,
        symbol: str,
        regime: Regime,
        previous_regime: Regime,
        price: float,
        positions: list[Position],
    ) -> list[TradeSignal]:
        if previous_regime != Regime.SHOCK:
            return []
        signals: list[TradeSignal] = []
        if regime == Regime.TREND_LONG:
            for pos in positions:
                if pos.side == PositionSide.SHORT and pos.contracts > 0:
                    signals.append(
                        TradeSignal(
                            symbol=symbol,
                            signal_type=SignalType.HEDGE_TRANSITION,
                            side=Side.BUY,
                            position_side=PositionSide.LONG,
                            regime=regime,
                            price=price,
                            stop_loss=0.0,
                            reason="shock_to_trend_long_hedge_short_grid",
                            metadata={"hedge_contracts": pos.contracts},
                        )
                    )
        if regime == Regime.TREND_SHORT:
            for pos in positions:
                if pos.side == PositionSide.LONG and pos.contracts > 0:
                    signals.append(
                        TradeSignal(
                            symbol=symbol,
                            signal_type=SignalType.HEDGE_TRANSITION,
                            side=Side.SELL,
                            position_side=PositionSide.SHORT,
                            regime=regime,
                            price=price,
                            stop_loss=0.0,
                            reason="shock_to_trend_short_hedge_long_grid",
                            metadata={"hedge_contracts": pos.contracts},
                        )
                    )
        return signals

    def _user_4h_signal(
        self,
        symbol: str,
        regime: Regime,
        features: MarketFeatures,
        candles_5m: list[list[float]],
    ) -> TradeSignal | None:
        df = ohlcv_frame(candles_5m)
        if len(df) < 35 or features.atr_1h <= 0:
            return None
        price = float(df.close.iloc[-1])
        previous = df.iloc[-2]
        current = df.iloc[-1]
        macd_line, signal_line, _ = macd(df.close)
        ema20_5m = ema(df.close, 20)
        rsi_5m = rsi(df.close, 14)
        last_rsi = float(rsi_5m.iloc[-1])
        ema_now = float(ema20_5m.iloc[-1])
        ema_prev = float(ema20_5m.iloc[-4])
        crossed_up = crossed_above(macd_line, signal_line)
        crossed_down = crossed_below(macd_line, signal_line)
        recent_pullback_down = recent_down_candle(df)
        recent_pullback_up = recent_up_candle(df)
        trend_up_aligned = self._multi_timeframe_aligned(df, PositionSide.LONG)
        trend_down_aligned = self._multi_timeframe_aligned(df, PositionSide.SHORT)
        volatility_policy = self._volatility_policy(price, features, df)
        trend_long_near_breakout = (
            price >= features.range_high_4h - volatility_policy.breakout_retrace_atr * features.atr_1h
        )
        trend_short_near_breakout = (
            price <= features.range_low_4h + volatility_policy.breakout_retrace_atr * features.atr_1h
        )
        long_momentum_positive = bool(macd_line.iloc[-1] >= signal_line.iloc[-1])
        short_momentum_positive = bool(macd_line.iloc[-1] <= signal_line.iloc[-1])

        if regime == Regime.SHOCK:
            midpoint = (features.range_high_4h + features.range_low_4h) / 2
            if price < midpoint and crossed_up:
                stop = self._cap_stop(price, features.range_low_4h, PositionSide.LONG)
                take_profit = price + self._reward(price, stop, self.shock_reward_risk)
                opportunity = score_opportunity(
                    symbol=symbol,
                    regime=regime,
                    side=PositionSide.LONG,
                    checks={
                        "regime_direction": True,
                        "pullback": price < midpoint,
                        "confirmation_candle": float(current.close) > float(current.open),
                        "momentum_cross": crossed_up,
                        "momentum_positive": long_momentum_positive,
                        "price_location": price > float(previous.low),
                        "rsi_quality": last_rsi <= 52,
                        "not_chasing": True,
                        "range_position": price > features.range_low_4h,
                    },
                    reward_risk=abs(take_profit - price) / abs(price - stop) if price != stop else 0.0,
                    volatility_tier=volatility_policy.tier,
                )
                return self._entry_signal(
                    symbol,
                    Side.BUY,
                    PositionSide.LONG,
                    Regime.SHOCK,
                    price,
                    stop,
                    take_profit,
                    "user_4h_shock_long_reversal",
                    {
                        **opportunity_metadata(opportunity),
                        "risk_multiplier": max(self.defensive_risk_multiplier * 0.5, opportunity.risk_multiplier),
                    },
                )
            if price > midpoint and crossed_down:
                stop = self._cap_stop(price, features.range_high_4h, PositionSide.SHORT)
                take_profit = price - self._reward(price, stop, self.shock_reward_risk)
                opportunity = score_opportunity(
                    symbol=symbol,
                    regime=regime,
                    side=PositionSide.SHORT,
                    checks={
                        "regime_direction": True,
                        "pullback": price > midpoint,
                        "confirmation_candle": float(current.close) < float(current.open),
                        "momentum_cross": crossed_down,
                        "momentum_positive": short_momentum_positive,
                        "price_location": price < float(previous.high),
                        "rsi_quality": last_rsi >= 48,
                        "not_chasing": True,
                        "range_position": price < features.range_high_4h,
                    },
                    reward_risk=abs(price - take_profit) / abs(stop - price) if price != stop else 0.0,
                    volatility_tier=volatility_policy.tier,
                )
                return self._entry_signal(
                    symbol,
                    Side.SELL,
                    PositionSide.SHORT,
                    Regime.SHOCK,
                    price,
                    stop,
                    take_profit,
                    "user_4h_shock_short_reversal",
                    {
                        **opportunity_metadata(opportunity),
                        "risk_multiplier": max(self.defensive_risk_multiplier * 0.5, opportunity.risk_multiplier),
                    },
                )

        trend_long_rsi_quality = 40 <= last_rsi <= 66
        trend_long_opportunity = score_opportunity(
            symbol=symbol,
            regime=regime,
            side=PositionSide.LONG,
            checks={
                "regime_direction": regime == Regime.TREND_LONG,
                "multi_timeframe": trend_up_aligned,
                "one_hour_trend": features.ema20_1h >= features.ema60_1h,
                "four_hour_trend": trend_long_near_breakout,
                "pullback": recent_pullback_down,
                "confirmation_candle": float(current.close) > float(current.open),
                "momentum_cross": crossed_up,
                "momentum_positive": long_momentum_positive,
                "price_location": price >= ema_now * 0.998,
                "ema_slope": ema_now >= ema_prev,
                "rsi_quality": trend_long_rsi_quality,
                "not_chasing": last_rsi <= 70,
            },
            penalties={
                "extreme_volatility": volatility_policy.tier == "EXTREME",
                "counter_ema": features.ema20_1h < features.ema60_1h,
            },
            reward_risk=self.trend_reward_risk,
            volatility_tier=volatility_policy.tier,
            min_score=88,
        )
        if (
            regime == Regime.TREND_LONG
            and trend_long_opportunity.allow_trade
            and (not volatility_policy.require_multi_timeframe or trend_up_aligned)
            and (not volatility_policy.require_near_breakout or trend_long_near_breakout)
            and recent_pullback_down
            and float(current.close) > float(current.open)
            and price >= ema_now * 0.998
            and trend_long_rsi_quality
            and crossed_up
            and self._sol_allowed(symbol, PositionSide.LONG, regime, trend_long_opportunity.score, features)
        ):
            raw_stop = self._trend_stop(price, features.previous_1h_low, features.atr_1h, PositionSide.LONG, volatility_policy)
            stop = self._cap_stop(price, raw_stop, PositionSide.LONG, volatility_policy)
            stop = sol_structure_stop(symbol, PositionSide.LONG, price, stop)
            reward_risk = self.trend_reward_risk + (0.4 if trend_long_opportunity.score >= 88 else 0.2 if trend_long_opportunity.score >= 78 else 0.0)
            return self._entry_signal(
                symbol,
                Side.BUY,
                PositionSide.LONG,
                regime,
                price,
                stop,
                price + self._reward(price, stop, reward_risk),
                "user_4h_trend_long_pullback_confirmed",
                {
                    "trailing_gap_pct": self.trailing_gap_pct,
                    "min_trailing_activate_r": self.min_trailing_activate_r,
                    **opportunity_metadata(trend_long_opportunity),
                    "risk_multiplier": max(
                        self.trend_long_risk_multiplier * volatility_policy.risk_multiplier,
                        trend_long_opportunity.risk_multiplier,
                    ),
                    "volatility_tier": volatility_policy.tier,
                },
            )

        trend_short_rsi_quality = 34 <= last_rsi <= 60
        trend_short_opportunity = score_opportunity(
            symbol=symbol,
            regime=regime,
            side=PositionSide.SHORT,
            checks={
                "regime_direction": regime == Regime.TREND_SHORT,
                "multi_timeframe": trend_down_aligned,
                "one_hour_trend": features.ema20_1h <= features.ema60_1h,
                "four_hour_trend": trend_short_near_breakout and features.last_4h_close < features.prev_4h_close,
                "pullback": recent_pullback_up,
                "confirmation_candle": float(current.close) < float(current.open),
                "momentum_cross": crossed_down,
                "momentum_positive": short_momentum_positive,
                "price_location": price <= ema_now * 1.002,
                "ema_slope": ema_now <= ema_prev,
                "rsi_quality": trend_short_rsi_quality,
                "not_chasing": last_rsi >= 24,
            },
            penalties={
                "extreme_volatility": volatility_policy.tier == "EXTREME",
                "counter_ema": features.ema20_1h > features.ema60_1h,
                "overextended": price < features.range_low_4h * 0.985,
            },
            reward_risk=self.trend_reward_risk,
            volatility_tier=volatility_policy.tier,
            min_score=88,
        )
        if (
            self.enable_trend_short
            and regime == Regime.TREND_SHORT
            and trend_short_opportunity.allow_trade
            and (not volatility_policy.require_multi_timeframe or trend_down_aligned)
            and (not volatility_policy.require_near_breakout or trend_short_near_breakout)
            and features.ema20_1h <= features.ema60_1h
            and features.last_4h_close < features.prev_4h_close
            and features.last_4h_close <= features.range_low_4h * 1.01
            and price < features.range_low_4h * 1.003
            and recent_pullback_up
            and float(current.close) < float(current.open)
            and price <= ema_now * 1.002
            and trend_short_rsi_quality
            and crossed_down
            and self._sol_allowed(symbol, PositionSide.SHORT, regime, trend_short_opportunity.score, features)
        ):
            raw_stop = self._trend_stop(price, features.previous_1h_high, features.atr_1h, PositionSide.SHORT, volatility_policy)
            stop = self._cap_stop(price, raw_stop, PositionSide.SHORT, volatility_policy)
            stop = sol_structure_stop(symbol, PositionSide.SHORT, price, stop)
            reward_risk = self.trend_reward_risk + (0.4 if trend_short_opportunity.score >= 88 else 0.2 if trend_short_opportunity.score >= 78 else 0.0)
            return self._entry_signal(
                symbol,
                Side.SELL,
                PositionSide.SHORT,
                regime,
                price,
                stop,
                price - self._reward(price, stop, reward_risk),
                "user_4h_trend_short_pullback_confirmed",
                {
                    "trailing_gap_pct": self.trailing_gap_pct,
                    "min_trailing_activate_r": self.min_trailing_activate_r,
                    **opportunity_metadata(trend_short_opportunity),
                    "risk_multiplier": max(
                        self.trend_short_risk_multiplier * volatility_policy.risk_multiplier,
                        trend_short_opportunity.risk_multiplier,
                    ),
                    "volatility_tier": volatility_policy.tier,
                },
            )

        signal = self._user_4h_shock_trend_signal(
            symbol,
            regime,
            features,
            df,
            price,
            current,
            crossed_up,
            crossed_down,
            recent_pullback_down,
            recent_pullback_up,
            trend_up_aligned,
            trend_down_aligned,
            long_momentum_positive,
            short_momentum_positive,
            ema_now,
            ema_prev,
            last_rsi,
            volatility_policy,
        )
        return signal

    def _user_4h_shock_trend_signal(
        self,
        symbol: str,
        regime: Regime,
        features: MarketFeatures,
        df,
        price: float,
        current,
        crossed_up: bool,
        crossed_down: bool,
        recent_pullback_down: bool,
        recent_pullback_up: bool,
        trend_up_aligned: bool,
        trend_down_aligned: bool,
        long_momentum_positive: bool,
        short_momentum_positive: bool,
        ema_now: float,
        ema_prev: float,
        last_rsi: float,
        volatility_policy,
    ) -> TradeSignal | None:
        shock_trend_up_rsi_quality = 42 <= last_rsi <= 66
        recent_5h_position = self._recent_range_position(df, price, bars=60)
        local_top_without_impulse = recent_5h_position >= 0.94 and features.ret_24h < 0.008
        shock_trend_up_risk_throttle = 1.0
        if features.close_position_72h >= 0.75 and features.ret_72h <= 0:
            shock_trend_up_risk_throttle *= 0.70
        if recent_5h_position >= 0.90 and features.ret_24h < 0.008:
            shock_trend_up_risk_throttle *= 0.70
        shock_trend_up_opportunity = score_opportunity(
            symbol=symbol,
            regime=regime,
            side=PositionSide.LONG,
            checks={
                "regime_direction": regime == Regime.SHOCK_TREND_UP,
                "multi_timeframe": trend_up_aligned,
                "one_hour_trend": features.ema20_1h >= features.ema60_1h,
                "four_hour_trend": features.last_4h_close > features.prev_4h_close,
                "pullback": recent_pullback_down,
                "confirmation_candle": float(current.close) > float(current.open),
                "momentum_cross": crossed_up,
                "momentum_positive": long_momentum_positive,
                "price_location": price >= ema_now * 0.998 and price >= features.ema20_1h * 0.997,
                "ema_slope": ema_now >= ema_prev,
                "rsi_quality": shock_trend_up_rsi_quality,
                "not_chasing": last_rsi <= 68 and not local_top_without_impulse,
                "range_position": features.close_position_72h <= 0.92,
            },
            penalties={
                "directional_conflict": self._directional_breakout_active(features)
                and not (features.ret_72h > 0 and features.ret_24h > -0.01),
                "rsi_extreme": last_rsi > 72,
                "counter_ema": features.ema20_1h < features.ema60_1h,
                "overextended": features.close_position_72h > 0.96,
            },
            reward_risk=self.trend_reward_risk,
            volatility_tier=volatility_policy.tier,
            min_score=88,
        )
        if (
            regime == Regime.SHOCK_TREND_UP
            and shock_trend_up_opportunity.allow_trade
            and trend_up_aligned
            and features.ema20_1h >= features.ema60_1h
            and price >= features.ema20_1h * 0.997
            and features.last_4h_close > features.prev_4h_close
            and recent_pullback_down
            and float(current.close) > float(current.open)
            and crossed_up
            and price >= ema_now * 0.998
            and ema_now >= ema_prev
            and shock_trend_up_rsi_quality
            and not local_top_without_impulse
            and self._sol_allowed(symbol, PositionSide.LONG, regime, shock_trend_up_opportunity.score, features)
        ):
            stop = self._cap_stop(price, features.current_4h_low, PositionSide.LONG)
            stop = sol_structure_stop(symbol, PositionSide.LONG, price, stop)
            reward_risk = self.trend_reward_risk + (
                0.4 if shock_trend_up_opportunity.score >= 88 else 0.2 if shock_trend_up_opportunity.score >= 78 else 0.0
            )
            return self._entry_signal(
                symbol,
                Side.BUY,
                PositionSide.LONG,
                regime,
                price,
                stop,
                price + self._reward(price, stop, reward_risk),
                "user_4h_shock_trend_up_pullback_confirmed",
                {
                    "trailing_gap_pct": self.trailing_gap_pct,
                    "min_trailing_activate_r": self.min_trailing_activate_r,
                    "breakeven_activate_r": 0.75,
                    "breakeven_buffer_pct": 0.0003,
                    "risk_throttle": shock_trend_up_risk_throttle,
                    **opportunity_metadata(shock_trend_up_opportunity),
                    "risk_multiplier": max(
                        self.shock_trend_risk_multiplier,
                        shock_trend_up_opportunity.risk_multiplier,
                    ),
                    "volatility_tier": volatility_policy.tier,
                    "close_position_72h": round(features.close_position_72h, 4),
                    "ret_24h": round(features.ret_24h, 6),
                    "ret_72h": round(features.ret_72h, 6),
                    "recent_5h_position": round(recent_5h_position, 4),
                },
            )

        shock_trend_down_rsi_quality = 35 <= last_rsi <= 58
        shock_trend_down_range_ok = features.close_position_72h >= (
            0.18 if symbol.startswith("SOL/") else 0.08
        )
        shock_trend_down_momentum_ok = (
            features.ret_24h <= -0.003 or features.close_position_72h >= 0.30
            if symbol.startswith("SOL/")
            else True
        )
        low_range_short = features.close_position_72h < 0.35
        shock_trend_down_rebound_ready = (
            not low_range_short
            or (
                last_rsi >= 45
                and price >= ema_now * 0.998
                and self._timeframe_aligned(self._aggregate_bars(df, 3), PositionSide.SHORT, min_bars=12)
            )
        )
        shock_trend_down_risk_throttle = 1.0
        if features.close_position_72h < 0.20:
            shock_trend_down_risk_throttle *= 0.30
        elif features.close_position_72h < 0.30:
            shock_trend_down_risk_throttle *= 0.45
        elif features.close_position_72h < 0.35:
            shock_trend_down_risk_throttle *= 0.65
        if features.close_position_72h < 0.30 and features.ret_72h <= -0.05:
            shock_trend_down_risk_throttle *= 0.65
        if features.close_position_72h < 0.35 and features.ret_24h <= -0.018:
            shock_trend_down_risk_throttle *= 0.75
        if volatility_policy.tier == "HIGH":
            shock_trend_down_risk_throttle *= 0.85
        elif volatility_policy.tier == "EXTREME":
            shock_trend_down_risk_throttle *= 0.65
        shock_trend_down_opportunity = score_opportunity(
            symbol=symbol,
            regime=regime,
            side=PositionSide.SHORT,
            checks={
                "regime_direction": regime == Regime.SHOCK_TREND_DOWN,
                "multi_timeframe": trend_down_aligned,
                "one_hour_trend": features.ema20_1h <= features.ema60_1h,
                "four_hour_trend": features.last_4h_close < features.prev_4h_close,
                "pullback": recent_pullback_up,
                "confirmation_candle": float(current.close) < float(current.open),
                "momentum_cross": crossed_down,
                "momentum_positive": short_momentum_positive,
                "price_location": price <= ema_now * 1.002 and price <= features.ema20_1h * 1.003,
                "ema_slope": ema_now <= ema_prev,
                "rsi_quality": shock_trend_down_rsi_quality,
                "not_chasing": last_rsi >= 24,
                "range_position": shock_trend_down_range_ok,
                "low_range_rebound": shock_trend_down_rebound_ready,
            },
            penalties={
                "directional_conflict": self._directional_breakout_active(features)
                and not (features.ret_72h < 0 and features.ret_24h < 0.01),
                "rsi_extreme": last_rsi < 20,
                "counter_ema": features.ema20_1h > features.ema60_1h,
                "overextended": features.close_position_72h < (0.18 if symbol.startswith("SOL/") else 0.04),
            },
            reward_risk=self.trend_reward_risk,
            volatility_tier=volatility_policy.tier,
            min_score=96,
        )
        if (
            regime == Regime.SHOCK_TREND_DOWN
            and shock_trend_down_opportunity.allow_trade
            and shock_trend_down_opportunity.score >= 96
            and trend_down_aligned
            and features.ema20_1h <= features.ema60_1h
            and price <= features.ema20_1h * 1.003
            and features.last_4h_close < features.prev_4h_close
            and recent_pullback_up
            and float(current.close) < float(current.open)
            and crossed_down
            and price <= ema_now * 1.002
            and ema_now <= ema_prev
            and shock_trend_down_rsi_quality
            and shock_trend_down_range_ok
            and shock_trend_down_momentum_ok
            and shock_trend_down_rebound_ready
            and self._sol_allowed(symbol, PositionSide.SHORT, regime, shock_trend_down_opportunity.score, features)
        ):
            stop = self._cap_stop(price, features.current_4h_high, PositionSide.SHORT)
            stop = sol_structure_stop(symbol, PositionSide.SHORT, price, stop)
            reward_risk = self.trend_reward_risk + (
                0.4
                if shock_trend_down_opportunity.score >= 88
                else 0.2
                if shock_trend_down_opportunity.score >= 78
                else 0.0
            )
            return self._entry_signal(
                symbol,
                Side.SELL,
                PositionSide.SHORT,
                regime,
                price,
                stop,
                price - self._reward(price, stop, reward_risk),
                "user_4h_shock_trend_down_pullback_confirmed",
                {
                    "trailing_gap_pct": self.trailing_gap_pct,
                    "min_trailing_activate_r": max(self.min_trailing_activate_r, 0.85 if low_range_short else 1.2),
                    "breakeven_activate_r": 0.65 if low_range_short else 0.85,
                    "breakeven_buffer_pct": 0.0003,
                    "risk_throttle": shock_trend_down_risk_throttle,
                    **opportunity_metadata(shock_trend_down_opportunity),
                    "risk_multiplier": max(
                        self.shock_trend_down_risk_multiplier,
                        shock_trend_down_opportunity.risk_multiplier,
                    ),
                    "volatility_tier": volatility_policy.tier,
                    "close_position_72h": round(features.close_position_72h, 4),
                    "ret_24h": round(features.ret_24h, 6),
                    "ret_72h": round(features.ret_72h, 6),
                },
            )
        return None

    @staticmethod
    def _recent_range_position(df, price: float, bars: int) -> float:
        recent = df.tail(bars)
        if recent.empty:
            return 0.5
        high = float(recent.high.max())
        low = float(recent.low.min())
        if high <= low:
            return 0.5
        return (price - low) / (high - low)

    def _entry_signal(
        self,
        symbol: str,
        side: Side,
        position_side: PositionSide,
        regime: Regime,
        price: float,
        stop: float,
        take_profit: float,
        reason: str,
        metadata: dict[str, object],
        signal_type: SignalType = SignalType.ENTER_TREND,
    ) -> TradeSignal:
        return TradeSignal(
            symbol=symbol,
            signal_type=signal_type,
            side=side,
            position_side=position_side,
            regime=regime,
            price=price,
            stop_loss=stop,
            take_profit=take_profit,
            reason=reason,
            metadata=metadata,
        )

    def _sol_allowed(
        self,
        symbol: str,
        side: PositionSide,
        regime: Regime,
        score: int,
        features: MarketFeatures,
    ) -> bool:
        return sol_trade_allowed(
            symbol=symbol,
            side=side,
            regime=regime,
            score=score,
            close_position_72h=features.close_position_72h,
            ret_24h=features.ret_24h,
            ret_72h=features.ret_72h,
            range_72h=features.range_72h,
        )

    def _directional_breakout_active(self, features: MarketFeatures) -> bool:
        wide_directional = abs(features.ret_72h) >= 0.04 and features.range_72h >= 0.07
        short_directional = abs(features.ret_24h) >= 0.03 and features.range_24h >= 0.035
        return wide_directional or short_directional

    def _trend_signal(
        self,
        symbol: str,
        regime: Regime,
        features: MarketFeatures,
        candles_5m: list[list[float]],
    ) -> TradeSignal | None:
        df = ohlcv_frame(candles_5m)
        if len(df) < 60 or features.atr_1h <= 0:
            return None
        ema20_5m = ema(df.close, 20)
        rsi_5m = rsi(df.close, 14)
        macd_line, signal_line, _ = macd(df.close)
        price = float(df.close.iloc[-1])
        last_rsi = float(rsi_5m.iloc[-1])
        volatility_policy = self._volatility_policy(price, features, df)

        if regime == Regime.TREND_LONG:
            near_breakout = price >= features.range_high_4h - volatility_policy.breakout_retrace_atr * features.atr_1h
            momentum_positive = bool(macd_line.iloc[-1] >= signal_line.iloc[-1])
            rsi_quality = 40 <= last_rsi <= 64
            pullback = (
                (not volatility_policy.require_near_breakout or near_breakout)
                and (not volatility_policy.require_multi_timeframe or self._multi_timeframe_aligned(df, PositionSide.LONG))
                and price >= float(ema20_5m.iloc[-1])
                and rsi_quality
            )
            opportunity = score_opportunity(
                symbol=symbol,
                regime=regime,
                side=PositionSide.LONG,
                checks={
                    "regime_direction": True,
                    "multi_timeframe": self._multi_timeframe_aligned(df, PositionSide.LONG),
                    "one_hour_trend": features.ema20_1h >= features.ema60_1h,
                    "four_hour_trend": near_breakout,
                    "pullback": recent_down_candle(df),
                    "confirmation_candle": float(df.close.iloc[-1]) > float(df.open.iloc[-1]),
                    "momentum_cross": crossed_above(macd_line, signal_line),
                    "momentum_positive": momentum_positive,
                    "price_location": price >= float(ema20_5m.iloc[-1]),
                    "ema_slope": float(ema20_5m.iloc[-1]) >= float(ema20_5m.iloc[-4]),
                    "rsi_quality": rsi_quality,
                    "not_chasing": last_rsi <= 68,
                },
                penalties={"extreme_volatility": volatility_policy.tier == "EXTREME"},
                reward_risk=2.5,
                volatility_tier=volatility_policy.tier,
                min_score=88,
            )
            if (
                pullback
                and opportunity.allow_trade
                and crossed_above(macd_line, signal_line)
                and sol_trade_allowed(
                    symbol=symbol,
                    side=PositionSide.LONG,
                    regime=regime,
                    score=opportunity.score,
                    close_position_72h=features.close_position_72h,
                    ret_24h=features.ret_24h,
                    ret_72h=features.ret_72h,
                    range_72h=features.range_72h,
                )
            ):
                raw_stop = max(float(df.low.iloc[-12:].min()), price - 1.2 * features.atr_1h)
                raw_stop = self._trend_stop(price, raw_stop, features.atr_1h, PositionSide.LONG, volatility_policy)
                stop = self._cap_stop(price, raw_stop, PositionSide.LONG, volatility_policy)
                stop = sol_structure_stop(symbol, PositionSide.LONG, price, stop)
                return TradeSignal(
                    symbol=symbol,
                    signal_type=SignalType.ENTER_TREND,
                    side=Side.BUY,
                    position_side=PositionSide.LONG,
                    regime=regime,
                    price=price,
                    stop_loss=stop,
                    take_profit=price + self._reward(price, stop, 2.5),
                    reason="trend_long_pullback_macd_cross",
                    metadata={"volatility_tier": volatility_policy.tier, **opportunity_metadata(opportunity)},
                )

        if regime == Regime.TREND_SHORT:
            near_breakout = price <= features.range_low_4h + volatility_policy.breakout_retrace_atr * features.atr_1h
            momentum_positive = bool(macd_line.iloc[-1] <= signal_line.iloc[-1])
            rsi_quality = 36 <= last_rsi <= 58
            pullback = (
                (not volatility_policy.require_near_breakout or near_breakout)
                and (not volatility_policy.require_multi_timeframe or self._multi_timeframe_aligned(df, PositionSide.SHORT))
                and price <= float(ema20_5m.iloc[-1])
                and rsi_quality
            )
            opportunity = score_opportunity(
                symbol=symbol,
                regime=regime,
                side=PositionSide.SHORT,
                checks={
                    "regime_direction": True,
                    "multi_timeframe": self._multi_timeframe_aligned(df, PositionSide.SHORT),
                    "one_hour_trend": features.ema20_1h <= features.ema60_1h,
                    "four_hour_trend": near_breakout,
                    "pullback": recent_up_candle(df),
                    "confirmation_candle": float(df.close.iloc[-1]) < float(df.open.iloc[-1]),
                    "momentum_cross": crossed_below(macd_line, signal_line),
                    "momentum_positive": momentum_positive,
                    "price_location": price <= float(ema20_5m.iloc[-1]),
                    "ema_slope": float(ema20_5m.iloc[-1]) <= float(ema20_5m.iloc[-4]),
                    "rsi_quality": rsi_quality,
                    "not_chasing": last_rsi >= 28,
                },
                penalties={"extreme_volatility": volatility_policy.tier == "EXTREME"},
                reward_risk=2.5,
                volatility_tier=volatility_policy.tier,
                min_score=88,
            )
            if (
                pullback
                and opportunity.allow_trade
                and crossed_below(macd_line, signal_line)
                and sol_trade_allowed(
                    symbol=symbol,
                    side=PositionSide.SHORT,
                    regime=regime,
                    score=opportunity.score,
                    close_position_72h=features.close_position_72h,
                    ret_24h=features.ret_24h,
                    ret_72h=features.ret_72h,
                    range_72h=features.range_72h,
                )
            ):
                raw_stop = min(float(df.high.iloc[-12:].max()), price + 1.2 * features.atr_1h)
                raw_stop = self._trend_stop(price, raw_stop, features.atr_1h, PositionSide.SHORT, volatility_policy)
                stop = self._cap_stop(price, raw_stop, PositionSide.SHORT, volatility_policy)
                stop = sol_structure_stop(symbol, PositionSide.SHORT, price, stop)
                return TradeSignal(
                    symbol=symbol,
                    signal_type=SignalType.ENTER_TREND,
                    side=Side.SELL,
                    position_side=PositionSide.SHORT,
                    regime=regime,
                    price=price,
                    stop_loss=stop,
                    take_profit=price - self._reward(price, stop, 2.5),
                    reason="trend_short_pullback_macd_cross",
                    metadata={"volatility_tier": volatility_policy.tier, **opportunity_metadata(opportunity)},
                )
        return None

    def _shock_trend_signal(
        self,
        symbol: str,
        regime: Regime,
        features: MarketFeatures,
        candles_5m: list[list[float]],
    ) -> TradeSignal | None:
        df = ohlcv_frame(candles_5m)
        if len(df) < 60:
            return None
        macd_line, signal_line, _ = macd(df.close)
        ema20_5m = ema(df.close, 20)
        rsi_5m = rsi(df.close, 14)
        price = float(df.close.iloc[-1])
        current = df.iloc[-1]
        recent_pullback_down = bool((df.close.iloc[-4:-1] < df.open.iloc[-4:-1]).any())
        recent_pullback_up = bool((df.close.iloc[-4:-1] > df.open.iloc[-4:-1]).any())
        ema_now = float(ema20_5m.iloc[-1])
        ema_prev = float(ema20_5m.iloc[-4])
        last_rsi = float(rsi_5m.iloc[-1])
        current_4h = df.iloc[-48:]
        frame_4h = self._aggregate_bars(df, 48)
        last_4h_up = len(frame_4h) >= 2 and float(frame_4h.close.iloc[-1]) > float(frame_4h.close.iloc[-2])
        last_4h_down = len(frame_4h) >= 2 and float(frame_4h.close.iloc[-1]) < float(frame_4h.close.iloc[-2])
        trend_up_aligned = self._multi_timeframe_aligned(df, PositionSide.LONG)
        trend_down_aligned = self._multi_timeframe_aligned(df, PositionSide.SHORT)
        long_momentum_positive = bool(macd_line.iloc[-1] >= signal_line.iloc[-1])
        short_momentum_positive = bool(macd_line.iloc[-1] <= signal_line.iloc[-1])

        long_rsi_quality = 42 <= last_rsi <= 66
        long_opportunity = score_opportunity(
            symbol=symbol,
            regime=regime,
            side=PositionSide.LONG,
            checks={
                "regime_direction": regime == Regime.SHOCK_TREND_UP,
                "multi_timeframe": trend_up_aligned,
                "one_hour_trend": features.ema20_1h >= features.ema60_1h,
                "four_hour_trend": last_4h_up,
                "pullback": recent_pullback_down,
                "confirmation_candle": float(current.close) > float(current.open),
                "momentum_cross": crossed_above(macd_line, signal_line),
                "momentum_positive": long_momentum_positive,
                "price_location": price >= ema_now * 0.998 and price >= features.ema20_1h * 0.997,
                "ema_slope": ema_now >= ema_prev,
                "rsi_quality": long_rsi_quality,
                "not_chasing": last_rsi <= 68,
            },
            penalties={
                "rsi_extreme": last_rsi > 72,
                "counter_ema": features.ema20_1h < features.ema60_1h,
            },
            reward_risk=self.trend_reward_risk,
            volatility_tier="NORMAL",
            min_score=88,
        )
        if (
            regime == Regime.SHOCK_TREND_UP
            and long_opportunity.allow_trade
            and trend_up_aligned
            and features.ema20_1h >= features.ema60_1h
            and price >= features.ema20_1h * 0.997
            and last_4h_up
            and recent_pullback_down
            and float(current.close) > float(current.open)
            and crossed_above(macd_line, signal_line)
            and price >= ema_now * 0.998
            and ema_now >= ema_prev
            and long_rsi_quality
            and sol_trade_allowed(
                symbol=symbol,
                side=PositionSide.LONG,
                regime=regime,
                score=long_opportunity.score,
                close_position_72h=features.close_position_72h,
                ret_24h=features.ret_24h,
                ret_72h=features.ret_72h,
                range_72h=features.range_72h,
            )
        ):
            stop = self._cap_stop(price, float(current_4h.low.min()), PositionSide.LONG)
            stop = sol_structure_stop(symbol, PositionSide.LONG, price, stop)
            reward_risk = self.trend_reward_risk + (0.4 if long_opportunity.score >= 88 else 0.2 if long_opportunity.score >= 78 else 0.0)
            return TradeSignal(
                symbol=symbol,
                signal_type=SignalType.ENTER_TREND,
                side=Side.BUY,
                position_side=PositionSide.LONG,
                regime=regime,
                price=price,
                stop_loss=stop,
                take_profit=price + self._reward(price, stop, reward_risk),
                reason="shock_trend_up_pullback_confirmed",
                metadata=opportunity_metadata(long_opportunity),
            )

        short_rsi_quality = 34 <= last_rsi <= 58
        short_opportunity = score_opportunity(
            symbol=symbol,
            regime=regime,
            side=PositionSide.SHORT,
            checks={
                "regime_direction": regime == Regime.SHOCK_TREND_DOWN,
                "multi_timeframe": trend_down_aligned,
                "one_hour_trend": features.ema20_1h <= features.ema60_1h,
                "four_hour_trend": last_4h_down,
                "pullback": recent_pullback_up,
                "confirmation_candle": float(current.close) < float(current.open),
                "momentum_cross": crossed_below(macd_line, signal_line),
                "momentum_positive": short_momentum_positive,
                "price_location": price <= ema_now * 1.002 and price <= features.ema20_1h * 1.003,
                "ema_slope": ema_now <= ema_prev,
                "rsi_quality": short_rsi_quality,
                "not_chasing": last_rsi >= 26,
            },
            penalties={
                "rsi_extreme": last_rsi < 22,
                "counter_ema": features.ema20_1h > features.ema60_1h,
            },
            reward_risk=self.trend_reward_risk,
            volatility_tier="NORMAL",
            min_score=88,
        )
        if (
            regime == Regime.SHOCK_TREND_DOWN
            and short_opportunity.allow_trade
            and trend_down_aligned
            and features.ema20_1h <= features.ema60_1h
            and price <= features.ema20_1h * 1.003
            and last_4h_down
            and recent_pullback_up
            and float(current.close) < float(current.open)
            and crossed_below(macd_line, signal_line)
            and price <= ema_now * 1.002
            and ema_now <= ema_prev
            and short_rsi_quality
            and sol_trade_allowed(
                symbol=symbol,
                side=PositionSide.SHORT,
                regime=regime,
                score=short_opportunity.score,
                close_position_72h=features.close_position_72h,
                ret_24h=features.ret_24h,
                ret_72h=features.ret_72h,
                range_72h=features.range_72h,
            )
        ):
            stop = self._cap_stop(price, float(current_4h.high.max()), PositionSide.SHORT)
            stop = sol_structure_stop(symbol, PositionSide.SHORT, price, stop)
            reward_risk = self.trend_reward_risk + (0.4 if short_opportunity.score >= 88 else 0.2 if short_opportunity.score >= 78 else 0.0)
            return TradeSignal(
                symbol=symbol,
                signal_type=SignalType.ENTER_TREND,
                side=Side.SELL,
                position_side=PositionSide.SHORT,
                regime=regime,
                price=price,
                stop_loss=stop,
                take_profit=price - self._reward(price, stop, reward_risk),
                reason="shock_trend_down_pullback_confirmed",
                metadata=opportunity_metadata(short_opportunity),
            )
        return None

    def _multi_timeframe_aligned(self, df_5m, side: PositionSide) -> bool:
        frame_15m = self._aggregate_bars(df_5m, 3)
        frame_1h = self._aggregate_bars(df_5m, 12)
        return (
            self._timeframe_aligned(df_5m, side, min_bars=30)
            and self._timeframe_aligned(frame_15m, side, min_bars=12)
            and self._timeframe_aligned(frame_1h, side, min_bars=6)
        )

    def _aggregate_bars(self, frame, group_size: int):
        groups = []
        rows = frame.tail((len(frame) // group_size) * group_size)
        for start in range(0, len(rows), group_size):
            chunk = rows.iloc[start : start + group_size]
            if chunk.empty:
                continue
            groups.append(
                [
                    float(chunk.ts.iloc[-1]),
                    float(chunk.open.iloc[0]),
                    float(chunk.high.max()),
                    float(chunk.low.min()),
                    float(chunk.close.iloc[-1]),
                    float(chunk.volume.sum()),
                ]
            )
        return ohlcv_frame(groups)

    def _timeframe_aligned(self, frame, side: PositionSide, min_bars: int) -> bool:
        if len(frame) < min_bars:
            return False
        closes = frame.close.astype(float)
        ema_line = ema(closes, min(20, max(3, len(closes) // 2)))
        ema_now = float(ema_line.iloc[-1])
        ema_prev = float(ema_line.iloc[-min(4, len(ema_line))])
        last_close = float(closes.iloc[-1])
        reference = float(closes.iloc[-min(4, len(closes))])
        recent_return = (last_close - reference) / reference if reference else 0.0
        if side == PositionSide.LONG:
            return last_close >= ema_now and ema_now >= ema_prev and recent_return >= -0.003
        return last_close <= ema_now and ema_now <= ema_prev and recent_return <= 0.003

    def _grid_signal(
        self,
        symbol: str,
        features: MarketFeatures,
        candles_5m: list[list[float]],
    ) -> TradeSignal | None:
        df = ohlcv_frame(candles_5m)
        if len(df) < 35 or features.atr_1h <= 0:
            return None
        midpoint = (features.range_high_4h + features.range_low_4h) / 2
        range_width = features.range_high_4h - features.range_low_4h
        long_zone_low = features.range_low_4h + 0.20 * range_width
        long_zone_high = features.range_low_4h + 0.45 * range_width
        short_zone_low = features.range_low_4h + 0.55 * range_width
        short_zone_high = features.range_low_4h + 0.80 * range_width
        price = float(df.close.iloc[-1])
        macd_line, signal_line, _ = macd(df.close)
        rsi_5m = rsi(df.close, 14)
        last_rsi_5m = float(rsi_5m.iloc[-1])
        atr_5m = float(atr(df.high, df.low, df.close).iloc[-1]) or features.atr_1h

        if (
            long_zone_low <= price <= min(midpoint, long_zone_high)
            and features.rsi_1h < 35
            and last_rsi_5m < 48
            and crossed_above(macd_line, signal_line)
        ):
            stop = price - 1.0 * atr_5m
            stop = self._cap_stop(price, stop, PositionSide.LONG)
            reward = self._reward(price, stop, self.shock_reward_risk)
            return TradeSignal(
                symbol=symbol,
                signal_type=SignalType.ENTER_GRID,
                side=Side.BUY,
                position_side=PositionSide.LONG,
                regime=Regime.SHOCK,
                price=price,
                stop_loss=stop,
                take_profit=price + reward,
                reason="shock_grid_long_inner_reversal",
                metadata={"grid_layers": [1.0, 0.5, 0.5], "grid_spacing_atr": [1.2, 1.8]},
            )

        if (
            max(midpoint, short_zone_low) <= price <= short_zone_high
            and features.rsi_1h > 65
            and last_rsi_5m > 52
            and crossed_below(macd_line, signal_line)
        ):
            stop = price + 1.0 * atr_5m
            stop = self._cap_stop(price, stop, PositionSide.SHORT)
            reward = self._reward(price, stop, self.shock_reward_risk)
            return TradeSignal(
                symbol=symbol,
                signal_type=SignalType.ENTER_GRID,
                side=Side.SELL,
                position_side=PositionSide.SHORT,
                regime=Regime.SHOCK,
                price=price,
                stop_loss=stop,
                take_profit=price - reward,
                reason="shock_grid_short_inner_reversal",
                metadata={"grid_layers": [1.0, 0.5, 0.5], "grid_spacing_atr": [1.2, 1.8]},
            )
        return None

    def _volatility_policy(self, price: float, features: MarketFeatures, df_5m):
        ret_24h = 0.0
        range_24h = 0.0
        if len(df_5m) >= 288:
            recent_24h = df_5m.iloc[-288:]
            open_24h = float(recent_24h.open.iloc[0])
            high_24h = float(recent_24h.high.max())
            low_24h = float(recent_24h.low.min())
            ret_24h = (price - open_24h) / open_24h if open_24h else 0.0
            range_24h = (high_24h - low_24h) / price if price else 0.0
        return build_volatility_policy(
            price=price,
            atr_1h=features.atr_1h,
            base_min_stop_loss_pct=self.min_stop_loss_pct,
            high_vol_min_stop_loss_pct=self.high_vol_min_stop_loss_pct,
            extreme_vol_min_stop_loss_pct=self.extreme_vol_min_stop_loss_pct,
            ret_24h=ret_24h,
            range_24h=range_24h,
            range_72h=features.range_amplitude_4h,
        )

    def _trend_stop(self, price: float, raw_stop: float, atr_1h: float, side: PositionSide, volatility_policy) -> float:
        if volatility_policy.stop_buffer_atr > 0:
            buffer = volatility_policy.stop_buffer_atr * atr_1h
            if side == PositionSide.LONG:
                return min(raw_stop - buffer, price - buffer)
            return max(raw_stop + buffer, price + buffer)
        return raw_stop

    def _cap_stop(self, price: float, raw_stop: float, side: PositionSide, volatility_policy=None) -> float:
        min_stop_loss_pct = volatility_policy.min_stop_loss_pct if volatility_policy else self.min_stop_loss_pct
        min_distance = price * min_stop_loss_pct
        max_distance = price * self.max_stop_loss_pct
        if side == PositionSide.LONG:
            stop = min(raw_stop, price - min_distance)
            return max(stop, price - max_distance)
        stop = max(raw_stop, price + min_distance)
        return min(stop, price + max_distance)

    def _reward(self, price: float, stop: float, reward_risk: float) -> float:
        return max(price * self.min_take_profit_pct, reward_risk * abs(price - stop))


def recent_down_candle(df) -> bool:
    if len(df) < 4:
        return False
    return bool((df.close.iloc[-4:-1] < df.open.iloc[-4:-1]).any())


def recent_up_candle(df) -> bool:
    if len(df) < 4:
        return False
    return bool((df.close.iloc[-4:-1] > df.open.iloc[-4:-1]).any())

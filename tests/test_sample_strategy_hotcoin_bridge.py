import json
import logging
import sys
import types

import pandas as pd

talib_module = types.ModuleType("talib")
talib_abstract = types.ModuleType("talib.abstract")
technical_module = types.ModuleType("technical")
qtpylib_module = types.ModuleType("technical.qtpylib")
freqtrade_enums = types.ModuleType("freqtrade.enums")
freqtrade_strategy = types.ModuleType("freqtrade.strategy")


class _RunMode:
    DRY_RUN = "dry_run"
    LIVE = "live"


class _Parameter:
    def __init__(self, *args, default=None, **kwargs):
        self.value = default


def _informative(*args, **kwargs):
    def decorator(func):
        return func

    return decorator


freqtrade_enums.RunMode = _RunMode
freqtrade_strategy.DecimalParameter = _Parameter
freqtrade_strategy.IntParameter = _Parameter
freqtrade_strategy.IStrategy = object
freqtrade_strategy.Trade = object
freqtrade_strategy.informative = _informative
technical_module.qtpylib = qtpylib_module
sys.modules.setdefault("talib", talib_module)
sys.modules.setdefault("talib.abstract", talib_abstract)
sys.modules.setdefault("technical", technical_module)
sys.modules.setdefault("technical.qtpylib", qtpylib_module)
sys.modules.setdefault("freqtrade.enums", freqtrade_enums)
sys.modules.setdefault("freqtrade.strategy", freqtrade_strategy)

from user_data.strategies.SampleStrategy import SampleStrategy


class ImmediateThread:
    def __init__(self, target, args=(), daemon=None):
        self.target = target
        self.args = args

    def start(self):
        self.target(*self.args)


def _strategy():
    strategy = object.__new__(SampleStrategy)
    strategy.config = {"runmode": "dry_run"}
    strategy._signal_email_cache = set()
    strategy._email_warned = False
    return strategy


def test_load_trade_execution_settings_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADE_EXECUTION_SETTINGS_PATH", str(tmp_path / "missing.json"))

    assert _strategy()._load_trade_execution_settings() == {}


def test_hotcoin_bridge_generates_dry_run_command(monkeypatch, tmp_path):
    settings_path = tmp_path / "trade_execution.json"
    settings_path.write_text(
        json.dumps(
            {
                "hotcoin_signal_bridge_enabled": True,
                "hotcoin_signal_execute": False,
                "hotcoin_order_amount": 2,
                "hotcoin_order_type": "market",
                "hotcoin_mode": "web",
                "hotcoin_adapter_path": "/freqtrade/scripts/hotcoin_adapter.py",
                "hotcoin_session_path": "/freqtrade/user_data/hotcoin_session.json",
                "hotcoin_atr_stop_mult": 2,
                "hotcoin_reward_risk_mult": 1.5,
            }
        )
    )
    monkeypatch.setenv("TRADE_EXECUTION_SETTINGS_PATH", str(settings_path))
    monkeypatch.setattr("user_data.strategies.SampleStrategy.threading.Thread", ImmediateThread)

    calls = []

    def fake_run(command, capture_output, text, timeout, env):
        calls.append(
            {
                "command": command,
                "env": env,
                "timeout": timeout,
            }
        )

        class Result:
            returncode = 0
            stdout = '{"dry_run": true}'
            stderr = ""

        return Result()

    monkeypatch.setattr("user_data.strategies.SampleStrategy.subprocess.run", fake_run)
    strategy = _strategy()
    last = pd.Series({"close": 100.0, "atr": 5.0})

    strategy._dispatch_hotcoin_signal("BTC/USDT:USDT", "long", last, "2026-05-12T08:00:00")

    assert len(calls) == 1
    command = calls[0]["command"]
    assert command[:4] == ["python", "/freqtrade/scripts/hotcoin_adapter.py", "order", "--mode"]
    assert "--yes" not in command
    assert command[command.index("--side") + 1] == "open_long"
    assert command[command.index("--amount") + 1] == "2"
    assert command[command.index("--stop-loss") + 1] == "90.00000000"
    assert command[command.index("--take-profit") + 1] == "115.00000000"
    assert calls[0]["env"]["HOTCOIN_SESSION_PATH"] == "/freqtrade/user_data/hotcoin_session.json"


def test_hotcoin_bridge_adds_yes_when_execute_enabled(monkeypatch, tmp_path):
    settings_path = tmp_path / "trade_execution.json"
    settings_path.write_text(
        json.dumps(
            {
                "hotcoin_signal_bridge_enabled": True,
                "hotcoin_signal_execute": True,
                "hotcoin_order_amount": 1,
                "hotcoin_order_type": "limit",
                "hotcoin_mode": "web",
                "hotcoin_adapter_path": "/adapter.py",
            }
        )
    )
    monkeypatch.setenv("TRADE_EXECUTION_SETTINGS_PATH", str(settings_path))
    monkeypatch.setattr("user_data.strategies.SampleStrategy.threading.Thread", ImmediateThread)

    calls = []

    def fake_run(command, capture_output, text, timeout, env):
        calls.append(command)

        class Result:
            returncode = 0
            stdout = "{}"
            stderr = ""

        return Result()

    monkeypatch.setattr("user_data.strategies.SampleStrategy.subprocess.run", fake_run)
    strategy = _strategy()
    last = pd.Series({"close": 100.0, "atr": 5.0})

    strategy._dispatch_hotcoin_signal("ETH/USDT:USDT", "short", last, "2026-05-12T08:00:00")

    command = calls[0]
    assert command[command.index("--side") + 1] == "open_short"
    assert command[command.index("--price") + 1] == "100.00000000"
    assert "--yes" in command


def test_hotcoin_bridge_disabled_does_not_spawn(monkeypatch, tmp_path):
    settings_path = tmp_path / "trade_execution.json"
    settings_path.write_text(json.dumps({"hotcoin_signal_bridge_enabled": False}))
    monkeypatch.setenv("TRADE_EXECUTION_SETTINGS_PATH", str(settings_path))

    def fail_thread(*args, **kwargs):
        raise AssertionError("Thread should not be created")

    monkeypatch.setattr("user_data.strategies.SampleStrategy.threading.Thread", fail_thread)
    _strategy()._dispatch_hotcoin_signal(
        "BTC/USDT:USDT", "long", pd.Series({"close": 100.0, "atr": 5.0}), "time"
    )


def test_entry_diagnostics_logs_blockers(caplog):
    strategy = _strategy()
    dataframe = pd.DataFrame(
        [
            {
                "date": pd.Timestamp("2026-05-12T08:00:00Z"),
                "close": 100.0,
                "ema20": 101.0,
                "ema50": 102.0,
                "ema200": 103.0,
                "rsi": 48.0,
                "adx": 15.0,
                "macd": -0.2,
                "macdsignal": 0.1,
                "macdhist": -0.3,
                "volume_ratio": 0.75,
                "volatility_ratio": 0.9,
                "atr_pct": 0.01,
                "trend_context_long": False,
                "trend_down_1h": False,
                "trend_down_15m": False,
                "trend_up_1h": False,
                "trend_up_15m": False,
                "range_market_1h": False,
                "regime_high_vol": False,
                "regime_low_vol": False,
                "regime_balanced": True,
            }
        ]
    )
    false_series = pd.Series([False])

    with caplog.at_level(logging.INFO):
        strategy._log_entry_diagnostics(
            dataframe,
            {"pair": "BTC/USDT:USDT"},
            pd.Series([22.0]),
            pd.Series([1.2]),
            pd.Series([34.0]),
            pd.Series([66.0]),
            false_series,
            false_series,
            false_series,
            false_series,
            false_series,
            false_series,
        )

    assert "Signal diagnostics BTC/USDT:USDT" in caplog.text
    assert "no_long_trend_context" in caplog.text
    assert "volume_ratio 0.75<=1.05" in caplog.text


def test_emit_signal_email_ignores_non_entry(monkeypatch):
    strategy = _strategy()
    calls = []
    monkeypatch.setattr(strategy, "_send_email_async", lambda *args: calls.append(args))
    dataframe = pd.DataFrame(
        [
            {
                "date": pd.Timestamp("2026-05-12T08:00:00Z"),
                "exit_long": 1,
                "close": 100.0,
            }
        ]
    )

    strategy._emit_signal_email(dataframe, {"pair": "BTC/USDT:USDT"}, "exit", "long")

    assert calls == []

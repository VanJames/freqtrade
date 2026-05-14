import sys
import types


_STUBBED_MODULES = [
    "talib",
    "talib.abstract",
    "technical",
    "technical.qtpylib",
    "freqtrade.enums",
    "freqtrade.strategy",
]
_PREVIOUS_MODULES = {name: sys.modules.get(name) for name in _STUBBED_MODULES}

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
from user_data.strategies.SampleStrategyActive import SampleStrategyActive
from user_data.strategies.SampleStrategyPullbackLong import SampleStrategyPullbackLong
from user_data.strategies.SampleStrategyPullbackShort import SampleStrategyPullbackShort
from user_data.strategies.SampleStrategyScalp import SampleStrategyScalp

for _module_name, _previous_module in _PREVIOUS_MODULES.items():
    if _previous_module is None:
        sys.modules.pop(_module_name, None)
    else:
        sys.modules[_module_name] = _previous_module


def test_active_variant_lowers_entry_gates():
    assert SampleStrategyActive.adx_threshold.value < SampleStrategy.adx_threshold.value
    assert SampleStrategyActive.volume_ratio_min.value < SampleStrategy.volume_ratio_min.value
    assert SampleStrategyActive.ai_edge_threshold.value < SampleStrategy.ai_edge_threshold.value
    assert SampleStrategyActive.short_rsi_trigger.value < SampleStrategy.short_rsi_trigger.value


def test_scalp_variant_is_more_active_than_active_variant():
    assert SampleStrategyScalp.adx_threshold.value < SampleStrategyActive.adx_threshold.value
    assert SampleStrategyScalp.volume_ratio_min.value < SampleStrategyActive.volume_ratio_min.value
    assert SampleStrategyScalp.ai_edge_threshold.value < SampleStrategyActive.ai_edge_threshold.value
    assert SampleStrategyScalp.short_rsi_trigger.value < SampleStrategyActive.short_rsi_trigger.value
    assert SampleStrategyScalp.risk_per_trade.value < SampleStrategyActive.risk_per_trade.value


def test_pullback_variants_are_direction_specific():
    assert SampleStrategyPullbackShort.can_short is True
    assert SampleStrategyPullbackLong.can_short is False
    assert SampleStrategyPullbackShort.minimal_roi["0"] < SampleStrategy.minimal_roi["0"]
    assert SampleStrategyPullbackLong.minimal_roi["0"] < SampleStrategy.minimal_roi["0"]

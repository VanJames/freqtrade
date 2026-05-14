from scripts import hot_switch_backtester


def test_strategy_config_path_reuses_original_when_pairs_unchanged(tmp_path):
    config_path = tmp_path / "config.json"
    config_data = {
        "exchange": {
            "pair_whitelist": ["BTC/USDT:USDT", "ETH/USDT:USDT"],
        }
    }
    config_path.write_text("{}\n")

    result = hot_switch_backtester.strategy_config_path(
        config_path=config_path,
        config_data=config_data,
        pair_selection={},
        strategy="SampleStrategy",
        config_dir=tmp_path / "configs",
    )

    assert result == config_path


def test_strategy_config_path_writes_temp_config_for_strategy_pairs(tmp_path):
    config_path = tmp_path / "config.json"
    config_data = {
        "exchange": {
            "pair_whitelist": ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"],
        },
        "freqai": {
            "feature_parameters": {
                "include_corr_pairlist": ["BTC/USDT:USDT", "ETH/USDT:USDT"]
            }
        },
    }
    config_path.write_text("{}\n")
    pair_selection = {
        "strategies": {
            "AIGeneratedLongTrendContinuationRunner2xSelective": {
                "allowed_pairs": ["BTC/USDT:USDT", "SOL/USDT:USDT"]
            }
        }
    }

    result = hot_switch_backtester.strategy_config_path(
        config_path=config_path,
        config_data=config_data,
        pair_selection=pair_selection,
        strategy="AIGeneratedLongTrendContinuationRunner2xSelective",
        config_dir=tmp_path / "configs",
    )

    written = result.read_text()
    assert result != config_path
    assert '"BTC/USDT:USDT"' in written
    assert '"SOL/USDT:USDT"' in written
    assert '"ETH/USDT:USDT"' not in written


def test_strategy_config_path_writes_temp_config_for_unique_freqai_identifier(tmp_path):
    config_path = tmp_path / "config.json"
    config_data = {
        "exchange": {
            "pair_whitelist": ["BTC/USDT:USDT", "ETH/USDT:USDT"],
        },
        "freqai": {
            "identifier": "sample-hybrid-ai",
            "feature_parameters": {
                "include_corr_pairlist": ["BTC/USDT:USDT", "ETH/USDT:USDT"]
            },
        },
    }
    config_path.write_text("{}\n")

    result = hot_switch_backtester.strategy_config_path(
        config_path=config_path,
        config_data=config_data,
        pair_selection={},
        strategy="AIGeneratedLongTrendContinuationRunner2xTrendActive",
        config_dir=tmp_path / "configs",
    )

    written = result.read_text()
    assert result != config_path
    assert '"identifier": "sample-hybrid-ai-aigeneratedlongtrendcontinuationrunner2xtrendactive"' in written

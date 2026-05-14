import py_compile
import json

from scripts import ai_strategy_lab


def make_args(tmp_path, **overrides):
    strategy_path = overrides.pop("strategy_path", tmp_path / "strategies")
    output = overrides.pop("output", tmp_path / "candidates.json")
    ledger = overrides.pop("ledger", tmp_path / "candidates.jsonl")
    config = overrides.pop("config", tmp_path / "config.json")
    if not config.exists():
        config.write_text("{}")
    defaults = {
        "config": str(config),
        "strategy_path": str(strategy_path),
        "output": str(output),
        "ledger": str(ledger),
        "kronos_forecast": str(tmp_path / "missing_kronos.json"),
        "formal_pool": str(tmp_path / "missing_formal_pool.json"),
        "kronos_return_threshold_pct": 0.12,
        "snapshot_limit": 10,
        "max_candidates": 4,
        "exploration_max_candidates": 8,
        "exploration_stale_runs_threshold": 3,
        "use_llm": False,
        "advisor_model": "deepseek-chat",
        "advisor_base_url": "",
        "advisor_timeout": 60,
        "max_preserved_strategies": 8,
        "preserved_prune_stale_runs_threshold": 6,
    }
    defaults.update(overrides)
    return type("Args", (), defaults)()


def test_normalize_candidate_sanitizes_to_safe_strategy_class():
    candidate = ai_strategy_lab.normalize_candidate(
        {
            "name": "../bad short strategy!",
            "archetype": "pullback",
            "side": "short",
            "risk_profile": "aggressive",
            "stoploss": -0.5,
            "volume_min": 99,
        },
        1,
        "llm",
    )

    assert candidate.class_name.startswith("AIGenerated")
    assert "/" not in candidate.class_name
    assert candidate.archetype == "pullback"
    assert candidate.side == "short"
    assert candidate.stoploss == -0.12
    assert candidate.volume_min == 3.5


def test_rendered_strategy_compiles(tmp_path):
    candidate = ai_strategy_lab.normalize_candidate(
        {
            "name": "short_pullback_test",
            "archetype": "pullback",
            "side": "short",
            "risk_profile": "conservative",
        },
        1,
        "heuristic",
    )
    path = tmp_path / f"{candidate.class_name}.py"
    path.write_text(ai_strategy_lab.render_strategy(candidate))

    py_compile.compile(str(path), doraise=True)


def test_generate_candidates_writes_strategy_files(monkeypatch, tmp_path):
    strategy_path = tmp_path / "strategies"
    monkeypatch.setattr(
        ai_strategy_lab,
        "latest_market_snapshot",
        lambda config, limit: {
            "summary": {
                "pair_count": 3,
                "trend_up_count": 0,
                "trend_down_count": 3,
                "median_atr_pct": 0.003,
            }
        },
    )

    args = make_args(tmp_path, strategy_path=strategy_path, max_candidates=2)
    payload = ai_strategy_lab.generate_candidates(args)

    assert len(payload["strategy_names"]) == 2
    for name in payload["strategy_names"]:
        assert (strategy_path / f"{name}.py").exists()
    assert (tmp_path / "candidates.json").exists()
    assert (tmp_path / "candidates.jsonl").exists()


def test_generate_candidates_keeps_heuristic_candidates_when_llm_enabled(monkeypatch, tmp_path):
    monkeypatch.setattr(
        ai_strategy_lab,
        "latest_market_snapshot",
        lambda config, limit: {
            "summary": {
                "pair_count": 3,
                "trend_up_count": 3,
                "trend_down_count": 0,
                "median_atr_pct": 0.003,
            }
        },
    )
    monkeypatch.setattr(
        ai_strategy_lab,
        "llm_candidates",
        lambda *args, **kwargs: [
            {
                "name": "llm_short_test",
                "archetype": "trend",
                "side": "short",
                "risk_profile": "balanced",
            }
        ],
    )

    args = make_args(tmp_path, strategy_path=tmp_path / "strategies", max_candidates=8, use_llm=True)

    payload = ai_strategy_lab.generate_candidates(args)
    generated_by = {item["generated_by"] for item in payload["candidates"]}

    assert {"heuristic", "llm"} <= generated_by
    assert "AIGeneratedLongTrendContinuation" in payload["strategy_names"]
    assert "AIGeneratedLlmShortTest" in payload["strategy_names"]


def test_generate_candidates_preserves_previous_strategy_names(monkeypatch, tmp_path):
    strategy_path = tmp_path / "strategies"
    strategy_path.mkdir()
    survivor = strategy_path / "AIGeneratedSurvivor.py"
    survivor.write_text("class AIGeneratedSurvivor: pass\n")
    (tmp_path / "candidates.json").write_text('{"strategy_names": ["AIGeneratedSurvivor"]}')
    monkeypatch.setattr(
        ai_strategy_lab,
        "latest_market_snapshot",
        lambda config, limit: {
            "summary": {
                "pair_count": 3,
                "trend_up_count": 3,
                "trend_down_count": 0,
                "median_atr_pct": 0.001,
            }
        },
    )

    args = make_args(tmp_path, strategy_path=strategy_path, max_candidates=1)

    payload = ai_strategy_lab.generate_candidates(args)

    assert payload["generated_strategy_names"] == ["AIGeneratedLongTrendContinuation"]
    assert payload["preserved_strategy_names"] == ["AIGeneratedSurvivor"]
    assert "AIGeneratedSurvivor" in payload["strategy_names"]
    assert payload["preserved_retention"]["AIGeneratedSurvivor"]["stale_runs"] == 1


def test_generate_candidates_preserves_local_ai_strategy_files(monkeypatch, tmp_path):
    strategy_path = tmp_path / "strategies"
    strategy_path.mkdir()
    survivor = strategy_path / "AIGeneratedLocalRunner.py"
    survivor.write_text("class AIGeneratedLocalRunner: pass\n")
    monkeypatch.setattr(
        ai_strategy_lab,
        "latest_market_snapshot",
        lambda config, limit: {
            "summary": {
                "pair_count": 3,
                "trend_up_count": 3,
                "trend_down_count": 0,
                "median_atr_pct": 0.001,
            }
        },
    )

    args = make_args(tmp_path, strategy_path=strategy_path, max_candidates=1)

    payload = ai_strategy_lab.generate_candidates(args)

    assert "AIGeneratedLocalRunner" in payload["preserved_strategy_names"]
    assert "AIGeneratedLocalRunner" in payload["strategy_names"]


def test_generate_candidates_prunes_stale_preserved_strategy(monkeypatch, tmp_path):
    strategy_path = tmp_path / "strategies"
    strategy_path.mkdir()
    stale = strategy_path / "AIGeneratedStale.py"
    stale.write_text("class AIGeneratedStale: pass\n")
    output = tmp_path / "candidates.json"
    output.write_text(
        json.dumps(
            {
                "strategy_names": ["AIGeneratedStale"],
                "preserved_retention": {
                    "AIGeneratedStale": {
                        "stale_runs": 2,
                        "in_formal_pool": False,
                    }
                },
            }
        )
    )
    formal_pool = tmp_path / "formal_strategy_pool.json"
    formal_pool.write_text(json.dumps({"allowed_strategies": []}))
    monkeypatch.setattr(
        ai_strategy_lab,
        "latest_market_snapshot",
        lambda config, limit: {
            "summary": {
                "pair_count": 3,
                "trend_up_count": 3,
                "trend_down_count": 0,
                "median_atr_pct": 0.001,
            }
        },
    )

    payload = ai_strategy_lab.generate_candidates(
        make_args(
            tmp_path,
            strategy_path=strategy_path,
            output=output,
            formal_pool=formal_pool,
            max_candidates=1,
            preserved_prune_stale_runs_threshold=3,
        )
    )

    assert "AIGeneratedStale" not in payload["preserved_strategy_names"]
    assert payload["dropped_preserved_strategies"][0]["strategy"] == "AIGeneratedStale"
    assert payload["dropped_preserved_strategies"][0]["stale_runs"] == 3


def test_forecast_side_uses_average_predicted_return():
    assert (
        ai_strategy_lab.forecast_side(
            {"summary": {"market_bias": "hold", "avg_pred_return_pct": 0.3}},
            0.12,
        )
        == "long"
    )


def test_heuristic_candidates_expand_diverse_trend_pool():
    items = ai_strategy_lab.heuristic_candidates(
        {
            "summary": {
                "pair_count": 4,
                "trend_up_count": 3,
                "trend_down_count": 0,
                "median_atr_pct": 0.003,
                "median_bb_width": 0.02,
            }
        },
        {"summary": {"market_bias": "long"}},
        0.12,
    )

    names = {item["name"] for item in items}
    assert "long_trend_continuation" in names
    assert "long_trend_runner_active" in names
    assert "long_breakout_confirmation" in names


def test_select_diverse_candidates_prefers_unique_archetype_side_risk():
    candidates = [
        ai_strategy_lab.normalize_candidate(
            {"name": "trend_a", "archetype": "trend", "side": "long", "risk_profile": "balanced"},
            1,
            "heuristic",
        ),
        ai_strategy_lab.normalize_candidate(
            {"name": "trend_b", "archetype": "trend", "side": "long", "risk_profile": "balanced"},
            2,
            "heuristic",
        ),
        ai_strategy_lab.normalize_candidate(
            {"name": "pullback_a", "archetype": "pullback", "side": "long", "risk_profile": "conservative"},
            3,
            "heuristic",
        ),
        ai_strategy_lab.normalize_candidate(
            {"name": "breakout_a", "archetype": "breakout", "side": "long", "risk_profile": "balanced"},
            4,
            "heuristic",
        ),
    ]

    selected = ai_strategy_lab.select_diverse_candidates(candidates, max_candidates=3)

    assert len(selected) == 3
    assert len({(c.archetype, c.side, c.risk_profile) for c in selected}) == 3


def test_build_exploration_state_activates_after_stale_formal_pool_runs():
    previous_payload = {
        "formal_pool_summary": {
            "ai_allowed_strategies": ["AIGeneratedLongTrendContinuationRunner2xSelective"],
        },
        "exploration_state": {
            "stale_runs": 2,
        },
    }
    current_formal = {
        "exists": True,
        "ai_allowed_strategies": ["AIGeneratedLongTrendContinuationRunner2xSelective"],
    }

    state = ai_strategy_lab.build_exploration_state(
        previous_payload,
        current_formal,
        base_max_candidates=4,
        exploration_max_candidates=8,
        stale_runs_threshold=3,
    )

    assert state["stale_runs"] == 3
    assert state["exploration_active"] is True
    assert state["effective_max_candidates"] == 8
    assert state["reason"] == "no_new_formal_pool_entry"


def test_generate_candidates_records_formal_pool_feedback(monkeypatch, tmp_path):
    strategy_path = tmp_path / "strategies"
    strategy_path.mkdir()
    output = tmp_path / "candidates.json"
    output.write_text(
        json.dumps(
            {
                "formal_pool_summary": {
                    "ai_allowed_strategies": ["AIGeneratedLongTrendContinuationRunner2xSelective"],
                },
                "exploration_state": {
                    "stale_runs": 2,
                },
            }
        )
    )
    formal_pool = tmp_path / "formal_strategy_pool.json"
    formal_pool.write_text(
        json.dumps(
            {
                "allowed_strategies": ["AIGeneratedLongTrendContinuationRunner2xSelective"],
                "candidate_count": 1,
                "required_window_days": [30, 60],
            }
        )
    )
    monkeypatch.setattr(
        ai_strategy_lab,
        "latest_market_snapshot",
        lambda config, limit: {
            "summary": {
                "pair_count": 3,
                "trend_up_count": 3,
                "trend_down_count": 0,
                "median_atr_pct": 0.003,
            }
        },
    )

    payload = ai_strategy_lab.generate_candidates(
        make_args(
            tmp_path,
            strategy_path=strategy_path,
            output=output,
            formal_pool=formal_pool,
            max_candidates=2,
            exploration_max_candidates=5,
            exploration_stale_runs_threshold=3,
        )
    )

    assert payload["exploration_state"]["exploration_active"] is True
    assert payload["exploration_state"]["effective_max_candidates"] == 5
    assert payload["selection_mode"] == "diverse_greedy_exploration"
    assert payload["formal_pool_summary"]["exists"] is True

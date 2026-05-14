import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scripts import health_check


def test_analyze_signal_diagnostics_counts_blockers():
    records = [
        {
            "pair": "BTC/USDT:USDT",
            "time": "2026-05-14T10:00:00+00:00",
            "enter_long": False,
            "enter_short": False,
            "long_blockers": ["adx<=23", "volume<=1.5"],
            "short_blockers": ["hold_gate"],
        },
        {
            "pair": "ETH/USDT:USDT",
            "time": "2026-05-14T09:55:00+00:00",
            "enter_long": True,
            "enter_short": False,
            "long_blockers": [],
            "short_blockers": ["hold_gate"],
        },
    ]

    result = health_check.analyze_signal_diagnostics(records)

    assert result["count"] == 2
    assert result["enter_long_true"] == 1
    assert result["enter_short_true"] == 0
    assert result["both_blocked"] == 1
    assert result["top_short_blockers"][0] == ("hold_gate", 2)


def test_check_strategy_alignment_warns_on_hold_and_mismatch():
    result = health_check.check_strategy_alignment(
        {"strategy": "Runner2x"},
        {"direction_advisor_hold_policy": "hold"},
        {"recommended_strategy": "Selective"},
        {"recommended_strategy": "Selective", "final_action": "hold"},
        {"allowed_strategies": ["Selective"]},
    )

    assert result.status == "warn"
    assert "configured strategy is outside formal pool" in result.details["warnings"]
    assert "configured strategy differs from pair selection recommendation" in result.details["warnings"]
    assert "direction advisor currently blocks new entries with hold" in result.details["warnings"]


def test_check_formal_pool_warns_when_missing(tmp_path):
    result = health_check.check_formal_pool(tmp_path / "formal_strategy_pool.json", 180)

    assert result.status == "warn"
    assert "missing" in result.summary


def test_check_signal_diagnostics_freshness_warns_when_stale(tmp_path):
    path = tmp_path / "signal_diagnostics.jsonl"
    stale = (datetime.now(UTC) - timedelta(hours=4)).isoformat()
    path.write_text(json.dumps({"created_at": stale, "time": stale}) + "\n")

    result = health_check.check_signal_diagnostics_freshness(path, limit=10, stale_minutes=90)

    assert result.status == "warn"
    assert result.details["age_minutes"] > 90


def test_check_trade_db_reads_latest_closed_trade(tmp_path):
    db_path = tmp_path / "tradesv3.sqlite"
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        create table trades (
          pair text,
          strategy text,
          is_open integer,
          is_short integer,
          close_profit real,
          close_profit_abs real,
          close_date text
        )
        """
    )
    conn.execute(
        """
        insert into trades(pair, strategy, is_open, is_short, close_profit, close_profit_abs, close_date)
        values ('BTC/USDT:USDT', 'Runner2x', 0, 1, 0.01, 1.2, '2026-05-14T10:00:00+00:00')
        """
    )
    conn.commit()
    conn.close()

    result = health_check.check_trade_db(db_path)

    assert result.status == "ok"
    assert result.details["closed_trades"] == 1
    assert result.details["latest_closed_trade"]["strategy"] == "Runner2x"


def test_summarize_entry_pressure_includes_hold_and_blockers():
    signal_analysis = {
        "enter_long_true": 0,
        "enter_short_true": 0,
        "top_long_blockers": [("adx<=23", 5)],
        "top_short_blockers": [("hold_gate", 4)],
    }

    result = health_check.summarize_entry_pressure(signal_analysis, {"final_action": "hold"})

    assert "recent diagnostics show no passing long/short entries" in result
    assert "long blocker: adx<=23" in result
    assert "short blocker: hold_gate" in result
    assert "direction advisor is currently hold" in result


def test_check_core_logs_reports_researcher_errors(monkeypatch):
    def fake_logs(name: str, lines: int) -> list[str]:
        if name == "freqtrade":
            return [
                "2026-05-14 - Bot heartbeat. PID=1, version='2026.4', state='RUNNING'",
                "2026-05-14 - Direction advisor gate applied: action=hold reason=fresh",
            ]
        if name == "freqtrade-strategy-researcher":
            return [
                "Traceback (most recent call last):",
                "FileNotFoundError: /workspace/user_data/config.json",
            ]
        return []

    monkeypatch.setattr(health_check, "docker_logs_tail", fake_logs)

    results = health_check.check_core_logs(
        freqtrade_container="freqtrade",
        researcher_container="freqtrade-strategy-researcher",
        signal_analysis={
            "enter_long_true": 0,
            "enter_short_true": 0,
            "top_long_blockers": [],
            "top_short_blockers": [],
        },
        direction_payload={"final_action": "hold"},
        lines=100,
    )

    assert len(results) == 2
    assert results[0].name == "freqtrade_logs"
    assert results[0].status == "ok"
    assert results[1].name == "strategy_researcher_logs"
    assert results[1].status == "warn"
    assert "FileNotFoundError" in "\n".join(results[1].details["recent_errors"])


def test_check_strategy_alignment_only_flags_hold_when_hold_policy_blocks():
    result = health_check.check_strategy_alignment(
        {"strategy": "Runner2x"},
        {"direction_advisor_hold_policy": "normal"},
        {"recommended_strategy": "Runner2x"},
        {"recommended_strategy": "Runner2x", "final_action": "hold"},
        {"allowed_strategies": ["Runner2x"]},
    )

    assert result.status == "ok"
    assert "direction advisor currently blocks new entries with hold" not in result.details["warnings"]


def test_check_signal_path_sync_warns_on_mismatch(monkeypatch, tmp_path):
    path = tmp_path / "signal_diagnostics.jsonl"
    path.write_text('{"a":1}\n{"a":2}\n')

    monkeypatch.setattr(health_check, "docker_exec_output", lambda *_args, **_kwargs: (0, "100\n1"))

    result = health_check.check_signal_path_sync("freqtrade", path)

    assert result.status == "warn"
    assert "line count mismatch" in " ".join(result.details["warnings"])

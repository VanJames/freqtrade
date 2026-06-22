from trading_notice import cli


def base_env(monkeypatch):
    monkeypatch.setenv("TRADING_NOTICE_SKIP_DOTENV", "1")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_FROM", "alerts@example.test")


def test_cli_run_once_outputs_safe_status(monkeypatch, capsys):
    base_env(monkeypatch)
    seen = {}

    def fake_run_once(config, dry_run_email=False):
        from trading_notice.models import StrategyDecision

        seen["session_path"] = config.coinglass_session_path
        seen["heatmap_ranges"] = config.coinglass_heatmap_ranges
        seen["kline_check"] = config.kline_check_interval_seconds
        seen["price_move"] = config.liquidation_price_move_trigger
        seen["price_move_cooldown"] = config.liquidation_price_move_cooldown_seconds
        return StrategyDecision("c1", config.symbol, "no_trade", no_trade_reason="none")

    monkeypatch.setattr(cli, "run_once", fake_run_once)
    code = cli.main(
        [
            "run-once",
            "--symbol",
            "ETH/USDT",
            "--interval",
            "15m",
            "--recipients",
            "trader@example.test",
            "--coinglass-heatmap-url",
            "https://example.test/heatmap",
            "--coinglass-session-path",
            "/tmp/coinglass-session.json",
            "--coinglass-heatmap-ranges",
            "12 hour,24 hour,48 hour",
            "--kline-check-interval-seconds",
            "60",
            "--liquidation-price-move-trigger",
            "0.01",
            "--liquidation-price-move-cooldown-seconds",
            "900",
            "--dry-run-email",
        ]
    )

    assert code == 0
    assert seen["session_path"] == "/tmp/coinglass-session.json"
    assert seen["heatmap_ranges"] == ("12 hour", "24 hour", "48 hour")
    assert seen["kline_check"] == 60
    assert seen["price_move"] == 0.01
    assert seen["price_move_cooldown"] == 900
    assert "cycle_status=no_trade" in capsys.readouterr().out


def test_cli_run_once_reads_required_values_from_env(monkeypatch, capsys):
    base_env(monkeypatch)
    monkeypatch.setenv("TRADING_SYMBOL", "BTC/USDT")
    monkeypatch.setenv("ANALYSIS_INTERVAL", "15m")
    monkeypatch.setenv("RECIPIENTS", "trader@example.test")
    monkeypatch.setenv("COINGLASS_HEATMAP_URL", "https://example.test/heatmap")
    seen = {}

    def fake_run_once(config, dry_run_email=False):
        from trading_notice.models import StrategyDecision

        seen["symbol"] = config.symbol
        seen["dry_run_email"] = dry_run_email
        return StrategyDecision("c1", config.symbol, "no_trade", no_trade_reason="none")

    monkeypatch.setattr(cli, "run_once", fake_run_once)
    code = cli.main(["run-once", "--dry-run-email"])

    assert code == 0
    assert seen == {"symbol": "BTC/USDT", "dry_run_email": True}
    assert "cycle_status=no_trade" in capsys.readouterr().out


def test_cli_configuration_failure_uses_safe_exit(monkeypatch):
    monkeypatch.setenv("TRADING_NOTICE_SKIP_DOTENV", "1")
    for name in ("SMTP_HOST", "SMTP_PORT", "SMTP_FROM"):
        monkeypatch.delenv(name, raising=False)

    code = cli.main(
        [
            "run-once",
            "--symbol",
            "ETH/USDT",
            "--interval",
            "15m",
            "--recipients",
            "trader@example.test",
            "--coinglass-heatmap-url",
            "https://example.test/heatmap",
        ]
    )

    assert code == 1

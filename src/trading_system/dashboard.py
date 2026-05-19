from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from html import escape
from typing import Any

import asyncpg
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse


app = FastAPI(title="OKX Quant Dashboard")


def dsn() -> str:
    value = os.getenv("POSTGRES_DSN", "postgresql://postgres:postgres@localhost:5432/trading")
    return value.replace("postgresql+asyncpg://", "postgresql://", 1)


async def fetch_dashboard_data() -> dict[str, Any]:
    conn = await asyncpg.connect(dsn())
    try:
        latest_snapshot = await conn.fetchrow(
            """
            select snapshot_time, total_equity, active_hedging, serialized_memory
            from account_snapshots
            order by snapshot_time desc
            limit 1
            """
        )
        orders = await conn.fetch(
            """
            select order_id, symbol, regime_mode, initial_qty, maker_filled,
                   taker_twap_filled, fee_paid, created_at
            from order_tracks
            order by created_at desc
            limit 30
            """
        )
        order_count = await conn.fetchval("select count(*) from order_tracks")
    finally:
        await conn.close()

    return {
        "now": datetime.now(timezone.utc).isoformat(),
        "mode": {
            "dry_run": os.getenv("DRY_RUN", ""),
            "okx_demo": os.getenv("OKX_DEMO", ""),
            "symbols": os.getenv("SYMBOLS", ""),
            "llm_enabled": os.getenv("LLM_REGIME_REVIEW_ENABLED", ""),
            "llm_provider": os.getenv("LLM_REGIME_PROVIDER", ""),
            "llm_model": os.getenv("LLM_REGIME_MODEL", ""),
            "llm_cache_ttl_seconds": os.getenv("LLM_REGIME_REVIEW_CACHE_TTL_SECONDS", ""),
            "llm_min_interval_seconds": os.getenv("LLM_REGIME_REVIEW_MIN_INTERVAL_SECONDS", ""),
        },
        "latest_snapshot": normalize_row(latest_snapshot),
        "orders": [normalize_row(row) for row in orders],
        "order_count": int(order_count or 0),
    }


def normalize_row(row) -> dict[str, Any] | None:
    if row is None:
        return None
    result: dict[str, Any] = {}
    for key, value in dict(row).items():
        if isinstance(value, Decimal):
            result[key] = float(value)
        elif isinstance(value, datetime):
            result[key] = value.isoformat()
        elif key == "serialized_memory" and isinstance(value, str):
            result[key] = parse_memory(value)
        else:
            result[key] = value
    return result


def parse_memory(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


@app.get("/api/status")
async def api_status() -> JSONResponse:
    return JSONResponse(await fetch_dashboard_data())


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    data = await fetch_dashboard_data()
    return render_page(data)


def render_page(data: dict[str, Any]) -> str:
    snapshot = data["latest_snapshot"] or {}
    memory = snapshot.get("serialized_memory") or {}
    regimes = memory.get("regimes", {}) if isinstance(memory, dict) else {}
    hedge_locks = memory.get("hedge_locks", {}) if isinstance(memory, dict) else {}
    risk = memory.get("risk", {}) if isinstance(memory, dict) else {}
    orders = data["orders"]
    mode = data["mode"]

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="10">
  <title>OKX Quant Dashboard</title>
  <style>
    :root {{ color-scheme: dark; --bg:#0f1115; --panel:#171a21; --line:#2a2f3a; --text:#e8eaed; --muted:#9aa4b2; --good:#39d98a; --warn:#ffcc66; --bad:#ff6b6b; }}
    body {{ margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; background:var(--bg); color:var(--text); }}
    main {{ max-width:1180px; margin:0 auto; padding:24px; }}
    header {{ display:flex; justify-content:space-between; align-items:flex-start; gap:16px; margin-bottom:20px; }}
    h1 {{ margin:0; font-size:24px; letter-spacing:0; }}
    h2 {{ margin:0 0 12px; font-size:16px; color:var(--muted); font-weight:600; }}
    .grid {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; margin-bottom:16px; }}
    .panel {{ min-width:0; background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:16px; }}
    .metric {{ min-width:0; font-size:22px; font-weight:700; line-height:1.25; margin-top:6px; overflow-wrap:anywhere; word-break:break-word; }}
    .muted {{ color:var(--muted); font-size:13px; }}
    .status {{ display:inline-flex; padding:4px 8px; border-radius:999px; border:1px solid var(--line); font-size:12px; }}
    .live {{ color:var(--bad); border-color:rgba(255,107,107,.5); }}
    .demo {{ color:var(--warn); border-color:rgba(255,204,102,.5); }}
    .ok {{ color:var(--good); }}
    table {{ width:100%; border-collapse:collapse; font-size:13px; }}
    th, td {{ border-bottom:1px solid var(--line); padding:9px 8px; text-align:left; white-space:normal; overflow-wrap:anywhere; word-break:break-word; }}
    th {{ color:var(--muted); font-weight:600; }}
    .cards {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-bottom:16px; }}
    pre {{ overflow:auto; margin:0; font-size:12px; color:#c8d1dc; }}
    @media (max-width:900px) {{ .grid,.cards {{ grid-template-columns:1fr; }} header {{ flex-direction:column; }} }}
  </style>
</head>
<body>
<main>
  <header>
    <div>
      <h1>OKX Quant Dashboard</h1>
      <div class="muted">UTC {escape(str(data["now"]))} · 每 10 秒自动刷新</div>
    </div>
    <div>{mode_badge(mode)}</div>
  </header>

  <section class="grid">
    {metric("账户权益", fmt(snapshot.get("total_equity"), "USDT"))}
    {metric("订单总数", data["order_count"])}
    {metric("对冲状态", "ON" if snapshot.get("active_hedging") else "OFF")}
    {metric("交易品种", escape(str(mode.get("symbols") or "-")))}
  </section>

  <section class="cards">
    <div class="panel">
      <h2>当前行情</h2>
      {dict_table(regimes)}
    </div>
    <div class="panel">
      <h2>风控状态</h2>
      {dict_table(risk)}
    </div>
  </section>

  <section class="panel" style="margin-bottom:16px">
    <h2>最近订单</h2>
    {orders_table(orders)}
  </section>

  <section class="cards">
    <div class="panel">
      <h2>锁仓状态</h2>
      <pre>{escape(json.dumps(hedge_locks, ensure_ascii=False, indent=2))}</pre>
    </div>
    <div class="panel">
      <h2>运行配置</h2>
      {dict_table(mode)}
    </div>
  </section>
</main>
</body>
</html>"""


def mode_badge(mode: dict[str, Any]) -> str:
    if str(mode.get("dry_run")).lower() == "false" and str(mode.get("okx_demo")).lower() == "false":
        return '<span class="status live">REAL TRADING</span>'
    if str(mode.get("okx_demo")).lower() == "true":
        return '<span class="status demo">DEMO</span>'
    return '<span class="status ok">DRY RUN</span>'


def metric(label: str, value: Any, unit: str = "") -> str:
    suffix = f" {escape(unit)}" if unit else ""
    return f'<div class="panel"><div class="muted">{escape(label)}</div><div class="metric">{escape(str(value))}{suffix}</div></div>'


def fmt(value: Any, unit: str = "") -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.2f} {unit}".strip()
    return f"{value} {unit}".strip()


def dict_table(values: dict[str, Any]) -> str:
    if not values:
        return '<div class="muted">暂无数据</div>'
    rows = "".join(
        f"<tr><th>{escape(str(key))}</th><td>{escape(short_value(value))}</td></tr>"
        for key, value in values.items()
    )
    return f"<table>{rows}</table>"


def short_value(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.4f}"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def orders_table(orders: list[dict[str, Any]]) -> str:
    if not orders:
        return '<div class="muted">暂无订单记录</div>'
    rows = ""
    for order in orders:
        rows += (
            "<tr>"
            f"<td>{escape(str(order.get('created_at', '')))}</td>"
            f"<td>{escape(str(order.get('symbol', '')))}</td>"
            f"<td>{escape(str(order.get('regime_mode', '')))}</td>"
            f"<td>{escape(str(order.get('initial_qty', '')))}</td>"
            f"<td>{escape(str(order.get('maker_filled', '')))}</td>"
            f"<td>{escape(str(order.get('taker_twap_filled', '')))}</td>"
            f"<td>{escape(str(order.get('fee_paid', '')))}</td>"
            f"<td>{escape(str(order.get('order_id', '')))}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>时间</th><th>品种</th><th>行情</th><th>数量</th>"
        "<th>Maker</th><th>TWAP</th><th>手续费</th><th>订单ID</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )

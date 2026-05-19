from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from html import escape
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import asyncpg
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from trading_system.config import Settings
from trading_system.runtime_config import RUNTIME_FIELDS, runtime_defaults, validate_runtime_config


app = FastAPI(title="OKX Quant Dashboard")


def dashboard_tz() -> ZoneInfo:
    name = os.getenv("DASHBOARD_TIMEZONE", "Asia/Shanghai")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def dsn() -> str:
    value = os.getenv("POSTGRES_DSN", "postgresql://postgres:postgres@localhost:5432/trading")
    return value.replace("postgresql+asyncpg://", "postgresql://", 1)


async def fetch_dashboard_data() -> dict[str, Any]:
    settings = Settings()
    defaults = runtime_defaults(settings)
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
        runtime_rows = await conn.fetch("select setting_key, setting_value, updated_at from runtime_settings")
    finally:
        await conn.close()

    runtime_config = defaults | {
        str(row["setting_key"]): float(row["setting_value"])
        for row in runtime_rows
        if str(row["setting_key"]) in defaults
    }

    return {
        "now": datetime.now(timezone.utc).isoformat(),
        "local_now": format_local_time(datetime.now(timezone.utc).isoformat()),
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
        "runtime_config": runtime_config,
        "runtime_fields": [
            {
                "key": field.key,
                "label": field.label,
                "min": field.min_value,
                "max": field.max_value,
                "step": field.step,
                "percent": field.percent,
                "boolean": field.boolean,
            }
            for field in RUNTIME_FIELDS
        ],
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


@app.post("/api/runtime-config")
async def api_update_runtime_config(payload: dict[str, Any] = Body(...)) -> JSONResponse:
    try:
        values = validate_runtime_config(payload, Settings())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    conn = await asyncpg.connect(dsn())
    try:
        async with conn.transaction():
            for key, value in values.items():
                await conn.execute(
                    """
                    insert into runtime_settings(setting_key, setting_value, updated_at)
                    values($1, $2, now())
                    on conflict(setting_key)
                    do update set setting_value = excluded.setting_value, updated_at = excluded.updated_at
                    """,
                    key,
                    str(value),
                )
    finally:
        await conn.close()
    return JSONResponse({"ok": True, "runtime_config": values})


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    data = await fetch_dashboard_data()
    return render_page(data)


def render_page(data: dict[str, Any]) -> str:
    snapshot = data["latest_snapshot"] or {}
    memory = snapshot.get("serialized_memory") or {}
    regimes = memory.get("regimes", {}) if isinstance(memory, dict) else {}
    prices = memory.get("prices", {}) if isinstance(memory, dict) else {}
    regime_checked_at = memory.get("regime_checked_at", {}) if isinstance(memory, dict) else {}
    hedge_locks = memory.get("hedge_locks", {}) if isinstance(memory, dict) else {}
    risk = memory.get("risk", {}) if isinstance(memory, dict) else {}
    orders = data["orders"]
    mode = data["mode"]
    runtime_config = data["runtime_config"]
    runtime_fields = data["runtime_fields"]

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
    .form-grid {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; }}
    label {{ display:flex; flex-direction:column; gap:6px; color:var(--muted); font-size:13px; }}
    input {{ width:100%; box-sizing:border-box; border:1px solid var(--line); border-radius:6px; background:#10131a; color:var(--text); padding:10px 11px; font-size:14px; }}
    button {{ border:1px solid #3c76ff; background:#2258d4; color:white; border-radius:6px; padding:10px 14px; font-weight:700; cursor:pointer; }}
    .actions {{ display:flex; align-items:center; gap:12px; margin-top:14px; }}
    pre {{ overflow:auto; margin:0; font-size:12px; color:#c8d1dc; }}
    @media (max-width:900px) {{ .grid,.cards,.form-grid {{ grid-template-columns:1fr; }} header {{ flex-direction:column; }} }}
  </style>
</head>
<body>
<main>
  <header>
    <div>
      <h1>OKX Quant Dashboard</h1>
      <div class="muted">本地时间 {escape(str(data["local_now"]))} · 每 10 秒自动刷新</div>
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
      {market_table(mode, regimes, prices, regime_checked_at)}
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

  <section class="panel" style="margin-bottom:16px">
    <h2>动态风控配置</h2>
    {runtime_config_form(runtime_fields, runtime_config)}
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
<script>
  const form = document.getElementById("runtime-config-form");
  if (form) {{
    form.addEventListener("submit", async (event) => {{
      event.preventDefault();
      const status = document.getElementById("runtime-config-status");
      const payload = Object.fromEntries(new FormData(form).entries());
      form.querySelectorAll('input[type="checkbox"]').forEach((input) => {{
        payload[input.name] = input.checked ? "1" : "0";
      }});
      status.textContent = "保存中...";
      const response = await fetch("/api/runtime-config", {{
        method: "POST",
        headers: {{"Content-Type": "application/json"}},
        body: JSON.stringify(payload)
      }});
      if (response.ok) {{
        status.textContent = "已保存，实盘引擎下一轮自动应用";
      }} else {{
        const data = await response.json().catch(() => ({{detail: "保存失败"}}));
        status.textContent = data.detail || "保存失败";
      }}
    }});
  }}
</script>
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


def format_local_time(value: Any) -> str:
    if not value:
        return "-"
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value)
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return raw
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(dashboard_tz()).strftime("%Y-%m-%d %H:%M:%S %Z")


def market_table(
    mode: dict[str, Any],
    regimes: dict[str, Any],
    prices: dict[str, Any],
    regime_checked_at: dict[str, Any],
) -> str:
    symbols = [item.strip() for item in str(mode.get("symbols") or "").split(",") if item.strip()]
    if not symbols:
        symbols = sorted(set(regimes) | set(prices) | set(regime_checked_at))
    if not symbols:
        return '<div class="muted">暂无数据</div>'
    rows = ""
    for symbol in symbols:
        rows += (
            "<tr>"
            f"<td>{escape(symbol)}</td>"
            f"<td>{escape(format_price(prices.get(symbol)))}</td>"
            f"<td>{escape(str(regimes.get(symbol, '-')))}</td>"
            f"<td>{escape(format_local_time(regime_checked_at.get(symbol)))}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>品种</th><th>实时价格</th><th>行情判断</th><th>最近判断时间</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )


def format_price(value: Any) -> str:
    if value is None:
        return "-"
    try:
        price = float(value)
    except (TypeError, ValueError):
        return str(value)
    if price >= 1000:
        return f"{price:.2f}"
    if price >= 10:
        return f"{price:.4f}"
    return f"{price:.6f}"


def dict_table(values: dict[str, Any]) -> str:
    if not values:
        return '<div class="muted">暂无数据</div>'
    rows = "".join(
        f"<tr><th>{escape(str(key))}</th><td>{escape(short_value(value))}</td></tr>"
        for key, value in values.items()
    )
    return f"<table>{rows}</table>"


def runtime_config_form(fields: list[dict[str, Any]], values: dict[str, Any]) -> str:
    controls = []
    for field in fields:
        key = str(field["key"])
        hint = f"{field['min']} - {field['max']}"
        if field.get("percent"):
            hint += "，0.01=1%"
        input_type = "checkbox" if field.get("boolean") else "number"
        checked = " checked" if field.get("boolean") and float(values.get(key, 0.0) or 0.0) >= 1.0 else ""
        value_attr = " value=\"1\"" if field.get("boolean") else f" value=\"{escape(str(values.get(key, '')))}\""
        number_attrs = "" if field.get("boolean") else f" min=\"{field['min']}\" max=\"{field['max']}\" step=\"{field['step']}\""
        controls.append(
            "<label>"
            f"<span>{escape(str(field['label']))} <span class=\"muted\">{escape(hint)}</span></span>"
            f"<input name=\"{escape(key)}\" type=\"{input_type}\"{number_attrs}{value_attr}{checked}>"
            "</label>"
        )
    return (
        '<form id="runtime-config-form">'
        f'<div class="form-grid">{"".join(controls)}</div>'
        '<div class="actions"><button type="submit">保存配置</button>'
        '<span id="runtime-config-status" class="muted">修改后约 1 分钟内应用到实盘</span></div>'
        "</form>"
    )


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

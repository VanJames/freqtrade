from __future__ import annotations

import json
import os
import hmac
import hashlib
from datetime import datetime, timezone
from decimal import Decimal
from html import escape
from pathlib import Path
from urllib.parse import parse_qs
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import asyncpg
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

from trading_system.config import Settings
from trading_system.runtime_config import RUNTIME_FIELDS, runtime_defaults, validate_runtime_config


app = FastAPI(title="OKX Quant Dashboard")
FAVICON_PATH = Path(__file__).with_name("assets") / "favicon.ico"
REPORTS_DIR = Path(os.getenv("REPORTS_DIR", "reports"))
DASHBOARD_SESSION_COOKIE = "okx_quant_dashboard_session"


def dashboard_password() -> str:
    return os.getenv("DASHBOARD_PASSWORD", "9527")


def dashboard_session_secret() -> str:
    return os.getenv("DASHBOARD_SESSION_SECRET", dashboard_password())


def dashboard_session_token() -> str:
    payload = f"dashboard:{dashboard_password()}".encode("utf-8")
    return hmac.new(dashboard_session_secret().encode("utf-8"), payload, hashlib.sha256).hexdigest()


def dashboard_authenticated(request: Request) -> bool:
    token = request.cookies.get(DASHBOARD_SESSION_COOKIE, "")
    return hmac.compare_digest(token, dashboard_session_token())


def require_dashboard_auth(request: Request) -> None:
    if not dashboard_authenticated(request):
        raise HTTPException(status_code=401, detail="dashboard login required")


def dashboard_tz() -> ZoneInfo:
    name = os.getenv("DASHBOARD_TIMEZONE", "Asia/Shanghai")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def dsn() -> str:
    value = os.getenv("POSTGRES_DSN", "postgresql://postgres:postgres@localhost:5432/trading")
    return value.replace("postgresql+asyncpg://", "postgresql://", 1)


async def ensure_dashboard_schema(conn: asyncpg.Connection) -> None:
    await conn.execute(
        """
        create table if not exists order_tracks (
            order_id varchar(64) primary key,
            symbol varchar(32) not null,
            regime_mode varchar(20) not null,
            initial_qty numeric(18, 8) not null,
            maker_filled numeric(18, 8) not null default 0,
            taker_twap_filled numeric(18, 8) not null default 0,
            fee_paid numeric(18, 8) not null default 0,
            created_at timestamp with time zone not null
        )
        """
    )
    await conn.execute(
        """
        create table if not exists account_snapshots (
            snapshot_time timestamp with time zone primary key,
            total_equity numeric(18, 4) not null,
            active_hedging boolean not null default false,
            serialized_memory text not null
        )
        """
    )
    await conn.execute(
        """
        create table if not exists runtime_settings (
            setting_key varchar(64) primary key,
            setting_value text not null,
            updated_at timestamp with time zone not null
        )
        """
    )


async def fetch_dashboard_data() -> dict[str, Any]:
    settings = Settings()
    defaults = runtime_defaults(settings)
    conn = await asyncpg.connect(dsn())
    try:
        await ensure_dashboard_schema(conn)
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
        str(row["setting_key"]): str(row["setting_value"])
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
                "text": field.text,
                "description": field.description,
            }
            for field in RUNTIME_FIELDS
        ],
        "latest_tuning_report": latest_tuning_report(),
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
async def api_status(request: Request) -> JSONResponse:
    require_dashboard_auth(request)
    return JSONResponse(await fetch_dashboard_data())


@app.post("/api/runtime-config")
async def api_update_runtime_config(request: Request, payload: dict[str, Any] = Body(...)) -> JSONResponse:
    require_dashboard_auth(request)
    try:
        values = validate_runtime_config(payload, Settings())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    conn = await asyncpg.connect(dsn())
    try:
        await ensure_dashboard_schema(conn)
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


@app.get("/login", response_class=HTMLResponse)
async def login() -> str:
    return render_login_page()


@app.post("/login")
async def login_submit(request: Request) -> Response:
    body = (await request.body()).decode("utf-8")
    values = parse_qs(body)
    password = values.get("password", [""])[0]
    if not hmac.compare_digest(password, dashboard_password()):
        return HTMLResponse(render_login_page("密码错误"), status_code=401)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        DASHBOARD_SESSION_COOKIE,
        dashboard_session_token(),
        max_age=7 * 24 * 60 * 60,
        httponly=True,
        samesite="lax",
    )
    return response


@app.post("/logout")
async def logout() -> RedirectResponse:
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(DASHBOARD_SESSION_COOKIE)
    return response


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if not dashboard_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    data = await fetch_dashboard_data()
    return render_page(data)


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> FileResponse:
    return FileResponse(FAVICON_PATH, media_type="image/x-icon")


@app.get("/reports/{name}", include_in_schema=False)
async def report_file(request: Request, name: str) -> FileResponse:
    require_dashboard_auth(request)
    if "/" in name or "\\" in name or not name.endswith(".md"):
        raise HTTPException(status_code=404, detail="report not found")
    path = REPORTS_DIR / name
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="report not found")
    return FileResponse(path, media_type="text/markdown; charset=utf-8")


def render_page(data: dict[str, Any]) -> str:
    snapshot = data["latest_snapshot"] or {}
    memory = snapshot.get("serialized_memory") or {}
    regimes = memory.get("regimes", {}) if isinstance(memory, dict) else {}
    prices = memory.get("prices", {}) if isinstance(memory, dict) else {}
    regime_checked_at = memory.get("regime_checked_at", {}) if isinstance(memory, dict) else {}
    entry_diagnostics = memory.get("entry_diagnostics", {}) if isinstance(memory, dict) else {}
    hedge_locks = memory.get("hedge_locks", {}) if isinstance(memory, dict) else {}
    risk = memory.get("risk", {}) if isinstance(memory, dict) else {}
    engine_version = memory.get("engine_version", "-") if isinstance(memory, dict) else "-"
    snapshot_saved_at = memory.get("snapshot_saved_at") if isinstance(memory, dict) else None
    orders = data["orders"]
    mode = data["mode"]
    runtime_config = data["runtime_config"]
    runtime_fields = data["runtime_fields"]
    latest_tuning = data["latest_tuning_report"]
    overview_body = (
        '<section class="grid">'
        f'{metric("账户权益", fmt(snapshot.get("total_equity"), "USDT"))}'
        f'{metric("订单总数", data["order_count"])}'
        f'{metric("对冲状态", "ON" if snapshot.get("active_hedging") else "OFF")}'
        f'{metric("交易品种", str(mode.get("symbols") or "-"))}'
        f'{metric("快照时间", format_local_time(snapshot_saved_at or snapshot.get("snapshot_time")))}'
        f'{metric("引擎版本", str(engine_version or "-"))}'
        "</section>"
    )
    hedge_body = f'<pre>{escape(json.dumps(hedge_locks, ensure_ascii=False, indent=2))}</pre>'

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="10">
  <link rel="icon" href="/favicon.ico" sizes="32x32">
  <title>OKX Quant Dashboard</title>
  <style>
    :root {{ color-scheme: dark; --bg:#0f1115; --panel:#171a21; --line:#2a2f3a; --text:#e8eaed; --muted:#9aa4b2; --good:#39d98a; --warn:#ffcc66; --bad:#ff6b6b; }}
    body {{ margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; background:var(--bg); color:var(--text); }}
    main {{ max-width:1180px; margin:0 auto; padding:24px; }}
    header {{ display:flex; justify-content:space-between; align-items:flex-start; gap:16px; margin-bottom:20px; }}
    h1 {{ margin:0; font-size:24px; letter-spacing:0; }}
    h2 {{ margin:0; font-size:16px; color:var(--text); font-weight:700; }}
    .top-actions {{ display:flex; align-items:center; gap:10px; }}
    .grid {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; }}
    .panel {{ min-width:0; background:var(--panel); border:1px solid var(--line); border-radius:8px; }}
    details.panel {{ overflow:hidden; margin-bottom:16px; }}
    details.panel > summary {{ list-style:none; display:flex; align-items:center; justify-content:space-between; gap:12px; padding:16px; cursor:pointer; user-select:none; }}
    details.panel > summary::-webkit-details-marker {{ display:none; }}
    .panel-body {{ padding:0 16px 16px; }}
    .chevron {{ color:var(--muted); font-size:13px; }}
    details[open] .chevron::before {{ content:"收起"; }}
    details:not([open]) .chevron::before {{ content:"展开"; }}
    .metric-tile {{ min-width:0; background:#11151d; border:1px solid var(--line); border-radius:8px; padding:16px; }}
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
    .field-title {{ color:var(--text); font-weight:600; }}
    .field-note {{ min-height:34px; line-height:1.45; color:var(--muted); font-size:12px; }}
    .diag-list {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; }}
    .diag-card {{ min-width:0; border:1px solid var(--line); border-radius:8px; padding:12px; background:#11151d; }}
    .diag-head {{ display:flex; justify-content:space-between; gap:10px; align-items:flex-start; margin-bottom:8px; }}
    .diag-title {{ font-weight:700; overflow-wrap:anywhere; }}
    .diag-action {{ color:var(--muted); font-size:12px; margin-top:2px; }}
    .diag-summary {{ display:inline-flex; border:1px solid var(--line); border-radius:999px; padding:3px 8px; font-size:12px; color:var(--warn); white-space:nowrap; }}
    .diag-summary.ready {{ color:var(--good); }}
    .diag-summary.blocked {{ color:var(--bad); }}
    .diag-blockers {{ margin:8px 0 0; padding-left:18px; color:#d9dee8; font-size:13px; line-height:1.55; }}
    .diag-metrics {{ display:flex; flex-wrap:wrap; gap:6px; margin-top:10px; }}
    .diag-metric {{ border:1px solid var(--line); border-radius:999px; padding:3px 7px; color:var(--muted); font-size:12px; }}
    input {{ width:100%; box-sizing:border-box; border:1px solid var(--line); border-radius:6px; background:#10131a; color:var(--text); padding:10px 11px; font-size:14px; }}
    button {{ border:1px solid #3c76ff; background:#2258d4; color:white; border-radius:6px; padding:10px 14px; font-weight:700; cursor:pointer; }}
    .ghost-button {{ border-color:var(--line); background:#10131a; color:var(--text); }}
    .actions {{ display:flex; align-items:center; gap:12px; margin-top:14px; }}
    pre {{ overflow:auto; margin:0; font-size:12px; color:#c8d1dc; }}
    @media (max-width:900px) {{ .grid,.cards,.form-grid,.diag-list {{ grid-template-columns:1fr; }} header {{ flex-direction:column; }} }}
  </style>
</head>
<body>
<main>
  <header>
    <div>
      <h1>OKX Quant Dashboard</h1>
      <div class="muted">本地时间 {escape(str(data["local_now"]))} · 每 10 秒自动刷新</div>
    </div>
    <div class="top-actions">{mode_badge(mode)}<form method="post" action="/logout"><button class="ghost-button" type="submit">退出</button></form></div>
  </header>

  {collapsible_panel("概览", overview_body, "dashboard-panel-overview")}

  <section class="cards">
    {collapsible_panel("当前行情", market_table(mode, regimes, prices, regime_checked_at), "dashboard-panel-market")}
    {collapsible_panel("风控状态", dict_table(risk), "dashboard-panel-risk")}
  </section>

  {collapsible_panel("未下单原因", entry_diagnostics_panel(mode, entry_diagnostics), "dashboard-panel-entry-diagnostics")}

  {collapsible_panel("最近订单", orders_table(orders), "dashboard-panel-orders")}

  {collapsible_panel("动态风控配置", runtime_config_form(runtime_fields, runtime_config), "dashboard-panel-runtime")}

  {collapsible_panel("自动调参建议", tuning_report_panel(latest_tuning), "dashboard-panel-tuning")}

  <section class="cards">
    {collapsible_panel("锁仓状态", hedge_body, "dashboard-panel-hedge")}
    {collapsible_panel("运行配置", dict_table(mode), "dashboard-panel-mode")}
  </section>
</main>
<script>
  document.querySelectorAll("details[data-panel-id]").forEach((panel) => {{
    const key = "dashboard:" + panel.dataset.panelId + ":open";
    panel.open = localStorage.getItem(key) === "1";
    panel.addEventListener("toggle", () => {{
      localStorage.setItem(key, panel.open ? "1" : "0");
    }});
  }});
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


def render_login_page(error: str = "") -> str:
    error_html = f'<div class="error">{escape(error)}</div>' if error else ""
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="icon" href="/favicon.ico" sizes="32x32">
  <title>OKX Quant Dashboard Login</title>
  <style>
    :root {{ color-scheme: dark; --bg:#0f1115; --panel:#171a21; --line:#2a2f3a; --text:#e8eaed; --muted:#9aa4b2; --bad:#ff6b6b; }}
    body {{ margin:0; min-height:100vh; display:grid; place-items:center; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; background:var(--bg); color:var(--text); }}
    .login {{ width:min(360px, calc(100vw - 32px)); background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:22px; box-sizing:border-box; }}
    h1 {{ margin:0 0 6px; font-size:22px; letter-spacing:0; }}
    .muted {{ color:var(--muted); font-size:13px; margin-bottom:18px; }}
    label {{ display:flex; flex-direction:column; gap:8px; color:var(--muted); font-size:13px; }}
    input {{ width:100%; box-sizing:border-box; border:1px solid var(--line); border-radius:6px; background:#10131a; color:var(--text); padding:11px; font-size:16px; }}
    button {{ width:100%; margin-top:14px; border:1px solid #3c76ff; background:#2258d4; color:white; border-radius:6px; padding:11px 14px; font-weight:700; cursor:pointer; }}
    .error {{ margin-bottom:12px; color:var(--bad); font-size:13px; }}
  </style>
</head>
<body>
  <form class="login" method="post" action="/login">
    <h1>OKX Quant Dashboard</h1>
    <div class="muted">请输入访问密码</div>
    {error_html}
    <label>
      <span>密码</span>
      <input name="password" type="password" inputmode="numeric" autocomplete="current-password" autofocus>
    </label>
    <button type="submit">进入</button>
  </form>
</body>
</html>"""


def collapsible_panel(title: str, body: str, panel_id: str) -> str:
    return (
        f'<details class="panel" data-panel-id="{escape(panel_id)}">'
        f'<summary><h2>{escape(title)}</h2><span class="chevron" aria-hidden="true"></span></summary>'
        f'<div class="panel-body">{body}</div>'
        "</details>"
    )


def mode_badge(mode: dict[str, Any]) -> str:
    if str(mode.get("dry_run")).lower() == "false" and str(mode.get("okx_demo")).lower() == "false":
        return '<span class="status live">REAL TRADING</span>'
    if str(mode.get("okx_demo")).lower() == "true":
        return '<span class="status demo">DEMO</span>'
    return '<span class="status ok">DRY RUN</span>'


def metric(label: str, value: Any, unit: str = "") -> str:
    suffix = f" {escape(unit)}" if unit else ""
    return f'<div class="metric-tile"><div class="muted">{escape(label)}</div><div class="metric">{escape(str(value))}{suffix}</div></div>'


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


def entry_diagnostics_panel(mode: dict[str, Any], diagnostics: dict[str, Any]) -> str:
    symbols = [item.strip() for item in str(mode.get("symbols") or "").split(",") if item.strip()]
    if not symbols:
        symbols = sorted(diagnostics)
    if not symbols:
        return '<div class="muted">暂无诊断数据，等待实盘引擎保存下一次快照</div>'

    cards = ""
    for symbol in symbols:
        raw = diagnostics.get(symbol) if isinstance(diagnostics, dict) else None
        if not isinstance(raw, dict):
            cards += (
                '<div class="diag-card">'
                f'<div class="diag-head"><div><div class="diag-title">{escape(symbol)}</div>'
                '<div class="diag-action">等待下一轮策略检查</div></div>'
                '<span class="diag-summary">暂无数据</span></div>'
                '<div class="muted">引擎还没有写入该品种的入场诊断。通常是 app 尚未重启到新版本，或该品种循环还没跑到行情/仓位检查。</div>'
                "</div>"
            )
            continue

        summary = str(raw.get("summary") or "")
        blockers = raw.get("blockers") if isinstance(raw.get("blockers"), list) else []
        metrics = raw.get("metrics") if isinstance(raw.get("metrics"), dict) else {}
        blocker_items = ""
        if blockers:
            blocker_items = "".join(
                f"<li>{escape(condition_text(item))}</li>"
                for item in blockers
                if isinstance(item, dict)
            )
            blocker_html = f'<ul class="diag-blockers">{blocker_items}</ul>'
        else:
            blocker_html = '<div class="muted">当前入场条件已满足，等待风控、交易执行或下一轮检查。</div>'
        metric_html = "".join(
            f'<span class="diag-metric">{escape(metric_label(key))}: {escape(format_metric_value(value))}</span>'
            for key, value in metrics.items()
            if key in {
                "price",
                "rsi_5m",
                "opportunity_score",
                "min_score",
                "close_position_72h",
                "ret_24h",
                "ret_72h",
                "volatility_tier",
            }
        )
        metric_body = metric_html or '<span class="diag-metric">暂无指标</span>'
        cards += (
            '<div class="diag-card">'
            '<div class="diag-head">'
            f'<div><div class="diag-title">{escape(symbol)}</div>'
            f'<div class="diag-action">{escape(action_label(str(raw.get("action") or "-")))}</div></div>'
            f'<span class="diag-summary {escape(summary_class(summary))}">{escape(summary_label(summary))}</span>'
            '</div>'
            f'{blocker_html}'
            f'<div class="diag-metrics">{metric_body}</div>'
            '</div>'
        )
    return f'<div class="diag-list">{cards}</div>'


def condition_text(item: dict[str, Any]) -> str:
    code = str(item.get("code") or "unknown")
    label = condition_label(code)
    if "value" not in item:
        return label
    return f"{label}，当前值 {format_metric_value(item.get('value'))}"


def condition_label(code: str) -> str:
    labels = {
        "regime_known": "行情判断不是 UNKNOWN",
        "supported_regime": "支持该行情类型",
        "enough_5m_candles": "5m K 线数量足够",
        "atr_ready": "ATR 波动率已计算",
        "llm_review_allow_trade": "LLM 复核允许交易",
        "llm_review_pending": "等待 LLM 复核返回",
        "live_symbol_check": "实盘引擎完成该品种检查",
        "live_5m_ohlcv": "等待 5m 实时 K 线更新",
        "snapshot_1h_ohlcv": "刷新 1h K 线快照",
        "fetch_positions": "读取 OKX 当前持仓",
        "symbol_loop_error": "该品种实盘循环无异常",
        "trend_short_enabled": "空头策略启用",
        "opportunity_score": "机会评分达标",
        "multi_timeframe": "5m/15m/1h 方向一致",
        "one_hour_trend": "1h 趋势同向",
        "one_hour_bullish": "1h EMA20 在 EMA60 上方",
        "one_hour_bearish": "1h EMA20 在 EMA60 下方",
        "four_hour_bullish": "4h 收盘方向向上",
        "four_hour_bearish": "4h 收盘方向向下",
        "near_breakout": "价格靠近 4h 突破位",
        "near_4h_low": "价格靠近 4h 低位",
        "pullback_down": "最近 5m 出现回调阴线",
        "pullback_up": "最近 5m 出现反弹阳线",
        "bullish_candle": "当前 5m 阳线确认",
        "bearish_candle": "当前 5m 阴线确认",
        "macd_cross_up": "5m MACD 金叉",
        "macd_cross_down": "5m MACD 死叉",
        "price_below_midpoint": "震荡区间价格在中线下方",
        "price_above_midpoint": "震荡区间价格在中线上方",
        "price_above_ema20_5m": "价格站上 5m EMA20",
        "price_below_ema20_5m": "价格压在 5m EMA20 下方",
        "price_above_ema20_1h": "价格站上 1h EMA20",
        "price_below_ema20_1h": "价格压在 1h EMA20 下方",
        "ema_slope_up": "5m EMA20 斜率向上",
        "ema_slope_down": "5m EMA20 斜率向下",
        "rsi_40_66": "5m RSI 在 40-66",
        "rsi_34_60": "5m RSI 在 34-60",
        "rsi_42_66": "5m RSI 在 42-66",
        "rsi_35_58": "5m RSI 在 35-58",
        "range_position_not_chasing": "72h 区间位置未追高",
        "range_position_short_room": "72h 区间还有做空空间",
        "down_momentum": "短线下跌动量达标",
        "sol_filter": "SOL 专属过滤通过",
    }
    if code.endswith("_timeout"):
        base_label = labels.get(code.removesuffix("_timeout"), code.removesuffix("_timeout"))
        return f"{base_label}超时"
    if code.startswith("risk_"):
        return f"风控允许下单：{code.removeprefix('risk_')}"
    return labels.get(code, code)


def action_label(action: str) -> str:
    return {
        "none": "不下单",
        "data_wait": "等待数据",
        "engine_started": "引擎已启动",
        "live_5m_wait": "等待 5m 行情",
        "refresh_higher_timeframes": "刷新 1h/4h 行情",
        "fetch_positions": "读取持仓",
        "symbol_loop_error": "品种循环报错",
        "llm_review": "LLM 复核",
        "shock_long": "震荡逢低做多",
        "shock_short": "震荡逢高做空",
        "trend_long": "单边上涨回调做多",
        "trend_short": "单边下跌反弹做空",
        "shock_trend_up": "震荡上行回调做多",
        "shock_trend_down": "震荡下行反弹做空",
    }.get(action, action)


def summary_label(summary: str) -> str:
    return {
        "waiting_for_conditions": "等待条件",
        "waiting_live_check": "等待检查",
        "waiting_market_data": "等待行情",
        "waiting_positions": "等待持仓",
        "waiting_llm_review": "等待LLM",
        "entry_conditions_met": "条件满足",
        "signal_ready": "信号已触发",
        "release_hedge_signal_ready": "解锁信号",
        "exit_signal_ready": "退出信号",
        "risk_rejected": "风控拒单",
        "order_submitted": "已提交订单",
        "llm_rejected": "LLM 拒绝",
        "symbol_loop_error": "循环异常",
    }.get(summary, summary or "-")


def summary_class(summary: str) -> str:
    if summary in {"entry_conditions_met", "signal_ready", "release_hedge_signal_ready", "exit_signal_ready", "order_submitted"}:
        return "ready"
    if summary in {"risk_rejected", "llm_rejected", "symbol_loop_error"}:
        return "blocked"
    return ""


def metric_label(key: str) -> str:
    return {
        "price": "价格",
        "rsi_5m": "RSI",
        "opportunity_score": "评分",
        "min_score": "最低分",
        "close_position_72h": "72h位置",
        "ret_24h": "24h涨跌",
        "ret_72h": "72h涨跌",
        "volatility_tier": "波动级别",
    }.get(str(key), str(key))


def format_metric_value(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)


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


def latest_tuning_report() -> dict[str, Any] | None:
    reports = sorted(REPORTS_DIR.glob("runtime_tuning_*.md"), key=lambda item: item.stat().st_mtime, reverse=True)
    if not reports:
        return None
    path = reports[0]
    text = path.read_text(encoding="utf-8")
    return {
        "name": path.name,
        "mtime": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
        "current": extract_report_metric(text, "## 当前参数"),
        "recommended": extract_report_metric(text, "## 推荐参数"),
        "reason": extract_report_reason(text),
    }


def extract_report_metric(text: str, section: str) -> dict[str, str]:
    lines = text.splitlines()
    try:
        start = lines.index(section)
    except ValueError:
        return {}
    result: dict[str, str] = {}
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        if line.startswith("- ") and ": `" in line:
            key, value = line[2:].split(": `", 1)
            result[key.strip()] = value.rstrip("`")
    return result


def extract_report_reason(text: str) -> str:
    lines = text.splitlines()
    try:
        start = lines.index("## 推荐原因")
    except ValueError:
        return ""
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            return ""
        if line.strip():
            return line.strip()
    return ""


def tuning_report_panel(report: dict[str, Any] | None) -> str:
    if not report:
        return '<div class="muted">暂无调参报告</div>'
    current = report.get("current") or {}
    recommended = report.get("recommended") or {}
    rows = (
        f"<tr><th>当前实盘参数回测</th><td>{escape(str(current.get('candidate', '-')))}</td>"
        f"<td>{escape(str(current.get('pnl', '-')))}</td><td>{escape(str(current.get('win_rate', '-')))}</td>"
        f"<td>{escape(str(current.get('trades', '-')))}</td></tr>"
        f"<tr><th>推荐参数回测</th><td>{escape(str(recommended.get('candidate', '-')))}</td>"
        f"<td>{escape(str(recommended.get('pnl', '-')))}</td><td>{escape(str(recommended.get('win_rate', '-')))}</td>"
        f"<td>{escape(str(recommended.get('trades', '-')))}</td></tr>"
    )
    return (
        f'<div class="muted">最近生成：{escape(format_local_time(report.get("mtime")))} · '
        f'<a href="/reports/{escape(str(report.get("name")))}" target="_blank">打开完整报告</a></div>'
        '<table style="margin-top:10px"><thead><tr><th>类型</th><th>参数档</th><th>收益</th><th>胜率</th><th>交易次数</th></tr></thead>'
        f"<tbody>{rows}</tbody></table>"
        f'<div class="muted" style="margin-top:10px">{escape(str(report.get("reason") or ""))}</div>'
    )


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
        input_type = "checkbox" if field.get("boolean") else "text" if field.get("text") else "number"
        checked = " checked" if field.get("boolean") and float(values.get(key, 0.0) or 0.0) >= 1.0 else ""
        value_attr = " value=\"1\"" if field.get("boolean") else f" value=\"{escape(str(values.get(key, '')))}\""
        number_attrs = (
            ""
            if field.get("boolean") or field.get("text")
            else f" min=\"{field['min']}\" max=\"{field['max']}\" step=\"{field['step']}\""
        )
        controls.append(
            "<label>"
            f"<span class=\"field-title\">{escape(str(field['label']))} <span class=\"muted\">{escape(hint)}</span></span>"
            f"<input name=\"{escape(key)}\" type=\"{input_type}\"{number_attrs}{value_attr}{checked}>"
            f"<span class=\"field-note\">{escape(str(field.get('description') or ''))}</span>"
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

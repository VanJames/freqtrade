from __future__ import annotations

import json
import os
import hmac
import hashlib
import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from html import escape
from pathlib import Path
from string import Template
from urllib.parse import parse_qs
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import asyncpg
import ccxt.async_support as ccxt
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

from trading_system.config import Settings
from trading_system.hotcoin import (
    HOTCOIN_SESSION_KEY,
    HotcoinWebSession,
    ensure_hotcoin_session_schema,
    load_hotcoin_session,
    save_hotcoin_session,
)
from trading_system.runtime_config import RUNTIME_FIELDS, runtime_defaults, validate_runtime_config


app = FastAPI(title="Quant Dashboard")
FAVICON_PATH = Path(__file__).with_name("assets") / "favicon.ico"
REPORTS_DIR = Path(os.getenv("REPORTS_DIR", "reports"))
DASHBOARD_SESSION_COOKIE = "okx_quant_dashboard_session"
OKX_BILL_CACHE: tuple[float, list[dict[str, Any]]] | None = None
OKX_BILL_CACHE_TTL_SECONDS = 60.0


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
            status varchar(32) not null default '',
            filled_qty numeric(18, 8) not null default 0,
            avg_price numeric(18, 8) not null default 0,
            realized_pnl numeric(18, 8),
            pnl_source varchar(32) not null default '',
            side varchar(8) not null default '',
            position_side varchar(8) not null default '',
            signal_reason text not null default '',
            exchange_id varchar(32) not null default '',
            created_at timestamp with time zone not null
        )
        """
    )
    await conn.execute(
        """
        alter table order_tracks
        add column if not exists status varchar(32) not null default '',
        add column if not exists filled_qty numeric(18, 8) not null default 0,
        add column if not exists avg_price numeric(18, 8) not null default 0,
        add column if not exists realized_pnl numeric(18, 8),
        add column if not exists pnl_source varchar(32) not null default '',
        add column if not exists side varchar(8) not null default '',
        add column if not exists position_side varchar(8) not null default '',
        add column if not exists signal_reason text not null default '',
        add column if not exists exchange_id varchar(32) not null default ''
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
    await ensure_hotcoin_session_schema(conn)


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
        runtime_rows = await conn.fetch("select setting_key, setting_value, updated_at from runtime_settings")
        exchange_rows = await conn.fetch(
            "select exchange_id, account_key, session_data, updated_at from exchange_sessions order by updated_at desc"
        )
    finally:
        await conn.close()

    runtime_values = {
        str(row["setting_key"]): str(row["setting_value"])
        for row in runtime_rows
    }
    runtime_config = defaults | {
        str(row["setting_key"]): str(row["setting_value"])
        for row in runtime_rows
        if str(row["setting_key"]) in defaults
    }
    selected_exchange = runtime_values.get("selected_exchange_id") or os.getenv("EXCHANGE_ID", settings.exchange_id)
    current_exchange = selected_exchange.lower().strip()
    order_rows = await fetch_orders_for_exchange(current_exchange)

    return {
        "now": datetime.now(timezone.utc).isoformat(),
        "local_now": format_local_time(datetime.now(timezone.utc).isoformat()),
        "mode": {
            "dry_run": os.getenv("DRY_RUN", ""),
            "okx_demo": os.getenv("OKX_DEMO", ""),
            "exchange_id": os.getenv("EXCHANGE_ID", settings.exchange_id),
            "selected_exchange_id": selected_exchange,
            "symbols": os.getenv("SYMBOLS", ""),
            "llm_enabled": os.getenv("LLM_REGIME_REVIEW_ENABLED", ""),
            "llm_provider": os.getenv("LLM_REGIME_PROVIDER", ""),
            "llm_model": os.getenv("LLM_REGIME_MODEL", ""),
            "llm_cache_ttl_seconds": os.getenv("LLM_REGIME_REVIEW_CACHE_TTL_SECONDS", ""),
            "llm_min_interval_seconds": os.getenv("LLM_REGIME_REVIEW_MIN_INTERVAL_SECONDS", ""),
        },
        "latest_snapshot": normalize_row(latest_snapshot),
        "orders": order_rows["orders"],
        "order_count": order_rows["count"],
        "runtime_config": runtime_config,
        "exchange_sessions": [safe_exchange_session(row) for row in exchange_rows],
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


async def fetch_orders_for_exchange(exchange_id: str) -> dict[str, Any]:
    conn = await asyncpg.connect(dsn())
    try:
        await ensure_dashboard_schema(conn)
        if exchange_id == "hotcoin":
            rows = await conn.fetch(
                """
                select order_id, symbol, regime_mode, initial_qty, maker_filled,
                       taker_twap_filled, fee_paid, status, filled_qty,
                       avg_price, realized_pnl, pnl_source, side, position_side, signal_reason,
                       exchange_id, created_at
                from order_tracks
                where exchange_id = 'hotcoin'
                order by created_at desc
                limit 30
                """
            )
            count = await conn.fetchval("select count(*) from order_tracks where exchange_id = 'hotcoin'")
        else:
            rows = await conn.fetch(
                """
                select order_id, symbol, regime_mode, initial_qty, maker_filled,
                       taker_twap_filled, fee_paid, status, filled_qty,
                       avg_price, realized_pnl, pnl_source, side, position_side, signal_reason,
                       exchange_id, created_at
                from order_tracks
                where exchange_id in ('', 'okx')
                order by created_at desc
                limit 30
                """
            )
            count = await conn.fetchval("select count(*) from order_tracks where exchange_id in ('', 'okx')")
    finally:
        await conn.close()
    orders = [normalize_row(row) for row in rows]
    if exchange_id == "hotcoin":
        orders = normalize_hotcoin_order_display(orders)
    else:
        orders = await merge_okx_realized_pnl_bills(orders)
        orders.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
        orders = orders[:30]
    return {"orders": orders, "count": int(count or 0)}


def normalize_hotcoin_order_display(orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for order in orders:
        if order.get("pnl_source") != "exchange":
            order["fee_paid"] = None
            order["realized_pnl"] = None
    return orders


async def merge_okx_realized_pnl_bills(orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    bills = await fetch_okx_realized_pnl_bills()
    if not bills:
        return orders
    by_order_id = {str(order.get("order_id")): order for order in orders if order.get("order_id")}
    for bill in bills:
        order_id = str(bill.get("order_id") or "")
        if not order_id:
            continue
        existing = by_order_id.get(order_id)
        if existing is not None:
            if bill.get("realized_pnl") not in {None, 0, 0.0}:
                existing["realized_pnl"] = bill["realized_pnl"]
            if float(existing.get("fee_paid") or 0.0) == 0.0:
                existing["fee_paid"] = bill.get("fee_paid", existing.get("fee_paid", 0.0))
            continue
        orders.append(bill)
        by_order_id[order_id] = bill
    return orders


async def fetch_okx_realized_pnl_bills(limit: int = 80) -> list[dict[str, Any]]:
    global OKX_BILL_CACHE
    now = asyncio.get_running_loop().time()
    if OKX_BILL_CACHE and now - OKX_BILL_CACHE[0] <= OKX_BILL_CACHE_TTL_SECONDS:
        return [dict(item) for item in OKX_BILL_CACHE[1]]
    settings = Settings()
    if not settings.okx_api_key:
        return []
    exchange = ccxt.okx(settings.okx_config())
    try:
        payload = await exchange.private_get_account_bills({"limit": str(limit)})
    except Exception:
        return []
    finally:
        await exchange.close()
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    aggregated: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        order_id = str(row.get("ordId") or "")
        inst_id = str(row.get("instId") or "")
        if not order_id or not inst_id.endswith("-SWAP"):
            continue
        pnl_value = _float(row.get("pnl"))
        fee_value = _float(row.get("fee"))
        if pnl_value == 0.0 and fee_value == 0.0:
            continue
        item = aggregated.setdefault(
            order_id,
            {
                "order_id": order_id,
                "symbol": okx_inst_id_to_symbol(inst_id),
                "regime_mode": "OKX_BILL",
                "initial_qty": 0.0,
                "maker_filled": 0.0,
                "taker_twap_filled": 0.0,
                "fee_paid": 0.0,
                "status": "exchange_bill",
                "filled_qty": 0.0,
                "avg_price": 0.0,
                "realized_pnl": 0.0,
                "side": "",
                "position_side": "",
                "signal_reason": "okx_account_bill",
                "exchange_id": "okx",
                "created_at": _okx_ts_to_iso(row.get("ts")),
            },
        )
        item["realized_pnl"] = float(item["realized_pnl"]) + pnl_value
        item["fee_paid"] = float(item["fee_paid"]) + abs(fee_value)
        current_time = str(item.get("created_at") or "")
        row_time = _okx_ts_to_iso(row.get("ts"))
        if row_time > current_time:
            item["created_at"] = row_time
    result = list(aggregated.values())
    OKX_BILL_CACHE = (now, [dict(item) for item in result])
    return result


def okx_inst_id_to_symbol(inst_id: str) -> str:
    base = inst_id.removesuffix("-SWAP").replace("-", "/")
    return f"{base}:USDT" if base.endswith("/USDT") else base


def _okx_ts_to_iso(value: Any) -> str:
    try:
        return datetime.fromtimestamp(float(value) / 1000.0, timezone.utc).isoformat()
    except (TypeError, ValueError):
        return datetime.now(timezone.utc).isoformat()


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def safe_exchange_session(row: asyncpg.Record) -> dict[str, Any]:
    data = normalize_row(row) or {}
    session = parse_memory(str(data.get("session_data") or "{}"))
    if isinstance(session, dict):
        data["connected"] = bool(session.get("token"))
        data["token"] = "已保存" if session.get("token") else "未保存"
        data["device_id"] = session.get("device_id", "")
    data.pop("session_data", None)
    return data


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


@app.post("/api/exchange/select")
async def api_select_exchange(request: Request, payload: dict[str, Any] = Body(...)) -> JSONResponse:
    require_dashboard_auth(request)
    exchange_id = str(payload.get("exchange_id") or "").lower().strip()
    if exchange_id not in {"okx", "hotcoin"}:
        raise HTTPException(status_code=400, detail="exchange_id must be okx or hotcoin")
    conn = await asyncpg.connect(dsn())
    try:
        await ensure_dashboard_schema(conn)
        await conn.execute(
            """
            insert into runtime_settings(setting_key, setting_value, updated_at)
            values('selected_exchange_id', $1, now())
            on conflict(setting_key)
            do update set setting_value = excluded.setting_value, updated_at = excluded.updated_at
            """,
            exchange_id,
        )
    finally:
        await conn.close()
    return JSONResponse({"ok": True, "exchange_id": exchange_id, "restart_required": True})


@app.post("/api/hotcoin/qr/start")
async def api_hotcoin_qr_start(request: Request) -> JSONResponse:
    require_dashboard_auth(request)
    settings = Settings()
    client = HotcoinWebSession(
        base_url=settings.hotcoin_base_url,
        device_id=settings.hotcoin_device_id,
        timeout=settings.exchange_request_timeout_seconds,
    )
    try:
        qr = await asyncio.to_thread(client.start_qr_login)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return JSONResponse({"ok": True, "qr_token": qr.token, "image": qr.image_data, "expires_at": qr.expires_at})


@app.post("/api/hotcoin/qr/poll")
async def api_hotcoin_qr_poll(request: Request, payload: dict[str, Any] = Body(...)) -> JSONResponse:
    require_dashboard_auth(request)
    qr_token = str(payload.get("qr_token") or "").strip()
    if not qr_token:
        raise HTTPException(status_code=400, detail="qr_token is required")
    settings = Settings()
    client = HotcoinWebSession(
        base_url=settings.hotcoin_base_url,
        device_id=settings.hotcoin_device_id,
        timeout=settings.exchange_request_timeout_seconds,
    )
    try:
        result = await asyncio.to_thread(client.poll_qr_login, qr_token)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if result.get("status") == "connected":
        await save_hotcoin_session(settings.postgres_dsn, HOTCOIN_SESSION_KEY, result["session"])
        user = result.get("user") if isinstance(result.get("user"), dict) else {}
        return JSONResponse({"ok": True, "status": "connected", "user": {"fid": user.get("fid")}})
    return JSONResponse({"ok": True, "status": result.get("status", "pending"), "message": result.get("message", "")})


@app.get("/api/hotcoin/session")
async def api_hotcoin_session(request: Request) -> JSONResponse:
    require_dashboard_auth(request)
    session = await load_hotcoin_session(Settings().postgres_dsn)
    return JSONResponse(
        {
            "ok": True,
            "connected": bool(session and session.get("token")),
            "saved_at": session.get("saved_at") if isinstance(session, dict) else None,
            "device_id": session.get("device_id") if isinstance(session, dict) else "",
        }
    )


@app.post("/api/hotcoin/test-session")
async def api_hotcoin_test_session(request: Request) -> JSONResponse:
    require_dashboard_auth(request)
    settings = Settings()
    session_data = await load_hotcoin_session(settings.postgres_dsn)
    if not session_data or not session_data.get("token"):
        return JSONResponse(
            {
                "ok": True,
                "connected": False,
                "trade_auth_valid": False,
                "summary": "未保存 Hotcoin 登录会话",
                "checks": [],
            }
        )

    client = HotcoinWebSession(
        base_url=settings.hotcoin_base_url,
        device_id=settings.hotcoin_device_id,
        timeout=settings.exchange_request_timeout_seconds,
        session_loader=lambda: asyncio.run(load_hotcoin_session(settings.postgres_dsn)),
    )
    client.load_session_data(session_data)
    checks: list[dict[str, Any]] = []

    def check_item(name: str, ok: bool, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        return {
            "name": name,
            "ok": ok,
            "code": payload.get("code"),
            "msg": payload.get("msg") or payload.get("message") or payload.get("error") or "",
        }

    try:
        user = await asyncio.to_thread(client.get_user_info)
        checks.append({"name": "user_info", "ok": bool(user), "code": 200 if user else None, "msg": ""})
    except Exception as exc:
        checks.append({"name": "user_info", "ok": False, "code": None, "msg": str(exc)})

    try:
        balance = await asyncio.to_thread(client.get_balance)
        checks.append(check_item("balance", balance.get("code") == 200, balance))
    except Exception as exc:
        checks.append({"name": "balance", "ok": False, "code": None, "msg": str(exc)})

    try:
        positions = await asyncio.to_thread(client.get_positions)
        checks.append({"name": "positions", "ok": isinstance(positions, list), "code": 200, "msg": f"{len(positions)} positions"})
    except Exception as exc:
        checks.append({"name": "positions", "ok": False, "code": None, "msg": str(exc)})

    probe_symbol = settings.symbols[0] if settings.symbols else "BTC/USDT:USDT"
    trade_probe: dict[str, Any]
    try:
        trade_probe = await asyncio.to_thread(
            client.place_order,
            symbol=probe_symbol,
            side="sell",
            amount=0.0,
            price=1.0,
            order_type="limit",
            reduce_only=True,
        )
        trade_auth_valid = trade_probe.get("code") != 401
        checks.append(check_item("zero_amount_order_probe", trade_auth_valid, trade_probe))
    except Exception as exc:
        trade_probe = {"code": None, "msg": str(exc)}
        trade_auth_valid = False
        checks.append({"name": "zero_amount_order_probe", "ok": False, "code": None, "msg": str(exc)})

    query_ok = any(item.get("ok") for item in checks if item.get("name") in {"user_info", "balance", "positions"})
    if trade_probe.get("code") == 401:
        summary = "交易接口返回 401，Hotcoin 登录或交易 Cookie 已失效"
    elif trade_auth_valid:
        summary = "交易接口未返回 401，登录态有效；0 数量下单返回的是业务校验结果"
    elif query_ok:
        summary = "查询接口可用，但交易接口探测失败，请查看错误信息"
    else:
        summary = "Hotcoin 查询与交易接口均未通过"
    return JSONResponse(
        {
            "ok": True,
            "connected": query_ok,
            "trade_auth_valid": trade_auth_valid,
            "probe_symbol": probe_symbol,
            "summary": summary,
            "checks": checks,
        }
    )


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
    initial_json = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    template = Template(r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="icon" href="/favicon.ico" sizes="32x32">
  <title>Quant Dashboard</title>
  <style>
    :root { color-scheme: dark; --bg:#0d1016; --sidebar:#11151d; --panel:#171b24; --panel2:#10141b; --line:#29313f; --text:#edf1f6; --muted:#94a0b2; --good:#33d17a; --warn:#f7c948; --bad:#ff6b6b; --accent:#4f8cff; --cyan:#3ddbd9; --orange:#ff9f43; }
    * { box-sizing:border-box; }
    body { margin:0; min-height:100vh; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; background:var(--bg); color:var(--text); }
    button,input,select { font:inherit; }
    button { border:1px solid #3b6fd8; background:#255bd6; color:white; border-radius:7px; padding:10px 13px; font-weight:700; cursor:pointer; transition:transform .18s ease,border-color .18s ease,background .18s ease; }
    button:hover { transform:translateY(-1px); }
    button:disabled { opacity:.55; cursor:not-allowed; transform:none; }
    input,select { width:100%; border:1px solid var(--line); border-radius:7px; background:#0e1219; color:var(--text); padding:10px 11px; }
    table { width:100%; border-collapse:collapse; font-size:13px; }
    th,td { border-bottom:1px solid var(--line); padding:10px 8px; text-align:left; white-space:normal; overflow-wrap:anywhere; word-break:break-word; vertical-align:top; }
    th { color:var(--muted); font-weight:700; }
    pre { margin:0; overflow:auto; color:#cbd4e1; font-size:12px; }
    .app-shell { min-height:100vh; display:grid; grid-template-columns:260px minmax(0,1fr); }
    .sidebar { position:sticky; top:0; height:100vh; padding:18px 14px; border-right:1px solid var(--line); background:var(--sidebar); display:flex; flex-direction:column; gap:16px; z-index:30; }
    .brand { padding:4px 8px 12px; border-bottom:1px solid var(--line); }
    .brand-title { margin:0; font-size:21px; letter-spacing:0; }
    .brand-subtitle { margin-top:6px; color:var(--muted); font-size:12px; line-height:1.45; }
    .nav { display:flex; flex-direction:column; gap:6px; overflow:auto; }
    .nav-button { display:grid; grid-template-columns:28px minmax(0,1fr) auto; align-items:center; gap:10px; width:100%; border:1px solid transparent; background:transparent; color:var(--muted); text-align:left; padding:10px; }
    .nav-button:hover,.nav-button.active { color:var(--text); background:#171d29; border-color:var(--line); transform:none; }
    .nav-icon { width:22px; height:22px; border-radius:6px; display:grid; place-items:center; color:var(--cyan); background:#0d1d23; font-size:12px; font-weight:800; }
    .nav-count { color:var(--muted); font-size:12px; }
    .sidebar-footer { margin-top:auto; display:flex; gap:8px; align-items:center; }
    .content { min-width:0; padding:18px 22px 32px; }
    .mobile-top { display:none; position:sticky; top:0; z-index:20; align-items:center; justify-content:space-between; gap:12px; padding:12px 14px; background:rgba(13,16,22,.94); border-bottom:1px solid var(--line); backdrop-filter:blur(12px); }
    .page-head { display:flex; justify-content:space-between; gap:16px; align-items:flex-start; margin-bottom:18px; }
    .page-title { margin:0; font-size:24px; letter-spacing:0; }
    .page-meta { margin-top:6px; color:var(--muted); font-size:13px; }
    .top-actions { display:flex; flex-wrap:wrap; gap:8px; justify-content:flex-end; align-items:center; }
    .ghost-button { border-color:var(--line); background:#111722; color:var(--text); }
    .status-pill { display:inline-flex; align-items:center; gap:7px; border:1px solid var(--line); border-radius:999px; padding:7px 10px; color:var(--muted); font-size:12px; background:#10151f; }
    .status-dot { width:7px; height:7px; border-radius:50%; background:var(--muted); box-shadow:0 0 0 4px rgba(148,160,178,.12); }
    .status-pill.live .status-dot { background:var(--bad); box-shadow:0 0 0 4px rgba(255,107,107,.13); }
    .status-pill.demo .status-dot { background:var(--warn); box-shadow:0 0 0 4px rgba(247,201,72,.13); }
    .status-pill.ok .status-dot { background:var(--good); box-shadow:0 0 0 4px rgba(51,209,122,.13); }
    .grid { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; }
    .two-col { display:grid; grid-template-columns:1fr 1fr; gap:14px; }
    .panel,.metric-tile,.diag-card,.flow-card { min-width:0; background:var(--panel); border:1px solid var(--line); border-radius:8px; box-shadow:0 18px 60px rgba(0,0,0,.18); }
    .panel { padding:16px; animation:rise .28s ease both; }
    .panel-title { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:14px; }
    .panel-title h2 { margin:0; font-size:16px; letter-spacing:0; }
    .muted { color:var(--muted); }
    .small { font-size:12px; }
    .metric-tile { padding:15px; background:linear-gradient(180deg,#171d27,#121720); }
    .metric-label { color:var(--muted); font-size:12px; }
    .metric-value { margin-top:7px; font-size:22px; font-weight:800; line-height:1.2; overflow-wrap:anywhere; }
    .table-wrap { overflow:auto; }
    .tag { display:inline-flex; border:1px solid var(--line); border-radius:999px; padding:3px 8px; color:var(--muted); font-size:12px; white-space:nowrap; }
    .tag.good { color:var(--good); border-color:rgba(51,209,122,.35); }
    .tag.warn { color:var(--warn); border-color:rgba(247,201,72,.35); }
    .tag.bad { color:var(--bad); border-color:rgba(255,107,107,.35); }
    .pnl-good { color:var(--good); font-weight:700; }
    .pnl-bad { color:var(--bad); font-weight:700; }
    .diag-list { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; }
    .diag-card { padding:13px; background:var(--panel2); transition:transform .18s ease,border-color .18s ease; }
    .diag-card:hover { transform:translateY(-2px); border-color:#3f4b60; }
    .diag-head { display:flex; justify-content:space-between; gap:10px; align-items:flex-start; margin-bottom:8px; }
    .diag-title { font-weight:800; overflow-wrap:anywhere; }
    .diag-action { color:var(--muted); font-size:12px; margin-top:2px; }
    .diag-progress { display:flex; flex-wrap:wrap; gap:6px; margin:8px 0 10px; }
    .condition-group { margin-top:10px; }
    .condition-heading { display:flex; align-items:center; justify-content:space-between; gap:8px; color:var(--muted); font-size:12px; font-weight:800; }
    .condition-list { list-style:none; margin:7px 0 0; padding:0; display:grid; gap:6px; color:#dce4ee; font-size:13px; line-height:1.45; }
    .condition-item { display:grid; grid-template-columns:18px minmax(0,1fr); gap:7px; align-items:start; border:1px solid rgba(255,255,255,.06); border-radius:8px; padding:8px; background:#10151f; }
    .condition-item.good { border-color:rgba(51,209,122,.22); }
    .condition-item.wait { border-color:rgba(247,201,72,.28); background:#14181e; }
    .condition-icon { width:18px; height:18px; border-radius:50%; display:inline-flex; align-items:center; justify-content:center; font-size:12px; font-weight:900; }
    .condition-item.good .condition-icon { color:#07120d; background:var(--good); }
    .condition-item.wait .condition-icon { color:#141006; background:var(--warn); }
    .condition-main { overflow-wrap:anywhere; }
    .condition-hint { margin-top:3px; color:var(--muted); font-size:12px; }
    .blockers { margin:8px 0 0; padding-left:18px; color:#dce4ee; font-size:13px; line-height:1.55; }
    .metric-tags { display:flex; flex-wrap:wrap; gap:6px; margin-top:10px; }
    .form-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; }
    .preset-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; margin-bottom:14px; }
    .preset-card { text-align:left; border-color:var(--line); background:#10151f; color:var(--text); padding:12px; min-height:126px; display:flex; flex-direction:column; gap:8px; align-items:flex-start; }
    .preset-card:hover { border-color:#4f8cff; background:#121b2a; transform:translateY(-1px); }
    .preset-card.active { border-color:rgba(51,209,122,.55); box-shadow:0 0 0 1px rgba(51,209,122,.16) inset; }
    .preset-title { font-weight:800; font-size:14px; }
    .preset-desc { color:var(--muted); font-size:12px; line-height:1.45; font-weight:500; }
    .preset-values { display:flex; flex-wrap:wrap; gap:6px; margin-top:auto; }
    .field { display:flex; flex-direction:column; gap:7px; color:var(--muted); font-size:13px; }
    .field-title { color:var(--text); font-weight:700; }
    .field-note { min-height:34px; line-height:1.45; color:var(--muted); font-size:12px; }
    .switch-row { flex-direction:row; align-items:center; justify-content:space-between; border:1px solid var(--line); border-radius:8px; padding:10px; background:#10151f; }
    .switch-row input { width:auto; transform:scale(1.15); }
    .actions { display:flex; flex-wrap:wrap; align-items:center; gap:10px; margin-top:14px; }
    .qr-box { display:grid; grid-template-columns:180px minmax(0,1fr); gap:16px; align-items:start; margin-top:14px; }
    .qr-box img { width:180px; height:180px; background:white; border-radius:8px; padding:8px; }
    .trade-flow { display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); gap:10px; position:relative; }
    .trade-flow::before { content:""; position:absolute; left:8%; right:8%; top:29px; height:2px; background:linear-gradient(90deg,var(--cyan),var(--accent),var(--orange)); opacity:.55; animation:flowline 2.8s linear infinite; }
    .flow-card { position:relative; padding:12px; background:#111722; z-index:1; overflow:hidden; }
    .flow-card::after { content:""; position:absolute; inset:auto -25% -45% -25%; height:60px; background:radial-gradient(circle,rgba(61,219,217,.18),transparent 70%); animation:pulse 2.4s ease-in-out infinite; }
    .flow-dot { width:12px; height:12px; border-radius:50%; background:var(--cyan); box-shadow:0 0 0 6px rgba(61,219,217,.12); margin-bottom:14px; animation:blink 1.6s ease-in-out infinite; }
    .flow-title { font-weight:800; font-size:13px; }
    .flow-desc { margin-top:5px; color:var(--muted); font-size:12px; line-height:1.45; }
    .drawer-mask { display:none; }
    @keyframes rise { from { opacity:0; transform:translateY(8px); } to { opacity:1; transform:translateY(0); } }
    @keyframes pulse { 0%,100% { transform:translateY(0); opacity:.6; } 50% { transform:translateY(-14px); opacity:1; } }
    @keyframes blink { 0%,100% { transform:scale(.86); } 50% { transform:scale(1.12); } }
    @keyframes flowline { from { filter:hue-rotate(0deg); } to { filter:hue-rotate(45deg); } }
    @media (max-width:1100px) { .grid { grid-template-columns:repeat(2,minmax(0,1fr)); } .diag-list,.form-grid,.preset-grid { grid-template-columns:repeat(2,minmax(0,1fr)); } .trade-flow { grid-template-columns:1fr; } .trade-flow::before { display:none; } }
    @media (max-width:860px) {
      .app-shell { display:block; }
      .mobile-top { display:flex; }
      .sidebar { position:fixed; left:0; top:0; bottom:0; width:284px; transform:translateX(-104%); transition:transform .22s ease; box-shadow:24px 0 80px rgba(0,0,0,.38); }
      .sidebar.open { transform:translateX(0); }
      .drawer-mask.open { display:block; position:fixed; inset:0; z-index:25; background:rgba(0,0,0,.55); }
      .content { padding:16px 14px 28px; }
      .page-head { flex-direction:column; }
      .top-actions { justify-content:flex-start; }
      .grid,.two-col,.diag-list,.form-grid,.preset-grid,.qr-box { grid-template-columns:1fr; }
      .metric-value { font-size:20px; }
    }
  </style>
</head>
<body>
<div id="root"></div>
<noscript>需要启用 JavaScript 才能查看控制台。</noscript>
<script>window.__DASHBOARD_DATA__ = $initial_data;</script>
<script crossorigin src="https://unpkg.com/react@18/umd/react.production.min.js"></script>
<script crossorigin src="https://unpkg.com/react-dom@18/umd/react-dom.production.min.js"></script>
<script>
(function () {
  const e = React.createElement;
  const navItems = [
    ["overview", "概览", "总"],
    ["market", "行情", "行"],
    ["notice", "清算策略", "清"],
    ["flow", "交易过程", "流"],
    ["diagnostics", "入场诊断", "诊"],
    ["positions", "持仓保护", "仓"],
    ["orders", "最近订单", "单"],
    ["risk", "风控配置", "控"],
    ["exchange", "交易所", "所"],
    ["tuning", "调参报告", "参"],
    ["system", "系统", "系"]
  ];
  const conditionLabels = {
    regime_known:"行情判断不是 UNKNOWN", supported_regime:"支持该行情类型", enough_5m_candles:"5m K 线数量足够", atr_ready:"ATR 波动率已计算",
    llm_review_allow_trade:"LLM 复核允许交易", llm_review_pending:"等待 LLM 复核返回", live_symbol_check:"实盘引擎完成该品种检查",
    live_5m_ohlcv:"等待 5m 实时 K 线更新", snapshot_1h_ohlcv:"刷新 1h K 线快照", fetch_positions:"读取当前持仓", symbol_loop_error:"该品种实盘循环无异常",
    trend_short_enabled:"空头策略启用", opportunity_score:"机会评分达标", multi_timeframe:"5m/15m/1h 方向一致", one_hour_trend:"1h 趋势同向",
    one_hour_bullish:"1h EMA20 在 EMA60 上方", one_hour_bearish:"1h EMA20 在 EMA60 下方", four_hour_bullish:"4h 收盘方向向上",
    four_hour_bearish:"4h 收盘方向向下", near_breakout:"价格靠近 4h 突破位", near_4h_low:"价格靠近 4h 低位",
    pullback_down:"最近 5m 出现回调阴线", pullback_up:"最近 5m 出现反弹阳线", bullish_candle:"当前 5m 阳线确认", bearish_candle:"当前 5m 阴线确认",
    macd_cross_up:"5m MACD 金叉", macd_cross_down:"5m MACD 死叉", price_below_midpoint:"震荡区间价格在中线下方", price_above_midpoint:"震荡区间价格在中线上方",
    price_above_ema20_5m:"价格站上 5m EMA20", price_below_ema20_5m:"价格压在 5m EMA20 下方", price_above_ema20_1h:"价格站上 1h EMA20",
    price_below_ema20_1h:"价格压在 1h EMA20 下方", ema_slope_up:"5m EMA20 斜率向上", ema_slope_down:"5m EMA20 斜率向下",
    rsi_40_66:"5m RSI 在 40-66", rsi_34_60:"5m RSI 在 34-60", rsi_42_66:"5m RSI 在 42-66", rsi_35_58:"5m RSI 在 35-58",
    range_position_not_chasing:"72h 区间位置未追高", range_position_short_room:"72h 区间还有做空空间", down_momentum:"短线下跌动量达标",
    low_range_rebound:"低位追空保护通过", sol_filter:"SOL 专属过滤通过"
  };
  const actionLabels = { none:"不下单", data_wait:"等待数据", engine_started:"引擎已启动", live_5m_wait:"等待 5m 行情", refresh_higher_timeframes:"刷新 1h/4h 行情", fetch_positions:"读取持仓", symbol_loop_error:"品种循环报错", llm_review:"LLM 复核", shock_long:"震荡逢低做多", shock_short:"震荡逢高做空", trend_long:"单边上涨回调做多", trend_short:"单边下跌反弹做空", shock_trend_up:"震荡上行回调做多", shock_trend_down:"震荡下行反弹做空" };
  const summaryLabels = { waiting_for_conditions:"等待条件", waiting_live_check:"等待检查", waiting_market_data:"等待行情", waiting_positions:"等待持仓", waiting_llm_review:"等待LLM", entry_conditions_met:"条件满足", signal_ready:"信号已触发", release_hedge_signal_ready:"解锁信号", exit_signal_ready:"退出信号", risk_rejected:"风控拒单", order_submitted:"已提交订单", llm_rejected:"LLM 拒绝", symbol_loop_error:"循环异常" };
  const metricLabels = { price:"价格", rsi_5m:"RSI", opportunity_score:"评分", min_score:"最低分", close_position_72h:"72h位置", ret_24h:"24h涨跌", ret_72h:"72h涨跌", volatility_tier:"波动级别" };

  function value(v, fallback) { return v === undefined || v === null || v === "" ? (arguments.length > 1 ? fallback : "-") : v; }
  function boolEnabled(v) {
    if (v === true) return true;
    if (v === false || v === null || v === undefined || v === "") return false;
    if (typeof v === "number") return v > 0;
    const normalized = String(v).trim().toLowerCase();
    return ["1", "1.0", "true", "on", "yes"].indexOf(normalized) >= 0;
  }
  function snapshot(data) { return data.latest_snapshot || {}; }
  function memory(data) {
    const raw = snapshot(data).serialized_memory;
    return raw && typeof raw === "object" ? raw : {};
  }
  function symbols(data) {
    const mode = data.mode || {};
    const mem = memory(data);
    const configured = String(mode.symbols || "").split(",").map((item) => item.trim()).filter(Boolean);
    const union = new Set(configured);
    Object.keys(mem.regimes || {}).forEach((item) => union.add(item));
    Object.keys(mem.prices || {}).forEach((item) => union.add(item));
    Object.keys(mem.entry_diagnostics || {}).forEach((item) => union.add(item));
    Object.keys(mem.liquidation_plugin || {}).forEach((item) => union.add(item));
    return Array.from(union);
  }
  function localTime(raw) {
    if (!raw) return "-";
    const date = new Date(raw);
    if (Number.isNaN(date.getTime())) return String(raw);
    return date.toLocaleString("zh-CN", { hour12:false });
  }
  function formatNumber(raw, digits) {
    if (raw === undefined || raw === null || raw === "") return "-";
    const num = Number(raw);
    if (!Number.isFinite(num)) return String(raw);
    return num.toFixed(digits).replace(/\.?0+$$/, "");
  }
  function price(raw) {
    const num = Number(raw);
    if (!Number.isFinite(num)) return value(raw);
    if (num >= 1000) return num.toFixed(2);
    if (num >= 10) return num.toFixed(4);
    return num.toFixed(6);
  }
  function pnl(raw) {
    if (raw === null || raw === undefined || raw === "") return "-";
    const num = Number(raw);
    if (!Number.isFinite(num)) return String(raw);
    return e("span", { className:num < 0 ? "pnl-bad" : "pnl-good" }, (num >= 0 ? "+" : "") + num.toFixed(4));
  }
  function isClosingOrder(row) {
    const side = String(row.side || "").toLowerCase();
    const positionSide = String(row.position_side || "").toLowerCase();
    return (positionSide === "long" && side === "sell") || (positionSide === "short" && side === "buy");
  }
  function orderPnl(row) {
    if (row.status === "exchange_bill") return pnl(row.realized_pnl);
    return isClosingOrder(row) ? pnl(row.realized_pnl) : "-";
  }
  function trailingActivatePrice(raw) {
    if (!raw || typeof raw !== "object") return null;
    const entry = Number(raw.entry_price), stop = Number(raw.stop_loss);
    if (!Number.isFinite(entry) || !Number.isFinite(stop) || stop <= 0) return null;
    const minR = Number(raw.min_trailing_activate_r || 1);
    const risk = Math.abs(stop - entry);
    const side = String(raw.position_side || raw.side || "").toLowerCase();
    return side === "short" ? entry - minR * risk : entry + minR * risk;
  }
  function conditionText(item) {
    const code = String((item && item.code) || "unknown");
    let label = conditionLabels[code] || code;
    if (!conditionLabels[code] && code.endsWith("_timeout")) label = (conditionLabels[code.replace(/_timeout$$/, "")] || code.replace(/_timeout$$/, "")) + "超时";
    if (code.indexOf("risk_") === 0) label = "风控允许下单：" + code.replace("risk_", "");
    if (!item || item.value === undefined) return label;
    return label + "，当前值 " + formatNumber(item.value, 4);
  }
  function conditionHint(item, metrics) {
    const code = String((item && item.code) || "unknown");
    const passed = !!(item && item.passed);
    if (code === "opportunity_score") {
      const minScore = metrics && metrics.min_score !== undefined ? formatNumber(metrics.min_score, 0) : "-";
      return passed ? "评分达到入场门槛。" : "评分不足，需要达到 " + minScore + " 才允许进入下一步。";
    }
    if (code === "rsi_35_58") return passed ? "RSI 位于反弹做空的可接受区间。" : "RSI 低于区间时容易在低位追空，等待反弹修复。";
    if (code === "rsi_42_66") return passed ? "RSI 位于回调做多的可接受区间。" : "RSI 不在做多确认区间，等待回调或动量修复。";
    if (code === "range_position_short_room") return passed ? "72h 区间仍有向下空间。" : "价格太靠近 72h 低位，继续追空空间不足。";
    if (code === "range_position_not_chasing") return passed ? "72h 区间位置没有明显追高。" : "价格太靠近 72h 高位，继续追多风险偏高。";
    if (code === "low_range_rebound") return passed ? "低位追空保护放行。" : "价格处在低位，需要先看到反弹后再转弱。";
    if (code === "multi_timeframe") return passed ? "5m、15m、1h 方向已经一致。" : "短中周期方向还没有完全一致。";
    if (code === "pullback_up") return passed ? "已经出现反弹，符合做空等待形态。" : "还没有出现足够反弹，避免直接追空。";
    if (code === "pullback_down") return passed ? "已经出现回调，符合做多等待形态。" : "还没有出现足够回调，避免直接追多。";
    if (code === "macd_cross_down") return passed ? "5m MACD 已给出转弱信号。" : "等待 5m MACD 死叉确认。";
    if (code === "macd_cross_up") return passed ? "5m MACD 已给出转强信号。" : "等待 5m MACD 金叉确认。";
    if (code === "bearish_candle") return passed ? "当前 5m K 线已转弱。" : "等待当前 5m K 线收成阴线。";
    if (code === "bullish_candle") return passed ? "当前 5m K 线已转强。" : "等待当前 5m K 线收成阳线。";
    if (code.indexOf("risk_") === 0) return passed ? "风控允许。" : "风控拒绝，通常是仓位、同向风险或盈亏比不达标。";
    return passed ? "已满足。" : "未满足，继续等待下一轮检查。";
  }
  function ConditionList({ title, items, metrics, kind }) {
    if (!items.length) return null;
    const icon = kind === "good" ? "✓" : "!";
    return e("div", { className:"condition-group" },
      e("div", { className:"condition-heading" }, e("span", null, title), e("span", null, items.length + " 项")),
      e("ul", { className:"condition-list" }, items.map((item, idx) => e("li", { className:"condition-item " + kind, key:idx },
        e("span", { className:"condition-icon" }, icon),
        e("span", { className:"condition-main" }, conditionText(item), e("div", { className:"condition-hint" }, conditionHint(item, metrics)))
      )))
    );
  }
  function summaryClass(summary) {
    if (["entry_conditions_met","signal_ready","release_hedge_signal_ready","exit_signal_ready","order_submitted"].indexOf(summary) >= 0) return "good";
    if (["risk_rejected","llm_rejected","symbol_loop_error"].indexOf(summary) >= 0) return "bad";
    return "warn";
  }
  function modeBadge(mode) {
    const exchange = String(mode.selected_exchange_id || mode.exchange_id || "okx").toUpperCase();
    let klass = "ok", text = exchange + " DRY RUN";
    if (String(mode.dry_run).toLowerCase() === "false" && String(mode.okx_demo).toLowerCase() === "false") { klass = "live"; text = exchange + " REAL"; }
    else if (String(mode.okx_demo).toLowerCase() === "true") { klass = "demo"; text = exchange + " DEMO"; }
    return e("span", { className:"status-pill " + klass }, e("span", { className:"status-dot" }), text);
  }
  function Panel(props) {
    return e("section", { className:"panel" }, e("div", { className:"panel-title" }, e("h2", null, props.title), props.extra || null), props.children);
  }
  function Metric(props) {
    return e("div", { className:"metric-tile" }, e("div", { className:"metric-label" }, props.label), e("div", { className:"metric-value" }, props.value));
  }
  function Empty(props) { return e("div", { className:"muted small" }, props.text || "暂无数据"); }
  function Table(props) {
    if (!props.rows || !props.rows.length) return e(Empty, { text: props.empty });
    return e("div", { className:"table-wrap" }, e("table", null,
      e("thead", null, e("tr", null, props.columns.map((col) => e("th", { key:col.key }, col.label)))),
      e("tbody", null, props.rows.map((row, idx) => e("tr", { key:idx }, props.columns.map((col) => e("td", { key:col.key }, col.render ? col.render(row) : value(row[col.key]))))))
    ));
  }
  function Overview({ data }) {
    const snap = snapshot(data), mem = memory(data), mode = data.mode || {};
    const exchange = String(mem.exchange_id || mode.selected_exchange_id || mode.exchange_id || "okx").toUpperCase();
    return e(React.Fragment, null,
      e("div", { className:"grid" },
        e(Metric, { label:"账户权益", value:formatNumber(snap.total_equity, 2) + " USDT" }),
        e(Metric, { label:"当前交易所", value:exchange }),
        e(Metric, { label:"订单总数", value:value(data.order_count, "0") }),
        e(Metric, { label:"对冲状态", value:snap.active_hedging ? "ON" : "OFF" }),
        e(Metric, { label:"行情源", value:String(mem.market_data_source || exchange).toUpperCase() }),
        e(Metric, { label:"快照时间", value:localTime(mem.snapshot_saved_at || snap.snapshot_time) }),
        e(Metric, { label:"引擎版本", value:value(mem.engine_version) }),
        e(Metric, { label:"更新时间", value:value(data.local_now) })
      )
    );
  }
  function Market({ data }) {
    const mem = memory(data);
    const rows = symbols(data).map((symbol) => ({ symbol, price:(mem.prices || {})[symbol], regime:(mem.regimes || {})[symbol], checked:(mem.regime_checked_at || {})[symbol] }));
    return e(Panel, { title:"当前行情" }, e(Table, { rows, empty:"暂无行情数据", columns:[
      {key:"symbol", label:"品种"}, {key:"price", label:"实时价格", render:(row) => price(row.price)}, {key:"regime", label:"行情判断", render:(row) => value(row.regime)}, {key:"checked", label:"最近判断时间", render:(row) => localTime(row.checked)}
    ]}));
  }
  function NoticeStrategy({ data }) {
    const mem = memory(data);
    const plugin = mem.liquidation_plugin || {};
    const rows = symbols(data).map((symbol) => Object.assign({ symbol }, plugin[symbol] || {}));
    return e(Panel, { title:"清算地图策略", extra:e("span", { className:"tag" }, "notice + 主策略互补") },
      e(Table, { rows, empty:"暂无清算地图数据", columns:[
        {key:"symbol", label:"品种"},
        {key:"status", label:"方向", render:(row) => value(row.status)},
        {key:"matched_scenario", label:"场景", render:(row) => value(row.matched_scenario)},
        {key:"weight", label:"权重", render:(row) => formatNumber(row.weight, 2)},
        {key:"suggested_entry_price", label:"建议下单价", render:(row) => price(row.suggested_entry_price)},
        {key:"stop_loss", label:"止损", render:(row) => price(row.stop_loss)},
        {key:"target_take_profit", label:"目标止盈", render:(row) => price(row.target_take_profit)},
        {key:"upper_strength", label:"上方空头清算", render:(row) => formatNumber(row.upper_strength, 2)},
        {key:"lower_strength", label:"下方多头清算", render:(row) => formatNumber(row.lower_strength, 2)},
        {key:"near_tie", label:"强度接近", render:(row) => row.near_tie === true ? "是" : (row.near_tie === false ? "否" : "-")},
        {key:"checked_at", label:"更新时间", render:(row) => localTime(row.checked_at)}
      ]}),
      e("div", { className:"diag-list", style:{ marginTop:"14px" } }, rows.map((row) => e("div", { className:"diag-card", key:row.symbol },
        e("div", { className:"diag-head" },
          e("div", null,
            e("div", { className:"diag-title" }, row.symbol),
            e("div", { className:"diag-action" }, value(row.trigger_condition || row.no_trade_reason || row.reason))
          ),
          e("span", { className:"tag " + (row.status === "failure" ? "bad" : (row.status === "no_trade" ? "warn" : "good")) }, value(row.status))
        ),
        e("div", { className:"metric-tags" },
          e("span", { className:"tag" }, "上方区间 " + price(row.upper_price_low) + " - " + price(row.upper_price_high)),
          e("span", { className:"tag" }, "下方区间 " + price(row.lower_price_low) + " - " + price(row.lower_price_high)),
          e("span", { className:"tag" }, "下单 " + price(row.suggested_entry_price)),
          e("span", { className:"tag" }, "止损 " + price(row.stop_loss)),
          e("span", { className:"tag" }, "止盈 " + price(row.target_take_profit)),
          e("span", { className:"tag" }, "对手盘 " + value(row.predicted_liquidation_side)),
          e("span", { className:"tag" }, "mapping " + value(row.mapping_version))
        ),
        e("div", { className:"muted small", style:{ marginTop:"10px" } }, value(row.basis || row.reason || row.no_trade_reason, "暂无依据"))
      )))
    );
  }
  function TradeFlow({ data }) {
    const mem = memory(data);
    const latestOrder = (data.orders || [])[0] || {};
    const steps = [
      ["行情扫描", "读取 5m/15m/1h/4h 与实时价格"],
      ["趋势识别", "按用户规则和过滤器生成行情类型"],
      ["信号评分", "等待回调、金叉/死叉和机会评分"],
      ["风控校验", "仓位、同向风险、盈亏比与 LLM 复核"],
      ["持仓监控", "止损、移动止盈、恢复仓位保护"]
    ];
    return e(Panel, { title:"交易过程动画", extra:e("span", { className:"tag" }, "最近订单 " + value(latestOrder.order_id)) },
      e("div", { className:"trade-flow" }, steps.map((step, idx) => e("div", { className:"flow-card", key:step[0] },
        e("div", { className:"flow-dot", style:{ animationDelay:(idx * 0.16) + "s" } }),
        e("div", { className:"flow-title" }, step[0]),
        e("div", { className:"flow-desc" }, step[1])
      ))),
      e("div", { className:"two-col", style:{ marginTop:"14px" } },
        e(Panel, { title:"风控状态" }, e(DictTable, { data:mem.risk || {} })),
        e(Panel, { title:"持仓监控" }, e(DictTable, { data:mem.position_monitor || {} }))
      )
    );
  }
  function Diagnostics({ data }) {
    const diag = memory(data).entry_diagnostics || {};
    const rows = symbols(data);
    if (!rows.length) return e(Panel, { title:"入场诊断" }, e(Empty, { text:"暂无诊断数据，等待实盘引擎保存下一次快照" }));
    return e(Panel, { title:"入场诊断", extra:e("span", { className:"tag" }, "解释为什么没下单") },
      e("div", { className:"diag-list" }, rows.map((symbol) => {
        const item = diag[symbol];
        if (!item || typeof item !== "object") return e("div", { className:"diag-card", key:symbol },
          e("div", { className:"diag-head" }, e("div", null, e("div", { className:"diag-title" }, symbol), e("div", { className:"diag-action" }, "等待下一轮策略检查")), e("span", { className:"tag warn" }, "暂无数据")),
          e("div", { className:"muted small" }, "引擎还没有写入该品种的入场诊断。通常是 app 尚未重启到新版本，或该品种循环还没跑到行情/仓位检查。")
        );
        const requirements = Array.isArray(item.requirements) ? item.requirements : [];
        const blockers = Array.isArray(item.blockers) ? item.blockers : requirements.filter((req) => !req.passed);
        const passedItems = requirements.filter((req) => req && req.passed);
        const metrics = item.metrics && typeof item.metrics === "object" ? item.metrics : {};
        const keys = ["price","rsi_5m","opportunity_score","min_score","close_position_72h","ret_24h","ret_72h","volatility_tier"];
        const total = requirements.length || (passedItems.length + blockers.length);
        const passedCount = passedItems.length;
        return e("div", { className:"diag-card", key:symbol },
          e("div", { className:"diag-head" }, e("div", null, e("div", { className:"diag-title" }, symbol), e("div", { className:"diag-action" }, actionLabels[item.action] || value(item.action))), e("span", { className:"tag " + summaryClass(item.summary) }, summaryLabels[item.summary] || value(item.summary))),
          e("div", { className:"diag-progress" },
            e("span", { className:"tag " + (blockers.length ? "warn" : "good") }, total ? "条件 " + passedCount + "/" + total : "等待条件数据"),
            metrics.opportunity_score !== undefined ? e("span", { className:"tag" }, "评分 " + formatNumber(metrics.opportunity_score, 0) + " / " + formatNumber(metrics.min_score, 0)) : null
          ),
          blockers.length ? e(ConditionList, { title:"还在等待", items:blockers, metrics, kind:"wait" }) : e("div", { className:"muted small" }, "当前入场条件已满足，等待风控、交易执行或下一轮检查。"),
          e(ConditionList, { title:"已经满足", items:passedItems, metrics, kind:"good" }),
          e("div", { className:"metric-tags" }, keys.filter((key) => metrics[key] !== undefined).map((key) => e("span", { className:"tag", key:key }, (metricLabels[key] || key) + ": " + formatNumber(metrics[key], 4))))
        );
      }))
    );
  }
  function protectedRows(mem) {
    const rows = {};
    (Array.isArray(mem.positions) ? mem.positions : []).forEach((raw) => {
      if (!raw || typeof raw !== "object") return;
      const symbol = String(raw.symbol || ""), side = String(raw.side || "");
      if (!symbol || !side) return;
      rows[symbol + ":" + side] = { symbol, side, contracts:raw.contracts, entry_price:raw.entry_price, source:"交易所持仓" };
    });
    Object.entries(mem.trailing_states || {}).forEach(([key, raw]) => {
      if (!raw || typeof raw !== "object") return;
      const symbol = String(raw.symbol || key.split(":")[0]), side = String(raw.position_side || key.split(":").pop());
      rows[symbol + ":" + side] = Object.assign(rows[symbol + ":" + side] || { symbol, side }, {
        entry_price:raw.entry_price,
        stop_loss:raw.stop_loss,
        take_profit:raw.take_profit,
        trailing_activate_price:trailingActivatePrice(raw),
        trailing_active:!!raw.active,
        atr:raw.atr,
        source:"移动止盈状态"
      });
    });
    Object.entries(mem.recovered_positions || {}).forEach(([key, raw]) => {
      if (!raw || typeof raw !== "object") return;
      const symbol = String(raw.symbol || key.split(":")[0]), side = String(raw.side || key.split(":").pop());
      rows[symbol + ":" + side] = Object.assign(rows[symbol + ":" + side] || { symbol, side }, raw, { source:"恢复持仓" });
    });
    return Object.values(rows);
  }
  function Positions({ data }) {
    const rows = protectedRows(memory(data));
    return e(Panel, { title:"持仓保护" }, e(Table, { rows, empty:"暂无持仓保护数据", columns:[
      {key:"symbol", label:"品种"}, {key:"side", label:"方向"}, {key:"contracts", label:"数量", render:(row) => formatNumber(row.contracts, 6)}, {key:"entry_price", label:"开仓价", render:(row) => price(row.entry_price)},
      {key:"stop_loss", label:"保护止损", render:(row) => price(row.stop_loss)}, {key:"take_profit", label:"止盈", render:(row) => price(row.take_profit)},
      {key:"trailing_activate_price", label:"trailing激活价", render:(row) => price(row.trailing_activate_price)}, {key:"trailing_active", label:"trailing已激活", render:(row) => row.trailing_active ? "true" : "false"},
      {key:"atr", label:"ATR", render:(row) => formatNumber(row.atr, 6)}, {key:"regime", label:"恢复行情"}, {key:"source", label:"来源"}
    ]}));
  }
  function Orders({ data }) {
    return e(Panel, { title:"最近订单", extra:e("span", { className:"tag" }, "最多 30 条") }, e(Table, { rows:data.orders || [], empty:"暂无订单", columns:[
      {key:"created_at", label:"时间", render:(row) => localTime(row.created_at)}, {key:"symbol", label:"品种"}, {key:"regime_mode", label:"行情"}, {key:"side", label:"方向"}, {key:"position_side", label:"持仓方向"},
      {key:"initial_qty", label:"数量", render:(row) => formatNumber(row.initial_qty, 6)}, {key:"avg_price", label:"均价", render:(row) => price(row.avg_price)}, {key:"fee_paid", label:"手续费", render:(row) => formatNumber(row.fee_paid, 6)},
      {key:"realized_pnl", label:"已实现盈亏", render:(row) => orderPnl(row)}, {key:"status", label:"状态"}, {key:"order_id", label:"订单ID"}
    ]}));
  }
  function DictTable({ data }) {
    const rows = Object.entries(data || {}).map(([key, val]) => ({ key, val }));
    return e(Table, { rows, empty:"暂无数据", columns:[{key:"key", label:"字段"}, {key:"val", label:"值", render:(row) => typeof row.val === "object" ? e("pre", null, JSON.stringify(row.val, null, 2)) : String(value(row.val))}] });
  }
  const runtimePresets = [
    {
      key:"balanced_3x_scout",
      title:"推荐均衡 3x",
      desc:"当前建议实盘档：启用动态仓位和 SHOCK 趋势先遣单，追求收益同时限制单信号放大。",
      values:{
        risk_percent:"0.012",
        same_direction_risk_limit:"0.08",
        max_signal_risk_multiplier:"3",
        confirmation_position_sizing:"1",
        confirmation_max_risk_multiplier:"3",
        enable_shock_trend_scout:"1",
        shock_trend_scout_risk_multiplier:"1.5"
      }
    },
    {
      key:"conservative_2x",
      title:"保守 2x",
      desc:"降低同向风险和单信号放大，适合新参数观察期或连续回撤后临时降风险。",
      values:{
        same_direction_risk_limit:"0.04",
        max_signal_risk_multiplier:"2",
        confirmation_position_sizing:"1",
        confirmation_max_risk_multiplier:"2",
        enable_shock_trend_scout:"1",
        shock_trend_scout_risk_multiplier:"1"
      }
    },
    {
      key:"aggressive_reference",
      title:"旧激进参考",
      desc:"接近早期高收益回测的风险参数，回撤会明显放大；只建议短期手动验证。",
      values:{
        same_direction_risk_limit:"0.20",
        max_signal_risk_multiplier:"20",
        confirmation_position_sizing:"1",
        confirmation_max_risk_multiplier:"20",
        enable_shock_trend_scout:"1",
        shock_trend_scout_risk_multiplier:"1.5"
      }
    }
  ];
  function normalizedPresetValue(val) {
    if (val === true) return "1";
    if (val === false) return "0";
    if (typeof val === "number") return String(Number(val.toFixed(8)));
    return String(value(val, ""));
  }
  function presetActive(form, preset) {
    return Object.entries(preset.values).every(([key, val]) => normalizedPresetValue(form[key]) === String(val));
  }
  function RuntimeConfig({ data, onData }) {
    const [form, setForm] = React.useState(data.runtime_config || {});
    const [status, setStatus] = React.useState("");
    React.useEffect(() => setForm(data.runtime_config || {}), [data.runtime_config]);
    function applyPreset(preset) {
      setForm(Object.assign({}, form, preset.values));
      setStatus("已填充：" + preset.title + "，点击保存后生效");
    }
    async function submit(event) {
      event.preventDefault();
      setStatus("保存中...");
      const response = await fetch("/api/runtime-config", { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(form) });
      const body = await response.json().catch(() => ({}));
      if (response.ok) { setStatus("已保存，实盘引擎下一轮自动应用"); onData && onData(Object.assign({}, data, { runtime_config: body.runtime_config || form })); }
      else setStatus(body.detail || "保存失败");
    }
    return e(Panel, { title:"动态风控配置" }, e("form", { onSubmit:submit },
      e("div", { className:"preset-grid" }, runtimePresets.map((preset) => e("button", { type:"button", key:preset.key, className:"preset-card " + (presetActive(form, preset) ? "active" : ""), onClick:() => applyPreset(preset) },
        e("span", { className:"preset-title" }, preset.title),
        e("span", { className:"preset-desc" }, preset.desc),
        e("span", { className:"preset-values" },
          e("span", { className:"tag" }, "信号 " + preset.values.max_signal_risk_multiplier + "x"),
          e("span", { className:"tag" }, "同向 " + formatNumber(Number(preset.values.same_direction_risk_limit) * 100, 1) + "%"),
          e("span", { className:"tag good" }, preset.values.enable_shock_trend_scout === "1" ? "Scout 开" : "Scout 关")
        )
      ))),
      e("div", { className:"form-grid" }, (data.runtime_fields || []).map((field) => {
        const checked = boolEnabled(form[field.key]);
        if (field.boolean) return e("label", { className:"field switch-row", key:field.key }, e("span", null, e("span", { className:"field-title" }, field.label), e("div", { className:"field-note" }, field.description || "")), e("input", { type:"checkbox", name:field.key, checked, onChange:(event) => setForm(Object.assign({}, form, { [field.key]: event.target.checked ? "1" : "0" })) }));
        const inputType = ["email_pass", "llm_regime_api_key"].includes(field.key) ? "password" : (field.text ? "text" : "number");
        return e("label", { className:"field", key:field.key }, e("span", { className:"field-title" }, field.label), e("input", { type:inputType, name:field.key, min:field.min, max:field.max, step:field.step || "any", value:value(form[field.key], ""), onChange:(event) => setForm(Object.assign({}, form, { [field.key]: event.target.value })) }), e("span", { className:"field-note" }, field.description || ""));
      })),
      e("div", { className:"actions" }, e("button", { type:"submit" }, "保存配置"), e("span", { className:"muted small" }, status))
    ));
  }
  function Exchange({ data }) {
    const current = String((data.mode || {}).selected_exchange_id || (data.mode || {}).exchange_id || "okx").toLowerCase();
    const [exchange, setExchange] = React.useState(current);
    const [status, setStatus] = React.useState("");
    const [testStatus, setTestStatus] = React.useState("");
    const [testRows, setTestRows] = React.useState([]);
    const [qr, setQr] = React.useState(null);
    const [token, setToken] = React.useState("");
    React.useEffect(() => setExchange(current), [current]);
    React.useEffect(() => {
      if (!token) return undefined;
      const timer = setInterval(async () => {
        const response = await fetch("/api/hotcoin/qr/poll", { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({ qr_token:token }) });
        const body = await response.json().catch(() => ({}));
        if (body.status === "connected") { clearInterval(timer); setToken(""); setStatus("扫码成功，Hotcoin token 已保存到数据库"); }
        else if (body.status === "error") { clearInterval(timer); setToken(""); setStatus(body.message || "扫码失败"); }
        else setStatus("等待扫码确认...");
      }, 2000);
      return () => clearInterval(timer);
    }, [token]);
    async function save(event) {
      event.preventDefault();
      setStatus("保存中...");
      const response = await fetch("/api/exchange/select", { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({ exchange_id:exchange }) });
      const body = await response.json().catch(() => ({}));
      setStatus(response.ok ? "已保存；实盘 app 需要重启才会切换交易所" : (body.detail || "保存失败"));
    }
    async function startQr() {
      setStatus("正在生成二维码...");
      const response = await fetch("/api/hotcoin/qr/start", { method:"POST" });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) { setStatus(body.detail || "二维码生成失败"); return; }
      setQr(body.image); setToken(body.qr_token); setStatus("请用 Hotcoin App 扫码确认");
    }
    async function testHotcoin() {
      setTestStatus("测试中...");
      setTestRows([]);
      const response = await fetch("/api/hotcoin/test-session", { method:"POST" });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) { setTestStatus(body.detail || "测试失败"); return; }
      setTestStatus(body.summary || "测试完成");
      setTestRows(body.checks || []);
    }
    return e(Panel, { title:"交易所连接" },
      e("form", { onSubmit:save, className:"two-col" },
        e("label", { className:"field" }, e("span", { className:"field-title" }, "交易所"), e("select", { value:exchange, onChange:(event) => setExchange(event.target.value) }, e("option", { value:"okx" }, "OKX"), e("option", { value:"hotcoin" }, "Hotcoin"))),
        e("div", { className:"actions" }, e("button", { type:"submit" }, "保存交易所"), e("span", { className:"muted small" }, status))
      ),
      e("div", { className:"qr-box" }, qr ? e("img", { src:qr, alt:"Hotcoin QR" }) : e("div", { className:"panel", style:{ minHeight:"180px", display:"grid", placeItems:"center" } }, e("span", { className:"muted small" }, "Hotcoin 二维码")), e("div", null,
        e("div", { className:"actions" }, e("button", { type:"button", className:"ghost-button", onClick:startQr }, "生成 Hotcoin 扫码二维码"), e("button", { type:"button", className:"ghost-button", onClick:testHotcoin }, "测试 Hotcoin 会话")),
        e("div", { className:"muted small", style:{ marginTop:"10px" } }, "扫码成功后 token 会保存到数据库，交易所切换仍需重启实盘 app。"),
        testStatus ? e("div", { className:"muted small", style:{ marginTop:"10px" } }, testStatus) : null
      )),
      testRows.length ? e(Table, { rows:testRows, columns:[{key:"name", label:"检查项"}, {key:"ok", label:"结果", render:(row) => row.ok ? "通过" : "失败"}, {key:"code", label:"Code", render:(row) => value(row.code)}, {key:"msg", label:"返回", render:(row) => value(row.msg)}] }) : null,
      e(Panel, { title:"已保存会话" }, e(Table, { rows:data.exchange_sessions || [], empty:"暂无会话", columns:[{key:"exchange_id", label:"交易所"}, {key:"connected", label:"状态", render:(row) => row.connected ? "已连接" : "未连接"}, {key:"token", label:"Token"}, {key:"device_id", label:"设备"}, {key:"updated_at", label:"更新时间", render:(row) => localTime(row.updated_at)}] }))
    );
  }
  function Tuning({ data }) {
    const report = data.latest_tuning_report;
    if (!report) return e(Panel, { title:"自动调参建议" }, e(Empty, { text:"暂无调参报告" }));
    const rows = [{ type:"当前实盘参数回测", values:report.current || {} }, { type:"推荐参数回测", values:report.recommended || {} }];
    return e(Panel, { title:"自动调参建议", extra:e("a", { className:"tag", href:"/reports/" + report.name, target:"_blank" }, "打开报告") },
      e("div", { className:"muted small", style:{ marginBottom:"10px" } }, "最近生成：" + localTime(report.mtime) + " · " + value(report.reason)),
      e(Table, { rows, columns:[{key:"type", label:"类型"}, {key:"candidate", label:"参数档", render:(row) => value(row.values.candidate)}, {key:"pnl", label:"收益", render:(row) => value(row.values.pnl)}, {key:"win_rate", label:"胜率", render:(row) => value(row.values.win_rate)}, {key:"trades", label:"交易次数", render:(row) => value(row.values.trades)}] })
    );
  }
  function System({ data }) {
    return e("div", { className:"two-col" },
      e(Panel, { title:"运行配置" }, e(DictTable, { data:data.mode || {} })),
      e(Panel, { title:"锁仓状态" }, e(DictTable, { data:memory(data).hedge_locks || {} }))
    );
  }
  function App() {
    const [data, setData] = React.useState(window.__DASHBOARD_DATA__ || {});
    const [view, setView] = React.useState(localStorage.getItem("dashboard:view") || "overview");
    const [drawer, setDrawer] = React.useState(false);
    const [refreshing, setRefreshing] = React.useState(false);
    React.useEffect(() => {
      localStorage.setItem("dashboard:view", view);
      setDrawer(false);
    }, [view]);
    React.useEffect(() => {
      const load = async () => {
        setRefreshing(true);
        const response = await fetch("/api/status");
        if (response.status === 401) { window.location.href = "/login"; return; }
        if (response.ok) setData(await response.json());
        setRefreshing(false);
      };
      const timer = setInterval(load, 10000);
      return () => clearInterval(timer);
    }, []);
    const mode = data.mode || {};
    const viewTitle = (navItems.find((item) => item[0] === view) || navItems[0])[1];
    let body;
    if (view === "overview") body = e(Overview, { data });
    else if (view === "market") body = e(Market, { data });
    else if (view === "notice") body = e(NoticeStrategy, { data });
    else if (view === "flow") body = e(TradeFlow, { data });
    else if (view === "diagnostics") body = e(Diagnostics, { data });
    else if (view === "positions") body = e(Positions, { data });
    else if (view === "orders") body = e(Orders, { data });
    else if (view === "risk") body = e(RuntimeConfig, { data, onData:setData });
    else if (view === "exchange") body = e(Exchange, { data });
    else if (view === "tuning") body = e(Tuning, { data });
    else body = e(System, { data });
    return e(React.Fragment, null,
      e("div", { className:"mobile-top" }, e("button", { className:"ghost-button", onClick:() => setDrawer(true) }, "菜单"), e("strong", null, viewTitle), modeBadge(mode)),
      e("div", { className:drawer ? "drawer-mask open" : "drawer-mask", onClick:() => setDrawer(false) }),
      e("div", { className:"app-shell" },
        e("aside", { className:drawer ? "sidebar open" : "sidebar" },
          e("div", { className:"brand" }, e("h1", { className:"brand-title" }, "Quant Dashboard"), e("div", { className:"brand-subtitle" }, "本地时间 " + value(data.local_now) + " · 10 秒自动刷新")),
          e("nav", { className:"nav" }, navItems.map((item) => e("button", { key:item[0], className:"nav-button " + (view === item[0] ? "active" : ""), onClick:() => setView(item[0]) }, e("span", { className:"nav-icon" }, item[2]), e("span", null, item[1]), item[0] === "orders" ? e("span", { className:"nav-count" }, value(data.order_count, "0")) : null))),
          e("div", { className:"sidebar-footer" }, e("form", { method:"post", action:"/logout" }, e("button", { className:"ghost-button", type:"submit" }, "退出")), modeBadge(mode))
        ),
        e("main", { className:"content" },
          e("header", { className:"page-head" }, e("div", null, e("h1", { className:"page-title" }, viewTitle), e("div", { className:"page-meta" }, "最新快照 " + localTime((memory(data).snapshot_saved_at || snapshot(data).snapshot_time)) + (refreshing ? " · 刷新中" : ""))), e("div", { className:"top-actions" }, modeBadge(mode), e("button", { className:"ghost-button", onClick:async () => { const response = await fetch("/api/status"); if (response.ok) setData(await response.json()); } }, "立即刷新"))),
          body
        )
      )
    );
  }
  ReactDOM.createRoot(document.getElementById("root")).render(e(App));
})();
</script>
</body>
</html>""")
    return template.substitute(initial_data=initial_json)


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
    <h1>Quant Dashboard</h1>
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
    exchange_id = str(mode.get("selected_exchange_id") or mode.get("exchange_id") or "okx").upper()
    if str(mode.get("dry_run")).lower() == "false" and str(mode.get("okx_demo")).lower() == "false":
        return f'<span class="status live">{escape(exchange_id)} REAL</span>'
    if str(mode.get("okx_demo")).lower() == "true":
        return f'<span class="status demo">{escape(exchange_id)} DEMO</span>'
    return f'<span class="status ok">{escape(exchange_id)} DRY RUN</span>'


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


def recovered_positions_panel(
    recovered: dict[str, Any],
    trailing_states: dict[str, Any],
    live_positions: list[Any],
) -> str:
    positions = protected_position_rows(recovered, trailing_states, live_positions)
    if not positions:
        return '<div class="muted">暂无持仓保护数据。程序当前没有发现需要从交易所现有持仓重建保护状态的仓位。</div>'
    rows = ""
    for key, item in sorted(positions.items()):
        rows += (
            "<tr>"
            f"<td>{escape(str(item.get('symbol') or key))}</td>"
            f"<td>{escape(str(item.get('side') or '-'))}</td>"
            f"<td>{escape(format_metric_value(item.get('contracts', '-')))}</td>"
            f"<td>{escape(format_price(item.get('entry_price')))}</td>"
            f"<td>{escape(format_price(item.get('stop_loss')))}</td>"
            f"<td>{escape(format_price(item.get('take_profit')))}</td>"
            f"<td>{escape(format_price(item.get('trailing_activate_price')))}</td>"
            f"<td>{escape(str(item.get('trailing_active') if item.get('trailing_active') is not None else '-'))}</td>"
            f"<td>{escape(format_metric_value(item.get('atr', '-')))}</td>"
            f"<td>{escape(str(item.get('regime') or '-'))}</td>"
            f"<td>{escape(str(item.get('source') or '-'))}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>品种</th><th>方向</th><th>数量</th><th>开仓价</th>"
        "<th>保护止损</th><th>止盈</th><th>trailing激活价</th><th>trailing已激活</th><th>ATR</th><th>恢复行情</th><th>来源</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )


def protected_position_rows(
    recovered: dict[str, Any],
    trailing_states: dict[str, Any],
    live_positions: list[Any],
) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for raw in live_positions if isinstance(live_positions, list) else []:
        if not isinstance(raw, dict):
            continue
        symbol = str(raw.get("symbol") or "")
        side = str(raw.get("side") or "")
        if not symbol or not side:
            continue
        key = f"{symbol}:{side}"
        rows[key] = {
            "symbol": symbol,
            "side": side,
            "contracts": raw.get("contracts"),
            "entry_price": raw.get("entry_price"),
            "source": "交易所持仓",
        }
    for key, raw in trailing_states.items() if isinstance(trailing_states, dict) else []:
        if not isinstance(raw, dict):
            continue
        symbol = str(raw.get("symbol") or key.rsplit(":", 1)[0])
        side = str(raw.get("position_side") or key.rsplit(":", 1)[-1])
        row_key = f"{symbol}:{side}"
        row = rows.setdefault(row_key, {"symbol": symbol, "side": side})
        row.update(
            {
                "entry_price": raw.get("entry_price", row.get("entry_price")),
                "stop_loss": raw.get("stop_loss"),
                "take_profit": raw.get("take_profit"),
                "trailing_activate_price": trailing_activate_price(raw),
                "trailing_active": bool(raw.get("active")),
                "atr": raw.get("atr"),
                "source": "移动止盈状态",
            }
        )
    for key, raw in recovered.items() if isinstance(recovered, dict) else []:
        if not isinstance(raw, dict):
            continue
        symbol = str(raw.get("symbol") or key.rsplit(":", 1)[0])
        side = str(raw.get("side") or key.rsplit(":", 1)[-1])
        row_key = f"{symbol}:{side}"
        row = rows.setdefault(row_key, {"symbol": symbol, "side": side})
        for field in ("contracts", "entry_price", "stop_loss", "take_profit", "atr", "regime", "risk_multiplier"):
            value = raw.get(field)
            if value not in {None, "", 0, 0.0}:
                row[field] = value
        row["source"] = "恢复持仓"
    return rows


def trailing_activate_price(raw: dict[str, Any]) -> float | None:
    entry = _float(raw.get("entry_price"))
    stop = _float(raw.get("stop_loss"))
    if entry <= 0 or stop <= 0:
        return None
    min_r = _float(raw.get("min_trailing_activate_r")) or 1.0
    risk = abs(stop - entry)
    side = str(raw.get("position_side") or raw.get("side") or "").lower()
    return entry - min_r * risk if side == "short" else entry + min_r * risk


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
        "low_range_rebound": "低位追空保护通过",
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


def exchange_panel(mode: dict[str, Any], sessions: list[dict[str, Any]]) -> str:
    selected = str(mode.get("selected_exchange_id") or mode.get("exchange_id") or "okx").lower()
    configured = str(mode.get("exchange_id") or "okx").lower()
    hotcoin_session = next((item for item in sessions if item.get("exchange_id") == "hotcoin"), None)
    connected = bool(hotcoin_session and hotcoin_session.get("connected"))
    updated_at = format_local_time(hotcoin_session.get("updated_at")) if hotcoin_session else "-"
    okx_selected = " selected" if selected == "okx" else ""
    hotcoin_selected = " selected" if selected == "hotcoin" else ""
    return (
        '<form id="exchange-select-form">'
        '<div class="form-grid">'
        '<label><span class="field-title">交易所</span>'
        f'<select name="exchange_id"><option value="okx"{okx_selected}>OKX</option>'
        f'<option value="hotcoin"{hotcoin_selected}>Hotcoin</option></select>'
        '<span class="field-note">保存后会记录到数据库；实盘 app 初始化交易所依赖 EXCHANGE_ID，切换后需要重启 app。</span></label>'
        f'{dict_table({"当前 app 配置": configured, "网页选择": selected, "Hotcoin 登录": "已连接" if connected else "未连接", "Hotcoin 保存时间": updated_at})}'
        '</div>'
        '<div class="actions"><button type="submit">保存交易所选择</button>'
        '<button id="hotcoin-qr-start" class="ghost-button" type="button">Hotcoin 扫码登录</button>'
        '<span id="exchange-select-status" class="muted"></span></div>'
        '<div class="qr-box">'
        '<img id="hotcoin-qr-image" alt="Hotcoin QR Code">'
        '<div><div id="hotcoin-login-status" class="muted">选择 Hotcoin 后点击扫码登录；token 只保存到数据库，不会显示在网页。</div></div>'
        '</div>'
        '</form>'
    )


def dict_table(values: dict[str, Any]) -> str:
    if not values:
        return '<div class="muted">暂无数据</div>'
    rows = "".join(
        f"<tr><th>{escape(str(key))}</th><td>{escape(short_value(value))}</td></tr>"
        for key, value in values.items()
    )
    return f"<table>{rows}</table>"


def localized_monitor_status(values: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(values, dict) or not values:
        return {}
    return {
        "监控间隔秒数": values.get("interval_seconds"),
        "最近检查时间": format_local_time(values.get("last_checked_at")),
        "最近检查品种": values.get("last_symbol"),
        "最近监控价格": format_price(values.get("last_price")),
        "累计检查次数": values.get("checks"),
        "累计触发退出信号": values.get("exit_signals"),
    }


def runtime_config_form(fields: list[dict[str, Any]], values: dict[str, Any]) -> str:
    controls = []
    for field in fields:
        key = str(field["key"])
        hint = f"{field['min']} - {field['max']}"
        if field.get("percent"):
            hint += "，0.01=1%"
        input_type = "checkbox" if field.get("boolean") else "password" if key in {"email_pass", "llm_regime_api_key"} else "text" if field.get("text") else "number"
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
            f"<td>{escape(str(order.get('exchange_id') or 'okx'))}</td>"
            f"<td>{escape(str(order.get('symbol', '')))}</td>"
            f"<td>{escape(str(order.get('regime_mode', '')))}</td>"
            f"<td>{escape(str(order.get('status', '')))}</td>"
            f"<td>{escape(str(order.get('side', '')))} / {escape(str(order.get('position_side', '')))}</td>"
            f"<td>{escape(str(order.get('initial_qty', '')))}</td>"
            f"<td>{escape(str(order.get('filled_qty', '')))}</td>"
            f"<td>{escape(format_price(order.get('avg_price')))}</td>"
            f"<td>{escape(str(order.get('fee_paid', '')))}</td>"
            f"<td>{escape(format_pnl(order.get('realized_pnl')))}</td>"
            f"<td>{escape(str(order.get('signal_reason', '')))}</td>"
            f"<td>{escape(str(order.get('order_id', '')))}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>时间</th><th>交易所</th><th>品种</th><th>行情</th><th>状态</th><th>方向</th>"
        "<th>数量</th><th>已成交</th><th>均价</th><th>手续费</th><th>盈亏</th><th>原因</th><th>订单ID</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )


def format_pnl(value: Any) -> str:
    if value is None:
        return "-"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{number:.4f}"

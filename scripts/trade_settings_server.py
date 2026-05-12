#!/usr/bin/env python3
"""Small settings UI for selecting the execution venue and Hotcoin QR login."""

from __future__ import annotations

import base64
import html
import json
import os
import secrets
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Annotated, Any

import requests
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from scripts.hotcoin_adapter import HotcoinError, HotcoinWebClient, _load_dotenv


ROOT = Path(os.getenv("TRADE_SETTINGS_ROOT", "/workspace"))
ENV_PATH = ROOT / ".env"
CONFIG_PATH = ROOT / "user_data" / "config.json"
STATE_PATH = ROOT / "user_data" / "trade_execution.json"
SESSION_PATH = ROOT / "user_data" / "hotcoin_session.json"
PUBLIC_BASE_PATH = os.getenv("PUBLIC_BASE_PATH", "").rstrip("/")

security = HTTPBasic()
app = FastAPI(title="Trade Execution Settings")

_qr_lock = threading.Lock()
_qr_state: dict[str, Any] = {
    "token": None,
    "image_url": None,
    "status": "idle",
    "message": "",
    "started_at": None,
}


def _auth(credentials: Annotated[HTTPBasicCredentials, Depends(security)]) -> str:
    _load_dotenv(ENV_PATH)
    username = os.getenv("SETTINGS_USERNAME") or os.getenv("FREQTRADE__API_SERVER__USERNAME")
    password = os.getenv("SETTINGS_PASSWORD") or os.getenv("FREQTRADE__API_SERVER__PASSWORD")
    username = username or "freqtrader"
    password = password or "SuperSecurePassword"
    ok = secrets.compare_digest(credentials.username, username) and secrets.compare_digest(
        credentials.password, password
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


def _read_env() -> dict[str, str]:
    if not ENV_PATH.exists():
        return {}
    data: dict[str, str] = {}
    for raw in ENV_PATH.read_text().splitlines():
        if not raw.strip() or raw.lstrip().startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        data[key.strip()] = value.strip().strip('"').strip("'")
    return data


def _write_env(updates: dict[str, str]) -> None:
    lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
    seen: set[str] = set()
    out: list[str] = []
    for raw in lines:
        if "=" not in raw or raw.lstrip().startswith("#"):
            out.append(raw)
            continue
        key = raw.split("=", 1)[0].strip()
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(raw)
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(out).rstrip() + "\n")


def _restart_freqtrade() -> tuple[bool, str]:
    command = ["docker", "compose", "up", "-d", "--force-recreate", "freqtrade"]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=120)
    output = (result.stdout + "\n" + result.stderr).strip()
    return result.returncode == 0, output


def _read_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    try:
        data = json.loads(STATE_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _read_freqtrade_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        data = json.loads(CONFIG_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_freqtrade_dry_run(dry_run: bool) -> None:
    config = _read_freqtrade_config()
    if not config:
        raise HTTPException(status_code=500, detail=f"Config not found or invalid: {CONFIG_PATH}")
    config["dry_run"] = dry_run
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=4) + "\n")


def _write_state(data: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _redirect_url(message: str) -> str:
    return f"{PUBLIC_BASE_PATH}/?message={urllib.parse.quote(message)}"


def _masked(value: str | None) -> str:
    if not value:
        return "未配置"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def _read_hotcoin_session() -> dict[str, Any]:
    if not SESSION_PATH.exists():
        return {}
    try:
        data = json.loads(SESSION_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _hotcoin_login_status(env: dict[str, str] | None = None) -> dict[str, Any]:
    env = env or _read_env()
    session = _read_hotcoin_session()
    token = session.get("token") or env.get("HOTCOIN_WEB_TOKEN")
    return {
        "token_mask": _masked(token),
        "has_token": bool(token),
        "session_exists": SESSION_PATH.exists(),
        "verified": None,
        "message": "已保存登录态，建议点击验证确认是否仍有效。" if token else "未登录，请扫码登录。",
    }


def _verify_hotcoin_login() -> dict[str, Any]:
    status_data = _hotcoin_login_status()
    if not status_data["has_token"]:
        return {**status_data, "verified": False, "message": "未登录，请扫码登录。"}
    try:
        client = HotcoinWebClient(session_path=SESSION_PATH)
        data = client.user_info()
        ok = isinstance(data, dict) and data.get("code") == 200 and bool(data.get("data"))
        return {
            **_hotcoin_login_status(),
            "verified": ok,
            "message": "Hotcoin 登录态有效。" if ok else f"Hotcoin 登录态可能已失效：{data}",
        }
    except Exception as exc:
        return {
            **_hotcoin_login_status(),
            "verified": False,
            "message": f"验证失败，可能已失效或网络异常：{exc}",
        }


def _selected_exchange(env: dict[str, str]) -> str:
    state = _read_state()
    if state.get("hotcoin_signal_bridge_enabled") is True:
        return "hotcoin"
    if state.get("hotcoin_signal_bridge_enabled") is False:
        return "freqtrade"
    if env.get("HOTCOIN_SIGNAL_BRIDGE_ENABLED", "false").lower() in {"1", "true", "yes", "on"}:
        return "hotcoin"
    return "freqtrade"


def _page(message: str = "") -> str:
    env = _read_env()
    state = _read_state()
    freqtrade_config = _read_freqtrade_config()
    dry_run = bool(freqtrade_config.get("dry_run", True))
    selected = _selected_exchange(env)
    bridge_enabled = str(
        state.get("hotcoin_signal_bridge_enabled", env.get("HOTCOIN_SIGNAL_BRIDGE_ENABLED", "false"))
    ).lower()
    execute = str(state.get("hotcoin_signal_execute", env.get("HOTCOIN_SIGNAL_EXECUTE", "false"))).lower()
    amount = str(state.get("hotcoin_order_amount", env.get("HOTCOIN_ORDER_AMOUNT", "0")))
    order_type = str(state.get("hotcoin_order_type", env.get("HOTCOIN_ORDER_TYPE", "market")))
    login_status = _hotcoin_login_status(env)
    with _qr_lock:
        qr = dict(_qr_state)

    qr_block = ""
    if qr.get("image_url"):
        qr_block = f"""
        <section class="card qr" id="qr-section">
          <h2>Hotcoin 扫码登录</h2>
          <img src="{html.escape(qr['image_url'])}" alt="Hotcoin QR Code" />
          <p>状态：<span id="qr-status">{html.escape(str(qr.get('status', '')))}</span></p>
          <p id="qr-message">{html.escape(str(qr.get('message', '')))}</p>
        </section>
        """

    checked_freqtrade = "checked" if selected == "freqtrade" else ""
    checked_hotcoin = "checked" if selected == "hotcoin" else ""
    checked_execute = "checked" if execute.lower() in {"1", "true", "yes", "on"} else ""
    selected_market = "selected" if order_type == "market" else ""
    selected_limit = "selected" if order_type == "limit" else ""
    checked_dry = "checked" if dry_run else ""
    checked_live = "checked" if not dry_run else ""
    mode_label = "模拟盘 dry_run=true" if dry_run else "真实盘 dry_run=false"

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>交易执行设置</title>
  <style>
    :root {{ color-scheme: light; font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    body {{ margin: 0; min-height: 100vh; background: linear-gradient(135deg, #f6efe4, #dbeafe 55%, #e6f7ef); color: #172033; }}
    main {{ width: min(960px, calc(100vw - 32px)); margin: 36px auto; }}
    h1 {{ font-size: 34px; margin: 0 0 8px; letter-spacing: -0.03em; }}
    p {{ line-height: 1.65; }}
    .grid {{ display: grid; grid-template-columns: 1.25fr .75fr; gap: 18px; align-items: start; }}
    .card {{ background: rgba(255,255,255,.86); border: 1px solid rgba(23,32,51,.12); border-radius: 22px; padding: 22px; box-shadow: 0 18px 48px rgba(28,43,70,.12); backdrop-filter: blur(14px); }}
    label {{ display: block; margin: 14px 0 6px; font-weight: 700; }}
    input, select {{ width: 100%; box-sizing: border-box; padding: 12px 13px; border-radius: 12px; border: 1px solid #c5cedb; font-size: 15px; background: #fff; }}
    .radio {{ display: flex; gap: 12px; margin: 12px 0; }}
    .radio label {{ flex: 1; border: 1px solid #c5cedb; border-radius: 16px; padding: 14px; margin: 0; background: #fff; }}
    .radio input {{ width: auto; margin-right: 8px; }}
    button, .button {{ display: inline-flex; border: 0; border-radius: 13px; padding: 12px 16px; font-weight: 800; background: #0f766e; color: #fff; text-decoration: none; cursor: pointer; }}
    .secondary {{ background: #1f2937; }}
    .danger {{ background: #b91c1c; }}
    .muted {{ color: #5f6b7a; font-size: 14px; }}
    .msg {{ margin: 18px 0; padding: 12px 14px; background: #ecfeff; border: 1px solid #67e8f9; border-radius: 14px; }}
    .qr img {{ width: 280px; max-width: 100%; border-radius: 16px; background: #fff; padding: 12px; }}
    @media (max-width: 760px) {{ .grid {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
<main>
  <h1>交易执行设置</h1>
  <p class="muted">选择策略信号最终在哪个交易所执行。默认建议先使用 Hotcoin dry-run，确认日志正常后再开启真实执行。</p>
  {f'<div class="msg">{html.escape(message)}</div>' if message else ''}
  <div class="grid">
    <section class="card">
      <form method="post" action="{PUBLIC_BASE_PATH}/settings">
        <h2>下单交易所</h2>
        <div class="radio">
          <label><input type="radio" name="exchange" value="freqtrade" {checked_freqtrade}> Freqtrade 当前交易所 / OKX</label>
          <label><input type="radio" name="exchange" value="hotcoin" {checked_hotcoin}> Hotcoin 网页接口</label>
        </div>
        <h2>Freqtrade 交易模式</h2>
        <div class="radio">
          <label><input type="radio" name="freqtrade_mode" value="dry_run" {checked_dry}> 模拟盘</label>
          <label><input type="radio" name="freqtrade_mode" value="live" {checked_live}> 真实盘</label>
        </div>
        <label><input type="checkbox" name="live_confirm" value="true" style="width:auto"> 我确认已配置交易所 Key，并理解真实盘会实际下单</label>
        <p class="muted">当前 Freqtrade 模式：{html.escape(mode_label)}。修改该项后必须重启 freqtrade 容器才生效。</p>
        <label>Hotcoin 下单数量</label>
        <input name="hotcoin_amount" value="{html.escape(amount)}" placeholder="例如 1" />
        <label>订单类型</label>
        <select name="hotcoin_order_type">
          <option value="market" {selected_market}>市价</option>
          <option value="limit" {selected_limit}>限价</option>
        </select>
        <label><input type="checkbox" name="hotcoin_execute" value="true" {checked_execute} style="width:auto"> 允许 Hotcoin 真实下单</label>
        <p class="muted">当前桥接：{html.escape(bridge_enabled)}；真实执行：{html.escape(execute)}。关闭真实执行时只会 dry-run 写日志。</p>
        <button type="submit">保存设置</button>
      </form>
    </section>
    <aside class="card">
      <h2>Hotcoin 登录状态</h2>
      <p>Token：<span id="hotcoin-token">{html.escape(login_status["token_mask"])}</span></p>
      <p>Session 文件：<span id="hotcoin-session">{"已保存" if login_status["session_exists"] else "未保存"}</span></p>
      <p>状态：<span id="hotcoin-login-message">{html.escape(login_status["message"])}</span></p>
      <form method="post" action="{PUBLIC_BASE_PATH}/hotcoin/qr/start">
        <button class="secondary" type="submit">{"重新扫码登录" if login_status["has_token"] else "启动 Hotcoin 扫码登录"}</button>
      </form>
      <form method="post" action="{PUBLIC_BASE_PATH}/hotcoin/verify" style="margin-top:10px">
        <button type="submit">验证登录状态</button>
      </form>
      <p class="muted">扫码成功后会保存到 <code>user_data/hotcoin_session.json</code>，该文件不会进入 Git。</p>
    </aside>
  </div>
  {qr_block}
</main>
<script>
  const qrStatus = document.getElementById('qr-status');
  const qrMessage = document.getElementById('qr-message');
  const hotcoinToken = document.getElementById('hotcoin-token');
  const hotcoinSession = document.getElementById('hotcoin-session');
  const hotcoinLoginMessage = document.getElementById('hotcoin-login-message');
  async function refreshHotcoinLoginStatus() {{
    if (!hotcoinToken || !hotcoinSession) return;
    try {{
      const response = await fetch('{PUBLIC_BASE_PATH}/hotcoin/status', {{ credentials: 'same-origin' }});
      if (!response.ok) return;
      const data = await response.json();
      hotcoinToken.textContent = data.token_mask || '未配置';
      hotcoinSession.textContent = data.session_exists ? '已保存' : '未保存';
      if (hotcoinLoginMessage) hotcoinLoginMessage.textContent = data.message || '';
    }} catch (error) {{}}
  }}
  async function refreshQrStatus() {{
    if (!qrStatus || !qrMessage) return;
    try {{
      const response = await fetch('{PUBLIC_BASE_PATH}/hotcoin/qr/status', {{ credentials: 'same-origin' }});
      if (!response.ok) return;
      const data = await response.json();
      qrStatus.textContent = data.status || '';
      qrMessage.textContent = data.message || '';
      if (['logged_in', 'error', 'timeout'].includes(data.status)) {{
        window.clearInterval(window.__hotcoinQrTimer);
      }}
      if (data.status === 'logged_in') {{
        refreshHotcoinLoginStatus();
      }}
    }} catch (error) {{
      qrMessage.textContent = '二维码状态刷新失败：' + error;
    }}
  }}
  if (qrStatus && qrMessage && !['logged_in', 'error', 'timeout'].includes(qrStatus.textContent)) {{
    window.__hotcoinQrTimer = window.setInterval(refreshQrStatus, 3000);
  }}
</script>
</body>
</html>"""


def _landing_page() -> str:
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>AI Trading</title>
  <style>
    :root { font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: #122033; }
    body { margin: 0; min-height: 100vh; background: radial-gradient(circle at 18% 12%, #d9f99d 0, transparent 28%), radial-gradient(circle at 82% 18%, #bae6fd 0, transparent 26%), linear-gradient(135deg, #fff7ed, #eef2ff 52%, #ecfdf5); }
    header { width: min(1080px, calc(100vw - 32px)); margin: 0 auto; padding: 26px 0; display: flex; justify-content: space-between; align-items: center; }
    .brand { font-size: 22px; font-weight: 900; letter-spacing: -0.04em; }
    nav { display: flex; gap: 10px; flex-wrap: wrap; }
    a { color: inherit; text-decoration: none; }
    nav a { padding: 10px 14px; border: 1px solid rgba(18,32,51,.16); border-radius: 999px; background: rgba(255,255,255,.72); }
    main { width: min(1080px, calc(100vw - 32px)); margin: 84px auto 0; }
    h1 { max-width: 760px; font-size: clamp(42px, 8vw, 88px); line-height: .92; letter-spacing: -0.07em; margin: 0; }
    p { max-width: 620px; font-size: 18px; line-height: 1.75; color: #4b5b70; }
    .cards { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px; margin-top: 42px; }
    .card { padding: 24px; border-radius: 24px; background: rgba(255,255,255,.82); border: 1px solid rgba(18,32,51,.12); box-shadow: 0 24px 70px rgba(24,39,75,.14); }
    .card h2 { margin: 0 0 8px; font-size: 24px; }
    .button { display: inline-flex; margin-top: 16px; padding: 13px 17px; border-radius: 14px; background: #0f766e; color: #fff; font-weight: 850; }
    .button.dark { background: #172033; }
    @media (max-width: 760px) { header { align-items: flex-start; flex-direction: column; gap: 16px; } main { margin-top: 38px; } .cards { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  <header>
    <a class="brand" href="/">AI Trading</a>
    <nav>
      <a href="/trade">交易面板</a>
      <a href="/trade-settings/">交易设置</a>
    </nav>
  </header>
  <main>
    <h1>AI Trading Control Center</h1>
    <p>统一进入策略监控、交易执行设置和 Hotcoin 扫码登录。策略仍由 Freqtrade 执行，交易所执行目标可在设置页切换。</p>
    <section class="cards">
      <article class="card">
        <h2>Freqtrade 面板</h2>
        <p>查看运行状态、交易、收益、日志和策略指标。</p>
        <a class="button" href="/trade">打开交易面板</a>
      </article>
      <article class="card">
        <h2>交易执行设置</h2>
        <p>选择 OKX/Freqtrade 或 Hotcoin，下单数量、真实执行开关和 Hotcoin 扫码登录。</p>
        <a class="button dark" href="/trade-settings/">打开交易设置</a>
      </article>
    </section>
  </main>
</body>
</html>"""


def _poll_hotcoin_qr(token: str) -> None:
    client = HotcoinWebClient(session_path=SESSION_PATH)
    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            data = client.check_qr_login(token)
            code = data.get("code") if isinstance(data, dict) else None
            with _qr_lock:
                _qr_state["message"] = f"Hotcoin 返回 code={code}"
                _qr_state["status"] = "waiting"
            if code == 200:
                with _qr_lock:
                    _qr_state["status"] = "logged_in"
                    _qr_state["message"] = "扫码登录成功，session 已保存。"
                return
        except (requests.Timeout, requests.ConnectionError) as exc:
            with _qr_lock:
                _qr_state["status"] = "waiting"
                _qr_state["message"] = f"Hotcoin 登录状态接口暂时无响应，继续等待扫码确认：{exc}"
        except Exception as exc:
            with _qr_lock:
                _qr_state["status"] = "error"
                _qr_state["message"] = str(exc)
            return
        time.sleep(2)
    with _qr_lock:
        _qr_state["status"] = "timeout"
        _qr_state["message"] = "二维码登录超时，请重新生成。"


@app.post("/settings")
async def save_settings(
    request: Request, _: Annotated[str, Depends(_auth)]
) -> RedirectResponse:
    body = (await request.body()).decode()
    form = {
        key: values[-1]
        for key, values in urllib.parse.parse_qs(body, keep_blank_values=True).items()
    }
    exchange = form.get("exchange", "")
    hotcoin_amount = form.get("hotcoin_amount", "0")
    hotcoin_order_type = form.get("hotcoin_order_type", "market")
    hotcoin_execute = form.get("hotcoin_execute")
    freqtrade_mode = form.get("freqtrade_mode", "dry_run")
    live_confirm = form.get("live_confirm")
    if exchange not in {"freqtrade", "hotcoin"}:
        raise HTTPException(status_code=400, detail="Invalid exchange.")
    if hotcoin_order_type not in {"market", "limit"}:
        raise HTTPException(status_code=400, detail="Invalid order type.")
    if freqtrade_mode not in {"dry_run", "live"}:
        raise HTTPException(status_code=400, detail="Invalid Freqtrade mode.")
    if freqtrade_mode == "live" and live_confirm != "true":
        raise HTTPException(status_code=400, detail="切换真实盘必须勾选确认。")
    try:
        amount = float(hotcoin_amount)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Hotcoin amount must be numeric.") from exc
    _write_freqtrade_dry_run(freqtrade_mode != "live")
    _write_state(
        {
            "hotcoin_signal_bridge_enabled": exchange == "hotcoin",
            "hotcoin_signal_execute": hotcoin_execute == "true",
            "hotcoin_order_amount": amount,
            "hotcoin_order_type": hotcoin_order_type,
            "hotcoin_mode": "web",
            "hotcoin_session_path": "/freqtrade/user_data/hotcoin_session.json",
            "hotcoin_adapter_path": "/freqtrade/scripts/hotcoin_adapter.py",
            "updated_at": int(time.time()),
        }
    )
    if freqtrade_mode == "live":
        message = "设置已保存：Freqtrade 已切换为真实盘配置。请重启 freqtrade 容器后生效。"
    else:
        message = "设置已保存：Freqtrade 已切换为模拟盘配置。请重启 freqtrade 容器后生效；Hotcoin 桥接设置下一次信号实时读取。"
    return RedirectResponse(_redirect_url(message), status_code=303)


@app.post("/hotcoin/qr/start")
def start_hotcoin_qr(_: Annotated[str, Depends(_auth)]) -> RedirectResponse:
    try:
        client = HotcoinWebClient(session_path=SESSION_PATH)
        token = client.generate_qr_token()
        image_url = (
            "https://api.qrserver.com/v1/create-qr-code/?size=300x300&data="
            + urllib.parse.quote(token)
        )
        with _qr_lock:
            _qr_state.update(
                {
                    "token": token,
                    "image_url": image_url,
                    "status": "waiting",
                    "message": "请使用 Hotcoin App 扫码确认登录。",
                    "started_at": time.time(),
                }
            )
        threading.Thread(target=_poll_hotcoin_qr, args=(token,), daemon=True).start()
        message = "二维码已生成，请扫码。"
    except HotcoinError as exc:
        message = f"生成二维码失败：{exc}"
    return RedirectResponse(_redirect_url(message), status_code=303)


@app.post("/hotcoin/verify")
def verify_hotcoin(_: Annotated[str, Depends(_auth)]) -> RedirectResponse:
    status_data = _verify_hotcoin_login()
    message = str(status_data.get("message", "验证完成。"))
    return RedirectResponse(_redirect_url(message), status_code=303)


@app.get("/hotcoin/qr/status")
def hotcoin_qr_status(_: Annotated[str, Depends(_auth)]) -> dict[str, Any]:
    with _qr_lock:
        return {
            "status": _qr_state.get("status", "idle"),
            "message": _qr_state.get("message", ""),
            "started_at": _qr_state.get("started_at"),
        }


@app.get("/hotcoin/status")
def hotcoin_status(_: Annotated[str, Depends(_auth)]) -> dict[str, Any]:
    return _hotcoin_login_status()


@app.get("/hotcoin/verify")
def hotcoin_verify_status(_: Annotated[str, Depends(_auth)]) -> dict[str, Any]:
    return _verify_hotcoin_login()


@app.get("/landing", response_class=HTMLResponse)
def landing() -> str:
    return _landing_page()


@app.get("/", response_class=HTMLResponse)
def index_with_message(
    _: Annotated[str, Depends(_auth)], message: str = ""
) -> str:
    return _page(message)

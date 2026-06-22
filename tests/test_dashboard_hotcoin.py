from trading_system.dashboard import hotcoin_order_probe_check


def test_hotcoin_zero_amount_probe_500_is_auth_ok_but_not_passed():
    row = hotcoin_order_probe_check({"code": 500, "msg": "服务器内部错误"})

    assert row["ok"] is False
    assert row["auth_ok"] is True
    assert row["code"] == 500
    assert "认证通过" in row["msg"]
    assert "服务器内部错误" in row["msg"]


def test_hotcoin_zero_amount_probe_401_is_auth_failure():
    row = hotcoin_order_probe_check({"code": 401, "msg": "登录已失效，请重新登录"})

    assert row["ok"] is False
    assert row["auth_ok"] is False
    assert row["code"] == 401

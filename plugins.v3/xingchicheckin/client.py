"""Client for the normal Xingchi account check-in and refresh routes."""
import json
from urllib.parse import urlsplit

import requests

BASE = "https://xingchiapi.com/api/v1"
ALREADY_CLAIMED = object()
CHECKIN = "/user/checkin"


class CheckinError(Exception):
    pass


def proxy_settings(proxy):
    """把 MoviePilot 代理地址换算成 requests 可用的代理字典，格式不支持时直接报错。"""
    if not proxy:
        return {}
    try:
        proxy = proxy.strip()
        scheme = urlsplit(proxy).scheme.lower()
        if scheme == "socks5":
            proxy = "socks5h:" + proxy.split(":", 1)[1]
            scheme = "socks5h"
        if scheme not in {"http", "https", "socks5h", "socks4", "socks4a"} or not urlsplit(proxy).hostname:
            raise ValueError
    except (ValueError, AttributeError):
        raise CheckinError("MoviePilot 代理地址格式不受支持") from None
    return {"http": proxy, "https": proxy}


def api(method, route, *, token=None, body=None, proxy=None):
    """请求星驰接口，返回 data 字段；会话失效、重定向和非预期响应都转成 CheckinError。"""
    headers = {"Accept": "application/json", "Accept-Language": "zh-CN"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None or method == "POST":
        headers["Content-Type"] = "application/json"
    status = 0
    proxies = proxy_settings(proxy)
    try:
        with requests.Session() as session:
            if proxies:
                session.trust_env = False
            with session.request(method, BASE + route, headers=headers, json=body,
                                 proxies=proxies, timeout=(15, 25), allow_redirects=False, stream=True) as response:
                status = response.status_code
                if 300 <= status < 400:
                    raise CheckinError(f"站点返回重定向（HTTP {status}），未跟随")
                packet = json.loads(response.raw.read(256 * 1024, decode_content=True))
    except (ValueError, UnicodeError):
        if status in (401, 403) and route == "/auth/refresh":
            raise CheckinError(f"续期被拒绝（HTTP {status}）；请在插件配置中重新填写 refresh_token") from None
        raise CheckinError(f"站点返回非预期响应（HTTP {status}）") from None
    except (requests.RequestException, OSError, requests.packages.urllib3.exceptions.HTTPError) as exc:
        if isinstance(exc, requests.exceptions.InvalidSchema) and proxies:
            raise CheckinError("代理不可用：请检查宿主是否安装 requests 的 SOCKS 支持") from None
        mode = "MoviePilot 代理" if proxies else "未指定 MoviePilot 代理"
        raise CheckinError(f"站点连接失败（{mode}：{type(exc).__name__}）") from None
    if method == "POST" and route == CHECKIN and isinstance(packet, dict) and packet.get("reason") == "CHECKIN_ALREADY_CLAIMED":
        return ALREADY_CLAIMED
    if not isinstance(packet, dict) or not 200 <= status < 300 or packet.get("code") != 0:
        hint = "；如会话失效，请重新登录并在插件配置中更新 refresh_token" if route == "/auth/refresh" else ""
        raise CheckinError(f"{'续期' if route == '/auth/refresh' else '签到接口'}未成功（HTTP {status}）{hint}")
    return packet.get("data")


def refresh_session(refresh_token, *, proxy=None):
    """用 refresh_token 续期会话，返回 (access_token, 站点轮换后的新 refresh_token)。"""
    if not isinstance(refresh_token, str) or not refresh_token.strip():
        raise CheckinError("未配置 refresh_token；请在插件配置中填写登录后的会话令牌")
    refresh_token = refresh_token.strip()
    if len(refresh_token) > 8192 or any(c.isspace() for c in refresh_token):
        raise CheckinError("refresh_token 格式无效；请重新登录后粘贴完整令牌")
    session = api("POST", "/auth/refresh", body={"refresh_token": refresh_token}, proxy=proxy)
    if not isinstance(session, dict):
        raise CheckinError("续期响应缺少会话数据")
    token = session.get("access_token")
    rotated = session.get("refresh_token")
    if not isinstance(rotated, str) or not rotated:
        raise CheckinError("续期响应缺少新 refresh_token")
    if not isinstance(token, str) or not token:
        raise CheckinError("续期响应缺少 access_token")
    return token, rotated


def checkin_now(access_token, *, proxy=None):
    """按北京时间查询并提交一次签到，返回给用户看的结论文本。"""
    state = api("GET", CHECKIN + "?timezone=Asia%2FShanghai", token=access_token, proxy=proxy)
    if not isinstance(state, dict) or not isinstance(state.get("enabled"), bool):
        raise CheckinError("站点未返回可识别的签到状态，未提交签到")
    if not state["enabled"]:
        return "站点未启用签到"
    if state.get("claimed_today") is True:
        return "今日已签到，本次未重复提交"
    if state.get("claimed_today") is not False:
        raise CheckinError("站点未返回今日签到状态，未提交签到")
    result = api("POST", CHECKIN, token=access_token, proxy=proxy)
    if result is ALREADY_CLAIMED:
        return "今日已签到（站点确认）"
    if not isinstance(result, dict) or not isinstance(result.get("status"), dict) or result["status"].get("claimed_today") is not True:
        raise CheckinError("提交后的签到状态不明确，请到仪表盘核对")
    amount = result.get("amount")
    return f"签到成功，奖励 {amount:g}" if type(amount) in (int, float) else "签到成功"

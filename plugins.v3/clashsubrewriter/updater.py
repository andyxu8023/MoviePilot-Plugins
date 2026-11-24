# -*- coding: utf-8 -*-
"""订阅拉取、配置文件落盘（备份 + 原子替换）与容器重启。

落盘纪律：先写同目录临时文件并 fsync，再备份旧文件，最后 ``os.replace`` 原子替换。
任何一步异常都不会让目标目录里出现半截配置；替换后仍会重新解析一次以防意外。
重启容器只走 Docker 的 Unix socket（标准库实现），不受 MoviePilot 出口代理影响。
"""

from __future__ import annotations

import os
import socket as socket_module
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

#: 备份文件后缀模板
BACKUP_SUFFIX = ".mpbak"
#: Docker Engine API 的 Unix socket 路径
DOCKER_SOCKET = "/var/run/docker.sock"


class UpdateError(Exception):
    """拉取或落盘失败；调用方应保留原有配置文件不动。"""


def fetch_subscription(url: str,
                       user_agent: str = "",
                       proxy: str = "",
                       timeout: int = 30,
                       verify: bool = True,
                       retries: int = 2) -> str:
    """
    抓取订阅文本。

    :param url: 订阅地址（需要能直接返回 Clash YAML）
    :param user_agent: 自定义 UA；留空用 requests 默认
    :param proxy: 形如 ``http://127.0.0.1:7890`` 的代理地址，留空直连
    :param timeout: 单次请求超时（秒）
    :param verify: 是否校验 TLS 证书
    :param retries: 失败重试次数
    :return: 统一成 LF 换行的 UTF-8 文本
    """
    import requests

    if not url or not str(url).strip():
        raise UpdateError("订阅地址为空")
    url = str(url).strip()
    if not url.lower().startswith(("http://", "https://")):
        raise UpdateError("订阅地址必须是 http(s) 链接")
    headers = {"User-Agent": user_agent} if user_agent else {}
    proxies = {"http": proxy, "https": proxy} if proxy else None
    reason = "未知错误"
    for attempt in range(max(1, int(retries) + 1)):
        try:
            response = requests.get(url, headers=headers, proxies=proxies,
                                    timeout=max(5, int(timeout)), verify=bool(verify))
            if response.status_code != 200:
                reason = f"订阅返回 HTTP {response.status_code}"
                # 403 多半是 CF 或订阅配额，重试无意义
                if response.status_code in (401, 403, 404):
                    raise UpdateError(f"{reason}；订阅链接可能已失效或被 CF 拦截")
            else:
                body = response.content
                if not body:
                    reason = "订阅返回空内容"
                else:
                    return body.decode("utf-8-sig", errors="replace") \
                        .replace("\r\n", "\n").replace("\r", "\n")
        except UpdateError:
            raise
        except Exception as error:  # requests 的异常类型较多，统一收敛成可读原因
            reason = f"{type(error).__name__}: {error}"
        if attempt + 1 < max(1, int(retries) + 1):
            time.sleep(2)
    raise UpdateError(f"订阅拉取失败：{reason}")


def ensure_clash_yaml(text: str) -> Dict[str, Any]:
    """确认订阅文本是 Clash YAML 配置，返回解析结果。"""
    if not text or not text.strip():
        raise UpdateError("订阅内容为空")
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise UpdateError(f"订阅内容不是合法 YAML：{error}{_link_hint(text)}") from error
    if not isinstance(doc, dict):
        raise UpdateError("订阅内容不是 Clash 配置映射（顶层不是 key: value）"
                          + _link_hint(text))
    if not any(key in doc for key in ("proxies", "proxy-groups", "proxy-providers")):
        first = str(list(doc)[:3])
        raise UpdateError(f"订阅内容里没有 proxies/proxy-groups，顶层键是 {first}；"
                          f"这个链接可能不是 Clash 订阅")
    return doc


def _link_hint(text: str) -> str:
    """内容是节点链接或 base64 时给出可操作的提示。"""
    head = text.lstrip()[:400]
    schemes = ("ss://", "vmess://", "trojan://", "vless://", "hysteria://", "tuic://")
    if head.startswith(schemes) or any(f"\n{s}" in f"\n{head}" for s in schemes):
        return "；返回的是节点链接列表，请在订阅地址加上 Clash 参数（例如 ?clash=1）"
    if head and not head[0].isascii():
        return "；可能是 base64 订阅，请在订阅地址加上 Clash 参数（例如 ?clash=1）"
    return ""


def read_target(target_dir: str, target_file: str) -> Optional[str]:
    """读取现有目标配置文件文本，不存在返回 ``None``。"""
    path = Path(str(target_dir)) / str(target_file)
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise UpdateError(f"读取现有配置失败：{error}")


def deploy(target_dir: str,
           target_file: str,
           text: str,
           backup_dir: Optional[str] = None,
           backup_keep: int = 5) -> Dict[str, Any]:
    """
    备份旧文件后原子替换目标配置。

    :param target_dir: 目标目录（必须在容器内可见且可写）
    :param target_file: 目标文件名，不允许带路径
    :param text: 改写后的完整文本
    :param backup_dir: 备份目录；留空则备份到目标文件同目录
    :param backup_keep: 保留的备份份数，0 表示不备份
    :return: 含 ``target``、``backup``、``size``、``pruned`` 的落盘结果
    """
    directory = Path(str(target_dir))
    name = str(target_file)
    if not name or name != Path(name).name:
        raise UpdateError("目标文件名不能包含路径分隔符")
    if not directory.is_dir():
        raise UpdateError(f"目标目录不存在：{target_dir}")
    if not os.access(str(directory), os.W_OK):
        raise UpdateError(f"目标目录没有写权限：{target_dir}")
    target = directory / name
    payload = text.encode("utf-8")
    # 落盘前再解析一次，确保写出去的就是合法 Clash 配置（双重保险，引擎已校验过）
    try:
        doc = yaml.safe_load(payload.decode("utf-8"))
    except yaml.YAMLError as error:
        raise UpdateError(f"待写入内容不是合法 YAML，已放弃：{error}")
    if not isinstance(doc, dict):
        raise UpdateError("待写入内容不是 YAML 映射，已放弃")

    temp = directory / f"{name}.mpnew{os.getpid()}"
    backup: Optional[Path] = None
    pruned = 0
    try:
        with open(temp, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, 0o644)
        if int(backup_keep or 0) > 0 and target.is_file():
            holder = Path(str(backup_dir)) if backup_dir else directory
            try:
                holder.mkdir(parents=True, exist_ok=True)
            except OSError as error:
                raise UpdateError(f"备份目录不可用：{error}")
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = holder / f"{name}{BACKUP_SUFFIX}-{stamp}"
            index = 1
            while backup.exists():
                backup = holder / f"{name}{BACKUP_SUFFIX}-{stamp}-{index}"
                index += 1
            import shutil
            shutil.copy2(str(target), str(backup))
        os.replace(str(temp), str(target))
        temp = None
    except UpdateError:
        raise
    except OSError as error:
        raise UpdateError(f"写入目标目录失败：{error}")
    finally:
        if temp is not None and temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass
    if backup is not None:
        pruned = prune_backups(str(backup.parent), name, int(backup_keep or 0))
    return {"target": str(target), "backup": str(backup) if backup else "",
            "size": len(payload), "pruned": pruned}


def prune_backups(backup_dir: str, target_file: str, keep: int) -> int:
    """按修改时间保留最近 ``keep`` 份备份，返回删除份数。"""
    if keep <= 0:
        return 0
    holder = Path(str(backup_dir))
    if not holder.is_dir():
        return 0
    prefix = f"{str(target_file)}{BACKUP_SUFFIX}-"
    files = [p for p in holder.iterdir() if p.is_file() and p.name.startswith(prefix)]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    removed = 0
    for stale in files[keep:]:
        try:
            stale.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def list_backups(backup_dir: Optional[str], target_file: str, limit: int = 5) -> List[Dict[str, Any]]:
    """列出最近的备份，供详情页展示。"""
    holder = Path(str(backup_dir)) if backup_dir else Path(".")
    if not holder.is_dir():
        return []
    prefix = f"{str(target_file)}{BACKUP_SUFFIX}-"
    rows = []
    for path in holder.iterdir():
        if path.is_file() and path.name.startswith(prefix):
            stat = path.stat()
            rows.append({"name": path.name, "size": stat.st_size,
                         "time": datetime.fromtimestamp(stat.st_mtime).strftime("%m-%d %H:%M")})
    rows.sort(key=lambda r: r["name"], reverse=True)
    return rows[:max(1, int(limit))]


# --------------------------------------------------------------------- 容器重启


def _uds_request(method: str, path: str, socket_path: str = DOCKER_SOCKET,
                 timeout: int = 15) -> Tuple[int, Any]:
    """
    通过 Docker 的 Unix socket 调一次 API。

    刻意只用标准库：MoviePilot 的出口代理往往就架在这个 Clash 容器上，
    走 ``requests`` 会被 ``trust_env`` 的代理设置牵连，重启瞬间还可能正好撞上代理断线。
    Unix socket 与网络代理无关，重启链路最稳。

    :return: ``(HTTP 状态码, 解析后的 JSON 或原始文本)``
    """
    import http.client
    import json

    class _UnixConn(http.client.HTTPConnection):
        def connect(self):
            sock = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
            sock.settimeout(self.timeout)
            sock.connect(socket_path)
            self.sock = sock

    conn = _UnixConn("localhost", timeout=timeout)
    try:
        conn.request(method, path)
        response = conn.getresponse()
        raw = response.read().decode("utf-8", errors="replace")
        try:
            return response.status, json.loads(raw) if raw.strip() else None
        except ValueError:
            return response.status, raw
    finally:
        conn.close()


def find_container(name: str, socket_path: str = DOCKER_SOCKET) -> str:
    """
    按名字定位容器，返回精确的容器标识；找不到抛 :class:`UpdateError`。

    先按原名精确匹配（Docker 容器名本身大小写敏感），再退化为忽略大小写，
    避免用户把 ``Clash`` 写成 ``clash`` 就直接失败。

    :param name: 容器名（``docker-compose`` 里的 ``container_name``）
    :param socket_path: Docker socket 路径
    """
    wanted = str(name).strip().lstrip("/")
    if not wanted:
        raise UpdateError("容器名为空")
    if not Path(socket_path).exists():
        raise UpdateError(f"容器内没有 Docker socket（{socket_path} 不可用），"
                          f"无法由插件重启容器")
    status, rows = _uds_request("GET", "/containers/json?all=1", socket_path)
    if status != 200:
        detail = rows.get("message") if isinstance(rows, dict) else rows
        raise UpdateError(f"Docker API 返回 HTTP {status}：{detail}")
    if not isinstance(rows, list):
        raise UpdateError("Docker API 返回内容异常，拿不到容器列表")
    running = [r for r in rows if str((r.get("Names") or [""])[0]).lstrip("/") == wanted
               and (r.get("State") or "").lower() == "running"]
    if running:
        return str(running[0].get("Id") or wanted)
    exact = [r for r in rows if str((r.get("Names") or [""])[0]).lstrip("/") == wanted]
    if exact:
        return str(exact[0].get("Id") or wanted)
    loose = [r for r in rows if str((r.get("Names") or [""])[0]).lstrip("/").lower()
             == wanted.lower()]
    if loose:
        return str(loose[0].get("Id") or wanted)
    seen = "、".join(sorted({str((r.get("Names") or ["?"])[0]).lstrip("/") for r in rows}))[:300]
    raise UpdateError(f"找不到名为 {name} 的容器；当前可见容器：{seen}")


def restart_container(name: str,
                      socket_path: str = DOCKER_SOCKET,
                      timeout: int = 15,
                      stop_timeout: Optional[int] = None,
                      wait_running: int = 20) -> Dict[str, Any]:
    """
    重启指定容器并等待其回到 ``running``。

    :param name: 容器名
    :param socket_path: Docker socket 路径
    :param timeout: 单次 API 调用超时（秒）
    :param stop_timeout: 停止容器前的等待秒数，留空用 Docker 默认
    :param wait_running: 轮询等待容器重新 running 的最长秒数
    :return: ``{"container": 实际容器名, "id": 短 ID, "restarted": bool,
              "status": running 状态, "waited": 等待秒数}``
    """
    container_id = find_container(name, socket_path)
    path = f"/containers/{container_id}/restart"
    if stop_timeout and int(stop_timeout) > 0:
        path += f"?t={int(stop_timeout)}"
    status, body = _uds_request("POST", path, socket_path, timeout=max(int(timeout), 30))
    if status not in (204, 200):
        detail = body.get("message") if isinstance(body, dict) else body
        raise UpdateError(f"重启容器失败：Docker 返回 HTTP {status}：{detail}")
    inspected = _wait_running(container_id, socket_path, wait_running)
    label = name
    info_status, info = _uds_request("GET", f"/containers/{container_id}/json", socket_path)
    if info_status == 200 and isinstance(info, dict):
        label = str((info.get("Name") or name)).lstrip("/")
    return {"container": label, "id": container_id[:12], "restarted": True,
            "status": inspected.get("status", "unknown"),
            "started_at": inspected.get("started_at", ""),
            "waited": inspected.get("waited", 0)}


def _wait_running(container_id: str, socket_path: str, limit: int) -> Dict[str, Any]:
    """轮询容器状态直到 ``running`` 或超时（超时不算失败，只如实回报）。"""
    begin = time.time()
    deadline = begin + max(0, int(limit))
    state: Dict[str, Any] = {"status": "unknown", "started_at": "", "waited": 0.0}
    while True:
        status, info = _uds_request("GET", f"/containers/{container_id}/json", socket_path)
        detail = (info or {}).get("State", {}) if isinstance(info, dict) else {}
        state = {"status": str(detail.get("Status") or "unknown"),
                 "started_at": str(detail.get("StartedAt") or "")[:19],
                 "waited": round(time.time() - begin, 1)}
        if status == 200 and state["status"] == "running":
            return state
        if time.time() >= deadline:
            return state
        time.sleep(0.5)


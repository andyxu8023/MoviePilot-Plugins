# -*- coding: utf-8 -*-
"""
Clash 订阅改写：定时拉取机场订阅 → 按声明式规则改写 → 备份后原子替换到指定目录。

典型用途（本实例的默认配置就是这套）：iKuuu 订阅返回的是 Clash 配置，但每次都要手改
监听端口、开启 allow-lan、加一个香港自动切换组并挂进「选择节点」，改完再覆盖
``/Clash/iKuuu_V2.yaml``。本插件把这套手工动作固化成可编辑的 YAML 规则：

- 订阅只出 YAML，插件不做订阅转换；
- 改写只动目标行，其余内容逐字节保留（跟人工编辑结果一致，diff 干净）；
- 校验不通过一律不落盘，失败时原文件保持不变，并留有备份可回滚；
- 写盘成功后可按需重启 Clash 容器（走 Docker Unix socket，不依赖出口代理）。
"""

from __future__ import annotations

import difflib
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.triggers.cron import CronTrigger
from app.core.config import settings

try:  # V3 SDK 入口，缺失时退回旧包路径
    from app.sdk.plugin import _PluginBase
    from app.sdk.logging import logger
    from app.sdk.events import eventmanager
except ModuleNotFoundError:  # pragma: no cover - 仅老版本宿主走到这里
    from app.plugins import _PluginBase
    from app.log import logger
    from app.runtime.events import eventmanager
from app.schemas.types import EventType

from .rewriter import (RewriteError, parse_spec, resolve_secret, rewrite,
                       validate_result)
from .updater import (DOCKER_SOCKET, UpdateError, deploy, ensure_clash_yaml,
                      fetch_subscription, find_container, list_backups,
                      read_target, restart_container)

#: 详情页长文本换行样式（宿主列表默认单行截断）
_WRAP = ("display:block;white-space:normal;overflow:visible;"
         "text-overflow:clip;-webkit-line-clamp:unset")

#: 多行说明用：保留手写换行，同时允许长 token 处断行（内联样式优先于 Vuetify 工具类）
_WRAP_PRE = ("display:block;white-space:pre-line;overflow:visible;"
             "text-overflow:clip;-webkit-line-clamp:unset")

#: 默认改写规则：@SECRET 由配置项注入，源码里不留密钥
DEFAULT_SPEC = """# 只改需要改的行，其余内容原样保留
set:
  port: 27890
  socks-port: 27891
  allow-lan: true
  external-controller: 0.0.0.0:29090
  ipv6: true
  external-ui: /ui
  secret: '@SECRET'
insert_after:
  ipv6: mode
  external-ui: external-controller
  secret: external-ui
delete:
  - unified-delay
# 机场按 UA 追加的提示块（大写键，Clash 不识别），存在就清掉
delete_if_present:
  - Proxy
  - Proxy Group
  - Rules
groups: |-
  - name: 😋 自动切换
    type: url-test
    url: 'http://www.gstatic.com/generate_204'
    interval: 300
    lazy: true
    proxies:
      - "@MEMBERS(🇭🇰)"
references:
  - group: 🔰 选择节点
    item: 😋 自动切换
    position: first
"""

DEFAULTS = {
    "enabled": False,
    "cron": "0 4 * * *",
    "sub_link": "",
    "dashboard_secret": "",
    "target_dir": "/Clash",
    "target_file": "iKuuu_V2.yaml",
    "user_agent": "mihomo/1.18.1",
    "use_proxy": False,
    "verify_tls": True,
    "timeout": 30,
    "retries": 2,
    "backup_dir": "",
    "backup_keep": 5,
    "skip_if_same": True,
    "notify": True,
    "restart_container": True,
    "container_name": "Clash",
    "stop_timeout": 3,
    "spec_text": DEFAULT_SPEC,
}

#: 同一进程内串行执行，避免定时任务与手动触发重叠
RUN_LOCK = threading.Lock()
#: 详情页按钮等待后端返回的上限（秒），超时后转为「后台进行中」
_WAIT_SECONDS = 25


class ClashSubRewriter(_PluginBase):
    """定时更新 Clash 订阅配置文件，并按用户规则改写后替换到指定目录。"""

    plugin_name = "Clash 订阅改写"
    plugin_desc = "定时拉取机场订阅（iKuuu 等直出 Clash YAML 的站点），按 YAML 规则改写后备份并原子替换到指定目录，可选写盘后自动重启容器。"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/Mihomo_Meta_A.png"
    plugin_version = "1.1.1"
    plugin_label = "Clash,订阅,网络"
    # 插件作者
    plugin_author = "左岸"
    # 作者主页
    author_url = "https://github.com/andyxu8023"
    # 使用说明入口：已安装插件投影读类属性，市场投影读 package 的 plugin_readme，两处都要给
    plugin_readme = ("https://github.com/andyxu8023/MoviePilot-Plugins"
                     "/blob/main/plugins.v3/clashsubrewriter/README.md")
    plugin_config_prefix = "clashsubrewriter_"
    plugin_order = 55
    auth_level = 2

    _enabled = False
    _cron = ""
    _trigger = None
    _sub_link = ""
    _secret = ""
    _target_dir = DEFAULTS["target_dir"]
    _target_file = DEFAULTS["target_file"]
    _user_agent = ""
    _use_proxy = False
    _verify_tls = True
    _timeout = 30
    _retries = 2
    _backup_dir = ""
    _backup_keep = 5
    _skip_if_same = True
    _notify = True
    _restart_container = True
    _container_name = "Clash"
    _stop_timeout = 3
    _spec: Dict[str, Any] = {}
    _spec_error = ""
    _config_error = ""

    # ------------------------------------------------------------------ 生命周期
    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        """读取配置、校验改写规则与 Cron，不在此处触发更新。"""
        self.stop_service()
        merged = {**DEFAULTS, **(config or {})}
        self._enabled = bool(merged.get("enabled"))
        self._sub_link = str(merged.get("sub_link") or "").strip()
        self._secret = str(merged.get("dashboard_secret") or "")
        self._target_dir = str(merged.get("target_dir") or "").strip()
        self._target_file = str(merged.get("target_file") or "").strip()
        self._user_agent = str(merged.get("user_agent") or "").strip()
        self._use_proxy = bool(merged.get("use_proxy"))
        self._verify_tls = bool(merged.get("verify_tls"))
        self._timeout = int(merged.get("timeout") or 30)
        self._retries = int(merged.get("retries") or 0)
        self._backup_dir = str(merged.get("backup_dir") or "").strip()
        self._backup_keep = int(merged.get("backup_keep") or 0)
        self._skip_if_same = bool(merged.get("skip_if_same"))
        self._notify = bool(merged.get("notify"))
        self._restart_container = bool(merged.get("restart_container"))
        self._container_name = str(merged.get("container_name") or "").strip()
        self._stop_timeout = int(merged.get("stop_timeout") or 0)
        self._cron = str(merged.get("cron") or "").strip()
        self._config_error = ""
        self._spec_error = ""
        self._trigger = None

        if not self._sub_link:
            self._config_error = "订阅地址为空，保存前请先填写。"
        if not self._target_dir or not self._target_file:
            self._config_error = (self._config_error + " " if self._config_error else "") \
                + "目标目录与文件名都不能为空。"
        if self._restart_container and not self._container_name:
            self._config_error = (self._config_error + " " if self._config_error else "") \
                + "已勾选重启容器，但容器名为空。"
        if self._cron:
            try:
                self._trigger = CronTrigger.from_crontab(self._cron,
                                                         timezone=str(settings.TZ or "Asia/Shanghai"))
            except (ValueError, TypeError) as error:
                self._config_error = (self._config_error + " " if self._config_error else "") \
                    + f"Cron 无效（{error}）；请使用五段表达式，例如 0 4 * * *。"
                self._trigger = None
        try:
            self._spec = resolve_secret(parse_spec(str(merged.get("spec_text") or "")), self._secret)
        except RewriteError as error:
            self._spec = {}
            self._spec_error = str(error)
        if self._enabled:
            logger.info(f"Clash 订阅改写：已启用，目标 {self._target_dir}/{self._target_file}，"
                        f"计划 {self._cron or '未设置'}，"
                        f"写盘后{'重启 ' + self._container_name + ' 容器' if self._restart_container else '不重启容器'}")

    def _restart_hint(self) -> str:
        """把重启能力状态说成一句人话，供详情页与告警使用。"""
        if not self._restart_container:
            return "已关闭「写盘后重启容器」，改完需要自己重启 Clash"
        if not Path(DOCKER_SOCKET).exists():
            return f"打算重启 {self._container_name}，但容器内没有 {DOCKER_SOCKET}，重启会失败"
        try:
            find_container(self._container_name)
            return f"写盘后自动重启 {self._container_name} 容器（socket 与容器名均已确认）"
        except UpdateError as error:
            return f"写盘后打算重启 {self._container_name}，但当前定位不到该容器：{error}"

    def get_state(self) -> bool:
        """插件是否处于启用状态。"""
        return self._enabled

    def stop_service(self) -> None:
        """定时任务由宿主统一托管，这里无需额外清理。"""

    def get_service(self) -> List[Dict[str, Any]]:
        """返回按 Cron 执行的周期更新服务。"""
        if self._enabled and self._trigger is not None and not self._spec_error:
            return [{
                "id": f"{self.__class__.__name__}.Update",
                "name": f"{self.plugin_name}定时更新",
                "trigger": self._trigger,
                "func": self.update,
                "kwargs": {"reason": "定时"},
            }]
        return []

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """注册远程命令，手动触发一次更新。"""
        return [{
            "cmd": "/clashsub",
            "event": EventType.PluginAction,
            "desc": "立即更新并改写 Clash 配置文件",
            "category": "工具",
            "data": {"action": "ClashSubRewriter.update"},
        }]

    @eventmanager.register(EventType.PluginAction)
    def _on_plugin_action(self, event: Any) -> None:
        """响应 `/clashsub` 命令，在后台线程执行更新。"""
        data = getattr(event, "event_data", None) or {}
        if not isinstance(data, dict) or data.get("action") != "ClashSubRewriter.update":
            return
        if not self._enabled:
            self.post_message(title=self.plugin_name, text="插件未启用，本次更新已忽略。")
            return
        threading.Thread(target=self.update, kwargs={"reason": "命令"}, daemon=True).start()

    # ------------------------------------------------------------------ 核心动作
    def update(self, reason: str = "手动") -> Dict[str, Any]:
        """
        执行一次完整更新：拉取 → 改写 → 校验 → 备份 → 原子替换。

        任何一步失败都不会触碰目标文件，失败原因会写进详情页并（可选）推送通知。

        :param reason: 触发来源，仅用于日志与通知文案
        :return: ``{"ok": bool, "message": str, ...}``
        """
        if self._config_error or self._spec_error:
            message = f"配置未就绪：{self._config_error or self._spec_error}"
            self._record(False, message, reason, [])
            return {"ok": False, "message": message}
        if not RUN_LOCK.acquire(blocking=False):
            return {"ok": False, "message": "已有一次更新在进行中，本次跳过。"}
        started = time.monotonic()
        try:
            raw = fetch_subscription(self._sub_link, user_agent=self._user_agent,
                                     proxy=self._proxy_url(), timeout=self._timeout,
                                     verify=self._verify_tls, retries=self._retries)
            ensure_clash_yaml(raw)
            out, changes, doc = rewrite(raw, self._spec)
            stats = validate_result(out, doc, self._spec)
            old = read_target(self._target_dir, self._target_file)
            if old is not None and old == out and self._skip_if_same:
                message = (f"改写结果与现有文件完全一致（{stats['proxies']} 节点 / "
                           f"{stats['groups']} 策略组 / {stats['rules']} 规则），未写盘。")
                result = {"ok": True, "message": message, "changed": False, "stats": stats,
                          "changes": changes}
            else:
                info = deploy(self._target_dir, self._target_file, out,
                              backup_dir=self._backup_path(), backup_keep=self._backup_keep)
                message = (f"已更新 {info['target']}：{stats['proxies']} 节点 / "
                           f"{stats['groups']} 策略组 / {stats['rules']} 规则，"
                           f"{_fmt_size(info['size'])}")
                if info.get("backup"):
                    message += f"；旧文件已备份为 {Path(info['backup']).name}"
                restart_note = self._restart_after_write()
                if restart_note:
                    message += f"；{restart_note}"
                result = {"ok": True, "message": message, "changed": True, "stats": stats,
                          "changes": changes, "target": info["target"],
                          "restart": restart_note}
        except (UpdateError, RewriteError) as error:
            result = {"ok": False, "message": f"{reason}更新失败：{error}；现有配置文件未被修改。"}
        except Exception as error:  # 兜底，避免调度线程被异常打断
            logger.exception(f"Clash 订阅改写：未预期异常 {type(error).__name__}: {error}")
            result = {"ok": False, "message": f"更新异常（{type(error).__name__}）："
                                              f"{error}；现有配置文件未被修改。"}
        finally:
            RUN_LOCK.release()
        result["reason"] = reason
        result["duration"] = round(time.monotonic() - started, 2)
        self._record(result["ok"], result["message"], reason, result.get("changes") or [])
        if self._notify:
            self.post_message(title=self.plugin_name,
                              text=self._redact(result["message"]))
        return result

    def _restart_after_write(self) -> str:
        """
        写盘成功后按需重启 Clash 容器。

        重启失败**不会**让本次更新判为失败：配置已经正确落盘，把原因如实拼进消息即可，
        用户仍可手动重启。

        :return: 供详情页与通知展示的一句话
        """
        if not self._restart_container:
            return ""
        try:
            info = restart_container(self._container_name, stop_timeout=self._stop_timeout)
            return (f"已重启容器 {info['container']}（{info['status']}，"
                    f"等待 {info['waited']} 秒）")
        except UpdateError as error:
            logger.warning(f"Clash 订阅改写：配置已更新但重启容器失败：{error}")
            return f"配置已写入，但重启容器失败：{error}"
        except Exception as error:
            logger.exception(f"Clash 订阅改写：重启容器异常 {type(error).__name__}: {error}")
            return f"配置已写入，但重启容器异常：{type(error).__name__}: {error}"

    def preview(self) -> Dict[str, Any]:
        """
        预览改写结果但不写盘，差异存进详情页。

        :return: ``{"ok": bool, "message": str}``
        """
        if self._config_error or self._spec_error:
            return {"ok": False, "message": f"配置未就绪：{self._config_error or self._spec_error}"}
        if not RUN_LOCK.acquire(blocking=False):
            return {"ok": False, "message": "已有一次更新在进行中，请稍后再预览。"}
        try:
            raw = fetch_subscription(self._sub_link, user_agent=self._user_agent,
                                     proxy=self._proxy_url(), timeout=self._timeout,
                                     verify=self._verify_tls, retries=self._retries)
            ensure_clash_yaml(raw)
            out, changes, doc = rewrite(raw, self._spec)
            stats = validate_result(out, doc, self._spec)
            current = read_target(self._target_dir, self._target_file) or ""
            diff = list(difflib.unified_diff(current.split("\n"), out.split("\n"),
                                             "现有文件", "改写结果", lineterm="", n=1))
            body = diff[:80]
            if len(diff) > len(body):
                body.append(f"...（差异共 {len(diff)} 行，已截断）")
            self.save_data("preview", {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "changes": changes,
                "stats": stats,
                "diff": self._redact("\n".join(body)) or "（与现有文件无差异）",
            })
            message = f"预览完成：{len(changes)} 项改写，未写入文件。"
            return {"ok": True, "message": message}
        except (UpdateError, RewriteError) as error:
            return {"ok": False, "message": f"预览失败：{error}"}
        finally:
            RUN_LOCK.release()

    # ------------------------------------------------------------------ 扩展入口
    def get_form(self) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """返回 Vuetify JSON 配置表单。"""
        fields: List[Dict[str, Any]] = [
            {"component": "VAlert", "props": {"type": "info", "variant": "tonal",
             "text": "定时抓取订阅（需订阅直接返回 Clash YAML，iKuuu 的 ?clash= 链接即是），"
                     "按下方 YAML 规则改写，校验通过后备份并原子替换到目标目录。校验不通过不会改动原文件。"}},
            {"component": "VAlert", "props": {"type": "warning", "variant": "tonal",
             "text": "目标目录必须是 MoviePilot 容器内可见的路径。dreamacro/clash 不会热加载配置，"
                     "所以默认勾选了「写盘后自动重启容器」；关闭后需要自己重启，否则运行中的代理还是旧配置。"}},
            {"component": "VSwitch", "props": {"model": "enabled", "label": "启用定时更新"}},
            {"component": "VTextField", "props": {"model": "cron", "label": "更新时间（五段 Cron，北京时间）",
             "hint": "例：0 4 * * * 每天 04:00；30 */6 * * * 每 6 小时。星期字段 0=周一", "persistent-hint": True}},
            {"component": "VTextField", "props": {"model": "sub_link", "label": "订阅地址（直出 Clash YAML）",
             "placeholder": "https://xxx/link/token?clash=3"}},
            {"component": "VTextField", "props": {"model": "dashboard_secret", "label": "面板密钥（写入 secret 项）",
             "type": "password", "hint": "规则里用 @SECRET 引用；留空则不改写 secret", "persistent-hint": True}},
            {"component": "VTextField", "props": {"model": "target_dir", "label": "目标目录",
             "hint": "MoviePilot 容器内的绝对路径，例如 /Clash", "persistent-hint": True}},
            {"component": "VTextField", "props": {"model": "target_file", "label": "目标文件名",
             "hint": "只写文件名，例如 iKuuu_V2.yaml", "persistent-hint": True}},
            {"component": "VDivider"},
            {"component": "VTextField", "props": {"model": "user_agent", "label": "抓取 User-Agent",
             "hint": "留空或旧客户端写法会被机场拦下或塞入提示节点，建议保持 mihomo 这种现代内核填法",
             "persistent-hint": True}},
            {"component": "VSwitch", "props": {"model": "use_proxy", "label": "通过 MoviePilot 代理抓取（默认直连）"}},
            {"component": "VSwitch", "props": {"model": "verify_tls", "label": "校验 TLS 证书"}},
            {"component": "VTextField", "props": {"model": "timeout", "label": "抓取超时（秒）",
             "type": "number", "min": 5, "max": 120}},
            {"component": "VTextField", "props": {"model": "retries", "label": "失败重试次数",
             "type": "number", "min": 0, "max": 5}},
            {"component": "VTextField", "props": {"model": "backup_dir", "label": "备份目录（留空=插件数据目录）",
             "hint": "备份文件名带时间戳，不会污染目标目录", "persistent-hint": True}},
            {"component": "VTextField", "props": {"model": "backup_keep", "label": "保留备份份数（0=不备份）",
             "type": "number", "min": 0, "max": 30}},
            {"component": "VSwitch", "props": {"model": "skip_if_same", "label": "内容一致时跳过写盘"}},
            {"component": "VSwitch", "props": {"model": "notify", "label": "更新完成或失败后发送通知"}},
            {"component": "VSwitch", "props": {"model": "restart_container",
             "label": "写盘成功后自动重启容器（默认开启）",
             "hint": "只重启下面填的这一个容器；内容一致跳过写盘时不会重启",
             "persistent-hint": True}},
            {"component": "VTextField", "props": {"model": "container_name", "label": "容器名",
             "hint": "docker ps 里的容器名，本实例是 Clash", "persistent-hint": True}},
            {"component": "VTextField", "props": {"model": "stop_timeout", "label": "重启前的停止等待（秒，0=Docker 默认）",
             "type": "number", "min": 0, "max": 30}},
            {"component": "VDivider"},
            # 规则速查用 VAlert 而不是 VTextField 的 hint：Vuetify 的提示行（.v-input__details）
            # 自带 overflow:hidden，且 insert_after / @MEMBERS 这类无空格长 token 撑出宽度后
            # 不会自动换行，小屏只会看到一行被截断的文字。
            {"component": "VAlert", "props": {"type": "info", "variant": "tonal", "class": "mb-2",
             "style": _WRAP_PRE,
             "text": "规则速查（每行一项，可只留需要的）：\n"
                     "set → 覆盖或新增顶层标量键，例如 port、allow-lan、secret\n"
                     "insert_after → 新增键的插入位置，写成「新键: 锚点键」\n"
                     "delete → 删除顶层键（键名写错会直接报错）\n"
                     "delete_if_present → 删除存在才删的键，用于清掉机场按 UA 追加的提示块\n"
                     "groups → 新增策略组，原文照抄插入；成员写 @MEMBERS(正则)，按订阅节点名自动展开\n"
                     "references → 往既有策略组插入引用，group 填组名、item 填要插入的名字、position 填 first 或 last\n"
                     "rules_prepend / rules_append → 在规则表首尾加规则\n"
                     "@SECRET 引用「面板密钥」配置项，规则里不用写明文。写错规则会在保存后直接提示，且不会改动配置文件。"}},
            {"component": "VTextarea", "props": {"model": "spec_text", "label": "改写规则（YAML）",
             "rows": 22, "style": "font-family: monospace; white-space: pre;"}},
        ]
        return [{"component": "VForm", "content": fields}], dict(DEFAULTS)

    def get_page(self) -> List[Dict[str, Any]]:
        """详情页：状态、上次结果、变更清单、预览差异、备份与手动操作。"""
        last = self.get_data("last_run") or {}
        preview = self.get_data("preview") or {}
        backups = list_backups(self._backup_path(), self._target_file, 5)
        rows: List[Dict[str, Any]] = []

        status = "已启用" if self._enabled else "未启用"
        rows.append({"component": "VAlert", "props": {"type": "info", "variant": "tonal",
                     "text": f"{status}｜Cron：{self._cron or '未设置'}｜目标："
                             f"{self._target_dir}/{self._target_file}｜"
                             f"上次：{last.get('time') or '尚未运行'}｜{self._restart_hint()}"}})
        if self._config_error or self._spec_error:
            rows.append({"component": "VAlert", "props": {"type": "error", "variant": "tonal",
                         "text": f"配置未就绪，插件不会做任何写入："
                                 f"{self._redact(self._config_error or self._spec_error)}"}})
        elif last:
            rows.append({"component": "VAlert", "props": {
                "type": "success" if last.get("ok") else "error", "variant": "tonal",
                "text": self._redact(f"{last.get('time')}（{last.get('reason')}）："
                                     f"{last.get('message')}")}})
        else:
            rows.append({"component": "VAlert", "props": {"type": "warning", "variant": "tonal",
                         "text": "尚未运行过。可点「立即更新」试一次，或先「预览差异」看改对了没有。"}})

        rows.append({"component": "div", "props": {"style": "display:flex;flex-wrap:wrap;gap:8px;padding:12px 4px"},
                     "content": [
                         {"component": "VBtn", "props": {"size": "small", "color": "primary",
                          "variant": "flat", "class": "text-none"},
                          "text": "立即更新",
                          "events": {"click": {"api": f"plugin/{self.__class__.__name__}/run",
                                               "method": "POST"}}},
                         {"component": "VBtn", "props": {"size": "small", "variant": "tonal",
                          "color": "grey-lighten-3", "class": "text-none"},
                          "text": "预览差异（不写盘）",
                          "events": {"click": {"api": f"plugin/{self.__class__.__name__}/preview",
                                               "method": "GET"}}},
                         {"component": "VBtn", "props": {"size": "small", "variant": "tonal",
                          "color": "grey-lighten-3", "class": "text-none",
                          "prepend-icon": "mdi-restart"},
                          "text": "只重启容器",
                          "events": {"click": {"api": f"plugin/{self.__class__.__name__}/restart",
                                               "method": "POST"}}},
                         {"component": "VBtn", "props": {"size": "small", "variant": "text",
                          "color": "primary", "class": "text-none",
                          "href": self.plugin_readme, "target": "_blank",
                          "rel": "noopener noreferrer", "prepend-icon": "mdi-book-open-outline"},
                          "text": "使用说明"},
                     ]})

        changes = last.get("changes") or preview.get("changes") or []
        if changes:
            rows.append(_card("改写动作",
                              _wrap_list([self._redact(str(item)) for item in changes])))
        if preview.get("diff"):
            rows.append({"component": "VCard", "props": {"variant": "outlined",
                         "title": f"最近预览差异（{preview.get('time')}）"},
                         "content": [{"component": "VCardText", "props": {}, "content": [
                             {"component": "pre", "props": {"class": "text-caption text-break",
                              "style": "white-space:pre-wrap;word-break:break-all;margin:0"},
                              "text": preview["diff"]}]}]})
        if last.get("stats"):
            stats = last["stats"]
            rows.append({"component": "div", "props": {"class": "text-caption text-medium-emphasis px-4 py-2",
                         "style": _WRAP,
                         "text": f"节点 {stats.get('proxies')}｜策略组 {stats.get('groups')}｜"
                                 f"规则 {stats.get('rules')}｜本次耗时 {last.get('duration')} 秒"}})
        if backups:
            rows.append(_card("最近的备份", _kv_list(
                [(row["name"], f"{row['time']}｜{_fmt_size(row['size'])}") for row in backups])))
        history = (self.get_data("history") or [])[:8]
        if history:
            rows.append(_card("最近记录", _wrap_list([
                self._redact(f"{row.get('time')}｜{'成功' if row.get('ok') else '失败'}｜"
                             f"{row.get('message')}") for row in history])))
        return rows

    def get_api(self) -> List[Dict[str, Any]]:
        """注册详情页按钮使用的自定义路由。"""
        return [
            {"path": "/run", "endpoint": self.api_run, "methods": ["POST"],
             "auth": "bear", "summary": "立即更新 Clash 配置"},
            {"path": "/preview", "endpoint": self.api_preview, "methods": ["GET"],
             "auth": "bear", "summary": "预览改写差异（不写盘）"},
            {"path": "/restart", "endpoint": self.api_restart, "methods": ["POST"],
             "auth": "bear", "summary": "立即重启 Clash 容器"},
        ]

    # ------------------------------------------------------------------ API 端点
    def api_run(self) -> Dict[str, Any]:
        """`POST /plugin/ClashSubRewriter/run`：执行一次更新，超时则转为后台进行中。"""
        box: Dict[str, Any] = {}

        def _job() -> None:
            box.update(self.update(reason="页面"))

        worker = threading.Thread(target=_job, daemon=True)
        worker.start()
        worker.join(_WAIT_SECONDS)
        if worker.is_alive():
            return _envelope(True, "更新仍在进行（订阅拉取较慢），请稍后刷新本页面查看结果。", None)
        return _envelope(bool(box.get("ok")), self._redact(box.get("message") or "更新已结束"), None)

    def api_preview(self) -> Dict[str, Any]:
        """`GET /plugin/ClashSubRewriter/preview`：生成差异预览。"""
        box: Dict[str, Any] = {}

        def _job() -> None:
            box.update(self.preview())

        worker = threading.Thread(target=_job, daemon=True)
        worker.start()
        worker.join(_WAIT_SECONDS)
        if worker.is_alive():
            return _envelope(True, "预览仍在进行，请稍后刷新本页面。", None)
        return _envelope(bool(box.get("ok")), self._redact(box.get("message") or "预览已结束"), None)

    def api_restart(self) -> Dict[str, Any]:
        """``POST /plugin/ClashSubRewriter/restart``：只重启容器，不改配置文件。

        手动按钮不受「写盘后自动重启」开关影响，点击即执行。
        """
        box: Dict[str, Any] = {}

        def _job() -> None:
            try:
                info = restart_container(self._container_name,
                                         stop_timeout=self._stop_timeout)
                box.update({"ok": True,
                            "message": f"已重启容器 {info['container']}（{info['status']}，"
                                       f"等待 {info['waited']} 秒）"})
            except UpdateError as error:
                box.update({"ok": False, "message": f"重启容器失败：{error}"})
            except Exception as error:
                logger.exception(f"Clash 订阅改写：重启容器异常 {type(error).__name__}: {error}")
                box.update({"ok": False,
                            "message": f"重启容器异常：{type(error).__name__}: {error}"})

        worker = threading.Thread(target=_job, daemon=True)
        worker.start()
        worker.join(_WAIT_SECONDS)
        if worker.is_alive():
            return _envelope(True, "重启指令已下发，容器仍在启动中，请稍后刷新本页面。", None)
        self.save_data("last_restart", {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                        "ok": bool(box.get("ok")),
                                        "message": self._redact(box.get("message") or "")})
        return _envelope(bool(box.get("ok")), self._redact(box.get("message") or "重启已结束"), None)

    # ------------------------------------------------------------------ 内部工具
    def _proxy_url(self) -> str:
        """按配置决定是否使用 MoviePilot 出口代理。"""
        return str(settings.PROXY_HOST or "").strip() if self._use_proxy else ""

    def _backup_path(self) -> str:
        """备份目录：配置优先，默认落在插件数据目录，保持目标目录干净。"""
        return self._backup_dir or str(self.get_data_path() / "backups")

    def _redact(self, text: Any) -> str:
        """把面板密钥从任何展示文本里抹掉。"""
        body = str(text or "")
        if self._secret:
            body = body.replace(self._secret, "***")
        return body

    def _record(self, ok: bool, message: str, reason: str,
                changes: Optional[List[str]] = None) -> None:
        """写入最近一次运行结果与历史列表。"""
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.save_data("last_run", {"time": stamp, "ok": bool(ok),
                                    "message": self._redact(message),
                                    "reason": reason, "changes": changes or []})
        history = self.get_data("history") or []
        history.insert(0, {"time": stamp, "ok": bool(ok),
                           "message": self._redact(message)})
        self.save_data("history", history[:10])


def _envelope(success: bool, message: str, data: Any = None) -> Dict[str, Any]:
    """前端 envelope 合同：必须恰好是 success/message/data 三个键。"""
    return {"success": bool(success), "message": str(message), "data": data}


def _card(title: str, items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """把一组列表项包成描边卡片。"""
    return {"component": "VCard", "props": {"variant": "outlined", "title": title},
            "content": [{"component": "VList", "props": {"dense": True}, "content": items}]}


def _wrap_list(texts: List[str]) -> List[Dict[str, Any]]:
    """单列文本列表，每项按容器宽度自动换行。"""
    return [{"component": "VListItem", "props": {}, "content": [
        {"component": "VListItemSubtitle", "props": {"class": "text-break", "style": _WRAP},
         "text": text}]} for text in texts]


def _kv_list(pairs: List[Tuple[str, str]]) -> List[Dict[str, Any]]:
    """标题加说明的两行列表项。"""
    return [{"component": "VListItem", "props": {}, "content": [
        {"component": "VListItemTitle", "props": {"style": _WRAP}, "text": name},
        {"component": "VListItemSubtitle", "props": {"class": "text-break", "style": _WRAP},
         "text": note}]} for name, note in pairs]


def _fmt_size(size: Any) -> str:
    """把字节数格式化成易读文本。"""
    try:
        value = float(size)
    except (TypeError, ValueError):
        return str(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"

from datetime import datetime
from threading import Lock
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger
from app.core.config import settings

try:
    from app.sdk.plugin import _PluginBase
    from app.sdk.logging import logger
except ModuleNotFoundError as exc:
    if exc.name not in ("app.sdk", "app.sdk.plugin", "app.sdk.logging"):
        raise
    from app.plugins import _PluginBase
    from app.log import logger

from .client import CheckinError, checkin_now, refresh_session

TZ = ZoneInfo("Asia/Shanghai")
LEGACY_TOKEN_FILE = "refresh_token"
DEFAULT = {"enabled": False, "cron": "5 8 * * *", "notify": True, "refresh_token": ""}
RUN_LOCK = Lock()


class XingchiCheckin(_PluginBase):
    """星驰 API 每日签到插件，会话令牌保存在插件配置中并随签到自动轮换。"""

    plugin_name = "星驰 API 签到"
    plugin_desc = "使用已登录会话续期，每日自动签到，不参与抽奖。"
    plugin_icon = "https://raw.githubusercontent.com/andyxu8023/MoviePilot-Plugins/main/icons/xingchi.png"
    plugin_version = "1.1.1"
    plugin_author = "左岸"
    author_url = "https://github.com/andyxu8023"
    # 使用说明入口：已安装插件投影读类属性，市场投影读 package 的 plugin_readme，两处都要给
    plugin_readme = ("https://github.com/andyxu8023/MoviePilot-Plugins"
                     "/blob/main/plugins.v3/xingchicheckin/README.md")
    plugin_config_prefix = "xingchicheckin_"
    plugin_order = 50
    auth_level = 2
    _enabled = False
    _notify = True
    _cron = "5 8 * * *"
    _refresh_token = ""
    _trigger = None
    _config_error = ""

    def init_plugin(self, config=None):
        """读取配置并安排签到计划；配置里没令牌时尝试迁移一次旧版会话文件。"""
        config = {**DEFAULT, **(config or {})}
        self._enabled = bool(config["enabled"])
        self._notify = bool(config["notify"])
        self._config_error = ""
        self._trigger = None
        token = str(config.get("refresh_token") or "").strip()
        if token:
            self._refresh_token = token
        elif self._refresh_token:
            logger.warning("星驰签到：配置中的令牌为空，沿用内存中最近一次轮换的令牌；确需清除请停用后重新加载插件")
        if not self._refresh_token:
            self._migrate_legacy_token()
        self._cron = str(config["cron"]).strip()
        try:
            self._trigger = CronTrigger.from_crontab(self._cron, timezone=TZ)
        except ValueError:
            self._config_error = "Cron 无效：请填写五段表达式，例如 5 8 * * *。"
            logger.warning("星驰签到：" + self._config_error)

    def _all_config(self) -> dict:
        """构造完整配置字典，供 update_config 整体写回，避免轮换令牌时丢掉其它字段。"""
        return {
            "enabled": self._enabled,
            "cron": self._cron,
            "notify": self._notify,
            "refresh_token": self._refresh_token,
        }

    def _migrate_legacy_token(self) -> None:
        """把旧版写在插件数据目录的会话文件迁移进配置，成功后删除原文件。"""
        path = settings.PLUGIN_DATA_PATH / self.__class__.__name__ / LEGACY_TOKEN_FILE
        if not path.is_file():
            return
        try:
            token = path.read_text(encoding="utf-8").strip()
        except OSError:
            logger.warning("星驰签到：旧会话文件无法读取，请在配置中填写 refresh_token")
            return
        if not token:
            path.unlink(missing_ok=True)
            return
        self._refresh_token = token
        if self.update_config(self._all_config()) is False:
            logger.warning("星驰签到：旧会话令牌写入配置失败，本次仍按文件运行，请检查数据库写入")
            return
        path.unlink(missing_ok=True)
        logger.info("星驰签到：已把旧会话文件迁移到插件配置，并删除数据目录中的 refresh_token")

    def get_state(self):
        """返回插件是否处于可调度状态。"""
        return self._enabled and self._trigger is not None

    @staticmethod
    def get_command():
        """本插件不提供远程命令。"""
        return []

    def get_api(self):
        """本插件不提供自定义接口。"""
        return []

    def get_form(self):
        """返回配置表单与默认配置，令牌字段用密码框避免肩窥。"""
        fields = [
            ("VSwitch", "enabled", "启用自动签到", {}),
            ("VTextField", "cron", "签到时间（五段 Cron，北京时间）", {}),
            ("VTextField", "refresh_token", "会话令牌 refresh_token", {
                "type": "password",
                "autocomplete": "new-password",
                "placeholder": "登录后填写，签到时自动轮换，无需手工更新",
            }),
            ("VSwitch", "notify", "发送签到结果和会话失效通知", {}),
        ]
        content = []
        for component, model, label, extra in fields:
            props = {"model": model, "label": label}
            props.update(extra)
            content.append({"component": component, "props": props})
        return [{"component": "VForm", "content": content}], DEFAULT.copy()

    def get_page(self):
        """详情页展示最近结果与令牌状态，只显示长度不显示令牌内容。"""
        last = self.get_data("last_run") or {}
        text = self._config_error or (f"{last['time']}：{last['message']}" if last else "尚未运行。")
        token_text = (f"会话令牌：已配置（{len(self._refresh_token)} 位，签到后自动轮换）"
                      if self._refresh_token else "会话令牌：未配置，请在配置中填写 refresh_token")
        return [{"component": "VAlert", "props": {"type": "info", "text": text}},
                {"component": "VAlert", "props": {"type": "warning" if not self._refresh_token else "info",
                                                  "text": token_text}},
                # 宿主只在「插件市场详情」投影 plugin_readme，已安装插件界面没有说明入口，
                # 所以详情页底部自带一条指向仓库 README 的链接（新标签打开）。
                {"component": "div",
                 "props": {"style": "display:flex;align-items:center;gap:8px;flex-wrap:wrap"},
                 "content": [{
                     "component": "VBtn",
                     "props": {"size": "small", "variant": "text", "color": "primary",
                               "href": self.plugin_readme, "target": "_blank",
                               "rel": "noopener noreferrer",
                               "prepend-icon": "mdi-book-open-outline",
                               "class": "text-none"},
                     "text": "使用说明",
                 }]}]

    def get_service(self):
        """按配置的 Cron 注册每日签到服务。"""
        if not self.get_state():
            return []
        return [{"id": "XingchiCheckin.Daily", "name": "星驰 API 签到",
                 "trigger": self._trigger, "func": self.checkin, "kwargs": {}}]

    def checkin(self):
        """续期会话并把站点轮换后的新令牌写回配置，随后提交一次签到。"""
        if not self.get_state() or not RUN_LOCK.acquire(blocking=False):
            return
        try:
            proxy = (settings.PROXY_HOST or "").strip()
            try:
                if not self._refresh_token:
                    raise CheckinError("未配置 refresh_token；请在插件配置中填写登录后的会话令牌")
                access, rotated = refresh_session(self._refresh_token, proxy=proxy)
                self._refresh_token = rotated
                if self.update_config(self._all_config()) is False:
                    logger.warning("星驰签到：轮换后的令牌保存失败，下次加载可能要求重新登录")
                message = checkin_now(access, proxy=proxy)
                logger.info("星驰签到：" + message)
            except CheckinError as exc:
                message = str(exc)
                logger.warning("星驰签到：" + message)
            except Exception as exc:
                message = f"运行异常（{type(exc).__name__}）；请检查部署与配置中的会话令牌。"
                logger.warning("星驰签到：" + message)
            self.save_data("last_run", {"time": datetime.now(TZ).isoformat(timespec="seconds"), "message": message})
            if self._notify:
                self.post_message(title=self.plugin_name, text=message)
        finally:
            RUN_LOCK.release()

    def stop_service(self):
        """停止插件服务并关闭启用状态。"""
        self._enabled = False

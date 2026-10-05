"""
馒头（M-Team）登录保活提醒插件

通过站点 ApiKey 调用馒头官方允许第三方使用的 /member/profile 接口读取
最后登录时间，按“连续 40 天不登录将被删除账号”的保活期限计算剩余天数并
推送提醒。插件不做任何自动登录、Cookie 或会话式访问，完全符合站点现行政策。
"""

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.db.site_oper import SiteOper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import NotificationType
from app.utils.http import RequestUtils
from app.utils.string import StringUtils


class MTeamKeepAlive(_PluginBase):
    """馒头登录保活提醒插件：每日读取最后登录时间，按保活期限推送剩余天数提醒。"""

    # 插件名称
    plugin_name = "馒头登录保活提醒"
    # 插件描述
    plugin_desc = "通过站点 ApiKey 查询馒头最后浏览时间，按 40 天保活线推送剩余天数提醒，不执行自动登录。"
    # 插件图标
    plugin_icon = "https://static.m-team.cc/favicon.ico"
    # 插件版本
    plugin_version = "1.0.4"
    # 插件标签
    plugin_label = "站点"
    # 插件作者
    plugin_author = "左岸"
    # 作者主页
    author_url = "https://github.com/andyxu8023"
    # 插件配置项ID前缀
    plugin_config_prefix = "mteamkeepalive_"
    # 加载顺序
    plugin_order = 99
    # 可使用的用户级别
    auth_level = 1

    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None

    # 配置属性
    _enabled: bool = False
    _cron: str = "1 0 * * *"
    _onlyonce: bool = False
    _limit_days: int = 40
    _warn_days: int = 10
    _daily_report: bool = True

    # 最近一次检查结果（插件侧视图，仅普通字典）
    _last_result: Dict[str, Any] = {}

    # 详情页顶部大号字体样式（内联样式优先于组件默认字号与密度内边距）
    _big_text_style = ("font-size:clamp(2rem,7vw,3rem);font-weight:700;line-height:1.25;"
                       "text-align:center;padding:20px 12px")

    # 副标题样式：另起一行并退回常规字号（否则会继承顶部大号字体）
    _sub_text_style = "font-size:.95rem;font-weight:400;line-height:1.5;margin-top:6px"
    # 详情页需要醒目标记的站点关键字与样式（跟随提示框文字色，浅/深色背景都清晰）
    _keyword = "馒头"
    _keyword_style = ("font-weight:700;text-decoration:underline;"
                      "text-underline-offset:3px;color:inherit")
    # 副标题文案：正常 / 临期 / 已超期
    _subtitle_normal = "目前站点活跃度正常"
    _subtitle_warn = "站点活跃度已临期，请尽快手动登录馒头站点保活"
    _subtitle_over = "已超期未登录站点，账号可能已被封，请跳转至馒头站点确认"

    def init_plugin(self, config: dict = None) -> None:
        """根据插件配置初始化运行状态。"""
        # 停止现有任务
        self.stop_service()

        # 读取配置
        if config:
            self._enabled = bool(config.get("enabled", False))
            self._cron = str(config.get("cron") or "1 0 * * *")
            self._onlyonce = bool(config.get("onlyonce", False))
            self._limit_days = int(config.get("limit_days") or 40)
            self._warn_days = int(config.get("warn_days") or 10)
            self._daily_report = bool(config.get("daily_report", True))
            self._last_result = dict(config.get("last_result") or {})

        # 立即运行一次
        if self._enabled and self._onlyonce:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            logger.info("馒头登录保活提醒：立即运行一次")
            self._scheduler.add_job(
                func=self.run_check,
                trigger="date",
                run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                name="馒头登录保活检查",
            )
            # 关闭一次性开关并回写配置
            self._onlyonce = False
            self._save_config()
            if self._scheduler.get_jobs():
                self._scheduler.start()

    def get_state(self) -> bool:
        """获取插件启用状态。"""
        return self._enabled

    def _save_config(self) -> None:
        """保存插件配置（含最近一次检查结果）。"""
        self.update_config({
            "enabled": self._enabled,
            "cron": self._cron,
            "onlyonce": self._onlyonce,
            "limit_days": self._limit_days,
            "warn_days": self._warn_days,
            "daily_report": self._daily_report,
            "last_result": self._last_result,
        })

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """返回插件远程命令列表。"""
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """返回插件 API 列表。"""
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        """注册插件定时服务。"""
        if self._enabled and str(self._cron).strip().count(" ") == 4:
            try:
                return [{
                    "id": "MTeamKeepAlive",
                    "name": "馒头登录保活提醒服务",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self.run_check,
                    "kwargs": {},
                }]
            except Exception as err:
                logger.error(f"馒头保活提醒定时任务配置错误：{str(err)}")
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回插件配置表单与默认配置。"""
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "enabled", "label": "启用插件"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "daily_report", "label": "每日汇报"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "onlyonce", "label": "立即运行一次"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VCronField",
                                    "props": {
                                        "model": "cron",
                                        "label": "执行周期",
                                        "placeholder": "5位cron表达式，默认凌晨0点01分",
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "limit_days",
                                        "type": "number",
                                        "label": "保活期限（天）",
                                        "hint": "连续不登录达到该天数将被删号",
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "warn_days",
                                        "type": "number",
                                        "label": "临期提醒（天）",
                                        "hint": "剩余天数不大于该值时必发通知",
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "content": [{
                                "component": "VAlert",
                                "props": {
                                    "type": "info",
                                    "variant": "tonal",
                                    "text": "插件只调用官方允许第三方使用的 profile 接口查询最后浏览时间，"
                                            "不会自动登录。关闭“每日汇报”后，仅在剩余天数进入临期范围或检查失败时通知。",
                                },
                            }],
                        }],
                    },
                ],
            }
        ], {
            "enabled": False,
            "daily_report": True,
            "onlyonce": False,
            "cron": "1 0 * * *",
            "limit_days": 40,
            "warn_days": 10,
        }

    def _days_text(self, days: Any) -> str:
        """将距今天数转为可读文案，0 天显示今天。"""
        try:
            value = int(days)
        except (TypeError, ValueError):
            return "—"
        return "今天" if value <= 0 else f"{value} 天前"

    def _remaining_alert(self, remaining: Any) -> Tuple[str, str, str]:
        """按剩余天数返回提示框类型、大号文案与副标题：临期黄色、超期红色。"""
        try:
            value = int(remaining)
        except (TypeError, ValueError):
            return "info", "剩余 — 天", self._subtitle_normal
        if value < 0:
            return "error", f"已超期 {-value} 天", self._subtitle_over
        if value == 0:
            return "error", "剩余 0 天", self._subtitle_over
        if value <= self._warn_days:
            return "warning", f"剩余 {value} 天", self._subtitle_warn
        return "info", f"剩余 {value} 天", self._subtitle_normal

    def _subtitle_node(self, text: str, link: Optional[str] = None) -> dict:
        """生成副标题节点：常规字号另起一行，站点关键字加粗下划线，可带站点链接。"""
        children: List[dict] = []
        for index, part in enumerate(str(text).split(self._keyword)):
            if index:
                keyword: Dict[str, Any] = {
                    "component": "a" if link else "span",
                    "props": {"style": self._keyword_style},
                    "text": self._keyword,
                }
                if link:
                    keyword["props"].update({
                        "href": link,
                        "target": "_blank",
                        "rel": "noopener noreferrer",
                    })
                children.append(keyword)
            if part:
                children.append({"component": "span", "text": part})
        return {
            "component": "div",
            "props": {"style": self._sub_text_style},
            "content": children,
        }

    def get_page(self) -> Optional[List[dict]]:
        """返回插件详情页：顶部大号剩余天数与副标题（临期黄、超期红），历史含最近一次记录。"""
        if not self._enabled:
            return None
        content: List[dict] = []
        history = self.get_data("check_history") or []
        # 配置里的最近一次结果可能缺失（例如只写过历史），退回历史首条
        last = self._last_result or (history[0] if history else {})
        if last:
            alert_type, big_text, subtitle = self._remaining_alert(last.get("remaining"))
            alert: Dict[str, Any] = {
                "component": "VAlert",
                "props": {
                    "type": alert_type,
                    "variant": "tonal",
                    "style": self._big_text_style,
                },
                "text": big_text,
            }
            if subtitle:
                # 临期与超期文案里的「馒头」要能直接点回站点，地址取自站点管理
                link = self._site_url() if self._keyword in subtitle else None
                alert["content"] = [self._subtitle_node(subtitle, link)]
            content.append(alert)
        if history:
            content.append({
                "component": "VDivider",
                "props": {"class": "my-2"},
            })
            content.append({
                "component": "VListItem",
                "props": {"class": "text-subtitle-2 font-weight-bold"},
                "text": f"检查历史（最近 {min(len(history), 10)} 次）",
            })
            for index, record in enumerate(history[:10]):
                # 旧格式记录没有 last_browse 字段，退回按最后登录展示，避免标签误导
                browse = record.get("last_browse")
                main_line = (f"最后浏览 {browse}" if browse
                             else f"最后登录 {record.get('last_login') or '—'}")
                content.append({
                    "component": "VListItem",
                    "props": {
                        "dense": True,
                        "class": "text-break",
                        "style": "white-space:pre-line",
                    },
                    "text": (f"{'【最新】' if index == 0 else ''}"
                             f"{record.get('checked_at') or '—'} 检查：\n"
                             f"{main_line}，{self._days_text(record.get('days_since'))}，"
                             f"剩 {record.get('remaining', '—')} 天"),
                })
        if not content:
            content.append({
                "component": "VAlert",
                "props": {"type": "info", "text": "暂无检查记录，到达执行周期后自动检查。"},
            })
        # 弹窗标题已显示插件名，页面内不再重复包一层带标题的卡片
        return content

    def stop_service(self) -> None:
        """停止插件后台服务并释放资源。"""
        if self._scheduler:
            try:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
            except Exception as err:
                logger.error(f"停止馒头保活提醒服务失败：{str(err)}")

    def _get_site(self):
        """从已配置站点中定位馒头站点记录。"""
        sites = SiteOper().list_order_by_pri()
        for site in sites:
            domain = str(site.domain or site.url or "").lower()
            if "m-team" in domain:
                return site
        return None

    def _site_url(self) -> Optional[str]:
        """从站点管理取馒头站点地址，只保留协议与域名，避免带上 URL 参数或凭据。"""
        try:
            site = self._get_site()
            if not site:
                return None
            raw = str(site.url or "").strip()
            parsed = urlparse(raw if "//" in raw else f"//{raw}")
            netloc = parsed.netloc or str(site.domain or "").strip()
            # 去掉可能存在的用户信息和 URL 参数，只留主机名
            netloc = netloc.split("@")[-1].split("?")[0].strip("/")
            if not netloc:
                return None
            return f"{parsed.scheme or 'https'}://{netloc}/"
        except Exception as err:
            logger.warning(f"馒头保活提醒：获取站点地址失败：{str(err)}")
            return None

    def _fetch_profile(self, site) -> Optional[Dict[str, Any]]:
        """使用站点 ApiKey 调用官方允许的资料接口获取用户信息。"""
        domain = StringUtils.get_url_domain(site.url)
        if not domain:
            logger.error("馒头保活检查：站点地址无法解析域名")
            return None
        url = f"https://api.{domain}/api/member/profile"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": site.ua or "Mozilla/5.0",
            "x-api-key": site.apikey,
        }
        res = RequestUtils(
            headers=headers,
            proxies=settings.PROXY if site.proxy else None,
            timeout=30,
            referer=f"{str(site.url).rstrip('/')}/user",
        ).post_res(url, json={})
        if not res:
            logger.error("馒头保活检查：无法连接站点接口")
            return None
        if res.status_code != 200:
            logger.error(f"馒头保活检查：接口状态码 {res.status_code}")
            return None
        try:
            payload = res.json()
        except ValueError:
            logger.error("馒头保活检查：接口返回数据异常")
            return None
        if str(payload.get("code")) != "0":
            logger.error(f"馒头保活检查：接口返回 {payload.get('message') or payload.get('code')}")
            return None
        data = payload.get("data")
        return data if isinstance(data, dict) else None

    def run_check(self) -> None:
        """执行一次馒头登录保活检查，任何异常都转为错误通知，避免静默失败。"""
        try:
            self._do_check()
        except Exception as err:
            logger.error(f"馒头保活检查执行异常：{str(err)}")
            site_name = "馒头"
            try:
                site = self._get_site()
                if site:
                    site_name = site.name
            except Exception:
                pass
            self._notify(site_name, None, error=f"插件执行异常：{str(err)[:200]}")

    def _do_check(self) -> None:
        """执行保活检查主流程并按策略推送提醒。"""
        logger.info("开始执行馒头登录保活检查 ...")
        site = self._get_site()
        if not site:
            logger.warning("馒头保活提醒：未找到匹配的站点，请检查保活站点选择或馒头站点配置")
            return
        if not site.apikey:
            logger.warning("馒头保活提醒：站点未配置 ApiKey，无法查询登录信息")
            self._notify(site.name, None, error="站点未配置 ApiKey，请先在站点管理中补全")
            return

        data = self._fetch_profile(site)
        if not data:
            self._notify(site.name, None, error="查询站点资料接口失败，请检查 ApiKey 有效性或站点状态")
            return

        member_status = data.get("memberStatus") or {}
        browse_str = str(member_status.get("lastBrowse") or "").strip()
        login_str = str(member_status.get("lastLogin") or "").strip()
        # 馒头 40 天删号看“用浏览器存取网站网页”，以 lastBrowse 为准（已验证 ApiKey 调用不会刷新它）；
        # lastLogin 只记录凭据登录事件，免登录浏览不会更新，仅作参考
        active_str = browse_str or login_str
        if not active_str:
            self._notify(site.name, None, error="接口未返回最后浏览或登录时间，无法判断保活状态")
            return
        try:
            active_at = datetime.strptime(active_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            self._notify(site.name, None, error=f"最后浏览时间格式异常：{active_str}")
            return

        now = datetime.now(tz=pytz.timezone(settings.TZ))
        # 站点按“连续 N 天未登录/未浏览”判定，这里用自然日差值而不是 24 小时取整；
        # 否则昨天中午浏览、今天凌晨检查会被算成 0 天前（剩余天数少扣一天）
        days_since = max((now.date() - active_at.date()).days, 0)
        remaining = self._limit_days - days_since

        self._last_result = {
            "checked_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "last_browse": browse_str,
            "last_login": login_str,
            "days_since": days_since,
            "remaining": remaining,
        }
        self._save_config()

        history = self.get_data("check_history") or []
        history.insert(0, dict(self._last_result))
        self.save_data("check_history", history[:30])

        logger.info(
            f"馒头保活检查完成：最后浏览 {active_str}（{days_since} 天前），"
            f"最后登录 {login_str or '—'}，距 {self._limit_days} 天期限还剩 {remaining} 天"
        )

        urgent = remaining <= 0
        warn = remaining <= self._warn_days
        if self._daily_report or warn:
            self._notify(site.name, self._last_result)

    def _notify(self, site_name: str, result: Optional[Dict[str, Any]], error: Optional[str] = None) -> None:
        """推送保活提醒通知；result 为空表示检查失败，仅发送错误说明。"""
        if error:
            self.post_message(
                mtype=NotificationType.SiteMessage,
                title=f"【{site_name}】保活检查失败",
                text=f"馒头登录保活提醒插件执行失败：{error}",
            )
            return

        remaining = int(result.get("remaining", 0))
        urgent = remaining <= 0
        warn = remaining <= self._warn_days
        if urgent:
            title = f"🔴【{site_name}】保活已超期"
        elif warn:
            title = f"⚠️【{site_name}】登录保活提醒"
        else:
            title = f"【{site_name}】登录保活提醒"

        days_since = int(result.get("days_since", 0))
        days_text = "今天" if days_since <= 0 else f"{days_since} 天前"
        lines = [
            f"最后浏览：{result.get('last_browse') or result.get('last_login') or '—'}（{days_text}）",
            f"最后登录：{result.get('last_login') or '—'}",
            f"保活期限：连续 {self._limit_days} 天不登录或不浏览将被删除账号",
            f"剩余天数：{remaining} 天",
        ]
        if urgent:
            lines.append("已超期！账号面临删除风险，请立即手动登录一次（浏览器打开网站即可）。")
        elif warn:
            lines.append("临近保活期限，请尽早手动登录一次。")
        self.post_message(
            mtype=NotificationType.SiteMessage,
            title=title,
            text="\n".join(lines),
        )

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
    plugin_icon = "https://static.m-team.cc/static/media/logo.c8cadbc19d44126ea2ca6ccb239a06fb.svg"
    # 插件版本
    plugin_version = "1.0.5"
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

    # ---------- 详情页样式：只用已核对存在的静态类与内联样式，颜色一律交给组件属性 ----------
    # 主状态卡：去掉默认图标，让大号天数在卡里真正居中
    _hero_style = "padding:22px 16px;text-align:center"
    _hero_row_style = ("display:flex;align-items:baseline;justify-content:center;"
                       "gap:8px;flex-wrap:wrap")
    _hero_num_style = "font-size:clamp(2.4rem,9vw,3.4rem);font-weight:700;line-height:1"
    _hero_word_style = "font-size:1.05rem;font-weight:500;opacity:.85"
    _notice_title_style = "font-size:1.35rem;font-weight:600;line-height:1.3"
    # 副标题样式：另起一行并退回常规字号（否则会继承大号字体）
    _sub_text_style = ("font-size:.95rem;font-weight:400;line-height:1.5;"
                       "margin-top:10px;text-align:center")
    # 脚注、分区标题与网格标签/取值样式
    _foot_style = "font-size:.8rem;line-height:1.6;margin-top:14px;opacity:.85;text-align:center"
    _card_title_style = "font-size:1.02rem;font-weight:600;line-height:1.4"
    _label_style = "font-size:.72rem;line-height:1.4;letter-spacing:.03em"
    _value_style = "font-size:.95rem;line-height:1.45;margin-top:3px"
    _hint_style = "font-size:.75rem;line-height:1.4;margin-top:2px"
    # 概况网格：靠 flex 自适应列数，不用 VRow/VCol 的负 margin，窄屏不会散成一列一个大格
    _grid_style = "display:flex;flex-wrap:wrap;gap:14px 18px"
    _cell_style = "flex:1 1 calc(33.333% - 12px);min-width:160px"
    # 列表标题与副标题：覆盖 Vuetify 的单行截断，长文本按容器宽度自动换行
    _list_title_style = ("display:flex;align-items:center;gap:6px;flex-wrap:wrap;"
                         "overflow:visible;text-overflow:clip;-webkit-line-clamp:unset")
    _wrap_style = ("display:block;white-space:normal;overflow:visible;"
                   "text-overflow:clip;-webkit-line-clamp:unset")
    # 详情页需要醒目标记的站点关键字与样式（跟随提示框文字色，浅/深色背景都清晰）
    _keyword = "馒头"
    _keyword_style = ("font-weight:700;text-decoration:underline;"
                      "text-underline-offset:3px;color:inherit")
    # 副标题文案：正常 / 临期 / 已超期
    _subtitle_normal = "目前站点活跃度正常"
    _subtitle_warn = "站点活跃度已临期，请尽快手动登录馒头站点保活"
    _subtitle_over = "已超期未登录站点，账号可能已被封，请跳转至馒头站点确认"
    # 详情页最多展示的检查次数
    _page_history = 10

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

    def _status_meta(self, remaining: Any) -> Dict[str, str]:
        """按剩余天数返回状态色、大号文案分段、状态标签与副标题：临期黄、超期红。"""
        try:
            value = int(remaining)
        except (TypeError, ValueError):
            return {"color": "info", "lead": "剩余", "number": "—",
                    "label": "状态未知", "subtitle": self._subtitle_normal}
        if value < 0:
            return {"color": "error", "lead": "已超期", "number": str(-value),
                    "label": "需立即登录", "subtitle": self._subtitle_over}
        if value == 0:
            return {"color": "error", "lead": "剩余", "number": "0",
                    "label": "今天到期", "subtitle": self._subtitle_over}
        if value <= self._warn_days:
            return {"color": "warning", "lead": "剩余", "number": str(value),
                    "label": "临期提醒", "subtitle": self._subtitle_warn}
        return {"color": "info", "lead": "剩余", "number": str(value),
                "label": "活跃度正常", "subtitle": self._subtitle_normal}

    def _chip_text(self, remaining: Any) -> str:
        """剩余天数徽章文案。"""
        try:
            value = int(remaining)
        except (TypeError, ValueError):
            return "剩余 — 天"
        if value < 0:
            return f"超期 {-value} 天"
        if value == 0:
            return "今天到期"
        return f"剩 {value} 天"

    @staticmethod
    def _short_time(value: Any) -> str:
        """把 2026-10-17 01:52:51 收成 2026-10-17 01:52，窄屏上不易折行。"""
        text = str(value or "").strip()
        return text[:16] if text else "—"

    @staticmethod
    def _host(link: str) -> str:
        """取站点地址的主机名，用于概况卡的次要说明。"""
        try:
            return urlparse(str(link)).netloc or str(link)
        except Exception:
            return str(link)

    def _days_since(self, timestamp: Any) -> Optional[int]:
        """按自然日计算时间串距今的天数，取不到时间返回 None。"""
        text = str(timestamp or "").strip()
        if not text:
            return None
        for fmt, size in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d", 10)):
            try:
                moment = datetime.strptime(text[:size], fmt)
                break
            except ValueError:
                continue
        else:
            return None
        try:
            today = datetime.now(tz=pytz.timezone(settings.TZ)).date()
        except Exception:
            today = datetime.now().date()
        return max((today - moment.date()).days, 0)

    def _deadline_text(self, record: Dict[str, Any]) -> str:
        """按最后浏览（缺失时退回最后登录）加保活期限推算保活截止日。"""
        days = self._days_since(record.get("last_browse") or record.get("last_login"))
        if days is None:
            return "—"
        try:
            limit = int(self._limit_days)
        except (TypeError, ValueError):
            return "—"
        return (datetime.now() + timedelta(days=limit - days)).strftime("%Y-%m-%d")

    def _next_check_text(self) -> str:
        """按执行周期推算下一次自动检查时间，未启用或表达式非法时返回占位符。"""
        cron = str(self._cron or "").strip()
        if not self._enabled or cron.count(" ") != 4:
            return "—"
        try:
            timezone = pytz.timezone(settings.TZ)
            next_time = CronTrigger.from_crontab(
                cron, timezone=timezone).get_next_fire_time(None, datetime.now(tz=timezone))
            return next_time.strftime("%Y-%m-%d %H:%M") if next_time else "—"
        except Exception as err:
            logger.warning(f"馒头保活提醒：推算下次检查时间失败：{str(err)}")
            return "—"

    def _chip(self, text: str, color: str = "grey-lighten-3", variant: str = "tonal") -> dict:
        """生成一枚小徽章；颜色走组件属性，不依赖运行期才注入的全局颜色类。"""
        return {
            "component": "VChip",
            "props": {"size": "x-small", "variant": variant, "color": color,
                      "class": "text-none"},
            "text": text,
        }

    def _cell(self, label: str, value: Any, hint: str = "") -> dict:
        """概况网格中的一格：弱化标签 + 正常取值 + 可选补充说明。"""
        children: List[dict] = [
            {"component": "div",
             "props": {"class": "text-medium-emphasis", "style": self._label_style},
             "text": label},
            {"component": "div", "props": {"style": self._value_style},
             "text": str(value) if str(value or "").strip() else "—"},
        ]
        if hint:
            children.append({
                "component": "div",
                "props": {"class": "text-medium-emphasis", "style": self._hint_style},
                "text": hint,
            })
        return {"component": "div", "props": {"style": self._cell_style}, "content": children}

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

    def _notice_block(self, alert_type: str, title: str, description: str) -> dict:
        """未启用或暂无记录时的状态卡，与主卡保持同样的排版节奏。"""
        return {
            "component": "VAlert",
            "props": {"type": alert_type, "variant": "tonal", "icon": False,
                      "class": "mb-3", "style": self._hero_style},
            "content": [
                {"component": "div", "props": {"style": self._notice_title_style},
                 "text": title},
                {"component": "div", "props": {"style": self._sub_text_style},
                 "text": description},
            ],
        }

    def _hero_block(self, record: Dict[str, Any], link: Optional[str]) -> dict:
        """状态主卡：状态徽章 + 大号剩余天数 + 副标题 + 期限进度条 + 关键时间脚注。"""
        status = self._status_meta(record.get("remaining"))
        try:
            limit = max(int(self._limit_days), 1)
        except (TypeError, ValueError):
            limit = 40
        try:
            used = min(max(int(record.get("days_since") or 0), 0), limit)
        except (TypeError, ValueError):
            used = 0
        content: List[dict] = [
            {"component": "div", "props": {"style": "display:flex;justify-content:center"},
             "content": [self._chip(status["label"], status["color"], "flat")]},
            {"component": "div", "props": {"style": self._hero_row_style}, "content": [
                {"component": "span", "props": {"style": self._hero_word_style},
                 "text": status["lead"]},
                {"component": "span", "props": {"style": self._hero_num_style},
                 "text": status["number"]},
                {"component": "span", "props": {"style": self._hero_word_style}, "text": "天"},
            ]},
            # 临期与超期文案里的「馒头」要能直接点回站点，地址取自站点管理
            self._subtitle_node(status["subtitle"],
                                 link if self._keyword in status["subtitle"] else None),
            {"component": "VProgressLinear",
             "props": {"model-value": round(used * 100 / limit, 1), "color": status["color"],
                       "height": 6, "rounded": True, "class": "mt-4"}},
            {"component": "div", "props": {"style": self._foot_style},
             "text": (f"最后浏览 "
                      f"{self._short_time(record.get('last_browse') or record.get('last_login'))}"
                      f"（{self._days_text(record.get('days_since'))}） · "
                      f"已用 {used}/{limit} 天 · 保活截止 {self._deadline_text(record)}")},
        ]
        return {
            "component": "VAlert",
            "props": {"type": status["color"], "variant": "tonal", "icon": False,
                      "class": "mb-3", "style": self._hero_style},
            "content": content,
        }

    def _overview_card(self, record: Dict[str, Any], link: Optional[str],
                       site_name: str) -> dict:
        """概况卡：浏览与登录时间、检查周期、期限口径与站点入口。"""
        record = record or {}
        login_days = self._days_since(record.get("last_login"))
        browse = str(record.get("last_browse") or "").strip()
        actions: List[dict] = [{
            "component": "span",
            "props": {"class": "text-medium-emphasis", "style": self._hint_style},
            "text": "只读站点 ApiKey 的 profile 接口，不执行自动登录",
        }]
        if link:
            actions.append({
                "component": "VBtn",
                "props": {"size": "small", "variant": "tonal", "color": "primary",
                          "href": link, "target": "_blank", "rel": "noopener noreferrer",
                          "class": "text-none"},
                "text": f"打开 {site_name or self._keyword}",
            })
        return {
            "component": "VCard",
            "props": {"variant": "outlined", "class": "mb-3"},
            "content": [
                {"component": "div", "props": {"class": "px-4 pt-4 pb-3",
                                               "style": self._card_title_style},
                 "text": "检查概况"},
                {
                    "component": "div",
                    "props": {"class": "px-4", "style": self._grid_style},
                    "content": [
                        self._cell("最后浏览", self._short_time(browse),
                                   f"{self._days_text(record.get('days_since'))} · 保活看这项"
                                   if browse else ""),
                        self._cell("最后登录",
                                   self._short_time(record.get("last_login")),
                                   self._days_text(login_days) if login_days is not None else ""),
                        self._cell("最近检查", self._short_time(record.get("checked_at"))),
                        self._cell("下次检查", self._next_check_text(),
                                   f"执行周期 {self._cron}"),
                        self._cell("保活期限", f"{self._limit_days} 天",
                                   f"剩余不大于 {self._warn_days} 天必发通知"),
                        self._cell("保活站点", site_name or "未找到站点",
                                   self._host(link) if link else "站点管理里未匹配到馒头站点"),
                    ],
                },
                {
                    "component": "div",
                    "props": {"class": "px-4 py-3",
                              "style": "display:flex;align-items:center;"
                                       "justify-content:space-between;gap:10px;flex-wrap:wrap"},
                    "content": actions,
                },
            ],
        }

    def _history_card(self, history: List[dict]) -> dict:
        """历史卡：每次检查一行，标题带检查时间与剩余天数徽章，副标题说明当时的浏览时间。"""
        shown = history[:self._page_history]
        items: List[dict] = []
        for index, record in enumerate(shown):
            status = self._status_meta(record.get("remaining"))
            title_children: List[dict] = [
                {"component": "span", "text": self._short_time(record.get("checked_at"))},
            ]
            if index == 0:
                title_children.append(self._chip("最新", "primary", "tonal"))
            title_children.append(self._chip(self._chip_text(record.get("remaining")),
                                             status["color"], "flat"))
            # 旧格式记录没有 last_browse 字段，退回按最后登录展示，避免标签误导
            browse = record.get("last_browse")
            if browse:
                main_line = f"最后浏览 {self._short_time(browse)}"
            elif record.get("last_login"):
                main_line = f"最后登录 {self._short_time(record.get('last_login'))}"
            else:
                main_line = "无浏览与登录记录"
            items.append({
                "component": "VListItem",
                "props": {"density": "compact"},
                "content": [
                    {"component": "VListItemTitle",
                     "props": {"style": self._list_title_style},
                     "content": title_children},
                    {"component": "VListItemSubtitle",
                     "props": {"class": "text-break", "style": self._wrap_style},
                     "text": f"{main_line} · {self._days_text(record.get('days_since'))}"},
                ],
            })
            if index < len(shown) - 1:
                items.append({"component": "VDivider", "props": {"inset": True}})
        return {
            "component": "VCard",
            "props": {"variant": "outlined"},
            "content": [
                {
                    "component": "div",
                    "props": {"class": "px-4 pt-4",
                              "style": "display:flex;align-items:baseline;gap:8px;flex-wrap:wrap"},
                    "content": [
                        {"component": "span", "props": {"style": self._card_title_style},
                         "text": "检查历史"},
                        {"component": "span",
                         "props": {"class": "text-medium-emphasis", "style": self._hint_style},
                         "text": f"显示最近 {len(shown)} 次 / 共 {len(history)} 次"},
                    ],
                },
                {"component": "VList", "props": {"density": "compact", "class": "pt-1 pb-2"},
                 "content": items},
            ],
        }

    def get_page(self) -> List[dict]:
        """返回插件详情页：保活状态主卡 + 检查概况 + 检查历史。"""
        history = self.get_data("check_history") or []
        # 配置里的最近一次结果可能缺失（例如只写过历史），退回历史首条
        last = self._last_result or (history[0] if history else {})
        site = None
        try:
            site = self._get_site()
        except Exception as err:
            logger.warning(f"馒头保活提醒：读取站点信息失败：{str(err)}")
        link = self._site_url(site) if site else None
        site_name = str(getattr(site, "name", "") or "").strip() if site else ""

        if not self._enabled:
            return [self._notice_block(
                "warning", "插件未启用",
                f"在插件配置里打开「启用插件」后，会按执行周期 {self._cron} 自动检查"
                f"{site_name or self._keyword}的最后浏览时间并提醒。"
            )]
        blocks: List[dict] = []
        if last:
            blocks.append(self._hero_block(last, link))
        else:
            blocks.append(self._notice_block(
                "info", "暂无检查记录",
                "到达执行周期后会自动检查，并把剩余天数与检查历史显示在这里。"
            ))
        blocks.append(self._overview_card(last, link, site_name))
        if history:
            blocks.append(self._history_card(history))
        # 弹窗标题已显示插件名，页面内不再重复包一层带标题的卡片
        return blocks

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

    def _site_url(self, site=None) -> Optional[str]:
        """从站点管理取馒头站点地址，只保留协议与域名，避免带上 URL 参数或凭据。"""
        try:
            site = site if site is not None else self._get_site()
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

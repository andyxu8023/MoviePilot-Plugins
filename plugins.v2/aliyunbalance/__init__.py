"""
阿里云账户余额监控插件

调用阿里云官方 OpenAPI（BssOpenApi 2017-12-14 `QueryAccountBalance`）读取账户可用额度与
现金余额，在余额低于预警线/紧急线、余额骤降或查询失败时推送通知，并保留检查历史。

检查频率是自适应的：平时按「日常间隔」低频查询；一旦监听到宿主广播的
`ChainEventType.AgentLLMProvider`（本轮 Agent 开始解析 LLM 供应商，早于任何消耗）或
`EventType.AgentTokensUsage`（一轮结束后的模型用量事件），或发现相邻两次余额
明显下降，就切换到「每 N 分钟」的高频窗口，窗口到期后自动回落，不需要改 cron 或重载插件。
注意百炼大模型推理为分钟级出账（通常 2~10 分钟），高频读数存在固有滞后。

签名按阿里云 RPC 签名机制（HMAC-SHA1，SignatureVersion 1.0）用标准库实现，不引入额外
依赖；填写的 AccessKey 只需费用中心只读权限（`AliyunBSSReadOnlyAccess`）即可。
"""

import base64
import hashlib
import hmac
import os
import uuid
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import pytz
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import ChainEventType, EventType, NotificationType
from app.sdk.events import Event, eventmanager
from app.sdk.scheduler import add_plugin_once_job, remove_plugin_once_job
from app.utils.http import RequestUtils


class AliyunBalance(_PluginBase):
    """阿里云余额监控插件：自适应频率查询账户余额，按阈值、骤降与消费兜底规则推送通知。"""

    # 插件名称
    plugin_name = "阿里云余额监控"
    # 插件描述
    plugin_desc = ("监控阿里云账户可用额度与现金余额，智能助手调用大模型时自动转为高频检查，"
                   "低于阈值或余额骤降时推送通知。")
    # 插件图标：阿里云官方标识（640x640 橙底白色 logotype），存放在本仓库 icons/ 目录。
    # 必须用外链且域名要在图片代理白名单里：前端对 http 开头的图标一律走核心图片代理，
    # 代理按 SECURITY_IMAGE_DOMAINS 逐请求校验，alicdn / aliyun.com 不在名单内会被判
    # domain_not_allowed 而不显示；raw.githubusercontent.com 在默认名单内，故引用 GitHub。
    plugin_icon = ("https://raw.githubusercontent.com/andyxu8023/"
                   "MoviePilot-Plugins/main/icons/aliyunbalance.png")
    # 插件版本
    plugin_version = "1.6.11"
    # 插件标签
    plugin_label = "工具,监控,云服务"
    # 插件作者
    plugin_author = "左岸"
    # 作者主页
    author_url = "https://github.com/andyxu8023"
    # 使用说明入口：已安装插件投影读类属性，市场投影读 package 的 plugin_readme，两处都要给
    plugin_readme = ("https://github.com/andyxu8023/MoviePilot-Plugins"
                     "/blob/main/plugins.v2/aliyunbalance/README.md")
    # 插件配置项ID前缀
    plugin_config_prefix = "aliyunbalance_"
    # 加载顺序
    plugin_order = 96
    # 可使用的用户级别
    auth_level = 1

    # API 公共参数：产品版本与接口名（费用与成本 2017-12-14）
    _api_version = "2017-12-14"
    _api_action = "QueryAccountBalance"
    # 服务接入点：地域ID 与显示名（中国站 / 国际站）
    _endpoints: Dict[str, Tuple[str, str, str]] = {
        "business.aliyuncs.com": ("business.aliyuncs.com", "cn-hangzhou", "中国站（阿里云国内账号）"),
        "business.ap-southeast-1.aliyuncs.com": (
            "business.ap-southeast-1.aliyuncs.com", "ap-southeast-1", "国际站（alibabacloud.com 账号）"),
    }
    # 监控口径：取值字段与显示名
    _watch_fields: Dict[str, str] = {
        "AvailableAmount": "可用额度（含信控额度，欠费判定看这项）",
        "AvailableCashAmount": "现金余额（实际充值剩余部分）",
    }
    # 通知策略
    _notify_modes: Dict[str, str] = {
        "alert": "仅告警（低于阈值 / 余额骤降 / 查询失败）",
        "always": "每次检查都汇报",
    }
    # 币种符号
    _currencies: Dict[str, str] = {
        "CNY": "¥", "USD": "$", "JPY": "JP¥",
    }
    # 检查历史最多保留条数与详情页展示条数
    _history_keep = 60
    _history_show = 10
    # 一次性任务标识
    _once_job_id = "once"
    # 请求超时（秒）：该接口实测 0.5 秒返回；两条通道最坏 2×15 秒，避免长时间占住调度线程
    _timeout = 15
    # 详情页「立即查询」按钮的冷却时间（秒），避免连点反复打接口
    _refresh_cooldown = 10.0
    # 活跃窗口落库的最小前移量（秒）：模型用量事件每轮都会到，逐次写库没必要
    _window_write_grace = 60.0
    # 未配置 AccessKey 时可回退读取的阿里云标准环境变量（便于不把密钥写进插件配置）
    _env_key_id = "ALIBABA_CLOUD_ACCESS_KEY_ID"
    _env_key_secret = "ALIBABA_CLOUD_ACCESS_KEY_SECRET"
    # 今日消耗速率的最小观察时长（小时）与最小消耗额，样本不足时不折算速率
    _burn_min_hours = 0.5
    _burn_min_amount = 0.01

    # 全部可配置项的唯一默认值来源：init_plugin 与 get_form 都从这里取，避免加字段时漏改
    _defaults: Dict[str, Any] = {
        "enabled": False,
        "onlyonce": False,
        "cron": "0 9,21 * * *",
        "use_proxy": True,
        "access_key_id": "",
        "access_key_secret": "",
        "endpoint": "business.aliyuncs.com",
        "watch_field": "AvailableAmount",
        # 进度条满格金额：余额等于该值时进度条画满，留空或 0 回退按预警线计
        "bar_full": 0,
        "warn_threshold": 100,
        "critical_threshold": 20,
        "drop_percent": 0,
        "notify_mode": "alert",
        "repeat_hours": 24,
        "adaptive": True,
        # 自适应频率：高频间隔（分钟）、活跃窗口（分钟）、日常间隔（小时）、消费兜底（元）
        "tick_minutes": 30,
        "active_minutes": 60,
        "idle_hours": 12,
        "spend_floor": 0.5,
    }
    _float_fields = ("warn_threshold", "critical_threshold", "drop_percent", "repeat_hours",
                     "tick_minutes", "active_minutes", "idle_hours", "spend_floor",
                     "bar_full")
    _bool_fields = ("enabled", "onlyonce", "use_proxy", "adaptive")
    _str_fields = ("cron", "access_key_id", "access_key_secret", "endpoint", "watch_field",
                   "notify_mode")

    # 配置属性：默认值一律取自 _defaults
    _enabled: bool = _defaults["enabled"]
    _cron: str = _defaults["cron"]
    _onlyonce: bool = _defaults["onlyonce"]
    _access_key_id: str = _defaults["access_key_id"]
    _access_key_secret: str = _defaults["access_key_secret"]
    _endpoint: str = _defaults["endpoint"]
    _watch_field: str = _defaults["watch_field"]
    _bar_full: float = _defaults["bar_full"]
    _warn_threshold: float = _defaults["warn_threshold"]
    _critical_threshold: float = _defaults["critical_threshold"]
    _drop_percent: float = _defaults["drop_percent"]
    _notify_mode: str = _defaults["notify_mode"]
    _repeat_hours: float = _defaults["repeat_hours"]
    _use_proxy: bool = _defaults["use_proxy"]
    _adaptive: bool = _defaults["adaptive"]
    _tick_minutes: float = _defaults["tick_minutes"]
    _active_minutes: float = _defaults["active_minutes"]
    _idle_hours: float = _defaults["idle_hours"]
    _spend_floor: float = _defaults["spend_floor"]
    # 凭据来源：配置或环境变量，用于详情页标注
    _credential_source: str = "未配置"
    # 高频活跃窗口到期时间与当日模型用量（插件侧视图，只存普通字典）
    _active_until: str = ""
    _llm_usage: Dict[str, Any] = {}
    # 已落库的窗口到期时间，用于「明显前移才写库」的节流判断
    _window_written: str = ""
    # 当日消耗基线（首次成功读数与时间），用于算今日消耗与速率
    _daily: Dict[str, Any] = {}
    # 最近一次结果与告警去重状态（插件侧视图，只存普通字典）
    _last_result: Dict[str, Any] = {}
    _last_alert: Dict[str, Any] = {}
    # 详情页按钮的并发保护，init_plugin 中按实例创建，不放在类属性上
    _refresh_lock = None

    # 状态判定结果对应的颜色与文案（Vuetify 颜色交给组件属性）
    _status_meta: Dict[str, Tuple[str, str]] = {
        "normal": ("success", "余额充足"),
        "warn": ("warning", "余额低于预警线"),
        "critical": ("error", "余额低于紧急线"),
        "unknown": ("info", "状态未知"),
        "error": ("error", "查询失败"),
    }

    # ---------- 详情页样式：只用已核对存在的静态类与内联样式，颜色一律交给组件属性 ----------
    _hero_style = "padding:22px 16px;text-align:center"
    _hero_row_style = "display:flex;align-items:baseline;justify-content:center;gap:6px;flex-wrap:wrap"
    _hero_num_style = "font-size:clamp(2.2rem,9vw,3.2rem);font-weight:700;line-height:1"
    _hero_unit_style = "font-size:1.05rem;font-weight:500;opacity:.85"
    _notice_title_style = "font-size:1.35rem;font-weight:600;line-height:1.3"
    _sub_text_style = "font-size:.95rem;font-weight:400;line-height:1.5;margin-top:10px;text-align:center"
    _foot_style = "font-size:.8rem;line-height:1.6;margin-top:14px;opacity:.85;text-align:center"
    _card_title_style = "font-size:1.02rem;font-weight:600;line-height:1.4"
    # 列表标题与副标题：覆盖 Vuetify 的单行截断，长文本按容器宽度自动换行
    _list_title_style = ("display:flex;align-items:center;gap:6px;flex-wrap:wrap;"
                         "overflow:visible;text-overflow:clip;-webkit-line-clamp:unset")
    _wrap_style = ("display:block;white-space:normal;overflow:visible;"
                   "text-overflow:clip;-webkit-line-clamp:unset")
    # 明细行：左字段名 + 右取值（justify-space 类在前端产物里不存在，必须内联）
    _row_style = ("display:flex;align-items:baseline;justify-content:space-between;"
                  "gap:12px;width:100%")
    _row_value_style = ("flex:0 1 auto;min-width:0;text-align:right;font-weight:500;"
                        "white-space:normal;overflow:visible;text-overflow:clip;"
                        "-webkit-line-clamp:unset")
    _hero_badge_style = "display:flex;justify-content:center;align-items:center;gap:8px;flex-wrap:wrap"

    def init_plugin(self, config: Dict[str, Any] = None) -> None:
        """根据插件配置初始化运行状态。"""
        # 停止现有任务
        self.stop_service()

        # 锁与节流状态按实例创建：类属性共享的锁会让分身插件互相阻塞
        if not isinstance(self._refresh_lock, type(threading.Lock())):
            self._refresh_lock = threading.Lock()

        # 读取配置
        if config:
            for key in self._bool_fields:
                setattr(self, f"_{key}", bool(config.get(key, self._defaults[key])))
            for key in self._str_fields:
                setattr(self, f"_{key}",
                        str(config.get(key) or self._defaults[key]).strip())
            for key in self._float_fields:
                setattr(self, f"_{key}",
                       self._to_float(config.get(key), float(self._defaults[key])))
            self._active_until = str(config.get("active_until") or "").strip()
            self._window_written = str(config.get("window_written") or "").strip()
            self._llm_usage = dict(config.get("llm_usage") or {})
            self._daily = dict(config.get("daily") or {})
            self._last_result = dict(config.get("last_result") or {})
            self._last_alert = dict(config.get("last_alert") or {})

        # 数值下限收敛：避免 0 或负数把节奏算成 0（高频间隔至少 1 分钟、窗口不短于间隔）
        self._tick_minutes = max(self._tick_minutes, 1.0)
        self._active_minutes = max(self._active_minutes, self._tick_minutes)
        self._idle_hours = max(self._idle_hours, 1.0)
        self._repeat_hours = max(self._repeat_hours, 0.0)
        self._spend_floor = max(self._spend_floor, 0.0)
        # 满格金额非正数按「未设置」处理，进度条口径回退到预警线
        self._bar_full = max(self._bar_full, 0.0)
        self._resolve_credentials()

        # 立即运行一次：该开关是一次性动作，无论是否启用都要复位，
        # 否则每次保存配置或热重载都会再触发一轮查询
        if self._onlyonce:
            self._onlyonce = False
            if self._enabled:
                logger.info("阿里云余额监控：立即运行一次")
                add_plugin_once_job(
                    plugin_id=self.__class__.__name__,
                    job_id=self._once_job_id,
                    func=self.run_check,
                    name="阿里云余额立即查询",
                    delay_seconds=3,
                    func_kwargs={"manual": True},
                )
            self._save_config()

    def _resolve_credentials(self) -> None:
        """确定本次使用的 AccessKey：配置优先，留空时回退阿里云标准环境变量。

        不想把密钥写进插件配置（明文存 DB）的用户，可以只在容器里设置
        ALIBABA_CLOUD_ACCESS_KEY_ID / ALIBABA_CLOUD_ACCESS_KEY_SECRET。
        只读取取值本身，不写日志也不回显。
        """
        if self._access_key_id and self._access_key_secret:
            self._credential_source = "插件配置"
            return
        env_id = str(os.environ.get(self._env_key_id) or "").strip()
        env_secret = str(os.environ.get(self._env_key_secret) or "").strip()
        if env_id and env_secret:
            self._access_key_id = self._access_key_id or env_id
            self._access_key_secret = self._access_key_secret or env_secret
            self._credential_source = "环境变量"
            return
        self._credential_source = "未配置"

    def get_state(self) -> bool:
        """获取插件启用状态。"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """返回插件远程命令列表：/aliyunbalance 立即查询一次余额。"""
        return [{
            "cmd": "/aliyunbalance",
            "event": EventType.PluginAction,
            "desc": "立即查询一次阿里云账户余额并推送读数",
            "category": "云服务",
            "data": {"action": "aliyunbalance_refresh"},
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        """返回插件 API 列表：供详情页「立即查询」按钮调用。"""
        return [{
            "path": "/refresh",
            "endpoint": self.refresh_now,
            "methods": ["GET"],
            "auth": "bear",
            "summary": "立即查询一次阿里云账户余额并返回读数",
        }]

    def get_service(self) -> List[Dict[str, Any]]:
        """注册插件定时服务。"""
        if not self._enabled:
            return []
        if self._adaptive:
            try:
                return [{
                    "id": self.__class__.__name__,
                    "name": "阿里云余额自适应检查服务",
                    "trigger": IntervalTrigger(
                        minutes=self._tick_minutes,
                        start_date=self._next_wakeup()),
                    "func": self.run_check,
                    "kwargs": {},
                }]
            except Exception as err:
                logger.error(f"阿里云余额监控自适应服务注册失败，退回固定周期：{str(err)}")
        cron = str(self._cron or "").strip()
        if cron.count(" ") != 4:
            logger.warning(f"阿里云余额监控：执行周期不是 5 位 cron 表达式，定时服务未注册：{cron}")
            return []
        try:
            return [{
                "id": self.__class__.__name__,
                "name": "阿里云余额监控服务",
                "trigger": CronTrigger.from_crontab(cron),
                "func": self.run_check,
                "kwargs": {},
            }]
        except Exception as err:
            logger.error(f"阿里云余额监控定时任务配置错误：{str(err)}")
        return []

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
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
                                        "label": "执行周期（关闭自适应时生效）",
                                        "placeholder": "5位cron表达式，默认每天9点和21点",
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "use_proxy", "label": "优先走系统代理"},
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
                                        "model": "access_key_id",
                                        "label": "AccessKey ID",
                                        "hint": "建议使用只授予 AliyunBSSReadOnlyAccess 的 RAM 用户",
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
                                        "model": "access_key_secret",
                                        "type": "password",
                                        "label": "AccessKey Secret",
                                        "hint": "只保存在本机插件配置里，不会出现在详情页",
                                        "persistent-hint": True,
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
                                    "component": "VSelect",
                                    "props": {
                                        "model": "endpoint",
                                        "label": "服务接入点",
                                        "items": [
                                            {"title": text, "value": key}
                                            for key, (_, _, text) in self._endpoints.items()
                                        ],
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VSelect",
                                    "props": {
                                        "model": "watch_field",
                                        "label": "监控口径",
                                        "items": [
                                            {"title": text, "value": key}
                                            for key, text in self._watch_fields.items()
                                        ],
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
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "warn_threshold",
                                        "type": "number",
                                        "label": "预警线（金额）",
                                        "hint": "余额不大于该值转黄色，0 为关闭",
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "critical_threshold",
                                        "type": "number",
                                        "label": "紧急线（金额）",
                                        "hint": "余额不大于该值转红色，0 为关闭",
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "drop_percent",
                                        "type": "number",
                                        "label": "骤降提醒（%）",
                                        "hint": "较上次下降达到该百分比时提醒，0 为关闭",
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "bar_full",
                                        "type": "number",
                                        "label": "进度条满格金额",
                                        "hint": "余额等于该金额时进度条画满，留空或 0 回退按预警线计",
                                        "persistent-hint": True,
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
                                    "component": "VSelect",
                                    "props": {
                                        "model": "notify_mode",
                                        "label": "通知策略",
                                        "items": [
                                            {"title": text, "value": key}
                                            for key, text in self._notify_modes.items()
                                        ],
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "repeat_hours",
                                        "type": "number",
                                        "label": "同档位重复提醒间隔（小时）",
                                        "hint": "持续处于同一告警档位时的最小提醒间隔，0 为每次都发",
                                        "persistent-hint": True,
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
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "adaptive",
                                        "label": "自适应频率",
                                        "hint": "检测到模型调用时自动转高频",
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "tick_minutes",
                                        "type": "number",
                                        "label": "高频间隔（分钟）",
                                        "hint": "活跃期多久查一次，默认 30",
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "active_minutes",
                                        "type": "number",
                                        "label": "活跃窗口（分钟）",
                                        "hint": "每次检测到调用后保持高频的时长",
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "idle_hours",
                                        "type": "number",
                                        "label": "日常间隔（小时）",
                                        "hint": "完全没活动时最长多久查一次",
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "spend_floor",
                                        "type": "number",
                                        "label": "消费兜底阈值（元）",
                                        "hint": "余额比上次下降达该值也转高频，0 为关闭",
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
                                    "text": "自适应模式下服务每「高频间隔」唤醒一次：检测到智能助手或插件调用大模型、"
                                            "或余额相比上次明显下降时才真正查询阿里云，其余时间直接跳过不打接口；"
                                            "关闭开关则改回按左侧「执行周期」的 cron 固定查询。"
                                            "注意百炼推理为分钟级出账（通常 2~10 分钟），读数下降本身有滞后。"
                                            "插件只调用官方费用接口 QueryAccountBalance，不做任何写操作；"
                                            "RAM 用户需授予 AliyunBSSReadOnlyAccess（或仅 bss:DescribeAcccount），"
                                            "国际站账号请把服务接入点改为 business.ap-southeast-1.aliyuncs.com。",
                                },
                            }],
                        }],
                    },
                ],
            }
        ], dict(self._defaults)

    def stop_service(self) -> None:
        """停止插件后台服务并释放资源。"""
        try:
            remove_plugin_once_job(self.__class__.__name__, self._once_job_id)
        except Exception as err:
            logger.debug(f"阿里云余额监控：清理一次性任务失败：{str(err)}")

    # ---------- 配置与通用工具 ----------

    def _save_config(self) -> None:
        """保存插件配置（含最近一次结果与告警去重状态）。"""
        self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "cron": self._cron,
            "use_proxy": self._use_proxy,
            "access_key_id": self._access_key_id,
            "access_key_secret": self._access_key_secret,
            "endpoint": self._endpoint,
            "watch_field": self._watch_field,
            "bar_full": self._bar_full,
            "warn_threshold": self._warn_threshold,
            "critical_threshold": self._critical_threshold,
            "drop_percent": self._drop_percent,
            "notify_mode": self._notify_mode,
            "repeat_hours": self._repeat_hours,
            "adaptive": self._adaptive,
            "tick_minutes": self._tick_minutes,
            "active_minutes": self._active_minutes,
            "idle_hours": self._idle_hours,
            "spend_floor": self._spend_floor,
            "active_until": self._active_until,
            "window_written": self._window_written,
            "daily": self._daily,
            "credential_source": self._credential_source,
            "llm_usage": self._llm_usage,
            "last_result": self._last_result,
            "last_alert": self._last_alert,
        })

    @staticmethod
    def _to_float(value: Any, default: float = 0.0) -> float:
        """把配置或接口返回的金额字符串安全转为浮点数，取不到时用默认值。"""
        try:
            if value is None or str(value).strip() == "":
                return default
            return float(str(value).replace(",", "").strip())
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _to_int(value: Any) -> int:
        """把事件或配置里的数量字段安全转为非负整数，非法值按 0 处理。"""
        try:
            result = int(float(str(value).strip()))
        except (TypeError, ValueError):
            return 0
        return result if result > 0 else 0

    @classmethod
    def _opt_float(cls, value: Any) -> Optional[float]:
        """转成浮点数，空值或非法值返回 None；绝不返回 NaN，避免写入配置的 JSON 非法。"""
        if value is None or str(value).strip() == "":
            return None
        try:
            result = float(str(value).replace(",", "").strip())
        except (TypeError, ValueError):
            return None
        if result != result or result in (float("inf"), float("-inf")):
            return None
        return result

    @staticmethod
    def _mask_key(value: str) -> str:
        """掩码 AccessKey ID，详情页只保留首尾各 4 位。"""
        text = str(value or "").strip()
        if not text:
            return "未配置"
        if len(text) <= 8:
            return f"{text[:2]}****"
        return f"{text[:4]}****{text[-4:]}"

    def _money(self, value: Any, currency: str = "") -> str:
        """把金额格式化为千分位两位小数并带上币种符号，取不到数值时返回占位符。"""
        amount = self._opt_float(value)
        if amount is None:
            text = str(value or "").strip()
            return text if text else "—"
        symbol = self._currencies.get(str(currency or "").upper(), "")
        return f"{symbol}{amount:,.2f}"

    def _now(self) -> datetime:
        """按系统时区取当前时间。"""
        try:
            return datetime.now(tz=pytz.timezone(settings.TZ))
        except Exception:
            return datetime.now()

    def _status_of(self, value: Optional[float]) -> str:
        """按紧急线与预警线判定余额状态：critical / warn / normal。"""
        if value is None:
            return "unknown"
        critical = self._critical_threshold
        warn = self._warn_threshold
        if critical > 0 and value <= critical:
            return "critical"
        if warn > 0 and value <= warn:
            return "warn"
        return "normal"

    def _watch_label(self) -> str:
        """监控口径的短名称。"""
        return "可用额度" if self._watch_field == "AvailableAmount" else "现金余额"

    def _endpoint_label(self) -> str:
        """服务接入点的显示名。"""
        return self._endpoints.get(self._endpoint, ("", "", "自定义接入点"))[2]

    def _threshold_hint(self, currency: str = "", include_full: bool = True) -> str:
        """阈值配置的可读说明；include_full=False 时不列「满格金额」，那是进度条口径不是告警依据。"""
        parts: List[str] = []
        if self._bar_full > 0 and include_full:
            parts.append(f"满格金额 {self._money(self._bar_full, currency)}")
        if self._warn_threshold > 0:
            parts.append(f"预警线 {self._money(self._warn_threshold, currency)}")
        if self._critical_threshold > 0:
            parts.append(f"紧急线 {self._money(self._critical_threshold, currency)}")
        if self._drop_percent > 0:
            parts.append(f"骤降 {self._drop_percent:g}%")
        return " · ".join(parts) if parts else "未设置阈值"

    def _bar_full_amount(self) -> float:
        """进度条满格金额：优先用「进度条满格金额」，未设置时回退预警线，两者都没有返回 0。"""
        if self._bar_full > 0:
            return self._bar_full
        return self._warn_threshold if self._warn_threshold > 0 else 0.0

    def _bar_ratio(self, value: Optional[float], status: str) -> float:
        """余额相对满格金额的百分比（0-100）：无读数或没有任何口径时按状态给满格或空条。"""
        full = self._bar_full_amount()
        if value is not None and full > 0:
            return max(min(value / full * 100, 100), 0)
        return 100 if status == "normal" else 0

    def _bar_caption(self, currency: str) -> str:
        """进度条下方的口径文案：走满格金额时带上该金额，回退预警线时保持原有写法。"""
        if self._bar_full > 0:
            return f"相对满格金额 {self._money(self._bar_full, currency)}"
        return "相对预警线"

    def _next_check_text(self) -> str:
        """推算下一次自动检查时间：自适应按间隔估算，固定周期按 cron 推算。"""
        if not self._enabled:
            return "—"
        if self._adaptive:
            # 唤醒节奏是固定的，但真正查询由闸门决定，这里给出「最迟什么时候会有新读数」
            next_time = self._now().replace(tzinfo=None) + timedelta(minutes=self._tick_minutes)
            moment = str((self._last_result or {}).get("checked_at") or "").strip()
            if self._window_minutes_left() <= 0 and moment:
                try:
                    idle_next = (datetime.strptime(moment[:19], "%Y-%m-%d %H:%M:%S")
                                 + timedelta(hours=self._idle_hours))
                    if idle_next > next_time:
                        next_time = idle_next
                except (TypeError, ValueError):
                    pass
            return next_time.strftime("%Y-%m-%d %H:%M")
        cron = str(self._cron or "").strip()
        if cron.count(" ") != 4:
            return "—"
        try:
            tz = pytz.timezone(settings.TZ)
            next_time = CronTrigger.from_crontab(
                cron, timezone=tz).get_next_fire_time(None, self._now())
            return next_time.strftime("%Y-%m-%d %H:%M") if next_time else "—"
        except Exception as err:
            logger.warning(f"阿里云余额监控：推算下次检查时间失败：{str(err)}")
            return "—"

    # ---------- 自适应频率：活跃窗口、用量统计与事件监听 ----------

    @staticmethod
    def _parse_local(text: Any) -> Optional[datetime]:
        """解析不带时区的本地墙钟串，为空或非法时返回 None。"""
        value = str(text or "").strip()
        if not value:
            return None
        try:
            return datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            return None

    def _window_minutes_left(self) -> float:
        """返回高频活跃窗口剩余分钟数，不在窗口内返回 0。"""
        until = self._parse_local(self._active_until)
        if until is None:
            return 0.0
        # 窗口到期时间存的是本地墙钟串，比较前去掉时区，避免 aware/naive 相减异常
        left = (until - self._now().replace(tzinfo=None)).total_seconds() / 60
        return left if left > 0 else 0.0

    def _touch_window(self, reason: str, persist: bool = True) -> None:
        """把活跃窗口顺延一个窗口时长，转入高频检查。

        内存里的到期时间每次都更新；落库只在明显前移（大于写库阈值）时做一次，
        避免高频对话时每轮模型用量事件都触发一次数据库写。
        persist=False 给链式事件用：宿主虽然把插件处理器丢进受控线程池
        （EventHandlerBinding.run_sync_in_threadpool=True），但链是同步等待的——
        _resolve_llm_runtime_config 要 await 完所有处理器才继续建图，所以在这里落库
        等于让每轮 Agent 启动多等一次数据库写。改成只更新内存时间戳、不动「已落库」
        标记，等轮末用量事件（广播，不必等）或下一次真实检查时一起持久化，窗口长度本身不受影响。
        """
        if not self._adaptive:
            return
        minutes = max(self._active_minutes, self._tick_minutes)
        until = self._now().replace(tzinfo=None) + timedelta(minutes=minutes)
        text = until.strftime("%Y-%m-%d %H:%M:%S")
        self._active_until = text
        if not persist:
            logger.info(f"阿里云余额监控：转入高频（{reason}），窗口至 {self._short_time(text)}"
                        f"（仅内存，落库交给轮末用量事件）")
            return
        stored = self._parse_local(self._window_written)
        # 只在到期时间明显前移（大于写库阈值）时才落库，比较的是「新值 - 已落库值」
        if stored is None or (until - stored).total_seconds() > self._window_write_grace:
            self._window_written = text
            self._save_config()
            logger.info(f"阿里云余额监控：转入高频（{reason}），窗口至 {self._short_time(text)}")
        else:
            logger.debug(f"阿里云余额监控：活跃窗口续期（{reason}）至 {self._short_time(text)}")

    def _next_wakeup(self) -> datetime:
        """算出服务下次唤醒时间：以「上次检查 + 高频间隔」为锚。

        插件每次重载都会重新注册服务，若固定用「现在 + 30 秒」，重载风暴会把唤醒
        一直提前、把既有节奏推后；锚到上次检查点后，重载不再改变下一次查询的时间。
        """
        now_local = self._now().replace(tzinfo=None)
        due = now_local + timedelta(seconds=30)
        last_time = self._parse_local((self._last_result or {}).get("checked_at"))
        if last_time is not None:
            due = max(due, last_time + timedelta(minutes=self._tick_minutes))
        try:
            return pytz.timezone(settings.TZ).localize(due)
        except Exception:
            return self._now() + timedelta(seconds=30)

    def _update_daily_baseline(self, value: Optional[float], currency: str) -> None:
        """记录当日首次成功读数作为消耗基线，跨天自动重开。"""
        if value is None:
            return
        today = self._now().strftime("%Y-%m-%d")
        daily = dict(self._daily or {})
        if str(daily.get("date") or "") != today:
            self._daily = {"date": today, "value": value, "currency": currency,
                           "at": self._now().strftime("%Y-%m-%d %H:%M:%S")}
            return
        # 当日基线只记第一次成功读数：若每次检查都刷新，消耗就永远是 0
        daily["currency"] = currency or daily.get("currency")
        self._daily = daily

    def _burn_metrics(self, value: Optional[float]) -> Tuple[str, str]:
        """返回（今日消耗, 消耗速率）两段短文案：当日样本不足时给占位符。

        预计续航已按要求移除：它靠「当日首次读数 + 线性速率」外推，而账户里同时有充值、
        信控扣款与包年包月出账，读数本就不连续，算出来的小时数容易误导。
        """
        daily = dict(self._daily or {})
        base = self._opt_float(daily.get("value"))
        currency = str(daily.get("currency") or "")
        symbol = self._currencies.get(currency.upper(), "")
        if base is None or value is None \
                or str(daily.get("date") or "") != self._now().strftime("%Y-%m-%d"):
            return "—", "—"
        spent = max(base - value, 0.0)
        hours = self._seconds_since(daily.get("at")) / 3600
        spent_text = f"{symbol}{spent:,.2f}"
        if hours < self._burn_min_hours:
            return spent_text, "统计中"
        if spent < self._burn_min_amount:
            return spent_text, f"{symbol}0.00/小时"
        return spent_text, f"{symbol}{spent / hours:,.2f}/小时"

    def _burn_text(self, value: Optional[float]) -> Tuple[str, str]:
        """详情页「今日消耗」的主值与补充说明：只讲消耗与速率。"""
        spent, rate = self._burn_metrics(value)
        if spent == "—":
            return "样本不足", "需要当日至少两次成功读数"
        return f"已消耗 {spent}", f"速率 {rate}"

    # ---------- 首页仪表盘卡片：独立紧凑版式 ----------
    # 卡片与详情页刻意不同：详情页是居中的大号状态面板，卡片是左对齐的读数清单，
    # 读数下方用一行「今日消耗 + 速率」作依据。标题仍交给宿主 attrs 渲染，不自绘标题行
    # （自绘会和宿主标题重复，实测出现过两个「阿里云余额」）。宿主头部只把
    # title/subtitle 当纯文本渲染（前端编译产物里是 createTextVNode，无 v-html），
    # 图标塞不进宿主副标题，因此卡片顶部改由元素自绘一行居中的宽版标识；不放
    # 「可用额度」文字，监控口径由金额行与阈值行自身表达。
    _dash_num_style = "font-size:clamp(1.9rem,7.5vw,2.5rem);font-weight:700;line-height:1.05"
    _dash_unit_style = "font-size:.95rem;font-weight:500;opacity:.8"
    _dash_row_style = "display:flex;align-items:center;gap:8px;flex-wrap:wrap"
    _dash_caption_style = "font-size:.78rem;line-height:1.5;margin-top:4px"
    _dash_value_style = "font-size:1.15rem;font-weight:700;line-height:1.2"
    # 宽版阿里云标识（长方形 logotype）：插件目录内 PNG，运行时转 data URI
    _wide_logo_file = "aliyunbalance-wide.png"
    _wide_logo_cache: str = ""

    @classmethod
    def _wide_logo_src(cls) -> str:
        """取宽版标识 data URI；读不到文件返回空串，副标题行退化为纯文字。

        用 data URI 而不是外链：宿主 /plugin/file 静态路由要求 resource token（浏览器
        直取 401），外链图片又要过图片代理的域名白名单（alicdn 不在名单内）。内联
        既不依赖网络也不依赖鉴权，实测可正常渲染。
        """
        if cls._wide_logo_cache:
            return cls._wide_logo_cache
        png = os.path.join(os.path.dirname(os.path.abspath(__file__)), cls._wide_logo_file)
        try:
            with open(png, "rb") as fp:
                raw = fp.read()
            cls._wide_logo_cache = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
        except OSError as err:
            logger.warning(f"阿里云余额监控：读取宽版标识失败，副标题行不显示图片：{str(err)}")
            cls._wide_logo_cache = ""
        return cls._wide_logo_cache

    def _logo_row(self) -> Optional[dict]:
        """卡片顶部标识行：宽版阿里云标识居中、点击在新标签页打开本插件；读不到图时返回 None 整行省略。

        attrs 不传 subtitle（宿主副标题是纯文本节点放不下图），监控口径由金额行与
        阈值行自身表达，顶部只留居中的品牌标识，不放「可用额度」四个字。
        不要用负上边距把标识抬进宿主头部：elements 所在容器按上边缘裁切溢出，
        实测负 margin 会把标识顶部切掉。图片保留 logo 四周完整的橙色边距
        （604x166 裁切，用户确认橙边是标识设计的一部分），显示 120x33。

        点击为什么只能走深链而不是事件：仪表盘的渲染器 DashboardRender 只把 props
        透传给 Vuetify 组件、没有 v-on，详情页那套节点级 events 只在 PageRender 里生效，
        卡片元素点不出弹窗；宿主弹窗入口 openSharedDialog 在压缩产物里已被改名
        （/public/assets 全文 0 命中），注入脚本也接不上，所以原地弹窗只能改宿主前端。
        宿主插件页自带深链 #/plugins?id=<实例ID>：PluginCardListView.revealRoutePlugin()
        切到「已安装」、清掉文件夹与筛选、定位卡片并置 page_open=true，PluginCard 监听
        page_open 后按 has_page 弹出详情，就是「点图标打开插件」。
        用 target=_blank 的新标签而不是同页跳转：同页跳会把仪表盘换掉（用户明确要求
        不落到插件页）；新标签是冷启动，revealRoutePlugin 走 onMounted 那条路径，不依赖
        keep-alive 激活与 route.query 监听器的先后，自动弹窗必然触发。
        只给 href 不给 to：给了 to，Vuetify 会渲染 RouterLink（同页、忽略 target）；
        只给 href 才落回原生 <a target="_blank">，宿主是 hash 路由，改哈希即换页。
        外壳用 VBtn 而不是嵌套 VCard：dashboard 页对所有 .v-card 统一加了边框与阴影，
        卡里再套卡会多出可见边框。variant 用 text 不用 plain：plain 自带 opacity .4，
        会把标识整体压暗。
        """
        logo = self._wide_logo_src()
        if not logo:
            return None
        target = f"#/plugins?id={self.__class__.__name__}"
        return {"component": "div",
                "props": {"style": "display:flex;justify-content:center"},
                "content": [{
                    "component": "VBtn",
                    "props": {"href": target, "target": "_blank", "rel": "noopener",
                              "variant": "text", "class": "pa-0",
                              "title": "在新标签页打开插件详情",
                              "style": "min-width:0;min-height:0;height:auto;"
                                       "line-height:normal;border-radius:6px;"
                                       "text-decoration:none"},
                    "content": [{"component": "VImg",
                                 "props": {"src": logo, "width": 120, "height": 33,
                                           "contain": True, "rounded": 6, "alt": "阿里云"}}],
                }]}

    def _dash_caption(self, text: str, cls: str = "text-medium-emphasis") -> dict:
        """卡片里的一行小字说明。"""
        return {"component": "div",
                "props": {"class": f"text-caption {cls}", "style": self._dash_caption_style},
                "text": str(text)}

    def _rel_time(self, moment: Any) -> str:
        """相对时间描述，卡片上比绝对时间戳更有信息量。"""
        seconds = self._seconds_since(moment)
        if seconds >= 1e8:
            return "暂无"
        if seconds < 60:
            return "刚刚"
        if seconds < 3600:
            return f"{int(seconds // 60)} 分钟前"
        if seconds < 86400:
            return f"{seconds / 3600:.1f} 小时前"
        return f"{seconds / 86400:.1f} 天前"

    def _freq_short(self) -> str:
        """频率短文案：高频时带上窗口剩余分钟。"""
        if not self._adaptive:
            return "固定周期"
        left = int(self._window_minutes_left())
        if left > 0:
            return f"高频 {self._tick_minutes:g} 分钟（剩 {left} 分）"
        return f"日常 {self._idle_hours:g} 小时"

    def get_dashboard_meta(self) -> Optional[List[Dict[str, str]]]:
        """声明首页仪表盘卡片。"""
        if not self._enabled:
            return None
        return [{"key": "aliyunbalance", "name": "阿里云余额"}]

    def get_dashboard(self, key: str = "", **kwargs: Any) -> Optional[
            Tuple[Dict[str, Any], Dict[str, Any], Optional[List[dict]]]]:
        """首页仪表盘卡片：左对齐读数清单，金额最重、消耗与速率作依据。

        版式与详情页面板刻意区分：详情页是居中状态面板，卡片按「顶部标识行（居中长
        方形标识）→ 金额+状态 → 进度条（相对满格金额）→ 今日消耗与速率
        → 节奏」的顺序排，窄屏单列也不会出现空洞或错位。attrs 不传 subtitle：宿主
        副标题是纯文本节点放不下图，顶部标识行由卡片元素自绘，缺图时整行省略。
        """
        if key != "aliyunbalance" or not self._enabled:
            return None
        record = dict(self._last_result or {})
        cols = {"cols": 12, "md": 6, "lg": 4}
        if not record:
            elements = [self._notice_block("info", "尚未查询过",
                                           "在插件详情页点「立即查询」，或等自动检查出结果。")]
            logo_row = self._logo_row()
            if logo_row:
                elements.insert(0, logo_row)
            return (cols, {"title": "阿里云余额", "border": True}, elements)
        status = str(record.get("status") or "unknown")
        color, label = self._status_meta.get(status, self._status_meta["unknown"])
        currency = str(record.get("currency") or "")
        symbol = self._currencies.get(currency.upper(), "")
        value = self._opt_float(record.get("watch_value"))
        warn = self._warn_threshold
        ratio = self._bar_ratio(value, status)
        spent, rate = self._burn_metrics(value)
        # 进度条颜色只反映「最后已知读数」的档位：查询失败时不能用 error 色去涂一根
        # 满格条，那和 100% 自相矛盾；失败信号已经由状态徽章和错误行表达了
        bar_status = status if status != "error" else self._status_of(value)
        bar_color = self._status_meta.get(bar_status, self._status_meta["unknown"])[0]
        if value is None:
            bar_color = color
        chips: List[dict] = [self._chip(label, color, "flat")]
        if status == "error":
            chips.append(self._chip("数据滞后" if value is not None else "无读数",
                                    "grey-lighten-3", "tonal"))
        # 阈值口径在卡片里只取预警/紧急两项，骤降留给详情页，避免小屏折行
        limits = " · ".join(item for item in (
            f"预警线 {self._money(warn, currency)}" if warn > 0 else "",
            f"紧急线 {self._money(self._critical_threshold, currency)}"
            if self._critical_threshold > 0 else "",
        ) if item) or "未设置阈值"
        elements: List[dict] = [
            {
                "component": "div",
                "props": {"class": "d-flex align-center mt-2",
                          "style": f"{self._dash_row_style};justify-content:center"},
                "content": [
                    {"component": "div",
                     "props": {"style": "display:flex;align-items:baseline;gap:3px"},
                     "content": [
                         {"component": "span", "props": {"style": self._dash_unit_style},
                          "text": symbol},
                         {"component": "span", "props": {"style": self._dash_num_style},
                          "text": "—" if value is None else f"{value:,.2f}"},
                         {"component": "span",
                          "props": {"class": "text-medium-emphasis",
                                    "style": f"{self._dash_unit_style};margin-left:3px"},
                          "text": currency},
                     ]},
                    *chips,
                ],
            },
            {"component": "VProgressLinear",
             "props": {"model-value": round(ratio, 1), "color": bar_color,
                       "height": 8, "rounded": True, "class": "mt-3"}},
            self._dash_caption(f"{self._bar_caption(currency)} {ratio:.0f}% · {limits}"),
            {
                "component": "div",
                "props": {"class": "d-flex align-center",
                          "style": f"{self._dash_row_style};margin-top:14px"},
                "content": [
                    {"component": "span",
                     "props": {"class": "text-body-2 text-medium-emphasis"},
                     "text": "今日消耗"},
                    {"component": "span", "props": {"style": self._dash_value_style},
                     "text": spent},
                    {"component": "span",
                     "props": {"class": "text-caption text-medium-emphasis"},
                     "text": f"速率 {rate}" if rate != "—" else "样本不足"},
                ],
            },
        ]
        if status == "error":
            elements.append(self._dash_caption(
                f"最近一次查询失败：{str(record.get('error') or '未知原因')[:90]}",
                "text-error"))
        elements.append({"component": "VDivider", "props": {"class": "mt-3 mb-1"}})
        elements.append(self._dash_caption(
            f"{self._freq_short()} · {self._rel_time(record.get('checked_at'))}检查"))
        logo_row = self._logo_row()
        if logo_row:
            elements.insert(0, logo_row)
        return (cols, {"title": "阿里云余额", "border": True},
                elements)

    def _bump_usage(self, calls: int, tokens: int, model: str, source: str) -> None:
        """累计当天的模型调用次数与 token 用量，跨天自动归零。"""
        today = self._now().strftime("%Y-%m-%d")
        usage = dict(self._llm_usage or {})
        if str(usage.get("date") or "") != today:
            usage = {"date": today, "calls": 0, "tokens": 0}
        usage["calls"] = self._to_int(usage.get("calls")) + calls
        usage["tokens"] = self._to_int(usage.get("tokens")) + tokens
        if model:
            models = dict(usage.get("models") or {})
            models[model] = self._to_int(models.get(model)) + calls
            usage["models"] = models
        if source:
            usage["source"] = source
        usage["last_at"] = self._now().strftime("%Y-%m-%d %H:%M:%S")
        self._llm_usage = usage

    def _freq_text(self) -> str:
        """当前检查频率的可读文案。"""
        if not self._adaptive:
            return f"固定周期 {self._cron}"
        left = self._window_minutes_left()
        if left > 0:
            return f"高频 每 {self._tick_minutes:g} 分钟（窗口剩 {int(left)} 分钟）"
        return f"日常 最长 {self._idle_hours:g} 小时一查"

    def _usage_text(self) -> Tuple[str, str]:
        """当日模型用量的主值与补充说明，用于详情页概况格。"""
        usage = dict(self._llm_usage or {})
        if str(usage.get("date") or "") != self._now().strftime("%Y-%m-%d"):
            return "今日暂无调用", "有调用后自动转高频检查"
        models = "、".join(sorted(dict(usage.get("models") or {}).keys()))
        hint = " · ".join(item for item in (
            models,
            f"来源 {usage.get('source')}" if usage.get("source") else "",
            f"最近 {self._short_time(usage.get('last_at'))}" if usage.get("last_at") else "",
        ) if item)
        return (f"{self._to_int(usage.get('calls')):,} 次 / "
                f"{self._to_int(usage.get('tokens')):,} tokens",
                hint or "统计口径为宿主广播的模型用量事件")

    def _should_query_now(self) -> Tuple[bool, str]:
        """自适应闸门：判断本次唤醒是否真的需要调用阿里云接口。

        高频节奏以「距上次检查的时间」为准，而不是「唤醒了几次」：插件热重载会按
        start_date 重新提前唤醒一轮，只看活跃窗口的话，一次重载风暴就能把每 30 分钟
        变成一分钟多次，因此窗口内也必须攒够一个高频间隔才放行。
        """
        if not self._adaptive:
            return True, "固定周期"
        moment = str((self._last_result or {}).get("checked_at") or "").strip()
        if not moment:
            return True, "尚无检查记录"
        elapsed = self._seconds_since(moment)
        left = self._window_minutes_left()
        if left > 0:
            spacing = self._tick_minutes * 60 * 0.9
            if elapsed >= spacing:
                return True, f"活跃窗口内（剩 {int(left)} 分钟）"
            return False, (f"活跃窗口内，但距上次检查仅 {int(elapsed / 60)} 分钟，"
                           f"未到高频间隔 {self._tick_minutes:g} 分钟")
        if elapsed >= self._idle_hours * 3600:
            return True, f"距上次检查 {elapsed / 3600:.1f} 小时，已到日常间隔"
        return False, f"无模型调用且距上次检查仅 {elapsed / 3600:.1f} 小时"

    @eventmanager.register(ChainEventType.AgentLLMProvider)
    def on_agent_llm_provider(self, event: Event) -> None:
        """轮初信号：Agent 消耗 token 前先解析 LLM 供应商，命中即提前开启高频窗口。

        为什么不能只等 AgentTokensUsage：用量事件在编排层执行函数的 finally 里广播，
        载荷是整轮聚合值，一轮最多可跑 LLM_MAX_ITERATIONS（默认 512）步，超长轮次期间
        插件收不到任何信号，最坏要等满「日常间隔」才有下一次读数（读数和告警都只在
        读数时刻判定）。供应商解析在 _create_agent 开头调用，而每轮 _execute_agent 都会
        先把 _llm_runtime_config 置空，所以这条链事件每轮必然重发一次，且早于任何消耗。

        链式事件的约束：宿主按优先级把同一个 event 依次交给各处理器，处理器是就地修改
        event.event_data、返回值不参与链。所以本处理器只读不改、也不返回 event_data，
        免得影响供应商配置解析结果；载荷含 api_key 等敏感字段，只看有没有事件，绝不读取
        或记录任何取值。处理器异常由宿主 invoke_sync 兜住并只记日志，不会打断模型初始化。
        已知代价：那类「开始了但没真正调用模型就失败」的轮次也会开窗，最多多打一次只读
        接口，比漏掉整段长任务划算。
        """
        if not self._enabled or not self._adaptive:
            return
        if getattr(event, "event_data", None) is None:
            return
        self._touch_window("本轮 Agent 开始解析 LLM 供应商（轮初信号，早于消耗）",
                           persist=False)

    @eventmanager.register(EventType.AgentTokensUsage)
    def on_agent_tokens_usage(self, event: Event) -> None:
        """监听智能助手与插件广播的模型用量事件：命中即转入高频检查并累计当日用量。"""
        if not self._enabled or not self._adaptive:
            return
        data = getattr(event, "event_data", None)
        if data is None:
            return
        try:
            calls = self._to_int(getattr(data, "model_call_count", 0))
            tokens = self._to_int(getattr(data, "total_tokens", 0))
            model = str(getattr(data, "model", "") or "")
            source = str(getattr(data, "source", "") or "")
        except Exception as err:
            logger.debug(f"阿里云余额监控：解析模型用量事件失败：{str(err)}")
            return
        if calls <= 0 and tokens <= 0:
            # 没有实际用量（例如执行前就失败）不算活跃，避免无谓的高频查询
            return
        self._bump_usage(calls, tokens, model, source)
        self._touch_window(f"检测到模型调用 {calls} 次 / {tokens:,} tokens")

    @eventmanager.register(EventType.PluginAction)
    def on_plugin_action(self, event: Event) -> None:
        """处理 /aliyunbalance 斜杠命令：立即查询一次并把读数推到通知渠道。

        命令通过宿主 PluginAction 事件下发，data.action 用于区分本插件的动作；
        这里直接查一次（单请求 15 秒超时），不额外投递定时器，避免与定时服务重复。
        """
        if not self._enabled:
            return
        data = getattr(event, "event_data", None)
        if not isinstance(data, dict) or data.get("action") != "aliyunbalance_refresh":
            return
        logger.info("阿里云余额监控：收到 /aliyunbalance 命令，立即查询一次")
        self._do_check(manual=True)

    # ---------- 阿里云 OpenAPI 签名与请求 ----------

    @staticmethod
    def _percent_encode(value: Any) -> str:
        """按阿里云 RPC 签名规则做 RFC3986 百分号编码（空格为 %20，~ 与 * 按要求处理）。"""
        return quote("" if value is None else str(value), safe="")

    @classmethod
    def _canonical_query(cls, params: Dict[str, str]) -> str:
        """按参数名字典序拼出规范化请求串（名称与值分别百分号编码，不含 Signature 本身）。"""
        return "&".join(f"{cls._percent_encode(k)}={cls._percent_encode(params[k])}"
                        for k in sorted(params))

    @classmethod
    def _signature(cls, secret: str, params: Dict[str, str], method: str = "GET") -> str:
        """按阿里云 RPC 签名机制（HMAC-SHA1，SignatureVersion 1.0）计算签名值。"""
        canonical = cls._canonical_query(params)
        string_to_sign = (f"{method}&{cls._percent_encode('/')}"
                          f"&{cls._percent_encode(canonical)}")
        digest = hmac.new(f"{secret}&".encode("utf-8"),
                          string_to_sign.encode("utf-8"),
                          hashlib.sha1).digest()
        return base64.b64encode(digest).decode("utf-8")

    @classmethod
    def _signed_query(cls, secret: str, params: Dict[str, str]) -> str:
        """生成带 Signature 参数的完整查询串，签名值与其它参数同样做百分号编码。"""
        signature = cls._signature(secret, params)
        return f"{cls._canonical_query(params)}&Signature={cls._percent_encode(signature)}"

    def _signed_url(self, endpoint: str, region_id: str) -> str:
        """构造带签名的 QueryAccountBalance 请求地址。"""
        params: Dict[str, str] = {
            "Action": self._api_action,
            "Version": self._api_version,
            "Format": "JSON",
            "AccessKeyId": self._access_key_id,
            "SignatureMethod": "HMAC-SHA1",
            "SignatureVersion": "1.0",
            "SignatureNonce": uuid.uuid4().hex,
            "Timestamp": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "RegionId": region_id,
        }
        return f"https://{endpoint}/?{self._signed_query(self._access_key_secret, params)}"

    def _request_balance(
            self, url: str, proxies: Optional[Dict[str, Any]]
    ) -> Tuple[bool, Dict[str, Any], str]:
        """发起一次余额查询，返回（服务端是否已应答, 报文, 错误说明）。

        已应答但状态码非 200 时仍带回报文，调用方不应再换通道重试；
        注意 requests 的 Response 真值等价于 status_code < 400，判空必须用 is None。
        """
        try:
            res = RequestUtils(
                headers={"Accept": "application/json"},
                proxies=proxies,
                timeout=self._timeout,
                verify=True,
            ).get_res(url, raise_exception=True)
        except Exception as err:
            return False, {}, f"请求失败：{type(err).__name__}: {str(err)[:160]}"
        if res is None:
            return False, {}, "请求失败：未获取到响应"
        try:
            payload = res.json()
        except ValueError:
            return True, {}, f"响应不是合法 JSON（HTTP {res.status_code}）：{str(res.text)[:160]}"
        if not isinstance(payload, dict):
            return True, {}, f"响应结构异常（HTTP {res.status_code}）"
        if res.status_code != 200:
            code = str(payload.get("Code") or res.status_code)
            message = str(payload.get("Message") or "")
            return True, payload, f"HTTP {res.status_code}：{code} {message}".strip()
        return True, payload, ""

    # 常见错误码到可读排查建议的映射（按前缀匹配，越靠前优先级越高）
    _error_hints: Dict[str, str] = {
        "InvalidAccessKeyId.NotFound": "AccessKey ID 不存在或已被禁用，请到 RAM 控制台核对",
        "InvalidAccessKeyId": "AccessKey 无效，请重新填写",
        "SignatureDoesNotMatch": "签名不匹配，一般是 AccessKey Secret 填错或多了空格",
        "IncompleteSignature": "签名不完整，请检查 AccessKey Secret 是否被截断",
        "NoPermission": "RAM 用户缺少费用中心只读权限，请授予 AliyunBSSReadOnlyAccess",
        "NotAuthorized": "RAM 用户缺少费用中心只读权限，请授予 AliyunBSSReadOnlyAccess",
        "AuthSiteFail": "站点鉴权失败，多为账号站点与接入点不匹配（国际站需换接入点）",
        "NotApplicable": "该账号不适用此接口，常见于资源目录成员账号或虚商账号",
        "Throttling": "触发接口流控，请降低执行频率或稍后再试",
        "MissingParameter": "缺少必填参数，多为服务器时间与阿里云偏差过大，请校准容器时钟",
        "InvalidParameter.Timestamp": "请求时间戳非法，多为服务器时间与阿里云偏差过大，请校准容器时钟",
        "InternalError": "阿里云服务临时异常，稍后重试即可",
    }

    @classmethod
    def _friendly_error(cls, text: str, payload: Optional[Dict[str, Any]] = None) -> str:
        """给阿里云错误码补一句可执行的排查建议，避免首次配置时只看到裸错误码。"""
        code = str((payload or {}).get("Code") or "").strip()
        for prefix, hint in cls._error_hints.items():
            if code.startswith(prefix) or prefix in str(text):
                return f"{text}（{hint}）"
        return text

    def _query_balance(self) -> Tuple[bool, Dict[str, Any], str]:
        """调用 QueryAccountBalance，传输层失败时按「代理→直连」顺序自动兜底重试。"""
        endpoint, region_id, _ = self._endpoints.get(
            self._endpoint, (self._endpoint, "cn-hangzhou", ""))
        if not endpoint:
            return False, {}, "未配置服务接入点"
        attempts: List[Tuple[str, Optional[Dict[str, Any]]]] = []
        if self._use_proxy and settings.PROXY:
            attempts.append(("系统代理", settings.PROXY))
        attempts.append(("直连", None))

        errors: List[str] = []
        for label, proxies in attempts:
            answered, payload, error = self._request_balance(
                self._signed_url(endpoint, region_id), proxies)
            if answered:
                # 服务端已应答，换网络通道没有意义，直接把业务错误返回给使用者
                if error:
                    return False, payload, self._friendly_error(error, payload)
                # 业务层判定：Code 为 200 或 Success 为真才算成功
                code = str(payload.get("Code") or "").strip()
                success = payload.get("Success")
                if code and code != "200":
                    return False, payload, self._friendly_error(
                        f"{code}：{payload.get('Message') or '接口返回失败'}", payload)
                if success is False:
                    return False, payload, self._friendly_error(
                        str(payload.get("Message") or "接口返回失败"), payload)
                if not isinstance(payload.get("Data"), dict):
                    return False, payload, "接口未返回 Data 字段"
                logger.debug(f"阿里云余额监控：{label}通道查询成功")
                return True, payload, ""
            errors.append(f"[{label}] {error}")
            logger.warning(f"阿里云余额监控：{label}查询失败：{error}")
        return False, {}, "；".join(errors) if errors else "查询失败"

    # ---------- 检查主流程 ----------

    def run_check(self, manual: bool = False) -> None:
        """执行一次余额检查：自适应模式下先由闸门判断是否真的调用阿里云接口。"""
        try:
            if not manual:
                allow, reason = self._should_query_now()
                if not allow:
                    logger.debug(f"阿里云余额监控：跳过本次唤醒（{reason}）")
                    return
                logger.debug(f"阿里云余额监控：本次唤醒执行查询（{reason}）")
            self._do_check(manual=manual)
        except Exception as err:
            logger.error(f"阿里云余额监控执行异常：{str(err)}")
            self._record_failure(f"插件执行异常：{str(err)[:200]}")
            self._notify(self._last_result, force=True)

    def _append_history(self, record: Dict[str, Any]) -> None:
        """把一条读数（含失败）写入检查历史，超出保留条数的旧记录自动丢弃。"""
        history = self.get_data("balance_history") or []
        if not isinstance(history, list):
            history = []
        history.insert(0, dict(record))
        self.save_data("balance_history", history[:self._history_keep])

    def _record_failure(self, reason: str, request_id: str = "") -> None:
        """记录一次失败：保留最后一次成功读数，并把中断写进历史。

        早期实现把 _last_result 整体替换成只含错误信息的字典，结果是详情页主卡变成
        「—」、下一次成功查询取不到 prev_watch，环比与骤降判定会跨故障期失明。
        """
        previous = dict(self._last_result or {})
        keep: Dict[str, Any] = {}
        if previous.get("status") != "error" and previous.get("watch_value") is not None:
            # 上一次是成功读数：把它作为「最后已知读数」带过来，并记录它的时间
            for field in ("watch_value", "available", "cash", "credit", "quota", "currency"):
                if previous.get(field) is not None:
                    keep[field] = previous.get(field)
            keep["good_at"] = previous.get("checked_at")
        else:
            # 连续失败：继续沿用更早那次成功读数与时间，别把它冲掉
            for field in ("watch_value", "available", "cash", "credit", "quota", "currency",
                          "good_at"):
                if previous.get(field) is not None:
                    keep[field] = previous.get(field)
        record: Dict[str, Any] = dict(keep)
        record.update({
            "checked_at": self._now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": "error",
            "error": reason,
            "watch_field": self._watch_field,
            "stale": keep.get("watch_value") is not None,
            "delta": None,
            "delta_percent": None,
            "drop_alert": False,
            "request_id": request_id,
        })
        self._last_result = record
        self._save_config()
        self._append_history(record)

    def _do_check(self, manual: bool = False, notify: bool = True) -> None:
        """执行余额查询主流程并按策略推送通知；notify=False 时只更新读数（详情页按钮用）。"""
        if not self._access_key_id or not self._access_key_secret:
            logger.warning("阿里云余额监控：未配置 AccessKey，跳过本次检查")
            self._record_failure("未配置 AccessKey ID / AccessKey Secret")
            if notify:
                self._notify(self._last_result, force=True)
            return

        logger.info(f"开始查询阿里云账户余额：{self._endpoint}")
        ok, payload, error = self._query_balance()
        checked_at = self._now().strftime("%Y-%m-%d %H:%M:%S")

        if not ok:
            self._record_failure(error, str(payload.get("RequestId") or ""))
            if notify:
                self._notify(self._last_result, force=True)
            return

        data = payload.get("Data") or {}
        currency = str(data.get("Currency") or "").strip()
        available = self._opt_float(data.get("AvailableAmount"))
        cash = self._opt_float(data.get("AvailableCashAmount"))
        watch_value = cash if self._watch_field == "AvailableCashAmount" else available
        if watch_value is None:
            # 监控口径取不到数值时退回可用额度
            watch_value = available

        previous = dict(self._last_result or {})
        prev_watch = self._opt_float(previous.get("watch_value"))
        delta = None
        delta_percent = None
        if prev_watch is not None and watch_value is not None:
            delta = watch_value - prev_watch
            if prev_watch > 0:
                delta_percent = delta / prev_watch * 100

        status = self._status_of(watch_value)
        drop_alert = (self._drop_percent > 0 and delta_percent is not None
                      and delta_percent <= -self._drop_percent)

        record: Dict[str, Any] = {
            "checked_at": checked_at,
            "status": status,
            "watch_field": self._watch_field,
            "watch_value": watch_value,
            "available": available,
            "cash": cash,
            "credit": self._opt_float(data.get("CreditAmount")),
            "quota": self._opt_float(data.get("QuotaLimit")),
            "currency": currency,
            "delta": delta,
            "delta_percent": delta_percent,
            "drop_alert": drop_alert,
            "request_id": str(payload.get("RequestId") or ""),
        }
        self._last_result = record
        # 当日消耗基线只在成功读数时更新，与本次结果一起落库
        self._update_daily_baseline(watch_value, currency)
        self._save_config()
        self._append_history(record)

        # 消费兜底：即使没收到模型用量事件（消耗不是 MoviePilot 侧发起的），
        # 余额确实在下降也转入高频窗口，保证持续消费能被盯住
        if (self._adaptive and self._spend_floor > 0 and delta is not None
                and delta <= -self._spend_floor):
            self._touch_window(f"余额较上次下降 {self._money(-delta, currency)}")

        logger.info(
            f"阿里云余额监控：{self._watch_label()} {self._money(watch_value, currency)}"
            f"（可用 {self._money(available, currency)} / 现金 {self._money(cash, currency)}）"
            f"，状态 {status}，环比 {self._delta_text(record)}"
        )
        if notify:
            self._notify(record, manual=manual, force=drop_alert)

    # ---------- 详情页 API ----------

    @staticmethod
    def _envelope(success: bool, message: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """按宿主前端 envelope 校验返回三键。

        前端拦截器要求响应体恰好是 {success, message, data} 且类型匹配，少一个 data 键
        也会被判定 invalid-envelope：表现为「点了报错但状态其实已经变了」。
        """
        return {"success": bool(success), "message": str(message or ""), "data": data}

    def _seconds_since(self, moment: Any) -> float:
        """本地墙钟串距今的秒数，解析不了返回一个足够大的值（按「很久以前」处理）。"""
        last_time = self._parse_local(moment)
        if last_time is None:
            return 1e9
        # 落库的检查时间不带时区，比较前同样去掉时区，避免 aware/naive 相减异常
        return (self._now().replace(tzinfo=None) - last_time).total_seconds()

    def refresh_now(self) -> Dict[str, Any]:
        """详情页「立即查询」按钮入口：当场查一次并返回读数，不推送通知。

        用同步函数注册，FastAPI 会自动放进线程池执行，避免阻塞事件循环；
        冷却期内的重复点击直接返回当前读数，不再打阿里云接口。
        """
        # 并发保护：查询要 0.5~2 秒，期间再点一次会穿过 10 秒冷却重复打接口
        lock = self._refresh_lock or threading.Lock()
        self._refresh_lock = lock
        if not lock.acquire(blocking=False):
            return self._envelope(False, "已有一次查询正在进行，请稍候再试")
        try:
            if not self._enabled:
                return self._envelope(False, "插件未启用，请先在配置里打开「启用插件」")
            if not self._access_key_id or not self._access_key_secret:
                return self._envelope(False, "未配置 AccessKey ID / AccessKey Secret")
            record = dict(self._last_result or {})
            elapsed = self._seconds_since(record.get("checked_at"))
            if elapsed < self._refresh_cooldown:
                return self._envelope(
                    True,
                    f"{int(max(elapsed, 0))} 秒前刚查过，当前读数 "
                    f"{self._money(record.get('watch_value'), record.get('currency'))}",
                    {"checked_at": record.get("checked_at"),
                     "watch_value": record.get("watch_value"),
                     "status": record.get("status")})
            self._do_check(manual=False, notify=False)
            record = dict(self._last_result or {})
            brief = {"checked_at": record.get("checked_at"),
                     "watch_value": record.get("watch_value"),
                     "status": record.get("status")}
            if record.get("status") == "error":
                return self._envelope(False, str(record.get("error") or "查询失败"), brief)
            return self._envelope(
                True,
                f"已查询：{self._watch_label()} "
                f"{self._money(record.get('watch_value'), record.get('currency'))}"
                f"（环比 {self._delta_text(record)}）",
                brief)
        except Exception as err:
            logger.error(f"阿里云余额监控：立即查询失败：{str(err)}")
            return self._envelope(False, f"查询异常：{str(err)[:120]}")
        finally:
            lock.release()

    @staticmethod
    def _delta_text(record: Dict[str, Any]) -> str:
        """把环比变化渲染成 +/-% 文案。"""
        percent = record.get("delta_percent")
        if percent is None:
            return "—"
        try:
            value = float(percent)
        except (TypeError, ValueError):
            return "—"
        return f"{'+' if value > 0 else ''}{value:.2f}%"

    def _other_balance_text(self, record: Dict[str, Any], currency: str) -> str:
        """未被监控口径覆盖的另一项余额：只在两项确实不同时才占一行，相同就别重复同一个数字。"""
        available = self._opt_float(record.get("available"))
        cash = self._opt_float(record.get("cash"))
        if available is None or cash is None or abs(available - cash) < 0.005:
            return ""
        if self._watch_field == "AvailableAmount":
            return f"现金余额：{self._money(cash, currency)}"
        return f"可用额度：{self._money(available, currency)}"

    def _burn_line(self, value: Optional[float]) -> str:
        """通知里「今日消耗」一行：只给消耗与速率，样本不足时说清原因。"""
        spent, rate = self._burn_metrics(value)
        if spent == "—":
            return "今日消耗：样本不足（当日需至少两次成功读数）"
        if rate == "统计中":
            return f"今日消耗：{spent}（速率统计中）"
        return f"今日消耗：{spent}（速率 {rate}）"

    def _alert_trigger_line(self, record: Dict[str, Any], currency: str) -> str:
        """通知的触发行：说清这次为什么提醒并给出对应阈值线。

        当前读数只在第一行出现一次，这里不复述它（用户明确要求）；也不把全部阈值
        铺开——非告警场景（手动查询、恢复正常）才列一行阈值参考。
        """
        status = str(record.get("status") or "unknown")
        if status == "critical":
            return f"触发：低于紧急线 {self._money(self._critical_threshold, currency)}"
        if status == "warn":
            return f"触发：低于预警线 {self._money(self._warn_threshold, currency)}"
        if record.get("drop_alert"):
            percent = self._opt_float(record.get("delta_percent"))
            down = f"{abs(percent):.2f}%" if percent is not None else self._delta_text(record)
            if self._drop_percent > 0:
                return f"触发：较上次下降 {down}（阈值 {self._drop_percent:g}%）"
            return f"触发：较上次下降 {down}"
        return f"阈值：{self._threshold_hint(currency, include_full=False)}"

    def _alert_key(self, record: Dict[str, Any]) -> str:
        """告警档位指纹：状态 + 是否骤降，用于同档位去重。"""
        return f"{record.get('status') or 'unknown'}|{'drop' if record.get('drop_alert') else ''}"

    def _should_notify(self, record: Dict[str, Any]) -> Tuple[bool, str]:
        """按通知策略判断是否推送，返回（是否推送, 追加说明）。"""
        if self._notify_mode == "always":
            return True, ""
        key = self._alert_key(record)
        status = str(record.get("status") or "")
        last_key = str(self._last_alert.get("key") or "")
        last_status = last_key.split("|")[0] if last_key else ""
        # 档位变化：新进入告警、告警升级、或从告警恢复为正常都要发
        if key != last_key:
            if status in ("warn", "critical") or record.get("drop_alert"):
                return True, ""
            if status == "normal" and last_status in ("warn", "critical"):
                return True, f"已恢复到预警线以上（此前状态 {self._status_meta.get(last_status, ('', last_status))[1]}）"
            return False, ""
        # 同一告警档位：按间隔重复提醒
        if status in ("warn", "critical") or record.get("drop_alert"):
            if self._repeat_hours <= 0:
                return True, ""
            last_at = str(self._last_alert.get("at") or "")
            try:
                last_time = datetime.strptime(last_at[:19], "%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError):
                return True, ""
            # 落库的检查时间是不带时区的本地墙钟串，这里同样去掉时区再比差，
            # 否则 aware 与 naive 相减会抛异常，导致「同档位不重复提醒」永远失效
            elapsed = (self._now().replace(tzinfo=None) - last_time).total_seconds() / 3600
            if elapsed >= self._repeat_hours:
                return True, f"（同档位已持续 {int(elapsed)} 小时，按 {self._repeat_hours:g} 小时间隔重复提醒）"
            return False, ""
        return False, ""

    def _notify(self, record: Dict[str, Any], manual: bool = False, force: bool = False) -> None:
        """推送余额通知；force 为真（查询失败、余额骤降、手动触发）时忽略去重策略。"""
        record = record or {}
        status = str(record.get("status") or "unknown")
        suffix = ""
        if not (force or manual):
            allow, suffix = self._should_notify(record)
            if not allow:
                return
        if manual:
            suffix = f"{suffix}（手动触发）" if suffix else "（手动触发）"

        title_prefix = "阿里云余额监控"
        if status == "error":
            title = f"🔴【{title_prefix}】查询失败"
        elif status == "critical":
            title = f"🔴【{title_prefix}】余额低于紧急线"
        elif status == "warn":
            title = f"⚠️【{title_prefix}】余额低于预警线"
        elif record.get("drop_alert"):
            title = f"⚠️【{title_prefix}】余额骤降提醒"
        else:
            title = f"【{title_prefix}】余额正常"

        lines: List[str] = []
        if status == "error":
            lines.append(f"错误：{record.get('error') or '未知原因'}")
            lines.append(f"接入点：{self._endpoint_label()}（{self._endpoint}）")
            lines.append("排查建议：确认 AccessKey 有效且已授权费用只读权限；"
                         "国际站账号需把接入点改为 business.ap-southeast-1.aliyuncs.com；"
                         "网络受限可切换「优先走系统代理」。")
        else:
            currency = str(record.get("currency") or "")
            # 标题已经说过档位，正文不再复述状态；读数与环比合成一行
            main = f"{self._watch_label()}：{self._money(record.get('watch_value'), currency)}"
            if status != "unknown":
                main += f"（较上次 {self._delta_text(record)}）"
            if not self._currencies.get(currency.upper(), ""):
                main += f" · {currency or '未知币种'}"
            lines.append(main)
            other = self._other_balance_text(record, currency)
            if other:
                lines.append(other)
            lines.append(self._burn_line(self._opt_float(record.get("watch_value"))))
            lines.append(self._alert_trigger_line(record, currency))
        lines.append(f"检查时间：{self._short_time(record.get('checked_at'))}")
        if suffix:
            lines.append(f"说明：{suffix}")

        self.post_message(
            mtype=NotificationType.Plugin,
            title=title,
            text="\n".join(lines),
        )
        if status != "error":
            self._last_alert = {
                "key": self._alert_key(record),
                "at": record.get("checked_at") or self._now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            self._save_config()

    # ---------- 详情页 ----------

    def _chip(self, text: str, color: str, variant: str = "flat") -> dict:
        """生成状态徽章节点。"""
        return {
            "component": "VChip",
            "props": {"size": "small", "color": color, "variant": variant,
                      "class": "text-none"},
            "text": text,
        }

    def _row(self, label: str, value: Any, hint: str = "") -> dict:
        """列表中的一行：左侧字段名、右侧取值，可选第二行补充说明。"""
        children: List[dict] = [{
            "component": "div",
            "props": {"style": self._row_style},
            "content": [
                {"component": "span", "props": {"class": "text-body-2"}, "text": label},
                {"component": "span",
                 "props": {"class": "text-right", "style": self._row_value_style},
                 "text": str(value) if str(value or "").strip() else "—"},
            ],
        }]
        if hint:
            children.append({
                "component": "div",
                "props": {"class": "text-caption text-medium-emphasis",
                          "style": self._wrap_style},
                "text": hint,
            })
        return {"component": "VListItem",
                "props": {"density": "compact", "lines": False},
                "content": children}

    def _stack(self, label: str, value: Any, hint: str = "") -> dict:
        """长取值用：字段名单独一行，取值整行铺开，避免窄屏把左右两列挤成多行错位。"""
        children: List[dict] = [
            {"component": "div",
             "props": {"class": "text-caption text-medium-emphasis"},
             "text": label},
            {"component": "div",
             "props": {"class": "text-body-1", "style": self._wrap_style},
             "text": str(value) if str(value or "").strip() else "—"},
        ]
        if hint:
            children.append({
                "component": "div",
                "props": {"class": "text-caption text-medium-emphasis",
                          "style": self._wrap_style},
                "text": hint,
            })
        return {"component": "VListItem",
                "props": {"density": "compact", "lines": False},
                "content": children}

    def _action_bar(self) -> dict:
        """顶部操作条：「立即查询」按钮，走插件自定义 API，宿主收到成功事件后整页刷新。"""
        return {
            "component": "div",
            "props": {"class": "mb-3",
                      "style": "display:flex;align-items:center;"
                               "justify-content:space-between;gap:10px;flex-wrap:wrap"},
            "content": [
                {"component": "span",
                 "props": {"class": "text-caption text-medium-emphasis"},
                 "text": f"读数来自阿里云费用接口 · {int(self._refresh_cooldown)} 秒内重复点击不再请求"},
                {"component": "VBtn",
                 "props": {"size": "small", "variant": "flat", "color": "primary",
                           "class": "text-none"},
                 "text": "立即查询",
                 "events": {"click": {
                     "api": f"plugin/{self.__class__.__name__}/refresh",
                     "method": "GET",
                 }}},
            ],
        }

    @staticmethod
    def _group(title: str) -> dict:
        """列表分组标题（VListSubheader）。"""
        return {"component": "VListSubheader", "props": {"color": "primary"}, "text": title}

    def _freq_badge(self) -> dict:
        """主卡上的频率徽章：高频蓝、日常与固定灰。"""
        if not self._adaptive:
            return self._chip("固定周期", "grey-lighten-3", "tonal")
        if self._window_minutes_left() > 0:
            return self._chip(f"高频 {self._tick_minutes:g} 分钟", "primary", "tonal")
        return self._chip(f"日常 {self._idle_hours:g} 小时", "grey-lighten-3", "tonal")

    def _notice_block(self, alert_type: str, title: str, description: str) -> dict:
        """未启用或暂无记录时的状态卡。"""
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

    def _hero_block(self, record: Dict[str, Any]) -> dict:
        """状态主卡：状态徽章 + 大号余额 + 阈值说明 + 进度条（相对满格金额，未填时回退预警线）。"""
        status = str(record.get("status") or "unknown")
        color, label = self._status_meta.get(status, self._status_meta["unknown"])
        currency = str(record.get("currency") or "")
        value = self._opt_float(record.get("watch_value"))
        ratio = self._bar_ratio(value, status)
        content: List[dict] = [
            {"component": "div", "props": {"style": self._hero_badge_style},
             "content": [self._chip(label, color, "flat"), self._freq_badge()]},
            {"component": "div", "props": {"style": self._hero_row_style}, "content": [
                {"component": "span", "props": {"style": self._hero_unit_style},
                 "text": self._currencies.get(currency.upper(), "")},
                {"component": "span", "props": {"style": self._hero_num_style},
                 "text": "—" if value is None else f"{value:,.2f}"},
                {"component": "span", "props": {"style": self._hero_unit_style},
                 "text": currency or self._watch_label()},
            ]},
            {"component": "div", "props": {"style": self._sub_text_style},
             "text": f"{self._watch_label()} · 环比 {self._delta_text(record)}"},
            {"component": "VProgressLinear",
             "props": {"model-value": round(ratio, 1), "color": color,
                       "height": 6, "rounded": True, "class": "mt-4"}},
            {"component": "div", "props": {"style": self._foot_style},
             "text": self._threshold_hint(currency)},
            {"component": "div",
             "props": {"style": "font-size:.8rem;line-height:1.5;opacity:.85;text-align:center"},
             "text": (f"最近 {self._mm_time(record.get('checked_at'))} · "
                      f"下次 {self._mm_time(self._next_check_text())}")},
        ]
        return {
            "component": "VAlert",
            "props": {"type": color, "variant": "tonal", "icon": False,
                      "class": "mb-3", "style": self._hero_style},
            "content": content,
        }

    def _error_block(self, record: Dict[str, Any]) -> dict:
        """查询失败时的说明卡：把最后一次成功读数留在原地，避免失败一次就整页空白。"""
        value = self._opt_float(record.get("watch_value"))
        currency = str(record.get("currency") or "")
        content: List[dict] = [
            {"component": "div", "props": {"style": self._notice_title_style},
             "text": "查询失败"},
            {"component": "div", "props": {"style": self._wrap_style,
                                            "class": "text-break"},
             "text": str(record.get("error") or "未知错误")},
        ]
        if value is not None:
            content.append({
                "component": "div", "props": {"style": self._sub_text_style},
                "text": (f"最后已知{self._watch_label()} {self._money(value, currency)}"
                         f"（{self._short_time(record.get('good_at'))}）· 数据已滞后"),
            })
        content.append({
            "component": "div", "props": {"style": self._foot_style},
            "text": (f"接入点 {self._endpoint_label()}（{self._endpoint}）· "
                     f"AccessKey {self._mask_key(self._access_key_id)} · "
                     f"本次检查 {self._short_time(record.get('checked_at'))}"),
        })
        return {
            "component": "VAlert",
            "props": {"type": "error", "variant": "tonal", "icon": False,
                      "class": "mb-3", "style": self._hero_style},
            "content": content,
        }

    @staticmethod
    def _short_time(value: Any) -> str:
        """把 2026-10-29 09:00:00 收成 2026-10-29 09:00，窄屏上不易折行。"""
        text = str(value or "").strip()
        return text[:16] if text else "—"

    @staticmethod
    def _mm_time(value: Any) -> str:
        """页面行内用的紧凑时间：10-29 09:00，省掉年份给取值留出宽度。"""
        text = str(value or "").strip()
        return text[5:16] if len(text) >= 16 else (text or "—")

    def _detail_card(self, record: Dict[str, Any]) -> dict:
        """明细卡：余额、监控频率、接入信息三组列表，底部给出费用中心入口。"""
        record = record or {}
        currency = str(record.get("currency") or "")
        usage_value, usage_hint = self._usage_text()
        burn_value, burn_hint = self._burn_text(self._opt_float(record.get("watch_value")))
        # 短取值一律「左名右值」一行读完；长取值改成上下堆叠，避免窄屏两列互相挤压
        rows: List[dict] = [
            self._group("余额明细"),
            self._row("可用额度", self._money(record.get("available"), currency), "欠费判定看这项"),
            self._row("现金余额", self._money(record.get("cash"), currency), "实际充值剩余部分"),
            self._row("信控额度", self._money(record.get("credit"), currency), "后付费授信部分"),
            self._row("Quota 限额", self._money(record.get("quota"), currency)),
            self._row("币种", currency or "—"),
            self._group("监控与频率"),
            self._stack("检查频率", self._freq_text(),
                        f"高频 {self._tick_minutes:g} 分钟 · 活跃窗口 {self._active_minutes:g} 分钟 · "
                        f"日常 {self._idle_hours:g} 小时"
                        if self._adaptive else f"执行周期 {self._cron}"),
            self._stack("今日模型调用", usage_value, usage_hint),
            self._stack("今日消耗", burn_value, burn_hint),
            self._row("预警 / 紧急线",
                      f"{self._money(self._warn_threshold, currency)} / "
                      f"{self._money(self._critical_threshold, currency)}",
                      "余额不大于该值转黄 / 转红，填 0 关闭"),
            self._row("骤降提醒", f"{self._drop_percent:g}%" if self._drop_percent > 0 else "已关闭",
                      "按相邻两次余额的百分比变化判定"),
            self._row("消费兜底阈值",
                      self._money(self._spend_floor, currency) if self._spend_floor > 0 else "已关闭",
                      "余额比上次下降达该值也转高频"),
            self._row("最近检查", self._mm_time(record.get("checked_at"))),
            self._row("下次检查", self._mm_time(self._next_check_text()),
                      "自适应模式下为估算值，实际以唤醒节奏为准" if self._adaptive else ""),
            self._stack("通知策略", self._notify_modes.get(self._notify_mode, self._notify_mode),
                        f"同档位重复提醒间隔 {self._repeat_hours:g} 小时"
                        if self._notify_mode == "alert" else "每次检查都会推送读数"),
            self._group("接入信息"),
            self._row("监控口径", self._watch_label(), f"环比 {self._delta_text(record)}"),
            self._row("AccessKey ID", self._mask_key(self._access_key_id)),
            self._row("AccessKey Secret", "已配置" if self._access_key_secret else "未配置",
                      f"来源 {self._credential_source} · 页面与日志都不显示明文"),
            self._stack("服务接入点", self._endpoint, self._endpoint_label()),
            self._row("网络通道", "优先走系统代理" if self._use_proxy and settings.PROXY else "直连",
                      "代理失败会自动改直连重试一次"),
        ]
        return {
            "component": "VCard",
            "props": {"variant": "outlined", "class": "mb-3"},
            "content": [
                {"component": "VList",
                 "props": {"density": "compact", "lines": False},
                 "content": rows},
                {
                    "component": "div",
                    "props": {"class": "px-4 py-3",
                              "style": "display:flex;align-items:center;"
                                       "justify-content:space-between;gap:10px;flex-wrap:wrap"},
                    "content": [{
                        "component": "span",
                        "props": {"class": "text-caption text-medium-emphasis"},
                        "text": "只读费用接口，不做任何写操作",
                    }, {
                        # 宿主只有「插件市场详情」会读 plugin_readme，已安装插件的界面没有任何
                        # 说明入口，所以详情页底部自带一条指向仓库 README 的链接（新标签打开）。
                        "component": "div",
                        "props": {"style": "display:flex;align-items:center;gap:8px;flex-wrap:wrap"},
                        "content": [{
                            "component": "VBtn",
                            "props": {"size": "small", "variant": "text", "color": "primary",
                                      "href": self.plugin_readme,
                                      "target": "_blank", "rel": "noopener noreferrer",
                                      "prepend-icon": "mdi-book-open-outline",
                                      "class": "text-none"},
                            "text": "使用说明",
                        }, {
                            "component": "VBtn",
                            "props": {"size": "small", "variant": "tonal", "color": "primary",
                                      "href": "https://usercenter2.aliyun.com/home",
                                      "target": "_blank", "rel": "noopener noreferrer",
                                      "class": "text-none"},
                            "text": "打开阿里云费用中心",
                        }],
                    }],
                },
            ],
        }

    def _history_card(self, history: List[dict]) -> dict:
        """历史卡：每次检查一行，标题带检查时间与状态徽章，副标题说明当时余额与变化。"""
        shown = history[:self._history_show]
        items: List[dict] = []
        for index, record in enumerate(shown):
            status = str(record.get("status") or "unknown")
            color, label = self._status_meta.get(status, self._status_meta["unknown"])
            if status == "error":
                amount_text = "查询失败"
                parts = [f"错误：{record.get('error') or '未知原因'}"]
            else:
                currency = str(record.get("currency") or "")
                amount_text = self._money(record.get("watch_value"), currency)
                parts = [f"环比 {self._delta_text(record)}"]
                available = self._opt_float(record.get("available"))
                cash = self._opt_float(record.get("cash"))
                # 可用与现金一致时（无信控额度的常见情况）不再重复一遍，窄屏少一行
                if available is not None and cash is not None and abs(available - cash) > 0.005:
                    parts.append(f"可用 {self._money(available, currency)}"
                                 f" / 现金 {self._money(cash, currency)}")
            title_children: List[dict] = [
                {"component": "span", "text": self._mm_time(record.get("checked_at"))},
            ]
            if index == 0:
                title_children.append(self._chip("最新", "primary", "tonal"))
            if record.get("drop_alert"):
                title_children.append(self._chip("骤降", "warning", "tonal"))
            title_children.append(self._chip(label, color, "flat"))
            items.append({
                "component": "VListItem",
                "props": {"density": "compact"},
                "content": [
                    {"component": "VListItemTitle",
                     "props": {"style": self._list_title_style},
                     "content": title_children},
                    {"component": "VListItemSubtitle",
                     "props": {"style": self._wrap_style, "class": "text-break"},
                     "text": f"{amount_text} · {' · '.join(parts)}"},
                ],
            })
        return {
            "component": "VCard",
            "props": {"variant": "outlined"},
            "content": [
                {"component": "div",
                 "props": {"class": "px-4 pt-4 pb-3", "style": self._card_title_style},
                 "text": f"检查历史（最近 {len(shown)} 次）"},
                {"component": "VList", "props": {"density": "compact"}, "content": items},
            ],
        }

    def get_page(self) -> Optional[List[dict]]:
        """返回插件详情页面。"""
        record = dict(self._last_result or {})
        history = [item for item in (self.get_data("balance_history") or []) if isinstance(item, dict)]

        if not self._enabled:
            return [self._notice_block(
                "warning", "插件未启用",
                f"在插件配置里打开「启用插件」并填写 AccessKey 后，"
                f"会按自适应频率自动查询阿里云账户余额并按阈值提醒"
                f"（高频 {self._tick_minutes:g} 分钟 / 日常 {self._idle_hours:g} 小时）。"
            )]
        if not record:
            return [self._action_bar(), self._notice_block(
                "info", "暂无检查记录",
                "点上方「立即查询」可当场查一次；到达检查节奏后会自动查询，"
                "并把余额、模型调用用量与检查历史显示在这里。"
            )]

        blocks: List[dict] = [self._action_bar()]
        if record.get("status") == "error":
            blocks.append(self._error_block(record))
        else:
            blocks.append(self._hero_block(record))
        blocks.append(self._detail_card(record))
        if history:
            blocks.append(self._history_card(history))
        return blocks

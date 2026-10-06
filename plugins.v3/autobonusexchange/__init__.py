"""
NexusPHP 魔力值自动兑换插件

读取 MoviePilot 已配置站点，按架构适配器动态解析魔力商店并按策略自动兑换上传/下载量。
支持自适应限速、多站并发、单站串行、安全熔断与任务统计。
"""

import re
import time
import traceback
from datetime import datetime, timedelta
from multiprocessing.dummy import Pool as ThreadPool
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

import pytz
from app.core.config import settings
from app.db.site_oper import SiteOper
from app.helper.sites import SitesHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import NotificationType
from app.utils.http import RequestUtils
from app.utils.string import StringUtils
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger


class AutoBonusExchange(_PluginBase):
    """NexusPHP 魔力值自动兑换插件：按架构适配器动态解析魔力商店并按策略自动兑换上传/下载量。"""

    # 插件名称
    plugin_name = "魔力值自动兑换"
    # 插件描述
    plugin_desc = "读取 MoviePilot 已配置站点，按策略自动兑换上传/下载量。(不支持部分站点)"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/andyxu8023/MoviePilot-Plugins/main/icons/AutoBonusExchange.png"
    # 插件版本
    plugin_version = "2.0.6"
    # 插件作者
    plugin_author = "左岸"
    # 作者主页
    author_url = "https://github.com/andyxu8023"
    # 插件配置项ID前缀
    plugin_config_prefix = "autobonusexchange_"
    # 加载顺序
    plugin_order = 100
    # 可使用的用户级别
    auth_level = 2

    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None

    # 配置属性
    _enabled: bool = False
    _cron: str = ""
    _onlyonce: bool = False
    _notify: bool = False
    _queue_cnt: int = 3
    _bonus_sites: list = []
    _strategy: str = "fixed"
    _exchange_type: str = "upload"  # upload, download, both
    _keep_balance: float = 0
    _max_retry: int = 3
    _circuit_break_threshold: int = 5
    _rate_limit_ms: int = 1000

    # 运行时状态
    _today_data: dict = {}
    _circuit_breaker: dict = {}
    _force_run: bool = False

    def init_plugin(self, config: dict = None) -> None:
        """根据插件配置初始化运行状态。"""
        # 停止现有任务
        self.stop_service()

        # 配置
        if config:
            self._enabled = config.get("enabled", False)
            self._cron = config.get("cron", "") or "0 0 1 * *"
            self._onlyonce = config.get("onlyonce", False)
            self._notify = config.get("notify", False)
            self._queue_cnt = config.get("queue_cnt", 3)
            self._bonus_sites = config.get("bonus_sites", [])
            self._strategy = config.get("strategy", "fixed")
            self._exchange_type = config.get("exchange_type", "upload")
            self._keep_balance = float(config.get("keep_balance", 0))
            self._max_retry = int(config.get("max_retry", 3))
            self._circuit_break_threshold = int(config.get("circuit_break_threshold", 5))
            self._rate_limit_ms = int(config.get("rate_limit_ms", 1000))
            self._force_run = config.get("force_run", False)

            # 过滤掉已删除的站点
            all_sites = [site.id for site in SiteOper().list_order_by_pri()]
            self._bonus_sites = [site_id for site_id in all_sites if site_id in self._bonus_sites]

            # 保存配置
            self._save_config()

        # 启动任务
        if self._enabled or self._onlyonce:
            if self._onlyonce:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                logger.info("魔力值自动兑换服务启动，立即运行一次")
                self._scheduler.add_job(
                    func=self.exchange_bonus,
                    trigger='date',
                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                    name="魔力值自动兑换"
                )
                # 关闭一次性开关
                self._onlyonce = False
                self._save_config()

                # 启动任务
                if self._scheduler.get_jobs():
                    self._scheduler.print_jobs()
                    self._scheduler.start()

    def get_state(self) -> bool:
        """获取插件启用状态。"""
        return self._enabled

    def _save_config(self) -> None:
        """保存插件配置。"""
        self.update_config({
            "enabled": self._enabled,
            "notify": self._notify,
            "cron": self._cron,
            "onlyonce": self._onlyonce,
            "queue_cnt": self._queue_cnt,
            "bonus_sites": self._bonus_sites,
            "strategy": self._strategy,
            "exchange_type": self._exchange_type,
            "keep_balance": self._keep_balance,
            "max_retry": self._max_retry,
            "circuit_break_threshold": self._circuit_break_threshold,
            "rate_limit_ms": self._rate_limit_ms,
            "force_run": self._force_run,
        })

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """返回插件远程命令列表。"""
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """返回插件 API 列表。"""
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        """注册插件公共服务。"""
        if self._enabled and self._cron:
            try:
                if str(self._cron).strip().count(" ") == 4:
                    return [{
                        "id": "AutoBonusExchange",
                        "name": "魔力值自动兑换服务",
                        "trigger": CronTrigger.from_crontab(self._cron),
                        "func": self.exchange_bonus,
                        "kwargs": {}
                    }]
                logger.warning("魔力值自动兑换定时任务未注册：执行周期仅支持5位cron表达式")
            except Exception as err:
                logger.error(f"魔力兑换定时任务配置错误：{str(err)}")
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回插件配置表单与默认配置。"""
        # 站点选项
        site_options = [{"title": site.name, "value": site.id}
                        for site in SiteOper().list_order_by_pri()]

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
                                    "props": {"model": "enabled", "label": "启用插件"}
                                }]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "notify", "label": "发送通知"}
                                }]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "onlyonce", "label": "立即运行一次"}
                                }]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "force_run", "label": "强制运行(忽略今日已完成)"}
                                }]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VCronField",
                                    "props": {
                                        "model": "cron",
                                        "label": "执行周期",
                                        "placeholder": "5位cron表达式，留空默认 0 0 1 * *"
                                    }
                                }]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VSelect",
                                    "props": {
                                        "model": "strategy",
                                        "label": "兑换策略",
                                        "items": [
                                            {"title": "最大化兑换", "value": "maximize"},
                                            {"title": "保留余额", "value": "keep_balance"}
                                        ]
                                    }
                                }]
                            }
                        ]
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
                                        "model": "exchange_type",
                                        "label": "兑换类型",
                                        "items": [
                                            {"title": "仅上传量", "value": "upload"},
                                            {"title": "仅下载量", "value": "download"},
                                            {"title": "上传+下载", "value": "both"}
                                        ]
                                    }
                                }]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "keep_balance",
                                        "label": "保留魔力值",
                                        "type": "number",
                                        "placeholder": "0"
                                    }
                                }]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "rate_limit_ms",
                                        "label": "请求间隔(ms)",
                                        "type": "number",
                                        "placeholder": "1000"
                                    }
                                }]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "circuit_break_threshold",
                                        "label": "熔断阈值(次)",
                                        "type": "number",
                                        "placeholder": "连续失败次数"
                                    }
                                }]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [{
                                    "component": "VSelect",
                                    "props": {
                                        "model": "bonus_sites",
                                        "label": "兑换站点",
                                        "items": site_options,
                                        "multiple": True,
                                        "chips": True
                                    }
                                }]
                            }
                        ]
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
                                        "model": "queue_cnt",
                                        "label": "并发站点数",
                                        "type": "number",
                                        "placeholder": "3"
                                    }
                                }]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "max_retry",
                                        "label": "最大重试次数",
                                        "type": "number",
                                        "placeholder": "3"
                                    }
                                }]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "notify": False,
            "cron": "0 0 1 * *",
            "onlyonce": False,
            "queue_cnt": 3,
            "bonus_sites": [],
            "strategy": "fixed",
            "exchange_type": "upload",
            "keep_balance": 0,
            "max_retry": 3,
            "circuit_break_threshold": 5,
            "rate_limit_ms": 1000,
            "force_run": False,
        }

    def get_page(self) -> Optional[List[dict]]:
        """返回插件详情页面。"""
        if not self._enabled:
            return None

        # 获取今日数据
        today = datetime.today().strftime('%Y-%m-%d')
        today_data = self.get_data(key=f"bonus_{today}")

        if not today_data:
            return [{
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "text": "今日暂无兑换记录"
                }
            }]

        # 构建详情列表
        items = []

        def _fmt_bonus(value) -> str:
            """未取到魔力值时显示占位符，避免看起来像被清零。"""
            return self._fmt_num(value) if value else "—"

        for site_name, result in today_data.get("results", {}).items():
            status = result.get("status", "unknown")
            exchanged = result.get("exchanged") or []
            attempts = result.get("attempts") or []
            status_text = {"success": "成功", "failed": "失败", "skipped": "跳过"}.get(status, "未知")
            status_color = "success" if status == "success" else "error" if status == "failed" else "warning"

            texts = [f"魔力值: {_fmt_bonus(result.get('bonus_before', 0))} → "
                     f"{_fmt_bonus(result.get('bonus_after', 0))}"]
            for row in self._group_exchanged(exchanged):
                # 同档位多笔合并成一行，移动端不至于被重复条目刷屏
                texts.append(f"已兑换: {row['name']} × {row['count']}，"
                             f"单价 {self._fmt_num(row['cost'])}，"
                             f"共消耗 {self._fmt_num(row['cost'] * row['count'])}")
            for attempt in attempts:
                if attempt.get("success"):
                    continue
                texts.append(f"未落地: {attempt.get('item', '')} - "
                             f"{attempt.get('detail') or '兑换未生效'}")
            if result.get("error"):
                texts.append(f"原因: {result.get('error')}")
            if result.get("note") and not any(not a.get("success") for a in attempts):
                texts.append(f"说明: {result.get('note')}")

            content = [{
                "component": "VListItemTitle",
                "content": [{
                    "component": "VChip",
                    "props": {"color": status_color, "size": "small"},
                    "text": status_text
                }]
            }]
            # 每条信息一行，长句由浏览器按容器宽度自动换行（桌面端不再被固定列宽截断）
            for text in texts:
                content.append({
                    "component": "VListItemSubtitle",
                    "props": {"class": "text-break", "style": self._WRAP_STYLE},
                    "text": text
                })

            items.append({
                "component": "VListItem",
                "props": {"title": site_name},
                "content": content
            })

        return [{
            "component": "VCard",
            "props": {"title": "今日兑换记录"},
            "content": [{
                "component": "VList",
                "content": items
            }]
        }]

    def stop_service(self) -> None:
        """停止插件后台服务并释放资源。"""
        if self._scheduler:
            try:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
            except Exception as e:
                logger.error(f"停止魔力兑换服务失败：{str(e)}")

    def exchange_bonus(self) -> None:
        """执行魔力兑换任务。"""
        logger.info("开始执行 NexusPHP 魔力兑换任务...")

        # 获取今日日期
        today = datetime.today().strftime('%Y-%m-%d')

        # 检查今日是否已执行（可通过配置强制跳过）
        today_data = self.get_data(key=f"bonus_{today}")
        if today_data and today_data.get("completed") and not self._force_run:
            logger.info(f"今日 {today} 已完成魔力兑换，跳过")
            return

        self._force_run = False  # 重置强制标志

        # 获取所有站点
        all_sites = [site for site in SitesHelper().get_indexers() if not site.get("public")]

        # 过滤目标站点
        if self._bonus_sites:
            target_sites = [site for site in all_sites if site.get("id") in self._bonus_sites]
        else:
            target_sites = all_sites

        if not target_sites:
            logger.info("没有需要兑换的站点")
            return

        # 初始化今日数据
        today_data = {
            "date": today,
            "completed": False,
            "results": {},
            "start_time": datetime.now().isoformat(),
        }

        # 并发执行兑换
        logger.info(f"开始兑换，共 {len(target_sites)} 个站点，并发数 {self._queue_cnt}")

        with ThreadPool(min(len(target_sites), self._queue_cnt)) as p:
            results = p.map(self._exchange_site, target_sites)

        # 汇总结果
        for site_name, result in results:
            today_data["results"][site_name] = result

        today_data["completed"] = True
        today_data["end_time"] = datetime.now().isoformat()

        # 保存今日数据
        self.save_data(key=f"bonus_{today}", value=today_data)

        # 发送通知
        if self._notify:
            self._send_notification(today_data)

        logger.info(f"魔力兑换任务完成，共处理 {len(results)} 个站点")

    def _exchange_site(self, site_info: dict) -> Tuple[str, dict]:
        """
        兑换单个站点的魔力值。

        :param site_info: 站点信息字典
        :return: (站点名称, 兑换结果字典)
        """
        site_name = site_info.get("name", "未知站点")
        result = {
            "status": "unknown",
            "bonus_before": 0,
            "bonus_after": 0,
            "exchanged": [],
            "error": None,
            "note": None,
            "attempts": [],
        }

        try:
            # 检查熔断器
            if self._check_circuit_breaker(site_name):
                result["status"] = "skipped"
                result["error"] = (f"本轮该站已连续失败 {self._circuit_breaker.get(site_name, 0)} 次，"
                                   f"达到熔断阈值 {self._circuit_break_threshold}，跳过")
                logger.warn(f"{site_name} 已触发熔断，跳过兑换")
                return site_name, result

            # 获取站点信息
            site_url = site_info.get("url", "")
            site_cookie = site_info.get("cookie", "")
            site_apikey = site_info.get("apikey", "")
            site_token = site_info.get("token", "")
            ua = site_info.get("ua", "")
            proxies = settings.PROXY if site_info.get("proxy") else None
            timeout = site_info.get("timeout") or 60

            if not site_url:
                result["status"] = "failed"
                result["error"] = "未配置站点地址"
                logger.warn(f"{site_name} 未配置站点地址")
                return site_name, result

            # 判断认证方式 - 优先使用cookie
            auth_mode = None
            if site_cookie:
                auth_mode = "cookie"
            elif site_token:
                auth_mode = "token"
            elif site_apikey:
                auth_mode = "apikey"
            else:
                result["status"] = "failed"
                result["error"] = "未配置认证信息(Cookie/APIKey/Token)"
                logger.warn(f"{site_name} 未配置认证信息")
                return site_name, result

            logger.info(f"{site_name} 使用 {auth_mode} 认证模式")

            # 创建请求客户端
            if auth_mode == "token":
                # 馒头等使用 token 认证的站点
                req_utils = RequestUtils(
                    headers={
                        "Authorization": site_token,
                        "Content-Type": "application/json",
                        "Accept": "application/json, text/plain, */*",
                    },
                    ua=ua,
                    proxies=proxies,
                    timeout=timeout
                )
            elif auth_mode == "apikey":
                # 高清杜比等使用 apikey 认证的站点
                req_utils = RequestUtils(
                    headers={
                        "x-api-key": site_apikey,
                    },
                    ua=ua,
                    proxies=proxies,
                    timeout=timeout
                )
            else:
                # Cookie 认证
                req_utils = RequestUtils(
                    cookies=site_cookie,
                    ua=ua,
                    proxies=proxies,
                    timeout=timeout
                )

            # 获取魔力商店页面 - 检测实际URL
            # 有些站点用 mybonus.php，有些用 bonusshop.php
            bonus_url = urljoin(site_url, "mybonus.php")
            logger.info(f"开始获取 {site_name} 魔力商店: {bonus_url}")

            res = req_utils.get_res(url=bonus_url)
            
            # 检查页面是否包含兑换表单，如果没有则尝试 bonusshop.php
            has_exchange_form = res and res.status_code == 200 and re.search(r'<form[^>]*action=["\']?\?action=exchange["\']?', res.text, re.IGNORECASE)
            
            # 检查是否使用API模式（如青蛙站点）
            is_api_mode = res and res.status_code == 200 and '/api/bonus-shop/getItems' in res.text
            
            if not has_exchange_form and not is_api_mode:
                alt_url = urljoin(site_url, "bonusshop.php")
                logger.info(f"{site_name} mybonus.php 无兑换表单，尝试备用URL: {alt_url}")
                alt_res = req_utils.get_res(url=alt_url)
                if alt_res and alt_res.status_code == 200:
                    res = alt_res
                    bonus_url = alt_url
                    # 重新检查是否为API模式
                    is_api_mode = '/api/bonus-shop/getItems' in res.text
            
            if not res or res.status_code != 200:
                result["status"] = "failed"
                if res is None:
                    result["error"] = "获取魔力商店失败：请求无响应（网络不通、超时或被站点拒绝，Cookie 可能已失效）"
                else:
                    result["error"] = f"获取魔力商店失败：状态码 {res.status_code}（多为 Cookie 失效或被站点拦截）"
                logger.warn(f"{site_name} 获取魔力商店失败")
                self._record_circuit_break(site_name)
                return site_name, result

            # 解析魔力商店
            adapter = NexusPHPBonusAdapter()
            
            # 如果是API模式，使用API获取兑换项目
            if is_api_mode:
                logger.info(f"{site_name} 使用API模式获取兑换项目")
                bonus_info = self._parse_api_bonus_page(req_utils, site_url, res.text)
            else:
                bonus_info = adapter.parse_bonus_page(res.text)

            # 调试：保存 HTML 到文件
            try:
                debug_dir = "/config/temp/bonus_debug"
                import os
                os.makedirs(debug_dir, exist_ok=True)
                debug_file = f"{debug_dir}/{site_name.replace(' ', '_')}_mybonus.html"
                with open(debug_file, "w", encoding="utf-8") as f:
                    f.write(res.text)
                logger.info(f"{site_name} HTML 已保存到: {debug_file}")
            except Exception as e:
                logger.debug(f"保存调试文件失败: {e}")

            # 调试：输出 HTML 关键部分
            if bonus_info and not bonus_info.get("available_items"):
                # 输出前 2000 字符用于调试
                logger.debug(f"{site_name} mybonus.php HTML 前 2000 字符:\n{res.text[:2000]}")
                # 查找 form 标签
                form_match = re.search(r'<form[^>]*>.*?</form>', res.text, re.DOTALL | re.IGNORECASE)
                if form_match:
                    logger.debug(f"{site_name} 找到 form:\n{form_match.group(0)[:1500]}")
                # 查找 table 标签
                table_match = re.search(r'<table[^>]*>.*?</table>', res.text, re.DOTALL | re.IGNORECASE)
                if table_match:
                    logger.debug(f"{site_name} 找到 table:\n{table_match.group(0)[:1500]}")

            if not bonus_info:
                result["status"] = "failed"
                result["error"] = "解析魔力商店失败"
                logger.warn(f"{site_name} 解析魔力商店失败")
                self._record_circuit_break(site_name)
                return site_name, result

            result["bonus_before"] = bonus_info.get("current_bonus", 0)
            # 没有发生兑换时余额不变，跳过/失败的记录也要给出变化后的值
            result["bonus_after"] = result["bonus_before"]

            # 检查魔力值是否足够
            if bonus_info.get("current_bonus", 0) <= self._keep_balance:
                result["status"] = "skipped"
                result["error"] = (f"当前魔力值 {bonus_info.get('current_bonus')} 未超过保留值 "
                                   f"{self._keep_balance}，按设置不兑换")
                logger.info(f"{site_name} 魔力值不足，跳过")
                return site_name, result

            # 根据策略计算兑换方案
            exchange_plan = self._calculate_exchange_plan(bonus_info)

            if not exchange_plan:
                result["status"] = "skipped"
                result["error"] = self._diagnose_no_plan(bonus_info)
                logger.info(f"{site_name} 无可用兑换方案: {result['error']}")
                return site_name, result

            # 执行兑换
            running_bonus = bonus_info.get("current_bonus", 0) or 0
            for item in exchange_plan:
                # 检查项目是否可用
                if not item.get("available", True):
                    logger.info(f"{site_name} 跳过不可用项目: {item.get('name')}")
                    continue
                
                # 如果是API模式，使用API兑换
                bonus_of_item = running_bonus
                if is_api_mode:
                    exchange_result = self._exchange_api_item(
                        req_utils=req_utils,
                        site_url=site_url,
                        item_id=item.get("option"),
                        item_name=item.get("name"),
                        cost=item.get("cost")
                    )
                else:
                    exchange_result = adapter.exchange_item(
                        req_utils=req_utils,
                        bonus_url=bonus_url,
                        item_id=item.get("option"),
                        item_name=item.get("name"),
                        cost=item.get("cost"),
                        bonus_before=bonus_of_item
                    )

                if exchange_result.get("success"):
                    result["exchanged"].append({
                        "item": item.get("name"),
                        "cost": item.get("cost"),
                        "gain": item.get("gain", ""),
                    })
                    result["attempts"].append({
                        "item": item.get("name"),
                        "cost": item.get("cost"),
                        "success": True,
                        "detail": (f"扣除 {exchange_result.get('deduct', item.get('cost'))}，"
                                   f"余额 {bonus_of_item} → {exchange_result.get('bonus_after') or '未知'}")
                    })
                    logger.info(f"{site_name} 兑换成功: {item.get('name')} (消耗: {item.get('cost')})")
                    # 用站点确认后的余额继续比对下一笔，取不到就按消耗推算
                    observed_bonus = exchange_result.get("bonus_after")
                    running_bonus = observed_bonus if observed_bonus else running_bonus - (item.get("cost") or 0)
                    # 限速
                    time.sleep(self._rate_limit_ms / 1000)
                else:
                    fail_detail = (f"{exchange_result.get('error')}（余额 {bonus_of_item} → "
                                   f"{exchange_result.get('bonus_after') or '未知'}）")
                    result["attempts"].append({
                        "item": item.get("name"),
                        "cost": item.get("cost"),
                        "success": False,
                        "detail": fail_detail
                    })
                    logger.warn(f"{site_name} 兑换 {item.get('name')} 失败: {fail_detail}，停止本站本轮兑换")
                    self._record_circuit_break(site_name)
                    if result["exchanged"]:
                        result["note"] = f"成功 {len(result['exchanged'])} 笔后停止：{fail_detail}"
                    else:
                        result["error"] = f"{item.get('name')} 兑换失败：{fail_detail}"
                    break

            # 重新获取魔力值
            res = req_utils.get_res(url=bonus_url)
            if res and res.status_code == 200:
                if is_api_mode:
                    new_bonus_info = self._parse_api_bonus_page(req_utils, site_url, res.text)
                else:
                    new_bonus_info = adapter.parse_bonus_page(res.text)
                if new_bonus_info:
                    result["bonus_after"] = new_bonus_info.get("current_bonus", 0)

            result["status"] = "success" if result["exchanged"] else ("failed" if result.get("error") else "skipped")

        except Exception as e:
            result["status"] = "failed"
            result["error"] = str(e)
            logger.error(f"{site_name} 兑换异常: {str(e)}")
            traceback.print_exc()
            self._record_circuit_break(site_name)

        return site_name, result

    def _calculate_exchange_plan(self, bonus_info: dict) -> List[dict]:
        """
        根据策略计算兑换方案。

        :param bonus_info: 魔力商店信息
        :return: 兑换方案列表
        """
        current_bonus = bonus_info.get("current_bonus", 0)
        available_items = bonus_info.get("available_items", [])

        if not available_items:
            return []

        plan = []

        if self._strategy == "maximize":
            # 最大化策略
            plan = self._maximize_strategy(available_items, current_bonus)
        else:
            # 保留余额策略（默认）
            plan = self._keep_balance_strategy(available_items, current_bonus)

        return plan

    def _diagnose_no_plan(self, bonus_info: dict) -> str:
        """
        说明为什么算不出兑换方案，让记录里能直接看到原因而不是一句「无可用兑换方案」。

        :param bonus_info: 魔力商店信息
        :return: 原因描述
        """
        items = bonus_info.get("available_items") or []
        if not items:
            return "魔力商店没有解析到兑换项（站点商店结构不被适配器支持，或 Cookie 已失效取到的是登录页）"

        type_names = {"upload": "上传量", "download": "下载量", "both": "上传量/下载量"}
        type_name = type_names.get(self._exchange_type, self._exchange_type)
        traffic_items = self._filter_traffic_items(items)
        if not traffic_items:
            return (f"商店里没有「{type_name}」类档位，当前兑换类型设置为 {self._exchange_type}\n"
                    f"可改为「上传量/下载量」或换档位")

        usable_items = [item for item in traffic_items if item.get("available", True)]
        if not usable_items:
            tiers = "、".join([f"{item.get('name')}({item.get('cost')})" for item in traffic_items[:3]])
            return (f"「{type_name}」档位全部被站点禁用（兑换按钮不可点，通常是分享率已达标、等级或次数受限）\n"
                    f"禁用档位: {tiers}")

        min_cost = min(item.get("cost", 0) for item in usable_items)
        current_bonus = bonus_info.get("current_bonus", 0) or 0
        if self._strategy == "keep_balance":
            return (f"可用魔力值不足：余额 {current_bonus} - 保留值 {self._keep_balance} = "
                    f"{round(current_bonus - self._keep_balance, 2)}，低于最低档位消耗 {min_cost}")
        return f"最低档位消耗 {min_cost}，超过当前余额 {current_bonus}"

    # 列表文本交给浏览器按容器宽度自动换行，覆盖 Vuetify 默认的单行截断样式
    _WRAP_STYLE = ("display:block;white-space:normal;overflow:visible;"
                   "text-overflow:clip;-webkit-line-clamp:unset")

    def _filter_traffic_items(self, items: List[dict]) -> List[dict]:
        """根据兑换类型筛选流量项目。"""
        filtered = []
        for item in items:
            name = item.get("name", "").lower()
            is_upload = "上传" in name or "upload" in name
            is_download = "下载" in name or "download" in name
            
            if self._exchange_type == "upload" and is_upload:
                filtered.append(item)
            elif self._exchange_type == "download" and is_download:
                filtered.append(item)
            elif self._exchange_type == "both" and (is_upload or is_download):
                filtered.append(item)
        
        return filtered

    def _maximize_strategy(self, items: List[dict], current_bonus: float) -> List[dict]:
        """最大化兑换策略：重复兑换性价比最高的档位直到魔力值不足。"""
        plan = []
        remaining_bonus = current_bonus

        if remaining_bonus <= 0:
            return []

        # 只选择可用的项目
        available_items = [i for i in items if i.get("available", True)]
        
        # 根据兑换类型筛选
        traffic_items = self._filter_traffic_items(available_items)

        if not traffic_items:
            return []

        # 按性价比排序，性价比相同时优先选最大档位（消耗高的）
        sorted_items = sorted(traffic_items, key=lambda x: (x.get("ratio", 0), x.get("cost", 0)), reverse=True)
        
        # 找到成本不超过当前魔力值的最高性价比档位
        best_item = None
        for item in sorted_items:
            if item.get("cost", float("inf")) <= remaining_bonus:
                best_item = item
                break
        
        if not best_item:
            return []

        # 重复兑换性价比最高的可用档位
        while remaining_bonus >= best_item.get("cost", float("inf")):
            plan.append(best_item)
            remaining_bonus -= best_item.get("cost", 0)

        return plan

    def _keep_balance_strategy(self, items: List[dict], current_bonus: float) -> List[dict]:
        """保留余额策略：扣除保留值后，重复兑换性价比最高的档位。"""
        plan = []
        available_bonus = current_bonus - self._keep_balance

        if available_bonus <= 0:
            return []

        # 只选择可用的项目
        available_items = [i for i in items if i.get("available", True)]
        
        # 根据兑换类型筛选
        traffic_items = self._filter_traffic_items(available_items)

        if not traffic_items:
            return []

        # 按性价比排序，性价比相同时优先选最大档位（消耗高的）
        sorted_items = sorted(traffic_items, key=lambda x: (x.get("ratio", 0), x.get("cost", 0)), reverse=True)

        # 找到成本不超过可用魔力值的最高性价比档位
        best_item = None
        for item in sorted_items:
            if item.get("cost", float("inf")) <= available_bonus:
                best_item = item
                break
        
        if not best_item:
            return []

        # 重复兑换性价比最高的可用档位
        while available_bonus >= best_item.get("cost", float("inf")):
            plan.append(best_item)
            available_bonus -= best_item.get("cost", 0)

        return plan

    def _parse_api_bonus_page(self, req_utils: RequestUtils, site_url: str, html: str) -> Optional[dict]:
        """
        解析API模式的魔力商店页面（如青蛙站点）。

        :param req_utils: 请求工具
        :param site_url: 站点URL
        :param html: 页面 HTML 内容
        :return: 解析结果字典
        """
        if not html:
            return None

        result = {
            "current_bonus": 0,
            "available_items": [],
        }

        try:
            # 解析当前魔力值
            bonus_patterns = [
                r'(?:使用|详情)[^]]*]：\s*([\d][\d,.]*\d)',
                r'(?:使用|详情)[^]]*]:\s*([\d][\d,.]*\d)',
                r'当前([\d][\d,.]*\d)',
                r'qingwa-bonus[^>]*>([\d][\d,.]*\d)<',
                r'icon-bean-orange[^<]*<[^>]*>([\d][\d,.]*\d)<',
            ]
            for pattern in bonus_patterns:
                bonus_match = re.search(pattern, html, re.IGNORECASE)
                if bonus_match:
                    bonus_str = bonus_match.group(1).replace(",", "")
                    try:
                        result["current_bonus"] = float(bonus_str)
                    except ValueError:
                        pass
                    break

            # 通过API获取兑换项目列表
            api_url = urljoin(site_url, "api/bonus-shop/getItems")
            logger.info(f"请求API: {api_url}")
            
            res = req_utils.get_res(api_url)
            if not res or res.status_code != 200:
                logger.warn(f"API请求失败: {res.status_code if res else 'None'}")
                return result

            # 解析JSON响应
            import json
            try:
                items_data = json.loads(res.text)
                logger.info(f"API返回 {len(items_data)} 个项目")
            except json.JSONDecodeError as e:
                logger.error(f"JSON解析失败: {e}")
                return result

            # 转换API数据为统一格式
            items = []
            for item in items_data:
                item_id = item.get("id")
                item_name = item.get("name", "")
                cost = item.get("a_amount", 0)  # 消耗的蝌蚪/魔力值
                b_type = item.get("b_type", "")  # uploaded 或 downloaded
                b_amount = item.get("b_amount", 0)  # 获得的字节数

                # 计算性价比
                ratio = 0
                if b_type == "uploaded" and b_amount > 0:
                    gb = b_amount / (1024 * 1024 * 1024)  # 转换为GB
                    ratio = gb / cost if cost > 0 else 0
                elif b_type == "downloaded" and b_amount > 0:
                    gb = b_amount / (1024 * 1024 * 1024)
                    ratio = gb / cost * 0.5 if cost > 0 else 0

                items.append({
                    "option": item_id,
                    "name": item_name,
                    "cost": cost,
                    "ratio": ratio,
                    "available": True,  # API模式默认都可用
                    "b_type": b_type,
                    "b_amount": b_amount,
                })

            result["available_items"] = items
            logger.info(f"API解析到 {len(items)} 个兑换项目，当前魔力值: {result['current_bonus']}")

        except Exception as e:
            logger.error(f"API解析魔力商店失败: {str(e)}")
            traceback.print_exc()
            return None

        return result

    def _exchange_api_item(self, req_utils: RequestUtils, site_url: str, item_id: int,
                           item_name: str, cost: float) -> dict:
        """
        执行API模式的单个兑换操作。

        :param req_utils: 请求工具
        :param site_url: 站点URL
        :param item_id: 兑换选项 ID
        :param item_name: 兑换项名称
        :param cost: 消耗魔力值
        :return: 兑换结果
        """
        try:
            logger.info(f"开始API兑换: {item_name} (消耗: {cost})")

            # POST 请求兑换
            exchange_url = urljoin(site_url, "api/bonus-shop/exchange")
            data = {
                "id": str(item_id),
                "amount": "1",  # 数量为1
            }

            res = req_utils.post_res(url=exchange_url, data=data)

            if not res:
                logger.debug(f"{item_name} API兑换请求失败: 无响应")
                return {"success": False, "error": "请求失败"}

            if res.status_code != 200:
                logger.debug(f"{item_name} API兑换请求失败: 状态码 {res.status_code}")
                return {"success": False, "error": f"状态码: {res.status_code}"}

            # 解析JSON响应
            import json
            try:
                result_data = json.loads(res.text)
            except json.JSONDecodeError as e:
                logger.error(f"JSON解析失败: {e}")
                return {"success": False, "error": "响应解析失败"}

            # 检查是否兑换成功
            if result_data.get("success"):
                logger.info(f"API兑换成功: {item_name}")
                return {"success": True}
            else:
                error_msg = result_data.get("msg", "未知错误")
                logger.warn(f"API兑换失败: {item_name} - {error_msg}")
                return {"success": False, "error": error_msg}

        except Exception as e:
            return {"success": False, "error": str(e)}

    def _check_circuit_breaker(self, site_name: str) -> bool:
        """检查站点是否触发熔断。"""
        fail_count = self._circuit_breaker.get(site_name, 0)
        return fail_count >= self._circuit_break_threshold

    def _record_circuit_break(self, site_name: str) -> None:
        """记录站点熔断计数。"""
        current = self._circuit_breaker.get(site_name, 0)
        self._circuit_breaker[site_name] = current + 1

    # 把冗长的跳过原因压成短标签，同类站点合并成一行（按顺序优先匹配）
    _SKIP_LABELS = (
        ("站点已禁用全部档位", ("全部被站点禁用",)),
        ("未达保留值", ("未超过保留值",)),
        ("商店未解析到兑换项", ("没有解析到兑换项",)),
        ("没有匹配的档位类型", ("商店里没有「",)),
        ("可用魔力值不足", ("可用魔力值不足",)),
        ("余额低于最低档位", ("最低档位消耗",)),
        ("本轮连续失败已熔断", ("熔断",)),
    )
    # 单节内最多列出的站点数，超出折叠成「等 N 个站点」
    _MAX_LIST_SITES = 8
    # 通知整体长度上限，防止极端配置把消息渠道刷屏
    _MAX_NOTIFY_LEN = 1200
    # 档位名形如 "10.0 GB上传量"，用于汇总一共兑换到多少流量
    _TRAFFIC_RE = re.compile(r"([\d.]+)\s*(KB|MB|GB|TB)\s*(上传量|下载量)")

    @staticmethod
    def _fmt_num(value: Any) -> str:
        """统一格式化魔力值：整数不留小数位，小数最多两位并去尾零，附加千分位。"""
        try:
            num = float(value or 0)
        except (TypeError, ValueError):
            return str(value)
        if num == int(num):
            return f"{int(num):,}"
        return f"{num:,.2f}".rstrip("0").rstrip(".")

    @staticmethod
    def _one_line(value: Any, limit: int = 120) -> str:
        """只取原因首行并压缩空白，多行说明留给插件页面展示。"""
        line = re.sub(r"\s+", " ", str(value or "").split("\n", 1)[0]).strip()
        return f"{line[:limit]}…" if len(line) > limit else line

    @classmethod
    def _skip_label(cls, error: Any) -> str:
        """把跳过原因归类成短标签，无法归类时保留首行短句。"""
        raw = str(error or "")
        for label, keywords in cls._SKIP_LABELS:
            if any(keyword in raw for keyword in keywords):
                return label
        return cls._one_line(raw or "本轮无兑换动作", 40)

    @staticmethod
    def _group_exchanged(exchanged: List[dict]) -> List[dict]:
        """按档位与单价聚合兑换明细，同档位多笔合并，消耗大的排前面。"""
        grouped: Dict[str, dict] = {}
        for item in exchanged or []:
            name = str(item.get("gain") or item.get("item") or "未命名档位")
            try:
                cost = float(item.get("cost") or 0)
            except (TypeError, ValueError):
                cost = 0.0
            row = grouped.setdefault(f"{name}|{cost}", {"name": name, "cost": cost, "count": 0})
            row["count"] += 1
        return sorted(grouped.values(),
                      key=lambda row: row["cost"] * row["count"],
                      reverse=True)

    @classmethod
    def _sum_traffic(cls, exchanged: List[dict]) -> str:
        """累加档位名里的流量；有一笔解析不出就不给总量，避免单位混杂造成误导。"""
        totals: Dict[Tuple[str, str], float] = {}
        for item in exchanged or []:
            name = str(item.get("gain") or item.get("item") or "")
            matched = cls._TRAFFIC_RE.search(name)
            if not matched:
                return ""
            amount, unit, kind = matched.groups()
            try:
                value = float(amount)
            except ValueError:
                return ""
            totals[(unit, kind)] = totals.get((unit, kind), 0) + value
        parts = []
        for key in sorted(totals, key=lambda item: item[1] != "上传量"):
            parts.append(f"{cls._fmt_num(totals[key])} {key[0]}{key[1]}")
        return "、".join(parts)

    def _send_notification(self, today_data: dict) -> None:
        """发送兑换结果通知：汇总行在前，已兑换逐站一行，跳过站点按原因合并。"""
        results = today_data.get("results", {})
        exchanged_lines: List[str] = []
        failed_lines: List[str] = []
        skip_groups: Dict[str, List[str]] = {}
        skip_order: List[str] = []
        total_count = 0
        total_cost = 0.0
        all_exchanged: List[dict] = []

        for site_name, site_result in results.items():
            status = site_result.get("status")
            exchanged = site_result.get("exchanged") or []

            if status == "success":
                rows = self._group_exchanged(exchanged)
                site_cost = sum(row["cost"] * row["count"] for row in rows)
                total_count += len(exchanged)
                total_cost += site_cost
                all_exchanged.extend(exchanged)
                details = "、".join(
                    f"{row['name']} ×{row['count']}" if row["count"] > 1 else row["name"]
                    for row in rows
                )
                line = (f"{site_name}：{details}，消耗 {self._fmt_num(site_cost)}，"
                        f"魔力值 {self._fmt_num(site_result.get('bonus_before'))} → "
                        f"{self._fmt_num(site_result.get('bonus_after'))}")
                note = self._one_line(site_result.get("note"), 80)
                if note:
                    line += f"；{note}"
                exchanged_lines.append(line)
            elif status == "failed":
                reason = self._one_line(site_result.get("error") or "未知原因", 100)
                failed_lines.append(f"{site_name}：{reason}")
            else:
                label = self._skip_label(site_result.get("error"))
                if label not in skip_groups:
                    skip_groups[label] = []
                    skip_order.append(label)
                # 未达保留值的站点带上余额，其余只列站名，保证一行读完
                if label == "未达保留值":
                    skip_groups[label].append(
                        f"{site_name} {self._fmt_num(site_result.get('bonus_before'))}"
                    )
                else:
                    skip_groups[label].append(site_name)

        handled = f"处理 {len(results)} 个站点"
        if total_count:
            summary = f"兑换 {total_count} 笔，消耗 {self._fmt_num(total_cost)} 魔力值"
            traffic = self._sum_traffic(all_exchanged)
            if traffic:
                summary += f"，获得 {traffic}"
            failed_count = sum(1 for r in results.values() if r.get("status") == "failed")
            summary += f"（{handled}"
            if failed_count:
                summary += f"，失败 {failed_count} 个站点"
            summary += "）"
        else:
            summary = f"本轮没有兑换动作（{handled}）"

        sections: List[List[str]] = [[summary]]
        if exchanged_lines:
            sections.append(["【已兑换】"] + exchanged_lines)
        if failed_lines:
            sections.append(["【失败】"] + failed_lines)
        if skip_order:
            section = ["【未兑换】"]
            for label in skip_order:
                names = skip_groups[label]
                shown = "、".join(names[:self._MAX_LIST_SITES])
                if len(names) > self._MAX_LIST_SITES:
                    shown += f" 等 {len(names)} 个站点"
                section.append(f"{label}：{shown}")
            sections.append(section)

        message = "\n\n".join("\n".join(section) for section in sections)
        if len(message) > self._MAX_NOTIFY_LEN:
            message = (f"{message[:self._MAX_NOTIFY_LEN].rstrip()}"
                       f"\n…（内容过长已截断，详见插件页面记录）")

        self.post_message(
            mtype=NotificationType.SiteMessage,
            title="魔力值自动兑换完成",
            text=message
        )


class NexusPHPBonusAdapter:
    """NexusPHP 魔力商店适配器。"""

    # 判定兑换真实落地所需的最小扣费比例（容忍站点同时产生的做种收益）
    _MIN_DEDUCT_RATIO = 0.5

    def parse_bonus_page(self, html: str) -> Optional[dict]:
        """
        解析魔力商店页面。

        :param html: 页面 HTML 内容
        :return: 解析结果字典
        """
        if not html:
            return None

        result = {
            "current_bonus": 0,
            "available_items": [],
        }

        try:
            # 解析当前魔力值
            # 常见格式:
            #   [使用&说明]：554689.9  (PT时间)
            #   [使用</a>]: 7,105,672.0  (红豆饭等)
            #   魔力值（当前7,005,772.0  (部分站点)
            #   蝌蚪...使用</a>]: 329,468.8  (青蛙)
            #   憨豆...<div class="text-base font-bold">17122.9</div>  (憨憨)
            bonus_patterns = [
                r'(?:使用|详情)[^]]*]：\s*([\d][\d,.]*\d)',  # ]：数字
                r'(?:使用|详情)[^]]*]:\s*([\d][\d,.]*\d)',   # ]: 数字
                r'当前([\d][\d,.]*\d)',                       # 当前数字
                r'qingwa-bonus[^>]*>([\d][\d,.]*\d)<',       # 青蛙特殊div
                r'icon-bean-orange[^<]*<[^>]*>([\d][\d,.]*\d)<',  # 憨憨特殊div
            ]
            for pattern in bonus_patterns:
                bonus_match = re.search(pattern, html, re.IGNORECASE)
                if bonus_match:
                    bonus_str = bonus_match.group(1).replace(",", "")
                    try:
                        result["current_bonus"] = float(bonus_str)
                    except ValueError:
                        pass
                    break

            # 解析可兑换项目
            # 格式1 (标准NexusPHP): <form action="?action=exchange" method="post">
            #        <input type="hidden" name="option" value="X" />
            #        <h1>项目名称</h1>
            #        <td>价格</td>
            #        <input type="submit" value="交换" />
            #       </form>
            # 格式2 (憨憨等): <form action="?action=exchange" method="post">
            #        <input type="hidden" name="option" value="X" />
            #        <div class="font-bold text-base">项目名称</div>
            #        <div class="break-all">价格</div>
            #        <input type="submit" value="交换" />
            #       </form>
            items = []

            # 查找所有 form 表单
            form_pattern = r'<form[^>]*action=["\']?\?action=exchange["\']?[^>]*>(.*?)</form>'
            forms = re.findall(form_pattern, html, re.DOTALL | re.IGNORECASE)

            for form_html in forms:
                # 提取 option value
                option_match = re.search(r'<input[^>]*name=["\']option["\'][^>]*value=["\'](\d+)["\']', form_html, re.IGNORECASE)
                if not option_match:
                    continue
                option_id = int(option_match.group(1))

                # 提取项目名称 - 先尝试 h1 标签，再尝试 div 标签
                name_match = re.search(r'<h1[^>]*>(.*?)</h1>', form_html, re.IGNORECASE | re.DOTALL)
                if not name_match:
                    # 尝试 div 标签（憨憨等站点）
                    name_match = re.search(r'<div[^>]*class=["\'][^"\']*font-bold[^"\']*["\'][^>]*>(.*?)</div>', form_html, re.IGNORECASE | re.DOTALL)
                if not name_match:
                    continue
                item_name = self._clean_html(name_match.group(1))

                # 提取价格 - 先尝试 td 标签，再尝试 div 标签
                price_match = re.search(r'<td[^>]*align=["\']?center["\']?[^>]*>([\d,.]+)</td>', form_html, re.IGNORECASE)
                if not price_match:
                    # 尝试 div 标签（憨憨等站点）
                    price_match = re.search(r'<div[^>]*class=["\'][^"\']*break-all[^"\']*["\'][^>]*>([\d,.]+)</div>', form_html, re.IGNORECASE)
                if not price_match:
                    # 尝试其他格式
                    price_match = re.search(r'(\d[\d,.]*)\s*(?:个)?魔力', form_html, re.IGNORECASE)
                if not price_match:
                    continue

                price_str = price_match.group(1).replace(",", "")
                try:
                    cost = float(price_str)
                except ValueError:
                    continue

                # 检查是否可以交换 (submit 按钮未 disabled)
                # 匹配整个 submit input 标签，包括后面的 disabled 属性
                submit_match = re.search(r'<input[^>]*type=["\']submit["\'][^>]*/?\s*>', form_html, re.IGNORECASE)
                is_available = True
                if submit_match:
                    submit_tag = submit_match.group(0)
                    if re.search(r'\bdisabled\b', submit_tag, re.IGNORECASE):
                        is_available = False

                # 计算性价比
                ratio = self._calculate_ratio(item_name, cost)

                items.append({
                    "option": option_id,
                    "name": item_name,
                    "cost": cost,
                    "ratio": ratio,
                    "available": is_available,
                })

            result["available_items"] = items
            logger.debug(f"解析到 {len(items)} 个兑换项目，当前魔力值: {result['current_bonus']}")

        except Exception as e:
            logger.error(f"解析魔力商店失败: {str(e)}")
            traceback.print_exc()
            return None

        return result

    def _clean_html(self, html: str) -> str:
        """清理 HTML 标签，提取纯文本。"""
        if not html:
            return ""
        # 移除 HTML 标签
        text = re.sub(r'<[^>]+>', ' ', html)
        # 移除多余空白
        text = re.sub(r'\s+', ' ', text)
        return text.strip()

    def _calculate_ratio(self, item_name: str, cost: float) -> float:
        """计算兑换项的性价比。"""
        if cost <= 0:
            return 0
        # 上传量兑换
        if "上传" in item_name or "upload" in item_name.lower():
            upload_match = re.search(r'(\d+(?:\.\d+)?)\s*(?:GB|GiB)', item_name, re.IGNORECASE)
            if upload_match:
                upload_gb = float(upload_match.group(1))
                return upload_gb / cost
        # 下载量兑换
        if "下载" in item_name or "download" in item_name.lower():
            download_match = re.search(r'(\d+(?:\.\d+)?)\s*(?:GB|GiB)', item_name, re.IGNORECASE)
            if download_match:
                download_gb = float(download_match.group(1))
                return download_gb / cost * 0.5  # 下载量权重较低
        return 0

    def exchange_item(self, req_utils: RequestUtils, bonus_url: str, item_id: int,
                      item_name: str, cost: float,
                      bonus_before: Optional[float] = None) -> dict:
        """
        执行单个兑换操作。

        :param req_utils: 请求工具
        :param bonus_url: 魔力商店 URL
        :param item_id: 兑换选项 ID
        :param item_name: 兑换项名称
        :param cost: 消耗魔力值
        :param bonus_before: 兑换前的魔力值，用于比对是否真实扣费
        :return: 兑换结果
        """
        try:
            logger.info(f"开始兑换: {item_name} (消耗: {cost}, 兑换前余额: {bonus_before})")

            # POST 请求兑换 - 格式: ?action=exchange, option=X
            exchange_url = f"{bonus_url}?action=exchange"
            data = {
                "option": str(item_id),
                "submit": "交换",
            }

            res = req_utils.post_res(url=exchange_url, data=data)

            if not res:
                logger.debug(f"{item_name} 兑换请求失败: 无响应")
                return {"success": False, "error": "请求失败"}

            if res.status_code != 200:
                logger.debug(f"{item_name} 兑换请求失败: 状态码 {res.status_code}")
                return {"success": False, "error": f"状态码: {res.status_code}"}

            # 调试：记录响应片段
            logger.debug(f"{item_name} 兑换响应前500字符: {res.text[:500]}")

            # 以「兑换前后余额差」判定结果：站点是否真实扣费才是唯一可靠依据
            new_bonus = self._parse_page_bonus(res.text)

            if bonus_before is not None and cost and cost > 0:
                threshold = cost * self._MIN_DEDUCT_RATIO
                deducted = bonus_before - new_bonus if new_bonus is not None else None
                if deducted is None or deducted < threshold:
                    # 响应页看不出扣费，站点可能返回跳转前的页面，重抓商店页再确认
                    for wait_seconds in (1, 3):
                        time.sleep(wait_seconds)
                        check_res = req_utils.get_res(url=bonus_url)
                        if not check_res or check_res.status_code != 200:
                            continue
                        refetched = self._parse_page_bonus(check_res.text)
                        if refetched is None:
                            continue
                        new_bonus = refetched
                        deducted = bonus_before - new_bonus
                        if deducted >= threshold:
                            break
                if deducted is not None and deducted >= threshold:
                    logger.info(f"{item_name} 兑换成功: 余额 {bonus_before} → {new_bonus}，实际扣除 {round(deducted, 2)}")
                    return {"success": True, "bonus_after": new_bonus, "deduct": round(deducted, 2)}
                error_msg = self._extract_notice(res.text) or "魔力值未减少"
                logger.warn(f"{item_name} 兑换失败: {error_msg}（余额 {bonus_before} → {new_bonus}）")
                return {"success": False, "error": error_msg, "bonus_after": new_bonus}

            # 缺少兑换前基线时退回提示文本判定；无法确认一律按失败处理，避免重复提交
            notice = self._extract_notice(res.text)
            if notice:
                logger.warn(f"{item_name} 兑换失败: {notice}")
                return {"success": False, "error": notice, "bonus_after": new_bonus}
            for keyword in ["兑换成功", "交易成功", "交换成功", "成功", "success"]:
                if keyword.lower() in res.text.lower():
                    logger.info(f"{item_name} 兑换成功: 命中站点提示关键字 {keyword}")
                    return {"success": True, "bonus_after": new_bonus}
            logger.warn(f"{item_name} 无法确认兑换结果，按失败处理以避免重复提交")
            return {"success": False, "error": "无法确认兑换结果", "bonus_after": new_bonus}

        except Exception as e:
            return {"success": False, "error": str(e)}

    def _parse_page_bonus(self, html: str) -> Optional[float]:
        """
        从魔力商店页面解析当前魔力值。

        :param html: 页面 HTML 内容
        :return: 解析到的魔力值，解析不到返回 None
        """
        if not html:
            return None
        bonus_patterns = [
            r'(?:使用|详情)[^]]*]：\s*([\d][\d,.]*\d)',
            r'(?:使用|详情)[^]]*]:\s*([\d][\d,.]*\d)',
            r'当前([\d][\d,.]*\d)',
            r'魔力值[^:：]*[：:]\s*([\d][\d,.]*\d)',
            r'qingwa-bonus[^>]*>([\d][\d,.]*\d)<',
            r'icon-bean-orange[^<]*<[^>]*>([\d][\d,.]*\d)<',
        ]
        for pattern in bonus_patterns:
            bonus_match = re.search(pattern, html, re.IGNORECASE)
            if not bonus_match:
                continue
            try:
                return float(bonus_match.group(1).replace(",", ""))
            except ValueError:
                continue
        return None

    def _extract_notice(self, html: str) -> Optional[str]:
        """
        从站点提示块里提取失败原因，避免把商店页的固定说明文字当成兑换失败。
        """
        if not html:
            return None
        blocks = re.findall(
            r'<div[^>]*class=["\'][^"\']*(?:std|message|error|warning|tip)[^"\']*["\'][^>]*>(.*?)</div>',
            html, re.DOTALL | re.IGNORECASE)
        notice = " ".join(self._clean_html(block) for block in blocks)
        notice = re.sub(r'\s+', ' ', notice).strip()
        if not notice:
            return None
        if re.search(r'(失败|不足|不允许|无法|不能|已很高|权限|等级不够|用完|超过|限制|无效)', notice, re.IGNORECASE):
            return notice[:120]
        return None

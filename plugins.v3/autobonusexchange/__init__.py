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
    plugin_name = "魔力值自动兑换💻"
    # 插件描述
    plugin_desc = "读取 MoviePilot 已配置站点，按策略自动兑换上传/下载量。(不支持部分站点)"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/andyxu8023/MoviePilot-Plugins/main/icons/AutoBonusExchange.png"
    # 插件版本
    plugin_version = "1.0.0"
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
                                    "component": "VTextField",
                                    "props": {
                                        "model": "cron",
                                        "label": "定时周期",
                                        "placeholder": "0 3 * * * 或 2.5/9-23"
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
                                            {"title": "固定数量", "value": "fixed"},
                                            {"title": "最大化兑换", "value": "maximize"},
                                            {"title": "保留余额", "value": "keep_balance"},
                                            {"title": "分享率优先", "value": "ratio_priority"}
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
        for site_name, result in today_data.get("results", {}).items():
            status = result.get("status", "unknown")
            bonus_before = result.get("bonus_before", 0)
            bonus_after = result.get("bonus_after", 0)
            exchanged = result.get("exchanged", [])

            status_text = "成功" if status == "success" else "失败" if status == "failed" else "跳过"
            status_color = "success" if status == "success" else "error" if status == "failed" else "warning"

            items.append({
                "component": "VListItem",
                "props": {"title": site_name},
                "content": [
                    {
                        "component": "VListItemTitle",
                        "content": [{
                            "component": "VChip",
                            "props": {"color": status_color, "size": "small"},
                            "text": status_text
                        }]
                    },
                    {
                        "component": "VListItemSubtitle",
                        "text": f"魔力值: {bonus_before} → {bonus_after}"
                    }
                ]
            })

            # 兑换详情
            for ex in exchanged:
                items.append({
                    "component": "VListItem",
                    "props": {"title": f"  - {ex.get('item', '')}"},
                    "content": [{
                        "component": "VListItemSubtitle",
                        "text": f"消耗: {ex.get('cost', 0)}, 获得: {ex.get('gain', '')}"
                    }]
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
        }

        try:
            # 检查熔断器
            if self._check_circuit_breaker(site_name):
                result["status"] = "skipped"
                result["error"] = "站点已触发熔断，跳过"
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

            # 判断认证方式
            auth_mode = "cookie"
            if site_token:
                auth_mode = "token"
            elif site_apikey:
                auth_mode = "apikey"
            elif not site_cookie:
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
            
            # 如果 mybonus.php 返回404或内容不对，尝试 bonusshop.php
            if not res or res.status_code == 404 or (res.status_code == 200 and "mybonus" not in res.text.lower() and "bonusshop" not in res.text.lower() and "exchange" not in res.text.lower()):
                alt_url = urljoin(site_url, "bonusshop.php")
                logger.info(f"{site_name} 尝试备用URL: {alt_url}")
                res = req_utils.get_res(url=alt_url)
                if res and res.status_code == 200:
                    bonus_url = alt_url
            if not res or res.status_code != 200:
                result["status"] = "failed"
                result["error"] = f"获取魔力商店失败，状态码: {res.status_code if res else 'None'}"
                logger.warn(f"{site_name} 获取魔力商店失败")
                self._record_circuit_break(site_name)
                return site_name, result

            # 解析魔力商店
            adapter = NexusPHPBonusAdapter()
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

            # 检查魔力值是否足够
            if bonus_info.get("current_bonus", 0) <= self._keep_balance:
                result["status"] = "skipped"
                result["error"] = f"当前魔力值 {bonus_info.get('current_bonus')} 不足或低于保留值 {self._keep_balance}"
                logger.info(f"{site_name} 魔力值不足，跳过")
                return site_name, result

            # 根据策略计算兑换方案
            exchange_plan = self._calculate_exchange_plan(bonus_info)

            if not exchange_plan:
                result["status"] = "skipped"
                result["error"] = "无可用兑换方案"
                logger.info(f"{site_name} 无可用兑换方案")
                return site_name, result

            # 执行兑换
            for item in exchange_plan:
                exchange_result = adapter.exchange_item(
                    req_utils=req_utils,
                    bonus_url=bonus_url,
                    item_id=item.get("option"),
                    item_name=item.get("name"),
                    cost=item.get("cost")
                )

                if exchange_result.get("success"):
                    result["exchanged"].append({
                        "item": item.get("name"),
                        "cost": item.get("cost"),
                        "gain": item.get("gain", ""),
                    })
                    logger.info(f"{site_name} 兑换成功: {item.get('name')} (消耗: {item.get('cost')})")
                    # 限速
                    time.sleep(self._rate_limit_ms / 1000)
                else:
                    logger.warn(f"{site_name} 兑换 {item.get('name')} 失败: {exchange_result.get('error')}")
                    self._record_circuit_break(site_name)

            # 重新获取魔力值
            res = req_utils.get_res(url=bonus_url)
            if res and res.status_code == 200:
                new_bonus_info = adapter.parse_bonus_page(res.text)
                if new_bonus_info:
                    result["bonus_after"] = new_bonus_info.get("current_bonus", 0)

            result["status"] = "success" if result["exchanged"] else "skipped"

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

        if self._strategy == "fixed":
            # 固定数量策略
            plan = self._fixed_strategy(available_items, current_bonus)
        elif self._strategy == "maximize":
            # 最大化策略
            plan = self._maximize_strategy(available_items, current_bonus)
        elif self._strategy == "keep_balance":
            # 保留余额策略
            plan = self._keep_balance_strategy(available_items, current_bonus)
        elif self._strategy == "ratio_priority":
            # 分享率优先策略
            plan = self._ratio_priority_strategy(available_items, current_bonus)

        return plan

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

    def _fixed_strategy(self, items: List[dict], current_bonus: float) -> List[dict]:
        """固定数量兑换策略：先各档位一次，再用剩余魔力值重复兑换性价比最高的档位。"""
        plan = []
        remaining_bonus = current_bonus - self._keep_balance

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

        # 第一轮：每个档位兑换一次
        for item in sorted_items:
            if remaining_bonus >= item.get("cost", float("inf")):
                plan.append(item)
                remaining_bonus -= item.get("cost", 0)

        # 第二轮：用剩余魔力值重复兑换性价比最高的档位
        best_item = sorted_items[0]  # 性价比最高，同等性价比时消耗最大
        while remaining_bonus >= best_item.get("cost", float("inf")):
            plan.append(best_item)
            remaining_bonus -= best_item.get("cost", 0)

        return plan

    def _maximize_strategy(self, items: List[dict], current_bonus: float) -> List[dict]:
        """最大化兑换策略：重复兑换性价比最高的档位直到魔力值不足。"""
        plan = []
        remaining_bonus = current_bonus - self._keep_balance

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
        
        # 重复兑换性价比最高的档位
        best_item = sorted_items[0]
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

        # 重复兑换性价比最高的档位
        best_item = sorted_items[0]
        while available_bonus >= best_item.get("cost", float("inf")):
            plan.append(best_item)
            available_bonus -= best_item.get("cost", 0)

        return plan

    def _ratio_priority_strategy(self, items: List[dict], current_bonus: float) -> List[dict]:
        """分享率优先策略：优先兑换上传量，重复兑换性价比最高的档位。"""
        plan = []
        remaining_bonus = current_bonus - self._keep_balance

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

        # 找到第一个可用的档位（available=True）
        best_item = None
        for item in sorted_items:
            if item.get("available", False):
                best_item = item
                break
        
        if not best_item:
            return []

        # 重复兑换性价比最高的可用档位
        while remaining_bonus >= best_item.get("cost", float("inf")):
            plan.append(best_item)
            remaining_bonus -= best_item.get("cost", 0)

        return plan

    def _check_circuit_breaker(self, site_name: str) -> bool:
        """检查站点是否触发熔断。"""
        fail_count = self._circuit_breaker.get(site_name, 0)
        return fail_count >= self._circuit_break_threshold

    def _record_circuit_break(self, site_name: str) -> None:
        """记录站点熔断计数。"""
        current = self._circuit_breaker.get(site_name, 0)
        self._circuit_breaker[site_name] = current + 1

    def _send_notification(self, today_data: dict) -> None:
        """发送兑换结果通知。"""
        results = today_data.get("results", {})
        success_count = sum(1 for r in results.values() if r.get("status") == "success")
        failed_count = sum(1 for r in results.values() if r.get("status") == "failed")
        skipped_count = sum(1 for r in results.values() if r.get("status") == "skipped")

        message = f"成功: {success_count}, 失败: {failed_count}, 跳过: {skipped_count}"

        self.post_message(
            mtype=NotificationType.SiteMessage,
            title="魔力值自动兑换完成",
            text=message
        )


class NexusPHPBonusAdapter:
    """NexusPHP 魔力商店适配器。"""

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
            bonus_patterns = [
                r'(?:使用|详情)[^]]*]：\s*([\d][\d,.]*\d)',  # ]：数字
                r'(?:使用|详情)[^]]*]:\s*([\d][\d,.]*\d)',   # ]: 数字
                r'当前([\d][\d,.]*\d)',                       # 当前数字
                r'qingwa-bonus[^>]*>([\d][\d,.]*\d)<',       # 青蛙特殊div
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
            # 格式: <form action="?action=exchange" method="post">
            #        <input type="hidden" name="option" value="X" />
            #        <h1>项目名称</h1>
            #        <td>价格</td>
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

                # 提取项目名称 (在 h1 标签中，可能有class属性)
                name_match = re.search(r'<h1[^>]*>(.*?)</h1>', form_html, re.IGNORECASE | re.DOTALL)
                if not name_match:
                    continue
                item_name = self._clean_html(name_match.group(1))

                # 提取价格 (在第三个 td 中，或包含数字的 td)
                price_match = re.search(r'<td[^>]*align=["\']?center["\']?[^>]*>([\d,.]+)</td>', form_html, re.IGNORECASE)
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
                # 匹配 disabled, disabled="disabled", disabled='disabled' 等格式
                submit_match = re.search(r'<input[^>]*type=["\']submit["\'][^>]*value=["\']([^"\']+)["\']', form_html, re.IGNORECASE)
                is_available = True
                if submit_match:
                    # 检查整个 submit input 标签是否包含 disabled 属性
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
                      item_name: str, cost: float) -> dict:
        """
        执行单个兑换操作。

        :param req_utils: 请求工具
        :param bonus_url: 魔力商店 URL
        :param item_id: 兑换选项 ID
        :param item_name: 兑换项名称
        :param cost: 消耗魔力值
        :return: 兑换结果
        """
        try:
            logger.info(f"开始兑换: {item_name} (消耗: {cost})")

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

            # 检查是否兑换成功
            if "成功" in res.text or "success" in res.text.lower() or "兑换完成" in res.text:
                logger.info(f"兑换成功: {item_name}")
                return {"success": True}

            # 检查是否有错误提示
            if "失败" in res.text or "error" in res.text.lower() or "不足" in res.text:
                error_match = re.search(r'(?:错误|失败|Error)[：:]\s*([^<]+)', res.text)
                error_msg = error_match.group(1).strip() if error_match else "未知错误"
                return {"success": False, "error": error_msg}

            # 默认认为成功（NexusPHP 通常返回原页面）
            logger.debug(f"{item_name} 未检测到明确成功/失败标识，默认认为成功")
            return {"success": True}

        except Exception as e:
            return {"success": False, "error": str(e)}

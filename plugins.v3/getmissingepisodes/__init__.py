from pathlib import Path
from threading import Event, Lock
from threading import Thread
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import datetime
import pytz
import re
import time
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple, TypedDict
from urllib.parse import quote

from app.chain.tmdb import TmdbChain
from app.schemas.types import MediaType, MediaSource
from app import schemas
from app.chain.media import MediaChain
from app.chain.subscribe import SubscribeChain
from app.db.subscribe_oper import SubscribeOper
from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase
from app.chain.mediaserver import MediaServerChain
from app.helper.mediaserver import MediaServerHelper


class HistoryStatus(Enum):
    UNKNOW = "未知状态"
    ALL_EXIST = "全部存在"
    ADDED_RSS = "已加订阅"
    NO_EXIST = "存在缺失"
    FAILED = "获取失败"
    DELETED = "已删除"


class HistoryDataType(Enum):
    ALL_EXIST = "全部存在"
    ADDED_RSS = "已加订阅"
    NO_EXIST = "存在缺失"
    FAILED = "失败记录"
    ALL = "所有记录"
    LATEST = "最近记录"
    NOT_ALL_NO_EXIST = "已有季缺失"
    SKIPPED = "已跳过记录"
    FINISHED = "已完结"
    DELETED = "已删除记录"


class NoExistAction(Enum):
    ONLY_HISTORY = "仅检查记录"
    ADD_SUBSCRIBE = "添加到订阅"
    SET_ALL_EXIST = "标记为存在"


class Icons(Enum):
    STATISTICS = "icon_statistics"
    WARNING = "icon_warning"
    BUG_REMOVE = "icon_bug_remove"
    GLASSES = "icon_3d_glasses"
    ADD_SCHEDULE = "icon_add_schedule"
    TARGET = "icon_target"
    SKIP = "icon_skip"
    RECENT = "icon_recent"
    FINISHED = "icon_finished"
    DELETED = "icon_deleted"


class GetMissingEpisodesInfo(TypedDict, total=False):
    season: Optional[int]
    episode_no_exist: Optional[List[int]]
    episode_total: int  # 筛选后的总集数（用于检查）
    episode_total_unfiltered: int  # 实际总集数（用于订阅）


class TvNoExistInfo(TypedDict):
    title: str
    year: str
    path: str
    tmdbid: int
    poster_path: str
    backdrop_path: str                # TMDB 横版背景图，卡片背景用；缺失时卡片保持纯色底
    vote_average: float | str
    last_air_date: str
    season_episode_no_exist_info: Dict[str, GetMissingEpisodesInfo]
    status: str
    status_cn: str
    existing_seasons: Dict[str, List[int]]   # 媒体库里实际存在的季→集，归档后作为追新基线
    media_item_id: Optional[str]             # 媒体服务器条目ID，归档复核用


default_poster_path = "/assets/no-image-CweBJ8Ee.jpeg"


def create_tv_no_exist_info(
    title="未知",
    year="未知",
    path="未知",
    last_air_date="未知",
    tmdbid=0,
    vote_average=0.0,
    poster_path=default_poster_path,
    backdrop_path: str = "",
    season_episode_no_exist_info: Optional[Dict[str, GetMissingEpisodesInfo]] = None,
    status: str = "Unknown",
    status_cn: str = "未知",
    existing_seasons: Optional[Dict[str, List[int]]] = None,
    media_item_id: Optional[str] = None,
) -> TvNoExistInfo:
    logger.debug(f"season_episode_no_exist_info: {season_episode_no_exist_info}")
    return TvNoExistInfo(
        title=title,
        year=year,
        path=path,
        tmdbid=tmdbid,
        poster_path=poster_path,
        backdrop_path=backdrop_path,
        vote_average=vote_average,
        last_air_date=last_air_date,
        season_episode_no_exist_info=season_episode_no_exist_info or {},
        status=status,
        status_cn=status_cn,
        existing_seasons=existing_seasons or {},
        media_item_id=media_item_id,
    )


def merge_missing_seasons_into_ignored(
    base_ignored: Optional[List[int]],
    tv_no_exist_info: Optional[TvNoExistInfo | Dict[str, Any]],
) -> List[int]:
    """把缺失快照中的季并入忽略季列表，返回合并去重后的季号。

    被忽略的季在后续检测中整季跳过，用于表达“这些季不再需要补全”。
    手动「标记存在」与自动「缺失处理方式=标记为存在」共用这一份语义。
    """
    ignored: List[int] = []
    for raw_season in base_ignored or []:
        try:
            season = int(raw_season)
        except (TypeError, ValueError):
            logger.warning(f"忽略季列表存在非法季号 {raw_season}，已丢弃")
            continue
        if season not in ignored:
            ignored.append(season)

    for season_key in ((tv_no_exist_info or {}).get("season_episode_no_exist_info") or {}).keys():
        try:
            season = int(season_key)
        except (TypeError, ValueError):
            logger.warning(f"季号 {season_key} 无法转换为整数，跳过忽略标记")
            continue
        if season not in ignored:
            ignored.append(season)

    return ignored


def split_missing_seasons(
    tv_no_exist_info: Optional[TvNoExistInfo | Dict[str, Any]],
    requested: Optional[str],
) -> Tuple[List[int], List[int]]:
    """把缺失快照按本次选择拆成（要标记的季，仍保持缺失的季）。

    ``requested`` 为逗号分隔的季号；留空表示标记全部缺失季，兼容旧的整条标记入口。
    只接受确实处于缺失快照中的季号，避免把没检测到的季误写进忽略列表。
    """
    missing: List[int] = []
    for season_key in ((tv_no_exist_info or {}).get("season_episode_no_exist_info") or {}).keys():
        try:
            season = int(season_key)
        except (TypeError, ValueError):
            logger.warning(f"季号 {season_key} 无法转换为整数，跳过拆分")
            continue
        if season not in missing:
            missing.append(season)
    missing.sort()

    if requested is None or not str(requested).strip():
        return missing, []

    wanted: set[int] = set()
    for part in str(requested).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            wanted.add(int(part))
        except ValueError:
            logger.warning(f"待标记季号 {part} 非法，已忽略")
    selected = [season for season in missing if season in wanted]
    remaining = [season for season in missing if season not in wanted]
    return selected, remaining


def apply_missing_season_mark(
    historys: Dict[str, Any],
    unique: str,
    requested: Optional[str],
    now_str: str,
) -> Tuple[bool, str]:
    """把选中的缺失季标记为已存在：写入忽略季列表，并从缺失快照移除这些季。

    未选中的季原样留在快照里，下一轮扫描仍按缺失处理并继续补全；
    全部缺失季都被标记时才把状态改成「全部存在」，否则保持「存在缺失」。
    纯数据操作，不碰存储与锁，便于离线单测。返回 (是否成功, 提示信息)。
    """
    record = (historys.get("details") or {}).get(unique)
    if not record:
        return False, "记录不存在"

    tv_info = record.get("tv_no_exist_info") or {}
    selected, remaining = split_missing_seasons(tv_info, requested)
    if not selected:
        return False, "没有可标记的缺失季"

    record["ignored_seasons"] = merge_season_numbers(
        record.get("ignored_seasons") or [], selected
    )
    missing_map = tv_info.get("season_episode_no_exist_info") or {}
    for season in selected:
        missing_map.pop(str(season), None)
    tv_info["season_episode_no_exist_info"] = missing_map
    record["tv_no_exist_info"] = tv_info
    record["last_status_change"] = now_str

    selected_labels = "、".join(f"S{season}" for season in selected)
    if remaining:
        record["exist_status"] = HistoryStatus.NO_EXIST.value
        remaining_labels = "、".join(f"S{season}" for season in remaining)
        return True, f"已标记 {selected_labels} 为已存在，{remaining_labels} 仍按缺失处理"

    record["exist_status"] = HistoryStatus.ALL_EXIST.value
    return True, f"已标记 {selected_labels} 为已存在"


def parse_season_numbers(requested: Optional[str]) -> List[int]:
    """把逗号分隔的季号串解析成去重升序的整数列表；非法项静默跳过。"""
    result: set = set()
    for part in str(requested or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.add(int(part))
        except (TypeError, ValueError):
            continue
    return sorted(result)


def apply_ignored_season_unmark(
    historys: Dict[str, Any],
    unique: str,
    requested: Optional[str],
    now_str: str,
) -> Tuple[bool, str]:
    """撤销「已忽略季」：把选中的季从忽略列表移出，下一轮检测重新评估这些季。

    ``requested`` 为逗号分隔季号；留空表示撤销该记录全部已忽略季。
    与标记存在互为逆操作；缺失快照在标记时已被裁剪，因此撤销后需要
    靠下一轮扫描重新算出缺失，本函数不猜测缺失集数。
    纯数据操作，不碰存储与锁，便于离线单测。返回 (是否成功, 提示信息)。
    """
    record = (historys.get("details") or {}).get(unique)
    if not record:
        return False, "记录不存在"

    ignored = [season for season in (record.get("ignored_seasons") or []) if season is not None]
    if not ignored:
        return False, "该记录没有已忽略的季"

    selected = parse_season_numbers(requested)
    if not selected:
        # 未指定季号 = 撤销全部
        selected = sorted({int(season) for season in ignored})

    known = {int(season) for season in ignored}
    removed = [season for season in selected if season in known]
    if not removed:
        labels = "、".join(f"S{season}" for season in selected)
        return False, f"{labels} 不在该记录的忽略季中"

    record["ignored_seasons"] = sorted(known - {int(season) for season in removed})
    record["last_status_change"] = now_str

    labels = "、".join(f"S{season}" for season in removed)
    left = "，仍忽略 " + "、".join(f"S{season}" for season in record["ignored_seasons"]) \
        if record["ignored_seasons"] else ""
    return True, f"已撤销 {labels} 的忽略标记{left}，下一轮检测将重新评估这些季"


def merge_season_numbers(*groups: Optional[List[Any]]) -> List[int]:
    """合并多组季号并去重，保持先出现的顺序。

    用于剧集重新入库时把归档记录里的用户忽略意愿并回在库记录。
    """
    merged: List[int] = []
    for group in groups:
        for raw_season in group or []:
            try:
                season = int(raw_season)
            except (TypeError, ValueError):
                logger.warning(f"季号 {raw_season} 无法转换为整数，已丢弃")
                continue
            if season not in merged:
                merged.append(season)
    return merged


# 常规检测写回记录时必须原样保留的归档与追新字段
PERSISTENT_RECORD_FIELDS = (
    "mediaserver",
    "library",
    "deleted",
    "deleted_at",
    "last_seen",
    "delete_source",
    "follow",
    "follow_check_time",
    "follow_note",
    "action_override",
)


class ArchiveAction(Enum):
    """扫描结束后对单条历史记录做出的归档迁移决策。"""

    KEEP = "保持原样"
    UNARCHIVE = "撤销归档"
    MERGE_LIVE = "归档并回在库"
    SKIP_UNSCANNED = "服务器未扫完暂不判定"
    DROP_RENAMED = "清理换ID旧记录"
    NEED_CONFIRM = "待复核后归档"
    STAY_DELETED = "保持归档"


def decide_archive_transition(
    in_seen: bool,
    is_deleted: bool,
    tmdbid: int,
    seen_tmdbids: Dict[int, Any],
    mediaserver: Optional[str],
    scanned_servers: Set[str],
) -> ArchiveAction:
    """判定一条历史记录在本轮扫描后应如何迁移。

    纯决策函数，不做任何IO：条目是否真的被删除由调用方用媒体服务器复核后再定。
    ``seen_tmdbids`` 是按TMDBID索引的本轮在库条目，用于识别改名或重建库换ItemID，
    避免同部剧同时出现在常规列表和已删除列表里。
    """
    if in_seen:
        # 同ItemID仍在库里；已归档的话说明用户重新入库，撤销归档交回常规检测
        return ArchiveAction.UNARCHIVE if is_deleted else ArchiveAction.KEEP

    if is_deleted:
        # 归档记录不在本轮集合里属正常，只有确认同剧已换新条目时才并回出列
        if tmdbid and tmdbid in seen_tmdbids:
            return ArchiveAction.MERGE_LIVE
        return ArchiveAction.STAY_DELETED

    # 未完整枚举的服务器其记录不能参与删除判定，否则一次故障就整库误归档
    if not mediaserver or mediaserver not in scanned_servers:
        return ArchiveAction.SKIP_UNSCANNED

    # 同TMDBID已以新条目入库，旧键需要清理，避免两条记录并存
    if tmdbid and tmdbid in seen_tmdbids:
        return ArchiveAction.DROP_RENAMED

    # 本轮未见即交调用方复核媒体服务器条目是否真的不存在，确认后才归档
    return ArchiveAction.NEED_CONFIRM


def normalize_existing_seasons(
    raw: Optional[Dict[Any, Any]],
) -> Dict[int, List[int]]:
    """把持久化后的季集快照还原成 {季号(int): [集号(int)]}。

    JSON 会把字典键存成字符串，而 TMDB 季号是整数，比较前必须统一类型。
    """
    normalized: Dict[int, List[int]] = {}
    for season_key, episodes in (raw or {}).items():
        try:
            season = int(season_key)
        except (TypeError, ValueError):
            continue
        episode_numbers: List[int] = []
        for episode in episodes or []:
            try:
                episode_numbers.append(int(episode))
            except (TypeError, ValueError):
                continue
        normalized[season] = sorted(set(episode_numbers))
    return normalized


def format_existing_seasons_label(
    existing_seasons: Optional[Dict[Any, Any]],
    missing_info: Optional[Dict[str, Any]] = None,
) -> str:
    """生成卡片副信息用的季集摘要，例如 ``S1(12集) · S2(5/8)``。

    缺失快照里有该季的 TMDB 总集数时按 ``已存在/总数`` 显示，
    否则只显示已存在集数，避免编造分母。
    """
    normalized = normalize_existing_seasons(existing_seasons)
    if not normalized:
        return ""

    parts: List[str] = []
    for season in sorted(normalized):
        episodes = normalized[season]
        # 空列表说明该季删除时并没有任何集在库，展示出来只会误导，直接跳过
        if not episodes:
            continue
        season_key = str(season)
        total = 0
        season_missing = (missing_info or {}).get(season_key) or {}
        for key in ("episode_total_unfiltered", "episode_total"):
            value = season_missing.get(key)
            if isinstance(value, int) and value > 0:
                total = value
                break
        if total and total >= len(episodes):
            parts.append(f"S{season}({len(episodes)}/{total})")
        else:
            parts.append(f"S{season}({len(episodes)}集)")
    return " · ".join(parts)


_MEDIA_ITEM_ID_PATTERN = re.compile(r"^(?:\d+|[0-9a-fA-F-]{8,})$")


def select_records_to_clear(
    details: Dict[str, Any],
    scope: str,
    stale_days: int,
    now: Optional[datetime.datetime] = None,
) -> List[str]:
    """按清理范围挑出要移除的记录键，供分级清理使用。

    :param scope: skipped 仅已跳过 / deleted 仅已删除归档 / stale 仅长期未更新；
                  其余取值（含 all）返回空列表，全部清理由调用方直接清空整份数据
    :param stale_days: scope=stale 时的天数阈值，小于 1 时按 1 天处理
    :returns: 命中的记录键列表（升序）
    纯数据操作，不碰存储与锁，便于离线单测。
    """
    if scope not in ("skipped", "deleted", "stale"):
        return []
    now = now or datetime.datetime.now(tz=pytz.timezone(settings.TZ))
    keys: List[str] = []
    for key, record in (details or {}).items():
        if not record:
            continue
        if scope == "skipped":
            if record.get("skip"):
                keys.append(key)
        elif scope == "deleted":
            if record.get("deleted"):
                keys.append(key)
        else:
            stamp = record.get("last_check_full") or record.get("last_status_change") or ""
            try:
                checked = datetime.datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError):
                continue
            threshold = max(int(stale_days or 0), 1)
            if (now - checked.replace(tzinfo=now.tzinfo)).days >= threshold:
                keys.append(key)
    return sorted(keys)


def extract_media_item_id(unique_flag: str, mediaserver: Optional[str]) -> Optional[str]:
    """从历史记录键 ``{服务器}_{媒体库}_{ItemID}_{标题}`` 中取回条目ID。

    早期记录没有单独保存 ItemID，归档复核时需要这一份兜底解析。
    媒体库名可能含下划线，因此只在解析结果符合 GUID/数字ID 形状时才采用，
    取不出可靠ID时返回 None，由调用方放弃归档复核。
    """
    if not unique_flag:
        return None
    if mediaserver and unique_flag.startswith(f"{mediaserver}_"):
        remainder = unique_flag[len(mediaserver) + 1:]
    else:
        remainder = unique_flag
    segments = remainder.split("_")
    if len(segments) < 3:
        return None
    # 服务器名已剥离，第一段是媒体库，第二段才是条目ID；媒体库名含下划线时逐段试
    for candidate in segments[1:-1]:
        if _MEDIA_ITEM_ID_PATTERN.match(candidate):
            return candidate
    return None


class HistoryDetail(TypedDict, total=False):
    exist_status: Optional[str]
    tv_no_exist_info: Optional[TvNoExistInfo]
    last_check: Optional[str]
    last_check_full: Optional[str]
    first_found_time: Optional[str]
    last_status_change: Optional[str]
    skip: Optional[bool]
    ignored_seasons: Optional[List[int]]          # 新增：被手动标记存在的季号列表
    mediaserver: Optional[str]                    # 记录来源的媒体服务器名，归档复核用
    library: Optional[str]                        # 记录来源的媒体库名，详情页标注用
    deleted: Optional[bool]                       # 已从媒体库删除，保留为归档记录
    deleted_at: Optional[str]                     # 转为归档记录的时间
    last_seen: Optional[str]                      # 最后一次在媒体库中被检测到的时间
    delete_source: Optional[str]                  # 归档来源：scan_diff
    follow: Optional[bool]                        # 已删除剧集是否继续追新季
    follow_check_time: Optional[str]              # 最近一次追新检测时间
    follow_note: Optional[str]                    # 追新结果摘要，展示在卡片上


class ExtendedHistoryDetail(HistoryDetail):
    unique: Optional[str]


class History(TypedDict):
    details: Dict[str, HistoryDetail]


class GetMissingEpisodes(_PluginBase):
    plugin_name = "剧集管家"
    plugin_desc = "检测指定剧集库，对有新季或存在集缺失的剧集自动订阅补全"
    plugin_icon = "https://raw.githubusercontent.com/andyxu8023/MoviePilot-Plugins/main/icons/EpisodeNoExist.png"
    plugin_version = "3.2.8"
    plugin_author = "左岸"
    author_url = "https://github.com/andyxu8023"
    plugin_config_prefix = "getmissingepisodes_"
    plugin_order = 6
    auth_level = 2

    _event = Event()
    _lock = Lock()
    # 检测单飞闸门：保证同一时刻只有一轮检测在跑。
    # 类属性只是占位，真正使用的是 init_plugin 里创建的实例级锁，
    # 否则多实例模式下会共享同一把闸门。
    _scan_gate = Lock()

    # 私有属性
    _subChain: SubscribeChain
    _subOper: SubscribeOper
    _mediaChain: MediaChain
    _tmdbChain: TmdbChain
    _msChain: MediaServerChain
    _msHelper: MediaServerHelper
    _plugin_id = "GetMissingEpisodes"
    _scheduler = None

    # 配置属性
    _enabled: bool = False
    _cron: str = ""
    _onlyonce: bool = False
    _clear: bool = False
    _clearflag: bool = False
    _clear_scope: str = "all"
    _clear_stale_days: int = 90
    _only_season_exist: bool = True
    _only_aired: bool = True
    _no_exist_action: str = NoExistAction.ONLY_HISTORY.value
    _save_path_replaces: List[str] = []
    _whitelist_librarys: List[str] = []
    _whitelist_media_servers: List[str] = []
    _current_history_type: str = HistoryDataType.LATEST.value
    _auto_skip_finished: bool = False
    _include_s00_season: bool = False
    _show_sidebar_nav: bool = True

    # 运行时状态
    _checking: bool = False

    # Vue 工作台允许提交的配置项
    _EDITABLE_SETTINGS_KEYS = (
        "enabled",
        "cron",
        "onlyonce",
        "clear",
        "clear_scope",
        "clear_stale_days",
        "only_season_exist",
        "only_aired",
        "no_exist_action",
        "save_path_replaces",
        "whitelist_librarys",
        "whitelist_media_servers",
        "auto_skip_finished",
        "include_s00_season",
        "show_sidebar_nav",
    )

    def init_plugin(self, config: dict[str, Any] | None = None):
        """初始化插件"""
        try:
            # 检测闸门改为实例级：类属性会被多个插件实例共享
            self._scan_gate = Lock()
            self._subChain = SubscribeChain()
            self._subOper = SubscribeOper()
            self._mediaChain = MediaChain()
            self._tmdbChain = TmdbChain()
            self._msChain = MediaServerChain()
            self._msHelper = MediaServerHelper()

            if config:
                self._load_config(config)

            # 从存储中读取当前选中的历史数据类型
            saved_type = self.get_data("current_history_type")
            if saved_type:
                self._current_history_type = saved_type

            # 停止现有任务
            self.stop_service()

            # 启动服务
            if self._enabled or self._onlyonce:
                self._start_service()

        except Exception as e:
            logger.error(f"初始化插件失败: {str(e)}")
            raise

    def _load_config(self, config: dict[str, Any]):
        """加载配置"""
        self._enabled = config.get("enabled", False)
        self._onlyonce = config.get("onlyonce", False)
        self._cron = config.get("cron", "").strip()
        self._clear = config.get("clear", False)
        # 清理范围：all 全部 / skipped 仅已跳过 / deleted 仅已删除归档 / stale 仅长期未更新
        scope = str(config.get("clear_scope", "all") or "all").strip().lower()
        self._clear_scope = scope if scope in ("all", "skipped", "deleted", "stale") else "all"
        try:
            self._clear_stale_days = max(int(config.get("clear_stale_days", 90) or 90), 1)
        except (TypeError, ValueError):
            self._clear_stale_days = 90
        self._only_season_exist = config.get("only_season_exist", True)
        self._only_aired = config.get("only_aired", True)
        self._no_exist_action = config.get("no_exist_action", NoExistAction.ONLY_HISTORY.value)
        self._auto_skip_finished = config.get("auto_skip_finished", False)
        self._include_s00_season = config.get("include_s00_season", False)

        # 处理保存路径替换
        _save_path_replaces = config.get("save_path_replaces", "")
        if _save_path_replaces and isinstance(_save_path_replaces, str):
            self._save_path_replaces = [line.strip() for line in _save_path_replaces.split("\n") if line.strip()]
        else:
            self._save_path_replaces = []

        # 处理媒体库白名单
        self._whitelist_librarys = self._parse_list_config(
            config.get("whitelist_librarys", []), 
            default=[]
        )

        # 处理媒体服务器白名单
        self._whitelist_media_servers = self._parse_list_config(
            config.get("whitelist_media_servers", ""),
            default=[]
        )

        # 侧边栏入口开关，默认开启
        self._show_sidebar_nav = bool(config.get("show_sidebar_nav", True))

    def _parse_list_config(self, config_value: Any, default: List[str] = None) -> List[str]:
        """解析列表配置，支持字符串和列表格式"""
        if default is None:
            default = []
        
        if isinstance(config_value, str):
            if config_value:
                return [item.strip() for item in config_value.split(",") if item.strip()]
            else:
                return default
        elif isinstance(config_value, list):
            return [item for item in config_value if item]
        else:
            return default

    def _start_service(self):
        """启动服务"""
        if self._onlyonce:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            logger.info(f"{self.plugin_name}服务启动, 立即运行一次")
            self._scheduler.add_job(
                func=self.__refresh,
                trigger="date",
                run_date=datetime.datetime.now(tz=pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=3),
            )

            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

        if self._onlyonce or self._clear:
            # 记录缓存清理标志
            self._clearflag = self._clear
            # 关闭清理缓存和一次性开关
            self._clear = False
            self._onlyonce = False
            # 保存配置
            self._update_config()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_render_mode() -> Tuple[str, Optional[str]]:
        """声明插件渲染模式：存在联邦构建产物时使用 Vue 工作台。"""
        remote_entry = Path(__file__).parent / "dist" / "assets" / "remoteEntry.js"
        if remote_entry.is_file():
            return "vue", "dist/assets"
        return "vuetify", None

    def get_sidebar_nav(self) -> List[Dict[str, Any]]:
        """向主界面侧栏订阅分组注册剧集管家入口。"""
        if not self.get_state() or not self._show_sidebar_nav:
            return []
        if self.get_render_mode()[0] != "vue":
            return []
        return [
            {
                "nav_key": "main",
                "title": "剧集管家",
                "icon": "mdi-television-classic",
                "section": "subscribe",
                "permission": "subscribe",
                "order": 30,
            }
        ]

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """注册 Vue 工作台与外部调用使用的插件 API。"""
        return [
            {
                "path": "/delete_history",
                "endpoint": self.delete_history,
                "methods": ["GET"],
                "auth": "bear",
                "summary": f"删除 {self.plugin_name} 检查记录",
            },
            {
                "path": "/set_all_exist_history",
                "endpoint": self.set_all_exist_history,
                "methods": ["GET"],
                "auth": "bear",
                "summary": f"标记 {self.plugin_name} 存在记录",
            },
            {
                "path": "/unset_ignored_season_history",
                "endpoint": self.unset_ignored_season_history,
                "methods": ["GET"],
                "auth": "bear",
                "summary": f"撤销 {self.plugin_name} 已忽略季",
            },
            {
                "path": "/add_subscribe_history",
                "endpoint": self.add_subscribe_history,
                "methods": ["GET"],
                "auth": "bear",
                "summary": f"订阅 {self.plugin_name} 缺失记录",
            },
            {
                "path": "/toggle_skip_history",
                "endpoint": self.toggle_skip_history,
                "methods": ["GET"],
                "auth": "bear",
                "summary": f"切换 {self.plugin_name} 跳过状态",
            },
            {
                "path": "/toggle_follow_history",
                "endpoint": self.toggle_follow_history,
                "methods": ["GET"],
                "auth": "bear",
                "summary": f"切换 {self.plugin_name} 已删除剧集追新状态",
            },
            {
                "path": "/set_action_history",
                "endpoint": self.set_action_history,
                "methods": ["GET"],
                "auth": "bear",
                "summary": f"设置 {self.plugin_name} 单条剧集的缺失处理方式",
            },
            {
                "path": "/set_history_type",
                "endpoint": self.set_history_type,
                "methods": ["GET"],
                "auth": "bear",
                "summary": f"设置 {self.plugin_name} 历史数据类型",
            },
            {
                "path": "/status",
                "endpoint": self.get_status,
                "methods": ["GET"],
                "auth": "bear",
                "summary": f"获取 {self.plugin_name} 工作台总览数据",
            },
            {
                "path": "/settings",
                "endpoint": self.update_settings,
                "methods": ["POST"],
                "auth": "bear",
                "summary": f"保存 {self.plugin_name} 插件设置",
            },
            {
                "path": "/run",
                "endpoint": self.run_check_now,
                "methods": ["POST"],
                "auth": "bear",
                "summary": f"立即执行一次 {self.plugin_name} 缺失检测",
            },
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        if self._enabled and self._cron:
            return [
                {
                    "id": "GetMissingEpisodes",
                    "name": f"{self.plugin_name}",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self.__refresh,
                    "kwargs": {},
                }
            ]
        elif self._enabled:
            return [
                {
                    "id": "GetMissingEpisodes",
                    "name": f"{self.plugin_name}",
                    "trigger": CronTrigger.from_crontab("0 8 * * *"),
                    "func": self.__refresh,
                    "kwargs": {},
                }
            ]
        return []

    def __begin_scan(self) -> bool:
        """尝试占用检测闸门，已有检测在跑时返回 False。

        判定与置位必须在同一把锁内完成：先前写法是先查 `_checking` 再起线程、
        在线程内才置位，两次快速点击或「定时任务 + 手动检查」会双双通过闸门，
        两条线程各自读整份 history 再整份写回，后完成的会覆盖先完成的成果。
        """
        with self._scan_gate:
            if self._checking:
                return False
            self._checking = True
            return True

    def __end_scan(self) -> None:
        """释放检测闸门。"""
        with self._scan_gate:
            self._checking = False

    def __refresh(self):
        """刷新数据（定时任务与启动一次性入口，与手动检查共用同一把单飞闸门）"""
        if not self.__begin_scan():
            logger.info("已有检测在进行中，跳过本轮定时检测")
            return
        try:
            self.__get_mediaserver_tv_info()
        except Exception as e:
            logger.error(f"刷新数据失败: {str(e)}")
        finally:
            self.__end_scan()

    def __get_mediaservers(self):
        """获取媒体服务器"""
        try:
            mediaservers = self._msHelper.get_services()
            logger.info(f"获取到媒体服务器: {mediaservers}")
            return mediaservers or []
        except Exception as e:
            logger.error(f"获取媒体服务器失败: {str(e)}")
            return []

    def __get_mediaserver_tv_info(self) -> None:
        """获取媒体库电视剧数据"""
        logger.info("开始获取媒体库电视剧数据 ...")
        # 本轮扫描摘要计数器：随检测推进累加，结束时落盘供详情页展示
        scan_stats = {
            "started_at": datetime.datetime.now(tz=pytz.timezone(settings.TZ)).strftime("%Y-%m-%d %H:%M:%S"),
            "monotonic": time.monotonic(),
            "library_items": 0,
            "checked": 0,
            "skipped": 0,
            "all_exist": 0,
            "missing": 0,
            "subscribed": 0,
            "failed": 0,
            "archived": 0,
            "revived": 0,
            "renamed": 0,
            "follow_checked": 0,
            "follow_subscribed": 0,
        }
        
        # 清理检查记录：支持按范围分级清理，不再只有「一键全清」
        history = self.get_data("history")
        if self._clearflag:
            self._clearflag = False
            scope = str(self._clear_scope or "all").strip().lower()
            if scope not in ("all", "skipped", "deleted", "stale"):
                # 清理是破坏性动作：范围值异常时宁可不删，也绝不能退化成清空整本记录
                logger.warning(f"清理检查记录：未知的清理范围「{scope}」，本次不清理")
            elif scope == "all":
                logger.info("清理检查记录：全部记录")
                self.save_data("history", "")
                history = None
            else:
                details = (history or {}).get("details") or {}
                removed = select_records_to_clear(details, scope, self._clear_stale_days)
                scope_cn = {"skipped": "已跳过", "deleted": "已删除归档",
                            "stale": f"超过 {self._clear_stale_days} 天未更新"}.get(scope, scope)
                if removed:
                    for removed_key in removed:
                        details.pop(removed_key, None)
                    self.save_data("history", history)
                    logger.info(f"清理检查记录：范围「{scope_cn}」，移除 {len(removed)} 条")
                else:
                    logger.info(f"清理检查记录：范围「{scope_cn}」，没有符合条件的记录")

        history_data: Dict[str, Any] = history if history else {"details": {}}
        
        # 新增：记录本次扫描到的所有电视剧唯一标识
        seen_flags = set()
        # 本轮成功完成枚举的媒体服务器；未成功的服务器其历史记录不参与归档判定
        scanned_servers: Set[str] = set()
        # 本轮在库电视剧的 TMDBID -> 记录键，用于识别换ItemID和归档出列
        seen_tmdbids: Dict[int, str] = {}
        # 枚举期回填或同步期迁移改动了记录时，需要在同步块统一落盘。
        # 用单键 dict 承载：归档逻辑已拆到独立方法，需要能回写这个标志。
        records_flag = {"changed": False}

        def __remember_seen_tmdbid(record: Dict[str, Any], item_unique_flag: str):
            """把在库条目的TMDBID登记到本轮集合，用于换ItemID识别与归档出列。"""
            snapshot = (record or {}).get("tv_no_exist_info") or {}
            try:
                tmdbid = int(snapshot.get("tmdbid") or 0)
            except (TypeError, ValueError):
                return
            if tmdbid:
                seen_tmdbids[tmdbid] = item_unique_flag

        # 落库节流：整份 history 只在累计 10 条变更或本轮收尾时写一次。
        # 原先每处理完一部剧就把整本记录序列化写库一次，28 部剧即 28 次全量重写。
        save_state = {"dirty": 0}

        def __mark_history_dirty():
            save_state["dirty"] += 1
            if save_state["dirty"] >= 10:
                save_state["dirty"] = 0
                self.save_data("history", history_data)

        # 添加检查记录
        def __append_history(
            item_unique_flag: str,
            exist_status: HistoryStatus,
            tv_no_exist_info: TvNoExistInfo | Dict[str, Any] | None = None,
            ignore_missing_seasons: bool = False,
        ):
            with self._lock:
                current_time = datetime.datetime.now(tz=pytz.timezone(settings.TZ))
                current_time_str = current_time.strftime("%Y-%m-%d %H:%M:%S")

                # 检查是否已有记录
                existing_record = history_data["details"].get(item_unique_flag)
                
                # 检查是否需要自动跳过已完结剧集（已删除归档记录不参与自动跳过）
                auto_skip = False
                if self._auto_skip_finished and tv_no_exist_info and not (existing_record and existing_record.get("deleted")):
                    status_cn = tv_no_exist_info.get("status_cn", "")
                    if status_cn == "已完结":
                        auto_skip = True
                        logger.info(f"【{tv_no_exist_info.get('title', '未知')}】已完结，自动标记为跳过")
                
                if existing_record:
                    # 已有记录，保留原有的忽略列表
                    ignored_seasons = existing_record.get("ignored_seasons", [])

                    # 归档与追新字段不能被常规检测覆盖，写回时原样带回
                    carried_fields = {
                        field: existing_record[field]
                        for field in PERSISTENT_RECORD_FIELDS
                        if field != "mediaserver"
                        and existing_record.get(field) is not None
                    }

                    # 本轮拿不到已存在季集快照时，沿用上一次的基线快照
                    new_snapshot = tv_no_exist_info if tv_no_exist_info else existing_record.get("tv_no_exist_info")
                    old_snapshot = existing_record.get("tv_no_exist_info") or {}
                    if isinstance(new_snapshot, dict) and not new_snapshot.get("existing_seasons") and old_snapshot.get("existing_seasons"):
                        new_snapshot["existing_seasons"] = old_snapshot["existing_seasons"]
                    
                    existing_skip = existing_record.get("skip", False)
                    first_found_time = existing_record.get("first_found_time", existing_record.get("last_check_full", current_time_str))
                    previous_status = existing_record.get("exist_status")
                    
                    # 判断状态是否变化
                    status_changed = previous_status != exist_status.value
                    
                    # 如果状态变化，更新最后状态变更时间，否则保持不变
                    if status_changed:
                        last_status_change = current_time_str
                    else:
                        last_status_change = existing_record.get("last_status_change", existing_record.get("last_check_full", current_time_str))
                    
                    # 如果自动跳过，更新skip状态
                    final_skip = existing_skip or auto_skip
                    
                    history_data["details"][item_unique_flag] = {
                        "exist_status": exist_status.value,
                        "tv_no_exist_info": new_snapshot,
                        "last_check": current_time.strftime("%m-%d %H:%M"),
                        "last_check_full": current_time_str,
                        "first_found_time": first_found_time,
                        "last_status_change": last_status_change,
                        "skip": final_skip,
                        "ignored_seasons": ignored_seasons,
                        "mediaserver": mediaserver,
                        "library": library_name,
                        **carried_fields,
                    }
                else:
                    # 新记录，忽略列表初始为空
                    history_data["details"][item_unique_flag] = {
                        "exist_status": exist_status.value,
                        "tv_no_exist_info": tv_no_exist_info,
                        "last_check": current_time.strftime("%m-%d %H:%M"),
                        "last_check_full": current_time_str,
                        "first_found_time": current_time_str,
                        "last_status_change": current_time_str,
                        "skip": auto_skip,
                        "ignored_seasons": [],
                        "mediaserver": mediaserver,
                        "library": library_name,
                    }
                
                if ignore_missing_seasons:
                    # 标记为存在时把本轮缺失季写入忽略列表，避免下轮又算成缺失
                    merged_record = history_data["details"][item_unique_flag]
                    base_ignored = merged_record.get("ignored_seasons") or []
                    merged_ignored = merge_missing_seasons_into_ignored(
                        base_ignored,
                        merged_record.get("tv_no_exist_info"),
                    )
                    newly_ignored = [season for season in merged_ignored if season not in base_ignored]
                    if newly_ignored:
                        logger.info(f"【{item_title}】缺失季已加入忽略列表: {newly_ignored}")
                    merged_record["ignored_seasons"] = merged_ignored

                logger.debug(f"添加/更新检查记录: {item_unique_flag}, 状态: {exist_status.value}")
                # 摘要计数：按落库状态归类，「实际检测数」为这四类之和
                _bucket = {
                    HistoryStatus.ALL_EXIST.value: "all_exist",
                    HistoryStatus.NO_EXIST.value: "missing",
                    HistoryStatus.ADDED_RSS.value: "subscribed",
                    HistoryStatus.FAILED.value: "failed",
                }.get(exist_status.value)
                if _bucket:
                    scan_stats[_bucket] = scan_stats.get(_bucket, 0) + 1
                __mark_history_dirty()

        mediaservers = self.__get_mediaservers()
        if not mediaservers:
            logger.warning("未获取到媒体服务器")
            return

        logger.info(f"媒体服务器名称白名单: {self._whitelist_media_servers if self._whitelist_media_servers else '全部'}")
        logger.info(f"媒体库白名单: {self._whitelist_librarys}")

        details = history_data.get("details", {})
        logger.debug(f"历史记录数量: {len(details)}")

        # 遍历媒体服务器
        for mediaserver in mediaservers:
            if not mediaserver:
                continue
                
            # 检查媒体服务器白名单
            if self._whitelist_media_servers and mediaserver not in self._whitelist_media_servers:
                logger.info(f"【{mediaserver}】不在媒体服务器名称白名单内, 跳过")
                continue
                
            logger.info(f"开始获取媒体库 {mediaserver} 的数据 ...")

            item_count = 0
            # 只有完整枚举成功的媒体服务器才允许参与归档判定
            server_scan_ok = True
            scanned_library_count = 0
            try:
                librarys = self._msChain.librarys(mediaserver)
            except Exception as e:
                logger.error(f"获取媒体库列表失败: {str(e)}")
                continue

            for library in librarys:
                # 检查媒体库白名单
                if self._whitelist_librarys and library.name not in self._whitelist_librarys:
                    logger.debug(f"媒体库 {library.name} 不在白名单内，跳过")
                    continue

                # 记录当前媒体库名，供记录写回时标注来源（详情页区分同名剧）
                library_name = library.name
                scanned_library_count += 1
                logger.info(f"正在获取 {mediaserver} 媒体库 {library.name} ...")

                if not library.id:
                    logger.debug("未获取到Library ID, 跳过获取缺失集数")
                    server_scan_ok = False
                    continue

                try:
                    library_items = self._msChain.items(mediaserver, library.id)
                except Exception as e:
                    logger.error(f"获取媒体库项失败: {str(e)}")
                    server_scan_ok = False
                    continue

                if not library_items:
                    logger.debug("未获取到媒体库items信息, 跳过获取缺失集数")
                    server_scan_ok = False
                    continue

                for item in library_items:
                    item_count += 1

                    if not item or not item.item_id:
                        logger.debug("未获取到Item媒体信息或Item ID, 跳过获取缺失集数")
                        continue

                    item_title = item.title or item.original_title or f"ItemID: {item.item_id}"
                    item_unique_flag = f"{mediaserver}_{item.library}_{item.item_id}_{item_title}"
                    
                    # 新增：将本次扫描到的有效电视剧加入集合
                    seen_flags.add(item_unique_flag)

                    # 跳过检测的记录同样在库里，本轮算见过，不能参与归档判定

                    # 检查是否被标记为跳过
                    if item_unique_flag in details and details[item_unique_flag].get("skip", False):
                        logger.info(f"【{item_title}】已被标记为跳过, 跳过检测")
                        scan_stats["skipped"] += 1
                        # 跳过的条目仍在库里，登记其TMDBID以免被当成换ItemID或误归档
                        __remember_seen_tmdbid(details[item_unique_flag], item_unique_flag)
                        # 回填所属服务器：跳过记录不走常规检测写回，
                        # 没有该字段的话之后被删库会被"服务器未扫完"挡板挡住，永远进不了已删除归档
                        if not details[item_unique_flag].get("mediaserver"):
                            details[item_unique_flag]["mediaserver"] = mediaserver
                            records_flag["changed"] = True
                        # 同样回填所属媒体库名，跳过记录也能在卡片上标注来源
                        if not details[item_unique_flag].get("library"):
                            details[item_unique_flag]["library"] = library_name
                            records_flag["changed"] = True
                        continue

                    # 获取该记录的忽略季列表
                    ignored_seasons = []
                    if item_unique_flag in details:
                        ignored_seasons = details[item_unique_flag].get("ignored_seasons", [])

                    logger.info(f"正在获取 {item_title} ...")

                    # 检查媒体类型
                    item_type = MediaType.TV.value if item.item_type in ["Series", "show"] else MediaType.MOVIE.value
                    if item_type == MediaType.MOVIE.value:
                        logger.warning(f"【{item_title}】为{MediaType.MOVIE.value}, 跳过")
                        continue

                    # 获取季信息
                    seasoninfo = {}
                    _item_tmdbid = 0
                    if getattr(item, 'media_source', None) and getattr(item, 'media_id', None):
                        try:
                            if str(item.media_source) == 'themoviedb':
                                _item_tmdbid = int(item.media_id)
                        except (ValueError, TypeError) as e:
                            # 不静默吞掉：TMDBID 解析失败会让这条记录参与不了「换ID识别」，
                            # 真出问题时日志里要能看到线索
                            logger.debug(f"【{item_title}】TMDBID 解析失败: {str(e)}")
                    if item_type == MediaType.TV.value and _item_tmdbid:
                        seen_tmdbids[_item_tmdbid] = item_unique_flag
                        try:
                            espisodes_info = self._msChain.episodes(mediaserver, item.item_id) or []
                            for episode_info in espisodes_info:
                                seasoninfo[episode_info.season] = episode_info.episodes
                        except Exception as e:
                            logger.error(f"获取剧集信息失败: {str(e)}")

                    # 准备数据
                    item_dict = item.dict()
                    item_dict["seasoninfo"] = seasoninfo
                    item_dict["item_type"] = item_type
                    item_dict["tmdbid"] = _item_tmdbid
                    logger.debug(f"获到媒体库【{item_title}】数据：{item_dict}")

                    # 获取缺失集数信息，传入忽略季列表
                    is_add_subscribe_success, tv_no_exist_info = self.__get_item_no_exist_info(
                        item_dict, ignored_seasons
                    )

                    # 处理结果
                    self.__handle_item_missing_result(
                        item_unique_flag=item_unique_flag,
                        item_title=item_title,
                        is_add_subscribe_success=is_add_subscribe_success,
                        tv_no_exist_info=tv_no_exist_info,
                        history_data=history_data,
                        append_history=__append_history,
                    )

                logger.info(f"{mediaserver} 媒体库 {library.name} 获取数据完成")

            if server_scan_ok and scanned_library_count:
                scanned_servers.add(mediaserver)
            else:
                logger.warning(
                    f"【{mediaserver}】本轮未完整枚举（成功媒体库 {scanned_library_count} 个），"
                    "其历史记录本轮不参与已删除判定"
                )

        logger.info(f"媒体库缺失集数据获取完成, 已处理媒体数量: {item_count}")
        
        # ==== 归档：把媒体库里已消失的电视剧转入「已删除」 ====
        archived_count, revived_count, renamed_count = self.__sync_deleted_archive(
            history_data, seen_flags, seen_tmdbids, scanned_servers, records_flag
        )
        scan_stats["archived"] = archived_count
        scan_stats["revived"] = revived_count
        scan_stats["renamed"] = renamed_count
        # ==== 结束归档 ====

        # 收尾必存：把节流攒下的变更落库，保证中断前数据已持久化
        if save_state["dirty"]:
            save_state["dirty"] = 0
            self.save_data("history", history_data)

        # 追新：对已删除且开启追新的记录，用归档基线再走一遍缺失检测
        follow_checked, follow_subscribed = self.__follow_deleted_records(history_data)
        scan_stats["follow_checked"] = follow_checked
        scan_stats["follow_subscribed"] = follow_subscribed

        # 本轮扫描摘要落盘，供详情页顶部一行展示
        self.__save_scan_summary(scan_stats, item_count)

    def __save_scan_summary(self, stats: Dict[str, Any], item_count: int) -> None:
        """把本轮扫描计数整理成摘要并落盘。

        存到独立数据键 ``scan_summary``，避免整本 history 因摘要字段变胖；
        写失败只降级为日志，不影响检测结果。
        """
        checked = (stats.get("all_exist", 0) + stats.get("missing", 0)
                   + stats.get("subscribed", 0) + stats.get("failed", 0))
        summary = {
            "time": stats.get("started_at") or "",
            "duration": round(time.monotonic() - stats.get("monotonic", time.monotonic()), 1),
            "library_items": item_count,
            "checked": checked,
            "skipped": stats.get("skipped", 0),
            "missing": stats.get("missing", 0),
            "subscribed": stats.get("subscribed", 0),
            "failed": stats.get("failed", 0),
            "archived": stats.get("archived", 0),
            "revived": stats.get("revived", 0),
            "renamed": stats.get("renamed", 0),
            "follow_checked": stats.get("follow_checked", 0),
            "follow_subscribed": stats.get("follow_subscribed", 0),
        }
        try:
            self.save_data("scan_summary", summary)
        except Exception as e:
            logger.warning(f"保存扫描摘要失败: {str(e)}")
            return
        logger.info(
            f"本轮扫描摘要：媒体条目 {item_count} 个，实际检测 {checked} 部，"
            f"跳过 {summary['skipped']} 部，缺失 {summary['missing']} 部，"
            f"订阅 {summary['subscribed']} 部，失败 {summary['failed']} 部，"
            f"归档 {summary['archived']} 条，出列 {summary['revived']} 条，"
            f"追新 {summary['follow_checked']} 部，耗时 {summary['duration']} 秒"
        )

    def __handle_item_missing_result(self, item_unique_flag: str, item_title: str,
                                     is_add_subscribe_success: bool,
                                     tv_no_exist_info: Dict[str, Any],
                                     history_data: Dict[str, Any],
                                     append_history) -> None:
        """按「缺失处理方式」处理单条剧的检测结果并落记录。

        纯搬移自扫描主流程，行为与日志文案保持不变。
        append_history 由主流程传入（它是主流程内的闭包，持有 history_data 等状态）。

        :param history_data: 整份历史记录，用于读取该记录的单剧覆盖配置
        :param append_history: 主流程的 __append_history 闭包
        """
        if is_add_subscribe_success and tv_no_exist_info:
            if not tv_no_exist_info.get("season_episode_no_exist_info"):
                logger.info(f"【{item_title}】所有季集均已存在/订阅")
                append_history(
                    item_unique_flag=item_unique_flag,
                    exist_status=HistoryStatus.ALL_EXIST,
                    tv_no_exist_info=tv_no_exist_info,
                )
            else:
                logger.info(f"【{item_title}】缺失集数信息：{tv_no_exist_info}")

                # 单条剧集可覆盖全局缺失处理方式，未覆盖时回落全局配置
                record_action = self._resolve_no_exist_action(
                    history_data["details"].get(item_unique_flag)
                )
                if record_action == NoExistAction.ADD_SUBSCRIBE.value:
                    logger.info("开始订阅缺失集数")
                    is_add_subscribe_success = self.__add_subscribe_by_tv_no_exist_info(
                        tv_no_exist_info, item_unique_flag
                    )
                    if is_add_subscribe_success:
                        append_history(
                            item_unique_flag=item_unique_flag,
                            exist_status=HistoryStatus.ADDED_RSS,
                            tv_no_exist_info=tv_no_exist_info,
                        )
                    else:
                        logger.warning(f"订阅【{item_title}】失败, 仅记录缺失集数")
                        append_history(
                            item_unique_flag=item_unique_flag,
                            exist_status=HistoryStatus.NO_EXIST,
                            tv_no_exist_info=tv_no_exist_info,
                        )
                elif record_action == NoExistAction.SET_ALL_EXIST.value:
                    logger.debug("将缺失季集标记为存在")
                    append_history(
                        item_unique_flag=item_unique_flag,
                        exist_status=HistoryStatus.ALL_EXIST,
                        tv_no_exist_info=tv_no_exist_info,
                        ignore_missing_seasons=True,
                    )
                else:
                    logger.debug("仅记录缺失集数")
                    append_history(
                        item_unique_flag=item_unique_flag,
                        exist_status=HistoryStatus.NO_EXIST,
                        tv_no_exist_info=tv_no_exist_info,
                    )
        else:
            logger.warning(f"【{item_title}】获取缺失集数信息失败")
            append_history(
                item_unique_flag=item_unique_flag,
                exist_status=HistoryStatus.FAILED,
                tv_no_exist_info=tv_no_exist_info,
            )

    def __sync_deleted_archive(self, history_data: Dict[str, Any],
                               seen_flags: Set[str], seen_tmdbids: Dict[int, str],
                               scanned_servers: Set[str],
                               records_flag: Dict[str, bool]) -> Tuple[int, int, int]:
        """把本轮媒体库里已消失的电视剧转入「已删除」归档，并处理三种回流情况。

        纯搬移自扫描主流程，行为与日志文案保持不变。

        :param history_data: 整份历史记录，就地修改
        :param seen_flags: 本轮在库条目的唯一标识集合
        :param seen_tmdbids: 本轮在库条目的 TMDBID -> 记录键
        :param scanned_servers: 本轮完整枚举成功的媒体服务器
        :param records_flag: {"changed": bool}，枚举期已改动过记录时置 True，本方法也会置位
        :returns: (归档条数, 出列条数, 换ID清理条数)，供扫描摘要统计
        """
        with self._lock:
            current_time = datetime.datetime.now(tz=pytz.timezone(settings.TZ))
            current_time_str = current_time.strftime("%Y-%m-%d %H:%M:%S")
            details = history_data.get("details", {})
            archived_count = 0
            revived_count = 0
            renamed_count = 0

            for key, record in list(details.items()):
                if not record:
                    continue
                is_deleted = bool(record.get("deleted"))
                snapshot = record.get("tv_no_exist_info") or {}
                try:
                    tmdbid = int(snapshot.get("tmdbid") or 0)
                except (TypeError, ValueError):
                    tmdbid = 0
                server = record.get("mediaserver")

                action = decide_archive_transition(
                    in_seen=key in seen_flags,
                    is_deleted=is_deleted,
                    tmdbid=tmdbid,
                    seen_tmdbids=seen_tmdbids,
                    mediaserver=server,
                    scanned_servers=scanned_servers,
                )
                title = snapshot.get("title") or key

                if action in (ArchiveAction.KEEP, ArchiveAction.STAY_DELETED):
                    continue

                if action == ArchiveAction.UNARCHIVE:
                    logger.info(f"【{title}】已重新入库，恢复为常规检测")
                    record["deleted"] = False
                    record["follow"] = False
                    record["follow_note"] = ""
                    record["deleted_at"] = None
                    revived_count += 1
                    records_flag["changed"] = True
                    continue

                if action == ArchiveAction.MERGE_LIVE:
                    self.__merge_deleted_into_live(
                        details, key, seen_tmdbids[tmdbid], current_time_str
                    )
                    revived_count += 1
                    records_flag["changed"] = True
                    continue

                if action == ArchiveAction.SKIP_UNSCANNED:
                    logger.debug(f"【{key}】所属媒体服务器本轮未完整枚举，暂不做删除判定")
                    continue

                if action == ArchiveAction.DROP_RENAMED:
                    live_key = seen_tmdbids[tmdbid]
                    # 换条目ID重入库：旧记录的用户忽略意愿与首次发现时间先并过去
                    self.__carry_record_history(details, key, live_key)
                    logger.info(f"【{title}】以新条目重新入库，清理旧记录: {key}")
                    del details[key]
                    renamed_count += 1
                    records_flag["changed"] = True
                    continue

                # NEED_CONFIRM：复核确认条目真的消失后才归档
                if not self.__confirm_media_item_absent(server, record, key):
                    logger.info(f"【{title}】复核未确认已删除，保持原记录")
                    continue

                record["deleted"] = True
                record["exist_status"] = HistoryStatus.DELETED.value
                record["deleted_at"] = current_time_str
                record["last_seen"] = record.get("last_check_full") or record.get("last_status_change") or ""
                record["delete_source"] = "scan_diff"
                record["follow"] = False
                record["follow_note"] = ""
                record["last_status_change"] = current_time_str
                archived_count += 1
                logger.info(f"【{title}】已从媒体库删除，转入已删除记录")

            if records_flag["changed"] or archived_count or revived_count or renamed_count:
                self.save_data("history", history_data)
            logger.info(
                f"已删除记录同步完成：归档 {archived_count} 条，出列 {revived_count} 条，"
                f"清理换ID {renamed_count} 条"
            )
            return archived_count, revived_count, renamed_count

    def __confirm_media_item_absent(self, mediaserver: str, record: Dict[str, Any], key: str) -> bool:
        """复核条目是否真的从媒体服务器消失，取不到可靠结论时一律按“未删除”处理。"""
        snapshot = record.get("tv_no_exist_info") or {}
        item_id = snapshot.get("media_item_id") or extract_media_item_id(key, mediaserver)
        if not item_id:
            logger.warning(f"【{key}】无法确定媒体服务器条目ID，放弃删除复核")
            return False
        try:
            live_item = self._msChain.iteminfo(mediaserver, item_id)
        except Exception as e:
            logger.warning(f"【{key}】删除复核请求失败: {str(e)}")
            return False
        if live_item:
            logger.info(f"【{key}】条目 {item_id} 仍存在于 {mediaserver}，不归档")
            return False
        return True

    def __carry_record_history(
        self, details: Dict[str, Any], old_key: str, new_key: str
    ) -> None:
        """把旧记录键上的用户意愿迁移到新记录键。

        媒体服务器改名或重建库会让条目换 ItemID，记录键随之变化；
        不迁移的话「标记存在」写入的忽略季和首次发现时间会被静默丢弃。
        """
        old_record = details.get(old_key)
        new_record = details.get(new_key)
        if not old_record or not new_record:
            return
        new_record["ignored_seasons"] = merge_season_numbers(
            new_record.get("ignored_seasons"), old_record.get("ignored_seasons")
        )
        old_first = old_record.get("first_found_time")
        if old_first and (
            not new_record.get("first_found_time")
            or old_first < new_record["first_found_time"]
        ):
            new_record["first_found_time"] = old_first

    def __merge_deleted_into_live(
        self,
        details: Dict[str, Any],
        archived_key: str,
        live_key: str,
        current_time_str: str,
    ) -> None:
        """已删除剧集以新条目重新入库：把归档记录里的用户意愿并回在库记录后清理归档。"""
        archived = details.get(archived_key)
        live = details.get(live_key)
        if not archived or not live:
            return
        self.__carry_record_history(details, archived_key, live_key)
        live["deleted"] = False
        live["follow"] = False
        live["follow_note"] = ""
        del details[archived_key]
        logger.info(f"归档记录 {archived_key} 已并回在库记录 {live_key}")

    def __follow_deleted_records(self, history_data: Dict[str, Any]) -> Tuple[int, int]:
        """对已删除且开启追新的记录，用归档时的集级基线再走一遍缺失检测。

        检测逻辑完全复用常规缺失检测与订阅流程，因此遵循全局「缺失处理方式」；
        但显式绕过 ``only_season_exist``，否则基线之外的全新季会被跳过导致追新失效。
        记录的 ``exist_status`` 保持「已删除」，追新结果写进 ``follow_note``。

        :returns: (参与追新检测的条数, 其中订阅成功的条数)，供扫描摘要统计
        """
        details = history_data.get("details", {}) or {}
        targets = [
            (key, record)
            for key, record in details.items()
            if record and record.get("deleted") and record.get("follow")
        ]
        if not targets:
            return 0, 0

        logger.info(f"开始检测 {len(targets)} 部已删除剧集的新季 ...")
        current_time = datetime.datetime.now(tz=pytz.timezone(settings.TZ))
        current_time_str = current_time.strftime("%Y-%m-%d %H:%M:%S")
        subscribed_count = 0

        for key, record in targets:
            snapshot = record.get("tv_no_exist_info") or {}
            try:
                tmdbid = int(snapshot.get("tmdbid") or 0)
            except (TypeError, ValueError):
                tmdbid = 0
            title = snapshot.get("title") or "未知"

            if not tmdbid:
                logger.warning(f"【{title}】归档记录缺少TMDBID，无法追新")
                self.__update_follow_result(history_data, key, current_time_str, "缺少TMDBID，无法追新")
                continue

            item_dict = {
                "title": title,
                "original_title": title,
                "year": snapshot.get("year") or "",
                "path": snapshot.get("path") or "",
                "item_type": MediaType.TV.value,
                "tmdbid": tmdbid,
                "seasoninfo": normalize_existing_seasons(snapshot.get("existing_seasons")),
                "item_id": snapshot.get("media_item_id"),
            }
            ignored_seasons = merge_season_numbers(record.get("ignored_seasons"))

            baseline_note = ""
            if not item_dict["seasoninfo"]:
                # 升级前归档的记录没有集级基线，按删除时间推断当时已存在的季，
                # 否则整部剧的旧季会被当成缺失全部订回来
                reconstructed = self.__reconstruct_baseline(
                    tmdbid, record.get("last_seen") or record.get("deleted_at")
                )
                if reconstructed is None:
                    logger.warning(f"【{title}】无法推断删除前季集基线，本轮跳过追新")
                    self.__update_follow_result(
                        history_data, key, current_time_str, "无法推断删除前基线，未检测新季"
                    )
                    continue
                item_dict["seasoninfo"] = reconstructed
                snapshot["existing_seasons"] = {
                    str(season): episodes for season, episodes in reconstructed.items()
                }
                baseline_note = "基线按删除时间推断"
                logger.info(
                    f"【{title}】已按 {record.get('last_seen') or record.get('deleted_at')} "
                    f"推断删除前基线季: {sorted(reconstructed)}"
                )

            is_success, tv_no_exist_info = self.__get_item_no_exist_info(
                item_dict, ignored_seasons, force_full_season=True
            )
            if not is_success or not tv_no_exist_info:
                logger.warning(f"【{title}】追新检测失败")
                self.__update_follow_result(history_data, key, current_time_str, "追新检测失败")
                continue

            missing_info = tv_no_exist_info.get("season_episode_no_exist_info") or {}
            if not missing_info:
                self.__update_follow_result(
                    history_data,
                    key,
                    current_time_str,
                    "暂无新季" if not baseline_note else f"暂无新季（{baseline_note}）",
                    snapshot=tv_no_exist_info,
                )
                continue

            season_labels = "、".join(f"S{season}" for season in sorted(missing_info.keys(), key=lambda s: int(s)))
            # 已删除剧集的追新动作同样支持单剧覆盖，未覆盖时跟随全局配置
            action = self._resolve_no_exist_action(record)

            if action == NoExistAction.ADD_SUBSCRIBE.value:
                is_added = self.__add_subscribe_by_tv_no_exist_info(
                    tv_no_exist_info, key, force_full_season=True
                )
                if is_added:
                    note = f"已订阅 {season_labels}"
                    subscribed_count += 1
                    logger.info(f"【{title}】追新已订阅: {season_labels}")
                else:
                    note = f"订阅失败: {season_labels}"
                    logger.warning(f"【{title}】追新订阅失败: {season_labels}")
            elif action == NoExistAction.SET_ALL_EXIST.value:
                merged = merge_missing_seasons_into_ignored(ignored_seasons, tv_no_exist_info)
                with self._lock:
                    current = history_data.get("details", {}).get(key)
                    if current:
                        current["ignored_seasons"] = merged
                note = f"缺失季已标记存在: {season_labels}"
                logger.info(f"【{title}】{note}")
            else:
                note = f"发现缺失（仅记录）: {season_labels}"
                logger.info(f"【{title}】{note}")

            self.__update_follow_result(
                history_data,
                key,
                current_time_str,
                note if not baseline_note else f"{note}（{baseline_note}）",
                snapshot=tv_no_exist_info,
            )

        return len(targets), subscribed_count

    def __reconstruct_baseline(
        self, tmdbid: int, cutoff_time: Optional[str]
    ) -> Optional[Dict[int, List[int]]]:
        """按删除时间推断归档前已存在的季，用于补齐升级前旧记录缺失的集级基线。

        取 TMDB 季列表中首播日期不晚于 ``cutoff_time`` 的季，按当时的总集数
        视为完整存在，这样旧季不会被当成缺失重复订阅，只有之后新增的季会补订。
        取不到 TMDB 季信息时返回 ``None``，由调用方跳过本轮追新。
        """
        if not cutoff_time:
            cutoff_date = None
        else:
            try:
                cutoff_date = datetime.datetime.strptime(
                    str(cutoff_time)[:10], "%Y-%m-%d"
                ).date()
            except ValueError:
                cutoff_date = None

        try:
            seasons_info = self._tmdbChain.tmdb_seasons(tmdbid=tmdbid) or []
        except Exception as e:
            logger.warning(f"获取 TMDB 季列表失败 tmdbid={tmdbid}: {str(e)}")
            return None
        if not seasons_info:
            return None

        baseline: Dict[int, List[int]] = {}
        for season_info in seasons_info:
            season_number = getattr(season_info, "season_number", None)
            if season_number is None:
                continue
            air_date_str = getattr(season_info, "air_date", None)
            air_date = None
            if air_date_str:
                try:
                    air_date = datetime.datetime.strptime(
                        str(air_date_str)[:10], "%Y-%m-%d"
                    ).date()
                except ValueError:
                    air_date = None
            # 没有播出日期按新季处理，不进入基线；有日期则只认删除前已播出的季
            if cutoff_date and (air_date is None or air_date > cutoff_date):
                continue
            episode_count = getattr(season_info, "episode_count", None) or 0
            baseline[int(season_number)] = list(range(1, int(episode_count) + 1))
        return baseline

    def __update_follow_result(
        self,
        history_data: Dict[str, Any],
        key: str,
        current_time_str: str,
        note: str,
        snapshot: Optional[TvNoExistInfo] = None,
    ) -> None:
        """写回追新结果，保持「已删除」状态不被检测结果覆盖。"""
        with self._lock:
            record = history_data.get("details", {}).get(key)
            if not record:
                return
            record["follow_check_time"] = current_time_str
            record["follow_note"] = note
            if snapshot:
                old_snapshot = record.get("tv_no_exist_info") or {}
                if not snapshot.get("existing_seasons") and old_snapshot.get("existing_seasons"):
                    snapshot["existing_seasons"] = old_snapshot["existing_seasons"]
                if not snapshot.get("media_item_id") and old_snapshot.get("media_item_id"):
                    snapshot["media_item_id"] = old_snapshot["media_item_id"]
                record["tv_no_exist_info"] = snapshot
            record["last_check"] = current_time_str[5:16]
            self.save_data("history", history_data)

    def __get_item_no_exist_info(
        self,
        item_dict: dict[str, Any],
        ignored_seasons: Optional[List[int]] = None,
        force_full_season: bool = False
    ) -> tuple[bool, TvNoExistInfo]:
        """获取缺失集数，支持忽略指定季

        :param force_full_season: 已删除剧集追新时为 True，忽略「仅检查已有季缺失」配置，
                                  把基线之外的季（含全新季）全部按缺失处理
        """
        ignored_seasons = ignored_seasons or []
        only_season_exist = self._only_season_exist and not force_full_season
        title = item_dict.get("title") or item_dict.get("original_title") or "未知"

        tv_no_exist_info = create_tv_no_exist_info(
            title=title,
            year=item_dict.get("year", ""),
            path=item_dict.get("path", ""),
        )

        tmdbid: int | None = item_dict.get("tmdbid")
        if not tmdbid:
            logger.debug(f"【{title}】未获取到TMDBID, 跳过获取缺失集数")
            return False, tv_no_exist_info

        tv_no_exist_info["tmdbid"] = tmdbid

        mtype = item_dict.get("item_type")
        if not mtype or mtype != MediaType.TV.value:
            logger.debug(f"【{title}】媒体类型不为电视剧, 跳过获取缺失集数")
            return False, tv_no_exist_info

        # 添加不存在的季集信息
        def __append_season_info(
            season: int,
            episode_no_exist: List[int],
            episode_total: int,
            episode_total_unfiltered: int,
        ):
            logger.debug(f"添加【{title}】第【{season}】季缺失集：{episode_no_exist}")
            season_info: GetMissingEpisodesInfo = {
                "season": season,
                "episode_no_exist": episode_no_exist,
                "episode_total": episode_total,
                "episode_total_unfiltered": episode_total_unfiltered,
            }
            
            tv_no_exist_info["season_episode_no_exist_info"][str(season)] = season_info
            logger.debug(f"【{title}】缺失季集数的电视剧信息：{tv_no_exist_info}")

        exist_season_info = item_dict.get("seasoninfo") or {}
        logger.debug(f"【{title}】在媒体库已有季集信息：{exist_season_info}")

        # 归档需要留完整底账：把媒体库里实际存在的季集和条目ID一并写进快照
        tv_no_exist_info["existing_seasons"] = {
            str(season): sorted({int(episode) for episode in (episodes or []) if episode is not None})
            for season, episodes in exist_season_info.items()
            if season is not None
        }
        tv_no_exist_info["media_item_id"] = str(item_dict.get("item_id")) if item_dict.get("item_id") else None

        # 获取媒体信息
        try:
            tmdbinfo = self._mediaChain.recognize_media(
                mtype=MediaType.TV,
                media_source=MediaSource.TMDB,
                media_id=str(tmdbid),
            )
        except Exception as e:
            logger.error(f"获取媒体信息失败: {str(e)}")
            return False, tv_no_exist_info

        if tmdbinfo:
            # 获取剧集状态并转换为中文
            status = getattr(tmdbinfo, 'status', 'Unknown')
            status_cn = self.__convert_status_to_cn(status)
            
            tv_no_exist_info["poster_path"] = (
                tmdbinfo.poster_path
                or tv_no_exist_info.get("poster_path", default_poster_path)
            )
            # 背景图取不到时保留上一次的值，避免某次 TMDB 返回不全就把已有背景抹掉
            tv_no_exist_info["backdrop_path"] = (
                tmdbinfo.backdrop_path
                or tv_no_exist_info.get("backdrop_path", "")
            )
            tv_no_exist_info["vote_average"] = (
                tmdbinfo.vote_average
                or tv_no_exist_info.get("vote_average", 0.0)
            )
            tv_no_exist_info["last_air_date"] = (
                tmdbinfo.last_air_date
                or tv_no_exist_info.get("last_air_date", "未知")
            )
            tv_no_exist_info["status"] = status
            tv_no_exist_info["status_cn"] = status_cn

            # 检查tmdbinfo.seasons是否存在
            if not getattr(tmdbinfo, 'seasons', None):
                logger.debug(f"【{title}】未获取到TMDB季集信息, 跳过获取缺失集数")
                return False, tv_no_exist_info
            
            tmdbinfo_seasons = tmdbinfo.seasons.items()
            if not tmdbinfo_seasons:
                logger.debug(f"【{title}】未获取到TMDB季集信息, 跳过获取缺失集数")
                return False, tv_no_exist_info

            if not exist_season_info and not only_season_exist:
                logger.debug(f"【{title}】全部季不存在, 添加全部季集数")
                # 全部季不存在
                for season, _ in tmdbinfo_seasons:
                    # 检查是否跳过S00季
                    if season == 0 and not self._include_s00_season:
                        logger.debug(f"【{title}】跳过S00季检测")
                        continue
                    
                    # 检查是否被手动忽略
                    if season in ignored_seasons:
                        logger.info(f"【{title}】第【{season}】季已在忽略列表中，跳过检测")
                        continue
                    
                    filted_episodes, episode_total_unfiltered = self.__load_season_episodes(
                        tmdbid, season, title
                    )
                    if not filted_episodes:
                        logger.debug(f"【{title}】第【{season}】季未获取到TMDB集数信息, 跳过")
                        continue
                        
                    # 判断用户是否已经添加订阅
                    if self._subOper.exists(MediaSource.TMDB, str(tmdbid), season=season):
                        logger.info(f"【{title}】第【{season}】季已存在订阅, 跳过")
                        continue

                    __append_season_info(
                        season=season,
                        episode_no_exist=[],
                        episode_total=len(filted_episodes),
                        episode_total_unfiltered=episode_total_unfiltered,
                    )
            else:
                logger.debug(f"【{title}】检查每季缺失的集")
                # 检查每季缺失的季集
                for season, _ in tmdbinfo_seasons:
                    # 检查是否跳过S00季
                    if season == 0 and not self._include_s00_season:
                        logger.debug(f"【{title}】跳过S00季检测")
                        continue
                    
                    # 检查是否被手动忽略
                    if season in ignored_seasons:
                        logger.info(f"【{title}】第【{season}】季已在忽略列表中，跳过检测")
                        continue
                    
                    filted_episodes, episode_total_unfiltered = self.__load_season_episodes(
                        tmdbid, season, title
                    )
                    logger.debug(f"【{title}】第【{season}】季在TMDB的集数信息: {filted_episodes}")
                    if not filted_episodes:
                        logger.debug(f"【{title}】第【{season}】季未获取到TMDB集数信息, 跳过")
                        continue
                        
                    # 该季总集数（筛选后的）
                    episode_total = len(filted_episodes)

                    # 该季已存在的集
                    exist_episode = exist_season_info.get(season)
                    logger.debug(f"【{title}】第【{season}】季在媒体库已存在的集数信息: {exist_episode}")
                    
                    # 判断用户是否已经添加订阅
                    if self._subOper.exists(MediaSource.TMDB, str(tmdbid), season=season):
                        logger.info(f"【{title}】第【{season}】季已存在订阅, 跳过")
                        continue
                        
                    if exist_episode:
                        logger.debug(f"查找【{title}】第【{season}】季缺失集集数")
                        # 按TMDB集数查找缺失集
                        lack_episode = list(set(filted_episodes).difference(set(exist_episode)))

                        if not lack_episode:
                            logger.debug(f"【{title}】第【{season}】季全部集存在")
                            continue

                        # 添加不存在的季集信息
                        __append_season_info(
                            season=season,
                            episode_no_exist=lack_episode,
                            episode_total=episode_total,
                            episode_total_unfiltered=episode_total_unfiltered,
                        )
                    else:
                        logger.debug(f"【{title}】第【{season}】季全集不存在")
                        # 该季全集不存在，选项仅检查已有季缺失未开启时添加全部集
                        if not only_season_exist:
                            __append_season_info(
                                season=season,
                                episode_no_exist=[],
                                episode_total=episode_total,
                                episode_total_unfiltered=episode_total_unfiltered,
                            )

            logger.debug(f"【{title}】季集信息: {tv_no_exist_info}")

            # 存在不完整的剧集
            if tv_no_exist_info["season_episode_no_exist_info"]:
                logger.debug("媒体库中已存在部分剧集")
                return True, tv_no_exist_info

            # 全部存在
            logger.debug(f"【{title}】所有季集均已存在/订阅")
            return True, tv_no_exist_info
            
        else:
            logger.debug(f"【{title}】未获取到TMDB信息, 跳过获取缺失集数")
            return False, tv_no_exist_info

    def __convert_status_to_cn(self, status: str) -> str:
        """将剧集状态转换为中文"""
        status_mapping = {
            'Returning Series': '播出中',
            'Planned': '计划中',
            'In Production': '制作中',
            'Ended': '已完结',
            'Canceled': '已取消',
            'Cancelled': '已取消',
            'Pilot': '试播集',
            'Released': '已发布',
            'Post Production': '后期制作',
            'Returning': '回归中',
            'Rumored': '传言中',
            'In Development': '开发中',
            'Unknown': '未知状态',
            '': '未知状态',
        }
        
        # 尝试直接匹配
        if status in status_mapping:
            return status_mapping[status]
        
        # 尝试模糊匹配
        status_lower = status.lower()
        for key, value in status_mapping.items():
            if key.lower() in status_lower or status_lower in key.lower():
                return value
        
        # 默认返回原始状态
        return status

    def __load_season_episodes(self, tmdbid, season, title):
        """一次 TMDB 季详情请求，同时得出「筛选后集号列表」与「实际总集数」。

        原先 __filter_episodes 与 __get_total_episodes_unfiltered 各自调用一次
        tmdb_episodes（宿主该方法无缓存，直连 TMDB），同一季每轮要发两次完全相同的
        请求；合并为一次后季详情请求量减半。
        实际总集数仍按原始返回条目数计（含未开播集），与旧实现口径一致。
        """
        try:
            episodes_info = self._tmdbChain.tmdb_episodes(tmdbid=tmdbid, season=season)
        except Exception as e:
            logger.error(f"获取TMDB剧集信息失败: {str(e)}")
            return [], 0

        total_unfiltered = len(episodes_info) if episodes_info else 0
        episodes = []
        
        # 如果需要检查播出时间，预先获取当前时间
        current_date = None
        if self._only_aired:
            current_time = datetime.datetime.now(tz=pytz.timezone(settings.TZ))
            current_date = current_time.date()

        for episode in episodes_info:
            if episode:
                episode_name = f"【{title}】第 {season}季 {episode.name}"
                
                # 如果有播出日期
                if episode.air_date:
                    try:
                        air_date = datetime.datetime.strptime(episode.air_date, "%Y-%m-%d").date()
                        if self._only_aired:
                            # 仅已开播：只包括已开播的剧集
                            if air_date <= current_date:
                                episodes.append(episode.episode_number)
                            else:
                                logger.info(f"{episode_name} 发布时间: {episode.air_date} 未开播，不添加进集统计")
                        else:
                            # 全部：包括所有剧集，无论是否开播
                            episodes.append(episode.episode_number)
                    except ValueError as e:
                        logger.warning(f"{episode_name} 播出日期格式错误: {episode.air_date}, 错误: {str(e)}")
                        if not self._only_aired:
                            episodes.append(episode.episode_number)
                else:
                    # 没有播出日期，视为未开播
                    logger.info(f"{episode_name} 没有播出日期信息，视为未开播，不添加进集统计")
                    if not self._only_aired:
                        episodes.append(episode.episode_number)

        logger.debug(f"筛选后的集数: {episodes}")
        return episodes, total_unfiltered

    def _resolve_no_exist_action(self, record: Optional[Dict[str, Any]]) -> str:
        """取这条记录生效的缺失处理方式：单剧覆盖优先，未覆盖时回落全局配置。

        覆盖值只接受 ``NoExistAction`` 的枚举取值，脏数据一律忽略并回落全局，
        避免历史遗留字段让这部剧既不订阅也不记录。
        """
        override = (record or {}).get("action_override")
        if override and any(member.value == override for member in NoExistAction):
            return override
        return self._no_exist_action

    def set_action_history(self, key: str, action: str, apikey = None):
        """设置单条剧集的缺失处理方式，``action`` 传空串表示恢复跟随全局。

        覆盖只改变「算出缺失之后做什么」，不影响是否检测这部剧；要完全不检测请用跳过。
        """
        logger.info(f"开始设置单剧缺失处理方式: {key} -> {action or '跟随全局'}")
        if apikey and apikey != settings.API_TOKEN:
            logger.warning("API密钥错误")
            return schemas.Response(success=False, message="API密钥错误")

        valid_actions = [member.value for member in NoExistAction]
        if action and action not in valid_actions:
            logger.warning(f"无效的缺失处理方式: {action}")
            return schemas.Response(success=False, message="无效的缺失处理方式")

        with self._lock:
            historys = self.get_data("history")
            if not historys or not historys.get("details"):
                logger.warning("未找到检查记录")
                return schemas.Response(success=False, message="未找到检查记录")

            record = historys["details"].get(key)
            if not record:
                logger.warning(f"记录不存在: {key}")
                return schemas.Response(success=False, message="记录不存在")

            record["action_override"] = action or ""
            self.save_data("history", historys)
            effective = self._resolve_no_exist_action(record)
            title = (record.get("tv_no_exist_info") or {}).get("title") or "该记录"

        message = (
            f"{title} 的缺失处理方式已改为「{effective}」"
            if action else f"{title} 已恢复跟随全局（{effective}）"
        )
        logger.info(f"{message}: {key}")
        return schemas.Response(
            success=True,
            message=message,
            data=self._build_board_data(),
        )

    def _current_config(self) -> Dict[str, Any]:
        """返回当前生效的完整插件配置。"""
        return {
            "enabled": self._enabled,
            "cron": self._cron,
            "onlyonce": self._onlyonce,
            "clear": self._clear,
            "clear_scope": self._clear_scope,
            "clear_stale_days": self._clear_stale_days,
            "only_season_exist": self._only_season_exist,
            "only_aired": self._only_aired,
            "no_exist_action": self._no_exist_action,
            "save_path_replaces": "\n".join(map(str, self._save_path_replaces)),
            "whitelist_librarys": self._whitelist_librarys,
            "whitelist_media_servers": ",".join(self._whitelist_media_servers) if self._whitelist_media_servers else "",
            "auto_skip_finished": self._auto_skip_finished,
            "include_s00_season": self._include_s00_season,
            "show_sidebar_nav": self._show_sidebar_nav,
        }

    def _update_config(self):
        """更新配置"""
        config = self._current_config()
        logger.info(f"更新配置 {config}")
        self.update_config(config)

    def _library_options(self) -> Tuple[List[str], List[str]]:
        """获取媒体服务器名称与媒体库名称候选项。"""
        mediaservers: List[str] = []
        libraries: List[str] = []
        try:
            services = self._msHelper.get_services() or []
        except Exception as e:
            logger.error(f"获取媒体服务器列表失败: {str(e)}")
            return [], []
        for mediaserver in services:
            if not mediaserver:
                continue
            mediaservers.append(mediaserver)
            try:
                for library in self._msChain.librarys(mediaserver) or []:
                    if library and library.name:
                        libraries.append(library.name)
            except Exception as e:
                logger.warning(f"获取 {mediaserver} 媒体库列表失败: {str(e)}")
        return sorted(set(mediaservers)), sorted(set(libraries))

    def _build_media_link(
        self,
        tmdbid: int,
        title: Optional[str] = None,
        year: Optional[str] = None,
    ) -> str:
        """生成剧集在 MoviePilot 前端媒体详情页的链接。

        前端 /media 页按 ``media_source`` + ``media_id`` 定位媒体，``title`` 与 ``year``
        用于页面标题与识别兜底，四个参数齐全才能直达；旧写法 ``mediaid=tmdb:xxx`` 前端
        不识别，点击只会落到空白页。中文参数统一做 URL 编码。
        """
        params = [f"media_source={MediaSource.TMDB.value}", f"media_id={tmdbid}"]
        if title and title != "未知":
            params.append(f"title={quote(str(title))}")
        if year and year != "未知":
            params.append(f"year={quote(str(year))}")
        params.append(f"type={quote(MediaType.TV.value)}")
        link = f"#/media?{'&'.join(params)}"
        mp_domain = settings.MP_DOMAIN()
        if not mp_domain:
            return link
        if mp_domain.endswith("/"):
            return f"{mp_domain}{link}"
        return f"{mp_domain}/{link}"

    def _build_board_data(self, include_items: bool = True) -> Dict[str, Any]:
        """构建 Vue 工作台需要的统计、记录与配置数据。

        :param include_items: False 时只回统计与运行标志，供检测中的轮询使用
        """
        historys = self.get_data("history")
        details = (historys or {}).get("details", {}) or {}

        status_tags = {
            HistoryStatus.FAILED.value: "failed",
            HistoryStatus.ADDED_RSS.value: "added_rss",
            HistoryStatus.ALL_EXIST.value: "all_exist",
            HistoryStatus.NO_EXIST.value: "no_exist",
            HistoryStatus.DELETED.value: "deleted",
        }
        records: List[Dict[str, Any]] = []
        for unique, item in details.items():
            item = item or {}
            tv_info = item.get("tv_no_exist_info") or {}
            season_info = tv_info.get("season_episode_no_exist_info") or {}
            seasons: List[Dict[str, Any]] = []
            episode_missing = False
            for season_key, season_value in season_info.items():
                season_value = season_value or {}
                no_exist = list(season_value.get("episode_no_exist") or [])
                if no_exist:
                    episode_missing = True
                seasons.append(
                    {
                        "season": season_value.get("season", season_key),
                        "episode_no_exist": no_exist,
                        "episode_total": season_value.get("episode_total", 0),
                        "episode_total_unfiltered": season_value.get("episode_total_unfiltered", 0),
                    }
                )
            exist_status = item.get("exist_status") or HistoryStatus.UNKNOW.value
            tmdbid = tv_info.get("tmdbid", 0) or 0
            is_deleted = bool(item.get("deleted"))
            existing_label = format_existing_seasons_label(
                tv_info.get("existing_seasons"), season_info
            )
            records.append(
                {
                    "unique": unique,
                    "title": tv_info.get("title", "未知"),
                    "year": tv_info.get("year", "未知"),
                    "path": tv_info.get("path", "未知"),
                    "tmdbid": tmdbid,
                    "poster": tv_info.get("poster_path") or default_poster_path,
                    "backdrop": tv_info.get("backdrop_path") or "",
                    "vote_average": tv_info.get("vote_average", 0.0),
                    "last_air_date": tv_info.get("last_air_date", "未知"),
                    "status": tv_info.get("status", "Unknown"),
                    "status_cn": tv_info.get("status_cn", "未知"),
                    "exist_status": exist_status,
                    "mediaserver": item.get("mediaserver") or "",
                    "library": item.get("library") or "",
                    "skip": bool(item.get("skip", False)),
                    "ignored_seasons": item.get("ignored_seasons") or [],
                    "seasons": seasons,
                    "episode_missing": episode_missing,
                    "last_check": item.get("last_check", ""),
                    "last_check_full": item.get("last_check_full", ""),
                    "first_found_time": item.get("first_found_time", ""),
                    "last_status_change": item.get("last_status_change", item.get("last_check_full", "")),
                    "media_link": self._build_media_link(
                        tmdbid, tv_info.get("title"), tv_info.get("year")
                    ),
                    "deleted": is_deleted,
                    "deleted_at": item.get("deleted_at") or "",
                    "last_seen": item.get("last_seen") or "",
                    "delete_source": item.get("delete_source") or "",
                    "follow": bool(item.get("follow", False)),
                    "follow_note": item.get("follow_note") or "",
                    "follow_check_time": item.get("follow_check_time") or "",
                    "existing_label": existing_label,
                    "action_override": item.get("action_override") or "",
                    "effective_action": self._resolve_no_exist_action(item),
                }
            )

        records.sort(key=lambda record: record.get("last_status_change") or "", reverse=True)

        statistics: Dict[str, int] = {
            "all": len(records),
            "total": 0,
            "no_exist": 0,
            "not_all_no_exist": 0,
            "failed": 0,
            "all_exist": 0,
            "added_rss": 0,
            "skipped": 0,
            "finished": 0,
            "deleted": 0,
        }
        last_check = ""
        for record in records:
            tags: List[str] = ["all"]
            # 归档且未开启追新的记录本轮没有任何检测动作：常规检测以在库条目为入口碰不到它，
            # 追新区又不选它，因此不计入「总处理」；开启追新后才算被处理的记录。
            is_processed = not record["skip"] and (not record["deleted"] or record["follow"])
            if is_processed:
                tags.append("total")
                statistics["total"] += 1
            status_tag = status_tags.get(record["exist_status"])
            if status_tag:
                tags.append(status_tag)
                statistics[status_tag] += 1
            if record["skip"]:
                tags.append("skipped")
                statistics["skipped"] += 1
            if record["status_cn"] == "已完结":
                tags.append("finished")
                statistics["finished"] += 1
            if status_tag == "no_exist" and record["episode_missing"]:
                tags.append("not_all_no_exist")
                statistics["not_all_no_exist"] += 1
            record["tags"] = tags
            if record.get("last_check_full") and record["last_check_full"] > last_check:
                last_check = record["last_check_full"]

        if not include_items:
            # 轮询模式：不回卡片明细（整表约 70KB），也不枚举媒体库
            # （_library_options 每次都要向媒体服务器发请求，没必要跟着轮询重复做）
            return {
                "summary": True,
                "statistics": statistics,
                "running": self._checking,
                "enabled": self._enabled,
                "cron": self._cron,
                "last_check": last_check,
            }

        mediaserver_options, library_options = self._library_options()
        return {
            "config": self._current_config(),
            "library_options": library_options,
            "mediaserver_options": mediaserver_options,
            "statistics": statistics,
            "items": records,
            "history_type": self._current_history_type,
            "running": self._checking,
            "enabled": self._enabled,
            "cron": self._cron,
            "last_check": last_check,
            # 最近一次完整扫描的计数摘要，供详情页顶部一行展示
            "last_scan": self.get_data("scan_summary") or {},
        }

    def get_status(self, summary: str = "") -> schemas.Response:
        """返回 Vue 工作台总览数据。

        :param summary: 传 1/true 时只返回统计与运行标志，供检测中的前端轮询使用
        """
        try:
            light = str(summary or "").strip().lower() in ("1", "true", "yes", "on")
            return schemas.Response(
                success=True, data=self._build_board_data(include_items=not light)
            )
        except Exception as e:
            logger.error(f"获取剧集管家总览数据失败: {str(e)}")
            return schemas.Response(success=False, message=f"获取总览数据失败: {str(e)}")

    def update_settings(self, payload: Dict[str, Any]) -> schemas.Response:
        """保存 Vue 工作台提交的插件设置并重建运行服务。"""
        if not isinstance(payload, dict):
            return schemas.Response(success=False, message="配置格式不正确")
        config = self._current_config()
        for key in self._EDITABLE_SETTINGS_KEYS:
            if key in payload:
                config[key] = payload[key]
        if isinstance(config.get("save_path_replaces"), list):
            config["save_path_replaces"] = "\n".join(
                str(line).strip() for line in config["save_path_replaces"] if str(line).strip()
            )
        try:
            self._load_config(config)
            self._update_config()
            self.stop_service()
            if self._enabled or self._onlyonce:
                self._start_service()
            return schemas.Response(
                success=True,
                message="设置已保存",
                data=self._build_board_data(),
            )
        except Exception as e:
            logger.error(f"保存剧集管家设置失败: {str(e)}")
            return schemas.Response(success=False, message=f"保存设置失败: {str(e)}")

    def run_check_now(self) -> schemas.Response:
        """在后台线程中立即执行一次缺失检测。"""
        # 闸门在这里就占用，用户才能立刻拿到「正在进行中」的明确反馈；
        # 所有权随后交给工作线程，由它负责释放。
        if not self.__begin_scan():
            return schemas.Response(success=False, message="检测正在进行中，请稍后再试")
        try:
            Thread(target=self.__run_check_thread, daemon=True).start()
        except Exception as e:
            # 线程没起来必须归还闸门，否则之后永远触发不了检测
            self.__end_scan()
            logger.error(f"启动检测线程失败: {str(e)}")
            return schemas.Response(success=False, message=f"启动检测失败: {str(e)}")
        return schemas.Response(success=True, message="已触发一次检测")

    def __run_check_thread(self):
        """执行一次检测。闸门已由 run_check_now 占用，这里只负责跑完并释放。"""
        try:
            self.__get_mediaserver_tv_info()
        except Exception as e:
            logger.error(f"手动触发检测失败: {str(e)}")
        finally:
            self.__end_scan()

    def stop_service(self):
        """停止服务"""
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._event.set()
                    self._scheduler.shutdown()
                    self._event.clear()
                self._scheduler = None
        except Exception as e:
            logger.error(f"停止服务时出错: {str(e)}")


    def __check_and_add_subscribe(
        self,
        title: str,
        year: str,
        tmdbid: int,
        season: int,
        save_path: str | None = None,
        total_episode: int | None = None,
        total_episode_unfiltered: int | None = None,
    ):
        """检查并添加订阅"""
        title_season = f"{title} ({year}) 第 {season} 季"
        logger.info(f"开始检查 {title_season} 是否已添加订阅")

        save_path_replaced = None
        if self._save_path_replaces and save_path:
            for _save_path_replace in self._save_path_replaces:
                replace_list = [part.strip() for part in _save_path_replace.split(":") if part.strip()]
                if len(replace_list) < 2:
                    continue
                _lib_path_str, _save_path_str = replace_list[:2]
                logger.debug(f"替换路径: {_lib_path_str} -> {_save_path_str}")
                # 使用精确的路径前缀匹配
                if save_path.startswith(_lib_path_str):
                    save_path_replaced = save_path.replace(_lib_path_str, _save_path_str, 1)
                    logger.info(f"{title_season} 的下载路径替换为: {save_path_replaced}")
                    break

        # 判断用户是否已经添加订阅
        if self._subOper.exists(MediaSource.TMDB, str(tmdbid), season=season):
            logger.info(f"{title_season} 订阅已存在")
            return True

        logger.info(f"开始添加订阅: {title_season}")

        if not isinstance(season, int):
            try:
                season = int(season)
            except ValueError:
                logger.warning("season 无法转换为整数")
                return False

        # 优先使用实际总集数，如果未提供则使用筛选后的总集数
        final_total_episode = total_episode_unfiltered or total_episode
        
        # 添加订阅
        try:
            is_add_success, msg = self._subChain.add(
                title=title,
                year=year,
                mtype=MediaType.TV,
                media_source=MediaSource.TMDB,
                media_id=str(tmdbid),
                season=season,
                exist_ok=True,
                username=self.plugin_name,
                save_path=save_path_replaced,
                total_episode=final_total_episode,
            )
            logger.debug(f"添加订阅 {title_season} 结果: {is_add_success}, {msg}")
            if not is_add_success:
                logger.warning(f"添加订阅 {title_season} 失败: {msg}")
                return False
            logger.info(f"已添加订阅: {title_season}")
            return True
        except Exception as e:
            logger.error(f"添加订阅失败: {str(e)}")
            return False

    @staticmethod
    def __update_exist_status_by_unique(historys, unique: str, new_status: str):
        """根据唯一标识更新存在状态"""
        if unique in historys["details"]:
            historys["details"][unique]["exist_status"] = new_status
            # 状态变化时更新最后状态变更时间
            current_time = datetime.datetime.now(tz=pytz.timezone(settings.TZ))
            historys["details"][unique]["last_status_change"] = current_time.strftime("%Y-%m-%d %H:%M:%S")
            logger.info(f"更新检查记录 {unique} 状态为: {new_status}")
            return True, historys
        else:
            logger.warning(f"unique: {unique} 不在历史记录里")
            return False, historys

    def __add_subscribe_by_tv_no_exist_info(
        self, tv_no_exist_info: TvNoExistInfo, unique: str, force_full_season: bool = False
    ) -> bool:
        """根据缺失信息添加订阅"""
        title = tv_no_exist_info.get("title")
        year = tv_no_exist_info.get("year")
        tmdbid = tv_no_exist_info.get("tmdbid")
        save_path = tv_no_exist_info.get("path")
        season_episode_no_exist_info = tv_no_exist_info.get("season_episode_no_exist_info", {})

        if not title or not year or not tmdbid or not season_episode_no_exist_info:
            logger.warning(f"unique: {unique} 季集信息不完整, 跳过订阅")
            return False

        all_success = True
        for season_key in season_episode_no_exist_info.keys():
            season_info = season_episode_no_exist_info.get(season_key)
            total_episode = season_info.get("episode_total") if season_info else None
            total_episode_unfiltered = season_info.get("episode_total_unfiltered") if season_info else None
            episode_no_exist = season_info.get("episode_no_exist") if season_info else None

            if not episode_no_exist:
                logger.info(
                    f"【{title}】第 {season_key} 季所有集均缺失, 仅添加已有季选项为: "
                    f"{self._only_season_exist and not force_full_season}"
                )
                if self._only_season_exist and not force_full_season:
                    logger.info(f"跳过订阅:【{title}】第 {season_key} 季")
                    continue
                else:
                    logger.info(f"添加订阅:【{title}】第 {season_key} 季")
            else:
                logger.info(f"【{title}】第 {season_key} 季缺失集数: {episode_no_exist}, 将添加订阅")

            # 转换季节号为整数
            try:
                season_int = int(season_key)
            except ValueError:
                logger.warning(f"season {season_key} 无法转换为整数，跳过此季")
                all_success = False
                continue

            is_add_subscribe_success = self.__check_and_add_subscribe(
                title=title,
                year=year,
                tmdbid=tmdbid,
                season=season_int,
                save_path=save_path,
                total_episode=total_episode,
                total_episode_unfiltered=total_episode_unfiltered,
            )
            if not is_add_subscribe_success:
                all_success = False

        return all_success

    def __add_subscribe_by_unique(self, historys, unique: str):
        """根据唯一标识添加订阅"""
        if unique in historys["details"]:
            tv_no_exist_info = historys["details"][unique]["tv_no_exist_info"]
            is_add_subscribe_success = self.__add_subscribe_by_tv_no_exist_info(tv_no_exist_info, unique)
            if is_add_subscribe_success:
                is_update_exist_status_success, historys = self.__update_exist_status_by_unique(
                    historys=historys,
                    unique=unique,
                    new_status=HistoryStatus.ADDED_RSS.value,
                )
                return is_update_exist_status_success, historys
            else:
                return False, historys
        else:
            logger.warning(f"unique: {unique} 不在历史记录里")
            return False, historys

    def delete_history(self, key: str, apikey: Optional[str] = None):
        """删除同步检查记录"""
        logger.info(f"开始删除检查记录: {key}")
        if apikey and apikey != settings.API_TOKEN:
            logger.warning("API密钥错误")
            return schemas.Response(success=False, message="API密钥错误")
            
        historys = self.get_data("history")
        if not historys:
            logger.warning("未找到检查记录")
            return schemas.Response(success=False, message="未找到检查记录")

        is_success, historys = GetMissingEpisodes.__remove_history_by_unique(historys, key)

        if is_success:
            logger.info(f"删除检查记录 {key} 成功")
            self.save_data("history", historys)
            return schemas.Response(success=True, message="删除成功")
        else:
            logger.warning(f"删除检查记录 {key} 失败")
            return schemas.Response(success=False, message="删除失败")

    def add_subscribe_history(self, key: str, apikey: Optional[str] = None):
        """订阅缺失检查记录"""
        logger.info(f"开始订阅检查记录: {key}")
        if apikey and apikey != settings.API_TOKEN:
            logger.warning("API密钥错误")
            return schemas.Response(success=False, message="API密钥错误")
            
        historys = self.get_data("history")
        if not historys:
            logger.warning("未找到检查记录")
            return schemas.Response(success=False, message="未找到检查记录")

        is_success, historys = self.__add_subscribe_by_unique(historys, key)
        if is_success:
            logger.info(f"添加 {key} 订阅成功")
            self.save_data("history", historys)
            return schemas.Response(success=True, message="订阅成功")
        else:
            logger.warning(f"添加 {key} 订阅失败")
            return schemas.Response(success=False, message="订阅失败")

    def set_all_exist_history(self, key: str, seasons: Optional[str] = None, apikey: Optional[str] = None):
        """标记存在检查记录：只把选中的缺失季并入忽略季，未选中的季继续算缺失。

        ``seasons`` 为逗号分隔的季号；留空表示标记全部缺失季，兼容旧的整条标记入口。
        只有当全部缺失季都被标记后，记录状态才转为「全部存在」。
        """
        logger.info(f"开始标记存在检查记录: {key}, 选中季: {seasons or '全部缺失季'}")
        if apikey and apikey != settings.API_TOKEN:
            logger.warning("API密钥错误")
            return schemas.Response(success=False, message="API密钥错误")

        current_time = datetime.datetime.now(tz=pytz.timezone(settings.TZ))
        current_time_str = current_time.strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            historys = self.get_data("history")
            if not historys:
                logger.warning("未找到检查记录")
                return schemas.Response(success=False, message="未找到检查记录")
            # 数据改动全部交给纯函数处理，端点只负责加锁与落盘
            is_success, message = apply_missing_season_mark(
                historys, key, seasons, current_time_str
            )
            if is_success:
                self.save_data("history", historys)

        if is_success:
            logger.info(f"标记存在 {key} 成功: {message}")
            return schemas.Response(success=True, message=message)
        logger.warning(f"标记存在 {key} 失败: {message}")
        return schemas.Response(success=False, message=message)

    def unset_ignored_season_history(self, key: str, seasons: Optional[str] = None, apikey: Optional[str] = None):
        """撤销忽略季：把选中的季移出忽略列表，下一轮检测重新评估这些季。

        ``seasons`` 为逗号分隔的季号；留空表示撤销该记录全部已忽略季。
        """
        logger.info(f"开始撤销忽略季: {key}, 选中季: {seasons or '全部已忽略季'}")
        if apikey and apikey != settings.API_TOKEN:
            logger.warning("API密钥错误")
            return schemas.Response(success=False, message="API密钥错误")

        current_time = datetime.datetime.now(tz=pytz.timezone(settings.TZ))
        current_time_str = current_time.strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            historys = self.get_data("history")
            if not historys:
                logger.warning("未找到检查记录")
                return schemas.Response(success=False, message="未找到检查记录")
            # 数据改动交给纯函数，端点只负责加锁与落盘
            is_success, message = apply_ignored_season_unmark(
                historys, key, seasons, current_time_str
            )
            if is_success:
                self.save_data("history", historys)

        if is_success:
            logger.info(f"撤销忽略季 {key} 成功: {message}")
            return schemas.Response(success=True, message=message)
        logger.warning(f"撤销忽略季 {key} 失败: {message}")
        return schemas.Response(success=False, message=message)

    def toggle_skip_history(self, key: str, apikey: Optional[str] = None):
        """切换跳过状态"""
        logger.info(f"开始切换跳过状态: {key}")
        if apikey and apikey != settings.API_TOKEN:
            logger.warning("API密钥错误")
            return schemas.Response(success=False, message="API密钥错误")
            
        historys = self.get_data("history")
        if not historys:
            logger.warning("未找到检查记录")
            return schemas.Response(success=False, message="未找到检查记录")

        if key in historys["details"]:
            current_skip = historys["details"][key].get("skip", False)
            historys["details"][key]["skip"] = not current_skip
            # 跳过状态变化时也更新状态变更时间
            current_time = datetime.datetime.now(tz=pytz.timezone(settings.TZ))
            historys["details"][key]["last_status_change"] = current_time.strftime("%Y-%m-%d %H:%M:%S")
            self.save_data("history", historys)
            message = "取消跳过" if current_skip else "已跳过"
            logger.info(f"{message} {key}")
            return schemas.Response(success=True, message=f"{message}成功")
        else:
            logger.warning(f"切换跳过状态 {key} 失败")
            return schemas.Response(success=False, message="切换跳过状态失败")

    def toggle_follow_history(self, key: str, apikey: Optional[str] = None):
        """切换已删除剧集的追新状态：开启后每轮扫描用归档基线检测新季"""
        logger.info(f"开始切换追新状态: {key}")
        if apikey and apikey != settings.API_TOKEN:
            logger.warning("API密钥错误")
            return schemas.Response(success=False, message="API密钥错误")

        with self._lock:
            historys = self.get_data("history")
            if not historys or not historys.get("details"):
                logger.warning("未找到检查记录")
                return schemas.Response(success=False, message="未找到检查记录")

            record = historys["details"].get(key)
            if not record:
                logger.warning(f"记录不存在: {key}")
                return schemas.Response(success=False, message="记录不存在")
            if not record.get("deleted"):
                logger.warning(f"记录 {key} 不是已删除剧集，无法追新")
                return schemas.Response(success=False, message="只有已删除剧集可以追新")

            current_follow = bool(record.get("follow", False))
            record["follow"] = not current_follow
            current_time = datetime.datetime.now(tz=pytz.timezone(settings.TZ))
            current_time_str = current_time.strftime("%Y-%m-%d %H:%M:%S")
            record["last_status_change"] = current_time_str
            if record["follow"]:
                # 主动追新说明用户重新开始管这部剧，取消跳过才能计入总处理
                record["skip"] = False
                record["follow_note"] = "已开启追新，等待下一轮检测"
                message = "已开始追新"
            else:
                record["follow_note"] = ""
                message = "已停止追新"
            self.save_data("history", historys)

        logger.info(f"{message}: {key}")
        return schemas.Response(
            success=True,
            message=f"{message}成功",
            data=self._build_board_data(),
        )

    def set_history_type(self, history_type: str, apikey: Optional[str] = None):
        """设置历史数据类型"""
        logger.info(f"设置历史数据类型: {history_type}")
        if apikey and apikey != settings.API_TOKEN:
            logger.warning("API密钥错误")
            return schemas.Response(success=False, message="API密钥错误")
        
        # 验证历史数据类型是否有效
        valid_types = [dt.value for dt in HistoryDataType]
        if history_type not in valid_types:
            logger.warning(f"无效的历史数据类型: {history_type}")
            return schemas.Response(success=False, message="无效的历史数据类型")
        
        # 保存当前选中的历史数据类型
        self._current_history_type = history_type
        self.save_data("current_history_type", history_type)
        logger.info(f"历史数据类型已设置为: {history_type}")
        return schemas.Response(success=True, message="设置成功")

    def get_form(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        # 获取所有可用的媒体库
        available_libraries = []
        try:
            mediaservers = self._msHelper.get_services()
            if mediaservers:
                for mediaserver in mediaservers:
                    librarys = self._msChain.librarys(mediaserver)
                    for library in librarys:
                        available_libraries.append(library.name)
        except Exception as e:
            logger.error(f"获取媒体库列表失败: {str(e)}")
        
        # 去重并排序
        available_libraries = sorted(list(set(available_libraries)))
        
        # 构建媒体库选项
        library_items = [{"title": lib, "value": lib} for lib in available_libraries]
        
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                        },
                                    }
                                ],
                            },                            
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "only_aired",
                                            "label": "仅订阅已开播剧集",
                                            "hint": "开启：只订阅已开播的剧集；关闭：订阅所有剧集（包括未开播）",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "auto_skip_finished",
                                            "label": "自动跳过已完结剧集",
                                            "hint": "开启：已完结的剧集自动跳过检测；关闭：正常检测已完结剧集（注意：因TMDB剧集状态可随意编辑不一定准确，部分剧集实际并未完结却被标记成已完结，而导致插件漏检。出现此情况可自行去TMDB更改剧集状态，然后手动取消跳过该剧集。）",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "only_season_exist",
                                            "label": "仅检查已有季缺失",
                                            "hint": "开启：只检查媒体库中所有剧（除已跳过）已存在的季是否有集的缺失；关闭：检查媒体库中所有剧（除已跳过）是否存在季和集的缺失",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "include_s00_season",
                                            "label": "包含S00季检测",
                                            "hint": "开启：S00季（特别季/特典季）纳入缺失检测；关闭：跳过S00季检测",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "clear",
                                            "label": "清理检查记录",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "clear_scope",
                                            "label": "清理范围",
                                            "hint": "勾选「清理检查记录」后，下轮检测按此范围移除记录；全部=清空所有记录",
                                            "persistent_hint": True,
                                            "items": [
                                                {"title": "全部记录", "value": "all"},
                                                {"title": "仅已跳过的记录", "value": "skipped"},
                                                {"title": "仅已删除归档记录", "value": "deleted"},
                                                {"title": "仅长期未更新的记录", "value": "stale"},
                                            ],
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "clear_stale_days",
                                            "label": "未更新天数",
                                            "type": "number",
                                            "min": 1,
                                            "hint": "仅「清理范围=长期未更新」时生效，超过该天数未再检测到的记录会被移除",
                                            "persistent_hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "onlyonce",
                                            "label": "立即运行一次",
                                        },
                                    }
                                ],
                            },                   
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VCronField",
                                        "props": {
                                            "model": "cron",
                                            "label": "执行周期",
                                            "placeholder": "5位cron表达式, 留空自动",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "no_exist_action",
                                            "label": "全局缺失处理方式",
                                            "items": [
                                                {
                                                    "title": f"{NoExistAction.ONLY_HISTORY.value}",
                                                    "value": f"{NoExistAction.ONLY_HISTORY.value}",
                                                },
                                                {
                                                    "title": f"{NoExistAction.ADD_SUBSCRIBE.value}",
                                                    "value": f"{NoExistAction.ADD_SUBSCRIBE.value}",
                                                },
                                                {
                                                    "title": f"{NoExistAction.SET_ALL_EXIST.value}",
                                                    "value": f"{NoExistAction.SET_ALL_EXIST.value}",
                                                },
                                            ],
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 12},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "whitelist_librarys",
                                            "label": "电视剧媒体库白名单",
                                            "items": library_items,
                                            "multiple": True,
                                            "chips": True,
                                            "closable-chips": True,
                                            "placeholder": "请选择要检查的电视剧媒体库",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 12},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "whitelist_media_servers",
                                            "label": "媒体服务器名称白名单",
                                            "placeholder": "留空默认全部, 多个名称用英文逗号分隔: emby,embyA,embyB,jellyfin,plex",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "save_path_replaces",
                                            "label": "下载路径替换, 一行一个",
                                            "placeholder": "将媒体库电视剧的路径替换为下载路径, 用英文冒号作为分割。不输入则按默认下载路径处理。\n例如将'/media/library/tv/上载新生 (2020)'的下载路径设置为'/downloads/tv', 则输入 /media/library:/downloads",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '注意：插件首次检测时建议全局缺失处理方式选择[仅检查记录]，根据检查记录手动跳过一些不需要持续检测的剧集，然后再将缺失处理方式改为[添加到订阅]，以尽量避免因TMDB上某些剧集信息错误而产生误订阅。'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ],
            }
        ], {
            "enabled": False,
            "cron": "",
            "onlyonce": False,
            "only_season_exist": True,
            "only_aired": True,
            "auto_skip_finished": False,
            "include_s00_season": False,
            "show_sidebar_nav": True,
            "clear": False,
            "no_exist_action": NoExistAction.ONLY_HISTORY.value,
            "save_path_replaces": "",
            "whitelist_media_servers": "",
            "whitelist_librarys": [],
        }


    def get_page(self) -> List[Dict[str, Any]]:
        """详情页由 Vue 联邦组件渲染，这里固定返回空列表。

        注意：本方法必须保留定义，不能删除。宿主按「是否实现 get_page」计算
        `has_page`（metadata.py: supports_plugin_hook(instance, "get_page")），
        前端「插件面板」只列 has_page 为真的插件；删掉它会让本插件从面板消失。
        原先配套的 Vuetify JSON 回退实现（__get_legacy_page 等约 720 行）已确认
        无任何入口可达——get_render_mode 走 vuetify 分支时这里返回空页而非旧界面，
        回退并不成立，故一并移除，避免同一套界面两处维护。
        """
        return []


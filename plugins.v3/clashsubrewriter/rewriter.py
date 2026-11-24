# -*- coding: utf-8 -*-
"""
Clash 订阅改写引擎（纯文本定位改写，可离线单测）。

为什么要自己做改写而不是 ``yaml.safe_load`` + ``safe_dump``：
V2Board 输出的订阅文件混合了多种缩进风格（``proxy-groups`` 序列项缩进 2 格、
``rules`` 序列项缩进 1 格、节点行使用 ``- {name: xx, server: yy}`` 的 flow 风格），
任何 YAML 库整体重排都会把上万行文件改得面目全非，审阅时看不出到底动了哪里。
因此这里只定位并替换需要变的行，需要新增的行按目标缩进插入，其余字节一律不动，
改写结果与人工编辑保持一致。

安全约定：``rewrite()`` 只产出文本，``validate_result()`` 必须通过，调用方才允许落盘。
"""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Dict, List, Optional, Tuple

import yaml

#: 改写规则允许出现的字段，写错键名时直接报错，避免静默忽略
SPEC_KEYS = {"set", "insert_after", "delete", "delete_if_present", "groups",
             "references", "rules_prepend", "rules_append"}
#: 规则文本里的成员占位符：``- "@MEMBERS(正则)"``
MEMBERS_RE = re.compile(r"""^@MEMBERS\((?P<pattern>.*)\)$""")
#: 配置值占位符，由插件层注入（避免把面板密钥写进可发布的源码）
SECRET_TOKEN = "@SECRET"
#: YAML 序列项行
DASH_RE = re.compile(r"^(?P<indent>\s*)-(?P<gap>\s*)(?P<value>.*)$")
#: 裸写（plain scalar）最不安全的起始字符
_LEADING_RISK = "-?:,[]{}#&*!|>%@\"'"
#: Clash 内置出站目标，规则/成员引用它们不算悬空引用
_BUILTIN_TARGETS = {"DIRECT", "REJECT", "PASS", "COMPATIBLE"}
#: 变更说明里需要打码的顶层键
SENSITIVE_KEYS = {"secret", "password", "token", "uuid", "api_key", "apikey"}


class RewriteError(Exception):
    """改写规则不合法，或改写结果未通过校验；调用方必须放弃落盘。"""


def scalar_text(value: Any) -> str:
    """按人手书写习惯渲染标量：能安全裸写就不加引号。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if (text and text.strip() == text and text[0] not in _LEADING_RISK
            and ": " not in text and " #" not in text and "\n" not in text
            and not text.endswith(":")):
        try:
            if yaml.safe_load(text) == value:
                return text
        except yaml.YAMLError:
            pass
    return "'" + text.replace("'", "''") + "'"


def _mask(key: Any, value: Any) -> str:
    """生成变更说明文本，敏感键只记键名不记值。"""
    rendered = scalar_text(value)
    if str(key).lower() in SENSITIVE_KEYS:
        return f"{key}: ***（已打码）"
    return f"{key}: {rendered}"


def _unquote(value: str) -> str:
    """取出序列项内容，顺带剥掉一层引号。"""
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        inner = text[1:-1]
        return inner.replace("''", "'") if text[0] == "'" else inner
    return text


def parse_spec(text: Optional[str]) -> Dict[str, Any]:
    """解析并校验改写规则文本，返回规则字典；不合法时抛 :class:`RewriteError`。"""
    if not text or not text.strip():
        raise RewriteError("改写规则为空")
    try:
        spec = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise RewriteError(f"改写规则不是合法 YAML：{error}") from error
    if not isinstance(spec, dict):
        raise RewriteError("改写规则必须是一个 YAML 映射（顶层是 key: value）")
    unknown = set(spec) - SPEC_KEYS
    if unknown:
        raise RewriteError(f"改写规则含未知字段：{'、'.join(sorted(str(k) for k in unknown))}")
    for key in ("set", "insert_after"):
        value = spec.get(key)
        if value is None:
            continue
        if not isinstance(value, dict):
            raise RewriteError(f"{key} 必须是「键: 值」映射")
        for name, item in value.items():
            if not isinstance(item, (str, int, float, bool, type(None))):
                raise RewriteError(f"{key} 的 {name} 只支持标量（字符串/数字/布尔）")
    for key in ("delete", "rules_prepend", "rules_append"):
        value = spec.get(key)
        if value is None:
            continue
        if not isinstance(value, list) or any(not isinstance(i, str) for i in value):
            raise RewriteError(f"{key} 必须是字符串列表")
    if spec.get("delete_if_present") is not None:
        value = spec["delete_if_present"]
        if not isinstance(value, list) or any(not isinstance(i, str) for i in value):
            raise RewriteError("delete_if_present 必须是字符串列表")
    groups = spec.get("groups")
    if groups is not None and not isinstance(groups, str):
        raise RewriteError("groups 必须是字符串形式的 YAML 片段（建议用 |- 写法）")
    references = spec.get("references") or []
    if not isinstance(references, list):
        raise RewriteError("references 必须是列表")
    for ref in references:
        if not isinstance(ref, dict) or not ref.get("group") or ref.get("item") is None:
            raise RewriteError("references 每一项都需要 group 与 item")
        if str(ref.get("position") or "first").lower() not in ("first", "last"):
            raise RewriteError("references 的 position 只能是 first 或 last")
    return spec


def resolve_secret(spec: Dict[str, Any], secret: str) -> Dict[str, Any]:
    """把规则里的 ``@SECRET`` 占位符替换成面板密钥，返回副本（源码里不留密钥）。"""
    resolved = deepcopy(spec)
    if not secret:
        for name, value in (resolved.get("set") or {}).items():
            if isinstance(value, str) and SECRET_TOKEN in value:
                raise RewriteError(f"改写规则用到了 {SECRET_TOKEN}，但配置里没填面板密钥")
    else:
        for mapping in (resolved.get("set"),):
            for name, value in list((mapping or {}).items()):
                if isinstance(value, str) and SECRET_TOKEN in value:
                    mapping[name] = value.replace(SECRET_TOKEN, secret)
        if isinstance(resolved.get("groups"), str):
            resolved["groups"] = resolved["groups"].replace(SECRET_TOKEN, secret)
        for ref in (resolved.get("references") or []):
            if isinstance(ref.get("item"), str):
                ref["item"] = ref["item"].replace(SECRET_TOKEN, secret)
    return resolved


def top_key_span(lines: List[str], key: str) -> Tuple[int, int]:
    """定位顶层键 ``key`` 的行区间 ``[start, end)``；命中次数不为 1 时抛错。"""
    pattern = re.compile(rf"^{re.escape(str(key))}:(?:\s|$)")
    hits = [i for i, line in enumerate(lines) if pattern.match(line)]
    if len(hits) != 1:
        raise RewriteError(f"顶层键 {key} 命中 {len(hits)} 次，无法定位")
    start = hits[0]
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index] and not lines[index][0].isspace():
            end = index
            break
    return start, end


def align_block(text: str, indent: int) -> str:
    """把 YAML 片段整体平移到目标缩进，只处理空格缩进。"""
    rows = [line for line in text.split("\n") if line.strip()]
    if not rows:
        raise RewriteError("策略组片段为空")
    base = min(len(line) - len(line.lstrip(" ")) for line in rows)
    shift = indent - base
    if shift == 0:
        return text
    out = []
    for line in text.split("\n"):
        if not line.strip():
            out.append(line)
            continue
        out.append(" " * max(0, len(line) - len(line.lstrip(" ")) + shift) + line.lstrip(" "))
    return "\n".join(out)


def proxy_names(doc: Dict[str, Any]) -> List[str]:
    """按订阅里的出现顺序返回节点名列表。"""
    names = []
    for item in doc.get("proxies") or []:
        if isinstance(item, dict) and item.get("name"):
            names.append(str(item["name"]))
    return names


def expand_members(text: str, doc: Dict[str, Any]) -> str:
    """把 ``- "@MEMBERS(正则)"`` 一行展开成实际节点名，缩进与写法沿用该行。"""
    names = proxy_names(doc)
    out: List[str] = []
    expanded = 0
    for line in text.split("\n"):
        match = DASH_RE.match(line)
        if not match:
            out.append(line)
            continue
        placeholder = MEMBERS_RE.match(_unquote(match.group("value")))
        if not placeholder:
            out.append(line)
            continue
        try:
            regex = re.compile(placeholder.group("pattern"), re.IGNORECASE)
        except re.error as error:
            raise RewriteError(f"@MEMBERS 正则不合法：{error}") from error
        matched = [name for name in names if regex.search(name)]
        if not matched:
            raise RewriteError(f"@MEMBERS({placeholder.group('pattern')}) 没匹配到任何节点，"
                               f"订阅节点可能已改名")
        gap = match.group("gap") or " "
        for name in matched:
            out.append(f"{match.group('indent')}-{gap}{scalar_text(name)}")
        expanded += 1
    if expanded == 0:
        raise RewriteError("策略组片段里没有 @MEMBERS(...) 占位符，节点名会变成死引用")
    return "\n".join(out)


def rewrite(src_text: str, spec: Dict[str, Any]) -> Tuple[str, List[str], Dict[str, Any]]:
    """
    按规则改写订阅文本。

    :param src_text: 订阅原始文本（未经 YAML 重排）
    :param spec: :func:`parse_spec` 校验过的规则
    :return: (改写结果文本, 变更说明列表, 原始文档解析结果)
    """
    changes: List[str] = []
    doc = yaml.safe_load(src_text)
    if not isinstance(doc, dict):
        raise RewriteError("订阅内容不是 Clash 配置映射")
    lines = src_text.split("\n")

    # 1) 删除顶层键（整块删除，避免留下空行语义）
    #    delete 要求键必须存在（写错键名能立刻发现）；delete_if_present 用于
    #    机场按 UA 追加的提示块，例如 iKuuu 会塞大写开头的 Proxy / Proxy Group / Rules
    for key in spec.get("delete") or []:
        start, end = top_key_span(lines, key)
        del lines[start:end]
        changes.append(f"删除顶层键 {key}")
    for key in spec.get("delete_if_present") or []:
        try:
            start, end = top_key_span(lines, key)
        except RewriteError:
            continue
        del lines[start:end]
        changes.append(f"删除机场附加块 {key}")

    # 2) 顶层标量键：已存在就地改值，不存在按 insert_after 锚点插入
    anchors = spec.get("insert_after") or {}
    for key, value in (spec.get("set") or {}).items():
        text = f"{key}: {scalar_text(value)}"
        try:
            start, _ = top_key_span(lines, key)
        except RewriteError:
            start = None
        if start is not None:
            if lines[start] != text:
                lines[start] = text
                changes.append(f"改 {_mask(key, value)}")
            continue
        anchor = anchors.get(key)
        anchor_start = None
        if anchor:
            try:
                anchor_start, _ = top_key_span(lines, anchor)
            except RewriteError:
                anchor_start = None
        if anchor_start is None:
            # 锚点键不存在（订阅结构变了）时退回顶部块首行，仍能写入且不乱序
            anchor_start = next((i for i, line in enumerate(lines)
                                 if line and not line[0].isspace()), 1) - 1
        lines.insert(anchor_start + 1, text)
        changes.append(f"增 {_mask(key, value)}")

    # 3) proxy-groups 区间定位
    pg_hits = [i for i, line in enumerate(lines) if re.match(r"^proxy-groups:(?:\s|$)", line)]
    if len(pg_hits) != 1:
        raise RewriteError("订阅里找不到唯一的 proxy-groups: 定义")
    pg_at = pg_hits[0]
    group_re = re.compile(r"^(?P<indent>\s*)-\s+name:\s*(?P<name>.*?)\s*$")

    def group_rows() -> List[int]:
        """返回所有策略组起始行。"""
        rows = []
        for index in range(pg_at + 1, len(lines)):
            if lines[index] and not lines[index][0].isspace():
                break
            if group_re.match(lines[index]):
                rows.append(index)
        return rows

    # 4) 插入新增策略组（片段原文照抄，只对齐缩进 + 展开成员占位符）
    snippet = (spec.get("groups") or "").strip("\n")
    if snippet:
        rows = group_rows()
        indent = len(group_re.match(lines[rows[0]]).group("indent")) if rows else 2
        block = expand_members(align_block(snippet, indent), doc)
        lines[pg_at + 1:pg_at + 1] = block.split("\n")
        changes.append(f"新增策略组块（{len(block.splitlines())} 行）")

    # 5) 往既有策略组的 proxies 里插入引用（如把自动切换组塞进手动选择组）
    for ref in spec.get("references") or []:
        group, item = str(ref["group"]), ref["item"]
        rows = group_rows()
        bounds = rows + [len(lines)]
        target = None
        for pos, row in enumerate(rows):
            if group_re.match(lines[row]).group("name") == group:
                target = (row, bounds[pos + 1])
                break
        if target is None:
            raise RewriteError(f"找不到策略组「{group}」")
        gstart, gend = target
        pv = next((i for i in range(gstart + 1, gend)
                   if re.match(r"^\s+proxies:(?:\s|$)", lines[i])), None)
        if pv is None:
            raise RewriteError(f"策略组「{group}」没有 proxies 列表")
        item_indent = len(re.match(r"^(\s*)", lines[pv]).group(1)) + 2
        rendered = f"{' ' * item_indent}- {scalar_text(item)}"
        members = [i for i in range(pv + 1, gend)
                   if DASH_RE.match(lines[i])
                   and len(DASH_RE.match(lines[i]).group("indent")) == item_indent]
        if any(_unquote(DASH_RE.match(lines[i]).group("value")) == str(item) for i in members):
            changes.append(f"「{group}」已含「{item}」，未重复插入")
            continue
        position = str(ref.get("position") or "first").lower()
        if position == "last" and members:
            lines.insert(members[-1] + 1, rendered)
        else:
            lines.insert(pv + 1, rendered)
        changes.append(f"「{group}」{'首位' if position != 'last' else '末位'}插入「{item}」")

    # 6) 规则追加/前置（同一套块定位，缩进照抄既有规则行）
    rules_at = [i for i, line in enumerate(lines) if re.match(r"^rules:(?:\s|$)", line)]
    for field, position in (("rules_prepend", "head"), ("rules_append", "tail")):
        raw_items = spec.get(field) or []
        if not raw_items:
            continue
        if len(rules_at) != 1:
            raise RewriteError("订阅里找不到唯一的 rules: 定义，无法插入规则")
        start = rules_at[0]
        end = next((i for i in range(start + 1, len(lines))
                    if lines[i] and not lines[i][0].isspace()), len(lines))
        existing = [_unquote(DASH_RE.match(lines[i]).group("value"))
                    for i in range(start + 1, end) if DASH_RE.match(lines[i])]
        indent = len(DASH_RE.match(next((lines[i] for i in range(start + 1, end)
                                        if DASH_RE.match(lines[i])), " - x")).group("indent"))
        added = []
        for rule in raw_items:
            if str(rule) in existing:
                continue
            added.append(f"{' ' * indent}- {scalar_text(rule)}")
        if not added:
            changes.append(f"{field} 的规则已存在，未重复插入")
            continue
        if position == "head":
            lines[start + 1:start + 1] = added
        else:
            tail = end - 1 if lines[end - 1] == "" else end
            lines[tail:tail] = added
        changes.append(f"{field} 插入 {len(added)} 条规则")

    return "\n".join(lines), changes, doc


def validate_result(out_text: str, src_doc: Dict[str, Any],
                    spec: Dict[str, Any]) -> Dict[str, Any]:
    """
    校验改写结果：结构必须仍是合法 Clash 配置，且只能按规则变化。

    :return: 统计信息（节点数、策略组数、规则数）
    :raises RewriteError: 任何一项校验不通过
    """
    try:
        doc = yaml.safe_load(out_text)
    except yaml.YAMLError as error:
        raise RewriteError(f"改写结果不是合法 YAML，已放弃写入：{error}") from error
    if not isinstance(doc, dict):
        raise RewriteError("改写结果不是 YAML 映射，已放弃写入")
    for key, value in (spec.get("set") or {}).items():
        if doc.get(key) != value:
            raise RewriteError(f"校验失败：{key} 期望 {value!r}，实际 {doc.get(key)!r}")
    for key in spec.get("delete") or []:
        if key in doc:
            raise RewriteError(f"校验失败：顶层键 {key} 没有被删除")
    proxies = doc.get("proxies") or []
    groups = doc.get("proxy-groups") or []
    rules = doc.get("rules") or []
    if len(proxies) != len(src_doc.get("proxies") or []):
        raise RewriteError(f"校验失败：节点数量从 {len(src_doc.get('proxies') or [])} "
                           f"变成了 {len(proxies)}")
    src_group_names = {g.get("name") for g in (src_doc.get("proxy-groups") or [])}
    if len(groups) < len(src_group_names):
        raise RewriteError("校验失败：原有策略组数量减少")
    if len(rules) < len(src_doc.get("rules") or []):
        raise RewriteError("校验失败：规则数量减少")
    names = [g.get("name") for g in groups]
    if len(names) != len(set(names)):
        raise RewriteError("校验失败：存在重名策略组，Clash 会直接加载失败")
    known = set(str(n) for n in names) | {str(p.get("name")) for p in proxies} | _BUILTIN_TARGETS
    for group in groups:
        members = group.get("proxies") or []
        if not members:
            raise RewriteError(f"校验失败：策略组「{group.get('name')}」成员为空")
        for member in members:
            if str(member) not in known:
                raise RewriteError(f"校验失败：策略组「{group.get('name')}」引用了不存在的成员"
                                   f"「{member}」（节点可能已改名）")
    for rule in rules:
        text = str(rule)
        if text.endswith(",no-resolve"):
            text = text[:-len(",no-resolve")]
        target = text.rsplit(",", 1)[-1].strip()
        if target not in known:
            raise RewriteError(f"校验失败：规则目标「{target}」既不是节点也不是策略组")
    return {"proxies": len(proxies), "groups": len(groups), "rules": len(rules)}

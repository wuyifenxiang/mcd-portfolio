#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
麦门持仓 / mcd-portfolio
=========================

把麦当劳账户里散落的优惠券、麦麦省待领券、积分和本月活动，
汇总成一张可截图的「持仓卡」。

设计原则
--------
1. 只用 Python 标准库，零依赖，克隆下来就能跑。
2. 所有数据实时来自麦当劳 MCP Server，不做任何本地缓存或猜测。
3. 算不出来的数字就不写。宁可留白，不编造金额。

用法
----
    # macOS / Linux
    export MCD_MCP_TOKEN=your_token
    python3 mcd_portfolio.py                  # 生成 card.html
    python3 mcd_portfolio.py --claim          # 先一键领取麦麦省全部券，再生成
    python3 mcd_portfolio.py --json data.json # 同时导出结构化数据

    # Windows PowerShell
    $env:MCD_MCP_TOKEN="your_token"
    python mcd_portfolio.py --out card.html
"""

from __future__ import annotations

import argparse
import html
import http.client
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

DEFAULT_MCP_URL = "https://mcp.mcd.cn"
TOKEN_ENV = "MCD_MCP_TOKEN"

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_TEMPLATE = SCRIPT_DIR.parent / "assets" / "card-template.html"


# --------------------------------------------------------------------------
# MCP 客户端
# --------------------------------------------------------------------------

class McpError(RuntimeError):
    """MCP 调用失败。"""


def _extract_sse(raw: str) -> str:
    """从 text/event-stream 响应中取出最后一个可解析的 JSON 对象。"""
    payload = None
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        chunk = line[5:].strip()
        if not chunk or chunk == "[DONE]":
            continue
        try:
            json.loads(chunk)
        except ValueError:
            continue
        payload = chunk
    return payload if payload is not None else raw


class McpClient:
    """麦当劳 MCP Server 的最小 JSON-RPC 客户端（Streamable HTTP）。"""

    def __init__(self, token: str, url: str = DEFAULT_MCP_URL, timeout: int = 30):
        if not token:
            raise McpError("缺少 MCP Token")
        self.token = token
        self.url = url.rstrip("/")
        self.timeout = timeout
        self._seq = 0

    def call(self, name: str, arguments: dict | None = None) -> str:
        """调用一个 MCP Tool，返回其文本结果。"""
        self._seq += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._seq,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        # 整段网络操作都在 try 里：连 Request 构造都算，因为 --mcp-url 不带 scheme
        # 时会在这一步抛 ValueError，漏在外面就会裸 traceback。
        try:
            req = urllib.request.Request(self.url, data=body, method="POST")
            req.add_header("Content-Type", "application/json")
            req.add_header("Accept", "application/json, text/event-stream")
            req.add_header("Authorization", "Bearer " + self.token)

            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                ctype = (resp.headers.get("Content-Type") or "").lower()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:  # noqa: BLE001 - 错误信息尽力而为
                pass
            if exc.code == 401:
                raise McpError(
                    "MCP Token 无效或已过期（401）。请到 https://open.mcd.cn/mcp "
                    "重新申请后再试。"
                ) from exc
            if exc.code == 429:
                raise McpError(
                    "触发限流（429）：单个 Token 每分钟最多 600 次请求，请稍后重试。"
                ) from exc
            raise McpError(f"HTTP {exc.code} 调用 {name} 失败：{detail}") from exc
        except urllib.error.URLError as exc:
            raise McpError(f"无法连接 {self.url}：{exc.reason}") from exc
        except (OSError, TimeoutError, http.client.HTTPException, ValueError) as exc:
            # 服务器接受连接但不响应（超时）、中途掐断（RemoteDisconnected /
            # IncompleteRead）、地址不合法等，统一包成 McpError，不让异常穿透出去。
            raise McpError(
                f"调用 {name} 时连接异常（{type(exc).__name__}）：{exc}"
            ) from exc

        if "text/event-stream" in ctype:
            raw = _extract_sse(raw)

        try:
            obj = json.loads(raw)
        except ValueError as exc:
            raise McpError(f"{name} 返回了无法解析的内容：{raw[:200]}") from exc

        if isinstance(obj, dict) and obj.get("error"):
            raise McpError(f"{name} 报错：{obj['error']}")

        content = (obj.get("result") or {}).get("content") or []
        texts = [c.get("text", "") for c in content if isinstance(c, dict)]
        return "\n".join(t for t in texts if t)


# --------------------------------------------------------------------------
# 解析层：把 MCP 的文本结果拆成结构化数据
# --------------------------------------------------------------------------

DATE_RANGE_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2})\s+\d{2}:\d{2}\s*-\s*(\d{4}-\d{2}-\d{2})\s+\d{2}:\d{2}"
)
TIME_WINDOW_RE = re.compile(r"\d{2}:\d{2}\s*-\s*\d{2}:\d{2}")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _to_datetime(value: str) -> datetime | None:
    """把一个字符串尽力解析成 datetime（兼容常见格式与秒/毫秒时间戳）。"""
    v = (value or "").strip().replace("T", " ")
    if not v:
        return None
    if re.fullmatch(r"\d{10}|\d{13}", v):  # 秒 / 毫秒时间戳
        ts = int(v)
        if len(v) == 13:
            ts //= 1000
        try:
            return datetime.fromtimestamp(ts)
        except (OverflowError, OSError, ValueError):
            return None
    for fmt, n in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d %H:%M", 16), ("%Y-%m-%d", 10)):
        try:
            return datetime.strptime(v[:n], fmt)
        except ValueError:
            continue
    return None


def parse_now(raw: str) -> datetime | None:
    """从 now-time-info 的返回中取出服务器时间。

    按 formatted → datetime → timestamp 的顺序逐个尝试，任何一个能解析就用它。
    原实现只要 formatted 在、但格式变了就直接返回 None，让同响应里的 datetime
    字段白给——这里把回退链真正打通。
    """
    raw = raw or ""
    candidates: list[str] = []
    for key in ("formatted", "datetime"):
        m = re.search(r'"%s"\s*:\s*"([^"]+)"' % key, raw)
        if m:
            candidates.append(m.group(1))
    m = re.search(r'"timestamp"\s*:\s*"?(\d{10,13})"?', raw)
    if m:
        candidates.append(m.group(1))
    m = re.search(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})", raw)
    if m:
        candidates.append(f"{m.group(1)} {m.group(2)}")

    for cand in candidates:
        dt = _to_datetime(cand)
        if dt:
            return dt
    return None


def _int_field(raw: str, key: str) -> int:
    m = re.search(r'"%s"\s*:\s*"?(-?\d+)"?' % re.escape(key), raw or "")
    return int(m.group(1)) if m else 0


def parse_account(raw: str) -> dict:
    """解析 query-my-account 的积分账户。"""
    return {
        "available": _int_field(raw, "availablePoint"),
        "accumulative": _int_field(raw, "accumulativePoint"),
        "used": _int_field(raw, "usedPoint"),
        "expiring_this_month": _int_field(raw, "currentMouthExpirePoint"),
        "expiring_next_month": _int_field(raw, "nextMouthExpirePoint"),
    }


def _split_validity(text: str) -> tuple[str | None, str | None, list[str]]:
    """拆出 有效期 里的 起止日期 与 可用时段。"""
    if not text:
        return None, None, []
    start = end = None
    rest = text
    m = DATE_RANGE_RE.search(text)
    if m:
        start, end = m.group(1), m.group(2)
        rest = text[m.end():]
    else:
        m = re.search(r"(\d{4}-\d{2}-\d{2})", text)
        if m:
            end = m.group(1)
    windows = [w.replace(" ", "") for w in TIME_WINDOW_RE.findall(rest)]
    return start, end, windows


def parse_owned_coupons(raw: str) -> list[dict]:
    """解析 query-my-coupons：用户已持有的券。"""
    coupons: list[dict] = []
    if not raw:
        return coupons
    for block in re.split(r"^##\s+", raw, flags=re.M)[1:]:
        lines = block.splitlines()
        if not lines:
            continue
        name = _clean(lines[0])
        if not name:
            continue
        body = "\n".join(lines[1:])

        m = re.search(r"\*\*优惠\*\*[:：]\s*(.+)", body)
        offer = _clean(m.group(1)) if m else ""
        m = re.search(r"\*\*有效期\*\*[:：]\s*(.+)", body)
        validity = _clean(m.group(1)) if m else ""
        m = re.search(r"\*\*标签\*\*[:：]\s*(.+)", body)
        tags = (
            [t.strip() for t in re.split(r"[、,，]", m.group(1)) if t.strip()]
            if m
            else []
        )
        m = re.search(r'<img[^>]*src="([^"]+)"', body)
        image = m.group(1) if m else ""

        start, end, windows = _split_validity(validity)
        coupons.append(
            {
                "name": name,
                "offer": offer,
                "validity": validity,
                "start": start,
                "end": end,
                "windows": windows,
                "tags": tags,
                "image": image,
            }
        )
    return coupons


def parse_available_coupons(raw: str) -> list[dict]:
    """解析 available-coupons：麦麦省里可领取的券。"""
    out: list[dict] = []
    if not raw:
        return out
    current: dict | None = None
    for line in raw.splitlines():
        s = line.strip()
        m = re.match(r"^-\s*优惠券标题[:：]\s*(.*?)\s*\\?$", s)
        if m:
            if current:
                out.append(current)
            current = {"title": _clean(m.group(1)), "status": "", "image": ""}
            continue
        if current is None:
            continue
        m = re.match(r"^状态[:：]\s*(.*?)\s*\\?$", s)
        if m:
            current["status"] = _clean(m.group(1))
            continue
        m = re.search(r'<img[^>]*src="([^"]+)"', s)
        if m and not current["image"]:
            current["image"] = m.group(1)
    if current:
        out.append(current)
    return [c for c in out if c["title"]]


def parse_campaigns(raw: str) -> list[dict]:
    """解析 campaign-calendar：本月活动。"""
    out: list[dict] = []
    if not raw:
        return out
    section = ""
    current: dict | None = None
    for line in raw.splitlines():
        s = line.strip()
        m = re.match(r"^####\s+(.*)$", s)
        if m:
            if current:
                out.append(current)
                current = None
            section = _clean(m.group(1))
            continue
        m = re.search(r"\*\*活动标题\*\*[:：]\s*(.*)$", s)
        if m:
            if current:
                out.append(current)
            current = {"date": section, "title": _clean(m.group(1)), "desc": ""}
            continue
        m = re.search(r"\*\*活动内容介绍\*\*[:：]\s*(.*)$", s)
        if m and current is not None and not current["desc"]:
            current["desc"] = _clean(m.group(1))
    if current:
        out.append(current)

    # 去重键用 (日期, 标题)：不同日期的同名活动不能被误删
    seen: set[tuple] = set()
    uniq: list[dict] = []
    for c in out:
        key = (c.get("date", ""), c["title"])
        if not c["title"] or key in seen:
            continue
        seen.add(key)
        m = re.match(r"(\d{4})年(\d{1,2})月(\d{1,2})日\s*(.*)", c["date"])
        if m:
            c["label"] = "%s/%02d" % (m.group(2), int(m.group(3)))
            c["sort"] = "%s-%02d-%02d" % (m.group(1), int(m.group(2)), int(m.group(3)))
            c["phase"] = m.group(4).strip()
        else:
            c["label"] = ""
            c["sort"] = ""
            c["phase"] = c["date"]
        uniq.append(c)

    # 活动日历按时间正序返回，这里倒序让最近/最新的排在卡片最上方
    uniq.sort(key=lambda c: c["sort"], reverse=True)
    return uniq


# --------------------------------------------------------------------------
# 模型层
# --------------------------------------------------------------------------

# 卡片「即将归零」区块最少铺几条，以及同期券最多展开几条
MIN_RISK_ROWS = 3
MAX_RISK_ROWS = 8

# ok 标记的键 → 工具名，用于把「哪个工具挂了」讲给用户听
TOOL_LABELS = {
    "now": "now-time-info",
    "owned": "query-my-coupons",
    "claimable": "available-coupons",
    "account": "query-my-account",
    "campaign": "campaign-calendar",
    "bind": "auto-bind-coupons",
}

# 这几个工具取不到时，卡片绝不允许把「没取到」说成「没有」
CRITICAL_TOOLS = ("now", "owned", "claimable", "account")


def _parse_date(value: str | None):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None


def _usable_now(windows: list[str], now: datetime | None):
    """返回 True/False；无法判断时返回 None。"""
    if not windows or now is None:
        return None
    cur = now.strftime("%H:%M")
    for w in windows:
        parts = w.split("-")
        if len(parts) != 2:
            continue
        lo, hi = parts[0].strip(), parts[1].strip()
        if lo <= hi:
            if lo <= cur <= hi:
                return True
        elif cur >= lo or cur <= hi:
            return True
    return False


def _next_open(windows: list[str], now: datetime | None):
    """当前不可用时，下一个开放时段的起点。

    返回 ('today', 'HH:MM') / ('ended', None) / ('unknown', None)。
    只取**严格晚于**当前时刻的起点——把已经过去的时刻当成「X 后开放」是错的。
    """
    if not windows or now is None:
        return ("unknown", None)
    cur = now.strftime("%H:%M")
    starts = []
    for w in windows:
        parts = w.split("-")
        if len(parts) != 2:
            continue
        lo = parts[0].strip()
        if lo and lo > cur:
            starts.append(lo)
    if starts:
        return ("today", min(starts))
    return ("ended", None)


def build_model(data: dict, now: datetime | None) -> dict:
    """把原始解析结果整理成渲染所需的模型。"""
    owned = data.get("owned_coupons") or []
    claimable = data.get("claimable_coupons") or []
    account = data.get("account") or {}
    campaigns = data.get("campaigns") or []
    ok = data.get("ok") or {}

    # 关键工具里只要有没取到的，就不能再输出「账户很干净」这类肯定判断
    failed_tools = [TOOL_LABELS[k] for k in CRITICAL_TOOLS if not ok.get(k, True)]
    data_complete = not failed_tools

    today = (now or datetime.now()).date()

    for c in owned:
        end = _parse_date(c.get("end"))
        c["days_left"] = (end.date() - today).days if end else None
        c["usable_now"] = _usable_now(c.get("windows") or [], now)
        c["next_open"] = _next_open(c.get("windows") or [], now)

    risky_all = [c for c in owned if c.get("days_left") is not None and c["days_left"] >= 0]
    risky_all.sort(key=lambda c: c["days_left"])
    expired = [c for c in owned if c.get("days_left") is not None and c["days_left"] < 0]

    # 到期日解析不出来的券：既进不了 risky，也进不了 expired，会被静默吞掉。
    # 单独收起来在卡片上说明，避免出现「统计 1 张、列表 0 张」的自相矛盾。
    unparsed = [
        {"name": c["name"], "validity": c.get("validity") or "(接口未返回有效期字段)"}
        for c in owned
        if c.get("days_left") is None
    ]

    # 最早到期的那一批必须整批露出，否则顶部结论句的张数会和卡片里的券块对不上。
    # 先用最早批次铺满，再用次早的补足到 MIN_RISK_ROWS 条。
    min_days = risky_all[0]["days_left"] if risky_all else None
    same_day_count = sum(1 for c in risky_all if c["days_left"] == min_days)
    primary = [c for c in risky_all if c["days_left"] == min_days]
    rest = [c for c in risky_all if c["days_left"] != min_days]
    shown_primary = primary[:MAX_RISK_ROWS]
    risky = shown_primary + rest[: max(0, MIN_RISK_ROWS - len(shown_primary))]
    # 极端情况下同期券多到展示不下，记下来以便卡片显式说明
    risky_omitted = max(0, len(primary) - len(shown_primary))

    grouped: dict[str, int] = {}
    for c in claimable:
        grouped[c["title"]] = grouped.get(c["title"], 0) + 1

    # 活动排序：今日优先，其次未来（近的在前），最后往期（新的在前）
    today_str = (now or datetime.now()).strftime("%Y-%m-%d")
    today_items, future_items, past_items = [], [], []
    for c in campaigns:
        s = c.get("sort") or ""
        if (c.get("phase") or "") == "今日" or s == today_str:
            c["badge"] = "今日"
            today_items.append(c)
        elif s and s > today_str:
            c["badge"] = c.get("label") or ""
            future_items.append(c)
        elif s:
            c["badge"] = c.get("label") or ""
            past_items.append(c)
    future_items.sort(key=lambda c: c["sort"])
    past_items.sort(key=lambda c: c["sort"], reverse=True)
    # 今日 3 条 + 即将 2 条：既有马上能用的，也有值得等的
    picked = today_items[:3] + future_items[:2] + past_items[:1]
    picked = picked[:5]

    return {
        "now": now,
        "owned": owned,
        "claimable": claimable,
        "claimable_grouped": grouped,
        "account": account,
        "campaigns": picked,
        "today_count": len(today_items),
        "campaign_total": len(campaigns),
        "campaign_shown": len(picked),
        "time_source": "server" if now else "local",
        "risky": risky,
        "risky_omitted": risky_omitted,
        "same_day_count": same_day_count,
        "expired_count": len(expired),
        "unparsed": unparsed,
        "ok": ok,
        "failed_tools": failed_tools,
        "data_complete": data_complete,
        "stats": {
            "owned": len(owned),
            "claimable": len(claimable),
            "points": int(account.get("available") or 0),
        },
    }


# --------------------------------------------------------------------------
# 渲染层
# --------------------------------------------------------------------------

_WEEKDAYS = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


def _e(value) -> str:
    return html.escape(str(value if value is not None else ""))


def _render_banner(model: dict) -> str:
    """取数不全时的显著提示条。没有失败就不渲染。"""
    failed = model.get("failed_tools") or []
    if not failed:
        return ""
    names = "、".join(_e(t) for t in failed)
    return (
        '<div class="warn"><b>!</b> 部分数据未取到（' + names + '）。'
        '下面标「未取到」的数字不代表真实为 0，请稍后重试。</div>'
    )


def _headline(model: dict) -> str:
    """卡片顶部那句最关键的话——它决定别人会不会截图。"""
    ok = model.get("ok") or {}
    claimable = model["stats"]["claimable"]
    risky = model["risky"]
    bits: list[str] = []
    if risky:
        d = risky[0]["days_left"]
        n = model["same_day_count"]
        when = "今天" if d == 0 else f"{d} 天后"
        bits.append(f"有 {n} 张券 {when}归零")
    # 只有确认取到了麦麦省数据，才敢报「还没领」
    if claimable and ok.get("claimable", True):
        bits.append(f"{claimable} 张券还没领")
    if bits:
        return "你的麦当劳账户里，" + "，".join(bits) + "。"

    # 取数不全时，绝不下「没有需要处理的券」这类肯定结论
    if not model.get("data_complete", True):
        return "部分数据未取到，暂时无法给出账户结论，请看下方提示条。"

    # 没有在有效期内的券时，也别急着说「账户很干净」——过期和本来就没有是两回事
    expired = model.get("expired_count") or 0
    if expired:
        return f"你的麦当劳账户里，有 {expired} 张券已经过期作废。"
    if not model.get("owned"):
        return "你的麦当劳账户里，目前没有持有中的优惠券。"
    return "持有中的券都还没到需要处理的阶段。"


def _render_risk(model: dict) -> str:
    ok = model.get("ok") or {}
    if not ok.get("owned", True):
        return (
            '<div class="sec"><h2>即将归零</h2>'
            '<div class="empty">未取到券数据（query-my-coupons 调用失败），'
            '暂时无法判断哪些券即将到期。</div></div>'
        )

    risky = model["risky"]
    if not risky:
        # 有券但到期日解析不出来时，不能说「没有持有中的优惠券」——那会和上方统计打架
        if model.get("unparsed") and not model["expired_count"]:
            note = "可识别到期日的券里，暂时没有即将归零的。"
        elif model["expired_count"]:
            note = f"有 {model['expired_count']} 张券已过期；其余券都不在即将归零范围内。"
        else:
            note = "当前没有持有中的优惠券。"
        return (
            '<div class="sec"><h2>即将归零</h2>'
            f'<div class="empty">{_e(note)}</div></div>'
        )

    cards = []
    for c in risky:
        days = c["days_left"]
        if days == 0:
            day_html = '<b>今天</b><i>最后一天</i>'
        else:
            day_html = f'<b>{days}</b><i>天后归零</i>'

        metas = []
        if c.get("end"):
            metas.append(f"有效期至 <b>{_e(c['end'])}</b>")
        if c.get("windows"):
            span = "、".join(w.replace("-", "–") for w in c["windows"])
            metas.append(f"每日可核销时段 <b>{_e(span)}</b>")
        if c.get("usable_now") is True:
            metas.append('<b>现在可用</b>')
        elif c.get("usable_now") is False:
            kind, val = c.get("next_open") or ("unknown", None)
            if kind == "today":
                metas.append(f"当前不可用，<b>{_e(val)}</b> 后开放")
            elif kind == "ended":
                metas.append("当前不可用，<b>今日时段已结束</b>")
            else:
                metas.append("当前不可用")
        if c.get("tags"):
            metas.append(" · ".join(_e(t) for t in c["tags"]))

        cards.append(
            '<div class="risk">'
            f'<div class="days">{day_html}</div>'
            '<div class="body">'
            f'<div class="name">{_e(c["name"])}</div>'
            f'<div class="meta">{"<br>".join(metas) if metas else "—"}</div>'
            "</div></div>"
        )

    if model.get("risky_omitted"):
        cards.append(
            f'<div class="empty">另有 {model["risky_omitted"]} 张同日到期的券未展开。</div>'
        )

    return (
        '<div class="sec"><h2>即将归零'
        '<span class="tag">优先处理</span></h2>'
        + "".join(cards)
        + "</div>"
    )


def _render_unparsed(model: dict) -> str:
    """把到期日解析不出来的券单独列出来，不再让它们人间蒸发。"""
    items = model.get("unparsed") or []
    if not items:
        return ""
    lis = []
    for c in items:
        lis.append(
            f'<li><span class="t">{_e(c["name"])}'
            '<div style="color:#6B6A66;font-size:12px;margin-top:3px">'
            f'原始有效期：{_e(c["validity"])}</div></span></li>'
        )
    return (
        '<div class="sec"><h2>到期日无法解析'
        f'<span class="tag">{len(items)} 张</span></h2>'
        '<div class="empty">以下券的到期日无法从接口返回值里解析出来，'
        '未计入「即将归零」判断，但仍计入上方「已持有券」张数。</div>'
        f'<ul class="list">{"".join(lis)}</ul></div>'
    )


def _render_claim(model: dict) -> str:
    ok = model.get("ok") or {}
    if not ok.get("claimable", True):
        return (
            '<div class="sec"><h2>待领取 · 麦麦省</h2>'
            '<div class="empty">未取到麦麦省数据（available-coupons 调用失败）。</div></div>'
        )
    grouped = model["claimable_grouped"]
    if not grouped:
        return (
            '<div class="sec"><h2>待领取 · 麦麦省</h2>'
            '<div class="empty">麦麦省当前没有可领取的券。</div></div>'
        )
    chips = []
    for title, n in grouped.items():
        dup = ' class="chip dup"' if n > 1 else ' class="chip"'
        attr = f' data-n="{n}"' if n > 1 else ""
        chips.append(f"<span{dup}{attr}>{_e(title)}</span>")
    return (
        '<div class="sec"><h2>待领取 · 麦麦省'
        f'<span class="tag">{model["stats"]["claimable"]} 张</span></h2>'
        f'<div class="chips">{"".join(chips)}</div>'
        "</div>"
    )


def _render_campaigns(model: dict) -> str:
    items = model["campaigns"]
    if not items:
        ok = model.get("ok") or {}
        msg = ("未取到活动日历（campaign-calendar 调用失败）。"
               if not ok.get("campaign", True) else "本月暂无活动。")
        return (
            '<div class="sec"><h2>活动日历</h2>'
            f'<div class="empty">{_e(msg)}</div></div>'
        )
    lis = []
    for c in items:
        desc = c.get("desc") or ""
        if len(desc) > 46:
            desc = desc[:46] + "…"
        text = _e(c["title"])
        if desc:
            text += f'<div style="color:#6B6A66;font-size:12px;margin-top:3px">{_e(desc)}</div>'
        hot = ' style="color:#DA291C;font-weight:500"' if c.get("badge") == "今日" else ""
        lis.append(f'<li><span class="d"{hot}>{_e(c.get("badge") or "")}</span>'
                   f'<span class="t">{text}</span></li>')
    tag = ""
    if model.get("today_count"):
        tag = f'今日 {model["today_count"]} 条'
        # 徽章数字和下面对得上的条数不一样时，把「展示了几条」也说清楚
        if (model.get("campaign_total") or 0) > (model.get("campaign_shown") or 0):
            tag += f' · 展示 {model["campaign_shown"]} 条'
        tag = f'<span class="tag">{tag}</span>'
    return f'<div class="sec"><h2>活动日历{tag}</h2><ul class="list">{"".join(lis)}</ul></div>'


def _render_summary(model: dict) -> str:
    ok = model.get("ok") or {}
    s = model["stats"]
    parts = [
        f"已持有 {s['owned']} 张券" if ok.get("owned", True) else "已持有券未取到",
        f"麦麦省待领 {s['claimable']} 张" if ok.get("claimable", True) else "麦麦省待领未取到",
        f"可用积分 {s['points']} 分" if ok.get("account", True) else "可用积分未取到",
    ]
    if model["risky"]:
        d = model["risky"][0]["days_left"]
        parts.append("最快一张 %s 天后归零" % d if d > 0 else "有一张券今天到期")
    return " · ".join(parts) + "。"


def _stat(ok: bool, value, unit: str) -> str:
    """统计块的取值：取不到时明确写「未取到」，不写 0。"""
    if not ok:
        return '<span class="na">未取到</span>'
    return f'{value}<span class="u">{unit}</span>'


def render(model: dict, template: str) -> str:
    out = template
    ok = model.get("ok") or {}
    now = model["now"]
    if now:
        head = now.strftime("%Y-%m-%d %H:%M") + "<br>" + _WEEKDAYS[now.weekday()]
        basis = ""
    else:
        # 服务器时间没取到，只能用本机时间兜底——那就必须在卡片上写明
        local = datetime.now()
        head = local.strftime("%Y-%m-%d %H:%M") + "<br>" + _WEEKDAYS[local.weekday()]
        basis = "时间基准：本机时间（服务器时间未取到）"
    repl = {
        "{{GENERATED_AT}}": head,
        "{{TIME_BASIS}}": _e(basis),
        "{{WARN_BANNER}}": _render_banner(model),
        "{{HEADLINE}}": _e(_headline(model)),
        "{{STAT_OWNED}}": _stat(ok.get("owned", True), model["stats"]["owned"], "张"),
        "{{STAT_CLAIMABLE}}": _stat(ok.get("claimable", True), model["stats"]["claimable"], "张"),
        "{{STAT_POINTS}}": _stat(ok.get("account", True), model["stats"]["points"], "分"),
        "{{RISK_SECTION}}": _render_risk(model),
        "{{UNPARSED_SECTION}}": _render_unparsed(model),
        "{{CLAIM_SECTION}}": _render_claim(model),
        "{{CAMPAIGN_SECTION}}": _render_campaigns(model),
        "{{SUMMARY_LINE}}": _e(_render_summary(model)),
    }
    for k, v in repl.items():
        out = out.replace(k, v)

    # 自定义模板缺键时，str.replace 会静默留下一整块 {{...}}，必须显式报错
    leftovers = sorted(set(re.findall(r"\{\{[A-Z_]+\}\}", out)))
    if leftovers:
        raise ValueError("模板里还有未替换的占位符：" + "、".join(leftovers))
    return out


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

# auto-bind-coupons 的返回格式官方未公开，只能靠关键词判断成败。
# 拿不准时一律返回 unknown——宁可说「未确认」，也绝不谎报成功。
BIND_SUCCESS_HINTS = ("成功", "已领取", "领取完成", "已绑定", "绑定成功")
BIND_FAILURE_HINTS = ("失败", "无法领取", "已过期", "库存不足", "已领完", "已结束")


def _judge_bind(ok_flag, raw: str) -> str:
    """返回 'ok' / 'fail' / 'unknown'。"""
    if ok_flag is False:
        return "fail"
    text = _clean(raw or "")
    if not text:
        return "unknown"
    low = text.lower()
    if re.search(r'"success"\s*:\s*false', low) or re.search(r'"code"\s*:\s*[45]\d\d', low):
        return "fail"
    if any(h in text for h in BIND_FAILURE_HINTS):
        return "fail"
    if "error" in low or "exception" in low:
        return "fail"
    if any(h in text for h in BIND_SUCCESS_HINTS) or re.search(r'"success"\s*:\s*true', low):
        return "ok"
    return "unknown"


def _claim_report(ok_flag, raw: str, before: dict, after: dict) -> dict:
    return {"status": _judge_bind(ok_flag, raw), "before": before, "after": after}


def collect(client: McpClient, claim: bool) -> dict:
    """依次调用 MCP 工具，收集原始数据，并记录每个工具的成败。"""
    data: dict = {}
    soft_fail: list[str] = []
    # 成败标记：区分「真的返回了 0」和「根本没取到」的唯一依据
    ok_flags: dict[str, bool] = {}

    def try_call(key: str, name: str, args=None) -> str:
        try:
            raw = client.call(name, args)
            ok_flags[key] = True
            return raw
        except McpError as exc:
            ok_flags[key] = False
            soft_fail.append(f"{name}: {exc}")
            return ""

    data["now_raw"] = try_call("now", "now-time-info")

    pre_owned_raw = pre_claim_raw = ""
    if claim:
        # 领券前先做一次快照，否则无法给出「领前 → 领后」的对比
        pre_owned_raw = try_call("owned_pre", "query-my-coupons",
                                 {"page": "1", "pageSize": "200"})
        pre_claim_raw = try_call("claimable_pre", "available-coupons")
        data["bind_raw"] = try_call("bind", "auto-bind-coupons")

    data["owned_raw"] = try_call("owned", "query-my-coupons", {"page": "1", "pageSize": "200"})
    data["claimable_raw"] = try_call("claimable", "available-coupons")
    data["account_raw"] = try_call("account", "query-my-account")
    data["campaign_raw"] = try_call("campaign", "campaign-calendar")

    data["now"] = parse_now(data["now_raw"])
    data["owned_coupons"] = parse_owned_coupons(data["owned_raw"])
    data["claimable_coupons"] = parse_available_coupons(data["claimable_raw"])
    data["account"] = parse_account(data["account_raw"])
    data["campaigns"] = parse_campaigns(data["campaign_raw"])
    data["ok"] = ok_flags
    data["soft_fail"] = soft_fail

    if claim:
        data["claim_report"] = _claim_report(
            ok_flags.get("bind"),
            data.get("bind_raw") or "",
            {
                "owned": len(parse_owned_coupons(pre_owned_raw)),
                "claimable": len(parse_available_coupons(pre_claim_raw)),
            },
            {
                "owned": len(data["owned_coupons"]),
                "claimable": len(data["claimable_coupons"]),
            },
        )
    return data


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001 - 老版本 Python 或非标准流
        pass

    ap = argparse.ArgumentParser(
        prog="mcd-portfolio",
        description="麦门持仓：把麦当劳账户里的券和积分汇总成一张可用卡片。",
    )
    ap.add_argument("--token", default=os.environ.get(TOKEN_ENV, ""),
                    help=f"MCP Token，默认读环境变量 {TOKEN_ENV}")
    ap.add_argument("--mcp-url", default=DEFAULT_MCP_URL, help="MCP 服务地址")
    ap.add_argument("--out", default="card.html", help="输出的 HTML 路径")
    ap.add_argument("--json", dest="json_out", default="", help="可选：导出结构化数据")
    ap.add_argument("--template", default=str(DEFAULT_TEMPLATE), help="卡片模板路径")
    ap.add_argument("--claim", action="store_true",
                    help="先调用 auto-bind-coupons 领取麦麦省全部券（写操作）")
    args = ap.parse_args(argv)

    # 顶层兜底：任何没被 McpError 接住的异常（网络、编码、I/O 等）都在这里
    # 变成一句话提示，绝不把裸 traceback 甩给用户。
    try:
        return _run(args)
    except McpError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - 兜底，不吞掉，但换成可读提示
        print(f"[!] 运行出错（{type(exc).__name__}）：{exc}", file=sys.stderr)
        print("  请检查 --mcp-url 是否正确、网络与代理是否正常，然后重试。",
              file=sys.stderr)
        return 1


def _report_claim(data: dict) -> bool:
    """打印领券结果。返回 True 表示领券失败。"""
    rep = data.get("claim_report") or {}
    status = rep.get("status")
    before = rep.get("before") or {}
    after = rep.get("after") or {}
    b_o, b_c = before.get("owned"), before.get("claimable")
    a_o, a_c = after.get("owned"), after.get("claimable")
    delta = f"券包 {b_o} → {a_o} 张，麦麦省待领 {b_c} → {a_c} 张"

    if status == "ok":
        print(f"[*] 领券成功：{delta}")
        return False
    if status == "fail":
        reason = next(
            (s for s in (data.get("soft_fail") or []) if s.startswith("auto-bind-coupons")),
            "",
        )
        print(f"[!] 领券失败：auto-bind-coupons 未成功返回。{reason}", file=sys.stderr)
        print(f"    当前 {delta}。", file=sys.stderr)
        return True
    print(f"[*] 领券结果未确认：{delta}。接口未返回明确的成败信息。")
    return False


def _run(args) -> int:
    if not args.token:
        print(
            "缺少 MCP Token。\n"
            "  1) 打开 https://open.mcd.cn/mcp 用手机号登录，在控制台申请 Token\n"
            f"  2) export {TOKEN_ENV}=<你的Token>，或用 --token 传入",
            file=sys.stderr,
        )
        return 2

    client = McpClient(args.token, args.mcp_url)
    data = collect(client, args.claim)

    claim_failed = False
    if args.claim:
        claim_failed = _report_claim(data)

    model = build_model(data, data.get("now"))

    template_path = Path(args.template)
    if not template_path.is_file():
        print(f"[!] 找不到卡片模板：{template_path}", file=sys.stderr)
        return 1
    try:
        html_out = render(model, template_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        print(f"[!] 卡片模板不可用：{exc}", file=sys.stderr)
        return 1

    out_path = Path(args.out)
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(html_out, encoding="utf-8")
    except OSError as exc:
        print(f"[!] 无法写入卡片 {out_path}：{exc}", file=sys.stderr)
        return 1

    if args.json_out:
        payload = {k: v for k, v in data.items() if not k.endswith("_raw")}
        payload["stats"] = model["stats"]
        payload["claimable_grouped"] = model["claimable_grouped"]
        payload["today_count"] = model["today_count"]
        payload["unparsed"] = model["unparsed"]
        payload["risky"] = [
            {k: v for k, v in c.items() if k != "image"} for c in model["risky"]
        ]
        json_path = Path(args.json_out)
        try:
            json_path.parent.mkdir(parents=True, exist_ok=True)
            json_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        except OSError as exc:
            print(f"[!] 无法写入 JSON {json_path}：{exc}", file=sys.stderr)
            return 1

    s = model["stats"]
    print(f"[+] 持仓卡已生成：{out_path.resolve()}")
    print(f"    已持有券 {s['owned']} 张 | 麦麦省待领 {s['claimable']} 张 | 可用积分 {s['points']} 分")
    for c in model["risky"]:
        print(f"    ! {c['name']} — {c['days_left']} 天后归零")

    soft_fail = data.get("soft_fail") or []
    # 全部调用都失败 = 地址/网络/Token 层面的问题，必须给非 0 退出码，
    # 否则截图传播时只剩一张「未取到」的卡，看不出其实什么都没跑通。
    if data.get("ok") and not any(data["ok"].values()):
        first = soft_fail[0] if soft_fail else "未知错误"
        print(
            f"[!] 全部数据都未取到：{first}；"
            f"请检查 --mcp-url（当前 {args.mcp_url}）与 Token 后重试。",
            file=sys.stderr,
        )
        return 1
    for line in soft_fail:
        print(f"    (部分数据未取到) {line}", file=sys.stderr)
    if claim_failed:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

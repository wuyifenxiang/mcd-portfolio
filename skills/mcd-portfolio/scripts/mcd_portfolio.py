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
        req = urllib.request.Request(self.url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json, text/event-stream")
        req.add_header("Authorization", "Bearer " + self.token)

        try:
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


def parse_now(raw: str) -> datetime | None:
    """从 now-time-info 的返回中取出服务器时间。"""
    candidate = None
    m = re.search(r'"formatted"\s*:\s*"([^"]+)"', raw or "")
    if m:
        candidate = m.group(1)
    if not candidate:
        m = re.search(
            r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})", raw or ""
        )
        if m:
            candidate = f"{m.group(1)} {m.group(2)}"
    if not candidate:
        return None
    try:
        return datetime.strptime(candidate.replace("T", " ")[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
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

    seen: set[str] = set()
    uniq: list[dict] = []
    for c in out:
        if not c["title"] or c["title"] in seen:
            continue
        seen.add(c["title"])
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


def build_model(data: dict, now: datetime | None) -> dict:
    """把原始解析结果整理成渲染所需的模型。"""
    owned = data.get("owned_coupons") or []
    claimable = data.get("claimable_coupons") or []
    account = data.get("account") or {}
    campaigns = data.get("campaigns") or []

    today = (now or datetime.now()).date()

    for c in owned:
        end = _parse_date(c.get("end"))
        c["days_left"] = (end.date() - today).days if end else None
        c["usable_now"] = _usable_now(c.get("windows") or [], now)

    risky_all = [c for c in owned if c.get("days_left") is not None and c["days_left"] >= 0]
    risky_all.sort(key=lambda c: c["days_left"])
    expired = [c for c in owned if c.get("days_left") is not None and c["days_left"] < 0]

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
        "risky": risky,
        "risky_omitted": risky_omitted,
        "same_day_count": same_day_count,
        "expired_count": len(expired),
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


def _headline(model: dict) -> str:
    """卡片顶部那句最关键的话——它决定别人会不会截图。"""
    claimable = model["stats"]["claimable"]
    risky = model["risky"]
    bits: list[str] = []
    if risky:
        d = risky[0]["days_left"]
        n = model["same_day_count"]
        when = "今天" if d == 0 else f"{d} 天后"
        bits.append(f"有 {n} 张券 {when}归零")
    if claimable:
        bits.append(f"{claimable} 张券还没领")
    if bits:
        return "你的麦当劳账户里，" + "，".join(bits) + "。"

    # 没有在有效期内的券时，别急着说"账户很干净"——过期和本来就没有是两回事
    expired = model.get("expired_count") or 0
    if expired:
        return f"你的麦当劳账户里，有 {expired} 张券已经过期作废。"
    if not model.get("owned"):
        return "你的麦当劳账户里，目前没有持有中的优惠券。"
    return "持有中的券都还没到需要处理的阶段。"


def _render_risk(model: dict) -> str:
    risky = model["risky"]
    if not risky:
        note = "当前没有持有中的优惠券。"
        if model["expired_count"]:
            note = f"有 {model['expired_count']} 张券已过期，当前没有持有中的优惠券。"
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
            nxt = (c.get("windows") or ["—"])[0].split("-")[0].replace("-", ":")
            metas.append(f"当前不可用，<b>{_e(nxt)}</b> 后开放")
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


def _render_claim(model: dict) -> str:
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
        return (
            '<div class="sec"><h2>活动日历</h2>'
            '<div class="empty">暂未读到活动日历。</div></div>'
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
    tag = f'<span class="tag">今日 {model["today_count"]} 条</span>' if model.get("today_count") else ""
    return f'<div class="sec"><h2>活动日历{tag}</h2><ul class="list">{"".join(lis)}</ul></div>'


def _render_summary(model: dict) -> str:
    s = model["stats"]
    parts = [
        f"已持有 {s['owned']} 张券",
        f"麦麦省待领 {s['claimable']} 张",
        f"可用积分 {s['points']} 分",
    ]
    if model["risky"]:
        d = model["risky"][0]["days_left"]
        parts.append("最快一张 %s 天后归零" % d if d > 0 else "有一张券今天到期")
    return " · ".join(parts) + "。"


def render(model: dict, template: str) -> str:
    out = template
    now = model["now"]
    if now:
        head = now.strftime("%Y-%m-%d %H:%M") + "<br>" + _WEEKDAYS[now.weekday()]
    else:
        head = "时间未知"
    repl = {
        "{{GENERATED_AT}}": head,
        "{{HEADLINE}}": _e(_headline(model)),
        "{{STAT_OWNED}}": str(model["stats"]["owned"]),
        "{{STAT_CLAIMABLE}}": str(model["stats"]["claimable"]),
        "{{STAT_POINTS}}": str(model["stats"]["points"]),
        "{{RISK_SECTION}}": _render_risk(model),
        "{{CLAIM_SECTION}}": _render_claim(model),
        "{{CAMPAIGN_SECTION}}": _render_campaigns(model),
        "{{SUMMARY_LINE}}": _e(_render_summary(model)),
    }
    for k, v in repl.items():
        out = out.replace(k, v)
    return out


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

def collect(client: McpClient, claim: bool) -> dict:
    """依次调用 MCP 工具，收集原始数据。"""
    data: dict = {}
    soft_fail: list[str] = []

    def try_call(name, args=None):
        try:
            return client.call(name, args)
        except McpError as exc:
            soft_fail.append(f"{name}: {exc}")
            return ""

    data["now_raw"] = try_call("now-time-info")
    if claim:
        data["bind_raw"] = try_call("auto-bind-coupons")
    data["owned_raw"] = try_call("query-my-coupons", {"page": "1", "pageSize": "200"})
    data["claimable_raw"] = try_call("available-coupons")
    data["account_raw"] = try_call("query-my-account")
    data["campaign_raw"] = try_call("campaign-calendar")

    data["now"] = parse_now(data["now_raw"])
    data["owned_coupons"] = parse_owned_coupons(data["owned_raw"])
    data["claimable_coupons"] = parse_available_coupons(data["claimable_raw"])
    data["account"] = parse_account(data["account_raw"])
    data["campaigns"] = parse_campaigns(data["campaign_raw"])
    data["soft_fail"] = soft_fail
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

    if not args.token:
        print(
            "缺少 MCP Token。\n"
            "  1) 打开 https://open.mcd.cn/mcp 用手机号登录，在控制台申请 Token\n"
            f"  2) export {TOKEN_ENV}=<你的Token>，或用 --token 传入",
            file=sys.stderr,
        )
        return 2

    try:
        client = McpClient(args.token, args.mcp_url)
        data = collect(client, args.claim)
    except McpError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1

    if args.claim:
        print("[*] 已请求领取麦麦省全部可领券。")

    model = build_model(data, data.get("now"))

    template_path = Path(args.template)
    if not template_path.is_file():
        print(f"[!] 找不到卡片模板：{template_path}", file=sys.stderr)
        return 1
    html_out = render(model, template_path.read_text(encoding="utf-8"))

    out_path = Path(args.out)
    out_path.write_text(html_out, encoding="utf-8")

    if args.json_out:
        payload = {k: v for k, v in data.items() if not k.endswith("_raw")}
        payload["stats"] = model["stats"]
        payload["claimable_grouped"] = model["claimable_grouped"]
        payload["today_count"] = model["today_count"]
        payload["risky"] = [
            {k: v for k, v in c.items() if k != "image"} for c in model["risky"]
        ]
        Path(args.json_out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

    s = model["stats"]
    print(f"[+] 持仓卡已生成：{out_path.resolve()}")
    print(f"    已持有券 {s['owned']} 张 | 麦麦省待领 {s['claimable']} 张 | 可用积分 {s['points']} 分")
    for c in model["risky"]:
        print(f"    ! {c['name']} — {c['days_left']} 天后归零")
    for line in data.get("soft_fail", []):
        print(f"    (部分数据未取到) {line}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

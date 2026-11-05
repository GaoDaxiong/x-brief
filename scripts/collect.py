#!/usr/bin/env python3
"""每日简报 · 数据采集（GitHub Actions 流水线第一步）。

数据源（全部免费）：
  1. AIHOT agent 接口（latest / hot / daily）——返回 Markdown 文本，本脚本解析。
  2. NewsNow 公共实例热榜（微博 / 知乎 / B站 / 抖音）——必须带浏览器 UA。
  3. X（twscrape）：巡查 scripts/watchlist.txt 名单近 24h 时间线 + 关键词搜索。
     Cookie 只从环境变量 X_AUTH_TOKEN / X_CT0 读取，账号库建在临时目录，用完即弃。

时间窗口：前一天 07:00 至当天 07:00（Asia/Shanghai）。
目标日期由环境变量 BRIEF_DATE（YYYY-MM-DD）指定，缺省为当天（北京时间）。

输出：
  data/candidates.json    候选条目 [{source, author, handle, time_iso, text, url, likes, retweets, avatar_url}]
  data/collect_meta.json  各源条数、错误、watchlist 状态、窗口信息（供 generate.py 与步骤摘要使用）

单个源失败不中断整体；全部源都无产出时以非零码退出，避免生成空简报。
本地调试可设 BRIEF_SKIP_X=1 跳过 X 部分。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

CST = ZoneInfo("Asia/Shanghai")
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
WATCHLIST_PATH = ROOT / "scripts" / "watchlist.txt"

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

AIHOT_BASE = "https://aihot.news/api/v1/agent"
NEWSNOW_BASE = "https://newsnow.busiyi.world/api/s"
NEWSNOW_SOURCES = {
    "weibo": "微博热搜",
    "zhihu": "知乎热榜",
    "bilibili": "B站热搜",
    "douyin": "抖音热榜",
}

# (query, product) —— product: Top / Latest
X_SEARCHES = [
    ("Claude Code 经验 lang:zh min_faves:20", "Top"),
    ("Codex lang:zh min_faves:20", "Top"),
    ("AI Agent 教程 lang:zh min_faves:20", "Top"),
    ("开源项目 lang:zh min_faves:20", "Top"),
    ("AI 视频 生成 lang:zh min_faves:20", "Top"),
    ("AI 游戏 lang:zh min_faves:10", "Top"),
    ("免费额度 lang:zh min_faves:10", "Latest"),
    ("限免 OR 福利 AI工具 lang:zh min_faves:10", "Latest"),
]


def target_window() -> tuple[datetime, datetime, str]:
    raw = os.environ.get("BRIEF_DATE", "").strip()
    if raw:
        day = datetime.strptime(raw, "%Y-%m-%d").date()
    else:
        day = datetime.now(CST).date()
    end = datetime(day.year, day.month, day.day, 7, 0, tzinfo=CST)
    return end - timedelta(days=1), end, day.isoformat()


def fetch_text(url: str, timeout: int = 25) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def dedup_key(c: dict) -> str:
    m = re.search(r"/status/(\d+)", c.get("url", ""))
    if m:
        return f"status:{m.group(1)}"
    return "url:" + c.get("url", "").rstrip("/").lower()


# ---------------------------------------------------------------- AIHOT
_MD_ITEM = re.compile(r"\[([^\]]+)\]\((https://aihot\.news/items/[^)\s]+)\)")


def _aihot_time(mmdd_hhmm: str, window_end: datetime) -> str:
    m = re.match(r"(\d{2})-(\d{2}) (\d{2}):(\d{2})", mmdd_hhmm)
    if not m:
        return window_end.isoformat()
    mo, d, hh, mm = (int(x) for x in m.groups())
    dt = datetime(window_end.year, mo, d, hh, mm, tzinfo=CST)
    if dt > window_end + timedelta(hours=12):
        dt = dt.replace(year=dt.year - 1)
    return dt.isoformat()


def parse_aihot(md: str, source: str, window_end: datetime) -> list[dict]:
    """解析 AIHOT agent 接口返回的 Markdown（latest / hot / daily 三种版式）。"""
    out: list[dict] = []
    matches = list(_MD_ITEM.finditer(md))
    for idx, m in enumerate(matches):
        title, item_url = m.group(1).strip(), m.group(2).strip()
        seg_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(md)
        seg = md[m.end():seg_end]
        # 原文链接（latest 版式）
        real = re.search(r"原文：\s*(https?://\S+)", seg)
        url = real.group(1).strip() if real else item_url
        # 摘要：latest 用「摘要：」前缀；daily 是紧随的缩进段落
        summ = re.search(r"摘要：\s*(.+)", seg)
        if summ:
            text_body = summ.group(1).strip()
        else:
            lines = [ln.strip() for ln in seg.splitlines() if ln.strip()]
            body_lines = [ln for ln in lines if not ln.startswith(("信源：", "来龙去脉："))]
            text_body = body_lines[-1] if body_lines else ""
        # 作者 / 信源
        author = ""
        src_m = re.search(r"信源：\s*([^·（(]+)", seg)
        tail_m = re.match(r"\s*·\s*([^·]+)·", seg)
        if src_m:
            author = src_m.group(1).strip()
        elif tail_m:
            author = tail_m.group(1).strip()
        else:
            first = re.search(r"^\s*(.+?)·\s*发布于", seg, re.M)
            if first:
                author = first.group(1).strip()
        # 时间
        t_m = re.search(r"(?:发布于|最新进展)\s*(\d{2}-\d{2} \d{2}:\d{2})", seg)
        time_iso = _aihot_time(t_m.group(1), window_end) if t_m else window_end.isoformat()
        out.append({
            "source": source,
            "author": author or "AIHOT",
            "handle": "",
            "time_iso": time_iso,
            "text": f"{title}\n{text_body}" if text_body else title,
            "url": url,
            "likes": 0,
            "retweets": 0,
            "avatar_url": "",
        })
    return out


def collect_aihot(window_end: datetime, meta: dict) -> list[dict]:
    out: list[dict] = []
    for name, path in (("aihot_latest", "latest?limit=30"),
                       ("aihot_hot", "hot"),
                       ("aihot_daily", "daily")):
        try:
            md = fetch_text(f"{AIHOT_BASE}/{path}")
            items = parse_aihot(md, name, window_end)
            meta["counts"][name] = len(items)
            out.extend(items)
        except Exception as e:  # 单源失败不中断
            meta["counts"][name] = 0
            meta["errors"].append(f"{name}: {type(e).__name__}: {e}")
    return out


# ---------------------------------------------------------------- NewsNow
def collect_newsnow(window_end: datetime, meta: dict) -> list[dict]:
    out: list[dict] = []
    for sid, label in NEWSNOW_SOURCES.items():
        key = f"newsnow_{sid}"
        try:
            raw = fetch_text(f"{NEWSNOW_BASE}?id={sid}")
            data = json.loads(raw)
            items = data.get("items", []) if isinstance(data, dict) else data
            upd = data.get("updatedTime") if isinstance(data, dict) else None
            if upd:
                time_iso = datetime.fromtimestamp(upd / 1000, tz=timezone.utc).astimezone(CST).isoformat()
            else:
                time_iso = window_end.isoformat()
            got = []
            for it in items or []:
                title = (it.get("title") or "").strip()
                url = (it.get("url") or it.get("mobileUrl") or "").strip()
                if title and url:
                    got.append({
                        "source": key,
                        "author": label,
                        "handle": "",
                        "time_iso": time_iso,
                        "text": title,
                        "url": url,
                        "likes": 0,
                        "retweets": 0,
                        "avatar_url": "",
                    })
            meta["counts"][key] = len(got)
            out.extend(got)
        except Exception as e:
            meta["counts"][key] = 0
            meta["errors"].append(f"{key}: {type(e).__name__}: {e}")
    return out


# ---------------------------------------------------------------- X / twscrape
def load_watchlist() -> list[dict]:
    rows: list[dict] = []
    if not WATCHLIST_PATH.exists():
        return rows
    for line in WATCHLIST_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        rows.append({
            "handle": parts[0].lstrip("@"),
            "name": parts[1] if len(parts) > 1 else parts[0],
            "identity": parts[2] if len(parts) > 2 else "",
        })
    return rows


def _tweet_to_candidate(tw, source: str) -> dict | None:
    try:
        user = getattr(tw, "user", None)
        username = getattr(user, "username", "") or ""
        dt = getattr(tw, "date", None)
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        url = getattr(tw, "url", "") or f"https://x.com/{username}/status/{getattr(tw, 'id', '')}"
        return {
            "source": source,
            "author": getattr(user, "displayname", "") or username,
            "handle": username,
            "time_iso": dt.astimezone(CST).isoformat(),
            "text": (getattr(tw, "rawContent", "") or "").strip(),
            "url": url,
            "likes": int(getattr(tw, "likeCount", 0) or 0),
            "retweets": int(getattr(tw, "retweetCount", 0) or 0),
            "avatar_url": getattr(user, "profileImageUrl", "") or "",
            "_dt": dt,
        }
    except Exception:
        return None


async def _collect_x_async(window_start: datetime, window_end: datetime,
                           watchlist: list[dict], meta: dict) -> list[dict]:
    from twscrape import API  # 懒加载：本地无 twscrape 时也不影响 py_compile / 其他源

    auth = os.environ.get("X_AUTH_TOKEN", "").strip()
    ct0 = os.environ.get("X_CT0", "").strip()
    if not auth or not ct0:
        meta["x_status"] = "skipped_no_credentials"
        print("[x] X_AUTH_TOKEN / X_CT0 未设置，跳过 X 采集", file=sys.stderr)
        return []

    tmp = tempfile.mkdtemp(prefix="brief-twscrape-")
    api = API(os.path.join(tmp, "accounts.db"))
    cookie_str = f"auth_token={auth}; ct0={ct0}"
    try:
        await api.pool.add_account_cookies("briefbot", cookie_str)
    except TypeError:
        await api.pool.add_account_cookies("briefbot", {"auth_token": auth, "ct0": ct0})

    def in_window(c: dict) -> bool:
        return window_start <= c["_dt"] < window_end

    out: list[dict] = []

    # 1) 名单巡查
    for row in watchlist:
        handle = row["handle"]
        status = "failed"
        got: list[dict] = []
        try:
            user = await api.user_by_login(handle)
            uid = getattr(user, "id", None) if user else None
            if uid:
                async for tw in api.user_tweets(uid, limit=30):
                    c = _tweet_to_candidate(tw, "x_watchlist")
                    if c and in_window(c):
                        got.append(c)
                status = "ok" if got else "no_posts"
        except Exception as e:
            meta["errors"].append(f"x_watchlist @{handle}: {type(e).__name__}: {e}")
        if status == "failed":  # 回退：from: 搜索（个别账号 user_by_login 取不到）
            try:
                async for tw in api.search(f"from:{handle}", limit=20, kv={"product": "Latest"}):
                    c = _tweet_to_candidate(tw, "x_watchlist")
                    if c and in_window(c):
                        got.append(c)
                status = "ok" if got else "failed"
            except Exception as e:
                meta["errors"].append(f"x_watchlist @{handle} fallback: {type(e).__name__}: {e}")
        meta["watchlist_status"][handle] = status
        meta["counts"][f"x_watchlist_{handle}"] = len(got)
        out.extend(got)

    # 2) 关键词搜索
    search_total = 0
    for query, product in X_SEARCHES:
        try:
            n = 0
            async for tw in api.search(query, limit=25, kv={"product": product}):
                c = _tweet_to_candidate(tw, "x_search")
                if c and in_window(c):
                    out.append(c)
                    n += 1
            search_total += n
        except Exception as e:
            meta["errors"].append(f"x_search [{query}]: {type(e).__name__}: {e}")
    meta["counts"]["x_search"] = search_total
    meta["x_status"] = "ok"
    for c in out:
        c.pop("_dt", None)
    return out


def collect_x(window_start: datetime, window_end: datetime,
              watchlist: list[dict], meta: dict) -> list[dict]:
    if os.environ.get("BRIEF_SKIP_X") == "1":
        meta["x_status"] = "skipped_by_env"
        return []
    try:
        return asyncio.run(_collect_x_async(window_start, window_end, watchlist, meta))
    except Exception as e:  # twscrape 整体失败：输出空 X 部分并注明，不中断
        meta["x_status"] = f"failed: {type(e).__name__}: {e}"
        meta["errors"].append(f"x_collect: {type(e).__name__}: {e}")
        print(f"[x] twscrape 整体失败：{e}", file=sys.stderr)
        return []


# ---------------------------------------------------------------- main
def main() -> int:
    window_start, window_end, date_str = target_window()
    meta: dict = {
        "date": date_str,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "counts": {},
        "errors": [],
        "x_status": "not_run",
        "watchlist_status": {},
    }
    watchlist = load_watchlist()
    meta["watchlist_total"] = len(watchlist)

    all_items: list[dict] = []
    all_items.extend(collect_aihot(window_end, meta))
    all_items.extend(collect_newsnow(window_end, meta))
    all_items.extend(collect_x(window_start, window_end, watchlist, meta))

    # 去重（按 status id / url），保留先到者（AIHOT > NewsNow > X 巡查 > X 搜索的加入顺序内）
    seen: set[str] = set()
    deduped: list[dict] = []
    for c in all_items:
        k = dedup_key(c)
        if k in seen:
            continue
        seen.add(k)
        deduped.append(c)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "candidates.json").write_text(
        json.dumps(deduped, ensure_ascii=False, indent=1), encoding="utf-8")
    meta["total_candidates"] = len(deduped)
    (DATA_DIR / "collect_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"[collect] date={date_str} window={meta['window_start']}..{meta['window_end']}")
    for k, v in sorted(meta["counts"].items()):
        print(f"[collect] {k}: {v}")
    print(f"[collect] total(deduped)={len(deduped)}  x_status={meta['x_status']}")
    for e in meta["errors"]:
        print(f"[collect][warn] {e}", file=sys.stderr)

    if not deduped:
        print("[collect] 全部来源均无产出，退出以避免生成空简报", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

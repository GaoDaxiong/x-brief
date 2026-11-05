#!/usr/bin/env python3
"""每日简报 · 生成（GitHub Actions 流水线第二步）。

流程：
  1. 读 data/candidates.json（collect.py 产出），本地粗筛（方向关键词 / 排除词 /
     互动量排序 / 每源限量），分成新闻池、X 池、福利池、名单分组并编号 cid。
  2. 调用 GitHub Models（https://models.github.ai/inference/chat/completions，
     model=openai/gpt-4o-mini，Authorization: Bearer $GITHUB_TOKEN）分 3 次完成
     选题 + 一句话摘要 + 分区 + 来源标签。模型只能引用候选 cid，链接一律由
     候选原始 url 生成，杜绝编造。任一调用失败重试 1 次，再失败退出非零。
  3. 以仓库内最新一期 briefs/*.html 为模板（保留 CSS/JS/收藏逻辑），渲染
     briefs/<日期>.html、index.html（../ 前缀改 ./）、archive.html（追加一条）、
     sw.js（VERSION 换新）。收藏 JSON（xb-items）与条目 data-fid 一一对应。

环境变量：
  BRIEF_DATE   目标日期 YYYY-MM-DD（缺省为当天，Asia/Shanghai）
  GITHUB_TOKEN GitHub Models 调用令牌（DRY_RUN 时不需要）
  DRY_RUN=1    跳过模型，用规则摘要离线生成（本地自测用）

输出另写 data/generate_meta.json（各分区条数等，供工作流步骤摘要使用）。
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import secrets
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

CST = ZoneInfo("Asia/Shanghai")
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

MODEL_URL = "https://models.github.ai/inference/chat/completions"
MODEL_NAME = "openai/gpt-4o-mini"

# ------------------------------------------------------------------ 分区与标签
# id, 目录名, emoji, CSS 变量, JSON 用的收藏色对
SECTIONS = [
    ("top3", "今日重点", "🔥", "--top2", ["#e0306f", "#f06595"]),
    ("ai", "AI 与大模型", "🤖", "--th-violet", ["#7c3aed", "#a78bfa"]),
    ("tech", "编程与开发者工具", "💻", "--th-indigo", ["#4f46e5", "#8b93ff"]),
    ("company", "科技公司动态", "🏢", "--th-sky", ["#0284c7", "#38bdf8"]),
    ("creations", "AI 造物精选", "🎨", "--th-royal", ["#5b4fd6", "#a59bff"]),
    ("xcn", "X 中文圈精选", "💬", "--th-orange", ["#ea580c", "#fb923c"]),
    ("deals", "薅羊毛 · 福利", "🎟", "--th-rose", ["#e11d48", "#fb7185"]),
    ("watchlist", "关注账号动态", "👀", "--th-purple", ["#a21caf", "#e879f9"]),
]
SEC_BY_ID = {s[0]: s for s in SECTIONS}
XCN_SUBS = [("xcn_exp", "经验帖", "📝"), ("xcn_tut", "教程帖", "📚"), ("xcn_oss", "开源推荐", "⭐")]

TAG_CLASS = {
    "官方": "t-official",
    "个人分享": "t-personal",
    "转述": "t-relay",
    "媒体报道": "t-media",
    "推广": "t-promo",
    "未证实": "t-unverified",
}
TARGETS = {"top3": 10, "ai": 10, "tech": 10, "company": 10,
           "creations": 20, "xcn_total": 10, "deals": 20}

EXCLUDE_WORDS = [
    "刑案", "嫌疑人", "通缉", "命案", "杀人", "强奸", "猥亵",
    "股价", "涨停", "跌停", "涨幅", "跌幅", "A股", "沪指", "深成指", "创业板指",
    "金价", "黄金价格", "白银价格", "进博会", "广交会",
    "校招", "起薪", "副教授去世", "教师离世", "学生跳楼", "公诉", "落马", "被查",
    "中期选举", "大选投票", "明星", "整容", "恋情", "彩票",
    "空投", "币圈", "加密货币", "比特币价格", "以太坊价格", "炒币", "合约交易",
]
BUCKET_KEYWORDS = {
    "ai": ["模型", "LLM", "GPT", "Claude", "Gemini", "大模型", "推理模型", "参数",
           "开源权重", "多模态", "智能体", "Agent", "benchmark", "评测"],
    "tech": ["代码", "编程", "开发", "IDE", "编辑器", "GitHub", "开源", "API", "SDK",
             "框架", "插件", "命令行", "终端", "重构", "部署", "Codex", "Cursor"],
    "company": ["收购", "融资", "任命", "离职", "加入", "跳槽", "裁员", "营收", "发布会",
                "公司", "团队", "创始人", "CEO"],
    "creations": ["AI 视频", "AI视频", "生成视频", "短片", "MV", "动画", "游戏", "复刻",
                  "Sora", "可灵", "即梦", "Runway", "Pika", "数字人", "AI 绘画", "AI绘画",
                  "文生图", "图生视频", "作品"],
    "deals": ["免费", "限免", "额度", "优惠", "赠送", "白嫖", "试用", "折扣", "领取",
              "邀请码", "Pro 会员", "订阅优惠", "0 元", "免费领取"],
    "xcn_exp": ["经验", "实测", "心得", "复盘", "方法论", "踩坑"],
    "xcn_tut": ["教程", "课程", "手把手", "入门", "指南", "拆解"],
    "xcn_oss": ["开源", "GitHub", "仓库", "star", "Star"],
}

STAR_SVG = ('<svg class="si" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3.2l2.55 5.55 '
            '6.05.7-4.5 4.15 1.25 6-5.35-3.05L6.65 19.6l1.25-6-4.5-4.15 6.05-.7z"/></svg>')
LINK_SVG = ('<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M4.5 2.5h5v5M9.5 2.5 3 9" /></svg>')
PLACEHOLDER_AVATAR = "data:image/gif;base64,R0lGODlhAQABAIAAAP///wAAACH5BAEAAAAALAAAAAABAAEAAAICRAEAOw=="

WEEKDAY_CN = "一二三四五六日"


def esc(s: str) -> str:
    return html.escape(str(s or ""), quote=True)


def collapse(s: str, limit: int = 0) -> str:
    t = re.sub(r"\s+", " ", str(s or "")).strip()
    return t[:limit] if limit and len(t) > limit else t


# ------------------------------------------------------------------ 候选粗筛
def dedup_key(c: dict) -> str:
    m = re.search(r"/status/(\d+)", c.get("url", ""))
    if m:
        return f"status:{m.group(1)}"
    return "url:" + c.get("url", "").rstrip("/").lower()


def excluded(c: dict) -> bool:
    if c.get("source") == "x_watchlist":
        return False  # 名单博主为人工 curated，不做关键词排除
    text = c.get("text", "")
    return any(w in text for w in EXCLUDE_WORDS)


def bucket_score(c: dict, bucket: str) -> int:
    text = c.get("text", "")
    return sum(1 for kw in BUCKET_KEYWORDS[bucket] if kw.lower() in text.lower())


def engagement(c: dict) -> int:
    return int(c.get("likes", 0) or 0) + 2 * int(c.get("retweets", 0) or 0)


class Pools:
    """把候选编号并分池；同一候选跨池共用一个 cid。"""

    def __init__(self, candidates: list[dict]):
        self.by_cid: dict[str, dict] = {}
        self._key2cid: dict[str, str] = {}
        self._n = 0
        self.news_pool: list[str] = []
        self.x_pool: list[str] = []
        self.deals_pool: list[str] = []
        self.watch_groups: dict[str, list[str]] = {}
        self._build(candidates)

    def cid_of(self, c: dict) -> str:
        k = dedup_key(c)
        if k not in self._key2cid:
            self._n += 1
            cid = f"c{self._n}"
            self._key2cid[k] = cid
            self.by_cid[cid] = c
        return self._key2cid[k]

    def _build(self, candidates: list[dict]):
        usable = [c for c in candidates if not excluded(c) and c.get("url")]
        aihot = [c for c in usable if c["source"].startswith("aihot")]
        order = {"aihot_hot": 0, "aihot_daily": 1, "aihot_latest": 2}
        aihot.sort(key=lambda c: order.get(c["source"], 3))
        newsnow = [c for c in usable if c["source"].startswith("newsnow")
                   and (bucket_score(c, "ai") + bucket_score(c, "tech")
                        + bucket_score(c, "company")) > 0]
        x_search = sorted([c for c in usable if c["source"] == "x_search"],
                          key=engagement, reverse=True)
        x_watch = [c for c in usable if c["source"] == "x_watchlist"]

        for c in aihot[:30] + newsnow[:12] + x_search[:20]:
            cid = self.cid_of(c)
            if cid not in self.news_pool:
                self.news_pool.append(cid)
        for c in sorted(x_search + x_watch, key=engagement, reverse=True)[:80]:
            cid = self.cid_of(c)
            if cid not in self.x_pool:
                self.x_pool.append(cid)
        deals = [c for c in usable if bucket_score(c, "deals") > 0]
        deals.sort(key=engagement, reverse=True)
        for c in deals[:30]:
            cid = self.cid_of(c)
            if cid not in self.deals_pool:
                self.deals_pool.append(cid)
        for c in x_watch:
            cid = self.cid_of(c)
            self.watch_groups.setdefault(c.get("handle", ""), []).append(cid)


def cand_line(pools: Pools, cid: str) -> str:
    c = pools.by_cid[cid]
    who = c.get("author", "")
    if c.get("handle"):
        who += f" @{c['handle']}"
    return (f"{cid} | {c['source']} | {who} | ♥{c.get('likes', 0)} ↻{c.get('retweets', 0)} "
            f"| {collapse(c.get('text', ''), 200)}")


# ------------------------------------------------------------------ 模型调用
SYSTEM_POLICY = """你是《X 每日简报 · X 情报站》的编辑。你的任务是从候选条目中选题、写一句话摘要、归入分区、标注来源类型，输出严格 JSON。

分区定义：
- top3 今日重点：当天最值得看的几条（可与其他分区重复选题角度，但同一条目全局只出现一次）
- ai AI 与大模型：模型发布/更新、能力评测、研究进展
- tech 编程与开发者工具：开发工具、开源项目动态、编程方法论、IDE/Agent 工具
- company 科技公司动态：公司层面的收购、融资、人事、组织与产品战略（不收股价行情）
- creations AI 造物精选：用 AI 做出的视频、游戏、图片、音乐、互动作品等具体成品与案例
- xcn_exp / xcn_tut / xcn_oss X 中文圈精选的经验帖 / 教程帖 / 开源推荐（只收中文圈博主的帖）
- deals 薅羊毛·福利：真实可领的免费额度、限免、优惠、赠送

来源标签只能取：官方 / 个人分享 / 转述 / 媒体报道 / 推广 / 未证实。
判定：一手当事人或官方账号发布=官方；博主个人观点与实测=个人分享；转述他人或媒体说法=转述；新闻媒体报道=媒体报道；明显营销=推广；只有传闻、无一手证据=未证实。

硬规则（违反即作废）：
1. 只能引用候选里给出的 cid，绝不编造链接、数字、人物或事实；摘要只能基于候选文本。
2. 传闻、猜测、单一匿名信源必须标「未证实」；转述类标「转述」并在 detail 里注明消息来源。
3. 绝不收录：社会/刑案、时政人事、校园事件、职场争议、贸易展会、金价、A股与大盘行情、纯财经、加密货币空投。
4. headline ≤ 38 字，讲清事实；detail 一句话到两句话（60–110 字），补充候选文本里的关键细节与互动量。
5. 同一事件多条候选时只选信息最全的一条；可在 also_cids 里附带 1-2 条同事件候选作补充链接。
6. 条数是目标不是配额：候选不足或质量不够时宁可少选，绝不用不相关内容凑数。
7. 输出只能是 JSON 对象本身，不要 Markdown 代码块，不要任何解释文字。"""


def call_model(user_prompt: str) -> dict:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        raise RuntimeError("GITHUB_TOKEN 未设置")
    body = json.dumps({
        "model": MODEL_NAME,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM_POLICY},
            {"role": "user", "content": user_prompt},
        ],
    }).encode("utf-8")
    last_err: Exception | None = None
    for attempt in (1, 2):  # 失败重试 1 次
        try:
            req = urllib.request.Request(
                MODEL_URL, data=body, method="POST",
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"]
            return json.loads(content)
        except Exception as e:
            last_err = e
            print(f"[generate][warn] 模型调用第 {attempt} 次失败：{type(e).__name__}: {e}",
                  file=sys.stderr)
    raise RuntimeError(f"模型调用两次均失败：{last_err}")


def _valid_news(raw: dict, pools: Pools, used: set[str], cap: int) -> list[dict]:
    out: list[dict] = []
    for it in raw if isinstance(raw, list) else []:
        if len(out) >= cap:
            break
        if not isinstance(it, dict):
            continue
        cid = str(it.get("cid", ""))
        if cid not in pools.by_cid or cid in used:
            continue
        headline = collapse(it.get("headline", ""), 60)
        detail = collapse(it.get("detail", ""), 300)
        if not headline or not detail:
            continue
        used.add(cid)
        tag = it.get("tag", "")
        if tag not in TAG_CLASS:
            tag = "个人分享" if pools.by_cid[cid]["source"].startswith("x") else "转述"
        links = [link_for(pools, cid)]
        for extra in it.get("also_cids", []) or []:
            extra = str(extra)
            if extra in pools.by_cid and len(links) < 3:
                links.append(link_for(pools, extra))
        out.append({"cid": cid, "headline": headline, "detail": detail, "tag": tag,
                    "highlight": bool(it.get("highlight")), "links": links})
    return out


def link_for(pools: Pools, cid: str) -> dict:
    c = pools.by_cid[cid]
    if c.get("handle"):
        label = f"@{c['handle']}"
    elif c["source"].startswith("aihot"):
        label = "AIHOT 条目" if "aihot.news" in c["url"] else (c.get("author") or "原文")
    elif c["source"].startswith("newsnow"):
        label = c.get("author") or "热榜"
    else:
        label = c.get("author") or "原文"
    return {"label": label, "url": c["url"]}


def model_items(pools: Pools, watchlist: list[dict]) -> dict:
    used: set[str] = set()
    items: dict[str, list[dict]] = {k: [] for k in
                                    ("top3", "ai", "tech", "company", "creations",
                                     "xcn_exp", "xcn_tut", "xcn_oss", "deals", "watchlist")}

    # ---- 调用 A：新闻类分区
    lines = "\n".join(cand_line(pools, cid) for cid in pools.news_pool)
    prompt_a = (
        f"候选（新闻池）：\n{lines}\n\n"
        f"请输出 JSON：{{\"top3\": [...], \"ai\": [...], \"tech\": [...], \"company\": [...]}}\n"
        f"条数目标：top3 {TARGETS['top3']}、ai {TARGETS['ai']}、tech {TARGETS['tech']}、"
        f"company {TARGETS['company']}（不足可少选）。\n"
        "每条格式：{\"cid\": \"c1\", \"headline\": \"...\", \"detail\": \"...\", "
        "\"tag\": \"官方\", \"highlight\": false, \"also_cids\": []}。"
        "每个分区最多把 2 条标 highlight=true（真正重要的）。")
    res_a = call_model(prompt_a)
    for sec in ("top3", "ai", "tech", "company"):
        items[sec] = _valid_news(res_a.get(sec), pools, used, TARGETS[sec])

    # ---- 调用 B：造物 + 中文圈
    lines = "\n".join(cand_line(pools, cid) for cid in pools.x_pool)
    prompt_b = (
        f"候选（X 池，含名单博主与搜索结果）：\n{lines}\n\n"
        "请输出 JSON：{\"creations\": [...], \"xcn_exp\": [...], \"xcn_tut\": [...], "
        "\"xcn_oss\": [...]}\n"
        f"条数目标：creations {TARGETS['creations']}；xcn 三个子区合计 {TARGETS['xcn_total']}"
        "（经验/教程/开源按内容性质分）。每条格式同前。"
        "creations 只收具体作品/案例，不要纯资讯；xcn 只收中文帖。")
    res_b = call_model(prompt_b)
    items["creations"] = _valid_news(res_b.get("creations"), pools, used, TARGETS["creations"])
    xcn_left = TARGETS["xcn_total"]
    for sec in ("xcn_exp", "xcn_tut", "xcn_oss"):
        got = _valid_news(res_b.get(sec), pools, used, xcn_left)
        items[sec] = got
        xcn_left -= len(got)

    # ---- 调用 C：福利 + 名单账号动态
    deal_lines = "\n".join(cand_line(pools, cid) for cid in pools.deals_pool)
    acc_blocks = []
    for row in watchlist:
        cids = pools.watch_groups.get(row["handle"], [])
        if not cids:
            continue
        ls = "\n".join("  " + cand_line(pools, cid) for cid in cids[:8])
        acc_blocks.append(f"账号 @{row['handle']}（{row['name']}）：\n{ls}")
    prompt_c = (
        f"候选（福利池）：\n{deal_lines}\n\n"
        f"候选（名单账号近 24h 发帖，按账号分组）：\n" + "\n".join(acc_blocks) + "\n\n"
        "请输出 JSON：{\"deals\": [...], \"watchlist\": [...]}\n"
        f"deals 条数目标 {TARGETS['deals']}（不足可少选，坚决剔除加密货币空投与刷量帖）。"
        "每条格式：{\"cid\": \"c1\", \"headline\": \"...\", \"how\": \"怎么领（一句话）\", "
        "\"deadline\": \"截止/有效期（候选没写就填『以原帖与平台页面为准』）\", \"tag\": \"转述\"}。\n"
        "watchlist：每个有发帖的账号一条，格式：{\"handle\": \"dotey\", "
        "\"summary\": \"把该账号这几条帖子概括成一句话（≤60字）\", \"cid\": \"代表帖的cid\"}。")
    res_c = call_model(prompt_c)
    for it in res_c.get("deals") if isinstance(res_c.get("deals"), list) else []:
        if len(items["deals"]) >= TARGETS["deals"]:
            break
        if not isinstance(it, dict):
            continue
        cid = str(it.get("cid", ""))
        if cid not in pools.by_cid or cid in used:
            continue
        headline = collapse(it.get("headline", ""), 60)
        if not headline:
            continue
        used.add(cid)
        tag = it.get("tag", "")
        if tag not in TAG_CLASS:
            tag = "转述"
        items["deals"].append({
            "cid": cid, "headline": headline,
            "how": collapse(it.get("how", ""), 200) or "见原帖说明",
            "deadline": collapse(it.get("deadline", ""), 80) or "以原帖与平台页面为准",
            "tag": tag, "links": [link_for(pools, cid)],
        })
    wl_by_handle = {r["handle"].lower(): r for r in watchlist}
    for it in res_c.get("watchlist") if isinstance(res_c.get("watchlist"), list) else []:
        if not isinstance(it, dict):
            continue
        row = wl_by_handle.get(str(it.get("handle", "")).lower().lstrip("@"))
        if not row:
            continue
        summary = collapse(it.get("summary", ""), 200)
        cid = str(it.get("cid", ""))
        if not summary or cid not in pools.by_cid:
            continue
        items["watchlist"].append({
            "handle": row["handle"], "name": row["name"], "identity": row["identity"],
            "summary": summary, "cid": cid,
            "avatar_url": pools.by_cid[cid].get("avatar_url", ""),
            "links": [{"label": "原帖", "url": pools.by_cid[cid]["url"]}],
        })
    return items


# ------------------------------------------------------------------ DRY_RUN 规则版
def dry_run_items(pools: Pools, watchlist: list[dict]) -> dict:
    items: dict[str, list[dict]] = {k: [] for k in
                                    ("top3", "ai", "tech", "company", "creations",
                                     "xcn_exp", "xcn_tut", "xcn_oss", "deals", "watchlist")}
    used: set[str] = set()

    def make(cid: str) -> dict:
        c = pools.by_cid[cid]
        first = collapse(c["text"].split("\n")[0], 42)
        detail = collapse(c["text"], 150)
        if c.get("likes"):
            detail += f" 原帖 ♥{c['likes']}。"
        if c["source"].startswith("aihot"):
            tag = "官方" if "官方" in c.get("author", "") else "转述"
        elif c["source"].startswith("newsnow"):
            tag = "媒体报道"
        else:
            tag = "个人分享"
        return {"cid": cid, "headline": first or "(无标题)", "detail": detail,
                "tag": tag, "highlight": False, "links": [link_for(pools, cid)]}

    def fill(sec: str, cids: list[str], cap: int, bucket: str | None = None):
        for cid in cids:
            if len(items[sec]) >= cap or cid in used:
                continue
            if bucket and bucket_score(pools.by_cid[cid], bucket) == 0 and sec != "ai":
                continue
            used.add(cid)
            items[sec].append(make(cid))

    # deals 需要 how/deadline 字段，单独构造
    for cid in pools.deals_pool:
        if len(items["deals"]) >= TARGETS["deals"] or cid in used:
            continue
        used.add(cid)
        base = make(cid)
        items["deals"].append({
            "cid": cid, "headline": base["headline"],
            "how": collapse(pools.by_cid[cid]["text"], 160),
            "deadline": "以原帖与平台页面为准",
            "tag": base["tag"], "links": base["links"],
        })
    fill("creations", pools.x_pool, TARGETS["creations"], "creations")
    fill("xcn_oss", pools.x_pool, 4, "xcn_oss")
    fill("xcn_tut", pools.x_pool, 3, "xcn_tut")
    fill("xcn_exp", pools.x_pool, 3, "xcn_exp")
    fill("top3", pools.news_pool, 3)
    fill("tech", pools.news_pool, TARGETS["tech"], "tech")
    fill("company", pools.news_pool, TARGETS["company"], "company")
    fill("ai", pools.news_pool, TARGETS["ai"])
    for sec in ("ai", "tech"):
        if items[sec]:
            items[sec][0]["highlight"] = True
    for row in watchlist:
        cids = pools.watch_groups.get(row["handle"], [])
        if not cids:
            continue
        heads = [collapse(pools.by_cid[c]["text"].split("\n")[0], 30) for c in cids[:3]]
        items["watchlist"].append({
            "handle": row["handle"], "name": row["name"], "identity": row["identity"],
            "summary": "；".join(heads), "cid": cids[0],
            "avatar_url": pools.by_cid[cids[0]].get("avatar_url", ""),
            "links": [{"label": "原帖", "url": pools.by_cid[cids[0]]["url"]}],
        })
    return items


# ------------------------------------------------------------------ 渲染
def fid_for(date_str: str, section: str, key: str) -> str:
    return "i-" + hashlib.md5(f"{date_str}|{section}|{key}".encode()).hexdigest()[:10]


def star_btn(fid: str, headline: str) -> str:
    return (f'<button class="star" type="button" data-star="{fid}" aria-pressed="false" '
            f'aria-label="收藏：{esc(headline)}" title="收藏">{STAR_SVG}</button>')


def links_html(links: list[dict]) -> str:
    if not links:
        return ""
    inner = "".join(
        f'<a class="lnk" href="{esc(l["url"])}" target="_blank" rel="noopener noreferrer">'
        f'{esc(l["label"])}{LINK_SVG}</a>' for l in links)
    return f'<div class="links">{inner}</div>'


def tag_html(tag: str) -> str:
    if tag in TAG_CLASS:
        return f' <span class="tag {TAG_CLASS[tag]}">{esc(tag)}</span>'
    return ""


class Renderer:
    def __init__(self, date_str: str, weekday_cn: str, items: dict, meta: dict,
                 watchlist: list[dict]):
        self.date = date_str
        y, m, d = (int(x) for x in date_str.split("-"))
        self.y, self.m, self.d = y, m, d
        self.date_label = f"{m}月{d}日（周{weekday_cn}）"
        self.weekday = f"周{weekday_cn}"  # 收藏 JSON 的 weekday 用全称（与旧刊一致）
        self.items = items
        self.meta = meta or {}
        self.watchlist = watchlist
        self.more_n = 0
        self.json_items: dict[str, dict] = {}
        self._avatar_cache: dict[str, str] = {}

    # ---- 收藏 JSON 登记
    def register(self, fid: str, kind: str, sec_id: str, sec_name: str,
                 emoji: str, color: list[str], headline: str, detail: str,
                 links: list[dict], tag: str | None = None, extra: dict | None = None):
        entry = {
            "id": fid, "date": self.date, "weekday": self.weekday, "kind": kind,
            "section": {"id": sec_id, "name": sec_name, "emoji": emoji, "color": color},
            "headline": headline, "detail": detail, "links": links,
        }
        if tag:
            entry["tag"] = tag
        if extra:
            entry.update(extra)
        self.json_items[fid] = entry

    def next_more(self, prefix: str) -> str:
        self.more_n += 1
        return f"{prefix}{self.more_n}"

    # ---- 条目
    def news_li(self, it: dict, sec_id: str, sec_name: str, emoji: str,
                color: list[str], kind: str = "news", rank: int | None = None) -> str:
        fid = fid_for(self.date, sec_id, it["cid"] + it["headline"])
        prefix = "t" if sec_id == "top3" else "m"
        more_id = self.next_more(prefix)
        hl = it.get("highlight") and rank is None
        self.register(fid, "top" if rank else kind, sec_id, sec_name, emoji, color,
                      it["headline"], it["detail"], it["links"], tag=it.get("tag"),
                      extra=({"rank": rank} if rank else
                             ({"highlight": True} if hl else None)))
        hlb = '<span class="hlb">⭐ 重点</span>' if hl else ""
        lc = "" if rank else f'<span class="lc">{len(it["links"])}</span>'
        row = (f'<div class="rw"><button class="row" type="button" aria-expanded="false" '
               f'aria-controls="{more_id}">{hlb}<span class="txt">'
               f'<strong class="hd">{esc(it["headline"])}</strong>{tag_html(it.get("tag", ""))}</span>'
               f'<span class="ind" aria-hidden="true">{lc}<span class="chv"></span></span></button>'
               f'{star_btn(fid, it["headline"])}</div>'
               f'<div class="more" id="{more_id}"><p class=dt>{esc(it["detail"])}</p>'
               f'{links_html(it["links"])}</div>')
        if rank:
            return (f'<li class="item f top-item" id="{fid}" data-fid="{fid}">'
                    f'<span class="num">{rank}</span><div class="tbody">{row}</div></li>')
        cls = "item f hl-item" if hl else "item f"
        return f'<li class="{cls}" id="{fid}" data-fid="{fid}">{row}</li>'

    def deal_li(self, it: dict) -> str:
        fid = fid_for(self.date, "deals", it["cid"] + it["headline"])
        detail_json = f"怎么领：{it['how']}。截止：{it['deadline']}"
        self.register(fid, "deal", "deals", "薅羊毛 · 福利与优惠", "🎟",
                      SEC_BY_ID["deals"][4], it["headline"], detail_json, it["links"],
                      tag=it.get("tag"), extra={"status": "active"})
        return (f'<li class="deal f" id="{fid}" data-fid="{fid}"><div class="dtop">'
                f'<span class="tag {TAG_CLASS.get(it.get("tag", "转述"), "t-relay")}">'
                f'{esc(it.get("tag", "转述"))}</span><span class="stb ok">进行中</span>'
                f'{star_btn(fid, it["headline"])}</div>'
                f'<p class="dh">{esc(it["headline"])}</p>'
                f'<p class="drow"><span class="dk">怎么领</span><span>{esc(it["how"])}</span></p>'
                f'<p class="drow"><span class="dk">截止</span><span>{esc(it["deadline"])}</span></p>'
                f'{links_html(it["links"])}</li>')

    def avatar(self, url: str) -> str:
        if not url:
            return PLACEHOLDER_AVATAR
        if url in self._avatar_cache:
            return self._avatar_cache[url]
        result = PLACEHOLDER_AVATAR
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=6) as resp:
                blob = resp.read(300_000)
                mime = resp.headers.get_content_type() or "image/jpeg"
            if blob:
                result = f"data:{mime};base64," + base64.b64encode(blob).decode()
        except Exception:
            pass
        self._avatar_cache[url] = result
        return result

    def acc_li(self, acc: dict) -> str:
        fid = fid_for(self.date, "watchlist", acc["handle"])
        self.register(fid, "account", "watchlist", "关注账号动态 › 名单博主", "👀",
                      SEC_BY_ID["watchlist"][4], acc["name"], acc["summary"], acc["links"],
                      extra={"username": acc["handle"], "identity": acc["identity"]})
        first_url = acc["links"][0]["url"] if acc["links"] else f"https://x.com/{acc['handle']}"
        return (f'<li class="acc f" id="{fid}" data-fid="{fid}">'
                f'<img class="av" src="{self.avatar(acc.get("avatar_url", ""))}" alt="" '
                f'width="36" height="36" loading="lazy" decoding="async">'
                f'<div class="abody"><div class="ahead">'
                f'<a class="aname" href="{esc(first_url)}" target="_blank" rel="noopener noreferrer">'
                f'{esc(acc["name"])}</a><span class="idb">{esc(acc["identity"])}</span>'
                f'<span class="hdl">@{esc(acc["handle"])}</span></div>'
                f'<p class="asum">{esc(acc["summary"])}</p></div>'
                f'<div class="links alinks">'
                f'<a class="lnk" href="{esc(first_url)}" target="_blank" rel="noopener noreferrer">'
                f'原帖{LINK_SVG}</a></div>{star_btn(fid, acc["name"])}</li>')

    # ---- 分区
    def section_html(self, sec_id: str) -> str:
        _, label, emoji, cvar, color = SEC_BY_ID[sec_id]
        its = self.items.get(sec_id, [])
        if sec_id == "top3":
            lis = "".join(self.news_li(it, "top3", f"今天最重要的 {len(its)} 件事",
                                       emoji, color, rank=i + 1)
                          for i, it in enumerate(its))
            return (f'<section class="card sec top" id="top3"><div class="tglow" '
                    f'aria-hidden="true"></div><h2 class="sech"><span class="emo">{emoji}</span>'
                    f'<span class="nm">今天最重要的 {len(its)} 件事</span></h2>'
                    f'<ol class="items">{lis}</ol></section>')
        if sec_id == "deals":
            lis = "".join(self.deal_li(it) for it in its)
            return (f'<section class="card sec" id="deals" style="--c:var({cvar})">'
                    f'<h2 class="sech"><span class="emo">{emoji}</span>'
                    f'<span class="nm">薅羊毛 · 福利与优惠</span><span class="cnt">{len(its)}</span></h2>'
                    f'<ul class="deals">{lis}</ul></section>')
        if sec_id == "watchlist":
            return self.watchlist_html()
        lis = "".join(self.news_li(it, sec_id, label, emoji, color) for it in its)
        return (f'<section class="card sec" id="{sec_id}" style="--c:var({cvar})">'
                f'<h2 class="sech"><span class="emo">{emoji}</span><span class="nm">{label}</span>'
                f'<span class="cnt">{len(its)}</span></h2><ul class="items">{lis}</ul></section>')

    def xcn_html(self) -> str:
        _, label, emoji, cvar, color = SEC_BY_ID["xcn"]
        total = sum(len(self.items.get(s, [])) for s, _, _ in XCN_SUBS)
        parts = [f'<section class="card sec" id="xcn" style="--c:var({cvar})">'
                 f'<h2 class="sech"><span class="emo">{emoji}</span><span class="nm">{label}</span>'
                 f'<span class="cnt">{total}</span></h2>']
        for sub_id, sub_name, sub_emoji in XCN_SUBS:
            its = self.items.get(sub_id, [])
            if not its:
                continue
            lis = "".join(self.news_li(it, f"xcn/{sub_id.split('_')[1]}",
                                       f"X 中文圈精选 › {sub_name}", sub_emoji, color)
                          for it in its)
            parts.append(f'<div class="sub" id="{sub_id.replace("_", "-")}"><h3 class="subh">'
                         f'<span class="emo">{sub_emoji}</span>{sub_name}</h3>'
                         f'<ul class="items">{lis}</ul></div>')
        parts.append("</section>")
        return "".join(parts)

    def watchlist_html(self) -> str:
        _, label, emoji, cvar, _ = SEC_BY_ID["watchlist"]
        accs = self.items.get("watchlist", [])
        status = self.meta.get("watchlist_status", {})
        by_handle = {r["handle"].lower(): r for r in self.watchlist}
        missing = []
        have = {a["handle"].lower() for a in accs}
        for h, st in status.items():
            if h.lower() in have:
                continue
            row = by_handle.get(h.lower())
            if row:
                missing.append((row, "本窗未发帖" if st == "no_posts" else "未取到"))
        gcount = f"{len(accs)} 条动态"
        if missing:
            gcount += f" · {len(missing)} 未发/未取到"
        lis = "".join(self.acc_li(a) for a in accs)
        nopost = ""
        if missing:
            chips = "".join(
                f'<a class="npc" href="https://x.com/{esc(r["handle"])}" target="_blank" '
                f'rel="noopener noreferrer"><span class="nav-av" '
                f'style="--h:{sum((i + 1) * ord(ch) for i, ch in enumerate(r["handle"])) % 360}">'
                f'{esc(r["name"][:1])}</span>{esc(r["name"])}（{why}）</a>'
                for r, why in missing)
            nopost = (f'<div class="nopost"><span class="nplabel">本窗未发帖 / 未取到：</span>'
                      f'{chips}</div>')
        return (f'<section class="card sec wl" id="watchlist" style="--c:var({cvar})">'
                f'<h2 class="sech"><span class="emo">{emoji}</span><span class="nm">{label}</span>'
                f'<span class="cnt">{len(accs)}</span>'
                f'<button class="mini" type="button" id="toggleAll" '
                f'aria-label="展开或收起全部分组">全部收起</button></h2>'
                f'<details class="grp" open><summary><span class="gname">名单博主</span>'
                f'<span class="gcount">{gcount}</span><span class="chev" aria-hidden="true"></span>'
                f'</summary><ul class=accs>{lis}</ul>{nopost}</details></section>')

    def quick_html(self, present: list[str]) -> str:
        lis = []
        for sec_id in present:
            if sec_id in ("top3", "watchlist"):
                continue
            _, label, emoji, cvar, _ = SEC_BY_ID[sec_id]
            if sec_id == "xcn":
                heads = [it["headline"] for s, _, _ in XCN_SUBS
                         for it in self.items.get(s, [])][:4]
            else:
                heads = [it["headline"] for it in self.items.get(sec_id, [])][:4]
            if not heads:
                continue
            qt = "；".join(heads) + "。"
            lis.append(f'<li><a href="#{sec_id}" style="--c:var({cvar})">'
                       f'<span class="qe">{emoji}</span><span class="qx">'
                       f'<span class="qn">{label}</span><span class="qt">{esc(qt)}</span></span>'
                       f'<span class="qa" aria-hidden="true">›</span></a></li>')
        if not lis:
            return ""
        return (f'<section class="card quick" id="quick"><h2 class="sech">'
                f'<span class="emo">⚡</span><span class="nm">今日速览</span></h2>'
                f'<ul>{"".join(lis)}</ul></section>')

    def warnings_html(self) -> str:
        ws, we = self.meta.get("window_start", ""), self.meta.get("window_end", "")
        lines = []
        if ws and we:
            lines.append(f"检索窗：北京时间 {ws[:16].replace('T', ' ')} 至 {we[5:16].replace('T', ' ')}。"
                         "本期由 GitHub Actions 流水线自动生成（选题与摘要经 GitHub Models 免费额度完成）。")
        xst = self.meta.get("x_status", "")
        if xst and xst != "ok":
            lines.append(f"X 数据本期不可用（{esc(xst)}），本期内容以 AIHOT 与热榜来源为主。")
        status = self.meta.get("watchlist_status", {})
        failed = [h for h, s in status.items() if s == "failed"]
        if failed:
            lines.append("名单中 " + "、".join("@" + h for h in failed) +
                         " 本窗接口未取到，名单保留，下期继续巡查。")
        all_tags = [e.get("tag") for e in self.json_items.values()]
        if any(t in ("未证实", "转述") for t in all_tags):
            lines.append("本期含标「未证实 / 转述」的条目：相关说法未回原文逐字核对，请以官方原文为准。")
        if self.items.get("deals"):
            lines.append("福利类信息时效性强，领取条件与截止以活动页面为准；凡要求付费、授权钱包或提供验证码的「福利」均属可疑，请勿参与。")
        lines.append("按用户偏好，本期排除社会 / 刑案、时政人事、校园、职场争议、贸易展会、金价与股市行情类内容。")
        lis = "".join(f'<li class="f">{ln}</li>' for ln in lines)
        return (f'<section class="card sec warn" id="warnings"><h2 class="sech">'
                f'<span class="emo">⚠️</span><span class="nm">提醒</span></h2><ul>{lis}</ul></section>')

    def present_sections(self) -> list[str]:
        present = []
        for sec_id, *_ in SECTIONS:
            if sec_id == "xcn":
                if any(self.items.get(s) for s, _, _ in XCN_SUBS):
                    present.append("xcn")
            elif self.items.get(sec_id):
                present.append(sec_id)
        return present

    def count_of(self, sec_id: str) -> int:
        if sec_id == "xcn":
            return sum(len(self.items.get(s, [])) for s, _, _ in XCN_SUBS)
        return len(self.items.get(sec_id, []))

    def render(self, template: str) -> str:
        present = self.present_sections()
        # ---- hero
        n_news = sum(len(self.items.get(s, [])) for s in
                     ("top3", "ai", "tech", "company", "creations",
                      "xcn_exp", "xcn_tut", "xcn_oss"))
        n_deals = len(self.items.get("deals", []))
        n_acc = len(self.items.get("watchlist", []))
        hero = (f'<header class="hero"><div class="wrap">\n'
                f'  <span class="kicker">🗞 X 每日简报 · {self.date_label}</span>\n'
                f'  <h1>{self.date_label}</h1>\n'
                f'  <p class="subt">{self.date} · AI / 编程 / 科技公司动态速览</p>\n'
                f'  <div class="stats"><span><b>{n_news}</b> 条要闻</span>'
                f'<span><b>{n_deals}</b> 条福利</span><span><b>{n_acc}</b> 条账号动态</span>'
                f'<span><b>{len(present)}</b> 个板块</span></div>\n'
                f'  <button class="theme" id="themeBtn" type="button" aria-label="切换主题">🌙</button>\n'
                f'</div></header>\n')
        # ---- chipbar + aside
        chips, asides = [], []
        for sec_id in present + ["warnings"]:
            if sec_id == "warnings":
                label, emoji, cvar, cnt = "提醒", "⚠️", "--y-fg", None
            else:
                _, label, emoji, cvar, _ = SEC_BY_ID[sec_id]
                cnt = self.count_of(sec_id)
            chips.append(f'<a data-nav href="#{sec_id}" style="--c:var({cvar})">{emoji} {label}</a>')
            cnt_html = f'<span class=c>{cnt}</span>' if cnt is not None else ""
            asides.append(f'<a data-nav href="#{sec_id}" style="--c:var({cvar})">'
                          f'<span class="emo">{emoji}</span>{label}{cnt_html}</a>')
        chipbar = ('<nav class="chipbar" aria-label="板块导航"><div class="chips">'
                   + "".join(chips) + '</div></nav>\n')
        aside = ('<div class="wrap layout">\n  <aside class="side" aria-label="目录"><h4>目录</h4>'
                 + "".join(asides) + '</aside>')
        # ---- sections（顺序与模板一致：top3 → quick → 其余 → warnings）
        secs: list[str] = []
        if "top3" in present:
            secs.append(self.section_html("top3"))
        secs.append(self.quick_html(present))
        for sec_id in ("ai", "tech", "company", "creations"):
            if sec_id in present:
                secs.append(self.section_html(sec_id))
        if "xcn" in present:
            secs.append(self.xcn_html())
        if "deals" in present:
            secs.append(self.section_html("deals"))
        if "watchlist" in present:
            secs.append(self.watchlist_html())
        secs.append(self.warnings_html())
        sections_html = "".join(s for s in secs if s)
        footer = (f'    <footer>X 每日简报 · {self.date_label} · {self.date} · '
                  f'单文件离线版</footer>\n')
        # ---- 拼接模板
        if '<header class="hero">' not in template or '</aside>' not in template:
            raise RuntimeError("模板结构与预期不符（缺 hero/aside 锚点），拒绝生成")
        prefix = template[:template.index('<header class="hero">')]
        prefix = re.sub(r"<title>.*?</title>",
                        f"<title>X 每日简报 · {self.date_label} · {self.date_label}</title>",
                        prefix, count=1, flags=re.S)
        first_sec = template.index('<section class="card')
        mid_static = template[template.index('</aside>') + len('</aside>'):first_sec]
        suffix = template[template.rindex('  </main>'):]
        page = prefix + hero + chipbar + aside + mid_static + sections_html + footer + suffix
        # ---- 收藏 JSON
        payload = {"v": 1, "date": self.date, "weekday": self.weekday,
                   "title": f"X 每日简报 · {self.date_label}", "subtitle": self.date_label,
                   "items": self.json_items}
        payload_str = json.dumps(payload, ensure_ascii=False).replace("</script", "<\\/script")
        page, n = re.subn(r'(<script type="application/json" id="xb-items">).*?(</script>)',
                          lambda m: m.group(1) + payload_str + m.group(2), page, count=1, flags=re.S)
        if n == 0:
            raise RuntimeError("模板缺少 xb-items 收藏 JSON 锚点，拒绝生成")
        return page


# ------------------------------------------------------------------ archive / sw
def update_archive(archive_html: str, date_str: str, weekday_cn: str) -> str:
    y, m, d = (int(x) for x in date_str.split("-"))
    already = f"briefs/{date_str}.html" in archive_html
    out = archive_html.replace('<span class="new">最新</span>', "")
    if not already:
        out = re.sub(r"共 (\d+) 期",
                     lambda mm: f"共 {int(mm.group(1)) + 1} 期", out, count=1)
    entry = (f'<li><a href="briefs/{date_str}.html"><div class="d"><b>{d}</b><span>{m}月</span></div>'
             f'<div class="m"><div class="t">{y}年{m}月{d}日 · 周{weekday_cn}'
             f'<span class="new">最新</span></div><div class="s">X 每日简报</div></div>'
             f'<span class="go">›</span></a></li>')
    if f"briefs/{date_str}.html" in out:  # 重跑同日：替换旧条目
        out = re.sub(r'<li><a href="briefs/' + date_str + r'\.html">.*?</a></li>',
                     entry, out, count=1, flags=re.S)
        return out
    out = re.sub(r"(X 情报站 · 最新 )\d{4}-\d{2}-\d{2}",
                 lambda mm: mm.group(1) + date_str, out)
    month_head = f"<h2>{y}年{m}月</h2><ul>"
    if month_head in out:
        out = out.replace(month_head, month_head + "\n" + entry, 1)
    else:
        anchor = '<div class="wrap">\n'
        if anchor not in out:
            raise RuntimeError("archive.html 结构与预期不符（缺 wrap 锚点）")
        out = out.replace(anchor, anchor + f"<h2>{y}年{m}月</h2><ul>\n{entry}\n</ul>\n", 1)
    return out


def update_sw(sw_js: str) -> str:
    new, n = re.subn(r"(const VERSION = ')[^']+(')",
                     lambda m: m.group(1) + secrets.token_hex(5) + m.group(2), sw_js, count=1)
    if n == 0:
        raise RuntimeError("sw.js 缺少 VERSION 常量")
    return new


# ------------------------------------------------------------------ main
def load_watchlist() -> list[dict]:
    rows: list[dict] = []
    p = ROOT / "scripts" / "watchlist.txt"
    if not p.exists():
        return rows
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [x.strip() for x in line.split("|")]
        rows.append({"handle": parts[0].lstrip("@"),
                     "name": parts[1] if len(parts) > 1 else parts[0],
                     "identity": parts[2] if len(parts) > 2 else ""})
    return rows


def main() -> int:
    raw_date = os.environ.get("BRIEF_DATE", "").strip()
    if raw_date:
        day = datetime.strptime(raw_date, "%Y-%m-%d").date()
    else:
        day = datetime.now(CST).date()
    date_str = day.isoformat()
    weekday_cn = WEEKDAY_CN[day.weekday()]
    dry = os.environ.get("DRY_RUN") == "1"

    cand_path = DATA_DIR / "candidates.json"
    if not cand_path.exists():
        print("[generate] data/candidates.json 不存在，请先运行 collect.py", file=sys.stderr)
        return 2
    candidates = json.loads(cand_path.read_text(encoding="utf-8"))
    meta_path = DATA_DIR / "collect_meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}

    watchlist = load_watchlist()
    pools = Pools(candidates)
    print(f"[generate] 候选 {len(candidates)} 条 → 粗筛编号 {len(pools.by_cid)} 条 "
          f"(news={len(pools.news_pool)} x={len(pools.x_pool)} deals={len(pools.deals_pool)})")

    items = dry_run_items(pools, watchlist) if dry else model_items(pools, watchlist)
    total = sum(len(v) for v in items.values())
    counts = {k: len(v) for k, v in items.items() if v}
    print(f"[generate] 成稿条目 {total} 条：{counts}")
    if total == 0:
        print("[generate] 模型未产出任何条目，退出（不产出半成品）", file=sys.stderr)
        return 3

    briefs_dir = ROOT / "briefs"
    brief_files = sorted(briefs_dir.glob("*.html")) if briefs_dir.exists() else []
    brief_files = [p for p in brief_files if re.match(r"\d{4}-\d{2}-\d{2}\.html", p.name)]
    if not brief_files:
        print("[generate] 仓库内找不到 briefs/*.html 模板", file=sys.stderr)
        return 2
    template_path = brief_files[-1]
    if template_path.name == f"{date_str}.html" and len(brief_files) > 1:
        template_path = brief_files[-2]
    template = template_path.read_text(encoding="utf-8")
    print(f"[generate] 模板：{template_path.name}")

    renderer = Renderer(date_str, weekday_cn, items, meta, watchlist)
    page = renderer.render(template)
    # 校验：data-fid 与收藏 JSON 一一对应
    fids = re.findall(r'data-fid="([^"]+)"', page)
    if set(fids) != set(renderer.json_items.keys()) or len(fids) != len(renderer.json_items):
        print("[generate] 校验失败：data-fid 与收藏 JSON 不一致，拒绝写出", file=sys.stderr)
        return 3

    briefs_dir.mkdir(parents=True, exist_ok=True)
    (briefs_dir / f"{date_str}.html").write_text(page, encoding="utf-8")
    index_page = re.sub(r'((?:href|src)=")\.\./', r"\1./", page)
    index_page = index_page.replace("register('../sw.js'", "register('./sw.js'")
    index_page = index_page.replace("scope:'../'", "scope:'./'")
    (ROOT / "index.html").write_text(index_page, encoding="utf-8")

    arch_path = ROOT / "archive.html"
    if arch_path.exists():
        arch_path.write_text(update_archive(arch_path.read_text(encoding="utf-8"),
                                            date_str, weekday_cn), encoding="utf-8")
    sw_path = ROOT / "sw.js"
    if sw_path.exists():
        sw_path.write_text(update_sw(sw_path.read_text(encoding="utf-8")), encoding="utf-8")

    gen_meta = {"date": date_str, "weekday": weekday_cn, "dry_run": dry,
                "counts": counts, "total_items": total,
                "template": template_path.name}
    (DATA_DIR / "generate_meta.json").write_text(
        json.dumps(gen_meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[generate] 已写出 briefs/{date_str}.html、index.html、archive.html、sw.js")
    return 0


if __name__ == "__main__":
    sys.exit(main())

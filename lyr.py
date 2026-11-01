# -*- coding: utf-8 -*-
"""lyrics.net.cn 歌词抓取 + LRC 文本解析工具。

网站结构（2026-11 实测）：
  搜索  GET /search/?q=<词>   结果区一「按歌名搜索」内 <a href="/lyrics/<id>">歌名-歌手</a>
  详情  GET /lyrics/<id>      <div class="lyrics_main"> 内每行一个 <div>歌词行</div>

抓取统一走 curl 子进程（requests 的 TLS 指纹在部分国内站点会被拒，服务器上已验证 curl 稳定）。
"""
from __future__ import annotations

import json
import re
import subprocess

SITE = "https://lyrics.net.cn"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
HTTP_TIMEOUT = 20

# 制作信息行（不算歌词正文）
CREDIT_RE = re.compile(
    r"作词|作曲|编曲|制作|混音|母带|监制|填词|谱曲|版权|录音|和声|吉他|贝斯|键盘|弦乐|"
    r"编排|出品|发行|策划|封面|文案|翻译|统筹|宣发|企划|出品人|词曲|op|sp", re.I)
# LRC 时间戳
TS_RE = re.compile(r"^\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
# 元数据标签行 [ti:] [ar:] 等（无冒号数值的如 [00:10] 属时间戳，已被 TS_RE 先行匹配）
TAG_RE = re.compile(r"^\[(ti|ar|al|by|offset|length|re|ve|hash|total|encoding):", re.I)


class LyrError(Exception):
    pass


def _curl(url: str) -> str:
    """curl 子进程抓页面，失败抛 LyrError。"""
    try:
        proc = subprocess.run(
            ["curl", "-4", "-sS", "-m", str(HTTP_TIMEOUT), "-A", UA, url],
            capture_output=True, timeout=HTTP_TIMEOUT + 10, shell=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise LyrError(f"抓取失败：{e}") from e
    if proc.returncode != 0:
        raise LyrError(f"curl 退出码 {proc.returncode}")
    return proc.stdout.decode("utf-8", errors="replace")


def _norm(s: str) -> str:
    """归一化：全角转半角、去标点空格、小写。用于歌名/歌手匹配。"""
    table = {ord(c): e for c, e in zip("，。！？：（）【】“”‘’", ',.!?()[]""\'\'')}
    s = s.translate(table)
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", s).lower()


# ==================== 搜索与详情 ====================

def search_site(query: str) -> list:
    """按关键词搜索，返回 [{id, label}]（取「按歌曲名搜索」区，保持站点排序）。"""
    html = _curl(f"{SITE}/search/?q={_urlquote(query)}")
    heads = [m.start() for m in re.finditer(r'class="search_title"', html)]
    if not heads:
        return []
    heads.append(len(html))
    seg = ""
    for i in range(len(heads) - 1):           # 优先「按歌曲名搜索」区，找不到就全页
        chunk = html[heads[i]:heads[i + 1]]
        if "歌名" in chunk[:120]:
            seg = chunk
            break
    if not seg:
        seg = html
    out, seen = [], set()
    for m in re.finditer(r'href="/lyrics/(\d+)">([^<]+)</a>', seg):
        lid, label = m.group(1), m.group(2).strip()
        if lid not in seen:
            out.append({"id": lid, "label": label})
            seen.add(lid)
    return out


def _urlquote(s: str) -> str:
    from urllib.parse import quote
    return quote(s, safe="")


def fetch(lid: str) -> dict:
    """抓详情页，返回 {title, artist, lines}；无正文抛 LyrError。"""
    html = _curl(f"{SITE}/lyrics/{lid}")
    m_title = re.search(r"<h2>([^<]+)</h2>", html)
    m_artist = re.search(r'<a id="artist"[^>]*>([^<]+)</a>', html)
    body = re.search(r'<div class="lyrics_main">(.*?)(<a href="/interpretation|</div>\s*</div>)',
                     html, re.S)
    if not body:
        raise LyrError("详情页没有歌词容器")
    lines = []
    for m in re.finditer(r"<div[^>]*>([^<>]*)</div>", body.group(1)):
        text = m.group(1).strip()
        if text and not CREDIT_RE.search(text):   # credits 在详情页偶见，不进正文
            lines.append(text)
    if not lines:
        raise LyrError("详情页歌词为空")
    return {
        "title": m_title.group(1).strip() if m_title else "",
        "artist": m_artist.group(1).strip() if m_artist else "",
        "lines": lines,
    }


def _query_variants(title: str) -> list:
    """查询变体：完整歌名优先；长英文多词名补一个「前两个词」的短版。"""
    qs = [title]
    words = re.findall(r"[A-Za-z0-9']+", title)
    if len(words) >= 3 and len(title) > 20:
        qs.append(" ".join(words[:2]))
    return qs


def find_best(title: str, artist: str) -> dict | None:
    """两个方向的候选关键词各搜一次，打分挑最佳；找不到返回 None。"""
    tried = set()
    best, best_score = None, 0
    for t, a in ((title, artist), (artist, title)):   # 「歌手 - 歌名」顺序未知，双向试
        for q in _query_variants(t):                  # 长名先试完整，再试短版
            if not q or _norm(q) in tried:
                continue
            tried.add(_norm(q))
            for cand in search_site(q):
                label = cand["label"]
                nt, na = _norm(t), _norm(a or "")
                score = 0
                # 「歌名-歌手」两侧都可能再含 -，两种切法都试，取高分
                for cl, ca in (label.rpartition("-")[0::2], label.partition("-")[0::2]):
                    ncl, nca = _norm(cl), _norm(ca)
                    s2 = 0
                    if ncl == nt:
                        s2 = 80
                    elif ncl and (ncl in nt or nt in ncl):
                        s2 = 40
                    if s2 and na and nca and (nca == na or nca in na or na in nca):
                        s2 += 20                      # 歌手也对上才加置信
                    score = max(score, s2)
                if score > best_score:
                    best, best_score = cand, score
            if best_score >= 100:                     # 歌名+歌手全对上，不必再搜
                return best
    return best if best_score >= 40 else None


# ==================== LRC 解析（供修复流程复用） ====================

def lrc_lines(raw: str) -> list:
    """把 .lrc 原文拆成纯文本行列表（兼容 JSONL 头、时间戳行、普通行）。"""
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("{"):
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict) and isinstance(obj.get("c"), list):
                text = "".join(c.get("tx", "") for c in obj["c"]).strip()
                if text:
                    out.append(text)
            continue
        text = TS_RE.sub("", line).strip()
        if text:
            out.append(text)
    return out


def lrc_body_count(raw: str) -> int:
    """正文行数（剔除制作信息行），<=2 视为只有 credits。"""
    return sum(1 for t in lrc_lines(raw) if not CREDIT_RE.search(t))


def parse_seconds(ts: str) -> float | None:
    """'[01:23.45]' / '1:23.45' / '01:23:45' -> 秒（float）；解析失败返回 None。"""
    m = TS_RE.match(ts) or re.match(r"^\[?(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]?$", ts.strip())
    if not m:
        return None
    minute, sec = int(m.group(1)), int(m.group(2))
    frac = m.group(3) or "0"
    return minute * 60 + sec + int(frac.ljust(3, "0")[:3]) / 1000


def fmt_ts(seconds: float) -> str:
    """秒 -> '[mm:ss.xx]'（LRC 标准两位厘秒）。"""
    seconds = max(0.0, float(seconds))
    minute, rest = divmod(seconds, 60)
    return "[%02d:%05.2f]" % (int(minute), rest)


def parse_lines(raw: str) -> list:
    """LRC 原文 -> [{'t': 秒, 'text': 文本}]（只保留有时间戳且非制作信息的行）。"""
    out = []
    for line in raw.splitlines():
        s = line.strip()
        if not s:
            continue
        m = TS_RE.match(s)
        if not m:
            continue
        text = TS_RE.sub("", s).strip()
        if not text or CREDIT_RE.search(text):
            continue
        sec = parse_seconds(m.group(0))
        if sec is None:
            continue
        out.append({"t": round(sec, 2), "text": text})
    return out


def build_lrc(meta: list, lines: list) -> str:
    """元数据行（原样保留）+ 时间戳正文 -> LRC 文本。"""
    body = []
    for item in lines:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        body.append(fmt_ts(float(item.get("t") or 0)) + text)
    parts = list(meta or [])
    if parts and body:
        parts.append("")
    return "\n".join(parts + body) + "\n"


def split_lrc(raw: str) -> tuple:
    """拆成 (元数据行列表, 时间戳行列表)。

    元数据行 = JSONL credits / [ti:] 等标签 / 无时间戳的自由文本（作词作曲等），
    原样保留；时间戳行 = (时间戳原文, 文本)。ASR 识别出的 credits 时间戳行不算元数据。
    """
    meta, ts_lines = [], []
    for line in raw.splitlines():
        line = line.rstrip()
        s = line.strip()
        if not s:
            continue
        if s.startswith("{"):                                   # JSONL credits 原样保留
            meta.append(line)
            continue
        m = TAG_RE.match(s)
        if m:
            meta.append(line)
            continue
        m = TS_RE.match(s)
        if m:
            text = TS_RE.sub("", s).strip()
            if text and not CREDIT_RE.search(text):
                ts_lines.append((m.group(0), text))
            # credits / 纯时间戳行不保留：对齐后的正文会取代它们
            continue
        meta.append(line)                                       # 无时间戳的自由文本行
    return meta, ts_lines

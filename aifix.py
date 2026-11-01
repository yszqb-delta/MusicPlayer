# -*- coding: utf-8 -*-
"""AI 歌词校对：把 ASR 时间轴与 lyrics.net.cn 的正确歌词对齐，并纠正转写错别字。

模型走 OpenAI 兼容接口。凭据放在项目根 ai_config.json：
    {"base_url": "https://api.deepseek.com", "api_key": "sk-...", "model": "deepseek-chat"}

可选字段：
    align_model —— 只用于歌词对齐的模型（不填则用 model）。纯机械的对齐任务用
                   非推理模型（deepseek-chat）又快又稳；推理模型（deepseek-flash /
                   deepseek-reasoner）会把 token 全花在思考上，容易吐不出正文。
    max_tokens  —— 单次输出上限，默认 8000；推理模型建议 32000。

该文件不进代码库；缺失时本模块抛 AifixError，由调用方提示。
"""
from __future__ import annotations

import json
import os
import re
import time

import requests

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_config.json")
HTTP_TIMEOUT = 300
MAX_RETRIES = 2
DEFAULT_MAX_TOKENS = 8000
BIG_MAX_TOKENS = 32000        # 推理模型的思考过程很容易吃满 8k，自动放大重试

TS_LINE_RE = re.compile(r"^\[\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?\]\s*\S")
# 模型偶尔把时间写成 [00:18:60]（两个冒号），归一成标准的 [00:18.60]
TS_FIX_RE = re.compile(r"^\[(\d{1,3}):(\d{1,2}):(\d{1,3})\]")


class AifixError(Exception):
    pass


def _load_config() -> dict:
    if not os.path.isfile(CONFIG_PATH):
        raise AifixError("缺少 ai_config.json（base_url / api_key / model）")
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError) as e:
        raise AifixError(f"ai_config.json 读取失败：{e}") from e
    for key in ("base_url", "api_key", "model"):
        if not cfg.get(key):
            raise AifixError(f"ai_config.json 缺少 {key}")
    return cfg


def _salvage(reasoning: str) -> str:
    """推理模型把结果写进了思考过程时，从里面捞出最后一段连续的 LRC 行。"""
    lines = [l.strip().strip("`") for l in (reasoning or "").splitlines()]
    block, best = [], []
    for line in lines:
        if TS_LINE_RE.match(line):
            block.append(line)
        else:
            if len(block) > len(best):
                best = block
            block = []
    if len(block) > len(best):
        best = block
    return "\n".join(best) if len(best) >= 3 else ""


def _chat(cfg: dict, system: str, user: str) -> str:
    """调一次对话接口。空正文（推理模型思考超限）会自动放大 max_tokens 重试。"""
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    model = cfg.get("align_model") or cfg["model"]
    try:
        limit = int(cfg.get("max_tokens") or DEFAULT_MAX_TOKENS)
    except (TypeError, ValueError):
        limit = DEFAULT_MAX_TOKENS

    last_err = ""
    for attempt in range(1, MAX_RETRIES + 1):
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": 0.2,
            "max_tokens": limit,
            "stream": False,
        }
        try:
            resp = requests.post(
                url, json=payload, timeout=HTTP_TIMEOUT,
                headers={"Authorization": f"Bearer {cfg['api_key']}"})
        except requests.RequestException as e:
            last_err = f"网络请求失败：{e}"
            time.sleep(2 * attempt)
            continue

        if resp.status_code != 200:
            last_err = f"接口返回 {resp.status_code}：{resp.text[:200]}"
            time.sleep(2 * attempt)
            continue

        try:
            choice = resp.json()["choices"][0]
            msg = choice.get("message") or {}
        except (ValueError, KeyError, IndexError) as e:
            last_err = f"响应解析失败：{e}"
            continue

        content = (msg.get("content") or "").strip()
        if content:
            return content

        # 正文为空：推理模型把 token 全花在 reasoning_content 上了
        reasoning = msg.get("reasoning_content") or ""
        salvaged = _salvage(reasoning)
        if salvaged:
            return salvaged

        last_err = ("模型没有输出正文（finish_reason=%s，思考内容 %d 字）。"
                    "推理模型容易这样，请在 ai_config.json 里加 "
                    "\"align_model\": \"deepseek-chat\"" % (choice.get("finish_reason"),
                                                        len(reasoning)))
        if limit < BIG_MAX_TOKENS:          # 放大上限再试一次
            limit = BIG_MAX_TOKENS
            continue
        break
    raise AifixError(last_err or "AI 请求失败")


_SYSTEM = """你是歌词时间轴校对助手。给你两份材料：
1) ASR 语音识别出的带时间戳歌词：时间可靠，但文字可能有同音字/错别字/多字漏字，也可能混入识别出的制作信息；
2) 正确歌词的纯文本行（无时间戳）。

任务：把正确歌词逐行对齐到 ASR 时间戳，输出一份完整 LRC。规则：
- 每行输出格式为 [分:秒.厘秒]歌词文本，时间戳必须取自 ASR 行（可微调到该句起点），保持时间递增；
- 歌词文字以材料 2 为准（这是正确文本），逐行原样使用，不要增删改写、不要合并或拆行；
- 材料 1 里的制作信息（作词/作曲等）、哼唱、空行全部忽略；
- 材料 2 中在前奏/间奏处找不到对应语音的行，用前后时间戳线性内插；
- 如果材料 1 的内容与材料 2 明显不是同一首歌（例如纯音乐、内容完全对不上），只输出 NOT_MATCH；
- 只输出 LRC 行或 NOT_MATCH，不要任何解释、不要复述推理过程、不要代码块或多余空行。"""


def _normalize(out: str) -> list:
    """把模型输出整理成干净的 LRC 行：去代码块/列表符号、修时间格式、按时间排序。"""
    rows, seen = [], set()
    for raw in out.splitlines():
        line = raw.strip().strip("`").strip()
        line = re.sub(r"^[-*+>]\s+", "", line)          # markdown 列表符号
        line = TS_FIX_RE.sub(lambda m: "[%s:%s.%s]" % (m.group(1), m.group(2),
                                                     m.group(3).ljust(2, "0")[:2]), line)
        if not TS_LINE_RE.match(line):
            continue
        ts = line[:line.index("]") + 1]
        if ts in seen:                                   # 同一时间戳只留第一条
            continue
        seen.add(ts)
        rows.append(line)
    rows.sort(key=lambda l: l[1:l.index("]")])           # 递增（同宽度可字典序比较）
    return rows


def align(asr_lrc: str, site_lines: list, title: str = "", artist: str = "") -> list:
    """(ASR 的 LRC 文本, 网站歌词行) -> 对齐后的 LRC 正文行列表。

    内容对不上时抛 AifixError("NOT_MATCH")，由调用方按"疑似纯音乐"处理。
    """
    if not asr_lrc.strip() or not site_lines:
        raise AifixError("对齐输入为空")
    site_text = "\n".join(site_lines)
    user = f"歌曲：{artist} - {title}\n\n【材料1：ASR 带时间戳转写】\n{asr_lrc.strip()}\n\n" \
           f"【材料2：正确歌词】\n{site_text}"
    out = _chat(_load_config(), _SYSTEM, user)
    if "NOT_MATCH" in out and TS_LINE_RE.search(out) is None:
        raise AifixError("NOT_MATCH")
    good = _normalize(out)
    if not good or len(good) < max(3, len(site_lines) // 2):
        raise AifixError(f"AI 输出不完整（{len(good)} 行有效 / 网站共 {len(site_lines)} 行）")
    return good

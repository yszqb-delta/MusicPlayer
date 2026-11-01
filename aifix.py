# -*- coding: utf-8 -*-
"""AI 歌词校对：把 ASR 时间轴与 lyrics.net.cn 的正确歌词对齐，并纠正转写错别字。

模型走 OpenAI 兼容接口（DeepSeek）。凭据放在项目根 ai_config.json：
    {"base_url": "https://api.deepseek.com", "api_key": "sk-...", "model": "deepseek-flash"}
该文件不进代码库；缺失时本模块抛 AifixError，由调用方提示。
"""
from __future__ import annotations

import json
import os
import re

import requests

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_config.json")
HTTP_TIMEOUT = 180
MAX_RETRIES = 2

TS_LINE_RE = re.compile(r"^\[\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?\]\s*\S")


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


def _chat(cfg: dict, system: str, user: str) -> str:
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    payload = {
        "model": cfg["model"],
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0.2,
        "max_tokens": 8000,
        "stream": False,
    }
    last_err = ""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(
                url, json=payload, timeout=HTTP_TIMEOUT,
                headers={"Authorization": f"Bearer {cfg['api_key']}"})
        except requests.RequestException as e:
            last_err = f"网络请求失败：{e}"
        else:
            if resp.status_code == 200:
                try:
                    return resp.json()["choices"][0]["message"]["content"]
                except (ValueError, KeyError, IndexError) as e:
                    last_err = f"响应解析失败：{e}"
            else:
                last_err = f"接口返回 {resp.status_code}：{resp.text[:200]}"
        if attempt < MAX_RETRIES:
            import time
            time.sleep(2 * attempt)
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
- 只输出 LRC 行或 NOT_MATCH，不要任何解释、代码块或多余空行。"""


def align(asr_lrc: str, site_lines: list, title: str = "", artist: str = "") -> list:
    """(ASR 的 LRC 文本, 网站歌词行) -> 对齐后的 LRC 正文行列表。

    内容对不上时抛 AifixError("NOT_MATCH")，由调用方按"疑似纯音乐"处理。
    """
    if not asr_lrc.strip() or not site_lines:
        raise AifixError("对齐输入为空")
    site_text = "\n".join(site_lines)
    user = f"歌曲：{artist} - {title}\n\n【材料1：ASR 带时间戳转写】\n{asr_lrc.strip()}\n\n" \
           f"【材料2：正确歌词】\n{site_text}"
    out = _chat(_load_config(), _SYSTEM, user).strip()
    if "NOT_MATCH" in out and TS_LINE_RE.search(out) is None:
        raise AifixError("NOT_MATCH")
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    good = [l for l in lines if TS_LINE_RE.match(l)]
    if not good or len(good) < max(3, len(site_lines) // 2):
        raise AifixError(f"AI 输出无法解析（{len(good)} 行有效 / 网站共 {len(site_lines)} 行）")
    return good

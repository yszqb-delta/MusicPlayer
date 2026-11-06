"""音乐播放器后端入口。

路由分组：页面 / 音乐库 / 上传 / 优盘 / 设置
配置常量集中在 Config，部署时只需改这里。
"""
import hashlib
import json
import os
import re
import subprocess
import threading
import time
from urllib.parse import quote

from flask import Flask, abort, jsonify, render_template, request, send_file

import aifix
import asr
import covers
import lyr
from musiclib import AUDIO_EXTS, MusicLibrary, sanitize_filename
from storage import JsonStore
from usbops import MUSIC_MOUNT, eject, list_partitions, mount


class Config:
    PORT = int(os.environ.get("PORT", 5003))
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    META_PATH = os.path.join(BASE_DIR, "music_meta.json")
    SETTINGS_PATH = os.path.join(BASE_DIR, "settings.json")
    COVERS_DIR = os.path.join(BASE_DIR, "static", "covers")
    BG_DIR = os.path.join(BASE_DIR, "static", "bg")
    LRC_BAK_DIR = os.path.join(BASE_DIR, "lrc_backup")  # 补词前原 .lrc 备份
    DEFAULT_BG = "/static/img/starfield.jpg"  # 播放页默认背景（可自行更换）
    DEFAULT_HOME_BG = "/static/img/spacestation.jpg"  # 主页默认背景
    IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024 * 1024  # 单次上传上限 512MB

lib = MusicLibrary(Config.META_PATH)
settings = JsonStore(Config.SETTINGS_PATH, default={"bg": None, "home_bg": None})


def music_dir_ready() -> bool:
    # 音乐目录存在即可（挂载点判断对桌面系统不友好）
    return os.path.isdir(MUSIC_MOUNT)


def media_url(rel: str) -> str:
    """把优盘内相对路径转成 /media/ URL（逐段转义）。"""
    return "/media/" + "/".join(quote(p) for p in rel.split("/"))


def ensure_covers(songs: list) -> None:
    """自动封面：同名图片优先；否则提取内嵌封面落盘（<歌名>.jpg/png），并写进 meta。

    - 用户在「封面」按钮里手动设置/清除过（cover_manual）的歌不自动处理
    - 提取过但确实没有内嵌封面的（no_cover）不重复尝试，保证扫描仍然轻快
    """
    for song in songs:
        sid = song["path"]
        meta_song = lib.get(sid) or {}
        if meta_song.get("cover_manual") or meta_song.get("cover"):
            continue
        full = os.path.join(MUSIC_MOUNT, sid)
        if not os.path.isfile(full):
            continue
        side = covers.find_sidecar(full)
        if side is None:
            if meta_song.get("no_cover"):
                continue
            art = covers.extract_embedded(full)
            if art is None:
                lib.update(sid, no_cover=True)
                continue
            try:
                side = covers.save_sidecar(full, art[0], art[1])
            except OSError:
                continue
        rel = os.path.relpath(side, MUSIC_MOUNT).replace(os.sep, "/")
        lib.update(sid, cover=media_url(rel))


def safe_path(rel: str) -> str:
    """防止路径穿越：解析后必须仍在优盘根目录内。"""
    full = os.path.realpath(os.path.join(MUSIC_MOUNT, rel))
    root = os.path.realpath(MUSIC_MOUNT)
    if full != root and not full.startswith(root + os.sep):
        abort(403)
    return full


def save_image(file_storage, save_dir: str, prefix: str) -> str:
    """保存上传的图片，返回可访问的静态 URL；失败抛 ValueError。"""
    filename = sanitize_filename(file_storage.filename or "")
    ext = os.path.splitext(filename)[1].lower()
    if ext not in Config.IMAGE_EXTS:
        raise ValueError("不支持的图片格式")
    digest = hashlib.md5(file_storage.read()).hexdigest()[:12]
    file_storage.stream.seek(0)
    name = f"{prefix}_{digest}{ext}"
    os.makedirs(save_dir, exist_ok=True)
    file_storage.save(os.path.join(save_dir, name))
    return "/" + os.path.relpath(os.path.join(save_dir, name), Config.BASE_DIR).replace(os.sep, "/")


# ==================== 页面 ====================

@app.get("/")
def index():
    return render_template("index.html")


@app.get("/upload")
def upload_page():
    return render_template("upload.html")


# ==================== 音乐库 ====================

@app.get("/api/songs")
def api_songs():
    if not music_dir_ready():
        return jsonify({"mounted": False, "songs": []})
    songs = lib.scan(MUSIC_MOUNT)
    ensure_covers(songs)          # 自动提取/关联内嵌与同名封面
    return jsonify({"mounted": True, "songs": lib.songs()})


@app.patch("/api/songs/<path:sid>")
def api_update_song(sid):
    if lib.get(sid) is None:
        abort(404)
    data = request.get_json(silent=True) or {}
    fields = {}
    if "title" in data and str(data["title"]).strip():
        fields["title"] = str(data["title"]).strip()
    if "rating" in data:
        rating = data["rating"]
        if rating is not None and (not isinstance(rating, int) or not 1 <= rating <= 5):
            return jsonify({"error": "评级必须是 1~5 的整数"}), 400
        fields["rating"] = rating
    return jsonify(lib.update(sid, **fields))


@app.delete("/api/songs/<path:sid>")
def api_delete_song(sid):
    if lib.get(sid) is None:
        abort(404)
    removed = []
    if music_dir_ready():
        full = safe_path(sid)
        lrc = os.path.splitext(full)[0] + ".lrc"
        for path in (full, lrc):
            if os.path.isfile(path):
                try:
                    os.remove(path)
                    removed.append(os.path.basename(path))
                except OSError:
                    pass
        # 同名并列的封面图片一起删
        for ext in covers.COVER_EXTS:
            img = os.path.splitext(full)[0] + ext
            if os.path.isfile(img):
                try:
                    os.remove(img)
                    removed.append(os.path.basename(img))
                except OSError:
                    pass
    lib.remove(sid)
    return jsonify({"ok": True, "removed": removed})


@app.get("/api/lyrics/<path:sid>")
def api_lyrics(sid):
    song = lib.get(sid)
    if song is None:
        abort(404)
    # 旧数据迁移：meta 里内嵌的歌词落盘为 .lrc，之后文件是唯一来源
    if song.get("lyrics") and music_dir_ready():
        full = safe_path(sid)
        if os.path.isfile(full):
            lrc_path = os.path.splitext(full)[0] + ".lrc"
            if not os.path.isfile(lrc_path):
                with open(lrc_path, "w", encoding="utf-8") as f:
                    f.write(song["lyrics"])
            lib.update(sid, lyrics=None)
    text = lib.find_lrc(sid, MUSIC_MOUNT) if music_dir_ready() else None
    return jsonify({"lyrics": text, "source": "file" if text else "none"})


@app.post("/api/songs/<path:sid>/lyrics")
def api_link_lyrics(sid):
    if lib.get(sid) is None:
        abort(404)
    if not music_dir_ready():
        return jsonify({"error": "优盘未挂载，无法保存歌词文件"}), 400
    if "file" in request.files:
        text = request.files["file"].stream.read().decode("utf-8", errors="replace")
    else:
        data = request.get_json(silent=True) or {}
        text = str(data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "歌词内容为空"}), 400
    full = safe_path(sid)
    if not os.path.isfile(full):
        return jsonify({"error": "音频文件不存在"}), 404
    lrc_path = os.path.splitext(full)[0] + ".lrc"   # 与音乐并列、同名存放
    with open(lrc_path, "w", encoding="utf-8") as f:
        f.write(text)
    lib.update(sid, lyrics=None)                     # 内嵌歌词清掉，避免双源不一致
    return jsonify({"ok": True, "saved_as": os.path.basename(lrc_path)})


@app.post("/api/songs/<path:sid>/cover")
def api_link_cover(sid):
    if lib.get(sid) is None:
        abort(404)
    if "file" in request.files:
        try:
            url = save_image(request.files["file"], Config.COVERS_DIR, "cover")
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
    else:
        data = request.get_json(silent=True) or {}
        if "url" not in data:
            return jsonify({"error": "请提供图片文件或 URL"}), 400
        url = str(data.get("url") or "").strip() or None  # 空字符串表示清除封面
    # 用户手动设置/清除后，自动封面不再覆盖该歌
    return jsonify(lib.update(sid, cover=url, cover_manual=True))


# ==================== 歌词工坊（找词 / 识别 / AI 对齐 / 手改 一条链路） ====================
# 原「识别」「补词」「手动补词页」三个入口合并成一处，流程：
#   1) 网站有词 → 取时间轴（现有 .lrc 优先，没有就跑必剪 ASR）→ AI 逐行对齐纠错 → 写回
#   2) 网站没词 → 直接识别出时间轴，前端把「时间 + 文字」逐行列出来给用户改
#   3) 只想微调 → 读出现有 LRC 的每一行，改完保存（原有制作信息行原样保留）
# ASR 很贵（一首 1~3 分钟），结果按「路径+大小+修改时间」缓存到 asr_cache/，
# 同一首歌反复打开歌词工坊不会重复识别。
ASR_CACHE_DIR = os.path.join(Config.BASE_DIR, "asr_cache")
_asr_run_lock = threading.Lock()          # 同一时刻只跑一个识别任务


def _lrc_path(rel: str) -> str:
    """音频对应的 .lrc 路径（同名并列）。"""
    return os.path.splitext(os.path.join(MUSIC_MOUNT, rel))[0] + ".lrc"


def _read_lyric(rel: str) -> tuple:
    """读出现有 .lrc -> (元数据行, [{'t','text'}])；文件不存在返回 ([], [])。"""
    path = _lrc_path(rel)
    if not os.path.isfile(path):
        return [], []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read()
    except OSError:
        return [], []
    meta, ts_lines = lyr.split_lrc(raw)
    lines = []
    for ts, text in ts_lines:
        sec = lyr.parse_seconds(ts)
        if sec is not None:
            lines.append({"t": round(sec, 2), "text": text})
    return meta, lines


def _asr_cache_key(rel: str) -> str | None:
    full = os.path.join(MUSIC_MOUNT, rel)
    try:
        st = os.stat(full)
    except OSError:
        return None
    raw = "%s|%d|%d" % (rel, st.st_size, int(st.st_mtime))
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _recognize(rel: str, force: bool = False) -> str:
    """必剪 ASR -> LRC 文本（带磁盘缓存；force=True 忽略缓存重新识别）。"""
    ext = os.path.splitext(rel)[1].lower()
    if ext not in asr.SUPPORTED_EXTS:
        raise asr.AsrError(f"{ext} 格式不支持在线识别（支持 mp3/flac/m4a/wav）")
    key = _asr_cache_key(rel)
    cache = os.path.join(ASR_CACHE_DIR, key + ".lrc") if key else None
    if cache and not force and os.path.isfile(cache):
        try:
            with open(cache, "r", encoding="utf-8", errors="replace") as f:
                cached = f.read()
            if cached.strip():
                return cached
        except OSError:
            pass
    full = os.path.join(MUSIC_MOUNT, rel)
    if not os.path.isfile(full):
        raise asr.AsrError("音频文件不存在")
    with _asr_run_lock:                    # 串行化，避免并发把优盘和接口打爆
        with open(full, "rb") as f:
            text = asr.recognize(f.read())
    if not text.strip():
        raise asr.AsrError("没有识别到任何内容")
    if cache:
        try:
            os.makedirs(ASR_CACHE_DIR, exist_ok=True)
            with open(cache, "w", encoding="utf-8") as f:
                f.write(text)
        except OSError:
            pass                           # 缓存写失败不影响主流程
    return text


def _timeline(rel: str, lines: list, force_asr: bool = False) -> tuple:
    """取时间轴：现有 .lrc 优先，没有（或强制）才跑 ASR。返回 (lines, 错误信息)。"""
    if lines and not force_asr:
        return lines, None
    try:
        return lyr.parse_lines(_recognize(rel, force=force_asr)) or lines, None
    except asr.AsrError as e:
        return lines, str(e)


def _site_lyric(rel: str) -> tuple:
    """文件名拆「歌手 - 歌名」双向搜网站，返回 (候选信息, 歌词详情) 或 (None, None)。"""
    stem = os.path.splitext(os.path.basename(rel))[0]
    parts = [p.strip() for p in stem.split(" - ") if p.strip()]
    pairs = [(parts[-1], parts[0]), (parts[0], parts[-1])] if len(parts) >= 2 else [(stem, "")]
    for title, artist in pairs:
        if not title:
            continue
        try:
            cand = lyr.find_best(title, artist)
            if not cand:
                continue
            info = lyr.fetch(cand["id"])
        except lyr.LyrError:
            continue
        if info.get("lines"):
            return cand, info
    return None, None


def _save_lyric(rel: str, meta: list, lines: list) -> str | None:
    """写回 .lrc（写前备份），返回备份文件名。"""
    path = _lrc_path(rel)
    text = lyr.build_lrc(meta, lines)
    bak = _backup_lrc(path, rel) if os.path.isfile(path) else None
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    if lib.get(rel) is not None:      # 清掉可能存在的旧内嵌歌词，避免双源不一致
        lib.update(rel, lyrics=None)
    return bak


def _lyric_auto(rel: str, force_asr: bool = False, allow_asr: bool = True) -> dict:
    """自动链路：网站找词 + 时间轴 + AI 对齐，成功即写回。

    返回 {status, note, meta, lines, source?, saved?, backup?}
    status:
      aligned     已对齐并保存
      no_site     网站没词（已附识别时间轴，供手动修改）
      ai_error    AI 失败（已附时间轴，供手动修改）
      no_timeline 既没词也没识别出时间轴
    """
    meta, lines = _read_lyric(rel)
    cand, info = _site_lyric(rel)
    if cand is None:
        if allow_asr:
            lines, err = _timeline(rel, lines, force_asr)
        else:
            err = None
        note = "网站上没找到这首歌的歌词"
        if err:
            note += f"（识别时间轴也失败：{err}）"
        elif lines:
            note += "，下面是识别出的时间轴，请对照音频改文字后保存"
        else:
            note += "，也没有可用时间轴；可粘贴带时间戳的歌词或上传 .lrc"
        return {"status": "no_site", "meta": meta, "lines": lines, "note": note}

    if allow_asr:
        lines, err = _timeline(rel, lines, force_asr)
    else:
        err = None
    if not lines:
        note = "网站有词，但这首歌没有可用时间轴"
        if err:
            note += f"（识别失败：{err}）"
        else:
            note += "（格式不支持在线识别）；可上传 .lrc 或粘贴带时间戳的歌词"
        return {"status": "no_timeline", "meta": meta, "lines": [], "note": note}

    asr_lrc = "\n".join(lyr.fmt_ts(l["t"]) + l["text"] for l in lines)
    try:
        body = aifix.align(asr_lrc, info["lines"], info.get("title") or "",
                           info.get("artist") or "")
    except aifix.AifixError as e:
        if str(e) == "NOT_MATCH":
            return {"status": "no_site", "meta": meta, "lines": lines,
                    "note": "网站有同名歌词，但内容对不上（疑似纯音乐或版本不同），"
                            "下面是识别出的时间轴，可直接改文字后保存"}
        return {"status": "ai_error", "meta": meta, "lines": lines,
                "note": f"AI 对齐失败：{e}。下面是现有时间轴，可直接改文字后保存"}

    new_lines = []
    for line in body:
        close = line.index("]")
        sec = lyr.parse_seconds(line[:close + 1])
        text = line[close + 1:].strip()
        if sec is not None and text:
            new_lines.append({"t": round(sec, 2), "text": text})
    try:
        bak = _save_lyric(rel, meta, new_lines)
    except OSError as e:
        return {"status": "ai_error", "meta": meta, "lines": new_lines,
                "note": f"对齐成功但写入失败：{e}"}
    out = {"status": "aligned", "meta": meta, "lines": new_lines, "saved": True,
           "source": cand.get("label"),
           "note": "来源「%s」，对齐 %d 行，已保存" % (cand.get("label"), len(new_lines))}
    if bak:
        out["backup"] = bak
    return out


@app.get("/api/songs/<path:sid>/lyric")
def api_lyric_get(sid):
    """歌词工坊打开时读取：元数据 + 逐行（时间/文字）。"""
    if lib.get(sid) is None:
        abort(404)
    if not music_dir_ready():
        return jsonify({"error": "优盘未挂载"}), 400
    meta, lines = _read_lyric(sid)
    return jsonify({"meta": meta, "lines": lines,
                    "has_body": len(lines) > 2,
                    "lrc": os.path.basename(_lrc_path(sid)) if os.path.isfile(_lrc_path(sid)) else None})


@app.post("/api/songs/<path:sid>/lyric")
def api_lyric_save(sid):
    """保存歌词工坊的编辑结果；未传 meta 时自动沿用文件里原有的制作信息行。"""
    if lib.get(sid) is None:
        abort(404)
    if not music_dir_ready():
        return jsonify({"error": "优盘未挂载"}), 400
    data = request.get_json(silent=True) or {}
    raw_lines = data.get("lines")
    if not isinstance(raw_lines, list):
        return jsonify({"error": "缺少歌词行"}), 400
    clean = []
    for idx, item in enumerate(raw_lines, 1):
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        try:
            sec = float(item.get("t") or 0)
        except (TypeError, ValueError):
            return jsonify({"error": f"第 {idx} 行的时间不是数字"}), 400
        clean.append({"t": max(0.0, round(sec, 2)), "text": text})
    if not clean:
        return jsonify({"error": "歌词内容为空"}), 400
    clean.sort(key=lambda x: x["t"])

    old_meta, _old_lines = _read_lyric(sid)
    meta = data.get("meta")
    if isinstance(meta, list):
        meta = [str(m).strip() for m in meta if str(m).strip()]
    else:
        meta = old_meta
    meta = meta or old_meta          # 前端清空了元数据也别真丢制作信息

    try:
        bak = _save_lyric(sid, meta, clean)
    except OSError as e:
        return jsonify({"error": f"写入失败：{e}"}), 500
    out = {"ok": True, "lines": len(clean), "saved_as": os.path.basename(_lrc_path(sid))}
    if bak:
        out["backup"] = bak
    return jsonify(out)


@app.post("/api/songs/<path:sid>/lyric/auto")
def api_lyric_auto(sid):
    """自动补词：网站找词 + 时间轴 + AI 对齐。可能耗时 1~3 分钟（首次要跑 ASR）。"""
    if lib.get(sid) is None:
        abort(404)
    if not music_dir_ready():
        return jsonify({"error": "优盘未挂载"}), 400
    if _fix_state["running"]:
        return jsonify({"error": "已有批量补词任务在进行中，请稍后再试"}), 409
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(_lyric_auto(sid, force_asr=bool(body.get("force_asr"))))
    except asr.AsrError as e:
        return jsonify({"error": f"语音识别失败：{e}"}), 502
    except OSError as e:
        return jsonify({"error": f"文件读写失败：{e}"}), 500


@app.post("/api/songs/<path:sid>/lyric/asr")
def api_lyric_asr(sid):
    """强制重新识别时间轴（忽略缓存），只返回结果不写文件。"""
    if lib.get(sid) is None:
        abort(404)
    if not music_dir_ready():
        return jsonify({"error": "优盘未挂载"}), 400
    try:
        lines = lyr.parse_lines(_recognize(sid, force=True))
    except asr.AsrError as e:
        return jsonify({"error": f"语音识别失败：{e}"}), 502
    except OSError as e:
        return jsonify({"error": f"读取音频失败：{e}"}), 500
    return jsonify({"lines": lines})


# ==================== 歌词配对与孤儿清理 ====================
# 优盘里的 .lrc 与音频同名并列时自动生效。配不上的常见原因：
#   1. 「歌手 - 歌名」与「歌名 - 歌手」命名顺序相反
#   2. Windows 重复下载产生的 "(1)" 后缀（副本文件）
#   3. 同一首歌艺术家署名不同（如 Remix 多署名、逗号/分号差异）
# 先给 1~3 一次改名字配对的机会；内容相同的重复副本和真孤儿才进入清理。

_DUP_SUFFIX_RE = re.compile(r"[\(（]\s*\d{1,3}\s*[\)）]\s*$")


def _norm_stem(s: str) -> str:
    """归一化文件名主干：只保留中英文与数字，忽略大小写和标点空格。"""
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", s).lower()


def _lrc_payload(path: str) -> str:
    """提取歌词的纯文本内容（去掉时间戳），用于比较两份歌词是否相同。兼容 JSON/JSONL 格式。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read()
    except OSError:
        return ""
    texts = []
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict) and isinstance(obj.get("c"), list):
                texts.append("".join(c.get("tx", "") for c in obj["c"]))
                continue
            if isinstance(obj, list):
                texts.extend("".join(c.get("tx", "") for c in (l.get("c") or []) if isinstance(l, dict))
                             for l in obj if isinstance(l, dict))
                continue
        texts.append(line)
    return re.sub(r"\s+", "", "".join(texts))


def _scan_lrc_pairs() -> tuple:
    """扫描优盘，返回 (可配对 {lrc相对路径: 音频sid}, 真孤儿列表, 重复副本列表)。"""
    from musiclib import AUDIO_EXTS
    audios = []   # [{rel, raw, k_full, segs}]
    for dirpath, _dirs, files in os.walk(MUSIC_MOUNT):
        for name in files:
            if os.path.splitext(name)[1].lower() not in AUDIO_EXTS:
                continue
            rel = os.path.relpath(os.path.join(dirpath, name), MUSIC_MOUNT).replace(os.sep, "/")
            raw_stem = os.path.splitext(name)[0]
            stem = _DUP_SUFFIX_RE.sub("", raw_stem).strip()
            segs = [_norm_stem(s) for s in (p.strip() for p in stem.split(" - ")) if _norm_stem(s)]
            audios.append({
                "rel": rel,
                "raw": raw_stem.lower(),
                "k_full": _norm_stem(stem),
                "segs": segs,
            })

    pairs, orphans, duplicates = {}, [], []
    for dirpath, _dirs, files in os.walk(MUSIC_MOUNT):
        for name in files:
            if not name.lower().endswith(".lrc"):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, MUSIC_MOUNT).replace(os.sep, "/")
            raw_stem = os.path.splitext(name)[0]
            stem = _DUP_SUFFIX_RE.sub("", raw_stem).strip()
            k_full = _norm_stem(stem)
            parts = [p.strip() for p in stem.split(" - ") if p.strip()]
            k_rev = _norm_stem(" - ".join(reversed(parts))) if len(parts) > 1 else k_full

            # 第一优先：完整主干相等（正序或反序，忽略重复后缀）
            hits = [a for a in audios if a["k_full"] in (k_full, k_rev)]
            if len(hits) > 1:
                # 多首匹配时，优先与文件名大小写不敏感完全相同的那个
                exact = [a for a in hits if a["raw"] == raw_stem.lower()]
                if exact:
                    hits = exact[:1]
            # 第二优先：歌名相等 + 艺术家互相包含（双方「歌手-歌名」顺序未知，双向尝试），候选需唯一
            if not hits and len(parts) >= 2:
                an, tn = _norm_stem(parts[0]), _norm_stem(parts[-1])
                if tn:
                    cand = []
                    for a in audios:
                        segs = a["segs"]
                        if len(segs) < 2:
                            continue
                        for a_seg, t_seg in ((segs[0], segs[1]), (segs[1], segs[0])):
                            if tn == t_seg and an and (an in a_seg or a_seg in an):
                                cand.append(a)
                                break
                    if len(cand) > 1:
                        orphans.append({"path": rel, "size": _safe_size(full),
                                        "reason": "匹配到多首同名歌曲，需人工确认"})
                        continue
                    hits = cand

            if len(hits) > 1:
                orphans.append({"path": rel, "size": _safe_size(full),
                                "reason": "匹配到多首同名歌曲，需人工确认"})
                continue
            if not hits:
                orphans.append({"path": rel, "size": _safe_size(full),
                                "reason": "优盘上没有对应的音频"})
                continue
            target = hits[0]["rel"]
            target_lrc = os.path.splitext(target)[0] + ".lrc"
            if target_lrc == rel:
                continue                     # 本来就配对正确
            if os.path.exists(os.path.join(MUSIC_MOUNT, target_lrc)):
                # 目标已有歌词：这是重复副本，比较内容后决定能否清理
                duplicates.append({
                    "path": rel, "target": target_lrc,
                    "same_content": _lrc_payload(full) == _lrc_payload(os.path.join(MUSIC_MOUNT, target_lrc)),
                })
            else:
                pairs[rel] = target
    return pairs, orphans, duplicates


def _safe_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


@app.get("/api/lrc/orphans")
def api_lrc_orphans():
    if not music_dir_ready():
        return jsonify({"error": "优盘未挂载"}), 400
    pairs, orphans, duplicates = _scan_lrc_pairs()
    adoptable = [{"lrc": l, "audio": a} for l, a in pairs.items()]
    return jsonify({"orphans": orphans, "adoptable": adoptable, "duplicates": duplicates})


@app.post("/api/lrc/adopt")
def api_lrc_adopt():
    """把命名不一致的 lrc 改名成与音频同名，使其自动配对生效。"""
    if not music_dir_ready():
        return jsonify({"error": "优盘未挂载"}), 400
    pairs, _orphans, _dups = _scan_lrc_pairs()
    adopted, skipped = [], []
    for lrc_rel, audio_sid in pairs.items():
        lrc_full = os.path.join(MUSIC_MOUNT, lrc_rel)
        target = os.path.splitext(os.path.join(MUSIC_MOUNT, audio_sid))[0] + ".lrc"
        if os.path.exists(target):               # 目标已存在就不覆盖，避免误伤
            skipped.append(lrc_rel)
            continue
        try:
            os.rename(lrc_full, target)
            adopted.append(os.path.basename(target))
        except OSError:
            skipped.append(lrc_rel)
    return jsonify({"ok": True, "adopted": adopted, "skipped": skipped, "count": len(adopted)})


@app.post("/api/lrc/cleanup")
def api_lrc_cleanup():
    if not music_dir_ready():
        return jsonify({"error": "优盘未挂载"}), 400
    _pairs, orphans, duplicates = _scan_lrc_pairs()
    removed = []
    # 真孤儿 + 内容与正版完全相同的重复副本才会被删；内容不同的副本保留
    victims = [(o["path"],) for o in orphans]
    victims += [(d["path"],) for d in duplicates if d["same_content"]]
    for (rel,) in victims:
        try:
            os.remove(os.path.join(MUSIC_MOUNT, rel))
            removed.append(rel)
        except OSError:
            pass
    kept = [d["path"] for d in duplicates if not d["same_content"]]
    return jsonify({"ok": True, "removed": removed, "count": len(removed), "kept_diff": kept})


# ==================== 歌词补全（lyrics.net.cn 找词 + AI 校对对齐） ====================
# 流程：文件名拆「歌手 - 歌名」→ 网站搜词（双向顺序都试）→ 无词直接跳过（纯音乐自然筛掉）
#       → 现有 .lrc 无时间轴时先跑 ASR → AI 把网站歌词逐行对齐到时间轴并纠错
#       → 原有的作词作曲等元数据行原样保留，与新正文合并写回 .lrc（写前备份到 lrc_backup/）
# 同一时刻只跑一个修复任务（单首/批量共用一把锁），状态存这里供前端轮询。
_fix_state = {
    "running": False, "mode": "", "sid": None, "current": "",
    "total": 0, "done": 0, "msg": "", "results": [],
}
_fix_lock = threading.Lock()


def _backup_lrc(lrc_path: str, rel: str) -> str | None:
    """补词前把原 .lrc 备份到 lrc_backup/，返回备份文件名。"""
    try:
        os.makedirs(Config.LRC_BAK_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        name = rel.replace("/", "_") + "." + stamp + ".bak"
        with open(lrc_path, "rb") as src, open(os.path.join(Config.LRC_BAK_DIR, name), "wb") as dst:
            dst.write(src.read())
        return name
    except OSError:
        return None


def _fix_one(rel: str, allow_asr: bool = True) -> dict:
    """单首补词（批量复用）：走统一流水线，结果映射成批量进度用的 action。"""
    try:
        out = _lyric_auto(rel, allow_asr=allow_asr)
    except Exception as e:                 # 单首失败不拖垮整批
        return {"song": rel, "action": "error", "reason": f"未知错误：{e}"}
    status = out.get("status")
    if status == "aligned":
        res = {"song": rel, "action": "updated", "lines": len(out.get("lines") or []),
               "source": out.get("source")}
        if out.get("backup"):
            res["backup"] = out["backup"]
        return res
    if status == "no_site":
        reason = "网站上没找到这首（可在歌词工坊手动补）"
    elif status == "no_timeline":
        reason = "没有可用时间轴"
    else:
        reason = out.get("note") or "AI 校对失败"
    return {"song": rel, "action": "skipped", "reason": reason}


def _fix_worker(rels: list, mode: str) -> None:
    _fix_state.update(running=True, mode=mode, total=len(rels), done=0,
                      msg="", results=[], current="")
    try:
        for rel in rels:
            if not _fix_state["running"]:      # 批量允许中途叫停
                break
            _fix_state.update(current=rel)
            try:
                # 批量模式不跑 ASR：没词的歌先跳过（ASR 很贵，留给用户在歌词工坊单首处理）
                result = _fix_one(rel, allow_asr=(mode != "batch"))
            except Exception as e:             # 后台线程兜底，单首失败不拖垮整批
                result = {"song": rel, "action": "error", "reason": f"未知错误：{e}"}
            _fix_state["results"].append(result)
            _fix_state["done"] += 1
            if mode == "batch":
                time.sleep(2)                  # 对歌词网站限频，礼貌抓取
        _fix_state.update(running=False, current="", msg="完成")
    finally:
        _fix_state["running"] = False


@app.post("/api/songs/<path:sid>/lyrfix")
def api_lyrfix(sid):
    """单首补词：网站找词 + AI 校对，后台执行。"""
    if lib.get(sid) is None:
        abort(404)
    if not music_dir_ready():
        return jsonify({"error": "优盘未挂载"}), 400
    with _fix_lock:
        if _fix_state["running"]:
            return jsonify({"error": "已有补词任务在进行中"}), 409
        if not os.path.isfile(safe_path(sid)):
            return jsonify({"error": "音频文件不存在"}), 404
        _fix_state.update(sid=sid, running=True)
        threading.Thread(target=_fix_worker, args=([sid], "single"), daemon=True).start()
    return jsonify({"started": True})


@app.post("/api/lyrfix/batch")
def api_lyrfix_batch():
    """批量补词：扫描全库中「无 .lrc 或只有制作信息」的歌，逐首处理。"""
    if not music_dir_ready():
        return jsonify({"error": "优盘未挂载"}), 400
    with _fix_lock:
        if _fix_state["running"]:
            return jsonify({"error": "已有补词任务在进行中"}), 409
        needy = []
        for s in lib.scan(MUSIC_MOUNT):
            rel = s["path"]
            lrc_path = os.path.splitext(os.path.join(MUSIC_MOUNT, rel))[0] + ".lrc"
            if not os.path.isfile(lrc_path):
                needy.append(rel)
                continue
            try:
                with open(lrc_path, "r", encoding="utf-8", errors="replace") as f:
                    raw = f.read()
            except OSError:
                continue
            if lyr.lrc_body_count(raw) <= 2:      # 正文 <=2 行：只有作词作曲等
                needy.append(rel)
        if not needy:
            return jsonify({"ok": True, "total": 0, "msg": "没有需要补词的歌"})
        _fix_state.update(sid=None, running=True)
        threading.Thread(target=_fix_worker, args=(needy, "batch"), daemon=True).start()
    return jsonify({"started": True, "total": len(needy)})


@app.post("/api/lyrfix/stop")
def api_lyrfix_stop():
    """叫停批量任务（当前这首会跑完，之后的不再开始）。"""
    _fix_state["running"] = False
    return jsonify({"ok": True})


@app.get("/api/lyrfix/status")
def api_lyrfix_status():
    return jsonify(_fix_state)


@app.get("/api/lyrfix/needy")
def api_lyrfix_needy():
    """手动补词页：列出「没有 .lrc 或正文 ≤2 行」的歌。"""
    if not music_dir_ready():
        return jsonify({"error": "优盘未挂载"}), 400
    needy = []
    for s in lib.scan(MUSIC_MOUNT):
        rel = s["path"]
        lrc_path = os.path.splitext(os.path.join(MUSIC_MOUNT, rel))[0] + ".lrc"
        has_body = False
        if os.path.isfile(lrc_path):
            try:
                with open(lrc_path, "r", encoding="utf-8", errors="replace") as f:
                    has_body = lyr.lrc_body_count(f.read()) > 2
            except OSError:
                pass
        if not has_body:
            needy.append({"path": rel, "title": s.get("title") or s.get("filename") or rel,
                          "has_lrc": os.path.isfile(lrc_path)})
    return jsonify({"songs": needy})


# ==================== 媒体流 ====================

@app.get("/media/<path:relpath>")
def media(relpath):
    full = safe_path(relpath)
    if not os.path.isfile(full):
        abort(404)
    # conditional=True 让 werkzeug 自动处理 Range 请求（拖动进度条必需）
    return send_file(full, conditional=True)


# ==================== 上传 ====================

@app.post("/api/upload")
def api_upload():
    if not music_dir_ready():
        return jsonify({"error": "优盘未挂载，请先在主页挂载优盘"}), 400
    from musiclib import AUDIO_EXTS  # 局部引用避免误导
    saved, failed = [], []
    for fs in request.files.getlist("files"):
        name = sanitize_filename(fs.filename or "")
        ext = os.path.splitext(name)[1].lower()
        if ext not in AUDIO_EXTS and ext != ".lrc":   # 歌词文件与音乐一起传
            failed.append({"name": name, "reason": "不支持的文件格式"})
            continue
        target = os.path.join(MUSIC_MOUNT, name)
        base, ext = os.path.splitext(target)
        n = 1
        while os.path.exists(target):  # 重名自动加序号
            target = f"{base}({n}){ext}"
            n += 1
        fs.save(target)
        saved.append(os.path.basename(target))
    songs = lib.scan(MUSIC_MOUNT)
    ensure_covers(songs)          # 新上传的歌自动提取内嵌封面
    return jsonify({"saved": saved, "failed": failed})


# ==================== 优盘 ====================

@app.get("/api/usb/status")
def api_usb_status():
    return jsonify({"partitions": list_partitions(), "mount_point": MUSIC_MOUNT})


@app.post("/api/usb/mount")
def api_usb_mount():
    dev = str((request.get_json(silent=True) or {}).get("dev") or "")
    return jsonify(mount(dev))


@app.post("/api/usb/eject")
def api_usb_eject():
    return jsonify(eject())


# ==================== 设置（背景图等） ====================

@app.get("/api/settings")
def api_settings():
    data = settings.load()
    return jsonify({
        "bg": data.get("bg") or Config.DEFAULT_BG,
        "home_bg": data.get("home_bg") or Config.DEFAULT_HOME_BG,
    })


@app.post("/api/settings/bg")
def api_set_bg():
    data = request.get_json(silent=True) or {}
    target = "home_bg" if data.get("target") == "home" else "bg"   # 默认改播放页
    default = Config.DEFAULT_HOME_BG if target == "home_bg" else Config.DEFAULT_BG
    if data.get("reset"):
        current = settings.load()
        current[target] = None
        settings.save(current)
        return jsonify({"bg": default})
    if "file" in request.files:
        try:
            url = save_image(request.files["file"], Config.BG_DIR, "bg")
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
    else:
        url = str(data.get("url") or "").strip()
        if not url:
            return jsonify({"error": "请提供图片文件或 URL"}), 400
    current = settings.load()
    current[target] = url
    settings.save(current)
    return jsonify({"bg": url})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=Config.PORT, debug=False)

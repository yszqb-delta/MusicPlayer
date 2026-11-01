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
import netease
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


@app.get("/lyrfix")
def lyrfix_page():
    return render_template("lyrfix.html")


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


# ==================== 歌词自动识别（必剪 ASR） ====================
# 同一时刻只跑一个识别任务，状态存这里供前端轮询
_asr_state = {"sid": None, "status": "idle", "msg": ""}  # idle/running/done/error
_asr_lock = threading.Lock()


def _asr_worker(sid: str, audio_path: str) -> None:
    try:
        with open(audio_path, "rb") as f:
            binary = f.read()
        lrc_text = asr.recognize(binary)
        if not lrc_text.strip():
            raise asr.AsrError("没有识别到任何内容")
        # .lrc 与音乐并列同名存放，是歌词的唯一来源
        with open(os.path.splitext(audio_path)[0] + ".lrc", "w", encoding="utf-8") as f:
            f.write(lrc_text)
        lib.update(sid, lyrics=None)     # 清掉可能存在的旧内嵌歌词
        _asr_state.update(status="done", msg="识别完成")
    except asr.AsrError as e:
        _asr_state.update(status="error", msg=str(e))
    except OSError as e:
        _asr_state.update(status="error", msg=f"读取音频失败：{e}")
    except Exception as e:  # 后台线程兜底，避免静默失败
        _asr_state.update(status="error", msg=f"未知错误：{e}")


@app.post("/api/songs/<path:sid>/autolrc")
def api_autolrc(sid):
    if lib.get(sid) is None:
        abort(404)
    if not music_dir_ready():
        return jsonify({"error": "优盘未挂载"}), 400
    with _asr_lock:
        if _asr_state["status"] == "running":
            return jsonify({"error": "已有识别任务在进行中"}), 409
        ext = os.path.splitext(sid)[1].lower()
        if ext not in asr.SUPPORTED_EXTS:
            return jsonify({"error": f"{ext} 格式不支持在线识别（支持 mp3/flac/m4a/wav），请手动上传歌词"}), 400
        # 已有同名 .lrc 就直接用，不浪费一次识别
        if lib.find_lrc(sid, MUSIC_MOUNT):
            _asr_state.update(sid=sid, status="done", msg="发现同名 .lrc，已直接使用")
            return jsonify({"started": True, "reused": True})
        audio_path = safe_path(sid)
        if not os.path.isfile(audio_path):
            return jsonify({"error": "音频文件不存在"}), 404
        _asr_state.update(sid=sid, status="running", msg="")
        threading.Thread(target=_asr_worker, args=(sid, audio_path), daemon=True).start()
    return jsonify({"started": True})


@app.get("/api/asr/status")
def api_asr_status():
    return jsonify(_asr_state)


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


def _fix_one(rel: str) -> dict:
    """对单首歌执行完整补词流程，返回结果 dict（不抛常规业务异常）。"""
    full = os.path.join(MUSIC_MOUNT, rel)
    stem = os.path.splitext(os.path.basename(rel))[0]
    parts = [p.strip() for p in stem.split(" - ") if p.strip()]
    pairs = [(parts[-1], parts[0]), (parts[0], parts[-1])] if len(parts) >= 2 else [(stem, "")]

    # 1. 网站搜词（先搜再决定要不要 ASR，纯音乐在网站就没词，省一次识别）
    cand = None
    for title, artist in pairs:
        if not title:
            continue
        try:
            cand = lyr.find_best(title, artist)
        except lyr.LyrError:
            cand = None
        if cand:
            break
    if cand is None:
        return {"song": rel, "action": "skipped", "reason": "网站上没找到这首"}
    try:
        info = lyr.fetch(cand["id"])
    except lyr.LyrError as e:
        return {"song": rel, "action": "skipped", "reason": f"取词失败：{e}"}

    # 2. 时间轴：优先用现有 .lrc 里已有的；没有就跑 ASR
    lrc_path = os.path.splitext(full)[0] + ".lrc"
    meta, ts_lines = [], []
    if os.path.isfile(lrc_path):
        try:
            with open(lrc_path, "r", encoding="utf-8", errors="replace") as f:
                meta, ts_lines = lyr.split_lrc(f.read())
        except OSError:
            pass
    if not ts_lines:
        ext = os.path.splitext(rel)[1].lower()
        if ext not in asr.SUPPORTED_EXTS:
            return {"song": rel, "action": "skipped", "reason": f"网站有词，但 {ext} 无法自动识别时间轴"}
        try:
            with open(full, "rb") as f:
                asr_text = asr.recognize(f.read())
            _, ts_lines = lyr.split_lrc(asr_text)
        except asr.AsrError as e:
            return {"song": rel, "action": "skipped", "reason": f"网站有词，但识别时间轴失败：{e}"}
        if not ts_lines:
            return {"song": rel, "action": "skipped", "reason": "网站有词，但没识别出时间轴"}
    asr_lrc = "\n".join(ts + text for ts, text in ts_lines)

    # 3. AI 校对对齐
    try:
        body = aifix.align(asr_lrc, info["lines"], info["title"] or stem, info["artist"])
    except aifix.AifixError as e:
        if str(e) == "NOT_MATCH":
            return {"song": rel, "action": "skipped", "reason": "疑似纯音乐（AI 判定内容不符）"}
        return {"song": rel, "action": "error", "reason": f"AI 校对失败：{e}"}

    # 4. 元数据 + 新正文合并写回（写前备份）
    final = "\n".join(meta + ([""] if meta else []) + body) + "\n"
    bak = _backup_lrc(lrc_path, rel) if os.path.isfile(lrc_path) else None
    try:
        with open(lrc_path, "w", encoding="utf-8") as f:
            f.write(final)
    except OSError as e:
        return {"song": rel, "action": "error", "reason": f"写入失败：{e}"}
    if lib.get(rel) is not None:       # 清掉可能存在的旧内嵌歌词，避免双源不一致
        lib.update(rel, lyrics=None)
    out = {"song": rel, "action": "updated", "lines": len(body), "source": cand["label"]}
    if bak:
        out["backup"] = bak
    return out


def _fix_worker(rels: list, mode: str) -> None:
    _fix_state.update(running=True, mode=mode, total=len(rels), done=0,
                      msg="", results=[], current="")
    try:
        for rel in rels:
            if not _fix_state["running"]:      # 批量允许中途叫停
                break
            _fix_state.update(current=rel)
            try:
                result = _fix_one(rel)
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


# ==================== 网易云（每日推荐 / 歌单 / 下载整理） ====================
# 播放走 /api/netease/stream/<id>（302 到实时获取的 CDN 地址）；下载由后台线程
# 整理三件套：<歌手 - 歌名>.mp3 + 同名 .lrc + 同名封面图，落盘后自动进歌曲列表。
_dl_state = {"running": False, "current": "", "done": 0, "total": 0, "msg": "", "results": []}
_dl_lock = threading.Lock()
_url_cache = {}          # song_id -> (url, expire_ts)，CDN 地址有效期约 20 分钟
URL_CACHE_TTL = 300


@app.get("/api/netease/status")
def api_net_status():
    return jsonify(netease.login_status())


@app.get("/api/netease/qr")
def api_net_qr():
    """生成扫码登录二维码（SVG，内容为网易云官方登录链接）。"""
    try:
        unikey = netease.qr_key()
    except netease.NetError as e:
        return jsonify({"error": str(e)}), 502
    import qrcode
    import qrcode.image.svg as svg_img
    img = qrcode.make(f"https://music.163.com/login?codekey={unikey}",
                      image_factory=svg_img.SvgPathImage, box_size=10, border=2)
    import io
    buf = io.BytesIO()
    img.save(buf)
    svg = buf.getvalue().decode("utf-8")
    svg = re.sub(r'(<svg[^>]*?)\swidth="[^"]*"\sheight="[^"]*"', r"\1", svg, count=1)
    return jsonify({"ok": True, "key": unikey, "svg": svg})


@app.get("/api/netease/qrcheck")
def api_net_qrcheck():
    key = request.args.get("key", "")
    try:
        return jsonify(netease.qr_check(key))
    except netease.NetError as e:
        return jsonify({"error": str(e)}), 502


@app.post("/api/netease/logout")
def api_net_logout():
    netease.logout()
    return jsonify({"ok": True})


@app.post("/api/netease/cookie-login")
def api_net_cookie_login():
    """扫码的保底方案：粘贴浏览器里 music.163.com 的 Cookie 登录。"""
    data = request.get_json(silent=True) or {}
    try:
        out = netease.cookie_login(data.get("cookie", ""))
        return jsonify({"ok": True, "nickname": out["nickname"]})
    except netease.NetError as e:
        return jsonify({"error": str(e)}), 502


@app.get("/api/netease/daily")
def api_net_daily():
    fresh = request.args.get("fresh") == "1"   # 「重新加载」按钮绕过当日缓存
    try:
        return jsonify({"songs": netease.daily_songs(fresh=fresh)})
    except netease.NetError as e:
        return jsonify({"error": str(e)}), 502


@app.get("/api/netease/playlists")
def api_net_playlists():
    try:
        return jsonify({"playlists": netease.personalized_playlists(12)})
    except netease.NetError as e:
        return jsonify({"error": str(e)}), 502


@app.get("/api/netease/playlist/<int:pid>")
def api_net_playlist(pid):
    try:
        return jsonify(netease.playlist_detail(pid))
    except netease.NetError as e:
        return jsonify({"error": str(e)}), 502


@app.get("/api/netease/lyric/<int:sid_n>")
def api_net_lyric(sid_n):
    try:
        return app.response_class(netease.song_lyric(sid_n), mimetype="text/plain")
    except netease.NetError as e:
        return jsonify({"error": str(e)}), 502


@app.get("/api/netease/stream/<int:sid_n>")
def api_net_stream(sid_n):
    """302 到实时获取的播放地址，<audio> 会自动跟随并支持 Range。"""
    cached = _url_cache.get(sid_n)
    if cached and cached[1] > time.time():
        url = cached[0]
    else:
        try:
            info = netease.song_url(sid_n)
        except netease.NetError as e:
            return jsonify({"error": str(e)}), 502
        url = info.get("url") or ""
        if not url:
            return jsonify({"error": "拿不到播放地址（可能需要 VIP 或暂无版权）"}), 404
        _url_cache[sid_n] = (url, time.time() + URL_CACHE_TTL)
    resp = app.response_class(status=302)
    resp.headers["Location"] = url
    return resp


def _download_one(song_id: int) -> dict:
    """下载单曲并整理三件套（音频 + 同名 .lrc + 同名封面）。"""
    try:
        detail = netease.songs_detail_bulk([song_id])
        if not detail:
            return {"id": song_id, "action": "error", "reason": "拿不到歌曲信息"}
        brief = netease._song_brief(detail[0])
        name = brief["name"] or str(song_id)
        artist = brief["artist"]
        stem = sanitize_filename(f"{artist} - {name}")
        info = netease.song_url(song_id)
        if not info["url"]:
            return {"id": song_id, "song": f"{artist} - {name}",
                    "action": "skipped", "reason": "拿不到下载地址（VIP/无版权）"}
        if info.get("trial"):
            # 30 秒试听片段没有收藏价值，不落库，保持曲库干净
            return {"id": song_id, "song": f"{artist} - {name}",
                    "action": "skipped",
                    "reason": "VIP 歌曲当前账号只有 30 秒试听，已跳过下载"}
        ext = "." + (info["type"] or "mp3").lower()
        target = os.path.join(MUSIC_MOUNT, stem + ext)
        if os.path.isfile(target):
            return {"id": song_id, "song": f"{artist} - {name}",
                    "action": "skipped", "reason": "歌曲已存在"}
        proc = subprocess.run(["curl", "-4", "-sS", "-m", "300", "-o", target, info["url"]],
                              capture_output=True, timeout=320, shell=False)
        if proc.returncode != 0 or not os.path.isfile(target) or os.path.getsize(target) < 100_000:
            try:
                os.remove(target)
            except OSError:
                pass
            return {"id": song_id, "song": f"{artist} - {name}",
                    "action": "error", "reason": "音频下载失败"}
        saved = [os.path.basename(target)]
        # 歌词：网易 LRC 本身带作词作曲头，直接落盘
        try:
            lrc_text = netease.song_lyric(song_id)
            if lrc_text.strip():
                lrc_path = os.path.splitext(target)[0] + ".lrc"
                with open(lrc_path, "w", encoding="utf-8") as f:
                    f.write(lrc_text)
                saved.append(os.path.basename(lrc_path))
        except netease.NetError:
            pass
        # 封面：同名图片会被 ensure_covers 自动关联；已有同名封面就不再下载
        if brief.get("pic"):
            pic_path = os.path.splitext(target)[0] + ".jpg"
            if not os.path.isfile(pic_path):
                proc = subprocess.run(["curl", "-4", "-sS", "-m", "60", "-o", pic_path, brief["pic"]],
                                      capture_output=True, timeout=70, shell=False)
                if proc.returncode == 0 and os.path.isfile(pic_path) and os.path.getsize(pic_path) > 5000:
                    saved.append(os.path.basename(pic_path))
                elif os.path.isfile(pic_path):
                    try:
                        os.remove(pic_path)
                    except OSError:
                        pass
        return {"id": song_id, "song": f"{artist} - {name}", "action": "updated", "saved": saved}
    except netease.NetError as e:
        return {"id": song_id, "action": "error", "reason": str(e)}
    except (OSError, subprocess.SubprocessError) as e:
        return {"id": song_id, "action": "error", "reason": f"下载失败：{e}"}


def _download_worker(song_ids: list) -> None:
    _dl_state.update(running=True, total=len(song_ids), done=0, msg="", results=[], current="")
    try:
        for sid_n in song_ids:
            if not _dl_state["running"]:
                break
            _dl_state.update(current=str(sid_n))
            _dl_state["results"].append(_download_one(sid_n))
            _dl_state["done"] += 1
            time.sleep(1)                     # 对网易 CDN 限频
        _dl_state.update(running=False, current="", msg="完成")
    finally:
        _dl_state["running"] = False


@app.post("/api/netease/download")
def api_net_download():
    if not music_dir_ready():
        return jsonify({"error": "优盘未挂载"}), 400
    data = request.get_json(silent=True) or {}
    ids = data.get("ids") or ([data["id"]] if data.get("id") else [])
    ids = [int(i) for i in ids if str(i).strip().lstrip("-").isdigit()]
    if not ids:
        return jsonify({"error": "请提供歌曲 id"}), 400
    with _dl_lock:
        if _dl_state["running"]:
            return jsonify({"error": "已有下载任务在进行中"}), 409
        threading.Thread(target=_download_worker, args=(ids,), daemon=True).start()
    return jsonify({"started": True, "total": len(ids)})


@app.post("/api/netease/dlstop")
def api_net_dlstop():
    _dl_state["running"] = False
    return jsonify({"ok": True})


@app.get("/api/netease/dlstatus")
def api_net_dlstatus():
    return jsonify(_dl_state)


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

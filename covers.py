"""音频封面提取。

规则与歌词一致：封面文件与音乐并列同名存放（<歌名>.jpg/png）。
- 优先使用已存在的同名图片（用户手动放的也算）
- 没有同名图片时，从音频内嵌封面（ID3 APIC / FLAC Picture / MP4 covr）提取一份落盘
"""
import os

from mutagen import File as MutagenFile

COVER_EXTS = (".jpg", ".jpeg", ".png", ".webp")
_MIME_EXT = {"image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


def find_sidecar(audio_path: str):
    """返回与音频同名的已有封面图片路径，没有则 None。"""
    base = os.path.splitext(audio_path)[0]
    for ext in COVER_EXTS:
        candidate = base + ext
        if os.path.isfile(candidate):
            return candidate
    return None


def _ext_from_mime(mime: str) -> str:
    return _MIME_EXT.get((mime or "").lower(), ".jpg")


def extract_embedded(audio_path: str):
    """提取内嵌封面，返回 (bytes, ext)；没有则 None。不抛异常。"""
    try:
        audio = MutagenFile(audio_path)
    except Exception:
        return None
    tags = getattr(audio, "tags", None)
    if audio is None or tags is None:
        return None

    art = None
    # MP3：ID3 APIC 帧
    if hasattr(tags, "getall"):
        apics = tags.getall("APIC")
        if apics:
            art = (apics[0].data, _ext_from_mime(apics[0].mime))
    # M4A：covr 原子
    if art is None and hasattr(tags, "get"):
        try:
            covr = tags.get("covr") or []
        except Exception:
            covr = []
        if covr:
            first = covr[0]
            mime = "image/png" if bytes(first)[:8].startswith(b"\x89PNG") else "image/jpeg"
            art = (bytes(first), _ext_from_mime(mime))
    # FLAC：pictures 列表
    if art is None and hasattr(audio, "pictures") and audio.pictures:
        pic = audio.pictures[0]
        art = (pic.data, _ext_from_mime(pic.mime))

    if not art or not art[0]:
        return None
    return art


def save_sidecar(audio_path: str, data: bytes, ext: str) -> str:
    """把封面写入与音频并列的同名图片，返回路径。已存在同名图片时不覆盖。"""
    base = os.path.splitext(audio_path)[0] + ext
    if os.path.exists(base):
        return base
    with open(base, "wb") as f:
        f.write(data)
    return base

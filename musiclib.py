"""音乐库扫描与元数据管理。

- 歌曲以「相对优盘根目录的路径」作为唯一 ID
- 元数据（显示名/评级/封面/歌词）持久化在 JSON 文件中
- 文件重名不同目录互不影响；文件被删后元数据自动清理
"""
import os
import re

from storage import JsonStore

AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".aac", ".wav", ".ogg", ".oga", ".wma", ".ape"}


def sanitize_filename(name: str) -> str:
    """保留中文与常用字符，去掉路径分隔符与文件系统非法字符。"""
    name = os.path.basename(name).strip()
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name)
    return name or "untitled"


class MusicLibrary:
    def __init__(self, meta_path: str):
        self.store = JsonStore(meta_path)
        self.meta = self.store.load()  # {relpath: {title, rating, cover, lyrics}}

    # ---------- 扫描 ----------
    def scan(self, music_dir: str) -> list:
        found = {}
        for root, _dirs, files in os.walk(music_dir):
            for name in files:
                if os.path.splitext(name)[1].lower() not in AUDIO_EXTS:
                    continue
                rel = os.path.relpath(os.path.join(root, name), music_dir)
                rel = rel.replace(os.sep, "/")
                found[rel] = {"path": rel, "filename": name, **self.meta.get(rel, {})}
        self.meta = found
        self.store.save(found)
        return self.songs()

    def songs(self) -> list:
        return sorted(self.meta.values(), key=lambda s: s["path"].lower())

    def get(self, sid: str):
        return self.meta.get(sid)

    # ---------- 元数据修改 ----------
    def update(self, sid: str, **fields) -> dict:
        song = self.meta[sid]
        for key in ("title", "rating", "cover", "lyrics", "no_cover", "cover_manual"):
            if key in fields:
                song[key] = fields[key]
        self.store.save(self.meta)
        return song

    def remove(self, sid: str) -> None:
        self.meta.pop(sid, None)
        self.store.save(self.meta)

    # ---------- 歌词 ----------
    def find_lrc(self, sid: str, music_dir: str):
        """查找与音频同名的 .lrc 文件（同目录）。"""
        base = os.path.splitext(os.path.join(music_dir, sid))[0]
        lrc_path = base + ".lrc"
        if os.path.isfile(lrc_path):
            try:
                with open(lrc_path, "r", encoding="utf-8", errors="replace") as f:
                    return f.read()
            except OSError:
                return None
        return None

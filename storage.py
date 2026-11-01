"""轻量 JSON 文件存储（原子写入，防止断电损坏）。"""
import json
import os
import tempfile


class JsonStore:
    def __init__(self, path: str, default=None):
        self.path = path
        self.default = default if default is not None else {}

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return self.default

    def save(self, data) -> None:
        """先写临时文件再替换，避免写一半断电导致数据丢失。"""
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

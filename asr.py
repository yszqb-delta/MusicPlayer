"""必剪（Bilibili Bcut）语音识别 → LRC 歌词。

流程参考开源项目 ARS Tool / VideoCaptioner 的 BcutASR 实现：
申请上传 -> 分片上传 -> 提交 -> 创建任务 -> 轮询结果 -> 拼 LRC

仅依赖 requests；识别失败抛 AsrError，由调用方决定如何提示。
"""
import json
import time

import requests

API_BASE = "https://member.bilibili.com/x/bcut/rubick-interface"
HEADERS = {
    "User-Agent": "Bilibili/1.0.0 (https://www.bilibili.com)",
    "Content-Type": "application/json",
}
SUPPORTED_EXTS = {".mp3", ".flac", ".m4a", ".wav"}

POLL_INTERVAL = 1.5
POLL_MAX = 200          # 最长轮询约 5 分钟
HTTP_TIMEOUT = 30


class AsrError(Exception):
    pass


def _ms_to_lrc(ms: int) -> str:
    seconds = ms / 1000
    minutes, seconds = divmod(seconds, 60)
    return f"{int(minutes):02d}:{seconds:05.2f}"


def _post(url: str, payload: dict) -> dict:
    try:
        resp = requests.post(url, data=json.dumps(payload), headers=HEADERS, timeout=HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise AsrError(f"网络请求失败：{e}") from e
    if resp.status_code != 200:
        raise AsrError(f"接口返回 {resp.status_code}")
    data = resp.json()
    if data.get("code") not in (0, "0"):
        raise AsrError(f"接口报错：{data.get('message') or data}")
    return data["data"]


def recognize(binary: bytes) -> str:
    """音频二进制 -> LRC 文本（含逐句时间戳）。"""
    if not binary:
        raise AsrError("音频内容为空")

    # 1. 申请上传
    data = _post(f"{API_BASE}/resource/create", {
        "type": 2,
        "name": "audio.mp3",
        "size": len(binary),
        "ResourceFileType": "mp3",
        "model_id": "8",
    })
    etags = []
    try:
        # 2. 分片上传（每片 per_size 字节）
        for clip, url in enumerate(data["upload_urls"]):
            start = clip * data["per_size"]
            try:
                resp = requests.put(url, data=binary[start:start + data["per_size"]],
                                    headers=HEADERS, timeout=HTTP_TIMEOUT)
                resp.raise_for_status()
            except requests.RequestException as e:
                raise AsrError(f"上传分片失败：{e}") from e
            etags.append(resp.headers.get("Etag", ""))

        # 3. 提交上传
        commit = _post(f"{API_BASE}/resource/create/complete", {
            "InBossKey": data["in_boss_key"],
            "ResourceId": data["resource_id"],
            "Etags": ",".join(etags),
            "UploadId": data["upload_id"],
            "model_id": "8",
        })

        # 4. 创建识别任务
        task = _post(f"{API_BASE}/task", {"resource": commit["download_url"], "model_id": "8"})
        task_id = task["task_id"]
    except KeyError as e:
        raise AsrError(f"接口响应缺字段：{e}") from e

    # 5. 轮询结果
    result = None
    for _ in range(int(POLL_MAX / POLL_INTERVAL)):
        resp = requests.get(f"{API_BASE}/task/result",
                            params={"model_id": 7, "task_id": task_id},
                            headers=HEADERS, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        body = resp.json()["data"]
        state = body.get("state")
        if state == 4:                       # 完成
            result = body.get("result")
            break
        if state in (3, 5):                  # 失败 / 取消
            raise AsrError("识别任务失败，稍后重试或手动上传歌词")
        time.sleep(POLL_INTERVAL)
    if result is None:
        raise AsrError("识别超时")

    # 6. 拼 LRC
    try:
        utterances = json.loads(result)["utterances"]
    except (json.JSONDecodeError, KeyError) as e:
        raise AsrError(f"结果解析失败：{e}") from e

    return "\n".join(
        f"[{_ms_to_lrc(int(u['start_time']))}]{u.get('transcript', '').strip()}"
        for u in utterances if u.get("transcript", "").strip()
    )

# -*- coding: utf-8 -*-
"""网易云音乐接口封装：weapi 加密、二维码登录、每日推荐/推荐歌单/歌曲 URL/歌词/详情。

接口逻辑参考开源项目 NeteaseCloudMusicApi（weapi = AES-CBC 双层加密 + RSA 无填充）。
登录态只保存 MUSIC_U cookie（netease_session.json，权限 600），不保存任何密码。
抓取范围：用户账号本身可播放的内容（每日推荐/歌单），仅供局域网个人使用。
"""
from __future__ import annotations

import base64
import json
import os
import random
import re
import string
import time

import requests
from Crypto.Cipher import AES

BASE = "https://music.163.com"
SESSION_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "netease_session.json")

_PRESET_KEY = b"0CoJUm6Qyw8W8jud"
_IV = b"0102030405060708"
_PUB_MOD = ("00e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7"
            "b725152b3ab17a876aea8a5aa76d2e417629ec4ee341f56135fccf6952"
            "80104e0312ecbda92557c93870114af6c9d05c4f7f0c3685b7a46bee25"
            "5932575cce10b424d813cfe4875d3e82047b97ddef52741d546b8e289d"
            "c6935b3ece0462db0a22b8e7")

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Referer": "https://music.163.com/st/webplayer",
    "Content-Type": "application/x-www-form-urlencoded",
}
HTTP_TIMEOUT = 20

_TITLED = re.compile(r"</?(?:em|b|i)>")   # 搜索结果里的高亮标签（防御性清理）


class NetError(Exception):
    pass


# ==================== 会话 ====================

def load_session() -> dict:
    try:
        with open(SESSION_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_session(sess: dict) -> None:
    try:
        with open(SESSION_PATH, "w", encoding="utf-8") as f:
            json.dump(sess, f, ensure_ascii=False)
        os.chmod(SESSION_PATH, 0o600)
    except OSError:
        pass


def _music_u() -> str:
    return load_session().get("music_u") or ""


# ==================== weapi 加密 ====================

def _aes_cbc_b64(text: str, key: bytes) -> str:
    pad = 16 - len(text.encode()) % 16
    data = text.encode() + pad.to_bytes(1, "big") * pad
    cipher = AES.new(key, AES.MODE_CBC, _IV)
    return base64.b64encode(cipher.encrypt(data)).decode()


def _rsa_no_pad(text: str) -> str:
    raw = text[::-1].encode().hex()
    return format(pow(int(raw, 16), 0x010001, int(_PUB_MOD, 16)), "0>256x")


def _weapi_payload(obj: dict) -> dict:
    text = json.dumps(obj)
    sec_key = "".join(random.choices(string.ascii_letters + string.digits, k=16))
    params = _aes_cbc_b64(_aes_cbc_b64(text, _PRESET_KEY), sec_key.encode())
    return {"params": params, "encSecKey": _rsa_no_pad(sec_key)}


def _cookies_dict() -> dict:
    cookies = {"os": "pc", "appver": "2.10.13"}
    mu = _music_u()
    if mu:
        cookies["MUSIC_U"] = mu
    return cookies


def _weapi(path: str, data: dict, need_login: bool = False) -> dict:
    """POST weapi 接口，返回 JSON 体；响应头/原始 resp 需要时用 _weapi_raw。"""
    if need_login and not _music_u():
        raise NetError("未登录网易云，请先扫码登录")
    return _weapi_raw(path, data).json()


def _weapi_raw(path: str, data: dict) -> requests.Response:
    try:
        resp = requests.post(BASE + path, data=_weapi_payload(data),
                             headers=HEADERS, cookies=_cookies_dict(),
                             timeout=HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise NetError(f"网络请求失败：{e}") from e
    if resp.status_code != 200:
        raise NetError(f"接口返回 {resp.status_code}")
    return resp


def _get_api(path: str, params: dict | None = None) -> dict:
    cookies = {"os": "pc", "appver": "2.10.13"}
    if _music_u():
        cookies["MUSIC_U"] = _music_u()
    try:
        resp = requests.get(BASE + path, params=params, headers=HEADERS,
                            cookies=cookies, timeout=HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise NetError(f"网络请求失败：{e}") from e
    if resp.status_code != 200:
        raise NetError(f"接口返回 {resp.status_code}")
    try:
        return resp.json()
    except ValueError as e:
        raise NetError("响应不是 JSON") from e


# ==================== 二维码登录 ====================

def qr_key() -> str:
    body = _weapi("/weapi/login/qrcode/unikey", {"type": 1})
    unikey = (body.get("unikey") or body.get("data", {}).get("unikey") or "")
    if not unikey:
        raise NetError("获取登录二维码失败")
    return unikey


def qr_check(unikey: str) -> dict:
    """轮询扫码状态：800=过期 802=已扫码待确认 803=成功。"""
    resp = _weapi_raw("/weapi/login/qrcode/client/login", {"key": unikey, "type": 1})
    try:
        body = resp.json()
    except ValueError as e:
        raise NetError("响应不是 JSON") from e
    code = body.get("code")
    out = {"code": code}
    if code == 803:
        music_u = _extract_music_u(body, resp)
        if not music_u:
            # 兜底：拿不到凭证就报错，让用户重试
            raise NetError("登录成功但未拿到凭证，请重试")
        sess = load_session()
        sess["music_u"] = music_u
        # 803 响应体自带 account/profile（实测 /api/nuser/account/get 对二维码
        # 登录的 cookie 恒返回 null，所以昵称必须在登录响应里就地取）
        profile = body.get("profile") or {}
        sess["nickname"] = profile.get("nickname") or sess.get("nickname") or "网易云用户"
        if profile.get("avatarUrl"):
            sess["avatar"] = profile["avatarUrl"]
        acct = body.get("account") or {}
        if acct.get("id"):
            sess["uid"] = acct["id"]
        save_session(sess)
        out["nickname"] = sess["nickname"]
    return out


def _extract_music_u(body: dict, resp: requests.Response) -> str:
    """MUSIC_U 优先在响应头 Set-Cookie，其次 body.cookie 字段，两处都找。"""
    candidates = [", ".join(v for k, v in resp.headers.items() if k.lower() == "set-cookie")]
    if isinstance(body.get("cookie"), str):
        candidates.append(body["cookie"])
    for c in resp.cookies.values():
        candidates.append(str(c))
    raw = " ;; ".join(candidates)
    m = re.search(r"MUSIC_U=([0-9a-fA-F]{32})", raw)
    return m.group(1) if m else ""


def login_status() -> dict:
    """登录态 = 是否存有 MUSIC_U。cookie 过期由内容接口报错体现
    （实测 /api/nuser/account/get 对二维码登录的 cookie 恒返回 null，不能用它判断）。"""
    if not _music_u():
        return {"logged": False}
    sess = load_session()
    return {"logged": True,
            "nickname": sess.get("nickname") or "网易云用户",
            "avatar": sess.get("avatar") or ""}


def logout() -> None:
    sess = load_session()
    sess.pop("music_u", None)
    sess.pop("nickname", None)
    sess.pop("avatar", None)
    save_session(sess)


# ==================== 内容接口 ====================

def _artists_of(item: dict) -> str:
    if isinstance(item.get("artists"), list):
        return "/".join(a.get("name", "") for a in item["artists"] if a.get("name"))
    if isinstance(item.get("ar"), list):
        return "/".join(a.get("name", "") for a in item["ar"] if a.get("name"))
    return "未知歌手"


def _song_brief(item: dict) -> dict:
    album = item.get("album") or item.get("al") or {}
    return {
        "id": item.get("id"),
        "name": _TITLED.sub("", item.get("name") or ""),
        "artist": _artists_of(item),
        "duration": int(item.get("duration") or item.get("dt") or 0),
        "pic": album.get("picUrl") or "",
        "fee": item.get("fee", 0),          # 1=VIP 8=低音质免费 0=免费
    }


def daily_songs() -> list:
    body = _weapi("/weapi/v3/discovery/recommend/songs",
                  {"limit": 30, "offset": 0, "total": True}, need_login=True)
    if body.get("code") == 301 or (body.get("code") != 200 and not body.get("data")):
        raise NetError("登录已过期，请点右上角昵称退出后重新扫码")
    songs = (body.get("data") or {}).get("dailySongs") or []
    return [_song_brief(s) for s in songs]


def personalized_playlists(limit: int = 12) -> list:
    body = _weapi("/weapi/personalized/playlist",
                  {"limit": limit, "offset": 0, "total": True})
    out = []
    for p in body.get("result") or []:
        out.append({"id": p.get("id"), "name": p.get("name") or "",
                    "pic": p.get("picUrl") or "", "count": p.get("trackCount") or 0})
    return out


def playlist_detail(pid: int) -> dict:
    body = _weapi("/weapi/v6/playlist/detail", {"id": pid, "n": 1000, "s": 8})
    pl = body.get("playlist") or {}
    tracks = pl.get("tracks") or []
    if not tracks and pl.get("trackIds"):
        ids = [t["id"] for t in pl["trackIds"][:50] if t.get("id")]
        tracks = songs_detail_bulk(ids)
    return {"name": pl.get("name") or "歌单",
            "pic": pl.get("coverImgUrl") or "",
            "songs": [_song_brief(t) for t in tracks]}


def songs_detail_bulk(ids: list) -> list:
    if not ids:
        return []
    c = json.dumps([{"id": i} for i in ids], separators=(",", ":"))
    body = _get_api("/api/v3/song/detail", {"c": c, "ids": f"[{','.join(map(str, ids))}]"})
    return body.get("songs") or []


def song_url(song_id: int, br: int = 320000) -> dict:
    """返回 {url, type, fee}；url 为空表示当前账号拿不到该音质/无版权。"""
    body = _weapi("/weapi/song/enhance/player/url",
                  {"ids": [song_id], "br": br, "csrf_token": ""})
    data = (body.get("data") or [{}])[0]
    return {"url": data.get("url") or "", "type": data.get("type") or "",
            "fee": data.get("fee", 0), "br": data.get("br", 0)}


def song_lyric(song_id: int) -> str:
    body = _get_api("/api/song/lyric", {"id": song_id, "lv": 1, "kv": 1, "tv": -1})
    return (body.get("lrc") or {}).get("lyric") or ""

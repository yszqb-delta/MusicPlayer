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
    """请求 cookie：MUSIC_U + 登录时保存的完整 cookie 集（__csrf/NMTID 等）。
    实测只带 MUSIC_U 的残缺会话会被当成冷启动设备，个人日推/用户接口异常。"""
    sess = load_session()
    cookies = {"os": "pc", "appver": "2.10.13"}
    cookies.update(sess.get("cookies") or {})
    mu = sess.get("music_u")
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
    try:
        resp = requests.get(BASE + path, params=params, headers=HEADERS,
                            cookies=_cookies_dict(), timeout=HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise NetError(f"网络请求失败：{e}") from e
    if resp.status_code != 200:
        raise NetError(f"接口返回 {resp.status_code}")
    try:
        return resp.json()
    except ValueError as e:
        raise NetError("响应不是 JSON") from e


def _post_clean(path: str, data: dict) -> requests.Response:
    """不携带任何 cookie 的 weapi POST——二维码登录专用。
    实测把旧会话 cookie（__csrf/MUSIC_R_U 等）带进登录轮询时，
    803 响应下发的 MUSIC_U 是匿名 token（account/get 为 null、
    日推变成泛推荐）；对齐 NeteaseCloudMusicApi 标准实现改为干净请求。"""
    try:
        resp = requests.post(BASE + path, data=_weapi_payload(data),
                             headers=HEADERS, timeout=HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise NetError(f"网络请求失败：{e}") from e
    if resp.status_code != 200:
        raise NetError(f"接口返回 {resp.status_code}")
    return resp


# ==================== 扫码登录（NCM 1:1 设备指纹） ====================

LOGIN_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0"),
    "Referer": BASE,
    "Content-Type": "application/x-www-form-urlencoded",
}
_ID_XOR_KEY = "3go8&$8*3*3h0k(2)2"
_login_ctx = {"cookies": None}
_DEVICE_IDS = None


def _device_id() -> str:
    """随机取一个真实格式的设备 ID（列表来自 NeteaseCloudMusicApi 公开数据）。"""
    global _DEVICE_IDS
    if not _DEVICE_IDS:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "deviceid.txt")
        try:
            with open(path, "r", encoding="utf-8") as f:
                ids = [x.strip() for x in f.read().splitlines() if x.strip()]
            _DEVICE_IDS = ids or ["".join(random.choices("0123456789ABCDEF", k=52))]
        except OSError:
            _DEVICE_IDS = ["".join(random.choices("0123456789ABCDEF", k=52))]
    return random.choice(_DEVICE_IDS)


def _dll_encode_id(dev_id: str) -> str:
    xored = "".join(chr(ord(c) ^ ord(_ID_XOR_KEY[i % len(_ID_XOR_KEY)]))
                    for i, c in enumerate(dev_id))
    import hashlib
    return base64.b64encode(hashlib.md5(xored.encode()).digest()).decode()


def _login_begin() -> None:
    """开始一次扫码登录：按 NeteaseCloudMusicApi 请求层 1:1 构造设备指纹
    cookie（_ntes_nuid/_ntes_nnid/WNMCID/WEVNSM/__remember_me/ntes_kaola_ad
    + iphone 客户端档案），并注册匿名游客 MUSIC_A。
    实测缺失这套指纹时，扫码 803 只会下发匿名 MUSIC_U（登录态绑定失败）。"""
    nuid = "".join(random.choices("0123456789abcdef", k=32))
    ts = str(int(time.time() * 1000))
    wnm = "".join(random.choices(string.ascii_lowercase, k=6))
    dev_id = _device_id()
    ck = {
        "__remember_me": "true",
        "ntes_kaola_ad": "1",
        "_ntes_nuid": nuid,
        "_ntes_nnid": "%s,%s" % (nuid, ts),
        "WNMCID": "%s.%s.01.0" % (wnm, ts),
        "WEVNSM": "1.0.0",
        "osver": "16.2",
        "os": "iPhone OS",
        "channel": "distribution",
        "appver": "9.0.90",
        "deviceId": dev_id,
    }
    _login_ctx["cookies"] = ck
    try:
        username = base64.b64encode(
            ("%s %s" % (dev_id, _dll_encode_id(dev_id))).encode()).decode()
        resp = requests.post(BASE + "/weapi/register/anonimous",
                             data=_weapi_payload({"username": username,
                                                  "csrf_token": ""}),
                             headers=LOGIN_HEADERS, cookies=ck,
                             timeout=HTTP_TIMEOUT)
        if resp.json().get("code") == 200 and resp.cookies.get("MUSIC_A"):
            ck["MUSIC_A"] = resp.cookies["MUSIC_A"]
    except (requests.RequestException, ValueError):
        pass


def _post_login(path: str, data: dict) -> requests.Response:
    """登录专用 weapi POST：携带本次登录的设备指纹 cookie（不含旧会话）。"""
    data = dict(data)
    data.setdefault("csrf_token", "")
    try:
        resp = requests.post(BASE + path, data=_weapi_payload(data),
                             headers=LOGIN_HEADERS,
                             cookies=dict(_login_ctx.get("cookies") or {}),
                             timeout=HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise NetError(f"网络请求失败：{e}") from e
    if resp.status_code != 200:
        raise NetError(f"接口返回 {resp.status_code}")
    return resp


# ==================== 二维码登录 ====================

def qr_key() -> str:
    _login_begin()          # 每次出新二维码 = 一次新的登录会话（新设备指纹）
    body = _post_login("/weapi/login/qrcode/unikey", {"type": 3}).json()
    unikey = (body.get("unikey") or body.get("data", {}).get("unikey") or "")
    if not unikey:
        raise NetError("获取登录二维码失败")
    return unikey


def qr_check(unikey: str) -> dict:
    """轮询扫码状态：800=过期 802=已扫码待确认 803=成功。"""
    resp = _post_login("/weapi/login/qrcode/client/login", {"key": unikey, "type": 3})
    try:
        body = resp.json()
    except ValueError as e:
        raise NetError("响应不是 JSON") from e
    code = body.get("code")
    out = {"code": code}
    if code == 803:
        # 响应里可能出现多个 MUSIC_U（先匿名 token 后登录 token），
        # 逐个用账号接口验证，自动选中真正绑定身份的那个
        cands = _music_u_candidates(body, resp)
        if not cands:
            raise NetError("登录成功但未拿到凭证，请重试")
        # 保存登录响应下发的完整 cookie 集（__csrf/NMTID/MUSIC_A 等）。
        # 实测只带 MUSIC_U 的残缺会话会被网易云当作冷启动设备：
        # account/get 返回 null、每日推荐变成与账号无关的泛推荐。
        extra = {}
        for k, v in resp.cookies.items():
            extra[str(k)] = str(v)
        if isinstance(body.get("cookie"), str):
            for m in re.finditer(r"([A-Za-z_][A-Za-z0-9_]*)=([^;]*)", body["cookie"]):
                extra[m.group(1)] = m.group(2)
        extra.pop("MUSIC_U", None)
        # 调试存档：完整的 803 响应头与 body（不含敏感值本身，仅结构排查用）
        try:
            dbg = {"set_cookie": [v for k, v in resp.headers.items() if k.lower() == "set-cookie"],
                   "body_keys": list(body.keys()),
                   "cookie_fields": sorted(extra.keys()),
                   "candidates": len(cands),
                   "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "login_debug.json"), "w", encoding="utf-8") as f:
                json.dump(dbg, f, ensure_ascii=False, indent=1)
        except OSError:
            pass
        music_u, acct, profile = cands[0], {}, {}
        for cand in cands:
            # 用「本次登录响应自己的 cookie + 候选 token」验证身份，
            # 不掺旧会话；端点用 w/nuser/account/get（NCM login_status 同款）
            try:
                r = requests.post(BASE + "/weapi/w/nuser/account/get",
                                  data=_weapi_payload({}),
                                  headers=HEADERS,
                                  cookies=dict(extra, MUSIC_U=cand, os="pc"),
                                  timeout=HTTP_TIMEOUT)
                b2 = r.json()
                if isinstance(b2.get("profile"), dict) and b2["profile"].get("nickname"):
                    music_u, acct, profile = cand, (b2.get("account") or {}), b2["profile"]
                    break
            except (requests.RequestException, ValueError):
                continue
        if not (isinstance(profile, dict) and profile.get("nickname")):
            # 所有候选都验不出身份——大概率是匿名 token，明确报错让用户重扫或走 Cookie 登录
            raise NetError("登录成功但未能绑定账号（服务端下发了匿名凭证）。"
                           "请重试一次扫码，或改用「Cookie 登录」")
        sess = load_session()
        sess["music_u"] = music_u
        if extra:
            sess["cookies"] = extra
        # 803 响应体也自带 account/profile（接口拿不到时兜底）
        acct = acct or (body.get("account") or {})
        profile = profile or (body.get("profile") or {})
        if profile.get("nickname"):
            sess["nickname"] = profile["nickname"]
        elif not sess.get("nickname") or sess.get("nickname") == "网易云用户":
            sess["nickname"] = "网易云用户"
        if profile.get("avatarUrl"):
            sess["avatar"] = profile["avatarUrl"]
        if acct.get("id"):
            sess["uid"] = acct["id"]
        save_session(sess)
        out["nickname"] = sess["nickname"]
    return out


def _music_u_candidates(body: dict, resp: requests.Response) -> list:
    """收集 803 响应里出现过的所有 MUSIC_U 候选（后出现的优先——
    登录成功时服务端往往先下发匿名 cookie 再重写为登录态）。"""
    cands = []
    for k, v in resp.headers.items():
        if k.lower() == "set-cookie":
            cands += re.findall(r"MUSIC_U=([0-9a-fA-F]{32})", v)
    if isinstance(body.get("cookie"), str):
        cands += re.findall(r"MUSIC_U=([0-9a-fA-F]{32})", body["cookie"])
    cands += re.findall(r"MUSIC_U=([0-9a-fA-F]{32})", str(resp.cookies.get_dict()))
    seen, out = set(), []
    for c in cands:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return list(reversed(out))


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
    for k in ("music_u", "nickname", "avatar", "uid", "cookies"):
        sess.pop(k, None)
    save_session(sess)


def cookie_login(cookie_str: str) -> dict:
    """用浏览器复制的 Cookie 字符串登录（扫码的保底方案）。
    解析出 MUSIC_U 及同源的 __csrf 等，先验证身份再落盘。"""
    pairs = {}
    for part in re.split(r"[;\n\r]+", cookie_str or ""):
        if "=" in part:
            k, _, v = part.strip().partition("=")
            k = k.strip()
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
                pairs[k] = v.strip()
    music_u = pairs.pop("MUSIC_U", "")
    if not re.fullmatch(r"[0-9a-fA-F]{32}", music_u or ""):
        raise NetError("Cookie 里没找到有效的 MUSIC_U（登录凭证）")

    try:
        r = requests.post(BASE + "/weapi/w/nuser/account/get",
                          data=_weapi_payload({}), headers=HEADERS,
                          cookies=dict(pairs, MUSIC_U=music_u, os="pc"),
                          timeout=HTTP_TIMEOUT)
        body = r.json()
    except (requests.RequestException, ValueError) as e:
        raise NetError(f"凭证校验失败：{e}") from e
    profile = body.get("profile") if isinstance(body.get("profile"), dict) else {}
    if not profile.get("nickname"):
        raise NetError("这个 Cookie 无法识别账号（可能已失效或复制不全），"
                       "请在已登录 music.163.com 的浏览器里重新复制")

    pairs.pop("os", None)
    sess = load_session()
    sess["music_u"] = music_u
    sess["cookies"] = pairs            # __csrf/NMTID 等一并保存
    sess["nickname"] = profile.get("nickname") or "网易云用户"
    if profile.get("avatarUrl"):
        sess["avatar"] = profile["avatarUrl"]
    acct = body.get("account") or {}
    if acct.get("id"):
        sess["uid"] = acct["id"]
    save_session(sess)
    return {"nickname": sess["nickname"]}


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


_daily_cache = {"date": "", "mu": "", "songs": []}


def daily_songs(fresh: bool = False) -> list:
    """每日推荐。注意：该接口每次会从推荐池里随机抽 30 首（实测连续两次
    请求仅约 2/3 重合），因此这里连抓两次合并去重、并按日期缓存——同一天
    内列表稳定不变，覆盖也更全（约 40 首，更接近当天完整池）。
    fresh=True（前端「重新加载」按钮）时绕过缓存；换登录会话自动失效。"""
    today = time.strftime("%Y%m%d")
    mu = _music_u()[:8]
    if (not fresh and _daily_cache["date"] == today and _daily_cache["mu"] == mu
            and _daily_cache["songs"]):
        return _daily_cache["songs"]
    merged, seen = [], set()
    for _ in range(2):
        body = _weapi("/weapi/v3/discovery/recommend/songs",
                      {"limit": 30, "offset": 0, "total": True}, need_login=True)
        if body.get("code") == 301 or (body.get("code") != 200 and not body.get("data")):
            raise NetError("登录已过期，请点右上角昵称退出后重新扫码")
        songs = (body.get("data") or {}).get("dailySongs") or []
        for s in songs:
            if s.get("id") and s["id"] not in seen:
                seen.add(s["id"])
                merged.append(s)
    out = [_song_brief(s) for s in merged]
    _daily_cache["date"] = today
    _daily_cache["mu"] = mu
    _daily_cache["songs"] = out
    return out


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
    """返回 {url, type, fee, br, trial}；url 为空表示拿不到该音质/无版权；
    trial=True 表示当前账号只能拿到 30 秒试听片段（网易云服务端判定，
    换接口/换音质参数/换客户端身份都无法绕过，通常因账号无 VIP）。"""
    body = _weapi("/weapi/song/enhance/player/url",
                  {"ids": [song_id], "br": br, "csrf_token": ""})
    data = (body.get("data") or [{}])[0]
    return {"url": data.get("url") or "", "type": data.get("type") or "",
            "fee": data.get("fee", 0), "br": data.get("br", 0),
            "trial": bool(data.get("freeTrialInfo"))}


def song_lyric(song_id: int) -> str:
    body = _get_api("/api/song/lyric", {"id": song_id, "lv": 1, "kv": 1, "tv": -1})
    return (body.get("lrc") or {}).get("lyric") or ""

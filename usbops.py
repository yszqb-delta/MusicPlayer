"""优盘挂载/弹出操作（仅 Linux 可用）。

安全规则（不可违反）：
1. 只操作「可移动磁盘」的分区（lsblk RM=1）
2. 挂载点被受保护目录占用的分区一律跳过
3. 挂载目标固定为 MUSIC_MOUNT，弹出时只卸载 MUSIC_MOUNT 上的设备
"""
import json
import os
import shutil
import subprocess

# 音乐目录：环境变量 MUSIC_DIR 可覆盖，默认为项目根下的 music/
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MUSIC_MOUNT = os.environ.get("MUSIC_DIR") or os.path.join(_BASE_DIR, "music")
# 受保护挂载点（永不出现在可挂载列表）：环境变量 USB_PROTECTED_MOUNTS，逗号分隔
PROTECTED_MOUNTS = {
    p.strip() for p in os.environ.get("USB_PROTECTED_MOUNTS", "").split(",") if p.strip()
}


def _run(cmd: list, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def _mountpoints(info: dict) -> list:
    """兼容 lsblk 新旧版本：mountpoint 可能是字符串或列表。"""
    mp = info.get("mountpoint")
    if isinstance(mp, list):
        return [m for m in mp if m]
    return [mp] if mp else []


def list_partitions() -> list:
    """列出所有可移动磁盘分区及挂载状态。"""
    # -b 让 size 返回字节数而不是 "14.9G" 这类字符串
    proc = _run(["lsblk", "--json", "-b", "-o", "NAME,RM,SIZE,FSTYPE,MOUNTPOINT,TYPE"])
    if proc.returncode != 0:
        return []
    result = []
    try:
        devices = json.loads(proc.stdout)["blockdevices"]
    except (json.JSONDecodeError, KeyError):
        return []
    for disk in devices:
        if disk.get("type") != "disk" or not disk.get("rm"):
            continue
        for part in disk.get("children") or []:
            if part.get("type") != "part":
                continue
            mps = _mountpoints(part)
            protected = any(mp in PROTECTED_MOUNTS for mp in mps)
            entry = {
                "dev": f"/dev/{part['name']}",
                "size_bytes": part.get("size"),
                "fstype": part.get("fstype") or "",
                "mountpoints": mps,
                "protected": protected,          # 挂在 /mnt/usb 上，禁止操作
                "mounted_here": MUSIC_MOUNT in mps,
                "can_mount": not protected and not mps,
            }
            if entry["mounted_here"]:            # 附带容量使用情况，供前端展示
                try:
                    usage = shutil.disk_usage(MUSIC_MOUNT)
                    entry["usage"] = {"total": usage.total, "used": usage.used, "free": usage.free}
                except OSError:
                    pass
            result.append(entry)
    return result


def _mounted_at(mount_point: str):
    """返回当前挂在 mount_point 上的设备名，没有则返回 None。"""
    proc = _run(["findmnt", "-rn", "-o", "SOURCE,TARGET", mount_point])
    if proc.returncode != 0:
        return None
    return proc.stdout.split()[0] if proc.stdout else None


def mount(dev: str) -> dict:
    """把指定分区挂载到 MUSIC_MOUNT。"""
    parts = {p["dev"]: p for p in list_partitions()}
    part = parts.get(dev)
    if part is None:
        return {"ok": False, "msg": "不是可移动磁盘的分区，拒绝操作"}
    if part["protected"]:
        return {"ok": False, "msg": "该分区被其他项目使用，禁止操作"}
    if part["mounted_here"]:
        return {"ok": True, "msg": "已经挂载了"}

    os.makedirs(MUSIC_MOUNT, exist_ok=True)
    proc = _run(["mount", dev, MUSIC_MOUNT])
    if proc.returncode != 0:
        return {"ok": False, "msg": (proc.stderr or "mount 失败").strip()}
    return {"ok": True, "msg": f"{dev} 已挂载到 {MUSIC_MOUNT}"}


def eject() -> dict:
    """安全弹出：同步 -> 卸载 -> 尝试断电。"""
    dev = _mounted_at(MUSIC_MOUNT)
    if dev is None:
        return {"ok": False, "msg": "没有优盘挂载在 " + MUSIC_MOUNT}

    _run(["sync"])
    proc = _run(["umount", MUSIC_MOUNT])
    if proc.returncode != 0:
        # 可能还有进程占用，稍等重试一次
        _run(["sync"])
        proc = _run(["umount", MUSIC_MOUNT])
    if proc.returncode != 0:
        return {"ok": False, "msg": "卸载失败，可能有程序正在占用：" + (proc.stderr or "").strip()}

    # 尝试让优盘断电（指示灯熄灭，可安全拔出）；没有 udisksctl 也不影响数据安全
    parent = dev.rstrip("0123456789")
    if shutil.which("udisksctl"):
        _run(["udisksctl", "power-off", "-b", parent])
    return {"ok": True, "msg": f"{dev} 已安全弹出，可以拔掉了"}

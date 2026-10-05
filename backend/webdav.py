"""WebDAV 网盘自动备份：把一份"轻备份包"定期传到用户自己的网盘。

**本地优先不动摇**：WebDAV 只是哑存储——上传出去的是数据的一份拷贝，应用永远不从
网盘读任何配置；恢复是显式动作（下载 → 走本地恢复流程 → 重启落地）。凭据只存本地
config.yaml，跟 api_key 同一个待遇（进任何导出前置空）。

轻包口径：eggpaper.db 快照（SQLite backup API，WAL 里没合页的账也在）+ 脱敏后的
config.yaml。批注、问答、术语、析读、分类、日历全在 db 里；**不含 papers/ 的 PDF
原件与译文、不含截图**——那批东西体积是库的大头，每天全量上传对网盘流量不友好，
且各有自己的来源（PDF 可重下，译文可重翻）。固定文件名覆盖上传（eggpaper-backup.zip），
历史版本交给网盘自己的回收站/版本机制（坚果云等都有）。

兼容性：只用 PUT / GET / OPTIONS 三个动词（各家 WebDAV 支持度最齐的交集）；
Basic 认证；https 校验默认开。测试用 EGGPAPER_WEBDAV_BASE 换基址。
"""
import base64
import json
import os
import sqlite3
import tempfile
import threading
import time
import zipfile

import httpx

import config
import db

FILENAME = "eggpaper-backup.zip"
_state_lock = threading.Lock()
_running = False
_cancel = False

def _state_path() -> str:
    return os.path.join(config.DATA_DIR, "webdav.json")

def conf() -> dict:
    w = config.load().get("webdav", {})
    return {"url": (w.get("url") or "").strip().rstrip("/"),
            "username": w.get("username") or "", "password": w.get("password") or "",
            "auto": bool(w.get("auto")), "days": max(1, int(w.get("days") or 1))}

def configured(c: dict = None) -> bool:
    c = c or conf()
    return bool(c["url"].lower().startswith(("http://", "https://")) and c["url"])

def _read_state() -> dict:
    try:
        with open(_state_path(), encoding="utf-8") as f:
            s = json.load(f)
        return s if isinstance(s, dict) else {}
    except Exception:
        return {}

def _write_state(**kw):
    with _state_lock:
        s = _read_state()
        s.update(kw)
        tmp = _state_path() + f".{time.time_ns()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(s, f, ensure_ascii=False)
            os.replace(tmp, _state_path())
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass

def status() -> dict:
    s = _read_state()
    return {"configured": configured(), "auto": conf()["auto"], "days": conf()["days"],
            "running": _running, "last_ok": s.get("last_ok", 0),
            "last_at": s.get("last_at", ""), "last_error": s.get("error", ""),
            "last_size": s.get("last_size", 0)}

def _auth(c: dict) -> dict:
    token = base64.b64encode(f"{c['username']}:{c['password']}".encode("utf-8")).decode("ascii")
    return {"Authorization": "Basic " + token}

def test(url: str, username: str, password: str) -> tuple:
    """连通性检查：OPTIONS 一发（最轻、各家都支持）。目录不存在不算失败——
    有些网盘要求先在网页端建好目录，PUT 时报错话术里说明。"""
    url = (url or "").strip().rstrip("/")
    if not url.lower().startswith(("http://", "https://")):
        return False, "要填 http(s) 开头的 WebDAV 地址"
    c = {"username": username or "", "password": password or ""}
    try:
        r = httpx.options(url, headers=_auth(c), timeout=httpx.Timeout(12, connect=8),
                          follow_redirects=True)
        if r.status_code in (200, 204):
            return True, ""
        if r.status_code in (401, 403):
            return False, "用户名或密码不对（{}）".format(r.status_code)
        if r.status_code == 404:
            return True, ""      # 有的实现 OPTIONS 任意路径都 404；真正见分晓在 PUT
        return False, f"服务回了 {r.status_code}"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:120]}"

def build_lite_zip(dest: str):
    """轻备份包：db 快照 + 脱敏 config。db 用 SQLite backup API 出快照——
    直接拷文件会把 WAL 里没合页的最近提交丢掉（os._exit 退出不 checkpoint）。"""
    snap = os.path.join(tempfile.gettempdir(),
                        f"eggpaper_db_snap_{os.getpid()}_{time.time_ns()}.tmp")
    src = sqlite3.connect(db.DB_PATH)
    try:
        dst = sqlite3.connect(snap)
        with dst:
            src.backup(dst)
        dst.close()
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
            z.write(snap, "eggpaper.db")
            cfg_path = os.path.join(config.DATA_DIR, "config.yaml")
            if os.path.isfile(cfg_path):
                z.writestr("config.yaml", config.strip_secrets(cfg_path))
    finally:
        src.close()
        try:
            os.remove(snap)
        except OSError:
            pass

def _db_path() -> str:
    return db.DB_PATH

def run_backup(reason: str = "manual") -> dict:
    """打轻包 → PUT 上传。同一个时刻只跑一份（自动与手动撞车时后者直接返回现状）。"""
    global _running
    c = conf()
    if not configured(c):
        _write_state(error="还没配 WebDAV 地址")
        return status()
    with _state_lock:
        if _running:
            return status()
        _running = True
    tmp = ""
    try:
        fd, tmp = tempfile.mkstemp(suffix=".zip")
        os.close(fd)
        build_lite_zip(tmp)
        size = os.path.getsize(tmp)
        r = httpx.put(f"{c['url']}/{FILENAME}", headers=_auth(c),
                      content=open(tmp, "rb"), timeout=httpx.Timeout(600, connect=12),
                      follow_redirects=True)
        if r.status_code in (200, 201, 204):
            _write_state(last_ok=time.time(), last_at=time.strftime("%Y-%m-%d %H:%M"),
                         error="", last_size=size)
            print(f"[eggpaper] WebDAV 备份完成（{reason}）：{size // 1024} KB → {c['url']}/{FILENAME}")
        else:
            why = f"上传被拒（{r.status_code}）"
            if r.status_code in (409, 404, 502):
                why += "：网盘上可能还没有这个目录，先在网盘网页端建好再试"
            _write_state(error=why)
            print(f"[eggpaper] WebDAV 备份失败（{reason}）：{why}")
    except Exception as e:
        _write_state(error=f"{type(e).__name__}: {str(e)[:160]}")
        print(f"[eggpaper] WebDAV 备份失败（{reason}）：{_read_state().get('error', '')}")
    finally:
        if tmp and os.path.isfile(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        with _state_lock:
            _running = False
    return status()

def fetch_latest(dest: str) -> tuple:
    """把网盘上的备份拉到 dest。返回 (ok, why)。"""
    c = conf()
    if not configured(c):
        return False, "还没配 WebDAV 地址"
    try:
        with httpx.stream("GET", f"{c['url']}/{FILENAME}", headers=_auth(c),
                          timeout=httpx.Timeout(600, connect=12), follow_redirects=True) as r:
            if r.status_code == 404:
                return False, "网盘上还没有备份"
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:160]}"

def due() -> bool:
    """自动备份到点了没有：auto 开着、距上次成功超过 days 天。失败后的重试节流
    跟成功用同一条时间线——网盘连不通时每半小时的 tick 重试一次，不算频繁。"""
    c = conf()
    if not c["auto"] or not configured(c):
        return False
    last = _read_state().get("last_ok", 0)
    return (not last) or (time.time() - last >= c["days"] * 86400)

def auto_loop():
    """启动后的常驻节拍：每半小时看一眼到点没有。到点就传一份（失败安静，
    状态留给设置页显示）。"""
    time.sleep(90)          # 起动高峰别抢 IO：让解析、清扫、引擎预热先走
    while True:
        try:
            if due():
                run_backup("auto")
        except Exception as e:
            print(f"[eggpaper] WebDAV 自动备份异常：{type(e).__name__}: {str(e)[:120]}")
        time.sleep(1800)

def kick_auto():
    threading.Thread(target=auto_loop, daemon=True).start()

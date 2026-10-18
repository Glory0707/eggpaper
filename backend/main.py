"""eggpaper 本地服务。唯一出网：用户配置的 LLM API 与 pdf2zh 翻译服务。"""
import base64
import collections
import copy
import json
import math
import os
import queue
import re
import secrets
import shutil
import tempfile
import threading
import time
import traceback
import uuid
import zipfile
from urllib.parse import urlparse

import httpx
import uvicorn
from concurrent.futures import ThreadPoolExecutor, as_completed

from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

import appinfo
import citation
import compare
import fsutil

appinfo.migrate_if_needed()

import config
import crossref
import db
import engine_install
import llm
import ocr
import pdfparse
import picker
import translate_full
import update
import zotero

app = FastAPI(title="eggpaper", version=appinfo.version())
app.add_middleware(CORSMiddleware,
                   allow_origin_regex=r"^http://(127\.0\.0\.1|localhost)(:\d+)?$",
                   allow_methods=["*"], allow_headers=["*"])

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}

def _host_only(h: str) -> str:
    """Host/Origin 头去掉端口（IPv6 字面量带方括号）。"""
    h = (h or "").strip().lower()
    if h.startswith("["):
        return h[1:h.index("]")] if "]" in h else h
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h

@app.middleware("http")
async def _local_only(request: Request, call_next):
    """本机服务再拦一道来源。浏览器不受 CORS 约束，任意网页都能跨站打 127.0.0.1
    （CSRF）；DNS rebinding 还能把 Host 换成攻击者的域名。三道信号（OWASP CSRF
    备忘录与 CERT VU#652514 的标准做法）：Host 非本机、非本机或 null Origin、
    Sec-Fetch-Site: cross-site 一律 403——本机 UI 与 curl（无这些头）不受影响。"""
    host = _host_only(request.headers.get("host", ""))
    if host and host not in _LOCAL_HOSTS:
        return JSONResponse({"detail": "非本机访问已拒绝"}, status_code=403)
    origin = (request.headers.get("origin") or "").strip()
    if origin:   # "null" 也拦：sandbox iframe 可伪造 Origin: null，同属跨站
        if origin.lower() == "null" or _host_only(urlparse(origin).netloc) not in _LOCAL_HOSTS:
            return JSONResponse({"detail": "非本机来源已拒绝"}, status_code=403)
    if (request.headers.get("sec-fetch-site") or "").lower() == "cross-site":
        return JSONResponse({"detail": "非本机来源已拒绝"}, status_code=403)   # 兜住无 Origin 的跨站导航 GET
    return await call_next(request)

@app.middleware("http")
async def _cap_upload_size(request: Request, call_next):
    """上传体先看 content-length 再决定收不收：**必须在路由之前拦**——`file: UploadFile`
    的参数解析发生在端点函数体之前（整包先被 multipart 收进 starlette 的暂存文件），
    端点里再判等于已经白收完一遍。超限直接 413 人话，用户不用白等几分钟上传。
    没有 content-length（chunked）的走端点里的流式上限，那边边收边判。"""
    if request.method == "POST" and request.url.path == "/api/papers":
        try:
            clen = int(request.headers.get("content-length") or 0)
        except ValueError:
            clen = 0
        if clen > _MAX_UPLOAD + (1 << 20):     # 表单边界/字段名那点开销宽限 1MB
            return JSONResponse({"detail": "PDF 超过 500MB，请拆分或压缩"},
                                status_code=413)
    return await call_next(request)

READY = threading.Event()
SERVER = None                # 由 serve() 赋值（uvicorn.Server）：真正 bind 成功才 started=True。
                             # desktop.py 的就绪闸盯它——别再 bind 探端口，探针会和
                             # 服务线程的 bind 撞车（10048 秒退，2026-10-19 实测）
DEFAULT_PORT = 8430          # 与 desktop.py 的 PORTS[0] 一致：改默认端口时两处一起动

def _demo_mode(cfg: dict = None) -> bool:
    """现在这几件事走不走演示数据。**只留这一个判断口。**

    全项目**只有这一个**判断口：用户勾了演示，或者压根没配 key。分散判断会让同一屏里
    一半功能报 `RuntimeError: MOCK`、另一半悄悄给〔演示〕数据。
    """
    cfg = cfg or config.load()
    return bool(cfg["mock"]) or not (cfg.get("provider", {}).get("api_key") or "").strip()

_demo_txt = llm._demo_txt          # 演示文案双语：与 llm 共用同一份实现

# 人话映射统一在 llm.human_error：「测试连接」、流式问答、这里的异常处理器共用一份，
# 免得"哪里报错"决定"用户看到什么"。别名形式与 _demo_txt/_strip_secrets 同款。
_human_msg = llm.human_error

@app.exception_handler(Exception)
async def _any_error(request, exc):
    """别让异常裸奔——前端只会拿到一个"500"，用户看到的就是这个数字。"""
    hint = _human_msg(exc)
    _applog(f"[eggpaper] {request.url.path} 出错 → {hint}")   # 打包版没有 stdout：走 _applog 才留得下痕
    return JSONResponse({"detail": hint}, status_code=500)

@app.exception_handler(OverflowError)
async def _too_big(request, exc):
    """超大整数：客户端拿 10**30 当 id/序号送进来，SQLite 绑参数当场 OverflowError。
    这是客户端输入的问题，回 400 人话；不拦的话它冒充服务端 500，日志里也看不出是参数坏。"""
    _applog(f"[eggpaper] {request.url.path} 数值超界：{exc}")
    return JSONResponse({"detail": "参数数值超出范围"}, status_code=400)

@app.exception_handler(RequestValidationError)
async def _bad_params(request, exc):
    """参数不合法时 FastAPI 默认回一坨 422 的数组，前端那句 detail 展示不了。
    翻成一句人话，并压成 400（客户端的问题，不该记成服务端 500）。"""
    errs = exc.errors() or [{}]
    where = ".".join(str(x) for x in errs[0].get("loc", []) if x != "body") or "参数"
    return JSONResponse({"detail": f"{where} 不合法：{errs[0].get('msg', '请求格式不对')}"},
                        status_code=400)

PAPERS_DIR = os.path.join(config.DATA_DIR, "papers")
_import_lock = threading.Lock()     # 查重到建库必须一气呵成：并发导入同一份文件会各得一篇
_restore_lock = threading.Lock()    # 恢复的解包全程持锁：并发恢复会互相拆对方的暂存目录

_dir_cache = {}                     # pid -> papers/ 下的目录名（可读名迁移后与解析结果）
_dir_lock = threading.Lock()

def _slug(s: str, cap: int = 48) -> str:
    """文件/文件夹名里的安全短名：连字还原（PDF 提取常带 ﬁﬂ，资源管理器里是怪字）、
    去 Windows 非法字符与控制符，压空白，截 cap 字。"""
    s = (s or "").translate({0xFB00: "ff", 0xFB01: "fi", 0xFB02: "fl", 0xFB03: "ffi", 0xFB04: "ffl"})
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', " ", s)
    return re.sub(r"\s+", " ", s).strip(" .-")[:cap].strip()

def _display_name(p: dict) -> str:
    """Zotero 式显示名：`作者 - 年份 - 标题`，缺哪段省哪段（作者只取第一位，同 Zotero
    默认的 1-2 位）。文件夹名、库内文件名都从它派生——盘上名字里从此没有 pid：
    人找得着，把 PDF 发给别人对方也认得是哪篇。洗不出可读名就空串（调用方保持原样）。"""
    parts = []
    au = re.split(r"[;,，、；&]|\s+and\s+", (p.get("authors") or "").strip())[0].strip()
    au = re.sub(r"\s+et\s+al\.?$", "", au, flags=re.I).strip()
    if au:
        parts.append(au[:40])
    yr = str(p.get("year") or "").strip()
    if yr:
        parts.append(yr)
    t = (p.get("title") or "").strip()
    if t:
        parts.append(t)
    return _slug(" - ".join(parts), cap=64)

def paper_dir(pid: str) -> str:
    """一篇论文的全部盘上数据都收在它自己的文件夹里：
    `<显示名>.pdf`（库内原件）+ `<显示名>（译文）.pdf` + `<显示名>（双语）.pdf` + .pages/（页级中间产物）。
    目录名认 DB 的 path 字段（锚）——盘上名字随标题改，字段跟着改写，两边永不脱钩；
    顺带认老布局的 `<id>/` 与 `<id> 前缀`目录（对齐迁移没跑到时的兜底）。
    解析结果进 _dir_cache，别在热路径上反复查库 listdir。"""
    with _dir_lock:
        name = _dir_cache.get(pid)
    if name:
        d = os.path.join(PAPERS_DIR, name)
        if os.path.isdir(d):
            return d
        with _dir_lock:
            _dir_cache.pop(pid, None)
    v = (db.get_paper(pid) or {}).get("path") or ""
    if v:
        d = os.path.dirname(v)
        if os.path.isdir(d):
            with _dir_lock:
                _dir_cache[pid] = os.path.basename(d)
            return d
    d = os.path.join(PAPERS_DIR, pid)
    if os.path.isdir(d):
        with _dir_lock:
            _dir_cache[pid] = pid
        return d
    try:
        prefix = pid + " "
        for n in os.listdir(PAPERS_DIR):
            if n.startswith(prefix):
                with _dir_lock:
                    _dir_cache[pid] = n
                return os.path.join(PAPERS_DIR, n)
    except OSError:
        pass
    return d if v else os.path.join(PAPERS_DIR, pid)

def _discard_paper_dir(pid: str, pid_dir: str):
    """导入失败的收场：半截目录整个清掉，缓存别留悬空项（rmtree 对空目录就是 rmdir）。"""
    shutil.rmtree(pid_dir, ignore_errors=True)
    with _dir_lock:
        _dir_cache.pop(pid, None)

def _backfill_pdf_hashes():
    """给旧库论文慢慢补内容指纹（导入判重的第二把钥匙）。

    启动后单独一条低优先级线程：一篇算完歇 2 秒，几百 MB 的库几分钟补完，
    不跟导入/析读抢 IO。算过的论文不会再来第二遍（判据就是指纹为空）。
    """
    def work():
        for pid, path in db.papers_missing_hash():
            h = fsutil.sha256_file(path or "", quiet=True)
            if h:
                try:
                    db.update_paper(pid, pdf_hash=h)
                except Exception as e:
                    _applog(f"指纹回填 {pid} 失败：{_human_msg(e)}")
            time.sleep(2)

    threading.Thread(target=work, daemon=True).start()

def _migrate_paper_layout():
    """旧平铺布局（library/{pid}.pdf、translated/{pid}-mono/dual.pdf、translated/.pages-{pid}/）
    一次性搬进 papers/{pid}/，db 里的路径同步改写；搬完清掉空壳旧目录。有文件搬不动
    （被占用）就不删目录，下次启动接着试。"""
    lib = os.path.join(config.DATA_DIR, "library")
    tr = os.path.join(config.DATA_DIR, "translated")
    if not os.path.isdir(lib) and not os.path.isdir(tr):
        return

    def under(path: str, root: str) -> bool:
        p = os.path.normcase(os.path.abspath(path)) if path else ""
        return p.startswith(os.path.normcase(os.path.abspath(root)) + os.sep)

    moved = 0
    stuck = set()
    for row in db.list_papers():
        pid = row["id"]
        p = db.get_paper(pid) or {}
        updates = {}
        for key, dest in (("path", "paper.pdf"), ("mono_path", "mono.pdf"), ("dual_path", "dual.pdf")):
            src = p.get(key) or ""
            root = lib if key == "path" else tr
            if not under(src, root) or not os.path.isfile(src):
                continue
            os.makedirs(paper_dir(pid), exist_ok=True)
            dst = os.path.join(paper_dir(pid), dest)
            try:
                os.replace(src, dst)
            except OSError:
                stuck.add(os.path.normcase(os.path.abspath(src)))
                continue
            updates[key] = dst
            moved += 1
        old_pages = os.path.join(tr, f".pages-{pid}")
        if os.path.isdir(old_pages):
            os.makedirs(paper_dir(pid), exist_ok=True)
            new_pages = os.path.join(paper_dir(pid), ".pages")
            shutil.rmtree(new_pages, ignore_errors=True)
            try:
                os.replace(old_pages, new_pages)
            except OSError:
                pass
        if updates:
            db.update_paper(pid, **updates)
    for d in (lib, tr):
        if not os.path.isdir(d):
            continue
        for dirpath, _dirs, files in os.walk(d):
            for fn in files:
                fp = os.path.normcase(os.path.abspath(os.path.join(dirpath, fn)))
                if fp in stuck:
                    continue
                try:
                    os.remove(os.path.join(dirpath, fn))
                except OSError:
                    pass
        shutil.rmtree(d, ignore_errors=True)
    if moved:
        _applog(f"数据目录迁移：{moved} 个文件归位到 papers/<论文 id>/ 每篇一个文件夹")

# 成品（译文/双语）文件名的中缀只在这一处写字面量：_INNER_SUFFIX 认名/改名、
# _variant_names 拼名，差一个字就认不出自家刚写出的成品
_MONO_SUFFIX = "（译文）"
_DUAL_SUFFIX = "（双语）"

_INNER_SUFFIX = (("path", ""), ("mono_path", _MONO_SUFFIX), ("dual_path", _DUAL_SUFFIX))

def _variant_names(base: str) -> tuple:
    """两个成品文件名（含扩展名、不含目录）：(译文, 双语)——中缀与 _INNER_SUFFIX 同源。

    base 由调用方从显示名算好（空名的兜底各按各的口径：serve 路径退回 "paper"、
    认领旧成品时退回空 = 没有可认的名字）。空 base 给空名，别造一个假名。
    """
    if not base:
        return "", ""
    return f"{base}{_MONO_SUFFIX}.pdf", f"{base}{_DUAL_SUFFIX}.pdf"

def _late_align(pid: str):
    """当场没对齐成的（刚导入的 PDF 被杀软攥几秒是常态）交给后台保洁追平：
    每 2 秒再试一次 _align_names，挪成功或 30 秒封顶就收工。"""
    def work():
        deadline = time.time() + 30
        while time.time() < deadline:
            time.sleep(2)
            try:
                # 返回 False = 没什么可挪（已对齐）或挪不动到封顶；篇没了也收工
                if not _align_names(pid) or _pid_gone(pid):
                    return
            except Exception as e:
                # 原来这里静默 return：名字对不上是"看得见"的小毛病，但它在 30 秒内
                # 一直失败却一个字都不记，事后完全无从判断（是占用、还是逻辑错）
                _applog(f"名字对齐 {pid} 中途失败，放弃这一轮（标题照旧，下次启动的迁移再追一次）：{_human_msg(e)}")
                return
    threading.Thread(target=work, daemon=True).start()

def _align_names(pid: str) -> bool:
    """把一篇论文的盘上名字对齐到 Zotero 式可读名：文件夹 = `作者 - 年份 - 标题`，
    库内文件 = `<显示名>.pdf` / `<显示名>（译文）.pdf` / `<显示名>（双语）.pdf`，
    DB 的 path/mono_path/dual_path 同步改写（字段是锚：先挪盘上、后改字段，不指空）。

    幂等可断续：挪不动（被占用/目标已存在）安静放弃——标题照改，留给下次启动的
    _rename_paper_dirs 追平；当前名已经是这个显示名的合法形态（含历史碰撞后缀
    ` (2)`）就不动，免得两篇同题论文每次启动互换后缀。基础名腾空了（比如同题旧版
    被删）优先升级成干净名。"""
    p = db.get_paper(pid) or {}
    base = _display_name(p)
    if not base:
        return False
    cur = paper_dir(pid)
    if not os.path.isdir(cur):
        return False
    moved = False
    cur_base = os.path.basename(os.path.normpath(cur))
    want_name = cur_base
    if cur_base != base and not os.path.exists(os.path.join(PAPERS_DIR, base)):
        want_name = base                       # 干净名空着：优先用它
    elif cur_base != base and \
            not re.match(rf"^{re.escape(base)}(?: \(\d+\))?$", cur_base):
        want_name = base                       # 名字跟标题对不上：找第一个空位
        i = 2
        while i <= 9 and os.path.exists(os.path.join(PAPERS_DIR, want_name)):
            want_name = f"{base} ({i})"
            i += 1
    # **先挪文件、后挪文件夹**：原件被杀软/浏览器攥着时 replace 会输——那就连夹子
    # 也别挪，免得「夹已改名、path 字段还指着旧夹」的悬空态（下一拍保洁再来追平）。
    blocked = False
    ups = {}
    _legacy_inner = {"path": "paper.pdf", "mono_path": "mono.pdf", "dual_path": "dual.pdf"}
    for key, suf in _INNER_SUFFIX:
        v = p.get(key) or ""
        if v and not os.path.isfile(v):
            # 字段断了（数据目录迁移只搬了文件没跟上字段、外部原件被清）：夹内有像样的
            # 就认领——新旧两种命名都认。不认领的话这份译文永远躺在夹里没人指。
            legacy = _legacy_inner[key]
            v = next((os.path.join(cur, c) for c in (f"{base}{suf}.pdf", legacy)
                      if c and os.path.isfile(os.path.join(cur, c))), "")
        if not v or not os.path.isfile(v):
            continue
        if os.path.normcase(os.path.dirname(os.path.abspath(v))) != \
                os.path.normcase(os.path.abspath(cur)):
            continue    # 外部原件：不属于这里的对齐范围
        fn = f"{base}{suf}.pdf"
        if os.path.basename(v) == fn:
            if v != p.get(key):
                ups[key] = v          # 认领到的文件恰好已在目标名上：字段直接修回
                moved = True
            continue
        tgt = os.path.join(cur, fn)
        if os.path.exists(tgt):
            continue
        try:
            os.replace(v, tgt)
        except OSError:
            blocked = key == "path"    # 原件挪不动：这一拍夹子不动，字段照旧（一致）
            continue
        ups[key] = tgt
        moved = True
    if not blocked and want_name != cur_base:
        want = os.path.join(PAPERS_DIR, want_name)
        old_dir = cur
        try:
            os.rename(cur, want)
        except OSError:
            pass                       # 被占用：夹名不动，已挪的文件照常改字段
        else:
            with _dir_lock:
                _dir_cache[pid] = want_name
            cur = want
            for k in list(ups):
                ups[k] = os.path.join(want, os.path.basename(ups[k]))
            # 文件早已就位（本轮被同名检查跳过）的：夹子挪了，字段必须跟着改，
            # 否则 path/mono_path 全悬在旧夹上
            for key, _suf in _INNER_SUFFIX:
                if key in ups:
                    continue
                v = p.get(key) or ""
                if v and os.path.normcase(os.path.dirname(os.path.abspath(v))) == \
                        os.path.normcase(os.path.abspath(old_dir)):
                    ups[key] = os.path.join(want, os.path.basename(v))
            moved = True
    if ups:
        db.update_paper(pid, **ups)
    return moved

def _align_or_late(pid: str) -> bool:
    """盘上名字立刻对齐可读名；当场没挪动（杀软攥着刚落的 PDF 之类）交给保洁几秒内追平。
    返回是否真的挪了（改名端点要原样回给前端）。"""
    moved = _align_names(pid)
    if not moved:
        _late_align(pid)
    return moved

def _rename_paper_dirs():
    """启动时把每篇的夹名与库内文件名对齐可读名（老库的 `<id> <标题短名>`/裸 id、
    改名时目录被占用没跟上的、crossref 后来才补上作者年份的一律在这里追平）。

    **只在启动时做**：跑任务期间挪目录，读写路径就赌输了；启动时没有任何任务在
    跑。运行中的改名诉求（标题编辑）由忙碌闸拒绝，不在这里补。
    """
    n = 0
    for row in db.list_papers():
        if _align_names(row["id"]):
            n += 1
    if n:
        _applog(f"论文夹名/文件名对齐可读名：{n} 篇")

def _repair_paper_paths():
    """path 字段与库内副本互相修（启动时跑一遍）：

    - path 断了（原件被移动/删除、目录被改名）而库内副本还在 → 字段改回库内副本；
    - 库内副本缺了而 path 还在 → 拷一份进库（"eggpaper 目录下自动存一版"）。
    对应"用户动了原 PDF / 清过数据目录"的场景——能救的启动时都救回来。"""
    fixed = copied = 0
    for row in db.list_papers():
        pid = row["id"]
        p = db.get_paper(pid) or {}
        d = paper_dir(pid)
        base = _display_name(p)
        src = p.get("path") or ""
        inside = src and os.path.normcase(os.path.dirname(os.path.abspath(src))) == \
            os.path.normcase(os.path.abspath(d))
        # 库内副本：先认 path 字段（在本夹里），断了退夹里的新旧两种命名
        lib = src if inside and os.path.isfile(src) else ""
        if not lib:
            for cand in _lib_candidates(base):
                c = os.path.join(d, cand) if cand else ""
                if c and os.path.isfile(c):
                    lib = c
                    break
        if lib:
            if os.path.normcase(os.path.abspath(src or lib)) != os.path.normcase(os.path.abspath(lib)):
                db.update_paper(pid, path=lib)
                fixed += 1
            continue
        if src and os.path.isfile(src) and not inside:
            try:
                os.makedirs(d, exist_ok=True)
                shutil.copy2(src, os.path.join(d, _lib_name(base)))
                copied += 1
            except OSError:
                pass
    if fixed or copied:
        _applog(f"自愈：{fixed} 篇的来源路径改回库内副本，{copied} 篇补了库内副本")

def _lib_candidates(base: str) -> tuple:
    """库内副本的候选名（按优先级）：可读名在前、老命名 paper.pdf 兜底。

    base 为空（还没对齐出显示名）时首位是空串——调用方按"有没有"跳过它，
    保持与历史写法一致的顺序与空位语义。"""
    return ((f"{base}.pdf" if base else ""), "paper.pdf")

def _lib_name(base: str) -> str:
    """库内副本最终落的名字：有可读名就用它，否则退回老命名。"""
    return f"{base}.pdf" if base else "paper.pdf"

def _paper_src(pid: str, p: dict) -> str:
    """这篇论文的原 PDF 在哪：先认 path 字段，断了退库内副本（顺手把字段修回来）。

    历史遗留行的 path 可能指着早已移动/删除的外部原件——eggpaper 反正存了自己的副本，
    不该因为外部文件动了就判死。"""
    src = p.get("path") or ""
    if src and os.path.exists(src):
        return src
    d = paper_dir(pid)
    base = _display_name(p)
    for cand in _lib_candidates(base):
        alt = os.path.join(d, cand) if cand else ""
        if alt and os.path.exists(alt):
            try:
                if src:
                    db.update_paper(pid, path=alt)
            except Exception as e:
                _applog(f"路径自愈 {pid}: path 字段改不回库内副本（{_human_msg(e)}）")
            return alt
    return src or os.path.join(d, "paper.pdf")

def _paper_src_or_404(pid: str, p: dict) -> str:
    """要真读 PDF 的端点用这扇闸：原件和库内副本都不在，回一句 PDF_GONE 而不是
    让 pymupdf 在打开时翻成 500。"""
    src = _paper_src(pid, p)
    if not src or not os.path.exists(src):
        raise HTTPException(404, PDF_GONE)
    return src

@app.on_event("startup")
def _startup():
    _apply_pending_restore()   # 必须最先生效：换的是数据目录的内容，db 还没被碰过
    config.ensure_dirs()
    os.makedirs(PAPERS_DIR, exist_ok=True)
    _migrate_paper_layout()
    _rename_paper_dirs()

    def _late_boot():
        # 修路径（可能整本拷 PDF）、孤儿清扫（可能删几个 GB）、认领孤儿译文、
        # 指纹回填都是分钟级的大库重活——留在 startup 里，用户看到的就是
        # 白屏干等几分钟。搬去后台：界面先起来，这些活几秒内默默跟上。
        try:
            _sweep_old_data_dirs()   # 可能 rmtree 掉几个 GB 的旧数据目录：别挡在 uvicorn accept 前面
            _repair_paper_paths()
            try:
                for pid in os.listdir(PAPERS_DIR):
                    translate_full.sweep_key_copies(os.path.join(PAPERS_DIR, pid, ".pages"))
            except OSError:
                pass
            translate_full.sweep_page_dirs(PAPERS_DIR)
            try:
                import engine_install
                gone = engine_install.sweep_leftovers()
                if gone:
                    _applog("引擎残骸已清：" + "、".join(gone))
            except Exception as e:
                _applog(f"引擎残骸清扫失败（不影响启动）：{_human_msg(e)}")
            _clear_zombie_jobs()
            _backfill_pdf_hashes()
            _reparse_sweep()
        except Exception:
            _applog("启动收尾失败：" + traceback.format_exc(limit=3))
    threading.Thread(target=_late_boot, daemon=True).start()

    def _warm_engine():
        time.sleep(3)
        try:
            cfg = config.load()
            exe = translate_full.engine_path((cfg["pdf2zh"].get("path") or "").strip())
            translate_full.engine_probe_cached(exe)
            import engine_install
            engine_install.find_installed()   # 目录树走一遍进缓存：首发点击别付这笔
            # 服务探针一并预热：冷探针是一次最坏 2.5 秒的 TCP 连接，别让用户点
            # 「全文翻译」的第一下付这笔账（前端按钮的即时反馈就靠 POST 快速返回）
            svc = (cfg["pdf2zh"].get("service") or "bing").strip()
            translate_full.choose_service(svc, _pdf2zh_env(svc, cfg)[1])
        except Exception as e:
            _applog(f"引擎/服务预热失败（不影响使用，点翻译时会重试）：{_human_msg(e)}")
    threading.Thread(target=_warm_engine, daemon=True).start()

def _apply_pending_restore():
    """上一次会话里「从备份恢复」的暂存在这里落地：此刻 db 还没人打开、papers 无人引用，
    才换得动整个数据目录的内容（Windows 上打开中的文件不能改名）。

    旧目录整个改名让位、暂存顶上之后，引擎的资产缓存（home/，几百 MB，跟着数据目录住）
    从旧目录搬回来——那是引擎自己能重新生成的，但重新生成要重新解包几分钟，没必要。
    旧目录留给后台线程慢慢删，起不来不等它。
    """
    mark = os.path.join(config.DATA_DIR, "pending_restore.json")
    if not os.path.isfile(mark):
        return
    try:
        with open(mark, encoding="utf-8") as f:
            staged = (json.load(f) or {}).get("staged") or ""
        if not staged or not os.path.isdir(staged):
            os.remove(mark)
            return
        data = config.DATA_DIR
        old = data + ".old-" + time.strftime("%Y%m%d%H%M%S")
        last = None
        # 6 轮 × 3 秒：上一会话的 pdf2zh/杀毒/资源管理器预览都可能还握着数据目录里的句柄
        # （Windows 上"有文件被打开"就让整个目录改名失败，WinError 5/32）。原来只等 3 轮，
        # 杀软实时扫描一轮就能把这 9 秒耗完——那次的结局是"恢复静默没落地，标记留着
        # 下次启动再来"，用户看到的却是"恢复完成、已重启"。等久一点，别把十几秒算成失败。
        for attempt in range(6):
            try:
                os.rename(data, old)
                last = None
                break
            except OSError as e:
                last = e
                if attempt < 5:
                    _applog(f"备份恢复落地：数据目录改名被占用（第 {attempt + 1} 次），3 秒后重试：{e}")
                    time.sleep(3)
        if last is not None:
            raise last                 # 标记留在原地，下次启动重试
        try:
            os.rename(staged, data)
        except OSError:
            os.rename(old, data)      # 顶不上就回滚，库里还是原样
            raise
        # 撕标记：标记文件本身就在数据目录里，刚跟着旧目录一起被改名搬走了，
        # 所以要到 old 里去删——原路径此刻指的是新目录、文件不在那儿，os.remove 必抛
        # FileNotFoundError，整段就被下面的 except 兜住：日志写"落地失败"（其实已成功），
        # home/ 资产的回搬与旧目录的清理也一并被跳过。删不掉不算失败：old 反正要删。
        try:
            os.remove(os.path.join(old, "pending_restore.json"))
        except OSError:
            pass
        home_old = os.path.join(old, "home")
        if os.path.isdir(home_old) and not os.path.exists(os.path.join(data, "home")):
            try:
                os.rename(home_old, os.path.join(data, "home"))
            except OSError:
                pass
        _applog(f"备份恢复生效：{staged} -> {data}")
        def _rmtree_late():
            time.sleep(5)
            shutil.rmtree(old, ignore_errors=True)
        threading.Thread(target=_rmtree_late, daemon=True).start()
    except Exception:
        _applog("备份恢复落地失败：" + traceback.format_exc(limit=3))

def _sweep_old_data_dirs():
    """恢复落地后的旧目录（上个会话没删完的）启动时顺手清；.restore 暂存同理——标记还在
    （这次启动就要用它落地）时别碰。另回收数据目录里的上传残渣（.upload-*.tmp）：只删超过
    1 天的，正在写的临时文件 mtime 每块都在刷新，误伤不到活的请求。"""
    base = os.path.dirname(config.DATA_DIR)
    name = os.path.basename(config.DATA_DIR)
    try:
        leftovers = [d for d in os.listdir(base) if d.startswith(name + ".old-")
                     or d == name + ".restore"]
    except OSError:
        leftovers = []
    if os.path.isfile(os.path.join(config.DATA_DIR, "pending_restore.json")):
        leftovers = [d for d in leftovers if not d.endswith(".restore")]
    for d in leftovers:
        shutil.rmtree(os.path.join(base, d), ignore_errors=True)
    try:
        stale = time.time() - 86400
        for fn in os.listdir(config.DATA_DIR):
            fp = os.path.join(config.DATA_DIR, fn)
            if fn.startswith(".upload-") and fn.endswith(".tmp") \
                    and os.path.isfile(fp) and os.path.getmtime(fp) < stale:
                fsutil.rm_quiet(fp)
                _applog(f"回收上传残渣：{fn}")
    except OSError:
        pass

_TRASH_PREFIX = ".trash-"

def _sweep_orphan_papers():
    """papers/ 里没有论文指向的文件夹：改名进 .trash-<本场时间戳>-<原名>/ 观察一场，
    下一场启动才删。孤儿来源：导入写了一半进程被杀，或旧平铺布局的残骸。
    **绝不就地 rmtree**——db 被换空/损坏而 papers/ 完好时会把整个文库一次删光；
    留一场启动的窗口，db 修回来了下一场按 id 从 trash 抢回。夹名认两代
    （老名取空格前那段判 id，可读名按 DB path 锚）——**这步绝不能比 rename pass 先跑**。"""
    try:
        dirs = os.listdir(PAPERS_DIR)
    except OSError:
        return
    known = {row["id"] for row in db.list_papers()}
    # 夹名认主两把尺：可读名（无 pid）按 DB path 锚——path 的 dirname 就是它的夹；
    # 老两代名（裸 `<id>/`、`<id> <短名>/`）保留 id 前缀判法兜底。可读名夹在 DB
    # 路径断了时靠启动自愈先修回来（_repair_paper_paths 先于本清扫跑），不误伤。
    known_dirs = {os.path.normcase(os.path.abspath(os.path.dirname(v)))
                  for v in (r["path"] or "" for r in db.q("SELECT path FROM papers")) if v}

    def owned(name: str) -> bool:
        if os.path.normcase(os.path.abspath(os.path.join(PAPERS_DIR, name))) in known_dirs:
            return True
        i = name.find(" ")
        return (name if i < 0 else name[:i]) in known

    stamp = time.strftime("%Y%m%d%H%M%S")
    rescued = deleted = trashed = 0
    for d in dirs:
        if not d.startswith(_TRASH_PREFIX):
            continue
        rest = d[len(_TRASH_PREFIX):]
        # 与下方改名格式互逆：.trash-<14位时间戳>-<原名>；认不出时间戳就整个当原名
        orig = rest[15:] if (len(rest) > 15 and rest[:14].isdigit() and rest[14] == "-") else rest
        if owned(orig):
            if not os.path.exists(os.path.join(PAPERS_DIR, orig)):
                try:
                    os.rename(os.path.join(PAPERS_DIR, d), os.path.join(PAPERS_DIR, orig))
                    rescued += 1
                except OSError:
                    pass
            continue
        if rest[:14].isdigit() and rest[:14] >= stamp:
            continue                      # 本场刚进观察的：留到下一场启动再处置
        shutil.rmtree(os.path.join(PAPERS_DIR, d), ignore_errors=True)
        deleted += 1
    for d in dirs:
        if d.startswith(_TRASH_PREFIX) or owned(d):
            continue
        try:
            os.rename(os.path.join(PAPERS_DIR, d),
                      os.path.join(PAPERS_DIR, f"{_TRASH_PREFIX}{stamp}-{d}"))
            trashed += 1
        except OSError:
            continue
    if trashed or rescued or deleted:
        _applog(f"启动清理：孤儿文件夹 {trashed} 个进 .trash 观察一场，"
                f"从 trash 抢回 {rescued} 个，删掉上一场留下的 {deleted} 个")

def _clear_zombie_jobs():
    """把"上一次进程留下的在跑状态"清掉。

    析读/翻译都是**守护线程**在跑，状态却写在数据库里：进程一没（崩溃、
    装新版重启、托盘退出），那条 `running` 就永远留在库里——POST 看到 running
    直接返回，用户点多少次都没反应。刚启动的进程里不可能有任务在跑，这些状态
    全是僵尸，一律归零；运行中途的判断看 `_live_jobs`（本进程的真实登记）。
    """
    _adopt_orphan_translation()
    _sweep_orphan_papers()
    for col, kind in (("analysis_status", "analysis"),
                      ("translate_status", "translate")):
        rows = db.q(f"SELECT id FROM papers WHERE {col} IN ('running','queued')")

        def alive(x: str) -> bool:
            if kind == "analysis":
                # 析读的活口有两个真相源：在跑的线程 + 排队还没被 loop 接走的。
                # 只看 _job_live 会把"已入队、loop 还没取到"的篇当僵尸归零——
                # 晚启动清扫能跑好几分钟，这期间导入的篇正好落在这个窗口里
                return _analysis_inflight(x)
            return _job_live(kind, x) or translate_full.job(x)["status"] in ("running", "queued")

        # 晚启动清扫与本进程刚接的新任务有竞争窗：活着的任务（含本进程已在跑的翻译）
        # 不能归零，否则界面状态闪回"没做过"，诱发重复点击。
        # 条件里再验一次状态：查完清单到这一刻之间任务可能已经跑完（done 写在库里），
        # 无条件 UPDATE 会把刚写完的 done 一起抹成 none
        stale = [r["id"] for r in rows if not alive(r["id"])]
        if not stale:
            continue
        qmarks = ",".join("?" * len(stale))
        db.q(f"UPDATE papers SET {col}='none' WHERE id IN ({qmarks}) "
             f"AND {col} IN ('running','queued')", tuple(stale), commit=True)
        _applog(f"启动清理：{len(stale)} 篇的 {col} 卡在 running/queued，已归零")

def _adopt_orphan_translation():
    """收留"孤儿译文"。

    pdf2zh 是我们起的**独立进程**：eggpaper 关掉/装新版本时它不会被一起带走，
    会接着把 mono.pdf 写完。但那条 running 状态被上面的清理归零了，
    用户回来看到的还是「全文翻译」——白译一场，还得再等两分钟。启动时看一眼文件在不在，
    在就直接认领成 done。只认「看起来完整」的文件（有 %%EOF 收尾），
    免得把写到一半就被杀掉的那份当成成品。
    """
    for row in db.list_papers():
        p = db.get_paper(row["id"]) or {}
        if p.get("translate_status") == "done" and (p.get("mono_path") or p.get("dual_path")):
            continue
        got = _claim_existing_translation(p["id"])
        if not got:
            continue
        _applog(f"认领上次没结算的译文：{p['id']} → {os.path.basename(got.get('mono') or got.get('dual'))}")

# ---------------- 设置 ----------------

@app.get("/api/settings")
def get_settings():
    cfg = config.load()
    p = cfg["provider"]
    return {"provider": {"base_url": p["base_url"], "model": p["model"],
                         "vision_model": p.get("vision_model", ""),
                         "has_key": bool(p["api_key"]),
                         # 完整 key 给前端：本地单机应用，key 本就明文躺在 config.yaml；
                         # 输入框默认 password 遮着，「显示」按钮切明文时能看到全 key——
                         # 之前只回前 6 字符加省略号，点显示还是一堆点，等于没显示。
                         # 保存端（PUT）对带 "…" 的回传不落盘，打码链路残留也写不坏。
                         "api_key": p["api_key"] or ""},
            "mock": cfg["mock"], "pdf2zh": cfg["pdf2zh"], "update": cfg.get("update", {}),
            "ui_lang": cfg.get("ui_lang", "zh"),
            "shot_save": cfg.get("shot_save", True),
            "analysis": cfg.get("analysis", {}),
            "data_dir": appinfo.data_dir()}

@app.put("/api/settings")
def put_settings(body: dict):
    # 在副本上改、存盘成功后才生效：否则 save 失败（盘满/被锁）时内存已被改掉，
    # 本会话用新值、重启回旧值，用户看到"没保存成功"却行为诡异
    cfg = copy.deepcopy(config.load())
    if "provider" in body:
        if not isinstance(body["provider"], dict):
            raise HTTPException(400, "模型服务那一栏格式不对")
        for k in ("base_url", "model", "vision_model"):
            if k in body["provider"]:
                v = body["provider"][k]
                cfg["provider"][k] = v.strip() if isinstance(v, str) else ""
        if isinstance(body["provider"].get("api_key"), str) and "…" not in body["provider"]["api_key"]:
            cfg["provider"]["api_key"] = body["provider"]["api_key"].strip()
    if "mock" in body:
        cfg["mock"] = bool(body["mock"])
    if body.get("ui_lang") in ("zh", "en"):
        cfg["ui_lang"] = body["ui_lang"]
    if "shot_save" in body:
        cfg["shot_save"] = bool(body["shot_save"])
    if isinstance(body.get("analysis"), dict) and "auto" in body["analysis"]:
        cfg.setdefault("analysis", {})["auto"] = bool(body["analysis"]["auto"])
    if "pdf2zh" in body:
        if not isinstance(body["pdf2zh"], dict):
            raise HTTPException(400, "翻译引擎那一栏格式不对")
        # 值一律收编成字符串/布尔：塞个数字进来，会在翻译启动的 .strip() 上炸出 500
        for k, v in body["pdf2zh"].items():
            if k in ("path", "service", "options", "deepl_key"):
                cfg["pdf2zh"][k] = v.strip() if isinstance(v, str) else ""
    if "update" in body:
        u = body["update"]
        if not isinstance(u, dict):
            raise HTTPException(400, "更新源那一栏格式不对")
        if isinstance(u.get("feed_url"), str):
            cfg["update"]["feed_url"] = u["feed_url"].strip()
        if "auto_check" in u:
            cfg["update"]["auto_check"] = bool(u["auto_check"])
    was_demo = _demo_mode(cfg)
    if cfg["provider"]["api_key"] and "mock" not in body and "provider" in body:
        cfg["mock"] = False
    config.save(cfg)
    if was_demo != _demo_mode(cfg):
        n = db.clear_ai_results()
        _applog(f"模型模式切换（演示→{'演示' if _demo_mode(cfg) else '真实'}）：清了 {n} 篇的缓存产物")
    return get_settings()

@app.get("/api/pdf2zh/engine")
def pdf2zh_engine(path: str = ""):
    """全文翻译引擎在哪、能不能跑。设置面板用它显示状态。

    「给别人装」的场景全靠这一条：那台电脑上 pdf2zh 装没装、装在 PATH 之外、
    还是装残了（.exe 在但包里没了）、或者是 1.9 旧版（没有术语锁定）——
    从前只能靠"点一下全文翻译看它报什么"，而报出来的是"bing 连不上"，指错方向。
    """
    cfg = config.load()
    want = (path or cfg["pdf2zh"].get("path") or "").strip()
    exe = translate_full.engine_path(want)
    ok, why = translate_full.engine_probe_cached(exe)
    ver = translate_full.engine_version(exe) if ok else ()
    # 升级链路：已装版本比钉住版本老就亮「有新版」。升级动作 = engine_install.start()
    # （多源下载/校验/原地换装/预热全是现成的），前端只需要一个旗标。
    pinned = engine_install.pinned_version()
    upgrade = False
    if ok and pinned and ver:
        try:
            upgrade = ver < tuple(int(x) for x in pinned.split("."))
        except ValueError:
            upgrade = False
    return {"ok": ok, "path": exe, "why": why, "configured": bool(want),
            "version": ".".join(map(str, ver)) if ver else "",
            "pinned": pinned, "upgrade": upgrade}

_dialog_times: list[float] = []
_dialog_gate = threading.Lock()

def _dialog_throttle():
    """原生对话框是模态的：本机接口无鉴权，恶意页面高频 POST 能把桌面糊满弹窗
    （用户点什么都是在替攻击者选路径）。1 秒最多 1 次、1 分钟最多 6 次。"""
    with _dialog_gate:
        now = time.monotonic()
        while _dialog_times and now - _dialog_times[0] > 60.0:
            _dialog_times.pop(0)
        if (_dialog_times and now - _dialog_times[-1] < 1.0) or len(_dialog_times) >= 6:
            raise HTTPException(429, "操作太频繁，稍候再试")
        _dialog_times.append(now)

@app.post("/api/data/pick")
def data_pick():
    """弹原生目录选择框，返回选中的路径（取消返回空串）。"""
    _dialog_throttle()
    return {"path": picker.pick_folder("选择数据目录")}

@app.post("/api/data/location")
def set_data_location(body: dict):
    """改数据目录：写指针，重启后 migrate_if_needed() 自动把数据整体搬过去。

    当场能拦的都拦下（绝对路径、非程序目录、翻译进行中）；剩下的交给启动时的迁移。
    """
    target = str((body or {}).get("path") or "").strip().strip('"')
    if not target:
        raise HTTPException(400, "路径为空")
    if not os.path.isabs(target):
        raise HTTPException(400, "要填完整路径（如 D:\Papers\Eggpaper）")
    target = os.path.abspath(target)
    low = target.lower()
    if low.startswith(os.path.join(appinfo.exe_dir(), "_internal").lower() + os.sep):
        raise HTTPException(400, "不能放在程序目录里")
    if any(running["status"] == "running" for running in translate_full.JOBS.values()):
        raise HTTPException(400, "全文翻译正在进行，结束后再迁")
    probe = os.path.join(target, ".probe")
    try:
        os.makedirs(target, exist_ok=True)
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
    except Exception as e:
        raise HTTPException(400, f"这个位置写不进去（{type(e).__name__}）")
    appinfo.write_pointer(target)
    return {"ok": True, "restart": True, "path": target}

# ---------------- 全库备份（换机迁移：导出的 zip 就是一份数据目录） ----------------

# 这些不进备份：home/ 是引擎的资产缓存（换机后引擎自己重新解包），db 的 wal/shm 陪跑文件
# 用快照替代；主库文件本身也由快照代表——walk 里再撞见同名条目会把活库盖到快照上头
# （os._exit 退出不 checkpoint，WAL 里最近的提交只在快照里），所以一并跳过。
_BACKUP_SKIP_DIRS = {"home"}
_BACKUP_SKIP_FILES = {"eggpaper.db", "eggpaper.db-wal", "eggpaper.db-shm", "pending_restore.json"}
# 恢复解包的跳过名单：快照里的 eggpaper.db **必须**落地（跳过它落地就是个空库），
# 只有陪跑文件不进位
_RESTORE_SKIP_FILES = _BACKUP_SKIP_FILES - {"eggpaper.db"}

# key 脱敏在 config.strip_secrets（password 键一并置空）
_strip_secrets = config.strip_secrets

def _backup_zip_stream():
    """整个数据目录打成一个 zip（STORED：PDF 本来就是压缩格式，再压一遍白烧 CPU）。
    db 不能直接读文件——写入进行中 WAL 里还有没合页的账，用 SQLite 的 backup API
    出一份完整快照。"""
    import sqlite3
    import zipfile
    tmp = tempfile.TemporaryFile()
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
        # 快照名带随机后缀：按 pid 命名的话同进程并发导出会用同一路径互拆
        snap = os.path.join(tempfile.gettempdir(), f"eggpaper_db_snap_{os.getpid()}_{time.time_ns()}.tmp")
        src = sqlite3.connect(db.DB_PATH)
        try:
            dst = sqlite3.connect(snap)
            with dst:
                src.backup(dst)
            dst.close()
            z.write(snap, "eggpaper.db")
        finally:
            src.close()
            try:
                os.remove(snap)
            except OSError:
                pass
        for root, dirs, files in os.walk(config.DATA_DIR):
            dirs[:] = [d for d in dirs if d not in _BACKUP_SKIP_DIRS]
            for fn in files:
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, config.DATA_DIR).replace("\\", "/")
                if rel in _BACKUP_SKIP_FILES or rel.endswith(".tmp") or rel.endswith(".part"):
                    continue
                try:
                    if rel == "config.yaml" or rel.startswith("config.yaml."):
                        # .bad/.bak 等残副本同样可能带 key，一并脱敏
                        z.writestr(rel, _strip_secrets(full))
                    else:
                        z.write(full, rel)
                except OSError:
                    pass     # 导出途中被删/被翻译收尾清掉的文件：跳过，别让整场导出 500
    tmp.seek(0)
    try:
        while True:
            chunk = tmp.read(1 << 20)
            if not chunk:
                break
            yield chunk
    finally:
        tmp.close()

@app.get("/api/backup/export")
def backup_export():
    name = time.strftime("eggpaper-backup-%Y%m%d.zip")
    return StreamingResponse(_backup_zip_stream(), media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})

@app.post("/api/backup/pick")
def backup_pick():
    """弹原生文件框选备份 zip（本地服务代劳浏览器拿不到的路径），取消返回空串。"""
    _dialog_throttle()
    return {"path": picker.pick_file(
        "选择 eggpaper 备份文件", "eggpaper 备份\0*.zip\0所有文件\0*.*\0")}

@app.post("/api/backup/restore")
def backup_restore(body: dict):
    """从备份 zip 恢复。**重启后生效**：db 此刻开着，换不动整个数据目录；
    先解包到旁边的暂存目录并留标记，下次启动 _apply_pending_restore() 落地。

    这里校验两道：是个能读的 zip、里面有 eggpaper.db（防选错文件把文库清了）。
    解包时跳过 zip slip（路径里带 .. 的条目）和排除名单。
    全程持锁：并发恢复会互相拆掉对方正在写的暂存目录。
    """
    src = str((body or {}).get("path") or "").strip().strip('"')
    _stage_restore(src)
    return {"ok": True, "restart": True}

def _stage_restore(src: str):
    """备份恢复的落地准备：校验 + 解包到暂存 + 写标记；失败抛 HTTPException。"""
    with _restore_lock:
        if not src or not os.path.isfile(src):
            raise HTTPException(404, "找不到这个文件")
        if not src.lower().endswith(".zip"):
            raise HTTPException(400, "要选备份的 .zip 文件")
        try:
            z = zipfile.ZipFile(src)
        except zipfile.BadZipFile:
            raise HTTPException(400, "这个 zip 读不了")
        with z:
            names = z.namelist()
            if "eggpaper.db" not in names:
                raise HTTPException(400, "这不是 eggpaper 的备份文件")
            staged = config.DATA_DIR + ".restore"
            # 解包前先估总量：塞满磁盘会把正在运行的库一起拖死
            need = sum(i.file_size for i in z.infolist())
            free = shutil.disk_usage(config.DATA_DIR).free
            if need > free * 0.9:
                raise HTTPException(400, f"磁盘空间不足，备份约需 {need // (1 << 20)} MB")
            shutil.rmtree(staged, ignore_errors=True)
            os.makedirs(staged)
            for info in z.infolist():
                parts = info.filename.replace("\\", "/").split("/")
                if not info.filename.endswith("/") and parts[0] not in _BACKUP_SKIP_DIRS \
                        and ".." not in parts and "/".join(parts) not in _RESTORE_SKIP_FILES:
                    target = os.path.join(staged, *parts)
                    try:
                        os.makedirs(os.path.dirname(target), exist_ok=True)
                        with z.open(info) as fsrc, open(target, "wb") as fdst:
                            shutil.copyfileobj(fsrc, fdst, 1 << 20)
                    except OSError as e:
                        # 备份是在别的系统/别的路径规则下打的：条目名可能撞上 Windows
                        # 的非法字符（? | 保留名 CON/…）或超长路径，open 当场 OSError。
                        # 这不是"服务器坏了"，是这份备份这份文件解不开——400 人话 +
                        # 把半截暂存目录收掉（留着它，下次恢复会从一堆残骸上再解一遍）
                        shutil.rmtree(staged, ignore_errors=True)
                        raise HTTPException(400, f"备份里有这份系统解不开的文件名"
                                                 f"（{parts[-1][:60]}）：{_human_msg(e)}")
    try:
        with open(os.path.join(config.DATA_DIR, "pending_restore.json"), "w", encoding="utf-8") as f:
            json.dump({"staged": staged}, f)
    except OSError as e:
        raise HTTPException(500, f"写恢复标记失败：{_human_msg(e)}")

def _no_install_while_translating():
    """换装会把引擎目录从正在跑的 pdf2zh 脚下 rename 走：模块懒加载失败、批次神秘报错，
    exe 被占用还会在盘上留下删不掉的 .old（升级链路的其他病灶都从这儿长出来）。
    正在翻译就拒绝，口径与"改数据目录要先停下载"一致：顺序得由用户定，但得说人话。"""
    for jpid, j in list(translate_full.JOBS.items()):
        if (j or {}).get("status") in ("running", "queued"):
            raise HTTPException(400, "正在全文翻译，结束后再安装/升级引擎")

@app.post("/api/pdf2zh/install")
def pdf2zh_install(body: dict = None):
    """一键把引擎装到用户机器上。不给 url 就走内置源表（更新源同源 → 镜像 → 官方直连，
    自动换源+续传+校验，见 engine_install）；给了 url 就只用这一个（测试/局域网用）。"""
    url = str(((body or {}).get("url") or "")).strip()
    _no_install_while_translating()
    return engine_install.start(url)

@app.get("/api/pdf2zh/install-status")
def pdf2zh_install_status():
    return engine_install.status()

@app.post("/api/pdf2zh/install-cancel")
def pdf2zh_install_cancel():
    """喊停下载。断点留在 engines/pdf2zh.partial/，下次从断点接着下。"""
    engine_install.cancel()
    return engine_install.status()

@app.post("/api/pdf2zh/install-from-file")
async def pdf2zh_install_from_file(file: UploadFile = File(...)):
    """从本地 zip 装引擎：网络到不了 GitHub 时，这条路才是真正可用的分发方式——
    下好一份引擎包，和安装包一起发给别人，对方在这里选那个文件即可（零基础）。"""
    if not (file.filename or "").lower().endswith(".zip"):
        raise HTTPException(400, "要选 .zip 文件")
    _no_install_while_translating()
    updir = engine_install.install_dir() + ".upload"
    os.makedirs(updir, exist_ok=True)
    # 随机名：固定 engine.zip 的话，并发上传会当场截断正在解压的那一份
    tmp = os.path.join(updir, f"engine-{uuid.uuid4().hex[:8]}.zip")
    size = 0
    try:
        with open(tmp, "wb") as f:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > 2 * 1024 * 1024 * 1024:
                    raise HTTPException(400, "文件过大")
                f.write(chunk)
    except Exception:
        fsutil.rm_quiet(tmp)        # 超限/写一半失败：两 GB 的残骸别一直躺在盘上
        raise
    return engine_install.start_from_zip(tmp, cleanup=True)

@app.post("/api/settings/test")
def test_settings():
    return llm.test_connection()

# ---------------- 版本与更新 ----------------

@app.get("/api/version")
def version_info():
    return {"version": appinfo.version(), "packaged": update.is_packaged(),
            "data_dir": config.DATA_DIR, "quitting": _QUITTING["user"]}

def _cache_hours(cfg: dict) -> float:
    """更新缓存小时数来自用户配置文件：空/坏值/"6h" 这种写法都不该让"查更新"整个 500，
    坏值退回默认 6 小时（合理范围外也退回——负数和天文数字都会让缓存逻辑失真）。"""
    try:
        h = float(cfg.get("cache_hours") or 6)
    except (TypeError, ValueError):
        return 6.0
    return h if 0 < h <= 24 * 30 else 6.0

@app.get("/api/update/check")
def update_check(force: bool = False):
    """查更新源。force=false 时只读缓存不联网（打开软件时的那次安静探测走这条）。
    更新源留空 = 用内置的 Gitee 源（用户零配置）；想彻底关掉检查用「自动检查」开关。"""
    cfg = config.load().get("update", {})
    feed = str(cfg.get("feed_url") or config.DEFAULTS["update"]["feed_url"])
    return update.check(feed, force=force, cache_hours=_cache_hours(cfg))

@app.post("/api/update/download")
def update_download(body: dict):
    """下载安装包。url 只认更新源 check() 给出的那条：本机接口无鉴权（任意网页可
    POST），收调用方自给的 url 等于"引导下载任意 exe"——install 有目录护栏，
    在 download 这头把链截断。sha256/size 同样以更新源为准，不信任调用方。"""
    cfg = config.load().get("update", {})
    feed = str(cfg.get("feed_url") or config.DEFAULTS["update"]["feed_url"])
    info = update.check(feed, force=False, cache_hours=_cache_hours(cfg))
    url = (body or {}).get("url") or ""
    if not (info.get("ok") and info.get("url")) or url != info["url"]:
        raise HTTPException(400, "下载地址与更新源不符，请重试")
    update.start_download(info["url"], (info.get("sha256") or "").lower(), int(info.get("size") or 0))
    return update.progress()

@app.get("/api/update/progress")
def update_progress():
    return update.progress()

@app.post("/api/update/install")
def update_install(body: dict):
    """把下好的安装包交给系统，然后本进程退出（安装器要替换的正是它占着的文件）。"""
    path = (body or {}).get("path") or update.progress().get("path") or ""
    if not update.install(path):
        raise HTTPException(400, "安装包缺失或无法启动，请重新下载")
    return {"ok": True}

@app.post("/api/update/reveal")
def update_reveal(body: dict):
    update.open_folder((body or {}).get("path") or update.progress().get("path") or "")
    return {"ok": True}

def _my_port() -> int:
    """本进程到底在哪个端口上。桌面入口会按 8430→8431→8432 找第一个空闲的，
    instance.json 里记着真实端口（desktop.py 写的），读不到再退回 8430——
    写死 8430 的话，服务落在 8431 时"在独立窗口打开"开出的是别人家的页面。"""
    try:
        with open(os.path.join(os.path.dirname(config.DATA_DIR), "instance.json"), encoding="utf-8") as f:
            p = int(json.load(f).get("port") or 0)
        if p:
            return p
    except Exception as e:
        _applog(f"读 instance.json 失败（退回默认端口 {DEFAULT_PORT}）：{type(e).__name__}: {str(e)[:120]}")
    return DEFAULT_PORT

@app.post("/api/window")
def open_native_window():
    """把界面开成一个没有浏览器边框的独立窗口（界面里点一下就多一个"应用窗口"）。
    开发模式下返回失败原因即可，不必假装成功。"""
    import window as winmod
    how = winmod.open_window(f"http://127.0.0.1:{_my_port()}/")
    if not how:
        raise HTTPException(503, "没找到可用的浏览器（Edge/Chrome）")
    return {"ok": True, "how": how}

@app.post("/api/screenshot")
def take_screenshot(body: dict = None):
    """抓 eggpaper 窗口当前画面回 PNG（base64，前端按选区裁剪后写剪贴板）。
    body 带前端量好的浏览器镶边（标题栏/边框，CSS px）就裁成纯视口，选区坐标
    从此和 client 坐标一一对应。"""
    import screenshot as shot
    try:
        png = shot.capture_window()
        b = body or {}
        if "chrome_top" in b or "border" in b:
            png = shot.crop_viewport(png, b.get("chrome_top"), b.get("border"), b.get("dpr"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"截图失败：{e}")
    return {"png": shot.to_base64(png)}

@app.post("/api/screenshot/save")
def save_screenshot(body: dict):
    """把前端裁好的选区 PNG 落盘（统一命名 egg_时间_标题_页码）。

    入参是前端 canvas 裁出来的 PNG base64。坏 base64（长度不对/掺了 data URL 前缀）
    或乱传类型以前会把 AttributeError/binascii 冒成 500——前端只可能发对的，
    但这属于"客户端输入问题"，统一收成 400 人话。PNG 签名要核：这个端点写出的
    文件必是 .png，不能把任意字节顶着 .png 扩展名落盘。"""
    import screenshot as shot
    b = body or {}
    raw = b.get("png")
    raw = raw if isinstance(raw, str) else ""
    try:
        data = base64.b64decode(raw)
    except Exception:
        raise HTTPException(400, "图片内容不是有效的 PNG 数据")
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise HTTPException(400, "图片内容不是有效的 PNG 数据" if data else "没有图片内容")
    title = b.get("title")
    try:
        name = shot.save(data, title if isinstance(title, str) else "", b.get("page") or 0)
    except OSError as e:
        raise HTTPException(500, f"存不下去：{e}")
    return {"name": name}

@app.post("/api/screenshot/folder")
def open_screenshot_folder():
    """打开截图目录（资源管理器）；目录此刻还没建过就现建一个。"""
    import screenshot as shot
    d = shot.folder()
    os.makedirs(d, exist_ok=True)
    os.startfile(d)     # noqa: S606 - 本机服务代开资源管理器
    return {"ok": True}

_QUITTING = {"user": False}

@app.post("/api/quit")
def quit_app(body: dict = None):
    """退出整个程序（打包版：没有控制台窗口，用户需要一个"关掉它"的地方）。

    body.reason = "user"（设置里的退出按钮）时通知所有页面自行关闭；
    "upgrade"（desktop 升级接管请旧实例让位）不通知——旧页面要留给
    版本轮询自动刷新到新实例，弹"已退出"只会打扰。"""
    if not update.is_packaged():
        return {"ok": False, "reason": "开发模式：直接在终端里 Ctrl+C"}
    if (body or {}).get("reason") != "upgrade":
        _QUITTING["user"] = True
    def bye():
        # 宽限 4.5 秒：页面 3 秒轮询一次 open-request，3.2 只留 0.2 秒余量——
        # 刚轮询过一圈的页面会赶不上这一拍，托盘退出就留着满屏尸体页
        time.sleep(4.5)
        os._exit(0)
    threading.Thread(target=bye, daemon=True).start()
    return {"ok": True}

# ---------------- 论文 ----------------

@app.get("/api/papers")
def papers():
    return db.list_papers()

@app.get("/api/search")
def search_everywhere(q: str = ""):
    """全库内容检索（粗粒度）：一眼卡/七问/主张/问答/正文，命中按篇聚合。"""
    return db.search_content((q or "")[:200])

def _ingest(pid: str, filename: str, path: str, pdf_hash: str = "") -> dict:
    """把已经落在 papers/<pid>/ 里的一份 PDF 建进库（上传与"双击打开"两条路共用）。

    解析失败要收拾干净：留着半篇没有段落的"论文"，用户点开只能看见一个空书架。
    """
    try:
        page0 = pdfparse.page0_lines(path)     # 一趟扫描喂两个提取器：元数据只开一遍文档
        title = pdfparse.extract_title(path, page0)
        authors = pdfparse.extract_authors(path, page0)
        paras = pdfparse.extract_paragraphs(path)
        n_pages = pdfparse.page_count(path)    # 用完就关：裸开会让 Windows 短暂锁着文件
    except Exception as e:
        try:
            os.remove(path)
        except OSError:
            pass
        raise HTTPException(400, f"PDF 读不了：{_human_msg(e)}")
    db.create_paper(pid, filename, title, path, n_pages, authors)
    # 建行之后的每一步都可能抛（写段落/判类型/起析读线程）。调用方在异常里只删了
    # papers/<pid>/ 目录，行还在库里——书架上因此会留下一篇"点开只有 PDF_GONE"的
    # 僵尸论文（析读还会报"没有文字层"）。这一整段包起来：失败连行一起撤。
    try:
        return _ingest_finish(pid, path, pdf_hash, paras)
    except Exception:
        db.purge_paper(pid)
        raise


def _ingest_finish(pid: str, path: str, pdf_hash: str, paras: list) -> dict:
    """建行之后的收尾（对齐可读名、写段落、判类型、按设置起析读/OCR）。"""
    if pdf_hash:
        db.update_paper(pid, pdf_hash=pdf_hash)
    try:
        _align_or_late(pid)   # 标题/作者刚提取完：夹名、文件名立刻对齐可读名（挪不动由保洁追平）
    except Exception as e:
        _applog(f"导入对齐 {pid} 失败（标题照存，下次启动的迁移会再对一次）：{_human_msg(e)}")
    db.replace_paragraphs(pid, paras)
    _ensure_paper_type(pid)
    crossref.kick(pid, on_update=lambda _pid=pid: _align_names(_pid))     # 按首页 DOI 联网补元数据（后台线程，失败安静；补到作者/年份就把夹名再对齐一拍）
    row = db.get_paper(pid)
    db.update_paper(pid, last_read_at=db._now())
    auto = (config.load().get("analysis") or {}).get("auto")
    auto = True if auto is None else bool(auto)     # 设置里没这一项时按老行为来：自动析读
    if paras:
        if auto:
            _enqueue_analysis(pid, paras)
        else:
            # 「导入即析读」被关掉：只入库不花 token，想读时点一下「析读」（analyze 端点）
            db.update_paper(pid, analysis_status="none")
    elif ocr.available():
        # 扫描件：无感补一段 OCR（后台线程），识别完段落入库；要不要接着析读看设置。
        # 状态借「排队通读中」显示——用户不需要知道中间多了道识别的工序。
        db.update_paper(pid, analysis_status="queued", analysis_error=None)
        threading.Thread(target=_ocr_then_analyze, args=(pid, path, auto), daemon=True).start()
    else:
        # 没有文字层、OCR 引擎也没装：这不是"已析读"，如实报空态（装引擎后重新析读即可）。
        # 报 done 是假成功——书架显示"已析读"、点进去却什么都没有
        db.update_paper(pid, analysis_status="error",
                        analysis_error="这份 PDF 没有文字层（多半是扫描件），装上 OCR 引擎后重新析读即可")
    return {"paper": row, "n_paragraphs": len(paras),
            "n_captions": sum(1 for p in paras if p["caption"]),
            "no_text": not paras}


def _analysis_reset(pid: str, where: str = ""):
    """取消/中断的收尾动作本身：清旗子、状态归"没做过"、按需记一笔日志。

    篇已被删就直接收手（状态行都没了，别把死篇写回库；旗子随篇作废，没人再查它）。
    OCR 与析读两条路共用这一份（where 为空 = 静默收尾，OCR 的两处就是这么用的）。
    """
    if _pid_gone(pid):
        return
    _cancel_clear("analysis", pid)
    db.update_paper(pid, analysis_status="none", analysis_error=None)
    if where:
        _applog(f"析读 {pid}: {where}")

def _analysis_cancelled(pid: str, where: str) -> bool:
    """析读的取消闸：请求了取消就收尾并写一笔日志。
    返回 True 时调用方直接 return——已生成的部分按各阶段自己的口径保留。"""
    if not _cancel_requested("analysis", pid):
        return False
    _analysis_reset(pid, where)
    return True


def _ocr_then_analyze(pid: str, path: str, auto: bool = True):
    """扫描件的隐形后半段：OCR → 段落入库 → 排析读。失败落 analysis_error，说人话。

    auto=False（设置关掉了导入即析读）：OCR 照跑——没有文字层连翻译/划词都做不了，
    这道工序不属于析读；识别完只把状态归位，不排队花 token 的析读。

    登记成 ("analysis", pid) 的在跑任务：analysis 端点有"状态说 queued 但进程里没活 =
    上次被中断"的惰性检测，OCR 这一段不登记的话刚导入的篇会被它误杀回 none。
    这里再登记一次是给**导入路径**兜底（导入时线程由 import 自己起，端点里没有登记点）；
    analyze 端点已经在锁内登记过了，重复 add 幂等无害。
    取消旗在 OCR 里**逐页**生效（识别是分钟级的，整本跑完才看旗等于没给取消）；
    _job_done 挪到最后的 finally——先撤登记再排析读的话，惰性检测会在毫秒窗里
    把刚排上的 queued 误归零。
    """
    with _q_lock:
        _job_start("analysis", pid)
    t0 = time.time()
    try:
        try:
            paras = ocr.ocr_pdf(
                path, should_stop=lambda: _pid_gone(pid) or _cancel_requested("analysis", pid))
        except ocr.OcrStopped:
            _analysis_reset(pid)      # OCR 前后探到取消/删篇：旗子收掉别留给下一次误杀
            return
        if not paras:
            raise ValueError("没认出文字")
        _applog(f"OCR {pid}: {len(paras)} 段，{time.time() - t0:.1f}s")
        if _pid_gone(pid) or _cancel_requested("analysis", pid):
            _analysis_reset(pid)
            return
        db.replace_paragraphs(pid, paras)
        _ensure_paper_type(pid)
        if auto:
            _enqueue_analysis(pid, paras)
        else:
            db.update_paper(pid, analysis_status="none")
    except ocr.OcrUnavailable as e:
        db.update_paper(pid, analysis_status="error", analysis_error=str(e))
    except Exception:
        _applog(f"OCR {pid} 失败：{traceback.format_exc(limit=3)}")
        db.update_paper(pid, analysis_status="error",
                        analysis_error="扫描件文字识别没成功，可能太糊")
    finally:
        _job_done("analysis", pid)

def _new_paper_dir(name: str):
    """给一篇新论文建库内目录，返回 (pid, pid_dir, base)——上传与路径导入共用。
    名字先按上传文件名的安全短名起（导入收尾 _align_names 会再对齐到
    「作者 - 年份 - 标题」），撞名追加 (2)(3)：盘上名字里从此没有 pid。"""
    pid = db.new_id()
    base = _slug(os.path.splitext(name)[0], cap=64) or "paper"
    want, i = base, 2
    while os.path.exists(os.path.join(PAPERS_DIR, want)) and i <= 9:
        want = f"{base} ({i})"
        i += 1
    pid_dir = os.path.join(PAPERS_DIR, want)
    os.makedirs(pid_dir, exist_ok=True)
    with _dir_lock:
        _dir_cache[pid] = os.path.basename(pid_dir)
    return pid, pid_dir, base

def _attach_same_title(out: dict, pid: str) -> dict:
    """导入收尾：发现库里已有同题论文就附在响应里，前端提示用户。"""
    same = db.find_same_title((out.get("paper") or {}).get("title") or "", exclude_pid=pid)
    if same:
        out["same_title"] = db.get_paper(same)
    return out

_MAX_UPLOAD = 500 * 1024 * 1024   # 论文 PDF 的合理上限：防手滑上传整个硬盘，也防内存被顶穿

@app.post("/api/papers")
async def upload(request: Request, file: UploadFile = File(...)):
    # 分块落盘：以前 chunks 攒满再 b"".join，峰值两份 500MB 驻留在**事件循环线程**上，
    # 攒的那一拍整个服务（含正在流的问答）一起停摆。现在 4MB 一块边收边写临时文件。
    # content-length 的预检在 _cap_upload_size 中间件里（要赶在 multipart 解析之前）。
    tmp = os.path.join(config.DATA_DIR, f".upload-{uuid.uuid4().hex}.tmp")
    size, head = 0, b""
    try:
        with open(tmp, "wb") as f:
            while True:
                chunk = await file.read(1 << 22)
                if not chunk:
                    break
                size += len(chunk)
                if size > _MAX_UPLOAD:
                    raise HTTPException(400, "PDF 太大（超过 500MB），先拆分或压缩再导入")
                if not head:
                    head = chunk[:16]
                f.write(chunk)
    except HTTPException:
        fsutil.rm_quiet(tmp)
        raise
    except OSError:
        fsutil.rm_quiet(tmp)
        raise HTTPException(500, "PDF 没写进库里（磁盘满了或被占用）")
    if head[:4] != b"%PDF":
        fsutil.rm_quiet(tmp)
        raise HTTPException(400, "不是 PDF 文件")
    if size < 256:
        fsutil.rm_quiet(tmp)
        raise HTTPException(400, "文件太小，不像完整的 PDF")
    name = (file.filename or "paper.pdf").split("/")[-1].split("\\")[-1]

    def _import():
        with _import_lock:
            pdf_hash = fsutil.sha256_file(tmp, quiet=True)   # 流式算哈希：内存只占一块 1MB 缓冲
            dup = db.find_duplicate(name, size, pdf_hash)
            if dup:
                fsutil.rm_quiet(tmp)
                paras = db.get_paragraphs(dup)
                return {"paper": db.get_paper(dup), "n_paragraphs": len(paras),
                        "n_captions": sum(1 for pp in paras if pp.get("caption")),
                        "no_text": not paras, "duplicate": True}
            pid, pid_dir, base = _new_paper_dir(name)
            path = os.path.join(pid_dir, f"{base}.pdf")   # 库内自存一份：删原件、挪原件都不影响
            try:
                os.replace(tmp, path)                     # 同盘改名，瞬时完成
            except OSError:
                _discard_paper_dir(pid, pid_dir)   # 盘满写不下：别留空论文目录
                raise HTTPException(500, "PDF 没写进库里（磁盘满了或被占用）")
            try:
                out = _ingest(pid, name, path, pdf_hash)
            except Exception:
                _discard_paper_dir(pid, pid_dir)   # 坏文件别留空论文目录
                raise
            return _attach_same_title(out, pid)

    try:
        return await run_in_threadpool(_import)
    except Exception:
        fsutil.rm_quiet(tmp)      # _import 没接住的失败（判重前的异常等）：临时文件别留在数据目录
        raise

@app.post("/api/papers/import-path")
def import_path(body: dict):
    """从本机路径导入 PDF：双击 PDF、右键「用 eggpaper 打开」走这条。

    和上传的区别是**不经过浏览器**——文件已经在磁盘上，直接复制进库。
    同一个文件双击两次不该得到两篇：同名同大小（或内容指纹相同）就当成同一份，直接打开它。
    """
    src = (body or {}).get("path")
    src = src if isinstance(src, str) else ""
    src = os.path.expanduser(_utf8_safe(src).strip().strip('"'))
    if not src or not os.path.isfile(src):
        raise HTTPException(404, f"找不到这个文件：{src or '(空)'}")
    if not src.lower().endswith(".pdf"):
        raise HTTPException(400, "eggpaper 只认 PDF")
    name, size = os.path.basename(src), os.path.getsize(src)
    with _import_lock:
        # 建目录必须与判重、落盘同在一把锁里（与上传路径同一写法）：目录名是按撞名
        # 现找的，锁外 check-then-act 会让两个并存请求拿到同一个目录，失败方
        # _discard_paper_dir 一把 rmtree 掉赢家刚落盘的 PDF
        pid, pid_dir, base = _new_paper_dir(name)
        dest = os.path.join(pid_dir, f"{base}.pdf")
        try:
            pdf_hash = fsutil.sha256_file(src, dest)   # 边复制边算指纹：别把几百 MB 的文件整读两遍
        except OSError as e:
            _discard_paper_dir(pid, pid_dir)   # 复制不出来（盘满/源被锁）：别留半截目录
            raise HTTPException(400, f"复制不出来：{_human_msg(e)}")
        dup = db.find_duplicate(name, size, pdf_hash)
        if dup:
            fsutil.rm_quiet(dest)
            _discard_paper_dir(pid, pid_dir)   # 刚建的空文件夹顺手收掉
            _pending_open_push(dup)
            return {"paper": db.get_paper(dup), "duplicate": True}
        try:
            out = _ingest(pid, name, dest, pdf_hash)
        except Exception:
            _discard_paper_dir(pid, pid_dir)   # 坏文件别留空论文目录
            raise
        _attach_same_title(out, pid)
    _pending_open_push(pid)
    return out

# 待打开的论文：**保持顺序的队列**。桌面入口把「双击的每一个文件」都 POST 给同一个实例
# （一次选中两个 PDF 双击就是两条），窗口侧每 3 秒轮询取一条。原来只有一个槽位、
# 后写覆盖：两个窗口同时轮询时一个拿到、另一个永远拿不到（"另一篇不弹"），
# 而"最新的那条覆盖前一条"还会让先导入那篇彻底丢掉打开机会。
# 上限 3 条：页面关着时的积压不做无限回放（回来时连开十几篇是灾难）。
_pending_order = collections.deque(maxlen=3)


def _pending_open_push(pid):
    _pending_order.append(pid)


def _pending_open_pop():
    try:
        return _pending_order.popleft()
    except IndexError:
        return None

@app.post("/api/papers/{pid}/supersede")
def supersede_paper(pid: str, body: dict):
    """版本升级：pid（刚导入的那份）顶掉旧篇 old_pid——软资产跟迁、旧篇删除。"""
    old_pid = (body or {}).get("old_pid")
    old_pid = _utf8_safe(old_pid) if isinstance(old_pid, str) else ""
    old = _paper_or_404(old_pid)
    new = _paper_or_404(pid)
    if old["id"] == pid:
        raise HTTPException(400, "新旧是同一篇")
    for row in (new, old):
        if row and row["translate_status"] == "running":
            raise HTTPException(400, "这篇正在全文翻译，结束后再替换")
    with _deleted_lock:
        # 旧篇从现在起视同已删：析读/七问线程的 _pid_gone 护栏全部生效，
        # 不再往被替换掉的 pid 白烧 LLM 或插回孤儿行
        _deleted_pids.add(old["id"])
    _cancel_request("analysis", old["id"])
    with _lines_probe_lock:
        _lines_probe_done.discard(old["id"])
    try:
        with _key_lock("translate:" + old["id"]):
            old_dir = paper_dir(old["id"])   # **purge 前**算好：DB 行一删，paper_dir 只剩
            # 兜底解析（读不到 path 字段），按可读名建的目录它认不出来——旧 PDF/译文
            # 就永远留在盘上，还占着干净名让新篇升不回去（delete_paper 同款教训）
            if translate_full.cancel(old["id"], old_dir):
                _applog(f"替换 {old['id']}：终止了还在跑的全文翻译")
            stats = db.supersede(pid, old["id"])
            with _dir_lock:
                _dir_cache.pop(old["id"], None)
            shutil.rmtree(old_dir, ignore_errors=True)
            _applog(f"替换 {old['id']}：旧目录 {old_dir} "
                    f"{'已清掉' if not os.path.exists(old_dir) else '删不动，交后台保洁'}"
                    f"（新版会接着升回干净名）")
            if os.path.exists(old_dir):
                _late_rm_paper_dir(old["id"], old, old_dir)   # 杀软/索引器攥着：交后台保洁
            translate_full.JOBS.pop(old["id"], None)
            _forget_pid_locks(old["id"])
    except Exception:
        # 迁移没成（DB 整体回滚了）：旧篇还在，把"视同已删"的护栏撤回——
        # 不撤的话这篇在本进程里从此 _pid_gone，新生成的析读/七问结果会被永久静默丢弃
        with _deleted_lock:
            _deleted_pids.discard(old["id"])
        raise
    try:
        _align_names(pid)   # 同题旧版让位腾出了干净名：新篇立刻从 `… (2)` 升回干净名
    except Exception as e:
        _applog(f"替换后对齐 {pid} 失败（名字下轮启动会再对一次）：{_human_msg(e)}")
    return stats

@app.get("/api/open-request")
def open_request():
    return {"pid": _pending_open_pop(), "quitting": _QUITTING["user"]}

@app.get("/api/zotero/items")
def zotero_items():
    """Zotero 桌面版的本地库清单（只读）。连接失败给到界面的是人话，不是堆栈。"""
    try:
        return {"items": zotero.list_items()}
    except zotero.ZoteroUnavailable as e:
        raise HTTPException(503, str(e))

@app.post("/api/papers/{pid}/meta")
def paper_meta(pid: str, body: dict):
    """回填可信元数据（Zotero 导入后）：只覆盖给了值的字段。"""
    _paper_or_404(pid)
    b = body or {}
    db.set_paper_meta(pid, _utf8_safe(str(b.get("title") or "")),
                      _utf8_safe(str(b.get("authors") or "")),
                      _utf8_safe(str(b.get("year") or "")))
    try:
        _align_or_late(pid)   # 可信元数据回填完，盘上名字跟着对齐
    except Exception as e:
        _applog(f"元数据回填后对齐 {pid} 失败（名字下轮启动会再对一次）：{_human_msg(e)}")
    return {"paper": db.get_paper(pid)}

@app.post("/api/papers/{pid}/title")
def paper_title(pid: str, body: dict):
    """手动改标题（首页识别不出像样标题时的补手工）：改库、文件夹名跟着对齐。

    忙碌闸：析读/翻译握着这个目录里的路径，改到一半目录被挪，哪边都收不了场——
    一律拒绝，等跑完再改。"""
    p = _paper_or_404(pid)
    title = _utf8_safe(str((body or {}).get("title") or "")).strip()
    if not title:
        raise HTTPException(400, "标题不能是空的")
    if (p["analysis_status"] in ("running", "queued") and _analysis_inflight(pid)) \
            or p["translate_status"] in ("running", "queued") \
            or translate_full.job(pid)["status"] in ("running", "queued"):
        raise HTTPException(400, "有析读或翻译在跑，结束后再改")
    db.set_paper_meta(pid, title=title)
    renamed = _align_or_late(pid)
    if renamed:
        _applog(f"标题 {pid}: 夹名/文件名已对齐新标题")
    return {"paper": db.get_paper(pid), "renamed": renamed}

def _paper_or_404(pid: str) -> dict:
    p = db.get_paper(pid)
    if not p:
        _applog(f"404 论文不存在：pid={pid}（多半是别的窗口删过、页面列表还没跟新）")
        raise HTTPException(404, "论文不存在")
    return p

@app.get("/api/library/version")
def library_version():
    """文库版本号：篇数 + 最新导入时间。开着的页面每几秒对一次账，
    别的窗口导入/删除了，这边列表自动跟新——不再拿着陈旧列表去撞「论文不存在」。"""
    n, mx = db.q("SELECT COUNT(*), IFNULL(MAX(created_at),'') FROM papers")[0]
    return {"v": f"{n}:{mx}"}

NO_TEXT = "这份 PDF 没有文字层（多半是扫描件），析读和提问用不了"
PDF_GONE = "PDF 不在原位了，拖回窗口重新导入即可，批注不会丢。"

def _require_paras(pid: str) -> None:
    """扫描件没有文字层：让它过一个"请求模型、等半天、返回胡话"的流程是最坏的选择，
    直接说清楚做不到什么、还能做什么。"""
    if not db.has_paragraphs(pid):
        raise HTTPException(400, NO_TEXT)


_SQLITE_INT_MAX = 2 ** 63 - 1

_LONE_SURR = re.compile("[\ud800-\udfff]")

def _utf8_safe(s: str) -> str:
    """剥掉孤立代理字符（\\ud800–\\udfff）：内存 str 里合法、UTF-8 编不出来，
    存 SQLite / 当文件名 / 写日志都会 UnicodeEncodeError 冒 500。正常打字不会带，
    粘贴别处内容或直调接口才见得到——写入口剥干净，不留到下一站炸。"""
    if not _LONE_SURR.search(s):
        return s
    return s.encode("utf-8", "ignore").decode("utf-8")

def _s_arg(v) -> str:
    """JSON body 里的字符串字段：非字符串一律当"没给"（回空串），别 str() 硬转。

    {"note": 123} 硬转会把它当批注存进去（"123"），而 {"note": {}} 直接
    AttributeError 冒成 500。口径统一：不是字符串 = 空 = 该报"不能为空"就报 400。
    代理字符（见 _utf8_safe）在这里统一剥：这是绝大多数字符串字段的门。"""
    return _utf8_safe(v).strip() if isinstance(v, str) else ""

def _int_arg(v, status: int, msg: str) -> int:
    """请求参数里的裸 int：坏值回一句人话，别让 ValueError 冒成 500 被误译成模型错误。
    非整数一律拒绝：1.5 截成 1 是"悄悄译错段"，比 400 糟糕得多（bool 是 int 的子类，也拒）。
    超出 64 位整数范围的也要拦：SQLite 绑参数直接 OverflowError——客户端送一个
    10**30 就能换来一个 500 和满屏堆栈，其实该是 400。"""
    if isinstance(v, bool):
        raise HTTPException(status, msg)
    if isinstance(v, int):
        if -_SQLITE_INT_MAX <= v <= _SQLITE_INT_MAX:
            return v
        raise HTTPException(status, msg)
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise HTTPException(status, msg)
    if not f.is_integer() or abs(f) >= 2 ** 53:   # 超 2^53 的 float 已丢精度，不能当序号用
        raise HTTPException(status, msg)
    return int(f)


def _paper_needing_paras(pid: str) -> dict:
    """「这篇存在且读得了」是绝大多数生成端点的共同前置。"""
    p = _paper_or_404(pid)
    _require_paras(pid)
    return p

@app.get("/api/papers/{pid}")
def get_paper(pid: str):
    """打开/换篇每次都拉：各生成卡的大 JSON 留在各自端点里，别跟着这一趟白跑。"""
    p = _paper_or_404(pid)
    p["n_paragraphs"] = db.count_paragraphs(pid)
    for k in ("summary", "suggest", "advisor", "method_card", "citation",
              "evidence_qs", "path", "mono_path", "dual_path", "pdf_hash"):
        p.pop(k, None)
    return p

def _detect_paper_type(title: str, paras: list) -> str:
    """研究型（research）/ 综述型（review），启发式，一次模型调用都不花。

    综述几乎都会**自报家门**：标题带 review/survey/综述，或者摘要/引言里明说
    "this review / this survey / we review"。三个信号命中其一就算；拿不准一律算
    研究型——综述策略（只灰参考文献、谱系卡）误用到研究型论文上比反过来更难看。
    """
    rx = re.compile(r"\b(reviews?|surveys?|tutorial|primer|state[- ]of[- ]the[- ]art"
                    r"|recent advances|challenges and opportunities|opportunities and challenges"
                    r"|perspectives? on|roadmap|progress and (?:challenges|prospects))\b"
                    r"|综述|述评|进展与挑战", re.I)
    title_hit = bool((title or "").strip()) and bool(rx.search(title or ""))
    lead = " ".join((p.get("text") or "") for p in (paras or [])[:8])
    lead_hit = bool(re.search(r"in (this|the) (review|survey)|we (review|survey)|本(文|篇)综述|这篇综述", lead, re.I))
    return "review" if (title_hit or lead_hit) else "research"

def _ensure_paper_type(pid: str) -> str:
    """论文的类型字段，没有就现判一次（旧论文升级上来也走得到）。"""
    p = db.get_paper(pid) or {}
    t = (p.get("paper_type") or "").strip()
    if t in ("research", "review"):
        return t
    t = _detect_paper_type(p.get("title"), db.get_paragraphs(pid))
    db.update_paper(pid, paper_type=t)
    if t == "review":
        _applog(f"{pid}: 判定为综述，③/谱系卡按综述策略走")
    return t

@app.post("/api/papers/{pid}/touch")
def touch_paper(pid: str):
    """记一笔"最近读过"，文库按最近阅读排序时用；论文日历按天再记一笔。
    顺手把「待读」消掉——都打开读了，计划就算完成，日历上的点自己消失。"""
    _paper_or_404(pid)
    db.update_paper(pid, last_read_at=db._now())
    db.log_read(pid, time.strftime("%Y-%m-%d"))
    db.set_plan(pid, "")
    return {"ok": True}


@app.post("/api/papers/{pid}/plan")
def plan_paper(pid: str, body: dict = None):
    """把一篇排到某天读（日历上的空心点）。day 传空串=取消；重排直接给新日期。
    没有完成态、没有提醒——打开那篇的瞬间 touch 就把计划消了。"""
    _paper_or_404(pid)
    day = str(((body or {}).get("day") or "")).strip()
    if day:
        try:
            time.strptime(day, "%Y-%m-%d")     # 正则之外的兜底：2026-02-30 这类不存在的日期
        except ValueError:
            raise HTTPException(400, "day 要是真实存在的 YYYY-MM-DD 或空串")
    db.set_plan(pid, day)
    return {"ok": True}

@app.get("/api/calendar")
def calendar_view(month: str = ""):
    """论文日历：某个月里"哪天读了什么、哪天入了什么、哪天排了待读"。

    读过 = 阅读日志（touch 按天记）∪ 每篇 last_read_at 的日期——日志上线之前的
    旧记录没有逐天历史，用最后读过的那天做只读推导，不造假回填一整段历史。
    新入库 = created_at 的日期。返回的都是库里的篇目，点一下就能打开。"""
    if not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", month or ""):
        month = time.strftime("%Y-%m")
    papers = db.list_papers()
    by_id = {p["id"]: p for p in papers}
    reads, added, plans = {}, {}, {}

    def put(bucket, day, pid):
        if day and day.startswith(month) and pid in by_id:
            bucket.setdefault(day, set()).add(pid)

    for row in db.reading_days(month):
        put(reads, row["day"], row["paper_id"])
    for p in papers:
        put(reads, (p["last_read_at"] or "")[:10], p["id"])
        put(added, (p["created_at"] or "")[:10], p["id"])
        put(plans, (p["plan_day"] or "")[:10], p["id"])     # 待读：排到哪天算哪天（含未来）

    days = {}
    for day in set(reads) | set(added) | set(plans):
        entry = {}
        for key, bucket in (("reads", reads), ("added", added), ("plans", plans)):
            entry[key] = [{"id": pid,
                           "title": (by_id[pid]["title"] or by_id[pid]["filename"] or "").strip()}
                          for pid in sorted(bucket.get(day, set()))]
        days[day] = entry
    return {"month": month, "days": days}

_deleted_pids: set = set()      # 已删篇：析读线程写库前查这里，避免给死篇插回孤儿行
_deleted_lock = threading.Lock()

def _pid_gone(pid: str) -> bool:
    with _deleted_lock:
        return pid in _deleted_pids

def _forget_pid_locks(pid: str):
    """删篇收尾：把按篇取的互斥锁簿子里的死键清掉（键的口径一律是 `…:<pid>[:…]`）——
    不回收的话，一个删过几百篇的会话会把死 pid 的锁对象永远留在内存里。只清簿子里的
    登记，还在等旧锁的线程拿到的还是同一个对象，行为不变。"""
    for k in [k for k in list(_key_locks) if pid in k.split(":")]:
        _key_locks.pop(k, None)

@app.delete("/api/papers/{pid}")
def delete_paper(pid: str):
    p = _paper_or_404(pid)      # 404 守卫；path 字段留给收尾删库内文件用
    with _deleted_lock:
        _deleted_pids.add(pid)   # 有意的守卫：在跑的析读/七问线程靠它不再往死篇写孤儿行
    with _lines_probe_lock:
        _lines_probe_done.discard(pid)
    stale_rects = [n["id"] for n in db.get_marginalia(pid)]   # 先取（查库不占锁），再收死键
    with _rect_lock:
        _rect_tried.difference_update(stale_rects)   # 钉子行要没了，补定位的死键顺手收掉
    with _key_lock("translate:" + pid):
        _cancel_request("analysis", pid)      # 已发出的 LLM 调用跑完这一拍就收，不再续下一拍
        d = paper_dir(pid)                    # purge 前算好：之后 DB 没了，只剩兜底解析
        db.purge_paper(pid)
        if translate_full.cancel(pid, d):
            _applog(f"删论文 {pid}：同时终止了还在跑的全文翻译")
        shutil.rmtree(d, ignore_errors=True)
    for _ in range(6):
        if not os.path.exists(d):
            break
        # 刚落盘的 PDF 会被杀软/索引器攥几秒（WinError 32，文件级共享锁）：这几拍是
        # 当场收走文件夹的唯一机会。**等待在锁外做**：锁里 sleep 最多 3 秒，会把同篇
        # 的其它操作（翻译收尾/变体生成）一起串行堵住——文件锁跟锁里在办的事无关。
        time.sleep(0.5)
        shutil.rmtree(d, ignore_errors=True)
    if os.path.exists(d):
        _late_rm_paper_dir(pid, p, d)
    translate_full.JOBS.pop(pid, None)      # 翻译任务登记簿：篇都没了，条目别再留着
    _forget_pid_locks(pid)

def _late_rm_paper_dir(pid: str, p: dict, d: str):
    """当场没收成功的文件夹交给后台保洁：每 2 秒试一次、封顶 60 秒。杀软松手就收走。
    每拍删前先对 DB path 锚——同名文件夹万一已被新导入占走（几十秒窗口内重导入同题，
    极罕见），立刻收手，别把新论文的库删了。d 必须调用方先算好传进来：DB 行已 purge，
    这时再 paper_dir(pid) 只会兜底出一个错的目录。"""
    def work():
        deadline = time.time() + 60
        while time.time() < deadline and os.path.exists(d):
            time.sleep(2)
            try:
                if any(os.path.normcase(os.path.dirname(r["path"] or "")) == os.path.normcase(d)
                       for r in db.q("SELECT path FROM papers")):
                    return
            except Exception as e:
                _applog(f"收旧论文夹 {d} 查库失败，放弃这一轮（目录留在盘上，用户看得见也删得掉）：{_human_msg(e)}")
                return
            shutil.rmtree(d, ignore_errors=True)
        if os.path.exists(d):
            # 60 秒都没删掉（多半是杀软/阅读器一直攥着句柄）：留一行日志，
            # 否则盘上凭空多出一个占空间的旧目录，用户无从知道它是什么
            _applog(f"旧论文夹 60 秒内没删掉，留在盘上：{d}")
    threading.Thread(target=work, daemon=True).start()
    with _dir_lock:
        _dir_cache.pop(pid, None)
    # 夹子还被攥着时，先把里面的文件删掉（夹空了后续 rmdir 也轻）。**只删夹子里的**：
    # path 字段万一指着外部原件（历史遗留行的口径），那是用户自己的文件，不碰
    d_norm = os.path.normcase(os.path.abspath(d))
    for k in ("path", "dual_path", "mono_path"):
        v = p.get(k) or ""
        if v and os.path.normcase(os.path.dirname(os.path.abspath(v))) == d_norm:
            fsutil.rm_quiet(v)

_key_locks: dict = {}

def _key_lock(key: str) -> threading.Lock:
    """按 key 取进程内互斥锁（dict.setdefault 本身原子，不需要守护锁）：
    同一 key 的生成任务并发请求在这里排队，拿锁的一方真生成，后到者重读缓存直接返回。"""
    return _key_locks.setdefault(key, threading.Lock())

class _SafeFile(FileResponse):
    """PDF 单文件响应：删论文/换版本与下载/翻页请求赛跑时（另一窗口正在删这篇），
    starlette 是**发送响应时**才 stat 文件的——文件那时已经没了就抛 RuntimeError，
    用户看到的是一句 500。头还没发出去就当场降级成 404 人话；已经发了一半的
    （stat 过了、open 之前才消失）只能静默收场，任何方案都改不了已定的状态码。"""
    async def __call__(self, scope, receive, send):
        started = [False]
        async def track(message):
            if message["type"] == "http.response.start":
                started[0] = True
            await send(message)
        try:
            await super().__call__(scope, receive, track)
        except (FileNotFoundError, RuntimeError):
            if started[0]:
                return
            await JSONResponse({"detail": "文件不存在（这篇可能刚被删除或替换）"},
                               status_code=404)(scope, receive, send)

def _pdf_or_404(path: str, msg: str) -> _SafeFile:
    """盘上有就服务这个 PDF，没有就按调用方口径 404（三种变体各有各的人话）。"""
    if path and os.path.exists(path):
        return _SafeFile(path, media_type="application/pdf")
    raise HTTPException(404, msg)

def _sync_translate_from_job(pid: str, p: dict, j: dict) -> dict:
    """job 说 done、库还没跟上的那一拍：把成品路径同步进库（状态端点与取件端点共用）。

    状态端点本来就在做这件事，但那是**惰性同步**——没人来问状态就没人落库；直接在
    done 后取件的客户端（端到端实测在负载下撞过：做完立刻取 mono 得到 404）要先问
    一次 job。同步时顺带清掉上一轮的两个残件：dual 是按旧 mono 派生的缓存（已过期）、
    旧名 mono 是没人指的孤儿。返回重读后的 paper（调用方后续判断用）。"""
    if j.get("status") != "done":
        return p
    if (j.get("mono") or "") == (p.get("mono_path") or "") and p.get("translate_status") == "done":
        return p
    old_dual = p.get("dual_path") or ""
    old_mono = p.get("mono_path") or ""
    db.update_paper(pid, mono_path=j["mono"] or "", dual_path="",
                    translate_status="done", translate_error="")
    if old_dual:
        fsutil.rm_quiet(old_dual)
    if old_mono and old_mono != (j.get("mono") or ""):
        fsutil.rm_quiet(old_mono)
    return _paper_or_404(pid)

@app.get("/api/papers/{pid}/pdf")
def paper_pdf(pid: str, variant: str = "original"):
    p = _paper_or_404(pid)
    mono_name, dual_name = _variant_names(_display_name(p) or "paper")
    if variant == "dual":
        dual = p.get("dual_path") or ""
        if dual and os.path.exists(dual):
            return _pdf_or_404(dual, "双语版尚未生成")   # 也走 _SafeFile：删除/换版本抢在发送前删掉文件时降级 404，别裸 500
        mono = p.get("mono_path") or os.path.join(paper_dir(pid), mono_name)
        src = _paper_src(pid, p)
        if (mono and os.path.exists(mono) and src and os.path.exists(src)):
            try:
                with _key_lock("variant:" + pid + ":dual"):
                    dual = os.path.join(paper_dir(pid), dual_name)
                    if not os.path.exists(dual):
                        translate_full.derive_dual(src, mono, dual)
                    db.update_paper(pid, dual_path=dual)
                return _pdf_or_404(dual, "双语版尚未生成")
            except OSError:
                # Windows 上刚换名的产物可能还被上一个响应占着句柄：当作没生成好，让前端走原文
                raise HTTPException(404, "双语版尚未生成")
        raise HTTPException(404, "双语版尚未生成")
    if variant == "mono":
        mono = p.get("mono_path") or ""
        if not (mono and os.path.exists(mono)):
            p = _sync_translate_from_job(pid, p, translate_full.job(pid))
            mono = p.get("mono_path") or ""
        if mono and os.path.exists(mono):
            return _pdf_or_404(mono, "译文版尚未生成")   # 同上：快路径也要 _SafeFile 的竞态降级
        dual = p.get("dual_path") or ""
        if dual and os.path.exists(dual):
            with _key_lock("variant:" + pid + ":mono"):
                mono = os.path.join(paper_dir(pid), mono_name)
                if not os.path.exists(mono):
                    translate_full.derive_mono(dual, mono)
                db.update_paper(pid, mono_path=mono)
            return _pdf_or_404(mono, "译文版尚未生成")
        raise HTTPException(404, "译文版尚未生成")
    src = _paper_src(pid, p)
    return _pdf_or_404(src, PDF_GONE)

_lines_probe_done: set = set()   # 行级补解析每篇每进程只试一次：对不上的篇反复整本重解析纯属浪费
_lines_probe_lock = threading.Lock()   # 上面这本簿子的锁：端点线程与删篇线程并发翻它

def _claim_once(book: set, lock, key) -> bool:
    """“只试一次”的簿子：查与登记必须在同一把锁里（两边都读到“没试过”就会各跑一遍）。

    行级补解析与页边补定位两处共用：将来要给簿子加 TTL 或上限，只改这一处。"""
    with lock:
        if key in book:
            return False
        book.add(key)
        return True

def _probe_lines_once(pid: str) -> bool:
    """这篇要不要做段落重解析：两个窗口同时首拉时，本意是每篇只跑一次整本重解析。"""
    return _claim_once(_lines_probe_done, _lines_probe_lock, pid)

STALE_NOTE = "段落已重排，重新析读一次"

def _reparse_paper(pid: str) -> str:
    """重解析一篇的段落（免费、本地 CPU）。返回 'same'|'migrated'|'stale'|'fresh'|'skip'。

    解析规则演进过（缩进断段、公式块不进 ¶、文献表兜底三处收紧），库里老规则切出来的
    大 ¶ 要整批换代。换代就会重排编号，而页边卡、骨架角色、主张依据、回答里的 ¶n
    全是按编号指的——先按**文字**把锚点搬过去，再判骨架还能不能信：
    新解析救回来的正文超过一成、或一成老段找不到落点，骨架就作废（标 stale，
    由"再打开这篇"这个动作触发重析读，不在开机时替用户花钱）。
    """
    p = db.get_paper(pid)
    if not p:
        return "skip"
    src = _paper_src(pid, p)
    if not os.path.exists(src):
        return "skip"
    old = db.get_paragraphs(pid, with_lines=False)
    fresh = pdfparse.extract_paragraphs(src)
    if not fresh:
        return "skip"          # 扫描件/纯图 PDF 解析不出东西：别把 OCR 出来的段落抹了
    if _pid_gone(pid):
        return "skip"
    if old and db.paragraphs_match(pid, fresh):
        db.replace_paragraphs(pid, fresh)      # 只是补行级坐标/页高，编号一个没动
        return "same"
    if old:
        # 迁移锚点 + 换段落表 + （必要时）作废骨架，全在同一事务里：
        # 分两笔写，中间崩就是"锚点已迁、旧骨架还挂着 done"或"老表 + 新编号"的静默错位
        stats = db.migrate_anchors(pid, [(r["idx"], r["text"]) for r in old], fresh,
                                   stale_note=STALE_NOTE)
    else:
        if _pid_gone(pid):
            return "skip"
        db.replace_paragraphs(pid, fresh)
        return "fresh"
    if _pid_gone(pid):
        return "skip"
    _applog(f"段落重解析 {pid}: {len(old)}→{len(fresh)} 段，锚点迁移 {stats['mapped']}"
            f"（丢 {stats['orphan']}），新救回 {stats['uncovered']}"
            + (f"，页边卡跟迁 {stats['pins']}（丢 {stats['pins_lost']}）"
               if stats["pins"] or stats["pins_lost"] else ""))
    if stats["material"]:
        _cancel_request("analysis", pid)      # 还在跑的那轮析读：收手，别再往下烧钱
        _applog(f"段落重排后骨架作废 {pid}：新解析救回 {stats['uncovered']} 段、丢 {stats['orphan']} 段")
        return "stale"
    return "migrated"

def _maybe_requeue_stale(pid: str):
    """骨架被段落重排作废过的论文：用户再打开它时自动重析读一次。

    钱花在"用户真的打开了这篇"这个动作上，开机扫全库时不替任何人花。
    设置里关掉了「导入即析读」就不自动——只把作废原因留在析读按钮旁边。
    """
    p = db.get_paper(pid) or {}
    if p.get("analysis_status") != "none" or (p.get("analysis_error") or "") != STALE_NOTE:
        return
    if _demo_mode() or not (config.load().get("analysis") or {}).get("auto", True):
        return
    if _analysis_inflight(pid):
        return
    _applog(f"段落重排后重析读 {pid}")
    _enqueue_analysis(pid, db.get_paragraphs(pid, with_lines=False))

_reparse_ev_lock = threading.Lock()
_reparse_events: dict = {}       # pid -> Event：正在解析的那一篇

def _reparse_paper_sync(pid: str):
    """重解析一篇：**同一篇只跑一次**，其他调用者等它跑完再回。

    为什么要等：两个窗口同时打开同一篇老格式论文时，后到的那个会读到**上一代**段落表
    （一个窗口一套 ¶ 编号，页边卡/引用跳转全对不上）。等的是"这一次解析落地"，
    不是永远——等超时（大部头也就几十秒）就按现状读库，不把请求挂死。
    """
    with _reparse_ev_lock:
        ev = _reparse_events.get(pid)
        if ev is None:
            ev = threading.Event()
            _reparse_events[pid] = ev
            first = True
        else:
            first = False
    if not first:
        ev.wait(timeout=180)
        return
    try:
        _reparse_paper(pid)
    except Exception as e:
        _applog(f"[eggpaper] 段落重解析失败 {pid}: {e}")
    finally:
        with _reparse_ev_lock:
            _reparse_events.pop(pid, None)
        ev.set()

def _wait_reparse(pid: str, timeout: float = 180):
    """别人正在解析这一篇就等它落地（没有在解析的就不等）。"""
    with _reparse_ev_lock:
        ev = _reparse_events.get(pid)
    if ev is not None:
        ev.wait(timeout=timeout)

@app.get("/api/papers/{pid}/paragraphs")
def paragraphs(pid: str):
    _paper_or_404(pid)
    if db.paragraphs_stale(pid):
        if _probe_lines_once(pid):
            _reparse_paper_sync(pid)
        else:
            _wait_reparse(pid)        # 别的窗口/开机扫库正在解析：等它落地，别给上一代
    _maybe_requeue_stale(pid)
    return db.get_paragraphs(pid)

def _reparse_sweep():
    """开机把全库老解析器的段落整批换代（免费、本地 CPU）。

    一篇一篇来、每篇之间歇一拍：解析是纯 Python 循环，连着跑会把 GIL 占满——
    用户这时正在开第一篇文章。已解析过的（本进程的簿子）跳过。
    """
    try:
        rows = db.q("SELECT DISTINCT paper_id FROM paragraphs "
                    "WHERE bbox LIKE '{%' AND bbox NOT LIKE '%\"ph\"%'")
    except Exception as e:
        _applog(f"[eggpaper] 段落换代开机扫描失败，本次跳过：{e}")
        return
    n, lost = 0, 0
    for r in rows:
        pid = r["paper_id"]
        if not _probe_lines_once(pid):
            continue
        try:
            what = _reparse_paper_sync(pid)
        except Exception as e:
            _applog(f"[eggpaper] 段落重解析失败 {pid}: {e}")
            continue
        if what != "skip":
            n += 1
        if what == "stale":
            lost += 1
        time.sleep(1.0)
    if n:
        _applog(f"段落整批换代：{n} 篇完成" + (f"，其中 {lost} 篇骨架作废待重析读" if lost else ""))

# ---------------- 骨架分析 ----------------

def _caps_job(pid: str) -> dict:
    """析读支线：扫出全部图表注并整批翻译。图表区域是纯函数（路径+mtime 缓存），
    这里算过一遍，灯箱/图层面板就直接命中。"""
    pdir = db.get_paper(pid) or {}
    pairs = [(i, f["caption"]) for i, f in enumerate(_figures_for(pid, pdir)) if f.get("caption")]
    return llm.translate_captions(pairs)

def _submit_subtasks(ex, pid: str, paras: list, kind: str, demo: bool,
                     tfut, cap_fut) -> dict:
    """骨架落库之后：把其余生成任务排进池子，返回 {future: 名字} 的簿子。

    tfut/cap_fut 是骨架阶段已经发出去的术语/图注任务，并进同一本簿子一起等。
    一眼卡先发、歇 4 秒再放其余六个并发：带着全文前缀的请求要让服务端把公共前缀
    落盘（实测几秒后才可命中）。不错峰时六个并发的首拍会各吃一次全文未命中——
    20 页真论文实测第一拍 0% 命中、白付 11k tokens。术语表同理，且与骨架同发的话
    两个全文首拍**都**全价未命中（12-13 点那笔 71k 未命中输入的主因之一）：跟着骨架
    4 秒后发才命中缓存。入参换成非参考文献段落：术语不长在文献表里，前缀还与骨架
    对齐、命中更彻底。
    """
    title = (db.get_paper(pid) or {}).get("title") or ""
    use = [p for p in paras if not p.get("in_refs")]
    futs = {}
    if not demo:
        futs[ex.submit(llm.extract_terms, title, use)] = "terms"
        futs[ex.submit(_summary_compute, pid)] = "summary"
        time.sleep(4)
    # ②③的生成按篇型分流：研究型 principle+method，综述型 principle+how（组织方式）
    todo = (["motive", "principle", "method"] if kind == "research"
            else ["motive", "principle", "how"]) + ["next", "lens", "doubt"]
    # 真实模式走 _six_compute（与端点同锁同缓存）：前端在析读一完成就会来要答案，
    # 两边必须串到同一把锁上，否则同一问生成两遍
    futs.update({ex.submit(_mock_six, k) if demo else ex.submit(_six_compute, pid, k): k
                 for k in todo})
    if not demo:
        futs[ex.submit(_suggest_compute, pid)] = "suggest"
    if cap_fut is not None:
        futs[cap_fut] = "figcaps"
    if tfut is not None:
        futs[tfut] = "terms"
    _analysis_progress[pid] = {"done": 1, "total": 1 + len(futs)}   # 骨架算已完成的 1 项
    return futs

def _drain_subtasks(pid: str, futs: dict) -> bool:
    """等这一轮子任务收齐、逐项落库、推进度。返回 False = 论文已被删、别再往下走。

    取消旗一起来就停等（不再写库）；剩下没跑到的那几个交给 finally 里的
    shutdown(cancel_futures=True) 收。
    """
    for fut in as_completed(futs):
        k = futs[fut]
        if _cancel_requested("analysis", pid):
            break
        try:
            got = fut.result()
            if k == "terms":
                n = _save_terms(pid, got)
                _applog(f"析读 {pid}: 本篇术语 {n} 条" if n else f"析读 {pid}: 术语这次是空的")
            elif k == "summary":
                # _summary_compute 已锁内落库；这里只剩"真没生成出来"的记一句
                if not (isinstance(got, dict) and got.get("one_line")):
                    _applog(f"析读 {pid}: 一眼卡这次是空的（速览页会再试一次）")
            elif k == "suggest":
                if not (isinstance(got, dict) and got.get("questions")):
                    _applog(f"析读 {pid}: 推荐问题这次是空的（提问页会再试一次）")
            elif k == "figcaps":
                if got:
                    if _pid_gone(pid):
                        return False
                    # 锁内读改写合并落库：懒翻译按坐标存的旧键不被整包覆写抹掉
                    db.fig_caps_add(pid, got)
                    _applog(f"析读 {pid}: 图注中文 {len(got)} 条")
                else:
                    # 限流/超时/坏 JSON 都在 translate_captions 里被收敛成 {}，原先只在
                    # got 非空时记日志——翻译挂了用户在灯箱里只看到英文图注，
                    # 事后翻 app.log 一个字都没有，"没图注"还是"翻译失败"分不出来
                    _applog(f"析读 {pid}: 图注中文这次没生成（这篇没图注，或翻译被限流/解析失败——界面用英文原注）")
            elif got.get("text") or got.get("items"):
                if _pid_gone(pid):
                    _applog(f"析读 {pid}: 论文已删除，丢弃七问·{k}")
                    return False
                db.answer_put(pid, k, got)
            else:
                _applog(f"析读 {pid}: 七问·{k} 这次是空的")
        except Exception as e:
            _applog(f"析读 {pid}: {k} 没生成（{_human_msg(e)}）")
        d = _analysis_progress.get(pid)
        if d:
            d["done"] = min(d.get("total") or 0, d.get("done", 0) + 1)
    return True

def _run_analysis(pid: str, paras: list):
    ex = None
    try:
        title = (db.get_paper(pid) or {}).get("title") or ""
        use = [p for p in paras if not p.get("in_refs")]
        kind = _ensure_paper_type(pid)
        demo = _demo_mode()
        if _analysis_cancelled(pid, "已取消（开始前）"):
            return
        ex = ThreadPoolExecutor(max_workers=5)
        tfut = None
        cap_fut = None
        if demo:
            data = llm.mock_analyze(paras)
            tfut = ex.submit(_demo_terms, title, paras)
        else:
            # 图注翻译独立成一次调用，与骨架**并行**（图表扫描 O(n²) 也不再挡在骨架前面）：
            # 挤在同一次输出里的代价（16k 顶截断 → 坏 JSON 重烧 → 整个析读判失败）见
            # llm.analyze_skeleton 的文档
            if not llm._is_en():
                cap_fut = ex.submit(_caps_job, pid)
            data = llm.analyze_skeleton(title, use, kind=kind)
        for p in paras:
            if p["in_refs"]:
                data["roles"][str(p["idx"])] = "boilerplate"
                data["purposes"][str(p["idx"])] = "参考文献"
        # 指纹闸：骨架是按**这一版**段落算的，跑完时段落若已换代（另一窗口重解析过），
        # 写进去就是静默错位——整批不写，这篇回到"未析读"
        if not db.set_analysis(pid, data["claims"],
                               {k: {"role": v, "purpose": data["purposes"].get(k, "")}
                                for k, v in data["roles"].items()},
                               expect=db.para_sig(paras)):
            # 作废也要落到**终态**：只 return 的话状态停在 running，界面永远"正在通读…"
            # （只能等重启时的僵尸清扫）。落成"未析读 + 原因"，再打开时会自动重析读一次
            _applog(f"析读 {pid}: 段落已换代，这次结果作废（不写骨架，也不写七问/摘要）")
            db.update_paper(pid, analysis_status="none", analysis_error=STALE_NOTE)
            return
        db.answers_clear(pid)
        upd = dict(summary=None, suggest=None, advisor=None, method_card=None,
                   abbrs=db.dumps_cjk(data.get("abbrs", {})),
                   evidence_qs=db.dumps_cjk(data.get("evidence_qs", {})))
        if data.get("fig_caps"):
            # 只在真的拿到了译文时才覆盖：懒翻译存下的旧值不被一次没带图注的析读清掉
            upd["fig_caps"] = db.dumps_cjk(data["fig_caps"])
        db.update_paper(pid, **upd)
        if _analysis_cancelled(pid, "已取消（骨架完成后，骨架保留）"):
            return
        futs = _submit_subtasks(ex, pid, paras, kind, demo, tfut, cap_fut)
        if not _drain_subtasks(pid, futs):
            return
        if _analysis_cancelled(pid, "已取消，已完成的部分保留"):
            return
    except Exception as e:
        hint = _human_msg(e)
        _applog(f"析读失败 {pid}: {type(e).__name__}: {str(e)[:300]}")
        # 状态已经是 none 的（被段落换代作废 / 用户取消后清场）别再标 error：
        # STALE_NOTE 是"再打开这篇时自动重析读"的触发条件，踩掉它就永远不重跑了
        try:
            cur = (db.get_paper(pid) or {}).get("analysis_status") or "none"
            if cur == "none":
                _applog(f"析读 {pid}: 状态已是未析读（作废/取消），这次失败不标错")
            else:
                db.fail_analysis(pid, hint)
        except Exception:
            pass
    finally:
        _cancel_clear("analysis", pid)
        _analysis_progress.pop(pid, None)
        if ex:
            ex.shutdown(wait=False, cancel_futures=True)
        _job_done("analysis", pid)

@app.post("/api/papers/{pid}/analyze")
def analyze(pid: str):
    p = _paper_or_404(pid)
    if not db.get_paragraphs(pid, with_lines=False):
        # 扫描件重试：OCR 这道工序没跑成（引擎缺失/识别失败），点「重新析读」从头再来
        if not ocr.available():
            raise HTTPException(400, NO_TEXT)
        with _key_lock("job:" + pid):
            if _analysis_inflight(pid):
                return {"status": p["analysis_status"] or "queued"}
            db.update_paper(pid, analysis_status="queued", analysis_error=None)
            # 登记必须在锁内、起线程**之前**做完：放在 _ocr_then_analyze 里（线程被
            # 调度到才登记）的话，双击的两下都看到"没人在跑"，各起一个 OCR 线程——
            # 两个进程扫同一份 PDF，先完成的 _job_done 又会在另一个还在跑时清掉登记，
            # /analysis 端点于是把状态归零、按钮复活，能再叠第三个
            _job_start("analysis", pid)
            threading.Thread(target=_ocr_then_analyze, args=(pid, _paper_src(pid, p)), daemon=True).start()
        return {"status": "queued"}
    # 查互斥与占位排队必须同锁：拆开就有 TOCTOU——两边都过了检查再各自启动，照样互相清缓存
    with _key_lock("job:" + pid):
        p = db.get_paper(pid) or p     # 锁内重读：锁外那份可能是"已完成"的旧读，照它判会连环重析
        if p["analysis_status"] in ("running", "queued") and _analysis_inflight(pid):
            return {"status": p["analysis_status"]}
        if p["analysis_status"] in ("running", "queued"):
            _applog(f"析读 {pid}: 数据库里是 {p['analysis_status']} 但本进程没有这个任务（上次被中断），重来")
        _enqueue_analysis(pid, db.get_paragraphs(pid, with_lines=False))
    return {"status": "queued"}

@app.get("/api/papers/{pid}/analysis")
def analysis(pid: str):
    p = _paper_or_404(pid)
    status, claims, annos = db.get_analysis(pid)
    if status is None:               # 两次取行之间被删：诚实 404，别回半个空壳
        raise HTTPException(404, "论文不存在")
    if status in ("running", "queued") and not _analysis_inflight(pid):
        db.update_paper(pid, analysis_status="none")
        status = "none"
    eqs = _json_of(p.get("evidence_qs"))
    return {"status": status, "error": p["analysis_error"], "claims": claims, "annotations": annos,
            "evidence_qs": eqs, "progress": _analysis_progress.get(pid)}


def _resolve_rects(pid: str):
    """把 quote 定位成页面矩形（**退路**：这里的搜索跨不过换行和连字符，真正的
    逐行精确划线在前端做；框选钉子 kind=region 自带矩形，不参与）。

    每条钉子只试一次（`_rect_tried`）：搜不到的钉子（引文跨栏、被截断、模型抄错了）
    永远搜不到，而每试一次就要开一次 PDF——原来 GET /marginalia 每次都把没 rect 的
    钉子重试一遍，页边留一条搜不到的钉子，之后每次打开这篇都白开一次 PDF。
    """
    import pymupdf
    p = db.get_paper(pid)
    if not p:
        return
    # PDF 不在（被移动/网盘没同步）时就此打住：GET /marginalia 不能为它 500，
    # 析读线程更不能在批注已经完整落库之后在这翻成"失败"。路径按 _paper_src
    # 的自愈口径解析——外部原件没了但库内副本还在，照样能补坐标。
    src = _paper_src(pid, p)
    if not os.path.exists(src):
        return
    doc = None
    try:
        for n in db.get_marginalia(pid):
            if n["rect"] or n["kind"] == "region":
                continue
            if not _rect_mark_tried(n["id"]):
                continue
            if doc is None:
                doc = pymupdf.open(src)
            rects = []
            for cut in (0, 60, 36, 20):
                probe = n["quote"] if cut == 0 else n["quote"][:cut].strip()
                if len(probe) < 8:
                    continue
                rects = doc[n["page"]].search_for(probe)
                if rects:
                    break
            if rects:
                r = rects[0]
                db.marginalia_set_rect(n["id"], {"x0": r.x0, "y0": r.y0, "x1": r.x1, "y1": r.y1})
    except Exception as e:
        _applog(f"批注定位 {pid} 失败（不影响批注本身）: {e}")
    finally:
        if doc:
            doc.close()

_rect_tried = set()
_rect_lock = threading.Lock()   # _rect_tried 的锁：GET /marginalia（端点线程）与删篇线程并发翻它

def _rect_mark_tried(nid) -> bool:
    """这条钉子值不值得试一次补定位（并发开两次页边时，每条只试一次是这里的设计口径）。"""
    return _claim_once(_rect_tried, _rect_lock, nid)

_live_jobs = set()
_live_lock = threading.Lock()   # _live_jobs 的读写锁：析读 worker、OCR 线程与各端点都会翻这本簿子

def _job_start(kind: str, pid: str):
    with _live_lock:
        _live_jobs.add((kind, pid))

def _job_live(kind: str, pid: str) -> bool:
    with _live_lock:
        return (kind, pid) in _live_jobs

def _job_done(kind: str, pid: str):
    with _live_lock:
        _live_jobs.discard((kind, pid))

# ---- 长任务取消：协作式。线程在阶段边界查探针，查到就不再写库、状态归回"没做过"。
_cancel_flags: set = set()        # (kind, pid)：旗子按任务种类隔离，取消析读不误杀同篇的翻译
_cancel_lock = threading.Lock()

def _cancel_requested(kind: str, pid: str) -> bool:
    with _cancel_lock:
        return (kind, pid) in _cancel_flags

def _cancel_request(kind: str, pid: str):
    with _cancel_lock:
        _cancel_flags.add((kind, pid))

def _cancel_clear(kind: str, pid: str):
    with _cancel_lock:
        _cancel_flags.discard((kind, pid))

_analysis_progress: dict = {}   # pid -> {"done": n, "total": m}：析读子任务计数，给"已完成 n/m 项"用

# ---------------- 析读队列：一次导入多篇时，一篇一篇地读 ----------------
_q_lock = threading.Lock()
_analysis_q = queue.Queue()
_analysis_worker = [None]
_analysis_pending = set()

def _analysis_inflight(pid: str) -> bool:
    """这篇是不是"在跑或排队中"（两个真相源合起来看，别再漏一个）。"""
    return _job_live("analysis", pid) or pid in _analysis_pending

def _enqueue_analysis(pid: str, paras: list):
    with _q_lock:
        if pid in _analysis_pending:
            return
        _cancel_clear("analysis", pid)   # 上次取消留下的旗子别误杀这次
        _analysis_pending.add(pid)
        db.update_paper(pid, analysis_status="queued", analysis_error=None)
        _analysis_q.put((pid, paras))
        w = _analysis_worker[0]
        if w is None or not w.is_alive():
            w = threading.Thread(target=_analysis_loop, daemon=True)
            _analysis_worker[0] = w
            w.start()

def _analysis_loop():
    """队列空了就自己退出（下次导入再起一条）。空判断与入队在同一把锁里，不会漏活。"""
    while True:
        with _q_lock:
            if _analysis_q.empty():
                _analysis_worker[0] = None
                return
            pid, paras = _analysis_q.get_nowait()
        if _pid_gone(pid):
            # 死篇也要把排队登记摘干净：留着它，"这篇在排队"就永远是"真"，
            # 同名 pid 的任何未来查询都会被骗（登记簿还白涨一个条目）
            with _q_lock:
                _analysis_pending.discard(pid)
            continue
        with _q_lock:
            # 先起活、再摘排队登记：`_analysis_inflight` 是"线程活着 or 还在排队"两个真相源
            # 的或，反过来的话中间会经过"两边都不真"的一瞬——晚启动清僵尸正好扫到这里，
            # 就把刚接手的任务当僵尸、状态清成 none（界面显示"未析读"，结果其实在跑）
            _job_start("analysis", pid)
            _analysis_pending.discard(pid)
        try:
            # 状态写入放进 try：这一句自己抛（磁盘满 / database is locked）时，原先线程
            # 当场死、finally 之外没人摘登记簿——("analysis", pid) 永久留在 _live_jobs 里，
            # 这篇从此"永远在跑"：/analyze 直接返回、界面一直排队，只能重启才解。
            db.update_paper(pid, analysis_status="running", analysis_error=None)
            _run_analysis(pid, paras)
        except Exception as e:
            # 崩了要留痕：打包版没有 stdout，线程里的异常连一个字都留不下（前台只看到
            # "通读中…"永远转）。状态落到 error，用户看得见也能重试。
            _applog(f"析读 {pid} 线程异常：{_human_msg(e)}")
            try:
                db.update_paper(pid, analysis_status="error", analysis_error=_human_msg(e))
            except Exception:
                pass
        finally:
            _job_done("analysis", pid)

_applog_lock = threading.Lock()

def _applog(msg: str):
    """往 app.log 写一行。打包版（console=False）没有 stdout，print 出去的东西一个字都留不下——
    而"这次到底花了多久、卡在哪一段"恰恰是用户最常问的。和 window.py 写的是同一份日志。
    文件超过 1MB 就截到尾部的 1/4：日志只增不删的话，跑上一年能吃掉几十 MB。

    轮转是"读旧-截短-改写"三步，多线程同时跨过 1MB 线时互相截断会把刚写的一行整段吃掉
    （丢的还常是 token 账）——整段读改写+追加都进同一把锁。"""
    try:
        path = appinfo.log_path(create=True)
        with _applog_lock:
            try:
                if os.path.getsize(path) > 1_000_000:
                    with open(path, encoding="utf-8", errors="replace") as f:
                        f.seek(-250_000, 2)
                        f.readline()
                        tail = f.read()
                    with open(path, "w", encoding="utf-8") as f:
                        f.write("[eggpaper] （日志超 1MB，只保留最近一段）\n" + tail)
            except OSError:
                pass
            with open(path, "a", encoding="utf-8") as f:
                f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass

llm.applog = _applog     # token 账从 print 换成落盘：打包版没有 stdout，
                         # 不注入的话用户"钱花在哪"永远只有平台账单一个答案
crossref.applog = _applog      # 元数据回填的失败/回填记录：同上，打包版只有网页看不到的 stdout
engine_install.applog = _applog   # 引擎安装的失败细节（源逐个试、warmup 没做成）：同上

USER_NOTE_KINDS = ("lookup", "region", "note")   # 页边只下发用户钉的三种 kind；老库里 kind 在此之外的存量行不下发

@app.get("/api/papers/{pid}/marginalia")
def marginalia_get(pid: str):
    _paper_or_404(pid)
    def user_notes():
        return [dict(n, rect=_json_of(n["rect"], None))
                for n in db.get_marginalia(pid) if n["kind"] in USER_NOTE_KINDS]
    notes = user_notes()
    if any(not n["rect"] for n in notes):
        _resolve_rects(pid)          # 老钉子补定位（每条只试一次，见 _resolve_rects）
        notes = user_notes()
    return {"notes": notes}

@app.post("/api/papers/{pid}/analysis/cancel")
def analysis_cancel(pid: str):
    """请求取消析读：正在跑的线程在阶段边界看到旗子就停，已生成的部分保留。"""
    _paper_or_404(pid)
    _cancel_request("analysis", pid)
    return {"ok": True}

@app.post("/api/papers/{pid}/translate-full/cancel")
def translate_cancel(pid: str):
    """停全文翻译：掐掉 pdf2zh 进程；已译好的页留在 .pages/ 里，下次接着译。"""
    p = _paper_or_404(pid)
    stopped = translate_full.cancel(pid, paper_dir(pid))
    cur = db.get_paper(pid) or p     # 状态此刻可能刚好翻成 done：按最新值判，别把陈旧的 running 盖成 none
    if stopped or cur["translate_status"] == "running":
        db.update_paper(pid, translate_status="none", translate_error="")
        _applog(f"全文翻译 {pid}: 已取消")
    return {"ok": True, "stopped": bool(stopped)}

@app.post("/api/papers/{pid}/pin")
def pin_lookup(pid: str, body: dict):
    """把查译/段译/框选答疑/自己写的批注钉到页边（用户资产，持久化）。"""
    _paper_or_404(pid)
    quote = _s_arg(body.get("quote"))
    note = _s_arg(body.get("note"))
    if not quote or not note:
        raise HTTPException(400, "quote 与 note 不能为空")
    para_idx = _int_arg(body.get("para_idx") or 0, 400, "para_idx 得是整数")
    if para_idx < -1:
        raise HTTPException(400, "para_idx 最小 -1（划词在段外）")   # 负值落库后引文永远对不上
    try:
        page = int(body.get("page") or 0)
    except (TypeError, ValueError):
        page = 0
    if (body.get("kind") or "") == "note":
        return {"id": db.marginalia_add(pid, para_idx, page, quote[:200], note[:600],
                                        kind="note")}
    rect = body.get("rect") or None
    if rect:
        try:
            rect = {k: float(rect[k]) for k in ("x0", "y0", "x1", "y1")}
        except (KeyError, TypeError, ValueError):
            rect = None
    if rect:
        return {"id": db.marginalia_add(pid, para_idx, page, quote[:200],
                                        note[:600], kind="region", rect=rect)}
    dup = db.marginalia_upsert_lookup(pid, para_idx, page, quote[:200], note[:600])
    return {"id": dup}

@app.delete("/api/papers/{pid}/marginalia/{mid}")
def marginalia_remove(pid: str, mid: int):
    _paper_or_404(pid)
    if not db.marginalia_delete_count(mid, pid):
        raise HTTPException(403, "这条批注不属于这篇论文")
    return {"ok": True}

# ---------------- 一眼卡 ----------------

def _require_shape(data, keys: tuple, what: str):
    """模型返回的形状不对/是空的，就别把它当成功缓存下来。

    为什么：`parse_json` 取"第一个 { 到最后一个 }"，模型把结果包成 `[{...}]` 时能解析成
    里面那个对象，于是 `data.get("questions", [])` 得到 `[]`——而路由会把这个空壳
    `json.dumps` 存进 papers，从此永远命中缓存（速览页空着、而且不会自愈，因为"重新析读"
    也不一定清得到它）。七问与引用早就做了这个判断，这里是把它补成统一的一道闸。
    """
    if not isinstance(data, dict) or not any(data.get(k) for k in keys):
        raise HTTPException(503, f"{what}没生成出来，再点一次")


_JSON_DEFAULT = object()      # _json_of 的哨兵：区分"没给兜底"（回 {}）与"显式要 None"

def _json_of(raw, fallback=_JSON_DEFAULT):
    """缓存字段里可能存进坏 JSON（老版本/半截写盘）：读的时候一律过这道，别 500。

    fallback 不传 = 坏值/空值回 {}（"当没缓存"，走重生成）；显式传 None = 回 None
    （页边说"没有 rect 就是 null"这类两态字段）。
    实现只有一份（db._safe_json）：这里以前各写一遍，坏值兜底的口径一旦分叉，
    就会出现"某个端点 500、另一个端点安静"的经典不一致。
    """
    if fallback is _JSON_DEFAULT:
        fallback = {}
    return db._safe_json(raw, fallback)


def _gen_card(pid: str, field: str, lock: str, *, what: str, shape: tuple,
              demo_fn, real_fn, precond=None, cached_value=None, cached: bool = False):
    """一眼卡/提问建议/方法卡三个端点同用的骨架：

    锁外缓存命中 → （cached=1 只读不生成）→ 按篇锁 → 锁内重读（等锁期间可能已被
    首个请求写回）→ 前置条件 → 演示或真身生成 → 形状闸 → 写回。
    precond(p) 返回非 None 即"前置条件不满足"的空响应（如还没析读的提问建议）。
    """
    p = _paper_needing_paras(pid)

    def _hit(pp):
        """缓存命中就回响应、没有回 None：坏缓存一律当没有（走重生成）。

        锁外快返与锁内复查是同一件事，以前各写一份——加一种"坏缓存"口径时
        只会加到其中一处，于是"等锁的人"和"没等锁的人"拿到两种结果。"""
        if pp[field]:
            cached_json = _json_of(pp[field], None)
            if cached_json is not None:
                return JSONResponse(cached_json)
        if cached and cached_value is not None:
            return cached_value
        return None

    r = _hit(p)
    if r is not None:
        return r
    if precond:                              # 锁外快返：前置不满足就别排在一次真生成后面
        empty = precond(p)
        if empty is not None:
            return empty
    with _key_lock(lock + ":" + pid):
        p = db.get_paper(pid)
        if not p:
            _paper_or_404(pid)          # 排队等生成时论文被删：404，别 None 下标炸 500
        r = _hit(p)
        if r is not None:
            return r
        empty = precond(p) if precond else None
        if empty is not None:
            return empty
        if _demo_mode():
            data = demo_fn(p)
        else:
            data = real_fn(p)
            _require_shape(data, shape, what)
        db.update_paper(pid, **{field: db.dumps_cjk(data)})
    return data


def _summary_material(pid: str):
    """一眼卡的两份输入：全文段落 + 按全文命中的锁定术语（管线与端点共用同一口径）。"""
    paras = db.get_paragraphs(pid)
    corpus = " ".join(pp["text"] for pp in paras)[:60000]
    return paras, _hits_merged(db.glossary_hit(pid, corpus), corpus)

def _hits_merged(hits: list, text: str) -> list:
    """单篇/跨篇命中的术语 + 全局学科库命中（功能④）。本篇的词优先：同一个词在
    本篇词表和学科库里译法打架时，锁本篇的——这篇正文语境里的写法比通用译法可信。
    封顶 30 条（Lokalise 的实践：命中注入的表过长就是噪音；超出按本篇优先截断）。"""
    have = {str(h.get("en", "")).lower() for h in (hits or [])}
    for g in db.glossary_global_hits(text or ""):
        if str(g.get("en", "")).lower() not in have:
            hits.append(g)
    return hits[:30]

def _summary_compute(pid: str) -> dict:
    """一眼卡的生成+落库：析读管线的预生成与 /summary 端点**共用同一把锁**——
    析读一完成用户就打开速览页时，谁先拿到锁谁生成，后到的命中刚写回的缓存。
    （与 _six_compute 同一个道理：不共锁，同一张卡就会生成两遍、两倍钱。）"""
    with _key_lock("summary:" + pid):
        p = db.get_paper(pid)
        if not p:
            _paper_or_404(pid)          # 排队等生成时论文被删：404，别 None 下标炸 500
        if p.get("summary"):
            cached_json = _json_of(p["summary"], None)
            if cached_json is not None:
                return cached_json            # 坏缓存当没有，走重生成
        paras, hits = _summary_material(pid)
        data = llm.summarize(p["title"], paras, hits)
        _require_shape(data, ("one_line", "novelty", "findings", "keywords"), "一眼卡")
        if not _pid_gone(pid):          # 生成隔着一次模型调用，论文可能已被删
            db.update_paper(pid, summary=db.dumps_cjk(data))
        return data

def _suggest_compute(pid: str) -> dict:
    """推荐问题的生成+落库：与 /suggest 端点共锁，道理同 _summary_compute。"""
    with _key_lock("suggest:" + pid):
        p = db.get_paper(pid)
        if not p:
            _paper_or_404(pid)
        if p.get("suggest"):
            cached_json = _json_of(p["suggest"], None)
            if cached_json is not None:
                return cached_json
        _, claims, annos = db.get_analysis(pid)
        data = llm.suggest_questions(p["title"], claims, annos)
        _require_shape(data, ("questions",), "提问建议")
        if not _pid_gone(pid):
            db.update_paper(pid, suggest=db.dumps_cjk(data))
        return data

@app.get("/api/papers/{pid}/summary")
def summary(pid: str):
    def real(p):
        paras, hits = _summary_material(pid)
        return llm.summarize(p["title"], paras, hits)
    return _gen_card(pid, "summary", "summary", what="一眼卡", shape=("one_line", "novelty", "findings", "keywords"),
                     demo_fn=lambda p: {"one_line": _demo_txt(
                                         f"〔演示〕《{(p.get('title') or '未命名')[:30]}》用一套可复现的流程回答了它的核心问题。",
                                                            f"[demo] '{(p.get('title') or 'untitled')[:30]}' answers its core question."),
                                      "novelty": _demo_txt("演示创新点：换了一类可设计的对象，别的还在用老办法",
                                                           "[demo] Novelty: a new designable subject, the rest is standard"),
                                      "findings": _demo_txt("演示发现：关键指标 4.2% → 11.8%（p < 0.001）",
                                             "[demo] Findings: key metric 4.2% → 11.8% (p < 0.001)"),
                                      "keywords": [_demo_txt("演示", "demo")]},
                     real_fn=real)

@app.get("/api/papers/{pid}/suggest")
def suggest(pid: str):
    def real(p):
        _, claims, annos = db.get_analysis(pid)
        return llm.suggest_questions(p["title"], claims, annos)
    return _gen_card(pid, "suggest", "suggest", what="提问建议", shape=("questions",),
                     demo_fn=lambda p: {"questions": [_demo_txt("〔演示〕关键数字是在什么条件下测的？", "[demo] Under what conditions were the key numbers measured?"),
                                                    _demo_txt("〔演示〕这个思路能搬到我的体系吗？", "[demo] Does this approach transfer to my system?"),
                                                    _demo_txt("〔演示〕方法上最容易被挑的毛病是什么？", "[demo] What would a reviewer pick on in the methods?")]},
                     real_fn=real,
                     precond=lambda p: {"questions": []} if p["analysis_status"] != "done" else None)

KIND_ZH = {"lookup": "查译", "region": "选区问答", "note": "批注"}

@app.post("/api/ask-visual")
def ask_visual(body: dict):
    b = body or {}
    image = b.get("image")
    # 类型先卡死：image=123/[..]/{..} 直接 .startswith 是 AttributeError→500
    if not isinstance(image, str) or not image.startswith("data:image"):
        raise HTTPException(400, "缺少图像数据")
    question = _s_arg(b.get("question")) or "解释这张图/公式。"
    if _demo_mode():
        return {"answer": "〔演示模式〕视觉问答需要配置视觉模型。"}
    ans = llm.vision_ask(image, question)
    if not ans.strip():
        raise HTTPException(503, "模型没返回内容，重试一次")
    return {"answer": ans}

# ---------------- 七问里需要现场生成的那几问 ----------------

SIX_KEYS = ("motive", "principle", "method", "how", "next", "lens", "doubt")

def _paras_of_role(annos: dict, by_idx: dict, roles: set, cap: int = 8):
    out = [by_idx[int(k)] for k, v in annos.items() if v["role"] in roles and int(k) in by_idx]
    return sorted(out, key=lambda p: p["idx"])[:cap]

def _gen_six(p: dict, key: str):
    pid = p["id"]
    is_review = p.get("paper_type") == "review"
    _, claims, annos = db.get_analysis(pid)
    paras = db.get_paragraphs(pid)                 # 全程一份快照：别为角色过滤反复整载
    by_idx = {x["idx"]: x for x in paras}
    def role(roles: set, cap: int = 8):
        return _paras_of_role(annos, by_idx, roles, cap)
    if key == "principle":
        return llm.answer_principle(p["title"], claims, paras,
                                    kind="review" if is_review else "research")
    if key == "method":
        if is_review:
            raise HTTPException(400, "综述没有实验层——看「它把文献怎么组织的？」")
        return llm.answer_method(p["title"], claims, paras)
    if key == "how":
        if is_review:
            return llm.answer_how_review(p["title"], claims, paras)
        raise HTTPException(400, "研究型论文不单独生成这一问")
    if key == "motive":
        return llm.answer_motive(p["title"], paras,
                                 role({"gap"}),
                                 role({"background"}, 6),
                                 claims)
    if key == "next":
        return llm.answer_next(p["title"], paras,
                               role({"limitation"}),
                               role({"extension"}, 5),
                               claims)
    if key == "doubt":
        return llm.answer_doubt(p["title"], paras, role({"limitation"}), claims,
                                kind="review" if is_review else "research")
    if key == "lens":
        # 管线期 lens 与 summary 并行生成，调用方传入的快照里 summary 还是 None——
        # 照抄快照会让「换个学科」永远拿不到那句话，还被 v=2 缓存固化。现场重读一次。
        p = db.get_paper(pid) or p
    # 缓存字段一律走 _json_of：老版本/半截写盘会留下坏 JSON，直接 json.loads 会让
    # 这一问独自 500，错误还会被 human_error 归成"模型没按约定格式回"（甩给模型）
    s = _json_of(p.get("summary"))
    return llm.answer_lens(p["title"], (s or {}).get("one_line", ""), claims, paras)

def _demo_terms(title, paras) -> dict:
    return {"terms": [{"en": "demo term", "zh": "演示术语", "kind": "method"}], "abbrs": {}}

def _save_terms(pid: str, got) -> int:
    """术语与缩写一次落库——它们是同一次调用的产物，分两处写迟早会只写一半。"""
    if _pid_gone(pid):
        return 0
    got = got if isinstance(got, dict) else {}
    terms = got.get("terms") or []
    if terms:
        db.glossary_put_ai(pid, terms)
    added = db.merge_abbrs(pid, got.get("abbrs") or {})
    if added:
        _applog(f"[eggpaper] 本篇缩写补了 {added} 条")
    return len(terms)

# text 型演示答案：zh/en 各一段，_mock_six 查表
_MOCK_TEXT = {
    "motive": ("〔演示模式〕现有做法依赖随机、不可控的关键步骤，产出的质量波动大、没法按需设计 [¶3]；"
               "这件事卡住了下游一整类应用，而这到今天没有好解法 [¶2]——"
               "所以这篇要用一套可设计的规则化结构来实现稳定的高质量输出。",
               "[demo mode] Current practice relies on random, uncontrollable steps, so the "
               "output quality fluctuates and cannot be designed [¶3]; this blocks a whole "
               "class of downstream applications and still lacks a good solution [¶2] — hence "
               "this work uses a designable, regular structure for stable, high-quality output."),
    "principle": ("〔演示模式〕这套做法成立靠的是一个已知的物理效应：结构规则化之后能量面变平，"
                  "反应不再赌概率 [¶4]——本质上是把随机过程换成了可设计的确定性路径。",
                  "[demo mode] This rests on a known physical effect: a regular structure "
                  "flattens the energy landscape, so formation stops being a gamble [¶4] — "
                  "in essence a random process replaced by a designable, deterministic path."),
    "method": ("〔演示模式〕它用原位表征盯住全程，配一组对照实验把关键变量单离出来 [¶8]；"
               "比前人改进在免掉了不可控的随机步骤，同样的产量下批次间差异小了一个量级 [¶10]。",
               "[demo mode] It tracks the whole process with in-situ characterization plus "
               "controls that isolate the key variable [¶8]; the improvement is dropping the "
               "uncontrollable random step, with batch variability an order lower [¶10]."),
    "how": ("〔演示模式〕这篇综述按它的分类线索把文献组织成三大块，逐块对比优劣，"
            "最后落到位开放问题上 [¶5]。",
            "[demo mode] This review organizes the literature into three blocks along its "
            "own classification, compares them block by block, and closes with open "
            "questions [¶5]."),
}

def _mock_six(key: str) -> dict:
    if key in _MOCK_TEXT:
        return {"text": _demo_txt(*_MOCK_TEXT[key])}
    if key == "lens":
        return {"v": 3, "items": [
            {"lead": _demo_txt("做实验的", "Experimentalist"),
             "text": _demo_txt("〔演示模式〕会盯着原位数据太少这件事——漂亮的机理说法要配原位证据才站得住。",
                               "[demo mode] Would zero in on how thin the in-situ data is — a pretty "
                               "mechanism story needs in-situ evidence to stand."),
             "ask": _demo_txt("有没有原位数据支持这条机理？", "Is there in-situ data supporting this mechanism?")},
            {"lead": _demo_txt("做计算的", "Simulation"),
             "text": _demo_txt("〔演示模式〕想拿这套实验数字先验一验自己的力场，对不上就说明模型缺项。",
                               "[demo mode] Would validate a force field against these experimental numbers; "
                               "mismatches would reveal missing terms."),
             "ask": _demo_txt("这套数据能用来校准力场吗？", "Can this data calibrate a force field?")},
            {"lead": _demo_txt("做政策的", "Policy"),
             "text": _demo_txt("〔演示模式〕看到的是成本表里那笔没算进去的外部性，会追问谁承担。",
                               "[demo mode] Sees the unpriced externality missing from the cost table, and "
                               "asks who bears it."),
             "ask": _demo_txt("成本核算包含外部性吗？", "Does the cost accounting include externalities?")},
        ]}
    if key == "doubt":
        return {"v": 1, "items": [
            {"lead": _demo_txt("可疑", "Suspicious"),
             "text": _demo_txt("〔演示模式〕关键结论只来自单一体系的少量数据点，换体系的普适性还没人验证 [¶10]。",
                               "[demo mode] The key conclusion rests on few data points from a single "
                               "system; nobody has tested whether it generalizes [¶10]."),
             "ask": _demo_txt("换一类体系这条结论还成立吗？", "Would this conclusion hold in another system?")},
            {"lead": _demo_txt("含糊", "Vague"),
             "text": _demo_txt("〔演示模式〕响应度没给测量口径与误差，读者核不了 [¶6]。",
                               "[demo mode] The responsivity comes without measurement conditions or "
                               "error bars, so readers cannot check it [¶6]."),
             "ask": _demo_txt("这个数的误差和测量条件是什么？", "What are the error bars and conditions?")},
        ]}
    return {"v": 3, "items": [
        {"lead": _demo_txt("论文已说明", "Admitted"),
         "text": _demo_txt("〔演示模式〕换一组对照/基线把这条结论单离出来 [¶12]。",
                           "[demo mode] Isolate this conclusion with a different set of controls or baselines [¶12]."),
         "ask": _demo_txt("怎么设计对照/基线才能单离这条结论？", "What controls or baselines would isolate this conclusion?")},
        {"lead": _demo_txt("新方向", "New direction"),
         "text": _demo_txt("〔演示模式〕把这套判据搬去另一类体系或数据集，够撑一篇新论文：对象换了、结论还没人验证过 [¶18]。",
                           "[demo mode] Carry these criteria to another class of systems or datasets — enough for "
                           "a new paper: new subject, conclusions nobody has tested yet [¶18]."),
         "ask": _demo_txt("搬到另一类体系要先验证什么？", "What must be validated first in the new setting?")},
    ]}

def _stale_answer(key: str, val) -> bool:
    """lens/next 带 v 版本号：v<3 的旧口径缓存当"没生成过"（全量/单键两条读路同判据，
    作废口径只此一处）。"""
    return key in ("lens", "next") and isinstance(val, dict) and (val.get("v") or 0) < 3

@app.get("/api/papers/{pid}/six-answers")
def six_answers(pid: str):
    """只读缓存：打开一篇论文时问一次，没生成过的题返回 null。
    lens/next 带 v 版本号（单键端点同样校验）——v<3 的旧口径缓存在这里一并作废。"""
    _paper_or_404(pid)
    out = {}
    for key, val in (db.answers_all(pid) or {}).items():
        out[key] = None if _stale_answer(key, val) else val
    return out

def _six_compute(pid: str, key: str) -> dict:
    """单键生成+落库：析读管线的预生成与端点的现场请求**共用同一把 per-key 锁**——
    谁先拿到锁谁生成，后到的直接命中刚写回的缓存。以前管线在锁外生成、端点在锁内
    生成，析读一完成前端立即来请求，同一问会真的生成两遍（两遍钱）。"""
    with _key_lock("six:" + pid + ":" + key):
        cached = db.answer_get(pid, key)
        if cached and _stale_answer(key, cached):
            cached = None
        if cached:
            return cached
        p = db.get_paper(pid)
        if not p:
            _paper_or_404(pid)          # 排队等生成时论文被删：404，别把 None 递进生成
        data = _gen_six(p, key)
        if not (data.get("text") or data.get("items")):
            raise HTTPException(503, "模型没返回内容，重试一次")
        if _pid_gone(pid):      # 生成隔着一次 LLM 调用，期间论文可能已被删/被替换：只返回不落库
            return data
        db.answer_put(pid, key, data)
        return data

@app.get("/api/papers/{pid}/six-answers/{key}")
def six_answer(pid: str, key: str):
    _paper_or_404(pid)
    if key not in SIX_KEYS:
        raise HTTPException(404, "没有这个问题")
    if _demo_mode():
        return _mock_six(key)
    _require_paras(pid)
    return _six_compute(pid, key)

@app.post("/api/compare")
def compare_papers(body: dict):
    """数据对比表：勾选的几篇各抽一次、按同一 schema 拼成一张表（格子带 ¶ 锚点）。

    cols（自定义列口径）：用户自定义列 [{label, hint?}]，给了它就抽纯自定义列；
    没有 cols 照旧六维勾选。两种口径同一入口，篇数都钳 12——每篇各一次调用，
    成本只随篇数走，口径不该影响上限。"""
    raw_ids = (body or {}).get("ids")
    ids = [x for x in raw_ids if isinstance(x, str)] if isinstance(raw_ids, list) else []
    ids = list(dict.fromkeys(ids))       # 重复 id 去重保序：同一篇抽两次 = 重复列 + 白花的缓存键
    ids = ids[:12]
    if len(ids) < 2:
        raise HTTPException(400, "至少选两篇才能对比")
    cols = [c for c in ((body or {}).get("cols") or [])
            if isinstance(c, dict) and str(c.get("label") or "").strip()]
    dims = [str(k) for k in ((body or {}).get("dims") or []) if str(k) in compare.KEYS]
    papers = []
    for pid in ids:
        p = db.get_paper(pid)
        if not p:
            raise HTTPException(404, "有篇论文不存在，刷新文库后再试")
        if not db.get_paragraphs(pid, with_lines=False):
            raise HTTPException(400, "扫描件没有文字层，进不了对比")
        papers.append(p)

    def material_of(xpid):
        _status, claims, annos = db.get_analysis(xpid)
        return db.get_paragraphs(xpid, with_lines=False), claims, annos

    demo = _demo_mode()
    # cols 在场 = 自定义列口径：纯用户自定义列（dims=[]），不掺固定维度；没有 cols 照旧六维勾选
    meta = compare.dims_meta(dims=[] if cols else (dims or None), cols=cols or None)
    cells = compare.extract_all(papers, material_of, demo=demo, meta=meta)
    return {"papers": [{k: p.get(k) for k in ("id", "title", "filename", "authors", "year")}
                       for p in papers],
            "cells": cells,
            "dims": [{"k": k, "label": label} for k, label, _h in meta],
            "demo": demo}

@app.get("/api/papers/{pid}/method-card")
def method_card(pid: str, cached: bool = False):
    def real(p):
        what, fn = (("谱系卡", llm.survey_card) if p.get("paper_type") == "review"
                    else ("方法卡", llm.method_card))
        data = fn(p["title"], db.get_paragraphs(pid))
        _require_shape(data, ("goal", "steps"), what)
        return data
    return _gen_card(pid, "method_card", "mcard", what="方法卡", shape=("goal", "steps"),
                     demo_fn=lambda p: {"goal": _demo_txt("〔演示〕可复现 protocol", "[demo] Reproducible protocol"),
                                      "system": _demo_txt("演示体系", "demo system"),
                                      "conditions": _demo_txt("演示条件", "demo conditions"),
                                      "steps": [_demo_txt("步骤一", "Step one"), _demo_txt("步骤二", "Step two")],
                                      "notes": ""},
                     real_fn=real, cached=cached, cached_value={})

@app.get("/api/papers/{pid}/citation")
def paper_citation(pid: str, cached: bool = False, refresh: bool = False):
    """引用信息：作者/刊名/卷期页/DOI，抄一次存下来，之后所有格式都是本地排版。

    和 method-card 同一套规矩：cached=1 只读缓存，没有就明说没有——打开浮层
    不该悄悄花掉一次模型调用。refresh=1 是「重新识别」：认错了要能重认一次。
    排版在 citation.py 里做（模型只负责抄字段）。
    """
    p = _paper_or_404(pid)

    def _cached_hit(pp):
        """有缓存就排版返回、没有回 None（锁外快返与锁内复查共用一份）。"""
        if pp["citation"] and not refresh:
            meta = _json_of(pp["citation"])
            if meta:              # 坏缓存当没有，走重生成
                return {"meta": meta, "groups": citation.groups(meta)}
        return None

    r = _cached_hit(p)
    if r is not None:
        return r
    if cached:
        return {"meta": None, "groups": []}
    if _demo_mode():
        meta = {"authors": [{"family": "Zhang", "given": "Wei"}, {"family": "Li", "given": "Na"}],
                "title": _demo_txt("〔演示〕一篇论文的标题", "[demo] A paper title"),
                "journal": "Journal of Demo Studies",
                "journal_abbr": "J. Demo Stud.", "year": "2024", "volume": "12",
                "issue": "3", "pages": "345-352", "doi": "10.0000/demo.2024.12345"}
    else:
      with _key_lock("cite:" + pid):
        p = db.get_paper(pid)
        if not p:
            _paper_or_404(pid)      # 排队等生成时论文被删：404，别 None 下标炸 500
        r = _cached_hit(p)
        if r is not None:
            return r
        src = pdfparse.citation_source(_paper_src_or_404(pid, p))
        raw = llm.extract_citation(p["title"], src)
        meta = citation.sanity(raw, src, fallback_title=p["title"], fallback_author=p["authors"] or "")
        if not (meta.get("title") or meta.get("authors")):
            raise HTTPException(503, "首页没认出文献信息")
        # 落库必须在锁内：放在锁外的话，第二个等待者拿锁时还读不到，会再花一次调用
        db.update_paper(pid, citation=db.dumps_cjk(meta))
    return {"meta": meta, "groups": citation.groups(meta)}

@app.post("/api/cite-table")
def cite_table(body: dict):
    """批量引用表格（文库多选导出）：每篇一行，引用格式全部本地排版。

    **不触发任何模型调用**——没识别过刊头的那几篇如实标 recognized=false，
    让前端提示用户补识别，而不是悄悄为几十篇各花一次调用。
    """
    raw_ids = (body or {}).get("ids")
    ids = [x for x in raw_ids if isinstance(x, str)] if isinstance(raw_ids, list) else []
    out = []
    for pid in ids[:200]:
        p = db.get_paper(pid)
        if not p:
            continue
        meta = _json_of(p["citation"]) if p["citation"] else None
        row = {"id": pid, "title": p["title"] or p["filename"], "authors": p["authors"] or "",
               "year": p["year"] or "", "recognized": bool(meta)}
        if meta:
            row["cite"] = {r["k"]: r["text"] for g in citation.groups(meta) for r in g["rows"]}
        out.append(row)
    return out

@app.get("/api/papers/{pid}/export.md")
def export_md(pid: str):
    """导出笔记 .md。栏目标题跟随界面语言（ui_lang），内容本身保持原文
    （摘要等生成内容用生成时的语言，问答与页边批注是你说过的话——导出不做翻译）。"""
    p = _paper_or_404(pid)
    en = (config.load().get("ui_lang") or "zh") == "en"
    T = {
        "skeleton": ("论证骨架", "Argument skeleton"),
        "six": ("六个问题", "Six questions"),
        "mcard": ("方法卡", "Method card"),
        "scard": ("谱系卡", "Survey map"),
        "notes": ("页边批注与查译", "Margin notes & lookups"),
        "cited": ("库内互引", "Cited by in library"),
        "qa": ("问答", "Q&A"),
        "you": ("你", "You"),
    }
    def L(key):
        return T[key][1] if en else T[key][0]

    lines = [f"# {p['title'] or p['filename']}", ""]
    if p["summary"]:
        s = _json_of(p["summary"])
        lines += [f"**{s.get('one_line', '')}**", ""]
        if s.get("novelty"):
            lines.append(f"- 创新点：{s['novelty']}")
        lines.append(f"- 发现：{s.get('findings', '')}")
        kw = " · ".join(str(k).strip() for k in (s.get("keywords") or []) if str(k).strip())
        if kw:
            lines.append(f"- 关键词：{kw}")
        lines.append("")
    status, claims, annos = db.get_analysis(pid)
    if claims:
        lines += [f"## {L('skeleton')}", ""]
        for c in claims:
            lines.append(f"- **{c['id']} {c['text']}**")
            for a in c["anchors"]:
                anno = annos.get(str(a))
                if anno:
                    lines.append(f"  - ¶{a}：{anno['purpose']}")
            lines.append("")
    # 七问里现场生成的那几问（answers 表，原先导出漏了——写综述/组会汇报时最值钱的恰是它们）。
    # ⑤「哪里可疑」此前漏在表外，补上；④「结论有哪些」就是上面的骨架主张-证据链，不重复一遍。
    answers = db.answers_all(pid)
    is_rev = (p.get("paper_type") == "review")
    SIX_LABELS = {"motive": ("① 要解决什么、为什么", "① What & why"),
                  "principle": ("② 原理是什么", "② Underlying principle"),
                  "method": ("③ 怎么解决的", "③ How it was done"),
                  "how": ("③ 它把文献怎么组织的", "③ How the literature is organized"),
                  "doubt": ("⑤ 哪里可疑、还有什么没解决", "⑤ Doubts & open issues"),
                  "next": ("⑥ 还能做什么", "⑥ What next"),
                  "lens": ("⑦ 换个学科怎么看", "⑦ Other lenses")}
    six_lines = []
    for k, lab in SIX_LABELS.items():
        a = answers.get(k)
        if not isinstance(a, dict):
            continue
        if a.get("text"):
            six_lines.append(f"- **{lab[1] if en else lab[0]}**：{a['text']}")
        for it in a.get("items") or []:
            head = f"{lab[1] if en else lab[0]} · {it.get('lead', '')}".strip(" ·")
            six_lines.append(f"- **{head}**：{it.get('text', '')}")
            if it.get("ask"):
                six_lines.append(f"  - ↗ {it['ask']}")
    if six_lines:
        lines += [f"## {L('six')}", ""] + six_lines + [""]
    if p["method_card"]:
        mc = _json_of(p["method_card"])
        if mc.get("goal") or mc.get("steps"):
            lines += [f"## {L('scard' if is_rev else 'mcard')}", ""]
            if mc.get("goal"):
                lab = ("Position" if is_rev else "Goal") if en else ("定位" if is_rev else "目标")
                lines.append(f"- {lab}: {mc['goal']}")
            for s in mc.get("steps") or []:
                lines.append(f"- {s}")
            if mc.get("notes"):
                lab = "Notes" if en else ("入门" if is_rev else "注意")
                lines.append(f"- {lab}: {mc['notes']}")
            lines.append("")
    # 库内互引：谁引了这篇、引的哪句、什么立场（扫描免费，立场只读缓存）
    cb = []
    try:
        cached = db.cite_ctx_all(pid)
        for x in _citedby_scan(p):
            for h in (cached.get(x["citing"]["id"]) or x["hits"])[:3]:
                cb.append((x["citing"], h))
    except Exception:
        cb = []
    if cb:
        # 立场键必须是 contrast（LLM 输出与 _CITE_STANCE_ZH 的取值）：写成 contest 时
        # ST.get("contrast") 落空、取默认的"提及"，导出的 .md 里"质疑"一直显示成"提及"
        ST = {"support": ("支持", "supports"), "contrast": ("质疑", "questions"),
              "mention": ("提及", "mentions")}
        lines += [f"## {L('cited')}", ""]
        for citing, h in cb:
            st = ST.get(h.get("stance") or "mention", ST["mention"])
            yr = f" {citing['year']}" if citing.get("year") else ""
            t = citing.get("title") or citing.get("filename") or "（无标题）"
            lines.append(f"- **{t}{yr}**（{st[1] if en else st[0]}）：{h.get('quote') or ''}")
        lines.append("")
    notes = [n for n in db.get_marginalia(pid) if n["kind"] in USER_NOTE_KINDS]
    if notes:
        lines += [f"## {L('notes')}", ""]
        for n in notes:
            zh = KIND_ZH.get(n["kind"], n["kind"])
            lines.append(f"- **[你 · {zh}] {n['note']}** — “{n['quote'][:48]}”")
        lines.append("")
    convs = db.conv_list(pid)
    for c in convs:
        msgs = db.qa_history(pid, c["id"])
        if not msgs:
            continue
        if not any(m["content"] for m in msgs):
            continue
        lines += [f"## {L('qa')} · {c['title']}", ""]
        for m in msgs:
            role = ("EGGPAPER") if m["role"] == "assistant" else L("you")
            lines.append(f"**{role}:** {m['content']}")
            lines.append("")
        lines.append("")
    md = "\n".join(lines)
    return Response(content=md, media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename=eggpaper-{pid}.md"})

_CSV_FORMULA_LEADS = ("=", "+", "-", "@", "\t", "\r")

def _csv_cell(v) -> str:
    # Excel/WPS 把 =+-@、制表符开头的单元格当公式执行（AI 词条源自 PDF 原文，
    # 恶意构造的论文能把公式种进词表）：统一前置 ' 中性化
    v = str(v)
    return "'" + v if v[:1] in _CSV_FORMULA_LEADS else v

def _csv_response(body: str) -> Response:
    """术语导出的响应壳（单篇词表与学科库两个端点共用同一套口径）。"""
    return Response(content=body, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=eggpaper-terms.csv"})

def _csv_buf(header: list, rows) -> str:
    """CSV 导出共用管线：BOM + 表头 + 逐格公式注入防护。返回完整文本，响应壳交给
    _csv_response。两个词表导出端点原来各自抄了一份组装代码，改口径要改两处、
    漏一处就是两种行为——收在这里一处说话。"""
    import csv
    import io
    buf = io.StringIO()
    buf.write("\ufeff")     # UTF-8 BOM：Excel 打开中文不乱码
    w = csv.writer(buf)
    w.writerow(header)
    for row in rows:
        w.writerow([_csv_cell(c) for c in row])
    return buf.getvalue()

@app.get("/api/papers/{pid}/glossary/export.csv")
def glossary_export(pid: str):
    _paper_or_404(pid)
    rows = [(r["term_en"], r["term_zh"], r["source"]) for r in db.glossary_list(pid)]
    return _csv_response(_csv_buf(["term_en", "term_zh", "source"], rows))

# ---------------- 图表速览 ----------------

FIG_GFX_GAP = 18.0
FIG_TXT_GAP = 16.0
FIG_WIDE_GAP = 9.0
FIG_RULE_TXT_GAP = 8.0
FIG_HDR_FOOT = 46.0
FIG_MIN_W, FIG_MIN_H = 60.0, 40.0

CAPTION_RE = re.compile(
    r"^\s*(Fig(?:ure)?s?\.?|Table|Tab\.?|Scheme|Algorithm|Chart|Plate|图|表|算法|附图|附表)"
    r"\s*\.?\s*(\d+)([a-z])?\s*[:.．|—–－—:，,]?\s*", re.I)

def _caption_of(first_text):
    """块首行像不像题注。返回 (kind, label)；不是题注返回 None。
    "Table 3"（裸标签）、"Fig. 1. …"、"图 1：…" 都算；"Figure 3 shows"（正文
    开头顺嘴提到图）不算——数字后面得是分隔符、行尾、大写词或 CJK 才行。"""
    m = CAPTION_RE.match(first_text)
    if not m:
        return None
    rest = first_text[m.end():]
    if rest and not (rest[0].isupper() or ord(rest[0]) > 0x2e7f):
        return None
    prefix = m.group(1).lower()
    kind = "table" if prefix.startswith(("tab", "表", "附表", "算法")) else "figure"
    label = m.group(0).strip().rstrip(":.．|—–－—:，, ").strip()
    return kind, label

def _two_columns(page, lines):
    """正文长行的分布像不像双栏。跨栏长行（通栏图表、页眉）占太多则是单栏。"""
    pw = page.rect.width
    longs = [r for r, t in lines if r.width > 0.35 * pw and len(t.strip()) >= 20]
    cross = sum(1 for r in longs if r.x0 < pw * 0.47 and r.x1 > pw * 0.53)
    left = sum(1 for r in longs if r.x1 <= pw * 0.53)
    right = sum(1 for r in longs if r.x0 >= pw * 0.47)
    return left >= 8 and right >= 8 and cross < 0.3 * (left + right)

def _wide_flags(rects, colw_of, lm, rm):
    """逐行判"宽行"：超过栏宽七成；或者 140pt 以上、顶到页边还和邻行同宽同头
    （半栏排版的正文段落）。后者是为了认出单栏页里左右并排的两段正文——
    它们是屏障；表格里的单元格段落（缩在页面中间）不算。"""
    flags = []
    for r in rects:
        f = r.width >= 0.7 * max(colw_of(r), 60)
        if not f and r.width >= 140 and (r.x0 <= lm + 8 or r.x1 >= rm - 8):
            for r2 in rects:
                if r2 is r:
                    continue
                if (abs(r2.x0 - r.x0) <= 6 and abs(r2.width - r.width) <= 0.25 * r.width
                        and max(r.y0 - r2.y1, r2.y0 - r.y1) <= 1.8 * max(r.height, 1)):
                    f = True
                    break
        flags.append(f)
    return flags

def _x_overlap_ok(rect, R, gfx: bool, gap: float) -> bool:
    """候选是否与当前并集在横向上"算同一列"（纯读，不改任何状态）。

    完全落在并集里当然算；否则要求横向重叠达到阈值（图元 15%、文字 25%），
    或者"图元 + 贴得极近 + 横向盖住并集"（通栏图）。
    """
    if rect.x0 >= R.x0 - 2 and rect.x1 <= R.x1 + 2:
        return True
    xov = min(R.x1, rect.x1) - max(R.x0, rect.x0)
    if xov >= (0.15 if gfx else 0.25) * rect.width:
        return True
    return bool(gfx and gap <= 8 and rect.x0 <= R.x0 + 4 and rect.x1 >= R.x1 - 4)

def _blocked_by(rect, R, blockers, up: bool) -> bool:
    """候选与并集之间是否隔着正文块/别的题注（隔着就到此为止，别越过去吸）。

    只认"横向有实质重叠（>30% 宽）且正好落在这一侧"的遮挡物；纯读，不改状态。
    """
    for b in blockers:
        if min(R.x1, b.x1) - max(R.x0, b.x0) <= 0.3 * b.width:
            continue
        if up:
            if b.y1 <= R.y0 + 1 and b.y0 >= rect.y1 - 1:
                return True
        elif b.y0 >= R.y1 - 1 and b.y1 <= rect.y0 + 1:
            return True
    return False

def _band_fill(up, cap_rect, cands, blockers, y_clip):
    """从题注往一个方向吸收紧邻的图元。cands: [(rect, 是图元, 是宽文字行)]；
    blockers: 正文块/别的题注（撞上就当没看见，整个填充到此为止）。
    返回 (吸收并集, 图形面积, 吸收的文字总高)。"""
    import pymupdf
    R = pymupdf.Rect(cap_rect)
    graphic, txt_h = 0.0, 0.0
    rule_hit = False
    used = [False] * len(cands)
    while True:
        best, best_gap = None, 1e9
        for ci, (rect, gfx, wide) in enumerate(cands):
            if used[ci]:
                continue
            if up:
                if rect.y1 > R.y0 + 2 or rect.y1 < y_clip:
                    continue
                gap = R.y0 - rect.y1
            else:
                if rect.y0 < R.y1 - 2 or rect.y0 > y_clip:
                    continue
                gap = rect.y0 - R.y1
            if gap < -2 or gap >= best_gap:
                continue
            lim = FIG_GFX_GAP if gfx else (FIG_WIDE_GAP if wide else FIG_TXT_GAP)
            if rule_hit and not gfx:
                lim = min(lim, FIG_RULE_TXT_GAP)
            if gap > lim:
                continue
            if not _x_overlap_ok(rect, R, gfx, gap):
                continue
            if _blocked_by(rect, R, blockers, up):
                continue
            best, best_gap = ci, gap
        if best is None:
            break
        ci = best
        used[ci] = True
        rect, gfx, wide = cands[ci]
        R |= rect
        if gfx:
            graphic += max(rect.get_area(), rect.width * 1.5 if rect.height <= 2.5 else 0.0)
            if rect.height <= 2.5 and rect.width >= 0.55 * max(R.width, 200):
                rule_hit = True
        else:
            txt_h += rect.height
    return R, graphic, txt_h

def _iou(a, b):
    inter = (a & b).get_area()
    return inter / max(a.get_area() + b.get_area() - inter, 1e-6)

def _page_candidates(page) -> dict:
    """一页的图表候选素材（纯计算，数值口径见各自的常量）：题注 caps、图元/文字候选
    cands、正文屏障 blockers、图片 imgs，外加页宽高与天头地脚。同一页只取一遍
    get_text/get_image_info/get_drawings。"""
    import pymupdf
    pw, ph = page.rect.width, page.rect.height
    dtext = page.get_text("dict")
    # 图表区域要的是**原始行**（不做连字/空白归一）：归一化会改行宽与行距的相邻判定，
    # 这份 PDF 的图区是纯函数，动一个数就动全部区域框
    lines = pdfparse.text_lines(page, clean=False, blocks=dtext.get("blocks", []))
    two_col = _two_columns(page, lines)
    lm = min((r.x0 for r, t in lines), default=36.0)
    rm = max((r.x1 for r, t in lines), default=pw - 36.0)
    mid = pw / 2

    def colw_of(r):
        if not two_col:
            return rm - lm
        return (mid - 6 - lm) if (r.x0 + r.x1) / 2 < mid else (rm - mid - 6)

    ytop, ybot = FIG_HDR_FOOT, ph - FIG_HDR_FOOT
    caps, cands, blockers = [], [], []
    for blk in dtext.get("blocks", []):
        if blk.get("type") != 0:
            continue
        blines = [ln for ln in blk.get("lines", []) if ln.get("spans")]
        if not blines:
            continue
        brect = pymupdf.Rect(blk["bbox"])
        if brect.y1 < ytop or brect.y0 > ybot:
            continue
        rects = [pymupdf.Rect(ln["bbox"]) for ln in blines]
        texts = ["".join(sp["text"] for sp in ln["spans"]) for ln in blines]
        cap = _caption_of(texts[0].strip())
        if cap:
            cap_txt = re.sub(r"\s+", " ", " ".join(t.strip() for t in texts))
            caps.append({"rect": brect, "kind": cap[0], "label": cap[1],
                         "caption": cap_txt})   # 全文，不截断——灯箱图注中文版要完整呈现
            continue
        wide = _wide_flags(rects, colw_of, lm, rm)   # O(n²) 邻行扫描，只算一遍
        if sum(wide) * 2 >= len(rects):
            blockers.append(brect)
        else:
            cands.extend((r, False, w) for r, w in zip(rects, wide))
    imgs = page.get_image_info()
    for info in imgs:
        r = pymupdf.Rect(info["bbox"])
        if r.width < 12 or r.height < 8 or r.get_area() > 0.85 * pw * ph:
            continue
        if r.y1 < ytop or r.y0 > ybot:
            continue
        cands.append((r, True, False))
    for dr in page.get_drawings():
        r = dr["rect"]
        if r.width < 5 or (r.height < 3 and r.width < 18) or r.get_area() > 0.85 * pw * ph:
            continue
        if r.y1 < ytop or r.y0 > ybot:
            continue
        if r.height < 1:
            r = pymupdf.Rect(r.x0, r.y0 - 0.75, r.x1, r.y1 + 0.75)
        cands.append((r, True, False))
    return {"caps": caps, "cands": cands, "blockers": blockers, "imgs": imgs,
            "pw": pw, "ph": ph, "ytop": ytop, "ybot": ybot, "lm": lm, "rm": rm}

def _drop_rule_pairs(cands: list, blockers: list, lm: float, rm: float) -> list:
    """整批长横线里成对夹住屏障的（表格框线/页眉线这类排版线，横跨半页以上）整个
    剔除：它们不是图元，留着会把近邻的题注吸成一个假区域。返回筛过的 cands。"""
    import pymupdf
    long_rules = [(i, r) for i, (r, g, w) in enumerate(cands)
                  if g and r.height <= 4.5 and r.width >= 0.5 * (rm - lm)]
    drop = set()
    for i1, r1 in long_rules:
        for i2, r2 in long_rules:
            if i2 == i1 or abs(r1.x0 - r2.x0) > 8 or abs(r1.x1 - r2.x1) > 8:
                continue
            span = pymupdf.Rect(r1.x0, min(r1.y0, r2.y0), r1.x1, max(r1.y1, r2.y1))
            if 12 < span.height < 130 and any(
                    (b & span).get_area() > 0.5 * b.get_area() for b in blockers):
                drop.update((i1, i2))
    if not drop:
        return cands
    return [c for i, c in enumerate(cands) if i not in drop]

def _pick_regions(pg: dict, pno: int, cands: list) -> list:
    """从每条题注出发上下各吸收一次紧邻图元，选成区的一边；同页重叠去重，
    非首页再补"题注都没认到的大图"（首页的大图多是期刊 logo/排版图，不当天图收）。"""
    import pymupdf
    pw, ph, ytop, ybot = pg["pw"], pg["ph"], pg["ytop"], pg["ybot"]
    caps, blockers, imgs = pg["caps"], pg["blockers"], pg["imgs"]
    entries = []
    for c in sorted(caps, key=lambda x: x["rect"].y0):
        up_R, up_g, up_t = _band_fill(True, c["rect"], cands, blockers, ytop)
        dn_R, dn_g, dn_t = _band_fill(False, c["rect"], cands, blockers, ybot)
        if up_g or dn_g:
            pick_up = (c["kind"] == "figure") if up_g == dn_g else up_g > dn_g
        elif up_t != dn_t:
            pick_up = up_t > dn_t
        else:
            pick_up = (c["kind"] == "figure")
        R, g, t = (up_R, up_g, up_t) if pick_up else (dn_R, dn_g, dn_t)
        if g < 250 and t < 18:
            continue
        R = (pymupdf.Rect(R.x0 - 3, R.y0 - 3, R.x1 + 3, R.y1 + 3)
             & pymupdf.Rect(3, 3, pw - 3, ph - 3))
        if R.width < FIG_MIN_W or R.height < FIG_MIN_H:
            continue
        c["region"] = R
        entries.append(c)
    entries.sort(key=lambda x: x["rect"].y0)
    keep = []
    for e in entries:
        if any(_iou(e["region"], k["region"]) > 0.45 for k in keep):
            continue
        keep.append(e)
    if pno > 0:
        for info in imgs:
            r = pymupdf.Rect(info["bbox"])
            if r.width < 180 or r.height < 110 or r.get_area() > 0.55 * pw * ph:
                continue
            if r.y1 < ytop or r.y0 > ybot:
                continue
            if any((rg & r).get_area() > 0.4 * r.get_area() for rg in
                   [e["region"] for e in keep]):
                continue
            keep.append({"region": r, "kind": "figure", "label": "", "caption": ""})
    out = []
    for c in keep:
        r = c["region"]
        out.append({"page": pno, "x0": round(r.x0, 1), "y0": round(r.y0, 1),
                    "x1": round(r.x1, 1), "y1": round(r.y1, 1),
                    "kind": c["kind"], "label": c.get("label") or "",
                    "caption": c.get("caption") or ""})
    return out

def _figure_regions(path):
    """一份 PDF 里的全部图表区域。图的位置是**这份 PDF 的纯函数**，
    端点层按 路径+mtime 缓存，同一份文件算一次就够。"""
    import pymupdf
    out = []
    doc = pymupdf.open(path)
    try:
        for pno in range(len(doc)):
            pg = _page_candidates(doc[pno])
            cands = _drop_rule_pairs(pg["cands"], pg["blockers"], pg["lm"], pg["rm"])
            out += _pick_regions(pg, pno, cands)
        out.sort(key=lambda e: (e["page"], e["y0"]))
    finally:
        doc.close()
    return out

_fig_cache = {}
_fig_inflight = {}
_fig_fail: dict = {}          # (src,mtime) -> {"at": ts, "msg": 人话}：坏 PDF 的负缓存
_FIG_FAIL_TTL = 300           # 5 分钟内不重扫：图表扫描是整本 O(n²) 邻行匹配，坏 PDF 每次都重跑纯属烧 CPU
_fig_png_cache: dict = {}     # (src,mtime,page,裁剪框,dpi) -> 渲染好的 PNG 字节（重复取图不重光栅化）

def _figures_for(pid: str, p: dict) -> list:
    """这篇论文的图表区域列表（内存缓存按"路径+mtime"记账，画一次到处用）。

    失败也记一笔负缓存（短 TTL）：打不开/解析炸的 PDF 不然每个请求都重跑一遍整本扫描。
    负缓存绝不覆盖成功结果——成功路径先查缓存、失败单独一本账。"""
    src = _paper_src_or_404(pid, p)
    try:
        key = (src, os.path.getmtime(src))
    except OSError:
        key = (src, 0)

    def _bad() -> str:
        bad = _fig_fail.get(key)
        if bad and time.time() - bad["at"] < _FIG_FAIL_TTL:
            return bad["msg"]
        return ""

    if key in _fig_cache:
        return _fig_cache[key]
    msg = _bad()
    if msg:
        raise HTTPException(400, msg)
    mine_ev = threading.Event()
    prev = _fig_inflight.setdefault(key, mine_ev)
    if prev is not mine_ev:
        # 已经有人在算这份图表：等它，别双份跑 O(n²) 邻行扫描。
        # 判据必须拿"自己新建的那份"去比——setdefault 返回的是现有值，拿它比永远
        # 相等，等待段会整个变成死代码（这正是修之前的形态，两个窗格同开一篇就双份烧 CPU）
        prev.wait(120)
        if key in _fig_cache:
            return _fig_cache[key]
        msg = _bad()             # 先跑的那个失败了：负缓存直接复用，不重扫
        if msg:
            raise HTTPException(400, msg)
        # 等超时、先跑者还没出结果（超大的 PDF）：不陪等了，自己接管起一轮重算，
        # 用自己这份旗子。极端情形多算一遍，好过整个请求陪着挂死
        mine_ev = threading.Event()
        _fig_inflight[key] = mine_ev
    try:
        try:
            out = _figure_regions(src)
        except Exception as e:
            msg = f"解析图表失败：{str(e)[:120]}"
            _fig_fail[key] = {"at": time.time(), "msg": msg}
            fsutil.cap_oldest(_fig_fail, 16)   # 负缓存也只是个短账本，别无限涨
            raise HTTPException(400, msg)
        # 先落缓存（含负账销号）再唤醒等待者：他们醒来第一件事就是查缓存，
        # 反过来的话会看到"没有结果也没有失败"又各自重算一遍
        _fig_cache[key] = out
        _fig_fail.pop(key, None)                # 这份路径+mtime 又能扫了：负账销掉
        fsutil.cap_oldest(_fig_cache, 8)
        return out
    finally:
        mine_ev.set()
        _fig_inflight.pop(key, None)

@app.get("/api/papers/{pid}/figures")
def figures(pid: str):
    return {"figures": _figures_for(pid, _paper_or_404(pid))}

def _mostly_cjk(t: str) -> bool:
    """图注本身已是中文（中文文献）就别再"翻译"一遍。"""
    letters = [c for c in t if c.isalpha()]
    return not letters or sum(1 for c in letters if "\u4e00" <= c <= "\u9fff") > len(letters) * 0.3

@app.get("/api/papers/{pid}/fig_caption")
def fig_caption(pid: str, idx: int = 0):
    """灯箱图注的中文版：懒翻译一次落库（db.fig_caps），之后进页面读缓存不花钱。
    英文界面不打这层（原文就是用户要的语言）；英文界面没有模型时回原文。

    同一张图快速点两次会发两次模型调用：per-key 锁内重读缓存（与 _gen_card 同款），
    后到的那次命中刚落库的译文直接返回。"""
    p = _paper_or_404(pid)
    figs = _figures_for(pid, p)
    if idx < 0 or idx >= len(figs):
        raise HTTPException(404, "没有这张图")
    cap = (figs[idx].get("caption") or "").strip()
    if (not cap or _mostly_cjk(cap) or _demo_mode()
            or (config.load().get("ui_lang") or "zh") != "zh"):
        return {"zh": cap, "original": cap}
    key = f'{figs[idx]["page"]}:{figs[idx]["x0"]}:{figs[idx]["y0"]}'
    with _key_lock("figcap:" + pid + ":" + key):
        caps = db.fig_caps(pid)
        hit = caps.get(str(idx)) or caps.get(key)   # 析读时按编号存；旧版懒翻译按坐标存——两把钥匙都认
        if hit:
            return {"zh": hit, "original": cap}
        hits = _hits_merged(db.glossary_hit(pid, cap), cap)
        msgs = llm.translate_caption_messages(cap, hits)
        # no_think 与 translate_captions（析读批译）同口径：同一任务两条路，思考开着
        # 会对同一条图注多花一个数量级的钱
        zh = llm.chat(msgs, max_tokens=6000, temperature=0.2, no_think=True, scene="图注").strip()
        if zh:
            # 锁内读改写：灯箱两张图并发懒翻译时，后写覆盖前写会丢掉一条译文缓存
            db.fig_caps_add(pid, {key: zh, str(idx): zh})
        return {"zh": zh or cap, "original": cap}


@app.get("/api/papers/{pid}/toc")
def paper_toc(pid: str):
    """PDF 自带的书签目录（get_toc：[层级, 标题, 页码]，页码 1 起）。
    读取本身很便宜，不值得缓存；没有书签就返回空表，前端给一句空态。"""
    p = _paper_or_404(pid)
    src = _paper_src_or_404(pid, p)
    doc = _open_pdf(src)
    try:
        toc = [{"level": lv, "title": title.strip(), "page": page - 1}
               for lv, title, page in doc.get_toc() if page and 1 <= page <= len(doc)]
    finally:
        doc.close()
    return {"toc": toc}

@app.get("/api/papers/{pid}/figure.png")
def figure_png(pid: str, page: int, x0: float, y0: float, x1: float, y1: float, dpi: int = 130):
    import pymupdf
    p = _paper_or_404(pid)
    src = _paper_src_or_404(pid, p)
    doc = _open_pdf(src)
    try:
        if page < 0 or page >= len(doc):
            raise HTTPException(404, f"页码越界：这篇只有 {len(doc)} 页")
        if not (36 <= dpi <= 400):
            raise HTTPException(400, "dpi 只支持 36–400")
        # 坐标必须是有限数：Pydantic 放行 nan/inf，而 Rect 不校验、
        # clip.is_empty（x0>=x1）与 clip.width<4 对 nan 恒为 False——nan 会一路进
        # get_pixmap，喂给底层的 fz_round_rect 是未定义行为（原生巨幅 bbox / 500）
        coords = (x0, y0, x1, y1)
        if not all(math.isfinite(c) for c in coords):
            raise HTTPException(400, "截图坐标不合法")
        clip = pymupdf.Rect(x0, y0, x1, y1) & doc[page].rect
        # 先与页面求交：不与页面相交的超大 clip 会按 dpi 放大成巨幅像素请求，直接打爆内存
        if clip.is_empty or clip.width < 4 or clip.height < 4:
            raise HTTPException(400, "截图范围太小")
        # 同一张图的请求会重复（概览卡出图、灯箱放大、面板重排后又取一遍）：整页 get_pixmap
        # 是几十~上百毫秒的活，按 文件+mtime+页+裁剪框+dpi 缓存住，重复请求直接回字节。
        # mtime 进键：重新导入/重译换了盘上文件就自动是新图，不会命中旧渲染。
        try:
            mtime = os.path.getmtime(src)
        except OSError:
            mtime = 0
        ck = (src, mtime, page, round(clip.x0, 1), round(clip.y0, 1),
              round(clip.x1, 1), round(clip.y1, 1), dpi)
        hit = _fig_png_cache.get(ck)
        if hit is not None:
            return Response(content=hit, media_type="image/png")
        pix = doc[page].get_pixmap(clip=clip, dpi=dpi)
        png = pix.tobytes("png")
    finally:
        doc.close()
    _fig_png_cache[ck] = png
    fsutil.cap_oldest(_fig_png_cache, 32)   # 每张几十~数百 KB，封顶 32 张（够一屏概览 + 灯箱往复）
    return Response(content=png, media_type="image/png")

# ---------------- 问答（流式 + 多会话） ----------------

def _valid_conv(pid: str, raw):
    """body 里的 conv_id：给了就校验存在且属于这篇（否则 404），返回 int；没给返回 None。"""
    if not raw:
        return None
    cid = _int_arg(raw, 404, "会话不存在")
    c = db.conv_get(cid)
    if not c or c["paper_id"] != pid:
        raise HTTPException(404, "会话不存在")
    return cid

def _sse(obj: dict) -> str:
    return "data: " + db.dumps_cjk(obj) + "\n\n"

def _sse_response(gen, keep_alive: bool = False):
    """三条 SSE 端点共用的响应壳。"""
    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    if keep_alive:
        headers["Connection"] = "keep-alive"
    return StreamingResponse(gen, media_type="text/event-stream", headers=headers)

def _open_pdf(src: str):
    import pymupdf
    try:
        return pymupdf.open(src)
    except Exception as e:
        raise HTTPException(400, f"PDF 打不开：{str(e)[:120]}")

def _type_out(text: str, step: int = 3, delay: float = 0.02):
    """演示流的假打字：每次吐 step 个字符，拖出真流式的手感。"""
    for i in range(0, len(text), step):
        yield text[i:i + step]
        time.sleep(delay)

def _mock_stream(question: str):
    """演示模式也走流式：同一条前端代码路径，接上真 key 不用改任何东西。"""
    if (config.load().get("ui_lang") or "zh") == "en":
        text = ("[Demo mode] This is a canned answer. [para 1] With an API key "
                "configured, real answers appear here.\n\n"
                "· You asked: " + question[:60] + "\n"
                "· Answers stream in; stop anytime.")
    else:
        text = ("〔演示模式〕这是模拟回答，配好 API key 后这里会是真答案。\n\n"
                "· 你问的是：" + question[:60] + "\n"
                "· 回答逐字出现，可中途停下。")
    yield from _type_out(text)

def _autotitle(pid: str, conv_id: int, question: str, is_first: bool):
    """第一个问题就是这摊对话的标题——和豆包/DeepSeek 一样，省得用户自己起名。"""
    if not is_first:
        return
    c = db.conv_get(conv_id)
    if c and c["title"] in ("", db.NEW_CONV):
        t = question.strip().replace("\n", " ")[:18]
        db.conv_rename(conv_id, t + ("…" if len(question.strip()) > 18 else ""))

KEEP_MSGS, FOLD_AT, FOLD_CHARS = 8, 12, 6000

def _context(pid: str, conv_id: int, history: list):
    """返回 (摘要, 原样带上的历史)。超预算就把较早的几条压成摘要存回会话。

    压缩在提问之前同步做完，代价是每折一次多一次模型调用；但这是"宁慢不丢"的一步：
    直接把老消息截断，用户前面确认过的结论和术语就会凭空消失，模型随即开始自相矛盾。
    **压缩失败（限流、超时）时不许静默丢**：把老消息截短了照样带上，粗糙好过失忆。
    """
    c = db.conv_get(conv_id) or {}
    upto = c.get("summary_upto") or 0
    tail = [m for m in history if (m.get("id") or 0) > upto]
    chars = sum(len(m.get("content") or "") for m in tail)
    if len(tail) <= FOLD_AT and chars <= FOLD_CHARS:
        return c.get("summary") or "", tail
    head, rest = tail[:-KEEP_MSGS], tail[-KEEP_MSGS:]
    if not head:
        return c.get("summary") or "", tail
    prev = c.get("summary") or ""
    new_sum = ""
    if not _demo_mode():
        new_sum = llm.summarize_dialog(prev, head)
    if new_sum:
        db.conv_set_summary(conv_id, new_sum, head[-1].get("id") or 0)
        return new_sum, rest
    short = [dict(m, content=(m.get("content") or "")[:240] + "…") for m in head[-40:]]
    return prev, short + rest

RECON_SYSTEM = """你在为一次跨论文的提问挑选相关文献。给你一份编号清单（标题、有没有析读摘要）。
挑出与问题最相关的篇目（最多 8 篇，按相关度从高到低）。只输出一个 JSON 数组（元素是编号整数），
不要输出任何别的文字；一篇都不相关就输出 []。"""

def _title_index():
    """一次拿全库的（行, 规范化标题）：refs 里每一条都调 _paper_by_title 的话，
    每条都把整个库列一遍（N+1 的全表捞），refs 长度还没上限。"""
    return [(r, (r["title"] or "").strip().lower()) for r in db.list_papers()]

def _paper_by_title(title: str, index=None):
    """按标题找论文（大小写不敏感）。全等优先；其次唯一包含；都不中返回 None。
    跨文献提问的《标题》引用是模型/用户手打的，容得起一点点不齐，但不该撞错篇。"""
    t = (title or "").strip().lower()
    if not t:
        return None
    norm = _title_index() if index is None else index
    for r, rt in norm:
        if rt == t:
            return r["id"]
    contains = [r for r, rt in norm if (t in rt or rt in t) and rt]
    return contains[0]["id"] if contains else None

def _recon_pick(question: str, papers: list, cap: int = 8):
    """范围是全库/分类时的第一拍"侦察"：只喂标题清单（几十篇也才几千字），
    让模型挑出最相关的篇目（cap 上限），第二拍再取材或按摘要级上下文作答——
    库再大也不会把全文塞爆。失败/演示模式返回演示性挑选，绝不抛错：侦察失败
    顶多选得不准，不该把提问打断。
    「整个文库」勾选会把几百篇全列进来：清单封顶 120 篇（按导入时间最新优先，
    侦察选的是"最相关"，新库大概率比三年前的存档相关），超出的不进清单。"""
    if not papers:
        return []
    if _demo_mode():
        return [p["id"] for p in papers[:3]]
    lines = "\n".join(
        f"{i}. {(p['title'] or p['filename'] or '').strip()}"
        f"（{'已析读' if p['analysis_status'] == 'done' else '未析读'}）"
        for i, p in enumerate(papers[:120]))
    try:
        out = llm.chat([{"role": "system", "content": RECON_SYSTEM},
                        {"role": "user", "content": f"文献清单：\n{lines}\n\n问题：{question}"}],
                       max_tokens=150, temperature=0, no_think=True, scene="跨文献侦察")
        nums = [int(n) for n in re.findall(r"\d+", out)]
        picked = []
        for n in nums:
            if 0 <= n < len(papers) and papers[n]["id"] not in picked:
                picked.append(papers[n]["id"])
        if not picked:
            _applog(f"跨文献侦察没挑出篇目（输出：{out[:60]!r}），退回前 3 篇")
        return picked[:cap]
    except Exception as e:
        _applog(f"跨文献侦察失败，退回前 3 篇: {_human_msg(e)}")
        return [p["id"] for p in papers[:3]]

def _gather_others(question: str, cand: list) -> list:
    """全库分层问答的取材拍（PaperQA2 方向）：候选多于 4 篇时不搬全文（上下文装不下），
    逐篇调一次取材（共享前缀吃服务端缓存，输入按约 1/50 计费），并行 3 路。
    取不到材料的篇就缺席——单篇失败不拖垮整场回答。"""
    papers = [p for p in (db.get_paper(x) for x in cand[:8]) if p]
    def one(o):
        try:
            paras = db.get_paragraphs(o["id"], with_lines=False)
            if not paras:
                return None
            picks = llm.gather(o.get("title") or "", paras, question)
            if not picks:
                return None
            return {"title": o.get("title") or o.get("filename") or "未命名", "paras": picks}
        except Exception as e:
            _applog(f"取材 {o['id']} 失败（这篇缺席）：{_human_msg(e)}")
            return None
    out = []
    with ThreadPoolExecutor(max_workers=min(3, max(1, len(papers)))) as ex:
        for r in ex.map(one, papers):
            if r:
                out.append(r)
    return out

def _qa_add_revive(pid: str, role: str, content: str, conv_id: int, question: str) -> tuple:
    """会话在流式中途被删时的落库兜底：qa_add 返回 0（会话没了）就重建一摊会话再写一次。

    答案不能因为用户删了个会话就静默蒸发——SSE 里只回 assistant_id: null 的话，
    用户看着满屏刚生成的字，历史里却一个字都没有。重建的会话用提问前 30 字当标题
    （与 _autotitle 同一口味）。论文自己已被删（pid 进了 _deleted_pids）时不重建：
    那只会插出一条谁也看不到的孤儿会话。仍写不进去返回 (0, 原会话号)。
    返回 (消息 id, 实际落进的会话号)：调用方要拿会话号告诉前端新家在哪。
    """
    aid = db.qa_add(pid, role, content, conv_id=conv_id)
    if aid:
        return aid, conv_id
    if _pid_gone(pid):
        return 0, conv_id
    try:
        cid = db.conv_create(pid, (question or "").strip().replace("\n", " ")[:30] or db.NEW_CONV)
        aid = db.qa_add(pid, role, content, conv_id=cid)
        if aid:
            _applog(f"问答 {pid}: 会话 {conv_id} 已被删，{role} 消息改存到新会话 {cid}")
            return aid, cid
        _applog(f"问答 {pid}: 会话 {conv_id} 已被删、新会话 {cid} 也没写进 {role} 消息")
    except Exception as e:
        _applog(f"问答 {pid}: 会话 {conv_id} 已被删且重建失败（{_human_msg(e)}），{role} 消息没能落库")
    return 0, conv_id

def _ref_pools(p: dict, valid: set, others: list) -> dict:
    """问答引用校验的"哪一篇"池子：{标题: 段落号集}——本篇 + 本次一起问的别篇。
    标题认不出的引用服务端无从判对错（见 llm.filter_answer_refs），留着原样给前端。"""
    pools = {}
    t = (p.get("title") or "").strip()
    if t:
        pools[t] = valid
    for o in others or []:
        ot = (o.get("title") or "").strip()
        if ot:
            pools[ot] = {x.get("idx") for x in (o.get("paras") or []) if isinstance(x, dict)}
    return pools

def _stream_answer(p: dict, conv_id: int, question: str, ref_pids=None, cont: dict = None):
    """流式回答。事件四种：delta（增量文字）/ done（依据段号 + 落库 id + 有没有被截断）
    / status（跨篇取材在等）/ error。

    落库的时机有两处：正常结束在这里写；用户中途点"停止生成"由前端调 qa-save 写，
    因为客户端断开时服务端不保证还能把生成器走完——让能拿到半截答案的那一端负责存。

    cont = {"mid": 那条被截断的回答, "partial": 它已经写出的内容}：续写模式。
    历史取到那条问题之前、问题原样再传一遍，消息列表与上一次调用逐字节同构——
    服务端缓存照旧命中；写回的是同一条消息（不新增问答对）。
    """
    pid = p["id"]
    uid = None
    if cont is None:
        uid = db.qa_add(pid, "user", question, conv_id=conv_id)
        hist = db.qa_history(pid, conv_id)[:-1]        # 去掉刚落的那条用户消息
    else:
        hist = db.qa_history(pid, conv_id)
        keep = []
        for m in hist:
            if m["id"] == cont["mid"]:
                break
            keep.append(m)
        hist = keep[:-1] if keep and keep[-1]["role"] == "user" else keep
    buf = []
    valid, pools = set(), {}
    cut = [False]                                      # 被输出上限截断（finish_reason=length）
    try:
        if _demo_mode():
            gen = _mock_stream(question)
        else:
            summary, ctx = _context(pid, conv_id, hist)
            pids = [pid] + [x for x in (ref_pids or []) if x != pid]
            paras = db.get_paragraphs(pid, with_lines=False)   # 一问只取一遍：下面三处全用它
            corpus = " ".join(pp["text"] for pp in paras)[:60000]
            hits = _hits_merged(db.glossary_hits_all(pids, corpus), corpus)
            others = []
            cand = [x for x in (ref_pids or []) if x != pid]
            if len(cand) > 3:
                # 引用可能带着已删除的 pid：get_paper 回 None，先滤掉再侦察
                cand = _recon_pick(question, [p for p in map(db.get_paper, cand) if p]) or cand[:4]
            if len(cand) > 4:
                # 分层取材：候选多于 4 篇，全文装不下——先逐篇抽相关段落，作答只见摘录。
                # 取材是分钟内但可感的等待，先发一条状态让"在等"看得见
                yield _sse({"type": "status", "mode": "gather", "n": min(len(cand), 8)})
                others = _gather_others(question, cand)
            else:
                # 恰好 4 篇原来会**静默**丢掉第 4 篇（这里切的是 [:3]，而上面走的是"多于 4
                # 才取材"）——读数与 README 的口径都是"超过 4 篇才转为逐篇取材"。
                # 装的预算是按篇数均分的（llm.ask_messages：每篇 60000/篇数），多带一篇
                # 只是每篇少给一点，不会撑爆上下文。
                for x in cand[:4]:
                    o = db.get_paper(x)
                    if o:
                        others.append({"title": o.get("title") or o.get("filename") or "未命名",
                                       "paras": db.get_paragraphs(x, with_lines=False)})
            msgs = (llm.continue_messages(p["title"], paras, ctx, question, cont["partial"],
                                          hits, summary, others)
                    if cont is not None else
                    llm.ask_messages(p["title"], paras, ctx, question, hits, summary, others))
            gen = llm.chat_stream(msgs, scene="问答",
                                  on_finish=lambda f: cut.__setitem__(0, f == "length"))
            valid = {pp["idx"] for pp in paras}
            pools = _ref_pools(p, valid, others)
        # 每片都过引用校验（带标题的按那篇、裸号按本篇）：越界的 ¶n 不入手，用户就不会
        # 看见一个点不动的死引用（七问/方法卡早这么做，问答因为走流式一直没补）
        for piece in llm.filter_answer_stream(gen, valid, pools):
            buf.append(piece)
            yield _sse({"type": "delta", "text": piece})
        ans = llm.filter_answer_refs("".join(buf), valid, pools)
        if not ans.strip():
            raise RuntimeError("模型这次没返回内容")
        if cont is not None:
            # 续写：接在原来那条后面。消息被删了（用户在续写途中删掉）或已被别处
            # 续写过（乐观锁对不上）就不写，如实说一声
            if not db.qa_append_assistant(cont["mid"], ans, expect=len(cont["partial"])):
                yield _sse({"type": "error",
                            "message": "这条回答已经变过了（别处删了或续写过），刷新看看",
                            "assistant_id": None})
                return
            yield _sse({"type": "done", "user_id": None, "assistant_id": cont["mid"],
                        "appended": True, "truncated": cut[0]})
            return
        aid, conv_used = _qa_add_revive(pid, "assistant", ans, conv_id, question)
        _autotitle(pid, conv_id, question, not any(h["role"] == "user" for h in hist))
        if not aid:
            # 会话被删、重建也没写成：答案真没落库，如实报错，别拿 assistant_id: null 冒充"存好了"
            yield _sse({"type": "error", "message": "答案没能存进对话，请在新会话里重问一次",
                        "user_id": uid or None, "assistant_id": None})
            return
        yield _sse({"type": "done", "user_id": uid or None, "assistant_id": aid,
                    "truncated": cut[0],
                    "conv_id": conv_used if conv_used != conv_id else None})
    except GeneratorExit:
        # 客户端断开（点了「停止生成」/关了窗口）：把已经吐出去的那半截接回原消息。
        # 续写没有 qa-save 这条退路（那会新增一条消息），不接就是"屏幕上有、库里没有"，
        # 重载即消失。GeneratorExit 是 BaseException，接住后**不许再 yield**，写完就抛。
        if cont is not None:
            got = "".join(buf)
            if got.strip():
                db.qa_append_assistant(cont["mid"], got, expect=len(cont["partial"]))
        raise
    except Exception as e:
        hint = _human_msg(e)
        ans = "".join(buf)
        if cont is not None:
            # 续写失败：已经吐出来的那半截接回原消息（能存多少是多少），别整段丢掉
            if ans.strip():
                db.qa_append_assistant(cont["mid"], ans, expect=len(cont["partial"]))
            yield _sse({"type": "error", "message": hint, "assistant_id": cont["mid"],
                        "appended": bool(ans.strip())})
            return
        aid, conv_used = _qa_add_revive(pid, "assistant",
                                        (ans + "\n\n⚠ " + hint) if ans.strip() else "⚠ " + hint,
                                        conv_id, question)
        yield _sse({"type": "error", "message": hint, "user_id": uid or None, "assistant_id": aid or None,
                    "conv_id": conv_used if aid and conv_used != conv_id else None})

@app.get("/api/library/overview")
def library_overview():
    """跨文献引用选择器的数据源：全部论文 + 分类映射。
    一次请求带全（库是本地的，几百篇也只是几十 KB），前端按分类分组勾选。"""
    return {"papers": db.list_papers(), "colls": db.collections_list(), "map": db.collection_map()}

@app.post("/api/papers/{pid}/ask")
def ask(pid: str, body: dict):
    p = _paper_or_404(pid)
    question = _s_arg(body.get("question"))
    if not question:
        raise HTTPException(400, "问题不能为空")
    question = question[:20000]      # 天文数字的粘贴别原样进库进模型
    _require_paras(pid)
    conv_id = _valid_conv(pid, body.get("conv_id")) or db.conv_list(pid)[0]["id"]
    ref_pids = []
    titles = [t for t in (body.get("refs") if isinstance(body.get("refs"), list) else []) if isinstance(t, str)]
    index = _title_index() if titles else None      # 建一次表，整批 refs 共用
    for t in titles:
        pid2 = _paper_by_title(t, index)
        if pid2 and pid2 != pid and pid2 not in ref_pids:
            ref_pids.append(pid2)
    return _sse_response(_stream_answer(p, conv_id, question, ref_pids), keep_alive=True)

_cont_lock = threading.Lock()
_cont_inflight: set = set()      # 正在续写的消息号：同一段同时只许一发（见 ask_continue）

def _cont_release_wrap(gen, mid: int):
    """包一层：流怎么结束（正常/出错/客户端断开）都要把"续写中"的登记摘掉。"""
    try:
        for x in gen:
            yield x
    finally:
        with _cont_lock:
            _cont_inflight.discard(mid)

@app.post("/api/papers/{pid}/ask-continue")
def ask_continue(pid: str, body: dict):
    """接着写：上一条回答被输出上限截断了，从断点续写（写回同一条消息，不新增问答对）。

    前缀与上一次调用逐字节相同（历史取到那条问题之前、问题原样再传），续写这拍
    只有尾部那点新内容按全价算，其余全走服务端缓存。

    同一段**同时只许一发**：两个窗口/双击连发时，第二发若也跑完流程就会把同一段
    续写接两遍（正文里出现两份）。这里用一本"续写中"的簿子当场挡住（409），
    写库那一步另有一道乐观锁兜底（`qa_append_assistant(expect=…)`）。"""
    p = _paper_or_404(pid)
    _require_paras(pid)
    try:
        mid = int(body.get("mid") or 0)
    except (TypeError, ValueError):
        mid = 0
    m = db.qa_msg_get(mid)
    if not m or m["paper_id"] != pid or m["role"] != "assistant" or not m.get("conv_id"):
        raise HTTPException(404, "这条回答不在了")
    hist = db.qa_history(pid, m["conv_id"])
    if not hist or hist[-1]["id"] != mid:
        raise HTTPException(400, "只能续写最后一条回答")
    question = next((x["content"] for x in reversed(hist[:-1]) if x["role"] == "user"), "")
    if not question:
        raise HTTPException(400, "找不到这条回答对应的问题")
    if not (m.get("content") or "").strip():
        raise HTTPException(400, "这条回答是空的，重新生成一次")
    with _cont_lock:
        if mid in _cont_inflight:
            raise HTTPException(409, "这条回答正在续写中，等它写完再点")
        _cont_inflight.add(mid)
    try:
        gen = _stream_answer(p, m["conv_id"], question, None,
                             cont={"mid": mid, "partial": m["content"]})
        return _sse_response(_cont_release_wrap(gen, mid), keep_alive=True)
    except Exception:
        with _cont_lock:
            _cont_inflight.discard(mid)
        raise

@app.get("/api/papers/{pid}/conversations")
def conversations(pid: str):
    _paper_or_404(pid)
    return db.conv_list(pid)

@app.post("/api/papers/{pid}/conversations")
def conversation_new(pid: str, body: dict = None):
    _paper_or_404(pid)
    return {"id": db.conv_create(pid, (_s_arg((body or {}).get("title")) or db.NEW_CONV)[:60])}

@app.patch("/api/conversations/{cid}")
def conversation_patch(cid: int, body: dict):
    if not db.conv_get(cid):
        raise HTTPException(404, "会话不存在")
    db.conv_rename(cid, (_s_arg(body.get("title")) or db.NEW_CONV)[:200])
    return {"ok": True}

@app.delete("/api/conversations/{cid}")
def conversation_delete(cid: int):
    if not db.conv_get(cid):
        raise HTTPException(404, "会话不存在")
    db.conv_delete(cid)
    return {"ok": True}

@app.get("/api/papers/{pid}/qa-history")
def qa_history(pid: str, conv_id: int = None):
    _paper_or_404(pid)
    if conv_id is None:
        return {"messages": db.qa_history(pid), "conv_id": None}
    return {"messages": db.qa_history(pid, conv_id), "conv_id": conv_id}

@app.post("/api/papers/{pid}/qa-save")
def qa_save(pid: str, body: dict):
    """用户中途"停止生成"：把已经吐出来的半截答案存下来。
    返回两端 id，前端据此把"删除/重新生成"接回真实的行上。

    会话在流式中途被删：既不写进死会话（会留孤儿），也不把半截答案丢掉——
    另起一摊会话存下，新会话号回给前端（它得跟着搬，删除/重生成才对得上号）。
    属于别的论文的会话一律 404（与 /ask 同口径，跨篇写入不放行）。"""
    _paper_or_404(pid)
    content = _s_arg(body.get("content"))
    if not content:
        return {"ok": False}
    raw = body.get("conv_id")
    conv_id = None
    if raw:
        cid = _int_arg(raw, 404, "会话不存在")
        c = db.conv_get(cid)
        if c and c["paper_id"] != pid:
            raise HTTPException(404, "会话不存在")
        conv_id = cid if c else None            # 会话已删：不写死会话，下面另起一摊
    created = False
    if not conv_id:
        if _pid_gone(pid):
            raise HTTPException(404, "会话不存在")
        conv_id = db.conv_create(pid, content.replace("\n", " ")[:30] or db.NEW_CONV)
        created = True
        _applog(f"问答 {pid}: 原会话已删，半截答案改存到新会话 {conv_id}")
    aid = db.qa_add(pid, "assistant", content, conv_id=conv_id)
    return {"ok": True,
            "assistant_id": aid or None,
            "conv_id": conv_id if (aid and created) else None,
            "user_id": db.qa_last_user_id(pid, conv_id) if (aid and conv_id) else None}

@app.post("/api/papers/{pid}/regenerate")
def qa_regenerate(pid: str, body: dict):
    """重新生成：把这一问一答都撤掉，返回原问题，由前端重新发问。"""
    _paper_or_404(pid)
    conv_id = body.get("conv_id")
    if not conv_id:
        raise HTTPException(400, "缺少会话")
    conv_id = _int_arg(conv_id, 400, "缺少会话")
    q = db.qa_drop_last_assistant(pid, conv_id)
    if not q:
        raise HTTPException(400, "没有可重新生成的问题")
    return {"question": q}

@app.delete("/api/conversations/{cid}/messages/{mid}")
def qa_delete_one(cid: int, mid: int):
    row = db.q("SELECT conv_id FROM qa_messages WHERE id=?", (mid,))
    if not row:
        raise HTTPException(404, "这条消息不存在")
    if row[0]["conv_id"] != cid:
        raise HTTPException(404, "这条消息不属于这个会话")
    db.qa_delete(mid)
    return {"ok": True}

@app.delete("/api/papers/{pid}/qa-history")
def qa_clear(pid: str):
    _paper_or_404(pid)
    db.qa_clear(pid)
    return {"ok": True}

# ---------------- 文库分类 ----------------

@app.get("/api/collections")
def collections():
    return {"collections": db.collections_list(), "map": db.collection_map()}

@app.post("/api/collections")
def collection_new(body: dict):
    name = _utf8_safe(str(body.get("name") or "")).strip()[:60]
    if not name:
        raise HTTPException(400, "分类要有名字")
    return {"id": db.collection_add(name)}

@app.patch("/api/collections/{cid}")
def collection_patch(cid: int, body: dict):
    name = _utf8_safe(str(body.get("name") or "")).strip()[:60]
    if not name:
        raise HTTPException(400, "分类要有名字")
    db.collection_rename(cid, name)
    return {"ok": True}

@app.delete("/api/collections/{cid}")
def collection_delete(cid: int):
    db.collection_delete(cid)
    return {"ok": True}

@app.put("/api/papers/{pid}/collections")
def paper_collections_set(pid: str, body: dict):
    _paper_or_404(pid)
    ids = body.get("ids") or []
    if not isinstance(ids, list):
        raise HTTPException(400, "ids 必须是数组")
    try:
        ids = [int(x) for x in ids]
    except (TypeError, ValueError):
        raise HTTPException(400, "ids 必须是整数数组")
    db.set_paper_collections(pid, ids)
    return {"ok": True, "ids": ids}

# ---------------- 翻译 ----------------

def _mock_translate(text: str):
    """演示模式的假译文也假装在打字：同一条前端代码路径。前缀随界面语言。"""
    yield from _type_out(_demo_txt("〔演示译文〕", "[demo translation] ") + text[:120])

def _translate_sse(text: str, context: str, hits: list):
    """流式翻译。事件：delta（增量）/ done（术语命中）/ error（人话）。

    术语命中随 done 一起回——它在翻译开始前就查好了，不必等译文走完。
    """
    buf = []
    try:
        if _demo_mode():
            gen = _mock_translate(text)
        else:
            gen = llm.translate_stream(text, context, hits)
        for piece in gen:
            buf.append(piece)
            yield _sse({"type": "delta", "text": piece})
        zh = "".join(buf)
        if not zh.strip():
            raise RuntimeError("模型这次没返回内容")
        yield _sse({"type": "done", "hits": hits})
    except Exception as e:
        yield _sse({"type": "error", "message": _human_msg(e)})

@app.post("/api/papers/{pid}/translate-selection")
def translate_selection(pid: str, body: dict):
    _paper_or_404(pid)
    text = _s_arg(body.get("text"))
    if not text:
        raise HTTPException(400, "没有选中文本")
    hits = _hits_merged(db.glossary_hit(pid, text), text)
    context = _s_arg(body.get("context"))
    return _sse_response(_translate_sse(text, context, hits))

@app.post("/api/papers/{pid}/translate-para")
def translate_para(pid: str, body: dict):
    _paper_or_404(pid)
    idx = _int_arg(body.get("idx"), 400, "缺 idx（要译哪一段）")
    para = db.get_paragraph(pid, idx)
    if not para:
        raise HTTPException(404, "段落不存在")
    hits = _hits_merged(db.glossary_hit(pid, para["text"]), para["text"])
    ctx_row = db.get_paragraph(pid, idx - 1)
    ctx = (ctx_row or {}).get("text", "")
    return _sse_response(_translate_sse(para["text"], ctx, hits))

_RELAY_TOKEN = secrets.token_urlsafe(16)   # 引擎拿来叩中转的临时令牌，每次启动换
RELAY_PORT = 0        # serve() 落定；不开端口直调 app 的场合（TestClient）保持 0 → 翻译走直连老路
_relay_http = httpx.AsyncClient(timeout=llm.LLM_TIMEOUT)

@app.post("/llm-relay/chat/completions")
async def _llm_relay(req: Request):
    """全文翻译引擎的透明中转：注入 thinking:disabled 再转发，真 key 不出进程。

    引擎的通用 openai 通道没有 thinking 入口，而 deepseek-flash 默认开思考——翻译
    是逐段短改写，思考纯属白烧（输出 token 是输入的 5.6 倍，大头就是它）。引擎的
    base_url 指到本端点，这里补上开关后转发给真端点；顺带把引擎请求的 token 账
    记进 app.log（scene=翻译引擎），翻译烧多少钱第一次看得见。
    429/5xx 原样透传：引擎的 tenacity 退避重试靠状态码工作，吞了它就瞎了。
    """
    if req.headers.get("authorization") != f"Bearer {_RELAY_TOKEN}":
        raise HTTPException(403, "relay token mismatch")
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(400, "bad json")
    if isinstance(body, dict):
        body["thinking"] = {"type": "disabled"}
    prov = config.load().get("provider") or {}
    base = (prov.get("base_url") or "").strip().rstrip("/")
    key = (prov.get("api_key") or "").strip()
    if not base or not key:
        raise HTTPException(502, "provider 未配置")
    try:
        r = await _relay_http.post(f"{base}/chat/completions",
                                   headers={"Authorization": f"Bearer {key}"},
                                   json=body)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"上游 {type(e).__name__}")
    try:
        usage = r.json().get("usage") or {}
    except Exception:
        usage = {}
    if usage:
        llm._log_cache(usage, "翻译引擎")
    return Response(content=r.content, status_code=r.status_code,
                    media_type=r.headers.get("content-type", "application/json"))

def _pdf2zh_env(service: str, cfg: dict):
    """全文翻译要的 key 从哪来：**用用户在「设置」里已经填的那一套**，不让他填第二遍。

    pdf2zh_next（2.x）的配置体系是 pydantic-settings：`--openai-api-key` 对应环境变量
    `PDF2ZH_OPENAI_API_KEY`（`--` 换 `PDF2ZH_`、`-` 换 `_`），所以 key 只经环境变量
    交给子进程，不进命令行。openai 通道再进一层：引擎拿到的不是真 key 而是一次性
    令牌，base_url 指向本进程的 /llm-relay——请求注入 thinking:disabled 后由中转
    持真 key 转发（引擎通道没有 thinking 入口，不中转就只能白烧思考 token）。
    子进程 HOME 指到数据目录 home/（见 translate_full._page_env），引擎若把收到的值
    自动落盘也只落在我们自己文件夹里，任务收尾会扫掉。返回 (环境变量, 端点主机名)。
    """
    prov = cfg.get("provider") or {}
    key = (prov.get("api_key") or "").strip()
    base = (prov.get("base_url") or "").strip()
    model = (prov.get("model") or "").strip()
    if service == "deepl":
        # DeepL 用自己的 key（设置里「DeepL Key」那栏），跟模型 API 的 key 是两回事
        dk = ((cfg.get("pdf2zh") or {}).get("deepl_key") or "").strip()
        if not dk:
            return {}, ""
        return {"PDF2ZH_DEEPL_AUTH_KEY": dk}, ""
    if not key:
        return {}, ""
    if service == "openai":
        if RELAY_PORT:
            envs = {"PDF2ZH_OPENAI_API_KEY": _RELAY_TOKEN,
                    "PDF2ZH_OPENAI_BASE_URL": f"http://127.0.0.1:{RELAY_PORT}/llm-relay"}
        else:
            envs = {"PDF2ZH_OPENAI_API_KEY": key}
            if base:
                envs["PDF2ZH_OPENAI_BASE_URL"] = base
        if model:
            envs["PDF2ZH_OPENAI_MODEL"] = model
        u = urlparse(base)
        host = u.hostname or ""
        if host and u.port:
            host = f"{host}:{u.port}"       # 带端口的端点（本地网关等）预检要探对端口
        return envs, host
    if service == "deepseek":
        envs = {"PDF2ZH_DEEPSEEK_API_KEY": key}
        if model:
            envs["PDF2ZH_DEEPSEEK_MODEL"] = model
        return envs, "api.deepseek.com"
    return {}, ""

def _translate_flavor(service: str, cfg: dict) -> str:
    """页级缓存指纹里"换模型/换端点"那一维。key 一定不能进（每人自己的、还会变），
    relay 的本地 base_url 也不能进（端口每进程都变，进去等于每轮都作废缓存）——
    只取"哪个模型 + 哪个上游端点"，那才是会改变译文风格与术语口径的东西。
    bing/google/deepl 不吃 prompt，没有模型这一维，回空串。"""
    if service not in translate_full.LLM_SERVICES:
        return ""
    prov = cfg.get("provider") or {}
    model = (prov.get("model") or "").strip()
    base = (prov.get("base_url") or "").strip()
    u = urlparse(base)
    host = (u.hostname or "") + (f":{u.port}" if u.port else "")
    return f"{service}|{model}|{host}"

def _claim_existing_translation(pid: str) -> dict:
    """认领已经写完的成品译文：pdf2zh 是独立进程，可能在本进程启动**之后**才把
    成品写完，没人认领界面上就永远停在「全文翻译」。有成品就登记 dual/mono、
    状态打成 done，返回成品 dict；没有返回空。两次 stat + 尾部读，够便宜。
    成品名两代并存：可读名 `<显示名>（译文|双语）.pdf` 与老名 mono/dual.pdf 都认。"""
    base = _display_name(db.get_paper(pid) or {})
    mono_n, dual_n = _variant_names(base)
    got = translate_full.adopt_existing(
        paper_dir(pid),
        mono=[mono_n] if mono_n else [],
        dual=[dual_n] if dual_n else [])
    if got:
        db.update_paper(pid, dual_path=got.get("dual") or "", mono_path=got.get("mono") or "",
                        translate_status="done", translate_error="")
    return got or {}

def _pipe_stale_reason(pid: str, p: dict) -> str:
    """盘上那份成品还算不算"当前口径"的译文：'' = 算（或压根没成品），非空 = 该重译的原因。

    判据在 translate_full.product_pipe_reason（口径版本 + 这篇有没有扫描件页）。这里是
    "把成品名与原件路径凑齐"的那一层——成品名随显示名走，别在别处再拼一遍。
    """
    nb = _display_name(p) or "paper"
    mono_n, dual_n = _variant_names(nb)
    return translate_full.product_pipe_reason(
        paper_dir(pid), _paper_src(pid, p),
        mono=[mono_n] if mono_n else [], dual=[dual_n] if dual_n else [])

@app.post("/api/papers/{pid}/translate-full")
def translate_full_start(pid: str, force: bool = False):
    p = _paper_or_404(pid)
    cfg = config.load()
    if force and translate_full.job(pid)["status"] in ("running", "queued"):
        # 「重新全文翻译」按在在跑的任务上要明说，别静默空转——用户以为重译开始了，
        # 界面却一直停在旧任务上（start() 的在途去重会原样退回旧 job）
        raise HTTPException(400, "正在翻译中，稍后再试")
    svc = (cfg["pdf2zh"].get("service") or "bing").strip()
    envs, host = _pdf2zh_env(svc, cfg)
    stale_why = ""
    if not force:
        # 认领成品之前先问一句"这份还算不算当前口径的译文"：升级后旧口径的成品不能继续
        # 冒充"上次已经译好了"——不然扫描件那篇一直是重影，用户点几次都不动
        # （2026-10-14 用户反馈：0.1.2 修了重影，他升级后再译还是重影）
        stale_why = _pipe_stale_reason(pid, p)
        if not stale_why and _claim_existing_translation(pid):
            _applog(f"全文翻译 {pid}: 发现上次已经译好的成品，直接认领")
            return {"status": "done", "service": "", "note": "上次已经译好了"}
    used, note = translate_full.choose_service(svc, host)
    if used is None:
        raise HTTPException(400, note)
    if stale_why:
        # 旧口径的成品：不认领，按重译走（force 的效果），把原因说给用户听。点一次翻译就
        # 真译一遍——这是点这颗按钮的本意（而不是"什么都不做"）
        _applog(f"全文翻译 {pid}: 成品是旧口径的，不认领——{stale_why}")
        force = True
        note = f"{stale_why}；{note}" if note else stale_why
    engine = (cfg["pdf2zh"].get("path") or "").strip()
    exe = translate_full.engine_path(engine)
    hot = translate_full.probe_cached(exe)
    if hot is None:
        # 冷缓存：一次探测要跑 `--version`，实测 3 秒起——点「全文翻译」不该卡在它
        # 上面。放行 start()（后台线程里有同款把关，探出坏引擎会落成任务错误），
        # 这里只起个后台预热，让下一次点击拿到热缓存。
        threading.Thread(target=translate_full.engine_probe_cached, args=(exe,), daemon=True).start()
    elif not hot[0]:
        ok, why = hot
        # 引擎不在（或还是 1.9 旧版）：后台自动下载安装（多源+校验+续传+预热，
        # 见 engine_install），旧版也会被原地换掉。前端见到"状态变成下载中"就弹
        # 等待卡、装完自动把这次全文翻译续上。
        # 自动安装的判断看**磁盘上装没装**（find_installed），不是 engine_path——
        # E2E 的 EGGPAPER_ENGINE_OFF 只让"寻找"失明，不能让装好的引擎被当成没装，
        # 否则装完又再起一轮下载（E2E 实测撞过）。
        st = engine_install.status()
        exe_found = engine_install.find_installed()
        ver = translate_full.engine_version(exe_found) if exe_found else ()
        stale = bool(ver) and ver < (2,)   # 1.9 在位（旧版安装或自填路径）：也要走重装换掉
        if (not exe_found or stale) and st["state"] not in ("downloading", "unpacking", "warming"):
            engine_install.start()
            raise HTTPException(400, f"{'旧版' if stale else '缺'}全文翻译引擎，"
                                     f"正在后台自动安装，装好会自动继续")
        if st["state"] in ("downloading", "unpacking", "warming"):
            raise HTTPException(400, "翻译引擎正在后台安装…")
        if not exe_found or stale:
            raise HTTPException(400, f"全文翻译引擎{'升级' if stale else '下载'}没成功"
                                     f"（{st.get('error') or '原因未知'}）")
        raise HTTPException(400, f"全文翻译引擎起不来（{why}）")
    src = _paper_src_or_404(pid, p)
    with _key_lock("translate:" + pid):
        # 删除/替换版本与这里抢同一把锁：要么任务先起（删除侧的 cancel 会掐掉它），
        # 要么删除先走完（下面的复查看到论文已注销，404 收场）。否则服务预检的秒级
        # 窗口里论文被删，run() 会把已删目录当"幽灵翻译"原样重建出来
        if _pid_gone(pid):
            raise HTTPException(404, "论文不存在")
        # 全文链的术语注入同样合并学科库（本篇优先、学科库补缺、封顶 200 条）：
        # 全文翻译是术语一致性最贵的一条链，划词/段落/问答都合并了，唯独这里漏掉的话，
        # 学科库在最该生效的地方失效。词表是手工策展的，并集不会爆炸，封顶只是护栏
        gseen = set()
        glossary_rows = []

        def add_terms(rows, cap=None):
            """把一份词表并进来：按 term_en 去重、空项跳过；cap 只对后面的学科库生效。"""
            for r in rows:
                en = (r.get("term_en") or "").strip()
                if not en or en.lower() in gseen:
                    continue
                if cap is not None and len(glossary_rows) >= cap:
                    return
                gseen.add(en.lower())
                glossary_rows.append({"term_en": en, "term_zh": (r.get("term_zh") or "").strip()})

        add_terms(db.glossary_list(pid))
        add_terms(db.glossary_global_list(), 200)
        nb = _display_name(db.get_paper(pid) or {}) or "paper"
        mono_n, dual_n = _variant_names(nb)
        translate_full.start(pid, src, paper_dir(pid), used,
                             cfg["pdf2zh"].get("options", ""), envs=envs, log=_applog,
                             note=note, engine=engine,
                             flavor=_translate_flavor(used, cfg),
                             glossary_rows=glossary_rows, fresh=force,
                             mono_name=mono_n, dual_name=dual_n)
        db.update_paper(pid, translate_status="running", translate_error="")
    return {"status": "running", "service": used, "note": note}

@app.get("/api/papers/{pid}/translate-status")
def translate_full_status(pid: str):
    p = _paper_or_404(pid)
    j = translate_full.job(pid)
    if j["status"] == "none" and p["translate_status"] not in ("running", "done", "error"):
        # 这里顺手认领一次：没人认领界面上就永远显示「全文翻译」
        if _claim_existing_translation(pid):
            _applog(f"全文翻译 {pid}: 运行中发现已写完的成品，直接认领")
            p = _paper_or_404(pid)
    if j["status"] == "done":
        # mono 路径变了（重译改名/成品换名），或 db 还停在 running（重译同路径成品时
        # 路径不变）都要写；残件清理与取件端点的补同步共用同一段（_sync_translate_from_job）
        p = _sync_translate_from_job(pid, p, j)
    elif j["status"] == "error" and p["translate_status"] != "error":
        db.update_paper(pid, translate_status="error", translate_error=j["error"])
    return j

# ---------------- 术语表 ----------------

@app.get("/api/papers/{pid}/glossary")
def glossary_list(pid: str):
    _paper_or_404(pid)
    return db.glossary_list(pid)

def _term_clean(s):
    """术语入表的统一清洗（单篇/学科库两个端点共用）：控制字符剥掉（复制粘贴会带
    \x00/\x1f，SQLite 存得下、界面显示不出来）、代理字符剥掉（\ud800 这类存库
    当场 UnicodeEncodeError），非字符串收编成文本；钳长在调用处做
    ——这是词表不是文本库。"""
    s = s if isinstance(s, str) else ("" if s is None else str(s))
    return _utf8_safe("".join(ch for ch in s if ord(ch) >= 32)).strip()

@app.post("/api/papers/{pid}/glossary")
def glossary_add(pid: str, body: dict):
    _paper_or_404(pid)
    en = _term_clean(body.get("term_en"))[:120]
    zh = _term_clean(body.get("term_zh"))[:120]
    if not en or not zh:
        raise HTTPException(400, "中英文都要填")
    gid = db.glossary_add(pid, en, zh, body.get("source", "manual"))
    return {"id": gid}

@app.post("/api/papers/{pid}/glossary/generate")
def glossary_generate(pid: str):
    """按篇发掘术语（+这篇自己的缩写）：这一篇还没有词表时，打开术语页调它一次。

    这里只在**确实为空**时花钱，生成过就纯读库（第二次进来不发请求）——
    为了看一眼术语把整篇重新析读一遍的代价不能有。析读时照样会生成，这条路只是兜漏网。
    """
    _paper_or_404(pid)
    with _key_lock("terms:" + pid):
        rows = db.glossary_list(pid)
        if rows:
            return {"items": rows, "generated": False, "abbrs": _abbrs_of(pid)}
        _require_paras(pid)
        p = db.get_paper(pid)
        got = _demo_terms(p["title"], []) if _demo_mode() else llm.extract_terms(
            p["title"], db.get_paragraphs(pid))
        if not _save_terms(pid, got):
            raise HTTPException(503, "模型没给出术语，重试一次")
        return {"items": db.glossary_list(pid), "generated": True, "abbrs": _abbrs_of(pid)}

def _abbrs_of(pid: str) -> dict:
    """本篇缩写表（缺/坏值都当空表：这是一份纯展示缓存，不值得为它 500）。"""
    row = db.get_paper(pid)
    out = _json_of((row or {}).get("abbrs"))
    return out if isinstance(out, dict) else {}

@app.delete("/api/glossary/{gid}")
def glossary_delete(gid: int, pid: str = ""):
    """删术语。带 pid 就校验归属（批注那边同款越权实锤过）：术语 id 全库自增，
    陈旧页面拿 A 篇的 id 打到 B 篇的行上会删错人；不带 pid 的老客户端照旧放行。"""
    if pid and not db.glossary_delete_owned(gid, pid):
        raise HTTPException(403, "这个术语不属于这篇论文")
    db.glossary_delete(gid)
    return {"ok": True}

# ---------------- 全局学科术语库（跨篇复用的公共层） ----------------

@app.get("/api/glossary-global")
def glossary_global_list():
    return db.glossary_global_list()

@app.post("/api/glossary-global")
def glossary_global_add(body: dict):
    en = _term_clean((body or {}).get("term_en"))[:120]
    zh = _term_clean((body or {}).get("term_zh"))[:120]
    note = _term_clean((body or {}).get("note"))[:200]
    if not en or not zh:
        raise HTTPException(400, "中英文都要填")
    return {"id": db.glossary_global_add(en, zh, note)}

@app.delete("/api/glossary-global/{gid}")
def glossary_global_delete(gid: int):
    db.glossary_global_delete(gid)
    return {"ok": True}

@app.get("/api/glossary-global/export.csv")
def glossary_global_csv():
    """学科库导出：与单篇词表同一套 CSV 口径（同样的公式注入防护）。"""
    rows = [(r["term_en"], r["term_zh"]) for r in db.glossary_global_list()]
    return _csv_response(_csv_buf(["term_en", "term_zh"], rows))

# ---------------- 库内互引（scite 式）：库里谁引了这篇、引的哪句、什么立场 ----------------

_CITE_STANCE_ZH = {"support": "支持", "contrast": "质疑", "mention": "提及"}

def _sentence_around(text: str, at: int, span: int = 220) -> str:
    """命中位置 → 完整句子（前后扩到句界）。找不到句界就按窗口切。"""
    s, e = max(0, at - span), min(len(text), at + span)
    left = max(text.rfind(x, s, at) for x in (". ", "。", "! ", "? ", "；", "; "))
    right = min((i for i in (text.find(x, at, e) for x in (". ", "。", "! ", "? ")) if i >= 0),
                default=e)
    if left >= s:
        s = left + (2 if text[left:left + 2] in (". ", "! ", "? ") else 1)
    return " ".join(text[s:right + 1].split())[:300]

def _citedby_scan(p: dict) -> list:
    """本地免费扫描：库内其他论文的正文里，哪些句子提到了这篇（标题或 DOI）。
    SQL 子串粗筛 + 归一化精筛（连字/换行 hyphen 与前端划线同一套折叠规则），
    一篇最多留 3 句——互引要看的是"谁引了、怎么引"，不是逐句清单。

    已知召回边界：常规引用标记是 (Author, year)，全标题只在**实质讨论**这篇的
    正文句里出现（related work / 引言综述句）——匹配不到数字式顺带引用，但这正是
    "谁认真引了这篇"的高价值子集。参考文献表本来就不入库（pdfparse 丢弃 refs 段，
    存了会动整条解析语义），扫不了。"""
    title = (p.get("title") or "").strip()
    doi = ""
    try:
        doi = (json.loads(p.get("citation") or "{}") or {}).get("doi") or ""
    except Exception:
        pass
    if len(title) < 12 and not doi:
        return []                      # 标题太短匹配必炸误报；什么钥匙都没有就别扫
    tlow = title.lower()
    tnorm = llm.norm_text(title)
    tnorm_cmp = tnorm if len(tnorm) >= 8 else ""      # 判据里反复用，别在循环里重算
    # SQL LIKE 先把候选缩到命中标题片段/DOI 的那几篇（几百篇的库不再全表拉进 Python）；
    # 精筛（大小写、归一化折叠）仍在 Python 侧做。片段取标题前 24 字——跨行连字符
    # 断在这段里的极少数会漏，属于已知召回边界（见 docstring）。
    like_clauses, like_params = [], []
    if len(title) >= 12:
        frag = title[:24].translate(db._LIKE_ESC)
        like_clauses.append("text LIKE ? ESCAPE '\\'")
        like_params.append(f"%{frag}%")
    if doi:
        like_clauses.append("text LIKE ? ESCAPE '\\'")
        like_params.append(f"%{doi.translate(db._LIKE_ESC)}%")
    rows = db.q(f"SELECT paper_id, idx, page, text FROM paragraphs "
                f"WHERE in_refs=0 AND paper_id != ? AND ({' OR '.join(like_clauses)})",
                (p["id"], *like_params))
    # 同一篇的另一版本（导入时选了"两篇都保留"）标题归一化相同，会把对方当引用——排除。
    # 目标标题的归一化只算一遍（与 db.find_same_title 同款）：原来每个候选都拿它重算两遍
    t_norm = db._norm_title(title)
    # 阈值与 db.find_same_title 同款（归一化太短的标题不判同篇）；判据与行无关，提到查询外
    twins = set()
    if len(t_norm) >= 8:
        twins = {r["id"] for r in db.q("SELECT id, title FROM papers")
                 if db._norm_title(r["title"]) == t_norm} - {p["id"]}
    by_paper = {}
    doi_low = doi.lower()
    for r in rows:
        text = r["text"] or ""
        low = text.lower()             # 三个分支共用一份小写：原来一行文字要降一两遍
        at = -1
        if tlow and tlow in low:
            at = low.find(tlow)
        elif tnorm_cmp and tnorm_cmp in llm.norm_text(text):
            at = -2                    # 归一化才对得上（跨行连字符/连字）：句子提不出来，用段落摘录
        elif doi_low and doi_low in low:
            at = low.find(doi_low)
        if at == -1:
            continue
        quote = _sentence_around(text, at) if at >= 0 else " ".join(text.split())[:200]
        by_paper.setdefault(r["paper_id"], []).append(
            {"para": r["idx"], "page": r["page"], "quote": quote})
    # 引用者的元信息一次 IN 查（原来是逐篇 get_paper：引用者一多就是 N+1）。
    # 查不到的（两拍之间被删）跳过——与原 get_paper 回 None 时的行为一致
    wanted = [cid for cid in by_paper if cid not in twins]
    meta = {}
    if wanted:
        marks = ",".join("?" * len(wanted))
        meta = {r["id"]: r for r in db.q(
            f"SELECT id, title, filename, authors, year FROM papers WHERE id IN ({marks})", tuple(wanted))}
    out = []
    for citing_id, hits in by_paper.items():
        if citing_id in twins:
            continue
        o = meta.get(citing_id)
        if not o:
            continue
        out.append({"citing": {"id": citing_id,
                               "title": o["title"] or o["filename"] or "",
                               "authors": o["authors"] or "", "year": o["year"] or ""},
                    "hits": hits[:3]})
    out.sort(key=lambda x: (x["citing"]["title"] or "").lower())
    return out

CITE_SYSTEM = """你在为研究者整理"库内互引"：另一篇论文的正文里出现了当前这篇的标题/DOI，
下面是从 citing 论文正文里抽出的命中句子。逐句判断引用立场：
support = 引用它来支撑自己的论点/方法；contrast = 引它同时指出分歧、局限或反驳；
mention = 中性提及（背景、相关工作罗列）。
只输出 JSON，不要解释：{"stances":[{"i":<序号>,"s":"support|contrast|mention"}]}"""

def _citedby_classify(pid: str, scan: list) -> list:
    """立场判定：只对没缓存过的（被引篇, 引用篇）对花钱，一次调用判完，结果落缓存。
    调用方持 per-key 锁：双击「判定立场」的两个请求串行过锁，后到的看到刚落的
    缓存就不再花钱（与 _six_compute 同款套路）。失败回退 mention——立场缺失
    不该把整个互引列表藏起来。"""
    cached = db.cite_ctx_all(pid)
    todo = [x for x in scan if x["citing"]["id"] not in cached]
    todo_ids = {x["citing"]["id"] for x in todo}
    for x in todo:                    # 默认 mention：判定不出/演示模式都回退到它
        for h in x["hits"]:
            h["stance"] = "mention"
    if todo and not _demo_mode():
        flat = []                       # 序号 → (citing_id, 句子)：模型只回序号，按它对回去
        for x in todo:
            for h in x["hits"]:
                flat.append((x["citing"]["id"], h))
        lines = [f"{i}. 《{x['citing']['title'][:60]}》说：{h['quote'][:200]}"
                 for i, (_cid, h) in enumerate(flat)]
        stances = {}
        try:
            data = llm.chat_json(
                [{"role": "system", "content": CITE_SYSTEM + llm._lang_tail()},
                 {"role": "user", "content": "\n".join(lines)[:12000]}],
                max_tokens=2000, temperature=0, no_think=True, scene="互引立场")
            for it in (data.get("stances") or []):
                if isinstance(it, dict) and str(it.get("s")) in _CITE_STANCE_ZH:
                    try:
                        stances[int(it["i"])] = str(it["s"])
                    except (TypeError, ValueError):
                        pass
        except Exception as e:
            _applog(f"互引立场判定失败（回退 mention）：{_human_msg(e)}")
        for i, (_, h) in enumerate(flat):
            h["stance"] = stances.get(i, "mention")
        for x in todo:
            cid = x["citing"]["id"]
            if not _pid_gone(pid) and not _pid_gone(cid):
                db.cite_ctx_put(pid, cid, x["hits"])
    out = []
    for x in scan:
        cid = x["citing"]["id"]
        hits = x["hits"] if cid in todo_ids else (cached.get(cid) or x["hits"])
        out.append({"citing": x["citing"], "hits": hits[:3]})
    return out

@app.get("/api/papers/{pid}/citedby")
def citedby(pid: str):
    """扫描免费（本地子串 + 归一化），立场只读缓存——花钱的判定走 classify。"""
    p = _paper_or_404(pid)
    scan = _citedby_scan(p)
    cached = db.cite_ctx_all(pid)
    items = [{"citing": x["citing"], "hits": (cached.get(x["citing"]["id"]) or x["hits"])[:3]}
             for x in scan]
    return {"items": items, "n": sum(len(x["hits"]) for x in items)}

@app.post("/api/papers/{pid}/citedby/classify")
def citedby_classify(pid: str):
    p = _paper_or_404(pid)
    scan = _citedby_scan(p)
    if not scan:
        return {"items": [], "n": 0}
    # 判定+落缓存同锁：双击的两个请求在这里串行，后到的命中刚写的缓存不再花一次调用
    with _key_lock("citedby:" + pid):
        items = _citedby_classify(pid, scan)
    return {"items": items, "n": sum(len(x["hits"]) for x in items)}

# ---------------- 前端静态托管（构建后） ----------------

def _static_page(fname: str):
    return FileResponse(os.path.join(os.path.dirname(__file__), fname),
                        media_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "no-cache, must-revalidate"})

@app.get("/guide")
def guide_page():
    """使用指南：一页静态 HTML。设置里可打开；砍界面文案时的安全网。"""
    return _static_page("guide.html")

@app.get("/model")
def model_page():
    """配模型教程：与使用指南分开的一页，设置里模型一节的「教程」打开。"""
    return _static_page("model.html")

DIST = appinfo.dist_dir()
if os.path.isdir(DIST):
    from fastapi.staticfiles import StaticFiles

    @app.get("/", include_in_schema=False)
    @app.get("/index.html", include_in_schema=False)
    def _index():
        return FileResponse(os.path.join(DIST, "index.html"),
                            media_type="text/html; charset=utf-8",
                            headers={"Cache-Control": "no-cache, must-revalidate"})

    @app.api_route("/{path:path}", methods=["POST", "PUT", "DELETE", "PATCH"],
                   include_in_schema=False)
    def _api_404(path: str):
        # SPA 的 StaticFiles 挂在 / 上，会把未知路径的 POST 吃成 405（暗示"路径存在"）——
        # 已删端点的旧客户端、打错的 API 路径都该得到诚实的 404。GET 不用管：挂载点
        # 本来就回 404。所有真路由都注册在这条兜底之前，不会被动到。
        raise HTTPException(404, "Not Found")

    app.mount("/", StaticFiles(directory=DIST, html=True), name="static")

@app.on_event("startup")
async def _boost_pool():
    """线程池扩容（默认 40）。所有端点都是同步 def，跑在这个池上；LLM 调用一次
    数百秒、流式问答整场占一条，锁的等待者也在池里干等——并发一上来 40 个令牌
    被生成任务占满，文库列表、PDF 取流全在排队（uvicorn 单 worker，没有第二条车道）。
    扩到 128 让"几个任务在生成"不再演成"全站一起卡"。"""
    try:
        from anyio import to_thread
        to_thread.current_default_thread_limiter().total_tokens = 128
    except Exception as e:
        _applog(f"线程池扩容失败（并发任务会挤在默认 40 个令牌上）：{type(e).__name__}: {str(e)[:120]}")

@app.on_event("startup")
def _mark_ready():
    READY.set()

def serve(port: int = DEFAULT_PORT, log_level: str = "info"):
    """起服务。

    打包版传 `log_config=None`：uvicorn 默认要装一套**带颜色的控制台日志**，
    而打包版没有控制台（console=False）——实测在冻结环境里这一步会直接抛
    `ValueError: Unable to configure formatter 'default'`，服务起不来，
    用户那头就是"双击图标没反应"。日志本来也没地方显示，索性不装。
    开发模式保持原样（终端里要看请求日志）。

    用 Config/Server 起而不是 uvicorn.run：`SERVER.started` 在**真正 bind 成功**后
    才置真，desktop.py 的就绪闸盯它认"服务确实起来了"——不再用 bind 探针去猜
    （探针自己占端口，和服务线程的 bind 撞车就是 10048 秒退，2026-10-19 实测）。
    """
    global RELAY_PORT, SERVER
    RELAY_PORT = port              # 翻译中转要知道自己该听哪个口（见 _pdf2zh_env）
    try:
        import window
        window.start_icon_guard()   # 任务栏图标守护：所有 eggpaper 窗口 15s 重钉一次
    except Exception:
        _applog("图标守护启动失败：\n" + traceback.format_exc())
    if appinfo.is_frozen():
        cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, access_log=False)
    else:
        cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level=log_level)
    SERVER = uvicorn.Server(cfg)
    try:
        SERVER.run()
    except KeyboardInterrupt:
        pass                     # 与 uvicorn.run 的收尾一致：Ctrl+C 静默退出

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve(ap.parse_args().port)

"""eggpaper 本地服务。唯一出网：用户配置的 LLM API 与 pdf2zh 翻译服务。"""
import json
import os
import queue
import re
import shutil
import threading
import time
from urllib.parse import urlparse

import uvicorn
from concurrent.futures import ThreadPoolExecutor, as_completed

from fastapi import FastAPI, File, HTTPException, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

import appinfo
import citation

appinfo.migrate_if_needed()   # 数据目录迁移必须在 config/db 打开数据库之前完成

import config
import db
import engine_install
import llm
import pdfparse
import picker
import translate_full
import update

app = FastAPI(title="eggpaper", version="0.1.0")
# 只放行本机来源：界面与服务同源，不需要 CORS，这条只为让 `npm run dev`（Vite 5173）能用。
# 放开成 * 等于任何网页都能读 /api/settings 与论文正文，还能 POST /api/update/install。
app.add_middleware(CORSMiddleware,
                   allow_origin_regex=r"^http://(127\.0\.0\.1|localhost)(:\d+)?$",
                   allow_methods=["*"], allow_headers=["*"])

# 服务"已经能接请求了"的信号。启动器靠它判断就绪——**故意不用"回连自己一次"那种探测**：
# 实测有的机器上（安全软件在管链路），进程连自己 127.0.0.1 的连接会卡在 SYN_SENT（丢包而不是拒绝），
# 于是"服务起来了但探不通"，启动器等 15 秒就把自己退掉——用户看到的就是"双击没反应"。
READY = threading.Event()


def _demo_mode(cfg: dict = None) -> bool:
    """现在这几件事走不走演示数据。**只留这一个判断口。**

    全项目**只有这一个**判断口：用户勾了演示，或者压根没配 key。分散判断会让同一屏里
    一半功能报 `RuntimeError: MOCK`、另一半悄悄给〔演示〕数据。
    """
    cfg = cfg or config.load()
    return bool(cfg["mock"]) or not (cfg.get("provider", {}).get("api_key") or "").strip()



def _human_msg(exc: Exception) -> str:
    """把模型服务最常见的几种失败翻成人话。异常处理器与新加的流式问答共用这一份，
    免得"哪里报错"决定"用户看到什么"。"""
    msg = str(exc) or exc.__class__.__name__
    low = msg.lower()
    if "429" in msg or "too many requests" in low:
        return "模型服务限流了（429），等一会儿再试"
    if "401" in msg or "unauthorized" in low or "invalid api key" in low:
        return "API KEY 无效或过期（401），去设置里检查"
    if "402" in msg or "insufficient" in low or "quota" in low:
        return "账户余额/额度不足，模型服务拒绝了请求"
    if "timeout" in low or "timed out" in low:
        return "模型服务超时了，重试一次通常就好"
    if "connect" in low or "connection" in low:
        return "连不上模型服务，检查网络与 base_url"
    if "404" in msg and "model" in low:
        return "模型名不对（404），去设置里核对"
    if isinstance(exc, (json.JSONDecodeError, ValueError)):
        return "模型这次没按约定的格式回，重试一次通常就好"
    if isinstance(exc, HTTPException):
        return str(exc.detail)
    return f"{exc.__class__.__name__}: {msg[:160]}"


@app.exception_handler(Exception)
async def _any_error(request, exc):
    """别让异常裸奔——前端只会拿到一个"500"，用户看到的就是这个数字。"""
    hint = _human_msg(exc)
    print(f"[eggpaper] {request.url.path} 出错 → {hint}")
    return JSONResponse({"detail": hint}, status_code=500)


@app.exception_handler(RequestValidationError)
async def _bad_params(request, exc):
    """参数不合法时 FastAPI 默认回一坨 422 的数组，前端那句 detail 展示不了。
    翻成一句人话，并压成 400（客户端的问题，不该记成服务端 500）。"""
    errs = exc.errors() or [{}]
    where = ".".join(str(x) for x in errs[0].get("loc", []) if x != "body") or "参数"
    return JSONResponse({"detail": f"{where} 不合法：{errs[0].get('msg', '请求格式不对')}"},
                        status_code=400)

PAPERS_DIR = os.path.join(config.DATA_DIR, "papers")


def paper_dir(pid: str) -> str:
    """一篇论文的全部盘上数据都收在它自己的文件夹里：
    paper.pdf（原件）+ mono.pdf（译文版）+ dual.pdf（双语缓存）+ .pages/（页级中间产物）。
    删论文 = 删文件夹，用户在资源管理器里也一眼能对上号。"""
    return os.path.join(PAPERS_DIR, pid)


def _backfill_pdf_hashes():
    """给旧库论文慢慢补内容指纹（导入判重的第二把钥匙）。

    启动后单独一条低优先级线程：一篇算完歇 2 秒，几百 MB 的库几分钟补完，
    不跟导入/析读抢 IO。算过的论文不会再来第二遍（判据就是指纹为空）。
    """
    import hashlib

    def work():
        for pid, path in db.papers_missing_hash():
            h = _pdf_hash_file(path or "")
            if h:
                try:
                    db.update_paper(pid, pdf_hash=h)
                except Exception:
                    pass
            time.sleep(2)       # 一篇算完歇一拍，不跟导入/析读抢盘
        # 一轮补完就走；补不出的（文件被挪走）下次启动再试

    threading.Thread(target=work, daemon=True).start()


def _migrate_paper_layout():
    """旧平铺布局（library/{pid}.pdf、translated/{pid}-mono/dual.pdf、translated/.pages-{pid}/）
    一次性搬进 papers/{pid}/，db 里的路径同步改写。

    0.1.29 之前原 PDF 和译文平铺在两个公共目录里，论文一多就对不上号；
    现在每篇一个文件夹：paper.pdf + mono.pdf + dual.pdf + .pages/，删论文 = 删文件夹。
    搬完后旧目录里剩下的必然是没有论文指向的孤儿，整个清掉；
    有文件搬不动（被占用）就不删目录，下次启动接着试。"""
    lib = os.path.join(config.DATA_DIR, "library")
    tr = os.path.join(config.DATA_DIR, "translated")
    if not os.path.isdir(lib) and not os.path.isdir(tr):
        return

    def under(path: str, root: str) -> bool:
        p = os.path.normcase(os.path.abspath(path)) if path else ""
        return p.startswith(os.path.normcase(os.path.abspath(root)) + os.sep)

    moved = 0
    stuck = set()        # 搬不动的文件（被占用等）：虽然已被 db 引用，这次只能留下
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
    # 旧目录里剩下的只有两类：搬不动的（db 还引用着，留下）和孤儿（没有任何论文指向，
    # 半截导入/历史残留）——孤儿直接清，旧目录随之整个消失
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


@app.on_event("startup")
def _startup():
    config.ensure_dirs()
    os.makedirs(PAPERS_DIR, exist_ok=True)
    _migrate_paper_layout()
    try:
        for pid in os.listdir(PAPERS_DIR):
            translate_full.sweep_configs(os.path.join(PAPERS_DIR, pid, ".pages"))
    except OSError:
        pass
    translate_full.sweep_page_dirs(PAPERS_DIR)   # 回收没人回来认领的页级中间产物（留给重跑复用的那批）
    _clear_zombie_jobs()
    _backfill_pdf_hashes()

    def _warm_engine():
        # 后台预热 pdf2zh 探测（--version 要 3 秒）：用户打开设置时状态已经在手，
        # 不用看着"未安装"闪两秒才变"可用"
        time.sleep(3)
        try:
            exe = translate_full.engine_path((config.load()["pdf2zh"].get("path") or "").strip())
            translate_full.engine_probe_cached(exe)
        except Exception:
            pass
    threading.Thread(target=_warm_engine, daemon=True).start()


def _sweep_orphan_papers():
    """清掉 papers/ 里没有论文指向的文件夹。

    什么时候会有：导入写了一半进程被杀（PDF 落了盘、库记录没写上），
    或者旧平铺布局迁移前留下的残骸。不清的话它们会一直占着几十 MB，
    而且"删过了"的东西还在盘上。
    """
    try:
        dirs = os.listdir(PAPERS_DIR)
    except OSError:
        return
    known = {row["id"] for row in db.list_papers()}
    n = 0
    for d in dirs:
        if d in known:
            continue
        shutil.rmtree(os.path.join(PAPERS_DIR, d), ignore_errors=True)
        n += 1
    if n:
        _applog(f"启动清理：删掉 {n} 个没有论文指向的文件夹")


def _clear_zombie_jobs():
    """把"上一次进程留下的在跑状态"清掉。

    析读/眉批/翻译都是**守护线程**在跑，而状态写在数据库里；进程一没（崩溃、taskkill、
    装新版重启、托盘退出），那条 `running` 就永远留在库里：POST 看到 running 直接返回，
    用户点多少次都没反应、界面永远停在"写批注中"。

    刚启动的进程里不可能有任务在跑，所以这些状态全是僵尸，一律归零（回到"还没做过"，
    按钮自然重新出现）。运行中途的判断看 `_live_jobs`——那是本进程的真实登记。
    """
    _adopt_orphan_translation()          # 先认领：上一次进程退出时 pdf2zh 可能已经把译完写好了
    _sweep_orphan_papers()               # 再把没人认领的产物清掉
    for col in ("analysis_status", "marginalia_status", "translate_status"):
        n = db.q(f"SELECT COUNT(*) FROM papers WHERE {col} IN ('running','queued')")[0][0]
        if n:
            db.q(f"UPDATE papers SET {col}='none' WHERE {col} IN ('running','queued')", commit=True)
            _applog(f"启动清理：{n} 篇的 {col} 卡在 running/queued，已归零")


def _adopt_orphan_translation():
    """收留"孤儿译文"。

    pdf2zh 是我们起的**独立进程**：eggpaper 关掉/装新版本时它不会被一起带走，
    会接着把 mono.pdf 写完。但那条 running 状态被上面的清理归零了，
    用户回来看到的还是「整本翻译」——白译一场，还得再等两分钟。启动时看一眼文件在不在，
    在就直接认领成 done。只认「看起来完整」的文件（有 %%EOF 收尾），
    免得把写到一半就被杀掉的那份当成成品。
    """
    # 用 get_paper 逐篇取（list_papers 的列里**故意没有** path/dual_path——
    # 那份列表是要发给浏览器的，不该把用户的本地路径捎出去）
    for row in db.list_papers():
        p = db.get_paper(row["id"]) or {}
        if p.get("translate_status") == "done" and (p.get("mono_path") or p.get("dual_path")):
            continue
        got = translate_full.adopt_existing(paper_dir(p["id"]))
        if not got:
            continue
        db.update_paper(p["id"], dual_path=got.get("dual") or "", mono_path=got.get("mono") or "",
                        translate_status="done", translate_error="")
        _applog(f"认领上次没结算的译文：{p['id']} → {os.path.basename(got.get('mono') or got.get('dual'))}")


# ---------------- 设置 ----------------

@app.get("/api/settings")
def get_settings():
    cfg = config.load()
    p = cfg["provider"]
    return {"provider": {"base_url": p["base_url"], "model": p["model"],
                         "vision_model": p.get("vision_model", ""),
                         "has_key": bool(p["api_key"]), "key_masked": (p["api_key"][:6] + "…") if p["api_key"] else ""},
            "mock": cfg["mock"], "pdf2zh": cfg["pdf2zh"], "update": cfg.get("update", {}),
            "data_dir": appinfo.data_dir()}


@app.put("/api/settings")
def put_settings(body: dict):
    cfg = config.load()
    if "provider" in body:
        for k in ("base_url", "model", "vision_model"):
            if k in body["provider"]:
                cfg["provider"][k] = body["provider"][k].strip()
        if isinstance(body["provider"].get("api_key"), str) and "…" not in body["provider"]["api_key"]:
            cfg["provider"]["api_key"] = body["provider"]["api_key"].strip()
    if "mock" in body:
        cfg["mock"] = bool(body["mock"])
    if "pdf2zh" in body:
        cfg["pdf2zh"].update(body["pdf2zh"])
    if "update" in body:
        u = body["update"]
        if isinstance(u.get("feed_url"), str):
            cfg["update"]["feed_url"] = u["feed_url"].strip()
        if "auto_check" in u:
            cfg["update"]["auto_check"] = bool(u["auto_check"])
    # 填了 key 就自动退出演示模式，但**只在用户真的提交了 provider 时才动**：局部保存
    # （如「立即检查更新」只发 {update:{...}}）不能顺手关掉它——用户没碰过模型设置，却会
    # 突然开始真调模型（可能立刻 401/欠费），而弹窗里那个勾还打着。
    was_demo = _demo_mode(cfg)
    if cfg["provider"]["api_key"] and "mock" not in body and "provider" in body:
        cfg["mock"] = False
    config.save(cfg)
    if was_demo != _demo_mode(cfg):
        # 演示↔真实 切换了：把上一模式留下的模型产物清掉。
        # 不清的话，演示模式点过一次的引用卡/一眼卡会一直顶着"已缓存"显示假数据
        # （J. Demo Chem. 那种），换了真 key 也不会自己变。
        n = db.clear_ai_results()
        _applog(f"模型模式切换（演示→{'演示' if _demo_mode(cfg) else '真实'}）：清了 {n} 篇的缓存产物")
    return get_settings()


@app.get("/api/pdf2zh/engine")
def pdf2zh_engine(path: str = ""):
    """整本翻译引擎在哪、能不能跑。设置面板用它显示状态。

    「给别人装」的场景全靠这一条：那台电脑上 pdf2zh 装没装、装在 PATH 之外、
    还是装残了（.exe 在但包里没了）——从前只能靠"点一下整本翻译看它报什么"，
    而报出来的是"bing 连不上"，指错方向。
    """
    cfg = config.load()
    want = (path or cfg["pdf2zh"].get("path") or "").strip()
    exe = translate_full.engine_path(want)
    ok, why = translate_full.engine_probe_cached(exe)
    return {"ok": ok, "path": exe, "why": why, "configured": bool(want)}


@app.post("/api/data/pick")
def data_pick():
    """弹原生目录选择框，返回选中的路径（取消返回空串）。"""
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
        raise HTTPException(400, "整本翻译正在进行，结束后再迁")
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


@app.post("/api/pdf2zh/install")
def pdf2zh_install(body: dict = None):
    """一键把引擎装到用户机器上（从官方源下载，我们不再分发它——AGPL 见 engine_install）。

    body 里可以给 url：国内直连 GitHub 常常慢，用户手上有镜像/局域网地址就填进来。
    """
    url = str(((body or {}).get("url") or "")).strip()
    return engine_install.start(url)


@app.get("/api/pdf2zh/install-status")
def pdf2zh_install_status():
    return engine_install.status()


@app.post("/api/pdf2zh/install-from-file")
async def pdf2zh_install_from_file(file: UploadFile = File(...)):
    """从本地 zip 装引擎：网络到不了 GitHub 时，这条路才是真正可用的分发方式——
    下好一份引擎包，和安装包一起发给别人，对方在这里选那个文件即可（零基础）。"""
    if not (file.filename or "").lower().endswith(".zip"):
        raise HTTPException(400, "要选 .zip 文件")
    tmp = os.path.join(engine_install.install_dir() + ".upload", "engine.zip")
    os.makedirs(os.path.dirname(tmp), exist_ok=True)
    size = 0
    with open(tmp, "wb") as f:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > 2 * 1024 * 1024 * 1024:
                raise HTTPException(400, "文件过大")
            f.write(chunk)
    return engine_install.start_from_zip(tmp)


@app.post("/api/settings/test")
def test_settings():
    return llm.test_connection()


# ---------------- 版本与更新 ----------------

@app.get("/api/version")
def version_info():
    return {"version": appinfo.version(), "packaged": update.is_packaged(),
            "data_dir": config.DATA_DIR, "quitting": _QUITTING["user"]}


@app.get("/api/update/check")
def update_check(force: bool = False):
    """查更新源。auto=0 时只读缓存不联网（打开软件时的那次安静探测走这条）。"""
    cfg = config.load().get("update", {})
    return update.check(cfg.get("feed_url", ""), force=force,
                        cache_hours=float(cfg.get("cache_hours") or 6))


@app.post("/api/update/download")
def update_download(body: dict):
    url = (body or {}).get("url") or ""
    if not url:
        raise HTTPException(400, "没有下载地址")
    update.start_download(url, (body.get("sha256") or "").lower(), int(body.get("size") or 0))
    return update.progress()


@app.get("/api/update/progress")
def update_progress():
    return update.progress()


@app.post("/api/update/install")
def update_install(body: dict):
    """把下好的安装包交给系统，然后本进程退出（安装器要替换的正是它占着的文件）。"""
    path = (body or {}).get("path") or update.progress().get("path") or ""
    if not update.install(path):
        raise HTTPException(400, "安装包不在或无法启动，重新下载一次")
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
    except Exception:
        pass
    return 8430


@app.post("/api/window")
def open_native_window():
    """把界面开成一个没有浏览器边框的独立窗口（界面里点一下就多一个"应用窗口"）。
    开发模式下返回失败原因即可，不必假装成功。"""
    import window as winmod
    how = winmod.open_window(f"http://127.0.0.1:{_my_port()}/")
    if not how:
        raise HTTPException(503, "没找到可用的浏览器（Edge/Chrome），用当前这个窗口看就行")
    return {"ok": True, "how": how}


# 用户主动退出（设置 → 退出 eggpaper）：置位后各页面在 3 秒轮询里看到 quitting
# 就自己关窗（独立窗口是浏览器 --app 模式，window.close() 有效；普通标签页尽力），
# 后端多等一拍轮询再退——不然后台一死，窗口只能靠用户手动一个一个关。
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
    import threading as _th
    def bye():
        time.sleep(3.2)      # ≥ 一整拍 3s 轮询：所有页面都来得及看到 quitting
        os._exit(0)
    _th.Thread(target=bye, daemon=True).start()
    return {"ok": True}


# ---------------- 论文 ----------------

@app.get("/api/papers")
def papers():
    return db.list_papers()


# 注意：这里**不要**加 @app.post("/api/papers")。这个函数是"把已经落在库里的 PDF 建进库"
# 的内部步骤，上传路由（下面那个 async def upload）与"双击打开"都要调它。
# 它头上曾经挂着一个装饰器，而 FastAPI 按注册顺序匹配——于是 POST /api/papers 命中的是它，
# 要求 pid/filename/path 三个查询参数，**浏览器拖入/点击导入永远 422**（双击打开那条路不经过
# 这个路由，所以一直正常，问题就被掩盖了）。
def _pdf_hash_file(path: str) -> str:
    """流式算一份 PDF 的 sha256（几百 MB 也就一两秒，内存只占一块 1MB 的缓冲）。"""
    import hashlib
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def _pdf_hash_bytes(raw: bytes) -> str:
    import hashlib
    return hashlib.sha256(raw).hexdigest()


def _ingest(pid: str, filename: str, path: str, pdf_hash: str = "") -> dict:
    """把已经落在 papers/<pid>/ 里的一份 PDF 建进库（上传与"双击打开"两条路共用）。

    解析失败要收拾干净：留着半篇没有段落的"论文"，用户点开只能看见一个空书架。
    """
    try:
        title = pdfparse.extract_title(path)
        authors = pdfparse.extract_authors(path)
        paras = pdfparse.extract_paragraphs(path)
        import pymupdf
        n_pages = len(pymupdf.open(path))
    except Exception as e:
        try:
            os.remove(path)
        except OSError:
            pass
        raise HTTPException(400, f"这份 PDF 读不了：{_human_msg(e)}")
    db.create_paper(pid, filename, title, path, n_pages, authors)
    if pdf_hash:
        db.update_paper(pid, pdf_hash=pdf_hash)
    db.replace_paragraphs(pid, paras)
    _ensure_paper_type(pid)     # 导入时就判好类型：析读提示词、略读、③、谱系卡都吃这一位
    row = db.get_paper(pid)
    # AI 主动：导入即排队后台通读，打开时简报已就绪（没有文字层的扫描件没得析读，直接标完成）
    db.update_paper(pid, last_read_at=time.strftime("%Y-%m-%d %H:%M:%S"))
    if paras:
        _enqueue_analysis(pid, paras)
    else:
        db.update_paper(pid, analysis_status="done")
    return {"paper": row, "n_paragraphs": len(paras),
            "n_captions": sum(1 for p in paras if p["caption"]),
            "no_text": not paras}


@app.post("/api/papers")
async def upload(file: UploadFile = File(...)):
    raw = await file.read()
    if raw[:4] != b"%PDF":
        raise HTTPException(400, "不是 PDF 文件")
    if len(raw) < 256:
        raise HTTPException(400, "这个文件太小了，不像是完整的 PDF（可能没传完）")
    name = (file.filename or "paper.pdf").split("/")[-1].split("\\")[-1]
    # 内容指纹优先：改名重导的同一份文件也认得出（不占第二份库空间、不重跑析读）
    dup = db.find_duplicate(name, len(raw), _pdf_hash_bytes(raw))
    if dup:
        # 同一份文件已经在库里：不建第二篇，直接把它交出去。
        # 两条导入路径（双击打开 / 拖入点选）都要判重，否则同一篇会进库两遍。
        return {"paper": db.get_paper(dup), "n_paragraphs": len(db.get_paragraphs(dup)),
                "duplicate": True}
    pid = db.new_id()
    os.makedirs(paper_dir(pid), exist_ok=True)
    path = os.path.join(paper_dir(pid), "paper.pdf")
    with open(path, "wb") as f:
        f.write(raw)
    # 解析（抽标题/段落）是**同步阻塞**的活，几秒钟起步。直接在 async 路由里做会卡住整个
    # 事件循环——界面那几条轮询全部停摆，用户看到的是"导入时界面死了"。丢进线程池。
    return await run_in_threadpool(_ingest, pid, name, path, _pdf_hash_bytes(raw))


@app.post("/api/papers/import-path")
def import_path(body: dict):
    """从本机路径导入 PDF：双击 PDF、右键「用 eggpaper 打开」走这条。

    和上传的区别是**不经过浏览器**——文件已经在磁盘上，直接复制进库。
    同一个文件双击两次不该得到两篇：同名同大小（或内容指纹相同）就当成同一份，直接打开它。
    """
    src = os.path.expanduser(((body or {}).get("path") or "").strip().strip('"'))
    if not src or not os.path.isfile(src):
        raise HTTPException(404, f"找不到这个文件：{src or '(空)'}")
    if not src.lower().endswith(".pdf"):
        raise HTTPException(400, "eggpaper 只认 PDF")
    name, size = os.path.basename(src), os.path.getsize(src)
    pdf_hash = _pdf_hash_file(src)
    dup = db.find_duplicate(name, size, pdf_hash)          # 判重口径与上传那条路共用一份
    if dup:
        _pending_open["pid"] = dup               # 已经在库里：让界面切过去就行
        return {"paper": db.get_paper(dup), "duplicate": True}
    pid = db.new_id()
    os.makedirs(paper_dir(pid), exist_ok=True)
    dest = os.path.join(paper_dir(pid), "paper.pdf")
    try:
        shutil.copyfile(src, dest)
    except OSError as e:
        raise HTTPException(400, f"复制不出来：{_human_msg(e)}")
    out = _ingest(pid, name, dest, pdf_hash)
    _pending_open["pid"] = pid
    return out


# 界面每 3 秒轮询一次：拿到"要打开哪一篇"就切过去（双击 PDF 时应用已经开着的情况）
_pending_open = {"pid": None}


@app.get("/api/open-request")
def open_request():
    pid = _pending_open["pid"]
    _pending_open["pid"] = None
    return {"pid": pid, "quitting": _QUITTING["user"]}


def _paper_or_404(pid: str) -> dict:
    p = db.get_paper(pid)
    if not p:
        raise HTTPException(404, "论文不存在")
    return p


NO_TEXT = "这份 PDF 没有可提取的文字层（多半是扫描件），析读和提问都无从下手；原文照样能读，图表也能框选问 AI"


def _require_paras(pid: str) -> None:
    """扫描件没有文字层：让它过一个"请求模型、等半天、返回胡话"的流程是最坏的选择，
    直接说清楚做不到什么、还能做什么。"""
    if not db.get_paragraphs(pid):
        raise HTTPException(400, NO_TEXT)


@app.get("/api/papers/{pid}")
def get_paper(pid: str):
    p = _paper_or_404(pid)
    paras = db.get_paragraphs(pid)
    p["n_paragraphs"] = len(paras)
    return p


def _detect_paper_type(title: str, paras: list) -> str:
    """研究型（research）/ 综述型（review），启发式，一次模型调用都不花。

    综述几乎都会**自报家门**：标题带 review/survey/综述，或者摘要/引言里明说
    "this review / this survey / we review"。三个信号命中其一就算；拿不准一律算
    研究型——综述策略（只灰参考文献、谱系卡）误用到研究型论文上比反过来更难看。
    """
    rx = re.compile(r"\b(reviews?|surveys?|tutorial|primer|state[- ]of[- ]the[- ]art)\b|综述|述评", re.I)
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
        _applog(f"{pid}: 判定为综述，③/谱系卡/略读按综述策略走")
    return t


@app.post("/api/papers/{pid}/touch")
def touch_paper(pid: str):
    """记一笔"最近读过"，文库按最近阅读排序时用。"""
    _paper_or_404(pid)
    db.update_paper(pid, last_read_at=time.strftime("%Y-%m-%d %H:%M:%S"))
    return {"ok": True}


def _rm(path: str):
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


@app.delete("/api/papers/{pid}")
def delete_paper(pid: str):
    p = _paper_or_404(pid)
    db.purge_paper(pid)          # 段落/骨架/眉批/问答会话/分类归属，一张表都不留
    # 文件也要走干净：这篇论文的全部数据就在它自己的文件夹里（paper.pdf + 译文 +
    # 页级中间产物）。先掐掉还在跑的整本翻译：pdf2zh 是独立进程，不掐的话它会把
    # mono.pdf 写回来——用户以为"删掉 = 痕迹全消失"，盘上却留着一份孤立的译文。
    if translate_full.cancel(pid):
        _applog(f"删论文 {pid}：同时终止了还在跑的整本翻译")
    shutil.rmtree(paper_dir(pid), ignore_errors=True)
    _rm(p["path"])               # 库记录指向库外旧位置的兜底（正常都已在 papers/ 里）
    _rm(p["dual_path"]); _rm(p.get("mono_path"))
    return {"ok": True}


# 派生译文/双语文件的互斥锁：首开双语的两个并发请求只该派生一次（写同一文件会写花）
_variant_locks: dict = {}
_variant_guard = threading.Lock()


def _variant_lock(key: str) -> threading.Lock:
    with _variant_guard:
        return _variant_locks.setdefault(key, threading.Lock())


@app.get("/api/papers/{pid}/pdf")
def paper_pdf(pid: str, variant: str = "original"):
    p = _paper_or_404(pid)
    if variant == "dual":
        dual = p.get("dual_path") or ""
        if dual and os.path.exists(dual):
            return FileResponse(dual, media_type="application/pdf")
        # 省盘策略：盘上只落译文版，双语版（它的两倍大）**首次点开时**由 原文+译文 派生并缓存
        mono = p.get("mono_path") or os.path.join(paper_dir(pid), "mono.pdf")
        src = p.get("path") or ""
        if (mono and os.path.exists(mono) and src and os.path.exists(src)):
            with _variant_lock(pid + ":dual"):
                dual = os.path.join(paper_dir(pid), "dual.pdf")
                if not os.path.exists(dual):
                    translate_full.derive_dual(src, mono, dual)
                db.update_paper(pid, dual_path=dual)
            return FileResponse(dual, media_type="application/pdf")
        raise HTTPException(404, "双语版尚未生成")
    if variant == "mono":
        mono = p.get("mono_path") or ""
        if mono and os.path.exists(mono):
            return FileResponse(mono, media_type="application/pdf")
        # 旧版存量只有双语版：抽偶数页合成译文版，补上之后与新品同构
        dual = p.get("dual_path") or ""
        if dual and os.path.exists(dual):
            with _variant_lock(pid + ":mono"):
                mono = os.path.join(paper_dir(pid), "mono.pdf")
                if not os.path.exists(mono):
                    translate_full.derive_mono(dual, mono)
                db.update_paper(pid, mono_path=mono)
            return FileResponse(mono, media_type="application/pdf")
        raise HTTPException(404, "译文版尚未生成")
    if not os.path.exists(p["path"]):
        # 用户在资源管理器里挪走/删掉库里的 PDF 了。必须自己判：交给 FileResponse 会抛
        # RuntimeError("File at path ... does not exist") → 500 + 一句英文黑话。
        raise HTTPException(404, "这篇论文的 PDF 不在原来的位置了（可能被移动或删除）。"
                                 "把它拖回来重新导入一次即可，批注不会丢。")
    return FileResponse(p["path"], media_type="application/pdf")


@app.get("/api/papers/{pid}/paragraphs")
def paragraphs(pid: str):
    _paper_or_404(pid)
    # 旧库的段落只有段落框、没有行级坐标，页边引文就只能整段涂。惰性补一次，但**先核对**：
    # 新解析的结果必须与库里那批段落逐段一致才敢整表替换——批注/主张锚点/略读都按 para_idx
    # 指位置，分组一变就会整体错位，而且是静默的。核对不过就这次不补（页边退回段落框），
    # 至少不动用户已有的东西。
    if db.paragraphs_need_lines(pid):
        p = db.get_paper(pid)
        path = p.get("path") or os.path.join(paper_dir(pid), "paper.pdf")
        try:
            if os.path.exists(path):
                fresh = pdfparse.extract_paragraphs(path)
                if db.paragraphs_match(pid, fresh):
                    db.replace_paragraphs(pid, fresh)
                else:
                    _applog(f"行级坐标补解析 {pid}: 新解析的段落与库里那批对不上，这次不补（避免批注错位）")
        except Exception as e:
            print(f"[eggpaper] 行级坐标补解析失败 {pid}: {e}")
    return db.get_paragraphs(pid)


# ---------------- 骨架分析 ----------------

def _run_analysis(pid: str, paras: list):
    ex = None
    try:
        title = db.get_paper(pid)["title"]
        use = [p for p in paras if not p.get("in_refs")]
        kind = _ensure_paper_type(pid)      # 旧论文升级上来没判过类型，这里兜底补一次
        demo = _demo_mode()
        # 术语表跟骨架**互不依赖**，却曾排在骨架后面串行——骨架跑多久，术语就白等多久
        # （术语是 48k 字进、8k token 出的一次大调用）。先把它发出去，与骨架同时跑。
        # 传的是**全部**段落而不是 use：里面的"正文里有没有这个词"要跟界面上的
        # 判据同源（界面拿的就是全篇），否则会出现界面画"—"、库里其实有。
        ex = ThreadPoolExecutor(max_workers=5)
        tfut = ex.submit(_demo_terms if demo else llm.extract_terms, title, paras)
        if demo:
            data = llm.mock_analyze(paras)
        else:
            data = llm.analyze_skeleton(title, use, kind=kind)
        # 参考文献段强制 boilerplate
        for p in paras:
            if p["in_refs"]:
                data["roles"][str(p["idx"])] = "boilerplate"
                data["purposes"][str(p["idx"])] = "参考文献"
        db.set_analysis(pid, data["claims"], {k: {"role": v, "purpose": data["purposes"].get(k, "")}
                                              for k, v in data["roles"].items()})
        # 主张换了一批，所有"由主张派生的东西"就都是旧结论了：五问②④⑤、一眼卡、
        # 推荐问题、导师三问、方法卡。只清其中一半是最难看的——一眼卡说 A，骨架里
        # 已经没有 A 了，或者三问还在问一个被删掉的主张。宁再生一次。
        db.answers_clear(pid)
        db.update_paper(pid, summary=None, suggest=None, advisor=None, method_card=None,
                        abbrs=json.dumps(data.get("abbrs", {}), ensure_ascii=False),
                        evidence_qs=json.dumps(data.get("evidence_qs", {}), ensure_ascii=False))
        # 五问剩下的三条在**首次析读时一次备齐**：读者点开"动机 / 还能做什么 / 换个学科"
        # 的时候不该再等一次模型调用，界面上也就不需要那个「获取」按钮了。
        # 三条互不依赖 → 并行跑；单条失败只记一行日志，绝不让整次析读陪葬
        # （那一问留空，重新析读会再来一次）。旧口径的①②两问（problem/why）已合成
        # motive，不再依赖骨架顺手写的 problem 字段——它由模型对着缺口段现场生成。
        p2 = db.get_paper(pid)
        todo = ["motive", "next", "lens"]
        futs = {ex.submit(_mock_six, k) if demo else ex.submit(_gen_six, p2, k): k
                for k in todo}
        if not demo:
            # 一眼卡也在这里顺手写掉：原来它由前端在析读完成的下一拍再要一次，
            # 用户得对着"正在写一眼卡…"多等一次调用的工夫。现在析读完即就绪。
            futs[ex.submit(llm.summarize, p2["title"], paras)] = "summary"
            # 推荐问题同理：它吃骨架的主张，原来要等用户第一次进"提问"页才现场生成
            # （4~10 秒的干等，空态里只有四个通用问题）。这里一并写掉。
            _, claims2, annos2 = db.get_analysis(pid)
            futs[ex.submit(llm.suggest_questions, p2["title"], claims2, annos2)] = "suggest"
        futs[tfut] = "terms"
        for fut in as_completed(futs):
            k = futs[fut]
            try:
                got = fut.result()
                if k == "terms":
                    n = _save_terms(pid, got)
                    _applog(f"析读 {pid}: 本篇术语 {n} 条" if n else f"析读 {pid}: 术语这次是空的")
                elif k == "summary":
                    if isinstance(got, dict) and got.get("one_line"):
                        db.update_paper(pid, summary=json.dumps(got, ensure_ascii=False))
                    else:
                        _applog(f"析读 {pid}: 一眼卡这次是空的（速览页会再试一次）")
                elif k == "suggest":
                    if isinstance(got, dict) and got.get("questions"):
                        db.update_paper(pid, suggest=json.dumps(got, ensure_ascii=False))
                    else:
                        _applog(f"析读 {pid}: 推荐问题这次是空的（提问页会再试一次）")
                elif got.get("text") or got.get("items"):
                    db.answer_put(pid, k, got)
                else:
                    _applog(f"析读 {pid}: 五问·{k} 这次是空的")
            except Exception as e:
                # 单条失败只记日志：限流/格式错不该让整次析读陪葬，那一问留空待补
                _applog(f"析读 {pid}: {k} 没生成（{_human_msg(e)}）")
    except Exception as e:
        # 人话 + **不清空**已有结果：一次限流不该让上次读出来的骨架陪葬
        hint = _human_msg(e)
        _applog(f"析读失败 {pid}: {type(e).__name__}: {str(e)[:300]}")
        db.fail_analysis(pid, hint)
    finally:
        if ex:
            ex.shutdown(wait=False, cancel_futures=True)   # 失败路径上没跑完的（术语）就别等了
        _job_done("analysis", pid)


@app.post("/api/papers/{pid}/analyze")
def analyze(pid: str):
    p = _paper_or_404(pid)
    _require_paras(pid)
    if p["analysis_status"] in ("running", "queued") and _analysis_inflight(pid):
        return {"status": p["analysis_status"]}
    if p["analysis_status"] in ("running", "queued"):
        _applog(f"析读 {pid}: 数据库里是 {p['analysis_status']} 但本进程没有这个任务（上次被中断），重来")
    # 也排队：手动重读一篇时，后台可能正在读别的几篇，一起冲上去只会互相抢限流
    _enqueue_analysis(pid, db.get_paragraphs(pid))
    return {"status": "queued"}


@app.get("/api/papers/{pid}/analysis")
def analysis(pid: str):
    p = _paper_or_404(pid)
    status, claims, annos = db.get_analysis(pid)
    if status in ("running", "queued") and not _analysis_inflight(pid):
        db.update_paper(pid, analysis_status="none")        # 僵尸状态：归零
        status = "none"
    eqs = json.loads(p["evidence_qs"]) if p.get("evidence_qs") else {}
    return {"status": status, "error": p["analysis_error"], "claims": claims, "annotations": annos, "evidence_qs": eqs}


@app.post("/api/papers/{pid}/override-role")
def override_role(pid: str, body: dict):
    """人工改判某段的角色。

    界面上没有入口（角色现在只影响略读蒙纱），接口与数据留着：万一模型把该读的段落
    蒙掉了，可以用它改判；读者那边还有一个"这段也要读"的手选（PdfViewer 的 skimKeep）。
    """
    _paper_or_404(pid)
    role = body.get("role") or ""
    # 空串 = 回到推断（卡片上的「回到推断」），别当成非法角色拒掉
    if role and role not in llm.ROLES:
        raise HTTPException(400, "角色不合法")
    try:
        ridx = int(body["para_idx"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "缺 para_idx（要改哪一段）")
    db.override_annotation(pid, ridx, role)
    return {"ok": True}


# ---------------- 眉批（句级批注） ----------------

def _band(n: dict) -> str:
    """一条批注属于哪一档。

    库里存了档位就用存的（自造类型只有存下来的那份算数）；老数据没存过，按类型推。
    判"哪些是可疑之处"要看**档位**而不是类型名：类型是开放词表，模型会自造
    「参考态不一」这种 warn 档的批注，只认 kind=='warning' 会把它们漏在外面。
    """
    return n.get("band") or llm.BAND_OF.get(n["kind"], "")


def _resolve_rects(pid: str):
    """把 quote 定位成页面矩形。

    只作为**退路**：这里的搜索会跨不过换行和连字符，所以只能拿引文开头的一小段去搜，
    搜到的也只是那一行。真正的逐行精确划线在前端做（引文对回字符 → 取字符矩形）。
    框选钉子（kind=region）自带矩形，不参与。

    每条钉子只试一次（`_rect_tried`）：搜不到的钉子（引文跨栏、被截断、模型抄错了）
    永远搜不到，而每试一次就要开一次 PDF。原来 GET /marginalia 每次都把所有没 rect 的
    钉子重试一遍——页边留一条搜不到的钉子，之后每次打开这篇论文都白开一次 PDF。
    """
    import pymupdf
    p = db.get_paper(pid)
    if not p:
        return
    doc = None
    for n in db.get_marginalia(pid):
        if n["rect"] or n["kind"] == "region" or n["id"] in _rect_tried:
            continue
        _rect_tried.add(n["id"])       # 成功失败都算试过，失败的不再重复开 PDF
        if doc is None:
            doc = pymupdf.open(p["path"])
        rects = []
        for cut in (0, 60, 36, 20):
            probe = n["quote"] if cut == 0 else n["quote"][:cut].strip()
            if len(probe) < 8:
                continue
            # 不用 quads=True：它返回的是 Quad（四个角点），没有 x0/y0/x1/y1。
            # 之前这里拿 Quad 当 Rect 取属性，只要页边出现一条**没有矩形**的钉子
            # （比如自己写的那条批注），GET /marginalia 就整个 500——整页眉批打不开。
            rects = doc[n["page"]].search_for(probe)
            if rects:
                break
        if rects:
            r = rects[0]
            db.marginalia_set_rect(n["id"], {"x0": r.x0, "y0": r.y0, "x1": r.x1, "y1": r.y1})
    if doc:
        doc.close()


_rect_tried = set()      # 试过定位的钉子 id（失败的不再重复开 PDF）

# 本进程**真正在跑**的长任务：{("marginalia", pid), ("analysis", pid), ...}
# 数据库里的 running 只是"上一次置的标记"，进程重启后它说明不了任何事；判断"是不是真的在跑"
# 只看这个集合。路由里登记、线程的 finally 里注销——所以"status=running 但不在集合里"
# 就是僵尸状态，可以放心重来（见 _clear_zombie_jobs 的注释）。
_live_jobs = set()
_live_lock = threading.Lock()      # "检查是否在跑 + 占位"要原子（见 marginalia_start）

# 眉批生成的实时进度：pid -> {"done": 已完成块数, "total": 总块数, "t0": 起始时刻}
# 只活在内存里——它是"这一次运行"的状态，进程重启后没有意义（也没必要进数据库）。
_margin_progress = {}


def _job_live(kind: str, pid: str) -> bool:
    return (kind, pid) in _live_jobs


def _job_done(kind: str, pid: str):
    _live_jobs.discard((kind, pid))


# ---------------- 析读队列：一次导入多篇时，一篇一篇地读 ----------------
# 为什么要排队而不是每篇开一条线程：析读是一次**整篇通读**（几十次调用），
# 五篇一起冲上去只会互相抢限流、每篇都变慢，而且用户根本不在等它们。
# 串行之后"最前面的那篇最快好"，界面上也就能说清谁在跑、谁在排队。
_q_lock = threading.Lock()
_analysis_q = queue.Queue()
_analysis_worker = [None]        # 装线程；列表是为了在闭包里能改
# 队列里**等着**的那些 pid。必须单独记一份：`_live_jobs` 是"正在跑"的登记，
# 而排队的论文要等出队那一刻才登记——于是"排队中"曾经既不算在跑、也不在队列里可查，
# 造成两个真 bug：① GET /analysis 把自己的排队当僵尸归零，界面从"排队通读中"退回
# "还没析读"；② 这时再点一次「析读」会**第二次入队**，同一篇被完整通读两遍（双倍 token）。
_analysis_pending = set()


def _analysis_inflight(pid: str) -> bool:
    """这篇是不是"在跑或排队中"（两个真相源合起来看，别再漏一个）。"""
    return _job_live("analysis", pid) or pid in _analysis_pending


def _enqueue_analysis(pid: str, paras: list):
    with _q_lock:
        if pid in _analysis_pending:
            return                      # 已经排着了：再点一次不排队，也不重复烧钱
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
        with _q_lock:
            _analysis_pending.discard(pid)
        _live_jobs.add(("analysis", pid))
        db.update_paper(pid, analysis_status="running", analysis_error=None)
        try:
            _run_analysis(pid, paras)
        finally:
            _job_done("analysis", pid)


def _applog(msg: str):
    """往 app.log 写一行。打包版（console=False）没有 stdout，print 出去的东西一个字都留不下——
    而"这次到底花了多久、卡在哪一段"恰恰是用户最常问的。和 window.py 写的是同一份日志。
    文件超过 1MB 就截到尾部的 1/4：日志只增不删的话，跑上一年能吃掉几十 MB。"""
    try:
        import time as _t
        d = os.path.join(os.path.dirname(appinfo.data_dir()), "logs")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "app.log")
        try:
            if os.path.getsize(path) > 1_000_000:
                with open(path, encoding="utf-8", errors="replace") as f:
                    f.seek(-250_000, 2)
                    f.readline()                    # 扔掉截断的半行
                    tail = f.read()
                with open(path, "w", encoding="utf-8") as f:
                    f.write("[eggpaper] （日志超 1MB，只保留最近一段）\n" + tail)
        except OSError:
            pass
        with open(path, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (_t.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass


def _run_marginalia(pid: str):
    import time as _t
    t0 = _t.time()
    _margin_progress[pid] = {"done": 0, "total": 0, "t0": t0}

    def on_chunk(done, total):
        p = _margin_progress.get(pid) or {"t0": t0}
        p.update(done=done, total=total)
        _margin_progress[pid] = p

    try:
        title = db.get_paper(pid)["title"]
        paras = db.get_paragraphs(pid)
        use = [p for p in paras if not p.get("in_refs")]
        if _demo_mode():
            notes, misses = llm.mock_marginalia(paras), 0
            on_chunk(1, 1)
        else:
            notes, misses = llm.analyze_marginalia(title, use, on_chunk=on_chunk)
        t_llm = _t.time() - t0
        # 有块没成要说出来：状态仍是 done（有效的批注都写进去了），但把缺口写成一条提示
        db.set_marginalia(pid, notes)
        if misses:
            db.update_paper(pid, marginalia_error=(
                f"{misses} 段块没能生成批注（模型限流或超时，已重试过一遍）。"
                f"这些段落现在是空的——想补齐可以再点一次「重新生成」。"))
            _applog(f"眉批 {pid}: {misses} 块重试后仍失败")
        # "还能做什么"吃眉批里的"有坑"，导师三问的输入也是这批 warning——重写眉批要一起作废
        db.answers_clear(pid)
        db.update_paper(pid, advisor=None)
        _resolve_rects(pid)
        t_all = _t.time() - t0
        n = _margin_progress.get(pid, {}).get("total") or 0
        # 一行说清"慢在哪儿"：块数 × 单块时间 = 模型，剩下的是定位与写库。
        # 顺带记下**这一次用的是哪个端点/模型**——"改了设置到底生效没有"看这一行就够。
        cfg = config.load()["provider"]
        _applog(f"眉批完成 {pid}: {len(notes)} 条 / {n} 块 · 模型 {t_llm:.1f}s · "
                f"定位+入库 {t_all - t_llm:.1f}s · 总 {t_all:.1f}s"
                + (f" · 平均 {t_llm / n:.1f}s/块" if n else "")
                + f" · {cfg['model']} @ {cfg['base_url']}")
        print(f"[eggpaper] 眉批 {pid}: {len(notes)} 条 · 模型 {t_llm:.1f}s · 定位 {t_all - t_llm:.1f}s")
    except Exception as e:
        _applog(f"眉批失败 {pid}: {type(e).__name__}: {str(e)[:300]}")
        db.fail_marginalia(pid, _human_msg(e))
    finally:
        _margin_progress.pop(pid, None)
        _job_done("marginalia", pid)


@app.post("/api/papers/{pid}/marginalia")
def marginalia_start(pid: str):
    p = _paper_or_404(pid)
    # 检查与占位必须在**同一把锁**里：路由跑在线程池里是真并发，连点两下（或双击）时
    # 两条请求会都读到"没在跑"，然后各自起一条线程——整篇眉批的模型调用花两遍，
    # 而且先结束的那条会把还在跑的那条的进度抹掉（进度条中途消失）。
    with _live_lock:
        if _job_live("marginalia", pid):
            return {"status": "running"}
        _live_jobs.add(("marginalia", pid))
    _margin_progress[pid] = {"done": 0, "total": 0, "t0": time.time()}
    db.update_paper(pid, marginalia_status="running", marginalia_error=None)
    threading.Thread(target=_run_marginalia, args=(pid,), daemon=True).start()
    return {"status": "running"}


@app.get("/api/papers/{pid}/marginalia")
def marginalia_get(pid: str):
    p = _paper_or_404(pid)
    if p["marginalia_status"] == "running" and not _job_live("marginalia", pid):
        db.update_paper(pid, marginalia_status="none")     # 僵尸状态：归零，让按钮回来
        p = _paper_or_404(pid)
    if p["marginalia_status"] == "done" and any(not n["rect"] for n in db.get_marginalia(pid)):
        _resolve_rects(pid)
    return {"status": p["marginalia_status"], "error": p.get("marginalia_error"),
            "progress": _margin_progress.get(pid),
            "notes": [dict(n, rect=json.loads(n["rect"]) if n["rect"] else None) for n in db.get_marginalia(pid)]}


@app.post("/api/papers/{pid}/pin")
def pin_lookup(pid: str, body: dict):
    """把查译/段译/框选答疑/自己写的批注钉到页边（用户资产，持久化）。"""
    p = _paper_or_404(pid)
    quote = (body.get("quote") or "").strip()
    note = (body.get("note") or "").strip()
    if not quote or not note:
        raise HTTPException(400, "quote 与 note 不能为空")
    try:
        para_idx = int(body.get("para_idx") or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, "para_idx 得是整数")
    page = int(body.get("page") or 0)
    # 自己写的批注：锚点还是选中的那句话，内容是用户的原话。
    # 不比"同段重钉"——同一段里想写两条就写两条，页边是读者的本子，不是去重器。
    if (body.get("kind") or "").strip() == "note":
        return {"id": db.marginalia_add(pid, para_idx, page, quote[:200], note[:600],
                                        kind="note", band="mine")}
    # 框选答疑自带区域矩形：锚点就是那块区域，也不和别的钉子挤同一段。
    # 用 kind=region 单独标记：前端据此知道"这个矩形就是唯一真相"，
    # 而不是像引文钉子那样要回原文重新把引文对回字符。
    rect = body.get("rect") or None
    if rect:
        try:
            rect = {k: float(rect[k]) for k in ("x0", "y0", "x1", "y1")}
        except (KeyError, TypeError, ValueError):
            rect = None
    if rect:
        return {"id": db.marginalia_add(pid, para_idx, page, quote[:200],
                                        note[:600], kind="region", rect=rect, band="mine")}
    # 同段重钉 = 更新而非新增
    dup = db.q("SELECT id FROM marginalia WHERE paper_id=? AND kind='lookup' AND para_idx=?",
               (pid, para_idx))
    if dup:
        db.q("UPDATE marginalia SET note=?, quote=? WHERE id=?", (note[:600], quote[:200], dup[0]["id"]), commit=True)
        return {"id": dup[0]["id"]}
    mid = db.marginalia_add(pid, para_idx, page, quote[:200], note[:600],
                            kind="lookup", band="mine")
    return {"id": mid}


@app.delete("/api/papers/{pid}/marginalia/{mid}")
def marginalia_remove(pid: str, mid: int):
    _paper_or_404(pid)
    db.marginalia_delete(mid)
    return {"ok": True}


# ---------------- 一眼卡 ----------------

@app.get("/api/papers/{pid}/summary")
def summary(pid: str):
    p = _paper_or_404(pid)
    _require_paras(pid)
    if p["summary"]:
        return JSONResponse(json.loads(p["summary"]))
    if _demo_mode():
        data = {"one_line": "〔演示模式〕这是一篇测试论文的一眼卡摘要。", "contributions": "演示贡献", "methods": "演示方法",
                "findings": "演示发现", "keywords": ["演示"]}
    else:
        hits = db.glossary_hit(pid, " ".join(pp["text"] for pp in db.get_paragraphs(pid))[:60000])
        data = llm.summarize(p["title"], db.get_paragraphs(pid), hits)
        _require_shape(data, ("one_line", "findings", "keywords"), "一眼卡")
    db.update_paper(pid, summary=json.dumps(data, ensure_ascii=False))
    return data


@app.get("/api/papers/{pid}/suggest")
def suggest(pid: str):
    p = _paper_or_404(pid)
    _require_paras(pid)
    if p["suggest"]:
        return JSONResponse(json.loads(p["suggest"]))
    if p["analysis_status"] != "done":
        return {"questions": []}
    if _demo_mode():
        data = {"questions": ["〔演示〕核心证据的强度如何？", "〔演示〕方法上有什么可挑剔的？"]}
    else:
        _, claims, annos = db.get_analysis(pid)
        data = llm.suggest_questions(p["title"], claims, annos)
        _require_shape(data, ("questions",), "提问建议")
    db.update_paper(pid, suggest=json.dumps(data, ensure_ascii=False))
    return data


def _require_shape(data, keys: tuple, what: str):
    """模型返回的形状不对/是空的，就别把它当成功缓存下来。

    为什么：`parse_json` 取"第一个 { 到最后一个 }"，模型把结果包成 `[{...}]` 时能解析成
    里面那个对象，于是 `data.get("questions", [])` 得到 `[]`——而路由会把这个空壳
    `json.dumps` 存进 papers，从此永远命中缓存（速览页空着、而且不会自愈，因为"重新析读"
    也不一定清得到它）。五问与引用早就做了这个判断，这里是把它补成统一的一道闸。
    """
    if not isinstance(data, dict) or not any(data.get(k) for k in keys):
        raise HTTPException(503, f"{what}没生成出来（模型这次返回的是空的），过一会儿再点一次")


KIND_ZH = {"hedge": "妥协让步", "padding": "凑字数", "stiff": "生硬别扭", "redundant": "多余重复",
           "hype": "吹嘘过头", "ai": "AI 痕迹", "insight": "点睛之笔", "warning": "有坑",
           "conflict": "前后打架", "lookup": "查译", "region": "选区问答", "note": "批注"}


@app.get("/api/papers/{pid}/advisor")
def advisor(pid: str, cached: bool = False):
    p = _paper_or_404(pid)
    _require_paras(pid)
    if p["advisor"]:
        return JSONResponse(json.loads(p["advisor"]))
    if cached:                       # 同 method-card：进速览页只读缓存，不顺手生成
        return {"questions": []}
    if p["analysis_status"] != "done":
        return {"questions": []}
    if _demo_mode():
        data = {"questions": [{"q": "〔演示〕证据够硬吗？", "outline": ["演示要点"]}]}
    else:
        _, claims, annos = db.get_analysis(pid)
        # 眉批已标的"有坑"当作**作者/读者已经认了的**薄弱点喂进去——三问的任务是
        # 在它们之上再狠一层，而不是把同一批话说第二遍（「问题」页④已经说过一遍了）
        warns = [f"{n['note']}（{n['quote'][:30]}）" for n in db.get_marginalia(pid) if _band(n) == "warn"]
        data = llm.advisor_questions(p["title"], claims, warns)
        _require_shape(data, ("questions",), "导师三问")
    db.update_paper(pid, advisor=json.dumps(data, ensure_ascii=False))
    return data


@app.post("/api/ask-visual")
def ask_visual(body: dict):
    image = body.get("image") or ""
    if not image.startswith("data:image"):
        raise HTTPException(400, "缺少图像数据")
    question = (body.get("question") or "").strip() or "解释这张图/公式。"
    if _demo_mode():
        return {"answer": "〔演示模式〕视觉问答需要配置视觉模型。"}
    ans = llm.vision_ask(image, question)
    if not ans.strip():
        raise HTTPException(503, "模型这次没返回内容，请重试")
    return {"answer": ans}


# ---------------- 五问里需要现场生成的那几问 ----------------
# 免费的两问（要解决什么 / 怎么解决的 / 还没解决什么）直接用骨架数据，不花 token；
# 这三问按需生成、按篇缓存，和「获取」同一个纪律。语料只喂相关的几类段落。

# problem 也走这条：析读时会顺带产出（骨架提示词的 problem 字段），没有缓存时现生成
SIX_KEYS = ("motive", "how", "next", "lens")


def _paras_of_role(pid: str, roles: set, cap: int = 8):
    _, _, annos = db.get_analysis(pid)
    paras = {x["idx"]: x for x in db.get_paragraphs(pid)}
    out = [paras[int(k)] for k, v in annos.items() if v["role"] in roles and int(k) in paras]
    return sorted(out, key=lambda p: p["idx"])[:cap]


def _gen_six(p: dict, key: str):
    pid = p["id"]
    _, claims, annos = db.get_analysis(pid)
    if key == "how":
        # ③「怎么解决的」研究型由前端拿主张-证据链直接拼（不生成）；综述没有实验证据层，
        # 那条路是空壳——由模型直接说清"它把文献怎么组织的"
        if p.get("paper_type") == "review":
            return llm.answer_how_review(p["title"], claims, db.get_paragraphs(pid))
        raise HTTPException(400, "研究型论文的这一问由骨架的主张-证据链直接拼出，无需生成")
    if key == "motive":
        # ①「要解决什么、为什么」：原来的①②两问产出高度重合（都吃缺口段+背景段+主张），
        # 读者也分不清该点哪个——合成一问，两三句话说清"解决什么 + 为什么非解决不可"
        return llm.answer_motive(p["title"],
                                 _paras_of_role(pid, {"gap"}),
                                 _paras_of_role(pid, {"background"}, 6),
                                 claims)
    if key == "next":
        warns = [n["note"] for n in db.get_marginalia(pid) if _band(n) == "warn"][:6]
        return llm.answer_next(p["title"],
                               _paras_of_role(pid, {"limitation"}),
                               _paras_of_role(pid, {"extension"}, 5),
                               claims, warns)
    s = json.loads(p["summary"]) if p.get("summary") else {}
    return llm.answer_lens(p["title"], s.get("one_line", ""), claims, db.get_paragraphs(pid))


def _demo_terms(title, paras) -> dict:
    return {"terms": [{"en": "demo term", "zh": "演示术语", "kind": "method"}], "abbrs": {}}


def _save_terms(pid: str, got) -> int:
    """术语与缩写一次落库——它们是同一次调用的产物，分两处写迟早会只写一半。"""
    got = got if isinstance(got, dict) else {}
    terms = got.get("terms") or []
    if terms:
        db.glossary_put_ai(pid, terms)
    added = db.merge_abbrs(pid, got.get("abbrs") or {})
    if added:
        print(f"[eggpaper] 本篇缩写补了 {added} 条")
    return len(terms)


def _mock_six(key: str) -> dict:
    if key == "how":
        return {"text": "〔演示模式〕这篇综述按它的分类线索把文献组织成三大块，逐块对比优劣，"
                        "最后落到位开放问题上 [¶5]。", "cites": [5]}
    if key == "motive":
        return {"text": "〔演示模式〕现有做法依赖随机、不可控的缺陷位点，做出来的活性没法设计 [¶3]；"
                        "这件事卡住了下游一整类应用，而这到今天没有好解法 [¶2]——"
                        "所以这篇要用本征有序的结构位点来实现可控的高活性。", "cites": [2, 3]}
    if key == "lens":
        return {"v": 2, "items": [
            {"lead": "做表征的", "text": "〔演示模式〕会盯着原位数据太少这件事——漂亮的机理说法要配原位证据才站得住。",
             "ask": "有没有原位数据支持这条机理？", "cites": []},
            {"lead": "做计算的", "text": "〔演示模式〕想拿这套实验数字先验一验自己的力场，对不上就说明模型缺项。",
             "ask": "这套数据能用来校准力场吗？", "cites": []},
            {"lead": "做政策的", "text": "〔演示模式〕看到的是成本表里那笔没算进去的外部性，会追问谁承担。",
             "ask": "成本核算包含外部性吗？", "cites": []},
        ]}
    return {"items": [
        {"lead": "它承认的", "text": "〔演示模式〕换一组对照样品把这条路径单离出来 [¶12]。",
         "ask": "怎么设计对照才能单离这条路径？", "cites": [12]},
        {"lead": "新方向", "text": "〔演示模式〕把这套判据搬去另一族氧化物，够撑一篇新论文："
         "体系换了、结论还没人验证过 [¶18]。", "ask": "换到另一族氧化物要先验证什么？", "cites": [18]},
    ]}


@app.get("/api/papers/{pid}/six-answers")
def six_answers(pid: str):
    """只读缓存：打开一篇论文时问一次，没生成过的题返回 null。"""
    _paper_or_404(pid)
    return db.answers_all(pid)


@app.get("/api/papers/{pid}/six-answers/{key}")
def six_answer(pid: str, key: str):
    p = _paper_or_404(pid)
    if key not in SIX_KEYS:
        raise HTTPException(404, "没有这个问题")
    cached = db.answer_get(pid, key)
    # ⑤⑥换过口径：lens 从"几条视角"→"一段话"→又回到"条目式"（v=2），next 换成
    # "两条腿"（v=2）。旧口径留着只会照旧显示，当它不存在，走下面的重新生成覆盖掉。
    if cached and key in ("lens", "next") and not cached.get("v"):
        cached = None
    if cached:
        return cached
    _require_paras(pid)
    data = _mock_six(key) if _demo_mode() else _gen_six(p, key)
    if not (data.get("text") or data.get("items")):
        raise HTTPException(503, "模型这次没返回内容，重试一次通常就好")
    db.answer_put(pid, key, data)
    return data


@app.get("/api/papers/{pid}/method-card")
def method_card(pid: str, cached: bool = False):
    p = _paper_or_404(pid)
    _require_paras(pid)
    if p["method_card"]:
        return JSONResponse(json.loads(p["method_card"]))
    # cached=1：只读缓存，没有就明说"没有"。进速览页要先把算过的东西显示出来，
    # 但"读缓存"和"花一次模型调用"是两件事，不能让前者偷偷变成后者。
    if cached:
        return {}
    if _demo_mode():
        data = {"goal": "〔演示〕可复现 protocol", "system": "演示体系", "conditions": "演示条件",
                "steps": ["步骤一", "步骤二"], "notes": ""}
    elif p.get("paper_type") == "review":
        # 综述没有"可复现的方法"，同一张卡换谱系口径：分类/脉络/各线关系（普适，不预设数据集）
        data = llm.survey_card(p["title"], db.get_paragraphs(pid))
        _require_shape(data, ("goal", "steps"), "谱系卡")
    else:
        data = llm.method_card(p["title"], db.get_paragraphs(pid))
        _require_shape(data, ("goal", "steps"), "方法卡")
    db.update_paper(pid, method_card=json.dumps(data, ensure_ascii=False))
    return data


@app.get("/api/papers/{pid}/citation")
def paper_citation(pid: str, cached: bool = False, refresh: bool = False):
    """引用信息：作者/刊名/卷期页/DOI，抄一次存下来，之后所有格式都是本地排版。

    和 method-card 同一套规矩：cached=1 只读缓存，没有就明说没有——打开浮层
    不该悄悄花掉一次模型调用。refresh=1 是「重新识别」：认错了要能重认一次。
    排版在 citation.py 里做（模型只负责抄字段）。
    """
    p = _paper_or_404(pid)
    if p["citation"] and not refresh:
        meta = json.loads(p["citation"])
        return {"meta": meta, "groups": citation.groups(meta)}
    if cached:
        return {"meta": None, "groups": []}
    if _demo_mode():
        meta = {"authors": [{"family": "Zhang", "given": "Wei"}, {"family": "Li", "given": "Na"}],
                "title": "〔演示〕一篇论文的标题", "journal": "Journal of Demo Chemistry",
                "journal_abbr": "J. Demo Chem.", "year": "2024", "volume": "12",
                "issue": "3", "pages": "345-352", "doi": "10.0000/demo.2024.12345"}
    else:
        src = pdfparse.citation_source(p["path"])
        raw = llm.extract_citation(p["title"], src)
        # 抄完先过一遍筛：源文里没出现过的字段一律清空（防的是"看起来很合理的假卷号"）
        meta = citation.sanity(raw, src, fallback_title=p["title"], fallback_author=p["authors"] or "")
    if not (meta.get("title") or meta.get("authors")):
        raise HTTPException(503, "首页没认出文献信息，这份 PDF 可能没印刊头刊脚，只能手工补了")
    db.update_paper(pid, citation=json.dumps(meta, ensure_ascii=False))
    return {"meta": meta, "groups": citation.groups(meta)}


@app.get("/api/papers/{pid}/export.md")
def export_md(pid: str):
    p = _paper_or_404(pid)
    paras = {x["idx"]: x for x in db.get_paragraphs(pid)}
    lines = [f"# {p['title'] or p['filename']}", ""]
    if p["summary"]:
        s = json.loads(p["summary"])
        lines += [f"**{s.get('one_line', '')}**", "",
                  f"- 贡献：{s.get('contributions', '')}",
                  f"- 方法：{s.get('methods', '')}",
                  f"- 发现：{s.get('findings', '')}", ""]
    status, claims, annos = db.get_analysis(pid)
    if claims:
        lines += ["## 论证骨架", ""]
        for c in claims:
            lines.append(f"- **{c['id']} {c['text']}**")
            for a in c["anchors"]:
                anno = annos.get(str(a))
                if anno:
                    lines.append(f"  - ¶{a}：{anno['purpose']}")
            lines.append("")
    notes = db.get_marginalia(pid)
    if notes:
        lines += ["## 眉批与查译", ""]
        for n in notes:
            # 类型名与界面口径一致：自造款用模型给的短标签，自己钉的三种算"你 ·"，
            # 其余查同一张表——导出里写的是"前后打架 / 你 · 选区问答"，不是 [conflict]。
            zh = (n.get("label") or "").strip() or KIND_ZH.get(n["kind"], n["kind"])
            who = "你 · " + zh if n["kind"] in ("lookup", "region", "note") else zh
            lines.append(f"- **[{who}] {n['note']}** — “{n['quote'][:48]}”")
        lines.append("")
    md = "\n".join(lines)
    return Response(content=md, media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename=eggpaper-{pid}.md"})


@app.get("/api/papers/{pid}/glossary/export.csv")
def glossary_export(pid: str):
    _paper_or_404(pid)
    import csv
    import io
    buf = io.StringIO()
    buf.write("﻿")     # BOM：中文 Windows 上 Excel/WPS 按 ANSI 解 UTF-8 CSV，不加就是乱码
    w = csv.writer(buf)
    w.writerow(["term_en", "term_zh", "domain", "note", "source"])
    for r in db.glossary_list(pid):
        w.writerow([r["term_en"], r["term_zh"], r["domain"], r["note"], r["source"]])
    return Response(content=buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=eggpaper-terms.csv"})


# ---------------- 图表速览 ----------------

@app.get("/api/papers/{pid}/figures")
def figures(pid: str):
    """图的位置是**这份 PDF 的纯函数**：同一份文件算一次就够（缓存按 路径+mtime）。

    怎么定裁剪框：**以文本为基石 + 二维聚类**。论文里每张图/表都带题注（Fig. 1a /
    Table 2…），把页面上所有"图的内容"——位图、矢量图元、短文本行（长正文行排除，
    它们是分隔物）——按二维邻近（25pt）聚成簇；题注就近挂簇，簇框就是图/表区域。
    通栏图、并排图、多面板图都天然正确：面板之间距离近聚成一簇，两栏正文因为
    是"长行"不参与聚类，不会把区域拉到别人家。

    孤零零没题注、也没进任何簇的大位图自己当一张图；小块（面板碎片）不单列。
    """
    import pymupdf
    p = _paper_or_404(pid)
    try:
        key = (p["path"], os.path.getmtime(p["path"]))
    except OSError:
        key = (p["path"], 0)
    if key in _fig_cache:
        return {"figures": _fig_cache[key]}
    out = []
    if not os.path.exists(p["path"]):
        raise HTTPException(404, "这篇论文的 PDF 不在原来的位置了（可能被移动或删除）")
    doc = pymupdf.open(p["path"])
    try:
        for pno in range(len(doc)):
            page = doc[pno]
            pw, ph = page.rect.width, page.rect.height
            dtext = page.get_text("dict")
            items, caps = [], []
            for blk in dtext.get("blocks", []):
                blines = [ln for ln in blk.get("lines", []) if ln.get("spans")]
                if not blines:
                    continue
                rects = [pymupdf.Rect(ln["bbox"]) for ln in blines]
                first = "".join(sp["text"] for sp in blines[0]["spans"]).strip()
                m = CAPTION_RE.match(first)
                if m:
                    prefix = m.group(1)
                    caps.append({"rect": pymupdf.Rect(blk["bbox"]), "lines": rects,
                                 "cy0": rects[0].y0,
                                 "kind": "table" if prefix[:3].lower().startswith(("tab", "表")) else "figure",
                                 "label": prefix.rstrip(".")})
                    continue                     # 题注行不进聚类（否则会把全页的图串成一簇）
                for ln in blines:
                    t = "".join(sp["text"] for sp in ln["spans"])
                    r = pymupdf.Rect(ln["bbox"])
                    # 正文行长且字多：不进聚类
                    if len(t.strip()) >= 30 and r.width >= 0.3 * pw:
                        continue
                    items.append(r)
            for i in page.get_image_info():
                r = pymupdf.Rect(i["bbox"])
                if r.width > 60 and r.height > 40:
                    items.append(r)
            for drect in page.get_drawings():
                r = drect["rect"]
                if r.width > 8 and r.height > 3:
                    items.append(r)
            # 二维邻近聚类（每侧外扩 PAD 求交，BFS 连通）
            PAD = 11.0
            n = len(items)
            seen = [False] * n
            clusters = []
            for i in range(n):
                if seen[i]:
                    continue
                box = pymupdf.Rect(items[i]) + (-PAD, -PAD, PAD, PAD)
                seen[i] = True
                members = [items[i]]
                stack = [i]
                while stack:
                    j = stack.pop()
                    for k in range(n):
                        if seen[k]:
                            continue
                        if box.intersects(pymupdf.Rect(items[k]) + (-PAD, -PAD, PAD, PAD)):
                            seen[k] = True
                            members.append(items[k])
                            box |= (pymupdf.Rect(items[k]) + (-PAD, -PAD, PAD, PAD))
                            stack.append(k)
                full = pymupdf.Rect(members[0])
                for r in members[1:]:
                    full |= r
                clusters.append({"rect": full, "n": len(members)})
            # 题注挂簇：60pt 内最近的簇；簇框 ∪ 题注 = 区域
            entries = []
            used = set()
            for c in sorted(caps, key=lambda x: x["cy0"]):
                cr = c["rect"]
                best, bd = None, 1e9
                for ci, cl in enumerate(clusters):
                    if ci in used:
                        continue
                    grow = cl["rect"] + (-60, -60, 60, 60)
                    if not grow.intersects(cr):
                        continue
                    dist = (max(cr.y0 - cl["rect"].y1, cl["rect"].y0 - cr.y1, 0)
                            + max(cr.x0 - cl["rect"].x1, cl["rect"].x0 - cr.x1, 0))
                    if dist < bd:
                        bd, best = dist, ci
                if best is None:
                    continue
                used.add(best)
                region = clusters[best]["rect"] | cr
                # 题注近旁的表格横线收进来（顶线常在题注上方 ~20pt）
                for rl in page.get_drawings():
                    r = rl["rect"]
                    if r.height < 3 and r.width > 40 and (region + (-40, -40, 40, 40)).intersects(r):
                        region |= r
                region = pymupdf.Rect(max(20, region.x0 - 4), max(20, region.y0 - 4),
                                      min(pw - 20, region.x1 + 4), min(ph - 20, region.y1 + 4))
                if region.height < 45 or region.width < 60:
                    continue
                c["region"] = region
                entries.append(c)
            # 没被题注认领的大位图：自己当一张图（面板碎片不单列）
            rects = [pymupdf.Rect(i["bbox"]) for i in page.get_image_info()
                     if i["bbox"][2] - i["bbox"][0] > 80 and i["bbox"][3] - i["bbox"][1] > 60]
            for r in rects:
                covered = any((c["region"] & r).get_area() > 0.5 * r.get_area() for c in entries)
                if covered:
                    continue
                in_cluster = any((cl["rect"] & r).get_area() > 0.5 * r.get_area() and ci in used
                                 for ci, cl in enumerate(clusters))
                if in_cluster:
                    continue
                if r.width >= 200 and r.height >= 100:
                    entries.append({"rect": r, "region": r, "kind": "figure",
                                    "label": "", "cy0": r.y0})
            entries.sort(key=lambda x: x["cy0"])
            for c in entries:
                r = c["region"]
                out.append({"page": pno, "x0": round(r.x0, 1), "y0": round(r.y0, 1),
                            "x1": round(r.x1, 1), "y1": round(r.y1, 1),
                            "kind": c["kind"], "label": c.get("label") or ""})
        out.sort(key=lambda e: (e["page"], e["y0"]))
    finally:
        doc.close()
    _fig_cache[key] = out
    while len(_fig_cache) > 8:
        _fig_cache.pop(next(iter(_fig_cache)))
    return {"figures": out}


_fig_cache = {}
# 题注：Fig. 4 / Figure 2a / Table 1 / Tab. 2 / Scheme 3 / 图 3 / 表 1
CAPTION_RE = re.compile(r"^\s*((?:Fig(?:ure)?s?\.?|Table|Tab\.?|Scheme|图|表)\s*\d+\s*[a-z]?)", re.I)
@app.get("/api/papers/{pid}/figure.png")
def figure_png(pid: str, page: int, x0: float, y0: float, x1: float, y1: float, dpi: int = 130):
    import pymupdf
    p = _paper_or_404(pid)
    if not os.path.exists(p["path"]):
        raise HTTPException(404, "这篇论文的 PDF 不在原来的位置了（可能被移动或删除）")
    doc = pymupdf.open(p["path"])
    try:
        # 手工/陈旧请求可能给出越界页码、负矩形、离谱 dpi（dpi=100000 能撑爆内存）：
        # 一律 400/404 并说清哪儿不对，别让它变成 500
        if page < 0 or page >= len(doc):
            raise HTTPException(404, f"页码越界：这篇只有 {len(doc)} 页")
        if not (36 <= dpi <= 400):
            raise HTTPException(400, "dpi 只支持 36–400")
        if x1 - x0 < 4 or y1 - y0 < 4:
            raise HTTPException(400, "截图范围太小")
        pix = doc[page].get_pixmap(clip=pymupdf.Rect(x0, y0, x1, y1), dpi=dpi)
        return Response(content=pix.tobytes("png"), media_type="image/png")
    finally:
        doc.close()


# ---------------- 问答（流式 + 多会话） ----------------

def _sse(obj: dict) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


def _mock_stream(question: str):
    """演示模式也走流式：同一条前端代码路径，接上真 key 不用改任何东西。"""
    text = ("〔演示模式〕这是模拟回答，用来跑通界面。[¶1] 配好 API key 后这里会是真答案。\n\n"
            "· 你问的是：" + question[:60] + "\n"
            "· 回答会逐字出现，可以中途停下；停下时已经吐出来的部分会留着。")
    for i in range(0, len(text), 3):
        yield text[i:i + 3]
        time.sleep(0.02)


def _autotitle(pid: str, conv_id: int, question: str, is_first: bool):
    """第一个问题就是这摊对话的标题——和豆包/DeepSeek 一样，省得用户自己起名。"""
    if not is_first:
        return
    c = db.conv_get(conv_id)
    if c and c["title"] in ("", "新对话"):
        t = question.strip().replace("\n", " ")[:18]
        db.conv_rename(conv_id, t + ("…" if len(question.strip()) > 18 else ""))


# 上下文预算：最近几轮原样带，更早的折进摘要。
# 保留 8 条（4 轮）原文——足够接住"你刚才说的那个""再详细点"这类指代；
# 折到 12 条 / 6000 字以上才动手，别每问一句都去调一次压缩。
KEEP_MSGS, FOLD_AT, FOLD_CHARS = 8, 12, 6000


def _context(pid: str, conv_id: int, history: list):
    """返回 (摘要, 原样带上的历史)。超预算就把较早的几条压成摘要存回会话。

    压缩在提问之前同步做完，代价是每折一次多一次模型调用；但这是"宁慢不丢"的一步：
    直接把老消息截断，用户前面确认过的结论和术语就会凭空消失，模型随即开始自相矛盾。
    **压缩失败（限流、超时）时不许静默丢**：把老消息截短了照样带上，粗糙好过失忆。
    """
    c = db.conv_get(conv_id) or {}
    upto = c.get("summary_upto") or 0
    # tail = 还没被折进摘要的那些（删过的消息这里自然就没有了）
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
    # 摘要没成：截短了带上，别丢
    short = [dict(m, content=(m.get("content") or "")[:240] + "…") for m in head[-40:]]
    return prev, short + rest


def _stream_answer(p: dict, conv_id: int, question: str):
    """流式回答。事件三种：delta（增量文字）/ done（依据段号 + 落库 id）/ error。

    落库的时机有两处：正常结束在这里写；用户中途点"停止生成"由前端调 qa-save 写，
    因为客户端断开时服务端不保证还能把生成器走完——让能拿到半截答案的那一端负责存。
    """
    pid = p["id"]
    uid = db.qa_add(pid, "user", question, conv_id=conv_id)
    hist = db.qa_history(pid, conv_id)[:-1]      # 不含刚写进去的这条；删过的消息这里自然就没有了
    buf = []
    try:
        if _demo_mode():
            gen = _mock_stream(question)
        else:
            summary, ctx = _context(pid, conv_id, hist)
            hits = db.glossary_hit(pid, " ".join(pp["text"] for pp in db.get_paragraphs(pid))[:60000])
            gen = llm.chat_stream(llm.ask_messages(p["title"], db.get_paragraphs(pid), ctx, question,
                                                   hits, summary))
        for piece in gen:
            buf.append(piece)
            yield _sse({"type": "delta", "text": piece})
        ans = "".join(buf)
        if not ans.strip():
            raise RuntimeError("模型这次没返回内容")
        cites = llm.cites_of(ans)
        aid = db.qa_add(pid, "assistant", ans, cites, conv_id=conv_id)
        _autotitle(pid, conv_id, question, not any(h["role"] == "user" for h in hist))
        yield _sse({"type": "done", "citations": cites, "user_id": uid, "assistant_id": aid})
    except Exception as e:
        hint = _human_msg(e)
        ans = "".join(buf)
        aid = db.qa_add(pid, "assistant", (ans + "\n\n⚠ " + hint) if ans.strip() else "⚠ " + hint,
                        llm.cites_of(ans), conv_id=conv_id)
        yield _sse({"type": "error", "message": hint, "user_id": uid, "assistant_id": aid})


@app.post("/api/papers/{pid}/ask")
def ask(pid: str, body: dict):
    p = _paper_or_404(pid)
    question = (body.get("question") or "").strip()
    if not question:
        raise HTTPException(400, "问题不能为空")
    _require_paras(pid)          # 没有原文就没有"只依据原文"这回事，它不该去答
    conv_id = body.get("conv_id")
    if conv_id:
        c = db.conv_get(int(conv_id))
        if not c or c["paper_id"] != pid:
            raise HTTPException(404, "会话不存在")
        conv_id = int(conv_id)
    else:
        conv_id = db.conv_list(pid)[0]["id"]
    # 校验都过了再开流：一旦开始 SSE，HTTP 头已经发出去，改不成 4xx 了
    return StreamingResponse(_stream_answer(p, conv_id, question), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                                      "Connection": "keep-alive"})


@app.get("/api/papers/{pid}/conversations")
def conversations(pid: str):
    _paper_or_404(pid)
    return db.conv_list(pid)

@app.post("/api/papers/{pid}/conversations")
def conversation_new(pid: str, body: dict = None):
    _paper_or_404(pid)
    return {"id": db.conv_create(pid, (body or {}).get("title") or "新对话")}


@app.patch("/api/conversations/{cid}")
def conversation_patch(cid: int, body: dict):
    if not db.conv_get(cid):
        raise HTTPException(404, "会话不存在")
    db.conv_rename(cid, (body.get("title") or "新对话").strip() or "新对话")
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
    返回两端 id，前端据此把"删除/重新生成"接回真实的行上。"""
    _paper_or_404(pid)
    content = (body.get("content") or "").strip()
    conv_id = body.get("conv_id")
    if not content:
        return {"ok": False}
    aid = db.qa_add(pid, "assistant", content, llm.cites_of(content), conv_id=conv_id)
    return {"ok": True, "citations": llm.cites_of(content),
            "assistant_id": aid, "user_id": db.qa_last_user_id(pid, conv_id) if conv_id else None}


@app.post("/api/papers/{pid}/regenerate")
def qa_regenerate(pid: str, body: dict):
    """重新生成：把这一问一答都撤掉，返回原问题，由前端重新发问。"""
    _paper_or_404(pid)
    conv_id = body.get("conv_id")
    if not conv_id:
        raise HTTPException(400, "缺少会话")
    q = db.qa_drop_last_assistant(pid, int(conv_id))
    if not q:
        raise HTTPException(400, "没有可重新生成的问题")
    return {"question": q}


@app.delete("/api/conversations/{cid}/messages/{mid}")
def qa_delete_one(cid: int, mid: int):
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
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "分类要有名字")
    return {"id": db.collection_add(name)}


@app.patch("/api/collections/{cid}")
def collection_patch(cid: int, body: dict):
    name = (body.get("name") or "").strip()
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
    db.set_paper_collections(pid, ids)
    return {"ok": True, "ids": ids}


# ---------------- 翻译 ----------------

def _mock_translate(text: str):
    """演示模式的假译文也假装在打字：同一条前端代码路径。"""
    t = "〔演示译文〕" + text[:120]
    for i in range(0, len(t), 3):
        yield t[i:i + 3]
        time.sleep(0.02)


def _translate_sse(pid: str, text: str, context: str, hits: list):
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
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "没有选中文本")
    hits = db.glossary_hit(pid, text)
    context = body.get("context", "")
    return StreamingResponse(_translate_sse(pid, text, context, hits), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/papers/{pid}/translate-para")
def translate_para(pid: str, body: dict):
    p = _paper_or_404(pid)
    paras = {p_["idx"]: p_ for p_ in db.get_paragraphs(pid)}
    try:
        idx = int(body.get("idx"))
    except (TypeError, ValueError):
        raise HTTPException(400, "缺 idx（要译哪一段）")
    if idx not in paras:
        raise HTTPException(404, "段落不存在")
    _require_paras(pid)          # 扫描件没有段落可译，直说
    para = paras[idx]
    hits = db.glossary_hit(pid, para["text"])
    ctx = paras.get(idx - 1, {}).get("text", "")
    return StreamingResponse(_translate_sse(pid, para["text"], ctx, hits), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _pdf2zh_env(service: str, cfg: dict):
    """整本翻译要的 key 从哪来：**用用户在「设置」里已经填的那一套**，不让他填第二遍。

    只经环境变量交给子进程（pdf2zh 读 OPENAI_*/DEEPSEEK_* 这些）。
    注意 pdf2zh 自己会把拿到的值落到 `~/.config/PDFMathTranslate/config.json`
    （它的 GUI 就靠那个文件），这一层我们管不了——能保证的是 eggpaper 这边
    不落盘、不进库、不打印。返回 (环境变量, 端点主机名)。
    """
    prov = cfg.get("provider") or {}
    key = (prov.get("api_key") or "").strip()
    base = (prov.get("base_url") or "").strip()
    model = (prov.get("model") or "").strip()
    if not key:
        return {}, ""
    if service == "openai":
        envs = {"OPENAI_API_KEY": key}
        if base:
            envs["OPENAI_BASE_URL"] = base
        if model:
            envs["OPENAI_MODEL"] = model
        return envs, (urlparse(base).hostname or "") if base else ""
    if service == "deepseek":
        envs = {"DEEPSEEK_API_KEY": key}
        if model:
            envs["DEEPSEEK_MODEL"] = model
        return envs, "api.deepseek.com"
    return {}, ""


@app.post("/api/papers/{pid}/translate-full")
def translate_full_start(pid: str, force: bool = False):
    p = _paper_or_404(pid)
    cfg = config.load()
    svc = (cfg["pdf2zh"].get("service") or "bing").strip()
    envs, host = _pdf2zh_env(svc, cfg)
    # 先探一下这个服务连不连得通。连不通的后果不是"慢"而是**永远不动**
    # （pdf2zh 会在每个请求上重试，CPU 0、界面停在「翻译中」），所以宁可在门口拦住。
    # 盘上已经有成品就先认领：pdf2zh 是独立进程，eggpaper 退出后它可能才写完——那次
    # 启动扫描已经过去了，状态被清成 none，用户再点一次会**重译一遍并覆盖**刚做好的文件。
    # force=1（界面上的「重新整本翻译」）跳过认领：译文打不开就得重译，认领旧文件没意义。
    if not force:
        got = translate_full.adopt_existing(paper_dir(pid))
        if got:
            db.update_paper(pid, dual_path=got.get("dual") or "", mono_path=got.get("mono") or "",
                            translate_status="done", translate_error="")
            _applog(f"整本翻译 {pid}: 发现上次已经译好的成品，直接认领")
            return {"status": "done", "service": "", "note": "上次已经译好了，直接用了那份成品"}
    used, note = translate_full.choose_service(svc, host)
    if used is None:
        raise HTTPException(400, note)
    engine = (cfg["pdf2zh"].get("path") or "").strip()
    # 引擎自检放在**点按钮这一拍**：后台线程里失败的话，用户要等一轮页级流水线跑完
    # 才看到错误（还可能被误报成网络问题）。这里当场说清楚，代价是一次 --version。
    exe = translate_full.engine_path(engine)
    ok, why = translate_full.engine_probe_cached(exe)
    if not ok:
        raise HTTPException(400, f"缺 pdf2zh 引擎（{why}）。到「设置 → 翻译引擎」安装，或填写路径。")
    translate_full.start(pid, p["path"], paper_dir(pid), used,
                         cfg["pdf2zh"].get("options", ""), envs=envs, log=_applog,
                         note=note, engine=engine)
    db.update_paper(pid, translate_status="running", translate_error="")
    return {"status": "running", "service": used, "note": note}


@app.get("/api/papers/{pid}/translate-status")
def translate_full_status(pid: str):
    p = _paper_or_404(pid)
    j = translate_full.job(pid)
    if j["status"] == "done":
        # 盘上只落译文版（省盘），dual 在这里是空串——别拿它去清掉旧版留下的双语文件：
        # 只有 mono 变了才落库；mono 没动就不写，dual_path 保持原样（派生缓存继续有效）
        if (j["mono"] or "") != (p["mono_path"] or ""):
            old_dual = p.get("dual_path") or ""
            db.update_paper(pid, mono_path=j["mono"] or "", dual_path="",
                            translate_status="done", translate_error="")
            # 重译过的论文，上一版的双语文件就是**旧译文**：不删的话「双语」按 dual_path
            # 还在盘上会直接端出来，用户看着旧译文以为重译没生效。删掉让首开按新 mono 重派生。
            if old_dual:
                _rm(old_dual)
    elif j["status"] == "error" and p["translate_status"] != "error":
        db.update_paper(pid, translate_status="error", translate_error=j["error"])
    return j


# ---------------- 术语表 ----------------

@app.get("/api/papers/{pid}/glossary")
def glossary_list(pid: str):
    _paper_or_404(pid)
    return db.glossary_list(pid)


@app.post("/api/papers/{pid}/glossary")
def glossary_add(pid: str, body: dict):
    _paper_or_404(pid)
    en = (body.get("term_en") or "").strip()
    zh = (body.get("term_zh") or "").strip()
    if not en or not zh:
        raise HTTPException(400, "中英文都要填")
    gid = db.glossary_add(pid, en, zh, body.get("domain", ""), body.get("note", ""),
                          body.get("source", "manual"))
    return {"id": gid}


# 一篇术语的生成互斥锁：同一篇被点两次（比如飞速切页签、或两个窗口）只该花一次 token。
_terms_locks: dict = {}
_terms_guard = threading.Lock()


def _terms_lock(pid: str) -> threading.Lock:
    with _terms_guard:
        return _terms_locks.setdefault(pid, threading.Lock())


@app.post("/api/papers/{pid}/glossary/generate")
def glossary_generate(pid: str):
    """按篇发掘术语（+这篇自己的缩写）：这一篇还没有词表时，打开术语页调它一次。

    为什么要懒生成：0.1.12 之前词表是全库共用的（那批种子行已删），旧论文的按篇词表是空的，
    而"为了看一眼术语把整篇重新析读一遍"的代价太大。这里只在**确实为空**时花钱，
    生成过就纯读库（第二次进来不发请求）。析读时照样会生成，这条路只是补历史欠账。
    """
    _paper_or_404(pid)
    with _terms_lock(pid):
        rows = db.glossary_list(pid)
        if rows:                       # 并发下第二个请求在这里等到结果，直接拿走
            return {"items": rows, "generated": False, "abbrs": _abbrs_of(pid)}
        _require_paras(pid)
        p = db.get_paper(pid)
        got = _demo_terms(p["title"], []) if _demo_mode() else llm.extract_terms(
            p["title"], db.get_paragraphs(pid))
        if not _save_terms(pid, got):
            raise HTTPException(503, "模型这次没给出术语，过一会儿再试一次")
        # abbrs 一并回：缩写表和术语表是同一批的产物，界面不用为此再取一次论文
        return {"items": db.glossary_list(pid), "generated": True, "abbrs": _abbrs_of(pid)}


def _abbrs_of(pid: str) -> dict:
    row = db.get_paper(pid)
    try:
        out = json.loads((row or {}).get("abbrs") or "{}")
        return out if isinstance(out, dict) else {}
    except Exception:
        return {}


@app.delete("/api/glossary/{gid}")
def glossary_delete(gid: int):
    db.glossary_delete(gid)
    return {"ok": True}


# ---------------- 前端静态托管（构建后） ----------------

@app.get("/guide")
def guide_page():
    """使用指南：一页静态 HTML。设置里可打开；砍界面文案时的安全网。"""
    return FileResponse(os.path.join(os.path.dirname(__file__), "guide.html"),
                        media_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "no-cache, must-revalidate"})


DIST = appinfo.dist_dir()
if os.path.isdir(DIST):
    from fastapi.staticfiles import StaticFiles

    # index.html 不能缓存：它里面写着这次构建的 chunk 文件名，缓存住旧的就会去要
    # 已经不存在的 chunk（新装的 _internal 里旧 chunk 已被清掉）。chunk 自己带
    # 内容 hash，可以放心长缓存——**只有这个壳必须每次问服务器**。
    @app.get("/", include_in_schema=False)
    @app.get("/index.html", include_in_schema=False)
    def _index():
        return FileResponse(os.path.join(DIST, "index.html"),
                            media_type="text/html; charset=utf-8",
                            headers={"Cache-Control": "no-cache, must-revalidate"})

    app.mount("/", StaticFiles(directory=DIST, html=True), name="static")


@app.on_event("startup")
def _mark_ready():
    READY.set()


def serve(port: int = 8430, log_level: str = "info"):
    """起服务。

    打包版传 `log_config=None`：uvicorn 默认要装一套**带颜色的控制台日志**，
    而打包版没有控制台（console=False）——实测在冻结环境里这一步会直接抛
    `ValueError: Unable to configure formatter 'default'`，服务起不来，
    用户那头就是"双击图标没反应"。日志本来也没地方显示，索性不装。
    开发模式保持原样（终端里要看请求日志）。
    """
    if appinfo.is_frozen():
        uvicorn.run(app, host="127.0.0.1", port=port, log_config=None, access_log=False)
    else:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level=log_level)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8430)
    serve(ap.parse_args().port)

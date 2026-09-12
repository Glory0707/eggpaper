"""pdf2zh 整本翻译封装：subprocess 隔离，绝不让 AGPL 代码进入本项目。

这个文件里的每一条"绕路"都是实测踩出来的，别照直觉改回去：

1. **必须带 CREATE_NO_WINDOW**。`pdf2zh.exe` 是控制台程序，而 eggpaper 打包版
   （console=False）自己没有控制台。Windows 的规矩是：没有控制台的进程去起一个
   控制台子进程，就给它**新开一个黑窗**。于是用户点一下「整本翻译」，屏幕正中间
   蹦出一个终端——那个终端不是我们的日志窗口，是 pdf2zh 自己的控制台。

2. **默认服务不能想当然**。pdf2zh 的 `google` 用的是 translate.google.com，在多数
   国内网络下**连不上**。连不上不是"慢"：它会在每个请求上重试，进程 CPU 恒为 0，
   界面永远停在「翻译中」，能拖到天荒地老（实测 4 分钟 0 CPU、0 输出）。
   所以开跑前先做一次 TCP 预检（2.5 秒），不通就当场换服务或者报错，别让它静默挂着。

3. **pdf2zh 的进度只在 stdout**。它用 tqdm 打 `11%|██ | 2/18 [00:07<00:57]`，
   用 Popen 逐行读 stdout：页进度报给前端并写进 app.log（管道里的 tqdm 每条进度是独立行，
   不是 \r 刷屏，正好逐行解析）。capture_output 那种跑法等于全程黑箱，用户什么都看不到。

4. **产物必须按源文件名的词干找**。整个翻译件共用一个 out_dir，而 pdf2zh 按输入名
   命名产物（`abc.pdf` → `abc-dual.pdf`）。用 `sorted(os.listdir())[0]` 认产物，译第二篇时
   会把第一篇的双语 PDF 认成自己的——A 的译文挂到 B 上。
"""
import collections
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time

JOBS = {}      # paper_id -> {status, error, dual, mono, service, note, pages, started}
_RUNNING = {}  # paper_id -> Popen：删论文时要能把它掐掉（见 cancel）
_PROBE = {}    # host -> (ok, why, at)  连通性预检的短期缓存
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_PROG = re.compile(r"(\d+)%\|[^|]*\|\s*(\d+)/(\d+)")
# "key 错了"这一类：pdf2zh 只会逐段刷日志、不会自己停，所以这里主动认
AUTH_FAILS = ("authentication fails", "invalid api key", "incorrect api key",
              "401 - ", "unauthorized", "invalid_api_key")

PROBE_TIMEOUT = 2.5      # 预检单次连接的上限：够连通的早连通了，不通的也别让用户等
PROBE_TTL = 600          # 一次预检结果管 10 分钟，别每次点按钮都白等 2.5 秒

# 各服务要先能连上的主机。故意不写全：拿不准的（自建端点、本地模型、需要另配的
# 云服务）留空 = 不预检——宁可让它自己去失败，也别在这里误报"不通"挡住一条能走的路。
SERVICE_HOST = {
    "google": "translate.google.com",
    "bing": "www.bing.com",
    "deepl": "api-free.deepl.com",
    "openai": "api.openai.com",              # 有 provider.base_url 时以它为准
    "deepseek": "api.deepseek.com",
    "zhipu": "open.bigmodel.cn",
    "silicon": "api.siliconflow.cn",
    "modelscope": "api-inference.modelscope.cn",
    "gemini": "generativelanguage.googleapis.com",
    "grok": "api.x.ai",
    "groq": "api.groq.com",
    "tencent": "tmt.tencentcloudapi.com",
}
# 配的服务不通时挨个试：免费、不需要 key、国内基本能连
AUTO_FALLBACK = ("bing",)


# ---------------- 连通性预检 ----------------

def probe(host: str, port: int = 443, timeout: float = PROBE_TIMEOUT, ttl: float = PROBE_TTL):
    """能不能连上 host:port。返回 (是否通, 不通时的一句人话)。结果缓存 ttl 秒。"""
    if not host:
        return True, ""                      # 不知道连哪儿就别拦
    hit = _PROBE.get(host)
    now = time.time()
    if hit and now - hit[2] < ttl:
        return hit[0], hit[1]
    ok, why = False, ""
    try:
        ip = socket.gethostbyname(host)
        s = socket.create_connection((ip, port), timeout=timeout)
        s.close()
        ok = True
    except Exception as e:
        why = f"{host} 连不上（{type(e).__name__}）"
    _PROBE[host] = (ok, why, now)
    return ok, why


def choose_service(service: str, host: str = ""):
    """定下这次真正用哪个服务。返回 (服务名或 None, 给用户的一句话)。

    host 传了就以它为准（openai 服务用的是用户在设置里填的那个端点）。
    """
    service = (service or "bing").strip()
    target = host or SERVICE_HOST.get(service, "")
    if probe(target)[0]:
        return service, ""
    why = probe(target)[1]
    for alt in AUTO_FALLBACK:
        if alt == service or not probe(SERVICE_HOST.get(alt, ""))[0]:
            continue
        return alt, f"{service} 在你的网络下不通（{why}），已自动改用 {alt}"
    return None, (f"{why}。「设置 → 整本翻译服务」换一个能用的（国内推荐 bing），"
                  f"或者先连上外网再试。")


# ---------------- 起进程 ----------------

def _cmd(pdf_path, out_dir, service, extra, cfg_path=""):
    exe = shutil.which("pdf2zh") or os.path.join(os.path.dirname(sys.executable), "pdf2zh.exe")
    if os.path.exists(exe):
        base = [exe]
    elif getattr(sys, "frozen", False):
        # 打包版没带 pdf2zh（AGPL 引擎另装）：这里直接抛 FileNotFoundError，
        # 由下面翻成人话，别让它去试"用打包出来的 exe 当 python 跑模块"那种怪命令
        raise FileNotFoundError("pdf2zh")
    else:                                    # 兜底：模块方式（新版 pdf2zh-next 支持）
        base = [sys.executable, "-m", "pdf2zh"]
    cmd = base + [pdf_path, "-o", out_dir, "--service", service]
    if cfg_path:
        cmd += ["--config", cfg_path]
    if extra:
        cmd += shlex.split(extra)
    return cmd


def _no_window():
    """(creationflags, startupinfo)：让子进程**不要**开出控制台窗口。

    CREATE_NO_WINDOW 是解药；startupinfo 那两行是给某些还看 SW_HIDE 的老路径上的
    双保险，两个一起给不冲突。
    """
    if os.name != "nt":
        return 0, None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0                       # SW_HIDE
    return 0x08000000, si                    # CREATE_NO_WINDOW


PDF2ZH_CONFIG = os.path.join(os.path.expanduser("~"), ".config", "PDFMathTranslate", "config.json")


def pinned_config(out_dir: str, pid: str, overlay: dict = None) -> str:
    """把 pdf2zh 的配置"钉"在一份临时副本上，返回路径（拿不到就返回空 = 不传）。

    为什么：pdf2zh 会把从环境变量读到的 OPENAI_API_KEY / DEEPSEEK_API_KEY
    **写进它自己的 `~/.config/PDFMathTranslate/config.json`**（它的 GUI 靠那个文件）。
    用户在 eggpaper 里选了 openai/deepseek 服务，key 不该顺带被复制到另一个程序的配置里。
    传 `--config <副本>` 之后它只写副本——实测（假 key 跑的）：真配置的 mtime 与
    sha256 一个字节没动，副本里拿到了那个 key。

    副本里先原样放**真配置的内容**：这样除了"写哪儿"，行为与不传时完全一致
    （用户自己设过的字体路径之类不会丢）。读不出来就退化成不传，不为了隐私把功能弄坏。
    overlay：本次运行的环境变量（key/base_url/model）**覆盖**副本里的同名项——
    pdf2zh 读配置优先于读环境，不覆盖的话副本里的旧 key 会顶掉这次真正要用的 key
    （实测：假 key 的测试因此"成功"跑真 key）。
    """
    dst = os.path.join(out_dir, f".pdf2zh-{pid}.json")
    try:
        if os.path.exists(PDF2ZH_CONFIG):
            with open(PDF2ZH_CONFIG, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {}
        if not isinstance(data, dict):
            data = {}
        if overlay:
            data.update({k: v for k, v in overlay.items() if v})
        with open(dst, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        return dst
    except Exception:
        return ""


def sweep_configs(out_dir: str, keep: str = "", older_than: float = 6 * 3600):
    """清掉陈旧的 --config 副本（里面有 key，别让它躺着）。

    **必须按年龄清**：out_dir 是所有论文共用的，而"另一篇正在翻译"的副本就在同一个目录里。
    "见到 .pdf2zh-*.json 就删"会把另一篇正在用的副本删掉——那份正是用来拦住 pdf2zh
    把 key 写进用户 ~/.config 的。keep 是本次要用的那份。
    """
    now = time.time()
    try:
        for fn in os.listdir(out_dir):
            if not (fn.startswith(".pdf2zh-") and fn.endswith(".json")):
                continue
            path = os.path.join(out_dir, fn)
            if keep and os.path.abspath(path) == os.path.abspath(keep):
                continue
            try:
                if now - os.path.getmtime(path) > older_than:
                    os.remove(path)
            except OSError:
                pass
    except OSError:
        pass


def cancel(pid: str):
    """删论文时用：把还在跑的 pdf2zh 掐掉。

    pdf2zh 是独立进程——不掐的话，"删掉这篇论文"之后它还会跑完、还会往
    translated/ 里写回 <pid>-dual.pdf，用户以为删干净了、盘上却留下孤立的译文文件。
    """
    j = job(pid)
    j["_abort"] = True                      # 页级流水线：让 worker 别再起下一页
    procs = _RUNNING.get(pid) or []
    for proc in procs:
        try:
            proc.kill()
        except Exception:
            pass
    return bool(procs)


def adopt_existing(pid: str, pdf_path: str, out_dir: str):
    """盘上已经有"看起来完整"的成品就认领，返回 {"dual":..., "mono":...} 或 None。

    为什么要有：pdf2zh 是独立进程，eggpaper 关掉/装新版本时它还在跑，写完之后没人认领——
    启动时那次扫描早过了。用户看到界面上还是「整本翻译」，点一次就重译一遍（还覆盖成品）。
    判定"完整"看 %%EOF 收尾，免得把写到一半就被杀掉的半截文件当成成品。
    """
    stem = os.path.splitext(os.path.basename(pdf_path))[0]
    dual = os.path.join(out_dir, stem + "-dual.pdf")
    try:
        if os.path.getsize(dual) < 10_000:
            return None
        with open(dual, "rb") as f:
            f.seek(max(0, os.path.getsize(dual) - 2048))
            tail = f.read()
    except OSError:
        return None
    if b"%%EOF" not in tail:
        return None
    mono = os.path.join(out_dir, stem + "-mono.pdf")
    return {"dual": dual, "mono": mono if os.path.exists(mono) else ""}


def job(pid: str) -> dict:
    return JOBS.setdefault(pid, {"status": "none", "error": "", "dual": "", "mono": "",
                                 "service": "", "note": "", "pages": [0, 0], "started": 0.0})



# ---------------- 整本翻译：按页流水线 ----------------
# 为什么不再"一个 pdf2zh 进程译整本"：实测它对单段翻译失败是**无限重试**
#（tenacity 默认没有上限），一页挂住全本挂住——8 页的论文停在 4/8 十几分钟不动
#（用户报的"卡在中间"），重试整本也没用：还是那个段落、还是挂。
# 改成按页起进程：
#   · 进度 = 已完成的页数，粒度天然精确，不会再"卡在中间不知道怎么回事"；
#   · 单页有超时，坏页最多拖几分钟就放弃，**绝不让一页拖死整本**；
#   · 失败的页回退用原文（双语里这一页是两页原文），成品永远完整、页码永不错位；
#   · 两个 worker 并行，墙钟时间比串行短；pdf2zh 自带译文缓存，重试的那页也快。

PAGE_WORKERS = 2         # 并发页数：开太大只会更快撞上服务限流
PAGE_TIMEOUT = 150       # 单页上限 2.5 分钟：正常一页几秒到几十秒；坏页早放弃早回退
PAGE_TRIES = 2           # 每页试两次（第二次走 pdf2zh 的译文缓存，通常很快）


def _page_dir(out_dir: str, pid: str, pno: int, t: int) -> str:
    return os.path.join(out_dir, f".pages-{pid}", f"p{pno}-{t}")


def _run_page(pdf_path: str, pno: int, out_dir: str, service: str, extra: str,
              envs: dict, cfg: str, proc_reg: list, auth_out: list) -> tuple:
    """翻译一页。返回 (产物 dict 或 None, 日志尾行 list)。超时/报错返回 (None, tail)。
    proc_reg：正在跑的进程都登记进来，删论文时 cancel() 能把它们掐掉。
    auth_out：一旦在输出里看到鉴权失败就**立刻**杀进程并记到这里——
    pdf2zh 对 401 是无限重试，等它自己结束要磨到天荒地老。"""
    cmd = _cmd(pdf_path, out_dir, service, extra, cfg) + ["--pages", str(pno)]
    tail = collections.deque(maxlen=5)
    flags, si = _no_window()
    proc = None
    deadline = time.time() + PAGE_TIMEOUT

    def _kill_when_stale():
        while proc.poll() is None and time.time() < deadline:
            time.sleep(2)
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace",
                                cwd=out_dir, env={**os.environ, **(envs or {})},
                                creationflags=flags, startupinfo=si)
        proc_reg.append(proc)
        threading.Thread(target=_kill_when_stale, daemon=True).start()
        for raw in proc.stdout:
            line = _ANSI.sub("", raw).strip()
            if not line or chr(13) in line or _PROG.search(line):
                continue                          # 进度行不进 tail（太吵）
            low = line.lower()
            if any(k in low for k in AUTH_FAILS):
                auth_out.append(f"{service} 服务的 key 不对（pdf2zh 报鉴权失败）：{line[-160:]}")
                try:
                    proc.kill()
                except Exception:
                    pass
                continue
            tail.append(line)
        proc.wait()
    except Exception as e:
        tail.append(f"{type(e).__name__}: {str(e)[-160:]}")
        return None, list(tail)
    stem = os.path.splitext(os.path.basename(pdf_path))[0]
    dual = os.path.join(out_dir, stem + "-dual.pdf")
    mono = os.path.join(out_dir, stem + "-mono.pdf")
    if os.path.exists(dual) and os.path.exists(mono):
        return {"dual": dual, "mono": mono}, list(tail)
    if not any("单页超时" in t for t in tail):
        tail.append(f"单页失败（退出码 {proc.poll()}）")
    return None, list(tail)


def start(pid: str, pdf_path: str, out_dir: str, service: str, extra: str = "",
          envs: dict = None, log=None, note: str = "") -> dict:
    """按页流水线翻译整本：进度=完成页数，坏页回退原文，一页卡不住整本。"""
    j = job(pid)
    if j["status"] == "running":
        return j

    def run():
        _say = say = log or (lambda _m: None)
        j.update(status="running", error="", dual="", mono="", service=service,
                 note=note, pages=[0, 0], started=time.time())
        page_root = os.path.join(out_dir, f".pages-{pid}")
        cfg_copy = ""
        procs = []                           # 在跑的页进程，cancel() 按这个掐
        try:
            import pymupdf
            os.makedirs(out_dir, exist_ok=True)
            os.makedirs(page_root, exist_ok=True)
            with pymupdf.open(pdf_path) as doc:
                n = len(doc)
            if envs:
                cfg_copy = pinned_config(page_root, pid)   # 拦住 key 被写进 pdf2zh 的配置
            j["pages"] = [0, n]
            _RUNNING[pid] = procs
            results = {}                     # pno(0 基) -> {"dual","mono"}
            lock = threading.Lock()
            auth_error = []                  # 鉴权失败：整本必败，立刻停下

            def worker(pno: int):
                # 单页的一切失败只影响这一页（回退原文），绝不允许拖死整本
                try:
                    _worker(pno)
                except Exception as e:
                    _say(f"整本翻译 {pid}: 第 {pno + 1} 页异常（{type(e).__name__}），保留原文")

            def _worker(pno: int):
                pdir = _page_dir(out_dir, pid, pno, 0)
                os.makedirs(pdir, exist_ok=True)
                cfg = (pinned_config(pdir, pid, overlay=envs) if envs else "") or cfg_copy
                got, tail = None, []
                auth_out = []
                for t in range(PAGE_TRIES):
                    if auth_error or j.get("_abort"):
                        return
                    pdir_t = _page_dir(out_dir, pid, pno, t)
                    os.makedirs(pdir_t, exist_ok=True)
                    got, tail = _run_page(pdf_path, pno + 1, pdir_t, service, extra,
                                          envs, cfg, procs, auth_out)
                    if auth_out:
                        auth_error.append(auth_out[0])
                        return
                    if got:
                        if t > 0:
                            _say(f"整本翻译 {pid}: 第 {pno + 1} 页第二次尝试成功")
                        break
                with lock:
                    if got:
                        results[pno] = got
                    j["pages"] = [len(results), n]
                if not got:
                    _say(f"整本翻译 {pid}: 第 {pno + 1} 页两次都没译成，这一页保留原文"
                         f"（{' / '.join(tail[-2:])}）")

            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=PAGE_WORKERS) as pool:
                list(pool.map(worker, range(n)))

            if auth_error:
                j.update(status="error", error=auth_error[0])
                say(f"整本翻译失败 {pid}：{auth_error[0]}")
                return
            if j.get("_abort"):
                j.update(status="error", error="已取消")
                return

            # ---- 组装：dual=奇页原文偶页译文；mono=纯译文。失败页回退原文 ----
            stem = os.path.splitext(os.path.basename(pdf_path))[0]
            dual_path = os.path.join(out_dir, stem + "-dual.pdf")
            mono_path = os.path.join(out_dir, stem + "-mono.pdf")
            failed = []
            src = pymupdf.open(pdf_path)
            dual = pymupdf.open()
            mono = pymupdf.open()
            try:
                for pno in range(n):
                    got = results.get(pno)
                    if got:
                        with pymupdf.open(got["dual"]) as d:
                            dual.insert_pdf(d)
                        with pymupdf.open(got["mono"]) as m:
                            mono.insert_pdf(m)
                    else:
                        failed.append(pno + 1)
                        # 双语里这一页放两页原文：保住"奇页原文偶页译文"的页码关系
                        dual.insert_pdf(src, from_page=pno, to_page=pno)
                        dual.insert_pdf(src, from_page=pno, to_page=pno)
                        mono.insert_pdf(src, from_page=pno, to_page=pno)
                if n and not results:
                    j.update(status="error",
                             error=f"所有页面都没译成（{service} 连不上或被限流）。"
                                   "换一个翻译服务（设置 → 整本翻译）再试。")
                    say(f"整本翻译失败 {pid}：全部页面失败")
                    return
                dual.save(dual_path)
                mono.save(mono_path)
            finally:
                src.close(); dual.close(); mono.close()

            done_note = (note or "") + (f"（第 {', '.join(map(str, failed))} 页没译成，保留原文）"
                                        if failed else "")
            j.update(status="done", dual=dual_path, mono=mono_path if os.path.exists(mono_path) else "",
                     error="", pages=[n, n], note=done_note)
            _say(f"整本翻译完成 {pid}：{int(time.time() - j['started'])}s · {service}"
                 f" · {len(results)}/{n} 页" + (f" · 失败页 {failed}" if failed else ""))
        except Exception as e:
            j.update(status="error", error=f"{type(e).__name__}: {str(e)[-400:]}")
            say(f"整本翻译失败 {pid}：{type(e).__name__}: {str(e)[-300]}")
        finally:
            _RUNNING.pop(pid, None)
            shutil.rmtree(page_root, ignore_errors=True)   # 页级产物已组装，别留盘
            for proc in procs:
                try:
                    proc.kill()
                except Exception:
                    pass

    threading.Thread(target=run, daemon=True).start()
    return j

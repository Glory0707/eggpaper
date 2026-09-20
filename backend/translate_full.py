"""pdf2zh 全文翻译封装：subprocess 隔离，绝不让 AGPL 代码进入本项目。

这个文件里的每一条"绕路"都是实测踩出来的，别照直觉改回去：

1. **必须带 CREATE_NO_WINDOW**。`pdf2zh.exe` 是控制台程序，而 eggpaper 打包版
   （console=False）自己没有控制台。Windows 的规矩是：没有控制台的进程去起一个
   控制台子进程，就给它**新开一个黑窗**。于是用户点一下「全文翻译」，屏幕正中间
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

JOBS = {}
_RUNNING = {}
_PROBE = {}
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_PROG = re.compile(r"(\d+)%\|[^|]*\|\s*(\d+)/(\d+)")
AUTH_FAILS = ("authentication fails", "invalid api key", "incorrect api key",
              "401 - ", "unauthorized", "invalid_api_key")

PROBE_TIMEOUT = 2.5
PROBE_TTL = 600

SERVICE_HOST = {
    "google": "translate.google.com",
    "bing": "www.bing.com",
    "deepl": "api-free.deepl.com",
    "openai": "api.openai.com",
    "deepseek": "api.deepseek.com",
    "zhipu": "open.bigmodel.cn",
    "silicon": "api.siliconflow.cn",
    "modelscope": "api-inference.modelscope.cn",
    "gemini": "generativelanguage.googleapis.com",
    "grok": "api.x.ai",
    "groq": "api.groq.com",
    "tencent": "tmt.tencentcloudapi.com",
}
AUTO_FALLBACK = ("bing",)

# ---------------- 连通性预检 ----------------

def probe(host: str, port: int = 443, timeout: float = PROBE_TIMEOUT, ttl: float = PROBE_TTL):
    """能不能连上 host:port。返回 (是否通, 不通时的一句人话)。结果缓存 ttl 秒。"""
    if not host:
        return True, ""
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
    return None, (f"{why}。「设置 → 全文翻译服务」换一个能用的（国内推荐 bing），"
                  f"或者先连上外网再试。")

# ---------------- 引擎：在哪、能不能跑 ----------------

def engine_path(explicit: str = "") -> str:
    """找 pdf2zh 引擎可执行文件；找不到返回空串。

    **只查 PATH 和 exe 同目录是不够的**——pip 装的 pdf2zh 落在 Python 的 Scripts
    目录，而那个目录不保证在 PATH 里（pip 自己都会提醒"不在 PATH"）。实测本机
    `%LOCALAPPDATA%\\Programs\\Python\\Python312\\Scripts\\pdf2zh.exe` 就躺在那儿，
    而 `shutil.which` 一个都看不见——"明明装了却说没装"就是这么来的。

    顺序：设置里指定的 → PATH → exe 同目录 → 数据目录 engines/ → 常见 Python Scripts。
    最后一处（数据目录 engines/）是给"手动放一个进来"留的稳位：那目录升级、卸载都不动。
    """
    import glob
    if os.environ.get("EGGPAPER_ENGINE_OFF"):
        return ""      # E2E 专用：假设这台机器没有引擎，验证"缺引擎自动安装"链路
    want = (explicit or "").strip().strip('"')
    if want and os.path.exists(want):
        return want
    cand = []
    w = shutil.which("pdf2zh")
    if w:
        cand.append(w)
    here = os.path.dirname(os.path.abspath(sys.executable))
    cand.append(os.path.join(here, "pdf2zh.exe"))
    try:
        import engine_install
        got = engine_install.find_installed()
        if got:
            cand.insert(0, got)
    except Exception:
        pass
    for pat in (r"%LOCALAPPDATA%\Programs\Python\Python3*\Scripts\pdf2zh.exe",
                r"%APPDATA%\Python\Python3*\Scripts\pdf2zh.exe",
                r"C:\Python3*\Scripts\pdf2zh.exe",
                r"C:\Program Files\Python3*\Scripts\pdf2zh.exe"):
        try:
            cand.extend(glob.glob(os.path.expandvars(pat)))
        except Exception:
            pass
    for p in cand:
        if p and os.path.exists(p):
            return p
    return ""

def engine_probe(path: str) -> tuple:
    """跑一次 `pdf2zh --version`，确认它**真的能跑**。返回 (可用, 说明)。

    为什么不能只看文件在不在：pip 卸载后残留的 .exe 壳照样在，运行时报
    `ModuleNotFoundError: No module named 'pdf2zh'`（本机实测就有一个）。那种
    "引擎看着在、每页都失败"的现场，只查文件存在永远查不出来。
    """
    if not path:
        return False, "没找到"
    if not os.path.exists(path):
        return False, "路径下没有文件"
    flags, si = _no_window()
    try:
        r = subprocess.run([path, "--version"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120,
                           creationflags=flags, startupinfo=si)
    except Exception:
        return False, "起不来"
    out = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
    low = out.lower()
    if "no module named 'pdf2zh'" in low or "modulenotfounderror" in low:
        return False, "exe 在但包已丢（空壳）"
    if r.returncode == 0 and "pdf2zh" in low:
        line = next((ln.strip() for ln in out.splitlines() if "pdf2zh" in ln.lower()), "")
        return True, line[:60]
    tail = out.splitlines()[-1].strip()[:160] if out else ""
    return False, tail or f"退出码 {r.returncode}"

_ENGINE = {}

def engine_probe_cached(path: str, ttl: float = 900) -> tuple:
    """engine_probe 的缓存版。`--version` 实测要 3 秒（pdf2zh 得 import 整套依赖），
    而这是**点一下按钮就要过的一道门**——每次等 3 秒不值。同一个文件 15 分钟内只探
    一次；文件被换掉（重装、改路径）mtime 一变立刻重探，不会拿着旧结论骗人。"""
    if not path:
        return engine_probe(path)
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return engine_probe(path)
    hit = _ENGINE.get(path)
    if hit and hit["mtime"] == mt and time.time() - hit["at"] < ttl:
        return hit["ok"], hit["why"]
    ok, why = engine_probe(path)
    _ENGINE[path] = {"mtime": mt, "ok": ok, "why": why, "at": time.time()}
    return ok, why

# ---------------- 起进程 ----------------

def _cmd(pdf_path, out_dir, service, extra, cfg_path="", engine: str = ""):
    exe = engine_path(engine)
    if exe:
        base = [exe]
    elif getattr(sys, "frozen", False):
        raise FileNotFoundError("pdf2zh")
    else:
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
    si.wShowWindow = 0
    return 0x08000000, si

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
    已删掉的论文文件夹里写回 mono.pdf，用户以为删干净了、盘上却留下孤立的译文文件。
    """
    j = job(pid)
    if j.get("status") in ("running", "queued"):
        # 只给在跑/在排队的一轮置旗：空闲时置旗会被下一轮 start 当成"取消前到达"误吞
        j["_abort"] = True
    procs = _RUNNING.get(pid) or []
    for proc in procs:
        try:
            proc.kill()
        except Exception:
            pass
    return bool(procs)

def adopt_existing(out_dir: str):
    """盘上已经有"看起来完整"的成品就认领，返回 {"dual","mono"}（缺的一方是空串）或 None。

    为什么要有：pdf2zh 是独立进程，eggpaper 关掉/装新版本时它还在跑，写完之后没人认领——
    启动时那次扫描早过了。用户看到界面上还是「全文翻译」，点一次就重译一遍（还覆盖成品）。
    判定"完整"看 %%EOF 收尾，免得把写到一半就被杀掉的半截文件当成成品。
    盘上**只落译文版（mono）**省盘，双语版按需派生。
    """
    mono = os.path.join(out_dir, "mono.pdf")
    dual = os.path.join(out_dir, "dual.pdf")
    mono_ok = _pdf_complete(mono)
    dual_ok = _pdf_complete(dual)
    if not mono_ok and not dual_ok:
        return None
    return {"dual": dual if dual_ok else "", "mono": mono if mono_ok else ""}

def job(pid: str) -> dict:
    return JOBS.setdefault(pid, {"status": "none", "error": "", "dual": "", "mono": "",
                                 "service": "", "note": "", "pages": [0, 0], "started": 0.0})

# ---------------- 全文翻译：按页流水线 ----------------

PAGE_WORKERS_FREE = 3
PAGE_WORKERS_LLM = 2
PAGE_TIMEOUT = 150
PAGE_TRIES = 2
BATCH_PAGES = 4          # 一次 pdf2zh 进程译几页：冷启动 ~3s/次，批得越大摊得越薄；失败整批拆单页兜底

LLM_SERVICES = {"openai", "deepseek", "zhipu", "silicon", "modelscope",
                "gemini", "grok", "groq"}

def _page_workers(service: str) -> int:
    return PAGE_WORKERS_LLM if service in LLM_SERVICES else PAGE_WORKERS_FREE

def _page_timeout(service: str) -> float:
    return PAGE_TIMEOUT * 2 if service in LLM_SERVICES else PAGE_TIMEOUT

def _page_dir(out_dir: str, pno: int, t: int) -> str:
    return os.path.join(out_dir, ".pages", f"p{pno}-{t}")

def _page_done(pdir: str, stem: str):
    """这一页的目录里有没有一份完整的译文版产物（%%EOF 收尾）。
    页级双语产物用完即删（省盘：组装成品由 原文+译文 派生，双语成品按需派生）。
    有 → 返回 {"mono": 路径}；没有 → None。重跑时靠它跳过已译好的页。"""
    mono = os.path.join(pdir, stem + "-mono.pdf")
    return {"mono": mono} if _pdf_complete(mono) else None

def derive_dual(pdf_path: str, mono_path: str, dual_path: str) -> str:
    """双语版 = 原文奇页 + 译文偶页交错。盘上只留译文版（省盘：双语是它的两倍大），
    用户第一次点「双语」时才合成并缓存。失败页（译文就是原文那页）交错出来天然是两页原文，
    与"奇原文偶译文"的页码关系一致。"""
    import pymupdf
    src = pymupdf.open(pdf_path)
    mono = pymupdf.open(mono_path)
    dual = pymupdf.open()
    try:
        for i in range(max(len(src), len(mono))):
            dual.insert_pdf(src, from_page=min(i, len(src) - 1), to_page=min(i, len(src) - 1))
            if i < len(mono):
                dual.insert_pdf(mono, from_page=i, to_page=i)
            else:
                dual.insert_pdf(src, from_page=min(i, len(src) - 1), to_page=min(i, len(src) - 1))
        # 半截产物一旦被当成品服务就固化了：先落临时名再原子换名
        dual.save(dual_path + ".part", garbage=4, deflate=True)
        os.replace(dual_path + ".part", dual_path)
    finally:
        src.close(); mono.close(); dual.close()
    return dual_path

def derive_mono(dual_path: str, mono_path: str) -> str:
    """旧版存量只有双语版时，抽偶数页（0 基 2i+1）合成译文版。"""
    import pymupdf
    dual = pymupdf.open(dual_path)
    mono = pymupdf.open()
    try:
        for i in range(1, len(dual), 2):
            mono.insert_pdf(dual, from_page=i, to_page=i)
        mono.save(mono_path + ".part", garbage=4, deflate=True)
        os.replace(mono_path + ".part", mono_path)
    finally:
        dual.close(); mono.close()
    return mono_path

def sweep_page_dirs(papers_root: str, max_age: float = 48 * 3600):
    """回收陈旧的 .pages/ 页级目录（页级产物各自内嵌全文档的字体，一份好几 MB）。

    翻译没跑完时成功的页**故意留着**（重跑直接复用，不再重译一遍），但用户也可能
    再也不回来——那就按年龄回收。启动时对整库调一次。
    """
    now = time.time()
    try:
        papers = os.listdir(papers_root)
    except OSError:
        return
    for pid in papers:
        path = os.path.join(papers_root, pid, ".pages")
        try:
            if os.path.isdir(path) and now - os.path.getmtime(path) > max_age:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass

def _sweep_key_copies(page_root: str, pid: str):
    """页级目录里的 pdf2zh 配置副本带着 key：留着复用是**为了省时间**，不是为了存 key。
    目录留下之前把副本扫掉（正文产物不需要它们）。"""
    for dirpath, _dirs, files in os.walk(page_root):
        for fn in files:
            if fn.startswith(f".pdf2zh-{pid}") and fn.endswith(".json"):
                try:
                    os.remove(os.path.join(dirpath, fn))
                except OSError:
                    pass

def _page_env(envs: dict) -> dict:
    """pdf2zh 子进程的环境。HOME/USERPROFILE 指到 eggpaper 数据目录下的 home/：
    pdf2zh 会往 ~/.config/PDFMathTranslate/ 写自己的配置——不指过去就散落在
    C 盘用户目录（用户要求：我们装的和写的都规范在 eggpaper 的文件夹里）。
    传给它的 --config 副本照旧生效，key 不落明文。"""
    env = {**os.environ, **(envs or {})}
    try:
        import appinfo
        home = os.path.join(appinfo.data_dir(), "home")
        os.makedirs(home, exist_ok=True)
        env["HOME"] = home
        env["USERPROFILE"] = home
    except Exception:
        pass
    return env

def _pages_count(pages: str) -> int:
    """--pages 的页数："5" → 1，"5-8" → 4。给超时预算用。"""
    m = re.match(r"^(\d+)(?:-(\d+))?$", (pages or "").strip())
    if not m:
        return 1
    a, b = int(m.group(1)), int(m.group(2) or m.group(1))
    return max(1, b - a + 1)

def _run_page(pdf_path: str, pages: str, out_dir: str, service: str, extra: str,
              envs: dict, cfg: str, proc_reg: list, auth_out: list, engine: str = "") -> tuple:
    """翻译一页（或一个页区间）。返回 (产物 dict 或 None, 日志尾行 list)。超时/报错返回 (None, tail)。
    proc_reg：正在跑的进程都登记进来，删论文时 cancel() 能把它们掐掉。
    auth_out：一旦在输出里看到鉴权失败就**立刻**杀进程并记到这里——
    pdf2zh 对 401 是无限重试，等它自己结束要磨到天荒地老。

    为什么收"页区间"：pdf2zh 每次冷启动约 3s（全套依赖 import），一页一进程时 30 页
    = 30 次纯开销。按 4 页一批冷启动摊薄 4 倍；批里单页坏了拆成单页再各试一遍，
    复用粒度从"页"退到"批"，断点续译（_page_done 逐页认）不变。
    """
    cmd = _cmd(pdf_path, out_dir, service, extra, cfg, engine) + ["--pages", pages]
    tail = collections.deque(maxlen=5)
    flags, si = _no_window()
    proc = None
    deadline = time.time() + _page_timeout(service) * _pages_count(pages)

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
                                cwd=out_dir, env=_page_env(envs),
                                creationflags=flags, startupinfo=si)
        proc_reg.append(proc)
        threading.Thread(target=_kill_when_stale, daemon=True).start()
        for raw in proc.stdout:
            line = _ANSI.sub("", raw).strip()
            if not line or chr(13) in line or _PROG.search(line):
                continue
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
    mono = os.path.join(out_dir, stem + "-mono.pdf")
    if _pdf_complete(mono):
        try:
            os.remove(os.path.join(out_dir, stem + "-dual.pdf"))
        except OSError:
            pass
        return {"mono": mono}, list(tail)
    if not any("单页超时" in t for t in tail):
        tail.append(f"单页失败（退出码 {proc.poll()}）")
    return None, list(tail)

def _pdf_complete(path: str) -> bool:
    """文件存在且以 %%EOF 收尾（写到一半被杀的文件没有这个）。"""
    try:
        if os.path.getsize(path) < 10_000:
            return False
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - 2048))
            return b"%%EOF" in f.read()
    except OSError:
        return False

def start(pid: str, pdf_path: str, out_dir: str, service: str, extra: str = "",
          envs: dict = None, log=None, note: str = "", engine: str = "") -> dict:
    """按页流水线翻译全文：进度=完成页数，坏页回退原文，一页卡不住整份文档。"""
    j = job(pid)
    if j["status"] == "running":
        return j

    def run():
        _say = say = log or (lambda _m: None)
        if j.pop("_abort", None):
            # start 返回后、线程还没调度到就被取消（或删论文）：安静收场，别照跑到底
            j.update(status="none", error="", pages=[0, 0])
            return
        j.update(status="running", error="", dual="", mono="", service=service,
                 note=note, pages=[0, 0], started=time.time())
        page_root = os.path.join(out_dir, ".pages")
        cfg_copy = ""
        procs = []
        results = {}
        exe = engine_path(engine)
        ok, why = engine_probe_cached(exe)
        if not ok:
            msg = f"缺 pdf2zh 引擎（{why}）。到「设置 → 翻译引擎」安装，或填写路径。"
            j.update(status="error", error=msg)
            say(f"全文翻译失败 {pid}：pdf2zh 引擎不可用（{why}）")
            return
        say(f"全文翻译 {pid}: 用引擎 {exe}（{why}）")
        try:
            import pymupdf
            os.makedirs(out_dir, exist_ok=True)
            try:
                sig = json.dumps({"m": int(os.path.getmtime(pdf_path)),
                                  "s": os.path.getsize(pdf_path)})
            except OSError:
                sig = ""
            mark = os.path.join(page_root, ".src.json")
            try:
                with open(mark, encoding="utf-8") as f:
                    old = f.read()
            except (OSError, ValueError):
                old = ""
            if old != sig:
                shutil.rmtree(page_root, ignore_errors=True)
            os.makedirs(page_root, exist_ok=True)
            try:
                with open(mark, "w", encoding="utf-8") as f:
                    f.write(sig)
            except OSError:
                pass
            with pymupdf.open(pdf_path) as doc:
                n = len(doc)
            stem = os.path.splitext(os.path.basename(pdf_path))[0]
            for pno in range(n):
                # 页目录名是 1 基（worker 写 p{batch[0]+1}-…），预扫描必须同制，否则永远续不上
                for t in range(PAGE_TRIES):
                    got = _page_done(_page_dir(out_dir, pno + 1, t), stem)
                    if got:
                        results[pno] = got
                        break
            if results:
                _say(f"全文翻译 {pid}: 复用上次已译好的 {len(results)}/{n} 页，只译剩下的")
            if envs:
                cfg_copy = pinned_config(page_root, pid)
            j["pages"] = [len(results), n]
            _RUNNING[pid] = procs
            lock = threading.Lock()
            auth_error = []

            def worker(batch: list):
                try:
                    _worker(batch)
                except Exception as e:
                    _say(f"全文翻译 {pid}: 第 {batch[0] + 1} 页起的一批异常（{type(e).__name__}），保留原文")

            def _worker(batch: list):
                """batch：连续的 0 基页号。整批两次都没成时拆成单页各再试一遍——
                别让一页坏页连坐同批的没坏页。"""
                a, b = batch[0] + 1, batch[-1] + 1
                label = str(a) if a == b else f"{a}-{b}"
                got, tail, auth_out = None, [], []
                for t in range(PAGE_TRIES):
                    if auth_error or j.get("_abort"):
                        return
                    pdir_t = _page_dir(out_dir, a, t)
                    os.makedirs(pdir_t, exist_ok=True)
                    cfg = (pinned_config(pdir_t, pid, overlay=envs) if envs else "") or cfg_copy
                    got, tail = _run_page(pdf_path, label, pdir_t, service, extra,
                                          envs, cfg, procs, auth_out, engine)
                    if auth_out:
                        auth_error.append(auth_out[0])
                        return
                    if got:
                        if t > 0:
                            _say(f"全文翻译 {pid}: 第 {label} 页第二次尝试成功")
                        break
                if got:
                    # pdf2zh 对 --pages 的产物有两种形态：全文（只有选中的页被译了）或只含选中页。
                    # 记下每页在产物里的真实位置，组装时按它取，别猜。
                    try:
                        with pymupdf.open(got["mono"]) as m:
                            ml = len(m)
                    except Exception:
                        ml = 0
                    with lock:
                        for p in batch:
                            if ml == n:
                                at = p                       # 全文产物：0 基页号即位置
                            elif ml == len(batch):
                                at = p - (a - 1)             # 只含区间：按批内顺序排
                            else:
                                at = min(p, max(0, ml - 1))
                            results[p] = {"mono": got["mono"], "at": at}
                        j["pages"] = [len(results), n]
                    return
                if len(batch) > 1:
                    _say(f"全文翻译 {pid}: 第 {label} 页整批两次都没成，拆成单页再各试一遍")
                    for p in batch:
                        _worker([p])
                    return
                _say(f"全文翻译 {pid}: 第 {a} 页两次都没译成，这一页保留原文"
                     f"（{' / '.join(tail[-2:])}）")

            from concurrent.futures import ThreadPoolExecutor
            pending = [p for p in range(n) if p not in results]
            batches = [pending[i:i + BATCH_PAGES] for i in range(0, len(pending), BATCH_PAGES)]
            with ThreadPoolExecutor(max_workers=_page_workers(service)) as pool:
                list(pool.map(worker, batches))

            if auth_error:
                j.update(status="error", error=auth_error[0])
                say(f"全文翻译失败 {pid}：{auth_error[0]}")
                return
            if j.pop("_abort", None):
                j.update(status="none", error="")      # 用户主动取消，不算一次失败
                return

            # ---- 组装：盘上只落译文版（省盘：双语是它的两倍大，首次点开时由 原文+译文
            mono_path = os.path.join(out_dir, "mono.pdf")
            failed = []
            src = pymupdf.open(pdf_path)
            mono = pymupdf.open()
            try:
                for pno in range(n):
                    got = results.get(pno)
                    ok = False
                    if got:
                        try:
                            with pymupdf.open(got["mono"]) as m:
                                at = got.get("at")
                                at = min(pno, len(m) - 1) if at is None else max(0, min(int(at), len(m) - 1))
                                mono.insert_pdf(m, from_page=at, to_page=at)
                            ok = True
                        except Exception:
                            pass
                    if not ok:
                        failed.append(pno + 1)
                        mono.insert_pdf(src, from_page=pno, to_page=pno)
                if n and not results:
                    j.update(status="error",
                             error=f"所有页面都没译成（{service} 连不上或被限流）。"
                                   "换一个翻译服务（设置 → 全文翻译）再试。")
                    say(f"全文翻译失败 {pid}：全部页面失败")
                    return
                mono.save(mono_path, garbage=4, deflate=True)
                try:
                    os.remove(os.path.join(out_dir, "dual.pdf"))
                except OSError:
                    pass
            finally:
                src.close(); mono.close()

            done_note = (note or "") + (f"（第 {', '.join(map(str, failed))} 页没译成，保留原文）"
                                        if failed else "")
            j.update(status="done", dual="", mono=mono_path,
                     error="", pages=[n, n], note=done_note)
            _say(f"全文翻译完成 {pid}：{int(time.time() - j['started'])}s · {service}"
                 f" · {len(results)}/{n} 页" + (f" · 失败页 {failed}" if failed else ""))
        except Exception as e:
            j.update(status="error", error=f"{type(e).__name__}: {str(e)[-400:]}")
            say(f"全文翻译失败 {pid}：{type(e).__name__}: {str(e)[-300]}")
        finally:
            _RUNNING.pop(pid, None)
            if j["status"] == "done" or not results:
                shutil.rmtree(page_root, ignore_errors=True)
            else:
                _sweep_key_copies(page_root, pid)
            for proc in procs:
                try:
                    proc.kill()
                except Exception:
                    pass

    j.update(status="queued", error="", pages=[0, 0])   # 线程还没跑到的窗口期也让 cancel 有归属
    threading.Thread(target=run, daemon=True).start()
    return j

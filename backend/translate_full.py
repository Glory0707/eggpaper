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
   以前是 subprocess.run(capture_output=True)，等于全程黑箱——用户看不到任何东西。
   现在改成 Popen 逐行读：页进度报给前端，同一份也写进 app.log。
   （顺带：管道里的 tqdm 每个进度是一条独立行，不是 \r 刷屏，正好逐行解析。）

4. **产物必须按源文件名的词干找**。整个翻译件共用一个 out_dir，而 pdf2zh 按输入名
   命名产物（`abc.pdf` → `abc-dual.pdf`）。以前是 `sorted(os.listdir())[0]`，
   译第二篇时会把第一篇的双语 PDF 认成自己的——A 的译文挂到 B 上。
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
_PROBE = {}    # host -> (ok, why, at)  连通性预检的短期缓存
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_PROG = re.compile(r"(\d+)%\|[^|]*\|\s*(\d+)/(\d+)")
# "key 错了"这一类：pdf2zh 只会逐段刷日志、不会自己停，所以这里主动认
AUTH_FAILS = ("authentication fails", "invalid api key", "incorrect api key",
              "401 - ", "unauthorized", "invalid_api_key")

PROBE_TIMEOUT = 2.5      # 预检单次连接的上限：够连通的早连通了，不通的也别让用户等
PROBE_TTL = 600          # 一次预检结果管 10 分钟，别每次点按钮都白等 2.5 秒
STALL_SECS = 360         # 连续 6 分钟没有任何输出 = 卡死（正常情况下 tqdm 每几秒一行）
HARD_SECS = 2400         # 兜底上限 40 分钟

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


# ---------------- 进度 ----------------

def parse_progress(line: str):
    """从 tqdm 那行里抠出 (已译页, 总页)。抠不到返回 None。"""
    m = _PROG.search(_ANSI.sub("", line))
    if not m:
        return None
    done, total = int(m.group(2)), int(m.group(3))
    return (done, total) if total > 0 else None


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


def pinned_config(out_dir: str, pid: str) -> str:
    """把 pdf2zh 的配置"钉"在一份临时副本上，返回路径（拿不到就返回空 = 不传）。

    为什么：pdf2zh 会把从环境变量读到的 OPENAI_API_KEY / DEEPSEEK_API_KEY
    **写进它自己的 `~/.config/PDFMathTranslate/config.json`**（它的 GUI 靠那个文件）。
    用户在 eggpaper 里选了 openai/deepseek 服务，key 不该顺带被复制到另一个程序的配置里。
    传 `--config <副本>` 之后它只写副本——实测（假 key 跑的）：真配置的 mtime 与
    sha256 一个字节没动，副本里拿到了那个 key。

    副本里先原样放**真配置的内容**：这样除了"写哪儿"，行为与不传时完全一致
    （用户自己设过的字体路径之类不会丢）。读不出来就退化成不传，不为了隐私把功能弄坏。
    """
    dst = os.path.join(out_dir, f".pdf2zh-{pid}.json")
    try:
        if os.path.exists(PDF2ZH_CONFIG):
            with open(PDF2ZH_CONFIG, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {}
        if not isinstance(data, dict):
            return ""
        with open(dst, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        return dst
    except Exception:
        return ""


def sweep_configs(out_dir: str):
    """清掉上一次留下的 --config 副本（里面有 key，别让它躺着）。"""
    try:
        for fn in os.listdir(out_dir):
            if fn.startswith(".pdf2zh-") and fn.endswith(".json"):
                try:
                    os.remove(os.path.join(out_dir, fn))
                except OSError:
                    pass
    except OSError:
        pass


def job(pid: str) -> dict:
    return JOBS.setdefault(pid, {"status": "none", "error": "", "dual": "", "mono": "",
                                 "service": "", "note": "", "pages": [0, 0], "started": 0.0})


def start(pid: str, pdf_path: str, out_dir: str, service: str, extra: str = "",
          envs: dict = None, log=None, note: str = "") -> dict:
    j = job(pid)
    if j["status"] == "running":
        return j

    def run():
        j.update(status="running", error="", dual="", mono="", service=service,
                 note=note, pages=[0, 0], started=time.time())
        last = [time.time()]                 # 最后一行输出的时刻，给卡死判定用
        tail = collections.deque(maxlen=8)   # 最近几行非进度输出：失败时要能说清为什么
        _say = log or (lambda _m: None)
        proc = None
        cfg_copy = ""
        try:
            os.makedirs(out_dir, exist_ok=True)
            sweep_configs(out_dir)           # 上一次万一被硬杀，留下的含 key 副本先清掉
            cfg_copy = pinned_config(out_dir, pid) if envs else ""
            cmd = _cmd(pdf_path, out_dir, service, extra, cfg_copy)
            _say(f"整本翻译开始 {pid}：{service} · {' '.join(cmd)}")
            env = {**os.environ, **(envs or {})}
            flags, si = _no_window()
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding="utf-8", errors="replace",
                                    cwd=out_dir, env=env, creationflags=flags, startupinfo=si)

            def watch():
                # 卡死判定：tqdm 每几秒就来一行，长时间静默只可能是网络挂住或死锁
                while proc.poll() is None:
                    time.sleep(5)
                    if time.time() - last[0] > STALL_SECS:
                        _say(f"整本翻译 {pid}：静默 {STALL_SECS}s 无输出，判定卡死，已终止")
                        j["error"] = (f"pdf2zh 卡住了：{STALL_SECS // 60} 分钟没有任何输出"
                                      f"（多半是 {service} 服务连不通或者被限流）。"
                                      f"换一个翻译服务再试。")
                        try:
                            proc.kill()
                        except Exception:
                            pass
                        return
                    if time.time() - j["started"] > HARD_SECS:
                        _say(f"整本翻译 {pid}：超过 {HARD_SECS // 60} 分钟，已终止")
                        j["error"] = f"超过 {HARD_SECS // 60} 分钟还没译完，已终止。"
                        try:
                            proc.kill()
                        except Exception:
                            pass
                        return

            threading.Thread(target=watch, daemon=True).start()
            for raw in proc.stdout:
                last[0] = time.time()
                line = _ANSI.sub("", raw).rstrip()
                if not line.strip():
                    continue
                low = line.lower()
                if any(k in low for k in AUTH_FAILS):
                    # key 不对时 pdf2zh **不会中止**：它逐段报 401，一路把所有页都试完
                    # （实测：假 key 也能磨 5 分钟以上，输出一直在刷所以看门狗也不响）。
                    # 这种错一眼就认得，当场掐掉，别让用户等一场必然失败的翻译。
                    j["error"] = f"{service} 服务的 key 不对（pdf2zh 报鉴权失败）：{line.strip()[-160:]}"
                    _say(f"整本翻译 {pid}：鉴权失败，已终止 —— {line.strip()[:200]}")
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    break
                pg = parse_progress(line)
                if pg:
                    j["pages"] = [pg[0], pg[1]]
                elif "\r" not in line:
                    tail.append(line)
                    _say(f"pdf2zh: {line}")     # 进度行不写日志（太吵），别的都留个痕
            try:
                rc = proc.wait(timeout=30)   # 上面两处主动 kill 过，别在这儿二次挂住
            except subprocess.TimeoutExpired:
                proc.kill()
                rc = proc.wait()
            if j["error"]:
                j["status"] = "error"
                return
            if rc != 0:
                # 只说"退出码 1"等于没说：pdf2zh 的原因就在它自己的输出里
                # （服务名不认识、key 没配、模型不存在……），把它最后几行带出来
                why = " / ".join(t.strip() for t in list(tail)[-3:] if t.strip())
                raise RuntimeError(f"pdf2zh 退出码 {rc}" + (f"：{why}" if why else ""))

            stem = os.path.splitext(os.path.basename(pdf_path))[0]
            dual = os.path.join(out_dir, stem + "-dual.pdf")
            mono = os.path.join(out_dir, stem + "-mono.pdf")
            if not os.path.exists(dual):
                raise RuntimeError("没有生成双语 PDF，输出目录里只有：" +
                                   ", ".join(sorted(os.listdir(out_dir))[:6]))
            j.update(status="done", dual=dual, mono=mono if os.path.exists(mono) else "",
                     pages=[j["pages"][1] or 0, j["pages"][1] or 0])
            _say(f"整本翻译完成 {pid}：{int(time.time() - j['started'])}s · {service}")
        except FileNotFoundError:
            j.update(status="error", error="这一版安装包里没带 pdf2zh（整本翻译引擎）。"
                                           "单独装它：pip install pdf2zh，或改用「译文/双语」之外的方式读原文")
            _say(f"整本翻译失败 {pid}：没装 pdf2zh")
        except Exception as e:
            j.update(status="error", error=f"{type(e).__name__}: {str(e)[-400:]}")
            _say(f"整本翻译失败 {pid}：{type(e).__name__}: {str(e)[-300]}")
        finally:
            if cfg_copy:
                try:
                    os.remove(cfg_copy)      # 副本里有 key，用完就删，别留在盘上
                except OSError:
                    pass
            if proc is not None and proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass

    threading.Thread(target=run, daemon=True).start()
    return j

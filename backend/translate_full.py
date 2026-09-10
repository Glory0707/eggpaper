"""pdf2zh 整本翻译封装：subprocess 隔离，绝不让 AGPL 代码进入本项目。"""
import os
import shlex
import shutil
import subprocess
import sys
import threading

JOBS = {}  # paper_id -> {status, error, dual}


def _cmd(pdf_path, out_dir, service, extra):
    exe = shutil.which("pdf2zh") or os.path.join(os.path.dirname(sys.executable), "pdf2zh.exe")
    if os.path.exists(exe):
        base = [exe]
    else:                                    # 兜底：模块方式（新版 pdf2zh-next 支持）
        base = [sys.executable, "-m", "pdf2zh"]
    cmd = base + [pdf_path, "-o", out_dir, "--service", service]
    if extra:
        cmd += shlex.split(extra)
    return cmd


def job(pid: str) -> dict:
    return JOBS.setdefault(pid, {"status": "none", "error": "", "dual": ""})


def start(pid: str, pdf_path: str, out_dir: str, service: str, extra: str = ""):
    j = job(pid)
    if j["status"] == "running":
        return

    def run():
        j.update(status="running", error="")
        try:
            os.makedirs(out_dir, exist_ok=True)
            cmd = _cmd(pdf_path, out_dir, service, extra)
            if extra:
                cmd += shlex.split(extra)
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=2400,
                               encoding="utf-8", errors="replace", cwd=out_dir)
            duals = [f for f in os.listdir(out_dir) if f.endswith("dual.pdf")]
            if not duals:
                raise RuntimeError("未生成双语 PDF：" + (p.stderr or p.stdout or "")[-400:])
            j.update(status="done", dual=os.path.join(out_dir, sorted(duals)[0]))
        except FileNotFoundError:
            j.update(status="error", error="pdf2zh 未安装（pip install pdf2zh）")
        except Exception as e:
            j.update(status="error", error=f"{type(e).__name__}: {str(e)[-400:]}")

    threading.Thread(target=run, daemon=True).start()

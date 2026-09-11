"""一条命令构建安装包 + 生成更新源需要的 latest.json。

    python tools/build_installer.py                # 用 VERSION 里的版本号
    python tools/build_installer.py --notes "修了三个 bug"

它做五件事，任何一步失败都直接停（不要产出一个半成品让人去装）：

    1. 前端构建（npm run build → frontend/dist）
    2. 生成图标（installer/eggpaper.ico，几何取自界面上那枚印章）
    3. PyInstaller 冻结（installer/eggpaper.spec → build/pyi/eggpaper/）
    4. Inno Setup 打安装包（installer/eggpaper.iss → release/eggpaper-<版本>-setup.exe）
    5. 写 release/latest.json（版本、大小、sha256、更新说明）——**用户端的更新提示读的就是它**

构建环境要装两样东西（一次性）：

    uv venv .build-venv --python 3.11
    uv pip install --python .build-venv/Scripts/python.exe -r backend/requirements.txt pyinstaller pillow
    winget install --id JRSoftware.InnoSetup
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
BUILD_VENV = os.path.join(ROOT, ".build-venv", "Scripts", "python.exe")
PYI_DIR = os.path.join(ROOT, "build", "pyi")
RELEASE = os.path.join(ROOT, "release")


def step(msg):
    print(f"\n=== {msg} ===", flush=True)


def run(cmd, cwd=ROOT, shell=False):
    print("  $", " ".join(cmd) if isinstance(cmd, list) else cmd, flush=True)
    r = subprocess.run(cmd, cwd=cwd, shell=shell)
    if r.returncode:
        sys.exit(f"!!! 这一步失败了（退出码 {r.returncode}），后面不再继续")


def version() -> str:
    with open(os.path.join(ROOT, "VERSION"), encoding="utf-8") as f:
        return f.read().strip()


def find_iscc() -> str:
    for p in (r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
              r"C:\Program Files\Inno Setup 6\ISCC.exe",
              os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Inno Setup 6", "ISCC.exe")):
        if os.path.exists(p):
            return p
    got = shutil.which("ISCC") or shutil.which("iscc")
    if got:
        return got
    sys.exit("没找到 Inno Setup 的 ISCC.exe。装一个：winget install --id JRSoftware.InnoSetup")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--notes", default="", help="更新说明（显示在用户端的更新提示里）")
    ap.add_argument("--notes-file", default="", help="从文件读更新说明")
    ap.add_argument("--min-version", default="", help="低于这个版本必须先升级（协议不兼容时用）")
    ap.add_argument("--skip-frontend", action="store_true", help="跳过前端构建（改的只有后端时）")
    args = ap.parse_args()

    ver = version()
    py = BUILD_VENV if os.path.exists(BUILD_VENV) else sys.executable
    print(f"eggpaper {ver}  ←  构建环境 {py}")

    if not args.skip_frontend:
        step("1/5 构建前端")
        run(["npm", "run", "build"], cwd=os.path.join(ROOT, "frontend"), shell=(os.name == "nt"))

    step("2/5 生成图标")
    run([py, os.path.join(ROOT, "tools", "make_icon.py")])

    step("3/5 冻结后端与界面（PyInstaller，onedir）")
    if os.path.isdir(PYI_DIR):
        shutil.rmtree(PYI_DIR, ignore_errors=True)
    run([py, "-m", "PyInstaller", "--noconfirm", "--distpath", PYI_DIR,
         "--workpath", os.path.join(ROOT, "build", "work"),
         os.path.join(ROOT, "installer", "eggpaper.spec")])

    step("4/5 打安装包（Inno Setup）")
    os.makedirs(RELEASE, exist_ok=True)
    run([find_iscc(), f"/DMyVersion={ver}", os.path.join(ROOT, "installer", "eggpaper.iss")])

    step("5/5 写 latest.json（用户端更新提示读这一份）")
    setup = os.path.join(RELEASE, f"eggpaper-{ver}-setup.exe")
    if not os.path.exists(setup):
        sys.exit(f"没找到安装包：{setup}")
    h = hashlib.sha256()
    with open(setup, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    notes = args.notes
    if args.notes_file and os.path.exists(args.notes_file):
        with open(args.notes_file, encoding="utf-8") as f:
            notes = f.read().strip()
    info = {
        "version": ver,
        "url": os.path.basename(setup),          # 相对文件名：跟 latest.json 放同一个目录就行
        "size": os.path.getsize(setup),
        "sha256": h.hexdigest(),
        "pub_date": time.strftime("%Y-%m-%d"),
        "notes": notes or "（这一版没写说明）",
    }
    if args.min_version:
        info["min_version"] = args.min_version
    with open(os.path.join(RELEASE, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)

    size_mb = os.path.getsize(setup) / 1048576
    print(f"\n好了：{setup}  （{size_mb:.1f} MB）")
    print(f"      {os.path.join(RELEASE, 'latest.json')}")
    print("\n发布（二选一）：")
    print("  · 本机/局域网：python tools/serve_update.py   → 把地址填进各客户端的设置·更新源")
    print("  · 任意静态托管：把 release/ 整个目录传上去，设置里填那个地址")


if __name__ == "__main__":
    main()

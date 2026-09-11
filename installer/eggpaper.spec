# PyInstaller 规格文件：onedir（不是 onefile）。
#
# 为什么 onedir：onefile 每次启动都要把几十 MB 解压到临时目录（首启 5~10 秒），
# 而且"正在运行的程序"对自己那个 exe 加着锁，安装器替换不了——升级会失败。
# onedir 启动快、升级时直接换文件。用户看到的是一个 eggpaper 文件夹 + 一个 exe，
# 安装器会把它放进 Program Files / 用户目录，用户不需要关心内部结构。
#
# 打包命令由 tools/build_installer.py 驱动（不要手敲 pyinstaller，参数容易漏）。

import os

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

datas = [
    (os.path.join(ROOT, "frontend", "dist"), "frontend/dist"),   # 前端界面
    (os.path.join(ROOT, "VERSION"), "."),                        # 版本号（更新检查要用）
]
# 后端按包名引用的东西，PyInstaller 静态分析看不到，手动点名
hiddenimports = [
    # 托盘（pystray 的后端是按平台动态选的，静态分析看不到）
    "pystray._win32", "PIL.Image", "PIL.ImageDraw",
    "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto", "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on", "uvicorn.lifespan.off",
    "appinfo", "citation", "config", "db", "glossary_seed", "llm", "pdfparse", "update",
]

a = Analysis(
    [os.path.join(ROOT, "desktop.py")],
    pathex=[os.path.join(ROOT, "backend")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # 瘦身：这些一个都用不到。**注意别把 PIL 列进来**——托盘图标要用它，
    # excludes 的优先级高于 hiddenimports，列进去就是"明明装了却说找不到"。
    excludes=["numpy", "scipy", "pandas", "matplotlib", "tkinter", "pytest",
              "IPython", "notebook"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="eggpaper",
    debug=False,
    strip=False,
    upx=False,
    console=False,                 # 无控制台窗口：日志写在 %LOCALAPPDATA%\eggpaper\logs
    icon=os.path.join(ROOT, "installer", "eggpaper.ico"),
)

coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False, upx=False,
    name="eggpaper",
)

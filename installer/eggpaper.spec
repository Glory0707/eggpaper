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
    (os.path.join(ROOT, "installer", "eggpaper.ico"), "."),      # 多尺寸图标：窗口/托盘取它
    (os.path.join(ROOT, "backend", "guide.html"), "."),          # 使用指南（设置里可打开）
]
hiddenimports = [
    # 托盘（pystray 的后端是按平台动态选的，静态分析看不到）
    "pystray._win32", "PIL.Image", "PIL.ImageDraw",
    "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto", "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on", "uvicorn.lifespan.off",
    "appinfo", "citation", "config", "db", "llm", "mark", "pdfparse",
    "update", "window",
    # 整本翻译这条链：translate_full 由 main 静态导入、engine_install 由 translate_full
    # 在函数里导入——静态分析通常扫得到，但这是"缺了整本翻译就废"的命门，显式列出。
    "translate_full", "engine_install",
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
              "IPython", "notebook",
              # 独立窗口走浏览器应用模式。pywebview 试过并撤掉：它在打包环境里
              # import 就卡住（pythonnet 加载 .NET 时握着 GIL，兜底计时都跑不到）。
              "webview", "pythonnet", "clr_loader",
              # Pillow 11 自带的 AVIF 解码插件（含 7.5 MB 的原生 _avif.pyd）：
              # 应用只画 PNG/ICO（托盘、印章），永远碰不到 AVIF。Pillow 的插件
              # 发现走 pkgutil 枚举 PIL 包下实际存在的模块，缺了就跳过，不会报错。
              "PIL.AvifImagePlugin"],
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

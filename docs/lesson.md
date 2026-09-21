# 踩坑与经验教训（lesson）

> 持久知识：标准工作流、红线、付出过时间的坑、用户定的口径。只记"下次还会遇到"的；版本流水账不记，能力清单以 [README](../README.md) 为准。

## 一、构建 · 发布 · 重装验证

- 发布一条龙：改 `VERSION` → `/d/hermes/uv-python/cpython-3.11.14-windows-x86_64-none/python.exe tools/build_installer.py --notes "…"`（前端构建→图标→冻结→Inno→latest.json 一条命令）。
- **Gitee 更新源发布三步**（0.1.41 实测闭环；0.1.42 起全程 API 化）：① 推 `release/latest.json` 到仓库 `update/latest.json`（浅克隆 + 令牌推送，**推完删克隆**——令牌明文留在 .git/config 里）；② 建 release 走 API：`POST /api/v5/repos/zhouao1207/eggpaper/releases`（tag_name=v<版本>、target_commitish=master）→ `PATCH …/releases/{id}` 补中文说明（POST 时 curl 命令行会把中文搅成 GBK 乱码，body 一律用 python requests/httpx 发）→ `POST …/releases/{id}/attach_files` multipart 传 exe（201 即成，92MB 约两分钟）；③ 闭环验证：匿名 curl raw 清单 → 匿名下载附件比 sha256 → `GET /api/update/check?force=true` 看 has_update。
- **Gitee 的 raw 地址现在会 302 到 raw.giteeusercontent.com**（0.1.42 实测变了）：curl 验证要加 `-L`；客户端 update.py 跟随重定向所以无感。
- **Gitee 网页编辑器把整串路径当文件名会建出嵌套目录**（"raw/master/update/latest.json" 变三层文件夹）；且路径里含分支名（master）时 Gitee 的 tree/blob/delete 路由 404/405、?path= 只救得回 blob——网页 UI 删不掉，只能 git push 修。2FA 账号 HTTPS 推送必须用私人令牌当密码。
- **`.build-venv` 不自动装依赖**：改了 `backend/requirements.txt` 必须手动 `uv pip install --python .build-venv/Scripts/python.exe -r backend/requirements.txt pyinstaller pillow`。OCR 引擎就这么漏过一次：包 34MB、模型没进去，装到别人机器才炸。
- 本机重装验证：先 `taskkill //IM eggpaper.exe //F` → `powershell Start-Process <setup.exe> '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /TASKS=desktopicon'` ——**不要 `-Wait`**（运行中的 exe 锁着文件，安装器收尾不退，永远等不完）→ 轮询 tasklist 等退出 → 启动。
- 装完核两条：`GET /api/version` 的 version/packaged；首页引用的 `index-*.js` 哈希与 `frontend/dist` 一致（确认装的是新前端，不是浏览器缓存）。

## 一·二、全文翻译引擎（pdf2zh_next 2.x，0.1.42 实测换装）

- **发行形态只有 with-assets win64 zip 靠谱**（~620MB，官方 sha256 钉在 engine_install）：普通包首译要在线下版面模型/字体，上游竞速含 huggingface——国内超时就死在预热里，整个翻译起不来（实测两次）。with-assets 内置资产，启动器首启自动 restore 到 `~/.cache/babeldoc`（HOME 已重定向到数据目录），装完离线可用。
- **装完把包里的 `offline_assets_*.zip` 改名 `.installed` 收起**：2.x 启动器每次进程启动见到它就把全部资产重新哈希一遍（~220MB），每批翻译白等 10-20 秒。engine_install._warmup 校验通过后做这件事。
- **2.x 产物命名带中缀**：`<stem>.no_watermark.zh.mono.pdf`，不是 1.9 的 `<stem>-mono.pdf`——找产物按词干前缀 glob（`_product_mono`），别钉死整名。
- **单进程冷启动 ~11s（收起 offline zip 后）**，1.9 约 3s：批大小 BATCH_PAGES 4→8 才摊得薄。`--pages` 配 `--only-include-translated-page` 让产物只含所选页；服务=每家一个旗标（`--bing`/`--openai`，默认引擎是 SiliconFlowFree 必须显式传）；key 只走 `PDF2ZH_*` 环境变量（`--openai-api-key` → `PDF2ZH_OPENAI_API_KEY`）。
- **术语 CSV 三条红线**：表头 `source,target`；裸 utf-8（BOM 会把表头变成 `\ufeffsource`）；不写 tgt_lng 列（写了要跟 `--lang-out` 归一化对上，`zh-CN`≠`zh` 会被整列过滤）。只在 LLM 服务注入（bing/google 不吃 prompt，物理上没法锁术语）。译文 PDF 的文本层有兼容表意字符（量≠量），验证术语命中要 NFKC 归一。
- **自动安装判断看 find_installed（磁盘真实态），不是 engine_path**：E2E 的 EGGPAPER_ENGINE_OFF 只让"寻找"失明；拿 engine_path 判断会让装好的引擎被当成没装、装完再起一轮下载（E2E 实测撞过）。
- **预扫描续译必须按批映射**：批目录名是批首页（p9 = 第 9-16 页的批）。老代码逐页探测，把 p9 里的 8 页产物当成"第 9 页"，组装 at=None 取到产物最后一页——重启续译把第 16 页插到第 9 页的位置（实测"复用 3/24"暴露）。修法见 translate_full.start 的预扫描段。
- 2.x 首译质量比 1.9 好一截（BabelDOC 版面模型 + 跨页上下文），bing 24 页实测 163-202s，3 批并行无锁；鉴权失败（401）会被 AUTH_FAILS 当场认出，rich 的 80 列换行没挤断关键词。

## 二、测试

- 回归在 `_qa/`（gitignored）：Playwright + `serve_temp.py`，`EGGPAPER_DATA`/`PORT` 环境变量起临时实例（8469~8482）。测试 python 用 hermes venv（httpx/playwright/fitz/rapidocr 齐）。
- 打包版黄金验证：`EGGPAPER_DATA` 指临时目录后直接跑 `D:\eggpaper\eggpaper.exe`。打包版不认 PORT 环境变量（起在 PORTS[0]=8430），但数据目录重定向生效——正好验"别人装完第一次打开"的完整链路。
- 端口格局：8430=打包实例（用户真库）兼源码默认端口（别同时开）；8431/8432 是 desktop.py PORTS 的后备。
- 断言经验：异步的轮询着等；别抓第一条 toast（可能是上一个动作发的）；文件选择框用 `expect_file_chooser`；用 python 直接 POST 造的数据 UI 不认（store 不知道），走 UI 动作或显式刷新；判据写"到达终态"，别写"必须看到进度条"（本地下载半秒完，进度条一闪而过）。

## 三、红线

- 用户 DeepSeek key 在 `%LOCALAPPDATA%\eggpaper\data\config.yaml`：不复制、不外泄、不进任何输出。
- 8430 是用户真库：不写测试数据，冒烟只读。
- 不入库：API key、用户文献/批注/术语数据、`docs/作者手记.md`、`_qa/`。
- 兼容底线：改提示词只动措辞；role/kind 枚举值、JSON schema、数据库字段不动——老缓存、前端映射、眉批色带全挂在上面。跨文件改键（组件、i18n.js、后端给模型的文案）要三处同步。
- 外部引擎一律 subprocess 不 import（AGPL 边界；pdf2zh 因此不随包分发，引擎另装）。

## 四、口径（用户定的，动手前先想）

- mac 仓不定期跟随（2026-09-22 定）：Windows 是唯一主仓，`D:\tools\eggpaper-macos` 不再逐版本同步——攒一批再跟，或用户点名时才跟；mac 自己的 VERSION 独立走，不追 Windows 版本号。
- 界面减法：不放解释性小字；一条信息只住一个房间，别处只给指针（细则见 [visual.md](visual.md)）。判据：删掉这行字，用户会损失什么？
- 文档同口径：简洁、删陈旧、不留版本流水账；持久经验进本文件。
- 提交信息：中文、`type(scope): 说人话`，把"为什么"写进去，结尾带版本号。
- 先测量再动手："糊/慢/卡"先变成数字（对比度脚本、分阶段耗时、墨段数）再谈改。眉批提速是范本：墙钟时间 ≈ 波数 × 单块时间，单块是模型的地板（带思考 ~36s/块），能压的只有波数——实测数字就写在 `CHUNK_WORKERS` 常量旁，别凭感觉调。
- 删功能连根删：测量代码、设置项、样式、导出小节、i18n 键一起删，再用审计脚本数引用，不靠印象。
- 闭环习惯：改码 → 回归 E2E → 构建 → 静默重装 → 8430 验证 → 提交，缺一步不算完。

## 五、图标（最容易白干的地方）

四条链路一个真源 `backend/mark.py` 的几何：桌面/exe=多尺寸 ICO、浏览器标签=favicon、独立窗口=运行时注入、托盘=pystray 现画。生成物（`installer/eggpaper.ico`、`frontend/public/icons/*`）构建时会重新生成，**不要手改**。

标准动作：改 mark.py → `python tools/check_icon.py`（闸门：白缝/三段墨/圆角透明，非零退出就别往下走）→ `python tools/make_icon.py` → **VERSION 加一**（图标 URL 的 `?v=` 由构建从 VERSION 替换，版本不变=浏览器继续吃旧图）→ 打包。

坑（每条付出过时间）：

- 独立窗口任务栏：Chromium 按**物理像素**取 favicon 档再放大，源头小怎么画都软；`--app-icon` 开关无效。正解=进程外 `WM_SETICON` 注入按窗口 DPI 现画的 HICON，**Win11 任务栏读 ICON_SMALL2**；favicon 加载后会把图标重设回去，要反复压 30 秒 + 常驻守护（见 window.py）。别用 LoadImage 从 ICO 选档（挑近档再缩，多一次重采样），按精确像素现画。
- 缩小先预乘 alpha 再 BOX 缩，否则圆角处"透明黑"平均进白边，一圈脏边。
- ICO 规范：≤64 必须 DIB 负载、只有 256 允许 PNG（全 PNG 被 PyInstaller 静默跳过）；PIL 不能进 PyInstaller excludes；改图标要清 PyInstaller 的 build 缓存。
- 内联 SVG favicon 是坑：Chromium 栅格化成小位图再放大，怎么改都糊，别加回来。
- 每档 DPI 的精确物理尺寸都要有原图（150% 缩放下任务栏要 30/36/48，缺哪档就有人替你缩放）。
- 三层缓存：浏览器 Favicons SQLite（`?v=` 破）、Windows iconcache（重启 explorer）、Chromium HTTP 缓存。清 Favicons 按完整 origin 前缀删，别 `LIKE '%8430%'` 子串匹配（误伤过无关网站）。
- `console=False` 的打包版没有 stderr：一切图标/窗口/线程异常必须写日志文件，否则症状只有"图标不对/没反应"，原因永远查不出。uvicorn 的彩色日志 dictConfig 在无控制台环境直接炸——打包版传 `log_config=None`（双击打不开的真因）。
- 托盘按系统真实尺寸原生画（先 SetProcessDpiAwareness 再问 SM_CXSMICON）；单尺寸 ICO 交给 pystray 会经历两次重采样。
- 判断"系统取了哪一档/槽位多大"：指纹法（每档染不同纯色，看目标位置显示什么颜色）、标定法（注入满幅色块量物理像素），比读源码快。
- 就绪判定别回连自己（有的机器对自己的 127.0.0.1 会卡 SYN_SENT）：进程内事件 + bind 检查 + instance.json 的 pid，三处都不碰网络。

## 六、提示词与 LLM

- **示例会被照抄**：schema 里想要什么形状就放什么形状的示例——compare 的 ref 想要整数就写 `"ref": 3`；写"段落号或 null"它就回字符串，锚点全废。
- 惰性检测判断"任务活着"不可靠：后台线程（OCR+析读）必须自己登记进 `_live_jobs`、finally 注销，否则被误判"上次中断"遭清零。
- 数据库里的 `running` 可能是僵尸（进程崩了状态还在）：启动时清零 + 运行中用 `_live_jobs` 识别，POST 不再被旧 running 挡住。
- 极小而合法的输入最容易被漏：整页只有一行文字时 `statistics.median([])` 炸掉整篇导入。
- 推理型模型 max_tokens 要留思考额度（骨架 16k、眉批 12k 量级）；等外部响应的地方必须有"在动"的东西（首字 30–60s，空气泡和坏了长得一样）；提问面板常驻（`v-show`），切页签不能 abort 掉正在生成的流。
- 演示模式文案双语成对（`_demo_txt`），改一处两处都改；演示数据要具体，不要摆拍腔。

## 七、杂

- pywebview+pythonnet 在打包环境 import 即卡死（握着 GIL，兜底计时都跑不到）：独立窗口保持 Edge/Chrome 应用模式，别再试原生窗口（window.py 注释有记录）。
- PyInstaller 用 onedir 不用 onefile：onefile 每次启动解压几十 MB，且"正在跑的程序锁着自己的 exe"，安装器替换不了它，升级必然失败。
- Inno 向导脚本里不能手写默认安装路径（吃掉 `/DIR=` 还覆盖记住的上次位置 → 装出第二份并存，正在跑的那份永远升不上去）；向导只留一个语言，中文用 `[Messages]` 直接覆盖。
- 打包版与源码版数据目录**故意分开**（源码折腾坏不影响用户那份）；升级只换程序，数据默认在安装目录旁 `data\`（老安装仍在 `%LOCALAPPDATA%\eggpaper\data`，原地继续），卸载不删。
- 同一处文案记得"页面标题"和"窗口标题"两头都看（Edge 应用模式的窗口标题跟页面走）。
- 热路径优化先建**对拍基准**再动手：md.js 流式增量解析写了前缀缓存，500 组随机追加/截断对拍抓出 68 处不等价，整体回退——rAF 合帧已覆盖实际需求。
- 批量改文案/词典的脚本要按原始行尾读写（`newline=""`）：i18n.js 在磁盘上是 CRLF，`line+"\n"` 替换永远静默失配。

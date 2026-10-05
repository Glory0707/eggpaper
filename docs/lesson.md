# 踩坑与经验教训（lesson）

> 持久知识：标准工作流、红线、付出过时间的坑、用户定的口径。只记"下次还会遇到"的；版本流水账不记，能力清单以 [README](../README.md) 为准。

## 一、构建 · 发布 · 重装验证

- 发布一条龙：改 `VERSION` → `/d/hermes/uv-python/cpython-3.11.14-windows-x86_64-none/python.exe tools/build_installer.py --notes "…"`（前端构建→图标→冻结→Inno→latest.json 一条命令）。
- **Gitee 更新源发布三步**（实测闭环，全程 API 化）：① 推 `release/latest.json` 到仓库 `update/latest.json`（浅克隆 + 令牌推送，**推完删克隆**——令牌明文留在 .git/config 里）；② 建 release 走 API：`POST /api/v5/repos/zhouao1207/eggpaper/releases`（tag_name=v<版本>、target_commitish=master）→ `PATCH …/releases/{id}` 补中文说明（POST 时 curl 命令行会把中文搅成 GBK 乱码，body 一律用 python requests/httpx 发）→ `POST …/releases/{id}/attach_files` multipart 传 exe（201 即成，92MB 约两分钟）；③ 闭环验证：匿名 curl raw 清单 → 匿名下载附件比 sha256 → `GET /api/update/check?force=true` 看 has_update。
- **Gitee 的 raw 地址现在会 302 到 raw.giteeusercontent.com**：curl 验证要加 `-L`；客户端 update.py 跟随重定向所以无感。
- **云端版本记录已清场**（0.1.0 基线重开时）：Gitee 的 11 个旧 release（v0.1.41~49/55/56）、同名 tag 与 `update/latest.json` 全删——查更新 404 按「没有更新」处理，降级提示不再出现；发版时按上面三步重建。tag 删除没有 API（DELETE /tags 404），走浅克隆 `git push origin :refs/tags/…`。
- 发布用的令牌在 Windows 凭据管理器：`git credential fill`（protocol=https、host=gitee.com）程序内取用，不落盘不回显。
- **git 配置了 127.0.0.1 代理，代理没开时连不上 gitee**：克隆/推送加 `-c http.proxy= -c https.proxy=` 直连（httpx 不读 git 代理配置，走 API 的调用不受影响）。
- **Gitee 网页编辑器把整串路径当文件名会建出嵌套目录**（"raw/master/update/latest.json" 变三层文件夹）；且路径里含分支名（master）时 Gitee 的 tree/blob/delete 路由 404/405、?path= 只救得回 blob——网页 UI 删不掉，只能 git push 修。2FA 账号 HTTPS 推送必须用私人令牌当密码。
- **`.build-venv` 不自动装依赖**：改了 `backend/requirements.txt` 必须手动 `uv pip install --python .build-venv/Scripts/python.exe -r backend/requirements.txt pyinstaller pillow`。OCR 引擎就这么漏过一次：包 34MB、模型没进去，装到别人机器才炸。
- 本机重装验证：先 `taskkill //IM eggpaper.exe //F` → `powershell Start-Process <setup.exe> '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /TASKS=desktopicon'` ——**不要 `-Wait`**（运行中的 exe 锁着文件，安装器收尾不退，永远等不完）→ 轮询 tasklist 等退出 → 启动。
- 装完核两条：`GET /api/version` 的 version/packaged；首页引用的 `index-*.js` 哈希与 `frontend/dist` 一致（确认装的是新前端，不是浏览器缓存）。
- **安装器自拉起偶发「服务报告已启动但端口空」自退**（实测一次，重开即好，根因未定位）：READY 是 startup 事件（绑定后置位），按理探测不会空——失败模式安全：进程自退、日志留「重开一次即可」，重开正常。静默安装后轮询 /api/version 为空就再 start 一次。

## 一·二、全文翻译引擎（pdf2zh_next 2.x）

- **发行形态只有 with-assets win64 zip 靠谱**（~620MB，官方 sha256 钉在 engine_install）：普通包首译要在线下版面模型/字体，上游竞速含 huggingface——国内超时就死在预热里，整个翻译起不来（实测两次）。with-assets 内置资产，启动器首启自动 restore 到 `~/.cache/babeldoc`（HOME 已重定向到数据目录），装完离线可用。
- **装完把包里的 `offline_assets_*.zip` 改名 `.installed` 收起**：2.x 启动器每次进程启动见到它就把全部资产重新哈希一遍（~220MB），每批翻译白等 10-20 秒。engine_install._warmup 校验通过后做这件事。
- **2.x 产物命名带中缀**：`<stem>.no_watermark.zh.mono.pdf`，不是 1.9 的 `<stem>-mono.pdf`——找产物按词干前缀 glob（`_product_mono`），别钉死整名。
- **单进程冷启动 ~11s（收起 offline zip 后）**，1.9 约 3s：批大小 BATCH_PAGES 4→8 才摊得薄。`--pages` 配 `--only-include-translated-page` 让产物只含所选页；服务=每家一个旗标（`--bing`/`--openai`，默认引擎是 SiliconFlowFree 必须显式传）；key 只走 `PDF2ZH_*` 环境变量（`--openai-api-key` → `PDF2ZH_OPENAI_API_KEY`）。
- **术语 CSV 三条红线**：表头 `source,target`；裸 utf-8（BOM 会把表头变成 `\ufeffsource`）；不写 tgt_lng 列（写了要跟 `--lang-out` 归一化对上，`zh-CN`≠`zh` 会被整列过滤）。只在 LLM 服务注入（bing/google 不吃 prompt，物理上没法锁术语）。译文 PDF 的文本层有兼容表意字符（量≠量），验证术语命中要 NFKC 归一。
- **自动安装判断看 find_installed（磁盘真实态），不是 engine_path**：E2E 的 EGGPAPER_ENGINE_OFF 只让"寻找"失明；拿 engine_path 判断会让装好的引擎被当成没装、装完再起一轮下载（E2E 实测撞过）。
- **预扫描续译必须按批映射**：批目录名是批首页（p9 = 第 9-16 页的批）。老代码逐页探测，把 p9 里的 8 页产物当成"第 9 页"，组装 at=None 取到产物最后一页——重启续译把第 16 页插到第 9 页的位置（实测"复用 3/24"暴露）。修法见 translate_full.start 的预扫描段。
- 2.x 首译质量比 1.9 好一截（BabelDOC 版面模型 + 跨页上下文），bing 24 页实测 161-202s，3 批并行无锁；鉴权失败（401）会被 AUTH_FAILS 当场认出，rich 的 80 列换行没挤断关键词。
- **调优先测量**：`--pool-max-workers 4`（批内并行）实测 69s→63s（~9%），要成倍放大 bing 请求并发，反爬风险不值——不采用。真正的大头是引擎下载：Range 分段并行（4 连接）实测 3.9x，镜像按单连接限速是常态；ghfast 支持 206，gh-proxy 只回 200（自动退单流）。
- **「重新全文翻译」必须 fresh**：force 路径不清 .pages 时，预扫描复用全部旧页，按钮等于没按（已修）。两篇同时翻译会叠出 6~8 个引擎进程，全库共享的 ENGINE_PROCS_CAP=4 信号量兜底。

## 二、测试

- 回归在 `_qa/`（gitignored）：Playwright + `serve_temp.py`，`EGGPAPER_DATA`/`PORT` 环境变量起临时实例（8469~8495）。测试 python 用 hermes venv（httpx/playwright/fitz/rapidocr 齐）。
- **测试实例的引擎与缓存一个共享一个不共享**：引擎装在 `dirname(data_dir)/engines`（`_qa/engines`，全实例共用，别删）；babeldoc 资产缓存在各数据目录的 `home/.cache`（每实例 ~337MB，只增不减）——`_tmp_*` 目录定期清，只留手工夹具。
- 打包版黄金验证：`EGGPAPER_DATA` 指临时目录后直接跑 `D:\eggpaper\eggpaper.exe`。打包版不认 PORT 环境变量（起在 PORTS[0]=8430），但数据目录重定向生效——正好验"别人装完第一次打开"的完整链路。
- 端口格局：8430=打包实例（用户真库）兼源码默认端口（别同时开）；8431/8432 是 desktop.py PORTS 的后备。
- 断言经验：异步的轮询着等；别抓第一条 toast（可能是上一个动作发的）；文件选择框用 `expect_file_chooser`；用 python 直接 POST 造的数据 UI 不认（store 不知道），走 UI 动作或显式刷新；判据写"到达终态"，别写"必须看到进度条"（本地下载半秒完，进度条一闪而过）。
- **推理模型会把 max_tokens 整个烧在思考上**：deepseek-flash 做多篇综述合成时先出几千块 reasoning_content，6000 的默认预算耗尽 → finish=length、正文零字（实测"模型这次没返回内容"就是它）。多篇对照/长 synthesis 类调用把预算提到 10000+；普通单篇问答 6000 够用。
- **测试串行跑共用数据目录的两处句柄竞态**：上一个 serve 刚 terminate，SQLite/目录句柄未必放干净——`make_fixtures` 清库会静默失败（后面的测试跑在别人的数据上），备份恢复的落地 rename 会静默失败（重启后没换库）。修法已进 shots_readme：清库重试 + `stop_srv` 等进程死透。
- **内联探针脚本崩了会漏掉 serve_temp 僵尸**（heredoc 里 Popen 之后没有 try/finally 的那种），僵尸占着端口，下一个用同端口的测试 bind 失败静默死掉、全跑到僵尸的旧数据上——症状是"昨天过的断言今天稳挂"（test_novelty 的「老缓存」断言就是这么挂的，清掉 8490 僵尸立刻全绿）。防线：serve_temp 启动前探端口、响了就退出；测试骨架 Popen 前同样预检速败。
- **改 SQL 查询的列清单，必须同步检查所有取列处**：find_same_title 把 `SELECT id, title` 精简成 `SELECT id`，循环里 `r["title"]` 炸 IndexError——上传库里已有标题的第二篇必 500（测试员轮实测抓住）。
- audit 脚本别把截图放在连点压测之后：竞态恢复期纸面瞬态空白，会被误判成渲染回归（完整重跑零复现才定性）。压测归压测，截图归稳定态。
- **删"废件"前先 grep 测试引用**：`_qa/new_paper.pdf` 看着像产物，实际是三个测试共用的事实夹具（生成方一次没跑，消费方全炸）。压测 100 发里偶发 1 发客户端 WinError 10053、单跑零复现＝负载下的连接 flake，别当产品 bug 查。
- **PDF 夹具正文一段 ≥14 词、按词折行**：解析的成段门槛是 14 词（中文 40 字），短段/单行页整组被吞（论文看着正常、compare 却报"扫描件"）；70 字硬切会把单词拦腰截断，正文子串匹配（互引/划线）全废——用 textwrap 按 70 折。
- **httpx 流式响应在小 body 上遇早关闭，一个分块都不 yield 就抛错**（数据全在缓冲里）：断流/续传测试要用 MB 级 body 分段写，300KB 的夹具测不出"断在 90%"。

## 三、红线

- 用户 DeepSeek key 在 `%LOCALAPPDATA%\eggpaper\data\config.yaml`：不复制、不外泄、不进任何输出。
- 8430 是用户真库：不写测试数据，冒烟只读。
- 不入库：API key、用户文献/批注/术语数据、`docs/作者手记.md`、`_qa/`。
- 兼容底线：改提示词只动措辞；role/kind 枚举值、JSON schema、数据库字段不动——老缓存、前端映射、眉批色带全挂在上面。跨文件改键（组件、i18n.js、后端给模型的文案）要三处同步。
- 外部引擎一律 subprocess 不 import（AGPL 边界；pdf2zh 因此不随包分发，引擎另装）。

## 四、口径（用户定的，动手前先想）

- 代码签名在正式版本发布前不做（2026-09-22 定）："未知发布者"提示由使用指南兜着，等有真实用户量和收入再买证书。
- mac 仓不定期跟随（2026-09-22 定）：Windows 是唯一主仓，`D:\tools\eggpaper-macos` 不再逐版本同步——攒一批再跟，或用户点名时才跟；mac 自己的 VERSION 独立走，不追 Windows 版本号。
- **版本号只听用户指令**（2026-09-29 定）：历史版本记录已清场，`VERSION`=0.1.0 重开；此后版本号变更必须用户明示，构建/安装一律沿用 `VERSION` 现值，不自动递增；Gitee feed 发布同样等指令。
- 界面减法：不放解释性小字；一条信息只住一个房间，别处只给指针（细则见 [visual.md](visual.md)）。判据：删掉这行字，用户会损失什么？
- 文档同口径：简洁、删陈旧、不留版本流水账；持久经验进本文件。
- 提交信息：中文、`type(scope): 说人话`，把"为什么"写进去，结尾带版本号。
- **新功能动手前先盘已有功能**：「综述矩阵」与既有「数据对比」重叠 1:1（同为多篇对照表；对比还更成熟——维度可选、逐篇抽取抗幻觉、格子带锚点），发版当天即撤，改成提问面板引用后一键唤起对比；同批的「组会月报」也撤了——原材料就是日历导出清单，LLM 那步只是强行解释一遍（数据薄时硬凑主线，数据厚时复述一眼卡），想让它解释什么，提问面板引用几篇随时能问，不需要常驻功能。AI 直接产出文章的三宗罪（幻觉/上下文/AI 味）靠"整理而非代笔"规避：结构化输出 + 材料进上下文 + ¶ 溯源；纯生成类交付物（related work 草稿）没被真实用户要过就不做。
- 先测量再动手："糊/慢/卡"先变成数字（对比度脚本、分阶段耗时、墨段数）再谈改。眉批提速是范本：墙钟时间 ≈ 波数 × 单块时间，单块是模型的地板（带思考 ~36s/块），能压的只有波数——实测数字就写在 `CHUNK_WORKERS` 常量旁，别凭感觉调。
- 删功能连根删：测量代码、设置项、样式、导出小节、i18n 键一起删，再用审计脚本数引用，不靠印象。
- 闭环习惯：改码 → 回归 E2E → 构建 → 静默重装 → 8430 验证 → 提交，缺一步不算完。

## 五、图标（最容易白干的地方）

四条链路一个真源 `backend/mark.py` 的几何：桌面/exe=多尺寸 ICO、浏览器标签=favicon、独立窗口=运行时注入、托盘=pystray 现画。生成物（`installer/eggpaper.ico`、`frontend/public/icons/*`）构建时会重新生成，**不要手改**。

标准动作：改 mark.py → `python tools/check_icon.py`（闸门：白缝/三段墨/圆角透明，非零退出就别往下走）→ `python tools/make_icon.py` → **VERSION 加一**（版本号变更须用户指令——图标一改就得同批升版本，`?v=` 由构建从 VERSION 替换，版本不变=浏览器继续吃旧图）→ 打包。

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
- **AI 上下文缓存吃的是「从第 0 个 token 起逐字节相同」的前缀**：同篇论文的整文任务共用 SHARED_SYSTEM + paper_doc（每段截到句界、全文 90k 保头也保尾），任务规则放全文之后——命中输入按约 1/50 计费。任何随任务/调用变化的字段（图表注、术语表、问题、历史）进了前缀就前功尽弃。
- **错误话术映射必须把异常类名并进匹配串**：httpx ConnectError 的消息体是「[WinError 10061] 目标计算机积极拒绝」，"connect" 只在类名里——只匹配消息体的话，连接类错误就带着原文漏给用户（实测）。
- **请求参数的整数要严格收**：`int(1.5)` 静默截成 1，对"译第几段"是悄悄译错段——非整数（含 bool、超 2^53 的浮点）一律 400。

## 七、杂

- pywebview+pythonnet 在打包环境 import 即卡死（握着 GIL，兜底计时都跑不到）：独立窗口保持 Edge/Chrome 应用模式，别再试原生窗口（window.py 注释有记录）。
- PyInstaller 用 onedir 不用 onefile：onefile 每次启动解压几十 MB，且"正在跑的程序锁着自己的 exe"，安装器替换不了它，升级必然失败。
- Inno 向导脚本里不能手写默认安装路径（吃掉 `/DIR=` 还覆盖记住的上次位置 → 装出第二份并存，正在跑的那份永远升不上去）；向导只留一个语言，中文用 `[Messages]` 直接覆盖。
- 打包版与源码版数据目录**故意分开**（源码折腾坏不影响用户那份）；升级只换程序，数据默认在安装目录旁 `data\`（老安装仍在 `%LOCALAPPDATA%\eggpaper\data`，原地继续），卸载不删。
- 同一处文案记得"页面标题"和"窗口标题"两头都看（Edge 应用模式的窗口标题跟页面走）。
- 热路径优化先建**对拍基准**再动手：md.js 流式增量解析写了前缀缓存，500 组随机追加/截断对拍抓出 68 处不等价，整体回退——rAF 合帧已覆盖实际需求。
- 批量改文案/词典的脚本要按原始行尾读写（`newline=""`）：i18n.js 在磁盘上是 CRLF，`line+"\n"` 替换永远静默失配。
- **按属性清理 HTML 标签会把同行其他属性一起删掉**：sed 清 `:title="''"` 把同一标签的 `@click` 一并吃掉——按钮还在、点了没反应（P0，脚本回归没拦住，真实浏览器走查才抓到）。属性级批量替换后，受影响控件必须在浏览器里真实点一遍。
- **overflow:hidden 的折叠容器可被程序化滚动**（scroll-into-view、拖选到边界都会触发）：内容被卷走后无法滚回，答案叠在标题上。折叠改用 `overflow:clip`（语义就是"裁剪且永不滚动"）；排查靠 `elementFromPoint` 实证命中元素。
- **i18n 静态清扫会误杀动态拼接的 key**（`t(cond ? 'A' : 'B')` 扫不到）：动态拼接改成完整字面量的分支写法，让清扫工具可扫。
- **渲染管线不能拿"过渡动画结束"当挂载前提**：纸面与破壳层曾是 v-if/v-else 互斥（out-in 过渡），画布元素要等过渡结束才挂载——后台标签页合成帧暂停、过渡永不结束，纸面永久空白，Playwright 可见页测试盖不住这条（Computer Use 后台页实测抓到）。挂载与遮罩分离：遮罩只负责盖，元素先存在。
- 中文提交信息用 `git commit -F <文件>` 传递。终端里 git 输出显示乱码先 `xxd` 验字节再下结论：仓库里存的是对的 UTF-8、只是终端拿 GBK 解码显示的情况占多数（本会话两次"乱码"惊吓都是显示假象）。
- **全局 CSS 选择器对新行有暗坑**：`.modal .f-row input{width:100%}` 会把新行里的 checkbox 也拉成一行宽，把「每天自动」的文字顶出弹窗右缘（视觉验收抓到，getBoundingClientRect 一量就现形）。新行里放 checkbox 显式 `width:auto`；截图目检别省。
- **互引立场的定位是"提示"不是"结论"**（2026-10-06 调研定）：scite 自己的论文（Nicholson 2021）测得机器分类 ~78.5%，独立评估（Bakker 2023, Hypothesis 35(2)）结论是"总体准确率低"——所以互引的原句永远原样展示（可自查），立场只是可选判定、默认回退 mention，别把界面做成"scite 说"。引用关系的召回靠正文全标题匹配（指向"实质讨论这篇"的高价值句）；参考文献表不入库（解析时丢弃），扫不了是已知边界。
- **WebDAV 探活用 PROPFIND 别用 OPTIONS**（Joplin WebDavApi.ts 源码级结论）：OPTIONS 的 DAV 头各家服务器不可靠；只看状态码不解析 body（各家响应格式不一）。坚果云免费档 1GB 上传/月、500MB 单文件、600 请求/30 分钟——自动备份必须"内容未变不传"（哈希比对，rclone/Zotero 同款），轻包口径是对的。
- **LLM 术语注入只做命中过滤**（Lokalise/BabelDOC 同款实践）：全量塞表是噪音；eggpaper 的 glossary_global_hits 子串命中 + 30 条封顶 + 本篇优先即此。缓存键注意：改动术语表会改变前缀，服务端缓存自然失效，无需自己做指纹。

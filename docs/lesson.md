# 踩坑与经验教训（lesson）

> 持久知识：标准工作流、红线、付出过时间的坑、用户定的口径。只记"下次还会遇到"的；版本流水账不记，能力清单以 [README](../README.md) 为准。

## 一、构建 · 发布 · 重装验证

- 发布一条龙（VERSION 变更须用户指令，见 §五）：改 `VERSION` → `/d/hermes/uv-python/cpython-3.11.14-windows-x86_64-none/python.exe tools/build_installer.py --notes "…"`（前端构建→图标→冻结→Inno→latest.json 一条命令）。
- **Gitee 更新源发布三步**（实测闭环，全程 API 化）：① 推 `release/latest.json` 到仓库 `update/latest.json`（浅克隆 + 令牌推送，**推完删克隆**——令牌明文留在 .git/config 里）；② 建 release 走 API：`POST /api/v5/repos/zhouao1207/eggpaper/releases`（tag_name=v<版本>、target_commitish=master）→ `PATCH …/releases/{id}` 补中文说明（POST 时 curl 命令行会把中文搅成 GBK 乱码，body 一律用 python requests/httpx 发）→ `POST …/releases/{id}/attach_files` multipart 传 exe（201 即成，92MB 约两分钟）；③ 闭环验证：匿名 curl raw 清单 → 匿名下载附件比 sha256 → `GET /api/update/check?force=true` 看 has_update。
- **Gitee 的 raw 地址现在会 302 到 raw.giteeusercontent.com**：curl 验证要加 `-L`；客户端 update.py 跟随重定向所以无感。
- **云端版本记录已清场**（0.1.0 基线重开时）：Gitee 的 11 个旧 release（v0.1.41~49/55/56）、同名 tag 与 `update/latest.json` 全删——查更新 404 按「没有更新」处理，降级提示不再出现；发版时按上面三步重建。tag 删除没有 API（DELETE /tags 404），走浅克隆 `git push origin :refs/tags/…`。
- 发布用的令牌在 Windows 凭据管理器：`git credential fill`（protocol=https、host=gitee.com）程序内取用，不落盘不回显。
- **git 配置了 127.0.0.1 代理，代理没开时连不上 gitee**：克隆/推送加 `-c http.proxy= -c https.proxy=` 直连（httpx 不读 git 代理配置，走 API 的调用不受影响）。
- **Gitee 网页编辑器把整串路径当文件名会建出嵌套目录**（"raw/master/update/latest.json" 变三层文件夹）；且路径里含分支名（master）时 Gitee 的 tree/blob/delete 路由 404/405、?path= 只救得回 blob——网页 UI 删不掉，只能 git push 修。2FA 账号 HTTPS 推送必须用私人令牌当密码。
- **`.build-venv` 不自动装依赖**：改了 `backend/requirements.txt` 必须手动 `uv pip install --python .build-venv/Scripts/python.exe -r backend/requirements.txt pyinstaller pillow`。OCR 引擎就这么漏过一次：包 34MB、模型没进去，装到别人机器才炸。
- 本机重装验证：先 `taskkill //IM eggpaper.exe //F` → **Git Bash 里直接前台跑安装器**：`MSYS_NO_PATHCONV=1 "<setup.exe>" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART "/DIR=D:\eggpaper"`——bash 原生等它退出还带回显码，最省事；别绕 `cmd //c start ""`（引号解析误开交互 cmd，安装器根本没跑）也别用 powershell `Start-Process`（**不要 `-Wait`**：运行中的 exe 锁着文件，安装器收尾不退，永远等不完）。装完启动，轮询 /api/version。
- 装完核两条：`GET /api/version` 的 version/packaged；首页引用的 `index-*.js` 哈希与 `frontend/dist` 一致（确认装的是新前端，不是浏览器缓存）。
- **安装器自拉起偶发「服务报告已启动但端口空」自退**（实测一次，重开即好，根因未定位）：READY 是 startup 事件（绑定后置位），按理探测不会空——失败模式安全：进程自退、日志留「重开一次即可」，重开正常。静默安装后轮询 /api/version 为空就再 start 一次。
- **双实例端口竞争的输家会变成"僵尸注册表"**（2026-10-08 实测一次 full chain）：重装时安装器自拉起 + 手动拉起同时跑，输家在 uvicorn bind 失败前就过了就绪闸——写了 instance.json、挂了托盘；用户在它的托盘点退出，POST 替端口主人退了役，自己走 graceful 路径永远等下去 → 无服务僵尸攥着 instance.json，之后每次启动都被「已有一个在跑」挡在门外（页面 Failed to fetch、PDF 空白、删除失败全是它的相）。修了三道闸（desktop.py）：就绪闸补验服务线程存活（`except BaseException`——bind 失败走 SystemExit，`except Exception` 接不住）；托盘退出加 8 秒看门狗 os._exit 兜底；启动对登记实例验尸（pid 活着但端口空闲 = 僵尸，接管启动）。**uvicorn 的 lifespan startup 事件在 create_server（bind）之前触发**——"就绪"和"bind 成功"之间天然有个输家窗口，任何"事件即就绪"的判定都要补一道服务线程存活验证。
- **测试 venv 的 python.exe 是启动器（launcher）**：`Popen(venv_python)` 的 pid 是 launcher 的，代码里 `os.getpid()` 是它拉起的真实解释器——两者对不上号；`terminate()` 只杀 launcher，真实解释器成孤儿继续占端口（test_naming 为此空转一小时）。这条坑每个新测试都要继承：测试要按登记 pid `taskkill /T` 杀树、按"登记被改写"而不是"Popen pid"下断言；骨架的 stop 必须"terminate 后按端口补刀"（Get-NetTCPConnection 拿 owner pid，taskkill /T /F），serve_temp 的端口预检只是速败不是保险；失败运行先按命令行 sweep 孤儿再跑，不然端口/数据目录残留把下一轮搅成 environment-flake。

## 二、全文翻译引擎（pdf2zh_next 2.x）

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
- **杀引擎进程一律连树杀（taskkill /T）**（2026-10-12 测试员轮）：pdf2zh 2.x 的 multiprocessing 子进程命令行里**不带本篇目录**——`kill_orphan_procs` 的目录追杀认不出它们，只 `proc.kill()` 会留下孙进程继续跑。两条实害：① 孙进程攥着 `.pages` 句柄，收尾 rmtree 静默失败（垃圾留盘）；② 它还继承着 stdout 管道写端，`for raw in proc.stdout` 永远等不到 EOF → `_run_page` 不返回、pool.map 不退出，任务永远"翻译中"，此后每次 start() 都原样退回这条死任务，这篇论文到重启为止再也翻不了。超时/鉴权失败/资源失败/收尾清场四条路全走 `_kill_tree`（单测用父+孙真进程验父子双死）。
- **任务状态要有自愈出口**（同轮）：状态（DB/job 字典）里的 running 可能比线程活得久（线程卡死、异常路径漏收尾）。`_WORKERS[pid] = 线程` 登记 + start() 里"状态说在跑、但登记簿里没有活线程 → 判死重开"，把"这篇再也翻不了"从只能重启变成自愈。登记动作必须放在 **start() 里、th.start() 之前**（同步置 queued 的窗口里第二下点击才看得到"有人在跑"）；收尾清理全部走完才撤登记。
- **页级缓存指纹必须覆盖"所有会改变产物的输入"**（同轮）：`.src.json` 只认源文件 mtime+size 时，换服务（bing→LLM）、改术语表、改 options、换引擎后再点翻译，旧页会被静默复用——一篇译文两种姿势混着，术语锁定只在新译的页上生效。指纹改成 `{m,s,svc,extra,eng,术语摘要}`（page_sig 抽成函数好单测），任一变化 → .pages 整目录作废重译。另记：老指纹不带 svc，升级后第一次翻译会把旧缓存当失配重译一遍——这正是要的。
- **升级换装的 .old 让路 + 校验失败清断点**（同轮）：① Windows 的 rename 撞已存在目标直接 FileExistsError——上次 .old 没删干净（exe 被攥）时每次升级都在这一步硬失败，还因为包已删要重下 600MB。修法：让路用带时间戳的 `pdf2zh.old.<ts>`，启动清扫按 `pdf2zh.old*` 前缀认（正式目录有引擎才删、没有则恢复一个回来）。② 分段断点（.partN）是按长度续传的，内容换了会永远在旧字节上接长——哈希不过必须把包与断点**一起**清掉，且换下一个源再试（`_verify_zip`），不然"已删除可重试"的提示会引人点十次都出不去。
- **换装引擎要拦"正在翻译"**（同轮）：`_unpack` 会把引擎目录从跑着的 pdf2zh 脚下 rename 走（模块懒加载失败、批神秘报错、exe 被占留下删不掉的 .old）。两个安装端点都过 `_no_install_while_translating`（口径同"改数据目录要先停下载"）。另：安装取消旗要在 `_from_zip` 的 finally 里清——残留的旗让下一次网络安装在第一检查点自我中止，用户看到的是"点了安装、毫秒级弹回 idle"。

## 三、测试

- 回归在 `_qa/`（gitignored）：Playwright + `serve_temp.py`，`EGGPAPER_DATA`/`PORT` 环境变量起临时实例（8465~8498）。**测试 python 用项目自带的 `.build-venv`**（uv 管，requirements 全套；playwright 不在 requirements 里，缺就 `uv pip install --python .build-venv/Scripts/python.exe playwright`；浏览器二进制在 `D:\hermes\ms-playwright`，全局环境变量 PLAYWRIGHT_BROWSERS_PATH 指着，换 venv 不用重下。原先记的"hermes venv"已被清空，别再找）。
- **全量回归前关掉所有常驻浏览器**：Computer Use 轮的 MCP 浏览器开着 8492 页面进回归，两个 UI 测试（new_paper_flow/multiselect）双双超时挂掉、单跑全绿——常驻实例与测试的 msedge 抢资源/抢 GPU 的量级足够把 4 秒的等待拖过 30 秒超时。MCP 会话结束必须 browser_close，回归跑之前也扫一眼 tasklist 里的 msedge 残留。
- **测试实例的引擎目录 = `dirname(数据目录)/engines`**：所有 `_qa/_tmp_*` 沙箱都指到 `_qa/engines`——那是 engine-install 测试自己下假引擎的沙箱（跑前跑后都会清，别当共享资产）；要真引擎的测试一律多候选探测（`D:\eggpaper\engines` 优先，随数据目录搬过家、别写死；见 shots_readme.ENGINE_CANDS / real_paths）。babeldoc 资产缓存在各数据目录的 `home/.cache`（每实例 ~337MB，只增不减）——`_tmp_*` 目录定期清，只留手工夹具。
- 打包版黄金验证：`EGGPAPER_DATA` 指临时目录后直接跑 `D:\eggpaper\eggpaper.exe`。打包版不认 PORT 环境变量（起在 PORTS[0]=8430），但数据目录重定向生效——正好验"别人装完第一次打开"的完整链路。
- 端口格局：8430=打包实例（用户真库）兼源码默认端口（别同时开）；8431/8432 是 desktop.py PORTS 的后备。
- 断言经验：异步的轮询着等；别抓第一条 toast（可能是上一个动作发的）；文件选择框用 `expect_file_chooser`；用 python 直接 POST 造的数据 UI 不认（store 不知道），走 UI 动作或显式刷新；判据写"到达终态"，别写"必须看到进度条"（本地下载半秒完，进度条一闪而过）。
- **测试串行跑共用数据目录的两处句柄竞态**：上一个 serve 刚 terminate，SQLite/目录句柄未必放干净——`make_fixtures` 清库会静默失败（后面的测试跑在别人的数据上），备份恢复的落地 rename 会静默失败（重启后没换库）。修法已进 shots_readme：清库重试 + `stop_srv` 等进程死透。
- **内联探针脚本崩了会漏掉 serve_temp 僵尸**（heredoc 里 Popen 之后没有 try/finally 的那种），僵尸占着端口，下一个用同端口的测试 bind 失败静默死掉、全跑到僵尸的旧数据上——症状是"昨天过的断言今天稳挂"（test_novelty 的「老缓存」断言就是这么挂的，清掉 8490 僵尸立刻全绿）。防线：serve_temp 启动前探端口、响了就退出；测试骨架 Popen 前同样预检速败。
- **改 SQL 查询的列清单，必须同步检查所有取列处**：find_same_title 把 `SELECT id, title` 精简成 `SELECT id`，循环里 `r["title"]` 炸 IndexError——上传库里已有标题的第二篇必 500（测试员轮实测抓住）。
- audit 脚本别把截图放在连点压测之后：竞态恢复期纸面瞬态空白，会被误判成渲染回归（完整重跑零复现才定性）。压测归压测，截图归稳定态。
- **删"废件"前先 grep 测试引用**：`_qa/new_paper.pdf` 看着像产物，实际是三个测试共用的事实夹具（生成方一次没跑，消费方全炸）。压测 100 发里偶发 1 发客户端 WinError 10053、单跑零复现＝负载下的连接 flake，别当产品 bug 查。
- **PDF 夹具正文一段 ≥14 词、按词折行**：解析的成段门槛是 14 词（中文 40 字），短段/单行页整组被吞（论文看着正常、compare 却报"扫描件"）；70 字硬切会把单词拦腰截断，正文子串匹配（互引/划线）全废——用 textwrap 按 70 折。
- **同内容文件上传会被去重返回已有篇**——对抗测试里三次"建新篇"实际都拿到了主测篇（duplicate 返回旧 id），后续"删新篇"删的全是主测篇，一路 404 查了半天。测试里每篇 fixture 必须内容不同；产品侧这个去重本身是对的（"库里已有这篇，直接打开"）。
- **测试客户端会先规范化 URL 再发**：`/api/papers/../papers` 这类原始路径穿越在 httpx/requests 里根本到不了服务器，测穿越必须用 `%2F` 编码形态；反过来 Starlette 的 {pid} 参数会把 %2F 解码回斜杠，服务端要靠"pid 必须先过 DB"这道闸兜住（本项目兜住了）。
- **Playwright evaluate 会 await 返回值**：`() => Promise.reject(...)` 直接把 rejection 抛回测试脚本，压根到不了页面的 unhandledrejection——要触发全局兜底 toast 得用 `setTimeout(() => Promise.reject(...), 0)` 包一层。
- **httpx 流式响应在小 body 上遇早关闭，一个分块都不 yield 就抛错**（数据全在缓冲里）：断流/续传测试要用 MB 级 body 分段写，300KB 的夹具测不出"断在 90%"。
- **锁的测试必须走端点路径**：per-key 锁加在端点上，测试直接调内层生成函数会绕过锁，测出"双击两次调用"的假阴性（互引 classify 实测：内层 calls=2、走端点 calls=1）。FastAPI 的同步 def 可直接当函数调。
- **i18n 检查器的文件清单就是漏词口子的上限**（2026-10-12 测试员轮）：它只扫写死在表里的 13 个文件，于是 CompareOverlay 的 CSV 表头 `'文献'`、ZoteroDialog 的字面量 `"(无标题)"`、CalendarPanel 的尾缀拼接（`t('待读 {n} 篇 · ')` 多了个 " · " 永远匹配不上词典）、CiteExport 的 `'作者 + 年'`、RightRail 的 `t('条')/t('步')`、LeftRail 的裸 `处` 整类逃过。清单改成 `os.listdir(components)` 动态收全量（含 find.js 等模块），漏词立刻从 7 处现形到 0。
- **"删掉了"的断言别用路径存在性——用内容指纹**（同轮）：supersede 会继承旧篇的作者/年份，新版的显示名可能**恰好就是旧版的名字**，于是新版目录合法地改名占用同一路径（"同题旧版让位、新篇升回干净名"是设计）。测试里 `os.path.exists(old_dir)` 于是恒真、断言形同虚设（反过来也差点误判成"没删掉"）。口径：删除类断言对 papers/ 下所有 PDF 算 sha256，验"旧内容不在、新内容在"。修 bug 前先确认自己的断言在测什么，别让测试替代码背锅。
- **进度类断言必须用"后台紧循环采样"，别用 sleep 轮询**（2026-10-12）：`while: get(); time.sleep(0.5)` 在负载下会被单次慢查询拖成"几十秒才采一次"，一次全量回归里 test_translate_llm_progress 就这么假红过（中间态 [2,5]/[4,5] 明明持续了半分钟，采样只踩到 (0,0)(0,5)(5,5)）。改成后台线程不 sleep 地采（查询本身 ~10ms），主线程只等收工——同样的用例立刻稳定采到 2/5、3/5、4/5。
- **收尾工具不许反过来把测试弄死**（同轮）：同一条用例的收尾用 `Get-NetTCPConnection` 补刀杀端口，这个 cmdlet 在负载下跑过 30 秒，subprocess 超时一炸就在"全部断言已过"之后把整个测试判红。换成 `netstat -ano` 解析（秒级返回）+ 任何一步失败都安静收场。凡是"按端口找进程"的健康检查，netstat 比 cmdlet 快一个量级。

## 四、红线

- 用户 DeepSeek key 在用户数据目录的 `config.yaml`（本机 `D:\eggpaper\data`；老安装 `%LOCALAPPDATA%\eggpaper\data`）：不复制、不外泄、不进任何输出。
- 8430 是用户真库：不写测试数据，冒烟只读。
- 不入库：API key、用户文献/批注/术语数据、`docs/作者手记.md`、`_qa/`。
- 兼容底线：改提示词只动措辞；role/kind 枚举值、JSON schema、数据库字段不动——老缓存、前端映射全挂在上面。跨文件改键（组件、i18n.js、后端给模型的文案）要三处同步。
- 外部引擎一律 subprocess 不 import（AGPL 边界；pdf2zh 因此不随包分发，引擎另装）。

## 五、口径（用户定的，动手前先想）

- 代码签名在正式版本发布前不做（2026-09-22 定）："未知发布者"提示由使用指南兜着，等有真实用户量和收入再买证书。
- mac 仓不定期跟随（2026-09-22 定）：Windows 是唯一主仓，`D:\tools\eggpaper-macos` 不再逐版本同步——攒一批再跟，或用户点名时才跟；mac 自己的 VERSION 独立走，不追 Windows 版本号。
- **版本号只听用户指令**（2026-09-29 定）：历史版本记录已清场，`VERSION`=0.1.0 重开；此后版本号变更必须用户明示，构建/安装一律沿用 `VERSION` 现值，不自动递增；Gitee feed 发布同样等指令。
- 界面减法：不放解释性小字；一条信息只住一个房间，别处只给指针（细则见 [visual.md](visual.md)）。判据：删掉这行字，用户会损失什么？
- 文档同口径：简洁、删陈旧、不留版本流水账；持久经验进本文件。文档对账时每条承诺都当断言核——不一致既可能是文档陈旧，也可能是代码缺口（README 承诺导出七问，代码只导了五问，就是这轮对出来的）。
- 提交信息：中文、`type(scope): 说人话`，把"为什么"写进去，结尾带版本号。
- **新功能动手前先盘已有功能**：「综述矩阵」与既有「数据对比」重叠 1:1（同为多篇对照表；对比还更成熟——维度可选、逐篇抽取抗幻觉、格子带锚点），发版当天即撤，改成提问面板引用后一键唤起对比；同批的「组会月报」也撤了——原材料就是日历导出清单，LLM 那步只是强行解释一遍（数据薄时硬凑主线，数据厚时复述一眼卡），想让它解释什么，提问面板引用几篇随时能问，不需要常驻功能。AI 直接产出文章的三宗罪（幻觉/上下文/AI 味）靠"整理而非代笔"规避：结构化输出 + 材料进上下文 + ¶ 溯源；纯生成类交付物（related work 草稿）没被真实用户要过就不做。
- **AI 眉批退场（2026-10-11，用户拍板）**：判决式行间批注与正文抢注意力，还压缩阅读宽度——owner 自己都不打开不看，质量再好（预埋靶 4/5）也救不了"没人看"。业界（Moonlight/Elicit/ReadPaper/SciSpace）清一色收敛在摘要+对话，没有一家做行间批判；批判性阅读的需求是真的，出口换成七问⑤「哪里可疑、还有什么没解决？」——模型现场生成冲突/可疑/含糊三类、带 ¶n 依据，原眉批验证过的刀法提示词搬进它的 prompt。删功能时的工程口径：AI 产出与用户资产同表同渲染时，只删生成链（起停/进度/取消/互锁/档位/i18n），用户的钉子（lookup/region/note）与 DB 字段一根不动；存量 AI 行不下发即视为消失。
- **删功能要删到"渲染口径"那一层，截图也是功能面**（2026-10-11 复扫 AI 眉批残留）：只删生成链不够——页边渲染还挂着整套档位解析口（bandOf/kindColor/KIND_COLOR/BAND_OF_KIND）、i18n 的九类标签、三档笔触与筛选 chip 的 CSS（.b-good/.b-warn/.b-noise/.band-chip）、README 的「批注三种笔触」条目，以及**首屏截图**（reader/chat 两张 9 月的旧图还在展示已删功能的右栏面板与三档筛选）。收口做法：用户资产只剩一种墨色笔触（样式收进 CSS，模板不再按 kind 取色）、i18n/文档/截图同批清，i18n 死键用 `_qa/i18n_prune.py` 复验。截图管线口径：`shots_readme.py` 的沙箱 config 要带 `pdf2zh.path`（引擎随数据目录 C→D 迁过，写死会过期——多候选探测）；提问截图种一条拟真问答直写沙箱 sqlite（README 图不值得烧用户余额），citations 写侧照旧给 `'[]'`；**读侧也必须容 NULL**——qa_history 曾直接 `json.loads(citations)`，老库里 NULL 行一读就 500（本轮实测复现并修，回归加了 NULL 用例）。通例：删功能连根删——测量代码、设置项、样式、导出小节、i18n 键、文档与截图一起删，再用审计脚本数引用，不靠印象。
- **测试的"真库"路径要跟数据目录一起搬**（2026-10-11）：写死 `%LOCALAPPDATA%\eggpaper\data` + 找不到就 `return` 的组合，让 test_api_smoke 整场 0s「全绿」（一个断言没跑），数据搬家后同类脚本静默降级成空转。修法：`_qa/real_paths.py` 多候选探测（`D:\eggpaper\data` 优先、旧安装路径兜底），`run_regression` 把每个测试输出里的「跳过/SKIP」行打出来——全绿也要看得见跳了什么。
- 先测量再动手："糊/慢/卡"先变成数字（对比度脚本、分阶段耗时、墨段数）再谈改。长任务分批提速是范本（当年在行间批注上实测）：墙钟时间 ≈ 波数 × 单块时间，单块是模型的地板（带思考时实测 ~36s/块，调用处现已关思考、地板大降），能压的只有波数——实测数字就写在 `CHUNK_WORKERS` 常量旁，别凭感觉调。
- 闭环习惯：改码 → 回归 E2E → 构建 → 静默重装 → 8430 验证 → 提交，缺一步不算完。
- **加新守卫前先查既有测试对同一行为断言了什么**：对抗轮给 para_idx 加"非负"钳位、给删除加 404，全量回归当场被 test_edges 打脸——`para_idx=-1` 是前端"划词在段外"的合法约定，删除不存在的 mid 断言的是幂等 200。正确修法是精化而不是一刀切：只拒 < -1；"mid 不存在"幂等放行、"mid 存在但不属于这篇"才 403。守卫的粒度要贴着调用方的真实语义走。
- **"两处行为不一致"不一定是缺陷**：同屏后面板不收（多选是连续工作流，接着对比/归类要面板）与导入后收面板（第一篇已自动打开，面板挡视线）是两个工作流各自的合理语义——把它"统一"掉的改动被回归当场打回（multiselect 第二次点 .lib-multi 依赖面板还开着）。改行为前先找依赖它的测试/工作流，找不到才真是漏网。

## 六、图标（最容易白干的地方）

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

## 七、提示词与 LLM

- **示例会被照抄**：schema 里想要什么形状就放什么形状的示例——compare 的 ref 想要整数就写 `"ref": 3`；写"段落号或 null"它就回字符串，锚点全废。
- 惰性检测判断"任务活着"不可靠：后台线程（OCR+析读）必须自己登记进 `_live_jobs`、finally 注销，否则被误判"上次中断"遭清零。
- 数据库里的 `running` 可能是僵尸（进程崩了状态还在）：启动时清零 + 运行中用 `_live_jobs` 识别，POST 不再被旧 running 挡住。
- 极小而合法的输入最容易被漏：整页只有一行文字时 `statistics.median([])` 炸掉整篇导入。
- 推理型模型 max_tokens 要留思考额度（骨架 16k 量级）：deepseek-flash 做多篇综述合成时先出几千块 reasoning_content，6000 的默认预算整个烧在思考上 → finish=length、正文零字（实测"模型这次没返回内容"就是它）——多篇对照/长 synthesis 类把预算提到 10000+，普通单篇问答 6000 够用。
- **批量结构化任务一律关思考**（`no_think=True` → `thinking: disabled`，端点不认识会忽略、无害）：思考按输出全价计费，实测骨架输出 3000 里 2400 是思考、批量短任务七成 token 在思考。判断口径：抄写/抽取/翻译/分类/改写/短问这类任务关了质量差别不大；开放式问答与读图问答才真吃推理深度。2026-10-10 全量盘点 17 个调用点关了 15（只留问答与视觉两处）；次日用户拍板七问三处、推荐问题一处开回——生成刁钻问题/方向要的正是推理深度，与抄写抽取不是一类活。终态：13 关，问答/视觉/七问/推荐问题开。**关后用真端点 A/B 验过（最短假论文、预埋冲突/无对照/hype/hedge/padding 五类靶子）**：骨架 11 段角色标注开/关完全一致，一眼卡关思考照样抓到摘要与正文的数字冲突，另一批量任务开/关各抓到 4/5 个靶子、质量打平（各有 1 条对方没有的真发现）——代价差 9 倍墙钟（4.3s vs 39.7s/块）与 ~10 倍输出 token。单发验证划词/图注/术语/引用/推荐问题/七问全数正常。另：deepseek-flash 的 usage 上报 completion 不含 reasoning（思考另列），且偶发口径混乱——判断思考开没开以墙钟为准，别信账面。
- **翻译引擎的思考只能靠本机中转关**（2026-10-10）：引擎通用 openai 通道没有 thinking 入口、DeepSeek 专属通道写死只认 `deepseek-v4-` 前缀——deepseek-flash 两条路都堵。解法：`_pdf2zh_env` 把引擎 base_url 指到本进程 `/llm-relay`（一次性令牌叩门，真 key 不出进程），中转注入 `thinking:disabled` 后持真 key 转发，429/5xx 原样透传保住引擎的 tenacity 退避。真端点实测（同篇 2 页）：输出 39879→2706——**翻译输出里 93% 是思考 token**，这正是"翻译输出远大于输入"的真身；¥0.085→¥0.011，174s→119s。关思考的质量也验过：同篇开/关各译一遍逐段并排读（14 段），信息完整度、数字、术语一致性全部等价，差异只在同义选择（刊名译不译、"外推/推断"、um 规不规范成 μm），无一处丢译错译——翻译是忠实话，思考不改变忠实度。中转顺带把引擎请求的 token 账记进 app.log（scene=翻译引擎），翻译烧多少第一次可见。**坑：serve_temp 起测试服务必须走 `main.serve()` 而非直接 `uvicorn.run`**——RELAY_PORT 在 serve() 里登记，绕过它测试路径会静默退回直连老路，测了个寂寞；生产与测试必须同一条起服务的路。
- 等外部响应的地方必须有"在动"的东西（首字 30–60s，空气泡和坏了长得一样）；提问面板常驻（`v-show`），切页签不能 abort 掉正在生成的流。**"在动"的信号必须放在主视线上，藏在悬浮提示里等于没有**（实测：翻译按钮的活性信号只有 title 提示，用户盯着「翻译中 0/4」三分半就按了停止——离译完只差 1 分钟；修法=按钮文本带秒级时钟 + 小文档批次拆两半让页数中途真的动）。
- **进度颗粒度=批时，批大小由「第一拍进度多久出现」决定，不由冷启动开销决定**：用户的耐心实测 ~3 分半（两次都在那时按停止）。openai 通道 ~66s/页（真库日志 4 页 264s），8 页一批 = 20 页论文 9 分钟无进度——"小文档拆两半"只救了 ≤8 页的文档。LLM 通道 2 页一批（每 ~2 分半跳一格），免费通道 bing ~5s/页维持 8 页。慢通道开跑时还要用 toast 把预期说破，正在译哪几页给悬浮提示留着即可（主视线放常驻文案用户反嫌吵，2026-10-09 实测后撤掉）。
- **带端口的端点要拆开 host:port 探**：预检把端口焊死 443，本地网关（127.0.0.1:8080 这类）会被误判"连不通"然后静默跌落 bing——用户设置明明填对了，译稿却是没术语锁定的免费通道产物，两头都查不到原因。
- **pdf2zh 2.x 的 LLM 通道默认 qps=4、翻译线程池=qps，每页几十段请求排成 4 路纵队**：真端点对照实测 --qps 12 墙钟 ~1.35x（215s→159s，两轮复现），再往上收益趋平（瓶颈转向端点每请求延迟）；429 有 tenacity 指数退避兜着，加并发不损质量。官方给 SiliconFlowFree 强制 qps=20，说明这就是认可的开闸方式。
- **注入自家词表时必须 `--no-auto-extract-glossary`（2.9.0）**：`get_glossaries_for_translation` 在"自动抽取开启且抽到了词"时**只用它抽的那份**——`--glossaries` 注入的词表被无视，术语锁定整个落空。关掉后墙钟无害（对照 162s vs 159s），顺带省每篇 ~76k input tokens（抽取几乎等于再译一遍的输入量）。读引擎源码 + 真端点对照双确认，缺一不可。
- **验 LLM 通道不需要真 key**：本地 http.server 20 行冒充 OpenAI 端点（stream/非 stream 都答），pdf2zh 全链路照跑——实测 BabelDOC 译 5 页发了 100 次请求（页内段落级并发，非流式）。
- **真端点对照实验必须当轮交叉重复 + 带时间戳日志**：晚高峰端点吞吐波动 2 倍起（同一配置 161s 与 366s 都出现过），单轮对照会得出完全相反的结论；rich 的 80 列换行会把日志搅碎，逐行打 `[xxx.xs]` 前缀再解析才靠得住。**而且真端点实验直接烧用户的 API 余额**（一轮六组对照烧掉 ~¥5，2026-10-09 用户明令停止）——真要动真端点先报预估成本征得同意，能用本地 stub 答的问题绝不上真端点。
- 演示模式文案双语成对（`_demo_txt`），改一处两处都改；演示数据要具体，不要摆拍腔。
- **AI 上下文缓存吃的是「从第 0 个 token 起逐字节相同」的前缀**：同篇论文的整文任务共用 SHARED_SYSTEM + paper_doc（每段截到句界、全文 90k 保头也保尾），任务规则放全文之后——命中输入按约 1/50 计费。任何随任务/调用变化的字段（图表注、术语表、问题、历史）进了前缀就前功尽弃。
- **错误话术映射必须把异常类名并进匹配串**：httpx ConnectError 的消息体是「[WinError 10061] 目标计算机积极拒绝」，"connect" 只在类名里——只匹配消息体的话，连接类错误就带着原文漏给用户（实测）。
- **请求参数的整数要严格收**：`int(1.5)` 静默截成 1，对"译第几段"是悄悄译错段——非整数（含 bool、超 2^53 的浮点）一律 400。
- **WinError 32 的文件锁用"打开探针"测不出来**：杀软/索引器攥着新落盘的 PDF 时开的是共享允许读写的句柄——append 探针畅通，delete/rename 照样被拒。删改恢复逻辑不要拿"文件开得开"当判据，直接试删并按退避重试（删除端点 0.5s×6 同步 + 保洁线程 2s×30 兜底；实测杀软攥 2~3 秒，偶尔更久）。
- **盘上改名与 DB 字段必须全有或全无**：`_align_names` 先挪文件、后挪夹子；原件（path 锚）挪不动就连夹子也不挪——否则"夹已改名、path 字段还指旧夹"的悬空态只有等自愈。尽力而为之上再挂保洁线程（每 2 秒重试、30 秒封顶），把「导入同秒就改名」撞上杀软窗口的场景也收进去。
- **孤儿认主必须用锚，别用名字前缀**：清扫曾按"夹名空格前那段 = pid"认主，命名一代迁到可读名（无 pid）它就把整个文库当孤儿送进 .trash。新名按 DB path 的 dirname 认主，老两代名保留 pid 前缀判法兜底；改认主逻辑前先把全部夹名代次列全。
- **本地库名字学 Zotero（2026-10 定稿）**：夹名与库内文件名 = `作者 - 年份 - 标题`（只取第一位作者，缺哪段省哪段，截 64 字），盘上彻底没有 pid；译文/双语成品带 `（译文）/（双语）` 后缀——用户翻数据目录找得到、单发一个 PDF 给别人对方也认得。成品名由调用方算好传进翻译管线（start(mono_name=)），管线只认路径不认名字；认领（adopt_existing）候选名可读名优先、mono/dual.pdf 老名兜底。
- **网关 200 响应里的字段类型没有任何协议保证**（2026-10-12 测试员轮）：`choices` 可能是 dict 或字符串数组、`message` 可能是字符串、`content` 可能是数组块（新协议）——逐层过类型闸（`_first_content`/`_json_or_empty`），取不到就当空内容走上层的重试/兜底。否则 AttributeError/KeyError 直接冒成 500——注释里写着"别拿 KeyError 砸用户"的那段代码，自己就漏了类型闸。
- **诊断话术不能拿异常类型当唯一判据**（同轮）：`test_connection` 里 `except RuntimeError: 演示模式（未配置 API key）` 会把"服务端 5xx/429/输出上限"三类同是 RuntimeError 的失败也说成"你没配 key"，用户去改一个根本没坏的设置。修法：先显式检查前置条件（`_require_live()` 的 MOCK 信号），其余异常一律走 human_error 如实报。
- **远端 JSON 的"解析段"也要在错误兜底里**（同轮）：update.check 的 `info.get(...)` / `int(size)` 在 try 之外——feed 回数组/字符串、或 size 写成 "41 MB"，"安静探测"就变 500，且不写失败缓存（每次轮询重打源）。凡解析远端数据，解析与字段取值一体兜底，且"取不到"要落成"没有更新"这种无害结论。

## 八、杂

- pywebview+pythonnet 在打包环境 import 即卡死（握着 GIL，兜底计时都跑不到）：独立窗口保持 Edge/Chrome 应用模式，别再试原生窗口（window.py 注释有记录）。
- PyInstaller 用 onedir 不用 onefile：onefile 每次启动解压几十 MB，且"正在跑的程序锁着自己的 exe"，安装器替换不了它，升级必然失败。
- Inno 向导脚本里不能手写默认安装路径（吃掉 `/DIR=` 还覆盖记住的上次位置 → 装出第二份并存，正在跑的那份永远升不上去）；向导只留一个语言，中文用 `[Messages]` 直接覆盖。
- **「rename 换目录 + 删旧的」的最后一删会被文件锁静默吞掉**：升级换装把旧引擎 rename 成 .old（rename 不受打开句柄影响，能成功），随后 rmtree 却可能被杀软/僵尸进程攥着的 exe 挡住——ignore_errors 一吞，600MB 的 .old 从此无人认领。凡是这种换装模式，都要配启动时的重扫兜底（engine_install.sweep_leftovers，2026-10-10 补）；开发目录里躺了 18 天的 .partial/.tmp 就是没人扫的活证据。
- **断点下载目录只按「年龄」清**：并行分段的 .partN 文件名里没有版本信息，换版本后旧断点永远续不上但代码认不出——7 天为界，新鲜的留着（600MB 续传值得），过期的删。find_installed 遇多份引擎并存时认版本号最高的（解析不出退 mtime 新的）——「更新只保留最新」是产品口径，自动换装、残骸清扫、多候选择优三处都要兑现。
- **TTL 缓存必须在「事实改变」的动作里主动失效，不能等过期**：装完引擎找 exe 用的 10 秒 find_installed 缓存，装前刚查的「没装」还在缓存里；下载够快（<10s 装完）时，刚解压的 exe 被自己的缓存瞎了眼，报「包里没有 pdf2zh.exe」整场安装报错。以前全靠 Playwright 启动慢（>10s 缓存早过期）侥幸通过——快机器/快网一凑出来就是必现。修法：换装动作完成后立刻 `_find_cache["at"]=0` 再查。
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
- **「内容没变就不传」的指纹必须打在逻辑内容上**：活库的文件字节被 WAL checkpoint 翻动，逻辑相同的两次备份在文件级哈希不同——文件级对比天天误报「变了」天天重传（webdav 实测）。对 iterdump 行序（+脱敏 config 文本）做哈希才稳定。
- **视觉审查里"看起来没生效"的遮罩类问题，先用像素定案再动手**：灯箱 rgba(5,5,6,.78) 的暗罩在截图缩略里看着像"背景全亮、缩放条透出"，getComputedStyle 全对、读 PNG 像素更实锤（全屏 60,60,61 = 255×0.22，遮罩好端端的）——深灰底白字的低对比在缩略显示里会被对比度感知骗成"亮背景"。顺序：眼见疑点 → 读像素/计算样式 → 才配得上改代码。
- **capture 阶段的 keydown 监听 stopPropagation 会吞掉全局 Esc**：多选模式的 escSel 挂在 document capture 上（先于 App 的 window 监听），一刀 stopPropagation 之后 App 的"收抽屉/关浮层"整条语义全部失联——实测"退了多选，文库抽屉还杵在那"。分层放行：确认框开着 → 全放行（App 的 dlgCancel 管）；归类菜单开着 → 收菜单后拦；裸多选 → 退多选后放行。凡是 capture+stopPropagation，都要问一句"后面的层级还听得见吗"。
- **开场问候这类"书桌专属"的浮层，入口函数要自带"是否已在读"守卫**：启动恢复上次阅读（lastPaper）与手动重载都会让 currentId 先于问候定时器就位，此时弹问候就是压在正文上 5.6 秒（高分辨率逐屏审查在 02/16/27/28 四屏撞到同一颗）。已有的 currentId watch 只覆盖"先问候后开论文"，反方向只有入口守卫能拦。
- **"unused 变量"可能是作用域断裂的信号，别照 linter 直接删绑定**：ruff 报 delete_paper 里 `p = _paper_or_404(pid)` 未使用，照删之后全量回归当场炸出 500——真正的病是 `_late_rm_paper_dir` 里裸引用了 `p`/`pid`/`d` 三个既非参数也非闭包的名字（历史重构漏传参），只要"文件夹当场没收失败"走保洁就 NameError，以前全靠"当场总能删掉"侥幸。linter 的价值在这类报告把灯照到可疑处，修法必须顺着作用域链查一遍再定。
- **删数据链路里的 `paper_dir(pid)` 要在 DB purge 之前算好**：purge 之后再解析只剩兜底路径（DB path 没了、缓存清了就猜 `<pid>`），命中全靠运气。凡是"先删账本再动盘上东西"的顺序，盘上路径一律提前取快照。
- **跨文件重复注释扫描是找"同一知识两处维护"的雷达**（≥14 字的注释句在多处出现）：本轮只撞到三处同名分区标题（编排层与调用层各一份，职责不同不是重复）。真命中过的例子是 styles.css 里漂移了三轮的 `92px→112px→132px` 注释——值改了注释没跟。
- **linter/注释声称的护栏要实测**：figure 端点注释说"clip 与页面求交防内存炸弹"，大 clip 一打确实安全——护栏真在；而 marginalia 删除端点看似有 _paper_or_404 守卫，跨篇 mid 却能删掉别人的卡——"篇存在"和"卡属于这篇"是两道闸，少一道就是越权。对抗轮实锤的还有：pin 的 para_idx 负数漏钳、railW/libW 从 localStorage 读回不钳位（拖动有 min/max 但"读回"这条路没人管）、变体连切时 load() 无重入守卫旧布局覆盖新布局。
- **「适宽」要跟着「可用宽度」重算，别只算一次就固化**（2026-10-11 打磨轮）：页边卷轴（184px）出现/消失、右栏收起、窗口 resize 都会改变可用宽度——以前 fit 比例只在 reflow 时算、钉完批注不重算，纸面 + 页边一起超宽，卡片被挤出视口（用户得再点一次「适宽」）。修法：watch(gutterW/gutterPad) → reflow；可用宽度从 DOM 实读内边距（原先 60/20 两个魔法数，样式一改就悄悄算偏）。回归断言最省事的口径：`desk.scrollWidth <= desk.clientWidth`。
- **全局 keydown 的 input 守卫会吞掉 Esc**（同轮）：App 的 onKey 遇到 input/textarea 直接 return，于是「框选模式永远能一键退出」在输入框里失效——在框选气泡里打完字按 Esc，只收气泡不收框选，纸上再点一下就开新选择，看着像卡死。修法：input 分支里只放行 Esc 一个键（其余照旧留给输入框）。
- **多列 flex 里多个 `margin-left:auto` 会先于 flex-grow 吃净空间**（同轮，术语行）：英文一列 + 中文一列 + 三颗图标各一个 auto 边距 → 文本列被压到 60px 宽，英文撕两行、中文断出孤字（「项目-位置绑/定」）。修法：文本合成一段内联流（`t-main`，当一句话排）、动作收进单个 `t-acts` 块只留一个 auto。
- **底缘浮层要定一条栈**（同轮）：toast 与缩放条同在 bottom:26px，报错长条会把缩放条从中截断（两边各剩半截，看着像渲染坏了）。定序：缩放条 18–52 / 气泡 58–88 / 查找条与返回钮 96 起。
- **宽度过渡的收起动画，内层必须定宽**（同轮）：`.rail-right{width:100%}` 跟着外层的 width 过渡逐帧变窄，面板里的字全程重排（那是挤压不是滑出），顺带把 PDF 的 ResizeObserver → reflow 拖成 2~3 轮重渲染。内层按展开宽度定宽 + 外层 `overflow:hidden`，视觉即滑出。
- **视觉审查要敢裁剪放大**（同轮方法）：整屏缩略里术语断字、气泡切字、卡片出视口都只像「轻微不对」；用 PIL 把可疑区域按 DPR 裁出来原尺寸一看，三处全是硬伤（_qa/_zoom/ 的局部图）。像素定案（读颜色）与裁剪放大（读排版）是缩略图审查的两把尺子。
- **替换（supersede）迁移要先按复合主键去重**（2026-10-11 后端打磨轮）：reading_log(day,paper_id)、paper_collections(paper_id,coll_id)、cite_ctx(paper_id,citing_id) 三张表是复合主键，`UPDATE ... SET paper_id=新 WHERE paper_id=旧` 撞上「新篇已有同 key 行」（同一天读过、进过同一个分类）就是 UNIQUE 冲突 → 整个事务回滚 → 旧篇还在，可调用方已把它放进 _deleted_pids：这篇在本进程里从此「视同已删」，析读/七问生成完的结果全被静默丢掉（书架看着好端端的）。修法：迁移前先删旧篇里「新篇已有」的行（并集去重、幂等），并把 _deleted_pids 的加入改成「失败撤回」。
- **读侧 JSON 一律容 NULL**（同轮，把 qa_history 那条教训铺开到全库）：`json.loads(x)` 遇 NULL 抛的是 TypeError，只 catch ValueError 照样 500——answers_all / answer_get / cite_ctx_all 三处同病。统一口径 `json.loads(x or "null")`；顺带记住 `except ValueError` 兜不住 None。
- **`_new_paper_dir` 这类「找名字-建目录」必须与判重同锁**（同轮）：锁外 check-then-act 让两个并存请求拿到同一个目录，失败方 `_discard_paper_dir` 一把 rmtree 掉赢家刚落盘的 PDF（双击同一个 PDF 两次是最短复现）。上传路径是对的，import-path 漏了——同一件事两条路要抄同一把锁。
- **换装只在新目录验证过关后才删旧副本**（同轮，engine_install）：`rename(final→.old) → 新目录就位 → 删 .old` 这条链里，中间任何一步失败而启动清扫又「见 .old 就删」，用户唯一能用的引擎（600MB）就没了。清扫 .old 前先用盘上真相确认正式目录里真有 exe，没有就把 .old 恢复成正式名。同源的检查：状态机的每个删除点都要问一句「另一份还在不在」——换装、清扫、回滚三处口径必须一致。
- **`_applog` 的轮转是读-改-写，必须进锁**（同轮）：多线程同时跨 1MB 线时 `open(w)` 互相截断，丢的恰是「钱花哪了」的 token 账。凡是「检查大小→改文件」的日志轮转，都要一把模块锁。
- **startup 里的重活按「用户能不能看见白屏」分家**（同轮）：startup 事件是同步跑的，uvicorn 要等它返回才 accept——GB 级 rmtree（上一场的旧数据目录）留在里面，下一场启动就是几分钟白屏。清扫挪进 _late_boot；换数据目录落地这种「接口层必须先完成」的一根不动。
- **重试口径：429/5xx 是瞬时的、值得退避一次；但流式回答一旦吐过字就绝不能重开**（同轮）：重开会给用户「半截 + 重来」两段。chat_stream 因此带 emitted 守卫；`human_error` 补 5xx 分支（「模型服务暂时不可用（HTTP 503），过一会儿再试」），别把 httpx 的原始异常甩到界面。
- **膨胀最快的表要有 LIMIT**（同轮）：全库内容搜索给正文限了 400 行、qa_messages 没限——会话历史是库里长得最快的表，每个搜索请求整表 LIKE 扫它。检索类 SQL 逐条核「这张表三年后多大」。
- **有成本的缓存触顶别 clear()**（同轮，compare._CACHE）：一把清空 = 用户下次打开对比表全表重抽（真金白银）。按插入序淘汰最旧的一批，留大头。
- **「查找范围」要按用户此刻看得见的那一层定义，不是按数据来源**（2026-10-12）：in-paper 查找里的 `if (it.origPage < 0) continue`（本意大概是「跳过非原文页」）把译文/双语里的译文页整类踢出查找——用户视角就是「译文里中英文都搜不到」，而双语里只有左半（原文）有命中。修法：逐项扫各自渲染出的文字层（版面是哪种就查哪种），命中框的归属从「页号」换成「条目下标 gi」——译文页的 origPage 恒为 -1，按页号归组会互相串、框会画到原文页上。
- **写中文 PDF 夹具先确认 pdf.js 抽得出来**（同轮）：PyMuPDF 内置字体 `fontname="china-s"` 写出的文字 MuPDF 自己读得到，但 pdf.js 抽不出（没有可用的 ToUnicode），文字层全空——拿它测「译文查找」会得到假红。用系统字体嵌进去（msyh.ttc / simsun.ttc / simhei.ttf 实测 pdf.js 都能正常抽取），或者直接借一份真 pdf2zh 产物（实测真译文 4/4 页文字层可读、中文查找有命中）。
- **给局部变量改名要按「函数」为单位扫，不是按「行」**（2026-10-12 重构轮）：把 `matchPaper` 里的局部 `t`（标题）改成 `q` 时只替换了定义处与第一个回调，第二个回调里还有一处 `t.includes(x)`——它不报「未定义」，而是安静地解析成 i18n 那个 `t()`（同名遮蔽），运行期变成 `t.includes is not a function`，提问面板的整条消息列表渲染当场炸掉（消息在、`.q-body` 一个都不出）。做法：改名前后都 grep 该函数体内这个名字的全部出现；`.vue` 里局部名与 import 撞名（t/ui/store…）的，改完必须在真页面里跑一遍相关面（本例 test_xref_jump 一跑就现形）。
- **heredoc 会连反斜杠一起吃掉，长补丁一律写成脚本文件再跑**（多轮反复踩）：`[",\r\n]` 这类转义序列经 heredoc 落地变成真换行（JS 正则不允许行终结符，整份文件语法错），f-string 与行尾续路反斜杠同样炸。凡补丁带转义序列或反斜杠，先写 `D:\tmp\xxx.py` 再执行，含反斜杠的匹配串用 chr(92) 拼，改完立刻 `node --check`。
- **断言要能失败——「跑了但什么都没测」的三种形态**（2026-10-12 测试优化轮，全量盘点 67 个测试抓到的）：① 恒真断言（`ck('空白 term_en 拒绝', st in (200,400) and (st==400 or True))` 后半段永远是 True）；② 攻击点打在**已改名的旧路由**上（/translate-cancel、/glossary.csv、/para-translate、/page.png 全 404，「非 5xx 即通过」于是全空过）；③ 夹具太稀（单页一两行正文被 ingest 当扫描件走 OCR，断言却在等 `analysis_status==done`，永远等不到）。判据很简单：写每条断言前先问「它失败的话长什么样」，答不上来就是没测。
- **测试一律自起沙箱，绝不打固定端口上的「别人」**（同轮，最高危的一条）：`test_golden_0151` 写着 `for x in GET 8430/api/papers: DELETE …`——写它时 8430 是个测试实例，后来那台成了**用户真库的端口**：再跑一次就是清库。凡默认 `BASE=http://127.0.0.1:8430` 又不自起服务的测试，先当数据风险处理（删或改成 `serve_temp` + `EGGPAPER_DATA` 沙箱）。
- **删测试前必须先把独有断言搬走——`_qa` 不入库，删掉就找不回来了**（同轮踩到）：`test_edges4.py`、`test_edges5.py` 删掉后想回头核对「那几条断言到底写了什么」已无从查起（不在 git、也没有归档），只能凭盘点记录重建少数几条。口径：删除前逐条搬进事实标准；批量删测试的轮次先把断言原文抄进 lesson 或提交信息。
- **固定等待换条件轮询，但「等坏事不发生」要换成「连续 N 拍都满足」**（同轮）：22 处 `wait_for_timeout(3000..12000)` 换成等真实状态（`analysis_status=='done'`、选择器出现、列表条数），既快又不 flaky；而 `test_hardened` 那 3 秒等的是「析读线程不许把行插回来」的竞态窗口，不能简单轮询——改成连续 6 拍每拍都符合、任一拍复活即失败。另记一个坑：`test_paperdir` 原等待等的不是「列表画出」而是**版本号轮询的第一拍**（基线），换成「等列表出现」反而不稳——换轮询前先确认原等待真正在等什么。
- **sparse PDF 夹具会被当扫描件**（同轮）：只写一两行文字的 PDF 在 `_ingest` 里出不了段落 → 走 OCR 路（本机 OCR 常失败 → `analysis_status=error`）。凡断言依赖「析读完成」的夹具，正文要按「多段多行」写足（与 `test_find_scope` / `shots_polish` 同口径）。
- **`dict.setdefault` 的返回值判"是不是我建的"是恒真**（2026-10-12 测试员轮）：`ev = d.setdefault(k, Event()); mine = d[k] is ev` 里 setdefault 返回的就是现有值，`mine` 永远 True——单飞守卫整个成了死代码，两个窗格同开一篇就双份跑整本 O(n²) 扫描。正确写法：先造哨兵 `ev = Event(); prev = d.setdefault(k, ev); mine = prev is ev`。凡是"我是不是第一个/创建者"的判据，先读一遍 setdefault/add/incr 的返回语义再落笔。
- **check-then-act 去重（先 SELECT 再 INSERT）在并发下必漏**（同轮）：pin 的"同段只留一条查译卡"在端点里拆成两次调用，连点/双窗格同钉各看到"没有旧卡"、各插一条。判重与写入必须同锁（`db.marginalia_upsert_lookup`）——`glossary_add` 早就修过同款（其注释就是"实测踩过"），这次漏的是"同一课没按类普查"。规则：凡"先查后写"的端点，逐个过一遍并发双发。
- **FastAPI 的 int 参数没有上限，SQLite 绑参会 OverflowError → 500**（同轮）：客户端送 `conv_id=10**30` 就是 500+堆栈。注册 `@app.exception_handler(OverflowError)` 回 400 人话（覆盖所有 path/query 的 int 参数）；body 里的 int 走 `_int_arg` 并把上界钳到 64 位。同类：body 的字符串字段要过类型闸（`_s_arg`：非字符串当"没给"），别 `str()` 硬转（`{"note": 123}` 会存进"123"）也别裸 `.strip()`（`{}` 直接 AttributeError）。
- **"在途"标记的收尾只认最新一拨**（同轮，前端）：老请求被顶掉（abort/换篇/换选区）之后它的 finally 不许清掉新请求的 busy——否则按钮提前复活、第三拨叠上来。做法：per-request token（`selSeq`/`selStream`/`sixBusy[k]={pid}`）或按 pid 比对。`selStream` 声明后从未赋值（abort 全空转、两条流的字混进一个气泡）是"流对象没留手"的典型，凡是流式/长请求都要留着 abort 手柄。
- **"取消/收罩"要把这一次交互的监听一并拆掉**（同轮）：截图拖到一半按 Esc 只撤了暗罩，文档上的 mousemove/mouseup 还挂着——用户以为取消了、随手松开鼠标，照样截图（还写剪贴板/落盘）。滚动的"临时契约"（拖拽/框选/截图）都要有 `dragEnd()` 这种一次性拆除函式，Esc/失焦/卸载三条路共用。
- **多窗格：按篇的锚点绝不能读全局表**（同轮）：PdfViewer 用全局 `paraByIdx`（活动篇的段落）解析非活动窗格的批注锚点——引文高亮画到不相干的 y、跳转跳错行、卡片引文回落到别篇的句子。每个窗格用**自己加载的 paras** 建表（byIdx）。判断口径：跨组件的 computed（store.paraByIdx 这种）只在"全局唯一活动篇"的语义下成立，多实例场景逐处核。
- **换篇（或换会话）要把"按篇状态"逐项过一遍**（同轮）：TOC 开着不重载（点一条拿旧篇页码跳新篇）、速览 figuresLoading 早退漏复位（永远"正在找图表…"）、六问 busy 跨篇（新篇的问题被跳过、停在"未生成"且无法再触发）、灯箱图注 capZh 无 epoch（旧篇译文写进新篇）、`tranOptimistic`/`tranProg` 全局（新篇显示假的"翻译中"、秒表续用旧篇起点、停止打到错篇）。规则：`if (已有) return` 守卫 + 一切 pending 标记，要么带 pid/epoch、要么在换篇 watch 里显式清。
- **在途提示用计数不用布尔**（同轮）：`capPending=true/false` 在快速翻图时被后来的请求互相抹掉——改计数（`++/--`），模板判 `> 0`。
- **loading 的早退路径也要收口**（同轮）：`if (换了篇) return` 写在 `loading=false` 之前，spinner 永远转着/empty 恒 false（AskPanel 空白无空态、TOC 永久"读取中"）。一律 try/finally 或显式复位，且复位只在"还是当前篇"时做。
- **单键快捷方式要看输入内容**（同轮）：提问框里裸 `/` 一律拦下开引用选择器——"and/or"、"A/B"、"10/20" 这些字符就永远打不出来。约定：只在**空输入框**里当命令键（slash-command 惯例）。
- **惰性同步的状态端点会与取件端点错开一拍**（2026-10-12 文档轮）：翻译完成的库落盘写在 `translate-status` 里（"谁来问状态才写库"），但 `pdf?variant=mono` 只认库里的 mono_path——job 已 done、还没人问过状态的那一拍取件就是 404。端到端测试在负载下撞到过一次（单跑零复现，日志只剩尾部 800 字符只能靠推理），修法：取件端点在库没跟上时直接问 job，补同步段抽成 `_sync_translate_from_job` 与状态端点共用（顺带清过期 dual 与旧名 mono）。通例：**惰性同步的写口与读口之间要检查"另一头是否真的问过"**，读口自己也要能补一次同步。
- **按下标删列表项的双击会删错行**（同轮）：第一次删除让后面的项顶上，第二下落在用户没点的那条上。改按对象身份/id 定位再删 + 在途守卫。
- **上传临时文件的所有权跟着"是否被接收"走**（同轮）：安装器忙时 `start_from_zip` 直接返回，上传端点只在"装完"才删临时包——2GB 残骸永驻 `.upload/`。忙分支按 `cleanup` 自己收掉，清扫器再按年龄兜底一层。
- **闸门要在"锁内、起线程之前"登记**（同轮）：OCR 的"占位"写在线程体内（被调度到才登记），双击的两下都看到"没人在跑"各起一个线程；两个 OCR 跑同一份 PDF，先完成的还清掉共享登记，让按钮复活再叠第三个。状态登记与状态检查必须在同一个临界区完成。
- **"解析失败"的 400 人话和"半截暂存"要一起收**（同轮）：备份 zip 里的非法 Windows 文件名（`?`/`|`/保留名/超长）会让恢复在 open 时 OSError——这不是服务端故障，400 人话 + 把半截暂存目录清掉（不清的话下次恢复从残骸上再解一遍）。
- **浮层化的侧栏会盖住"它下面的东西"，包括对面窗格的关闭钮**（2026-10-12 端到端走查）：多窗格模式曾把右栏改成 `position:fixed` 浮层（不挤压窗格），于是最右窗格的标题栏（关闭 / 标题）与页边批注整条竖带都在右栏底下——`elementFromPoint` 打在关闭钮上命中的是右栏自己的收起钮，最右窗格**关不掉**、批注看不见。修法回退成"占位挤压"（与单篇同口径）：宁可窗格窄一点，不能藏内容与控件。判据必须用 `elementFromPoint` + 矩形量（拿坐标点去问"这一点上是谁"），肉眼看不出来。
- **v-for 里的字符串 ref 会收成数组**（同轮实锤）：`<input v-for=... ref="pEditEl">` 之后 `pEditEl.value.focus()` 抛 `TypeError: q.focus is not a function`——Vue 3 对 v-for 内的字符串 ref 注册的是**元素数组**。用户侧现象：双击改名时弹"出错了"toast、输入框拿不到焦点（必须手点）。改 `:ref="el => (pEditEl = el)"`（与 App.vue 里 homeEl 同款）；修完 focus 与全选都正常（selectionStart/End = 0/len 是硬断言）。
- **"稳"的组合态要显式定义**：暗色（html.dark）与护眼底纹（html[data-care]）是两套变量，同时置上时 CSS 里 dark 覆盖 --paper，但 `--care-wash` 没被覆盖 → 暗色下纸面仍盖一层亮绿（用户以为坏了）。定案：暗色下不渲染 care-wash（纸面永远白是设计口径），底纹设置继续留着，回到亮色自动恢复——非破坏、也符合"纸面永远白"的注释。
- **端到端走查的工具边界要写清楚**（同轮）：① 本次会话 Computer Use 不可用（宿主返回 "Computer Use is unavailable for this node_repl session"，`input2.broker` 为空=桌面端没为本会话挂 CUA 运行时桥），改用官方 Browser Use（真浏览器 + 2560×1440 截图 + Playwright API）；② IAB 窗格里 `requestAnimationFrame` **不触发**（实测 rafFired=false，页面不被合成），于是所有 Vue Transition 会停在 `*-leave-from/active`（气泡点了"钉在页边"后不消失其实是假象）——**别用这种环境判"元素该消失却还在"**，要用状态断言（DOM/接口）；换 Playwright 通道后 rAF 正常；③ 模型看到的截图是 2560×1440 落盘 PNG 的**下采样显示图**，判"暗罩盖没盖住"这类问题必须读 PNG 像素（本例 66,66,67 = 255×0.34+5×0.66，两眼看着"没盖住"其实盖得好好的）。
- **Playwright 的可点击性判定比真人严**：抽屉的遮罩存在时，它拒绝点被遮的按钮（真人点下去是"关闭抽屉"）。要区分"Playwright 拒绝"与"真人点不到"：真人的点击路径用坐标点击/直接点遮罩复现，别把工具的限制当成产品 bug；反过来，`elementFromPoint` 命中别的元素才是真遮挡。


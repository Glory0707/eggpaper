# 图标：改哪里才有效

这份文档只讲一件事——**换图标时该动哪个文件、怎么验证**。起因是同一枚任务栏图标连改三轮
都没变化（改的都是不生效的地方），最后靠实验才定位。把那次的结论固化在这里，别再重走。

## 一、先看清：哪个界面吃哪份图

图标的"来源"不是一个，是四条链路。**改之前先确认你看到的那处属于哪条**：

| 出现的界面 | 图从哪来 | 谁生成 |
|---|---|---|
| 桌面快捷方式、资源管理器、开始菜单、Alt+Tab | exe 里嵌的**多尺寸 ICO** | `tools/make_icon.py` → `installer/eggpaper.ico` |
| 浏览器**标签页** | 页面声明的 favicon（`/favicon.ico` + `/icons/*.png`） | 同上 → `frontend/public/` |
| **独立窗口的标题栏与任务栏按钮** | 窗口创建后由 `window._give_window_icon()` **注入**的 HICON | 同上，运行时用 `mark.draw()` 现画 |
| 系统**托盘** | `desktop.py` 里 pystray 画的图 | `mark.draw(尺寸)` |

四条链路共用一个真源：**`backend/mark.py` 的几何**。改形状只改这里，其余全部由它派生。

## 二、真源：几何在 `backend/mark.py`

```python
CX, CY = 48.0, 48.0     # 蛋心（84 的视图里；与 frontend/src/components/EggMark.vue 同一套坐标）
RX = 30.0               # 左右半轴
RY_UP, RY_DN = 34, 42   # 上半短、下半长 → 钝端朝上（品牌形状的全部特征就在这一条）
RING = 8.5              # 环宽（小尺寸另有下限，别手调）
BARS = ((32, 30), (44, 36), (56, 22))   # 三条字条：y / 宽——最后一条短一截，像段末
BAR_H = 7.0
TILE_RADIUS = 0.20      # 圆角白垫的圆角 = 画布边长的 20%
```

**形状是"上短下长的蛋"，不是圆。** 所以环不能用 `ImageDraw.ellipse(outline=…, width=…)` 画
（那画出来的是正圆环，把上短下长抹平了）；正确做法是外蛋轮廓填满、再挖掉"各半径减一个环宽"的内蛋轮廓。

**底色：方形白垫 + 四个圆角**（`draw(px, tile=True)`）。这是用户定的终稿，三轮口径记在这里，改之前先读：

1. 不垫 → 深色任务栏上墨青环几乎看不见；
2. 垫成**蛋形** → 用户的反馈是"还是需要白色垫"（他要的是纸，不是更大的蛋）；
3. 定稿 = **方形的白色垫，只是四个角是圆角**——桌面快捷方式、任务栏、托盘、窗口图标全用这一版。

小尺寸（16/20/24）只画**三条**字条并把环加粗（从 `EggMark.vue` 的 compact 那版移植）：
再小就没有"三条线"这个特征可认了。

缩小时先**预乘 alpha** 再 BOX 缩（`_downscale_pm`）：直接缩 RGBA 会在圆角处把"白"和"透明黑"平均，
alpha 半透明、RGB 发灰，合成到任务栏/桌面上就是一圈脏边。

改完必须过闸门（会非零退出）：

```bash
python tools/check_icon.py     # 审的是**已装运的资产**（installer/eggpaper.ico、public/icons/icon-48.png）：
                               # 四角必须透明（圆角生效）、上沿中点必须不透明（垫在）、必须恰好 3 段墨
```

它会打印每一档的墨段数与缝宽，输出最近邻放大对照表并存到 `_qa/shots/`，
还附 ASCII 放大图——**先看数字和这张图，再谈好不好看。**

## 三、标准动作（照这个顺序，别跳）

```bash
# 1) 改 backend/mark.py 的几何（或只改配色 ACCENT/PAPER）
python tools/check_icon.py                    # 2) 闸门：缝隙不够就别往下走
python tools/make_icon.py                     # 3) 重新生成 ico + 网页整套 png
# 4) 改了图标就把 VERSION 加一（见第五节：图标 URL 的 ?v= 跟着版本号走）
python tools/build_installer.py --notes "…"   # 5) 打包（会自动重生成图标、重嵌 exe 图标）
```

> **第 4 步别漏**：`?v=` 是 `build_installer.py` 用 `VERSION` 里的版本号替换的，所以
> **版本号不变，图标 URL 就不变，浏览器会继续吃旧图**。只改图标、不升版本号时，
> 拿到新图的前提是清掉浏览器那份图标缓存（第五节）。

生成物一览（都由 `make_icon.py` 产出，不要手改）：

- `installer/eggpaper.ico` —— 15 档（16/20/24/30/32/36/40/42/48/60/64/72/96/128/256），
  `≤64` 用 DIB 负载、`128/256` 用 PNG（**ICO 规范：只有 256 允许 PNG 负载**；全 PNG 会被
  PyInstaller 静默跳过）
- `frontend/public/icons/icon-<size>.png` —— 同尺寸阶梯 + 192/512
- `frontend/public/favicon.ico` —— 给浏览器的多尺寸 ICO

**每一档 DPI 的精确物理尺寸都要有原图**，这是被逼出来的：150% 缩放下任务栏要 **30/36/48**
这些物理像素，缺哪档就有人替你缩放，一缩放就软。

## 四、独立窗口的任务栏图标：为什么不能只改 favicon

这块单独说，因为它是唯一"改了 favicon 也到不了位"的地方。三条实测结论：

1. **彩色指纹实验**（给 ICO 每档染一个纯色，看任务栏取哪个颜色）：150% 缩放下 Chromium 按
   **物理像素**取 24px 那档，却被 Windows 当逻辑 24 再放大到 36/48——源头位图偏小，画得再好也软。
2. **满幅色块标定**：注入 48/32/24 的满幅方块，任务栏都落到 **48×48 物理像素**的槽位。
   槽位是 48，而 favicon 给的是 24，所以软。
3. **`--app-icon=<ico>` 开关对应用模式窗口无效**（红方块 A/B：任务栏零变化）。

所以正解是**从进程外把图标盖掉**，实现见 `backend/window.py`：

- `open_window()` 用 `subprocess` 拉起浏览器后，起一个后台线程；
- 按窗口标题 + PID 找到那个 `Chrome_WidgetWin_1` 窗口；
- `GetDpiForWindow` 问真实 DPI → `GetSystemMetricsForDpi` 问出大小图标的物理像素；
- **不用 `LoadImage` 从 ICO 选档**（实测它会挑 60 再缩到 48，仍有一次重采样），而是
  `mark.draw(48)` 按精确像素现画 → `CreateBitmap` + `CreateIconIndirect` 造 HICON；
- `WM_SETICON` 三个槽都盖。**Windows 11 任务栏读的是 `ICON_SMALL2`**（分色实验测出：BIG 放红、
  SMALL 放绿、SMALL2 放蓝，任务栏显蓝），所以把 48 的大图放进 SMALL2，1:1 落槽；
- Chromium 在 favicon 加载后会把图标重设回去，所以后台线程**反复压约 30 秒**。

**改这块之后必须验证**（日志里会写"窗口图标已按物理像素注入（大 48px / 小 24px…）"）：

```bash
# 开窗口，等注入跑完
curl -s -X POST http://127.0.0.1:8430/api/window
grep "按物理像素注入" "$LOCALAPPDATA/eggpaper/logs/app.log" | tail -1
```

## 五、缓存：三层，改完看不到变化先怀疑它

| 缓存 | 存哪 | 怎么破 |
|---|---|---|
| 浏览器图标缓存 | Edge 配置里的 `Favicons` SQLite（按页面 URL 记账） | 图标 URL 上的 `?v=`，**发版时由 `build_installer.py` 自动替换成版本号** |
| Windows 图标缓存 | `%LOCALAPPDATA%\Microsoft\Windows\Explorer\iconcache_*.db` | 停 explorer → 删 `iconcache_*.db` → 拉起 explorer |
| Chromium 的 HTTP 缓存 | Edge 配置里 | 很少需要管；`?v=` 变了自然会重取 |

清 Edge 的图标缓存要**按完整 origin 前缀**删，别用子串：

```python
# %LOCALAPPDATA%\Microsoft\Edge\User Data\Default\Favicons
cur.execute("DELETE FROM icon_mapping WHERE page_url LIKE 'http://127.0.0.1:8430%'")
# 连带 favicons / favicon_bitmaps 里对应的 icon_id 一起删
```

> 踩过：写过一次 `LIKE '%8430%'`，把 6 条 URL 里碰巧含这串数字的**无关网站**的图标缓存
> 删了（下次访问会自动重建，但这是不该犯的错）。

## 六、验证：不要用眼睛替实验

**"糊"是个主观词，先把它变成数字。** 任务栏那处不能靠猜，做法是截图 + 按墨青色定位 + 放大：

```python
# 只截屏幕，不碰桌面上的其他窗口；找 accent 墨青像素成簇的位置，裁出来放大看
img = ImageGrab.grab(all_screens=True).convert("RGB")
band = img.crop((0, img.size[1] - 110, img.size[0], img.size[1]))    # 任务栏那一条
# 命中 acccent (29,78,95) 容差 45 的像素 → 按 x 聚类 → 每簇裁出、NEAREST 放大 10 倍存档
```

看这张放大图时，对照三件事：**白缝是否分明、边缘是不是 1px 干净抗锯齿、白块圆角有没有灰晕**。
（灰晕的来源是直接缩 RGBA：透明像素 RGB 是黑的，被平均进白边。`mark._downscale_pm` 用
预乘 alpha + BOX 缩放解决。）

要判断"系统到底取的是哪一档、槽位多大"，用这两个一次性实验，比读源码快得多：

- **指纹法**：给 ICO 每档染不同纯色 → 看目标位置显示什么颜色，就知道取了哪档；
- **标定法**：注入满幅纯色块 → 量它在屏幕上占多少物理像素，就知道槽位尺寸。

## 七、坑清单（每条都付出过时间）

- **别改 `installer/eggpaper.ico` 或 `frontend/public/icons/*.png`**：构建时会被重新生成覆盖。
- **改图标后必须清 PyInstaller 缓存**：图标只在 EXE 那一步用到，`workpath` 缓存会让它不重做
  （`build_installer.py` 已经会清 `build/pyi` 与 `build/work` 两处）。
- **ICO 里 `≤64` 必须是 DIB 负载**，只有 256 能用 PNG。
- **内联 SVG favicon 是个坑**：Chromium 会把它栅格化成很小的位图再放大，怎么改都糊
  （`index.html` 里曾挂过一条，已删，别再加回来）。
- **`PIL` 不能出现在 PyInstaller 的 `excludes` 里**（否则"Hidden import not found"，即使装了 Pillow）。
- **托盘图标要按系统真实尺寸原生画**（先 `SetProcessDpiAwareness`，再问 `SM_CXSMICON`）：
  pystray 的 Windows 后端把图存成单尺寸 ICO + `LR_DEFAULTSIZE`，画 32 交给它会经历两次重采样。
- **`console=False` 的打包版没有 stderr**：任何图标相关的失败都必须写日志，否则症状只有
  "图标不对"，原因永远查不出。

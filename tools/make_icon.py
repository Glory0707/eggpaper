"""从 EggMark.vue 的几何生成应用图标（多尺寸 .ico / 网页 PNG）。

形状来源：frontend/src/components/EggMark.vue 的 **compact 稿**（椭圆环 + 三条线）。
**不是截屏**，是用 Pillow 按同一份几何重画——构建时就地生成，不依赖浏览器。
几何数字只在 backend/mark.py 里维护一份。

**都要垫一层"方形 + 圆角"的白底**（`tile=True`）。这一条被用户纠正过三次，别再改回去：

- 不垫白底：深色任务栏上墨色几乎看不见 → 用户："还是需要白色垫在下面"
- 垫**方角**纸底：桌面/任务栏上看就是"一枚带四个角的方块" → 用户："又回退了，四角"
- 所以白底要垫，但形状是**方形 + 圆角**（常规应用图标那种"应用块"，半径 = 画布 20%）：
  用户最终口径是"就要方形的白色垫，只是四个角是圆角"。形状在 backend/mark.py 的
  draw(tile=True) 里生成，改几何只能改那里的比例常量。
- 另外**条数恒为三条**（用户拍板的那一版）：三条线是标识，放不下就压薄字条，不许减条。
"""
import io
import os

from PIL import Image

import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))
import mark  # noqa: E402  几何与配色只在 mark.py 里维护一份

VIEW, ACCENT, PAPER = mark.VIEW, mark.ACCENT, mark.PAPER


def _draw(px: int, tile: bool = False):
    return mark.draw(px, tile=tile)


def _dib(img) -> bytes:
    """把一张图写成 ICO 里的 BMP 负载（DIB）。

    布局：40 字节 BITMAPINFOHEADER（高度写**两倍**，因为下面还要带 AND 掩码）
    + 自下而上的 BGRA 像素 + 1bpp 的 AND 掩码（32 位图用不到，全 0，但每行要按
    4 字节对齐——这一条写错，Windows 会画出斜的条纹）。
    """
    import struct
    w, h = img.size
    px = img.convert("RGBA").load()
    rows = []
    for y in range(h - 1, -1, -1):
        row = bytearray()
        for x in range(w):
            r, g, b, a = px[x, y]
            row += bytes((b, g, r, a))
        rows.append(bytes(row))
    pixels = b"".join(rows)
    mask = bytes(((w + 31) // 32) * 4 * h)
    header = struct.pack("<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0, len(pixels) + len(mask),
                         0, 0, 0, 0)
    return header + pixels + mask


def _ico_bytes(imgs, sizes) -> bytes:
    """自己拼 ICO 容器：每个尺寸放专门为它画的那一张。

    为什么不用 Pillow 的 `save(format='ICO', sizes=[...], append_images=[...])`：
    实测那一组合**只写进了第一个尺寸**（生成出来的 ico 里只有一张 16×16），Windows
    只好把 16px 放大到 48/64 用——这就是"图标很糊、不像矢量"的第一个原因。

    第二个坑：ICO 里 **PNG 负载只对 256×256 合法**，小于它的必须是 BMP/DIB。
    全用 PNG 的话 Pillow 自己能读回来，但 PyInstaller 那类工具会静默跳过整份图标
    （实测：换了 ico，打出来的 exe 字节数一个不差，图标根本没上去）。
    所以这里按规范选负载：≤64 走 DIB，128/256 走 PNG。
    """
    import struct
    n = len(imgs)
    dirs, blobs = b"", b""
    offset = 6 + 16 * n
    for img, s in zip(imgs, sizes):
        if s >= 128:
            buf = io.BytesIO()
            img.save(buf, format="PNG", optimize=True)
            payload = buf.getvalue()
        else:
            payload = _dib(img)
        dirs += struct.pack("<BBBBHHII", 0 if s >= 256 else s, 0 if s >= 256 else s,
                            0, 0, 1, 32, len(payload), offset)
        blobs += payload
        offset += len(payload)
    return struct.pack("<HHH", 0, 1, n) + dirs + blobs


# 每一档 DPI 的**精确物理尺寸**都要有一张原图。这是用彩色指纹实验量出来的
# （在 ICO 里给每个尺寸染不同颜色，看任务栏取了哪个）：这台机器 150% 缩放，
# Chromium 的应用模式窗口从 favicon.ico 里取的是 **30px**（20 逻辑像素 × 1.5）——
# 之前 ICO 里没有 30，它就拿 32/24 缩放，任务栏永远是软的。
# 20/24/30/36/42/48 = 16~24 逻辑像素在 125%/150%/175%/200% 下的物理尺寸；
# 60/72/96 = 40/48/64 在 150% 下；128/256 留给大图标与高分屏。
ICO_SIZES = [16, 20, 24, 30, 32, 36, 40, 42, 48, 60, 64, 72, 96, 128, 256]


def make_ico(out_path: str, also_png: bool = True, tiled: bool = True) -> str:
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    sizes = ICO_SIZES
    # 每一档都垫**蛋形**白底：库里的 ICO 是任务栏、桌面快捷方式、浏览器标签共用的，
    # 各界面挑不同档，垫法必须一致；而白底让深色任务栏上也看得清。
    imgs = [_draw(s, tile=tiled) for s in sizes]
    with open(out_path, "wb") as f:
        f.write(_ico_bytes(imgs, sizes))
    # 顺手留一张 256 的 png：Inno Setup 的向导图标要 png/bmp
    if also_png:
        imgs[-1].save(out_path.replace(".ico", "-256.png"))
    return out_path


def contact_sheet(out_path: str) -> str:
    """放大对照表（最近邻 8 倍）：小尺寸到底糊不糊，看这张图就知道。"""
    sizes = [16, 24, 30, 36, 48, 72, 256]
    zoom = 8 if max(sizes) * 8 <= 2048 else 4
    imgs = [_draw(s, tile=True) for s in sizes]
    pad, label_h = 12, 0
    W = sum(i.width * zoom + pad for i in imgs) + pad
    H = max(i.height for i in imgs) * zoom + pad * 2
    sheet = Image.new("RGBA", (W, H), (246, 245, 242, 255))
    x = pad
    for i in imgs:
        big = i.resize((i.width * zoom, i.height * zoom), Image.NEAREST)
        sheet.alpha_composite(big, (x, pad))
        x += big.width + pad
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    sheet.convert("RGB").save(out_path)
    return out_path


WEB_SIZES = ICO_SIZES + [192, 512]


def make_web_icons(public_dir: str) -> list:
    """给前端放一整套**按目标尺寸各画一张**的 PNG（public/icons/）+ 根上的 favicon.ico。

    标签、独立窗口（Edge 应用模式）的标题栏与**任务栏**、"安装为应用"，都从这几份里挑。

    **两件踩出来的事**：
    1. 应用模式窗口在 Windows 上取的是 **favicon.ico**（不是 <link> 里的 PNG——探针里
       只给 PNG、不给 ICO 时任务栏直接是地球仪）。所以 ICO 里必须有每一档 DPI 的精确尺寸，
       尤其是 150% 缩放下要的 **30px**（彩色指纹实验量出来的，见 ICO_SIZES 注释）。
    2. 浏览器图标缓存按 URL 记。index.html 里所有图标 URL 都带 ?v=，由 build_installer.py
       在发版时替换成版本号，老用户升级后才会真的重新拉取。
    """
    out = []
    icons = os.path.join(public_dir, "icons")
    os.makedirs(icons, exist_ok=True)
    for size in WEB_SIZES:
        _draw(size, tile=True).save(os.path.join(icons, f"icon-{size}.png"))
        out.append(f"icons/icon-{size}.png")
    make_ico(os.path.join(public_dir, "favicon.ico"), also_png=False)
    out.append("favicon.ico")
    return out


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.normpath(os.path.join(here, ".."))
    print("写好:", make_ico(os.path.join(root, "installer", "eggpaper.ico")))
    print("网页图标:", make_web_icons(os.path.join(root, "frontend", "public")))
    print("对照表:", contact_sheet(os.path.join(root, "_qa", "shots", "icon_sheet.png")))

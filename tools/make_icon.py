"""从 EggMark.vue 的几何生成应用图标（多尺寸 .ico）。

形状来源：frontend/src/components/EggMark.vue（椭圆环 + 几行字条，末行短一截）。
**不是截屏**，是用 Pillow 按同一份几何重画——构建时就地生成，不依赖浏览器。

上一版为什么糊：所有尺寸都从"按 96 视口画的同一份几何"缩下来。缩到 16px 时
环只剩 1.4px、字条剩 1.2px，落成一团灰；而 48px 以上我又垫了一块白圆角底，
在浅色桌面上看就是"糊了一块的方块"。现在：

- **每个尺寸单独定几何**：小尺寸加粗环、减字条（16px 只留两条，环 2px 实心）
- **48px 以下不垫底**（透明 + 墨色，桌面/资源管理器里最干净）；
  48px 及以上垫纸色圆角底——**任务栏通常是深色**，纯墨色图标在那上面看不清
- 全部按 4 倍超采样后缩回目标尺寸，边缘才干净
"""
import io
import os

from PIL import Image, ImageDraw

VIEW = 96                                   # 与 SVG 的 viewBox 同尺度
ACCENT = (29, 78, 95)                       # --accent
PAPER = (255, 255, 255)

# 每个尺寸一套：(环宽, 字条列表[(y, 宽)], 条高)
SPEC = {
    16: (13.0, [(34, 26), (54, 16)], 10.0),          # 只留两条、环最粗
    24: (11.0, [(32, 30), (46, 34), (60, 18)], 8.0),
    32: (9.0, [(32, 30), (45, 36), (58, 20)], 7.0),
    48: (7.0, [(31, 34), (43, 40), (55, 38), (66, 20)], 6.0),
}
FULL = (6.0, [(30, 36), (42, 42), (54, 40), (66, 22)], 5.5)   # 128/256 用完整稿


def _spec(px):
    for k in sorted(SPEC):
        if px <= k:
            return SPEC[k]
    return FULL


def _draw(px: int, tile: bool) -> Image.Image:
    ss = 4
    size = px * ss
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    k = size / VIEW
    w, bars, h = _spec(px)
    if tile:
        r = size // 5
        d.rounded_rectangle([0, 0, size - 1, size - 1], radius=r, fill=PAPER)
    d.ellipse([18 * k, 14 * k, 78 * k, 82 * k], outline=ACCENT, width=max(2, round(w * k)))
    for y, bw in bars:
        d.rounded_rectangle([(48 - bw / 2) * k, (y - h / 2) * k,
                             (48 + bw / 2) * k, (y + h / 2) * k],
                            radius=h * k / 2, fill=ACCENT)
    return img.resize((px, px), Image.LANCZOS)


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


def make_ico(out_path: str) -> str:
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    sizes = [16, 24, 32, 48, 64, 128, 256]
    imgs = [_draw(s, tile=(s >= 48)) for s in sizes]
    with open(out_path, "wb") as f:
        f.write(_ico_bytes(imgs, sizes))
    # 顺手留一张 256 的 png：Inno Setup 的向导图标要 png/bmp
    imgs[-1].save(out_path.replace(".ico", "-256.png"))
    return out_path


def contact_sheet(out_path: str) -> str:
    """放大对照表（最近邻 8 倍）：小尺寸到底糊不糊，看这张图就知道。"""
    sizes = [16, 24, 32, 48, 64, 256]
    zoom = 8 if max(sizes) * 8 <= 2048 else 4
    imgs = [_draw(s, tile=(s >= 48)) for s in sizes]
    pad, label_h = 12, 0
    W = sum(i.width * zoom + pad for i in imgs) + pad
    H = max(i.height for i in imgs) * zoom + pad * 2
    sheet = Image.new("RGBA", (W, H), (246, 245, 242, 255))
    x = pad
    for i in imgs:
        big = i.resize((i.width * zoom, i.height * zoom), Image.NEAREST)
        sheet.alpha_composite(big, (x, pad))
        x += big.width + pad
    sheet.convert("RGB").save(out_path)
    return out_path


def make_web_icons(public_dir: str) -> list:
    """给前端也放一份：favicon + 192/512 的 PNG。

    这三个是**浏览器标签、独立窗口（Edge 应用模式）、任务栏**这几处的图标来源。
    之前只做了 exe 的图标，网页这边一个 link 都没有——标签页和任务栏就只能是
    浏览器的默认图或者被拉伸的小图，看着就是"糊"。
    """
    os.makedirs(public_dir, exist_ok=True)
    out = []
    make_ico(os.path.join(public_dir, "favicon.ico"))
    out.append("favicon.ico")
    for size in (192, 512):
        _draw(size, tile=True).save(os.path.join(public_dir, f"icon-{size}.png"))
        out.append(f"icon-{size}.png")
    return out


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.normpath(os.path.join(here, ".."))
    print("写好:", make_ico(os.path.join(root, "installer", "eggpaper.ico")))
    print("网页图标:", make_web_icons(os.path.join(root, "frontend", "public")))
    print("对照表:", contact_sheet(os.path.join(root, "_qa", "shots", "icon_sheet.png")))

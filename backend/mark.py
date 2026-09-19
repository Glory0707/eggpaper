"""那枚印章标记的几何：**与 EggMark.vue 的基准几何逐数字对齐**，按目标像素直接算。

一处定义，四处共用：`tools/make_icon.py` 生成 exe 图标与网页图标，`desktop.py` 画托盘，
`window.py` 注入窗口/任务栏图标，`tools/check_icon.py` 在栅格上实测。形状的唯一来源是
`frontend/src/components/EggMark.vue` —— 改那边就要改这里（数字一一对应，别凭感觉调）。

**用户拍板的那一版是"三条线"**（主页面左上角那枚）：
椭圆环（上半 ry 34 / 下半 ry 42，钝端朝上）+ 三条字条（宽 30/36/22，末条自然短一截）。
16px 上也得是三条——**条数是标识的一部分，不能因为放不下就减条**；放不下时压薄字条
（缝优先），而不是砍掉一条。这条是踩出来的：上一版按尺寸表 4→3→2 减条，48px 上画成了
四条宽条，整枚看着像个带横纹的方块（用户："又回退了，四角"）。

另外三条纪律（写进代码而不是注释）：

1. **白缝的下限**比笔画粗细更重要——缝糊了整枚就成一坨。`gap_floor()` 管这个。
2. 标记占画布 76/84 = 90%（Vue 的 viewBox 就是 84×84 里放一枚 60×76 的蛋）。
"""
import math

from PIL import Image, ImageChops, ImageDraw

VIEW = 84
ACCENT = (29, 78, 95)
PAPER = (255, 255, 255)

CX, CY = 48.0, 48.0
RX = 30.0
RY_UP, RY_DN = 34.0, 42.0
RING = 8.5
BARS = ((32.0, 30.0), (44.0, 36.0), (56.0, 22.0))
BAR_H = 7.0

RING_MIN = 1.4
BAR_H_MIN = 1.0
GAP_MIN = 0.75
GAP_MIN_RATIO = 0.045

def gap_floor(px: float) -> float:
    """白缝的下限：小尺寸用绝对像素（16px 上 1px 的差别就是成败），大尺寸按比例。"""
    return max(GAP_MIN, min(px * GAP_MIN_RATIO, 3.0))

def layout(px: float) -> dict:
    """算出这枚标记在 px 像素画布上的几何（单位就是像素）。

    返回 box（蛋的外接框）、ring（环宽）、bars（每条的 中心 y / 宽 / 厚）。
    """
    s = px / VIEW
    cx, cy = px * CENTER[0], px * CENTER[1]
    rx, ry_up, ry_dn = RX * s, RY_UP * s, RY_DN * s
    ring = max(RING_MIN, RING * s)
    ys = [cy + (by - CY) * s for by, _ in BARS]
    t = BAR_H * s
    if len(ys) > 1:
        space = min(ys[i] - ys[i - 1] for i in range(1, len(ys)))
        t = min(t, max(BAR_H_MIN, space - gap_floor(px)))
    t = max(BAR_H_MIN, t)
    bars = [{"y": ys[i], "w": max(1.5, w * s), "h": t} for i, (_, w) in enumerate(BARS)]
    return {"box": (cx - rx, cy - ry_up, cx + rx, cy + ry_dn),
            "ring": ring, "bars": bars, "rx": rx, "ry_up": ry_up, "ry_dn": ry_dn, "t": t}

def _egg_poly(cx, cy, rx, ry_up, ry_dn, n=200):
    """蛋的轮廓：上半椭圆 + 下半椭圆（两半共用 rx，所以中间是最宽的平滑过渡）。"""
    pts = []
    for i in range(n + 1):
        a = math.pi - math.pi * i / n
        pts.append((cx + rx * math.cos(a), cy - ry_up * math.sin(a)))
    for i in range(n + 1):
        a = -math.pi * i / n
        pts.append((cx + rx * math.cos(a), cy - ry_dn * math.sin(a)))
    return pts

def _downscale_pm(img: Image.Image, w: int, h: int) -> Image.Image:
    """预乘 alpha 后再 BOX 缩小。

    直接缩 RGBA 会在边缘产生一圈灰边：透明像素的 RGB 是 (0,0,0)，缩小把"白"和
    "透明黑"在圆角处平均，alpha 半透明、RGB 却发灰，合成到任务栏/桌面上就是一圈脏边
    （看着就是"图标糊"）。预乘后颜色按覆盖率加权，缩完再还原，边缘颜色才是对的。
    """
    r, g, b, a = img.split()
    pm = Image.merge("RGBA", (ImageChops.multiply(r, a), ImageChops.multiply(g, a),
                              ImageChops.multiply(b, a), a))
    pm = pm.resize((w, h), Image.BOX)
    r2, g2, b2, a2 = pm.split()
    rp, gp, bp, ap = r2.load(), g2.load(), b2.load(), a2.load()
    out = Image.new("RGBA", (w, h))
    op = out.load()
    for y in range(h):
        for x in range(w):
            av = ap[x, y]
            if av <= 2:
                op[x, y] = (0, 0, 0, 0)
            else:
                k = 255.0 / av
                op[x, y] = (min(255, round(rp[x, y] * k)), min(255, round(gp[x, y] * k)),
                            min(255, round(bp[x, y] * k)), av)
    return out

TILE_RADIUS = 0.20

VB_X, VB_Y = 6.0, 10.0
CENTER = ((CX - VB_X) / VIEW, (CY - VB_Y) / VIEW)

def draw(px: int, tile: bool = False) -> Image.Image:
    """画一枚标记。

    tile=True：垫一层**方形圆角**白底（就是常规应用图标那个样子）。
    用户的最终口径："就要方形的白色垫，只是四个角是圆角"——之前试过"不垫"
    （深色任务栏上墨色看不清）和"垫成跟徽标同形的蛋"（不是他要的），都别再回去。
    """
    ss = 4
    size = px * ss
    cx, cy = px * CENTER[0], px * CENTER[1]
    base = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    if tile:
        ImageDraw.Draw(base).rounded_rectangle([0, 0, size - 1, size - 1],
                                              radius=px * TILE_RADIUS * ss, fill=PAPER)

    g = layout(px)
    mask = Image.new("L", (size, size), 0)
    md = ImageDraw.Draw(mask)
    md.polygon(_egg_poly(cx * ss, cy * ss, g["rx"] * ss, g["ry_up"] * ss, g["ry_dn"] * ss),
               fill=255)
    rin = g["ring"] * ss
    md.polygon(_egg_poly(cx * ss, cy * ss, g["rx"] * ss - rin, g["ry_up"] * ss - rin,
                         g["ry_dn"] * ss - rin), fill=0)
    for b in g["bars"]:
        y, w, h = b["y"] * ss, b["w"] * ss, b["h"] * ss
        md.rounded_rectangle([cx * ss - w / 2, y - h / 2, cx * ss + w / 2, y + h / 2],
                             radius=h / 2, fill=255)

    layer = Image.new("RGBA", (size, size), ACCENT + (0,))
    layer.putalpha(mask)
    return _downscale_pm(Image.alpha_composite(base, layer), px, px)

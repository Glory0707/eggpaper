"""那枚印章标记的几何：**按目标像素尺寸直接算**，不再用 96 视口手调数字。

放在 backend/ 里是为了构建工具和应用共用同一份：`tools/make_icon.py` 用它生成
exe 图标与网页图标，`desktop.py` 用它画托盘图标。形状与
frontend/src/components/EggMark.vue 同源（椭圆环 + 几行字条，末行短一截）。

**小尺寸的判据是"白缝"，不是"笔画够粗"**——这条是踩出来的，写进代码而不是写进注释：

1. 手调的尺寸表很容易出现"字条离环只有 0.75px"这种数字（上一版 24px 的第一条字条
   就压在环的内沿上，两者在栅格上连成一片，整枚图标糊成一坨深色；用户连着说了三次
   "更糊了"）。现在所有缝——字条之间、字条到环——都等于同一个 g，g 由
   "环内高度 / (2n+1)"解出来；放不下就**减字条**（4→3→2），绝不压窄缝。
2. 上一版还把标记画在 96 视口的中间一块（60×68）里，等于自己缩掉三成，于是又有
   "托盘上的图标偏小"。现在标记占画布的 1-2×6% = 88%。
3. `tools/check_icon.py` 会把这些缝在栅格上实测出来（含 ASCII 放大图）。改动之后跑
   一次，缝小于 max(1.6px, 画布 7.5%) 就会报错——这是唯一能防住"越改越糊"的闸门。
"""
from PIL import Image, ImageDraw

VIEW = 96                                   # 画布名义单位（调用方读它当参考）
ACCENT = (29, 78, 95)                       # --accent
PAPER = (255, 255, 255)

RATIO = 0.79                                # 蛋的宽/高（品牌椭圆 60×76）
MARGIN = 0.06                               # 四周留白占画布比例
RING_RATIO = 0.085                          # 环宽 = 蛋高的这个比例（品牌 6/76）
RING_MIN = 1.5                              # 环宽下限（px）
BAR_MIN = 1.5                               # 字条厚度下限（px）
THICK = 0.88                                # 字条厚度 / 缝宽（留一点余量给抗锯齿）
GAP_MIN = 1.6                               # 白缝下限（px）
GAP_MIN_RATIO = 0.075                       # 白缝下限（占画布比例）
BARS_BY_SIZE = ((56, 4), (40, 4), (26, 3), (0, 2))   # 尺寸 ≥ 此值 → 用几根字条


def bars_for(px: float, inner_h: float) -> int:
    """这个尺寸放几根字条：先按尺寸取档，再按"缝够不够宽"往下减。"""
    n = 2
    for lo, cnt in BARS_BY_SIZE:
        if px >= lo:
            n = cnt
            break
    while n > 1 and inner_h / (n * THICK + n + 1) < gap_floor(px):
        n -= 1
    return n


def gap_floor(px: float) -> float:
    """白缝的下限：小尺寸用绝对像素（1px 的差别就是在 16px 上决定成败的那种），
    大尺寸按比例（3px 以上已经足够，再往上放大没有意义）。"""
    return max(GAP_MIN, min(px * GAP_MIN_RATIO, 3.0))


def layout(px: float) -> dict:
    """算出这枚标记在 px 像素画布上的几何（单位就是像素）。

    返回 box（环的外接框）、ring（环宽）、bars（每根字条的 中心 y / 宽 / 厚）。
    字条宽度按椭圆在**该高度**的内接宽度收，所以末行自然短一截，也不会顶到环。
    """
    m = max(1.0, px * MARGIN)
    eh = px - 2 * m                          # 蛋的外高
    ew = eh * RATIO                          # 蛋的外宽
    w = max(RING_MIN, eh * RING_RATIO)       # 环宽
    inner_h = eh - 2 * w
    n = bars_for(px, inner_h)
    gap = inner_h / (n * THICK + n + 1)      # 缝：解 n*THICK*gap + (n+1)*gap = inner_h
    thick = max(BAR_MIN, gap * THICK)        # 字条比缝略窄，抗锯齿吃掉的那点由它补
    top = (px - eh) / 2 + w
    # 环中心线的半轴；字条在不碰到环的前提下尽量宽（0.94 是留给抗锯齿的余量）
    a = (ew - w) / 2
    b = (eh - w) / 2
    bars = []
    for i in range(n):
        cy = top + (i + 1) * gap + i * thick + thick / 2
        # 宽度按字条**离中心最远的那条边**来算：靠椭圆两端时内接宽度收得很快，
        # 用中心线算会让字条的一角顶进环里
        dy = min(0.92, (abs(cy - px / 2) + thick / 2) / b)
        half = (a * (1 - dy * dy) ** 0.5 - w / 2) * 0.94
        bars.append((cy, max(2.0, half * 2), thick))
    return {"box": ((px - ew) / 2, (px - eh) / 2, (px + ew) / 2, (px + eh) / 2),
            "ring": w, "bars": bars}


def _downscale_pm(img: Image.Image, w: int, h: int) -> Image.Image:
    """预乘 alpha 后再 LANCZOS 缩小。

    直接缩 RGBA 会在边缘产生一圈灰边：透明像素的 RGB 是 (0,0,0)，缩小把"白"和
    "透明黑"在圆角处平均，alpha 半透明、RGB 却发灰，合成到任务栏/桌面上就是一圈脏边
    （看着就是"图标糊"）。预乘后颜色按覆盖率加权，缩完再还原，边缘颜色才是对的。
    """
    from PIL import ImageChops
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


def draw(px: int, tile: bool = False) -> Image.Image:
    """画一枚标记。tile=True 时垫一层纸色圆角底（深色地方也看得见）。"""
    ss = 4                                   # 超采样再缩，边缘才干净
    size = px * ss
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    g = layout(px)
    if tile:
        d.rounded_rectangle([0, 0, size - 1, size - 1], radius=size // 5, fill=PAPER)
    x0, y0, x1, y1 = (v * ss for v in g["box"])
    d.ellipse([x0, y0, x1, y1], outline=ACCENT, width=max(2, round(g["ring"] * ss)))
    rw = g["ring"] * ss
    for cy, bw, th in g["bars"]:
        x = bw * ss / 2
        y = th * ss / 2
        d.rounded_rectangle([size / 2 - x, cy * ss - y, size / 2 + x, cy * ss + y],
                            radius=min(x, y), fill=ACCENT)
    return _downscale_pm(img, px, px)

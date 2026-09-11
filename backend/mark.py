"""那枚印章标记的绘制：每个尺寸一套几何。

放在 backend/ 里是为了**构建工具和应用共用同一份**：
`tools/make_icon.py` 用它生成 exe 图标与网页图标，`desktop.py` 用它画托盘图标。

为什么每个尺寸单独定几何：所有尺寸都从"按 96 视口画的那一份"缩下来时，16px 上
环只剩 1.4px、字条剩 1.2px，落成一团灰。小尺寸要**加粗环、减字条**才立得住。
形状与 frontend/src/components/EggMark.vue 同源（椭圆环 + 几行字条，末行短一截）。
"""
from PIL import Image, ImageDraw

VIEW = 96                                   # 与 SVG 的 viewBox 同尺度
ACCENT = (29, 78, 95)                       # --accent
PAPER = (255, 255, 255)

# 「壮」的那一版：画满画布（椭圆撑到边）、环更粗、字条减到两条。
# 16/24px 用它——这两个尺寸上"照比例缩"的结果是环 2px、字条 1.6px，落下来就是一团糊。
# 托盘、favicon 的小尺寸、exe 的小尺寸都用它（用户在标题栏/任务栏/tab 上看到的就是这些）。
BOLD = {
    16: (15.0, [(36, 46), (58, 34)], 12.0),
    20: (14.0, [(35, 45), (57, 33)], 11.0),
    24: (14.0, [(34, 44), (56, 32)], 11.0),
    32: (13.0, [(34, 40), (56, 28)], 10.0),
}
# 尺寸 → (环宽, [(字条 y, 宽)], 字条高)。32px 以上细节吃得下，按正常比例来。
SPEC = {
    40: (8.0, [(32, 32), (44, 38), (56, 36), (67, 20)], 6.5),
    48: (7.0, [(31, 34), (43, 40), (55, 38), (66, 20)], 6.0),
}
FULL = (6.0, [(30, 36), (42, 42), (54, 40), (66, 22)], 5.5)   # 128/256 用完整稿
FULL_BOX = (18, 14, 78, 82)          # 正常比例时的椭圆框（96 视口）
BOLD_BOX = (5, 2, 91, 94)            # 画满时撑到边


def spec_for(px: int, tray: bool = False):
    """这套尺寸用哪一版几何 + 椭圆框。

    32px 以下一律走 BOLD（画满、环粗、两条字条）：这几个尺寸是"标题栏 / 任务栏 / 托盘 /
    tab"用的，读者只看一眼，细节留不住，**形状壮不壮**才是关键。32px 以上细节吃得下。
    """
    if px <= 32:                        # 小尺寸一律"壮"的那版（含托盘与任务栏）
        key = min(BOLD, key=lambda k: abs(k - px))
        return BOLD[key], BOLD_BOX
    for k in sorted(SPEC):
        if px <= k:
            return SPEC[k], FULL_BOX
    return FULL, FULL_BOX


def draw(px: int, tile: bool = False, tray: bool = False) -> Image.Image:
    """画一枚标记。tile=True 时垫一层纸色圆角底（深色任务栏上才看得见）。"""
    ss = 4
    size = px * ss
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    k = size / VIEW
    (w, bars, h), box = spec_for(px, tray)
    if tile:
        d.rounded_rectangle([0, 0, size - 1, size - 1], radius=size // 5, fill=PAPER)
    d.ellipse([box[0] * k, box[1] * k, box[2] * k, box[3] * k],
              outline=ACCENT, width=max(2, round(w * k)))
    for y, bw in bars:
        d.rounded_rectangle([(48 - bw / 2) * k, (y - h / 2) * k,
                             (48 + bw / 2) * k, (y + h / 2) * k],
                            radius=h * k / 2, fill=ACCENT)
    return img.resize((px, px), Image.LANCZOS)

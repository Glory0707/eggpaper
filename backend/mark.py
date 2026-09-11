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

# 尺寸 → (环宽, [(字条 y, 宽)], 字条高)
SPEC = {
    16: (13.0, [(34, 26), (54, 16)], 10.0),          # 只留两条、环最粗
    24: (11.0, [(32, 30), (46, 34), (60, 18)], 8.0),
    32: (9.0, [(32, 30), (45, 36), (58, 20)], 7.0),
    48: (7.0, [(31, 34), (43, 40), (55, 38), (66, 20)], 6.0),
}
FULL = (6.0, [(30, 36), (42, 42), (54, 40), (66, 22)], 5.5)   # 128/256 用完整稿
# 托盘专用：画满画布（托盘只给 16~20px，留白等于把自己缩小）
TRAY_SPEC = {
    16: (15.0, [(36, 46), (58, 34)], 12.0),
    24: (14.0, [(34, 44), (56, 32)], 11.0),
    32: (15.0, [(36, 46), (58, 34)], 12.0),
}


def spec_for(px: int, tray: bool = False):
    table = TRAY_SPEC if tray else SPEC
    for k in sorted(table):
        if px <= k:
            return table[k]
    return FULL if not tray else TRAY_SPEC[32]


def draw(px: int, tile: bool = False, tray: bool = False) -> Image.Image:
    """画一枚标记。tile=True 时垫一层纸色圆角底（深色任务栏上才看得见）。"""
    ss = 4
    size = px * ss
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    k = size / VIEW
    w, bars, h = spec_for(px, tray)
    if tray:                                 # 托盘：椭圆撑到边
        box = (5, 2, 91, 94)
    else:
        box = (18, 14, 78, 82)
    if tile:
        d.rounded_rectangle([0, 0, size - 1, size - 1], radius=size // 5, fill=PAPER)
    d.ellipse([box[0] * k, box[1] * k, box[2] * k, box[3] * k],
              outline=ACCENT, width=max(2, round(w * k)))
    for y, bw in bars:
        d.rounded_rectangle([(48 - bw / 2) * k, (y - h / 2) * k,
                             (48 + bw / 2) * k, (y + h / 2) * k],
                            radius=h * k / 2, fill=ACCENT)
    return img.resize((px, px), Image.LANCZOS)

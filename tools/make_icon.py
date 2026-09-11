"""从 EggMark.vue 的几何生成应用图标（多尺寸 PNG → .ico / .icns 不适用）。

印章的形状就写在 EggMark.vue 里（一个椭圆环 + 四行字条）。这里把同一份几何
用 Pillow 重画一遍——**不截屏、不依赖浏览器**，构建时就地生成，形状与界面上那枚一致。
16px 用简化稿（环加粗、三条字条），和组件里的 compact 同一个道理：小尺寸画七八条会糊。
"""
import os

from PIL import Image, ImageDraw

SIZE = 96                  # 和 SVG 的视图尺度一致
FULL = [(30, 36), (42, 42), (54, 40), (66, 22)]
SMALL = [(32, 30), (44, 36), (56, 22)]


def _draw(px: int, color, bg=None) -> Image.Image:
    ss = 4                                     # 超采样，缩到 16px 才不毛
    img = Image.new("RGBA", (px * ss, px * ss), bg or (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    k = px * ss / SIZE
    small = px <= 32
    bars, h = (SMALL, 7) if small else (FULL, 5.5)
    w = 8.5 if small else 6                    # 环的粗细：小了要加粗才看得见
    box = [18 * k, 14 * k, 78 * k, 82 * k]
    d.ellipse(box, outline=color, width=max(1, round(w * k)))
    for y, bw in bars:
        d.rounded_rectangle([(48 - bw / 2) * k, (y - h / 2) * k,
                             (48 + bw / 2) * k, (y + h / 2) * k],
                            radius=h * k / 2, fill=color)
    return img.resize((px, px), Image.LANCZOS)


def make_ico(out_path: str, accent=(29, 78, 95), paper=(255, 255, 255)) -> str:
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    sizes = [16, 24, 32, 48, 64, 128, 256]
    # 大尺寸用墨色印章压在纸色圆角上；小尺寸直接用墨色（16px 上看不出底色）
    imgs = []
    for s in sizes:
        if s >= 48:
            base = Image.new("RGBA", (s * 4, s * 4), (0, 0, 0, 0))
            d = ImageDraw.Draw(base)
            d.rounded_rectangle([0, 0, s * 4 - 1, s * 4 - 1], radius=s * 4 // 5, fill=paper)
            mark = _draw(s * 4, accent)
            base.alpha_composite(mark)
            imgs.append(base.resize((s, s), Image.LANCZOS))
        else:
            imgs.append(_draw(s, accent))
    imgs[0].save(out_path, format="ICO", sizes=[(s, s) for s in sizes],
                 append_images=imgs[1:])
    # 顺手留一张 256 的 png：Inno Setup 的向导图标要 png/bmp
    imgs[-1].save(out_path.replace(".ico", "-256.png"))
    return out_path


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    out = os.path.join(here, "..", "installer", "eggpaper.ico")
    print("写好:", make_ico(os.path.normpath(out)))

"""给图标几何上闸门：把每个尺寸的**白缝**在栅格上量出来。

上一版连糊三次的根因是"手调的尺寸表里，字条到环的缝只有 0.75px"——这种事肉眼
在屏幕上看不出来，但在栅格上一量就现形。所以这里把判据变成数字：

- 沿中心列量竖直方向的透明段（>= 半透明阈值算"看上去还是纸"），取最短的一段（不含画布外沿的留白）
- 缝必须 >= max(1.6px, 画布 7.5%)，否则退出码 1

顺带印 ASCII 放大图 + 存一张最近邻 8 倍的对照表，改完几何扫一眼就知道。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))
import mark  # noqa: E402
from PIL import Image  # noqa: E402

SIZES = (16, 20, 24, 32, 40, 48, 64)
ALPHA_PAPER = 90          # 低于这个不透明度就算"看得见的墨"


def gaps(px: int):
    """返回 (最小的内部缝, 各段缝)。缝 = 中心列上连续的"近透明"像素。"""
    a = mark.draw(px).split()[3]
    cx = px // 2
    col = [a.getpixel((cx, y)) for y in range(px)]
    runs, run = [], 0
    for v in col:
        if v < ALPHA_PAPER:
            run += 1
        elif run:
            runs.append(run)
            run = 0
    if run:
        runs.append(run)
    runs = runs[1:-1] if len(runs) > 2 else []      # 掐掉画布上下沿的留白
    return (min(runs) if runs else 0), runs


def art(px: int, z: int = 1):
    a = mark.draw(px).split()[3]
    return "\n".join("".join(" .:-=+*#%@"[min(9, a.getpixel((x, y)) * 10 // 256)]
                             for x in range(px)) for y in range(px))


def main():
    bad = []
    for s in SIZES:
        g, runs = gaps(s)
        need = mark.gap_floor(s)
        ok = g >= need
        geo = mark.layout(s)
        print(f"{s:>3}px  缝 {g:>2}px (需 >= {need:.1f})  环 {geo['ring']:.1f}px  "
              f"字条 {len(geo['bars'])} 根/{geo['bars'][0][2]:.1f}px  段长 {runs}"
              f"  {'ok' if ok else '**太窄**'}")
        if not ok:
            bad.append(s)
    for s in (16, 24):
        print(f"--- {s}px 放大 ---\n{art(s)}")
    here = os.path.dirname(os.path.abspath(__file__))
    shots = os.path.join(here, "..", "_qa", "shots")
    os.makedirs(shots, exist_ok=True)
    imgs = [mark.draw(s, tile=(s >= 48)) for s in SIZES]
    Z = 10
    W = sum(i.width * Z + 12 for i in imgs) + 12
    H = max(i.height for i in imgs) * Z + 24
    sheet = Image.new("RGBA", (W, H), (238, 240, 241, 255))
    x = 12
    for i in imgs:
        big = i.resize((i.width * Z, i.height * Z), Image.NEAREST)
        sheet.alpha_composite(big, (x, 12))
        x += big.width + 12
    sheet.convert("RGB").save(os.path.join(shots, "icon_px.png"))
    print("对照表:", os.path.join(shots, "icon_px.png"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

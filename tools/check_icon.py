"""给图标几何上闸门：把每个尺寸的**白缝**与**条数**在栅格上量出来。

上一版连糊三次的根因是"手调的尺寸表里，字条到环的缝只有 0.75px"——这种事肉眼
在屏幕上看不出来，但在栅格上一量就现形。所以这里把判据变成数字：

- 沿中心列量竖直方向的**墨色段**（接近 accent 且不透明），段与段之间的白就是"缝"，
  取最短的一段（不含蛋形上下沿外面的空白）
- 缝必须 >= mark.gap_floor(px)（最小档 0.75px、大尺寸按 4.5% 且 3px 封顶），否则退出码 1
- **条数必须恒为 3**：三条线是品牌标识（用户拍板的那一版），放不下就压薄字条而不是减条；
  曾经按尺寸表减到 2 条、又在 48px 上画成 4 条 → 用户："又回退了，四角"
- **白底是"方形 + 圆角"**（用户最终口径："就要方形的白色垫，只是四个角是圆角"）：
  闸门查两件事——**四角必须透明**（圆角真的生效，不是硬方角）+ **上沿中点必须不透明**
  （白底确实垫上了，别又变成透明底）。

顺带印 ASCII 放大图 + 存一张最近邻放大对照表，改完几何扫一眼就知道。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))
import mark  # noqa: E402
from PIL import Image  # noqa: E402

SIZES = (16, 20, 24, 32, 40, 48, 64)
ALPHA_INK = 90            # 低于这个不透明度就不算墨
ACCENT_TOL = 70           # 与 accent 的颜色容差（判定"这一段是墨"）


def _is_ink(p) -> bool:
    r, g, b, a = p
    return (a > ALPHA_INK and abs(r - mark.ACCENT[0]) < ACCENT_TOL
            and abs(g - mark.ACCENT[1]) < ACCENT_TOL and abs(b - mark.ACCENT[2]) < ACCENT_TOL)


def gaps(px: int):
    """返回 (最小的内部缝, 各段缝)，单位是像素但**带小数**。

    在 4 倍尺寸上量再除回去：按 1:1 数像素只能得到整数（24px 上真实缝是 1.43px，数出来
    是 1），那样闸门会把没问题的设计判成"太窄"——这一版三条线的缝天生比上一版紧
    （设计就是 条 7 : 缝 5），必须能量到小数才谈得上判据。
    """
    big = mark.draw(px * 4, tile=True)
    cx = px * 2
    col = [big.getpixel((cx, y)) for y in range(px * 4)]
    runs, run = [], 0
    for c in col:
        if not _is_ink(c):
            run += 1
        elif run:
            runs.append(run / 4.0)
            run = 0
    if run:
        runs.append(run / 4.0)
    runs = runs[1:-1] if len(runs) > 2 else []      # 掐掉蛋形上下沿外面的空白
    return (min(runs) if runs else 0.0), runs


def art(px: int) -> str:
    """ASCII 放大图：@ = 墨，. = 白纸，空 = 透明。"""
    im = mark.draw(px, tile=True)

    def ch(x, y):
        c = im.getpixel((x, y))
        if c[3] < 60:
            return " "
        return "@" if _is_ink(c) else "."

    return "\n".join("".join(ch(x, y) for x in range(px)) for y in range(px))


def audit_asset(path: str):
    """查一件真产物：四角必须是透明的（白底得是蛋形），中心列上必须是"环 + 三条线"。"""
    img = Image.open(path)
    try:
        img.size = (48, 48)          # ICO：挑 48 那一帧（PNG 会抛 AttributeError）
    except AttributeError:
        pass
    img = img.convert("RGBA")
    if img.size != (48, 48):
        return False, f"{path}: 不是 48×48（{img.size}），闸门没法量"
    opaque = sum(1 for p in ((1, 1), (46, 1), (1, 46), (46, 46)) if img.getpixel(p)[3] > 200)
    edge = img.getpixel((24, 1))[3] > 200          # 上沿中点：白底在不在
    runs, run, prev = [], 0, None
    for y in range(48):
        cur = _is_ink(img.getpixel((24, y)))
        if prev is None or cur == prev:
            run += 1
        else:
            runs.append((prev, run)); run = 1
        prev = cur
    runs.append((prev, run))
    bars = [n for v, n in runs if v][1:-1]              # 掐掉环的上下两段
    ok = opaque == 0 and edge and len(bars) == 3
    return ok, (f"{path}: 四角不透明 {opaque}/4（须 0 = 圆角生效）"
                f"  上沿白底 {'在' if edge else '**没有**'}"
                f"  中间墨段 {len(bars)}（须 3 = 三条线）  {'ok' if ok else '**不合格**'}")


def main():
    bad = []
    for s in SIZES:
        g, runs = gaps(s)
        need = mark.gap_floor(s)
        ok = g >= need
        geo = mark.layout(s)
        n_ok = len(geo["bars"]) == 3
        if not ok or not n_ok:
            bad.append(f"{s}px")
        print(f"{s:>3}px  缝 {g:.2f}px (需 >= {need:.2f})  环 {geo['ring']:.1f}px  "
              f"字条 {len(geo['bars'])} 根/{geo['bars'][0]['h']:.1f}px  段长 "
              f"{[round(r, 2) for r in runs]}"
              f"  {'ok' if ok else '**太窄**'}{'' if n_ok else ' **条数不是 3**'}")
    for s in (16, 24):
        print(f"--- {s}px 放大（@=墨 .=纸）---\n{art(s)}")

    here = os.path.dirname(os.path.abspath(__file__))
    # 真·产物也要过一遍：几何对了，但生成出来的 ICO / 网页 PNG 可能被别处又垫上方角底
    # （上一版就是在 make_icon 里给每一档垫了纸色方角底，几何闸门一点都没拦住）。
    for rel in ("../installer/eggpaper.ico", "../frontend/public/icons/icon-48.png"):
        fp = os.path.join(here, rel)
        if not os.path.exists(fp):
            print(f"{rel}: 还没生成，跳过（跑 tools/make_icon.py）")
            continue
        ok, msg = audit_asset(fp)
        print(msg)
        if not ok:
            bad.append(rel)

    shots = os.path.join(here, "..", "_qa", "shots")
    os.makedirs(shots, exist_ok=True)
    imgs = [mark.draw(s, tile=True) for s in SIZES]     # 与真实产物一致：蛋形白底
    Z = 10
    W = sum(i.width * Z + 12 for i in imgs) + 12
    H = max(i.height for i in imgs) * Z + 24
    sheet = Image.new("RGBA", (W, H), (110, 110, 110, 255))    # 灰底：白边看得清
    x = 12
    for i in imgs:
        big = i.resize((i.width * Z, i.height * Z), Image.NEAREST)
        sheet.alpha_composite(big, (x, 12))
        x += big.width + 12
    sheet.convert("RGB").save(os.path.join(shots, "icon_px.png"))
    print("对照表:", os.path.join(shots, "icon_px.png"))
    if bad:
        print("不合格：", bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

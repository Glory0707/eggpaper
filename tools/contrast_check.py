"""对比度验算：三档护眼底纹 × 三级墨，以及"纸面洗色后黑字还剩多少对比度"。

改配色（frontend/src/styles.css 里的 --paper / --paper-deep / --card / --card-2 /
--ink* / html[data-care] 那几段）之后跑一遍，全部 ≥4.5:1 才算过。

    python tools/contrast_check.py
"""


def hex2rgb(h):
    h = h.lstrip('#')
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def lum(rgb):
    def ch(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ratio(a, b):
    la, lb = lum(hex2rgb(a)), lum(hex2rgb(b))
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def wash(base, thin, alpha):
    """multiply 洗色：结果 = 底 × ((1-α) + α·色膜)"""
    b, t = hex2rgb(base), hex2rgb(thin)
    return '#%02x%02x%02x' % tuple(round(b[i] * ((1 - alpha) + alpha * t[i] / 255)) for i in range(3))


INK, INK2, INK3 = '#1d1b17', '#55524a', '#6b675e'

THEMES = {
    'off 纯白': ('#ffffff', '#f6f6f5', '#ffffff', '#ffffff', None),
    'mung 豆沙绿': ('#e4f2e6', '#daedde', '#eaf6ec', '#f5fbf6', ('#c7edcc', 0.9)),
    'cyan 浅青绿': ('#e2f2f2', '#d8ebeb', '#e9f6f6', '#f5fbfb', ('#cce8e8', 0.9)),
    'sand 米黄': ('#f6f6ea', '#efefe0', '#fafaf2', '#fdfef9', ('#f5f5dc', 0.9)),
}

# 皮肤（蛋仔）：换的是底、墨、主色三组值，所以要单独验一遍——
# 暖底最容易翻车的地方就是"深墨看着够深，其实压不到 4.5"
SKINS = {
    'egg 蛋仔': ('#fdf9f1', '#f8f2e6', '#fffdf8', '#fffdf9', '#241d15', '#5b5044', '#6f6455'),
}

print(f'{"底纹":<12}{"桌面":>7}{"沟槽":>7}{"面板":>7}{"浮层":>7}   {"纸面":>8}{"黑字/纸面":>10}')
worst = 99
for name, (paper, deep, card, card2, w) in THEMES.items():
    cells = []
    for bg in (paper, deep, card, card2):
        cells.append(min(ratio(INK, bg), ratio(INK2, bg), ratio(INK3, bg)))
    paper_col = wash('#ffffff', *w) if w else '#ffffff'
    page_ratio = ratio('#000000', paper_col)
    worst = min(worst, page_ratio, *cells)
    print(f'{name:<12}' + ''.join(f'{c:>7.2f}' for c in cells) +
          f'   {paper_col:>8}{page_ratio:>10.2f}')

print(f'\n每组取最差（INK/INK2/INK3 里最低的那项）：{worst:.2f}  '
      f'{"✓ 全部 ≥4.5:1" if worst >= 4.5 else "✗ 有不达标"}')

for name, (paper, deep, card, card2, i1, i2, i3) in SKINS.items():
    cells = [min(ratio(i1, bg), ratio(i2, bg), ratio(i3, bg)) for bg in (paper, deep, card, card2)]
    w = min(cells)
    worst = min(worst, w)
    print(f'{name:<12}' + ''.join(f'{c:>7.2f}' for c in cells) +
          f'   {"（无洗色）":>8}{ratio("#000000", paper):>10.2f}  最差 {w:.2f}'
          f' {"✓" if w >= 4.5 else "✗"}')

print('\n细项（off 与 mung 逐项）：')
for name in ('off 纯白', 'mung 豆沙绿'):
    paper, deep, card, card2, w = THEMES[name]
    print(f'  [{name}]')
    for label, bg in (('桌面', paper), ('沟槽', deep), ('面板', card), ('浮层', card2)):
        print(f'    {label}: ink {ratio(INK, bg):.2f} · ink-2 {ratio(INK2, bg):.2f} · ink-3 {ratio(INK3, bg):.2f}')
    if w:
        pc = wash('#ffffff', *w)
        print(f'    纸面 {pc}: 正文黑 {ratio("#000000", pc):.2f}')


# ---------- 强调色 ----------
# 主按钮是白字压在强调色上，标记是强调色压在白纸/三档护眼纸上——两头都得过线。
# 换色先看这一段：旧的琥珀 #d08a1c 白字压上去只有 2.86:1，主按钮一直是不达标的。
ACCENT, ACCENT_DEEP = '#1d4e5f', '#123a47'
print('\n强调色（靛青）：')
for name, bg in (('白纸', '#ffffff'), ('沟槽', '#f6f6f5'), ('豆沙绿纸', '#cdefd1'),
                 ('浅青绿纸', '#d1eaea'), ('米黄纸', '#f6f6e0')):
    print(f'  {name:<9} 强调色 {ratio(ACCENT, bg):>5.2f}   深档 {ratio(ACCENT_DEEP, bg):>5.2f}')
print(f'  白字压强调色 {ratio("#ffffff", ACCENT):.2f}（主按钮）')
print(f'  参照：白字压旧琥珀 #d08a1c 只有 {ratio("#ffffff", "#d08a1c"):.2f}——这是换色的硬理由')

# 蛋仔皮肤的强调色：暖橙比靛青难达标，白字压上去必须仍然 ≥4.5
EGG_ACCENT, EGG_ACCENT_DEEP = '#a8481a', '#7d3312'
print('\n蛋仔皮肤强调色（暖橙）：')
for name, bg in (('皮肤纸', '#fdf9f1'), ('皮肤沟槽', '#f8f2e6')):
    print(f'  {name:<9} 强调色 {ratio(EGG_ACCENT, bg):>5.2f}   深档 {ratio(EGG_ACCENT_DEEP, bg):>5.2f}')
print(f'  白字压强调色 {ratio("#ffffff", EGG_ACCENT):.2f}（主按钮）  '
      f'{"✓" if ratio("#ffffff", EGG_ACCENT) >= 4.5 else "✗"}')

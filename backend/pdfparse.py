"""PDF 版面解析：按行重建段落（支持双栏）、非正文区检测（参考文献/致谢/补充材料）、图注识别、坐标锚定。"""
import re
import statistics

import pymupdf

WATERMARK = re.compile(r"^arXiv:\S+\s")
PURE_NUM = re.compile(r"^\d{1,4}$")
CAPTION = re.compile(r"^(fig|figure|table|scheme|图|表)\.?\s*[0-9IVXS]+\.?", re.I)
SECTION_HEAD = re.compile(r"^(I|II|III|IV|V|VI|VII|VIII|IX|X)+\.?\s+[A-Z]")

# 各类"区域边界"标题：norm(去空格小写) 前缀匹配
HEAD_REFS = ("references", "bibliography")
HEAD_STOP = ("supplementalmaterial", "supplementarymaterial", "supportinginformation", "appendixsupp")
HEAD_NONBODY = ("acknowledg", "funding", "authorcontrib", "conflictofinterest", "dataavailab", "citedata")
AFFIL = re.compile(r"(University|Laboratory|Institute|Department|College|Academy|School of)")


def _clean(text: str) -> str:
    text = re.sub(r"(\w)-\s+(\w)", r"\1\2", text)     # 行尾断词
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _norm(text: str) -> str:
    return re.sub(r"[^a-z\u4e00-\u9fff]", "", text.lower())


def _heading_kind(text: str):
    """行文本 → 区域边界类型；None=普通行。"""
    if len(text) > 60:
        return None
    n = _norm(text)
    if n.startswith(HEAD_REFS):
        return "refs"
    if n.startswith(HEAD_STOP):
        return "stop"
    if n.startswith(HEAD_NONBODY):
        return "nonbody"
    return None


def extract_title(path: str) -> str:
    doc = pymupdf.open(path)
    try:
        page = doc[0]
        lines = []
        for b in page.get_text("dict")["blocks"]:
            if b["type"] != 0:
                continue
            for line in b["lines"]:
                t = _clean(" ".join(s["text"] for s in line["spans"]))
                size = max((s["size"] for s in line["spans"]), default=0)
                if t:
                    lines.append({"text": t, "size": size, "y": line["bbox"][1]})
        if not lines:
            return ""
        body = [l for l in lines if not WATERMARK.match(l["text"])]
        top = [l for l in body if l["y"] < page.rect.height * 0.45] or body
        max_size = max(l["size"] for l in top)
        cand = [l for l in top if l["size"] >= max_size - 1.6]
        cand.sort(key=lambda l: l["y"])
        return _clean(" ".join(l["text"] for l in cand))[:180]
    finally:
        doc.close()


def _page_lines(page: pymupdf.Page):
    lines = []
    for b in page.get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        for line in b["lines"]:
            spans = [s for s in line["spans"] if s["text"].strip()]
            if not spans:
                continue
            text = _clean(" ".join(s["text"] for s in spans))
            if not text:
                continue
            main_size = statistics.median(s["size"] for s in spans)
            lines.append({"text": text, "bbox": line["bbox"], "size": main_size})
    return lines


def extract_paragraphs(path: str) -> list:
    """返回 [{idx, page, bbox, text, in_refs, caption}]，idx 从 1 开始。"""
    doc = pymupdf.open(path)
    paras = []
    in_refs = False          # 参考文献区/致谢区（非正文）
    reached_supplement = False
    try:
        for pno in range(len(doc)):
            if reached_supplement:
                break
            page = doc[pno]
            ph, pw = page.rect.height, page.rect.width
            raw = []
            for ln in _page_lines(page):
                if WATERMARK.match(ln["text"]) or PURE_NUM.match(ln["text"]):
                    continue
                if ln["bbox"][3] < 55 or ln["bbox"][1] > ph - 55:
                    continue
                raw.append(ln)
            if not raw:
                continue

            body_size = statistics.median(l["size"] for l in raw)
            # 跨栏整宽的行（标题/摘要等）单独成组，避免被分栏切碎
            wide = [l for l in raw if l["bbox"][0] < pw * 0.42 and l["bbox"][2] > pw * 0.58]
            rest = [l for l in raw if l not in wide]
            left = [l for l in rest if l["bbox"][0] < pw * 0.5 - 20]
            right = [l for l in rest if l["bbox"][0] >= pw * 0.5 - 20]
            if left and right and len(right) >= 4:
                columns = [left, right]
                if wide:
                    # 与正文列的中位起始位置比较，决定宽行组（标题/摘要区）读序
                    col_median_y = statistics.median(l["bbox"][1] for l in left + right)
                    wide_max_y = max(l["bbox"][3] for l in wide)
                    columns = ([wide] + columns) if wide_max_y < col_median_y else (columns + [wide])
            else:
                columns = [raw]

            for col in columns:
                col.sort(key=lambda l: (l["bbox"][1], l["bbox"][0]))
                if not col:
                    continue
                ys = sorted(l["bbox"][1] for l in col)
                pitch = statistics.median(b - a for a, b in zip(ys, ys[1:]) if 3 < b - a < 30) or 12

                cur = {"lines": [col[0]]}
                groups = []
                for prev, ln in zip(col, col[1:]):
                    hk = _heading_kind(ln["text"])
                    if hk:
                        groups.append(cur)
                        cur = None
                        if hk == "stop":
                            reached_supplement = True
                        else:
                            in_refs = True
                        break
                    gap = ln["bbox"][1] - prev["bbox"][3]
                    same_style = abs(ln["size"] - cur["lines"][0]["size"]) < 1.2
                    if gap > pitch * 0.55 or not same_style:
                        groups.append(cur)
                        cur = {"lines": [ln]}
                    else:
                        cur["lines"].append(ln)
                if cur is not None:
                    groups.append(cur)
                if reached_supplement:
                    break

                for g in groups:
                    if g is None:
                        continue
                    ls = g["lines"]
                    first = ls[0]["text"]
                    hk = _heading_kind(first)
                    if hk:
                        if hk == "stop":
                            reached_supplement = True
                            break
                        in_refs = True
                        continue
                    text = " ".join(l["text"] for l in ls)
                    size = statistics.median(l["size"] for l in ls)
                    bbox = {"x0": min(l["bbox"][0] for l in ls), "y0": min(l["bbox"][1] for l in ls),
                            "x1": max(l["bbox"][2] for l in ls), "y1": max(l["bbox"][3] for l in ls)}
                    # 行级坐标也带上：页边引文要按句子画线，只有段落框就只能整段涂，
                    # 划线会盖住没被引用的字。前端拿它把引文对回具体哪几行。
                    lines = [{"text": l["text"], "bbox": {"x0": l["bbox"][0], "y0": l["bbox"][1],
                                                          "x1": l["bbox"][2], "y1": l["bbox"][3]}}
                             for l in ls]
                    caption = bool(CAPTION.match(first))
                    if caption:
                        if len(text.split()) < 6:
                            continue
                        paras.append({"page": pno, "bbox": bbox, "text": _clean(text), "lines": lines,
                                      "in_refs": False, "caption": True})
                        continue
                    if in_refs or len(text.split()) < 14 or size > body_size + 2.2:
                        continue
                    # 作者/机构块：机构关键词密集且不长
                    if len(AFFIL.findall(text)) >= 2 and len(text.split()) < 60:
                        continue
                    paras.append({"page": pno, "bbox": bbox, "text": _clean(text), "lines": lines,
                                  "in_refs": False, "caption": False})
    finally:
        doc.close()
    for i, p in enumerate(paras, 1):
        p["idx"] = i
    return paras

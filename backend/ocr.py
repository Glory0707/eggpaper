"""扫描件的文字层补齐：RapidOCR（PP-OCR 中英混合模型跑在 onnxruntime 上）。

为什么选它：一个模型中英文通吃（中文印刷体 98%+），CPU 就够，Apache 2.0，
模型随包分发不用用户下载——代价是安装包大几十 MB。

设计上它是导入管线的**隐形后半段**：文字层缺失的 PDF 在导入时自动进这条线，
识别完段落照常入库、析读自动排队，前端不需要任何新界面。引擎懒加载（首次
调用才载模型），失败向上抛人话，由调用方落到 analysis_error。

坐标约定：模型在渲染位图上识别，这里把全部框除以渲染倍率**还原到 PDF 坐标**，
和 pdfparse 产出的段落/行同一坐标系，框选、眉批锚点才能对得上。
"""
import re
import statistics
import threading

_DPI = 200
_ZOOM = _DPI / 72.0
_SCORE_MIN = 0.5

_engine = None
_engine_lock = threading.Lock()


class OcrUnavailable(Exception):
    """引擎没装好——导入方据此退回"能读原文"的现状。"""


def available() -> bool:
    try:
        import rapidocr_onnxruntime  # noqa: F401
        return True
    except Exception:
        return False


def _get_engine():
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                try:
                    from rapidocr_onnxruntime import RapidOCR
                except Exception as e:
                    raise OcrUnavailable(f"OCR 引擎没装好：{e}")
                _engine = RapidOCR()
    return _engine


# 纯页码/纯符号的行不值得进段落（照 pdfparse 的口味）
_PURE_NUM = re.compile(r"^[\d\s.\-—–/|°%()（）]+$")
_WATERMARK = re.compile(r"Watermark|watermark|第\s*\d+\s*页")


def _merge_text(a: str, b: str) -> str:
    """拼接两段文字：一侧是 ASCII 词内断开时才补空格，中文直接连。"""
    if a and b and re.search(r"[A-Za-z0-9,.;:%)]$", a) and re.match(r"^[A-Za-z0-9$(]", b):
        return a + " " + b
    return a + b


def _page_rows(items: list) -> list:
    """同一视觉行的识别块并成一行：y 中心聚类，行内按 x 排序。"""
    rows = []
    for it in sorted(items, key=lambda r: ((r[1] + r[3]) / 2, r[0])):
        yc = (it[1] + it[3]) / 2
        h = max(it[3] - it[1], 1)
        for row in rows:
            ry = (row["y0"] + row["y1"]) / 2
            if abs(yc - ry) <= max(h, row["h"]) * 0.45:
                row["items"].append(it)
                row["y0"] = min(row["y0"], it[1]); row["y1"] = max(row["y1"], it[3])
                row["h"] = row["y1"] - row["y0"]
                break
        else:
            rows.append({"items": [it], "y0": it[1], "y1": it[3], "h": h})
    out = []
    for row in sorted(rows, key=lambda r: r["y0"]):
        its = sorted(row["items"], key=lambda r: r[0])
        text = its[0][4]
        for prev, cur in zip(its, its[1:]):
            gap = cur[0] - prev[2]
            text = _merge_text(text, (" " if gap > (row["h"] * 0.35) else "") + cur[4])
        out.append({"text": text,
                    "bbox": [min(i[0] for i in its), row["y0"],
                             max(i[2] for i in its), row["y1"]],
                    "size": row["h"] * 0.75})
    return out


def _rows_to_paras(page_no: int, rows: list, pw: float) -> list:
    """行聚成段：垂直间距近的连成一段；双栏版式先判栏，左栏读完读右栏。"""
    if not rows:
        return []
    left = [r for r in rows if (r["bbox"][0] + r["bbox"][2]) / 2 < pw / 2]
    two_col = (0.3 < len(left) / len(rows) < 0.7)
    cols = [left, [r for r in rows if r not in left]] if two_col else [rows]

    paras, cur = [], None
    for col in cols:
        for ln in col:
            if cur is None:
                cur = {"lines": [ln]}
                continue
            prev = cur["lines"][-1]
            gap = ln["bbox"][1] - prev["bbox"][3]
            step = max(prev["size"], ln["size"])
            if gap <= step * 0.75:          # 行距紧：还在同一段
                cur["lines"].append(ln)
            else:
                paras.append(cur)
                cur = {"lines": [ln]}
        if cur is not None:
            paras.append(cur)
            cur = None

    out, in_refs = [], False
    for p in paras:
        lns = p["lines"]
        text = lns[0]["text"]
        for ln in lns[1:]:
            text = _merge_text(text, ln["text"])
        if re.match(r"^(References|参考文献|参\s*考\s*文\s*献)\s*$", text.strip()):
            in_refs = True
        if not text.strip() or _PURE_NUM.match(text.strip()):
            continue
        first = lns[0]["bbox"]
        out.append({
            "page": page_no,
            "bbox": [min(l["bbox"][0] for l in lns), first[1],
                     max(l["bbox"][2] for l in lns), max(l["bbox"][3] for l in lns)],
            "text": text.strip(),
            "in_refs": in_refs,
            "caption": "",
            "lines": [{"text": l["text"], "bbox": l["bbox"], "size": round(l["size"], 1)}
                      for l in lns],
        })
    return out


def ocr_pdf(path: str, pages: list = None) -> list:
    """整本识别，返回与 pdfparse.extract_paragraphs 同构的段落列表（含行级框）。"""
    import pymupdf
    engine = _get_engine()
    doc = pymupdf.open(path)
    out, idx = [], 0
    for pno in (pages if pages is not None else range(len(doc))):
        page = doc[pno]
        pix = page.get_pixmap(dpi=_DPI, colorspace=pymupdf.csRGB)
        img = pix.tobytes("png")
        result, _el = engine(img)
        items = []
        for box, text, score in (result or []):
            if score < _SCORE_MIN or not str(text).strip():
                continue
            xs = [pt[0] / _ZOOM for pt in box]
            ys = [pt[1] / _ZOOM for pt in box]
            t = str(text).strip()
            if _PURE_NUM.match(t) or _WATERMARK.search(t):
                continue
            items.append([min(xs), min(ys), max(xs), max(ys), t])
        if not items:
            continue
        for p in _rows_to_paras(pno, _page_rows(items), page.rect.width):
            idx += 1
            p["idx"] = idx
            out.append(p)
    return out

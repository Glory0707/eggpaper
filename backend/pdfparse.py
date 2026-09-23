"""PDF 版面解析：按行重建段落（支持双栏）、非正文区检测（参考文献/致谢/补充材料）、图注识别、坐标锚定。"""
import re
import statistics

import pymupdf

WATERMARK = re.compile(r"^arXiv:\S+\s")
PURE_NUM = re.compile(r"^\d{1,4}$")
CAPTION = re.compile(r"^(fig|figure|table|scheme|图|表)\.?\s*[0-9IVXS]+\.?", re.I)
SECTION_HEAD = re.compile(r"^(I|II|III|IV|V|VI|VII|VIII|IX|X)+\.?\s+[A-Z]")

HEAD_REFS = ("references", "bibliography", "参考文献")
HEAD_STOP = ("supplementalmaterial", "supplementarymaterial", "supportinginformation", "appendixsupp")
HEAD_NONBODY = ("acknowledg", "funding", "authorcontrib", "conflictofinterest", "dataavailab", "citedata",
                "致谢", "作者贡献", "利益冲突")
AFFIL = re.compile(r"(University|Laboratory|Institute|Department|College|Academy|School of"
                   r"|大学|学院|研究院|研究所|实验室|研究中心)")

HAS_CJK = re.compile(r"[\u4e00-\u9fff]")

def _enough(text: str, words: int = 14) -> bool:
    """"这段够长，是正文"的门槛。中文没有空格，split() 永远数不出词——按字符数。"""
    if HAS_CJK.search(text):
        return len(text) >= 40
    return len(text.split()) >= words

def _enough_caption(text: str) -> bool:
    if HAS_CJK.search(text):
        return len(text) >= 10
    return len(text.split()) >= 6

def _clean(text: str) -> str:
    text = re.sub(r"(\w)-\s+(\w)", r"\1\2", text)
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
        cand = sorted((l for l in top if l["size"] >= max_size - 0.8), key=lambda l: l["y"])
        # 只并**垂直相邻**的候选行：标题的行距在 1.5 倍字号以内，隔了空行就是别的块
        # （摘要/作者行的字号常与标题只差零点几磅，光凭字号带会把整段开头并进来）
        out = []
        for l in cand:
            if out and l["y"] - out[-1]["y"] > max(out[-1]["size"], l["size"]) * 1.8:
                break
            out.append(l)
        return _clean(" ".join(l["text"] for l in out))[:150]
    finally:
        doc.close()

SUP = re.compile(r"[\d*†‡§¶#{}\[\]]+")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+")
DATEISH = re.compile(r"\b(19|20)\d{2}\b|received|accepted|published|doi|preprint", re.I)

def extract_authors(path: str) -> str:
    """第一作者。只认"标题正下方那一两行里的第一个名字"——够用就行，不做完整作者解析。

    刻意保守：认不出来就返回空串（界面上就不显示），绝不拿机构名或日期凑数。
    """
    doc = pymupdf.open(path)
    try:
        page = doc[0]
        lines = []
        for b in page.get_text("dict")["blocks"]:
            if b["type"] != 0:
                continue
            for line in b["lines"]:
                t = _clean(" ".join(s["text"] for s in line["spans"]))
                if not t or WATERMARK.match(t):
                    continue
                size = max((s["size"] for s in line["spans"]), default=0)
                lines.append({"text": t, "size": size, "y0": line["bbox"][1], "y1": line["bbox"][3]})
        top = [l for l in lines if l["y0"] < page.rect.height * 0.5]
        if not top:
            return ""
        max_size = max(l["size"] for l in top)
        title = [l for l in top if l["size"] >= max_size - 1.6]
        if not title:
            return ""
        t_bottom = max(l["y1"] for l in title)
        below = sorted([l for l in top if l["y0"] >= t_bottom - 2 and l["size"] < max_size - 1.6],
                       key=lambda l: l["y0"])
        for ln in below[:3]:
            t = ln["text"]
            if len(t) > 200:
                continue
            if AFFIL.search(t) or EMAIL.search(t):
                continue          # 机构行/邮箱行本身不是作者，但后面可能还有
            # 年份/DOI/received 是刊头页脚的信号——作者区到这儿就结束了。
            # 只跳过不终止的话，下一行就是正文首行，标题的词会被当成作者（实测
            # 整库的作者全变成标题开头两个词）。宁可漏认，不可错认。
            if DATEISH.search(t):
                break
            if t.replace(" ", "").lower().startswith(("abstract", "keywords", "摘要", "关键词")):
                break             # 摘要开头 = 作者区结束，同理
            # 全角逗号/顿号也是作者分隔（中文论文的作者行没有半角逗号）
            first = re.split(r",|，|、|\band\b", t)[0]
            first = SUP.sub("", EMAIL.sub("", first)).strip(" .·&")
            words = first.split()
            if 1 <= len(words) <= 5 and 2 <= len(first) <= 40 and re.search(r"[A-Za-zÀ-ÿ\u4e00-\u9fff]", first):
                return first
        return ""
    finally:
        doc.close()

def citation_source(path: str) -> str:
    """给"识别引用信息"用的首页原文：不筛选、不去重，连页眉页脚一起交出去。

    为什么不能直接拿 extract_paragraphs 的结果：引用信息藏的那几个地方，恰好都是
    段落抽取**故意丢掉**的——页眉页脚的刊名/卷期页（按 y 坐标裁掉了）、题目作者块
    （按机构关键词滤掉了）、DOI 行（跟在页脚里）。所以这里另取一次原始行。
    第二页只给头尾几行：期刊的 running head 和 DOI 在那儿，正文没必要喂。
    """
    doc = pymupdf.open(path)
    try:
        out = []
        for pno in range(min(2, len(doc))):
            lines = [ln["text"] for ln in _page_lines(doc[pno])]
            out += lines[:110] if pno == 0 else lines[:6] + lines[-6:]
        md = doc.metadata or {}
        head = [f"{k}: {md[k]}" for k in ("title", "author", "subject", "keywords") if md.get(k)]
        body = "[首页原文]\n" + "\n".join(out)
        meta = ""
        if head:
            meta = "\n\n[PDF 元数据]\n" + "\n".join(head)
        room = max(1200, 6000 - len(meta))
        return body[:room] + meta
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
    in_refs = False
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
            wide = [l for l in raw if l["bbox"][0] < pw * 0.42 and l["bbox"][2] > pw * 0.58]
            rest = [l for l in raw if l not in wide]
            left = [l for l in rest if l["bbox"][0] < pw * 0.5 - 20]
            right = [l for l in rest if l["bbox"][0] >= pw * 0.5 - 20]
            if left and right and len(right) >= 4:
                columns = [left, right]
                if wide:
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
                gaps = [b - a for a, b in zip(ys, ys[1:]) if 3 < b - a < 30]
                pitch = statistics.median(gaps) if gaps else 12

                # 每组记下生成时的区域状态：refs 标题行触发 break 时 in_refs 才置位，
                # 它只该作用于它**之后**的组——处理循环若看外层变量，同列 refs 之前的
                # 正文组会被整体误杀（英文论文最后一页 References 前的段落一直在丢）
                cur = {"lines": [col[0]], "refs": in_refs}
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
                        cur = {"lines": [ln], "refs": in_refs}
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
                    lines = [{"text": l["text"], "bbox": {"x0": l["bbox"][0], "y0": l["bbox"][1],
                                                          "x1": l["bbox"][2], "y1": l["bbox"][3]}}
                             for l in ls]
                    caption = bool(CAPTION.match(first))
                    if caption:
                        if not _enough_caption(text):
                            continue
                        paras.append({"page": pno, "bbox": bbox, "text": _clean(text), "lines": lines,
                                      "in_refs": False, "caption": True})
                        continue
                    if g["refs"] or not _enough(text) or size > body_size + 2.2:
                        continue
                    # 机构行滤除只对"短行"下手：中文按字符算，否则带两处"大学"的正文段会被误杀
                    affilish = len(text.split()) < 60 if not HAS_CJK.search(text) else len(text) < 120
                    if affilish and len(AFFIL.findall(text)) >= 2:
                        continue
                    paras.append({"page": pno, "bbox": bbox, "text": _clean(text), "lines": lines,
                                  "in_refs": False, "caption": False})
    finally:
        doc.close()
    for i, p in enumerate(paras, 1):
        p["idx"] = i
    return paras

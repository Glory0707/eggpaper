"""参考文献格式：把从首页抄下来的那几个字段，排成可以直接粘进论文 / 幻灯片的条目。

**分工**：模型只管"把首页印着的那几行抄成字段"（见 llm.extract_citation），
排版一律用代码做。让模型自己去拼字符串一定会飘——多一个空格、少一个句点、
页码用连字符还是连接号，引用格式恰恰是错一个字符就要返工的东西。

**降级**：缺哪个字段就少写哪一段，宁可条目短一截，也不要补一个"看起来很像"的
数字进去。参考文献表里的假卷号、假页码会被原样抄进别人的论文，而且没人会去核。
"""
import re

# 分组：先给人写论文用的长条目，再给 PPT 用的短引用，最后是给软件吃的导入格式
GROUP_ZH = {"ref": "参考文献条目", "short": "短引用（PPT / 图注）", "import": "导入与链接"}


def _s(v) -> str:
    return str(v or "").strip()


def _dash(s: str, d: str) -> str:
    """页码里的连字符统一成该格式要的那个（APA/Nature 用 en dash，BibTeX 用 --）。"""
    return re.sub(r"\s*[-\u2010-\u2015\u2212]+\s*", d, _s(s))


def _flat(s: str) -> str:
    """比对用的归一形：去空白、各种横线统一成 -、变小写。

    能容忍换行（"6789–" / "6795" 跨行）、全角空格、不同字号的连字符——
    这些都不该算"源文里没有"。
    """
    s = re.sub(r"[\u2010-\u2015\u2212]", "-", _s(s))
    return re.sub(r"[\s\u00a0\u2009]+", "", s).lower()


def _authors(meta: dict) -> list:
    out = []
    for a in (meta.get("authors") or []):
        if isinstance(a, str):
            if a.strip():
                out.append({"family": a.strip(), "given": ""})
            continue
        fam, giv = _s(a.get("family")), _s(a.get("given"))
        if fam or giv:
            out.append({"family": fam or giv, "given": giv if fam else ""})
    return out


def _ini(given: str, spaced: bool = True, dots: bool = True) -> str:
    """"Wei-Ming" → "W.-M."；"Wei Ming" → "W. M."；已经是 "W." 的原样收下。

    dots=False 是给 GB/T 7714 用的：那份标准的缩写名不带点（"EINSTEIN A"）。
    """
    toks = []
    for word in re.split(r"[\s.]+", _s(given)):
        bits = [b for b in word.split("-") if b]
        if bits:
            toks.append("-".join(b[0].upper() + ("." if dots else "") for b in bits))
    return (" " if spaced else "").join(toks)


def _fam_ini(a: dict) -> str:
    ini = _ini(a["given"])
    return f"{a['family']}, {ini}".rstrip(", ")


def _au_gbt(aus: list) -> str:
    """GB/T 7714：姓全大写 + 名缩写（缩写之间留空格、不带点）；超过三个只列前三个，加"等"。"""
    names = [f"{a['family'].upper()} {_ini(a['given'], dots=False)}".strip() for a in aus]
    if not names:
        return ""
    return ", ".join(names[:3]) + (", 等" if len(names) > 3 else "")


def _au_apa(aus: list) -> str:
    """APA 7：姓 + 名缩写，末位前一个 &；超过六位列前六 + … + 末位。"""
    names = [_fam_ini(a) for a in aus]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) > 6:
        return ", ".join(names[:6] + ["…"] + [names[-1]])
    return ", ".join(names[:-1]) + ", & " + names[-1]


def _au_nature(aus: list) -> str:
    """Nature：五位以内列全（末位前 &），超过五位只写第一位 + et al.。"""
    names = [_fam_ini(a) for a in aus]
    if not names:
        return ""
    if len(names) > 5:
        return names[0] + " et al."
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " & " + names[-1]


def _au_short(aus: list) -> str:
    if not aus:
        return ""
    if len(aus) == 1:
        return aus[0]["family"]
    if len(aus) == 2:
        return aus[0]["family"] + " & " + aus[1]["family"]
    return aus[0]["family"] + " et al."


def _bib_tag(journal: str) -> str:
    """"Journal of the American Chemical Society" → "jacs"：取实词首字母。"""
    words = [w for w in re.findall(r"[A-Za-z]+", journal)
             if w.lower() not in ("of", "the", "and", "for", "in", "on")]
    return "".join(w[0] for w in words)[:5].lower()


def _bib_key(meta: dict, aus: list) -> str:
    fam = re.sub(r"[^a-z0-9]", "", (aus[0]["family"].lower() if aus else "")) or "ref"
    year = re.sub(r"[^0-9]", "", _s(meta.get("year")))
    tag = _bib_tag(_s(meta.get("journal_abbr")) or _s(meta.get("journal")))
    if not tag:                       # 没刊名：拿题目第一个实词顶上
        words = [w for w in re.findall(r"[A-Za-z]{3,}", _s(meta.get("title")))]
        tag = (words[0].lower()[:5] if words else "paper")
    return f"{fam}{year}{tag}"


def _journal(meta: dict, abbr_first: bool = False) -> str:
    full, ab = _s(meta.get("journal")), _s(meta.get("journal_abbr"))
    return (ab or full) if abbr_first else (full or ab)


def _preprint_id(meta: dict) -> str:
    """预印本裸编号：2609.06350（模型可能写成 arXiv:2609.06350v1，统一掉）。"""
    v = re.sub(r"^arxiv[:\s]*", "", _s(meta.get("preprint")), flags=re.I)
    return re.sub(r"v\d+$", "", v.strip()).strip()


def _preprint(meta: dict) -> str:
    pid = _preprint_id(meta)
    return f"arXiv:{pid}" if pid else ""


def _venue(meta: dict, abbr_first: bool = False) -> str:
    """短引用那一行的"出处"：有刊名用刊名，没有就用 arXiv——预印本的出处就是它。"""
    return _journal(meta, abbr_first) or ("arXiv" if _preprint_id(meta) else "")


# ---------------- 长条目 ----------------

def gbt(meta: dict) -> str:
    """GB/T 7714-2015：作者. 题名[J]. 刊名, 年, 卷(期): 起止页码.

    没有刊名（预印本）时，出处位让给预印本编号，文献类型改 [EB/OL]。
    """
    t = _s(meta.get("title"))
    if not t:
        return ""
    au = _au_gbt(_authors(meta))
    venue = _journal(meta) or _preprint(meta)
    tag = "J" if _journal(meta) else ("EB/OL" if venue else "J")
    loc = _s(meta.get("year"))
    if _s(meta.get("volume")):
        loc += (", " if loc else "") + _s(meta["volume"])
    if _s(meta.get("issue")):
        loc += f"({_s(meta['issue'])})"
    if _s(meta.get("pages")):
        loc += (": " if loc else "") + _dash(meta["pages"], "-")
    s = (au + ". ") if au else ""
    s += f"{t}[{tag}]." + (f" {venue}" if venue else "")
    if loc:
        s += (", " if venue else " ") + loc
    return s + "."


def apa(meta: dict) -> str:
    """APA 7：作者 (年). 题名. 刊名, 卷(期), 起止页码. https://doi.org/xxx"""
    t = _s(meta.get("title"))
    if not t:
        return ""
    au, j, year, pp = _au_apa(_authors(meta)), _journal(meta), _s(meta.get("year")), _preprint_id(meta)
    s = (au + " " if au else "") + (f"({year}). " if year else "")
    s += f"{t}." + (" [Preprint]." if pp and not j else "")
    if j:
        s += f" {j}"
        if _s(meta.get("volume")):
            s += f", {_s(meta['volume'])}"
            if _s(meta.get("issue")):
                s += f"({_s(meta['issue'])})"
        if _s(meta.get("pages")):
            s += f", {_dash(meta['pages'], '–')}"
        s += "."
    if _s(meta.get("doi")):
        s += f" https://doi.org/{_s(meta['doi'])}"
    elif pp:
        # arXiv 给每篇预印本分配了这个 DOI，比自己拼 abs 链接更稳（将来也能解析）
        s += f" arXiv. https://doi.org/10.48550/arXiv.{pp}"
    return s


def nature(meta: dict) -> str:
    """Nature 体：作者. 题名. 刊名缩写 卷, 起止页码 (年).　预印本按 Nature 自己的写法。"""
    t = _s(meta.get("title"))
    if not t:
        return ""
    au = _au_nature(_authors(meta))
    j, pp = _journal(meta, abbr_first=True), _preprint_id(meta)
    s = (au + " " if au else "") + f"{t}."
    if j:
        s += f" {j}"
    if _s(meta.get("volume")):
        s += f" {_s(meta['volume'])}," if j else f" {_s(meta['volume'])}"
    if _s(meta.get("pages")):
        s += f" {_dash(meta['pages'], '–')}"
    if pp and not j:
        s += f" Preprint at https://arxiv.org/abs/{pp}"
    if _s(meta.get("year")):
        s += f" ({_s(meta['year'])})"
    return s.strip().rstrip(",") + "."


def bibtex(meta: dict) -> str:
    aus = _authors(meta)
    pp = _preprint_id(meta)
    typ = "article" if _journal(meta) else "misc"
    rows = [("title", _s(meta.get("title"))), ("author", " and ".join(
        f"{a['family']}, {a['given']}".rstrip(", ") for a in aus)),
        ("journal", _journal(meta)), ("year", _s(meta.get("year"))),
        ("volume", _s(meta.get("volume"))), ("number", _s(meta.get("issue"))),
        ("pages", _dash(meta.get("pages"), "--")), ("doi", _s(meta.get("doi"))),
        ("eprint", pp), ("archivePrefix", "arXiv" if pp else "")]
    rows = [(k, v) for k, v in rows if v]
    if not rows:
        return ""
    pad = max(len(k) for k, _ in rows) + 1
    body = "\n".join(f"  {k:<{pad}}= {{{v}}}," for k, v in rows)
    return "@%s{%s,\n%s\n}" % (typ, _bib_key(meta, aus), body)


def ris(meta: dict) -> str:
    aus = _authors(meta)
    pg = _s(meta.get("pages"))
    pp = _preprint_id(meta)
    m = re.split(r"\s*[-\u2010-\u2015\u2212]+\s*", pg, maxsplit=1)
    rows = [("TY", "JOUR")]
    rows += [("AU", f"{a['family']}, {a['given']}".rstrip(", ")) for a in aus]
    rows += [("TI", _s(meta.get("title"))), ("JO", _venue(meta)),
             ("VL", _s(meta.get("volume"))), ("IS", _s(meta.get("issue")))]
    if pg:
        rows.append(("SP", m[0]))
        if len(m) > 1 and m[1]:
            rows.append(("EP", m[1]))
    rows += [("PY", _s(meta.get("year"))), ("DO", _s(meta.get("doi"))),
             ("UR", f"https://arxiv.org/abs/{pp}" if pp else "")]
    rows = [(k, v) for k, v in rows if v]
    return "\n".join(f"{k}  - {v}" for k, v in rows) + "\nER  -"


# ---------------- 短引用 ----------------

def short_year(meta: dict) -> str:
    """PPT 角标：Zhang et al., 2023"""
    au = _au_short(_authors(meta))
    parts = [x for x in (au, _s(meta.get("year"))) if x]
    return ", ".join(parts)


def short_journal_year(meta: dict) -> str:
    """PPT 页脚：Zhang et al., J. Am. Chem. Soc., 2023"""
    au = _au_short(_authors(meta))
    return ", ".join(x for x in (au, _venue(meta, abbr_first=True), _s(meta.get("year"))) if x)


def inline(meta: dict) -> str:
    """正文括注：(Zhang et al., 2023)"""
    inner = short_year(meta)
    return f"({inner})" if inner else ""


ROWS = [
    ("ref", "gbt7714", "GB/T 7714", "中文期刊 / 学位论文", gbt),
    ("ref", "apa", "APA 7", "英文期刊常见", apa),
    ("ref", "nature", "Nature 体", "理工投稿", nature),
    ("short", "short_y", "作者 + 年", "PPT 角标、图注", short_year),
    ("short", "short_jy", "作者 + 期刊 + 年", "PPT 页脚", short_journal_year),
    ("short", "inline", "正文括注", "写作时引一句", inline),
    ("import", "bibtex", "BibTeX", "LaTeX", bibtex),
    ("import", "ris", "RIS", "Zotero / EndNote", ris),
    ("import", "doi", "DOI", "投稿系统、文献管理", lambda m: _s(m.get("doi"))),
    ("import", "preprint", "预印本编号", "arXiv", _preprint),
]


def groups(meta: dict) -> list:
    """排好的格式，按组返回；排不出来的（缺关键字段）直接不出现。"""
    if not meta:
        return []
    out = []
    for gk, k, label, hint, fn in ROWS:
        try:
            text = fn(meta)
        except Exception:
            text = ""
        if not text:
            continue
        if not out or out[-1]["k"] != gk:      # 同组连排，换组才起新块
            out.append({"k": gk, "name": GROUP_ZH.get(gk, gk), "rows": []})
        out[-1]["rows"].append({"k": k, "label": label, "hint": hint, "text": text})
    return out


def sanity(meta: dict, src: str, fallback_title: str = "", fallback_author: str = "") -> dict:
    """把模型"顺手编出来"的字段擦掉：源文里没出现过的，一律清空。

    参考文献里最不能容忍的错误是**看起来很像的假数据**——一个不存在的卷号、一段
    没印过的页码，会被原样抄进别人的论文，还没人会去核。所以除题目外，其余字段
    都要求"这串字在首页文字里真的出现过"（归一化子串匹配，容忍换行与各种横线）。
    题目允许回退到版面分析抽出的标题；作者一个都对不上就回退到抽出的第一作者。
    """
    src_f = _flat(src)
    out = dict(meta or {})
    for k in ("journal", "journal_abbr", "year", "volume", "issue", "pages", "preprint"):
        v = _s(out.get(k))
        if v and _flat(v) not in src_f:
            out[k] = ""
    aus = _authors(out)
    if aus:
        kept = [a for a in aus if _flat(a["family"]) in src_f]
        out["authors"] = kept or ([{"family": fallback_author, "given": ""}] if fallback_author else [])
    elif fallback_author:
        out["authors"] = [{"family": fallback_author, "given": ""}]
    title = _s(out.get("title"))
    if not title or _flat(title) not in src_f:
        out["title"] = fallback_title or title
    doi = _s(out.get("doi")).rstrip(".")
    if doi and _flat(doi) not in src_f:
        doi = ""                       # DOI 格式严格，编一个出来必然是 404
    if not doi:                        # 首页印着但模型漏抄：自己正则捞一遍
        m = re.search(r"10\.\d{4,9}/[^\s\"'<>)\]]+", src)
        doi = m.group(0).rstrip(".,;") if m else ""
    out["doi"] = doi
    if not _s(out.get("preprint")):     # 同理：水印上印着 arXiv:2609.06350v1，模型有时会当噪音跳过
        m = re.search(r"arXiv[:\s]*(\d{4}\.\d{4,5})", src, re.I)
        out["preprint"] = f"arXiv:{m.group(1)}" if m else ""
    return out

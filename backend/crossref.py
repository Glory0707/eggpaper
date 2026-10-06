"""Crossref 元数据回填：导入一篇 PDF 后，按首页上的 DOI 去 Crossref 查官方记录，
把解析/抄写缺的字段补上（默认开启；匿名查询，无账号无 key）。

口径（"不猜"原则不动摇）：
- 只查 DOI 命中的记录；查不到、超时、网络不通——全部安静跳过，绝不妨碍导入。
- **只填空字段**：解析出来的标题、作者、年份不覆盖；引用卡（papers.citation）已有
  LLM 抄写的字段就整块不动。Crossref 给的也是真实元数据，但"已有就不碰"省掉一切
  归一化冲突。
- 数据只进 papers 表的 title/authors/year 与 citation JSON，格式与 citation.py 的
  meta 完全同构——引用浮层、九种排版、CSV 导出原样可用，省一次"抄首页"的模型调用。
"""
import json
import os
import re
import threading

import httpx

import db

# 与前端 find.js 的 DOI_RE 同口径：结构上就不会认错的 10. 前后缀；尾随标点剥掉
DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s\"'<>]+")

_API = os.environ.get("EGGPAPER_CROSSREF_API", "https://api.crossref.org")


def first_doi(text: str) -> str:
    """一段首页文字里的第一个 DOI（尾随标点剥掉，和前端划链接同一手法）。"""
    for m in DOI_RE.finditer(text or ""):
        doi = re.sub(r"[.,;)\]]+$", "", m.group(0))
        if len(doi) > 8:
            return doi
    return ""


def _s(v) -> str:
    return str(v if v is not None else "").strip()


def _meta_of(rec: dict) -> dict:
    """Crossref message → citation.meta 同构 dict。认不出的字段留空，不替你想。"""
    msg = rec.get("message") if isinstance(rec, dict) else None
    if not isinstance(msg, dict):
        return {}
    authors = []
    for a in (msg.get("author") or []):
        if isinstance(a, dict) and (a.get("family") or a.get("given")):
            authors.append({"family": _s(a.get("family")) or _s(a.get("given")),
                            "given": _s(a.get("given")) if a.get("family") else ""})
    titles = msg.get("title") or []
    years = []
    for k in ("issued", "published", "created"):
        dp = (msg.get(k) or {}).get("date-parts") or []
        if dp and dp[0] and dp[0][0]:
            years.append(str(dp[0][0]))
    return {"title": _s(titles[0] if titles else ""),
            "authors": authors,
            "journal": _s((msg.get("container-title") or [""])[0]),
            "journal_abbr": _s((msg.get("short-container-title") or [""])[0]),
            "year": years[0] if years else "",
            "volume": _s(msg.get("volume")), "issue": _s(msg.get("issue")),
            "pages": _s(msg.get("page")), "doi": _s(msg.get("DOI")) or ""}


def lookup(doi: str) -> dict:
    """查一条 DOI，返回 citation.meta 同构 dict；任何失败返回 {}。"""
    if not doi:
        return {}
    try:
        r = httpx.get(f"{_API}/works/{doi}", timeout=httpx.Timeout(10, connect=6),
                      follow_redirects=True,
                      headers={"User-Agent": "eggpaper/0.1 (local paper reader)"})
        r.raise_for_status()
        return _meta_of(r.json())
    except Exception as e:
        print(f"[eggpaper] Crossref 查询 {doi} 没成：{type(e).__name__}")
        return {}


def backfill(pid: str):
    """导入后的后台回填（不挡导入返回）。DOI 没找到、字段都齐——都不动手。"""
    try:
        p = db.get_paper(pid)
        if not p:
            return
        src = p.get("path") or ""
        if not src or not os.path.isfile(src):
            return
        try:
            import pymupdf
            with pymupdf.open(src) as d:
                text = "\n".join(d[i].get_text() for i in range(min(2, len(d))))
        except Exception:
            return
        doi = first_doi(text)
        if not doi:
            return
        meta = lookup(doi)
        if not meta or _pid_gone_guard(pid):
            return
        ups = {}
        if not (p.get("authors") or "").strip() and meta.get("authors"):
            ups["authors"] = "; ".join(a["family"] for a in meta["authors"][:8])[:300]
        if not (p.get("year") or "").strip() and meta.get("year"):
            ups["year"] = meta["year"][:16]
        if not p.get("citation"):
            have = {k: v for k, v in meta.items() if v}
            if have.get("title") or have.get("authors"):
                ups["citation"] = json.dumps(meta, ensure_ascii=False)
        if ups:
            db.update_paper(pid, **ups)
            print(f"[eggpaper] Crossref 回填 {pid}：{', '.join(ups)}（doi:{doi[:40]}）")
    except Exception as e:
        print(f"[eggpaper] Crossref 回填失败（不影响使用）：{type(e).__name__}: {str(e)[:120]}")


def _pid_gone_guard(pid: str) -> bool:
    """回填隔着一次网络调用，期间论文可能已被删/被替换。"""
    try:
        return db.get_paper(pid) is None
    except Exception:
        return True


def kick(pid: str):
    """导入管线调用：开一条后台线程，立刻返回。"""
    threading.Thread(target=backfill, args=(pid,), daemon=True).start()

"""从 Zotero 桌面版导入文献：读它的本地 HTTP API（localhost:23119）。

Zotero 7+ 自带这组接口（与 Web API 同款端点、从本地库出数据），不需要装任何插件。
我们的用法是刻意的**单向、一次性**：列表 → 勾选 → 把 PDF 连元数据拷进 eggpaper 的库，
之后两边互不相干——eggpaper 自己的分类是唯一的真相，绝不养第二套同步状态。

数据目录：附件的 path 字段是 `storage:文件名` 相对路径，要拼上 Zotero 的数据目录
（默认 %USERPROFILE%\\Zotero；用户改过的话只有 prefs.js 里记着）。
"""
import os
import re

import httpx

BASE = "http://localhost:23119/api"
TIMEOUT = 4.0
PAGE = 500

# 只要"能当论文读"的条目类型；网页快照、笔记、附件这些不进列表
ITEM_TYPES = {
    "journalArticle", "conferencePaper", "preprint", "report", "thesis",
    "book", "bookSection", "manuscript", "document",
}


class ZoteroUnavailable(Exception):
    """Zotero 没开或本地 API 没就绪——界面把它翻译成人话。"""


def _get(client: httpx.Client, path: str, params: dict = None):
    r = client.get(BASE + path, params=params or {})
    if r.status_code == 404 and "items" in path:
        # 本地 API 在老版本上对个别端点缩水；列表拉不到就别硬撑
        raise ZoteroUnavailable("这个 Zotero 版本不提供本地 API（需要 Zotero 7 以上）")
    r.raise_for_status()
    return r.json()


def _year(date: str) -> str:
    m = re.search(r"\d{4}", date or "")
    return m.group(0) if m else ""


def _authors(data: dict) -> str:
    names = []
    for c in data.get("creators") or []:
        if c.get("creatorType") != "author":
            continue
        names.append(c.get("name") or " ".join(x for x in (c.get("firstName"), c.get("lastName")) if x))
    return " · ".join(n.strip() for n in names if n and n.strip())


def data_dir() -> str:
    """Zotero 数据目录（storage 的父目录）：默认位置，不行翻 prefs.js。"""
    for p in (os.path.join(os.path.expanduser("~"), "Zotero"),
              os.path.join(os.environ.get("USERPROFILE", ""), "Zotero")):
        if os.path.isdir(os.path.join(p, "storage")):
            return p
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        prof = os.path.join(appdata, "Zotero", "Zotero", "Profiles")
        if os.path.isdir(prof):
            prefs = []
            for root, _dirs, files in os.walk(prof):
                prefs += [os.path.join(root, f) for f in files if f == "prefs.js"]
            prefs.sort(key=os.path.getmtime, reverse=True)
            for f in prefs[:2]:
                try:
                    txt = open(f, encoding="utf-8", errors="ignore").read()
                except OSError:
                    continue
                m = re.search(r'user_pref\("extensions\.zotero\.dataDir",\s*"((?:[^"\\]|\\.)*)"\)', txt)
                if m:
                    raw = m.group(1).encode().decode("unicode_escape")
                    raw = os.path.expandvars(raw)
                    if os.path.isdir(os.path.join(raw, "storage")):
                        return raw
    return ""


def _pdf_path(item: dict, zdir: str) -> str:
    """条目的 PDF 附件在本机的路径；没有或文件不在了就返回空。

    Zotero 附件两种存法都认：**托管附件**（path 是 `storage:文件名`，落在数据目录的
    storage/<KEY>/ 下）和**链接附件**（path 直接是本机绝对路径，文件留在原处）。"""
    d = item.get("data") or {}
    if d.get("itemType") != "attachment" or "pdf" not in (d.get("contentType") or ""):
        return ""
    raw = d.get("path") or ""
    if raw.startswith("storage:"):
        fname = raw[8:]
        if not fname or not zdir:
            return ""
        key = d.get("key") or (item.get("key") or "")
        for cand in (os.path.join(zdir, "storage", key, fname),
                     os.path.join(zdir, "storage", fname)):
            if os.path.isfile(cand):
                return cand
        return ""
    if raw and os.path.isfile(raw):
        return raw
    return ""


def list_items() -> list:
    """顶层条目 + 各自的 PDF 附件路径，扁平成前端好渲染的一列。"""
    zdir = data_dir()
    rows = []
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            items, start = [], 0
            while True:
                batch = _get(client, "/users/0/items", {"limit": PAGE, "start": start, "format": "json"})
                if not isinstance(batch, list) or not batch:
                    break
                items += batch
                if len(batch) < PAGE:
                    break
                start += PAGE
            pdf_of = {}
            for it in items:
                p = _pdf_path(it, zdir)
                if p:
                    pdf_of[(it.get("data") or {}).get("parentItem") or ""] = p
            for it in items:
                d = it.get("data") or {}
                if d.get("itemType") not in ITEM_TYPES:
                    continue
                rows.append({
                    "key": it.get("key"),
                    "type": d.get("itemType"),
                    "title": (d.get("title") or "").strip() or "(无标题)",
                    "authors": _authors(d),
                    "year": _year(d.get("date")),
                    "pdf": pdf_of.get(it.get("key"), ""),
                })
    except (httpx.ConnectError, httpx.ConnectTimeout):
        raise ZoteroUnavailable("连不上 Zotero——先把它打开，再回来点导入")
    except httpx.HTTPError as e:
        raise ZoteroUnavailable(f"Zotero 的本地接口出了问题：{e}")
    rows.sort(key=lambda r: (-(int(r["year"] or 0)), r["title"].lower()))
    return rows

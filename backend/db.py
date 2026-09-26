"""SQLite 存储：论文、段落、骨架标注、术语表、问答。全部本地。"""
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager

from config import DATA_DIR

DB_PATH = os.path.join(DATA_DIR, "eggpaper.db")
_lock = threading.Lock()
_conn = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers(
  id TEXT PRIMARY KEY, filename TEXT, title TEXT DEFAULT '', path TEXT, dual_path TEXT,
  n_pages INTEGER DEFAULT 0, created_at TEXT,
  analysis_status TEXT DEFAULT 'none', analysis_error TEXT,
  translate_status TEXT DEFAULT 'none', translate_error TEXT,
  marginalia_status TEXT DEFAULT 'none', marginalia_error TEXT,
  summary TEXT, paper_type TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS paragraphs(
  paper_id TEXT, idx INTEGER, page INTEGER, bbox TEXT, text TEXT, in_refs INTEGER DEFAULT 0, lines TEXT,
  PRIMARY KEY(paper_id, idx)
);
CREATE TABLE IF NOT EXISTS annotations(
  paper_id TEXT, para_idx INTEGER, role TEXT, purpose TEXT, user_override INTEGER DEFAULT 0,
  PRIMARY KEY(paper_id, para_idx)
);
CREATE TABLE IF NOT EXISTS claims(
  paper_id TEXT, cid TEXT, text TEXT, anchors TEXT,
  PRIMARY KEY(paper_id, cid)
);
CREATE TABLE IF NOT EXISTS glossary(
  id INTEGER PRIMARY KEY AUTOINCREMENT, paper_id TEXT, term_en TEXT, term_zh TEXT,
  domain TEXT DEFAULT '', note TEXT DEFAULT '', source TEXT DEFAULT 'manual', created_at TEXT
);
CREATE TABLE IF NOT EXISTS qa_messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, paper_id TEXT, role TEXT, content TEXT, citations TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS marginalia(
  id INTEGER PRIMARY KEY AUTOINCREMENT, paper_id TEXT, para_idx INTEGER, page INTEGER,
  quote TEXT, kind TEXT, note TEXT, rect TEXT
);
-- 一篇论文可以有好几摊对话（"读方法时问的"和"写综述时问的"不该混在一个上下文里），
-- 所以问答按会话分组；上下文只取本会话的历史，和豆包的"新对话"是一个意思。
CREATE TABLE IF NOT EXISTS conversations(
  id INTEGER PRIMARY KEY AUTOINCREMENT, paper_id TEXT, title TEXT, created_at TEXT, updated_at TEXT
);
-- 七问里需要现场生成的那几问（motive 要解决什么、principle/method/how 怎么解决、next 还能做什么、
-- lens 换个学科怎么看；键的口径见 llm.py）：按篇缓存，点过一次就不再花钱
CREATE TABLE IF NOT EXISTS answers(
  paper_id TEXT, key TEXT, json TEXT, PRIMARY KEY(paper_id, key)
);
-- 文库分类：一篇文献可以同时属于多个分类（Zotero 的 collection 语义，不是文件夹）
CREATE TABLE IF NOT EXISTS collections(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS paper_collections(
  paper_id TEXT, coll_id INTEGER, PRIMARY KEY(paper_id, coll_id)
);
-- 论文日历的阅读日志：每次打开论文按"天"记一笔（一篇一天只有一行）。
-- 更早的历史没有日志，日历端点用 papers.last_read_at / created_at 的日期做只读推导补齐。
CREATE TABLE IF NOT EXISTS reading_log(
  day TEXT, paper_id TEXT, PRIMARY KEY(day, paper_id)
);
"""

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_qa_msg_conv ON qa_messages(conv_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_qa_msg_paper ON qa_messages(paper_id)",
    "CREATE INDEX IF NOT EXISTS idx_marg_paper ON marginalia(paper_id)",
    "CREATE INDEX IF NOT EXISTS idx_gloss_paper ON glossary(paper_id)",
    "CREATE INDEX IF NOT EXISTS idx_conv_paper ON conversations(paper_id)",
    "CREATE INDEX IF NOT EXISTS idx_papers_hash ON papers(pdf_hash)",
)

def _get() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
        _migrate(_conn)   # 必须先补列再建索引：idx_qa_msg_conv/idx_papers_hash 的列来自迁移，
        for stmt in _INDEXES:   # 顺序反了会 OperationalError 被吞，索引从此再也建不上
            try:
                _conn.execute(stmt)
            except sqlite3.OperationalError:
                pass
        try:      # WAL：读写不再互斥全库 fsync；synchronous=NORMAL 够本地应用
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.OperationalError:
            pass
        _conn.commit()
    return _conn

def _migrate(c: sqlite3.Connection):
    """惰性迁移：旧库补列。"""
    for stmt in (
        "ALTER TABLE glossary ADD COLUMN paper_id TEXT",
        "ALTER TABLE papers ADD COLUMN mono_path TEXT",
        "ALTER TABLE annotations ADD COLUMN inferred_role TEXT",
        "ALTER TABLE papers ADD COLUMN suggest TEXT",
        "ALTER TABLE papers ADD COLUMN advisor TEXT",
        "ALTER TABLE papers ADD COLUMN method_card TEXT",
        "ALTER TABLE papers ADD COLUMN abbrs TEXT",
        "ALTER TABLE papers ADD COLUMN evidence_qs TEXT",
        "ALTER TABLE paragraphs ADD COLUMN lines TEXT",
        "ALTER TABLE qa_messages ADD COLUMN conv_id INTEGER",
        "ALTER TABLE papers ADD COLUMN last_read_at TEXT",
        "ALTER TABLE papers ADD COLUMN authors TEXT",
        "ALTER TABLE papers ADD COLUMN year TEXT",
        "ALTER TABLE conversations ADD COLUMN summary TEXT",
        "ALTER TABLE conversations ADD COLUMN summary_upto INTEGER DEFAULT 0",
        "ALTER TABLE papers ADD COLUMN citation TEXT",
        "ALTER TABLE marginalia ADD COLUMN label TEXT",
        "ALTER TABLE marginalia ADD COLUMN band TEXT",
        "ALTER TABLE papers ADD COLUMN paper_type TEXT DEFAULT ''",
        "ALTER TABLE papers ADD COLUMN pdf_hash TEXT DEFAULT ''",
        "ALTER TABLE papers ADD COLUMN fig_caps TEXT DEFAULT ''",
        "ALTER TABLE papers ADD COLUMN plan_day TEXT DEFAULT ''",
    ):
        try:
            c.execute(stmt)
        except sqlite3.OperationalError:
            pass
    try:
        c.execute("DELETE FROM glossary WHERE paper_id IS NULL OR paper_id=''")
    except sqlite3.OperationalError:
        pass

def q(sql: str, params=(), commit: bool = False):
    with _lock:
        cur = _get().execute(sql, params)
        rows = cur.fetchall()
        if commit:
            _get().commit()
        return rows

def q_insert(sql: str, params=()) -> int:
    """INSERT 并取自增 id——两步必须同锁：分开写会拿到别人那行的 rowid。"""
    with _lock:
        cur = _get().execute(sql, params)
        rowid = cur.lastrowid
        _get().commit()
        return rowid

def new_id() -> str:
    return uuid.uuid4().hex[:10]

# ---------- papers ----------

def create_paper(pid: str, filename: str, title: str, path: str, n_pages: int, authors: str = "") -> str:
    q("INSERT INTO papers(id, filename, title, path, n_pages, authors, created_at) VALUES(?,?,?,?,?,?,?)",
      (pid, filename, title, path, n_pages, authors, time.strftime("%Y-%m-%d %H:%M:%S")), commit=True)
    return pid

def list_papers():
    return [dict(r) for r in q(
        "SELECT id, filename, title, authors, year, n_pages, created_at, last_read_at, analysis_status, "
        "marginalia_status, translate_status, plan_day FROM papers ORDER BY created_at DESC")]


def set_plan(pid: str, day: str):
    """待读计划：一篇只占一天（重排就覆盖，取消置空）。打开那篇时由 touch 清掉。"""
    q("UPDATE papers SET plan_day=? WHERE id=?", (day or "", pid), commit=True)

def set_paper_meta(pid: str, title: str = "", authors: str = "", year: str = ""):
    """Zotero 导入后回填可信元数据：只覆盖给了值的字段，解析出来的不许被空值抹掉。
    长度钳在展示合理范围（标题进提示词/UI，别让一万字的怪输入撑爆版面）。"""
    fields = {k: v for k, v in (("title", str(title)[:500]), ("authors", str(authors)[:300]),
                                ("year", str(year)[:16])) if v}
    if fields:
        update_paper(pid, **fields)

def get_paper(pid: str):
    rows = q("SELECT * FROM papers WHERE id=?", (pid,))
    return dict(rows[0]) if rows else None

def update_paper(pid: str, **fields):
    keys = ",".join(f"{k}=?" for k in fields)
    q(f"UPDATE papers SET {keys} WHERE id=?", (*fields.values(), pid), commit=True)

def find_duplicate(filename: str, size: int, pdf_hash: str = ""):
    """找同一份 PDF 的已有论文，返回它的 id（没有则 None）。

    判重钥匙有两把，先**内容指纹**再"文件名 + 字节数"：改名重导的同一份文件
    （下载两次、文件管理器里复制一份）不该占第二份库空间——两篇论文意味着两遍通读、
    两份译文、两倍磁盘。指纹由导入方算好传进来；旧库的论文指纹是空的，回填线程
    （main 启动时）会慢慢补齐，补上之后这把钥匙就全库都好使了。
    """
    if pdf_hash:
        for r in q("SELECT id FROM papers WHERE pdf_hash=?", (pdf_hash,)):
            return r["id"]
    # 先让 SQL 把范围缩到同名，再逐个 stat 核对字节数（path 可能是 NULL/坏值，stat 炸了就跳过）
    for r in q("SELECT id, path FROM papers WHERE filename=?", (filename,)):
        try:
            if os.path.getsize(r["path"]) == size:
                return r["id"]
        except (OSError, TypeError, ValueError):
            continue
    return None

def papers_missing_hash():
    """还没算过内容指纹的论文（启动后的回填线程按这份清单慢慢补）。"""
    return [(r["id"], r["path"]) for r in
            q("SELECT id, path FROM papers WHERE pdf_hash IS NULL OR pdf_hash=''")]

# ---------- 全库内容检索（粗粒度） ----------

_LIKE_ESC = str.maketrans({"\\": "\\\\", "%": "\\%", "_": "\\_"})

def _snippet(text: str, kw: str, before: int = 20, after: int = 44) -> str:
    text = " ".join(str(text or "").split())
    i = text.lower().find(kw.lower())
    if i < 0:
        return text[:before + after]
    s, e = max(0, i - before), min(len(text), i + len(kw) + after)
    return ("…" if s else "") + text[s:e] + ("…" if e < len(text) else "")

def _json_text(raw: str) -> str:
    """一眼卡/七问答案存的是 JSON：把里面的字符串值抽出来拼平，片段才不带 JSON 壳。"""
    try:
        v = json.loads(raw)
    except Exception:
        return raw or ""
    parts = []
    def walk(x):
        if isinstance(x, str):
            parts.append(x)
        elif isinstance(x, list):
            for i in x:
                walk(i)
        elif isinstance(x, dict):
            for k, val in x.items():
                if k not in ("anchors", "para"):
                    walk(val)
    walk(v)
    return " ".join(parts)

def search_content(kw: str, limit: int = 30):
    """全库内容检索：**按篇聚合**（粗粒度——告诉你是哪篇、哪里命中，不逐条展开）。

    搜的是"读过的痕迹"：一眼卡 / 七问答案 / 核心主张 / 问答 / 正文段落；
    页边批注与查译不搜——那是页边自己的入口，混进全库搜索只会添噪音。
    LIKE 全扫不建索引：几百篇的库也就几万行短文本，百毫秒级，FTS 的索引与分词
    维护在这个量级不值得。来源按"越像这篇的身份越靠前"排序，同篇只记第一条片段。
    """
    kw = (kw or "").strip()
    if not kw:
        return []
    pat = "%" + kw.translate(_LIKE_ESC) + "%"
    out = {}   # pid -> {"where", "n", "snippet"}，插入序即来源优先级

    def add(rows, where, is_json=False):
        for r in rows:
            raw = _json_text(r["text"]) if is_json else r["text"]
            d = out.get(r["paper_id"])
            if d:
                d["n"] += 1
                if not d["snippet"] and raw:
                    d["snippet"] = _snippet(raw, kw)
            elif raw:
                out[r["paper_id"]] = {"where": where, "n": 1, "snippet": _snippet(raw, kw)}

    add(q("SELECT id AS paper_id, summary AS text FROM papers WHERE summary LIKE ?", (pat,)), "glance", True)
    add(q("SELECT paper_id, json AS text FROM answers WHERE json LIKE ?", (pat,)), "seven", True)
    add(q("SELECT paper_id, text FROM claims WHERE text LIKE ?", (pat,)), "claims")
    add(q("SELECT paper_id, content AS text FROM qa_messages WHERE content LIKE ?", (pat,)), "qa")
    add(q("SELECT paper_id, text FROM paragraphs WHERE text LIKE ? LIMIT 400", (pat,)), "body")
    if not out:
        return []
    ids = list(out)[:limit]
    qmarks = ",".join("?" * len(ids))
    meta = {r["id"]: r for r in q(
        f"SELECT id, title, filename, authors, year FROM papers WHERE id IN ({qmarks})", tuple(ids))}
    res = []
    for pid in ids:
        m = meta.get(pid)      # 搜索和删除赛跑，篇已删就不出
        if not m:
            continue
        d = out[pid]
        res.append({"id": pid, "title": m["title"] or m["filename"], "authors": m["authors"],
                    "year": m["year"], "where": d["where"], "n": d["n"], "snippet": d["snippet"]})
    return res

# 删库清单唯一正本：删一篇文献要扫的全部从表（purge 与 supersede 共用，加表只改这里）
PAPER_TABLES = ("paragraphs", "annotations", "claims", "marginalia", "glossary",
                "qa_messages", "conversations", "paper_collections", "answers", "reading_log")

@contextmanager
def _txn():
    """多语句事务：中途崩了整体回滚，不留半删状态。yield 已 BEGIN 的连接。"""
    with _lock:
        c = _get()
        c.execute("BEGIN")
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise

def purge_paper(pid: str):
    """删一篇文献 = 它的全部痕迹都从本地消失：段落、骨架、眉批、问答会话、分类归属。
    漏掉任何一张表都会留下读不出来的孤儿数据，所以一张一张点名列（见 PAPER_TABLES）。"""
    with _txn() as c:
        for t in PAPER_TABLES:
            c.execute(f"DELETE FROM {t} WHERE paper_id=?", (pid,))
        c.execute("DELETE FROM papers WHERE id=?", (pid,))

# ---------- 同论文识别与版本升级 ----------

def _norm_title(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())

def find_same_title(title: str, exclude_pid: str = ""):
    """归一化标题相同的已有论文（不同文件的"同一篇"：arXiv 换了 v2、换源重下的排版）。
    同一份文件在 find_duplicate 已经拦掉，这里抓的是内容不同的——要不要替换由用户拍板，
    这里只负责找到。标题归一化只留字母数字：大小写、空白、标点全忽略，误报几乎不存在
    （论文标题的唯一性足够高）。库里已有多篇同名时挑**最近导入**的那篇——用户拿来
    新版本要替换的几乎总是最新的一份。"""
    t = _norm_title(title)
    if len(t) < 8:
        return None
    rows = q("SELECT id, title FROM papers WHERE id != ? ORDER BY created_at DESC", (exclude_pid or "",))
    for r in rows:
        if _norm_title(r["title"]) == t:
            return r["id"]
    return None

def supersede(new_pid: str, old_pid: str) -> dict:
    """版本升级：新导入的那份（new）顶掉旧篇（old）。

    跟着论文走的软资产整表改主：问答会话、术语、分类、阅读日志；Zotero 回填过的
    作者/年份比新解析的准，老的非空就带过来。挂在段落号上的东西不迁——新版的段落
    结构已经变了，旧锚点留着只会指错地方（析读与眉批本来就会对新文重跑）；唯独
    用户钉的页边卡按引句文本尽力跟迁（去空白比对），新版里找不回原句的只能丢下，
    丢了几条如实在统计里报。七问/一眼卡等缓存清掉：那是按旧文生成的。一个事务，
    中途崩了整体回滚。
    """
    stats = {"pins": 0, "pins_lost": 0}
    with _txn() as c:
        for t in ("conversations", "qa_messages", "glossary", "reading_log",
                  "paper_collections"):
            c.execute(f"UPDATE {t} SET paper_id=? WHERE paper_id=?", (new_pid, old_pid))
        old = c.execute("SELECT authors, year FROM papers WHERE id=?", (old_pid,)).fetchone()
        if old and (old["authors"] or old["year"]):
            c.execute("UPDATE papers SET authors=COALESCE(?,authors), year=COALESCE(?,year) "
                      "WHERE id=?", (old["authors"], old["year"], new_pid))
        c.execute("DELETE FROM answers WHERE paper_id IN (?,?)", (new_pid, old_pid))
        # 用户钉卡跟迁：引句去空白后在新段落里找（跨版本文本层的空格常有出入）
        new_paras = [(_squash(r["text"]), r["idx"]) for r in
                     c.execute("SELECT idx, text FROM paragraphs WHERE paper_id=?", (new_pid,))]
        for m in c.execute("SELECT id, quote FROM marginalia WHERE paper_id=? "
                           "AND kind IN ('lookup','region','note')", (old_pid,)).fetchall():
            q_ = _squash(m["quote"])[:60]
            hit = next((idx for sq, idx in new_paras if q_ and q_ in sq), None)
            if hit is not None:
                c.execute("UPDATE marginalia SET paper_id=?, para_idx=?, rect=NULL WHERE id=?",
                          (new_pid, hit, m["id"]))
                stats["pins"] += 1
            else:
                stats["pins_lost"] += 1
        for t in PAPER_TABLES:
            c.execute(f"DELETE FROM {t} WHERE paper_id=?", (old_pid,))
        c.execute("DELETE FROM papers WHERE id=?", (old_pid,))
    return stats

def _squash(s: str) -> str:
    return "".join((s or "").split())

# ---------- 现场生成问题的答案缓存 ----------

def _plain_cites(v):
    """历史答案里的 [¶1] 这类方括号引用统一裸成 ¶1——引用就是可点的段落号，
    不该带一身括号。提示词已改，这里兜住升级前生成的旧缓存。"""
    if isinstance(v, str):
        return re.sub(r"\[+(¶[^[\]]*)\]+", r"\1", v)
    if isinstance(v, list):
        return [_plain_cites(x) for x in v]
    if isinstance(v, dict):
        return {k: (_plain_cites(x) if k in ("text", "items") else x) for k, x in v.items()}
    return v

def answers_all(pid: str) -> dict:
    out = {}
    for r in q("SELECT key, json FROM answers WHERE paper_id=?", (pid,)):
        try:
            out[r["key"]] = _plain_cites(json.loads(r["json"]))
        except ValueError:
            pass
    return out

def answer_get(pid: str, key: str):
    rows = q("SELECT json FROM answers WHERE paper_id=? AND key=?", (pid, key))
    if not rows:
        return None
    try:
        return _plain_cites(json.loads(rows[0]["json"]))
    except ValueError:
        return None

def answers_clear(pid: str):
    q("DELETE FROM answers WHERE paper_id=?", (pid,), commit=True)

def answer_put(pid: str, key: str, data):
    q("INSERT OR REPLACE INTO answers(paper_id, key, json) VALUES(?,?,?)",
      (pid, key, json.dumps(data, ensure_ascii=False)), commit=True)

# ---------- 文库分类（Zotero 的 collection 语义：一篇可属于多类） ----------

def collections_list():
    return [dict(r) for r in q(
        "SELECT c.id, c.name, (SELECT COUNT(*) FROM paper_collections p WHERE p.coll_id=c.id) AS n "
        "FROM collections c ORDER BY c.name COLLATE NOCASE")]

def collection_add(name: str) -> int:
    return q_insert("INSERT INTO collections(name, created_at) VALUES(?,?)",
                    (name[:60], time.strftime("%Y-%m-%d %H:%M:%S")))

def collection_rename(cid: int, name: str):
    q("UPDATE collections SET name=? WHERE id=?", (name[:60], cid), commit=True)

def collection_delete(cid: int):
    q("DELETE FROM paper_collections WHERE coll_id=?", (cid,), commit=True)
    q("DELETE FROM collections WHERE id=?", (cid,), commit=True)

def collection_map():
    """paper_id -> [coll_id]：一次查完，左栏不用每篇再问一次。"""
    m = {}
    for r in q("SELECT paper_id, coll_id FROM paper_collections"):
        m.setdefault(r["paper_id"], []).append(r["coll_id"])
    return m

def set_paper_collections(pid: str, cids: list):
    with _lock:
        c = _get()
        c.execute("DELETE FROM paper_collections WHERE paper_id=?", (pid,))
        c.executemany("INSERT OR IGNORE INTO paper_collections(paper_id, coll_id) VALUES(?,?)",
                      [(pid, int(x)) for x in cids])
        c.commit()

# ---------- paragraphs ----------

def replace_paragraphs(pid: str, paras: list):
    """整篇替换段落。**删与插必须在同一个事务里**。

    先 commit 删除、再另起一次 commit 插入的话，中间有一个真实的"这篇 0 段"窗口：
    `GET /paragraphs` 的惰性回填、`_run_marginalia` 取语料都可能落进去，最坏是拿空语料
    算眉批并以"成功"把整页批注覆盖掉。
    """
    with _txn() as c:
        # 论文行已经不在（刚被删）就不插：惰性补解析的几秒里可能撞上 purge
        if not c.execute("SELECT 1 FROM papers WHERE id=?", (pid,)).fetchone():
            c.rollback()
            return
        c.execute("DELETE FROM paragraphs WHERE paper_id=?", (pid,))
        c.executemany(
            "INSERT INTO paragraphs(paper_id, idx, page, bbox, text, in_refs, lines) VALUES(?,?,?,?,?,?,?)",
            [(pid, p["idx"], p["page"], json.dumps(p["bbox"]), p["text"],
              1 if p.get("in_refs") else 0, json.dumps(p.get("lines") or [])) for p in paras])

def get_paragraphs(pid: str, with_lines: bool = True):
    """with_lines=False 给纯文本消费方（问答/析读语料）：行级坐标是段落里最重的一块，
    不画线就不必反序列化它。"""
    rows = q("SELECT idx, page, bbox, text, in_refs, lines FROM paragraphs WHERE paper_id=? ORDER BY idx", (pid,))
    return [dict(r, bbox=json.loads(r["bbox"]), in_refs=bool(r["in_refs"]),
                 lines=(json.loads(r["lines"]) if (with_lines and r["lines"]) else [])) for r in rows]

def has_paragraphs(pid: str) -> bool:
    """只判"有没有文字层"时用这个：整载全部段落（含 bbox/lines 两坨 JSON）只为一判空，太重。"""
    return q("SELECT EXISTS(SELECT 1 FROM paragraphs WHERE paper_id=?) AS e", (pid,))[0]["e"] == 1

def count_paragraphs(pid: str) -> int:
    """只要段数（页眉的 读至 ¶n/m）时用这个，别为计数整载。"""
    return q("SELECT COUNT(*) AS c FROM paragraphs WHERE paper_id=?", (pid,))[0]["c"]

def get_paragraph(pid: str, idx: int):
    """单段读取（段落查译只碰一段）：全量拉一遍再挑一段是热路径上的浪费。"""
    rows = q("SELECT idx, page, bbox, text, in_refs, lines FROM paragraphs WHERE paper_id=? AND idx=?",
             (pid, idx))
    if not rows:
        return None
    r = rows[0]
    return dict(r, bbox=json.loads(r["bbox"]), in_refs=bool(r["in_refs"]),
                lines=(json.loads(r["lines"]) if r["lines"] else []))

def paragraphs_need_lines(pid: str) -> bool:
    """旧库里的段落没有行级坐标：拿这个判断要不要重解析一次。"""
    rows = q("SELECT lines FROM paragraphs WHERE paper_id=?", (pid,))
    return bool(rows) and all(not r["lines"] for r in rows)

def paragraphs_match(pid: str, paras: list) -> bool:
    """新解析出来的段落和库里存的**是不是同一批**（段数一样、每段的正文也一样）。

    为什么要问这个：行级坐标是靠"重解析一次"补的，而补的动作是整表替换。批注、主张锚点、
    页边笔迹全是按 para_idx 指位置的——只要新解析把段落分组改了一点点（解析规则演进过、
    或者这份 PDF 抽出来的结果本来就不稳定），替换就会把这些锚点整体挪位，而且是**静默**的。
    对不上时宁可这次不补（页边退回按段落框画），也别把用户已有的批注挪到别的段上。
    """
    rows = q("SELECT idx, text FROM paragraphs WHERE paper_id=? ORDER BY idx", (pid,))
    if len(rows) != len(paras):
        return False
    for r, p in zip(rows, paras):
        if int(r["idx"]) != int(p["idx"]):
            return False
        if (r["text"] or "").strip() != (p["text"] or "").strip():
            return False
    return True

# ---------- skeleton ----------

def set_analysis(pid: str, claims: list, annos: list, status: str = "done", error: str = None):
    with _lock:
        c = _get()
        # 存在性检查必须在锁内：purge 的事务可能插在 get 与写之间，把孤儿行插回死篇
        if not c.execute("SELECT 1 FROM papers WHERE id=?", (pid,)).fetchone():
            return
        c.execute("DELETE FROM annotations WHERE paper_id=?", (pid,))
        c.execute("DELETE FROM claims WHERE paper_id=?", (pid,))
        c.executemany("INSERT INTO claims(paper_id, cid, text, anchors) VALUES(?,?,?,?)",
                      [(pid, c_["id"], c_["text"], json.dumps(c_.get("anchors", []))) for c_ in claims])
        c.executemany("INSERT OR REPLACE INTO annotations(paper_id, para_idx, role, purpose) VALUES(?,?,?,?)",
                      [(pid, int(k), v["role"], v.get("purpose", "")) for k, v in annos.items()])
        c.execute("UPDATE papers SET analysis_status=?, analysis_error=? WHERE id=?", (status, error, pid))
        c.commit()

def clear_ai_results() -> int:
    """清掉"模型生成的、按篇缓存的"那些产物（换演示/真实模式时用）。

    只清模型产物：claims/annotations/marginalia 不动（那些是用户读过的成果，
    重算代价高、而且换个模式未必想重来一遍）；这里清的是看一眼就重算得出来的卡片。
    """
    cols = ("summary", "suggest", "advisor", "method_card", "citation", "abbrs", "evidence_qs")
    n = q("SELECT COUNT(*) FROM papers")[0][0]
    with _lock:
        c = _get()
        c.execute("UPDATE papers SET " + ", ".join(f"{k}=NULL" for k in cols))
        c.execute("DELETE FROM answers")
        c.commit()
    return n

def fig_caps(pid: str) -> dict:
    """灯箱图注的中文翻译缓存（"页:x0:y0" → 译文）。图不算析读产物，换模式不清。"""
    rows = q("SELECT fig_caps FROM papers WHERE id=?", (pid,))
    raw = rows[0]["fig_caps"] if rows else None
    try:
        return json.loads(raw) if raw else {}
    except Exception:
        return {}

def set_fig_caps(pid: str, caps: dict):
    q("UPDATE papers SET fig_caps=? WHERE id=?", (json.dumps(caps, ensure_ascii=False), pid), commit=True)

def fail_analysis(pid: str, error: str):
    """析读失败：**只记状态与原因，不动已经存在的 claims/annotations**。

    为什么单独开一个：失败绝不能走会删表的 set_analysis（
    它先把两张表删空再写——一次限流/超时就把用户刚才花过 token 读出来的骨架清空了。
    重算失败应该只是"这次没成"，不该把上次的成果一起赔进去
    （summarize_dialog 早就是"失败退回原摘要"的写法，这里是同一个道理）。"""
    q("UPDATE papers SET analysis_status='error', analysis_error=? WHERE id=?",
      (error, pid), commit=True)

def fail_marginalia(pid: str, error: str):
    """眉批失败：同理，AI 写的那批**留着**，只把状态与原因写下来（用户自己钉的本来就留着）。"""
    q("UPDATE papers SET marginalia_status='error', marginalia_error=? WHERE id=?",
      (error, pid), commit=True)

def get_analysis(pid: str):
    paper = get_paper(pid)
    rows = [{"id": r["cid"], "text": r["text"], "anchors": json.loads(r["anchors"])}
            for r in q("SELECT cid, text, anchors FROM claims WHERE paper_id=?", (pid,))]
    # 按编号的数字部分排（C2 在 C10 前面）：字典序会把 10+ 条主张排成 C1,C10,C11,…,C2
    def _num(r):
        m = re.search(r"\d+", str(r["id"]) or "")
        return int(m.group()) if m else 0
    claims = sorted(rows, key=_num)
    annos = {str(r["para_idx"]): {"role": r["role"], "purpose": r["purpose"]}
             for r in q("SELECT para_idx, role, purpose FROM annotations WHERE paper_id=?", (pid,))}
    return paper["analysis_status"], claims, annos

def glossary_list(pid: str):
    return [dict(r) for r in q("SELECT * FROM glossary WHERE paper_id=? ORDER BY term_en", (pid,))]

def glossary_add(pid: str, term_en: str, term_zh: str, source: str = "manual") -> int:
    # 手工添加也去重：同一个英文词条补一次中文译法=改这条，而不是插出两行。
    # 判重与插入必须在同一把锁里：拆开的 TOCTOU 会让并发同词插出两行（实测踩过）
    with _lock:
        c = _get()
        row = c.execute(
            "SELECT id FROM glossary WHERE paper_id=? AND term_en=?", (pid, term_en)).fetchone()
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        if row:
            c.execute("UPDATE glossary SET term_zh=? WHERE id=?", (term_zh, row["id"]))
            c.commit()
            return row["id"]
        cur = c.execute("INSERT INTO glossary(paper_id, term_en, term_zh, source, created_at)"
                        " VALUES(?,?,?,?,?)",
                        (pid, term_en, term_zh, source, now))
        rowid = cur.lastrowid
        c.commit()
        return rowid

def glossary_delete(gid: int):
    q("DELETE FROM glossary WHERE id=?", (gid,), commit=True)

def glossary_put_ai(pid: str, terms: list):
    """把模型发掘出来的术语整批写进这一篇（替换上一批 AI 词，读者的手写词不动）。"""
    with _lock:
        _get().execute("DELETE FROM glossary WHERE paper_id=? AND source='ai'", (pid,))
        _get().executemany(
            "INSERT INTO glossary(paper_id, term_en, term_zh, source, created_at)"
            " VALUES(?,?,?,?,?)",
            [(pid, t["en"], t["zh"], "ai",
              time.strftime("%Y-%m-%d %H:%M:%S")) for t in terms])
        _get().commit()

def merge_abbrs(pid: str, abbrs: dict) -> int:
    """把新发掘到的缩写并进这一篇的缩写表，返回补进去几条。

    只补缺、不覆盖：骨架那一次抽到的写法（"CNT": "carbon nanotube，碳纳米管"）是跟着
    正文语境来的，比术语那一次更贴原文，后来的一批不该把它顶掉。
    """
    if not isinstance(abbrs, dict) or not abbrs:
        return 0
    row = q("SELECT abbrs FROM papers WHERE id=?", (pid,))
    if not row:
        return 0
    try:
        cur = json.loads(row[0]["abbrs"] or "{}")
    except Exception:
        cur = {}
    if not isinstance(cur, dict):
        cur = {}
    added = 0
    for k, v in abbrs.items():
        k, v = str(k).strip(), str(v).strip()
        if k and v and k not in cur:
            cur[k], added = v, added + 1
    if added:
        update_paper(pid, abbrs=json.dumps(cur, ensure_ascii=False))
    return added

def glossary_hit(pid: str, text: str):
    """这一篇的词里，有哪些出现在给定文本里（大小写不敏感的子串匹配）。
    单篇版复用跨篇版：命中结构去掉 paper_id 就是原样。"""
    return [{k: v for k, v in h.items() if k != "paper_id"} for h in glossary_hits_all([pid], text)]

def glossary_hits_all(pids: list, text: str):
    """跨篇版 glossary_hit：几篇的词一起查，命中时带上所属篇 id——
    跨文献提问的术语句（"这个缩写在 A 里指什么、在 B 里又指什么"）靠它区分来源。"""
    pids = [x for x in pids if x]          # id 是 uuid 短哈希（含字母），不能 int() 强转
    if not pids or not (text or "").strip():
        return []
    qmarks = ",".join("?" * len(pids))
    low = text.lower()
    hits = []
    for r in q(f"SELECT paper_id, term_en, term_zh FROM glossary WHERE paper_id IN ({qmarks}) ORDER BY paper_id",
               tuple(pids)):
        if (r["term_en"] or "").lower() in low:
            hits.append({"paper_id": r["paper_id"], "en": r["term_en"], "zh": r["term_zh"]})
    return hits

# ---------- QA ----------

def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")

NEW_CONV = "新对话"      # 默认会话名：比对（自动改标题的判定）与写入共用这一个口径

def conv_create(pid: str, title: str = NEW_CONV) -> int:
    now = _now()
    return q_insert("INSERT INTO conversations(paper_id, title, created_at, updated_at) VALUES(?,?,?,?)",
                    (pid, title[:60], now, now))

def conv_list(pid: str):
    """一篇论文的会话列表。第一次问之前也会有一个默认会话，免得"没有会话"成为
    一条要前端特判的分支——列表永远至少有一条，永远是它被选中。

    带上每条会话的问答数 `n`：**不给界面显示**（名字后面挂个"· 4"只会把名字挤短），
    而是给前端过滤用——点了几下 ＋ 又没问的空会话不必留在选择器里，
    它们一条消息都没有、还都叫"新对话"，挂着纯是噪音。
    """
    if not q("SELECT id FROM conversations WHERE paper_id=?", (pid,)):
        # INSERT-if-absent 一条语句完成：两个窗口同时首拉也不会建出两条「新对话」
        now = _now()
        q("INSERT INTO conversations(paper_id, title, created_at, updated_at) "
          "SELECT ?, ?, ?, ? WHERE NOT EXISTS (SELECT 1 FROM conversations WHERE paper_id=?)",
          (pid, NEW_CONV, now, now, pid), commit=True)
    _adopt_orphan_qa(pid)
    return [dict(r) for r in q(
        "SELECT c.id, c.title, c.updated_at, c.summary, c.summary_upto, "
        "  (SELECT COUNT(*) FROM qa_messages m WHERE m.conv_id=c.id) AS n "
        "FROM conversations c WHERE c.paper_id=? ORDER BY c.updated_at DESC, c.id DESC", (pid,))]

def _adopt_orphan_qa(pid: str):
    """旧库的问答没有会话号：给它们单开一摊"此前的提问"，别让历史粘在新对话里。"""
    if not q("SELECT id FROM qa_messages WHERE paper_id=? AND conv_id IS NULL", (pid,)):
        return
    cid = conv_create(pid, "此前的提问")
    q("UPDATE qa_messages SET conv_id=? WHERE paper_id=? AND conv_id IS NULL", (cid, pid), commit=True)

def conv_rename(cid: int, title: str):
    q("UPDATE conversations SET title=? WHERE id=?", (title[:60], cid), commit=True)

def conv_delete(cid: int):
    q("DELETE FROM qa_messages WHERE conv_id=?", (cid,), commit=True)
    q("DELETE FROM conversations WHERE id=?", (cid,), commit=True)

def conv_set_summary(cid: int, summary: str, upto: int):
    q("UPDATE conversations SET summary=?, summary_upto=? WHERE id=?", (summary, int(upto), cid), commit=True)

def conv_touch_summary(cid: int):
    """删过一条已经被折进摘要的消息：摘要就不算数了，清掉让它按剩下的原文重建。
    "删掉的那条不进上下文"这条规矩，对摘要同样成立。"""
    c = conv_get(cid)
    if c and c.get("summary"):
        q("UPDATE conversations SET summary='', summary_upto=0 WHERE id=?", (cid,), commit=True)

def conv_get(cid: int):
    rows = q("SELECT * FROM conversations WHERE id=?", (cid,))
    return dict(rows[0]) if rows else None

def conv_touch(cid: int):
    q("UPDATE conversations SET updated_at=? WHERE id=?", (_now(), cid), commit=True)

def qa_add(pid: str, role: str, content: str, citations: list = None, conv_id: int = None) -> int:
    """写一条问答。会话已经被删掉时**不写**（返回 0）——否则会留下一条谁也看不到的孤儿，
    用户流式提问到一半把会话删了就会踩到。"""
    if conv_id and not q("SELECT id FROM conversations WHERE id=?", (conv_id,)):
        return 0
    mid = q_insert("INSERT INTO qa_messages(paper_id, role, content, citations, conv_id, created_at) VALUES(?,?,?,?,?,?)",
                   (pid, role, content, json.dumps(citations or []), conv_id, _now()))
    if conv_id:
        conv_touch(conv_id)
    return mid

def qa_last_user_id(pid: str, conv_id: int):
    rows = q("SELECT id FROM qa_messages WHERE paper_id=? AND conv_id=? AND role='user' ORDER BY id DESC LIMIT 1",
             (pid, conv_id))
    return rows[0]["id"] if rows else None

def qa_history(pid: str, conv_id: int = None, limit: int = 200):
    if conv_id:
        rows = q("SELECT role, content, citations, id, conv_id FROM qa_messages "
                 "WHERE paper_id=? AND conv_id=? ORDER BY id LIMIT ?", (pid, conv_id, limit * 4))
    else:
        rows = q("SELECT role, content, citations, id, conv_id FROM (SELECT * FROM qa_messages "
                 "WHERE paper_id=? ORDER BY id DESC LIMIT ?) ORDER BY id ASC", (pid, limit))
    return [dict(r, citations=json.loads(r["citations"])) for r in rows]

def qa_drop_last_assistant(pid: str, conv_id: int):
    """重新生成 = 把最后一条回答删掉，连同它的用户问题一起交回前端重问。
    返回被删掉的那条用户问题（没有就返回 None）。"""
    rows = q("SELECT id, role, content FROM qa_messages WHERE paper_id=? AND conv_id=? ORDER BY id DESC LIMIT 2",
             (pid, conv_id))
    last = next((r for r in rows if r["role"] == "assistant"), None)
    if last:
        q("DELETE FROM qa_messages WHERE id=?", (last["id"],), commit=True)
    prev = next((r for r in rows if r["role"] == "user" and (not last or r["id"] < last["id"])), None)
    if prev:
        q("DELETE FROM qa_messages WHERE id=?", (prev["id"],), commit=True)
    _touch_summary_if_covering(conv_id, last["id"] if last else 0)
    return prev["content"] if prev else None

def _touch_summary_if_covering(conv_id: int, upto: int):
    """删掉的消息已在对话摘要覆盖范围内就作废摘要（摘要描述的消息少了一条）。"""
    c = conv_get(conv_id)
    if c and (c.get("summary_upto") or 0) >= upto:
        conv_touch_summary(conv_id)

def qa_delete(mid: int):
    rows = q("SELECT conv_id FROM qa_messages WHERE id=?", (mid,))
    q("DELETE FROM qa_messages WHERE id=?", (mid,), commit=True)
    if rows and rows[0]["conv_id"]:
        _touch_summary_if_covering(rows[0]["conv_id"], mid)

def qa_clear(pid: str):
    q("DELETE FROM qa_messages WHERE paper_id=?", (pid,), commit=True)
    q("DELETE FROM conversations WHERE paper_id=?", (pid,), commit=True)

# ---------- 眉批（句级人性化批注） ----------

def set_marginalia(pid: str, notes: list, status: str = "done", error: str = None):
    """写入 AI 眉批（整篇重写）。

    只删 AI 写的那几种，**用户自己钉的（lookup/region/note）一根都不动**——
    这张表里住着两种人写的东西，前者可以重算，后者是读者的资产，重算眉批不该顺手把它抹了。
    """
    with _lock:
        c = _get()
        if not c.execute("SELECT 1 FROM papers WHERE id=?", (pid,)).fetchone():
            return
        c.execute("DELETE FROM marginalia WHERE paper_id=? AND kind NOT IN ('lookup','region','note')", (pid,))
        c.executemany(
            "INSERT INTO marginalia(paper_id, para_idx, page, quote, kind, note, label, band) "
            "VALUES(?,?,?,?,?,?,?,?)",
            [(pid, n["para_idx"], n["page"], n["quote"], n["kind"], n["note"],
              n.get("label", ""), n.get("band", "")) for n in notes])
        c.execute("UPDATE papers SET marginalia_status=? WHERE id=?", (status, pid))
        c.commit()
    if error:
        q("UPDATE papers SET marginalia_error=? WHERE id=?", (error, pid), commit=True)

def get_marginalia(pid: str):
    return [dict(r) for r in q(
        "SELECT id, para_idx, page, quote, kind, note, label, band, rect FROM marginalia "
        "WHERE paper_id=? ORDER BY page, para_idx", (pid,))]

def marginalia_set_rect(mid: int, rect: dict):
    q("UPDATE marginalia SET rect=? WHERE id=?", (json.dumps(rect), mid), commit=True)

def marginalia_add(pid: str, para_idx: int, page: int, quote: str, note: str, kind: str = "lookup",
                   rect: dict = None, label: str = "", band: str = "") -> int:
    return q_insert("INSERT INTO marginalia(paper_id, para_idx, page, quote, kind, note, rect, label, band) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (pid, para_idx, page, quote, kind, note, json.dumps(rect) if rect else None, label, band))

def marginalia_delete(mid: int):
    q("DELETE FROM marginalia WHERE id=?", (mid,), commit=True)

# ---------- 论文日历 ----------

def log_read(pid: str, day: str):
    """打开论文时记一笔当天阅读。INSERT OR IGNORE：一天一篇只有一行。"""
    q("INSERT OR IGNORE INTO reading_log(day, paper_id) VALUES(?,?)", (day, pid), commit=True)

def reading_days(month: str):
    """某个月（'YYYY-MM' 前缀）的全部阅读日志。"""
    return [dict(r) for r in q("SELECT day, paper_id FROM reading_log WHERE day LIKE ?", (month + "%",))]

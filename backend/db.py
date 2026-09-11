"""SQLite 存储：论文、段落、骨架标注、术语表、问答。全部本地。"""
import json
import os
import sqlite3
import threading
import time
import uuid

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
  summary TEXT
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
  id INTEGER PRIMARY KEY AUTOINCREMENT, term_en TEXT, term_zh TEXT,
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
-- 文库分类：一篇文献可以同时属于多个分类（Zotero 的 collection 语义，不是文件夹）
CREATE TABLE IF NOT EXISTS collections(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS paper_collections(
  paper_id TEXT, coll_id INTEGER, PRIMARY KEY(paper_id, coll_id)
);
"""


def _get() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
        _migrate(_conn)
        _conn.commit()
    return _conn


def _migrate(c: sqlite3.Connection):
    """惰性迁移：旧库补列。"""
    for stmt in (
        "ALTER TABLE papers ADD COLUMN mono_path TEXT",
        "ALTER TABLE annotations ADD COLUMN inferred_role TEXT",
        "ALTER TABLE papers ADD COLUMN suggest TEXT",
        "ALTER TABLE papers ADD COLUMN advisor TEXT",
        "ALTER TABLE papers ADD COLUMN method_card TEXT",
        "ALTER TABLE papers ADD COLUMN abbrs TEXT",
        "ALTER TABLE papers ADD COLUMN evidence_qs TEXT",
        "ALTER TABLE papers ADD COLUMN advisor TEXT",
        "ALTER TABLE paragraphs ADD COLUMN lines TEXT",   # 行级坐标：页边引文要按行画
        "ALTER TABLE qa_messages ADD COLUMN conv_id INTEGER",   # 旧问答没有会话，迁移到默认会话
        "ALTER TABLE papers ADD COLUMN last_read_at TEXT",      # 上次读到什么时候（文库排序用）
        "ALTER TABLE papers ADD COLUMN authors TEXT",           # 第一作者（文库列表上就显示这一条）
        "ALTER TABLE conversations ADD COLUMN summary TEXT",    # 较早对话压缩成的摘要（不丢关键信息）
        "ALTER TABLE conversations ADD COLUMN summary_upto INTEGER DEFAULT 0",  # 摘要已折到哪一条
    ):
        try:
            c.execute(stmt)
        except sqlite3.OperationalError:
            pass


def q(sql: str, params=(), commit: bool = False):
    with _lock:
        cur = _get().execute(sql, params)
        rows = cur.fetchall()
        if commit:
            _get().commit()
        return rows


def new_id() -> str:
    return uuid.uuid4().hex[:10]


# ---------- papers ----------

def create_paper(pid: str, filename: str, title: str, path: str, n_pages: int, authors: str = "") -> str:
    q("INSERT INTO papers(id, filename, title, path, n_pages, authors, created_at) VALUES(?,?,?,?,?,?,?)",
      (pid, filename, title, path, n_pages, authors, time.strftime("%Y-%m-%d %H:%M:%S")), commit=True)
    return pid


def list_papers():
    return [dict(r) for r in q(
        "SELECT id, filename, title, authors, n_pages, created_at, last_read_at, analysis_status, "
        "marginalia_status, translate_status FROM papers ORDER BY created_at DESC")]


def get_paper(pid: str):
    rows = q("SELECT * FROM papers WHERE id=?", (pid,))
    return dict(rows[0]) if rows else None


def update_paper(pid: str, **fields):
    keys = ",".join(f"{k}=?" for k in fields)
    q(f"UPDATE papers SET {keys} WHERE id=?", (*fields.values(), pid), commit=True)


def purge_paper(pid: str):
    """删一篇文献 = 它的全部痕迹都从本地消失：段落、骨架、眉批、问答会话、分类归属。
    漏掉任何一张表都会留下读不出来的孤儿数据，所以这里一张一张点名列。"""
    for t in ("paragraphs", "annotations", "claims", "marginalia",
              "qa_messages", "conversations", "paper_collections"):
        q(f"DELETE FROM {t} WHERE paper_id=?", (pid,), commit=True)
    q("DELETE FROM papers WHERE id=?", (pid,), commit=True)


# ---------- 文库分类（Zotero 的 collection 语义：一篇可属于多类） ----------

def collections_list():
    return [dict(r) for r in q(
        "SELECT c.id, c.name, (SELECT COUNT(*) FROM paper_collections p WHERE p.coll_id=c.id) AS n "
        "FROM collections c ORDER BY c.name COLLATE NOCASE")]


def collection_add(name: str) -> int:
    q("INSERT INTO collections(name, created_at) VALUES(?,?)",
      (name[:60], time.strftime("%Y-%m-%d %H:%M:%S")), commit=True)
    return q("SELECT last_insert_rowid() AS i")[0]["i"]


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
    q("DELETE FROM paragraphs WHERE paper_id=?", (pid,), commit=True)
    with _lock:
        _get().executemany(
            "INSERT INTO paragraphs(paper_id, idx, page, bbox, text, in_refs, lines) VALUES(?,?,?,?,?,?,?)",
            [(pid, p["idx"], p["page"], json.dumps(p["bbox"]), p["text"], 1 if p.get("in_refs") else 0,
              json.dumps(p.get("lines") or [])) for p in paras])
        _get().commit()


def get_paragraphs(pid: str):
    rows = q("SELECT idx, page, bbox, text, in_refs, lines FROM paragraphs WHERE paper_id=? ORDER BY idx", (pid,))
    return [dict(r, bbox=json.loads(r["bbox"]), in_refs=bool(r["in_refs"]),
                 lines=json.loads(r["lines"]) if r["lines"] else []) for r in rows]


def paragraphs_need_lines(pid: str) -> bool:
    """旧库里的段落没有行级坐标：拿这个判断要不要重解析一次。"""
    rows = q("SELECT lines FROM paragraphs WHERE paper_id=?", (pid,))
    return bool(rows) and all(not r["lines"] for r in rows)


# ---------- skeleton ----------

def set_analysis(pid: str, claims: list, annos: list, status: str = "done", error: str = None):
    if not get_paper(pid):
        return          # 后台析读跑完时这篇可能已被删除：别往空论文上灌数据
    with _lock:
        c = _get()
        c.execute("DELETE FROM annotations WHERE paper_id=?", (pid,))
        c.execute("DELETE FROM claims WHERE paper_id=?", (pid,))
        c.executemany("INSERT INTO claims(paper_id, cid, text, anchors) VALUES(?,?,?,?)",
                      [(pid, c_["id"], c_["text"], json.dumps(c_.get("anchors", []))) for c_ in claims])
        c.executemany("INSERT OR REPLACE INTO annotations(paper_id, para_idx, role, inferred_role, purpose, user_override) VALUES(?,?,?,?,?,0)",
                      [(pid, int(k), v["role"], v["role"], v.get("purpose", "")) for k, v in annos.items()])
        c.execute("UPDATE papers SET analysis_status=?, analysis_error=? WHERE id=?", (status, error, pid))
        c.commit()


def get_analysis(pid: str):
    paper = get_paper(pid)
    claims = [{"id": r["cid"], "text": r["text"], "anchors": json.loads(r["anchors"])}
              for r in q("SELECT cid, text, anchors FROM claims WHERE paper_id=? ORDER BY cid", (pid,))]
    annos = {str(r["para_idx"]): {"role": r["role"], "inferred_role": r["inferred_role"] or r["role"],
                                  "purpose": r["purpose"], "user_override": bool(r["user_override"])}
             for r in q("SELECT * FROM annotations WHERE paper_id=?", (pid,))}
    return paper["analysis_status"], claims, annos


def override_annotation(pid: str, para_idx: int, role: str):
    if role:
        q("UPDATE annotations SET role=?, user_override=1 WHERE paper_id=? AND para_idx=?", (role, pid, para_idx), commit=True)
    else:   # 回到推断
        q("UPDATE annotations SET role=inferred_role, user_override=0 WHERE paper_id=? AND para_idx=?", (pid, para_idx), commit=True)


# ---------- glossary ----------

def glossary_list():
    return [dict(r) for r in q("SELECT * FROM glossary ORDER BY id")]


def glossary_add(term_en: str, term_zh: str, domain: str = "", note: str = "", source: str = "manual") -> int:
    q("INSERT INTO glossary(term_en, term_zh, domain, note, source, created_at) VALUES(?,?,?,?,?,?)",
      (term_en, term_zh, domain, note, source, time.strftime("%Y-%m-%d %H:%M:%S")), commit=True)
    return q("SELECT last_insert_rowid() AS i")[0]["i"]


def glossary_delete(gid: int):
    q("DELETE FROM glossary WHERE id=?", (gid,), commit=True)


def glossary_seed(pairs: list):
    existing = {r["term_en"].lower() for r in q("SELECT term_en FROM glossary")}
    with _lock:
        _get().executemany(
            "INSERT INTO glossary(term_en, term_zh, domain, note, source, created_at) VALUES(?,?,?,?,?,?)",
            [(en, zh, domain, "", "seed", time.strftime("%Y-%m-%d %H:%M:%S"))
             for en, zh, domain in pairs if en.lower() not in existing])
        _get().commit()


def glossary_hit(text: str):
    """返回与文本精确子串匹配的术语对（大小写不敏感）。"""
    hits = []
    low = text.lower()
    for r in q("SELECT term_en, term_zh FROM glossary"):
        if r["term_en"].lower() in low:
            hits.append({"en": r["term_en"], "zh": r["term_zh"]})
    return hits


# ---------- QA ----------

def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def conv_create(pid: str, title: str = "新对话") -> int:
    now = _now()
    q("INSERT INTO conversations(paper_id, title, created_at, updated_at) VALUES(?,?,?,?)",
      (pid, title[:60], now, now), commit=True)
    return q("SELECT last_insert_rowid() AS i")[0]["i"]


def conv_list(pid: str):
    """一篇论文的会话列表。第一次问之前也会有一个默认会话，免得"没有会话"成为
    一条要前端特判的分支——列表永远至少有一条，永远是它被选中。"""
    if not q("SELECT id FROM conversations WHERE paper_id=?", (pid,)):
        conv_create(pid, "新对话")
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


def conv_touch(cid: int, title: str = None):
    if title:
        q("UPDATE conversations SET updated_at=?, title=? WHERE id=?", (_now(), title[:60], cid), commit=True)
    else:
        q("UPDATE conversations SET updated_at=? WHERE id=?", (_now(), cid), commit=True)


def qa_add(pid: str, role: str, content: str, citations: list = None, conv_id: int = None) -> int:
    q("INSERT INTO qa_messages(paper_id, role, content, citations, conv_id, created_at) VALUES(?,?,?,?,?,?)",
      (pid, role, content, json.dumps(citations or []), conv_id, _now()), commit=True)
    if conv_id:
        conv_touch(conv_id)
    return q("SELECT last_insert_rowid() AS i")[0]["i"]


def qa_last_user_id(pid: str, conv_id: int):
    rows = q("SELECT id FROM qa_messages WHERE paper_id=? AND conv_id=? AND role='user' ORDER BY id DESC LIMIT 1",
             (pid, conv_id))
    return rows[0]["id"] if rows else None


def qa_history(pid: str, conv_id: int = None, limit: int = 200):
    if conv_id:
        rows = q("SELECT role, content, citations, id, conv_id FROM qa_messages "
                 "WHERE paper_id=? AND conv_id=? ORDER BY id", (pid, conv_id))
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
    # 撤掉的两条如果已经折进摘要，摘要同样要作废（否则撤掉的内容还在上下文里）
    c = conv_get(conv_id)
    if c and (c.get("summary_upto") or 0) >= (last["id"] if last else 0):
        conv_touch_summary(conv_id)
    return prev["content"] if prev else None


def qa_delete(mid: int):
    rows = q("SELECT conv_id FROM qa_messages WHERE id=?", (mid,))
    q("DELETE FROM qa_messages WHERE id=?", (mid,), commit=True)
    if rows and rows[0]["conv_id"]:
        c = conv_get(rows[0]["conv_id"])
        if c and (c.get("summary_upto") or 0) >= mid:
            conv_touch_summary(c["id"])


def qa_clear(pid: str):
    q("DELETE FROM qa_messages WHERE paper_id=?", (pid,), commit=True)
    q("DELETE FROM conversations WHERE paper_id=?", (pid,), commit=True)


# ---------- 眉批（句级人性化批注） ----------

def set_marginalia(pid: str, notes: list, status: str = "done", error: str = None):
    if not get_paper(pid):
        return
    with _lock:
        c = _get()
        c.execute("DELETE FROM marginalia WHERE paper_id=?", (pid,))
        c.executemany(
            "INSERT INTO marginalia(paper_id, para_idx, page, quote, kind, note) VALUES(?,?,?,?,?,?)",
            [(pid, n["para_idx"], n["page"], n["quote"], n["kind"], n["note"]) for n in notes])
        c.execute("UPDATE papers SET marginalia_status=? WHERE id=?", (status, pid))
        c.commit()
    if error:
        q("UPDATE papers SET marginalia_error=? WHERE id=?", (error, pid), commit=True)


def get_marginalia(pid: str):
    return [dict(r) for r in q(
        "SELECT id, para_idx, page, quote, kind, note, rect FROM marginalia WHERE paper_id=? ORDER BY page, para_idx",
        (pid,))]


def marginalia_set_rect(mid: int, rect: dict):
    q("UPDATE marginalia SET rect=? WHERE id=?", (json.dumps(rect), mid), commit=True)


def marginalia_add(pid: str, para_idx: int, page: int, quote: str, note: str, kind: str = "lookup",
                   rect: dict = None) -> int:
    q("INSERT INTO marginalia(paper_id, para_idx, page, quote, kind, note, rect) VALUES(?,?,?,?,?,?,?)",
      (pid, para_idx, page, quote, kind, note, json.dumps(rect) if rect else None), commit=True)
    return q("SELECT last_insert_rowid() AS i")[0]["i"]


def marginalia_delete(mid: int):
    q("DELETE FROM marginalia WHERE id=?", (mid,), commit=True)

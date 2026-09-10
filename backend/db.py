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
  paper_id TEXT, idx INTEGER, page INTEGER, bbox TEXT, text TEXT, in_refs INTEGER DEFAULT 0,
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

def create_paper(pid: str, filename: str, title: str, path: str, n_pages: int) -> str:
    q("INSERT INTO papers(id, filename, title, path, n_pages, created_at) VALUES(?,?,?,?,?,?)",
      (pid, filename, title, path, n_pages, time.strftime("%Y-%m-%d %H:%M:%S")), commit=True)
    return pid


def list_papers():
    return [dict(r) for r in q(
        "SELECT id, filename, title, n_pages, created_at, analysis_status, marginalia_status, translate_status FROM papers ORDER BY created_at DESC")]


def get_paper(pid: str):
    rows = q("SELECT * FROM papers WHERE id=?", (pid,))
    return dict(rows[0]) if rows else None


def update_paper(pid: str, **fields):
    keys = ",".join(f"{k}=?" for k in fields)
    q(f"UPDATE papers SET {keys} WHERE id=?", (*fields.values(), pid), commit=True)


# ---------- paragraphs ----------

def replace_paragraphs(pid: str, paras: list):
    q("DELETE FROM paragraphs WHERE paper_id=?", (pid,), commit=True)
    with _lock:
        _get().executemany(
            "INSERT INTO paragraphs(paper_id, idx, page, bbox, text, in_refs) VALUES(?,?,?,?,?,?)",
            [(pid, p["idx"], p["page"], json.dumps(p["bbox"]), p["text"], 1 if p.get("in_refs") else 0) for p in paras])
        _get().commit()


def get_paragraphs(pid: str):
    rows = q("SELECT idx, page, bbox, text, in_refs FROM paragraphs WHERE paper_id=? ORDER BY idx", (pid,))
    return [dict(r, bbox=json.loads(r["bbox"]), in_refs=bool(r["in_refs"])) for r in rows]


# ---------- skeleton ----------

def set_analysis(pid: str, claims: list, annos: list, status: str = "done", error: str = None):
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

def qa_add(pid: str, role: str, content: str, citations: list = None):
    q("INSERT INTO qa_messages(paper_id, role, content, citations, created_at) VALUES(?,?,?,?,?)",
      (pid, role, content, json.dumps(citations or []), time.strftime("%Y-%m-%d %H:%M:%S")), commit=True)


def qa_history(pid: str, limit: int = 20):
    rows = q("SELECT role, content, citations FROM (SELECT * FROM qa_messages WHERE paper_id=? ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
             (pid, limit))
    return [dict(r, citations=json.loads(r["citations"])) for r in rows]


def qa_clear(pid: str):
    q("DELETE FROM qa_messages WHERE paper_id=?", (pid,), commit=True)


# ---------- 眉批（句级人性化批注） ----------

def set_marginalia(pid: str, notes: list, status: str = "done", error: str = None):
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


def marginalia_add(pid: str, para_idx: int, page: int, quote: str, note: str, kind: str = "lookup") -> int:
    q("INSERT INTO marginalia(paper_id, para_idx, page, quote, kind, note) VALUES(?,?,?,?,?,?)",
      (pid, para_idx, page, quote, kind, note), commit=True)
    return q("SELECT last_insert_rowid() AS i")[0]["i"]


def marginalia_delete(mid: int):
    q("DELETE FROM marginalia WHERE id=?", (mid,), commit=True)

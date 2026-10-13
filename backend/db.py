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
_lock = threading.Lock()          # 写事务串行：所有写路径（含 _txn）都在它里面
_conn_lock = threading.Lock()     # 只保护共享写连接的**首次初始化**（见 _get）
_conn = None
_local = threading.local()

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
-- 七问里需要现场生成的那几问（motive 要解决什么、principle 原理、method/how 怎么解决、
-- next 还能做什么、lens 换个学科怎么看、doubt 哪里可疑；键的口径见 llm.py）：按篇缓存，
-- 生成过一次就不再花钱
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
-- 全局学科术语库：跨篇复用的译法（一篇一份的术语表之外的公共层）。
-- 必须是独立的表——glossary 的旧迁移会把 paper_id 为空的行当脏数据删掉（_migrate），
-- 全局词混进那张表活不过一次升级。
CREATE TABLE IF NOT EXISTS glossary_global(
  id INTEGER PRIMARY KEY AUTOINCREMENT, term_en TEXT, term_zh TEXT,
  note TEXT DEFAULT '', created_at TEXT
);
-- 库内互引的立场判定缓存（scite 式）：谁引用了这篇、哪句、支持还是质疑。
-- 判定是一次模型调用，按（被引篇, 引用篇）缓存；本地扫描本身免费，不进这张表。
CREATE TABLE IF NOT EXISTS cite_ctx(
  paper_id TEXT, citing_id TEXT, hits TEXT, PRIMARY KEY(paper_id, citing_id)
);
"""

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_qa_msg_conv ON qa_messages(conv_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_qa_msg_paper ON qa_messages(paper_id)",
    "CREATE INDEX IF NOT EXISTS idx_marg_paper ON marginalia(paper_id)",
    "CREATE INDEX IF NOT EXISTS idx_gloss_paper ON glossary(paper_id)",
    "CREATE INDEX IF NOT EXISTS idx_conv_paper ON conversations(paper_id)",
    "CREATE INDEX IF NOT EXISTS idx_papers_hash ON papers(pdf_hash)",
    "CREATE INDEX IF NOT EXISTS idx_papers_created ON papers(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_papers_filename ON papers(filename)",
    # 分类计数（collections_list / library overview）对每个分类跑一次
    # WHERE p.coll_id=c.id 的相关子查询；这张关联表此前没有索引，库一大就是逐分类全表扫
    "CREATE INDEX IF NOT EXISTS idx_paper_colls_coll ON paper_collections(coll_id)",
)

def _get() -> sqlite3.Connection:
    global _conn
    if _conn is not None:
        return _conn
    # 首用并发：两个线程同时进来会各建一条连接、各自跑一遍 executescript + _migrate，
    # 一条连接被丢弃（泄漏），而且两条 DDL 撞在一起就是随机 "database is locked"。
    # 这里单独一把初始化锁，与 _lock 分开——_get 常在 _lock 里被调用，
    # 共用一个非可重入锁会当场自锁。
    with _conn_lock:
        if _conn is None:
            c = sqlite3.connect(DB_PATH, check_same_thread=False)
            try:
                c.row_factory = sqlite3.Row
                c.executescript(SCHEMA)
                _migrate(c)   # 必须先补列再建索引：idx_qa_msg_conv/idx_papers_hash 的列来自迁移，
                for stmt in _INDEXES:   # 顺序反了会 OperationalError 被吞，索引从此再也建不上
                    try:
                        c.execute(stmt)
                    except sqlite3.OperationalError:
                        pass
                try:      # WAL：读写不再互斥全库 fsync；synchronous=NORMAL 够本地应用
                    c.execute("PRAGMA journal_mode=WAL")
                    c.execute("PRAGMA synchronous=NORMAL")
                except sqlite3.OperationalError:
                    pass
                c.commit()
            except Exception:
                c.close()   # 库文件损坏时别把半初始化的连接挂到全局上：那会让之后每个查询
                raise       # 都死在"no such table"，而不是第一次就报清楚
            _conn = c
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

def _get_read() -> sqlite3.Connection:
    """线程自己的只读连接。WAL 允许任意多读者与写者并行——过去一把全局锁把
    execute+fetchall 整个串起来，全库搜索（几万行 LIKE 扫几遍）一跑，
    别的窗口连论文列表都刷不动。每线程一条、首次用前先让写连接把 schema 建好。"""
    c = getattr(_local, "conn", None)
    if c is None:
        _get()               # schema/迁移必须先就位：读连接建早了会"no such table"
        c = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=5.0)
        c.row_factory = sqlite3.Row
        try:
            c.execute("PRAGMA busy_timeout=5000")
        except sqlite3.OperationalError:
            pass
        _local.conn = c
    return c

def q(sql: str, params=(), commit: bool = False):
    if commit:
        with _lock:
            cur = _get().execute(sql, params)
            rows = cur.fetchall()
            _get().commit()
            return rows
    return _get_read().execute(sql, params).fetchall()

def q_insert(sql: str, params=()) -> int:
    """INSERT 并取自增 id——两步必须同锁：分开写会拿到别人那行的 rowid。"""
    with _lock:
        cur = _get().execute(sql, params)
        rowid = cur.lastrowid
        _get().commit()
        return rowid

def new_id() -> str:
    return uuid.uuid4().hex[:10]

def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")

def _safe_json(raw, fallback):
    """读侧 JSON 兜底（写侧一律 json.dumps）：NULL/空串/坏 JSON 一律回 fallback。

    老库与外部写入会留下 NULL（json.loads(None) 是 TypeError）和半截坏值；读的时候
    不过这道，一个坏值就把端点打成 500。fallback 逐调用点给：[]、{}、None 各按
    各自的"没有"口径，见各调用处的注释。
    """
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return fallback

# ---------- papers ----------

def create_paper(pid: str, filename: str, title: str, path: str, n_pages: int, authors: str = "") -> str:
    q("INSERT INTO papers(id, filename, title, path, n_pages, authors, created_at) VALUES(?,?,?,?,?,?,?)",
      (pid, filename, title, path, n_pages, authors, _now()), commit=True)
    return pid

def list_papers():
    return [dict(r) for r in q(
        "SELECT id, filename, title, authors, year, n_pages, created_at, last_read_at, analysis_status, "
        "translate_status, plan_day FROM papers ORDER BY created_at DESC")]


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

    "文件名 + 字节数"这把钥匙**必须再对一次内容**：同名同大小但内容是另一份 PDF
    （arXiv 重排、批量重生成的文件名常撞）会误判成重复——新文件被静默丢弃、
    用户拿到的是旧论文。对内容就是算一次库内那份的指纹（只在撞名场景发生），
    顺手把旧库缺的指纹补上。
    """
    if pdf_hash:
        for r in q("SELECT id FROM papers WHERE pdf_hash=?", (pdf_hash,)):
            return r["id"]
    # 先让 SQL 把范围缩到同名，再逐个 stat 核对字节数（path 可能是 NULL/坏值，stat 炸了就跳过）
    for r in q("SELECT id, path, pdf_hash FROM papers WHERE filename=?", (filename,)):
        try:
            if os.path.getsize(r["path"]) != size:
                continue
        except (OSError, TypeError, ValueError):
            continue
        if not pdf_hash:
            return r["id"]           # 导入方没给指纹（老口径的调用）：维持宽松判重
        have = (r["pdf_hash"] or "").strip()
        if not have:
            import fsutil            # 惰性导入：撞名时才走这条路，正常判重不受影响
            have = fsutil.sha256_file(r["path"], quiet=True) or ""
            if have:
                try:
                    q("UPDATE papers SET pdf_hash=? WHERE id=?", (have, r["id"]), commit=True)
                except Exception:
                    pass
        if have == pdf_hash:
            return r["id"]
        # 同名同大小、内容不同：不是同一份，继续看别的同名候选
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

    add(q("SELECT id AS paper_id, summary AS text FROM papers WHERE summary LIKE ? ESCAPE '\\'", (pat,)), "glance", True)
    add(q("SELECT paper_id, json AS text FROM answers WHERE json LIKE ? ESCAPE '\\'", (pat,)), "seven", True)
    add(q("SELECT paper_id, text FROM claims WHERE text LIKE ? ESCAPE '\\'", (pat,)), "claims")
    add(q("SELECT paper_id, content AS text FROM qa_messages WHERE content LIKE ? ESCAPE '\\' LIMIT 400", (pat,)), "qa")
    add(q("SELECT paper_id, text FROM paragraphs WHERE text LIKE ? ESCAPE '\\' LIMIT 400", (pat,)), "body")
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
                "qa_messages", "conversations", "paper_collections", "answers", "reading_log",
                "cite_ctx")

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
    """删一篇文献 = 它的全部痕迹都从本地消失：段落、骨架、页边批注、问答会话、分类归属，
    以及互引缓存里**它作为引用方**的那一半（作为被引方的行走 PAPER_TABLES 清单）。
    漏掉任何一张表都会留下读不出来的孤儿数据，所以一张一张点名列（见 PAPER_TABLES）。"""
    with _txn() as c:
        for t in PAPER_TABLES:
            c.execute(f"DELETE FROM {t} WHERE paper_id=?", (pid,))
        c.execute("DELETE FROM cite_ctx WHERE citing_id=?", (pid,))
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
    结构已经变了，旧锚点留着只会指错地方（析读本来就会对新文重跑）；唯独
    用户钉的页边卡按引句文本尽力跟迁（去空白比对），新版里找不回原句的只能丢下，
    丢了几条如实在统计里报。七问/一眼卡等缓存清掉：那是按旧文生成的。一个事务，
    中途崩了整体回滚。
    """
    stats = {"pins": 0, "pins_lost": 0}
    with _txn() as c:
        # reading_log(day,paper_id)、paper_collections(paper_id,coll_id)、cite_ctx(paper_id,citing_id)
        # 是复合主键：新篇若已有同 key 的行（同一天读过、放进过同一个分类），UPDATE 撞 PK
        # 会把整个事务掀掉回滚——论文成僵尸。先删旧篇里"新篇已有"的那些行：同一天保留新篇
        # 那份、同一个分类保留新篇那份，合并结果就是并集去重。幂等：重跑一遍无行可删。
        for t, k in (("reading_log", "day"), ("paper_collections", "coll_id"),
                     ("cite_ctx", "citing_id")):
            c.execute(f"DELETE FROM {t} WHERE paper_id=? AND {k} IN "
                      f"(SELECT {k} FROM {t} WHERE paper_id=?)", (old_pid, new_pid))
        for t in ("conversations", "qa_messages", "glossary", "reading_log",
                  "paper_collections", "cite_ctx"):
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
        # 读侧容 NULL（老库/外部写入）——与 qa_history 同口径。"null" 解析成 None =
        # 这一问没有缓存，前端按"没生成过"处理（不会拿空壳当真答案）
        out[r["key"]] = _plain_cites(_safe_json(r["json"], None))
    return out

def answer_get(pid: str, key: str):
    rows = q("SELECT json FROM answers WHERE paper_id=? AND key=?", (pid, key))
    if not rows:
        return None
    return _plain_cites(_safe_json(rows[0]["json"], None))

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
                    (name[:60], _now()))

def collection_rename(cid: int, name: str):
    q("UPDATE collections SET name=? WHERE id=?", (name[:60], cid), commit=True)

def collection_delete(cid: int):
    with _txn() as c:
        c.execute("DELETE FROM paper_collections WHERE coll_id=?", (cid,))
        c.execute("DELETE FROM collections WHERE id=?", (cid,))

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
    `GET /paragraphs` 的惰性回填、七问生成取语料都可能落进去，最坏是拿空语料
    跑生成、把空结论当成功写进缓存。
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
    # 缓存列一律过 _safe_json：老库/半截写盘会留下坏 JSON 或 NULL，直连 json.loads 会让
    # /paragraphs 整个 500（同文件 208 行就是为这件事定的口径）。bbox 另说：pdfparse 写
    # dict、ocr 写 list，兜底形状没法一刀切，这里不动它
    return [dict(r, bbox=json.loads(r["bbox"]), in_refs=bool(r["in_refs"]),
                 lines=(_safe_json(r["lines"], []) if with_lines else [])) for r in rows]

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
                lines=_safe_json(r["lines"], []))

def paragraphs_stale(pid: str) -> bool:
    """库里的段落是不是**旧解析器**的产物（该整篇重解析一次）。

    判据是 bbox 里有没有 ph（本页页高）：新解析器把页宽页高写进 bbox（译文页按
    "页内比例"落段区要用），老行没有这个键——它同时替代了原来的"没有行级坐标"
    判据（重解析本来就会补 lines）。SQL 上只看 dict 形状的 bbox：OCR 写的 bbox 是
    数组（扫描件没有文字层，重解析也解析不出东西），不能算"旧"，否则每次开篇都要
    白开一遍 PDF。

    每篇一次整本重解析：解析规则演进过（缩进断段/公式块/文献兜底三处收紧），
    库里的段落是老规则的产物——不重解析就永远停在"整节正文粘成一个大 ¶"的状态。
    """
    r = q("SELECT COUNT(*) AS n, SUM(CASE WHEN lines IS NULL OR lines='' THEN 1 ELSE 0 END) AS e, "
          "SUM(CASE WHEN bbox LIKE '{%' AND bbox NOT LIKE '%\"ph\"%' THEN 1 ELSE 0 END) AS old "
          "FROM paragraphs WHERE paper_id=?", (pid,))[0]
    n = r["n"] or 0
    return n > 0 and ((r["e"] or 0) == n or (r["old"] or 0) > 0)

def paragraphs_match(pid: str, paras: list) -> bool:
    """新解析出来的段落和库里存的**是不是同一批**（段数一样、每段的正文也一样）。

    对上了就整表替换（只是把行级坐标/页高补上，编号一个没动，锚点不需要迁移）。"""
    rows = q("SELECT idx, text FROM paragraphs WHERE paper_id=? ORDER BY idx", (pid,))
    if len(rows) != len(paras):
        return False
    for r, p in zip(rows, paras):
        if int(r["idx"]) != int(p["idx"]):
            return False
        if (r["text"] or "").strip() != (p["text"] or "").strip():
            return False
    return True

# ---------- 段落重解析：锚点迁移 ----------
# 解析规则一改，段落编号就整体重排。用户的锚点（页边卡/批注）与 AI 的锚点
# （骨架的角色、主张的依据段、七问回答里的 ¶n）全是按编号指的——不迁移就是静默错位：
# 点「¶12」跳到别的段上，比"少一个功能"更糟。这里按**文字**把它们搬到新编号上。

def _probe(part: str, new_sq: list):
    """一小截老段文字落在哪条新段里（逐条包含比对，找不到回 None）。"""
    if not part:
        return None
    for i, (_, nsq) in enumerate(new_sq):
        if nsq and part in nsq:
            return i
    return None

def _para_map(old_rows: list, new_paras: list) -> tuple:
    """老段号 → 新段号们（`{old_idx: [new_idx, ...]}`）＋覆盖统计。

    切分方向是双向的：新解析器**细分**了老的大 ¶（一条老段映射到连续几条新段），
    也可能把老的两小段并回一条（多条老段映射到同一条新段）。用文字包含关系定，
    不靠编号差：编号差在"页眉页脚滤除规则也变了"的时候会整体漂。

    返回 (mapping, orphan_old, uncovered_new)：
    orphan_old = 文字在新解析里找不到的老段（真丢了内容）；
    uncovered_new = 文字在老解析里找不到的新段（新规则救回来的正文）。
    """
    new_sq = [(p["idx"], _squash(p["text"])) for p in new_paras]
    mapping, orphan, covered = {}, 0, set()
    # 两种入参形状都收：[{idx,text}]（库里那批）与 [(idx,text)]（调用方顺手拼的）
    rows = [(int(r["idx"]), r["text"]) if isinstance(r, dict) else (int(r[0]), r[1])
            for r in old_rows]
    for idx, text in rows:
        sq = _squash(text)
        if not sq:
            continue
        head = _probe(sq[:80], new_sq)
        if head is None:                     # 老段开头被改了（页眉滤除规则变了）：逐级缩短再探
            for L in (40, 20, 12):
                head = _probe(sq[:L], new_sq)
                if head is not None:
                    break
        if head is None:                     # 反向：新段整体落在老段里（老段更长）
            head = next((i for i, (_, nsq) in enumerate(new_sq) if nsq and nsq in sq), None)
        if head is None:
            orphan += 1
            continue
        tail = _probe(sq[-80:], new_sq) if len(sq) > 160 else None
        end = tail if (tail is not None and tail > head) else head
        run = list(range(head, end + 1))
        k = end + 1
        while k < len(new_sq) and new_sq[k][1] and new_sq[k][1] in sq:
            run.append(k)                    # 尾巴上"整段都在老段里"的继续吃进来
            k += 1
        mapping[idx] = [new_sq[j][0] for j in run]
        covered.update(run)
    return mapping, orphan, max(0, len(new_sq) - len(covered))

def _remap_refs(text: str, mapping: dict) -> str:
    """把文字里的 ¶n 改写成新编号（映射到一段就用那一段，映射到多段就用第一段）。

    回答/摘要里的 ¶n 是指针不是内容：切分后老 ¶12 变成新 ¶12–14，指针指到 12
    至少还落在原地；不搬就整体错位到别的段上。找不到落点的编号原样留着——
    前端对越界引用本来就降级成纯文本，不会变成死按钮。
    """
    def one(m):
        n = int(m.group(1))
        tgt = mapping.get(n)
        return f"¶{tgt[0]}" if tgt else m.group(0)
    return re.sub(r"¶\s*(\d{1,4})", one, text or "")

def _remap_json_refs(raw, mapping: dict, keys: bool = False):
    """JSON 缓存里的文字统一过一遍 ¶n 改写（结构不动，只改字符串）。

    keys=True 用于"键就是段号"的缓存（evidence_qs）：键跟着映射走。其余缓存的键
    都是语义键（缩写、术语），不碰。"""
    if isinstance(raw, str):
        return _remap_refs(raw, mapping)
    if isinstance(raw, list):
        return [_remap_json_refs(x, mapping, keys) for x in raw]
    if isinstance(raw, dict):
        out = {}
        for k, v in raw.items():
            nk = str(mapping.get(int(k), [k])[0]) if keys and isinstance(k, str) and k.isdigit() else k
            out[nk] = _remap_json_refs(v, mapping, keys)
        return out
    return raw

def migrate_anchors(pid: str, old_rows: list, new_paras: list) -> dict:
    """重解析之前：把这篇的所有锚点按文字搬到新编号上，返回迁移统计。

    old_rows: [{idx, text}]（库里那批）；new_paras: pdfparse 的新输出。
    统计里 material=True 表示"这次重排把段落结构改到骨架不可信了"（新解析救回来的
    正文超过一成，或者有一成老段找不到落点）——由调用方决定要不要重析读。
    """
    mapping, orphan, uncovered = _para_map(old_rows, new_paras)
    n_old, n_new = max(1, len(old_rows)), max(1, len(new_paras))
    stats = {"mapped": len(mapping), "orphan": orphan, "uncovered": uncovered,
             "pins": 0, "pins_lost": 0, "refs": 0,
             "material": (uncovered > max(2, 0.12 * n_new)) or (orphan > max(2, 0.12 * n_old))}
    new_sq = [(p["idx"], _squash(p["text"])) for p in new_paras]
    with _txn() as c:
        if not c.execute("SELECT 1 FROM papers WHERE id=?", (pid,)).fetchone():
            c.rollback()
            return stats
        # ① 骨架的角色/用途：老段映射到几条新段，几条都继承（老块的角色覆盖它的碎片）
        annos = c.execute("SELECT para_idx, role, purpose FROM annotations WHERE paper_id=?",
                          (pid,)).fetchall()
        c.execute("DELETE FROM annotations WHERE paper_id=?", (pid,))
        rows = []
        for a in annos:
            for n in mapping.get(int(a["para_idx"]), []):
                rows.append((pid, int(n), a["role"], a["purpose"]))
        if rows:
            c.executemany("INSERT OR IGNORE INTO annotations(paper_id, para_idx, role, purpose) "
                          "VALUES(?,?,?,?)", rows)
        # ② 主张的依据段：老锚点 → 新范围（整段搬，编号连续不丢）
        for cl in c.execute("SELECT cid, anchors FROM claims WHERE paper_id=?", (pid,)).fetchall():
            try:
                old = json.loads(cl["anchors"] or "[]")
            except ValueError:
                continue
            got = []
            for a in (old if isinstance(old, list) else []):
                for n in mapping.get(int(a), []):
                    if n not in got:
                        got.append(n)
            c.execute("UPDATE claims SET anchors=? WHERE paper_id=? AND cid=?",
                      (json.dumps(sorted(got)), pid, cl["cid"]))
        # ③ 页边卡：引句在新段落里逐字找回（去空白比对），找不回就退到映射的第一段
        for m in c.execute("SELECT id, para_idx, quote, rect FROM marginalia WHERE paper_id=? "
                           "AND kind IN ('lookup','region','note')", (pid,)).fetchall():
            q_ = _squash(m["quote"])[:60]
            hit = next((nidx for nidx, nsq in new_sq if q_ and q_ in nsq), None)
            if hit is None:
                rng = mapping.get(int(m["para_idx"] or 0))
                hit = rng[0] if rng else None
            if hit is None:
                stats["pins_lost"] += 1
                continue
            if hit != int(m["para_idx"] or 0):
                c.execute("UPDATE marginalia SET para_idx=?, rect=NULL WHERE id=?", (hit, m["id"]))
            stats["pins"] += 1
        # ④ 文字里的 ¶n：回答、对话、摘要、卡片（JSON 里的字符串逐个改写）
        for key, raw in c.execute("SELECT key, json FROM answers WHERE paper_id=?", (pid,)).fetchall():
            new = json.dumps(_remap_json_refs(_safe_json(raw, None), mapping), ensure_ascii=False)
            if new != raw:
                stats["refs"] += 1
                c.execute("UPDATE answers SET json=? WHERE paper_id=? AND key=?", (new, pid, key))
        for r in c.execute("SELECT id, content FROM qa_messages WHERE paper_id=?", (pid,)).fetchall():
            new = _remap_refs(r["content"], mapping)
            if new != r["content"]:
                c.execute("UPDATE qa_messages SET content=? WHERE id=?", (new, r["id"]))
        for r in c.execute("SELECT id, summary FROM conversations WHERE paper_id=?", (pid,)).fetchall():
            new = _remap_refs(r["summary"], mapping)
            if new != (r["summary"] or ""):
                c.execute("UPDATE conversations SET summary=? WHERE id=?", (new, r["id"]))
        cols = (("summary", False), ("suggest", False), ("advisor", False),
                ("method_card", False), ("citation", False), ("evidence_qs", True))
        cur = c.execute("SELECT " + ", ".join(k for k, _ in cols) + " FROM papers WHERE id=?",
                        (pid,)).fetchone()
        if cur:
            upd = {}
            for k, keys in cols:
                if cur[k]:
                    new = _remap_json_refs(_safe_json(cur[k], cur[k]), mapping, keys)
                    if isinstance(new, str):
                        if new != cur[k]:
                            upd[k] = new
                    else:
                        s = json.dumps(new, ensure_ascii=False)
                        if s != cur[k]:
                            upd[k] = s
            if upd:
                c.execute("UPDATE papers SET " + ", ".join(f"{k}=?" for k in upd) +
                          " WHERE id=?", (*upd.values(), pid))
    return stats

def ai_invalidate(pid: str, why: str):
    """段落重排后骨架不再可信：把按段落编号生成的 AI 产物整批作废。

    清的是"锚在段号上的"（角色/主张/七问回答/一眼卡/方法卡/推荐问题）——
    它们全部由重析读重建。用户资产（页边卡、批注、术语表）一个不动：
    页边卡刚按引句迁过，术语表锚的是词不是段。
    """
    with _txn() as c:
        if not c.execute("SELECT 1 FROM papers WHERE id=?", (pid,)).fetchone():
            c.rollback()
            return
        c.execute("DELETE FROM annotations WHERE paper_id=?", (pid,))
        c.execute("DELETE FROM claims WHERE paper_id=?", (pid,))
        c.execute("DELETE FROM answers WHERE paper_id=?", (pid,))
        c.execute("UPDATE papers SET analysis_status='none', analysis_error=?, summary=NULL, "
                  "suggest=NULL, advisor=NULL, method_card=NULL, evidence_qs=NULL WHERE id=?",
                  (why, pid))

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
    """清掉"一眼就能重算出来的卡片"（换演示/真实模式时用）。

    claims/annotations/marginalia 不动：前两者是整篇析读的产物、重算代价高，
    后者是用户钉的页边卡（用户资产）；这里清的是 summary/suggest/… 这些缓存卡。
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
    return _safe_json(raw, {})

def fig_caps_add(pid: str, entries: dict):
    """往图注缓存里并进几条。读-合并-写必须同一把锁：灯箱两张图并发懒翻译，
    各自整包覆写会把对方刚落的译文冲掉。"""
    if not entries:
        return
    with _lock:
        c = _get()
        row = c.execute("SELECT fig_caps FROM papers WHERE id=?", (pid,)).fetchone()
        if not row:
            return
        cur = _safe_json(row["fig_caps"], {})
        if not isinstance(cur, dict):
            cur = {}
        cur.update(entries)
        c.execute("UPDATE papers SET fig_caps=? WHERE id=?",
                  (json.dumps(cur, ensure_ascii=False), pid))
        c.commit()

def fail_analysis(pid: str, error: str):
    """析读失败：**只记状态与原因，不动已经存在的 claims/annotations**。

    为什么单独开一个：失败绝不能走会删表的 set_analysis（
    它先把两张表删空再写——一次限流/超时就把用户刚才花过 token 读出来的骨架清空了。
    重算失败应该只是"这次没成"，不该把上次的成果一起赔进去
    （summarize_dialog 早就是"失败退回原摘要"的写法，这里是同一个道理）。"""
    q("UPDATE papers SET analysis_status='error', analysis_error=? WHERE id=?",
      (error, pid), commit=True)

def get_analysis(pid: str):
    paper = get_paper(pid)
    if not paper:
        # 取行与取析读之间被删（删篇竞态）：回 None 状态让端点自己判 404，
        # 别让 paper["analysis_status"] 的 None 下标把端点炸成 500
        return None, [], {}
    rows = [{"id": r["cid"], "text": r["text"], "anchors": _safe_json(r["anchors"], [])}
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
        now = _now()
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

def glossary_delete_owned(gid: int, pid: str) -> bool:
    """带归属校验的删除（同 marginalia_delete_count 的口径）：True=已删/本就不在；
    False=行存在但属于别篇——DELETE 条件里带 paper_id，判定与删除一条语句，无 TOCTOU。
    术语 id 是全库自增的：一个陈旧页面拿着 A 篇的 id 打到 B 篇的行上，不带校验就删错人。"""
    with _lock:
        c = _get()
        row = c.execute("SELECT paper_id FROM glossary WHERE id=?", (gid,)).fetchone()
        if row and row["paper_id"] != pid:
            return False
        c.execute("DELETE FROM glossary WHERE id=? AND paper_id=?", (gid, pid))
        c.commit()
        return True

def glossary_put_ai(pid: str, terms: list):
    """把模型发掘出来的术语整批写进这一篇（替换上一批 AI 词，读者的手写词不动）。"""
    with _lock:
        _get().execute("DELETE FROM glossary WHERE paper_id=? AND source='ai'", (pid,))
        _get().executemany(
            "INSERT INTO glossary(paper_id, term_en, term_zh, source, created_at)"
            " VALUES(?,?,?,?,?)",
            [(pid, t["en"], t["zh"], "ai",
              _now()) for t in terms])
        _get().commit()

def merge_abbrs(pid: str, abbrs: dict) -> int:
    """把新发掘到的缩写并进这一篇的缩写表，返回补进去几条。

    只补缺、不覆盖：骨架那一次抽到的写法（"CNT": "carbon nanotube，碳纳米管"）是跟着
    正文语境来的，比术语那一次更贴原文，后来的一批不该把它顶掉。
    """
    if not isinstance(abbrs, dict) or not abbrs:
        return 0
    added = 0
    with _lock:     # 读-合并-写同锁：析读管线与术语管线并发补缩写时，后写不许覆盖前写
        c = _get()
        row = c.execute("SELECT abbrs FROM papers WHERE id=?", (pid,)).fetchone()
        if not row:
            return 0
        cur = _safe_json(row["abbrs"], {})
        if not isinstance(cur, dict):
            cur = {}
        for k, v in abbrs.items():
            k, v = str(k).strip(), str(v).strip()
            if k and v and k not in cur:
                cur[k], added = v, added + 1
        if added:
            c.execute("UPDATE papers SET abbrs=? WHERE id=?", (json.dumps(cur, ensure_ascii=False), pid))
            c.commit()
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

# ---------- 全局学科术语库（跨篇复用的公共层） ----------

def glossary_global_list():
    return [dict(r) for r in q(
        "SELECT * FROM glossary_global ORDER BY term_en COLLATE NOCASE")]

def glossary_global_add(term_en: str, term_zh: str, note: str = "") -> int:
    """同词再收 = 改译法，不是插出两行（与单篇 glossary_add 同一判重口径，同锁防 TOCTOU）。"""
    with _lock:
        c = _get()
        row = c.execute("SELECT id FROM glossary_global WHERE term_en=? COLLATE NOCASE",
                        (term_en,)).fetchone()
        now = _now()
        if row:
            c.execute("UPDATE glossary_global SET term_zh=? WHERE id=?", (term_zh, row["id"]))
            c.commit()
            return row["id"]
        cur = c.execute("INSERT INTO glossary_global(term_en, term_zh, note, created_at) VALUES(?,?,?,?)",
                        (term_en, term_zh, note, now))
        rowid = cur.lastrowid
        c.commit()
        return rowid

def glossary_global_delete(gid: int):
    q("DELETE FROM glossary_global WHERE id=?", (gid,), commit=True)

def glossary_global_hits(text: str):
    """全局词里有哪些出现在给定文本里（大小写不敏感子串）。供翻译/问答与单篇词表合并。"""
    if not (text or "").strip():
        return []
    low = text.lower()
    return [{"en": r["term_en"], "zh": r["term_zh"]}
            for r in q("SELECT term_en, term_zh FROM glossary_global")
            if (r["term_en"] or "").lower() in low]

# ---------- 库内互引（scite 式）的立场缓存 ----------

def cite_ctx_all(pid: str) -> dict:
    """{citing_id: hits}——hits 是含 stance 的语境列表。"""
    out = {}
    for r in q("SELECT citing_id, hits FROM cite_ctx WHERE paper_id=?", (pid,)):
        # NULL/坏 JSON 容错同 qa_history：解析成 None 的调用方都按"没缓存"回落重判（真值取 or）
        out[r["citing_id"]] = _safe_json(r["hits"], None)
    return out

def cite_ctx_put(pid: str, citing_id: str, hits):
    q("INSERT OR REPLACE INTO cite_ctx(paper_id, citing_id, hits) VALUES(?,?,?)",
      (pid, citing_id, json.dumps(hits, ensure_ascii=False)), commit=True)

# ---------- QA ----------

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

ORPHAN_CONV = "此前的提问"

def _adopt_orphan_qa(pid: str):
    """旧库的问答没有会话号：给它们单开一摊"此前的提问"，别让历史粘在新对话里。
    查-建-挂同一个事务：拆开的话两个窗口同时首拉会各建一摊。"""
    with _txn() as c:
        if not c.execute("SELECT 1 FROM qa_messages WHERE paper_id=? AND conv_id IS NULL",
                         (pid,)).fetchone():
            return
        row = c.execute("SELECT id FROM conversations WHERE paper_id=? AND title=? LIMIT 1",
                        (pid, ORPHAN_CONV)).fetchone()
        if row:
            cid = row["id"]
        else:
            now = _now()
            cid = c.execute("INSERT INTO conversations(paper_id, title, created_at, updated_at) "
                            "VALUES(?,?,?,?)", (pid, ORPHAN_CONV, now, now)).lastrowid
        c.execute("UPDATE qa_messages SET conv_id=? WHERE paper_id=? AND conv_id IS NULL", (cid, pid))

def conv_rename(cid: int, title: str):
    q("UPDATE conversations SET title=? WHERE id=?", (title[:60], cid), commit=True)

def conv_delete(cid: int):
    with _txn() as c:
        c.execute("DELETE FROM qa_messages WHERE conv_id=?", (cid,))
        c.execute("DELETE FROM conversations WHERE id=?", (cid,))

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

def qa_add(pid: str, role: str, content: str, citations: list = None, conv_id: int = None) -> int:
    """写一条问答。会话已经被删掉时**不写**（返回 0）——否则会留下一条谁也看不到的孤儿，
    用户流式提问到一半把会话删了就会踩到。校验与插入同事务：拆开的毫秒窗里删会话
    还是能插进孤儿。"""
    with _txn() as c:
        if conv_id and not c.execute("SELECT 1 FROM conversations WHERE id=?", (conv_id,)).fetchone():
            return 0
        cur = c.execute("INSERT INTO qa_messages(paper_id, role, content, citations, conv_id, created_at) VALUES(?,?,?,?,?,?)",
                        (pid, role, content, json.dumps(citations or []), conv_id, _now()))
        if conv_id:
            c.execute("UPDATE conversations SET updated_at=? WHERE id=?", (_now(), conv_id))
        return cur.lastrowid

def qa_last_user_id(pid: str, conv_id: int):
    rows = q("SELECT id FROM qa_messages WHERE paper_id=? AND conv_id=? AND role='user' ORDER BY id DESC LIMIT 1",
             (pid, conv_id))
    return rows[0]["id"] if rows else None

def qa_msg_get(mid: int):
    """单条消息（继续生成要读那条被截断的回答）。"""
    rows = q("SELECT id, paper_id, role, content, conv_id FROM qa_messages WHERE id=?", (mid,))
    return dict(rows[0]) if rows else None

def qa_append_assistant(mid: int, add: str) -> bool:
    """把续写的一段接到那条回答末尾。行还在才写（用户在续写途中删了消息就算了）。"""
    with _txn() as c:
        row = c.execute("SELECT content FROM qa_messages WHERE id=?", (mid,)).fetchone()
        if not row:
            return False
        c.execute("UPDATE qa_messages SET content=? WHERE id=?",
                  (((row["content"] or "").rstrip() + "\n\n" + add.strip()) if row["content"] else add.strip(), mid))
        return True

def qa_history(pid: str, conv_id: int = None, limit: int = 200):
    if conv_id:
        rows = q("SELECT role, content, citations, id, conv_id FROM qa_messages "
                 "WHERE paper_id=? AND conv_id=? ORDER BY id LIMIT ?", (pid, conv_id, limit * 4))
    else:
        rows = q("SELECT role, content, citations, id, conv_id FROM (SELECT * FROM qa_messages "
                 "WHERE paper_id=? ORDER BY id DESC LIMIT ?) ORDER BY id ASC", (pid, limit))
    # 写侧一律 json.dumps；读侧容 NULL/坏值（老库/外部写入留下的历史行会炸 500）
    return [dict(r, citations=_safe_json(r["citations"], [])) for r in rows]

def qa_drop_last_assistant(pid: str, conv_id: int):
    """重新生成 = 把最后一条回答删掉，连同它的用户问题一起交回前端重问。
    返回被删掉的那条用户问题（没有就返回 None）。

    两个坑都堵在这里：
    ① 幂等——只认"最新一行是 assistant"这一种形态（那条回答 + 紧邻它前面的用户问题），
       两条 DELETE 同一个事务。以前取最后两行再从中挑 assistant，重复调用会各删一对；
       最后两行里没有 assistant 时（流式被掐断留下的孤立 user 行）更糟：
       _touch_summary_if_covering(..., 0) 里 `>= 0` 恒真，会把一份好端端的会话摘要清掉。
    ② 删不动就什么都不做：最后一行不是回答（孤立问题/空会话）时直接回 None。"""
    rows = q("SELECT id, role, content FROM qa_messages WHERE paper_id=? AND conv_id=? ORDER BY id DESC LIMIT 2",
             (pid, conv_id))
    if not rows or rows[0]["role"] != "assistant":
        return None
    last = rows[0]
    prev = next((r for r in rows[1:] if r["role"] == "user"), None)
    with _txn() as c:
        c.execute("DELETE FROM qa_messages WHERE id=?", (last["id"],))
        if prev:
            c.execute("DELETE FROM qa_messages WHERE id=?", (prev["id"],))
    _touch_summary_if_covering(conv_id, last["id"])
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
    with _txn() as c:
        c.execute("DELETE FROM qa_messages WHERE paper_id=?", (pid,))
        c.execute("DELETE FROM conversations WHERE paper_id=?", (pid,))

def get_marginalia(pid: str):
    return [dict(r) for r in q(
        "SELECT id, para_idx, page, quote, kind, note, rect FROM marginalia "
        "WHERE paper_id=? ORDER BY page, para_idx", (pid,))]

def marginalia_set_rect(mid: int, rect: dict):
    q("UPDATE marginalia SET rect=? WHERE id=?", (json.dumps(rect), mid), commit=True)

def marginalia_add(pid: str, para_idx: int, page: int, quote: str, note: str, kind: str = "lookup",
                   rect: dict = None) -> int:
    return q_insert("INSERT INTO marginalia(paper_id, para_idx, page, quote, kind, note, rect) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (pid, para_idx, page, quote, kind, note, json.dumps(rect) if rect else None))

def marginalia_upsert_lookup(pid: str, para_idx: int, page: int, quote: str, note: str) -> int:
    """查译卡的"同段只留一条"：查到就改、没有才插，查与写在**同一把锁**里完成。

    端点里先 SELECT 再 INSERT 的写法是 check-then-act：连点两下、或两个窗格同时
    钉同一段，两个请求都看到"没有旧卡"，各插一条，页边就出现两张一模一样的查译卡。
    glossary_add 修过同一课（见其注释），这里补齐。"""
    with _lock:
        c = _get()
        row = c.execute("SELECT id FROM marginalia WHERE paper_id=? AND kind='lookup' AND para_idx=?",
                        (pid, para_idx)).fetchone()
        if row:
            c.execute("UPDATE marginalia SET note=?, quote=?, page=? WHERE id=?",
                      (note, quote, page, row["id"]))
            c.commit()
            return row["id"]
        cur = c.execute("INSERT INTO marginalia(paper_id, para_idx, page, quote, kind, note, rect) "
                        "VALUES(?,?,?,?,'lookup',?,NULL)",
                        (pid, para_idx, page, quote, note))
        rowid = cur.lastrowid
        c.commit()
        return rowid

def marginalia_delete_count(mid: int, pid: str) -> bool:
    """带归属校验的删除。True=可删（含幂等：mid 本就不存在，前端重试/换页竞态都无害）；
    False=mid 存在但属于别篇——URL 是 A 篇、mid 是 B 篇的卡，删 B 就是越权（对抗轮实锤）。"""
    row = q("SELECT paper_id FROM marginalia WHERE id=?", (mid,))
    if not row:
        return True
    if row[0]["paper_id"] != pid:
        return False
    q("DELETE FROM marginalia WHERE id=?", (mid,), commit=True)
    return True

# ---------- 论文日历 ----------

def log_read(pid: str, day: str):
    """打开论文时记一笔当天阅读。INSERT OR IGNORE：一天一篇只有一行。"""
    q("INSERT OR IGNORE INTO reading_log(day, paper_id) VALUES(?,?)", (day, pid), commit=True)

def reading_days(month: str):
    """某个月（'YYYY-MM' 前缀）的全部阅读日志。"""
    return [dict(r) for r in q("SELECT day, paper_id FROM reading_log WHERE day LIKE ?", (month + "%",))]

"""数据对比表：勾选几篇文献，逐篇抽成同一张表的格子。

设计上的取舍：
- **维度固定六行**（研究问题/方法/研究体系/数据/关键结果/局限），每篇各调一次模型按同一
  schema 抽取——不需要第二次"合并对齐"调用，跨篇口径天然一致；论文没写到的维度如实写
  「原文未提及」，绝不硬凑。
- **可溯源**：每个格子带段落号（¶n），点得回原文。模型只允许用给出的材料，不许凭记忆补。
- 未析读的篇照样能对比：抽取材料用段落原文就够了，骨架只是锦上添花。
- **缓存命中**：system 用与析读/七问同一套「共享身份句 + 逐字节相同的全文块」开头
  （llm._doc_system）——同一篇论文析读时写下的服务端缓存，对比抽取的输入大头直接命中。
"""
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

from llm import chat_json, _lang_tail, _doc_system, _kick

# 行维度：键、界面名、给模型的一句话解释
DIMS = [
    ("problem", "研究问题", "这篇要解决的核心问题或核心假设"),
    ("method", "方法", "提出的方法/模型/理论框架的核心思想"),
    ("system", "研究体系", "研究/论证所用的对象、体系或场景"),
    ("datasets", "数据", "用到的数据集、样本或测量/调查手段"),
    ("results", "关键结果", "最有分量的定量或定性结论（带数字优先）"),
    ("limits", "局限", "作者自认的局限或方法适用边界"),
]
KEYS = [k for k, _n, _h in DIMS]

# 自定义维度（自定义列口径）：列名与给模型的一句话说明都由用户给，
# 键用 c1..c6 合成——缓存与格子结构跟固定维度完全同构
MAX_COLS = 6

def dims_meta(dims: list = None, cols: list = None) -> list:
    """本次要抽的全部维度：[(k, label, hint)]。dims=None = 固定六维全选；
    dims=[] = 固定维度一个不选（纯自定义列口径）；自定义列收编成
    c1..cN（label/hint 清洗钳长，label 必填）。"""
    out = [(k, n, h) for k, n, h in DIMS if dims is None or k in dims]
    for i, c in enumerate((cols or [])[:MAX_COLS]):
        if not isinstance(c, dict):
            continue
        label = str(c.get("label") or "").strip()[:40]
        if not label:
            continue
        hint = str(c.get("hint") or "").strip()[:200] or label
        out.append((f"c{i + 1}", label, hint))
    return out


def _messages(title: str, paras: list, claims: list, annos: dict, meta: list) -> list:
    # 段角色与主张是**这一篇的常量**：跟在全文块之后、维度说明之前——
    # 维度勾选组合每次可能不同，只能放在最后的 user 里
    role_note = ""
    anno_bits = [f"¶{idx} {a.get('role','')}" for idx, a in (annos or {}).items()
                 if a.get("role") and a.get("role") not in ("boilerplate",)]
    if anno_bits:
        role_note = "\n【段角色（后台推断，供定位用）】\n" + "、".join(anno_bits[:40])
    claim_bits = [f"{c['id']} {c['text'][:120]}" for c in (claims or [])][:8]
    if claim_bits:
        role_note += "\n【核心主张】\n" + "\n".join(claim_bits)
    keys = [k for k, _l, _h in meta]
    schema = json.dumps({k: {"text": "…", "ref": 3} for k in keys}, ensure_ascii=False)
    return [
        {"role": "system", "content": _doc_system(title, paras,
            "【任务：数据对比抽取】你是严谨的文献分析员。只依据给出的材料填表，绝不使用材料之外的知识；"
            "论文没写到的维度如实填「原文未提及」，不许编。" + role_note + _lang_tail())},
        {"role": "user", "content":
            f"为数据对比表抽取以下{len(keys)}个维度，每个维度给一段"
            f"（≤60字，关键结果优先带数字），并给出依据所在的段落号——ref 必须是**整数**"
            f"（如 3 代表 ¶3；确实定位不到才写 null）。\n"
            f"维度说明：{'；'.join(f'{k}={hint}' for k, _n, hint in meta)}。\n"
            f"输出 JSON：{schema}\n"
            + _kick("填好这些维度")},
    ]


def _norm(data: dict, keys: list = None, n_paras: int = 0) -> dict:
    out = {}
    for k in (keys or KEYS):
        cell = data.get(k) if isinstance(data, dict) else None
        if not isinstance(cell, dict):
            cell = {}
        text = str(cell.get("text") or "").strip() or "原文未提及"
        ref = cell.get("ref")
        if not isinstance(ref, int) or ref < 1 or (n_paras and ref > n_paras):
            ref = None          # 越界的 ¶n 点过去会静默落空，不如当作没定位到
        out[k] = {"text": text[:200], "ref": ref}
    return out


def _wclip(s: str, n: int) -> str:
    """按词界截断（演示文案不带半个词）：切点落在词中就回退到最近的空格。"""
    s = (s or "").strip()
    if len(s) <= n:
        return s
    cut = s[:n]
    if s[n] != " ":
        sp = cut.rfind(" ")
        if sp > n * 0.6:
            cut = cut[:sp]
    return cut.rstrip() + "…"


def _mock(paper: dict, meta: list) -> dict:
    """演示模式：一眼假但结构完整的格子，别让演示用户等一场真实的模型调用。"""
    t = _wclip(paper.get("title") or paper.get("filename") or "这篇文献", 18)
    cells = {}
    for i, (k, label, _h) in enumerate(meta):
        cells[k] = {"text": f"（演示）《{t}》的{label}示意内容",
                    "ref": 2 + i * 3}
    return cells


# 逐篇格子缓存：键 = (pid, 维度集（键+列名）, 材料指纹)。对比表没有它，某篇失败想补一格
# 只能整表重跑——其余 N-1 篇重新计费（一篇 3~8 分钱，五篇反复点就是真金白银）。
# 材料指纹盖住"重新析读后内容变了"的情况：主张文本或段数一变，缓存自动失效。
_CACHE: dict = {}

def _material_sig(claims: list, n_paras: int) -> str:
    h = hashlib.md5()
    for c in (claims or []):
        h.update(str(c.get("text") or "").encode("utf-8", "replace"))
    h.update(str(n_paras).encode())
    return h.hexdigest()[:12]

def extract_all(papers: list, material_of, demo: bool = False, meta: list = None) -> dict:
    """papers: [{id,title,...}]；material_of(pid) -> (paras, claims, annos)。
    meta = dims_meta() 的产出（键+列名+给模型的一句话说明）——固定维度与自定义列
    在这里已经收编成一张清单，抽取路径不再区分两种来源。
    返回 {pid: {dim: {text, ref}}}。每篇一线程并行，谁失败谁一格降级，不拖垮整张表。
    成功的篇进缓存：下次打开只重抽**失败的**和换了材料的，其余零成本。"""
    meta = meta or DIMS
    keys = [k for k, _l, _h in meta]
    out = {}
    if demo:
        return {p["id"]: _mock(p, meta) for p in papers}

    def one(p):
        paras, claims, annos = material_of(p["id"])
        sig = _material_sig(claims, len(paras))
        # 缓存键含 label+hint：改了列的说明文字，抽取口径就变了，旧格子不能再当命中
        ckey = (p["id"], tuple((k, lab, h) for k, lab, h in meta), sig)
        hit = _CACHE.get(ckey)
        if hit is not None:
            return p["id"], hit
        # 标题回落用空串不用文件名：析读管线就是 title or ""，两边一致前缀才命中
        msgs = _messages(p.get("title") or "", paras, claims, annos, meta)
        try:
            # 输出预算随维度数走：12 个维度一格约百 token，1600 的死数会在大表上
            # 静默截断 JSON（格变成"抽取失败"）；封顶防跑飞
            budget = min(4000, 600 + 160 * len(keys))
            cells = _norm(chat_json(msgs, max_tokens=budget, scene="数据对比"), keys, n_paras=len(paras))
            if len(_CACHE) > 400:
                _CACHE.clear()
            _CACHE[ckey] = cells
            return p["id"], cells
        except Exception:
            return p["id"], {k: {"text": "抽取失败——这篇没能读出结果，可再次打开对比重试",
                                 "ref": None} for k in keys}

    with ThreadPoolExecutor(max_workers=min(3, max(1, len(papers)))) as ex:
        futs = [ex.submit(one, p) for p in papers]
        for f in as_completed(futs):
            pid, cells = f.result()
            out[pid] = cells
    return out

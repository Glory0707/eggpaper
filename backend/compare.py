"""数据对比表：勾选几篇文献，逐篇抽成同一张表的格子。

设计上的取舍：
- **维度固定六行**（研究问题/方法/实验体系/数据/关键结果/局限），每篇各调一次模型按同一
  schema 抽取——不需要第二次"合并对齐"调用，跨篇口径天然一致；论文没写到的维度如实写
  「原文未提及」，绝不硬凑。
- **可溯源**：每个格子带段落号（¶n），点得回原文。模型只允许用给出的材料，不许凭记忆补。
- 未析读的篇照样能对比：抽取材料用段落原文就够了，骨架只是锦上添花。
"""
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

from llm import chat_json, _lang_tail

# 行维度：键、界面名、给模型的一句话解释
DIMS = [
    ("problem", "研究问题", "这篇要解决的核心问题或科学假设"),
    ("method", "方法", "提出的方法/模型/理论框架的核心思想"),
    ("system", "实验体系", "验证所用的对象/材料/场景体系"),
    ("datasets", "数据", "用到的数据集、样本或测量手段"),
    ("results", "关键结果", "最有分量的定量或定性结论（带数字优先）"),
    ("limits", "局限", "作者自认的局限或方法适用边界"),
]
KEYS = [k for k, _n, _h in DIMS]

# 每篇的材料预算：段落长截一刀、总长封顶，防止超长论文把上下文吃穿
_PARA_MAX, _TOTAL_MAX = 600, 26000


def _corpus(paras: list) -> str:
    out, used = [], 0
    for p in paras:
        if p.get("in_refs"):
            continue
        txt = (p.get("text") or "").strip()
        if len(txt) > _PARA_MAX:
            txt = txt[:_PARA_MAX] + "…"
        out.append(f"¶{p['idx']} {txt}")
        used += len(txt)
        if used > _TOTAL_MAX:
            out.append("（后文从略）")
            break
    return "\n".join(out)


def _messages(title: str, corpus: str, claims: list, annos: dict, keys: list) -> list:
    role_note = ""
    anno_bits = [f"¶{idx} {a.get('role','')}" for idx, a in (annos or {}).items()
                 if a.get("role") and a.get("role") not in ("boilerplate",)]
    if anno_bits:
        role_note = "\n【段角色（后台推断，供定位用）】\n" + "、".join(anno_bits[:40])
    claim_bits = [f"{c['id']} {c['text'][:80]}" for c in (claims or [])][:8]
    if claim_bits:
        role_note += "\n【核心主张】\n" + "\n".join(claim_bits)
    schema = json.dumps({k: {"text": "…", "ref": 3} for k in keys}, ensure_ascii=False)
    return [
        {"role": "system", "content":
            "你是严谨的文献分析员。只依据给出的材料填表，绝不使用材料之外的知识；"
            "论文没写到的维度如实填「原文未提及」，不许编。" + _lang_tail()},
        {"role": "user", "content":
            f"论文《{title}》。为数据对比表抽取以下{'六个' if len(keys) == len(KEYS) else len(keys)}维度，每个维度给一段"
            f"（≤60字，关键结果优先带数字），并给出依据所在的段落号——ref 必须是**整数**"
            f"（如 3 代表 ¶3；确实定位不到才写 null）。\n"
            f"维度说明：{'；'.join(f'{k}={hint}' for k, _n, hint in DIMS if k in keys)}。\n"
            f"输出 JSON：{schema}\n"
            f"{role_note}\n\n【正文】\n{corpus}"},
    ]


def _norm(data: dict, keys: list = None) -> dict:
    out = {}
    for k in (keys or KEYS):
        cell = data.get(k) if isinstance(data, dict) else None
        if not isinstance(cell, dict):
            cell = {}
        text = str(cell.get("text") or "").strip() or "原文未提及"
        ref = cell.get("ref")
        if not isinstance(ref, int) or ref < 1:
            ref = None
        out[k] = {"text": text[:200], "ref": ref}
    return out


def _mock(paper: dict, keys: list = None) -> dict:
    """演示模式：一眼假但结构完整的格子，别让演示用户等一场真实的模型调用。"""
    t = (paper.get("title") or paper.get("filename") or "这篇文献")[:18]
    cells = {}
    for i, k in enumerate(keys or KEYS):
        cells[k] = {"text": f"（演示）《{t}》的{'研究问题 方法 实验体系 数据 关键结果 局限'.split()[KEYS.index(k)]}示意内容",
                    "ref": 2 + KEYS.index(k) * 3}
    return cells


def extract_all(papers: list, material_of, demo: bool = False, dims: list = None) -> dict:
    """papers: [{id,title,...}]；material_of(pid) -> (paras, claims, annos)。
    dims 限定要抽的维度（用户在界面上勾过的）——少抽一个省一份模型调用。
    返回 {pid: {dim: {text, ref}}}。每篇一线程并行，谁失败谁一格降级，不拖垮整张表。"""
    keys = [k for k in KEYS if not dims or k in dims]
    out = {}
    if demo:
        return {p["id"]: _mock(p, keys) for p in papers}

    def one(p):
        paras, claims, annos = material_of(p["id"])
        msgs = _messages(p.get("title") or p.get("filename") or "", _corpus(paras), claims, annos, keys)
        try:
            return p["id"], _norm(chat_json(msgs, max_tokens=1600), keys)
        except Exception:
            return p["id"], {k: {"text": "抽取失败——这篇没能读出结果，可单独打开重试",
                                 "ref": None} for k in keys}

    with ThreadPoolExecutor(max_workers=min(3, max(1, len(papers)))) as ex:
        futs = [ex.submit(one, p) for p in papers]
        for f in as_completed(futs):
            pid, cells = f.result()
            out[pid] = cells
    return out

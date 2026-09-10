"""LLM 适配层：任意 OpenAI 兼容端点；无 key 时进入演示模式（启发式假数据，跑通全流程）。"""
import json
import re

import httpx

import config

ROLES = ["background", "gap", "claim", "evidence", "control", "boilerplate", "extension", "limitation"]

ROLE_ZH = {
    "background": "背景铺垫",
    "gap": "缺口转折",
    "claim": "核心主张",
    "evidence": "关键证据",
    "control": "对照参比",
    "boilerplate": "标准流程",
    "extension": "优化拓展",
    "limitation": "让步局限",
}

# ---------------- 基础调用 ----------------

def chat(messages: list, max_tokens: int = 4000, temperature: float = 0.2) -> str:
    cfg = config.load()
    if cfg["mock"] or not cfg["provider"]["api_key"]:
        raise RuntimeError("MOCK")
    budget = max_tokens
    out = ""
    for _ in range(2):                      # 推理模型可能耗尽 token 空想，空结果加倍重试
        r = httpx.post(
            f"{cfg['provider']['base_url'].rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {cfg['provider']['api_key']}"},
            json={"model": cfg["provider"]["model"], "messages": messages,
                  "max_tokens": budget, "temperature": temperature},
            timeout=600,
        )
        r.raise_for_status()
        out = (r.json()["choices"][0]["message"] or {}).get("content", "") or ""
        if out.strip():
            return out
        budget = int(budget * 1.6)
    return out


def parse_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    i, j = text.find("{"), text.rfind("}")
    if i < 0 or j < 0:
        raise ValueError(f"LLM 未返回 JSON: {text[:120]}")
    return json.loads(text[i:j + 1])


def test_connection() -> dict:
    try:
        out = chat([{"role": "user", "content": "只回复两个字：可用"}], max_tokens=2048)
        return {"ok": bool(out.strip()), "reply": out.strip()[:20]}
    except RuntimeError:
        return {"ok": False, "reply": "演示模式（未配置 API key）"}
    except Exception as e:
        return {"ok": False, "reply": f"{type(e).__name__}: {str(e)[:150]}"}


# ---------------- 骨架分析 ----------------

SKELETON_SYSTEM = """你是论文论证结构分析专家。研究者会把一篇论文的正文段落列表（每段以 ¶编号 开头）交给你，请你完全站在"作者写作意图"的角度分析论证结构：

角色定义（role 只能取以下八种）：
- background  背景铺垫：领域常识/前人工作综述，不读不影响理解主线
- gap         缺口转折：作者真正的出发点，指出未解决的问题或矛盾
- claim       核心主张：论文要证明的命题（we propose/demonstrate/report）
- evidence    关键证据：支撑主张的核心实验、主结果、主数据
- control     对照参比：仅为严谨而设的对照/参比/空白样，不是卖点
- boilerplate 标准流程：表征条件、仪器参数、标准步骤等样板描述
- extension   优化拓展：锦上添花的参数优化、应用演示、循环稳定性等
- limitation  让步局限：作者主动承认的弱点、边界条件、未来工作

只输出 JSON，不要 markdown 代码块，不要任何解释：
{"claims":[{"id":"C1","text":"<主张的中文概括，≤30字>","anchors":[<支撑该主张的关键证据段编号>]}],
 "roles":{"<¶编号>":"<角色>"},
 "purposes":{"<¶编号>":"<作者写这段的目的，≤22字，说人话>"}

要求：
1. claims 取 2~5 条，按论文叙事顺序；anchors 只能填 evidence 或 control 角色、且真实支撑该主张的段落编号
2. 每一个给出的段落都必须有 role 和 purpose，role 不得虚构枚举之外的值
3. purposes 用研究者口吻说人话，例如："堵审稿人的嘴""引出对照样品的必要性""交代测试条件，可跳过"
4. 不要虚构不存在的段落编号；参考文献部分（若有）一律标 boilerplate"""

PURPOSE_FALLBACK = {
    "background": "领域铺垫，可跳过", "gap": "作者真正的出发点", "claim": "论文要证明的核心",
    "evidence": "支撑主张的核心数据", "control": "仅为严谨的对照", "boilerplate": "样板描述，可跳过",
    "extension": "锦上添花的拓展", "limitation": "作者主动承认的弱点", "boilerplate_refs": "参考文献",
}


ROLE_ALIAS = {
    "background": "background", "gap": "gap", "claim": "claim", "evidence": "evidence",
    "control": "control", "boilerplate": "boilerplate", "extension": "extension", "limitation": "limitation",
    "背景": "background", "背景铺垫": "background", "缺口": "gap", "缺口转折": "gap",
    "主张": "claim", "核心主张": "claim", "证据": "evidence", "关键证据": "evidence",
    "对照": "control", "对照参比": "control", "参比": "control", "流程": "boilerplate",
    "标准流程": "boilerplate", "标准流程描述": "boilerplate", "样板": "boilerplate",
    "拓展": "extension", "优化拓展": "extension", "局限": "limitation", "让步": "limitation", "让步局限": "limitation",
}


def _key_num(k):
    if isinstance(k, int):
        return k
    m = re.search(r"\d+", str(k))
    return int(m.group()) if m else None


def analyze_skeleton(title: str, paras: list) -> dict:
    """paras: [{idx, text}]；返回 {"claims": [...], "roles": {...}, "purposes": {...}}"""
    body = "\n\n".join(f"¶{p['idx']} {p['text'][:1200]}" for p in paras)
    user = f"论文标题：{title or '（未识别）'}\n\n{body}"
    out = chat([
        {"role": "system", "content": SKELETON_SYSTEM},
        {"role": "user", "content": user},
    ], max_tokens=16000, temperature=0.2)
    data = parse_json(out)
    valid = {p["idx"] for p in paras}

    # claims：兼容 id/cid、anchors 可能是字符串
    claims_raw = data.get("claims") or []
    if isinstance(claims_raw, dict):
        claims_raw = [{"id": k, **v} for k, v in claims_raw.items()]
    claims = []
    for i, c in enumerate(claims_raw, 1):
        if not isinstance(c, dict) or not c.get("text"):
            continue
        anchors = c.get("anchors") or c.get("evidence") or []
        if isinstance(anchors, str):
            anchors = re.findall(r"\d+", anchors)
        claims.append({"id": str(c.get("id") or c.get("cid") or f"C{i}"),
                       "text": str(c["text"])[:60],
                       "anchors": [int(a) for a in anchors if int(a) in valid]})

    # roles：键提取数字；值兼容英文大小写与中文别名
    roles_raw = data.get("roles") or {}
    if isinstance(roles_raw, list):
        roles_raw = {it.get("para") or it.get("idx"): it.get("role") for it in roles_raw if isinstance(it, dict)}
    roles = {}
    for k, v in roles_raw.items():
        num = _key_num(k)
        role = ROLE_ALIAS.get(str(v).strip().lower()) or ROLE_ALIAS.get(str(v).strip())
        if num in valid and role:
            roles[str(num)] = role

    purposes = {}
    for k, v in (data.get("purposes") or {}).items():
        num = _key_num(k)
        if num in valid and isinstance(v, str) and v.strip():
            purposes[str(num)] = v.strip()[:40]
    for num, role in roles.items():
        purposes.setdefault(num, PURPOSE_FALLBACK.get(role, ""))

    if not roles:
        raise ValueError(f"骨架解析失败（roles 为空），原始输出: {out[:160]}")
    return {"claims": claims, "roles": roles, "purposes": purposes}


# ---------------- 一眼卡 ----------------

def _gloss_block(hits) -> str:
    if not hits:
        return ""
    return "术语表（以下术语必须使用锁定译法）：\n" + "\n".join(f"- {h['en']} → {h['zh']}" for h in hits) + "\n\n"


def summarize(title: str, paras: list, hits=None) -> dict:
    body = "\n\n".join(f"¶{p['idx']} {p['text'][:800]}" for p in paras if not p.get("in_refs"))[:60000]
    out = chat([
        {"role": "system", "content": _gloss_block(hits) +
            "你是论文精读助手。基于全文生成'一眼卡'，只输出 JSON："
            '{"one_line":"<一句话说清这篇论文做了什么、核心结果是什么，≤60字>",'
            '"contributions":"<贡献：解决了什么问题、为什么重要，≤80字>",'
            '"methods":"<方法：关键思路/材料体系/表征手段，≤80字>",'
            '"findings":"<发现：最重要的数据结论，带关键数字，≤80字>",'
            '"keywords":["<3~5个中文关键词>"]}'
            "不要 markdown 代码块，不要解释。"},
        {"role": "user", "content": f"论文标题：{title or ''}\n\n{body}"},
    ], max_tokens=4000, temperature=0.3)
    return parse_json(out)


# ---------------- 问答 ----------------

QA_SYSTEM = """你是论文精读助手，基于给定的论文全文回答研究者的问题。
规则：
1. 只基于论文原文回答，原文没有依据的要明说"原文未提及"
2. 关键论断后面标注依据段编号，格式如 [¶5] 或 [¶5,¶12]
3. 引用参考文献列表不作为依据
4. 回答用中文，术语首次出现给出英文"""


def ask(title: str, paras: list, history: list, question: str, hits=None) -> dict:
    body = "\n\n".join(f"¶{p['idx']} {p['text'][:1000]}" for p in paras if not p.get("in_refs"))[:80000]
    msgs = [{"role": "system", "content": QA_SYSTEM + _gloss_block(hits) + f"\n\n论文标题：{title or ''}\n\n{body}"}]
    for h in history[-6:]:
        msgs.append({"role": h["role"], "content": h["content"]})
    msgs.append({"role": "user", "content": question})
    out = chat(msgs, max_tokens=6000, temperature=0.3)
    cites = sorted({int(n) for n in re.findall(r"¶\s*(\d+)", out)})
    return {"answer": out, "citations": cites}


# ---------------- 划词/段落翻译 ----------------

def translate(text: str, context: str = "", hits: list = None) -> str:
    gloss = ""
    if hits:
        gloss = "术语表（必须使用以下译法）：\n" + "\n".join(f"- {h['en']} → {h['zh']}" for h in hits) + "\n\n"
    user = (f"{gloss}将下面的学术英文翻译成中文。要求：专业、准确、说人话；"
            "化学式、数字、单位、变量、引用标记保留原样；人名不译；只输出译文。\n")
    if context:
        user += f"[上下文：{context[:600]}]\n\n"
    user += f"[待翻译]\n{text[:4000]}"
    out = chat([
        {"role": "system", "content": "你是资深学术翻译，擅长化学/材料/工程领域论文的中英互译。"},
        {"role": "user", "content": user},
    ], max_tokens=4000, temperature=0.1)
    return out.strip()


# ---------------- 眉批（句级人性化批注） ----------------

MARGINALIA_SYSTEM = """你是实验室里最会读论文的师兄，正在一篇论文的打印稿上给师弟/师妹写眉批。你的批注从两个角度出发：穿透"作者写作时的心思"，减轻"读者的阅读负担"。只对值得说的句子下手——宁缺毋滥，绝大多数句子不值得批。

批注类型（kind 只能取以下八种）：
- hedge     妥协让步：作者在给不足找台阶、留后路（"虽然在…条件下"、"相对较高"这类含糊话）
- padding   凑字数：套话、正确的废话，整句删掉信息量不变
- stiff     生硬别扭：翻译腔、拗口、为严谨而硬拗的句式
- redundant 多余重复：同一件事换个说法又说一遍
- hype      吹嘘过头：证据只够说 X，作者说了 Y
- ai        AI 痕迹：典型 AI 腔——delve、crucial role、pave the way、Moreover/Furthermore 连环、句式长度均匀得可疑
- insight   点睛之笔：真正的关键句，值得画线
- warning   有坑：数据或方法的可疑之处，读者要小心

只输出 JSON，不要 markdown 代码块，不要解释：
{"notes":[{"para":<¶编号>, "quote":"<原句片段，逐字复制，≤80字符>", "kind":"<类型>", "note":"<批注，≤30字>"}]}

要求：
1. quote 必须逐字来自原文（可以只截句子的前半段），程序要靠它定位——绝对不要改写、翻译或加省略号
2. 每段最多 2 条；这一批总共 ≤10 条；没有值得批的段落就不批
3. note 要像人说话："这句纯凑字数，跳过""作者自己都没底""典型 AI 腔，删了不疼""这句是全文最硬的证据"
4. 八种类型都可能，别只盯一种；insight 和 warning 比吐槽更有价值"""


def analyze_marginalia(title: str, paras: list) -> list:
    """分块细读，返回 [{para_idx, page, quote, kind, note}]"""
    from concurrent.futures import ThreadPoolExecutor
    page_of = {p["idx"]: p["page"] for p in paras}
    chunks = [paras[i:i + 12] for i in range(0, len(paras), 12)]

    def run(chunk):
        body = "\n\n".join(f"¶{p['idx']} {p['text'][:900]}" for p in chunk)
        msgs = [
            {"role": "system", "content": MARGINALIA_SYSTEM},
            {"role": "user", "content": f"论文标题：{title or ''}\n\n{body}"},
        ]
        out = ""
        for attempt in range(2):   # 推理模型可能把 token 花在思考上，空结果重试一次
            out = chat(msgs, max_tokens=12000, temperature=0.3)
            if out.strip():
                break
        data = parse_json(out)
        return data.get("notes", []) if isinstance(data, dict) else []

    notes, seen = [], set()
    with ThreadPoolExecutor(max_workers=3) as ex:
        for batch in ex.map(run, chunks):
            for n in batch:
                try:
                    para_idx, quote, kind = int(n.get("para")), str(n.get("quote", "")).strip(), str(n.get("kind", ""))
                    note = str(n.get("note", "")).strip()[:60]
                except (TypeError, ValueError):
                    continue
                if not quote or kind not in ("hedge", "padding", "stiff", "redundant", "hype", "ai", "insight", "warning"):
                    continue
                if para_idx not in page_of or quote[:40] in seen:
                    continue
                seen.add(quote[:40])
                notes.append({"para_idx": para_idx, "page": page_of[para_idx], "quote": quote, "kind": kind, "note": note})
    return notes[:30]




_MOCK_PURPOSE = {
    "gap": "作者真正的出发点（演示）", "claim": "论文要证明的核心（演示）", "evidence": "核心数据段（演示）",
    "control": "仅为严谨的对照（演示）", "boilerplate": "样板段，可跳过（演示）", "limitation": "作者心虚处（演示）",
    "extension": "锦上添花（演示）", "background": "领域铺垫（演示）",
}


def mock_marginalia(paras: list) -> list:
    kinds = ["gap-note"] * 0 + ["insight", "padding", "hedge", "ai", "warning", "redundant"]
    notes = []
    for i, p in enumerate([p for p in paras if not p["in_refs"]][:6]):
        notes.append({"para_idx": p["idx"], "page": p["page"], "quote": p["text"][:60],
                      "kind": kinds[i % len(kinds)], "note": "〔演示〕" + _MOCK_PURPOSE.get(kinds[i % len(kinds)], "演示批注")})
    return notes


def mock_analyze(paras: list) -> dict:
    import db as gdb
    roles, purposes = {}, {}
    claim_idx = []
    for p in paras:
        t = p["text"].lower()
        if p.get("in_refs"):
            r = "boilerplate"
        elif re.search(r"however|remains|challenge|bottleneck|little attention|unclear", t):
            r = "gap"
        elif re.search(r"we propose|we demonstrate|we report|here we|this work|we develop|we design", t):
            r = "claim"
            claim_idx.append(p["idx"])
        elif re.search(r"\bfig(ure)?\.? ?\d|table \d", t) and re.search(r"\d+(\.\d+)?\s*%|increase|decrease|enhance|achieve|reach", t):
            r = "evidence"
        elif re.search(r"control|blank|reference sample|pristine|compared with|compared to", t):
            r = "control"
        elif re.search(r"xrd|sem|tem|xps|ftir|characteriz|instrument|calibrat|purchased|measured|condition", t):
            r = "boilerplate"
        elif re.search(r"limitation|caveat|future work|further stud|drawback", t):
            r = "limitation"
        elif re.search(r"moreover|furthermore|in addition|furthermore|optimiz|cycle stab", t):
            r = "extension"
        else:
            r = "background"
        roles[str(p["idx"])] = r
        purposes[str(p["idx"])] = _MOCK_PURPOSE.get(r, "（演示）")
    claims = [{"id": f"C{i+1}", "text": paras[ci-1]["text"][:40] + "…", "anchors": [
        int(k) for k, v in roles.items() if v == "evidence"][:2]} for i, ci in enumerate(claim_idx[:3])]
    if not claims:
        claims = [{"id": "C1", "text": "（演示模式：未识别到明确主张）", "anchors": []}]
    return {"claims": claims, "roles": roles, "purposes": purposes}

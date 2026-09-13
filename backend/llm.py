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

# 提示词纪律（写提示词之前先看一眼，2026-09-12 定的）
#   ① 写目的和角色，不写禁令。"不要复述某某"这类句子会把能力一起关掉——模型判断某句
#      需要展开讲，就该让它讲。要防重复，就把旧结论当**已知前提**交代给它，由它决定推多远。
#   ② 长度用"尽量 / 一两句"，不用"≤N 字"去砍。界面上的篇幅问题（一行塞多少）交给前端
#      排版解决，不靠提示词里截肢。
#   ③ 结构性要求（JSON 形状、依据段号 [¶n]）照旧写死——那不是限制能力，是让前端能渲染。
#   ④ 只在真需要的地方划边界：素材同源的两栏（④ 与导师三问）用**视角**区分，
#      不用"你不许说什么"区分。
#   ⑤ 每条提示词改完，拿这篇论文的真数据跑一次看输出——不跑就不知道是变松了还是变垮了。
#      验证"某函数不会调用模型"这类行为用哨兵桩，不要拿真实数据当靶子。


# ---------------- 基础调用 ----------------

def chat(messages: list, max_tokens: int = 4000, temperature: float = 0.2,
         no_think: bool = False) -> str:
    """非流式调用。no_think 的用途见 chat_stream：短任务别让推理模型先空想 8 秒。"""
    cfg = config.load()
    if cfg["mock"] or not cfg["provider"]["api_key"]:
        raise RuntimeError("MOCK")
    budget = max_tokens
    out = ""
    for _ in range(2):                      # 推理模型可能耗尽 token 空想，空结果加倍重试
        body = {"model": cfg["provider"]["model"], "messages": messages,
                "max_tokens": budget, "temperature": temperature}
        if no_think:
            body["thinking"] = {"type": "disabled"}
        r = httpx.post(
            f"{cfg['provider']['base_url'].rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {cfg['provider']['api_key']}"},
            json=body,
            timeout=600,
        )
        r.raise_for_status()
        out = (r.json()["choices"][0]["message"] or {}).get("content", "") or ""
        if out.strip():
            return out
        budget = int(budget * 1.6)
    return out


def chat_stream(messages: list, max_tokens: int = 6000, temperature: float = 0.3,
                no_think: bool = False):
    """逐字吐内容。前端要的是"字一个个出来"的手感，而不是转圈 20 秒再砸一大段。

    no_think：推理模型（GLM 系）输出正文前会先"思考"5–10 秒，期间一个字都不吐。
    翻译/短问答这类任务要不了那个深度，带上 thinking={"type":"disabled"} 能把
    首字时间从 ~8s 压到 ~1.5s；不认识这个字段的端点会忽略它，所以坏了也不伤。
    """
    cfg = config.load()
    if cfg["mock"] or not cfg["provider"]["api_key"]:
        raise RuntimeError("MOCK")
    payload = {"model": cfg["provider"]["model"], "messages": messages,
               "max_tokens": max_tokens, "temperature": temperature, "stream": True}
    if no_think:
        payload["thinking"] = {"type": "disabled"}
    with httpx.stream(
        "POST", f"{cfg['provider']['base_url'].rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {cfg['provider']['api_key']}"},
        json=payload, timeout=httpx.Timeout(600, connect=20),
    ) as r:
        r.raise_for_status()
        for raw in r.iter_lines():
            if not raw:
                continue
            line = raw[5:].strip() if raw.startswith("data:") else raw.strip()
            if not line or line == "[DONE]":
                if line == "[DONE]":
                    break
                continue
            try:
                j = json.loads(line)
            except ValueError:
                continue
            choices = j.get("choices") or [{}]
            delta = choices[0].get("delta") or {}
            piece = delta.get("content") or ""
            if piece:
                yield piece


def parse_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    i, j = text.find("{"), text.rfind("}")
    if i < 0 or j < 0:
        raise ValueError("模型这次没有按约定的 JSON 格式回，重试一次通常就好")
    raw = text[i:j + 1]
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # 模型最常见的两种小毛病先就地修：尾逗号、中文引号——别一巴掌推给用户
        fixed = re.sub(r",\s*([}\]])", r"\1", raw).replace("“", '"').replace("”", '"')
        return json.loads(fixed)


TERMS_SYSTEM = """你正在为一篇论文建它**自己的**术语表：读者读这篇时会卡住、需要中英对照的那些说法。

只收**这篇论文特有的**东西：
- 它自造或改名的方法 / 框架 / 模型名（例如 "transport figure of merit"）
- 它研究的材料、结构、器件、体系（例如 "rare-earth sesquioxide"）
- 它赖以成立的关键量、指标、判据（例如 "thermophotonic efficiency"）
- 它反复使用的领域专名（例如 "stokes shift"，中文"斯托克斯位移"）

**不要收**：通用学术词（method / result / figure / paper / study / data / analysis）、
只在参考文献里出现的词、任何一篇论文都会有的词。

kind 用三个短词之一：method（方法/框架）、material（材料/结构）、metric（量/指标/判据）。

另外单独给一份**这篇论文自己的缩写表** abbrs：正文里定义了、后面反复用的那些缩写，
键是缩写字面（照原文，如 "CNT"、"oPad"），值是它展开的英文全称 + 中文（≤40 字）。
不是这篇定义的、只是碰巧出现一次的缩写不要收；没有就给空对象。

只输出 JSON，不要 markdown 代码块，不要解释：
{"terms":[{"en":"<原文里的英文说法，逐字照抄>","zh":"<中文译名>","kind":"method|material|metric"}],
 "abbrs":{"<缩写>":"<英文全称 + 中文，≤40字>"}}

terms 给 15~40 条，宁多勿少但必须真的属于这篇；en 要能在正文里原样找到，别改写、别翻译。
"""


def extract_terms(title: str, paras: list) -> dict:
    """从正文里发掘**这篇论文自己的**术语与缩写（按篇建表用）。

    返回 {"terms": [...], "abbrs": {...}}；两者同一次调用出，是因为它们问的是同一件事
    （"这篇文献自己的说法有哪些"），分两次问既慢又会让两份表互相打架。
    失败由调用方兜住。
    """
    parts = ["¶%s %s" % (p["idx"], p["text"][:600]) for p in paras if not p.get("in_refs")]
    body = "\n\n".join(parts)
    msgs = [
        {"role": "system", "content": TERMS_SYSTEM},
        {"role": "user", "content": "论文标题：" + (title or "") + "\n\n" + body[:48000]},
    ]
    out = ""
    data = None
    for _ in range(3):                 # 空结果、或回了坏 JSON，都再试一次；别把解析器异常推给用户
        out = chat(msgs, max_tokens=8000, temperature=0.2)
        if not out.strip():
            continue
        try:
            data = parse_json(out)
            break
        except (json.JSONDecodeError, ValueError):
            continue
    if not isinstance(data, dict):
        return {"terms": [], "abbrs": {}}
    clean = _clean_terms(data.get("terms"))
    kept = _only_in_text(clean, paras)
    if len(kept) < len(clean):
        # 看得见：模型给的词里有几条正文里没有（多半是把一句话里不连续的成分拼成了一个词），
        # 静悄悄丢掉的话，"这次怎么少了几条"就永远查不出来
        print("[eggpaper] 术语过滤：正文里找不到的 %d 条已丢" % (len(clean) - len(kept)))
    return {"terms": kept, "abbrs": _clean_abbrs(data.get("abbrs"))}


def norm_text(s: str) -> str:
    """和前端 find.js / RightRail 的 fold() **逐字对齐**的归一化：

    小写 + 连字折叠（ﬁ/ﬂ…）+ 只留 [0-9a-z 汉字]。空格、连字符、标点一律丢掉——
    PDF 文字层里的下标（"Gd 2 O 2 S"）和换行 hyphen 靠这一步才对得上。
    两边不一致的话，界面就会给一个"正文里明明有"的词画上一道"—"。
    """
    out = []
    for ch in (s or "").lower():
        lig = _LIG.get(ch)
        if lig:
            out.append(lig)
        elif ch in _KEEP or "\u4e00" <= ch <= "\u9fff":
            out.append(ch)
    return "".join(out)


_LIG = {"\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi",
        "\ufb04": "ffl", "\ufb05": "ft", "\ufb06": "st"}   # 与 find.js 的 LIG 逐项一致
_KEEP = set("0123456789abcdefghijklmnopqrstuvwxyz")


def _only_in_text(terms: list, paras: list) -> list:
    """只留**真在这篇正文里**的词（用户口径：术语必须确实是本文的）。

    界面上每条术语右边那个「在文中找」箭头写的就是这个判断，判据要一模一样：
    对不上的词会出现一个点不动的"—"，而"术语表里一半的词查不到原文"正是之前的老毛病。
    """
    corpus = norm_text(" ".join(p.get("text") or "" for p in paras))
    if not corpus:                     # 没有正文可比（没解析/扫描件）就不过滤
        return terms
    return [t for t in terms if len(norm_text(t["en"])) >= 3 and norm_text(t["en"]) in corpus]


def _clean_terms(terms) -> list:
    if not isinstance(terms, list):
        return []
    clean, seen = [], set()
    for t in terms:
        if not isinstance(t, dict):
            continue
        en = str(t.get("en", "")).strip()
        zh = str(t.get("zh", "")).strip()
        kind = str(t.get("kind", "")).strip()
        if not en or not zh or len(en) > 80 or len(zh) > 40 or en.lower() in seen:
            continue
        seen.add(en.lower())
        clean.append({"en": en, "zh": zh,
                      "kind": kind if kind in ("method", "material", "metric") else ""})
    return clean[:48]


def _clean_abbrs(abbrs) -> dict:
    """缩写表：键必须真的像个缩写（短、没有空格），值是有内容的展开。"""
    if not isinstance(abbrs, dict):
        return {}
    out, seen = {}, set()
    for k, v in abbrs.items():
        k, v = str(k).strip(), str(v).strip()
        if not k or not v or len(k) > 16 or len(v) > 80 or any(ch.isspace() for ch in k):
            continue
        if k.lower() in seen:
            continue
        seen.add(k.lower())
        out[k] = v
    return out



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
 "purposes":{"<¶编号>":"<作者写这段的目的，≤22字，说人话>"},
 "problem":"<这篇论文要解决的问题：直接说清楚，一到两句，见要求 7>"}

要求：
1. claims 取 2~5 条，按论文叙事顺序；anchors 只能填 evidence 或 control 角色、且真实支撑该主张的段落编号
2. 每一个给出的段落都必须有 role 和 purpose，role 不得虚构枚举之外的值
3. purposes 用研究者口吻说人话，例如："堵审稿人的嘴""引出对照样品的必要性""交代测试条件，可跳过"
4. 不要虚构不存在的段落编号；参考文献部分（若有）一律标 boilerplate
4b. 图注（以 FIG./Figure/Table/Scheme 开头的段落）是**结果的一部分**，按它描述的内容给
    evidence 或 extension，绝不要标 boilerplate——读者正要看图注
5. 顺便抽取本文的缩写表 abbrs：{"abbrs":{"<缩写>":"<英文全称 + 中文，≤40字>"}}，没有就给空对象
6. 每条关键证据都要给出它直接回答的问题：{"evidence_qs":{"<¶编号>":"<该实验/数据直接回答的问题，≤22字>"}}
7. problem 是给读者看的**一句话答案**，不是摘抄：原文通常没有哪一句直接写着"我们要解决什么"，
   所以要用你自己的话把引言里的缺口综合成一句明确的陈述——谁在什么条件下没做到什么、
   因此这篇要回答什么；句尾用 [¶n] 标出它是从哪几段看出来的"""

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
        clean = []
        for a in anchors:
            n = _key_num(a)
            if n is not None and n in valid:
                clean.append(n)
        claims.append({"id": str(c.get("id") or c.get("cid") or f"C{i}"),
                       "text": str(c["text"])[:60],
                       "anchors": clean})

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

    abbrs_raw = data.get("abbrs") or {}
    abbrs = {str(k)[:24]: str(v)[:80] for k, v in abbrs_raw.items() if isinstance(v, str) and v.strip()}         if isinstance(abbrs_raw, dict) else {}
    eqs_raw = data.get("evidence_qs") or {}
    eqs = {}
    if isinstance(eqs_raw, dict):
        for k, v in eqs_raw.items():
            num = _key_num(k)
            if num in valid and isinstance(v, str) and v.strip():
                eqs[str(num)] = v.strip()[:36]

    if not roles:
        raise ValueError(f"骨架解析失败（roles 为空），原始输出: {out[:160]}")
    return {"claims": claims, "roles": roles, "purposes": purposes, "abbrs": abbrs, "evidence_qs": eqs}


# ---------------- 论文专属推荐问题 ----------------

def suggest_questions(title: str, claims: list, annos: dict) -> dict:
    """提问面板空态里的起步问题。

    这一栏和"导师三问"曾经撞车：两边都拿主张当素材、都在挑"证据强度/方法选择/适用边界"，
    生成出来是同一批问题换两种说法。这里改口径——**这一栏只帮读者读懂**（这里到底怎么做的、
    数字是在什么条件下得的、这个说法能不能推广到我要用的体系），审稿人式挑刺归导师三问。
    """
    claims_txt = "\n".join(f"- {c['text']}" for c in claims) or "（无）"
    gap = next((v["purpose"] for k, v in sorted(annos.items(), key=lambda x: int(x[0])) if v["role"] == "gap"), "")
    out = chat([
        {"role": "system", "content":
            "你在帮一位研究生读懂这篇论文。基于论文的主张与研究缺口，出 4 个他最想问出口的问题。"
            "好的问题具体到这篇的内容：怎么做的、数字在什么条件下得的、这个结论能不能用到别的体系、"
            "某个术语在这里到底指什么。你比他更懂这篇，什么最值得问由你判断——"
            "只要别停在'这篇讲了什么'这种翻开摘要就能回答的层面。"
            "每条一两句说完，别写成一段（面板里是竖排按钮，太长读着累）。"
            "只输出 JSON：{\"questions\":[\"...\"]}，不要代码块。"},
        {"role": "user", "content": f"论文标题：{title or ''}\n\n核心主张：\n{claims_txt}\n\n研究缺口：{gap}"},
    ], max_tokens=4000, temperature=0.5)
    data = parse_json(out)
    qs = [_clip_q(str(q)) for q in data.get("questions", []) if isinstance(q, str) and q.strip()]
    return {"questions": qs[:4]}


def _clip_q(q: str) -> str:
    """问题的长度上限只做兜底，且断在标点处。

    原来直接 `[:80]`——四个问题末尾全是半句（"…是否包含 vdW 色散修正与零点能"），
    读者拿到的是残句，比长一点糟糕得多。"""
    q = q.strip()
    if len(q) <= 150:
        return q
    cut = q[:150]
    stop = max(cut.rfind("？"), cut.rfind("。"), cut.rfind("；"), cut.rfind("? "))
    return cut[:stop + 1] if stop > 60 else cut + "…"


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
            '"findings":"<发现：最硬的数据结论，带关键数字；写得下就写，别硬压——按重要性排，读者先看到最要紧的那个>",'
            '"keywords":["<3~5个中文关键词>"]}'
            "不要 markdown 代码块，不要解释。"},
        {"role": "user", "content": f"论文标题：{title or ''}\n\n{body}"},
    ], max_tokens=4000, temperature=0.3)
    return parse_json(out)


# ---------------- 问答 ----------------

QA_SYSTEM = """你是论文精读助手，陪研究者读这篇论文，也顺手帮处理其他问题。
规则：
1. 论文相关的问题基于给定的论文全文回答；论文之外的知识性提问、计算、写作、翻译等请求
   也正常帮忙做——通用性要够，别把人挡回去，只要说明一句"这一点来自原文之外/原文未提及"
2. 论文内的关键论断标注依据段编号，格式如 [¶5] 或 [¶5,¶12]；论文没有依据的要明说
3. 引用参考文献列表不作为论文内容的依据
4. 回答用中文，术语首次出现给出英文"""


def ask_messages(title: str, paras: list, history: list, question: str, hits=None, summary: str = "") -> list:
    """组一次问答的消息体。流式与非流式走同一份，免得两边的上下文不一致。"""
    body = "\n\n".join(f"¶{p['idx']} {p['text'][:1000]}" for p in paras if not p.get("in_refs"))[:80000]
    msgs = [{"role": "system", "content": QA_SYSTEM + _gloss_block(hits) + f"\n\n论文标题：{title or ''}\n\n{body}"}]
    if summary:
        msgs.append({"role": "system", "content":
                     "以下是本次对话较早部分的摘要（其中的结论、术语译法、用户的关注点都继续有效，"
                     "不要重复已经确认过的事）：\n" + summary})
    for h in history:
        if h.get("role") in ("user", "assistant") and h.get("content"):
            msgs.append({"role": h["role"], "content": h["content"]})
    msgs.append({"role": "user", "content": question})
    return msgs


def cites_of(text: str) -> list:
    """从回答里抓 [¶5] 这类依据段号——引用角标可点击跳原文，靠的就是它。"""
    return sorted({int(n) for n in re.findall(r"¶\s*(\d+)", text or "")})


# ---------- 长对话的上下文压缩 ----------
# 一轮轮聊下去，上下文迟早会顶到上限。直接截断最早的那几轮是最坏的做法：用户
# 在前面确认过的结论、术语译法、关注点会凭空消失，模型就开始自相矛盾。所以
# "老的那几轮"要先压成摘要，和被删掉的消息一样——不在窗口里，但仍可追溯。
DIALOG_SUMMARY_SYSTEM = """你在为一次论文研读对话做上下文压缩。把给出的较早对话压成一份摘要，只输出摘要正文。

必须保留（这些丢了后面就全错）：
1. 已经确认过的结论、数字、事实，以及它们的依据段号（¶n）
2. 已经定下的术语译法与命名
3. 用户反复关心的点、明确的要求与否定的方向
4. 还没有解决的问题
可以丢：寒暄、重复表述、模型的推理过程、已经被推翻的中间结论。用中文，条目式，≤400 字。"""


def summarize_dialog(prev: str, messages: list) -> str:
    """把"已有摘要 + 这批较早的消息"压成新摘要。失败时退回原摘要（宁可留着旧的）。"""
    lines = []
    for m in messages:
        who = "用户" if m.get("role") == "user" else "助手"
        lines.append(f"{who}：{(m.get('content') or '')[:1500]}")
    user = ""
    if prev:
        user += f"[已有摘要]\n{prev}\n\n"
    user += "[需要并入的新对话]\n" + "\n".join(lines)
    try:
        out = chat([{"role": "system", "content": DIALOG_SUMMARY_SYSTEM},
                    {"role": "user", "content": user}], max_tokens=3000, temperature=0.2)
        return out.strip()[:2000] or prev
    except Exception:
        return prev


def ask(title: str, paras: list, history: list, question: str, hits=None, summary: str = "") -> dict:
    out = chat(ask_messages(title, paras, history, question, hits, summary), max_tokens=6000, temperature=0.3)
    return {"answer": out, "citations": cites_of(out)}


# ---------------- 划词/段落翻译 ----------------

def translate_messages(text: str, context: str = "", hits: list = None) -> list:
    """组一次翻译的消息体。流式与非流式共用，免得两条路译出来的风格不一致。"""
    gloss = ""
    if hits:
        gloss = "术语表（必须使用以下译法）：\n" + "\n".join(f"- {h['en']} → {h['zh']}" for h in hits) + "\n\n"
    user = (f"{gloss}将下面的学术英文翻译成中文。要求：专业、准确、说人话；"
            "化学式、数字、单位、变量、引用标记保留原样；人名不译；只输出译文。\n")
    if context:
        user += f"[上下文：{context[:600]}]\n\n"
    user += f"[待翻译]\n{text[:4000]}"
    return [
        {"role": "system", "content": "你是资深学术翻译，擅长化学/材料/工程领域论文的中英互译。"},
        {"role": "user", "content": user},
    ]


def translate(text: str, context: str = "", hits: list = None) -> str:
    out = chat(translate_messages(text, context, hits), max_tokens=4000, temperature=0.1)
    return out.strip()


def translate_stream(text: str, context: str = "", hits: list = None):
    """逐字翻译。划词等场景等不了 10 秒的整段——首字 1 秒内就该出现。
    翻译不需要模型先思考：关掉能把首字从 ~8s 压到 ~1.5s。"""
    return chat_stream(translate_messages(text, context, hits),
                       max_tokens=4000, temperature=0.1, no_think=True)


# ---------------- 眉批（句级人性化批注） ----------------

MARGINALIA_SYSTEM = """你是实验室里最会读论文的师兄，正在一篇论文的打印稿上给师弟/师妹写眉批。你只为一件事动笔：**让师弟少走弯路**——看清作者到底证明了什么、哪里证据不够、哪里和别处对不上。

只对"影响你怎么判断这篇论文"的句子下手：
- 主张：作者真的证明了什么，有没有说过头
- 证据与数据：指标、对照组、样本量、误差，够不够撑住那句话
- 前后一致：摘要/正文/图注/结论之间的数字与说法对不上
- 能不能复现：关键参数、单位、坐标系、统计口径缺没缺
- 读者的坑：容易被误读的地方；看着像结论、其实只是推测的地方

**语言层面基本不批**。语法、单复数、翻译腔、用词好坏、句子啰不啰嗦、像不像 AI 写的——
读者自己会读过去；批这些只会显得刻薄，而且占掉了页边本该留给硬问题的地方。
只有一种例外：这句话的**写法会让读者理解错**（含糊到看不出结论有多强、吹到超出证据、
绕到读不懂）。这时写的也是"读者会被带偏到哪儿"，不是"作者文笔不好"。

批注类型 kind：优先用这些常用款——口径一致，读者不用重新认颜色：
- insight   点睛之笔：真正的关键句，值得画线
- warning   有坑：数据或方法的可疑之处，读者要小心
- conflict  前后打架：这一处和本文别处的说法/数字对不上（摘要与结果、正文与图注、结论与数据）
- hedge     含糊其辞：**只在含糊会让读者误判结论强弱时**才用，不是"用词不精确"就写
- hype      吹嘘过头：证据只够说 X，作者说了 Y
- padding   凑字数：信息量为零的样板句，**且它挡住了读者的路**（比如把真正的结论埋在第 5 句）

另有三种保留款：stiff（生硬拗口）、ai（AI 腔）、redundant（重复）。它们仍然有效，但**默认不写**，
只有读者真会被绊住（一句话得读两遍才知道在说什么）时才用。整篇的语言类批注加起来不要超过 2-3 条。

真遇到装不下的东西（引用过时、坐标系没交代、图表看不清、术语前后不统一），自造一条：
kind 填 custom，label 填你那个短标签（≤6 字），band 填 good / warn / noise。
band 只有一个作用——决定它在纸上怎么被划、页边是什么颜色，所以只有三档。

只输出 JSON，不要 markdown 代码块，不要解释：
{"notes":[{"para":<¶编号>, "quote":"<原句，逐字复制，≤120字符>", "kind":"<类型或 custom>",
"label":"<自造类型的短标签，用常用款时留空>", "band":"<good|warn|noise，自造时必填>",
"note":"<批注，≤40字>"}]}

要求：
1. quote **要引完整的句子或完整的分句**：从句子（分句）的开头起，引到句末标点为止。
   不要在句子中间开始，也不要在句子中间断掉——程序靠它划出纸上的那一道线，
   引半截就只有半截被划上。逐字复制，绝对不要改写、翻译或加省略号；整句太长（>120 字）
   就取包含批注点的那个分句，但起点仍要落在自然的分句开头
2. 一段最多 3 条，**但不要求写满**。一篇四十来段的论文，十来二十条是正常量；
   写到三四十条基本就是在挑刺了。别为了凑数硬找，也别把真正该提的漏掉
3. note 像师兄在旁边低声提醒：就事论事，说清"哪里不对、为什么、你要注意什么"。
   不抖机灵、不嘲讽（"删了不疼""读着卡"这种口吻不要），也别空夸
4. insight / warning / conflict 比吐槽更有价值，但别为了显得有用而只挑刺或只夸
5. conflict 要指得出和哪里对不上（"和摘要说的 0.1 eV 不一致""图注写五段、正文写四段"），别只说"矛盾"

例：{"para":12,"quote":"The barrier is relatively high for practical operation.","kind":"custom",
"label":"口径含糊","band":"warn","note":"相对什么算高？全文没给这个数，也没给对比条件"}

不要写的例（语法、文风一律跳过）："a arrays 单复数不对"、"此外 Moreover 起句，AI 腔"、
"formidable challenge 是套话，删了不疼"。

"""


# 九种常用款 + 它们的档位。档位只有三档：值得读 / 要当心 / 可跳过——
# 页边颜色、纸上笔触都按它来。自造类型也必须落进这三档，否则页面上没有它的位置。
KINDS = ("hedge", "padding", "stiff", "redundant", "hype", "ai", "insight", "warning", "conflict")
BANDS = ("good", "warn", "noise")
BAND_OF = {"insight": "good",
           "warning": "warn", "hype": "warn", "ai": "warn", "conflict": "warn",
           "padding": "noise", "redundant": "noise", "stiff": "noise", "hedge": "noise"}

# 语言/文风类的 kind + 整篇硬上限。
# 为什么要有这道闸：改提示词之前，这篇 48 段的论文出过 48 条，其中 15 条是这一类
# （单复数、翻译腔、AI 腔、套话）——页边看着全是挑字眼的，而读者要的是"这篇哪里站不住"。
# 提示词里已经请模型少写，这里再上一道代码闸：模型偶尔忘了也兜得住。
PROSE_KINDS = ("stiff", "ai", "redundant", "padding")
PROSE_MAX = 4


CHUNK_PARAS = 12          # 一块多少段
CHUNK_WORKERS = 6         # 同时几块在跑


def analyze_marginalia(title: str, paras: list, on_chunk=None) -> tuple:
    """分块细读，返回 `(notes, failed_blocks)`：notes 是
    [{para_idx, page, quote, kind, label, band, note}]，failed_blocks 是**重试之后仍没成**的块数。

    为什么把失败数交出去：块级失败原来被静默吞掉——一次 429 只挂一块，界面照样显示"完成"，
    用户以为那些段落没问题。现在调用方能把"8 块里 1 块没成"写成一条提示，让他知道这片是空的。

    `on_chunk(已完成块数, 总块数)` 每读完一块回调一次（总量在第 0 秒就回调一次）——
    界面上那条小进度条吃的是它。为什么报"块"而不是百分比：一次请求是一条 12 段的完整
    LLM 调用，中间没有可信的颗粒度，报块数才是**真实**进度。

    **并发为什么是 6**（实测定的，别凭感觉调）：这篇 48 段的论文 = 4 块，
    3 并发要排两波、端到端 53 秒；6 并发一波就完、~36 秒。单块的耗时由模型决定
    （实测 DeepSeek 带思考 ~36 秒/块，其中七成 token 花在思考上），**墙钟时间 = 波数 × 单块时间**，
    所以能压的只有波数。再往上加并发收益就有限了（还容易被服务端限流），
    真正要更快只能减块数或减单块输出量，那是产品取舍，见 docs/plan.md M4.12。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    page_of = {p["idx"]: p["page"] for p in paras}
    n = CHUNK_PARAS
    chunks = [paras[i:i + n] for i in range(0, len(paras), n)]

    def run(chunk):
        body = "\n\n".join(f"¶{p['idx']} {p['text'][:900]}" for p in chunk)
        msgs = [
            {"role": "system", "content": MARGINALIA_SYSTEM},
            {"role": "user", "content": f"论文标题：{title or ''}\n\n{body}"},
        ]
        out = ""
        for attempt in range(2):   # 推理模型可能把 token 花在思考上，空结果重试一次
            out = chat(msgs, max_tokens=16000, temperature=0.3)
            if out.strip():
                break
        data = parse_json(out)
        return data.get("notes", []) if isinstance(data, dict) else []

    total = len(chunks)
    if on_chunk:
        try:
            on_chunk(0, total)     # 先报总量：界面从第一秒就能说"共 N 块"，而不是干等
        except Exception:
            pass
    batches = [None] * total
    state = {}                 # 块号 -> 成功；进度按"真的拿到结果的块"算，不虚报
    failed = []

    def wave(idx_list):
        """跑一批块（第一遍全部；第二遍只补失败的）。返回没成的块号。"""
        bad = []
        with ThreadPoolExecutor(max_workers=min(CHUNK_WORKERS, max(1, len(idx_list)))) as ex:
            # 用 as_completed 而不是 map：map 只在"轮到它"时才把结果交出来，第 1 块慢的时候
            # 后面早写完的块也报不出来——进度会假滞后。顺序仍按块号回填，最终批注次序不变。
            futs = {ex.submit(run, chunks[i]): i for i in idx_list}
            for fut in as_completed(futs):
                i = futs[fut]
                try:
                    batches[i] = fut.result()
                    state[i] = True
                except Exception:
                    bad.append(i)
                    batches[i] = []
                if on_chunk:
                    try:
                        on_chunk(len(state), total)
                    except Exception:
                        pass
        return bad

    failed = wave(list(range(total)))
    if failed and len(failed) < total:
        # 补一次失败的块。为什么要补：一次 429/超时只挂一块，用户拿到的就是"这篇有一段
        # 没有批注"，而他从界面上看不出来，只会以为那段没问题。代价只有失败块那么多。
        failed = wave(failed)
    if total and len(failed) == total:
        raise RuntimeError(f"{total} 块全部失败（模型或网络问题）")

    notes, seen, per_para = [], set(), {}
    prose_kept = 0
    for batch in batches:
        for n in batch:
            if not isinstance(n, dict):
                continue           # 模型偶尔把元素写成字符串/数字：跳过它，别让整篇崩在这儿
            try:
                para_idx, quote = int(n.get("para")), str(n.get("quote", "")).strip()
                note = str(n.get("note", "")).strip()[:80]
            except (TypeError, ValueError):
                continue
            kind = str(n.get("kind", "")).strip().lower()
            label, band = str(n.get("label", "")).strip()[:8], str(n.get("band", "")).strip().lower()
            if kind in KINDS:
                band, label = BAND_OF[kind], ""
            elif label and band in BANDS:
                kind = "custom"          # 自造款：标签 + 档位齐了才收，否则页面不知道把它画成什么
            else:
                continue
            # 语言/文风类整篇只留 PROSE_MAX 条（模型自己排的顺序就是它认为的轻重）
            if kind in PROSE_KINDS:
                if prose_kept >= PROSE_MAX:
                    continue
                prose_kept += 1
            # 一段最多三条：模型偶尔会对着同一句反复批，截胡在入口比让页边堆满好
            if not quote or quote[:40] in seen or per_para.get(para_idx, 0) >= 3:
                continue
            if para_idx not in page_of:
                continue
            seen.add(quote[:40])
            per_para[para_idx] = per_para.get(para_idx, 0) + 1
            notes.append({"para_idx": para_idx, "page": page_of[para_idx], "quote": quote,
                          "kind": kind, "label": label, "band": band, "note": note})
    return notes[:48], len(failed)




_MOCK_PURPOSE = {
    "gap": "作者真正的出发点（演示）", "claim": "论文要证明的核心（演示）", "evidence": "核心数据段（演示）",
    "control": "仅为严谨的对照（演示）", "boilerplate": "样板段，可跳过（演示）", "limitation": "作者心虚处（演示）",
    "extension": "锦上添花（演示）", "background": "领域铺垫（演示）",
}


def mock_marginalia(paras: list) -> list:
    kinds = ["insight", "padding", "hedge", "ai", "warning", "redundant"]
    notes = []
    for i, p in enumerate([p for p in paras if not p["in_refs"]][:6]):
        k = kinds[i % len(kinds)]
        notes.append({"para_idx": p["idx"], "page": p["page"], "quote": p["text"][:60], "kind": k,
                      "label": "", "band": BAND_OF[k], "note": "〔演示〕" + _MOCK_PURPOSE.get(k, "演示批注")})
    return notes


def mock_analyze(paras: list) -> dict:
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
    return {"claims": claims, "roles": roles, "purposes": purposes,
            "problem": "（演示模式）这篇论文要解决的问题是：演示用的占位陈述 [¶2]。"}


# ---------------- 方法卡（可复现 protocol） ----------------

def method_card(title: str, paras: list) -> dict:
    body = "\n\n".join(f"¶{p['idx']} {p['text'][:900]}" for p in paras)[:50000]
    out = chat([
        {"role": "system", "content":
            "你是实验室方法专家。把论文的方法部分整理成可复现的 protocol 卡，只输出 JSON："
            '{"goal":"<这套方法要达成什么，≤40字>",'
            '"system":"<材料体系/研究对象，≤60字>",'
            '"conditions":"<关键条件与参数：仪器、软件、参数值，≤120字>",'
            '"steps":["<步骤1，≤40字>", "<步骤2>", "..."],'
            '"notes":"<复现时要注意的坑，≤60字>"}'
            "步骤要具体可执行，保留关键数字。写法：化学式与上下标用 Unicode 字符"
            "（Sc₂O₃、10⁻⁷、Oₛ），不要 LaTeX、不要 $…$、不要用下划线代替下标。"
            "不要 markdown 代码块，不要解释。"},
        {"role": "user", "content": f"论文标题：{title or ''}\n\n{body}"},
    ], max_tokens=6000, temperature=0.3)
    return parse_json(out)


# ---------------- 引用信息 ----------------

CITATION_SYSTEM = """研究者要引用这篇论文，请你从首页上把"怎么引用它"那几个字段抄下来。他会把首页原始行（含刊头、页脚、DOI 行）和 PDF 自带元数据交给你。

这是一次**抄写**，不是一次推断。只有纸上印出来的才算数，没印的字段留空字符串——空白他还能自己补，
编出来的卷号页码会被原样粘进他的参考文献表，而且没人会去核，所以宁可空着。

- 不要从 DOI、URL、文件名、版权行里推年份。
- 年份要抄印出来的那一处：期刊论文看刊头/页脚的引文行（形如 J. Am. Chem. Soc. 2023, 145, 6789），
  预印本看日期戳（Dated: September 9, 2026，或页边的 6 Sep 2026）——那就是它的年份，不算猜。
- 不要按惯例把页码写成起止范围；不要"顺便"把刊名补全称。
- authors 按印刷顺序；family=姓，given=名，两条都要原始拼写；去掉上标数字、星号、十字、邮箱、机构名。
- title 用首页印的题目（跨行就拼成一行），不要用文件名，不要翻译，不要改写大小写。
- journal 是首页印的期刊全称，journal_abbr 是刊头/页脚印的缩写（形如 J. Am. Chem. Soc.）。
- pages 抄印出来的那串（6789-6795 或 6789−6795）；只印了起始页就写那一页。
- 预印本（arXiv 这类）：页边水印上那串编号就是它唯一的出处，抄进 preprint，
  形如 arXiv:2609.06350——版本号 v1/v2 去掉。它是水印不是正文，别当噪音跳过。

只输出 JSON：{"authors":[{"family":"","given":""}],"title":"","journal":"","journal_abbr":"",
"year":"","volume":"","issue":"","pages":"","doi":"","preprint":""}
不要 markdown 代码块，不要解释。"""


def extract_citation(title: str, src: str) -> dict:
    """从首页原文里抄出参考文献字段。排版不归它管（见 citation.py）。"""
    out = chat([
        {"role": "system", "content": CITATION_SYSTEM},
        {"role": "user", "content": f"[论文标题（版面分析抽的，可能不全）]\n{title or '（无）'}\n\n{src}"},
    ], max_tokens=2000, temperature=0, no_think=True)
    return parse_json(out)


# ---------------- 导师三问 ----------------

def advisor_questions(title: str, claims: list, warnings: list) -> dict:
    claims_txt = "\n".join(f"- {c['text']}" for c in claims) or "（无）"
    warn_txt = "\n".join(f"- {w}" for w in warnings) or "（无）"
    out = chat([
        {"role": "system", "content":
            # 与「问题④」共用同一批素材（主张 + 眉批有坑），差别在**视角**而不是禁令：
            # ④ 是"作者自己承认了什么"，这里是"拿到答辩桌上会被怎么问"。
            # 这里写过"不要再问这些"——那是拿能力换整洁：模型若判断某条薄弱点需要展开讲，
            # 就该让它讲。所以改成把旧结论当**已知前提**交给它，由它自己决定往前推到哪。
            "你是苛刻但建设性的导师。学生要拿这篇论文去组会汇报/答辩。"
            "下面这些『作者已承认的薄弱点』当作已知前提——它们本身不必再复述一遍，"
            "你要问的是更往里的问题：承认了还不够在哪里？缺的是哪一步证据？"
            "结论到底能走到哪一步？换个做法会怎样？"
            "出 3 个最可能把学生问住的问题（证据强度、方法选择、结论推广性都是好切入口，"
            "你也可以从自己对这篇的判断出发），每个配一份过关要点提纲。"
            '只输出 JSON：{"questions":[{"q":"<问题，≤60字>","outline":["<要点1，≤40字>","<要点2>"]}]}，不要代码块。'},
        {"role": "user", "content": f"论文标题：{title or ''}\n\n核心主张：\n{claims_txt}\n\n已承认的薄弱点（已知前提）：\n{warn_txt}"},
    ], max_tokens=6000, temperature=0.5)
    data = parse_json(out)
    qs = []
    for q in data.get("questions", []):
        if isinstance(q, dict) and q.get("q"):
            qs.append({"q": str(q["q"])[:80], "outline": [str(o)[:44] for o in (q.get("outline") or [])[:3]]})
    return {"questions": qs[:3]}


# ---------------- 视觉问答（框选/图表） ----------------

def vision_ask(image_dataurl: str, question: str) -> str:
    cfg = config.load()
    vm = cfg["provider"].get("vision_model", "").strip()
    if cfg["mock"]:
        return "〔演示模式〕视觉问答需要配置视觉模型。"
    if not vm:
        raise RuntimeError("未配置视觉模型（设置 → 视觉模型）")
    r = httpx.post(
        f"{cfg['provider']['base_url'].rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {cfg['provider']['api_key']}"},
        json={"model": vm, "max_tokens": 6000, "temperature": 0.3,
              "messages": [{"role": "user", "content": [
                  {"type": "image_url", "image_url": {"url": image_dataurl}},
                  {"type": "text", "text": question},
              ]}]},
        timeout=600,
    )
    r.raise_for_status()
    return (r.json()["choices"][0]["message"] or {}).get("content", "") or ""


# ---------------- 六个问题里需要生成的那三个 ----------------
# 语料只喂"回答这个问题用得上的那几类段落"，别把全文塞进去——
# 喂全了模型就会把别的问题的答案也一起倒出来，正好是我们要避免的。


def _paras_block(items: list, cap: int = 600) -> str:
    return "\n".join(f"¶{p['idx']} {(p.get('text') or '')[:cap]}" for p in items)


def _items(raw) -> dict:
    """三个生成题共用的清洗：lead/text/ask 三个字段，没有 text 的一条不留。"""
    items = []
    for it in (raw or []):
        if not isinstance(it, dict):
            continue
        text = str(it.get("text") or "").strip()[:300]
        if not text:
            continue
        items.append({"lead": str(it.get("lead") or "").strip()[:20],
                      "text": text,
                      "ask": str(it.get("ask") or "").strip()[:80],
                      "cites": cites_of(text)})
    return {"items": items[:3]}


def answer_problem(title: str, gaps: list, backgrounds: list, claims: list) -> dict:
    """要解决什么：**直接说出来**，不摘抄原文。

    ① 原来是"列出缺口段"，读者看到的是段落号加一截原文——可原文里根本没有哪一句写着
    "我们要解决什么"，那是要从引言里综合出来的。所以这一问现在由模型给一句明确的陈述，
    段落只作为依据标在句尾（读者要的原文在纸上，点 ¶ 就到）。
    给旧论文补这一问时走这条；重新析读之后，骨架提示词已经把 problem 一起产出了。
    """
    out = chat([
        {"role": "system", "content":
            "你在帮一位研究生说清一篇论文'要解决什么'。看下面给出的缺口段、背景段与主张，"
            "用你自己的话给出一句明确的陈述：谁在什么条件下还没做到什么，因此这篇论文要回答什么。"
            "要求：直接说结论，不要摘抄原文原句、不要'本文''该研究'开头；一到两句；"
            "句尾用 [¶n] 标出你是从哪几段看出来的。"
            '只输出 JSON：{"text":"<一两句话，含 [¶n] 标注>"}，不要代码块，不要解释。'},
        {"role": "user", "content":
            f"论文标题：{title or ''}\n\n"
            f"[作者指出的问题]\n{_paras_block(gaps)}\n\n"
            f"[背景]\n{_paras_block(backgrounds, 400)}\n\n"
            "[作者的主张]\n" + "\n".join(f"- {c['text']}" for c in claims)},
    ], max_tokens=3000, temperature=0.3, no_think=True)
    text = str(parse_json(out).get("text") or "").strip()[:400]
    return {"text": text, "cites": cites_of(text)}


def answer_why(title: str, gaps: list, backgrounds: list, claims: list) -> dict:
    """为什么要解决：为什么重要、为什么到现在还没解决。只吃缺口段 + 背景段 + 主张。"""
    out = chat([
        {"role": "system", "content":
            "你在帮一位研究生说清一篇论文'为什么值得做'。只依据给你的段落，说两句话："
            "这件事为什么重要（对领域、对什么有影响），以及为什么到现在还没解决或有争议。"
            "能标依据的句子都标上段号（如 [¶3]）。"
            '只输出 JSON：{"text":"<两句话，含 [¶n] 标注>"}，不要代码块，不要解释。'},
        {"role": "user", "content":
            f"论文标题：{title or ''}\n\n"
            f"[作者指出的问题]\n{_paras_block(gaps)}\n\n"
            f"[背景]\n{_paras_block(backgrounds, 400)}\n\n"
            "[作者的主张]\n" + "\n".join(f"- {c['text']}" for c in claims)},
    ], max_tokens=3000, temperature=0.3, no_think=True)
    text = str(parse_json(out).get("text") or "").strip()[:400]
    return {"text": text, "cites": cites_of(text)}


def answer_next(title: str, limits: list, exts: list, claims: list, warns: list) -> dict:
    """还能做什么：从作者承认的局限、他做的延伸、以及可疑之处往前推 2~3 条。"""
    payload = (f"论文标题：{title or ''}\n\n"
               f"[作者承认的局限]\n{_paras_block(limits)}\n\n"
               f"[作者做的延伸]\n{_paras_block(exts, 400)}\n\n"
               "[主张]\n" + "\n".join(f"- {c['text']}" for c in claims))
    if warns:
        payload += "\n\n[可疑之处]\n" + "\n".join(f"- {w}" for w in warns)
    out = chat([
        {"role": "system", "content":
            "你是带学生读论文的师兄。基于这篇论文承认的局限、它自己做的延伸、以及被标出的可疑之处，"
            "说出 2~3 条'接下来可以做什么'——要具体、可执行、有指向（该做哪个材料、该补哪组对照、"
            "该换哪种方法），每条尽量一两句话说清；'进一步研究''拓宽应用'这类话不算方向。"
            "这一栏的落点是**接下来做什么**：已经在别处说过的判断不必再交代一遍，"
            "直接说做什么、做了能拿到什么。"
            "每条配一句能直接拿去问模型的追问。"
            '只输出 JSON：{"items":[{"lead":"<方向名，≤10字>",'
            '"text":"<做什么、为什么，句尾带依据段号 [¶n]，有依据就标>",'
            '"ask":"<顺着这条往下问的一句话，≤40字>"}]}，不要代码块。'},
        {"role": "user", "content": payload},
    ], max_tokens=4000, temperature=0.45, no_think=True)
    return _items(parse_json(out).get("items"))


def answer_lens(title: str, one_line: str, claims: list, paras: list) -> dict:
    """换个学科怎么看：同一篇论文，别的领域的人会盯什么、会问什么。"""
    body = _paras_block([p for p in paras if not p.get("in_refs")][:24], 500)
    out = chat([
        {"role": "system", "content":
            "同一个问题，不同学科的人盯的地方不一样。这篇论文如果落到别的领域的人手里，"
            "他会盯它哪一部分、为什么。给出 2~3 个**真的不同**的领域视角"
            "（比如做催化的、做理论计算的、做表征的、做工程的、做产业化的），"
            "每个视角说清'他盯什么'和'他会问什么'——从这一行自己的判据出发，"
            "越具体越像真的。不必复述论文内容本身。"
            '只输出 JSON：{"items":[{"lead":"<领域名，≤10字>",'
            '"text":"<他会盯这篇的哪一点、为什么>",'
            '"ask":"<他会提出的那个问题，≤40字>"}]}，不要代码块。'},
        {"role": "user", "content":
            f"论文标题：{title or ''}\n一句话：{one_line or ''}\n\n"
            "[主张]\n" + "\n".join(f"- {c['text']}" for c in claims) +
            f"\n\n[正文节选]\n{body}"},
    ], max_tokens=4000, temperature=0.6, no_think=True)
    return _items(parse_json(out).get("items"))

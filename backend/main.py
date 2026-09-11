"""eggpaper 本地服务。唯一出网：用户配置的 LLM API 与 pdf2zh 翻译服务。"""
import json
import os
import threading
import time

import uvicorn
from fastapi import FastAPI, File, HTTPException, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

import config
import db
import llm
import pdfparse
import translate_full
from glossary_seed import SEED

app = FastAPI(title="eggpaper", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def _human_msg(exc: Exception) -> str:
    """把模型服务最常见的几种失败翻成人话。异常处理器与新加的流式问答共用这一份，
    免得"哪里报错"决定"用户看到什么"。"""
    msg = str(exc) or exc.__class__.__name__
    low = msg.lower()
    if "429" in msg or "too many requests" in low:
        return "模型服务限流了（429），等一会儿再试"
    if "401" in msg or "unauthorized" in low or "invalid api key" in low:
        return "API KEY 无效或过期（401），去设置里检查"
    if "402" in msg or "insufficient" in low or "quota" in low:
        return "账户余额/额度不足，模型服务拒绝了请求"
    if "timeout" in low or "timed out" in low:
        return "模型服务超时了，重试一次通常就好"
    if "connect" in low or "connection" in low:
        return "连不上模型服务，检查网络与 base_url"
    if "404" in msg and "model" in low:
        return "模型名不对（404），去设置里核对"
    if isinstance(exc, HTTPException):
        return str(exc.detail)
    return f"{exc.__class__.__name__}: {msg[:160]}"


@app.exception_handler(Exception)
async def _any_error(request, exc):
    """别让异常裸奔——前端只会拿到一个"500"，用户看到的就是这个数字。"""
    hint = _human_msg(exc)
    print(f"[eggpaper] {request.url.path} 出错 → {hint}")
    return JSONResponse({"detail": hint}, status_code=500)

PDF_DIR = os.path.join(config.DATA_DIR, "library")
TRANSLATED_DIR = os.path.join(config.DATA_DIR, "translated")


@app.on_event("startup")
def _startup():
    config.ensure_dirs()
    os.makedirs(PDF_DIR, exist_ok=True)
    os.makedirs(TRANSLATED_DIR, exist_ok=True)
    db.glossary_seed(SEED)


# ---------------- 设置 ----------------

@app.get("/api/settings")
def get_settings():
    cfg = config.load()
    p = cfg["provider"]
    return {"provider": {"base_url": p["base_url"], "model": p["model"],
                         "vision_model": p.get("vision_model", ""),
                         "has_key": bool(p["api_key"]), "key_masked": (p["api_key"][:6] + "…") if p["api_key"] else ""},
            "mock": cfg["mock"], "pdf2zh": cfg["pdf2zh"]}


@app.put("/api/settings")
def put_settings(body: dict):
    cfg = config.load()
    if "provider" in body:
        for k in ("base_url", "model", "vision_model"):
            if k in body["provider"]:
                cfg["provider"][k] = body["provider"][k].strip()
        if isinstance(body["provider"].get("api_key"), str) and "…" not in body["provider"]["api_key"]:
            cfg["provider"]["api_key"] = body["provider"]["api_key"].strip()
    if "mock" in body:
        cfg["mock"] = bool(body["mock"])
    if "pdf2zh" in body:
        cfg["pdf2zh"].update(body["pdf2zh"])
    # 填了 key 就自动退出演示模式
    if cfg["provider"]["api_key"] and "mock" not in body:
        cfg["mock"] = False
    config.save(cfg)
    return get_settings()


@app.post("/api/settings/test")
def test_settings():
    return llm.test_connection()


# ---------------- 论文 ----------------

@app.get("/api/papers")
def papers():
    return db.list_papers()


@app.post("/api/papers")
async def upload(file: UploadFile = File(...)):
    raw = await file.read()
    if raw[:4] != b"%PDF":
        raise HTTPException(400, "不是 PDF 文件")
    if len(raw) < 256:
        raise HTTPException(400, "这个文件太小了，不像是完整的 PDF（可能没传完）")
    pid = db.new_id()
    path = os.path.join(PDF_DIR, f"{pid}.pdf")
    with open(path, "wb") as f:
        f.write(raw)
    # 解析失败要收拾干净：留着半篇没有段落的"论文"，用户点开只能看见一个空书架
    try:
        title = pdfparse.extract_title(path)
        authors = pdfparse.extract_authors(path)
        paras = pdfparse.extract_paragraphs(path)
        import pymupdf
        n_pages = len(pymupdf.open(path))
    except Exception as e:
        try:
            os.remove(path)
        except OSError:
            pass
        raise HTTPException(400, f"这份 PDF 读不了：{_human_msg(e)}")
    db.create_paper(pid, file.filename, title, path, n_pages, authors)
    db.replace_paragraphs(pid, paras)
    row = db.get_paper(pid)
    # AI 主动：导入即后台通读，打开时简报已就绪（没有文字层的扫描件没得析读，直接标完成）
    db.update_paper(pid, last_read_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                    analysis_status="running" if paras else "done")
    if paras:
        threading.Thread(target=_run_analysis, args=(pid, paras), daemon=True).start()
    return {"paper": row, "n_paragraphs": len(paras),
            "n_captions": sum(1 for p in paras if p["caption"]),
            "no_text": not paras}


def _paper_or_404(pid: str) -> dict:
    p = db.get_paper(pid)
    if not p:
        raise HTTPException(404, "论文不存在")
    return p


NO_TEXT = "这份 PDF 没有可提取的文字层（多半是扫描件），析读和提问都无从下手；原文照样能读，图表也能框选问 AI"


def _require_paras(pid: str) -> None:
    """扫描件没有文字层：让它过一个"请求模型、等半天、返回胡话"的流程是最坏的选择，
    直接说清楚做不到什么、还能做什么。"""
    if not db.get_paragraphs(pid):
        raise HTTPException(400, NO_TEXT)


@app.get("/api/papers/{pid}")
def get_paper(pid: str):
    p = _paper_or_404(pid)
    paras = db.get_paragraphs(pid)
    p["n_paragraphs"] = len(paras)
    return p


@app.post("/api/papers/{pid}/touch")
def touch_paper(pid: str):
    """记一笔"最近读过"，文库按最近阅读排序时用。"""
    _paper_or_404(pid)
    db.update_paper(pid, last_read_at=time.strftime("%Y-%m-%d %H:%M:%S"))
    return {"ok": True}


def _rm(path: str):
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


@app.delete("/api/papers/{pid}")
def delete_paper(pid: str):
    p = _paper_or_404(pid)
    db.purge_paper(pid)          # 段落/骨架/眉批/问答会话/分类归属，一张表都不留
    # 文件也要走干净：原 PDF、双语版、译文版。双语文档是按 pid 命名的，
    # 万一 mono_path 没记上（翻译中途失败），按文件名把残留的一起扫掉。
    _rm(p["path"]); _rm(p["dual_path"]); _rm(p.get("mono_path"))
    try:
        for fn in os.listdir(TRANSLATED_DIR):
            if fn.startswith(pid):
                _rm(os.path.join(TRANSLATED_DIR, fn))
    except OSError:
        pass
    return {"ok": True}


@app.get("/api/papers/{pid}/pdf")
def paper_pdf(pid: str, variant: str = "original"):
    p = _paper_or_404(pid)
    if variant == "dual":
        if p["translate_status"] != "done" or not p["dual_path"] or not os.path.exists(p["dual_path"]):
            raise HTTPException(404, "双语版尚未生成")
        return FileResponse(p["dual_path"], media_type="application/pdf")
    if variant == "mono":
        mono = p.get("mono_path")
        if not mono and p["dual_path"]:
            mono = p["dual_path"].replace("-dual.pdf", "-mono.pdf")
        if not mono or not os.path.exists(mono):
            raise HTTPException(404, "译文版尚未生成")
        return FileResponse(mono, media_type="application/pdf")
    return FileResponse(p["path"], media_type="application/pdf")


@app.get("/api/papers/{pid}/paragraphs")
def paragraphs(pid: str):
    _paper_or_404(pid)
    # 旧库的段落只有段落框、没有行级坐标，页边引文就只能整段涂。
    # 惰性补一次：重解析同一份 PDF，重新分组的结果是确定的，idx 不变，批注不会错位。
    if db.paragraphs_need_lines(pid):
        p = db.get_paper(pid)
        path = p.get("path") or os.path.join(PDF_DIR, f"{pid}.pdf")
        try:
            if os.path.exists(path):
                db.replace_paragraphs(pid, pdfparse.extract_paragraphs(path))
        except Exception as e:
            print(f"[eggpaper] 行级坐标补解析失败 {pid}: {e}")
    return db.get_paragraphs(pid)


# ---------------- 骨架分析 ----------------

def _run_analysis(pid: str, paras: list):
    try:
        title = db.get_paper(pid)["title"]
        use = [p for p in paras if not p.get("in_refs")]
        if config.load()["mock"]:
            data = llm.mock_analyze(paras)
        else:
            data = llm.analyze_skeleton(title, use)
        # 参考文献段强制 boilerplate
        for p in paras:
            if p["in_refs"]:
                data["roles"][str(p["idx"])] = "boilerplate"
                data["purposes"][str(p["idx"])] = "参考文献"
        db.set_analysis(pid, data["claims"], {k: {"role": v, "purpose": data["purposes"].get(k, "")}
                                              for k, v in data["roles"].items()})
        db.update_paper(pid, abbrs=json.dumps(data.get("abbrs", {}), ensure_ascii=False),
                        evidence_qs=json.dumps(data.get("evidence_qs", {}), ensure_ascii=False))
    except Exception as e:
        db.set_analysis(pid, [], {}, status="error", error=f"{type(e).__name__}: {str(e)[:300]}")


@app.post("/api/papers/{pid}/analyze")
def analyze(pid: str):
    p = _paper_or_404(pid)
    _require_paras(pid)
    if p["analysis_status"] == "running":
        return {"status": "running"}
    db.update_paper(pid, analysis_status="running", analysis_error=None)
    threading.Thread(target=_run_analysis, args=(pid, db.get_paragraphs(pid)), daemon=True).start()
    return {"status": "running"}


@app.get("/api/papers/{pid}/analysis")
def analysis(pid: str):
    _paper_or_404(pid)
    status, claims, annos = db.get_analysis(pid)
    p = _paper_or_404(pid)
    eqs = json.loads(p["evidence_qs"]) if p.get("evidence_qs") else {}
    return {"status": status, "error": p["analysis_error"], "claims": claims, "annotations": annos, "evidence_qs": eqs}


@app.post("/api/papers/{pid}/override-role")
def override_role(pid: str, body: dict):
    _paper_or_404(pid)
    role = body.get("role") or ""
    # 空串 = 回到推断（卡片上的「回到推断」），别当成非法角色拒掉
    if role and role not in llm.ROLES:
        raise HTTPException(400, "角色不合法")
    db.override_annotation(pid, int(body["para_idx"]), role)
    return {"ok": True}


# ---------------- 眉批（句级批注） ----------------

def _resolve_rects(pid: str):
    """把 quote 定位成页面矩形。

    只作为**退路**：这里的搜索会跨不过换行和连字符，所以只能拿引文开头的一小段去搜，
    搜到的也只是那一行。真正的逐行精确划线在前端做（引文对回字符 → 取字符矩形）。
    框选钉子（kind=region）自带矩形，不参与。
    """
    import pymupdf
    p = db.get_paper(pid)
    if not p:
        return
    doc = None
    for n in db.get_marginalia(pid):
        if n["rect"] or n["kind"] == "region":
            continue
        if doc is None:
            doc = pymupdf.open(p["path"])
        rects = []
        for cut in (0, 60, 36, 20):
            probe = n["quote"] if cut == 0 else n["quote"][:cut].strip()
            if len(probe) < 8:
                continue
            rects = doc[n["page"]].search_for(probe, quads=True)
            if rects:
                break
        if rects:
            r = rects[0]
            db.marginalia_set_rect(n["id"], {"x0": r.x0, "y0": r.y0, "x1": r.x1, "y1": r.y1})
    if doc:
        doc.close()


def _run_marginalia(pid: str, paras: list):
    try:
        title = db.get_paper(pid)["title"]
        use = [p for p in paras if not p.get("in_refs")]
        notes = llm.mock_marginalia(paras) if config.load()["mock"] else llm.analyze_marginalia(title, use)
        db.set_marginalia(pid, notes)
        _resolve_rects(pid)
    except Exception as e:
        db.set_marginalia(pid, [], status="error", error=f"{type(e).__name__}: {str(e)[:300]}")


@app.post("/api/papers/{pid}/marginalia")
def marginalia_start(pid: str):
    p = _paper_or_404(pid)
    if p["marginalia_status"] == "running":
        return {"status": "running"}
    db.update_paper(pid, marginalia_status="running", marginalia_error=None)
    threading.Thread(target=_run_marginalia, args=(pid, db.get_paragraphs(pid)), daemon=True).start()
    return {"status": "running"}


@app.get("/api/papers/{pid}/marginalia")
def marginalia_get(pid: str):
    p = _paper_or_404(pid)
    if p["marginalia_status"] == "done" and any(not n["rect"] for n in db.get_marginalia(pid)):
        _resolve_rects(pid)
    return {"status": p["marginalia_status"], "error": p.get("marginalia_error"),
            "notes": [dict(n, rect=json.loads(n["rect"]) if n["rect"] else None) for n in db.get_marginalia(pid)]}


@app.post("/api/papers/{pid}/pin")
def pin_lookup(pid: str, body: dict):
    """把查译/段译/框选答疑钉到页边（用户资产，持久化）。"""
    p = _paper_or_404(pid)
    quote = (body.get("quote") or "").strip()
    note = (body.get("note") or "").strip()
    if not quote or not note:
        raise HTTPException(400, "quote 与 note 不能为空")
    para_idx = int(body.get("para_idx") or 0)
    # 框选答疑自带区域矩形：锚点就是那块区域，也不和别的钉子挤同一段。
    # 用 kind=region 单独标记：前端据此知道"这个矩形就是唯一真相"，
    # 而不是像引文钉子那样要回原文重新把引文对回字符。
    rect = body.get("rect") or None
    if rect:
        try:
            rect = {k: float(rect[k]) for k in ("x0", "y0", "x1", "y1")}
        except (KeyError, TypeError, ValueError):
            rect = None
    if rect:
        return {"id": db.marginalia_add(pid, para_idx, int(body.get("page") or 0), quote[:200],
                                        note[:600], kind="region", rect=rect)}
    # 同段重钉 = 更新而非新增
    dup = db.q("SELECT id FROM marginalia WHERE paper_id=? AND kind='lookup' AND para_idx=?",
               (pid, para_idx))
    if dup:
        db.q("UPDATE marginalia SET note=?, quote=? WHERE id=?", (note[:600], quote[:200], dup[0]["id"]), commit=True)
        return {"id": dup[0]["id"]}
    mid = db.marginalia_add(pid, para_idx, int(body.get("page") or 0), quote[:200], note[:600],
                            kind="lookup")
    return {"id": mid}


@app.delete("/api/papers/{pid}/marginalia/{mid}")
def marginalia_remove(pid: str, mid: int):
    _paper_or_404(pid)
    db.marginalia_delete(mid)
    return {"ok": True}


# ---------------- 一眼卡 ----------------

@app.get("/api/papers/{pid}/summary")
def summary(pid: str):
    p = _paper_or_404(pid)
    _require_paras(pid)
    if p["summary"]:
        return JSONResponse(json.loads(p["summary"]))
    if config.load()["mock"]:
        data = {"one_line": "〔演示模式〕这是一篇测试论文的一眼卡摘要。", "contributions": "演示贡献", "methods": "演示方法",
                "findings": "演示发现", "keywords": ["演示"]}
    else:
        hits = db.glossary_hit(" ".join(pp["text"] for pp in db.get_paragraphs(pid))[:60000])
        data = llm.summarize(p["title"], db.get_paragraphs(pid), hits)
    db.update_paper(pid, summary=json.dumps(data, ensure_ascii=False))
    return data


@app.get("/api/papers/{pid}/suggest")
def suggest(pid: str):
    p = _paper_or_404(pid)
    _require_paras(pid)
    if p["suggest"]:
        return JSONResponse(json.loads(p["suggest"]))
    if p["analysis_status"] != "done":
        return {"questions": []}
    if config.load()["mock"]:
        data = {"questions": ["〔演示〕核心证据的强度如何？", "〔演示〕方法上有什么可挑剔的？"]}
    else:
        _, claims, annos = db.get_analysis(pid)
        data = llm.suggest_questions(p["title"], claims, annos)
    db.update_paper(pid, suggest=json.dumps(data, ensure_ascii=False))
    return data


KIND_ZH = {"hedge": "妥协让步", "padding": "凑字数", "stiff": "生硬别扭", "redundant": "多余重复",
           "hype": "吹嘘过头", "ai": "AI 痕迹", "insight": "点睛之笔", "warning": "有坑", "lookup": "查译"}


@app.get("/api/papers/{pid}/advisor")
def advisor(pid: str):
    p = _paper_or_404(pid)
    _require_paras(pid)
    if p["advisor"]:
        return JSONResponse(json.loads(p["advisor"]))
    if p["analysis_status"] != "done":
        return {"questions": []}
    if config.load()["mock"]:
        data = {"questions": [{"q": "〔演示〕证据够硬吗？", "outline": ["演示要点"]}]}
    else:
        _, claims, annos = db.get_analysis(pid)
        warns = [f"{n['note']}（{n['quote'][:30]}）" for n in db.get_marginalia(pid) if n["kind"] == "warning"]
        data = llm.advisor_questions(p["title"], claims, warns)
    db.update_paper(pid, advisor=json.dumps(data, ensure_ascii=False))
    return data


@app.post("/api/ask-visual")
def ask_visual(body: dict):
    image = body.get("image") or ""
    if not image.startswith("data:image"):
        raise HTTPException(400, "缺少图像数据")
    question = (body.get("question") or "").strip() or "解释这张图/公式。"
    if config.load()["mock"]:
        return {"answer": "〔演示模式〕视觉问答需要配置视觉模型。"}
    ans = llm.vision_ask(image, question)
    if not ans.strip():
        raise HTTPException(503, "模型这次没返回内容，请重试")
    return {"answer": ans}


@app.get("/api/papers/{pid}/method-card")
def method_card(pid: str):
    p = _paper_or_404(pid)
    _require_paras(pid)
    if p["method_card"]:
        return JSONResponse(json.loads(p["method_card"]))
    if config.load()["mock"]:
        data = {"goal": "〔演示〕可复现 protocol", "system": "演示体系", "conditions": "演示条件",
                "steps": ["步骤一", "步骤二"], "notes": ""}
    else:
        data = llm.method_card(p["title"], db.get_paragraphs(pid))
    db.update_paper(pid, method_card=json.dumps(data, ensure_ascii=False))
    return data


@app.get("/api/papers/{pid}/export.md")
def export_md(pid: str):
    p = _paper_or_404(pid)
    paras = {x["idx"]: x for x in db.get_paragraphs(pid)}
    lines = [f"# {p['title'] or p['filename']}", ""]
    if p["summary"]:
        s = json.loads(p["summary"])
        lines += [f"**{s.get('one_line', '')}**", "",
                  f"- 贡献：{s.get('contributions', '')}",
                  f"- 方法：{s.get('methods', '')}",
                  f"- 发现：{s.get('findings', '')}", ""]
    status, claims, annos = db.get_analysis(pid)
    if claims:
        lines += ["## 论证骨架", ""]
        for c in claims:
            lines.append(f"- **{c['id']} {c['text']}**")
            for a in c["anchors"]:
                anno = annos.get(str(a))
                if anno:
                    lines.append(f"  - ¶{a} {llm.ROLE_ZH.get(anno['role'], '')}：{anno['purpose']}")
            lines.append("")
        by_role = {}
        for k, v in annos.items():
            by_role.setdefault(v["role"], []).append(int(k))
        lines += ["## 段落角色", ""]
        for role, idxs in sorted(by_role.items(), key=lambda x: -len(x[1])):
            lines.append(f"- **{llm.ROLE_ZH.get(role, role)}（{len(idxs)}）**：¶" + "、¶".join(str(i) for i in sorted(idxs)))
        lines.append("")
    notes = db.get_marginalia(pid)
    if notes:
        lines += ["## 眉批与查译", ""]
        for n in notes:
            who = "你" if n["kind"] == "lookup" else KIND_ZH.get(n["kind"], n["kind"])
            lines.append(f"- **[{who}] {n['note']}** — “{n['quote'][:48]}”")
        lines.append("")
    md = "\n".join(lines)
    return Response(content=md, media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename=eggpaper-{pid}.md"})


@app.get("/api/glossary/export.csv")
def glossary_export():
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["term_en", "term_zh", "domain", "note", "source"])
    for r in db.glossary_list():
        w.writerow([r["term_en"], r["term_zh"], r["domain"], r["note"], r["source"]])
    return Response(content=buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=eggpaper-glossary.csv"})


@app.get("/api/glossary/export.txt")
def glossary_export_anki():
    """Anki 可直接导入的 TSV（正面=英文，背面=中文）。"""
    lines = ["#separator:tab", "#html:false"]
    for r in db.glossary_list():
        lines.append(r["term_en"] + "\t" + r["term_zh"])
    return Response(content="\n".join(lines), media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=eggpaper-anki.txt"})


# ---------------- 图表速览 ----------------

@app.get("/api/papers/{pid}/figures")
def figures(pid: str):
    import pymupdf
    p = _paper_or_404(pid)
    out = []
    doc = pymupdf.open(p["path"])
    try:
        for pno in range(len(doc)):
            rects = [pymupdf.Rect(i["bbox"]) for i in doc[pno].get_image_info()
                     if i["bbox"][2] - i["bbox"][0] > 80 and i["bbox"][3] - i["bbox"][1] > 60]
            merged = []
            for r in rects:
                for m in merged:
                    if m.intersects(r):
                        m |= r
                        break
                else:
                    merged.append(r)
            for r in merged:
                out.append({"page": pno, "x0": round(r.x0, 1), "y0": round(r.y0, 1),
                            "x1": round(r.x1, 1), "y1": round(r.y1, 1)})
    finally:
        doc.close()
    return {"figures": out}


@app.get("/api/papers/{pid}/figure.png")
def figure_png(pid: str, page: int, x0: float, y0: float, x1: float, y1: float, dpi: int = 130):
    import pymupdf
    p = _paper_or_404(pid)
    doc = pymupdf.open(p["path"])
    try:
        pix = doc[page].get_pixmap(clip=pymupdf.Rect(x0, y0, x1, y1), dpi=dpi)
        return Response(content=pix.tobytes("png"), media_type="image/png")
    finally:
        doc.close()


# ---------------- 问答（流式 + 多会话） ----------------

def _sse(obj: dict) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


def _mock_stream(question: str):
    """演示模式也走流式：同一条前端代码路径，接上真 key 不用改任何东西。"""
    text = ("〔演示模式〕这是模拟回答，用来跑通界面。[¶1] 配好 API key 后这里会是真答案。\n\n"
            "· 你问的是：" + question[:60] + "\n"
            "· 回答会逐字出现，可以中途停下；停下时已经吐出来的部分会留着。")
    for i in range(0, len(text), 3):
        yield text[i:i + 3]
        time.sleep(0.02)


def _autotitle(pid: str, conv_id: int, question: str, is_first: bool):
    """第一个问题就是这摊对话的标题——和豆包/DeepSeek 一样，省得用户自己起名。"""
    if not is_first:
        return
    c = db.conv_get(conv_id)
    if c and c["title"] in ("", "新对话"):
        t = question.strip().replace("\n", " ")[:18]
        db.conv_rename(conv_id, t + ("…" if len(question.strip()) > 18 else ""))


# 上下文预算：最近几轮原样带，更早的折进摘要。
# 保留 8 条（4 轮）原文——足够接住"你刚才说的那个""再详细点"这类指代；
# 折到 12 条 / 6000 字以上才动手，别每问一句都去调一次压缩。
KEEP_MSGS, FOLD_AT, FOLD_CHARS = 8, 12, 6000


def _context(pid: str, conv_id: int, history: list):
    """返回 (摘要, 原样带上的历史)。超预算就把较早的几条压成摘要存回会话。

    压缩在提问之前同步做完，代价是每折一次多一次模型调用；但这是"宁慢不丢"的一步：
    直接把老消息截断，用户前面确认过的结论和术语就会凭空消失，模型随即开始自相矛盾。
    **压缩失败（限流、超时）时不许静默丢**：把老消息截短了照样带上，粗糙好过失忆。
    """
    c = db.conv_get(conv_id) or {}
    upto = c.get("summary_upto") or 0
    # tail = 还没被折进摘要的那些（删过的消息这里自然就没有了）
    tail = [m for m in history if (m.get("id") or 0) > upto]
    chars = sum(len(m.get("content") or "") for m in tail)
    if len(tail) <= FOLD_AT and chars <= FOLD_CHARS:
        return c.get("summary") or "", tail
    head, rest = tail[:-KEEP_MSGS], tail[-KEEP_MSGS:]
    if not head:
        return c.get("summary") or "", tail
    prev = c.get("summary") or ""
    new_sum = ""
    if not (config.load()["mock"] or not config.load()["provider"]["api_key"]):
        new_sum = llm.summarize_dialog(prev, head)
    if new_sum:
        db.conv_set_summary(conv_id, new_sum, head[-1].get("id") or 0)
        return new_sum, rest
    # 摘要没成：截短了带上，别丢
    short = [dict(m, content=(m.get("content") or "")[:240] + "…") for m in head[-40:]]
    return prev, short + rest


def _stream_answer(p: dict, conv_id: int, question: str):
    """流式回答。事件三种：delta（增量文字）/ done（依据段号 + 落库 id）/ error。

    落库的时机有两处：正常结束在这里写；用户中途点"停止生成"由前端调 qa-save 写，
    因为客户端断开时服务端不保证还能把生成器走完——让能拿到半截答案的那一端负责存。
    """
    pid = p["id"]
    uid = db.qa_add(pid, "user", question, conv_id=conv_id)
    hist = db.qa_history(pid, conv_id)[:-1]      # 不含刚写进去的这条；删过的消息这里自然就没有了
    buf = []
    try:
        if config.load()["mock"] or not config.load()["provider"]["api_key"]:
            gen = _mock_stream(question)
        else:
            summary, ctx = _context(pid, conv_id, hist)
            hits = db.glossary_hit(" ".join(pp["text"] for pp in db.get_paragraphs(pid))[:60000])
            gen = llm.chat_stream(llm.ask_messages(p["title"], db.get_paragraphs(pid), ctx, question,
                                                   hits, summary))
        for piece in gen:
            buf.append(piece)
            yield _sse({"type": "delta", "text": piece})
        ans = "".join(buf)
        if not ans.strip():
            raise RuntimeError("模型这次没返回内容")
        cites = llm.cites_of(ans)
        aid = db.qa_add(pid, "assistant", ans, cites, conv_id=conv_id)
        _autotitle(pid, conv_id, question, not any(h["role"] == "user" for h in hist))
        yield _sse({"type": "done", "citations": cites, "user_id": uid, "assistant_id": aid})
    except Exception as e:
        hint = _human_msg(e)
        ans = "".join(buf)
        aid = db.qa_add(pid, "assistant", (ans + "\n\n⚠ " + hint) if ans.strip() else "⚠ " + hint,
                        llm.cites_of(ans), conv_id=conv_id)
        yield _sse({"type": "error", "message": hint, "user_id": uid, "assistant_id": aid})


@app.post("/api/papers/{pid}/ask")
def ask(pid: str, body: dict):
    p = _paper_or_404(pid)
    question = (body.get("question") or "").strip()
    if not question:
        raise HTTPException(400, "问题不能为空")
    _require_paras(pid)          # 没有原文就没有"只依据原文"这回事，它不该去答
    conv_id = body.get("conv_id")
    if conv_id:
        c = db.conv_get(int(conv_id))
        if not c or c["paper_id"] != pid:
            raise HTTPException(404, "会话不存在")
        conv_id = int(conv_id)
    else:
        conv_id = db.conv_list(pid)[0]["id"]
    # 校验都过了再开流：一旦开始 SSE，HTTP 头已经发出去，改不成 4xx 了
    return StreamingResponse(_stream_answer(p, conv_id, question), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                                      "Connection": "keep-alive"})


@app.get("/api/papers/{pid}/conversations")
def conversations(pid: str):
    _paper_or_404(pid)
    return db.conv_list(pid)

@app.post("/api/papers/{pid}/conversations")
def conversation_new(pid: str, body: dict = None):
    _paper_or_404(pid)
    return {"id": db.conv_create(pid, (body or {}).get("title") or "新对话")}


@app.patch("/api/conversations/{cid}")
def conversation_patch(cid: int, body: dict):
    if not db.conv_get(cid):
        raise HTTPException(404, "会话不存在")
    db.conv_rename(cid, (body.get("title") or "新对话").strip() or "新对话")
    return {"ok": True}


@app.delete("/api/conversations/{cid}")
def conversation_delete(cid: int):
    if not db.conv_get(cid):
        raise HTTPException(404, "会话不存在")
    db.conv_delete(cid)
    return {"ok": True}


@app.get("/api/papers/{pid}/qa-history")
def qa_history(pid: str, conv_id: int = None):
    _paper_or_404(pid)
    if conv_id is None:
        return {"messages": db.qa_history(pid), "conv_id": None}
    return {"messages": db.qa_history(pid, conv_id), "conv_id": conv_id}


@app.post("/api/papers/{pid}/qa-save")
def qa_save(pid: str, body: dict):
    """用户中途"停止生成"：把已经吐出来的半截答案存下来。
    返回两端 id，前端据此把"删除/重新生成"接回真实的行上。"""
    _paper_or_404(pid)
    content = (body.get("content") or "").strip()
    conv_id = body.get("conv_id")
    if not content:
        return {"ok": False}
    aid = db.qa_add(pid, "assistant", content, llm.cites_of(content), conv_id=conv_id)
    return {"ok": True, "citations": llm.cites_of(content),
            "assistant_id": aid, "user_id": db.qa_last_user_id(pid, conv_id) if conv_id else None}


@app.post("/api/papers/{pid}/regenerate")
def qa_regenerate(pid: str, body: dict):
    """重新生成：把这一问一答都撤掉，返回原问题，由前端重新发问。"""
    _paper_or_404(pid)
    conv_id = body.get("conv_id")
    if not conv_id:
        raise HTTPException(400, "缺少会话")
    q = db.qa_drop_last_assistant(pid, int(conv_id))
    if not q:
        raise HTTPException(400, "没有可重新生成的问题")
    return {"question": q}


@app.delete("/api/conversations/{cid}/messages/{mid}")
def qa_delete_one(cid: int, mid: int):
    db.qa_delete(mid)
    return {"ok": True}


@app.delete("/api/papers/{pid}/qa-history")
def qa_clear(pid: str):
    _paper_or_404(pid)
    db.qa_clear(pid)
    return {"ok": True}


# ---------------- 文库分类 ----------------

@app.get("/api/collections")
def collections():
    return {"collections": db.collections_list(), "map": db.collection_map()}


@app.post("/api/collections")
def collection_new(body: dict):
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "分类要有名字")
    return {"id": db.collection_add(name)}


@app.patch("/api/collections/{cid}")
def collection_patch(cid: int, body: dict):
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "分类要有名字")
    db.collection_rename(cid, name)
    return {"ok": True}


@app.delete("/api/collections/{cid}")
def collection_delete(cid: int):
    db.collection_delete(cid)
    return {"ok": True}


@app.put("/api/papers/{pid}/collections")
def paper_collections_set(pid: str, body: dict):
    _paper_or_404(pid)
    ids = body.get("ids") or []
    if not isinstance(ids, list):
        raise HTTPException(400, "ids 必须是数组")
    db.set_paper_collections(pid, ids)
    return {"ok": True, "ids": ids}


# ---------------- 翻译 ----------------

def _mock_translate(text: str):
    """演示模式的假译文也假装在打字：同一条前端代码路径。"""
    t = "〔演示译文〕" + text[:120]
    for i in range(0, len(t), 3):
        yield t[i:i + 3]
        time.sleep(0.02)


def _translate_sse(pid: str, text: str, context: str, hits: list):
    """流式翻译。事件：delta（增量）/ done（术语命中）/ error（人话）。

    术语命中随 done 一起回——它在翻译开始前就查好了，不必等译文走完。
    """
    buf = []
    try:
        if config.load()["mock"] or not config.load()["provider"]["api_key"]:
            gen = _mock_translate(text)
        else:
            gen = llm.translate_stream(text, context, hits)
        for piece in gen:
            buf.append(piece)
            yield _sse({"type": "delta", "text": piece})
        zh = "".join(buf)
        if not zh.strip():
            raise RuntimeError("模型这次没返回内容")
        yield _sse({"type": "done", "hits": hits})
    except Exception as e:
        yield _sse({"type": "error", "message": _human_msg(e)})


@app.post("/api/papers/{pid}/translate-selection")
def translate_selection(pid: str, body: dict):
    _paper_or_404(pid)
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "没有选中文本")
    hits = db.glossary_hit(text)
    context = body.get("context", "")
    return StreamingResponse(_translate_sse(pid, text, context, hits), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/papers/{pid}/translate-para")
def translate_para(pid: str, body: dict):
    p = _paper_or_404(pid)
    paras = {p_["idx"]: p_ for p_ in db.get_paragraphs(pid)}
    idx = int(body["idx"])
    if idx not in paras:
        raise HTTPException(404, "段落不存在")
    _require_paras(pid)          # 扫描件没有段落可译，直说
    para = paras[idx]
    hits = db.glossary_hit(para["text"])
    ctx = paras.get(idx - 1, {}).get("text", "")
    return StreamingResponse(_translate_sse(pid, para["text"], ctx, hits), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/papers/{pid}/translate-full")
def translate_full_start(pid: str):
    p = _paper_or_404(pid)
    cfg = config.load()
    translate_full.start(pid, p["path"], TRANSLATED_DIR, cfg["pdf2zh"]["service"], cfg["pdf2zh"]["options"])
    return {"status": "running"}


@app.get("/api/papers/{pid}/translate-status")
def translate_full_status(pid: str):
    _paper_or_404(pid)
    j = translate_full.job(pid)
    if j["status"] == "done":
        p = _paper_or_404(pid)
        mono = j["dual"].replace("-dual.pdf", "-mono.pdf") if os.path.exists(j["dual"].replace("-dual.pdf", "-mono.pdf")) else ""
        if j["dual"] != p["dual_path"] or mono != (p["mono_path"] or ""):
            db.update_paper(pid, dual_path=j["dual"], mono_path=mono, translate_status="done")
    return j


# ---------------- 术语表 ----------------

@app.get("/api/glossary")
def glossary_list():
    return db.glossary_list()


@app.post("/api/glossary")
def glossary_add(body: dict):
    en = (body.get("term_en") or "").strip()
    zh = (body.get("term_zh") or "").strip()
    if not en or not zh:
        raise HTTPException(400, "中英文都要填")
    gid = db.glossary_add(en, zh, body.get("domain", ""), body.get("note", ""), body.get("source", "manual"))
    return {"id": gid}


@app.delete("/api/glossary/{gid}")
def glossary_delete(gid: int):
    db.glossary_delete(gid)
    return {"ok": True}


# ---------------- 前端静态托管（构建后） ----------------

DIST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend", "dist")
if os.path.isdir(DIST):
    from fastapi.staticfiles import StaticFiles
    app.mount("/", StaticFiles(directory=DIST, html=True), name="static")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8430)

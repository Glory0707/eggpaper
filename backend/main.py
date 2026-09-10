"""eggpaper 本地服务。唯一出网：用户配置的 LLM API 与 pdf2zh 翻译服务。"""
import json
import os
import threading

import uvicorn
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

import config
import db
import llm
import pdfparse
import translate_full
from glossary_seed import SEED

app = FastAPI(title="eggpaper", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

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
                         "has_key": bool(p["api_key"]), "key_masked": (p["api_key"][:6] + "…") if p["api_key"] else ""},
            "mock": cfg["mock"], "pdf2zh": cfg["pdf2zh"]}


@app.put("/api/settings")
def put_settings(body: dict):
    cfg = config.load()
    if "provider" in body:
        for k in ("base_url", "model"):
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
    if not raw[:4] == b"%PDF":
        raise HTTPException(400, "不是 PDF 文件")
    pid = db.new_id()
    path = os.path.join(PDF_DIR, f"{pid}.pdf")
    with open(path, "wb") as f:
        f.write(raw)
    title = pdfparse.extract_title(path)
    paras = pdfparse.extract_paragraphs(path)
    import pymupdf
    n_pages = len(pymupdf.open(path))
    db.create_paper(pid, file.filename, title, path, n_pages)
    db.replace_paragraphs(pid, paras)
    row = db.get_paper(pid)
    # AI 主动：导入即后台通读，打开时简报已就绪
    db.update_paper(pid, analysis_status="running")
    threading.Thread(target=_run_analysis, args=(pid, paras), daemon=True).start()
    return {"paper": row, "n_paragraphs": len(paras),
            "n_captions": sum(1 for p in paras if p["caption"])}


def _paper_or_404(pid: str) -> dict:
    p = db.get_paper(pid)
    if not p:
        raise HTTPException(404, "论文不存在")
    return p


@app.get("/api/papers/{pid}")
def get_paper(pid: str):
    p = _paper_or_404(pid)
    paras = db.get_paragraphs(pid)
    p["n_paragraphs"] = len(paras)
    return p


@app.delete("/api/papers/{pid}")
def delete_paper(pid: str):
    p = _paper_or_404(pid)
    for t in ("paragraphs", "annotations", "claims", "qa_messages", "marginalia"):
        db.q(f"DELETE FROM {t} WHERE paper_id=?", (pid,), commit=True)
    db.q("DELETE FROM papers WHERE id=?", (pid,), commit=True)
    for f in (p["path"], p["dual_path"]):
        if f and os.path.exists(f):
            try:
                os.remove(f)
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
    return db.get_paragraphs(pid)


# ---------------- 骨架分析 ----------------

def _run_analysis(pid: str, paras: list):
    try:
        title = db.get_paper(pid)["title"]
        use = [p for p in paras if not p["in_refs"]]
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
    except Exception as e:
        db.set_analysis(pid, [], {}, status="error", error=f"{type(e).__name__}: {str(e)[:300]}")


@app.post("/api/papers/{pid}/analyze")
def analyze(pid: str):
    p = _paper_or_404(pid)
    if p["analysis_status"] == "running":
        return {"status": "running"}
    db.update_paper(pid, analysis_status="running", analysis_error=None)
    threading.Thread(target=_run_analysis, args=(pid, db.get_paragraphs(pid)), daemon=True).start()
    return {"status": "running"}


@app.get("/api/papers/{pid}/analysis")
def analysis(pid: str):
    _paper_or_404(pid)
    status, claims, annos = db.get_analysis(pid)
    return {"status": status, "error": _paper_or_404(pid)["analysis_error"], "claims": claims, "annotations": annos}


@app.post("/api/papers/{pid}/override-role")
def override_role(pid: str, body: dict):
    _paper_or_404(pid)
    role = body.get("role")
    if role not in llm.ROLES:
        raise HTTPException(400, "角色不合法")
    db.override_annotation(pid, int(body["para_idx"]), role)
    return {"ok": True}


# ---------------- 眉批（句级批注） ----------------

def _resolve_rects(pid: str):
    """把 quote 定位为页面矩形（惰性：只解析尚未定位的批注）。"""
    import pymupdf
    p = db.get_paper(pid)
    if not p:
        return
    doc = None
    for n in db.get_marginalia(pid):
        if n["rect"]:
            continue
        if doc is None:
            doc = pymupdf.open(p["path"])
        rects = []
        for cut in (60, 36, 20):
            probe = n["quote"][:cut].strip()
            if len(probe) < 8:
                continue
            rects = doc[n["page"]].search_for(probe, quads=False)
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
    """把查译/段译钉到页边（用户资产，持久化）。"""
    p = _paper_or_404(pid)
    quote = (body.get("quote") or "").strip()
    note = (body.get("note") or "").strip()
    if not quote or not note:
        raise HTTPException(400, "quote 与 note 不能为空")
    mid = db.marginalia_add(pid, int(body.get("para_idx") or 0), int(body.get("page") or 0),
                            quote[:200], note[:600], kind="lookup")
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


# ---------------- 问答 ----------------

@app.post("/api/papers/{pid}/ask")
def ask(pid: str, body: dict):
    p = _paper_or_404(pid)
    question = (body.get("question") or "").strip()
    if not question:
        raise HTTPException(400, "问题不能为空")
    db.qa_add(pid, "user", question)
    if config.load()["mock"]:
        ans, cites = "〔演示模式〕这是模拟回答。[¶1]", [1]
    else:
        hits = db.glossary_hit(" ".join(pp["text"] for pp in db.get_paragraphs(pid))[:60000])
        r = llm.ask(p["title"], db.get_paragraphs(pid), db.qa_history(pid), question, hits)
        ans, cites = r["answer"], r["citations"]
    db.qa_add(pid, "assistant", ans, cites)
    return {"answer": ans, "citations": cites}


@app.get("/api/papers/{pid}/qa-history")
def qa_history(pid: str):
    _paper_or_404(pid)
    return db.qa_history(pid)


@app.delete("/api/papers/{pid}/qa-history")
def qa_clear(pid: str):
    _paper_or_404(pid)
    db.qa_clear(pid)
    return {"ok": True}


# ---------------- 翻译 ----------------

@app.post("/api/papers/{pid}/translate-selection")
def translate_selection(pid: str, body: dict):
    _paper_or_404(pid)
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "没有选中文本")
    hits = db.glossary_hit(text)
    if config.load()["mock"]:
        zh = "〔演示译文〕" + text[:120] + "…"
    else:
        zh = llm.translate(text, body.get("context", ""), hits)
    return {"zh": zh, "hits": hits}


@app.post("/api/papers/{pid}/translate-para")
def translate_para(pid: str, body: dict):
    p = _paper_or_404(pid)
    paras = {p_["idx"]: p_ for p_ in db.get_paragraphs(pid)}
    idx = int(body["idx"])
    if idx not in paras:
        raise HTTPException(404, "段落不存在")
    para = paras[idx]
    hits = db.glossary_hit(para["text"])
    if config.load()["mock"]:
        zh = "〔演示译文〕" + para["text"][:200] + "…"
    else:
        ctx = paras.get(idx - 1, {}).get("text", "")
        zh = llm.translate(para["text"], ctx, hits)
    if not zh.strip():
        raise HTTPException(503, "模型这次没返回内容，请重试一次")
    return {"zh": zh, "hits": hits}


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

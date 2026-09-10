# 🥚 eggpaper

> Crack the paper, read the marrow. 剥开论文的壳，直接读论证的芯。

**eggpaper** is a local-first, lightweight reader for scientific papers.

It does two things well:

1. **Glossary-accurate translation** — full-paper bilingual reading with layout & formulas preserved, where *your* personal glossary governs every translation, summary and answer.
2. **Argument-skeleton analysis** — it reads the paper the way the author wrote it: which claim is the core, which experiment is key evidence, which is just a control, which section is boilerplate you can skip.

No accounts. No subscriptions. Papers never leave your machine — only LLM API calls you configure (DeepSeek / GLM / any OpenAI-compatible endpoint, including local Ollama).

## Status

🚧 **MVP running.** Upload → parse → argument-skeleton analysis → margin role tabs → sentence-level marginalia (妥协 / 凑字数 / AI 痕迹…) → glossary-accurate selection & paragraph translation → full-paper Q&A with ¶-citations → one-glance summary card — all working end-to-end against real two-column chemistry papers.

Run it:

```bash
# backend (Python 3.10+)
pip install -r backend/requirements.txt
python backend/main.py            # 127.0.0.1:8430

# frontend
cd frontend && npm install && npm run dev   # http://localhost:5173
```

Open 设置 → paste any OpenAI-compatible base_url + key + model (DeepSeek / GLM / local Ollama all work). Without a key it runs in demo mode. Full design & roadmap: [docs/design.md](docs/design.md) (Chinese).

## Planned features

| | Feature | Status |
|---|---|---|
| 🌐 | Full-paper bilingual reading (pdf2zh engine, layout & formulas preserved) | ✅ done |
| ✏️ | Term-level translation with a personal glossary injected everywhere | ✅ done |
| 🦴 | **Argument skeleton** — claims → evidence chain → controls, annotated on the margin | ✅ done |
| ✒️ | **Marginalia** — sentence-level human notes: hedging, padding, stiff phrasing, AI-flavor… | ✅ done |
| 🔎 | Skim mode — fade the boilerplate, keep the load-bearing paragraphs | ✅ done |
| 💬 | Single-paper Q&A with ¶-anchored answers | ✅ done |
| 🃏 | One-glance summary card | ✅ done |
| 🔀 | Three translation views: side-by-side spread / full-text swap / interleaved | 🔜 next |
| 🧪 | Methods protocol card · abbreviations table · figures gallery | planned |
| 📓 | Markdown & Anki export · Zotero interop | planned |
| ⚖️ | Review mode · cross-paper data extraction · region-select visual Q&A | later |

## Why

Existing tools are either cloud-heavy (accounts, subscriptions, uploads), translation-only, or generic PDF chat. eggpaper fills the gap: a small local tool where terminology stays consistent across everything, and where the differentiator is not "translate more" but *"understand what the author is actually doing"* — a capability grounded in argumentative-zoning / rhetorical-role research that no reading product ships today.

## License

[AGPL-3.0](LICENSE) — same ecosystem as pdf2zh/BabelDOC. Never fork this into a closed-source product.

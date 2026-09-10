# 🥚 eggpaper

> Crack the paper, read the marrow. 剥开论文的壳，直接读论证的芯。

**eggpaper** is a local-first, lightweight reader for scientific papers.

It does two things well:

1. **Glossary-accurate translation** — full-paper bilingual reading with layout & formulas preserved, where *your* personal glossary governs every translation, summary and answer.
2. **Argument-skeleton analysis** — it reads the paper the way the author wrote it: which claim is the core, which experiment is key evidence, which is just a control, which section is boilerplate you can skip.

No accounts. No subscriptions. Papers never leave your machine — only LLM API calls you configure (DeepSeek / GLM / any OpenAI-compatible endpoint, including local Ollama).

## Status

🚧 **Design stage.** MVP development starting — see [docs/design.md](docs/design.md) for the full design (in Chinese), roadmap and MVP acceptance criteria.

## Planned features

| | Feature | Priority |
|---|---|---|
| 🌐 | Full-paper bilingual reading, layout & formulas preserved (pdf2zh engine) | P0 |
| ✏️ | Term-level translation with a personal glossary injected everywhere | P0 |
| 🦴 | **Argument skeleton** — claims → evidence chain → controls → limitations, annotated on the page margin | P1 |
| 🔎 | Skim mode — expand only the load-bearing paragraphs (and only translate those) | P1 |
| 💬 | Single-paper Q&A with page-anchored, role-tagged answers | P1.5 |
| 🃏 | Summary cards: one-glance card / methods protocol card / figures gallery | P1.5 |
| 📓 | Term book (export to CSV/Anki), Markdown notes export, Zotero interop | P2 |
| ⚖️ | Review mode — auto-draft weaknesses from the evidence chain | P2+ |

## Why

Existing tools are either cloud-heavy (accounts, subscriptions, uploads), translation-only, or generic PDF chat. eggpaper fills the gap: a small local tool where terminology stays consistent across everything, and where the differentiator is not "translate more" but *"understand what the author is actually doing"* — a capability grounded in argumentative-zoning / rhetorical-role research that no reading product ships today.

## License

[AGPL-3.0](LICENSE) — same ecosystem as pdf2zh/BabelDOC. Never fork this into a closed-source product.

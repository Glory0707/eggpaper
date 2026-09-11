"""引用格式排版的自查（不联网、不调模型）：手写几组元数据，看排出来的条目对不对。

改 backend/citation.py（格式、作者数规则、缺字段的降级、防编造守卫）之后跑一遍：

    python tools/citation_check.py

看三件事：九种格式排出来对不对；只有题目作者这种稀疏元数据会不会排成半截；以及
源文里没印过的卷期页有没有被 sanity() 擦掉。
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
import citation  # noqa: E402

FULL = {
    "authors": [{"family": "Zhang", "given": "Wei"}, {"family": "Li", "given": "Na"},
                {"family": "Wang", "given": "Qiang"}, {"family": "Zhao", "given": "Xin-Ming"}],
    "title": "A General Route to Ultrastable Perovskite Quantum Dots",
    "journal": "Journal of the American Chemical Society", "journal_abbr": "J. Am. Chem. Soc.",
    "year": "2023", "volume": "145", "issue": "12", "pages": "6789-6795",
    "doi": "10.1021/jacs.3c01234",
}
TWO = {"authors": [{"family": "Chen", "given": "Hao"}, {"family": "Liu", "given": "Yang"}],
       "title": "Interfacial Passivation in Perovskite Solar Cells", "journal": "Nature Materials",
       "journal_abbr": "Nat. Mater.", "year": "2022", "volume": "21", "pages": "1123-1131"}
ONE = {"authors": [{"family": "Nakamura", "given": "Satoshi"}], "title": "Single-Atom Catalysts",
       "journal": "Science", "year": "2021", "volume": "372", "issue": "6540", "pages": "eabf1234"}
SPARSE = {"authors": [{"family": "Guo", "given": "Lei"}], "title": "一篇只有题目和作者的会议论文",
          "year": "2020"}
NONE = {}
PRE = {"authors": [{"family": "Zhou", "given": "Shuxiang"}, {"family": "LaVerne", "given": "Jay A."},
                   {"family": "Hlushko", "given": "Hanna"}],
       "title": "Barrierless Water Dissociation on Rare-Earth Sesquioxide Surfaces from First Principles",
       "preprint": "arXiv:2609.06350v1", "year": "2026", "journal": "", "journal_abbr": ""}

for name, meta in (("FULL", FULL), ("TWO", TWO), ("ONE", ONE), ("SPARSE", SPARSE), ("NONE", NONE),
                   ("PREPRINT", PRE)):
    print("\n===== %s =====" % name)
    for g in citation.groups(meta):
        print("-- " + g["name"])
        for r in g["rows"]:
            print("   [%s] %r" % (r["label"], r["text"]))

print("\n===== 防编造守卫 =====")
SRC = "\n".join(["Journal of the American Chemical Society",
                 "A General Route to Ultrastable Perovskite Quantum Dots",
                 "Wei Zhang, Na Li, Qiang Wang, Xin-Ming Zhao",
                 "J. Am. Chem. Soc. 2023, 145, 6789\u22126795",
                 "https://doi.org/10.1021/jacs.3c01234", "Published 2023"])
BAD = dict(FULL)
BAD["volume"] = "146"                       # 源文里没有
BAD["pages"] = "6801-6810"                  # 源文里没有
BAD["authors"] = [{"family": "Zhang", "given": "Wei"}, {"family": "Fake", "given": "Author"}]
BAD["title"] = "A Wrong Title Nobody Printed"
BAD["doi"] = "10.1021/jacs.9c99999"
ok = citation.sanity(BAD, SRC, fallback_title="题目回退", fallback_author="Zhang")
print(json.dumps(ok, ensure_ascii=False, indent=1))
print("volume 应空:", ok["volume"] == "", "pages 应空:", ok["pages"] == "",
      "Fake 作者应被删:", [a["family"] for a in ok["authors"]] == ["Zhang"],
      "title 回退:", ok["title"] == "题目回退", "doi 回到真值:", ok["doi"] == "10.1021/jacs.3c01234")

print("\n===== 预印本：水印里捞编号 + 完整首页 =====")
SRC_PRE = "\n".join(["[首页原文]",
                     "Barrierless Water Dissociation on Rare-Earth Sesquioxide Surfaces from First",
                     "Principles",
                     "Shuxiang Zhou 1 , * Jay A. LaVerne 2 , and Hanna Hlushko 1",
                     "(Dated: September 9, 2026)",
                     "arXiv:2609.06350v1  [cond-mat.mtrl-sci]  6 Sep 2026"])
meta = citation.sanity({"authors": [{"family": "Zhou", "given": "Shuxiang"},
                                    {"family": "LaVerne", "given": "Jay A."},
                                    {"family": "Hlushko", "given": "Hanna"}],
                        "title": "Barrierless Water Dissociation on Rare-Earth Sesquioxide "
                                 "Surfaces from First Principles",
                        "year": "2026", "preprint": ""}, SRC_PRE)
print(json.dumps(meta, ensure_ascii=False))
for g in citation.groups(meta):
    for r in g["rows"]:
        print("   [%s] %r" % (r["label"], r["text"]))

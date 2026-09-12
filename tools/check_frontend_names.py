"""前端也要有"调用了但没定义/没导入"的闸门。

来历：`App.vue` 里 `await refreshMarginalia()` 写了**两处**，而 `refreshMarginalia`
从来没被 import——点「AI 眉批」时后端照常开始写，前端紧接着抛
`ReferenceError: refreshMarginalia is not defined`：界面既不知道任务在跑、也不会刷新，
只能靠离开再回来才看见批注。后端那侧 M4.6 加过同类检查（调用与定义对不上），
前端一直缺这一道，于是这个 bug 从写下那天活到现在。

做法：扒出每个 .js / .vue 的 script 块里**被当作函数调用**的裸标识符，减去
"本地声明的 + import 进来的 + 已知全局的"，剩下的就是可疑项。宁可少报也不要噪音，
所以白名单里放足了浏览器/Vue/项目约定的名字。
"""
import os
import re
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SRC = os.path.join(ROOT, "frontend", "src")

GLOBALS = {
    # JS / 浏览器
    "console", "window", "document", "localStorage", "sessionStorage", "fetch", "setTimeout",
    "clearTimeout", "setInterval", "clearInterval", "requestAnimationFrame", "cancelAnimationFrame",
    "JSON", "Object", "Array", "String", "Number", "Boolean", "Math", "Date", "RegExp", "Map", "Set",
    "Promise", "Error", "TypeError", "parseInt", "parseFloat", "isNaN", "isFinite", "encodeURIComponent",
    "decodeURIComponent", "structuredClone", "alert", "confirm", "prompt", "URL", "Blob", "FileReader",
    "FormData", "CustomEvent", "Event", "Intl", "Symbol", "WeakMap", "Proxy", "queueMicrotask",
    "getComputedStyle", "matchMedia", "Image", "Audio", "IntersectionObserver", "ResizeObserver",
    "MutationObserver", "AbortController", "TextEncoder", "TextDecoder", "requestIdleCallback",
    "open", "close", "scrollTo", "atob", "btoa", "DOMParser", "XMLHttpRequest", "EventSource", "Worker",
    # Vue 编译器宏与运行时
    "defineProps", "defineEmits", "defineExpose", "defineOptions", "defineSlots", "withDefaults",
    "ref", "reactive", "computed", "watch", "watchEffect", "onMounted", "onUnmounted", "onBeforeUnmount",
    "nextTick", "provide", "inject", "toRef", "toRefs", "unref", "markRaw", "shallowRef", "h", "useSlots",
    # 关键字/语法糖，正则扫到会误伤
    "async", "await", "if", "for", "while", "switch", "catch", "return", "typeof", "function",
    "new", "delete", "void", "in", "of", "do", "else", "case", "yield", "import", "from", "get", "set",
    # 项目里挂到 window 的 / 构建注入的
    "require", "module", "exports", "process",
}


def script_of(path: str) -> str:
    txt = open(path, encoding="utf-8").read()
    if path.endswith(".vue"):
        out = []
        for m in re.finditer(r"<script[^>]*>(.*?)</script>", txt, re.S):
            out.append(m.group(1))
        return "\n".join(out)
    return txt


def declared(code: str) -> set:
    """本地声明的名字：变量/函数/类/解构/参数（含 getter、箭头、方法简写）。"""
    names = set()
    def add_names(chunk):
        for piece in re.split(r"[,{}]", chunk or ""):
            piece = piece.split(":")[-1].strip().split("=")[0].strip()
            if re.fullmatch(r"[A-Za-z_$][\w$]*", piece or ""):
                names.add(piece)
    for pat in (r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)",
                r"\bfunction\s+([A-Za-z_$][\w$]*)",
                r"\bclass\s+([A-Za-z_$][\w$]*)",
                r"\b(?:const|let|var)\s*\{([^}]*)\}\s*=",
                r"\b(?:const|let|var)\s*\[([^\]]*)\]\s*=",
                r"\bfor\s*\(\s*(?:const|let|var)\s+([A-Za-z_$][\w$]*)",
                r"\bcatch\s*\(\s*([A-Za-z_$][\w$]*)"):
        for m in re.finditer(pat, code, re.M):
            add_names(m.group(1))
    # 函数/方法/箭头的参数表（含 getter/setter、async、解构、对象方法简写）
    # 同时把**方法名本身**收进来：对象字面量里的 `mounted(el) {…}`、`get narrow() {…}`
    # 都是属性键，不该被当成"没定义的裸调用"。
    for m in re.finditer(r"(?:^|[\s{(,;])(?:async\s+)?(?:get|set)?\s*([A-Za-z_$][\w$]*)\s*\(([^)]*)\)\s*\{",
                         code, re.M):
        names.add(m.group(1))
        add_names(m.group(2))
    for m in re.finditer(r"\(([^()]*)\)\s*=>", code):          # 带括号的箭头参数
        add_names(m.group(1))
    for m in re.finditer(r"(?<![\w.$])([A-Za-z_$][\w$]*)\s*=>", code):   # 单个裸参数 name =>
        names.add(m.group(1))
    for m in re.finditer(r"import\s+(?:([A-Za-z_$][\w$]*)\s*,?\s*)?(?:\{([^}]*)\})?\s*from", code):
        if m.group(1):
            names.add(m.group(1))
        if m.group(2):
            for part in m.group(2).split(","):
                part = part.strip().split(" as ")[-1].strip()
                if part:
                    names.add(part)
    return names


def suspects(path: str):
    code = script_of(path)
    local = declared(code)
    # 先剥掉注释、字符串、模板串，再剥正则字面量（不剥的话 `/(?<!X)/` 里的 `_(` 会误报）
    stripped = re.sub(r"//[^\n]*|/\*.*?\*/|'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|`(?:[^`\\]|\\.)*`",
                      " ", code, flags=re.S)
    stripped = re.sub(r"/(?:\\.|\[[^\]\n]*\]|[^/\n\\])+/[gimsuy]*", " ", stripped)
    out = []
    for m in re.finditer(r"(?<![\w.$])([A-Za-z_$][\w$]*)\s*\(", stripped):
        name = m.group(1)
        if name in local or name in GLOBALS:
            continue
        out.append(name)
    return sorted(set(out))


def main():
    bad = {}
    for base, _dirs, files in os.walk(SRC):
        if "node_modules" in base:
            continue
        for f in files:
            if not (f.endswith(".js") or f.endswith(".vue")):
                continue
            p = os.path.join(base, f)
            s = suspects(p)
            if s:
                bad[os.path.relpath(p, ROOT)] = s
    if not bad:
        print("前端名字检查：没有「调用了但没定义/没导入」的裸函数名 ✓")
        return 0
    print("前端可疑的裸调用（调用了，但本地与 import 里都没有这个顶层名字）：")
    for p, names in sorted(bad.items()):
        print(f"  {p}: {', '.join(names)}")
    print("\n（若其中某个是模板里注入的、或挂在 window 上的，把它加进本脚本的 GLOBALS）")
    return 1


if __name__ == "__main__":
    sys.exit(main())

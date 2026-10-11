"""文件与内存缓存的几个小工具：静默删除、流式 sha256、字典按插入序淘汰最旧。

这些原来在 main / translate_full / engine_install / update / compare 里各抄一份
（块大小、失败口径在抄写中已经漂开），收在这里一处说话。
"""
import hashlib
import os


def rm_quiet(path: str):
    """删一个文件：本来就不在、被占用（杀软/句柄）都当没发生——扫尾路径别因它失败。"""
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


def _chunks(f, size: int = 1 << 20):
    """按 1MB 一块读到尾：哈希与复制共用同一块大小、同一读序（内存只占一块缓冲）。"""
    return iter(lambda: f.read(size), b"")


def sha256_file(path: str, copy_to: str = None, quiet: bool = False) -> str:
    """流式算一份文件的 sha256。

    copy_to 给了就边算边复制一份：原来的"整读算哈希 + copyfile 整读整写"要把源
    读两遍，几百 MB 的 PDF 白等一遍。quiet=True 时读不了回 ""（判重/指纹回填那把
    钥匙的兜底口径：算不出来就当没有）；默认照抛 OSError——校验类调用方要能分辨
    "读不动"和"哈希对不上"两种失败。
    """
    h = hashlib.sha256()
    try:
        if copy_to:
            with open(path, "rb") as fsrc, open(copy_to, "wb") as fdst:
                for chunk in _chunks(fsrc):
                    h.update(chunk)
                    fdst.write(chunk)
        else:
            with open(path, "rb") as f:
                for chunk in _chunks(f):
                    h.update(chunk)
    except OSError:
        if not quiet:
            raise
        return ""
    return h.hexdigest()


def sha256_seeded(path: str):
    """下载断点续传的"先喂哈希"：新建 sha256 并把 path 现有内容流式喂进去。

    返回 (hasher, 已喂字节数)——hasher 接着吃后面新下的字节，拼完整才能校验得过
    （半截断点与源上的包对不上时，末尾的 sha256 会拦住）。
    """
    h = hashlib.sha256()
    n = 0
    with open(path, "rb") as f:
        for chunk in _chunks(f):
            h.update(chunk)
            n += len(chunk)
    return h, n


def cap_oldest(d: dict, keep: int):
    """字典按插入序淘汰最旧的一批，只留 keep 条（内存缓存封顶；dict 保序即淘汰序）。

    放在这里是因为 main 与 compare 都要用，而 compare 不能反向 import main。
    """
    while len(d) > keep:
        d.pop(next(iter(d)))

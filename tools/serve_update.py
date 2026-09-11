"""把 release/ 当更新源发布出去（本机或局域网），给已装好的用户推更新。

    python tools/serve_update.py                # 监听 0.0.0.0:8440，打印可用的地址
    python tools/serve_update.py --port 9000

用户端在「设置 → 更新源」里填这个地址（形如 http://192.168.1.5:8440），
之后每次打开软件都会问一次 latest.json，有新版就弹提示。

为什么用局域网直发而不是搞一套服务端：这个场景的"服务器"就是作者自己的电脑。
要做的是"我改完了，让装了的人能升上来"——一个静态目录足够，也方便换成
对象存储 / GitHub Releases（把 release/ 里的两个文件传上去，地址一填就完事）。
"""
import argparse
import http.server
import os
import socket

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
RELEASE = os.path.join(ROOT, "release")


def lan_ips():
    ips = []
    try:                                     # 拿本机在局域网里的地址（不会真的发包）
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    return ips


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=RELEASE, **kw)

    def end_headers(self):
        # 更新源必须"永远拿最新的"：中间任何一层缓存都会让用户看不到新版本
        self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

    def log_message(self, fmt, *a):
        print(f"  {self.address_string()}  {fmt % a}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8440)
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()
    if not os.path.isdir(RELEASE):
        raise SystemExit("还没有 release/ 目录，先跑 python tools/build_installer.py")
    files = sorted(os.listdir(RELEASE))
    print(f"更新源：{RELEASE}")
    print("目录里的文件：" + (", ".join(files) if files else "（空，先构建一次）"))
    if "latest.json" not in files:
        print("!! 没有 latest.json —— 客户端会当「没有更新」，先跑 tools/build_installer.py")
    print(f"\n监听 {args.host}:{args.port}，把下面之一填进客户端「设置 → 更新源」：")
    for ip in lan_ips():
        print(f"    http://{ip}:{args.port}")
    print(f"    http://127.0.0.1:{args.port}   （只给本机用）")
    http.server.ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()

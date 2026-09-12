"""把 release/ 当更新源发布出去，给**别的电脑上**已安装的 eggpaper 推更新。

    python tools/serve_update.py                 # 监听 0.0.0.0:8440，并打印可用的地址
    python tools/serve_update.py --port 9000
    python tools/serve_update.py --allow-firewall  # 顺手加一条入站放行规则（要管理员）

别的电脑要能收到更新，只差两件事：**这台机器在网络上能被访问到**、**端口没被防火墙挡住**。
这个脚本把这两件事都说清楚：启动后自己从局域网地址回连一次做可达性自检（能自己连通
说明至少本机的路由/绑定没问题），防火墙没放行时直接把命令打出来。

超过一个局域网时（对方在外地、或者你不想一直开着这台机器），把 release/ 里那两个文件
传到任意静态托管（对象存储、GitHub Releases、自己的服务器），地址填进客户端的
「设置 → 更新源」即可——客户端只认"一个能取到 latest.json 的地址"，不关心它在哪。

为什么用局域网直发：这个场景的"服务器"就是作者自己的电脑。要做的是"我改完了，
让装了的人能升上来"——一个静态目录足够。
"""
import argparse
import http.server
import os
import socket
import subprocess
import threading
import urllib.request

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


def self_check(port: int) -> str:
    """从局域网地址回连自己一次：能取到 latest.json 才说明这个源真的发出去了。"""
    for ip in lan_ips():
        try:
            with urllib.request.urlopen(f"http://{ip}:{port}/latest.json", timeout=2) as r:
                if r.status == 200:
                    return f"OK  http://{ip}:{port}/latest.json 可达（{len(r.read())} 字节）"
        except Exception as e:
            return f"!!  http://{ip}:{port}/latest.json 连不上（{type(e).__name__}）——多半是防火墙"
    return "??  没拿到局域网地址，只有本机能访问"


def open_firewall(port: int) -> None:
    cmd = (f'netsh advfirewall firewall add rule name="eggpaper 更新源" '
           f'dir=in action=allow protocol=TCP localport={port}')
    print("\n加防火墙放行规则（需要管理员权限）：")
    print("  " + cmd)
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        print("  →", (r.stdout or r.stderr or "").strip()[:120] or "（无输出）")
    except Exception as e:
        print("  → 没加成：", e)


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=RELEASE, **kw)

    def end_headers(self):
        # 更新源必须"永远拿最新的"：中间任何一层缓存都会让别的电脑看不到新版本
        self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

    def log_message(self, fmt, *a):
        print(f"  {self.address_string()}  {fmt % a}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8440)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--allow-firewall", action="store_true", help="顺手加一条入站放行规则（要管理员）")
    args = ap.parse_args()

    if not os.path.isdir(RELEASE):
        raise SystemExit("还没有 release/ 目录，先跑 python tools/build_installer.py")
    files = sorted(os.listdir(RELEASE))
    if "latest.json" not in files:
        raise SystemExit("release/ 里没有 latest.json —— 客户端会当成「没有更新」，"
                         "先跑 python tools/build_installer.py")
    print(f"更新源目录：{RELEASE}")
    print("文件：" + ", ".join(files))

    if args.allow_firewall:
        open_firewall(args.port)

    srv = http.server.ThreadingHTTPServer((args.host, args.port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"\n已监听 {args.host}:{args.port}")

    print("\n把下面之一填进**别的电脑**上 eggpaper 的「设置 → 更新源」：")
    for ip in lan_ips():
        print(f"    http://{ip}:{args.port}          ← 同一局域网里的电脑用这个")
    print(f"    http://127.0.0.1:{args.port}          ← 只给本机")

    print("\n可达性自检：", self_check(args.port))
    print("\n对方在外地、或你不想一直开着这台机器时：把 release/ 里的 latest.json 和安装包")
    print("传到任意静态托管（对象存储 / GitHub Releases / 自己的服务器），地址填进客户端即可。")
    print("\nCtrl+C 停止发布。")
    try:
        while True:
            threading.Event().wait(3600)
    except KeyboardInterrupt:
        print("\n已停止。")
        srv.shutdown()


if __name__ == "__main__":
    main()

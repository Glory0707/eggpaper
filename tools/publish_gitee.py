"""把当前版本发到 Gitee：推 feed（update/latest.json）→ 建 release → 传安装包附件 → 匿名闭环验证。

前置：先跑 `python tools/build_installer.py --notes "…"`——release/ 里要有
`eggpaper-<版本>-setup.exe` 与 `latest.json`（本脚本发布的就是这份清单）。

    python tools/publish_gitee.py --dry-run    # 只打印要做什么，不碰网络
    python tools/publish_gitee.py              # 真发

令牌：Windows 凭据管理器（`git credential fill`，进程内取用，不落盘、不回显、不进命令行）。
feed 用 **API 写文件**（有 sha 走 PUT 更新、没有走 POST 新建）——比"浅克隆 + 令牌推送"少一趟
仓库往返，也不在 .git/config 里留明文令牌。附件走 release 附件（raw 对 92MB 的大文件要登录，
附件直链匿名可下），latest.json 走仓库 raw，地址永不变。
"""
import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile

import httpx

OWNER, REPO = "zhouao1207", "eggpaper"
API = f"https://gitee.com/api/v5/repos/{OWNER}/{REPO}"
RAW = f"https://gitee.com/{OWNER}/{REPO}/raw/master"
ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
RELEASE = os.path.join(ROOT, "release")


def token() -> str:
    p = subprocess.run(["git", "credential", "fill"],
                       input="protocol=https\nhost=gitee.com\n\n",
                       capture_output=True, text=True, cwd=ROOT)
    for line in (p.stdout or "").splitlines():
        if line.startswith("password="):
            tk = line.split("=", 1)[1].strip()
            if tk:
                return tk
    sys.exit("拿不到 gitee 令牌：git credential fill 没回 password（凭据管理器里没存？）")


def load_local() -> tuple:
    with open(os.path.join(ROOT, "VERSION"), encoding="utf-8") as f:
        ver = f.read().strip()
    feed_path = os.path.join(RELEASE, "latest.json")
    exe = os.path.join(RELEASE, f"eggpaper-{ver}-setup.exe")
    if not (os.path.exists(feed_path) and os.path.exists(exe)):
        sys.exit(f"先构建：release/ 里缺 latest.json 或 {os.path.basename(exe)}")
    with open(feed_path, encoding="utf-8") as f:
        feed = json.load(f)
    if feed.get("version") != ver:
        sys.exit(f"清单版本 {feed.get('version')} 与 VERSION {ver} 不一致，先重新构建")
    h = hashlib.sha256()
    with open(exe, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    if h.hexdigest() != feed.get("sha256"):
        sys.exit("安装包的 sha256 与清单对不上——构建产物被换过？")
    print(f"本地：v{ver}  {os.path.basename(exe)}  {os.path.getsize(exe)/1048576:.1f}MB")
    print(f"      说明：{feed.get('notes')}")
    return ver, feed, exe


def _ok(r, what: str):
    if r.status_code >= 300 or not (r.text or "").strip():
        sys.exit(f"{what} 失败：HTTP {r.status_code} {r.text[:300]}")
    return r


def push_feed(cli: httpx.Client, tk: str, feed: dict, dry: bool) -> None:
    """update/latest.json：有 sha 是更新、没有是新建。"""
    path = "update/latest.json"
    body_text = json.dumps(feed, ensure_ascii=False, indent=2) + "\n"
    url = f"{API}/contents/{path}"
    r = cli.get(url, params={"access_token": tk}, timeout=30)
    sha = r.json().get("sha") if r.status_code == 200 else None
    print(f"feed：{'更新' if sha else '新建'} update/latest.json（v{feed['version']}）")
    if dry:
        return
    payload = {"access_token": tk, "content": base64.b64encode(body_text.encode()).decode(),
               "message": f"update: 更新清单 → {feed['version']}", "branch": "master"}
    if sha:
        payload["sha"] = sha
        r = cli.put(url, json=payload, timeout=60)
    else:
        r = cli.post(url, json=payload, timeout=60)
    _ok(r, "写 feed")


def create_release(cli: httpx.Client, tk: str, ver: str, notes: str, dry: bool) -> str:
    tag = f"v{ver}"
    r = cli.get(f"{API}/releases/tags/{tag}", params={"access_token": tk}, timeout=30)
    if r.status_code == 200:
        rid = str(r.json().get("id"))
        print(f"release：{tag} 已存在（id={rid}），补说明 + 复用")
        if not dry:
            _ok(cli.patch(f"{API}/releases/{rid}", json={
                "access_token": tk, "tag_name": tag, "name": f"eggpaper {ver}",
                "target_commitish": "master", "body": notes}, timeout=60), "改 release 说明")
        return rid
    print(f"release：新建 {tag}")
    if dry:
        return "0"
    r = _ok(cli.post(f"{API}/releases", json={
        "access_token": tk, "tag_name": tag, "target_commitish": "master",
        "name": f"eggpaper {ver}", "body": notes, "prerelease": False}, timeout=60),
        "建 release")
    return str(r.json().get("id"))


def attach(cli: httpx.Client, tk: str, rid: str, exe: str, dry: bool) -> None:
    name = os.path.basename(exe)
    r = cli.get(f"{API}/releases/{rid}", params={"access_token": tk}, timeout=30)
    have = [a.get("name") for a in ((r.json().get("assets") or []) if r.status_code == 200 else [])]
    if name in have:
        print(f"附件：{name} 已在，跳过上传")
        return
    print(f"附件：上传 {name}（{os.path.getsize(exe)/1048576:.1f}MB，按上次实测约两分钟）")
    if dry:
        return
    with open(exe, "rb") as f:
        files = {"file": (name, f.read(), "application/octet-stream")}
    _ok(cli.post(f"{API}/releases/{rid}/attach_files",
                 params={"access_token": tk}, files=files, timeout=900), "传附件")


def verify(feed: dict, dry: bool) -> None:
    if dry:
        return
    with httpx.Client(follow_redirects=True, timeout=60) as c:
        r = _ok(c.get(f"{RAW}/update/latest.json"), "读 feed")
        got = r.json()
        same = (got.get("version") == feed["version"] and got.get("sha256") == feed["sha256"]
                and got.get("url") == feed["url"])
        print(f"验证 feed：v{got.get('version')} sha 一致={same}")
        if not same:
            sys.exit("feed 内容与本地不一致（Gitee 缓存？稍等重试）")
        h = hashlib.sha256()
        n = 0
        with c.stream("GET", feed["url"]) as s:
            if s.status_code >= 300:          # 流式响应读不了 .text，只认状态码
                sys.exit(f"匿名下附件 失败：HTTP {s.status_code}")
            for chunk in s.iter_bytes(1 << 20):
                h.update(chunk)
                n += len(chunk)
        print(f"验证附件：匿名下载 {n/1048576:.1f}MB  sha256 {'一致' if h.hexdigest() == feed['sha256'] else '不一致！'}")
        if h.hexdigest() != feed["sha256"]:
            sys.exit("附件 sha256 不匹配")
    print("\n好了：feed 与 release 都就位，客户端匿名可下。")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    ver, feed, exe = load_local()
    tk = token()
    # **不许跟随重定向**：POST /releases 被 301/302 顶到时，httpx 会把 POST 降级成 GET，
    # 拿回一个空体——第一次实测就栽在这儿（响应体是 null，看着像"建成功了"其实没建）。
    with httpx.Client(follow_redirects=False) as cli:
        push_feed(cli, tk, feed, args.dry_run)
        rid = create_release(cli, tk, ver, feed.get("notes") or "", args.dry_run)
        attach(cli, tk, rid, exe, args.dry_run)
    verify(feed, args.dry_run)


if __name__ == "__main__":
    main()

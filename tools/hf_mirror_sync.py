#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
村界数据 → HuggingFace 镜像（用官方 login() + upload_folder()）。

🔴 铁律：本脚本在【有数据的机器 / HF Space】上跑，直连 HF，
   文件本体绝不经过 tianditu.guaigansheng.com 服务器。

两种用法：

  ① 本地已经从 GitHub 下载好数据（最简单，就是官方那套）：
       cd <村界数据目录>
       python hf_mirror_sync.py
     等价于：
       from huggingface_hub import login, upload_folder
       login()
       upload_folder(folder_path=".", repo_id="laoco/china-village-boundaries", repo_type="dataset")

  ② 自动从 GitHub latest release 拉到临时目录再整包上传：
       python hf_mirror_sync.py --from-github

token：交互登录用 login()；自动化（HF Space/cron）用环境变量 HF_TOKEN：
       export HF_TOKEN=hf_xxx   脚本会走 login(token=...) 非交互。
"""

import argparse
import os
import sys

from huggingface_hub import login, upload_folder

HF_REPO = "laoco/china-village-boundaries"
GITHUB_REPO = "thedavidweng/china-village-boundaries"
GH_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"


def do_login():
    """有 HF_TOKEN 走非交互，没有则交互式 login()（同官方用法）。"""
    token = os.environ.get("HF_TOKEN", "").strip()
    if token:
        login(token=token)
    else:
        login()  # 交互式：浏览器/粘贴 token


def latest_assets(tag=None):
    """拉 GitHub release 的资产下载地址（不含大文件本体）。tag=None=latest。"""
    import requests
    url = GH_API if not tag else f"https://api.github.com/repos/{GITHUB_REPO}/releases/tags/{tag}"
    r = requests.get(url, timeout=30, headers={"User-Agent": "hf-mirror-sync/1.0"})
    r.raise_for_status()
    rel = r.json()
    return rel["tag_name"], [a for a in rel["assets"]
                             if a["name"].endswith(".zip")
                             or a["name"] in ("manifest.json", "SHA256SUMS.txt")]


def download(url, dest, expect_size=None):
    """断点续传下载：已下载部分用 HTTP Range 续，支持中断后重跑不重来。

    - dest.part 存未完成的分片；完整后才原子改名为 dest
    - 服务端不支持 Range（返回 200 而非 206）时从头下
    - expect_size 提供时校验总长度
    """
    import requests
    part = dest + ".part"
    existing = os.path.getsize(part) if os.path.exists(part) else 0
    headers = {"User-Agent": "hf-mirror-sync/1.0"}
    if existing > 0:
        headers["Range"] = f"bytes={existing}-"
        print(f"    断点续传：已有 {existing // (1 << 20)} MB，从此处继续")

    r = requests.get(url, stream=True, timeout=(30, 300), headers=headers)
    if existing > 0 and r.status_code == 200:
        # 服务端忽略 Range、从头返回：丢弃旧分片
        print("    服务端不支持 Range，从头下载")
        existing = 0
    elif r.status_code == 206:
        pass  # 正常续传
    else:
        r.raise_for_status()
    # 期望总长（Content-Range: bytes start-end/total）
    total = expect_size
    cr = r.headers.get("Content-Range")
    if cr and "/" in cr:
        try:
            total = int(cr.split("/")[-1])
        except ValueError:
            pass
    elif r.headers.get("Content-Length") and existing == 0:
        try:
            total = int(r.headers["Content-Length"])
        except ValueError:
            pass

    mode = "ab" if existing > 0 else "wb"
    got = existing
    with open(part, mode) as f:
        for chunk in r.iter_content(chunk_size=1 << 20):
            if chunk:
                f.write(chunk)
                got += len(chunk)
    r.close()
    if total and got != total:
        raise IOError(f"下载不完整：{got}/{total}，.part 已保留，重跑续传")
    if expect_size and got != expect_size:
        raise IOError(f"大小不符：期望 {expect_size}，实际 {got}")
    os.replace(part, dest)  # 原子完成
    return got


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-github", action="store_true",
                    help="自动从 GitHub latest release 下载再上传；默认传当前目录")
    ap.add_argument("--download-only", action="store_true",
                    help="只下载到缓存目录，不上传（GitHub Actions 先下载再分别推 GitHub/HF）")
    ap.add_argument("--tag", default=None, help="指定 release tag（默认 latest）")
    ap.add_argument("--folder", default=".", help="要上传的本地目录（默认当前目录）")
    ap.add_argument("--cache-dir", default="./downloads",
                    help="下载缓存目录（断点续传的 .part 存这里，默认 ./downloads）")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不上传")
    args = ap.parse_args()

    if args.download_only:
        # 只下载，不需要 HF 登录
        tag, assets = latest_assets(args.tag)
        cache = os.path.abspath(args.cache_dir)
        os.makedirs(cache, exist_ok=True)
        print(f"GitHub 版本 {tag}，{len(assets)} 个文件 → 缓存目录 {cache}（支持断点续传）")
        for i, a in enumerate(assets, 1):
            dest = os.path.join(cache, a["name"])
            if os.path.exists(dest) and a.get("size") and os.path.getsize(dest) == a["size"]:
                print(f"  [{i}/{len(assets)}] 跳过（已完整） {a['name']}")
                continue
            print(f"  [{i}/{len(assets)}] 下载 {a['name']} ({a['size'] // (1 << 20)} MB)")
            download(a["browser_download_url"], dest, a.get("size"))
        print("下载完成")
        return

    do_login()

    if args.from_github:
        tag, assets = latest_assets(args.tag)
        cache = os.path.abspath(args.cache_dir)
        os.makedirs(cache, exist_ok=True)
        print(f"GitHub 版本 {tag}，{len(assets)} 个文件 → 缓存目录 {cache}（支持断点续传）")
        for i, a in enumerate(assets, 1):
            dest = os.path.join(cache, a["name"])
            if os.path.exists(dest) and a.get("size") and os.path.getsize(dest) == a["size"]:
                print(f"  [{i}/{len(assets)}] 跳过（已完整） {a['name']}")
                continue
            print(f"  [{i}/{len(assets)}] 下载 {a['name']} ({a['size'] // (1 << 20)} MB)")
            download(a["browser_download_url"], dest, a.get("size"))
        if args.dry_run:
            print("dry-run：跳过上传")
            return
        print(f"整包上传到 {HF_REPO} …")
        upload_folder(folder_path=cache, repo_id=HF_REPO, repo_type="dataset",
                      commit_message=f"mirror GitHub {tag}")
    else:
        folder = os.path.abspath(args.folder)
        if not os.path.isdir(folder):
            print(f"目录不存在：{folder}", file=sys.stderr)
            sys.exit(1)
        files = [f for f in os.listdir(folder) if os.path.isfile(os.path.join(folder, f))]
        print(f"上传目录 {folder}（{len(files)} 个文件）到 {HF_REPO}")
        if args.dry_run:
            print("dry-run：跳过上传")
            return
        upload_folder(folder_path=folder, repo_id=HF_REPO, repo_type="dataset",
                      commit_message="upload village boundaries")

    print("完成")


if __name__ == "__main__":
    main()

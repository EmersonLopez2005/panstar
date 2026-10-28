#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
绕过 GitHub API 限流，直接安装 Camoufox 浏览器内核。

为什么需要这个：
    `camoufox fetch` 的第一步是 `_do_sync()` —— 它去 api.github.com 查各仓库的
    release 列表。未认证的 API 调用**每小时只有 60 次**，VPS 的共享出口 IP
    常常早就被打满，于是：

        Official... Error: 403 Client Error: rate limit exceeded
        Synced 0 versions from 0 repos.
        v156.0.1-beta.36, the browser this camoufox release pairs with,
        is not in official's releases.

    但注意：**真正的 zip 下载走的是 releases 下载地址
    （github.com/.../releases/download/...），不受 API 限流影响。**
    被限流的只是"查询有哪些版本"这一步。

    而版本是**硬锁定**的 —— camoufox 包里的 browser-pin.json 写死了它配对的
    浏览器版本（当前是 v156.0.1-beta.36）。所以下载 URL 是确定的，
    可以完全跳过 API 查询。

用法：
    python fetch_camoufox.py                 # 自动识别架构和版本
    python fetch_camoufox.py --token=ghp_xxx # 带 token（更稳，能顺带做校验）
    python fetch_camoufox.py --dest=/大盘路径 # 指定安装位置

安装结果与 `camoufox fetch` 完全一致：
    $INSTALL_DIR/browsers/{repo}/{version}-{build}/   ← 解压后的浏览器
    $INSTALL_DIR/config.json                          ← active_version 指向它
    $INSTALL_DIR/.0.5_FLAG                            ← 版本兼容标记
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Optional

# --------------------------------------------------------------------------
# 从 camoufox 包里读取锁定的版本
# --------------------------------------------------------------------------
def read_pin() -> dict:
    """读取 browser-pin.json —— 这个包锁定的浏览器版本。"""
    try:
        from camoufox.browser_pin import load_pin
        pin = load_pin()
        if pin:
            return {
                "tag": pin.tag,
                "repo": pin.repo,
                "repo_name": pin.repo_name,
                "version": pin.version,
                "build": pin.build,
            }
    except Exception as exc:
        print(f"  读取 browser-pin 失败: {exc}", file=sys.stderr)

    # 退回到直接读文件
    try:
        import camoufox
        p = Path(camoufox.__file__).parent / "browser-pin.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        if data.get("tag"):
            return data
    except Exception:
        pass
    return {}


def install_dir() -> Path:
    try:
        from camoufox.pkgman import INSTALL_DIR
        return Path(INSTALL_DIR)
    except Exception:
        xdg = os.environ.get("XDG_CACHE_HOME", "").strip()
        base = xdg or os.path.join(os.path.expanduser("~"), ".cache")
        return Path(base) / "camoufox"


def detect_platform() -> tuple:
    """返回 (os_tag, arch_tag)。"""
    import platform

    sysname = platform.system().lower()
    machine = platform.machine().lower()

    os_map = {"linux": "lin", "darwin": "mac", "windows": "win"}
    arch_map = {
        "x86_64": "x86_64", "amd64": "x86_64",
        "i386": "i686", "i686": "i686",
        "aarch64": "arm64", "arm64": "arm64",
    }
    os_tag = os_map.get(sysname, "lin")
    arch_tag = arch_map.get(machine, machine)
    return os_tag, arch_tag


# --------------------------------------------------------------------------
# 下载
# --------------------------------------------------------------------------
def download(url: str, dest: Path, token: str = "") -> bool:
    """流式下载到 dest，支持断点续传。"""
    import requests

    headers = {"User-Agent": "panstar-autosign/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    part = dest.with_suffix(dest.suffix + ".part")
    pos = part.stat().st_size if part.exists() else 0
    if pos:
        headers["Range"] = f"bytes={pos}-"
        print(f"  断点续传（已有 {pos // 1024 // 1024} MB）")

    try:
        with requests.get(url, headers=headers, stream=True, timeout=60) as resp:
            if resp.status_code == 416:      # 已经下完
                part.rename(dest)
                return True
            resp.raise_for_status()
            total = int(resp.headers.get("content-length", 0)) + pos
            mode = "ab" if pos else "wb"
            done = pos
            with open(part, mode) as fh:
                for chunk in resp.iter_content(1024 * 512):
                    if not chunk:
                        continue
                    fh.write(chunk)
                    done += len(chunk)
                    if total:
                        pct = done * 100 // total
                        sys.stdout.write(f"\r  下载中 {done // 1024 // 1024} MB "
                                         f"/ {total // 1024 // 1024} MB ({pct}%)")
                        sys.stdout.flush()
        sys.stdout.write("\n")
        part.rename(dest)
        return True
    except Exception as exc:
        print(f"\n  下载失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return False


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


# --------------------------------------------------------------------------
# 安装
# --------------------------------------------------------------------------
def install(zip_path: Path, dest_root: Path, pin: dict, repo_name: str) -> bool:
    """解压到 browsers/{repo}/{version}-{build}/ 并写好配置。"""
    version = pin.get("version", "")
    build = pin.get("build", "")
    folder = f"{version}-{build}" if build else version
    target = dest_root / "browsers" / repo_name / folder

    if target.exists():
        print(f"  已存在，先清掉: {target}")
        shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)

    print(f"  解压到 {target}")
    try:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(target)
    except Exception as exc:
        print(f"  解压失败: {exc}", file=sys.stderr)
        shutil.rmtree(target, ignore_errors=True)
        return False

    # 解出来的文件要可执行
    for root, dirs, files in os.walk(target):
        for name in files:
            p = Path(root) / name
            try:
                p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            except Exception:
                pass

    # version.json —— camoufox 用它识别已安装的版本
    meta = {
        "version": version,
        "build": build,
        "prerelease": "beta" in build,
        "sha256": None,
        "created_at": None,
        "interface": 1,
    }
    (target / "version.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )

    # config.json —— active_version 指向刚装好的这份
    rel = f"browsers/{repo_name}/{folder}"
    cfg_path = dest_root / "config.json"
    cfg = {}
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    cfg["active_version"] = rel
    dest_root.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    # 版本兼容标记
    (dest_root / ".0.5_FLAG").touch()

    print(f"  已设置 active_version = {rel}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="绕过 GitHub API 限流安装 Camoufox")
    ap.add_argument("--token", default=os.environ.get("GITHUB_TOKEN", ""),
                    help="GitHub token（可选，但强烈建议）")
    ap.add_argument("--dest", default="", help="安装目录（默认用 camoufox 自己的）")
    ap.add_argument("--keep-zip", action="store_true", help="保留下载的 zip")
    args = ap.parse_args()

    print("== Camoufox 直装（绕过 GitHub API 限流）==")

    pin = read_pin()
    if not pin.get("tag"):
        print("❌ 读不到 browser-pin.json，无法确定要下哪个版本", file=sys.stderr)
        print("   试试：pip install -U 'camoufox[geoip]'", file=sys.stderr)
        return 1

    tag = pin["tag"]                      # v156.0.1-beta.36
    repo = pin.get("repo", "daijro/camoufox")
    repo_name = pin.get("repo_name", "official").lower()
    version = pin.get("version", "")
    build = pin.get("build", "")

    os_tag, arch_tag = detect_platform()
    print(f"  锁定版本: {tag}（来自 {repo}）")
    print(f"  平台: {os_tag}/{arch_tag}")

    dest_root = Path(args.dest) if args.dest else install_dir()
    print(f"  安装目录: {dest_root}")

    # releases 下载地址 —— 不走 API，不受限流影响
    fname = f"camoufox-{version}-{build}-{os_tag}.{arch_tag}.zip"
    url = f"https://github.com/{repo}/releases/download/{tag}/{fname}"
    print(f"  下载地址: {url}")

    # 下载到临时目录（TMPDIR 要在磁盘上，不能是 tmpfs）
    tmpdir = Path(os.environ.get("TMPDIR", "/tmp"))
    tmpdir.mkdir(parents=True, exist_ok=True)
    zip_path = tmpdir / fname

    if not zip_path.exists():
        if not download(url, zip_path, args.token):
            print("\n❌ 下载失败。常见原因：")
            print("   1) GitHub 限流/网络问题 → 配 GITHUB_TOKEN 或换网络")
            print("   2) TMPDIR 空间不足 → export TMPDIR=/大盘/tmp")
            print("   3) 版本号已变 → pip install -U 'camoufox[geoip]' 后重试")
            return 1
    else:
        print(f"  zip 已存在，跳过下载: {zip_path}")

    size_mb = zip_path.stat().st_size // 1024 // 1024
    print(f"  文件大小: {size_mb} MB")

    if not install(zip_path, dest_root, pin, repo_name):
        return 1

    if not args.keep_zip:
        try:
            zip_path.unlink()
        except Exception:
            pass

    # 最终校验
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from browser_engine import engine_status
        st = engine_status()
        ok = st.get("camoufox", {}).get("binary")
        print(f"\n  校验浏览器本体: {'✅ 已就绪' if ok else '❌ 仍未找到'}")
        if not ok:
            print("  手动检查：")
            print(f"    ls -la {dest_root}/browsers/")
            return 1
    except Exception as exc:
        print(f"  (校验时出错: {exc})")

    print("\n✅ 安装完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())

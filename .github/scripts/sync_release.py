#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, json, tempfile, shutil, pathlib, subprocess, datetime, re
import requests

GITHUB_API = "https://api.github.com"

def gh_headers():
    token = os.environ["GH_TOKEN"]
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}

def get_latest_release(repo):
    r = requests.get(f"{GITHUB_API}/repos/{repo}/releases/latest", headers=gh_headers())
    if r.status_code == 404:
        r2 = requests.get(f"{GITHUB_API}/repos/{repo}/releases?per_page=1", headers=gh_headers())
        r2.raise_for_status()
        items = r2.json()
        if not items:
            return None
        return items[0]
    r.raise_for_status()
    return r.json()

def release_exists(repo, tag):
    r = requests.get(f"{GITHUB_API}/repos/{repo}/releases/tags/{tag}", headers=gh_headers())
    return r.status_code == 200

def create_release(repo, tag, name, body, draft=False, prerelease=False, target_commitish=None):
    payload = {"tag_name": tag, "name": name or tag, "body": body or "", "draft": draft, "prerelease": prerelease}
    if target_commitish:
        payload["target_commitish"] = target_commitish
    r = requests.post(f"{GITHUB_API}/repos/{repo}/releases", headers=gh_headers(), json=payload)
    r.raise_for_status()
    return r.json()

def upload_asset(upload_url_template, filepath):
    upload_url = upload_url_template.split("{")[0]
    name = pathlib.Path(filepath).name
    with open(filepath, "rb") as f:
        r = requests.post(
            f"{upload_url}?name={requests.utils.quote(name)}",
            headers={**gh_headers(), "Content-Type": "application/octet-stream"},
            data=f,
        )
    r.raise_for_status()
    return r.json()

def download_upstream_assets(assets, dest_dir):
    paths = []
    for a in assets or []:
        url = a.get("browser_download_url")
        if not url:
            # 兜底用 assets API，但要 Accept: application/octet-stream
            url = a.get("url")
            if not url:
                continue
            resp = requests.get(url, headers={**gh_headers(), "Accept": "application/octet-stream"}, stream=True)
        else:
            resp = requests.get(url, headers=gh_headers(), stream=True)
        resp.raise_for_status()
        fn = a.get("name") or pathlib.Path(url).name
        out = pathlib.Path(dest_dir) / fn
        with open(out, "wb") as f:
            shutil.copyfileobj(resp.raw, f)
        paths.append(str(out))
    return paths

# -------- tracker I/O --------
def read_tracker_state(dir_path):
    p = pathlib.Path(dir_path) / "state.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8") or "{}")
        except Exception:
            return {}
    return {}

def write_tracker_state(dir_path, data):
    p = pathlib.Path(dir_path) / "state.json"
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    subprocess.run(["git", "add", "state.json"], cwd=dir_path, check=True)
    actor = os.environ.get("GITHUB_ACTOR", "github-actions[bot]")
    email = os.environ.get("GIT_EMAIL", "github-actions[bot]@users.noreply.github.com")
    subprocess.run(
        ["git", "-c", f"user.name={actor}", "-c", f"user.email={email}",
         "commit", "-m", f"chore(tracker): {data.get('last_tag', '')}"],
        cwd=dir_path, check=True
    )
    subprocess.run(["git", "push"], cwd=dir_path, check=True)

# -------- Baidu Netdisk (BaiduPCS-Go) --------
def extract_bduss(cookie: str) -> str:
    if not cookie:
        return ""
    m = re.search(r"BDUSS=([^;]+)", cookie)
    return m.group(1) if m else ""

def upload_to_baidunetdisk(local_dir, netdisk_prefix, cookie):
    import pathlib, re, subprocess

    def extract_bduss(cookie: str) -> str:
        m = re.search(r"BDUSS=([^;]+)", cookie or "")
        return m.group(1) if m else ""

    bduss = extract_bduss(cookie)
    if not bduss:
        print("未找到 BDUSS（请在 BAIDU_COOKIE 中包含 BDUSS=...;），跳过百度网盘上传。")
        return False

    # 远端目录例如：/lcdyk有的掌机/release-portmaster/2025.07.14-1510
    netdisk_prefix = netdisk_prefix if netdisk_prefix.startswith("/") else "/" + netdisk_prefix

    # 登录
    subprocess.run(["BaiduPCS-Go", "logout"], check=False)
    subprocess.run(["BaiduPCS-Go", "login", f"-bduss={bduss}"], check=True)

    # 确保远端目录存在
    subprocess.run(["BaiduPCS-Go", "mkdir", netdisk_prefix], check=False)

    # 只上传“目录里的文件”，不连同最外层目录名
    local_path = pathlib.Path(local_dir)
    uploaded_any = False
    for p in local_path.iterdir():
        # 根目录我们只放文件；若你以后放子目录，也按同名子目录创建后再传
        if p.is_file():
            cmd = ["BaiduPCS-Go", "upload", str(p), netdisk_prefix, "-retry", "3"]
            print("上传文件：", " ".join(cmd))
            subprocess.run(cmd, check=True)
            uploaded_any = True
        elif p.is_dir():
            # 若出现子目录，则把它的内容传到 远端/<子目录名>/ 下
            remote_sub = netdisk_prefix.rstrip("/") + "/" + p.name
            subprocess.run(["BaiduPCS-Go", "mkdir", remote_sub], check=False)
            cmd = ["BaiduPCS-Go", "upload", str(p), remote_sub, "-retry", "3"]
            print("上传子目录：", " ".join(cmd))
            subprocess.run(cmd, check=True)
            uploaded_any = True

    if not uploaded_any:
        print("本地待上传目录为空。")
    return uploaded_any

def main():
    upstream = os.environ["UPSTREAM_REPO"]
    current_repo = os.environ.get("GITHUB_REPOSITORY")
    tracker_dir = os.environ.get("TRACKER_DIR") or ".release-tracker"
    netdisk_prefix = os.environ.get("NETDISK_PREFIX") or f"/apps/release-sync/{current_repo}"
    baidu_cookie = os.environ.get("BAIDU_COOKIE") or ""

    rel = get_latest_release(upstream)
    if not rel:
        print("上游没有 release，退出。")
        return

    tag = rel["tag_name"]
    name = rel.get("name") or tag
    body = rel.get("body") or ""
    draft = rel.get("draft", False)
    prerelease = rel.get("prerelease", False)

    print(f"上游最新 release: {tag} (draft={draft}, prerelease={prerelease})")

    # 读取 tracker
    state = read_tracker_state(tracker_dir)
    last_tag = state.get("last_tag")
    if last_tag == tag:
        print(f"tracker 记录为 {last_tag}，与上游一致，跳过。")
        return

    tmp = tempfile.mkdtemp(prefix="release_assets_")
    try:
        asset_files = download_upstream_assets(rel.get("assets", []), tmp)
        print(f"下载资产 {len(asset_files)} 个：", asset_files)

        # 创建/补充当前仓库 release（若已存在则跳过创建）
        if not release_exists(current_repo, tag):
            created = create_release(current_repo, tag, name, body, draft, prerelease)
            print("已创建当前仓库 release:", created.get("html_url"))
            for fpath in asset_files:
                print("上传资产到当前仓库:", fpath)
                upload_asset(created["upload_url"], fpath)
        else:
            print(f"当前仓库已有 tag={tag} 的 release，跳过创建。")

        # 上传到百度网盘：会在 NETDISK_PREFIX 下再建 /<tag>
        remote_base = f"{netdisk_prefix.rstrip('/')}/{tag}"
        ok = upload_to_baidunetdisk(tmp, remote_base, baidu_cookie)
        if ok:
            print("百度网盘上传完成：", remote_base)

        # 更新 tracker
        new_state = {
            "last_tag": tag,
            "upstream_repo": upstream,
            "checked_at": datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat(),
        }
        write_tracker_state(tracker_dir, new_state)
        print("tracker 已更新：", new_state)

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

if __name__ == "__main__":
    main()

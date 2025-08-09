#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, json, tempfile, shutil, pathlib, subprocess, datetime, re
import requests

GITHUB_API = "https://api.github.com"

# ---------- GitHub helpers ----------
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
        url = a.get("browser_download_url") or a.get("url")
        if not url:
            continue
        if "api.github.com/repos" in url and "/assets/" in url:
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

# ---------- Tracker (one branch, many repos) ----------
def read_tracker_state(dir_path):
    p = pathlib.Path(dir_path) / "state.json"
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8") or "{}")
            if "repos" not in data or not isinstance(data["repos"], dict):
                data = {"repos": {}}
            return data
        except Exception:
            return {"repos": {}}
    return {"repos": {}}

def write_tracker_state(dir_path, state, repo, tag):
    p = pathlib.Path(dir_path) / "state.json"
    p.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    # commit message包含 repo 与 tag，避免冲突且可读
    actor = os.environ.get("GITHUB_ACTOR", "github-actions[bot]")
    email = os.environ.get("GIT_EMAIL", "github-actions[bot]@users.noreply.github.com")
    subprocess.run(["git", "add", "state.json"], cwd=dir_path, check=True)
    subprocess.run(
        ["git", "-c", f"user.name={actor}", "-c", f"user.email={email}",
         "commit", "-m", f"chore(tracker): {repo} -> {tag}"],
        cwd=dir_path, check=True
    )
    subprocess.run(["git", "push"], cwd=dir_path, check=True)

# ---------- Baidu Netdisk ----------
def extract_bduss(cookie: str) -> str:
    m = re.search(r"BDUSS=([^;]+)", cookie or "")
    return m.group(1) if m else ""

def upload_to_baidunetdisk(local_dir, remote_dir, cookie):
    bduss = extract_bduss(cookie)
    if not bduss:
        print("未找到 BDUSS（请在 BAIDU_COOKIE 中包含 BDUSS=...;），跳过百度网盘上传。")
        return False

    remote_dir = remote_dir if remote_dir.startswith("/") else "/" + remote_dir
    subprocess.run(["BaiduPCS-Go", "logout"], check=False)
    subprocess.run(["BaiduPCS-Go", "login", f"-bduss={bduss}"], check=True)
    subprocess.run(["BaiduPCS-Go", "mkdir", remote_dir], check=False)

    local_path = pathlib.Path(local_dir)
    uploaded_any = False
    for p in local_path.iterdir():
        if p.is_file():
            cmd = ["BaiduPCS-Go", "upload", str(p), remote_dir, "-retry", "3"]
            print("上传文件：", " ".join(cmd))
            subprocess.run(cmd, check=True)
            uploaded_any = True
        elif p.is_dir():
            remote_sub = remote_dir.rstrip("/") + "/" + p.name
            subprocess.run(["BaiduPCS-Go", "mkdir", remote_sub], check=False)
            cmd = ["BaiduPCS-Go", "upload", str(p), remote_sub, "-retry", "3"]
            print("上传子目录：", " ".join(cmd))
            subprocess.run(cmd, check=True)
            uploaded_any = True
    if not uploaded_any:
        print("本地待上传目录为空。")
    return uploaded_any

# ---------- Utils ----------
def parse_upstream_repos(s: str):
    s = (s or "").strip()
    if not s:
        return []
    if s.startswith("["):  # JSON array
        arr = json.loads(s)
        return [x.strip() for x in arr if x and str(x).strip()]
    return [ln.strip() for ln in s.splitlines() if ln.strip() and not ln.strip().startswith("#")]

def repo_name_only(full: str) -> str:
    return full.split("/", 1)[-1]

def load_repo_aliases():
    raw = os.environ.get("REPO_ALIASES", "").strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}

def folder_for_repo(full: str, aliases: dict) -> str:
    return aliases.get(full) or repo_name_only(full)

def now_utc_iso():
    return datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat()

# ---------- Main per-repo ----------
def process_one(upstream, tracker_dir, netdisk_prefix, append_tag, aliases, namespace_release_tags):
    current_repo = os.environ.get("GITHUB_REPOSITORY")

    # 读取 tracker
    state = read_tracker_state(tracker_dir)
    last_tag = (state["repos"].get(upstream) or {}).get("last_tag")

    rel = get_latest_release(upstream)
    if not rel:
        print(f"[{upstream}] 上游没有 release，跳过。")
        return

    upstream_tag = rel["tag_name"]
    name = rel.get("name") or upstream_tag
    body = rel.get("body") or ""
    draft = rel.get("draft", False)
    prerelease = rel.get("prerelease", False)

    print(f"[{upstream}] 最新 release: {upstream_tag} (draft={draft}, prerelease={prerelease})")
    if last_tag == upstream_tag:
        print(f"[{upstream}] tracker 已是 {last_tag}，无更新。")
        return

    # 本地 release tag：是否加仓库名前缀，避免同名 tag 冲突
    local_tag = f"{repo_name_only(upstream)}-{upstream_tag}" if namespace_release_tags else upstream_tag
    local_release_name = f"[{folder_for_repo(upstream, aliases)}] {name}"

    tmp = tempfile.mkdtemp(prefix=f"assets_{repo_name_only(upstream)}_")
    try:
        asset_files = download_upstream_assets(rel.get("assets", []), tmp)
        print(f"[{upstream}] 下载资产 {len(asset_files)} 个：", asset_files)

        # 在当前仓库创建/补充 release（如已存在同 tag 则跳过创建）
        if not release_exists(current_repo, local_tag):
            created = create_release(current_repo, local_tag, local_release_name, body, draft, prerelease)
            print(f"[{upstream}] 已创建本仓库 release:", created.get("html_url"))
            for fpath in asset_files:
                print(f"[{upstream}] 上传资产到当前仓库:", fpath)
                upload_asset(created["upload_url"], fpath)
        else:
            print(f"[{upstream}] 本仓库已存在 tag={local_tag} 的 release，跳过创建。")

        # 计算网盘目录：/前缀/<别名>[/<tag>]
        alias_folder = folder_for_repo(upstream, aliases)
        remote_dir = f"{netdisk_prefix.rstrip('/')}/{alias_folder}"
        if append_tag:
            remote_dir = f"{remote_dir}/{upstream_tag}"

        ok = upload_to_baidunetdisk(tmp, remote_dir, os.environ.get("BAIDU_COOKIE") or "")
        if ok:
            print(f"[{upstream}] 百度网盘上传完成：{remote_dir}")

        # 更新 tracker：按“上游原始 tag”记账（不会与其他仓冲突）
        state["repos"][upstream] = {"last_tag": upstream_tag, "checked_at": now_utc_iso()}
        write_tracker_state(tracker_dir, state, upstream, upstream_tag)
        print(f"[{upstream}] tracker 已更新。")

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

def main():
    upstreams = parse_upstream_repos(os.environ.get("UPSTREAM_REPOS", ""))
    if not upstreams:
        print("未配置 UPSTREAM_REPOS，退出。")
        return

    tracker_dir = os.environ.get("TRACKER_DIR") or ".release-tracker"
    netdisk_prefix = os.environ.get("NETDISK_PREFIX") or "/apps/release-sync"
    append_tag = (os.environ.get("NETDISK_APPEND_TAG", "true").lower() == "true")
    aliases = load_repo_aliases()
    namespace_release_tags = (os.environ.get("NAMESPACE_RELEASE_TAGS", "true").lower() == "true")

    for up in upstreams:  # 顺序处理，避免并发踩 tracker
        print("=" * 20, up, "=" * 20)
        try:
            process_one(up, tracker_dir, netdisk_prefix, append_tag, aliases, namespace_release_tags)
        except Exception as e:
            print(f"[{up}] 错误：{e}")

if __name__ == "__main__":
    main()

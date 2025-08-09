#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, json, tempfile, shutil, pathlib, subprocess, datetime, re, time
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

def download_single_asset(asset: dict, dest_dir: str) -> str:
    """
    流式下载单个 release 资产到临时文件，返回文件路径
    """
    url = asset.get("browser_download_url") or asset.get("url")
    if not url:
        raise RuntimeError("资产无下载地址")
    headers = gh_headers()
    if "api.github.com/repos" in url and "/assets/" in url:
        headers = {**headers, "Accept": "application/octet-stream"}
    fn = asset.get("name") or pathlib.Path(url).name
    out = pathlib.Path(dest_dir) / fn
    with requests.get(url, headers=headers, stream=True) as r:
        r.raise_for_status()
        with open(out, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
    return str(out)

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
    actor = os.environ.get("GITHUB_ACTOR", "github-actions[bot]")
    email = os.environ.get("GIT_EMAIL", "github-actions[bot]@users.noreply.github.com")
    subprocess.run(["git", "add", "state.json"], cwd=dir_path, check=True)
    subprocess.run(
        ["git", "-c", f"user.name={actor}", "-c", f"user.email={email}",
         "commit", "-m", f"chore(tracker): {repo} -> {tag}"],
        cwd=dir_path, check=True
    )
    subprocess.run(["git", "push"], cwd=dir_path, check=True)

# ---------- Baidu Netdisk (login once, upload per file) ----------
def extract_bduss(cookie: str) -> str:
    m = re.search(r"BDUSS=([^;]+)", cookie or "")
    return m.group(1) if m else ""

def baidu_login(cookie: str):
    bduss = extract_bduss(cookie)
    if not bduss:
        raise RuntimeError("未在 BAIDU_COOKIE 中找到 BDUSS=...;")
    subprocess.run(["BaiduPCS-Go", "logout"], check=False)
    subprocess.run(["BaiduPCS-Go", "login", f"-bduss={bduss}"], check=True)

def baidu_ensure_dir(remote_dir: str):
    remote_dir = remote_dir if remote_dir.startswith("/") else "/" + remote_dir
    subprocess.run(["BaiduPCS-Go", "mkdir", remote_dir], check=False)

def baidu_upload_file(local_path: str, remote_dir: str):
    remote_dir = remote_dir if remote_dir.startswith("/") else "/" + remote_dir
    subprocess.run(["BaiduPCS-Go", "upload", local_path, remote_dir, "-retry", "3"], check=True)

# ---------- Utils ----------
def parse_upstream_repos(s: str):
    s = (s or "").strip()
    if not s:
        return []
    if s.startswith("["):
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

    # 本地 release 的 tag：是否加仓库名前缀，避免冲突
    local_tag = f"{repo_name_only(upstream)}-{upstream_tag}" if namespace_release_tags else upstream_tag
    local_release_name = f"[{folder_for_repo(upstream, aliases)}] {name}"

    # 远端网盘目录：/前缀/<别名>[/<tag>]
    alias_folder = folder_for_repo(upstream, aliases)
    remote_dir = f"{netdisk_prefix.rstrip('/')}/{alias_folder}"
    if append_tag:
        remote_dir = f"{remote_dir}/{upstream_tag}"

    tmp = tempfile.mkdtemp(prefix=f"assets_{repo_name_only(upstream)}_")
    try:
        assets = rel.get("assets", []) or []
        print(f"[{upstream}] 待处理资产 {len(assets)} 个")

        # 如需在“当前仓库”创建 release，先创建拿到 upload_url
        created = None
        upload_url = None
        if not release_exists(current_repo, local_tag):
            created = create_release(current_repo, local_tag, local_release_name, body, draft, prerelease)
            print(f"[{upstream}] 已创建本仓库 release:", created.get("html_url"))
            upload_url = created["upload_url"]
        else:
            print(f"[{upstream}] 本仓库已存在 tag={local_tag} 的 release，跳过创建。")

        # 百度网盘：登录 + 确保目录
        cookie = os.environ.get("BAIDU_COOKIE") or ""
        try:
            baidu_login(cookie)
            baidu_ensure_dir(remote_dir)
            can_upload_baidu = True
        except Exception as e:
            print(f"[{upstream}] 百度网盘不可用：{e}")
            can_upload_baidu = False

        # 逐个资产：下载 ->（可选）传 GitHub release -> 传百度网盘 -> 删除
        for idx, a in enumerate(assets, 1):
            print(f"[{upstream}] 处理资产 {idx}/{len(assets)}：{a.get('name')}")
            try:
                fpath = download_single_asset(a, tmp)
            except OSError as oe:
                print(f"[{upstream}] 下载失败（可能磁盘不足）：{oe}")
                raise
            except Exception as e:
                print(f"[{upstream}] 下载失败：{e}")
                continue

            if upload_url:
                try:
                    upload_asset(upload_url, fpath)
                except Exception as e:
                    print(f"[{upstream}] 上传到本仓库失败：{e}")

            if can_upload_baidu:
                try:
                    baidu_upload_file(fpath, remote_dir)
                except Exception as e:
                    print(f"[{upstream}] 上传到百度网盘失败：{e}")

            try:
                os.remove(fpath)
            except Exception as e:
                print(f"[{upstream}] 清理本地文件失败：{e}")

        # 无论是否有资产，都根据 tag 更新 tracker
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

    # 顺序处理（不并行）
    for up in upstreams:
        print("=" * 20, up, "=" * 20)
        try:
            process_one(up, tracker_dir, netdisk_prefix, append_tag, aliases, namespace_release_tags)
        except Exception as e:
            print(f"[{up}] 错误：{e}")

if __name__ == "__main__":
    main()

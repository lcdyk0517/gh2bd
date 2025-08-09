# Release Sync → GitHub Releases & Baidu Netdisk

一个 GitHub Actions 工作流 + Python 脚本，用来**按日定时**检查多个上游仓库的最新 Release：

- 在当前仓库创建/镜像对应 Release（可自动为本地 tag 加前缀防冲突）
- 将每个资产逐个下载 → 上传到 **百度网盘** 指定目录 → **立即删除本地文件**，避免磁盘爆满
- 通过 `release-tracker` 分支里的 `state.json` 记录每个上游仓库已处理的版本，**不回填历史**、仅增量同步
- 串行处理多个上游，保证顺序与稳定性

> 当前默认计划：**每天 04:00 UTC** 运行（可改）。

---

## 目录结构

```
.github/
  workflows/
    release-sync.yml          # 工作流
  scripts/
    sync_release_multi.py     # 同步脚本（多仓 + 串行 + 逐文件上传）
```

---

## 工作原理（TL;DR）

1. Workflow 拉取仓库并准备 `release-tracker` 工作树（仅存 `state.json`）。
2. 按 `UPSTREAM_REPOS` 顺序遍历：
   - 调用 GitHub API 获取上游最新 Release
   - 与 `state.json.repos[<owner/repo>].last_tag` 比较：相同跳过，不同则继续
   - \*\*（可选）\*\*在当前仓库创建本地 Release（`NAMESPACE_RELEASE_TAGS` 为 `true` 时本地 tag 为 `repo-原tag`，避免冲突）
   - 对每个资产**逐个**执行：流式下载 → **上传到本地 Release（如新建）** → 上传到百度网盘目标目录 → 删除本地临时文件
   - 更新 tracker：写入该仓库的 `last_tag` 与时间戳
3. `concurrency.group=release-sync` 防止多次运行之间互相踩踏。

> **为什么逐个上传？** 传统做法会先把所有资产下载到磁盘再上传，容易触发 `No space left on device`。本脚本改为**单文件流水线**，极大降低磁盘占用。

---

## 安装与配置

### 1) 添加文件

将本仓库中的两个文件放到你的项目：

- `.github/workflows/release-sync.yml`
- `.github/scripts/sync_release_multi.py`

### 2) 配置 Secrets（必需）

- \`\`：一个具有 `repo` 权限的 Personal Access Token，用于：
  - 推送 `release-tracker` 分支
  - 创建/发布 Release 与上传资产
- \`\`：包含 `BDUSS=...;` 的完整百度网盘 Cookie。脚本会自动从中提取 `BDUSS` 并用 **BaiduPCS-Go** 登录。

> 安全提示：Secrets 不会出现在日志中；请勿将 Cookie/BDUSS 写入仓库文件。

### 3) 配置环境变量（可在 workflow 顶部 `env:` 修改）

- `UPSTREAM_REPOS`（多行）— 需要监控的上游仓库列表，例如：
  ```
  PortsMaster/PortMaster-GUI
  retroGFX/UnofficialOS
  ROCKNIX/distribution
  knulli-cfw/distribution
  AmberELEC/AmberELEC-prerelease
  ```
- `REPO_ALIASES`（JSON）— 上游仓库到**中文目录别名**的映射：
  ```json
  {
    "PortsMaster/PortMaster-GUI": "PortsMaster仓库",
    "retroGFX/UnofficialOS": "UOS仓库",
    "ROCKNIX/distribution": "Rocknix仓库",
    "knulli-cfw/distribution": "Knulli仓库",
    "AmberELEC/AmberELEC-prerelease": "Amberelec仓库"
  }
  ```
- `NETDISK_PREFIX` — 网盘根目录前缀，例如：`/lcdyk有的掌机/同步github`
- `NETDISK_APPEND_TAG` — 是否在别名目录后再追加 `/<tag>`（`"true"`/`"false"`）。
- `TRACKER_BRANCH` / `TRACKER_DIR` — tracker 分支与本地 worktree 目录。
- `NAMESPACE_RELEASE_TAGS` — 为**本地 Release tag**添加仓库名前缀（`repo-原tag`）以避免多个上游同名 tag 冲突。

> **路径示例**：若 `NETDISK_PREFIX=/lcdyk有的掌机/同步github`、`REPO_ALIASES` 映射到 “UOS仓库”，且 `NETDISK_APPEND_TAG=false`，则上传到： `/lcdyk有的掌机/同步github/UOS仓库/`
>
> 若将 `NETDISK_APPEND_TAG=true` 且上游 tag 为 `20250802`，则： `/lcdyk有的掌机/同步github/UOS仓库/20250802/`

### 4) 调度（Schedule）

工作流默认：

```yaml
on:
  schedule:
    - cron: "0 4 * * *"  # 每天 04:00 UTC
```

> GitHub Actions 的 `cron` 是 **UTC**。如需北京时间 04:00，请改为 `0 20 * * *`。

---

## Tracker 说明

- 分支：`release-tracker`
- 文件：`state.json`
- 结构：

```json
{
  "repos": {
    "owner/repo": { "last_tag": "v1.2.3", "checked_at": "2025-08-01T00:00:00Z" }
  }
}
```

- 首次运行只会记录**上游当下的最新 Release**，不会回填历史。

---

## 常用操作

### 手动触发一次同步

在 Actions 页面选择 `Sync multiple upstream releases → mirror here → upload to Baidu Netdisk`，点击 **Run workflow**。

### 新增/删除上游仓库

修改 `UPSTREAM_REPOS` 列表并（可选）在 `REPO_ALIASES` 增加/删除对应映射。

### 修改网盘目录结构

- 只到**别名目录**：`NETDISK_APPEND_TAG: "false"`
- 到**别名 + tag**：`NETDISK_APPEND_TAG: "true"`

### 确保串行执行

本仓库的脚本本身就是**顺序 for-loop**；工作流仅有一个 Job，不使用 matrix；此外使用 `concurrency.group` 保证不同 Run 不会重叠。

---

## 故障排查（Troubleshooting）

### `No space left on device`

- 本脚本已改为**逐文件下载→上传→删除**，磁盘占用极低。
- 若**单个资产**就超过 Runner 可用空间：
  - 方案 1：改用自托管 Runner（磁盘更大）
  - 方案 2：改为上传到 **百度智能云 BOS** 并使用支持**直传/分片**的工具（可联系维护者获取 rclone+BOS 版本）
- 可选释放空间步骤（已在 workflow 示例中启用）：删除 Android/ghc/dotnet 缓存、Docker prune。

### 网盘登录失败

- 确认 `BAIDU_COOKIE` 中包含 `BDUSS=...;`；
- 该 Secret 不要带引号；多个字段用分号分隔，如：`BDUSS=xxx; STOKEN=yyy;`（顺序不限）。

### GitHub 权限/限流

- 创建 Release/上传资产需要 `GH_PAT` 具备 `repo` 权限；
- 若遇到 403/404，检查仓库保护规则与 Token 权限；
- 速率受限时，可适当调低同步频率。

### 已存在同名本地 tag

- 保持 `NAMESPACE_RELEASE_TAGS=true`（默认），本地 tag 将是 `repo-上游tag`，避免冲突。

---

## 典型日志片段

```
==================== retroGFX/UnofficialOS ====================
[retroGFX/UnofficialOS] 最新 release: 20250802 (draft=False, prerelease=False)
[retroGFX/UnofficialOS] 处理资产 1/3: UOS.img.xz
...
==================== ROCKNIX/distribution ====================
[ROCKNIX/distribution] 最新 release: 20250517 (draft=False, prerelease=False)
...
```

---

## 进阶可选项

- `INTER_REPO_DELAY_SECONDS`：在仓库之间插入延时（默认 0），例如 `5` 可缓解 API 限流。
- **MEGA 链接同步**：可添加额外 Job 使用 `MEGAcmd`（`mega-get`）拉取公开链接后再用 BaiduPCS-Go 上传；或使用 `rclone` 直接从 MEGA → BOS（对象存储）以避免落盘（需要 MEGA 账号配置）。

---

## 致谢

- [BaiduPCS-Go](https://github.com/qjfoidnh/BaiduPCS-Go) — 百度网盘命令行工具
- GitHub Actions — 本仓库自动化平台

---

## 许可

本文档与脚本示例以仓库许可证为准（若未指定则默认 MIT）。


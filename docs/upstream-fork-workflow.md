# 上游 hermes-agent Fork 工作流

> 2026-08-05 重写：M5 退役包装层 `huanxing-hermes-runtime`（并入 Rust daemon 进程内
> `EmbeddedHermesRuntime`）后，本仓从 `hermes-runtime/upstream/hermes-agent` 提到
> `huanxing-apps/hermes-agent` 独立存在——**本仓就是唤星维护的 hermes-agent fork 本体**，
> 不再挂在任何包装层目录下。本文是这份 fork 的长期维护工作流。
> （前身：`huanxing-apps/hermes-runtime/docs/UPSTREAM-FORK-WORKFLOW.md`，随包装层仓归档。）

唤星跟随 NousResearch 上游迭代，同时加唤星专属逻辑（HASN/hasn-node 适配、多 channel
扩展等），因此维护这份独立 fork。daemon 进程内直接用本仓源码起上游 gateway
（`python -m hermes_cli.main gateway run`），中间没有第二层 Python 控制面。

## 仓库与分支拓扑

| 角色 | URL | 用途 |
|------|-----|------|
| **上游官方** | `git@github.com:NousResearch/hermes-agent.git` | 只读，定期 rebase 同步 |
| **唤星 fork**（本仓） | `git@github.com:youngshunf/hermes-agent.git` | push 写，长期维护 |

| 分支 | 跟踪 | 说明 |
|------|------|------|
| `main` | `origin/main` ← `upstream/main` | 严格跟随上游，**不允许唤星代码 commit 到 main** |
| `huanxing` | `origin/huanxing` | 唤星开发主线，所有功能/修改都在这条分支 |
| `huanxing-feature/*` | `origin/huanxing-feature/*` | 短期功能分支，最终 merge 回 `huanxing` |

## 两个本地路径的角色（不要混淆）

| 路径 | 角色 | 是否动代码 | git remote |
|------|------|-----------|-----------|
| `~/.hermes/hermes-agent` | 用户**本机日常使用**的官方 hermes-agent 安装 | **不动** | origin = NousResearch |
| `huanxing-apps/hermes-agent`（本仓） | 唤星 daemon 开发 / 打包 / 部署用的 fork checkout | **在这里改** | origin = fork, upstream = NousResearch |

daemon 侧经 `[runtime.hermes_supervisor] upstream_path`（开发）或打包布局自动解析
（生产）找到本仓；桌面/镜像打包脚本经 `HASN_HERMES_AGENT_DIR`（默认
`<hasn-node 仓同级>/huanxing-apps/hermes-agent`）取源。

## 日常工作流

> ## ⚠️ 铁律：**本仓 HEAD 只由统一构建入口自动捕获，禁止手改钉版**
>
> hasn-node 的开发/打包入口从本仓真实 Git checkout 读取 HEAD，把同一个 40 位 SHA
> 同时编进 daemon，并在正式制品中写入 `.huanxing-hermes-agent-commit`。daemon 运行时
> 仍做**逐字相等**比较，不等就拒绝启动任何 gateway。后果不是报个错就完了，是
> **该机器上全部分身离线**、UI 只显示「Runtime 心跳过期，渠道暂不可操作」。
>
> | | |
> |---|---|
> | 版本事实源 | 本次构建实际消费的 `git -C <hermes-agent> rev-parse HEAD` |
> | daemon 期望值 | 构建期环境 `HASN_BUILD_HERMES_UPSTREAM_COMMIT`，由脚本自动注入 |
> | 制品实际值 | 打包脚本写入的 `.huanxing-hermes-agent-commit`，与 daemon 期望值同源 |
>
> **实测踩过（2026-08-06）**：M5 收尾时往本仓推了一笔只改 `docs/` 的提交，dev 环境所有分身
> 当场离线。旧流程要求再去 hasn-node 手工 bump 常量，且打包脚本剥离 `.git` 后漏写 marker，
> v0.3.2 正式包因此把 11 个绑定全部留在 degraded。此后版本契约改为同源自动生成：
>
> 1. `hasn-node/scripts/lib/hermes-upstream.sh::hasn_resolve_hermes_upstream_commit` 只从真实 Git
>    HEAD 取值，读不到或格式不合法就拒绝构建；
> 2. 开发启动脚本自动把 HEAD 注入 Cargo；桌面打包脚本把同一个值注入 daemon 并写入制品 marker；
> 3. daemon 侧继续 fail-closed，错误信息带 `期望 / 实际 / 来源 / root`。
>
> **不要**在 Rust 源码或本仓 checkout 中手写 SHA/marker；绕过标准入口直接构建的 daemon
> 会使用未注入哨兵并拒绝启动 Hermes gateway。

### 1. 在 huanxing 分支做修改

```bash
cd huanxing-apps/hermes-agent
git checkout huanxing
# ... 编辑代码 ...
git commit -m "feat(huanxing): 新增 HASN channel 适配"
git push origin huanxing
```

### 2. 同步上游最新代码

```bash
cd huanxing-apps/hermes-agent

# 先同步 main 到上游
git fetch upstream
git checkout main
git merge --ff-only upstream/main
git push origin main

# 再把唤星分支 rebase 上去
git checkout huanxing
git rebase main
# 解决冲突 (如有) 后:
git push --force-with-lease origin huanxing
```

**约束**：rebase 而非 merge，保持 huanxing 分支线性历史，方便给上游提 PR。

### 3. 给上游 NousResearch 提 PR

如果某项修改是**通用增强**（不是唤星专属），应该回馈上游：

```bash
git checkout huanxing
git checkout -b upstream-pr/feature-name
# cherry-pick 相关 commit (剥离唤星专属代码):
git cherry-pick <sha1> <sha2>
git push origin upstream-pr/feature-name
gh pr create --repo NousResearch/hermes-agent \
  --base main --head youngshunf:upstream-pr/feature-name \
  --title "feat: ..." --body "..."
```

### 4. 唤星专属代码的隔离原则

为了让 rebase 几乎零冲突，**唤星专属业务代码**应放在独立目录或文件，**不混入上游既有文件**：

- 唤星专属模块：放 `gateway/huanxing/`、`hermes_cli/huanxing/` 这种独立子包
- 必须改上游既有文件时：只加最小注入点（`if HUANXING_FEATURE: ...` 风格的 hook），把业务代码本体留在 `gateway/huanxing/` 里
- 配置项、常量加唤星前缀（`HUANXING_*`），避免和上游命名冲突

## 部署打包与钉版

父仓（huanxing-project / hasn-node）**不追踪**本仓版本——各机器各自
`git clone -b huanxing` 本仓，部署打包以当前 checkout 的 HEAD 为准：

```bash
# 新机器首次接入
cd huanxing-apps
git clone -b huanxing git@github.com:youngshunf/hermes-agent.git hermes-agent
cd hermes-agent
uv venv venv --python 3.12
uv pip install --python venv/bin/python -e ".[messaging,cli,mcp]"
```

同一 40 位 SHA 由 daemon `UpstreamCheckout` 校验：开发态直接读 git HEAD；
生产 rsync/tar 不带 `.git`，因此 hasn-node 打包链路会在**制品目录**写入
`.huanxing-hermes-agent-commit`。源码 checkout 不保存这份派生标记。任一 SHA
不一致都会把 checkout 判为无效并拒绝启动 gateway。

**版本钉住**：生产发布在构建开始时捕获 huanxing checkout 的具体 SHA，并在构建结束前
再次校验 HEAD 与工作树未变化，避免无意中把构建途中的混合代码带上线。

### 构建与自检（无需维护 SHA）

```bash
# 开发：入口会校验 checkout/venv、捕获 HEAD 并把它注入 Cargo
cd hasn-node
scripts/dev-desktop.sh

# 正式发布：唯一入口自动完成 HEAD 冻结、daemon 注入和制品 marker 写入
scripts/build-desktop-update.sh

# 秒级契约测试：真实临时 Git 仓 + 注入/marker 顺序静态守卫
bash tests/test_build_desktop_scripts.sh
```

如果某个 fork 提交不该进产品，应在发布前把发布用 checkout 切到目标提交；构建入口会以
当时的实际 HEAD 为唯一版本事实。禁止构建后再替换制品里的 Hermes 树或 marker。

## 常见问题

### Q: 为什么不直接用 `~/.hermes/hermes-agent`？

那是用户本机日常使用的官方 hermes-agent 安装，独立运维，**不能受开发污染**。daemon
必须用唤星自己控制的 fork checkout。

### Q: 修改上游文件时，rebase 冲突太多怎么办？

回到「隔离原则」—— 把业务代码挪到 `gateway/huanxing/` 等独立目录，上游文件只留极薄注入点。如果已经深入修改了上游文件，考虑把这部分作为 PR 提回上游，让它进 main，下次 rebase 就消失了。

### Q: 给 NousResearch 提 PR 被拒绝怎么办？

继续在 huanxing 分支维护，每次 rebase 处理冲突。最坏情况：分叉成本可控，因为业务代码已经隔离。

#!/usr/bin/env bash
# 唤星 fork 合并闸门 —— 每次 `git merge upstream/main` 之后必须跑，绿了才算合并完成。
#
# 为什么需要它：本仓是上游 hermes-agent 的 fork，我们在里面嫁接了自己的特性
# （gateway/hasn_session、MCP status/reconnect 路由、mcp_tool 韧性、记忆
# contribute 钩子、profile 隔离的兜底 key、credential_pool 冷却档……）。
# 上游重构随时可能把嫁接冲掉，且**上游的测试永远不会替我们发现**——所以每次合并后
# 必须由我们自己的闸门把关。
#
# 两个模式：
#
#   ./scripts/huanxing_merge_gate.sh          # 硬闸（默认·约 1 分钟）
#       跑「fork 触碰过的全部测试文件」，要求 **100% 绿**，一个红都不许有。
#       文件清单不是手写的，而是从 git 动态推导（见下），所以新增 fork 测试
#       自动进闸，不会漏。
#
#   ./scripts/huanxing_merge_gate.sh --full   # 全量闸（约 15 分钟）
#       跑全量 2000+ 文件，红文件集合必须是基线
#       scripts/huanxing_upstream_red_baseline.txt 的子集。
#       **新冒出来的红 = 合并引入的回归 = 闸门失败。**
#
# 为什么全量不能要求「零红」：上游自带一批在 macOS 开发机上就是红的测试
# （CI 是 Linux，跑不到）。这些红与我们的改动无关——已在纯 upstream/main 的
# worktree 上逐个验证过。硬要求零红等于要求我们替上游修 macOS 兼容性，
# 那闸门只会被永久绕过。故改为「基线之外不许有新红」，既诚实又能挡住回归。
#
# 基线怎么维护：见 scripts/huanxing_upstream_red_baseline.txt 文件头。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BASELINE="$SCRIPT_DIR/huanxing_upstream_red_baseline.txt"

cd "$REPO_ROOT"

# 上游基点：fork 分支与 upstream/main 的分叉点。此点之后碰过的 tests/ 文件
# 就是「我们的测试面」——包含新增的和我们改过的上游测试。
_merge_base() {
  git merge-base upstream/main HEAD 2>/dev/null || {
    echo "error: 找不到 upstream/main —— 先 git remote add upstream <上游地址> && git fetch upstream" >&2
    exit 2
  }
}

# 从跑测输出里提取红文件清单。
# run_tests_parallel.py 末尾把失败分三桶打印，三桶都算红：
#   === N files with test failures ===                        真的有测试挂了
#   === N files where all tests passed but pytest exited non-zero ===  warnings-as-errors 等
#   === N files where no tests ran ===                        collection/import 错、超时
# 只解析这三段（而不是 grep 整个日志）——失败详情正文里也可能出现相似形状的行。
_red_files_from_log() {
  awk '
    /^=== [0-9]+ files? with test failures/ { in_red = 1; next }
    /^=== [0-9]+ files? where all tests passed but pytest exited non-zero/ { in_red = 1; next }
    /^=== [0-9]+ files? where no tests ran/ { in_red = 1; next }
    /^=== / { in_red = 0 }
    in_red && /^  / { print $1 }
  ' "$1" | sort -u
}

# ── 全量闸 ──────────────────────────────────────────────────────────────────
if [ "${1:-}" = "--full" ]; then
  LOG="$(mktemp -t hx_gate_full)"
  echo "▶ 全量跑（约 15 分钟）——红文件集须 ⊆ 基线"
  # 注意：不能用管道直接接 awk，管道会吞掉 run_tests.sh 的退出码（本项目踩过）。
  # 先落盘再解析。全量必然非零退出（基线红存在），故这里显式容错。
  ./scripts/run_tests.sh > "$LOG" 2>&1 || true
  tail -1 "$LOG"

  RED_F="$(mktemp -t hx_gate_red)"
  BASE_F="$(mktemp -t hx_gate_base)"
  _red_files_from_log "$LOG" > "$RED_F"
  grep -vE '^[[:space:]]*(#|$)' "$BASELINE" | sort -u > "$BASE_F"

  # 基线之外的新红 = 回归
  NEW_RED="$(comm -23 "$RED_F" "$BASE_F")"
  # 基线里已经变绿的 = 该从基线里删掉（只提示，不拦）
  FIXED="$(comm -13 "$RED_F" "$BASE_F")"

  if [ -n "$FIXED" ]; then
    echo
    echo "ℹ️  基线里这些文件现在是绿的，可从 $BASELINE 移除："
    echo "$FIXED" | sed 's/^/    /'
  fi

  if [ -n "$NEW_RED" ]; then
    echo
    echo "❌ 闸门失败：出现基线之外的新红文件（= 本次合并引入的回归）"
    echo "$NEW_RED" | sed 's/^/    /'
    echo
    echo "详细失败输出：$LOG"
    exit 1
  fi

  echo
  echo "✅ 全量闸通过：红文件集 ⊆ 基线，无新增回归"
  exit 0
fi

# ── 硬闸（默认）：fork 触碰的测试必须全绿 ───────────────────────────────────
BASE_REF="$(_merge_base)"
echo "▶ 上游分叉点：$BASE_REF"

# 动态推导 fork 测试面：分叉点之后我们改过/新增的 tests/*.py，且当前仍存在
# （--diff-filter=d 排除已删除的；再逐个 -f 复核，因为 run_tests.sh 对不存在的
#  路径是**静默跳过**的，漏了不报错，所以下面还要核对 Discovered 数量）。
FILES=()
while IFS= read -r f; do
  [ -n "$f" ] && [ -f "$f" ] && FILES+=("$f")
done < <(git diff --name-only --diff-filter=d "$BASE_REF" HEAD -- 'tests/*.py' 'tests/**/*.py' 2>/dev/null)

if [ ${#FILES[@]} -eq 0 ]; then
  echo "⚠️  未发现 fork 触碰的测试文件——检查 upstream/main 是否为最新（git fetch upstream）"
  exit 2
fi

echo "▶ fork 测试面：${#FILES[@]} 个文件"
printf '    %s\n' "${FILES[@]}"
echo

LOG="$(mktemp -t hx_gate_fork)"
./scripts/run_tests.sh "${FILES[@]}" > "$LOG" 2>&1 && RC=0 || RC=$?

# 核对发现数 == 传入数：run_tests.sh 静默跳过不存在的路径，不核对会得到
# 「跑了个寂寞却全绿」的假绿。
DISCOVERED="$(grep -oE 'Discovered [0-9]+ test files?' "$LOG" | grep -oE '[0-9]+' | head -1 || echo 0)"
if [ "$DISCOVERED" != "${#FILES[@]}" ]; then
  echo "❌ 闸门失败：传入 ${#FILES[@]} 个文件，只发现 $DISCOVERED 个（run_tests.sh 静默跳过了不存在的路径）"
  echo "详细输出：$LOG"
  exit 1
fi

tail -1 "$LOG"

if [ "$RC" != 0 ]; then
  echo
  echo "❌ 闸门失败：fork 测试面有红 —— 这是我们自己的特性，必须修到全绿"
  _red_files_from_log "$LOG" | sed 's/^/    /'
  echo
  echo "详细失败输出：$LOG"
  exit 1
fi

echo
echo "✅ 硬闸通过：fork 测试面 ${#FILES[@]} 个文件全绿"
echo "   （全量回归请另跑：./scripts/huanxing_merge_gate.sh --full）"

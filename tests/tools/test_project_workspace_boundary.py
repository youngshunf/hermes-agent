"""项目运行的真实文件系统边界回归测试。"""

import json
import shutil
import tempfile
from pathlib import Path

import pytest

from tools import file_tools, terminal_tool
from tools.delegate_tool import _derive_child_env_overrides, _resolve_workspace_hint


@pytest.fixture
def restricted_project(monkeypatch: pytest.MonkeyPatch):
    """创建使用真实本地执行环境的受限项目任务。"""
    test_root = Path(tempfile.mkdtemp(prefix=".doc42-boundary-", dir=Path.cwd()))
    project_root = (test_root / "project").resolve()
    project_root.mkdir()
    task_id = f"project-boundary-{test_root.name}"
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_LOCAL_PERSISTENT", "false")
    terminal_tool.register_task_env_overrides(
        task_id,
        {
            "cwd": str(project_root),
            "isolate_environment": True,
            "restrict_file_tools_to_cwd": True,
        },
    )
    try:
        yield task_id, project_root
    finally:
        terminal_tool.cleanup_vm(task_id, force_remove=True)
        terminal_tool.clear_task_env_overrides(task_id)
        file_tools.clear_file_ops_cache(task_id)
        shutil.rmtree(test_root)


def _assert_boundary_rejection(result: str, project_root: Path) -> None:
    payload = json.loads(result)
    assert payload["status"] == "blocked"
    assert "PROJECT_WORKSPACE_BOUNDARY" in payload["error"]
    assert str(project_root) not in result


def test_relative_file_tools_operate_in_project_and_redact_absolute_root(
    restricted_project,
):
    """读写、替换和搜索都在真实项目根内完成，响应只暴露相对路径。"""
    task_id, project_root = restricted_project

    write_result = file_tools.write_file_tool(
        "notes/proof.txt",
        "first sentinel",
        task_id=task_id,
    )
    assert (project_root / "notes/proof.txt").exists(), write_result
    assert (project_root / "notes/proof.txt").read_text() == "first sentinel"
    assert str(project_root) not in write_result
    assert json.loads(write_result)["resolved_path"] == "notes/proof.txt"

    read_result = file_tools.read_file_tool("notes/proof.txt", task_id=task_id)
    assert "first sentinel" in read_result
    assert str(project_root) not in read_result

    patch_result = file_tools.patch_tool(
        path="notes/proof.txt",
        old_string="first",
        new_string="second",
        task_id=task_id,
    )
    assert (project_root / "notes/proof.txt").read_text() == "second sentinel"
    assert str(project_root) not in patch_result

    search_result = file_tools.search_tool(
        pattern="second sentinel",
        path="notes",
        task_id=task_id,
    )
    assert "proof.txt" in search_result
    assert str(project_root) not in search_result


@pytest.mark.parametrize("path_kind", ["absolute", "parent"])
def test_file_write_rejects_absolute_and_parent_paths_without_side_effect(
    restricted_project,
    tmp_path: Path,
    path_kind: str,
):
    """绝对路径与父级跳转均在写入前被拒绝。"""
    task_id, project_root = restricted_project
    outside_target = tmp_path / "outside" / f"{path_kind}.txt"
    candidate = (
        str(outside_target) if path_kind == "absolute" else "../outside/parent.txt"
    )

    result = file_tools.write_file_tool(candidate, "escape", task_id=task_id)

    _assert_boundary_rejection(result, project_root)
    assert not outside_target.exists()


def test_file_write_rejects_symlink_escape(restricted_project, tmp_path: Path):
    """项目内指向外部的符号链接不能成为写入通道。"""
    task_id, project_root = restricted_project
    outside = tmp_path / "outside-link-target"
    outside.mkdir()
    (project_root / "linked").symlink_to(outside, target_is_directory=True)

    result = file_tools.write_file_tool(
        "linked/escaped.txt",
        "escape",
        task_id=task_id,
    )

    _assert_boundary_rejection(result, project_root)
    assert not (outside / "escaped.txt").exists()


def test_other_file_tools_reject_outside_inputs(restricted_project, tmp_path: Path):
    """读取、替换、V4A 补丁和搜索都复用同一项目根门禁。"""
    task_id, project_root = restricted_project
    outside = tmp_path / "other-tools-outside.txt"
    outside.write_text("unchanged")
    results = [
        file_tools.read_file_tool(str(outside), task_id=task_id),
        file_tools.patch_tool(
            path=str(outside),
            old_string="unchanged",
            new_string="changed",
            task_id=task_id,
        ),
        file_tools.patch_tool(
            mode="patch",
            patch=(
                "*** Begin Patch\n"
                f"*** Update File: {outside}\n"
                "@@\n"
                "-unchanged\n"
                "+changed\n"
                "*** End Patch"
            ),
            task_id=task_id,
        ),
        file_tools.search_tool(
            pattern="unchanged",
            path="..",
            task_id=task_id,
        ),
    ]

    for result in results:
        _assert_boundary_rejection(result, project_root)
    assert outside.read_text() == "unchanged"


def test_file_write_rejects_replaced_project_root(restricted_project, tmp_path: Path):
    """注册后的项目根若被替换为外部符号链接，后续操作立即失败。"""
    task_id, project_root = restricted_project
    relocated = tmp_path / "relocated-project"
    outside = tmp_path / "replacement-target"
    project_root.rename(relocated)
    outside.mkdir()
    project_root.symlink_to(outside, target_is_directory=True)

    result = file_tools.write_file_tool("escaped.txt", "escape", task_id=task_id)

    _assert_boundary_rejection(result, project_root)
    assert not (outside / "escaped.txt").exists()


def test_terminal_runs_in_project_and_redacts_pwd(restricted_project):
    """真实终端副作用证明 cwd 正确，响应不回传绝对根。"""
    task_id, project_root = restricted_project

    result = terminal_tool.terminal_tool(
        command="printf 'terminal-ok' > terminal-proof.txt; pwd",
        task_id=task_id,
        force=True,
    )

    payload = json.loads(result)
    assert payload["exit_code"] == 0
    assert (project_root / "terminal-proof.txt").read_text() == "terminal-ok"
    assert str(project_root) not in result


@pytest.mark.parametrize("background", [False, True])
@pytest.mark.parametrize("workdir_kind", ["absolute", "parent"])
def test_terminal_rejects_outside_workdir_before_spawn(
    restricted_project,
    tmp_path: Path,
    background: bool,
    workdir_kind: str,
):
    """前台和后台命令的显式 workdir 都不能越出项目根。"""
    task_id, project_root = restricted_project
    outside = tmp_path / "terminal-outside"
    outside.mkdir(exist_ok=True)
    workdir = str(outside) if workdir_kind == "absolute" else "../terminal-outside"

    result = terminal_tool.terminal_tool(
        command="printf 'escape' > should-not-exist.txt",
        background=background,
        workdir=workdir,
        task_id=task_id,
        force=True,
    )

    _assert_boundary_rejection(result, project_root)
    assert not (outside / "should-not-exist.txt").exists()


def test_delegate_inherits_project_workspace_restriction():
    """子分身获得相同 cwd、环境隔离和项目根限制。"""
    result = _derive_child_env_overrides(
        {
            "cwd": "/project",
            "isolate_environment": True,
            "restrict_file_tools_to_cwd": True,
        },
        "/project",
    )

    assert result == {
        "cwd": "/project",
        "isolate_environment": True,
        "restrict_file_tools_to_cwd": True,
    }


def test_restricted_project_does_not_put_absolute_workspace_in_child_prompt(
    restricted_project,
):
    """受限项目的子分身提示词不得包含本机绝对路径。"""
    task_id, project_root = restricted_project
    terminal_tool.record_session_cwd(task_id, str(project_root))

    class ParentAgent:
        _current_task_id = task_id

    try:
        assert _resolve_workspace_hint(ParentAgent()) is None
    finally:
        terminal_tool.clear_session_cwd(task_id)

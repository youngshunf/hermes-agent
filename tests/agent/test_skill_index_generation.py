"""技能索引 generation 的进程内热刷新回归测试。"""

from __future__ import annotations

import json
from pathlib import Path

from agent.prompt_builder import (
    build_skills_system_prompt,
    clear_skills_system_prompt_cache,
)
from agent.skill_utils import (
    advance_local_skill_index_generation,
    read_local_skill_index_generation,
)
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools.skill_manager_tool import skill_manage


def _write_skill(skills_dir: Path, name: str, description: str) -> Path:
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        (
            "---\n"
            f"name: {name}\n"
            f"description: {description}\n"
            "---\n"
            f"# {name}\n\n"
            "按真实流程执行。\n"
        ),
        encoding="utf-8",
    )
    return skill_md


def _use_hermes_home(home: Path):
    home.mkdir(parents=True, exist_ok=True)
    token = set_hermes_home_override(home)
    clear_skills_system_prompt_cache(clear_snapshot=True)
    return token


def test_local_generation_rebuilds_cached_prompt_for_add_edit_and_delete(
    tmp_path: Path,
) -> None:
    home = tmp_path / "profile"
    token = _use_hermes_home(home)
    try:
        skills_dir = home / "skills"
        _write_skill(skills_dir, "existing-skill", "旧描述")
        initial = build_skills_system_prompt()
        assert "existing-skill" in initial
        assert "旧描述" in initial

        added_md = _write_skill(skills_dir, "added-skill", "新增描述")
        added_md.parent.parent.joinpath("existing-skill", "SKILL.md").write_text(
            (
                "---\n"
                "name: existing-skill\n"
                "description: 新描述\n"
                "---\n"
                "# existing-skill\n\n"
                "按真实流程执行。\n"
            ),
            encoding="utf-8",
        )

        # 未推进 generation 时保留 LRU 快路径，不隐式递归扫描文件树。
        unchanged = build_skills_system_prompt()
        assert unchanged == initial
        assert "added-skill" not in unchanged
        assert "新描述" not in unchanged

        first_generation = advance_local_skill_index_generation()
        refreshed = build_skills_system_prompt()
        assert "added-skill" in refreshed
        assert "新增描述" in refreshed
        assert "新描述" in refreshed
        assert "旧描述" not in refreshed

        added_md.parent.joinpath("SKILL.md").unlink()
        added_md.parent.rmdir()
        assert build_skills_system_prompt() == refreshed

        second_generation = advance_local_skill_index_generation()
        assert second_generation != first_generation
        after_delete = build_skills_system_prompt()
        assert "added-skill" not in after_delete
    finally:
        clear_skills_system_prompt_cache(clear_snapshot=True)
        reset_hermes_home_override(token)


def test_external_revision_rebuilds_cached_prompt_without_copying_to_profile(
    tmp_path: Path,
) -> None:
    home = tmp_path / "profile"
    shared_common = tmp_path / "shared" / "common"
    shared_skills = shared_common / "skills"
    shared_skills.mkdir(parents=True)
    _write_skill(shared_skills, "shared-existing", "公共旧技能")
    shared_common.joinpath(".index.json").write_text(
        json.dumps({"revision": "rev-1", "skills": {}}),
        encoding="utf-8",
    )

    token = _use_hermes_home(home)
    try:
        home.joinpath("config.yaml").write_text(
            "skills:\n"
            "  external_dirs:\n"
            f"    - {shared_skills}\n",
            encoding="utf-8",
        )
        initial = build_skills_system_prompt()
        assert "shared-existing" in initial

        _write_skill(shared_skills, "shared-added", "公共新增技能")
        unchanged = build_skills_system_prompt()
        assert unchanged == initial
        assert "shared-added" not in unchanged
        assert not home.joinpath("skills", "shared-added").exists()

        shared_common.joinpath(".index.json").write_text(
            json.dumps({"revision": "rev-2", "skills": {}}),
            encoding="utf-8",
        )
        refreshed = build_skills_system_prompt()
        assert "shared-added" in refreshed
        assert "公共新增技能" in refreshed
        assert not home.joinpath("skills", "shared-added").exists()
    finally:
        clear_skills_system_prompt_cache(clear_snapshot=True)
        reset_hermes_home_override(token)


def test_native_skill_manager_advances_persistent_generation(tmp_path: Path) -> None:
    home = tmp_path / "profile"
    token = _use_hermes_home(home)
    try:
        before = read_local_skill_index_generation()
        result = json.loads(
            skill_manage(
                action="create",
                name="native-managed",
                content=(
                    "---\n"
                    "name: native-managed\n"
                    "description: 原生管理器创建\n"
                    "---\n"
                    "# native-managed\n\n"
                    "按真实流程执行。\n"
                ),
            )
        )
        assert result["success"] is True
        after = read_local_skill_index_generation()
        assert after != before
        assert "native-managed" in build_skills_system_prompt()
    finally:
        clear_skills_system_prompt_cache(clear_snapshot=True)
        reset_hermes_home_override(token)

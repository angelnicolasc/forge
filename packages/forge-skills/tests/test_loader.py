"""Tests for forge_skills.loader — SKILL.md and YAML round-trip."""

from __future__ import annotations

import io
import textwrap
from typing import TYPE_CHECKING

import pytest

from forge_skills.loader import load_dir, load_skill_md, load_skill_yaml

from .conftest import VALID_SKILL_MD

if TYPE_CHECKING:
    from pathlib import Path


class TestLoadSkillMd:
    def test_load_from_string(self) -> None:
        skill = load_skill_md(VALID_SKILL_MD)
        assert skill.name == "summarize"
        assert skill.description == "Summarize a document"
        assert skill.timeout_seconds == 30.0

    def test_load_examples(self) -> None:
        skill = load_skill_md(VALID_SKILL_MD)
        assert "summarize this document" in skill.examples
        assert "give me the key points" in skill.examples

    def test_load_parameters(self) -> None:
        skill = load_skill_md(VALID_SKILL_MD)
        assert len(skill.parameters) == 2
        text_param = skill.parameters[0]
        assert text_param.name == "text"
        assert text_param.required is True

    def test_optional_parameter_default(self) -> None:
        skill = load_skill_md(VALID_SKILL_MD)
        max_words = skill.parameters[1]
        assert max_words.name == "max_words"
        assert max_words.required is False
        assert max_words.default == 200

    def test_load_tags(self) -> None:
        skill = load_skill_md(VALID_SKILL_MD)
        assert "nlp" in skill.tags
        assert "text" in skill.tags

    def test_body_captured(self) -> None:
        skill = load_skill_md(VALID_SKILL_MD)
        assert "Summarize" in skill.body
        assert "{text}" in skill.body

    def test_load_from_stringio(self) -> None:
        buf = io.StringIO(VALID_SKILL_MD)
        skill = load_skill_md(buf)
        assert skill.name == "summarize"

    def test_load_from_bytes_io(self) -> None:
        buf = io.BytesIO(VALID_SKILL_MD.encode("utf-8"))
        skill = load_skill_md(buf)
        assert skill.name == "summarize"

    def test_load_from_path(self, tmp_path: Path) -> None:
        p = tmp_path / "summarize.md"
        p.write_text(VALID_SKILL_MD, encoding="utf-8")
        skill = load_skill_md(p)
        assert skill.name == "summarize"
        assert skill.source == str(p)

    def test_no_frontmatter_raises(self) -> None:
        with pytest.raises(ValueError, match="frontmatter"):
            load_skill_md("Just some text without frontmatter.")

    def test_invalid_yaml_frontmatter_raises(self) -> None:
        bad = "---\n{unclosed: [bracket\n---\n# body"
        with pytest.raises(ValueError, match="parse error"):
            load_skill_md(bad)

    def test_unsupported_type_raises(self) -> None:
        with pytest.raises(TypeError):
            load_skill_md(42)  # type: ignore[arg-type]

    def test_source_label_inline(self) -> None:
        skill = load_skill_md(VALID_SKILL_MD)
        assert skill.source == "inline"


class TestLoadSkillYaml:
    def test_load_from_string(self) -> None:
        yaml_str = textwrap.dedent("""
            name: ping
            description: Simple ping skill
            tags:
              - util
        """).strip()
        skill = load_skill_yaml(yaml_str)
        assert skill.name == "ping"
        assert "util" in skill.tags

    def test_load_from_path(self, tmp_path: Path) -> None:
        yaml_str = "name: hello\ndescription: A greeting skill\n"
        p = tmp_path / "hello.yaml"
        p.write_text(yaml_str, encoding="utf-8")
        skill = load_skill_yaml(p)
        assert skill.name == "hello"

    def test_load_with_parameters(self) -> None:
        yaml_str = textwrap.dedent("""
            name: greet
            parameters:
              - name: user
                type: string
                required: true
        """).strip()
        skill = load_skill_yaml(yaml_str)
        assert skill.parameters[0].name == "user"

    def test_invalid_yaml_raises(self) -> None:
        with pytest.raises(ValueError, match="parse error"):
            load_skill_yaml("{unclosed")

    def test_non_mapping_raises(self) -> None:
        with pytest.raises(ValueError, match="mapping"):
            load_skill_yaml("- item1\n- item2")


class TestLoadDir:
    def test_load_valid_skills(self, tmp_path: Path) -> None:
        for name in ("alpha", "beta"):
            md = f"---\nname: {name}\ndescription: skill {name}\n---\n# body\n"
            (tmp_path / f"{name}.md").write_text(md)
        skills = load_dir(tmp_path)
        names = {s.name for s in skills}
        assert names == {"alpha", "beta"}

    def test_invalid_files_skipped(self, tmp_path: Path) -> None:
        (tmp_path / "good.md").write_text("---\nname: good\n---\n")
        (tmp_path / "bad.md").write_text("no frontmatter here")
        skills = load_dir(tmp_path)
        assert len(skills) == 1
        assert skills[0].name == "good"

    def test_empty_dir_returns_empty(self, tmp_path: Path) -> None:
        assert load_dir(tmp_path) == []

    def test_nested_glob(self, tmp_path: Path) -> None:
        sub = tmp_path / "nlp"
        sub.mkdir()
        (sub / "translate.md").write_text("---\nname: translate\n---\n")
        skills = load_dir(tmp_path, glob="**/*.md")
        assert any(s.name == "translate" for s in skills)

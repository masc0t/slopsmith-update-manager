"""Tests for _apply_tree — the copy-overwrite apply that replaced the
directory-rename swap so plugin updates don't fail on Windows when the
plugin dir holds an open file (#22).
"""

from pathlib import Path

import routes


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_apply_tree_copies_nested_files_into_fresh_target(tmp_path):
    src = tmp_path / "src"
    target = tmp_path / "target"  # does not exist yet
    _write(src / "plugin.json", '{"id":"x"}')
    _write(src / "sub" / "screen.js", "console.log(1)")

    written = routes._apply_tree(src, target)

    assert (target / "plugin.json").read_text(encoding="utf-8") == '{"id":"x"}'
    assert (target / "sub" / "screen.js").read_text(encoding="utf-8") == "console.log(1)"
    assert set(written) == {"plugin.json", "sub/screen.js"}


def test_apply_tree_overwrites_existing_files(tmp_path):
    src = tmp_path / "src"
    target = tmp_path / "target"
    _write(target / "routes.py", "OLD")
    _write(src / "routes.py", "NEW")

    routes._apply_tree(src, target)

    assert (target / "routes.py").read_text(encoding="utf-8") == "NEW"


def test_apply_tree_preserves_files_absent_from_source(tmp_path):
    """Local state (.git, plugin-local data) not in the download stays put."""
    src = tmp_path / "src"
    target = tmp_path / "target"
    _write(target / ".git" / "HEAD", "ref: refs/heads/main")
    _write(target / "local_data.db", "user state")
    _write(src / "screen.html", "<div></div>")

    routes._apply_tree(src, target)

    assert (target / "screen.html").read_text(encoding="utf-8") == "<div></div>"
    # untouched
    assert (target / ".git" / "HEAD").read_text(encoding="utf-8") == "ref: refs/heads/main"
    assert (target / "local_data.db").read_text(encoding="utf-8") == "user state"

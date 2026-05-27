"""Tests for the v1.11.0 Storage tab — disk inventory + clear + open.

Exercises the pure helpers (_dir_size, _path_size, _validate_relpath,
_plugin_declared_paths), the cached inventory builder, the symbolic
target resolver, and the three storage endpoints wired via FastAPI's
TestClient. Also pins the cache-invalidation hooks added to install /
uninstall / apply_update.
"""

import json
import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def fake_dirs(tmp_path, monkeypatch):
    """Redirect every module-level path the storage code reads.

    Yields a dict of the redirected paths so tests can populate them
    with whatever shape they need.
    """
    plugins_dir = tmp_path / "plugins"
    config_dir = tmp_path / "config"
    cache_dir = config_dir / "update_manager"
    sloppak_dir = config_dir / "sloppak_cache"
    dlc_dir = tmp_path / "dlc"
    for p in (plugins_dir, config_dir, cache_dir, sloppak_dir, dlc_dir):
        p.mkdir(parents=True, exist_ok=True)
    remote_cache_file = cache_dir / "remote_cache.json"

    monkeypatch.setattr(routes, "PLUGINS_DIR", plugins_dir)
    # On desktop BUNDLED_PLUGINS_DIR points at the read-only app-bundle
    # plugins root; on Docker / source checkouts it equals PLUGINS_DIR.
    # Pin it to the same tmp here so the inventory doesn't leak the
    # dev tree's real plugins into the test universe.
    monkeypatch.setattr(routes, "BUNDLED_PLUGINS_DIR", plugins_dir)
    monkeypatch.setattr(routes, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(routes, "REMOTE_CACHE_FILE", remote_cache_file)
    # Reset the inventory + remote caches so tests start from a clean slate.
    monkeypatch.setattr(routes, "_inventory_cache", None)
    monkeypatch.setattr(routes, "_remote_cache", None)
    monkeypatch.setattr(routes, "_storage_ctx", {
        "get_dlc_dir": lambda: dlc_dir,
        "get_sloppak_cache_dir": lambda: sloppak_dir,
        "config_dir": config_dir,
    })

    yield {
        "tmp": tmp_path,
        "plugins": plugins_dir,
        "config": config_dir,
        "cache": cache_dir,
        "sloppak": sloppak_dir,
        "dlc": dlc_dir,
        "remote_cache_file": remote_cache_file,
    }


def _write_plugin(plugins_dir: Path, pid: str, *, dirname: str | None = None,
                  name: str | None = None,
                  server_files: list[str] | None = None,
                  diag_files: list[str] | None = None,
                  source_bytes: int = 0) -> Path:
    """Create a plugin directory with a manifest and optional padding bytes."""
    pdir = plugins_dir / (dirname or pid)
    pdir.mkdir(parents=True, exist_ok=True)
    manifest: dict = {"id": pid, "name": name or pid}
    if server_files is not None:
        manifest["settings"] = {"server_files": server_files}
    if diag_files is not None:
        manifest["diagnostics"] = {"server_files": diag_files}
    (pdir / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    if source_bytes:
        (pdir / "filler.bin").write_bytes(b"x" * source_bytes)
    return pdir


# ── _dir_size / _path_size ────────────────────────────────────────────


def test_dir_size_missing_path_returns_zero(tmp_path):
    assert routes._dir_size(tmp_path / "nope") == 0


def test_dir_size_sums_files_recursively(tmp_path):
    (tmp_path / "a.bin").write_bytes(b"x" * 100)
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.bin").write_bytes(b"y" * 250)
    (sub / "nested").mkdir()
    (sub / "nested" / "c.bin").write_bytes(b"z" * 7)
    assert routes._dir_size(tmp_path) == 357


def test_dir_size_skips_symlinks(tmp_path):
    target = tmp_path / "real.bin"
    target.write_bytes(b"x" * 500)
    link = tmp_path / "link.bin"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")
    # Total counts the real file once, not the symlink to it.
    assert routes._dir_size(tmp_path) == 500


def test_path_size_file(tmp_path):
    f = tmp_path / "x"
    f.write_bytes(b"hello")
    assert routes._path_size(f) == 5


def test_path_size_directory(tmp_path):
    (tmp_path / "a").write_bytes(b"1234")
    (tmp_path / "b").write_bytes(b"56")
    assert routes._path_size(tmp_path) == 6


def test_path_size_missing(tmp_path):
    assert routes._path_size(tmp_path / "missing") == 0


def test_path_size_symlink_returns_zero(tmp_path):
    target = tmp_path / "real.bin"
    target.write_bytes(b"x" * 100)
    link = tmp_path / "link.bin"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")
    assert routes._path_size(link) == 0


# ── _validate_relpath ─────────────────────────────────────────────────


@pytest.mark.parametrize("rp", [
    "x.db",
    "models/whisper.bin",
    "sub/dir/with/depth",
    ".hidden",          # leading-dot segments are allowed for server_files parity
    "sub/.hidden/file",
])
def test_validate_relpath_accepts(rp):
    assert routes._validate_relpath(rp) is True


@pytest.mark.parametrize("rp", [
    "",
    "/abs/path",
    "\\abs\\windows",
    "back\\slash",
    "C:/drive/letter",
    "../escape",
    "sub/../escape",
])
def test_validate_relpath_rejects(rp):
    assert routes._validate_relpath(rp) is False


def test_validate_relpath_rejects_non_string():
    assert routes._validate_relpath(None) is False  # type: ignore[arg-type]
    assert routes._validate_relpath(123) is False  # type: ignore[arg-type]


# ── _plugin_declared_paths ────────────────────────────────────────────


def test_plugin_declared_paths_merges_and_dedupes(fake_dirs):
    pdir = _write_plugin(
        fake_dirs["plugins"], "foo",
        server_files=["foo.db", "models/"],
        diag_files=["foo.db", "foo.diag.json"],  # foo.db duplicated across fields
    )
    paths = routes._plugin_declared_paths(pdir, fake_dirs["config"])
    rel = sorted(p.relative_to(fake_dirs["config"]).as_posix() for p in paths)
    assert rel == ["foo.db", "foo.diag.json", "models"]


def test_plugin_declared_paths_drops_invalid_relpaths(fake_dirs):
    pdir = _write_plugin(
        fake_dirs["plugins"], "foo",
        server_files=["ok.db", "../escape", "/abs"],
    )
    paths = routes._plugin_declared_paths(pdir, fake_dirs["config"])
    assert [p.name for p in paths] == ["ok.db"]


def test_plugin_declared_paths_missing_manifest(fake_dirs):
    pdir = fake_dirs["plugins"] / "no_manifest"
    pdir.mkdir()
    assert routes._plugin_declared_paths(pdir, fake_dirs["config"]) == []


def test_plugin_declared_paths_handles_malformed_manifest(fake_dirs):
    pdir = fake_dirs["plugins"] / "broken"
    pdir.mkdir()
    (pdir / "plugin.json").write_text("{not json", encoding="utf-8")
    assert routes._plugin_declared_paths(pdir, fake_dirs["config"]) == []


# ── _build_inventory ──────────────────────────────────────────────────


def test_build_inventory_emits_all_expected_buckets(fake_dirs):
    inv = routes._build_inventory()
    keys = [b["key"] for b in inv["buckets"]]
    # Order matters for the UI: DLC, user_data, plugins_dir, sloppak_cache, github_cache.
    assert keys == ["dlc", "user_data", "plugins_dir", "sloppak_cache", "github_cache"]
    assert inv["config_dir"] == str(fake_dirs["config"])
    # is_desktop is False in tests (we didn't set SLOPSMITH_PLUGINS_DIR).
    assert inv["is_desktop"] is False
    assert inv["can_open"] is False


def test_build_inventory_plugin_size_includes_source_and_data(fake_dirs):
    """The v1.11.0 fix: size_bytes = source_size + data_size, not just data."""
    _write_plugin(
        fake_dirs["plugins"], "withdata",
        server_files=["mydata.db"],
        source_bytes=1000,
    )
    # Plant declared data in config_dir so it has non-zero size too.
    (fake_dirs["config"] / "mydata.db").write_bytes(b"x" * 2500)

    inv = routes._build_inventory()
    plugin = next(p for p in inv["plugins"] if p["id"] == "withdata")
    # Source dir contains filler.bin (1000) + plugin.json — round-trip via _path_size.
    expected_source = routes._path_size(fake_dirs["plugins"] / "withdata")
    assert plugin["source_size_bytes"] == expected_source
    assert plugin["data_size_bytes"] == 2500
    assert plugin["size_bytes"] == expected_source + 2500
    assert plugin["declares_server_files"] is True


def test_build_inventory_plugin_without_declared_files_still_shows_source(fake_dirs):
    """Bug from review: undeclared plugin used to report size_bytes=0."""
    _write_plugin(fake_dirs["plugins"], "bare", source_bytes=300)
    inv = routes._build_inventory()
    plugin = next(p for p in inv["plugins"] if p["id"] == "bare")
    assert plugin["declares_server_files"] is False
    assert plugin["data_size_bytes"] == 0
    assert plugin["source_size_bytes"] > 0
    assert plugin["size_bytes"] == plugin["source_size_bytes"]


def test_build_inventory_open_path_prefers_largest_existing_data_path(fake_dirs):
    _write_plugin(
        fake_dirs["plugins"], "heavy",
        server_files=["small.db", "big.db", "missing.db"],
        source_bytes=100,
    )
    (fake_dirs["config"] / "small.db").write_bytes(b"x" * 100)
    (fake_dirs["config"] / "big.db").write_bytes(b"x" * 5000)
    # missing.db intentionally not created.

    inv = routes._build_inventory()
    plugin = next(p for p in inv["plugins"] if p["id"] == "heavy")
    assert plugin["open_path"] == str(fake_dirs["config"] / "big.db")


def test_build_inventory_open_path_falls_back_to_source_dir(fake_dirs):
    """No declared paths exist on disk → open the source dir instead."""
    _write_plugin(
        fake_dirs["plugins"], "absent",
        server_files=["never_created.db"],
        source_bytes=10,
    )
    inv = routes._build_inventory()
    plugin = next(p for p in inv["plugins"] if p["id"] == "absent")
    assert plugin["open_path"] == str(fake_dirs["plugins"] / "absent")


def test_build_inventory_plugins_sorted_by_total_size_desc(fake_dirs):
    _write_plugin(fake_dirs["plugins"], "small", source_bytes=50)
    _write_plugin(fake_dirs["plugins"], "big", source_bytes=10_000)
    _write_plugin(fake_dirs["plugins"], "mid", source_bytes=1_000)
    inv = routes._build_inventory()
    ids_in_order = [p["id"] for p in inv["plugins"]]
    assert ids_in_order == ["big", "mid", "small"]


def test_build_inventory_uses_manifest_id_not_dirname(fake_dirs):
    """Mirrors the divergence in _installed_plugin_dirs (e.g. dir tab_view, id tabview)."""
    _write_plugin(fake_dirs["plugins"], "myid", dirname="my_dirname", source_bytes=10)
    inv = routes._build_inventory()
    assert any(p["id"] == "myid" for p in inv["plugins"])


# ── _get_inventory caching ────────────────────────────────────────────


def test_get_inventory_caches_until_invalidated(fake_dirs):
    _write_plugin(fake_dirs["plugins"], "one", source_bytes=10)
    first = routes._get_inventory()
    # Add a second plugin after the walk — without invalidation, the
    # cached payload should not reflect it.
    _write_plugin(fake_dirs["plugins"], "two", source_bytes=20)
    cached = routes._get_inventory()
    assert cached is first
    assert {p["id"] for p in cached["plugins"]} == {"one"}

    routes._invalidate_inventory()
    fresh = routes._get_inventory()
    assert {p["id"] for p in fresh["plugins"]} == {"one", "two"}


def test_get_inventory_refresh_forces_recompute(fake_dirs):
    _write_plugin(fake_dirs["plugins"], "one", source_bytes=10)
    first = routes._get_inventory()
    _write_plugin(fake_dirs["plugins"], "two", source_bytes=20)
    refreshed = routes._get_inventory(refresh=True)
    assert refreshed is not first
    assert {p["id"] for p in refreshed["plugins"]} == {"one", "two"}


# ── _resolve_open_target ──────────────────────────────────────────────


def test_resolve_open_target_bucket_key(fake_dirs):
    routes._invalidate_inventory()
    p = routes._resolve_open_target("plugins_dir")
    assert p == fake_dirs["plugins"]


def test_resolve_open_target_unknown_returns_none(fake_dirs):
    assert routes._resolve_open_target("nonsense") is None


def test_resolve_open_target_missing_bucket_returns_none(fake_dirs, monkeypatch):
    # Point sloppak_cache at a nonexistent path so the bucket exists in
    # the inventory but the path doesn't on disk.
    nonexistent = fake_dirs["tmp"] / "gone"
    monkeypatch.setitem(routes._storage_ctx, "get_sloppak_cache_dir", lambda: nonexistent)
    routes._invalidate_inventory()
    assert routes._resolve_open_target("sloppak_cache") is None


def test_resolve_open_target_plugin_uses_open_path(fake_dirs):
    _write_plugin(fake_dirs["plugins"], "heavy",
                  server_files=["big.db"], source_bytes=10)
    big = fake_dirs["config"] / "big.db"
    big.write_bytes(b"x" * 9999)
    routes._invalidate_inventory()
    assert routes._resolve_open_target("plugin:heavy") == big


def test_resolve_open_target_plugin_falls_back_to_source(fake_dirs):
    _write_plugin(fake_dirs["plugins"], "bare", source_bytes=10)
    routes._invalidate_inventory()
    assert routes._resolve_open_target("plugin:bare") == fake_dirs["plugins"] / "bare"


def test_resolve_open_target_rejects_malformed_plugin_id(fake_dirs):
    assert routes._resolve_open_target("plugin:../etc") is None
    assert routes._resolve_open_target("plugin:") is None
    assert routes._resolve_open_target("plugin:has spaces") is None


def test_resolve_open_target_unknown_plugin_id(fake_dirs):
    assert routes._resolve_open_target("plugin:not_installed") is None


# ── Endpoint coverage via TestClient ──────────────────────────────────


@pytest.fixture
def client(fake_dirs):
    """Build a FastAPI app with the plugin's routes wired up."""
    app = FastAPI()
    context = {
        "get_dlc_dir": lambda: fake_dirs["dlc"],
        "get_sloppak_cache_dir": lambda: fake_dirs["sloppak"],
        "config_dir": fake_dirs["config"],
        "extract_meta": lambda *a, **kw: None,
        "meta_db": None,
        "load_sibling": lambda name: None,
        "log": __import__("logging").getLogger("test"),
    }
    routes.setup(app, context)
    with TestClient(app) as c:
        yield c


def test_storage_endpoint_returns_inventory(client, fake_dirs):
    _write_plugin(fake_dirs["plugins"], "demo", source_bytes=100)
    r = client.get("/api/plugins/update_manager/storage?refresh=1")
    assert r.status_code == 200
    body = r.json()
    assert "buckets" in body and "plugins" in body
    assert any(p["id"] == "demo" for p in body["plugins"])


def test_storage_clear_sloppak_wipes_contents(client, fake_dirs):
    (fake_dirs["sloppak"] / "song.bin").write_bytes(b"x" * 1024)
    (fake_dirs["sloppak"] / "sub").mkdir()
    (fake_dirs["sloppak"] / "sub" / "nested.bin").write_bytes(b"y" * 256)

    r = client.post(
        "/api/plugins/update_manager/storage/clear",
        json={"target": "sloppak_cache"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["freed_bytes"] == 1280
    # Dir kept, contents gone.
    assert fake_dirs["sloppak"].exists()
    assert list(fake_dirs["sloppak"].iterdir()) == []


def test_storage_clear_github_cache_deletes_file_and_drops_in_memory(client, fake_dirs):
    cache_file = fake_dirs["remote_cache_file"]
    cache_file.write_text(json.dumps({"k": {"value": 1, "etag": "x", "expires_at": 9e9}}))
    # Force the in-memory cache to load so we can prove it's reset.
    routes._remote_cache_get_dict()
    assert routes._remote_cache  # populated

    r = client.post(
        "/api/plugins/update_manager/storage/clear",
        json={"target": "github_cache"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["freed_bytes"] > 0
    assert not cache_file.exists()
    assert routes._remote_cache == {}


def test_storage_clear_rejects_unknown_target(client):
    r = client.post(
        "/api/plugins/update_manager/storage/clear",
        json={"target": "everything"},
    )
    assert r.status_code == 200
    assert "Unknown clear target" in r.json()["error"]


def test_storage_clear_rejects_missing_target(client):
    r = client.post(
        "/api/plugins/update_manager/storage/clear",
        json={},
    )
    assert r.status_code == 200
    assert r.json()["error"] == "Missing target"


def test_storage_clear_invalidates_inventory_cache(client, fake_dirs):
    # Prime the cache, then clear, then ensure the next inventory call rebuilds.
    routes._get_inventory()
    assert routes._inventory_cache is not None
    client.post(
        "/api/plugins/update_manager/storage/clear",
        json={"target": "github_cache"},
    )
    assert routes._inventory_cache is None


def test_storage_open_requires_desktop(client):
    """IS_DESKTOP defaults to False in tests — opening should be refused."""
    r = client.post(
        "/api/plugins/update_manager/storage/open",
        json={"target": "plugins_dir"},
    )
    assert r.status_code == 200
    assert "desktop" in r.json()["error"].lower()


def test_storage_open_rejects_unknown_target_on_desktop(client, monkeypatch):
    monkeypatch.setattr(routes, "IS_DESKTOP", True)
    r = client.post(
        "/api/plugins/update_manager/storage/open",
        json={"target": "nonsense"},
    )
    assert r.status_code == 200
    assert "Unknown or missing target" in r.json()["error"]


def test_storage_open_invokes_native_opener(client, fake_dirs, monkeypatch):
    monkeypatch.setattr(routes, "IS_DESKTOP", True)
    routes._invalidate_inventory()
    called = {}

    def fake_open(path):
        called["path"] = path
        return True, None

    monkeypatch.setattr(routes, "_open_path_native", fake_open)
    r = client.post(
        "/api/plugins/update_manager/storage/open",
        json={"target": "plugins_dir"},
    )
    body = r.json()
    assert body["ok"] is True
    assert called["path"] == fake_dirs["plugins"]


def test_storage_open_propagates_native_error(client, fake_dirs, monkeypatch):
    monkeypatch.setattr(routes, "IS_DESKTOP", True)
    routes._invalidate_inventory()
    monkeypatch.setattr(routes, "_open_path_native",
                        lambda p: (False, "boom"))
    r = client.post(
        "/api/plugins/update_manager/storage/open",
        json={"target": "plugins_dir"},
    )
    assert r.json()["error"] == "boom"


# ── Cache invalidation hooks on install / uninstall / apply_update ───


def test_uninstall_invalidates_inventory_cache(client, fake_dirs):
    _write_plugin(fake_dirs["plugins"], "victim", source_bytes=10)
    routes._get_inventory()  # prime
    assert routes._inventory_cache is not None
    r = client.post("/api/plugins/update_manager/uninstall/victim")
    assert r.status_code == 200
    assert r.json().get("ok") is True
    assert routes._inventory_cache is None


def test_install_invalidates_inventory_cache(client, fake_dirs, monkeypatch):
    """Stub the network bits — we only care that the success branch invalidates."""
    routes._get_inventory()
    assert routes._inventory_cache is not None

    monkeypatch.setattr(routes, "_default_branch", lambda o, r: "main")
    monkeypatch.setattr(routes, "_latest_sha", lambda o, r, b: "deadbeef" * 5)
    monkeypatch.setattr(routes, "_download_and_replace",
                        lambda owner, repo, ref, target, preserve_git: target.mkdir(parents=True, exist_ok=True))
    monkeypatch.setattr(routes, "_write_marker", lambda *a, **kw: None)

    r = client.post(
        "/api/plugins/update_manager/install",
        json={"url": "https://github.com/foo/bar", "dirname": "bar"},
    )
    body = r.json()
    assert body.get("ok") is True, body
    assert routes._inventory_cache is None


def test_install_without_dirname_derives_from_repo_slug(client, fake_dirs, monkeypatch):
    """Installing a not-in-registry plugin can omit dirname; the server
    derives a lowercase snake_case dir from the repo slug (slopsmith
    [-plugin]- prefix stripped, dashes → underscores, lowercased)."""
    monkeypatch.setattr(routes, "_default_branch", lambda o, r: "main")
    monkeypatch.setattr(routes, "_latest_sha", lambda o, r, b: "deadbeef" * 5)
    monkeypatch.setattr(routes, "_download_and_replace",
                        lambda owner, repo, ref, target, preserve_git: target.mkdir(parents=True, exist_ok=True))
    monkeypatch.setattr(routes, "_write_marker", lambda *a, **kw: None)

    r = client.post(
        "/api/plugins/update_manager/install",
        json={"url": "https://github.com/someone/slopsmith-plugin-Cool-Thing"},
    )
    body = r.json()
    assert body.get("ok") is True, body
    assert body["dirname"] == "cool_thing", body


def test_install_rejects_non_github_url(client):
    r = client.post(
        "/api/plugins/update_manager/install",
        json={"url": "https://example.com/not/github"},
    )
    body = r.json()
    assert "error" in body and "GitHub" in body["error"], body


def test_apply_update_invalidates_inventory_cache(client, fake_dirs, monkeypatch):
    pdir = _write_plugin(fake_dirs["plugins"], "tgt", source_bytes=10)
    routes._get_inventory()
    assert routes._inventory_cache is not None

    monkeypatch.setattr(routes, "_resolve_source",
                        lambda t: {"owner": "foo", "repo": "bar", "branch": "main", "source": "marker"})
    monkeypatch.setattr(routes, "_default_branch", lambda o, r: "main")
    monkeypatch.setattr(routes, "_latest_sha", lambda o, r, b: "feedface" * 5)
    monkeypatch.setattr(routes, "_download_and_replace", lambda *a, **kw: None)
    monkeypatch.setattr(routes, "_write_marker", lambda *a, **kw: None)

    r = client.post("/api/plugins/update_manager/update/tgt")
    body = r.json()
    assert body.get("ok") is True, body
    assert routes._inventory_cache is None


# ── Bundled-plugins-dir inventory (regression for the v1.9.0 desktop bug) ──


def test_installed_plugin_dirs_scans_bundled_dir_on_desktop(tmp_path, monkeypatch):
    """On desktop, PLUGINS_DIR is the user-writable plugins dir; the
    update_manager itself lives under BUNDLED_PLUGINS_DIR (the read-only
    app-bundle dir). Both must appear in the inventory so clicking
    Check on the update_manager row doesn't return "Plugin not found".
    """
    user_dir = tmp_path / "user_plugins"
    bundled_dir = tmp_path / "bundled_plugins"
    user_dir.mkdir()
    bundled_dir.mkdir()
    _write_plugin(user_dir, "midi_capo")
    _write_plugin(bundled_dir, "update_manager")
    monkeypatch.setattr(routes, "PLUGINS_DIR", user_dir)
    monkeypatch.setattr(routes, "BUNDLED_PLUGINS_DIR", bundled_dir)

    dirs = routes._installed_plugin_dirs()
    assert set(dirs.keys()) == {"midi_capo", "update_manager"}
    assert dirs["midi_capo"].parent.resolve() == user_dir.resolve()
    assert dirs["update_manager"].parent.resolve() == bundled_dir.resolve()


def test_installed_plugin_dirs_user_wins_on_id_collision(tmp_path, monkeypatch):
    """When the same plugin id exists in both dirs (user installed an
    external copy on top of a bundled one), the user-dir entry wins —
    matches slopsmith core's "user override beats bundled" load order.
    """
    user_dir = tmp_path / "user_plugins"
    bundled_dir = tmp_path / "bundled_plugins"
    user_dir.mkdir()
    bundled_dir.mkdir()
    _write_plugin(user_dir, "shared", name="user-copy")
    _write_plugin(bundled_dir, "shared", name="bundled-copy")
    monkeypatch.setattr(routes, "PLUGINS_DIR", user_dir)
    monkeypatch.setattr(routes, "BUNDLED_PLUGINS_DIR", bundled_dir)

    dirs = routes._installed_plugin_dirs()
    assert dirs["shared"].parent.resolve() == user_dir.resolve()


def test_is_bundled_flags_desktop_bundle_path(tmp_path, monkeypatch):
    """Plugins under BUNDLED_PLUGINS_DIR (when distinct from PLUGINS_DIR)
    are treated as bundled even without a manifest flag, so the UI hides
    Check/Update/Uninstall and the server refuses write operations.
    """
    user_dir = tmp_path / "user_plugins"
    bundled_dir = tmp_path / "bundled_plugins"
    user_dir.mkdir()
    bundled_dir.mkdir()
    user_plugin = _write_plugin(user_dir, "midi_capo")
    bundled_plugin = _write_plugin(bundled_dir, "update_manager")
    monkeypatch.setattr(routes, "PLUGINS_DIR", user_dir)
    monkeypatch.setattr(routes, "BUNDLED_PLUGINS_DIR", bundled_dir)

    assert routes._is_bundled(bundled_plugin) is True
    assert routes._is_bundled(user_plugin) is False


def test_is_bundled_does_not_flag_on_docker(tmp_path, monkeypatch):
    """On Docker / source checkouts the two dirs are equal; plugins
    living there are NOT bundled by path (only by manifest flag).
    """
    shared = tmp_path / "plugins"
    shared.mkdir()
    p = _write_plugin(shared, "midi_capo")
    monkeypatch.setattr(routes, "PLUGINS_DIR", shared)
    monkeypatch.setattr(routes, "BUNDLED_PLUGINS_DIR", shared)

    assert routes._is_bundled(p) is False


# ── _norm_key / _repo_slug ────────────────────────────────────────────


def test_norm_key_folds_case_and_separators():
    assert routes._norm_key("Tab-View") == "tab_view"
    assert routes._norm_key("transpose-chords") == "transpose_chords"
    assert routes._norm_key("  Foo_Bar ") == "foo_bar"
    assert routes._norm_key(None) == ""


def test_repo_slug_strips_conventional_prefixes():
    assert routes._repo_slug("byrongamatos/slopsmith-plugin-tabview") == "tabview"
    assert routes._repo_slug("byrongamatos/slopsmith-plugin-guitar-theory") == "guitar-theory"
    # Bare `slopsmith-` fallback (this repo) — longer prefix checked first.
    assert routes._repo_slug("masc0t/slopsmith-update-manager") == "update-manager"
    # No recognized prefix → repo name unchanged.
    assert routes._repo_slug("someone/unrelated-repo") == "unrelated-repo"


# ── Registry browse "Bundled" vs "Installed" (dir-name/id/slug mismatch) ──


def test_registry_marks_bundled_across_dir_id_slug_mismatches(client, fake_dirs, monkeypatch):
    """Regression: bundled plugins whose README install dirname matches
    neither their on-disk directory name NOR their manifest id were shown
    as "Installed" (or even an "Install" button) instead of "Bundled".

    Covers all three identity shapes the matcher must fold together:
      - dir != id, dirname == id     (tab_import: dir `tabimport`)
      - dir != id, dirname == id     (practice_journal: dir `practice`)
      - dir == id, dirname == slug   (tabview shipped under dir `tab_view`)
      - dir == id, slug needs `-`→`_` (guitar_theory under `guitar-theory-lab`)
      - dir == id == dirname         (setlist — already worked)
    """
    user_dir = fake_dirs["tmp"] / "user_plugins"
    bundled_dir = fake_dirs["tmp"] / "bundled_plugins"
    user_dir.mkdir()
    bundled_dir.mkdir()
    _write_plugin(user_dir, "midi_capo")
    _write_plugin(bundled_dir, "tab_import", dirname="tabimport", name="Import Tab")
    _write_plugin(bundled_dir, "practice_journal", dirname="practice", name="Practice Journal")
    _write_plugin(bundled_dir, "tabview", name="Tab View")
    _write_plugin(bundled_dir, "guitar_theory", name="Guitar Theory Lab")
    _write_plugin(bundled_dir, "setlist")
    monkeypatch.setattr(routes, "PLUGINS_DIR", user_dir)
    monkeypatch.setattr(routes, "BUNDLED_PLUGINS_DIR", bundled_dir)

    monkeypatch.setattr(routes, "_http_get", lambda *a, **kw: b"")
    monkeypatch.setattr(routes, "_parse_registry", lambda md: [
        {"name": "Import Tab", "dirname": "tab_import", "url": "u", "repo": "o/slopsmith-plugin-tabimport", "description": "d"},
        {"name": "Practice Journal", "dirname": "practice_journal", "url": "u", "repo": "o/slopsmith-plugin-practice", "description": "d"},
        {"name": "Tab View", "dirname": "tab_view", "url": "u", "repo": "o/slopsmith-plugin-tabview", "description": "d"},
        {"name": "Guitar Theory Lab", "dirname": "guitar-theory-lab", "url": "u", "repo": "o/slopsmith-plugin-guitar-theory", "description": "d"},
        {"name": "Setlist Builder", "dirname": "setlist", "url": "u", "repo": "o/slopsmith-plugin-setlist", "description": "d"},
        {"name": "MIDI Capo", "dirname": "midi_capo", "url": "u", "repo": "o/slopsmith-plugin-midi-capo", "description": "d"},
        {"name": "Tuner", "dirname": "tuner", "url": "u", "repo": "o/slopsmith-plugin-tuner", "description": "d"},
    ])

    r = client.get("/api/plugins/update_manager/registry")
    assert r.status_code == 200
    by_dir = {e["dirname"]: e for e in r.json()["entries"]}

    # Every bundled plugin → Bundled, regardless of which token matched.
    for d in ("tab_import", "practice_journal", "tab_view", "guitar-theory-lab", "setlist"):
        assert by_dir[d]["installed"] is True, d
        assert by_dir[d]["overrides_bundled"] is True, d

    # Manually-installed (user dir) → Installed, not Bundled.
    assert by_dir["midi_capo"]["installed"] is True
    assert by_dir["midi_capo"]["overrides_bundled"] is False

    # Not present anywhere → installable.
    assert by_dir["tuner"]["installed"] is False
    assert by_dir["tuner"]["overrides_bundled"] is False

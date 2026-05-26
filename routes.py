"""Update Manager - installs, updates, and restarts slopsmith plugins
and the slopsmith core itself via GitHub zip downloads.

No `git` CLI needed (the slopsmith container ships without it). State for
installed plugins is tracked in a `.slopsmith-installed.json` marker inside
each plugin directory. For plugins that were installed via host-side
`git clone` (the traditional method) the `.git/` directory is read directly
to infer origin and local commit.
"""

import fnmatch
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from fastapi import Request


REGISTRY_URL = "https://raw.githubusercontent.com/byrongamatos/slopsmith/main/README.md"
SLUG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_\-]{0,63}$")
GH_REPO_RE = re.compile(
    r"^https://github\.com/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+?)(?:\.git)?/?$"
)
MARKER = ".slopsmith-installed.json"
UA = "slopsmith-update-manager/1.4"
# Process start time, captured at module import. Exposed via the
# /start_time endpoint so the frontend can detect external restarts
# (e.g. `docker compose restart`) and clear the per-row pending-
# restart UI it would otherwise leave stuck across the restart.
_PROCESS_STARTED_AT = time.time()
_env_plugins = os.environ.get("SLOPSMITH_PLUGINS_DIR", "").strip()
IS_DESKTOP = bool(_env_plugins)
PLUGINS_DIR = Path(_env_plugins) if _env_plugins else Path(__file__).resolve().parent.parent
CACHE_DIR = Path(os.environ.get("CONFIG_DIR", "/config")) / "update_manager"
EXCL_FILE = CACHE_DIR / "exclusions.json"

CORE_REPO_OWNER = "byrongamatos"
CORE_REPO_NAME = "slopsmith"
CORE_MOUNTED_PATHS = {"server.py", "ug_browser.py", "lib", "static"}
CORE_IGNORED_PATHS = {"plugins", "*.md", "docs", "tests", ".claude"}
CORE_MARKER_FILE = CACHE_DIR / "core.json"
CORE_EXCLUSION_KEY = "__core__"
APP_ROOT = Path("/app")
REBUILD_CMD = "cd slopsmith && git pull && docker compose build web && docker compose up -d"
SELF_UPDATE_STAGING = CACHE_DIR / "self_update"


def _load_exclusions() -> set[str]:
    if not EXCL_FILE.exists():
        return set()
    try:
        data = json.loads(EXCL_FILE.read_text())
        return set(data.get("excluded", []))
    except Exception:
        return set()


def _save_exclusions(excluded: set[str]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    EXCL_FILE.write_text(json.dumps({"excluded": sorted(excluded)}, indent=2))


def _http_get(url: str, accept: str | None = None, timeout: int = 20) -> bytes:
    headers = {"User-Agent": UA}
    if accept:
        headers["Accept"] = accept
    # Optional GitHub auth lifts the unauthenticated 60/hour IP rate
    # limit to 5000/hour, which matters when a Check-for-updates pass
    # over 30+ plugins plus a few /versions clicks would otherwise
    # blow through the unauth budget. Sent only to api.github.com and
    # raw.githubusercontent.com so we don't leak the token to other
    # hosts (codeload.github.com doesn't need it for public repos).
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token and ("api.github.com" in url or "raw.githubusercontent.com" in url):
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _http_get_conditional(url: str, etag: str | None = None, accept: str | None = None, timeout: int = 20):
    """Conditional GET with If-None-Match.

    Returns (status, body_bytes_or_None, etag_or_None).
    304 → status=304, body=None, etag preserved (caller should keep
          the cached value but bump its TTL).
    200 → status=200, body=fresh bytes, etag=new ETag from response.
    Other → urllib raises (HTTPError for 4xx/5xx, OSError for network).

    Conditional GETs against api.github.com that return 304 do NOT
    count against the rate limit, so this is the primary mechanism
    for staying inside the 60/hour anonymous budget while still
    refreshing data on the long persistent-cache TTL.
    """
    headers = {"User-Agent": UA}
    if accept:
        headers["Accept"] = accept
    if etag:
        headers["If-None-Match"] = etag
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token and ("api.github.com" in url or "raw.githubusercontent.com" in url):
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), resp.headers.get("ETag")
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return 304, None, e.headers.get("ETag") if e.headers else etag
        raise


def _http_json(url: str) -> dict:
    return json.loads(_http_get(url, accept="application/vnd.github+json").decode("utf-8"))


def _parse_repo_url(url: str):
    m = GH_REPO_RE.match((url or "").rstrip("/"))
    if not m:
        return None, None
    return m.group(1), m.group(2)


def _parse_registry(md: str) -> list[dict]:
    """Parse the 'Available Plugins' table out of slopsmith's README."""
    entries = []
    in_section = False
    for line in md.splitlines():
        stripped = line.strip()
        if not in_section:
            if stripped.startswith("### Available Plugins"):
                in_section = True
            continue
        # Stop at next heading
        if stripped.startswith("## ") or (stripped.startswith("### ") and "Available Plugins" not in stripped):
            break
        if not stripped.startswith("|"):
            continue
        # Skip header and separator rows
        if re.match(r"^\|[\s\-:|]+\|$", stripped):
            continue
        m = re.match(
            r"\|\s*\[([^\]]+)\]\(([^)]+)\)\s*\|\s*([^|]+?)\s*\|\s*(.+?)\s*\|",
            stripped,
        )
        if not m:
            continue
        name, url, desc, install_cmd = m.groups()
        owner, repo = _parse_repo_url(url)
        if not owner:
            continue
        dm = re.search(r"\.git`?\s+([A-Za-z0-9_\-]+)`?", install_cmd)
        dirname = dm.group(1) if dm else repo
        entries.append({
            "name": name.strip(),
            "url": f"https://github.com/{owner}/{repo}",
            "repo": f"{owner}/{repo}",
            "description": desc.strip(),
            "dirname": dirname.strip(),
        })
    return entries


def _installed_plugin_dirs() -> dict[str, Path]:
    """Map plugin manifest id → directory Path.

    Keyed by the `id` from plugin.json (not the directory name) because
    the slopsmith core exposes plugins to the frontend by manifest id and
    those two can diverge (e.g. dir `tab_view` with id `tabview`).
    """
    out = {}
    if not PLUGINS_DIR.is_dir():
        return out
    for p in sorted(PLUGINS_DIR.iterdir()):
        if not p.is_dir() or p.name.startswith("_"):
            continue
        manifest = p / "plugin.json"
        if not manifest.exists():
            continue
        pid = p.name
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("id"), str) and data["id"]:
                pid = data["id"]
        except Exception:
            pass
        out[pid] = p
    return out


def _is_bundled(plugin_dir: Path) -> bool:
    """Return True if the plugin's manifest declares `bundled: true`.

    Bundled plugins ship in-tree with the slopsmith container image
    (slopsmith#160). They aren't `git clone`-installed and don't carry
    a marker file or `.git/`. Updates and uninstalls are handled by
    slopsmith core, not by this plugin.
    """
    manifest = plugin_dir / "plugin.json"
    if not manifest.exists():
        return False
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        return bool(isinstance(data, dict) and data.get("bundled") is True)
    except Exception:
        return False


def _read_marker(plugin_dir: Path) -> dict | None:
    f = plugin_dir / MARKER
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text())
    except Exception:
        return None


def _write_marker(plugin_dir: Path, owner: str, repo: str, branch: str, sha: str) -> None:
    (plugin_dir / MARKER).write_text(json.dumps({
        "repo": f"{owner}/{repo}",
        "url": f"https://github.com/{owner}/{repo}",
        "branch": branch,
        "sha": sha,
        "installed_at": int(time.time()),
    }, indent=2))


def _read_git_origin(plugin_dir: Path) -> str | None:
    cfg = plugin_dir / ".git" / "config"
    if not cfg.exists():
        return None
    try:
        text = cfg.read_text(errors="ignore")
    except Exception:
        return None
    m = re.search(r"url\s*=\s*(\S+)", text)
    return m.group(1) if m else None


def _read_git_local_sha(plugin_dir: Path) -> tuple[str | None, str | None, Path | None]:
    """Return (sha, branch, sha_source) for a `.git`-managed plugin dir.

    `sha_source` is the file the sha was actually read from — the loose
    ref file, .git/packed-refs, or .git/HEAD (detached). Callers that
    need to gauge "when was this sha last updated" (e.g. for freshness
    comparisons) should stat this path rather than guessing at HEAD.

    Branch names with slashes (e.g. `feature/foo`) are preserved
    verbatim — the ref name is the bit after `refs/heads/`, kept whole.
    """
    head = plugin_dir / ".git" / "HEAD"
    if not head.exists():
        return None, None, None
    try:
        h = head.read_text().strip()
    except Exception:
        return None, None, None
    if h.startswith("ref: "):
        ref_name = h[5:].strip()
        # Strip the leading category (refs/heads/, refs/tags/, ...) so
        # the returned `branch` value is the user-facing name. Handles
        # multi-slash branch names like `feature/foo` correctly.
        branch = ref_name
        for prefix in ("refs/heads/", "refs/tags/", "refs/remotes/"):
            if branch.startswith(prefix):
                branch = branch[len(prefix):]
                break
        ref_path = plugin_dir / ".git" / ref_name
        if ref_path.exists():
            try:
                return ref_path.read_text().strip(), branch, ref_path
            except Exception:
                pass
        packed = plugin_dir / ".git" / "packed-refs"
        if packed.exists():
            try:
                for ln in packed.read_text().splitlines():
                    if ln.endswith(" " + ref_name):
                        return ln.split()[0], branch, packed
            except Exception:
                pass
        return None, branch, None
    # Detached HEAD — the sha is in HEAD itself.
    return h, None, head


def _default_branch(owner: str, repo: str) -> str:
    """Return the repo's default branch, ETag-cached.

    Goes through _conditional_fetch_json so repeat resolutions for the
    same (owner, repo) are free under GitHub's rate limit. Important
    for the version-first optimisation in _check_one — branchless
    plugins (manifest/registry sources) would otherwise burn one
    api.github.com slot here before the cheap raw.githubusercontent
    version probe gets a chance. With caching, that cost is paid once
    per plugin per TTL window instead of every recheck.
    """
    try:
        data = _conditional_fetch_json(
            f"https://api.github.com/repos/{owner}/{repo}",
            key=f"default_branch:{owner}/{repo}",
        )
    except Exception:
        return "main"
    if isinstance(data, dict):
        return data.get("default_branch") or "main"
    return "main"


def _quote_ref(ref: str) -> str:
    """URL-encode a git ref for use as a GitHub URL path segment.

    Preserves the `refs/heads/` / `refs/tags/` / `refs/remotes/`
    prefix structure but encodes the branch / tag name portion (which
    may contain `/`, e.g. `feature/foo`). Without this, slash-named
    branches get split into extra path segments and the request 404s.
    """
    for prefix in ("refs/heads/", "refs/tags/", "refs/remotes/"):
        if ref.startswith(prefix):
            return prefix + urllib.parse.quote(ref[len(prefix):], safe="")
    return urllib.parse.quote(ref, safe="")


def _latest_sha(owner: str, repo: str, branch: str) -> str | None:
    data = _http_json(f"https://api.github.com/repos/{owner}/{repo}/commits/{_quote_ref(branch)}")
    return data.get("sha")


# Disk-persisted TTL cache for everything we fetch from GitHub.
#
# Without persistence, every container restart wipes the cache and the
# next Check-for-updates pass spends ~one API call per plugin to refill
# it — pointlessly burning rate-limit budget on data that almost never
# changes minute-to-minute. Persisting under CACHE_DIR (which lives on
# the rocksmith-config Docker volume) survives restarts; combined with
# the long TTL, a typical user only ever fetches each remote SHA once
# per session-day, even with frequent restarts.
#
# Stored as one JSON file with string keys so we don't have to
# serialise tuples. Per-entry shape: { value, expires_at }. The cache
# is loaded lazily on first read and rewritten on every put. Writes
# are best-effort — a disk failure (read-only filesystem, no permission)
# falls back to in-memory only.
REMOTE_CACHE_FILE = CACHE_DIR / "remote_cache.json"
_REMOTE_CACHE_TTL_S = 1800   # 30 minutes — longer than the old 5 min
_VERSIONS_CACHE_TTL_S = 3600 # 1 hour — version-bump history rarely churns
_remote_cache: dict | None = None
_remote_cache_lock = threading.Lock()


def _remote_cache_get_dict() -> dict:
    global _remote_cache
    if _remote_cache is not None:
        return _remote_cache
    with _remote_cache_lock:
        if _remote_cache is not None:
            return _remote_cache
        loaded: dict = {}
        if REMOTE_CACHE_FILE.exists():
            try:
                raw = json.loads(REMOTE_CACHE_FILE.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    loaded = raw
            except Exception:
                pass
        _remote_cache = loaded
        return _remote_cache


def _remote_cache_save_locked(cache: dict) -> None:
    """Serialise `cache` to disk. CALLER MUST HOLD `_remote_cache_lock`.

    Iterating `cache` via `json.dumps` while another thread inserts a
    key would raise "dictionary changed size during iteration", and two
    threads writing the file concurrently could interleave — both are
    real hazards now that the bulk `/updates` pass runs `_cache_put`
    from a 10-worker ThreadPoolExecutor. Holding the lock for the
    mutation + the save serialises all of it.
    """
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        REMOTE_CACHE_FILE.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    except Exception:
        # Disk-cache failure is non-fatal — the in-memory dict still
        # works for the rest of this process's lifetime.
        pass


def _cache_entry(key: str) -> dict | None:
    """Return the raw cache entry (value, etag, expires_at) or None.

    Differs from _cache_get in that it returns the entry even when
    expired — callers that do conditional refetch want the etag from
    the stale entry to send If-None-Match.

    Lock-free: `dict.get` is atomic under the GIL, and the entry dict
    it returns is only ever replaced wholesale (never mutated in place
    except for `expires_at`, under the lock), so a reader at worst sees
    a one-tick-stale `expires_at` — harmless.
    """
    cache = _remote_cache_get_dict()
    entry = cache.get(key)
    return entry if isinstance(entry, dict) else None


def _cache_get(key: str):
    """Return value if fresh; None if missing or expired."""
    entry = _cache_entry(key)
    if entry is None:
        return None
    if time.time() > entry.get("expires_at", 0):
        return None
    return entry.get("value")


def _cache_put(key: str, value, etag: str | None = None, ttl_s: int = _REMOTE_CACHE_TTL_S) -> None:
    cache = _remote_cache_get_dict()  # may briefly take the lock for lazy init, then releases it
    with _remote_cache_lock:
        cache[key] = {
            "value": value,
            "etag": etag,
            "expires_at": time.time() + ttl_s,
        }
        _remote_cache_save_locked(cache)


def _cache_refresh_ttl(key: str, ttl_s: int = _REMOTE_CACHE_TTL_S) -> None:
    """Bump expires_at without changing value — used after a 304."""
    cache = _remote_cache_get_dict()
    with _remote_cache_lock:
        entry = cache.get(key)
        if isinstance(entry, dict):
            entry["expires_at"] = time.time() + ttl_s
            _remote_cache_save_locked(cache)


def _cache_expire(key: str) -> None:
    """Mark a cache entry stale so the next read does a conditional
    refetch (304 → free, 200 → fresh value). The stored ETag is kept,
    so a force-refresh of unchanged data still costs nothing under the
    rate limit. Used by the per-plugin Check button to retry a
    previously-failed registry / source resolution on demand.
    """
    cache = _remote_cache_get_dict()
    with _remote_cache_lock:
        entry = cache.get(key)
        if isinstance(entry, dict):
            entry["expires_at"] = 0
            _remote_cache_save_locked(cache)


def _conditional_fetch_json(url: str, key: str, ttl_s: int = _REMOTE_CACHE_TTL_S):
    """Fetch JSON with persistent cache + conditional refresh.

    Behaviour:
      cache fresh → return cached value (no HTTP call)
      cache stale → conditional GET with stored etag
        304       → bump TTL, return cached value (no rate-limit cost)
        200       → store fresh value + new etag, return new value
        error     → on rate-limit (403/429), return stale value if any;
                    otherwise re-raise so callers can surface the error.
      cache miss  → unconditional GET, store result.
    """
    fresh = _cache_get(key)
    if fresh is not None:
        return fresh
    stale = _cache_entry(key)  # may be expired
    etag = stale.get("etag") if stale else None
    try:
        status, body, new_etag = _http_get_conditional(
            url, etag=etag, accept="application/vnd.github+json"
        )
    except urllib.error.HTTPError as e:
        if e.code in (403, 429) and stale is not None:
            # Rate-limited but we have stale data — serve it. The user
            # would rather see slightly-out-of-date info than a dead UI.
            _cache_refresh_ttl(key, ttl_s=60)  # short re-arm so we retry soon
            return stale.get("value")
        raise
    if status == 304 and stale is not None:
        _cache_refresh_ttl(key, ttl_s=ttl_s)
        return stale.get("value")
    if status == 200 and body is not None:
        try:
            value = json.loads(body.decode("utf-8"))
        except Exception:
            value = None
        _cache_put(key, value, etag=new_etag, ttl_s=ttl_s)
        return value
    return None


def _conditional_fetch_text(url: str, key: str, ttl_s: int = _REMOTE_CACHE_TTL_S) -> str | None:
    """Like _conditional_fetch_json, but returns the raw text body.

    Used for raw.githubusercontent.com payloads where we want to
    parse a particular field (plugin.json's `version`) outside the
    cache layer.
    """
    fresh = _cache_get(key)
    if fresh is not None:
        return fresh
    stale = _cache_entry(key)
    etag = stale.get("etag") if stale else None
    try:
        status, body, new_etag = _http_get_conditional(url, etag=etag)
    except urllib.error.HTTPError as e:
        if e.code in (403, 429) and stale is not None:
            _cache_refresh_ttl(key, ttl_s=60)
            return stale.get("value")
        raise
    if status == 304 and stale is not None:
        _cache_refresh_ttl(key, ttl_s=ttl_s)
        return stale.get("value")
    if status == 200 and body is not None:
        try:
            text = body.decode("utf-8")
        except Exception:
            text = None
        _cache_put(key, text, etag=new_etag, ttl_s=ttl_s)
        return text
    return None


def _latest_sha_cached(owner: str, repo: str, branch: str) -> str | None:
    key = f"sha:{owner}/{repo}@{branch}"
    data = _conditional_fetch_json(
        f"https://api.github.com/repos/{owner}/{repo}/commits/{_quote_ref(branch)}",
        key=key,
    )
    if isinstance(data, dict):
        return data.get("sha")
    return None


def _read_local_version(plugin_dir: Path) -> str | None:
    """Return the `version` field from a plugin's local plugin.json, or None."""
    manifest = plugin_dir / "plugin.json"
    if not manifest.exists():
        return None
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        v = data.get("version") if isinstance(data, dict) else None
        return v if isinstance(v, str) and v else None
    except Exception:
        return None


def _fetch_remote_version(owner: str, repo: str, ref: str) -> str | None:
    """Fetch plugin.json at `ref` (branch / tag / sha) and return its version, or None.

    Uses raw.githubusercontent.com via the conditional-fetch helper so
    refreshes after the cache TTL are free under GitHub's rate limit
    (304 responses to If-None-Match don't count). On rate-limit hits
    we serve stale data rather than failing the call — the version
    field rarely changes minute-to-minute.
    """
    if not ref:
        return None
    key = f"version:{owner}/{repo}@{ref}"
    try:
        text = _conditional_fetch_text(
            f"https://raw.githubusercontent.com/{owner}/{repo}/{_quote_ref(ref)}/plugin.json",
            key=key,
        )
    except Exception:
        return None
    if not text:
        return None
    try:
        data = json.loads(text)
        v = data.get("version") if isinstance(data, dict) else None
        return v if isinstance(v, str) and v else None
    except Exception:
        return None


def _list_versions(owner: str, repo: str, branch: str, limit: int = 30) -> list[dict]:
    """Return available versions for a plugin, ordered newest-first.

    Each entry: {ref, sha, version, source} where:
      - `ref` is the GitHub-resolvable identifier (refs/tags/X, sha, or branch)
      - `source` is "tag" (from git tags) or "history" (from plugin.json bump
        commits on the default branch).

    Tags are listed first; then plugin.json version-bump commits on the
    branch supplement them so plugins that don't tag their releases still
    expose their semver history. Stops after `limit` total entries to
    keep within GitHub's unauthenticated rate limit.
    """
    cache_key = f"versions:{owner}/{repo}@{branch}:{limit}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    versions: list[dict] = []
    seen_versions: set[str] = set()
    rate_limited = False

    def _is_rate_limit(err: BaseException) -> bool:
        return isinstance(err, urllib.error.HTTPError) and err.code in (403, 429)

    # 1. Git tags. One API call (cached + conditional via ETag, so
    #    refreshes after TTL are free under the rate limit).
    try:
        tags = _conditional_fetch_json(
            f"https://api.github.com/repos/{owner}/{repo}/tags?per_page={limit}",
            key=f"tags:{owner}/{repo}",
            ttl_s=_VERSIONS_CACHE_TTL_S,
        )
        for t in (tags or []):
            sha = ((t or {}).get("commit") or {}).get("sha")
            tag_name = (t or {}).get("name")
            if not (sha and tag_name):
                continue
            v = _fetch_remote_version(owner, repo, f"refs/tags/{tag_name}")
            entry = {
                "ref": f"refs/tags/{tag_name}",
                "sha": sha,
                "version": v,
                "source": "tag",
                "label": tag_name,
            }
            versions.append(entry)
            if v:
                seen_versions.add(v)
            if len(versions) >= limit:
                _cache_put(cache_key, versions, ttl_s=_VERSIONS_CACHE_TTL_S)
                return versions
    except Exception as e:
        if _is_rate_limit(e):
            rate_limited = True

    # 2. Scan plugin.json bump commits on the branch. One API call lists
    #    all commits that touched plugin.json; for each, one extra fetch
    #    reads the version. Cap to keep the worst-case unauthenticated
    #    rate-limit cost bounded.
    remaining = max(0, limit - len(versions))
    if remaining <= 0:
        _cache_put(cache_key, versions, ttl_s=_VERSIONS_CACHE_TTL_S)
        return versions
    try:
        commits = _conditional_fetch_json(
            f"https://api.github.com/repos/{owner}/{repo}/commits"
            f"?path=plugin.json&sha={branch}&per_page={remaining}",
            key=f"plugincommits:{owner}/{repo}@{branch}:{remaining}",
            ttl_s=_VERSIONS_CACHE_TTL_S,
        )
        for c in (commits or []):
            sha = (c or {}).get("sha")
            if not sha:
                continue
            v = _fetch_remote_version(owner, repo, sha)
            if not v or v in seen_versions:
                continue
            seen_versions.add(v)
            versions.append({
                "ref": sha,
                "sha": sha,
                "version": v,
                "source": "history",
                "label": v,
            })
            if len(versions) >= limit:
                break
    except Exception as e:
        if _is_rate_limit(e):
            rate_limited = True

    if rate_limited and not versions:
        # Distinguish "no versioned releases exist" from "GitHub
        # rate-limited us" — the UI shows different copy for each.
        # Don't cache rate-limit failures so the next attempt can
        # succeed once the limit resets.
        raise RuntimeError("rate_limited")
    _cache_put(cache_key, versions, ttl_s=_VERSIONS_CACHE_TTL_S)
    return versions


def _download_and_replace(owner: str, repo: str, ref: str, target: Path, preserve_git: bool) -> None:
    """Download repo zip at `ref` and atomically replace `target` dir contents.

    `ref` is anything codeload.github.com accepts after the trailing slash
    of `/zip/`: a branch path (`refs/heads/main`), a tag path
    (`refs/tags/v1.0.0`), or a commit sha. The legacy callers passed
    branch names without the `refs/heads/` prefix; preserve that
    behaviour by prepending it when the caller didn't.
    """
    if not (ref.startswith("refs/") or len(ref) >= 7 and all(c in "0123456789abcdef" for c in ref.lower())):
        # Bare branch name → expand to full ref so codeload finds it.
        ref = f"refs/heads/{ref}"
    url = f"https://codeload.github.com/{owner}/{repo}/zip/{_quote_ref(ref)}"
    data = _http_get(url, timeout=120)
    zf = zipfile.ZipFile(io.BytesIO(data))
    members = [m for m in zf.namelist() if not m.endswith("/")]
    if not members:
        raise RuntimeError("Empty archive")
    prefix = members[0].split("/")[0] + "/"

    staging = Path(tempfile.mkdtemp(prefix="slopsmith-plugin-", dir=str(target.parent)))
    try:
        for m in members:
            if not m.startswith(prefix):
                continue
            rel = m[len(prefix):]
            if not rel or ".." in Path(rel).parts:
                continue
            out = staging / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(zf.read(m))

        if preserve_git and (target / ".git").exists():
            shutil.move(str(target / ".git"), str(staging / ".git"))

        backup = target.with_name(target.name + ".bak")
        if target.exists():
            if backup.exists():
                shutil.rmtree(backup, ignore_errors=True)
            shutil.move(str(target), str(backup))
        shutil.move(str(staging), str(target))
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _resolve_source(plugin_dir: Path, force_registry: bool = False) -> dict | None:
    """Return {'owner','repo','branch','local_sha','local_version','source'} or None.

    When BOTH a marker and a .git/ exist (cloned plugin that was later
    UI-zip-updated, or zip-installed plugin that was later git-cloned
    on top), pick whichever has the more recent local provenance:

      - marker freshness = `installed_at` in .slopsmith-installed.json
        (falls back to the marker file's own mtime when `installed_at`
        is missing or malformed)
      - git freshness    = mtime of the actual file _read_git_local_sha
        resolved the sha from. That's:
          .git/refs/heads/<branch>   for loose-ref checkouts
          .git/packed-refs           for repos using packed-refs
          .git/HEAD                  for detached-HEAD checkouts
        Using the actual sha-source file matters: `git pull` rewrites
        whichever of these holds the branch tip, so its mtime is the
        one that advances on a fetch.

    This avoids two symmetric "stuck local_sha" failure modes:
      - Without freshness: a `git pull` would advance HEAD past the
        marker's sha, but the marker still wins → "Update available"
        forever even after the user is on the latest commit.
      - With naive "git always wins": a UI-triggered zip update
        rewrites the marker but leaves .git/refs/heads/<branch> at
        its old commit (preserve_git=True), so the git path would
        report the stale sha.

    Fall back to the existing single-source paths (and finally to
    manifest / registry resolution) when only one or neither is
    available.

    `force_registry=True` re-fetches the slopsmith README registry
    even if a cached copy is present — used by the per-plugin Check
    button so a registry fetch that failed on the cold pass can be
    retried on demand.
    """
    local_version = _read_local_version(plugin_dir)
    marker = _read_marker(plugin_dir)
    git_origin = _read_git_origin(plugin_dir)

    candidates: list[dict] = []

    if marker:
        owner, repo = _parse_repo_url(marker.get("url", ""))
        if owner:
            # Defensive parse — the marker file is on disk and could
            # carry a corrupted / non-numeric `installed_at` from an
            # older format or hand-edit. On any parse failure, fall
            # back to the marker file's mtime so freshness ordering
            # still works for the common "last write" semantics.
            # Kept as float so sub-second resolution survives the
            # `max()` tiebreak.
            installed_at = marker.get("installed_at")
            try:
                marker_freshness: float = float(installed_at)
            except (TypeError, ValueError):
                try:
                    marker_freshness = float((plugin_dir / MARKER).stat().st_mtime)
                except Exception:
                    marker_freshness = 0.0
            candidates.append({
                "owner": owner, "repo": repo,
                "branch": marker.get("branch"),
                "local_sha": marker.get("sha"),
                "local_version": local_version,
                "source": "zip",
                "_freshness": marker_freshness,
            })

    if git_origin:
        owner, repo = _parse_repo_url(git_origin)
        if owner:
            local_sha, branch, sha_source = _read_git_local_sha(plugin_dir)
            # Always append the candidate when origin parses, even if
            # local_sha couldn't be read. Falling through here would
            # leave the plugin "source unknown" and break update flows
            # for repos with unusual gitdir layouts / transient ref
            # corruption — the previous code path returned a git
            # candidate with local_sha=None in that case and we
            # preserve that.
            git_freshness: float = 0.0
            if sha_source is not None:
                try:
                    git_freshness = float(sha_source.stat().st_mtime)
                except Exception:
                    git_freshness = 0.0
            # Stat the same file the sha was actually read from so
            # the mtime tracks the last advance of THIS sha. That's
            # .git/refs/heads/<branch> for loose refs (handles
            # multi-slash names like feature/foo correctly),
            # .git/packed-refs for packed refs (which IS the file
            # `git pull` rewrites in that case — .git/HEAD wouldn't
            # move when staying on the same branch), or .git/HEAD
            # for detached-HEAD checkouts.
            candidates.append({
                "owner": owner, "repo": repo,
                "branch": branch,
                "local_sha": local_sha,
                "local_version": local_version,
                "source": "git",
                "_freshness": git_freshness,
            })

    if candidates:
        best = max(candidates, key=lambda c: c["_freshness"])
        return {k: v for k, v in best.items() if k != "_freshness"}
    # Plugin has neither a marker nor a .git/ — typical for the bundled
    # Electron desktop install where each plugin is a plain directory.
    # Try two fallbacks before giving up:
    #
    #   a) plugin.json's optional `url` / `repository` field.
    #   b) The Available Plugins registry in slopsmith's README, keyed by
    #      directory name. This is what makes the bundled Electron case
    #      work — the bundled install ships plain directories, but as
    #      long as the directory name matches a registry entry we can
    #      still resolve the upstream repo and detect updates.
    manifest = plugin_dir / "plugin.json"
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            url = data.get("url") or data.get("repository") or ""
            if isinstance(url, str):
                owner, repo = _parse_repo_url(url)
                if owner:
                    return {
                        "owner": owner, "repo": repo,
                        "branch": None,
                        "local_sha": None,
                        "local_version": local_version,
                        "source": "manifest",
                    }
        except Exception:
            pass
    try:
        for entry in _registry_cached(force=force_registry):
            if entry.get("dirname") == plugin_dir.name:
                owner, repo = _parse_repo_url(entry.get("url") or "")
                if owner:
                    return {
                        "owner": owner, "repo": repo,
                        "branch": None,
                        "local_sha": None,
                        "local_version": local_version,
                        "source": "registry",
                    }
    except Exception:
        pass
    #   c) Last-resort heuristic: most slopsmith plugins live at
    #      github.com/byrongamatos/slopsmith-plugin-<dirname> with the
    #      dir-name underscores swapped for dashes. Probe that guess via
    #      raw.githubusercontent.com (no api.github.com call) and accept
    #      it only if the fetched plugin.json's `id` matches this
    #      plugin's manifest id — that confirms we found the right repo
    #      rather than a name collision. Covers plugins not (yet) listed
    #      in the README's Available Plugins table.
    local_id = _read_manifest_id(plugin_dir)
    if local_id:
        guess_repo = "slopsmith-plugin-" + plugin_dir.name.replace("_", "-")
        for owner in ("byrongamatos",):
            for branch in ("main", "master"):
                try:
                    text = _conditional_fetch_text(
                        f"https://raw.githubusercontent.com/{owner}/{guess_repo}/{branch}/plugin.json",
                        key=f"version:{owner}/{guess_repo}@{branch}",
                    )
                except Exception:
                    text = None
                if not text:
                    continue
                try:
                    remote_id = (json.loads(text) or {}).get("id")
                except Exception:
                    remote_id = None
                if remote_id == local_id:
                    return {
                        "owner": owner, "repo": guess_repo,
                        "branch": branch,
                        "local_sha": None,
                        "local_version": local_version,
                        "source": "heuristic",
                    }
    return None


def _read_manifest_id(plugin_dir: Path) -> str | None:
    """Return the `id` field from a plugin's plugin.json, or None."""
    manifest = plugin_dir / "plugin.json"
    if not manifest.exists():
        return None
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        v = data.get("id") if isinstance(data, dict) else None
        return v if isinstance(v, str) and v else None
    except Exception:
        return None


def _version_tuple(v: str | None) -> tuple | None:
    """Parse a version string into a comparable numeric tuple.

    Handles the common shapes:
      "1.8.2"          → (1, 8, 2)
      "v1.8.2"         → (1, 8, 2)        leading v/V stripped
      "1.0.0-beta2"    → (1, 0, 0)        pre-release/build suffix dropped
      "1.0.0+build.7"  → (1, 0, 0)        (so a beta and its release compare
                                           equal — we'd miss the beta→release
                                           bump in the bulk pass but the
                                           per-plugin Check still catches it)

    Returns None when the numeric core still doesn't parse (e.g.
    "rolling", "" , "2024-01"). The sole caller, `_remote_is_newer`,
    treats a None on either side as "ordering unknown" and does NOT
    flag an update — see its docstring for the rationale.
    """
    if not isinstance(v, str) or not v:
        return None
    s = v.strip()
    if s[:1] in ("v", "V"):
        s = s[1:]
    # Drop a pre-release ("-beta2") or build ("+sha") suffix; keep the
    # dotted-numeric core that precedes it.
    for sep in ("-", "+"):
        if sep in s:
            s = s.split(sep, 1)[0]
    parts = s.split(".")
    out = []
    for p in parts:
        if not p.isdigit():
            return None
        out.append(int(p))
    return tuple(out) if out else None


def _remote_is_newer(local_version: str | None, remote_version: str | None) -> bool:
    """True iff remote_version is a strictly-newer release than local.

    Numeric comparison when both `_version_tuple()`-parse (covers
    plain "X.Y.Z", "vX.Y.Z", and pre-release/build-tagged forms via
    suffix-stripping). When a side genuinely won't parse as numeric,
    we DON'T flag an update — a false negative is recoverable on
    demand via the per-plugin Check button (which does SHA-based
    detection), whereas a false positive ("update available" pointing
    at an older or sideways version) is annoying and can lead to a
    failed Update. So when in doubt: stay quiet.
    """
    if not (local_version and remote_version):
        return False
    if local_version == remote_version:
        return False
    lt, rt = _version_tuple(local_version), _version_tuple(remote_version)
    if lt is not None and rt is not None:
        return rt > lt
    # Non-numeric scheme on at least one side — can't reliably order
    # them, so don't flag. The per-plugin Check button will catch a
    # real update via the SHA comparison.
    return False


def _remote_version_any_branch(owner: str, repo: str, hint_branch: str | None = None) -> tuple[str | None, str | None, str]:
    """Read plugin.json's `version` from the repo via raw.githubusercontent.com.

    Branch selection:
      - hint_branch given (a marker/git-tracked plugin): use ONLY that
        branch. Don't silently fall back to main/master — the tracked
        branch IS the source of truth, and `apply_update` will pull
        from it; falling back here would report an update against a
        branch the Update flow won't actually use.
      - hint_branch absent (a registry/heuristic/manifest-resolved
        plugin where we don't actually know the branch): try 'main'
        then 'master'.

    Every fetch goes to raw.githubusercontent.com — there are NO
    api.github.com calls here, so this never consumes the
    unauthenticated 60/hour API rate limit no matter how many plugins
    we check.

    Returns (version, branch_that_worked, status):
      status="ok"          version + branch are valid
      status="not_found"   raw.githubusercontent.com returned 404/410
                           for .../<branch>/plugin.json. This is
                           AMBIGUOUS — it can mean the branch doesn't
                           exist, the repo doesn't exist, OR plugin.json
                           simply isn't on that branch. Callers must
                           NOT claim a definite "branch not on remote";
                           use a `manifest_not_found`-style code. (The
                           full-check path proves a true branch-missing
                           condition separately via a 422 from the
                           commits API.)
      status="no_version"  plugin.json was fetched but has no usable
                           `version` field (malformed manifest).
      status="error"       transient failure (5xx, network, rate-limit
                           with no stale copy) — retryable. Caller
                           should NOT report it as "branch not there".
    On any non-ok status, version is None; branch is the hint (so the
    caller knows what we tried) or None when there was no hint.

    When more than one candidate branch is tried (the no-hint case:
    main then master) and they fail with different statuses, the
    returned status is the most-actionable one by precedence
    error > no_version > not_found — so e.g. "main exists but its
    manifest has no version, master 404s" reports `no_version`, not
    `not_found`.
    """
    candidates: list[str] = [hint_branch] if hint_branch else ["main", "master"]
    saw_error = False
    saw_no_version = False
    for b in candidates:
        try:
            text = _conditional_fetch_text(
                f"https://raw.githubusercontent.com/{owner}/{repo}/{_quote_ref(b)}/plugin.json",
                key=f"version:{owner}/{repo}@{b}",
            )
        except urllib.error.HTTPError as e:
            if e.code not in (404, 410):
                saw_error = True
            continue
        except Exception:
            saw_error = True
            continue
        if not text:
            # _conditional_fetch_text returned None for a non-2xx it
            # didn't classify (no body, no stale copy) — treat as transient.
            saw_error = True
            continue
        try:
            v = (json.loads(text) or {}).get("version")
        except Exception:
            v = None
        if isinstance(v, str) and v:
            return v, b, "ok"
        saw_no_version = True
    status = "error" if saw_error else ("no_version" if saw_no_version else "not_found")
    return None, (hint_branch or None), status


# `_check_one` runs in two modes (see the `bulk` flag below):
#
#   bulk=True  — used by the BULK /updates pass over every plugin.
#     Makes ZERO api.github.com calls. Branch selection: a
#     marker/git-tracked plugin uses ONLY its tracked branch (the
#     source of truth `apply_update` will pull from); a plugin with
#     no tracked branch (registry/heuristic-resolved) tries 'main'
#     then 'master'. All branch probes go to raw.githubusercontent.com.
#     Updates are detected purely by comparing local plugin.json
#     `version` against the remote `version` (semver-aware via
#     `_remote_is_newer`). The remote commit SHA is NOT fetched — it
#     isn't needed for detection, and `apply_update` fetches a fresh
#     SHA when the user actually clicks Update. This is the guarantee
#     that the cold first-run pass can never exhaust the rate-limit
#     budget: a 30-plugin install costs 0 api.github.com calls,
#     leaving the entire 60/hour budget for on-demand actions.
#
#   bulk=False — used by the per-plugin /check/{plugin_id} endpoint.
#     Does the FULL check: resolves the default branch via
#     api.github.com if the marker doesn't pin one, fetches the remote
#     tip SHA, and does SHA-based detection in addition to version
#     comparison. Costs 1–2 api.github.com calls — affordable because
#     the bulk pass spent nothing, so the user can always re-check (and
#     update) any individual external plugin even right after the cold
#     pass.
#
# Trade-off (bulk mode): a plugin whose author commits new code
# without bumping plugin.json `version` looks "up to date" in the
# bulk list. The per-plugin Check button (bulk=False) catches that
# via the SHA comparison, so it's recoverable on demand.
def _check_one(name: str, info: dict, bulk: bool = True) -> tuple[str, dict | None, dict | None, dict]:
    """Returns (name, result_entry_or_None, error_entry_or_None, source_updates).

    Module-level so both the bulk `/updates` parallel pool and the
    per-plugin `/check/{plugin_id}` endpoint can invoke it. `info` is the
    output of `_resolve_source(plugin_dir)`.
    """
    owner, repo = info["owner"], info["repo"]
    local_sha = info.get("local_sha")
    local_version = info.get("local_version")
    hint_branch = info.get("branch")  # may be None for marker-less plugins

    if bulk:
        # ── Bulk pass: version-only, zero api.github.com calls. ──
        remote_version, branch, status = _remote_version_any_branch(owner, repo, hint_branch)
        source_updates: dict = {"remote_version": remote_version}
        # Only record a resolved branch when we DIDN'T have a hint —
        # for hint-tracked plugins the marker's branch stays
        # authoritative (we never fall back off it in bulk mode).
        if branch and not hint_branch:
            source_updates["branch"] = branch
        if remote_version is None:
            tried = "main, master" if not hint_branch else f"'{branch}'"
            if status == "not_found":
                # A raw 404/410 on .../<branch>/plugin.json is ambiguous:
                # it could mean the branch doesn't exist, the REPO
                # doesn't exist, or plugin.json simply isn't on that
                # branch. Bulk mode (raw fetches only) can't tell which.
                # So use a `manifest_not_found` code with an honest,
                # still-actionable message rather than the stronger
                # `branch_not_on_remote` claim — which `_check_one`'s
                # full-check (bulk=False) path DOES emit, but only on a
                # 422 from the commits API, where the evidence is solid.
                # `apply_update` would still pull `branch` either way, so
                # this is the right thing for the user to act on.
                where = f"at {owner}/{repo}@{branch}" if hint_branch else f"at {owner}/{repo} (tried main, master)"
                return name, None, {
                    "code": "manifest_not_found",
                    "branch": branch,
                    "message": f"plugin.json not found {where} — the branch, repo, or manifest file may be missing",
                }, source_updates
            if status == "no_version":
                # The manifest was fetched fine — it just has no usable
                # `version` field. Distinct code so the UI doesn't tell
                # the user it's a connectivity problem; nothing to
                # retry until the plugin author fixes the manifest.
                return name, None, {
                    "code": "manifest_no_version",
                    "message": f"plugin.json on {owner}/{repo} ({tried}) has no version field",
                }, source_updates
            # status == "error": transient (5xx, network, rate-limit
            # with no stale copy). Retryable; the Check button is the
            # obvious next step. We don't slander a fine branch as
            # unpublished over a blip.
            return name, None, {
                "code": "version_unavailable",
                "message": f"Couldn't read plugin.json version from {owner}/{repo} ({tried}) — transient fetch error, retry",
            }, source_updates
        if _remote_is_newer(local_version, remote_version):
            return name, {
                "local": (local_sha or "")[:7],
                "remote": "",  # not fetched in bulk mode — apply_update resolves it
                "local_version": local_version,
                "remote_version": remote_version,
                "branch": branch,
                "source": info["source"],
                "repo": f"{owner}/{repo}",
            }, None, source_updates
        # Versions equal, local ahead, or no local_version → up to date.
        return name, None, None, source_updates

    # ── Full check (per-plugin /check endpoint): allowed to spend
    #    api.github.com calls. ──
    branch = hint_branch or _default_branch(owner, repo)
    source_updates = {"branch": branch} if not hint_branch else {}

    # Step 1: cheap version probe via raw.githubusercontent.com.
    remote_version = None
    if local_version:
        try:
            remote_version = _fetch_remote_version(owner, repo, branch)
        except Exception:
            remote_version = None
    source_updates["remote_version"] = remote_version

    # Step 2: decide if we need the api.github.com sha lookup.
    # When local_version == remote_version and we already have a
    # local_sha, the shas must match too (the manifest is
    # version-identical with the remote, and any sha-changing
    # commit would have bumped version) — display them as equal
    # without burning a rate-limit slot on the api call.
    versions_match = (
        local_version
        and remote_version
        and local_version == remote_version
    )
    need_sha = not local_sha or not versions_match
    remote_sha = None
    if need_sha:
        try:
            remote_sha = _latest_sha_cached(owner, repo, branch)
        except urllib.error.HTTPError as e:
            if e.code == 422:
                return name, None, {
                    "code": "branch_not_on_remote",
                    "branch": branch,
                    "message": f"Local branch '{branch}' not on {owner}/{repo}",
                }, source_updates
            return name, None, {
                "code": "http",
                "message": f"HTTP {e.code}: {e.reason}",
            }, source_updates
        except Exception as e:
            return name, None, {"code": "error", "message": str(e)}, source_updates
    else:
        # Versions match + we have a local sha → declare them equal
        # without spending an api.github.com call.
        remote_sha = local_sha

    # If we got here without a remote_version (because we had no
    # local_version to short-circuit with), fetch it now. This
    # matters for the version-fallback update detection below
    # and for the UI "X → Y" display when local_sha is missing.
    if remote_version is None and (need_sha and remote_sha):
        try:
            remote_version = _fetch_remote_version(owner, repo, branch)
        except Exception:
            remote_version = None
        source_updates["remote_version"] = remote_version

    sha_update = local_sha and remote_sha and remote_sha != local_sha
    ver_update = (not local_sha) and _remote_is_newer(local_version, remote_version)
    if sha_update or ver_update:
        return name, {
            "local": (local_sha or "")[:7],
            "remote": (remote_sha or "")[:7],
            "local_version": local_version,
            "remote_version": remote_version,
            "branch": branch,
            "source": info["source"],
            "repo": f"{owner}/{repo}",
        }, None, source_updates
    return name, None, None, source_updates


# Registry parsed from slopsmith's README. The raw README fetch goes
# through _conditional_fetch_text so refreshes after TTL are free
# (304 responses don't count against the rate limit). Parsing happens
# every time we hit the cache because the parsed dict isn't itself
# cached — but that's a few µs of regex work, dwarfed by network cost.
_REGISTRY_CACHE_KEY = "registry:slopsmith-readme"


def _registry_cached(force: bool = False) -> list[dict]:
    if force:
        # Drop to a stale state so the next fetch does a conditional
        # GET. Recovers from a registry fetch that failed on the cold
        # pass (network blip, transient 5xx) — the per-plugin Check
        # button passes force=True so the user can retry resolution.
        _cache_expire(_REGISTRY_CACHE_KEY)
    try:
        md = _conditional_fetch_text(
            REGISTRY_URL, key=_REGISTRY_CACHE_KEY, ttl_s=_VERSIONS_CACHE_TTL_S,
        )
    except Exception:
        md = None
    if not md:
        return []
    return _parse_registry(md)


def _load_core_marker() -> dict | None:
    if not CORE_MARKER_FILE.exists():
        return None
    try:
        return json.loads(CORE_MARKER_FILE.read_text())
    except Exception:
        return None


def _save_core_marker(sha: str, branch: str) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CORE_MARKER_FILE.write_text(json.dumps({
        "repo": f"{CORE_REPO_OWNER}/{CORE_REPO_NAME}",
        "url": f"https://github.com/{CORE_REPO_OWNER}/{CORE_REPO_NAME}",
        "branch": branch,
        "sha": sha,
        "installed_at": int(time.time()),
    }, indent=2))


def _core_changed_files(local_sha: str, remote_sha: str) -> list[dict]:
    """GitHub compare API. Returns [{filename, status}, ...]."""
    url = (
        f"https://api.github.com/repos/{CORE_REPO_OWNER}/{CORE_REPO_NAME}"
        f"/compare/{local_sha}...{remote_sha}"
    )
    data = _http_json(url)
    files = data.get("files") or []
    return [{"filename": f.get("filename", ""), "status": f.get("status", "")} for f in files]


def _is_ignored(path: str) -> bool:
    """Check if path matches any pattern in CORE_IGNORED_PATHS, supporting fnmatch wildcards."""
    top = path.split("/", 1)[0]
    for pattern in CORE_IGNORED_PATHS:
        if fnmatch.fnmatch(top, pattern):
            return True
    return False


def _classify_core_changes(files: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split changed files into (writable_to_mount, blockers).

    Files under CORE_IGNORED_PATHS are omitted from both lists (plugins/
    is managed separately; touching it here would clobber user installs).
    """
    writable, blockers = [], []
    for f in files:
        name = f.get("filename", "")
        if not name:
            continue
        if _is_ignored(name):
            continue
        top = name.split("/", 1)[0]
        if top in CORE_MOUNTED_PATHS:
            writable.append(f)
        else:
            blockers.append(f)
    return writable, blockers


def _download_core_stage(branch: str) -> tuple[Path, str]:
    """Download the core repo zip, extract to a tempdir, return (stage_root, prefix)."""
    url = f"https://codeload.github.com/{CORE_REPO_OWNER}/{CORE_REPO_NAME}/zip/{_quote_ref(f'refs/heads/{branch}')}"
    data = _http_get(url, timeout=180)
    zf = zipfile.ZipFile(io.BytesIO(data))
    members = [m for m in zf.namelist() if not m.endswith("/")]
    if not members:
        raise RuntimeError("Empty core archive")
    prefix = members[0].split("/")[0] + "/"
    stage = Path(tempfile.mkdtemp(prefix="slopsmith-core-"))
    for m in members:
        if not m.startswith(prefix):
            continue
        rel = m[len(prefix):]
        if not rel or ".." in Path(rel).parts:
            continue
        if _is_ignored(rel):
            continue
        top = rel.split("/", 1)[0]
        if top not in CORE_MOUNTED_PATHS:
            continue
        out = stage / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(zf.read(m))
    return stage, prefix


def _overlay_core(stage: Path) -> list[str]:
    """Non-destructive copy from stage into APP_ROOT.

    For every file under the staged mount-whitelist tree, write it into
    the matching location under /app, creating parent dirs as needed.
    Never delete existing files that are absent from the stage — this
    preserves runtime artifacts (e.g. static/audio_*.mp3, __pycache__/).
    Returns the list of relative paths written.
    """
    written: list[str] = []
    for src in stage.rglob("*"):
        if not src.is_file():
            continue
        rel = src.relative_to(stage)
        dst = APP_ROOT / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(dst))
        written.append(str(rel).replace("\\", "/"))
    return written


def _self_update(owner: str, repo: str, ref: str, sha: str) -> dict:
    """Download new version to staging and mark for pending restart.

    Returns {"ok": true, "pending_restart": true} so the UI can prompt
    the user to restart. On restart, _apply_pending_self_update will
    swap the files before re-execing the server. `ref` accepts the same
    forms as _download_and_replace (branch / refs/tags/X / sha).
    """
    staging = SELF_UPDATE_STAGING
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)

    if not (ref.startswith("refs/") or len(ref) >= 7 and all(c in "0123456789abcdef" for c in ref.lower())):
        ref = f"refs/heads/{ref}"
    url = f"https://codeload.github.com/{owner}/{repo}/zip/{_quote_ref(ref)}"
    data = _http_get(url, timeout=120)
    zf = zipfile.ZipFile(io.BytesIO(data))
    members = [m for m in zf.namelist() if not m.endswith("/")]
    if not members:
        return {"error": "Empty archive"}
    prefix = members[0].split("/")[0] + "/"

    for m in members:
        if not m.startswith(prefix):
            continue
        rel = m[len(prefix):]
        if not rel or ".." in Path(rel).parts:
            continue
        out = staging / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(zf.read(m))

    marker = staging / ".self_update_pending"
    marker.write_text(json.dumps({
        "owner": owner, "repo": repo,
        "ref": ref, "sha": sha,
        "staged_at": int(time.time()),
    }, indent=2))
    return {"ok": True, "pending_restart": True, "sha": sha[:7], "ref": ref}


def _apply_pending_self_update(target: Path) -> bool:
    """Swap staged self-update files into target dir. Returns True if swap occurred."""
    marker = SELF_UPDATE_STAGING / ".self_update_pending"
    if not marker.exists():
        return False
    staging = SELF_UPDATE_STAGING
    if not staging.exists():
        return False

    for src in staging.rglob("*"):
        if src.name.startswith("."):
            continue
        if not src.is_file():
            continue
        rel = src.relative_to(staging)
        dst = target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(dst))
    shutil.rmtree(staging, ignore_errors=True)
    return True


def setup(app, context):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # After a manual `git pull + docker compose build + up`, the source files
    # are bind-mounted from the updated host but the persistent marker still
    # holds the old SHA. Detect this by reading the live git HEAD (available
    # via the .git mount in docker-compose.yml) and auto-advance the marker.
    # Docker-only: the desktop app ships without /app/.git and doesn't use
    # core-update tracking at all.
    if not IS_DESKTOP:
        try:
            live_sha, live_branch, _ = _read_git_local_sha(APP_ROOT)
            if live_sha:
                marker = _load_core_marker()
                if marker and marker.get("sha") != live_sha:
                    _save_core_marker(live_sha, live_branch or marker.get("branch", "main"))
                    context["log"].info(
                        "core marker auto-advanced to %s (detected manual rebuild)", live_sha[:7]
                    )
        except Exception:
            pass

    @app.get("/api/plugins/update_manager/config")
    def get_config():
        return {"is_desktop": IS_DESKTOP}

    @app.get("/api/plugins/update_manager/registry")
    def registry():
        try:
            md = _http_get(REGISTRY_URL, timeout=15).decode("utf-8")
        except Exception as e:
            return {"error": f"Failed to fetch registry: {e}"}
        entries = _parse_registry(md)
        installed = _installed_plugin_dirs()
        installed_dirs = {p.name for p in installed.values()}
        bundled_dirs = {p.name for p in installed.values() if _is_bundled(p)}
        for e in entries:
            e["installed"] = e["dirname"] in installed_dirs
            # Surface dirname-collision with a bundled plugin so the UI
            # can prompt before installing an override. Doesn't catch the
            # case where the registry entry's install command targets a
            # different directory than the bundled one — that requires
            # an `upstream` annotation that isn't standardised yet.
            e["overrides_bundled"] = e["dirname"] in bundled_dirs
        return {"count": len(entries), "entries": entries, "bundled_dirs": sorted(bundled_dirs)}

    @app.post("/api/plugins/update_manager/install")
    async def install(body: dict):
        url = (body.get("url") or "").strip()
        dirname = (body.get("dirname") or "").strip()
        if not SLUG_RE.match(dirname):
            return {"error": "Invalid dirname"}
        owner, repo = _parse_repo_url(url)
        if not owner:
            return {"error": "URL must be a GitHub repo"}
        target = PLUGINS_DIR / dirname
        if target.exists():
            return {"error": f"Plugin directory '{dirname}' already exists"}
        try:
            branch = _default_branch(owner, repo)
            sha = _latest_sha(owner, repo, branch)
            if not sha:
                return {"error": "Could not resolve latest commit"}
            _download_and_replace(owner, repo, branch, target, preserve_git=False)
            _write_marker(target, owner, repo, branch, sha)
            return {
                "ok": True, "dirname": dirname, "branch": branch,
                "sha": sha[:7], "repo": f"{owner}/{repo}",
            }
        except urllib.error.HTTPError as e:
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            return {"error": f"HTTP {e.code}: {e.reason}"}
        except Exception as e:
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            return {"error": str(e)}

    @app.get("/api/plugins/update_manager/updates")
    def check_updates():
        results = {}
        errors = {}
        sources = {}
        bundled = []
        excluded = _load_exclusions()

        # Phase 1 (serial, fast): collect plugin metadata + decide which
        # plugins need remote work. All disk reads, no network. Builds
        # the `to_check` list of (name, owner, repo, branch, info) for
        # phase 2 to fan out.
        to_check: list[tuple] = []
        for name, p in _installed_plugin_dirs().items():
            if _is_bundled(p):
                bundled.append(name)
                sources[name] = {"bundled": True}
                continue
            info = _resolve_source(p)
            if info and info["owner"]:
                sources[name] = {
                    "repo": f"{info['owner']}/{info['repo']}",
                    "url": f"https://github.com/{info['owner']}/{info['repo']}",
                    "branch": info["branch"],
                    "source": info["source"],
                    "local_version": info.get("local_version"),
                }
            if name in excluded:
                continue
            if not info:
                continue
            to_check.append((name, info))

        # Phase 2 (parallel): per-plugin remote fetches. With 30+ plugins
        # the serial cost was ~30 × ~200ms = 6s+ on a cold cache, which
        # users felt as a slow Update Manager screen load. Threading
        # works because the per-plugin work is I/O-bound (GitHub HTTP)
        # and our cache helpers are thread-safe via the global lock.
        # max_workers=10 balances throughput against API politeness.
        # Each future calls module-level _check_one().
        if to_check:
            with ThreadPoolExecutor(max_workers=min(10, len(to_check))) as ex:
                futures = [ex.submit(_check_one, name, info) for name, info in to_check]
                for f in as_completed(futures):
                    name, result_entry, error_entry, source_updates = f.result()
                    if name in sources:
                        sources[name].update(source_updates)
                    if result_entry is not None:
                        results[name] = result_entry
                    if error_entry is not None:
                        errors[name] = error_entry
        return {
            "updates": results,
            "errors": errors,
            "excluded": sorted(excluded),
            "sources": sources,
            "bundled": sorted(bundled),
        }

    @app.get("/api/plugins/update_manager/check/{plugin_id}")
    def check_one_plugin(plugin_id: str):
        """Recheck a single external plugin against GitHub on demand.

        Always available for any non-bundled plugin (the UI's per-row
        "Check" button is never gated away). Unlike the bulk `/updates`
        pass — which is deliberately api.github.com-free so the cold
        first run can't exhaust the rate limit — this does the FULL
        check (resolves the default branch, fetches the remote tip SHA)
        because it's a single plugin and the whole 60/hour budget is
        available for these on-demand actions.

        If the bulk pass couldn't resolve a source for this plugin
        (e.g. the slopsmith README registry fetch failed on the cold
        pass), we force-refresh the registry here and retry resolution
        so the user is never permanently stuck with an unresolvable
        external plugin.

        Mirrors one slice of the bulk `/updates` response so the
        frontend can merge the result into its local
        `updates / errors / sources` dicts without re-running the
        whole batch.
        """
        if not SLUG_RE.match(plugin_id):
            return {"error": "Invalid plugin id"}
        dirs = _installed_plugin_dirs()
        p = dirs.get(plugin_id)
        if not p:
            return {"error": "Plugin not found"}

        excluded_set = _load_exclusions()
        bundled = _is_bundled(p)

        # Mirror Phase 1 (serial metadata) for this single plugin so the
        # response shape lines up with bulk `/updates`'s per-plugin slice.
        source: dict = {}
        info = None
        if bundled:
            source = {"bundled": True}
        else:
            info = _resolve_source(p)
            if not (info and info["owner"]):
                # First resolution attempt found nothing — retry with a
                # forced registry refresh in case the cold-pass fetch
                # failed transiently.
                info = _resolve_source(p, force_registry=True)
            if info and info["owner"]:
                source = {
                    "repo": f"{info['owner']}/{info['repo']}",
                    "url": f"https://github.com/{info['owner']}/{info['repo']}",
                    "branch": info["branch"],
                    "source": info["source"],
                    "local_version": info.get("local_version"),
                }

        result_entry = None
        error_entry = None
        if bundled:
            pass  # bundled plugins are managed by core; nothing to check
        elif plugin_id in excluded_set:
            pass  # excluded — just re-sync the excluded/bundled flags
        elif info:
            # Full check (bulk=False) — allowed to spend api.github.com calls.
            _, result_entry, error_entry, source_updates = _check_one(plugin_id, info, bulk=False)
            source.update(source_updates)
        else:
            error_entry = {
                "code": "source_unresolved",
                "message": (
                    "Couldn't determine this plugin's GitHub repo — no install marker, "
                    "no .git/, not listed in the slopsmith plugin registry, and the "
                    "byrongamatos/slopsmith-plugin-<dir> heuristic didn't match. "
                    "Re-install it via the Browse tab to record its source."
                ),
            }

        return {
            "plugin_id": plugin_id,
            "update": result_entry,
            "error": error_entry,
            "source": source,
            "excluded": plugin_id in excluded_set,
            "bundled": bundled,
        }

    @app.get("/api/plugins/update_manager/start_time")
    def process_start_time():
        """Return when this server process started.

        Used by the frontend to detect restarts that happen outside
        of the in-app "Restart now" flow (docker compose restart,
        host-side process kill, …). When the frontend sees the
        value change, it clears the per-row pending-restart UI that
        would otherwise stick across the external restart.
        """
        return {"started_at": _PROCESS_STARTED_AT}

    @app.get("/api/plugins/update_manager/exclusions")
    def get_exclusions():
        return {"excluded": sorted(_load_exclusions())}

    @app.post("/api/plugins/update_manager/exclusions")
    async def set_exclusion(body: dict):
        plugin_id = (body.get("plugin_id") or "").strip()
        exclude = bool(body.get("excluded"))
        if plugin_id != CORE_EXCLUSION_KEY and not SLUG_RE.match(plugin_id):
            return {"error": "Invalid plugin id"}
        excl = _load_exclusions()
        if exclude:
            excl.add(plugin_id)
        else:
            excl.discard(plugin_id)
        _save_exclusions(excl)
        return {"ok": True, "excluded": sorted(excl)}

    @app.get("/api/plugins/update_manager/versions/{plugin_id}")
    def list_plugin_versions(plugin_id: str):
        """List available versions a plugin can be pinned to.

        Surfaces git tags first (one API call), then plugin.json
        version-bump commits scanned from the default branch (one
        listing call + one fetch per commit, capped to keep within the
        unauthenticated GitHub rate limit). The UI uses this for the
        per-plugin "Versions" picker that supports both upgrade-to-
        specific and downgrade-to-specific flows.
        """
        if not SLUG_RE.match(plugin_id):
            return {"error": "Invalid plugin id"}
        target = _installed_plugin_dirs().get(plugin_id)
        if not target or not target.is_dir():
            return {"error": "Plugin not found"}
        if _is_bundled(target):
            return {
                "error": "Bundled with slopsmith core; version pinning not supported.",
                "bundled": True,
            }
        info = _resolve_source(target)
        if not info:
            return {"error": "Plugin source unknown (no marker, no .git/config)"}
        owner, repo = info["owner"], info["repo"]
        branch = info["branch"] or _default_branch(owner, repo)
        try:
            entries = _list_versions(owner, repo, branch)
            return {
                "plugin_id": plugin_id,
                "current_sha": info.get("local_sha"),
                "current_version": info.get("local_version"),
                "branch": branch,
                "repo": f"{owner}/{repo}",
                "versions": entries,
            }
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                return {"error": "GitHub rate limit hit. Try again in a few minutes.", "rate_limited": True}
            return {"error": f"HTTP {e.code}: {e.reason}"}
        except RuntimeError as e:
            if str(e) == "rate_limited":
                return {"error": "GitHub rate limit hit. Try again in a few minutes.", "rate_limited": True}
            return {"error": str(e)}
        except Exception as e:
            return {"error": str(e)}

    @app.post("/api/plugins/update_manager/update/{plugin_id}")
    async def apply_update(plugin_id: str, request: Request):
        if not SLUG_RE.match(plugin_id):
            return {"error": "Invalid plugin id"}
        if plugin_id in _load_exclusions():
            return {"error": "Plugin is excluded from updates"}
        target = _installed_plugin_dirs().get(plugin_id)
        if not target or not target.is_dir():
            return {"error": "Plugin not found"}
        if _is_bundled(target):
            return {
                "error": "Bundled with slopsmith core; updates ship with the slopsmith app itself.",
                "bundled": True,
            }
        info = _resolve_source(target)
        if not info:
            return {"error": "Plugin source unknown (no marker, no .git/config)"}
        # Optional `ref` in the JSON body lets the caller pin to a
        # specific tag or sha (upgrade or downgrade). Body parsing is
        # tolerant: missing body, empty body, or no `ref` field all
        # mean "update to latest on the tracked branch" — preserving
        # backwards-compatible behaviour for the existing Update button.
        ref = None
        try:
            body = await request.json()
            if isinstance(body, dict):
                rv = body.get("ref")
                if isinstance(rv, str) and rv.strip():
                    ref = rv.strip()
        except Exception:
            pass
        owner, repo = info["owner"], info["repo"]
        branch = info["branch"] or _default_branch(owner, repo)
        try:
            if ref:
                # User-provided ref. Resolve to a sha for the marker
                # write. For tag refs (refs/tags/X) we hit the API to
                # get the tagged commit's sha; for direct shas we
                # accept the value as-is.
                if ref.startswith("refs/tags/"):
                    tag = ref[len("refs/tags/"):]
                    try:
                        tag_data = _http_json(
                            f"https://api.github.com/repos/{owner}/{repo}/git/refs/tags/{urllib.parse.quote(tag, safe='')}"
                        )
                        sha = ((tag_data or {}).get("object") or {}).get("sha")
                    except Exception:
                        sha = None
                    if not sha:
                        return {"error": f"Could not resolve tag {tag!r}"}
                else:
                    sha = ref
                resolved_branch = branch  # remember tracked branch for the marker
            else:
                sha = _latest_sha(owner, repo, branch)
                if not sha:
                    return {"error": "Could not resolve latest commit"}
                ref = branch
                resolved_branch = branch
            if plugin_id == "update_manager":
                return _self_update(owner, repo, ref, sha)
            # preserve_git is gated on the **structural** presence of a
            # .git entry, NOT on info["source"]. After freshness-based
            # source selection in _resolve_source, a git-cloned plugin
            # with a recently-rewritten marker returns source="zip" —
            # using that here would drop the live .git/ directory (or
            # gitdir-file in the worktree / submodule case) and lose
            # the user's clone. Mirror _download_and_replace's own
            # check (`.exists()`) so worktrees and submodules — which
            # store `.git` as a file containing `gitdir: …` rather
            # than a directory — are also preserved.
            preserve_git = (target / ".git").exists()
            _download_and_replace(owner, repo, ref, target, preserve_git=preserve_git)
            _write_marker(target, owner, repo, resolved_branch, sha)
            return {"ok": True, "sha": sha[:7], "branch": resolved_branch, "ref": ref}
        except urllib.error.HTTPError as e:
            return {"error": f"HTTP {e.code}: {e.reason}"}
        except Exception as e:
            return {"error": str(e)}

    @app.post("/api/plugins/update_manager/restart")
    def restart_server():
        """Restart the uvicorn process in-place via os.execv.

        Replaces the current Python process image with a fresh uvicorn,
        same PID. Parent shell (PID 1) sees no child exit, so the
        container stays alive. No docker-compose restart policy needed.
        """
        if IS_DESKTOP:
            # Desktop restart is handled by the Electron renderer via slopsmithDesktop.plugins.restart().
            return {"ok": True, "desktop": True}
        plugin_target = PLUGINS_DIR / "update_manager"
        if _apply_pending_self_update(plugin_target):
            marker = SELF_UPDATE_STAGING / ".self_update_pending"
            marker.unlink(missing_ok=True)

        # Snapshot the original argv before returning (after exec, this
        # function never runs to completion).
        try:
            with open("/proc/self/cmdline", "rb") as f:
                argv = [x.decode("utf-8", "replace") for x in f.read().split(b"\x00") if x]
        except Exception:
            argv = []
        if not argv:
            argv = [sys.executable, "-m", "uvicorn", "server:app",
                    "--host", "0.0.0.0", "--port", "8000"]

        def _do_exec():
            # Small delay so this HTTP response can flush to the client
            # before we replace the process image.
            time.sleep(0.6)
            try:
                os.execv(argv[0], argv)
            except Exception:
                # Fallback to the documented CMD
                try:
                    os.execv(sys.executable,
                             [sys.executable, "-m", "uvicorn", "server:app",
                              "--host", "0.0.0.0", "--port", "8000"])
                except Exception:
                    # Last resort: terminate so the user knows something went wrong
                    os._exit(1)

        threading.Thread(target=_do_exec, daemon=True).start()
        return {"ok": True, "argv": argv}

    @app.get("/api/plugins/update_manager/core")
    def core_status():
        if IS_DESKTOP:
            return {"is_desktop": True, "hidden": True}
        marker = _load_core_marker()
        excluded = CORE_EXCLUSION_KEY in _load_exclusions()
        try:
            branch = (marker or {}).get("branch") or _default_branch(CORE_REPO_OWNER, CORE_REPO_NAME)
        except Exception:
            branch = "main"
        resp: dict = {
            "repo": f"{CORE_REPO_OWNER}/{CORE_REPO_NAME}",
            "url": f"https://github.com/{CORE_REPO_OWNER}/{CORE_REPO_NAME}",
            "branch": branch,
            "tracking": marker is not None,
            "local_sha": (marker or {}).get("sha"),
            "excluded": excluded,
        }
        try:
            remote_sha = _latest_sha(CORE_REPO_OWNER, CORE_REPO_NAME, branch)
        except Exception as e:
            resp["error"] = f"Failed to check remote: {e}"
            return resp
        resp["remote_sha"] = remote_sha
        if not marker or not marker.get("sha"):
            resp["behind"] = False
            return resp
        local_sha = marker["sha"]
        if local_sha == remote_sha:
            resp["behind"] = False
            resp["changed_files"] = []
            resp["blockers"] = []
            return resp
        try:
            changed = _core_changed_files(local_sha, remote_sha)
        except Exception as e:
            resp["behind"] = True
            resp["changed_files"] = []
            resp["blockers"] = []
            resp["compare_error"] = str(e)
            return resp
        writable, blockers = _classify_core_changes(changed)
        resp["behind"] = True
        resp["changed_files"] = writable + blockers
        resp["blockers"] = blockers
        resp["rebuild_required"] = bool(blockers)
        resp["rebuild_command"] = REBUILD_CMD if blockers else None
        return resp

    @app.post("/api/plugins/update_manager/core/init")
    async def core_init(body: dict):
        if IS_DESKTOP:
            return {"error": "Core updates are not supported in the desktop app."}
        sha = ((body or {}).get("sha") or "").strip() if isinstance(body, dict) else ""
        try:
            branch = _default_branch(CORE_REPO_OWNER, CORE_REPO_NAME)
            if not sha:
                sha = _latest_sha(CORE_REPO_OWNER, CORE_REPO_NAME, branch) or ""
            if not sha:
                return {"error": "Could not resolve SHA"}
            _save_core_marker(sha, branch)
            return {"ok": True, "sha": sha[:7], "branch": branch}
        except urllib.error.HTTPError as e:
            return {"error": f"HTTP {e.code}: {e.reason}"}
        except Exception as e:
            return {"error": str(e)}

    @app.post("/api/plugins/update_manager/core/update")
    def core_update():
        if IS_DESKTOP:
            return {"error": "Core updates are not supported in the desktop app."}
        if CORE_EXCLUSION_KEY in _load_exclusions():
            return {"error": "Core is excluded from updates"}
        marker = _load_core_marker()
        if not marker or not marker.get("sha"):
            return {"error": "Core tracking not initialized. Click 'Initialize tracking' first."}
        branch = marker.get("branch") or _default_branch(CORE_REPO_OWNER, CORE_REPO_NAME)
        try:
            remote_sha = _latest_sha(CORE_REPO_OWNER, CORE_REPO_NAME, branch)
        except urllib.error.HTTPError as e:
            return {"error": f"HTTP {e.code}: {e.reason}"}
        except Exception as e:
            return {"error": str(e)}
        if not remote_sha:
            return {"error": "Could not resolve latest commit"}
        if remote_sha == marker["sha"]:
            return {"ok": True, "sha": remote_sha[:7], "branch": branch, "written_files": [], "unchanged": True}
        try:
            changed = _core_changed_files(marker["sha"], remote_sha)
        except Exception as e:
            return {"error": f"Compare failed: {e}"}
        writable, blockers = _classify_core_changes(changed)
        if blockers:
            return {
                "error": "rebuild_required",
                "message": "Remote commits touch files that aren't bind-mounted. Rebuild the container.",
                "blockers": blockers,
                "command": REBUILD_CMD,
                "branch": branch,
                "remote_sha": remote_sha[:7],
            }
        if not writable:
            _save_core_marker(remote_sha, branch)
            return {"ok": True, "sha": remote_sha[:7], "branch": branch, "written_files": []}
        stage = None
        try:
            stage, _ = _download_core_stage(branch)
            written = _overlay_core(stage)
            _save_core_marker(remote_sha, branch)
            return {
                "ok": True,
                "sha": remote_sha[:7],
                "branch": branch,
                "written_files": written,
            }
        except urllib.error.HTTPError as e:
            return {"error": f"HTTP {e.code}: {e.reason}"}
        except Exception as e:
            return {"error": str(e)}
        finally:
            if stage and stage.exists():
                shutil.rmtree(stage, ignore_errors=True)

    @app.post("/api/plugins/update_manager/uninstall/{plugin_id}")
    def uninstall(plugin_id: str):
        if not SLUG_RE.match(plugin_id):
            return {"error": "Invalid plugin id"}
        if plugin_id == "update_manager":
            return {"error": "Cannot uninstall the update manager itself"}
        target = _installed_plugin_dirs().get(plugin_id)
        if not target or not target.is_dir():
            return {"error": "Plugin not found"}
        if _is_bundled(target):
            return {
                "error": "Bundled with slopsmith core; cannot be uninstalled. Install a standalone copy under a different directory to override.",
                "bundled": True,
            }
        try:
            shutil.rmtree(target)
            return {"ok": True}
        except Exception as e:
            return {"error": str(e)}

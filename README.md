# Slopsmith Plugin: Update Manager

A plugin for [Slopsmith](https://github.com/byrongamatos/slopsmith) that installs, updates, and uninstalls other plugins **and the slopsmith core itself** from a single in-app screen. Uses GitHub downloads directly — no `git` CLI inside the container, no terminal, no `docker compose` commands required.

<img width="997" height="999" alt="image" src="https://github.com/user-attachments/assets/57f68aca-7362-4f94-ab9e-73e269a6ad1e" />
<img width="994" height="1239" alt="updateman2" src="https://github.com/user-attachments/assets/4c1457c0-5bd2-48c7-982f-7cff33406119" />

## Features

- **Browse the registry** — parses the "Available Plugins" table from slopsmith's README and lists every plugin with a one-click Install button
- **Filter** — search by name, description, or directory
- **Plugin update detection** — compares each installed plugin's commit SHA against its GitHub default branch (works without the `git` binary)
- **One-click plugin update** — re-downloads the latest source via GitHub zip and replaces the plugin directory atomically
- **Slopsmith core updates** — tracks `byrongamatos/slopsmith` alongside your plugins, overlays updates onto the bind-mounted code paths (`server.py`, `ug_browser.py`, `lib/`, `static/`)
- **Rebuild-required detection** — if an upstream commit touches unmounted files (`Dockerfile`, `requirements.txt`, `docker-compose.yml`, etc.) the update is blocked and the UI shows a copy-paste host command
- **Update all** — sequentially updates the core plus every plugin that's behind
- **Uninstall** — removes a plugin directory
- **Exclusion list** — flag plugins (or the core) to skip during update checks and "update all" — persisted on the `/config` volume so it survives container restarts
- **In-place restart** — `Restart now` button re-execs the uvicorn process via `os.execv` without touching Docker, preserving PID 1 and container lifetime

## What's New

### v1.11.4
- **Registry browse: bundled plugins with mismatched dir/id now show "Bundled"** — desktop-bundled plugins whose on-disk directory name differs from their manifest id (e.g. dir `tabimport`/id `tab_import`, dir `practice`/id `practice_journal`) were mislabeled as green "Installed" instead of "Bundled". The registry README's install command yields a dirname equal to the manifest *id*, but `/registry` matched only against on-disk directory names, so `overrides_bundled` came back false while the frontend's id-based fallback flipped `installed` true. `/registry` now matches registry dirnames against both the directory name and the manifest id, keeping the two flags consistent.

### v1.11.3
- **Inventory the desktop app-bundle plugins dir** — on slopsmith-desktop, `SLOPSMITH_PLUGINS_DIR` only points at the writable user plugins dir (`%APPDATA%\slopsmith-desktop\plugins`), so plugins shipped inside the app bundle — including the Update Manager itself — were invisible to this plugin's inventory. Clicking **Check** on the update_manager row returned "Plugin not found". `_installed_plugin_dirs()` now scans both the user dir and the read-only bundled dir (where this plugin's own code is loaded from), and `_is_bundled` flags entries living under the bundled dir so the UI hides their Check/Update/Uninstall buttons (the desktop app's own auto-updater manages those).

### v1.11.2
- **Desktop-aware update instructions** — the green "Changes applied" banner no longer references `docker compose restart` to users on [slopsmith-desktop](https://github.com/byrongamatos/slopsmith-desktop). Desktop installs get a one-line "Restart Slopsmith to load the new code." paired with the in-app Restart button. The amber rebuild banner is now defensively `data-docker-only`-gated too (the server already hides core tracking on desktop, but belt-and-braces). Closes [#1](https://github.com/masc0t/slopsmith-update-manager/issues/1).
- **README split into Desktop / Docker top-level sections** — first-time readers no longer have to figure out which install path applies to them; core-update / rebuild flow lives under Docker only.

### v1.11.0
- **Storage tab** — new third tab surfaces where slopsmith is putting bytes on disk: DLC library, user data (Electron `userData` / `CONFIG_DIR`), plugins dir, sloppak extract cache, and the GitHub response cache, each with size and an Open-folder button (desktop only; docker installs get Copy-path). Driven by Discord complaints about C: drive fill-up and "where did the Tones plugin Python land" on Windows desktop installs.
- **Per-plugin disk breakdown** — under the location rows, each installed plugin's user-data footprint is listed by aggregating sizes of paths it declared in `settings.server_files` / `diagnostics.server_files` (per [slopsmith#113](https://github.com/byrongamatos/slopsmith/pull/113) / [#166](https://github.com/byrongamatos/slopsmith/pull/166)). Plugins that don't declare these fields show only their source-dir size. Sorted by size descending so heaviest hitters surface first.
- **Two clear buttons** — Sloppak extract cache (regenerates on next play) and GitHub response cache (next update check refetches). Plugin-owned files are inventory-only — without a manifest opt-in distinguishing cache from data, a blanket plugin-clear button would risk wiping user settings and history. Use the Open button to manage plugin-owned bytes yourself.
- **Path-whitelisted Open/Clear endpoints** — clients pass symbolic keys (`sloppak_cache`, `plugin:<id>`, etc.), never raw paths. The backend resolves keys against the just-computed inventory and rejects anything else, so a stray HTTP call can't trick the server into opening or deleting an arbitrary path.

### v1.10.0
- **Auto-advance core marker after manual rebuild** — after `git pull && docker compose build web && docker compose up -d`, the persistent `core.json` marker no longer holds the pre-pull SHA forever. On startup the plugin reads the live SHA from the bind-mounted `.git/` and advances the marker when it lags behind, clearing the false "rebuild required" banner that used to stick around indefinitely.
- **"Mark as current" button on the rebuild banner** — when a core update is blocked by unmounted-file changes, the amber banner now offers a one-click way to stamp the current remote HEAD as the installed marker. Useful when you've already rebuilt from the host and just want the UI to catch up without re-running the overlay.
- **Desktop-mode gate on the auto-advance hook** — the startup hook is now explicitly skipped under [slopsmith-desktop](https://github.com/byrongamatos/slopsmith-desktop) (`SLOPSMITH_PLUGINS_DIR` set). Desktop installs ship without `/app/.git` and don't use core tracking; the gate makes the intent explicit instead of relying on a silent `try/except`.

### v1.9.0
- **Bulk `/updates` never spends `api.github.com` budget** — the cold first-run check no longer rate-limits halfway through your plugin list. Bulk detection now uses pure version-string comparison against `raw.githubusercontent.com` (separate, far more generous limit) and tries marker-branch → `main` → `master` for branch resolution. A 30-plugin cold start now costs 0 `api.github.com` calls, leaving the full 60/hour budget for on-demand actions. Version comparison is semver-aware (`_remote_is_newer`), so a feature branch with a bumped manifest no longer surfaces a spurious self-update.
- **Per-plugin Check button always available** — the per-row Check button now appears on every non-bundled plugin, including rows in "Source unknown" or "Couldn't reach repo" error states. `/check/{plugin_id}` runs the full check (resolves default branch via `api.github.com`, fetches remote SHA) and retries source resolution with `force_registry=True`, so users are never permanently stuck on an unresolvable external plugin. New last-resort fallback probes `github.com/byrongamatos/slopsmith-plugin-<dir>` and accepts it only when the fetched `plugin.json` id matches.

### v1.8.2
- **Detect external restarts** — the per-row "Updated · restart to apply" UI now clears itself when the server has been restarted outside the in-app "Restart now" flow (e.g. `docker compose restart`, host-side process kill). New `GET /api/plugins/update_manager/start_time` endpoint exposes the process start time; the frontend records it in `localStorage["update_manager:knownStartTime"]` and clears pending state + the restart banner when it sees the value change.

### v1.8.1
- **Fix stuck "Update available" after `git pull`** — `_resolve_source` now picks the freshest of the marker (`installed_at`, falling back to the marker file's own mtime if missing/malformed) and the git ref's mtime — where "git ref" means whichever file the SHA was actually read from: `.git/refs/heads/<branch>` for loose refs, `.git/packed-refs` for packed-ref repos, or `.git/HEAD` for detached-HEAD checkouts. Cloned plugins whose marker is older than the current ref no longer appear behind forever; UI-zip-updated cloned plugins still surface the marker since it gets a newer `installed_at` than the preserved `.git/`. Bonus: per-row "Updated · restart to apply" status appears on plugins immediately after clicking Update, replacing the stale "Update available" until the user restarts.

### v1.8.0
- **Per-plugin Check button** — each plugin row now has a "Check" button next to its primary action. Re-checks just that plugin against GitHub, useful when the bulk cold pass has burnt through GitHub's anonymous rate-limit window and left some rows in "Check failed". Backed by a new `GET /check/{plugin_id}` endpoint that reuses the same conditional-fetch / version-first short-circuit as the bulk pass.

### v1.7.0
- **Surface plugin versions** — Local and Remote columns now stack the `plugin.json` `version` (top) over the short SHA (bottom), so installed and available versions are visible at a glance instead of just opaque commit hashes.
- **Pin to a specific version** — new per-row "Versions" link lazily fetches the available versions for a plugin (git tags + `plugin.json` bump-commits scanned from the default branch, capped at 30 entries). The popover lets you switch to any version — useful for downgrading when a release breaks something. `POST /update/{plugin_id}` accepts an optional `{ref}` body (`refs/tags/X` or a commit SHA) to pin the install.
- **Bundled-install update detection** — desktop installs that ship plugins as plain directories (no `.git/`, no marker) can now detect updates: `_resolve_source` falls back to `plugin.json`'s optional `url`/`repository` field, then to the "Available Plugins" registry parsed from slopsmith's README (keyed by directory name, cached for 10 minutes).

### v1.6.0
- **Bundled-plugin awareness** — plugins shipped in-tree by the slopsmith core (those with `"bundled": true` in `plugin.json`, e.g. the 3D Highway) are now first-class in the UI. Bundled rows render with a lock-icon "Bundled" badge instead of being invisible, and Update/Uninstall both refuse with clear errors — preventing the previous footgun where an Uninstall would have happily `rmtree`d container-image files until the next rebuild restored them.
- **Override warning in the registry** — Browse-tab entries whose dirname collides with a bundled plugin show an "Overrides bundled" pill, and Install prompts for confirmation before clobbering the bundled copy with an external override. `/updates` exposes a top-level `bundled[]` array and `/registry` annotates each entry with `overrides_bundled: bool`.

### v1.5.0
- **Ignore non-core files** — updates to documentation (`*.md`, `docs/`), tests (`tests/`), and Claude config (`.claude/`) no longer block core updates. These paths are silently skipped during both blocker detection and extraction.
- **Self-update** — the Update Manager can now update itself. When an update is available, clicking Update downloads the new version to a staging area, then prompts you to restart to apply it.

### v1.4.0
- **Slopsmith core tracking** — a new "Slopsmith Core" row appears above the plugin table. Click **Initialize tracking** once to stamp the current commit, then the card reports behind/ahead state against `byrongamatos/slopsmith`.
- **Non-destructive core overlay** — updates write only the files that exist in the upstream zip; files present locally and absent from the zip (e.g. `static/audio_*.mp3`, `__pycache__/`, every installed plugin dir) are left alone.
- **Rebuild-required guard** — if the GitHub compare between your installed SHA and the remote HEAD reports changes to any path outside the bind-mounted set, the update is blocked and an amber banner lists the offending files plus a copy-paste rebuild command.
- **Rename** — plugin id and install path are now `update_manager` (was `plugin_manager`). API routes moved from `/api/plugins/plugin_manager/*` to `/api/plugins/update_manager/*`.

## Slopsmith Desktop

On [slopsmith-desktop](https://github.com/byrongamatos/slopsmith-desktop) the Update Manager is pre-installed — no setup required. Open **Update Manager** in the nav.

- **Plugins** — install, update, and uninstall through the UI exactly the same way as on Docker.
- **Restart** — when an update applies, click **Restart now** in the green banner. Electron tears down and relaunches the bundled server for you.
- **Core updates** — not handled by this plugin on desktop. The slopsmith-desktop app has its own auto-updater for the core, so the "Slopsmith Core" card and the rebuild banner are hidden in desktop mode.
- **Storage tab** — uses native folder-open via Electron (the **Open** buttons launch your OS file manager).

## Docker

### Requirements

- Outbound HTTPS from the slopsmith container to `github.com`, `api.github.com`, `raw.githubusercontent.com`, `codeload.github.com`
- No additional Python dependencies — stdlib only (`urllib`, `zipfile`)
- A readable `/proc/self/cmdline` inside the container (standard on Linux) — used by the in-place restart

### Installation

```bash
cd /path/to/slopsmith/plugins
git clone https://github.com/masc0t/slopsmith-update-manager.git update_manager
docker compose restart
```

After this one-time bootstrap, further plugins can be installed, the core can be tracked, and the manager itself can be updated through the UI.

### How it works

1. Open **Update Manager** in the nav — the Updates tab checks installed plugins and the core against GitHub in parallel
2. For each installed plugin, the installed commit SHA is read from either a `.slopsmith-installed.json` marker (plugins installed through this tool) or the plugin's `.git/config` and `.git/HEAD` (plugins cloned manually on the host)
3. Core tracking lives at `/config/update_manager/core.json` — click **Initialize tracking** on first use to stamp the current remote HEAD as the baseline
4. Clicking **Update** on a plugin downloads its repo zip and atomically swaps the directory
5. Clicking **Update** on the core downloads the slopsmith zip and non-destructively overlays just the mounted paths, leaving runtime artifacts (audio files, pycache, other plugins) untouched
6. When the update completes, a green banner appears — click **Restart now** and the uvicorn process re-execs in place so the new code loads without a Docker restart

> **Note:** The plugin only writes to paths that `docker-compose.yml` bind-mounts into the container. Updates that touch image-baked files (`Dockerfile`, `requirements.txt`, etc.) are surfaced as **Rebuild required** and must be applied from the host.

### Slopsmith core updates

The core row appears above the plugin table on the Updates tab. It shows the installed SHA, the remote SHA, and a status pill:

- **Tracking not initialized** — click **Initialize tracking** to stamp the current remote HEAD (or call `POST /core/init` with an explicit `{sha}` to mark an older commit)
- **Up to date** — local matches remote
- **Update available** — safe to apply from the UI; all changed files fall under the mounted whitelist
- **Rebuild required** — remote commits touch files outside the mount whitelist; the Update button is disabled and the amber banner lists every blocker plus a copy-paste command

#### What gets overlayed

Only paths that `docker-compose.yml` bind-mounts into the container can be rewritten from inside it:

- `server.py`
- `ug_browser.py`
- `lib/`
- `static/`

`plugins/` is always excluded from core updates — each plugin is tracked independently by this tool and would be clobbered otherwise.

#### Rebuild command

When the core update is blocked, the UI surfaces this host-side command:

```bash
cd slopsmith && git pull && docker compose build web && docker compose up -d
```

#### Exclusions

Toggle the **Exclude** checkbox on the core row to skip it during future checks and "update all". Exclusions are persisted to `/config/update_manager/exclusions.json`.

## API

All endpoints are namespaced under `/api/plugins/update_manager/`:

| Method | Path                        | Description                                  |
|--------|-----------------------------|----------------------------------------------|
| GET    | `/registry`                 | Parses slopsmith's README and returns the plugin list |
| GET    | `/updates`                  | Compares installed plugins against GitHub    |
| GET    | `/check/{plugin_id}`        | Re-checks one plugin. Success: `{plugin_id, update, error, source, excluded, bundled}`. Validation failure (invalid id / not installed): `{error}` only (no `plugin_id`) |
| GET    | `/start_time`               | Returns `{started_at}` (server process start time, seconds since epoch). Used by the UI to detect external restarts. |
| POST   | `/install`                  | Body `{url, dirname}` — installs from a GitHub repo |
| POST   | `/update/{plugin_id}`       | Re-downloads latest source and swaps         |
| POST   | `/uninstall/{plugin_id}`    | Removes the plugin directory                 |
| GET    | `/exclusions`               | Returns the current exclusion list           |
| POST   | `/exclusions`               | Body `{plugin_id, excluded}` — toggles exclusion (use `plugin_id: "__core__"` for the core) |
| GET    | `/core`                     | Returns `{repo, branch, local_sha, remote_sha, behind, blockers, changed_files, tracking, excluded, rebuild_required}` |
| POST   | `/core/init`                | Body `{sha?}` — stamp marker with `sha` (or current remote HEAD) to enable core tracking |
| POST   | `/core/update`              | Overlays mounted core files from GitHub zip. Returns `{error: "rebuild_required", blockers, command}` if any unmounted file changed |
| POST   | `/restart`                  | Re-execs the server process in place         |

## Limitations

- GitHub's unauthenticated rate limit is 60 req/hour per IP. Each "Check for updates" makes one API call per installed plugin, plus one for the core — well within the limit for normal use
- Only GitHub repositories are supported (registry, compare, and zip endpoints are GitHub-specific)

## Other Plugins

- [MIDI Capo](https://github.com/masc0t/slopsmith-plugin-midi-capo) — send MIDI CC to your amp/modeler or internal VST to match each song's tuning automatically
- [Find More Songs](https://github.com/masc0t/slopsmith-plugin-find-more) — search CustomsForge for more songs by an artist
- [Invert Highway](https://github.com/masc0t/slopsmith-plugin-invert-highway) — flip the chord note stacking order on the highway

## License

MIT

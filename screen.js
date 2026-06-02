/* Update Manager - install + update plugins and slopsmith core over GitHub */
(function () {
    const RESTART_KEY = 'update_manager:restartPending';
    const PENDING_IDS_KEY = 'update_manager:pendingRestartIds';
    const START_TIME_KEY = 'update_manager:knownStartTime';
    const API = '/api/plugins/update_manager';
    const CORE_KEY = '__core__';

    let plugins = [];        // /api/plugins result (installed list)
    let updates = {};        // API + '/updates' -> { [id]: {local, remote, branch, source, repo} }
    let updateErrors = {};   // per-row CHECK errors (from /updates)
    let updateFailures = {}; // per-row UPDATE-ACTION errors (the Update click failed) — id -> message
    let sources = {};        // { [id]: {repo, url, branch, source} | {bundled: true} }
    let excluded = new Set(); // plugin ids the user has opted out of updates for
    let bundledIds = new Set(); // plugin ids that ship with slopsmith core (issue #1)
    let registry = [];       // API + '/registry' -> entries
    let coreStatus = null;   // API + '/core'
    let currentTab = 'updates';
    let isDesktop = !!window.slopsmithDesktop?.isDesktop;
    let _inflightChecks = new Set(); // plugin ids with an in-flight per-row recheck
    // Plugin ids whose update was applied but still need a server
    // restart to take effect. Persists across page reloads via
    // PENDING_IDS_KEY so the row keeps the "Updated · restart to
    // apply" status until the user actually restarts.
    let _pendingRestart = new Set(loadPendingRestart());

    function loadPendingRestart() {
        try {
            const raw = localStorage.getItem(PENDING_IDS_KEY);
            if (!raw) return [];
            const arr = JSON.parse(raw);
            return Array.isArray(arr) ? arr.filter(x => typeof x === 'string') : [];
        } catch (e) { return []; }
    }
    function savePendingRestart() {
        try {
            localStorage.setItem(PENDING_IDS_KEY, JSON.stringify([..._pendingRestart]));
        } catch (e) { /* ignore */ }
    }
    function markPendingRestart(id) {
        _pendingRestart.add(id);
        savePendingRestart();
    }
    function clearPendingRestart() {
        _pendingRestart.clear();
        try { localStorage.removeItem(PENDING_IDS_KEY); } catch (e) { /* ignore */ }
    }

    // Detect external server restarts (docker compose restart, host
    // kill, …) so the per-row "Updated · restart to apply" UI doesn't
    // stick across them. Compares the backend's process start_time
    // against the value last written to localStorage. On change with
    // pending entries: those updates are now live, clear pending.
    //
    // In-flight calls dedupe via _syncInFlight: updaterOnShow awaits
    // it before showing the banner, then immediately calls
    // updaterCheck which would otherwise refetch /start_time. Each
    // resolved promise is single-use so subsequent unrelated calls
    // (post-update, banner-dismiss…) still hit the network freshly.
    let _syncInFlight = null;
    function syncProcessStartTime() {
        if (_syncInFlight) return _syncInFlight;
        _syncInFlight = (async () => {
            try {
                return await _doSyncProcessStartTime();
            } finally {
                _syncInFlight = null;
            }
        })();
        return _syncInFlight;
    }
    async function _doSyncProcessStartTime() {
        let started_at;
        try {
            const r = await fetch(API + '/start_time', { cache: 'no-store' });
            const j = await r.json();
            started_at = (j && typeof j.started_at === 'number') ? j.started_at : null;
        } catch (e) { started_at = null; }
        if (started_at === null) return;
        let prev = null;
        try {
            const raw = localStorage.getItem(START_TIME_KEY);
            prev = raw ? Number(raw) : null;
            if (!Number.isFinite(prev)) prev = null;
        } catch (e) { prev = null; }
        if (prev !== null && prev !== started_at) {
            // Clear any restart-pending state held over from before
            // the restart. _pendingRestart covers per-plugin updates;
            // RESTART_KEY also gets set by core-update flows that
            // never touch _pendingRestart, so check both.
            let restartFlag = null;
            try { restartFlag = localStorage.getItem(RESTART_KEY); } catch (e) { /* ignore */ }
            if (_pendingRestart.size > 0 || restartFlag === '1') {
                clearPendingRestart();
                try { localStorage.removeItem(RESTART_KEY); } catch (e) { /* ignore */ }
                const banner = document.getElementById('updater-restart-banner');
                if (banner) banner.classList.add('hidden');
            }
        }
        try { localStorage.setItem(START_TIME_KEY, String(started_at)); } catch (e) { /* ignore */ }
    }

    // Refresh the "N updates available" status text and Update-all
    // button visibility from current state. Called after any flow that
    // mutates `updates` / `excluded` / `coreStatus`. Centralised so the
    // counter rules don't drift between bulk check, exclusion toggle,
    // and per-row recheck.
    function updaterRefreshStatusUI() {
        // Pending-restart ids are functionally up-to-date locally —
        // their new code is staged on disk; only the running process
        // is stale. Don't count them in the header, don't show
        // Update-all for them, don't let updaterUpdateAll re-pick
        // them on the next pass.
        const pluginCount = Object.keys(updates).filter(id => !_pendingRestart.has(id)).length;
        const coreBehind = !!(coreStatus && coreStatus.behind && !coreStatus.excluded);
        const total = pluginCount + (coreBehind ? 1 : 0);
        const statusEl = document.getElementById('updater-status');
        if (statusEl) {
            statusEl.textContent = total === 0
                ? 'Everything up to date.'
                : total + ' update' + (total > 1 ? 's' : '') + ' available';
        }
        const coreUpdatable = coreBehind && !coreStatus.rebuild_required;
        const allBtn = document.getElementById('updater-update-all-btn');
        if (allBtn) allBtn.classList.toggle('hidden', pluginCount === 0 && !coreUpdatable);
    }

    // ── Screen show hook ───────────────────────────────────────────────
    const _origShowScreen = window.showScreen;
    window.showScreen = function (id) {
        _origShowScreen(id);
        if (id === 'plugin-update_manager') updaterOnShow();
    };

    async function updaterOnShow() {
        try {
            const r = await fetch(API + '/config');
            const cfg = await r.json();
            isDesktop = cfg.is_desktop ?? isDesktop;
        } catch (e) { /* fall back to window.slopsmithDesktop */ }
        // Resolve external-restart drift before deciding whether to
        // surface the banner. Avoids flashing "restart pending" on a
        // server that's already running new code.
        await syncProcessStartTime();
        let restartFlag = null;
        try { restartFlag = localStorage.getItem(RESTART_KEY); } catch (e) { /* storage blocked */ }
        if (restartFlag === '1') {
            document.getElementById('updater-restart-banner').classList.remove('hidden');
        }
        if (isDesktop) {
            document.querySelectorAll('[data-docker-only]').forEach(el => el.classList.add('hidden'));
            document.querySelectorAll('[data-desktop-only]').forEach(el => el.classList.remove('hidden'));
        }
        if (currentTab === 'updates') updaterCheck();
        else updaterLoadRegistry();
    }

    // ── Tabs ───────────────────────────────────────────────────────────
    let storageData = null;     // last /storage payload — null until first load
    let storageLoading = false; // dedupe overlapping fetches

    window.updaterTab = function (tab) {
        currentTab = tab;
        const tUp = document.getElementById('updater-tab-updates');
        const tBr = document.getElementById('updater-tab-browse');
        const tSt = document.getElementById('updater-tab-storage');
        const pUp = document.getElementById('updater-pane-updates');
        const pBr = document.getElementById('updater-pane-browse');
        const pSt = document.getElementById('updater-pane-storage');
        const activeCls = 'px-4 py-2 text-sm transition border-b-2 border-accent text-white';
        const idleCls = 'px-4 py-2 text-sm transition border-b-2 border-transparent text-gray-500 hover:text-white';
        tUp.className = (tab === 'updates') ? activeCls : idleCls;
        tBr.className = (tab === 'browse') ? activeCls : idleCls;
        if (tSt) tSt.className = (tab === 'storage') ? activeCls : idleCls;
        pUp.classList.toggle('hidden', tab !== 'updates');
        pBr.classList.toggle('hidden', tab !== 'browse');
        if (pSt) pSt.classList.toggle('hidden', tab !== 'storage');
        if (tab === 'updates' && !plugins.length) updaterCheck();
        else if (tab === 'browse' && !registry.length) updaterLoadRegistry();
        else if (tab === 'storage' && storageData === null) updaterLoadStorage(false);
    };

    // ── Check for updates ──────────────────────────────────────────────
    window.updaterCheck = async function () {
        const btn = document.getElementById('updater-check-btn');
        const loading = document.getElementById('updater-loading');
        const status = document.getElementById('updater-status');
        const table = document.getElementById('updater-table');
        btn.disabled = true;
        btn.textContent = 'Checking...';
        loading.classList.remove('hidden');
        status.textContent = '';
        table.innerHTML = '';
        try {
            // Run start-time sync in parallel with the three data
            // fetches. Awaited before the pending-id strip below
            // so the freshly-fetched `updates` is filtered against
            // the post-clear pending set.
            const syncP = syncProcessStartTime();
            const [pRes, uRes, cRes] = await Promise.all([
                fetch('/api/plugins'),
                fetch(API + '/updates'),
                fetch(API + '/core'),
            ]);
            plugins = await pRes.json();
            const uData = await uRes.json();
            updates = uData.updates || {};
            updateErrors = uData.errors || {};
            sources = uData.sources || {};
            excluded = new Set(uData.excluded || []);
            bundledIds = new Set(uData.bundled || []);
            coreStatus = await cRes.json();
            await syncP;
            // Strip pending-restart ids from `updates` so the backend
            // can't re-introduce them between Update click and
            // restart. The marker write at update time may not yet
            // reflect on the GitHub-cached side, and we don't want
            // to revert the row's pending-restart UI just because
            // the bulk recheck still sees mismatched shas.
            for (const id of _pendingRestart) {
                delete updates[id];
                delete updateErrors[id];
            }
            updaterRenderCore();
            updaterRenderUpdates();
            document.getElementById('updater-last-checked').textContent =
                'Last checked: ' + new Date().toLocaleTimeString();
            updaterRefreshStatusUI();
        } catch (e) {
            status.textContent = 'Check failed: ' + e.message;
        } finally {
            btn.disabled = false;
            btn.textContent = 'Check for updates';
            loading.classList.add('hidden');
        }
    };

    // ── Core card ──────────────────────────────────────────────────────
    function updaterRenderCore() {
        const card = document.getElementById('updater-core-card');
        const rebuildBanner = document.getElementById('updater-rebuild-banner');
        const rebuildCmdEl = document.getElementById('updater-rebuild-cmd');
        const rebuildFilesEl = document.getElementById('updater-rebuild-files');
        if (!card) return;
        if (!coreStatus || coreStatus.hidden) {
            card.classList.add('hidden');
            if (rebuildBanner) rebuildBanner.classList.add('hidden');
            return;
        }

        const c = coreStatus;
        card.classList.remove('hidden');

        let statusHtml, actionHtml, localStr = '', remoteStr = '', rowBg;

        if (c.error) {
            rowBg = 'bg-dark-800/30';
            statusHtml = `<span class="text-red-400 text-xs" title="${esc(c.error)}">Check failed</span>`;
            actionHtml = `<button onclick="updaterCheck()" class="text-gray-400 hover:text-white text-xs transition">Retry</button>`;
        } else if (!c.tracking) {
            rowBg = 'bg-dark-800/30';
            statusHtml = `<span class="text-gray-400 font-semibold text-xs">Tracking not initialized</span>
                <div class="text-[10px] text-gray-600 mt-0.5">Stamp the current commit to enable update checks</div>`;
            actionHtml = `<button onclick="updaterInitCore(this)"
                class="bg-accent/20 hover:bg-accent/30 text-accent px-3 py-1 rounded-lg text-xs transition">Initialize tracking</button>`;
            remoteStr = (c.remote_sha || '').slice(0, 7);
        } else if (c.excluded) {
            rowBg = 'bg-dark-900/40 opacity-60';
            localStr = (c.local_sha || '').slice(0, 7);
            remoteStr = (c.remote_sha || '').slice(0, 7);
            statusHtml = `<span class="text-gray-500 font-semibold text-xs">Excluded</span>
                <div class="text-[10px] text-gray-600 mt-0.5">Updates disabled</div>`;
            actionHtml = `<span class="text-gray-700 text-xs">—</span>`;
        } else if (c.behind && c.rebuild_required) {
            rowBg = 'bg-dark-700/40';
            localStr = (c.local_sha || '').slice(0, 7);
            remoteStr = (c.remote_sha || '').slice(0, 7);
            statusHtml = `<span class="text-amber-400 font-semibold text-xs">Rebuild required</span>
                <div class="text-[10px] text-gray-600 mt-0.5">${(c.blockers || []).length} unmounted file(s) changed</div>`;
            actionHtml = `<button disabled title="Requires host-side rebuild"
                class="bg-dark-700 text-gray-500 px-3 py-1 rounded-lg text-xs cursor-not-allowed">Blocked</button>`;
        } else if (c.behind) {
            rowBg = 'bg-dark-700/40';
            localStr = (c.local_sha || '').slice(0, 7);
            remoteStr = (c.remote_sha || '').slice(0, 7);
            statusHtml = `<span class="text-amber-400 font-semibold text-xs">Update available</span>
                <div class="text-[10px] text-gray-600 mt-0.5">${esc(c.repo)} · ${esc(c.branch)}</div>`;
            actionHtml = `<button onclick="updaterUpdateCore(this)"
                class="bg-accent/20 hover:bg-accent/30 text-accent px-3 py-1 rounded-lg text-xs transition">Update</button>`;
        } else {
            rowBg = 'bg-dark-800/30';
            localStr = (c.local_sha || '').slice(0, 7);
            remoteStr = (c.remote_sha || '').slice(0, 7);
            statusHtml = `<span class="text-green-400 font-semibold text-xs">Up to date</span>`;
            actionHtml = `<span class="text-gray-600 text-xs">—</span>`;
        }

        const exclCheckbox = c.tracking
            ? `<label class="inline-flex items-center justify-center cursor-pointer" title="Exclude slopsmith core from update checks">
                <input type="checkbox" data-plugin-id="${CORE_KEY}" onchange="updaterToggleExclude(this)"
                    ${c.excluded ? 'checked' : ''}
                    class="accent-amber-500 w-3.5 h-3.5 rounded">
            </label>`
            : '<span class="text-gray-700 text-xs">—</span>';

        card.innerHTML = `
            <div class="grid grid-cols-[1fr_auto_auto_auto_auto_auto] gap-x-4 text-xs text-gray-500 font-semibold uppercase tracking-wider px-3 py-2 border-b border-gray-800">
                <span>Slopsmith Core</span>
                <span class="w-24 text-center hidden sm:block">Local</span>
                <span class="w-24 text-center hidden sm:block">Remote</span>
                <span class="w-28 text-center">Status</span>
                <span class="w-20 text-center" title="Exclude from automatic updates">Exclude</span>
                <span class="w-40 text-center">Action</span>
            </div>
            <div class="grid grid-cols-[1fr_auto_auto_auto_auto_auto] gap-x-4 items-center px-3 py-2.5 rounded-lg ${rowBg} transition">
                <div class="min-w-0">
                    <a href="${esc(c.url)}" target="_blank" rel="noopener"
                        class="text-sm text-white hover:text-accent hover:underline truncate inline-flex items-center gap-1"
                        title="${esc(c.repo)} · open on GitHub">Slopsmith
                        <svg class="w-3 h-3 text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14"/></svg>
                    </a>
                    <div class="text-xs text-gray-500 truncate">${esc(c.repo)} · ${esc(c.branch || 'main')}</div>
                </div>
                <span class="w-24 text-center text-xs text-gray-400 font-mono hidden sm:block">${esc(localStr)}</span>
                <span class="w-24 text-center text-xs text-gray-400 font-mono hidden sm:block">${esc(remoteStr)}</span>
                <span class="w-28 text-center">${statusHtml}</span>
                <span class="w-20 text-center">${exclCheckbox}</span>
                <span class="w-40 text-center">${actionHtml}</span>
            </div>`;

        if (c.rebuild_required && !c.excluded) {
            rebuildBanner.classList.remove('hidden');
            if (c.rebuild_command) rebuildCmdEl.textContent = c.rebuild_command;
            rebuildFilesEl.innerHTML = (c.blockers || [])
                .map(b => `<li>${esc(b.status || '?')}  ${esc(b.filename)}</li>`)
                .join('');
        } else {
            rebuildBanner.classList.add('hidden');
        }
    }

    window.updaterMarkCurrent = async function () {
        var btn = document.getElementById('updater-mark-current-btn');
        var status = document.getElementById('updater-mark-current-status');
        btn.disabled = true;
        btn.textContent = 'Updating...';
        status.textContent = '';
        try {
            var resp = await fetch('/api/plugins/update_manager/core/init', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: '{}'
            });
            var data = await resp.json();
            if (data.ok) {
                status.textContent = 'Tracking reset to ' + data.sha + ' (' + data.branch + ')';
                status.className = 'text-xs text-green-400 ml-2';
                btn.textContent = 'Mark as current';
                setTimeout(function () { updaterCheck(); }, 1000);
            } else {
                status.textContent = data.error || 'Failed';
                status.className = 'text-xs text-red-400 ml-2';
                btn.textContent = 'Mark as current';
            }
        } catch (e) {
            status.textContent = 'Network error';
            status.className = 'text-xs text-red-400 ml-2';
            btn.textContent = 'Mark as current';
        }
        btn.disabled = false;
    };

    window.updaterCopyRebuildCmd = async function () {
        const btn = document.getElementById('updater-rebuild-copy-btn');
        const cmd = document.getElementById('updater-rebuild-cmd').textContent;
        try {
            await navigator.clipboard.writeText(cmd);
            btn.textContent = 'Copied';
            setTimeout(() => { btn.textContent = 'Copy'; }, 1500);
        } catch (e) {
            btn.textContent = 'Copy failed';
        }
    };

    window.updaterInitCore = async function (btn) {
        btn.disabled = true;
        const orig = btn.textContent;
        btn.textContent = 'Initializing...';
        try {
            const resp = await fetch(API + '/core/init', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({}),
            });
            const data = await resp.json();
            if (data.ok) {
                await updaterReloadCore();
            } else {
                btn.disabled = false;
                btn.textContent = 'Failed';
                btn.title = data.error || '';
            }
        } catch (e) {
            btn.disabled = false;
            btn.textContent = orig;
            btn.title = e.message;
        }
    };

    window.updaterUpdateCore = async function (btn) {
        return updaterDoCoreUpdate(btn);
    };

    async function updaterDoCoreUpdate(btn) {
        btn.disabled = true;
        const orig = btn.textContent;
        btn.textContent = 'Updating...';
        try {
            const resp = await fetch(API + '/core/update', { method: 'POST' });
            const data = await resp.json();
            if (data.ok) {
                btn.outerHTML = '<span class="text-green-400 text-xs font-semibold">Updated</span>';
                localStorage.setItem(RESTART_KEY, '1');
                document.getElementById('updater-restart-banner').classList.remove('hidden');
                await updaterReloadCore();
                return true;
            }
            if (data.error === 'rebuild_required') {
                btn.disabled = false;
                btn.textContent = 'Blocked';
                btn.className = 'bg-amber-900/30 text-amber-400 px-3 py-1 rounded-lg text-xs';
                btn.title = data.message || 'Rebuild required';
                await updaterReloadCore();
                return false;
            }
            btn.disabled = false;
            btn.textContent = 'Failed';
            btn.title = data.error || 'Unknown error';
            btn.className = 'bg-red-900/30 text-red-400 px-3 py-1 rounded-lg text-xs';
            return false;
        } catch (e) {
            btn.disabled = false;
            btn.textContent = orig;
            btn.title = e.message;
            return false;
        }
    }

    async function updaterReloadCore() {
        try {
            const r = await fetch(API + '/core');
            coreStatus = await r.json();
            updaterRenderCore();
            // Core state feeds the status counter (coreBehind) and
            // Update-all visibility (coreUpdatable). Refresh after a
            // core init / update so the header doesn't lag the card.
            updaterRefreshStatusUI();
        } catch (e) { /* ignore */ }
    }

    function updaterRenderUpdates() {
        const c = document.getElementById('updater-table');
        if (!plugins.length) {
            c.innerHTML = '<div class="text-gray-500 text-sm py-8 text-center">No plugins installed.</div>';
            return;
        }

        let html = `<div class="grid grid-cols-[1fr_auto_auto_auto_auto_auto] gap-x-4 text-xs text-gray-500 font-semibold uppercase tracking-wider px-3 py-2 border-b border-gray-800">
            <span>Plugin</span>
            <span class="w-28 text-center hidden sm:block" title="Installed version (and short commit sha)">Local</span>
            <span class="w-28 text-center hidden sm:block" title="Latest version on the tracked branch">Remote</span>
            <span class="w-28 text-center">Status</span>
            <span class="w-20 text-center" title="Exclude from automatic updates">Exclude</span>
            <span class="w-44 text-center">Action</span>
        </div>`;

        for (const p of plugins) {
            const u = updates[p.id];
            const err = updateErrors[p.id];
            const isExcluded = excluded.has(p.id);
            const isSelf = p.id === 'update_manager';
            const isBundled = bundledIds.has(p.id) || p.bundled === true;
            const src = sources[p.id] || {};
            const localVersion = u && u.local_version || src.local_version || '';
            const remoteVersion = u && u.remote_version || src.remote_version || '';
            const localShaShort = u && u.local || '';
            const remoteShaShort = u && u.remote || '';

            let statusHtml, actionHtml, rowBg, localStr = '', remoteStr = '';
            const isPendingRestart = _pendingRestart.has(p.id);
            // A previous Update click failed. Surface the backend's actual
            // error inline (in the wide name column) so it's readable, not
            // hidden in a hover title. Cleared on a successful update or a
            // fresh re-check. Suppressed once the row is pending-restart.
            const failMsg = (!isPendingRestart && updateFailures[p.id]) ? updateFailures[p.id] : '';
            const failHtml = failMsg
                ? `<div class="text-[10px] text-red-400 mt-1 break-words" title="${esc(failMsg)}">Update failed: ${esc(failMsg)}</div>`
                : '';
            if (isPendingRestart) {
                // Update was applied but the new code isn't loaded
                // until the user restarts. Override "Update available"
                // (which the bulk endpoint may still report until the
                // marker hits the GitHub cache) so the row reflects
                // local reality immediately after the click.
                rowBg = 'bg-dark-700/40';
                statusHtml = `<span class="text-amber-400 font-semibold text-xs">Updated · restart to apply</span>
                    <div class="text-[10px] text-gray-600 mt-0.5">Click “Restart now” above</div>`;
                actionHtml = `<span class="text-gray-600 text-xs">—</span>`;
            } else if (isBundled) {
                // Bundled plugins can be updated now (the update lands as
                // a writable override copy on desktop, or in place on
                // docker/source). Show the Update action when one is
                // available; otherwise badge it as Bundled. Check /
                // Versions are still offered below the status.
                const bundledChip = `<span class="text-sky-400 font-semibold text-xs inline-flex items-center gap-1">
                        <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z"/></svg>
                        Bundled
                    </span>`;
                if (u) {
                    rowBg = 'bg-dark-700/40';
                    localStr = u.local;
                    remoteStr = u.remote;
                    statusHtml = `<span class="text-amber-400 font-semibold text-xs">Update available</span>
                        <div class="text-[10px] text-gray-600 mt-0.5">Bundled · ${esc(u.repo)} · ${esc(u.branch)}</div>`;
                    actionHtml = `<button data-plugin-id="${esc(p.id)}" onclick="updaterUpdate(this)"
                        class="bg-accent/20 hover:bg-accent/30 text-accent px-3 py-1 rounded-lg text-xs transition">Update</button>`;
                } else {
                    rowBg = 'bg-dark-800/30';
                    const sub = (src && src.url)
                        ? 'Up to date · updatable from GitHub'
                        : 'Managed by slopsmith core';
                    statusHtml = bundledChip + `<div class="text-[10px] text-gray-600 mt-0.5">${sub}</div>`;
                    actionHtml = `<span class="text-gray-700 text-xs">—</span>`;
                }
            } else if (isExcluded) {
                rowBg = 'bg-dark-900/40 opacity-60';
                statusHtml = `<span class="text-gray-500 font-semibold text-xs">Excluded</span>
                    <div class="text-[10px] text-gray-600 mt-0.5">Updates disabled</div>`;
                actionHtml = `<button data-plugin-id="${esc(p.id)}" onclick="updaterUninstall(this)"
                    class="text-gray-600 hover:text-red-400 text-xs transition">Uninstall</button>`;
            } else if (u) {
                rowBg = 'bg-dark-700/40';
                localStr = u.local;
                remoteStr = u.remote;
                statusHtml = `<span class="text-amber-400 font-semibold text-xs">Update available</span>
                    <div class="text-[10px] text-gray-600 mt-0.5">${esc(u.repo)} · ${esc(u.branch)}</div>`;
                actionHtml = `<button data-plugin-id="${esc(p.id)}" onclick="updaterUpdate(this)"
                    class="bg-accent/20 hover:bg-accent/30 text-accent px-3 py-1 rounded-lg text-xs transition">Update</button>`;
            } else if (err) {
                rowBg = 'bg-dark-800/30';
                const errObj = (typeof err === 'object' && err !== null) ? err : { code: 'error', message: String(err) };
                if (errObj.code === 'branch_not_on_remote') {
                    const br = errObj.branch || 'unknown';
                    statusHtml = `<span class="text-sky-400 font-semibold text-xs" title="Switch to the published branch (usually main), or push '${esc(br)}' to origin">Branch not published</span>
                        <div class="text-[10px] text-gray-600 mt-0.5">Local branch <code class="text-gray-400">${esc(br)}</code> not on remote</div>`;
                } else if (errObj.code === 'source_unresolved') {
                    statusHtml = `<span class="text-amber-400 text-xs" title="${esc(errObj.message || '')}">Source unknown</span>
                        <div class="text-[10px] text-gray-600 mt-0.5">Re-install via Browse to record its repo</div>`;
                } else if (errObj.code === 'manifest_not_found') {
                    const br = errObj.branch ? esc(errObj.branch) : '';
                    statusHtml = `<span class="text-amber-400 text-xs" title="${esc(errObj.message || '')}">Manifest not found</span>
                        <div class="text-[10px] text-gray-600 mt-0.5">${br ? `Branch <code class="text-gray-400">${br}</code> / repo / plugin.json may be missing` : 'Branch, repo, or plugin.json may be missing'} — click Check for details</div>`;
                } else if (errObj.code === 'manifest_no_version') {
                    statusHtml = `<span class="text-amber-400 text-xs" title="${esc(errObj.message || '')}">Manifest has no version</span>
                        <div class="text-[10px] text-gray-600 mt-0.5">Plugin author needs to add a version field</div>`;
                } else if (errObj.code === 'version_unavailable') {
                    statusHtml = `<span class="text-amber-400 text-xs" title="${esc(errObj.message || '')}">Couldn't reach repo</span>
                        <div class="text-[10px] text-gray-600 mt-0.5">Click Check to retry</div>`;
                } else {
                    statusHtml = `<span class="text-red-400 text-xs" title="${esc(errObj.message || 'Check failed')}">Check failed</span>
                        <div class="text-[10px] text-gray-600 mt-0.5">Click Check to retry</div>`;
                }
                actionHtml = `<span class="text-gray-600 text-xs">—</span>`;
            } else if (isSelf) {
                rowBg = 'bg-dark-800/30';
                if (u) {
                    localStr = u.local;
                    remoteStr = u.remote;
                    statusHtml = `<span class="text-amber-400 font-semibold text-xs">Update available</span>
                        <div class="text-[10px] text-gray-600 mt-0.5">${esc(u.repo)} · ${esc(u.branch)}</div>`;
                    actionHtml = `<button data-plugin-id="${esc(p.id)}" onclick="updaterUpdate(this)"
                        class="bg-accent/20 hover:bg-accent/30 text-accent px-3 py-1 rounded-lg text-xs transition">Update</button>`;
                } else {
                    statusHtml = `<span class="text-gray-500 text-xs">Self</span>`;
                    actionHtml = `<span class="text-gray-600 text-xs">—</span>`;
                }
            } else {
                rowBg = 'bg-dark-800/30';
                statusHtml = `<span class="text-green-400 font-semibold text-xs">Up to date</span>`;
                actionHtml = `<button data-plugin-id="${esc(p.id)}" onclick="updaterUninstall(this)"
                    class="text-gray-600 hover:text-red-400 text-xs transition">Uninstall</button>`;
            }

            const exclCheckbox = isSelf
                ? '<span class="text-gray-700 text-xs">—</span>'
                : `<label class="inline-flex items-center justify-center cursor-pointer" title="Exclude this plugin from update checks and bulk updates">
                    <input type="checkbox" data-plugin-id="${esc(p.id)}" onchange="updaterToggleExclude(this)"
                        ${isExcluded ? 'checked' : ''}
                        class="accent-amber-500 w-3.5 h-3.5 rounded">
                </label>`;

            const nameHtml = (src && src.url)
                ? `<a href="${esc(src.url)}" target="_blank" rel="noopener"
                        class="text-sm text-white hover:text-accent hover:underline truncate inline-flex items-center gap-1"
                        title="${esc(src.repo)} · open on GitHub">${esc(p.name)}
                        <svg class="w-3 h-3 text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14"/></svg>
                    </a>`
                : `<div class="text-sm text-white truncate">${esc(p.name)}</div>`;

            // Version pickers are useful for any plugin that the
            // backend can resolve a remote source for — even up-to-date
            // ones (downgrade flow). Hide for bundled plugins (managed
            // by core) and for the update_manager itself if the user
            // is mid-self-update flow (avoids surprise during pending
            // restart). The button is plain text so it doesn't compete
            // visually with the primary Update / Uninstall action.
            const canPickVersion = !isExcluded && !!(src && src.url);
            const versionsBtn = canPickVersion
                ? `<button data-plugin-id="${esc(p.id)}" onclick="updaterShowVersions(this)"
                        class="text-gray-500 hover:text-accent text-xs transition"
                        title="Pin this plugin to a specific version (upgrade or downgrade)">Versions</button>`
                : '';

            // Per-row recheck, shown for every plugin (bundled included).
            // The backend's /check/{id} endpoint does the full check,
            // force-refreshes the registry if the source couldn't be
            // resolved on the cold pass, and never depends on the bulk
            // pass having succeeded. (The bulk /updates pass makes zero
            // api.github.com calls, so the entire 60/hour budget is
            // available for these on-demand single-plugin checks — an
            // end user can always re-check and update any individual
            // plugin, even right after a cold first run.) Bundled plugins
            // resolve their upstream the same way and can report (and
            // apply) updates, so they get the Check button too.
            const canRecheck = true;
            const checking = _inflightChecks.has(p.id);
            // Excluded rows skip the GitHub round-trip on the backend
            // and only re-sync local state — phrase the tooltip
            // accordingly so users aren't promised a network call that
            // won't happen.
            const checkTitle = isExcluded
                ? 'Re-sync excluded state from server'
                : 'Re-check this plugin against GitHub now';
            const checkBtn = canRecheck
                ? `<button data-plugin-id="${esc(p.id)}" onclick="updaterCheckOne(this)"
                        ${checking ? 'disabled' : ''}
                        class="text-gray-500 hover:text-white text-xs transition ${checking ? 'opacity-60 cursor-wait' : ''}"
                        title="${esc(checkTitle)}">${checking ? '…' : 'Check'}</button>`
                : '';

            const localCell = (localVersion || localShaShort)
                ? `<div class="leading-tight">
                        ${localVersion ? `<div class="text-gray-200 text-xs">${esc(localVersion)}</div>` : ''}
                        ${localShaShort ? `<div class="text-gray-500 text-[10px] font-mono">${esc(localShaShort)}</div>` : ''}
                    </div>`
                : '<span class="text-gray-700 text-xs">—</span>';
            const remoteCell = (remoteVersion || remoteShaShort)
                ? `<div class="leading-tight">
                        ${remoteVersion ? `<div class="text-gray-200 text-xs">${esc(remoteVersion)}</div>` : ''}
                        ${remoteShaShort ? `<div class="text-gray-500 text-[10px] font-mono">${esc(remoteShaShort)}</div>` : ''}
                    </div>`
                : '<span class="text-gray-700 text-xs">—</span>';

            html += `<div class="grid grid-cols-[1fr_auto_auto_auto_auto_auto] gap-x-4 items-center px-3 py-2.5 rounded-lg ${rowBg} transition relative" data-row-id="${esc(p.id)}">
                <div class="min-w-0">
                    ${nameHtml}
                    <div class="text-xs text-gray-500 truncate">${esc(p.id)}</div>
                    ${failHtml}
                </div>
                <span class="w-28 text-center hidden sm:block">${localCell}</span>
                <span class="w-28 text-center hidden sm:block">${remoteCell}</span>
                <span class="w-28 text-center">${statusHtml}</span>
                <span class="w-20 text-center">${exclCheckbox}</span>
                <span class="w-44 text-center">
                    <div class="flex items-center justify-center gap-2">
                        ${checkBtn}
                        ${actionHtml}
                        ${versionsBtn}
                    </div>
                </span>
            </div>`;
        }
        c.innerHTML = html;
    }

    // ── Version picker (issue #5: pin to specific version) ─────────────
    //
    // A small popover anchored to the row's "Versions" button. Lazy-
    // fetches /versions/{id} only when the user opens the picker, so
    // the per-page Check-for-updates pass doesn't pay the rate-limit
    // cost (one tags listing + N plugin.json fetches) for plugins the
    // user never opens.
    let _activeVersionPopover = null;
    window.updaterShowVersions = async function (btn) {
        // Toggle: clicking the same button again closes the popover.
        if (_activeVersionPopover && _activeVersionPopover.dataset.pluginId === btn.dataset.pluginId) {
            _activeVersionPopover.remove();
            _activeVersionPopover = null;
            return;
        }
        if (_activeVersionPopover) {
            _activeVersionPopover.remove();
            _activeVersionPopover = null;
        }
        const id = btn.dataset.pluginId;
        const row = btn.closest('[data-row-id]');
        if (!row) return;
        const pop = document.createElement('div');
        pop.dataset.pluginId = id;
        pop.className = 'absolute right-3 top-full mt-1 z-50 bg-dark-800 border border-gray-700 rounded-lg shadow-xl text-xs w-72 max-h-72 overflow-y-auto';
        pop.innerHTML = '<div class="px-3 py-2 text-gray-500">Loading versions…</div>';
        row.appendChild(pop);
        _activeVersionPopover = pop;
        // Outside-click dismiss. Set up after a tick so the click that
        // opened the popover doesn't immediately close it.
        setTimeout(() => {
            const off = (ev) => {
                if (!pop.contains(ev.target) && ev.target !== btn) {
                    pop.remove();
                    if (_activeVersionPopover === pop) _activeVersionPopover = null;
                    document.removeEventListener('click', off, true);
                }
            };
            document.addEventListener('click', off, true);
        }, 0);
        try {
            const r = await fetch(API + '/versions/' + encodeURIComponent(id));
            const data = await r.json();
            if (data.error) {
                pop.innerHTML = `<div class="px-3 py-2 text-red-400">${esc(data.error)}</div>`;
                return;
            }
            const versions = data.versions || [];
            if (!versions.length) {
                pop.innerHTML = `
                    <div class="px-3 py-2 text-gray-400">
                        No versioned releases found.
                        <div class="text-[10px] text-gray-600 mt-1">
                            This repo has no git tags and no plugin.json version-bump commits we can pin to.
                            Use the regular <em>Update</em> button to track the latest commit on the branch.
                        </div>
                    </div>`;
                return;
            }
            const currentSha = data.current_sha || '';
            const currentVer = data.current_version || '';
            const items = versions.map(v => {
                const isCurrent = (v.sha && v.sha === currentSha) || (v.version && v.version === currentVer);
                const verLabel = v.version || v.label || '(unknown)';
                const tagBadge = v.source === 'tag'
                    ? `<span class="ml-2 text-[10px] px-1.5 py-0.5 rounded bg-sky-900/40 text-sky-300">tag ${esc(v.label)}</span>`
                    : '';
                const shaBadge = v.sha
                    ? `<span class="ml-2 text-[10px] text-gray-500 font-mono">${esc(v.sha.slice(0, 7))}</span>`
                    : '';
                const currentBadge = isCurrent
                    ? '<span class="ml-2 text-[10px] px-1.5 py-0.5 rounded bg-green-900/40 text-green-300">current</span>'
                    : '';
                const click = isCurrent
                    ? ''
                    : `onclick="updaterUpdateAtRef('${esc(id)}', '${esc(v.ref)}', '${esc(verLabel)}')"`;
                const interactive = isCurrent ? 'cursor-default text-gray-500' : 'cursor-pointer hover:bg-dark-700 text-gray-200';
                return `<div ${click} class="px-3 py-2 ${interactive} border-b border-gray-800 last:border-b-0 flex items-center justify-between">
                    <span class="truncate">${esc(verLabel)}${tagBadge}${currentBadge}</span>
                    <span>${shaBadge}</span>
                </div>`;
            }).join('');
            pop.innerHTML = `<div class="px-3 py-2 text-[10px] text-gray-500 border-b border-gray-800 sticky top-0 bg-dark-800">
                    Pick a version (downgrade or upgrade)
                </div>${items}`;
        } catch (e) {
            pop.innerHTML = `<div class="px-3 py-2 text-red-400">Failed: ${esc(e.message)}</div>`;
        }
    };

    window.updaterUpdateAtRef = async function (id, ref, label) {
        if (!confirm(`Switch ${id} to ${label}?\n\nThis replaces the plugin's files with the version at ref:\n  ${ref}\n\nA restart is required to pick up the change.`)) return;
        if (_activeVersionPopover) {
            _activeVersionPopover.remove();
            _activeVersionPopover = null;
        }
        try {
            const resp = await fetch(API + '/update/' + encodeURIComponent(id), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ ref }),
            });
            const data = await resp.json();
            if (data.ok) {
                localStorage.setItem(RESTART_KEY, '1');
                document.getElementById('updater-restart-banner').classList.remove('hidden');
                // Re-check so the row reflects the new local sha/version.
                if (typeof updaterCheck === 'function') updaterCheck();
            } else {
                alert('Failed to switch version: ' + (data.error || 'unknown error'));
            }
        } catch (e) {
            alert('Failed to switch version: ' + e.message);
        }
    };

    window.updaterToggleExclude = async function (cb) {
        const id = cb.dataset.pluginId;
        const shouldExclude = cb.checked;
        cb.disabled = true;
        try {
            const resp = await fetch(API + '/exclusions', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ plugin_id: id, excluded: shouldExclude }),
            });
            const data = await resp.json();
            if (data.ok) {
                excluded = new Set(data.excluded || []);
                if (id === CORE_KEY) {
                    if (coreStatus) coreStatus.excluded = shouldExclude;
                    updaterRenderCore();
                } else if (shouldExclude) {
                    delete updates[id];
                    delete updateErrors[id];
                }
                updaterRenderUpdates();
                updaterRefreshStatusUI();
            } else {
                cb.checked = !shouldExclude;
                cb.title = data.error || 'Failed';
            }
        } catch (e) {
            cb.checked = !shouldExclude;
            cb.title = e.message;
        } finally {
            cb.disabled = false;
        }
    };

    // ── Update one ─────────────────────────────────────────────────────
    window.updaterUpdate = async function (btn) {
        return updaterDoUpdate(btn.dataset.pluginId, btn);
    };

    // ── Re-check one plugin ────────────────────────────────────────────
    //
    // Surfaced as a per-row "Check" button alongside the primary action.
    // Calls the per-plugin endpoint (cheap: one slot of bulk's parallel
    // pool, ETag-cached) and merges the result back into the same shared
    // state the bulk flow uses, then re-renders. Lets the user retry a
    // single failed row after the cold-pass burnt through the anonymous
    // GitHub rate-limit window.
    //
    // In-flight state is tracked by plugin id (not by DOM element) so
    // updaterRenderUpdates() can render the new button in its disabled
    // spinner state. This avoids mutating the original click target after
    // a re-render has already replaced it.
    // Per-id deferred error slots. Multiple per-row checks can race,
    // so a single shared slot would let a late failure clobber an
    // earlier one before the earlier row got its '!' indicator.
    const _checkOneErrors = new Map();
    window.updaterCheckOne = async function (btn) {
        const id = btn.dataset.pluginId;
        if (_inflightChecks.has(id)) return;
        // A re-check supersedes a prior failed Update — drop the stale
        // failure note so the row reflects the fresh check result.
        delete updateFailures[id];
        _inflightChecks.add(id);
        updaterRenderUpdates();
        try {
            const resp = await fetch(API + '/check/' + encodeURIComponent(id));
            const data = await resp.json();
            if (data.error && !data.plugin_id) {
                // Validation-level error (bad id, plugin not found).
                // Surface on the freshly-rendered button after we drop
                // the in-flight flag below.
                _checkOneErrors.set(id, data.error);
                return;
            }
            // Pending-restart wins — backend may still report the
            // pre-update mismatch until its cache turns over and the
            // marker write lands. Don't let the per-row recheck
            // revert the "Updated · restart to apply" UI.
            if (_pendingRestart.has(id)) {
                delete updates[id];
                delete updateErrors[id];
            } else {
                if (data.update) updates[id] = data.update; else delete updates[id];
                if (data.error)  updateErrors[id] = data.error; else delete updateErrors[id];
            }
            // Replace, don't merge — the backend already returns the full
            // per-plugin source (Phase 1 fields + _check_one's
            // source_updates). Merging would leave stale repo/url/branch
            // fields if a plugin's source can no longer be resolved.
            if (data.source && Object.keys(data.source).length) {
                sources[id] = data.source;
            } else {
                delete sources[id];
            }
            if (data.excluded) excluded.add(id); else excluded.delete(id);
            if (data.bundled) bundledIds.add(id); else bundledIds.delete(id);
        } catch (e) {
            _checkOneErrors.set(id, e.message);
        } finally {
            _inflightChecks.delete(id);
            updaterRenderUpdates();
            updaterRefreshStatusUI();
            // Apply any deferred error indicator to the freshly-rendered
            // button. Done after re-render so we mutate the live element,
            // not a detached one. Per-id slot means concurrent failing
            // checks don't clobber each other.
            if (_checkOneErrors.has(id)) {
                const message = _checkOneErrors.get(id);
                _checkOneErrors.delete(id);
                const row = document.querySelector('[data-row-id="' + CSS.escape(id) + '"]');
                const newBtn = row ? row.querySelector('button[onclick^="updaterCheckOne("]') : null;
                if (newBtn) {
                    newBtn.textContent = '!';
                    newBtn.title = message;
                }
            }
        }
    };

    async function updaterDoUpdate(id, btn) {
        btn.disabled = true;
        btn.textContent = 'Updating...';
        try {
            const resp = await fetch(API + '/update/' + encodeURIComponent(id), { method: 'POST' });
            const data = await resp.json();
            if (data.ok) {
                // Both branches need a restart to load the new code,
                // so clear the row's stale "Update available" state and
                // mark it pending-restart. Re-render flips Status to
                // "Updated · restart to apply" immediately.
                delete updates[id];
                delete updateErrors[id];
                delete updateFailures[id];
                markPendingRestart(id);
                localStorage.setItem(RESTART_KEY, '1');
                document.getElementById('updater-restart-banner').classList.remove('hidden');
                updaterRenderUpdates();
                updaterRefreshStatusUI();
                return true;
            }
            // Surface the real reason inline (not just a hover title) and
            // re-render so the retry button stays. The backend already
            // returns the underlying exception in data.error.
            updateFailures[id] = data.error || 'Unknown error';
            updaterRenderUpdates();
            return false;
        } catch (e) {
            updateFailures[id] = e.message || 'Request failed';
            updaterRenderUpdates();
            return false;
        }
    }

    window.updaterUpdateAll = async function () {
        const allBtn = document.getElementById('updater-update-all-btn');
        allBtn.disabled = true;
        allBtn.textContent = 'Updating all...';

        // Core first — if it's behind, applicable, and not blocked.
        if (coreStatus && coreStatus.behind && !coreStatus.excluded && !coreStatus.rebuild_required) {
            const coreCard = document.getElementById('updater-core-card');
            const coreBtn = coreCard ? coreCard.querySelector('button[onclick^="updaterUpdateCore"]') : null;
            if (coreBtn) await updaterDoCoreUpdate(coreBtn);
        }

        for (const id of Object.keys(updates)) {
            if (excluded.has(id)) continue;
            // Pending-restart plugins are functionally up-to-date —
            // the new code is on disk; only the running process is
            // stale. Skip so Update-all doesn't re-pick them.
            if (_pendingRestart.has(id)) continue;
            const row = document.querySelector('[data-row-id="' + CSS.escape(id) + '"]');
            // Match the Update button specifically — the row may also
            // hold a "Check" button with data-plugin-id since v1.8.0.
            const btn = row ? row.querySelector('button[onclick^="updaterUpdate("]') : null;
            if (!btn) continue;
            await updaterDoUpdate(id, btn);
        }
        allBtn.classList.add('hidden');
        allBtn.disabled = false;
        allBtn.textContent = 'Update all';
    };

    // ── Uninstall ──────────────────────────────────────────────────────
    window.updaterUninstall = async function (btn) {
        const id = btn.dataset.pluginId;
        if (!confirm('Uninstall plugin "' + id + '"?\n\nThis removes its directory. Any local edits will be lost.')) return;
        btn.disabled = true;
        btn.textContent = 'Removing...';
        try {
            const resp = await fetch(API + '/uninstall/' + encodeURIComponent(id), { method: 'POST' });
            const data = await resp.json();
            if (data.ok) {
                btn.outerHTML = '<span class="text-green-400 text-xs font-semibold">Removed</span>';
                localStorage.setItem(RESTART_KEY, '1');
                document.getElementById('updater-restart-banner').classList.remove('hidden');
            } else {
                btn.disabled = false;
                btn.textContent = 'Failed';
                btn.title = data.error || '';
            }
        } catch (e) {
            btn.disabled = false;
            btn.textContent = 'Error';
            btn.title = e.message;
        }
    };

    // ── Registry / Browse ──────────────────────────────────────────────
    window.updaterLoadRegistry = async function () {
        const btn = document.getElementById('updater-reload-btn');
        const loading = document.getElementById('updater-browse-loading');
        const status = document.getElementById('updater-browse-status');
        btn.disabled = true;
        btn.textContent = 'Loading...';
        loading.classList.remove('hidden');
        status.textContent = '';
        try {
            const resp = await fetch(API + '/registry');
            const data = await resp.json();
            if (data.error) throw new Error(data.error);
            registry = data.entries || [];
            status.textContent = registry.length + ' plugins in registry';
            updaterRenderBrowse();
        } catch (e) {
            status.textContent = 'Failed: ' + e.message;
        } finally {
            btn.disabled = false;
            btn.textContent = 'Reload registry';
            loading.classList.add('hidden');
        }
    };

    window.updaterRenderBrowse = function () {
        const container = document.getElementById('updater-browse-list');
        const filter = (document.getElementById('updater-browse-filter').value || '').toLowerCase().trim();
        const installedSet = new Set(plugins.map(p => p.id));

        let rows = registry;
        if (filter) {
            rows = rows.filter(r =>
                r.name.toLowerCase().includes(filter) ||
                r.description.toLowerCase().includes(filter) ||
                r.dirname.toLowerCase().includes(filter) ||
                r.repo.toLowerCase().includes(filter));
        }

        if (!rows.length) {
            container.innerHTML = '<div class="text-gray-500 text-sm py-8 text-center">No plugins match.</div>';
            return;
        }

        let html = '';
        for (const r of rows) {
            const installed = r.installed || installedSet.has(r.dirname);
            const overridesBundled = !!r.overrides_bundled;
            let action;
            if (installed && overridesBundled) {
                action = '<span class="text-sky-400 text-xs font-semibold" title="Ships with slopsmith core">Bundled</span>';
            } else if (installed) {
                action = '<span class="text-green-400 text-xs font-semibold">Installed</span>';
            } else {
                action = `<button data-url="${esc(r.url)}" data-dirname="${esc(r.dirname)}"
                    data-overrides-bundled="${overridesBundled ? '1' : ''}" onclick="updaterInstall(this)"
                    class="bg-accent/20 hover:bg-accent/30 text-accent px-3 py-1 rounded-lg text-xs transition">Install</button>`;
            }

            const bundledBadge = overridesBundled
                ? `<span class="text-[10px] text-sky-300 bg-sky-900/30 border border-sky-500/30 rounded px-1.5 py-0.5"
                        title="A bundled copy of this plugin already ships with slopsmith. Installing this would override it.">Overrides bundled</span>`
                : '';

            html += `<div class="flex items-start gap-3 bg-dark-700/40 rounded-lg px-4 py-3">
                <div class="flex-1 min-w-0">
                    <div class="flex items-center gap-2 flex-wrap">
                        <a href="${esc(r.url)}" target="_blank" class="text-sm text-white hover:text-accent truncate">${esc(r.name)}</a>
                        <span class="text-[10px] text-gray-600 font-mono">${esc(r.repo)}</span>
                        ${bundledBadge}
                    </div>
                    <div class="text-xs text-gray-500 mt-0.5">${esc(r.description)}</div>
                    <div class="text-[10px] text-gray-600 mt-1 font-mono">dir: ${esc(r.dirname)}</div>
                </div>
                <div class="shrink-0 self-center">${action}</div>
            </div>`;
        }
        container.innerHTML = html;
    };

    window.updaterInstall = async function (btn) {
        const url = btn.dataset.url;
        const dirname = btn.dataset.dirname;
        if (btn.dataset.overridesBundled === '1') {
            const ok = confirm(
                'A bundled copy of "' + dirname + '" already ships with slopsmith.\n\n' +
                'Installing this version will override the bundled copy. Continue?'
            );
            if (!ok) return;
        }
        btn.disabled = true;
        btn.textContent = 'Installing...';
        try {
            const resp = await fetch(API + '/install', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ url, dirname }),
            });
            const data = await resp.json();
            if (data.ok) {
                btn.outerHTML = '<span class="text-green-400 text-xs font-semibold">Installed</span>';
                localStorage.setItem(RESTART_KEY, '1');
                document.getElementById('updater-restart-banner').classList.remove('hidden');
                // Refresh cached plugin list so Updates tab reflects the new install
                try {
                    const pRes = await fetch('/api/plugins');
                    plugins = await pRes.json();
                } catch (e) { /* ignore */ }
            } else {
                btn.disabled = false;
                btn.textContent = 'Failed';
                btn.title = data.error || '';
                btn.className = 'bg-red-900/30 text-red-400 px-3 py-1 rounded-lg text-xs';
            }
        } catch (e) {
            btn.disabled = false;
            btn.textContent = 'Error';
            btn.title = e.message;
        }
    };

    // Install a plugin from an arbitrary GitHub URL — covers plugins not
    // listed in the registry. `dirname` is optional; the server derives
    // one from the repo slug when it's blank.
    window.updaterInstallFromUrl = async function () {
        const urlEl = document.getElementById('updater-install-url');
        const dirEl = document.getElementById('updater-install-dirname');
        const btn = document.getElementById('updater-install-url-btn');
        const msg = document.getElementById('updater-install-url-msg');
        const url = (urlEl.value || '').trim();
        const dirname = (dirEl.value || '').trim();
        if (!url) {
            showInstallMsg(msg, 'Enter a GitHub repo URL.', false);
            urlEl.focus();
            return;
        }
        btn.disabled = true;
        btn.textContent = 'Installing...';
        msg.classList.add('hidden');
        try {
            const payload = { url };
            if (dirname) payload.dirname = dirname;
            const resp = await fetch(API + '/install', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            const data = await resp.json();
            if (data.ok) {
                showInstallMsg(msg, 'Installed ' + (data.repo || url) + ' into "' + data.dirname + '". Restart to load it.', true);
                urlEl.value = '';
                dirEl.value = '';
                localStorage.setItem(RESTART_KEY, '1');
                document.getElementById('updater-restart-banner').classList.remove('hidden');
                // Refresh the cached plugin list so the Updates tab and the
                // registry's Installed badges reflect the new install.
                try {
                    const pRes = await fetch('/api/plugins');
                    plugins = await pRes.json();
                } catch (e) { /* ignore */ }
                if (registry.length) updaterRenderBrowse();
            } else {
                showInstallMsg(msg, data.error || 'Install failed.', false);
            }
        } catch (e) {
            showInstallMsg(msg, 'Error: ' + e.message, false);
        } finally {
            btn.disabled = false;
            btn.textContent = 'Install';
        }
    };

    function showInstallMsg(el, text, success) {
        el.textContent = text;
        el.className = 'mt-2 text-xs ' + (success ? 'text-green-400' : 'text-red-400');
        el.classList.remove('hidden');
    }

    // ── Restart banner ─────────────────────────────────────────────────
    window.updaterCopyCmd = async function () {
        const btn = document.getElementById('updater-copy-btn');
        try {
            await navigator.clipboard.writeText('docker compose restart');
            btn.textContent = 'Copied';
            setTimeout(() => { btn.textContent = 'Copy'; }, 1500);
        } catch (e) {
            btn.textContent = 'Copy failed';
        }
    };

    window.updaterRestart = async function () {
        const btn = document.getElementById('updater-restart-btn');
        const copyBtn = document.getElementById('updater-copy-btn');
        const statusEl = document.getElementById('updater-restart-status');
        const origLabel = btn.textContent;
        btn.disabled = true;
        btn.textContent = 'Restarting...';
        if (copyBtn) copyBtn.disabled = true;
        statusEl.classList.remove('hidden');
        statusEl.className = 'text-xs text-gray-400 mb-2';
        statusEl.textContent = 'Sending restart signal...';

        if (isDesktop) {
            try { await window.slopsmithDesktop.plugins.restart(); } catch (e) { /* ignore */ }
        } else {
            try { await fetch(API + '/restart', { method: 'POST' }); } catch (e) { /* connection drop expected */ }
        }

        statusEl.textContent = 'Waiting for server to come back...';
        const start = Date.now();
        const deadline = start + 30000;
        let back = false;
        // Give uvicorn a moment to tear down before we start polling
        await new Promise(r => setTimeout(r, 1500));
        while (Date.now() < deadline) {
            try {
                const r = await fetch('/api/plugins', { cache: 'no-store' });
                if (r.ok) { back = true; break; }
            } catch (e) { /* still down */ }
            await new Promise(r => setTimeout(r, 1000));
        }

        if (back) {
            localStorage.removeItem(RESTART_KEY);
            clearPendingRestart();
            document.getElementById('updater-restart-banner').classList.add('hidden');
            const elapsed = Math.round((Date.now() - start) / 100) / 10;
            const s = document.getElementById('updater-status');
            if (s) {
                s.textContent = 'Restarted in ' + elapsed + 's.';
                s.className = 'text-xs text-green-400';
            }
            if (currentTab === 'updates') updaterCheck();
        } else {
            btn.disabled = false;
            btn.textContent = origLabel;
            if (copyBtn) copyBtn.disabled = false;
            statusEl.className = 'text-xs text-red-400 mb-2';
            statusEl.textContent = 'Server did not respond within 30s. Try restarting the app.';
        }
    };

    window.updaterDismissBanner = function () {
        document.getElementById('updater-restart-banner').classList.add('hidden');
        localStorage.removeItem(RESTART_KEY);
        clearPendingRestart();
        updaterRenderUpdates();
    };

    // ── Storage tab ────────────────────────────────────────────────────
    async function updaterLoadStorage(refresh) {
        if (storageLoading) return;
        storageLoading = true;
        const loading = document.getElementById('updater-storage-loading');
        const content = document.getElementById('updater-storage-content');
        const refreshBtn = document.getElementById('updater-storage-refresh-btn');
        const statusEl = document.getElementById('updater-storage-status');
        if (loading) loading.classList.remove('hidden');
        if (content) content.innerHTML = '';
        if (refreshBtn) refreshBtn.disabled = true;
        if (statusEl) { statusEl.textContent = ''; statusEl.className = 'text-xs text-gray-500'; }
        try {
            const url = API + '/storage' + (refresh ? '?refresh=1' : '');
            const r = await fetch(url);
            const j = await r.json();
            if (j.error) {
                if (statusEl) {
                    statusEl.textContent = 'Failed: ' + j.error;
                    statusEl.className = 'text-xs text-red-400';
                }
                return;
            }
            storageData = j;
            updaterRenderStorage();
        } catch (e) {
            if (statusEl) {
                statusEl.textContent = 'Failed: ' + e.message;
                statusEl.className = 'text-xs text-red-400';
            }
        } finally {
            storageLoading = false;
            if (loading) loading.classList.add('hidden');
            if (refreshBtn) refreshBtn.disabled = false;
        }
    }

    window.updaterStorageRefresh = function () { updaterLoadStorage(true); };

    function formatBytes(n) {
        if (typeof n !== 'number' || !isFinite(n) || n <= 0) return '0 B';
        const units = ['B', 'KB', 'MB', 'GB', 'TB'];
        let i = 0;
        let v = n;
        while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
        return (i === 0 ? v.toFixed(0) : v.toFixed(v >= 10 ? 1 : 2)) + ' ' + units[i];
    }

    // Describe a per-plugin row's size composition. Total = source dir
    // under PLUGINS_DIR + any declared user-data paths under config_dir;
    // call out the data contribution explicitly when a plugin declares
    // server_files so the user knows what the number covers.
    function pluginSizeDescription(p) {
        const src = formatBytes(p.source_size_bytes || 0);
        if (p.declares_server_files) {
            const data = formatBytes(p.data_size_bytes || 0);
            const n = p.declared_paths.length;
            return `Source ${src} + ${data} across ${n} declared user-data path${n === 1 ? '' : 's'} under your config dir.`;
        }
        return `Source ${src}. No declared user-data paths.`;
    }

    function updaterRenderStorage() {
        const content = document.getElementById('updater-storage-content');
        if (!content || !storageData) return;
        const canOpen = !!storageData.can_open;
        const buckets = storageData.buckets || [];
        const plugins = storageData.plugins || [];

        const bucketRowsHtml = buckets.map(b => storageRowHtml({
            key: b.key,
            label: b.label,
            path: b.path,
            size: b.size_bytes,
            exists: b.exists,
            clearable: b.clearable,
            clearConsequence: b.clear_consequence,
            description: b.description,
            canOpen,
        })).join('');

        const pluginRowsHtml = plugins.length === 0
            ? `<div class="text-gray-500 text-xs py-4 text-center">No installed plugins.</div>`
            : plugins.map(p => storageRowHtml({
                key: 'plugin:' + p.id,
                label: p.name,
                path: p.source_path,
                size: p.size_bytes,
                exists: true,
                clearable: false,
                description: pluginSizeDescription(p),
                canOpen,
            })).join('');

        content.innerHTML = `
            <div class="space-y-2 mb-6">
                <h3 class="text-xs font-semibold uppercase tracking-wider text-gray-500 mb-2">Locations</h3>
                ${bucketRowsHtml}
            </div>
            <div class="space-y-2">
                <h3 class="text-xs font-semibold uppercase tracking-wider text-gray-500 mb-2">Plugins (by size)</h3>
                ${pluginRowsHtml}
            </div>
            ${!canOpen ? `<p class="text-gray-500 text-[11px] mt-4">Open-folder buttons are disabled in non-desktop installs &mdash; use Copy to paste the path into your file manager.</p>` : ''}
        `;
    }

    function storageRowHtml(r) {
        const sizeText = r.exists ? formatBytes(r.size) : 'missing';
        const sizeColor = r.exists ? 'text-gray-300' : 'text-gray-600';
        const openLabel = r.canOpen ? 'Open' : 'Copy path';
        // Embed JS string literals via JSON.stringify, then HTML-escape
        // for safe attribute context. Handles Windows paths with
        // backslashes, paths with quotes, and consequence strings.
        const keyJs = esc(JSON.stringify(r.key));
        const pathJs = esc(JSON.stringify(r.path));
        const consequenceJs = esc(JSON.stringify(r.clearConsequence || ''));
        const openFn = r.canOpen ? `updaterStorageOpen(${keyJs})` : `updaterStorageCopy(${pathJs}, this)`;
        const clearBtn = r.clearable && r.exists && r.size > 0
            ? `<button onclick="updaterStorageClear(${keyJs}, ${r.size}, ${consequenceJs})"
                       class="bg-dark-600 hover:bg-red-900/50 text-gray-400 hover:text-red-400 px-3 py-1.5 rounded-md text-xs transition">Clear</button>`
            : '';
        return `
            <div class="bg-dark-800/50 border border-gray-800 rounded-xl p-4">
                <div class="flex items-start gap-3">
                    <div class="flex-1 min-w-0">
                        <div class="flex items-baseline gap-3 mb-1">
                            <span class="text-sm font-semibold text-gray-200">${esc(r.label)}</span>
                            <span class="text-sm font-mono ${sizeColor}">${esc(sizeText)}</span>
                        </div>
                        <div class="text-[11px] text-gray-500 font-mono break-all mb-1">${esc(r.path)}</div>
                        ${r.description ? `<div class="text-[11px] text-gray-500">${r.description}</div>` : ''}
                    </div>
                    <div class="flex items-center gap-2 shrink-0">
                        <button onclick="${openFn}"
                            class="bg-dark-600 hover:bg-dark-500 text-gray-300 px-3 py-1.5 rounded-md text-xs transition">${openLabel}</button>
                        ${clearBtn}
                    </div>
                </div>
            </div>
        `;
    }

    window.updaterStorageOpen = async function (target) {
        try {
            const r = await fetch(API + '/storage/open', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ target }),
            });
            const j = await r.json();
            if (j.error) {
                const statusEl = document.getElementById('updater-storage-status');
                if (statusEl) {
                    statusEl.textContent = 'Open failed: ' + j.error;
                    statusEl.className = 'text-xs text-red-400';
                }
            }
        } catch (e) {
            const statusEl = document.getElementById('updater-storage-status');
            if (statusEl) {
                statusEl.textContent = 'Open failed: ' + e.message;
                statusEl.className = 'text-xs text-red-400';
            }
        }
    };

    window.updaterStorageCopy = async function (path, btn) {
        try {
            await navigator.clipboard.writeText(path);
            const orig = btn.textContent;
            btn.textContent = 'Copied';
            setTimeout(() => { btn.textContent = orig; }, 1500);
        } catch (e) {
            btn.textContent = 'Copy failed';
        }
    };

    window.updaterStorageClear = async function (target, sizeBytes, consequence) {
        const sizeText = formatBytes(sizeBytes);
        const consequenceText = consequence ? ' ' + consequence : '';
        if (!confirm(`Clear ${sizeText}?${consequenceText}`)) return;
        const statusEl = document.getElementById('updater-storage-status');
        if (statusEl) {
            statusEl.textContent = 'Clearing...';
            statusEl.className = 'text-xs text-gray-400';
        }
        try {
            const r = await fetch(API + '/storage/clear', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ target }),
            });
            const j = await r.json();
            if (j.error) {
                if (statusEl) {
                    statusEl.textContent = 'Clear failed: ' + j.error;
                    statusEl.className = 'text-xs text-red-400';
                }
                return;
            }
            if (statusEl) {
                statusEl.textContent = 'Freed ' + formatBytes(j.freed_bytes || 0) + '.';
                statusEl.className = 'text-xs text-green-400';
            }
            // Re-fetch inventory so the sizes update immediately.
            await updaterLoadStorage(true);
        } catch (e) {
            if (statusEl) {
                statusEl.textContent = 'Clear failed: ' + e.message;
                statusEl.className = 'text-xs text-red-400';
            }
        }
    };

    // ── Utility ────────────────────────────────────────────────────────
    function esc(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
})();

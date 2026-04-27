/* =========================================================
   Avalon Q Controller v2 — frontend
   Vanilla ES2020. Single file. No build step.
   ========================================================= */

(() => {
  "use strict";

  // ============== State ==============
  const state = {
    miners: [],            // [{id, name, host, ...}]
    selectedMinerId: null,
    pools: [],             // for the selected miner
    schedule: null,
    statusByMiner: {},     // miner_id -> last status
    pollTimer: null,
  };

  // ============== DOM ==============
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

  // ============== Network ==============
  async function api(path, opts = {}) {
    const init = {
      method: opts.method || "GET",
      headers: { "Content-Type": "application/json" },
    };
    if (opts.body !== undefined) init.body = JSON.stringify(opts.body);
    const r = await fetch(path, init);
    const text = await r.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = { raw: text }; }
    if (!r.ok) {
      const msg = (data && (data.detail || data.error)) || r.statusText;
      throw new Error(msg);
    }
    return data;
  }

  function toast(message, kind = "ok") {
    const el = $("#toast");
    el.textContent = message;
    el.dataset.kind = kind;
    el.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { el.hidden = true; }, 3500);
  }

  // ============== Formatting ==============
  function fmtNumber(v, digits = 1) {
    if (v == null || isNaN(v)) return "—";
    return Number(v).toFixed(digits);
  }
  function fmtUptime(seconds) {
    if (seconds == null) return "—";
    const s = Number(seconds);
    if (!isFinite(s)) return "—";
    const d = Math.floor(s / 86400);
    const h = Math.floor((s % 86400) / 3600);
    const m = Math.floor((s % 3600) / 60);
    if (d > 0) return `${d}d ${h}h`;
    if (h > 0) return `${h}h ${m}m`;
    return `${m}m`;
  }
  function tHashes(mhs) { return mhs == null ? null : Number(mhs) / 1_000_000; }
  function formatTime(iso) {
    if (!iso) return "—";
    try { return new Date(iso).toLocaleString(); } catch { return iso; }
  }

  // ============== Action option lists ==============
  // Builds the <option>s for action selects, including pool actions for the
  // currently-selected miner.
  function actionOptions(includeNone = true) {
    const base = [
      { v: "off", label: "off — soft-off (standby)" },
      { v: "on", label: "on — soft-on (wake)" },
      { v: "eco", label: "eco — workmode 0 (~800 W)" },
      { v: "standard", label: "standard — workmode 1 (~1300 W)" },
      { v: "super", label: "super — workmode 2 (~1600 W)" },
    ];
    if (includeNone) base.unshift({ v: "none", label: "none — leave miner alone" });
    const pools = (state.pools || []).map(p => ({
      v: `pool:${p.id}`,
      label: `pool — ${p.name}`,
    }));
    return [...base, ...pools];
  }

  function rebuildActionSelects() {
    const opts = actionOptions(true);
    $$('select[data-action-select]').forEach(sel => {
      const cur = sel.value;
      sel.innerHTML = "";
      opts.forEach(o => {
        const opt = document.createElement("option");
        opt.value = o.v;
        opt.textContent = o.label;
        sel.appendChild(opt);
      });
      if (cur && opts.some(o => o.v === cur)) sel.value = cur;
    });
  }

  // ============== Sidebar / fleet list ==============
  async function refreshFleet() {
    try {
      state.miners = await api("/api/miners");
      renderFleetList();
      // Auto-select first miner if none picked
      if (!state.selectedMinerId && state.miners.length) {
        selectMiner(state.miners[0].id);
      } else if (!state.miners.length) {
        showEmpty();
      } else {
        // Re-paint selected
        const cur = state.miners.find(m => m.id === state.selectedMinerId);
        if (!cur) selectMiner(state.miners[0].id);
        else paintSelected(cur);
      }
      updateFleetSummary();
    } catch (e) { toast(e.message, "err"); }
  }

  function renderFleetList() {
    const list = $("#minerList");
    list.innerHTML = "";
    state.miners.forEach(m => {
      const node = $("#minerCardTemplate").content.firstElementChild.cloneNode(true);
      node.dataset.minerId = m.id;
      $(".miner-card__name", node).textContent = m.name;
      $(".miner-card__addr", node).textContent = `${m.host}:${m.port}`;
      const status = m.last_status || {};
      const stats = status.stats || {};
      const dot = $(".dot", node);
      if (m.paused) dot.dataset.state = "paused";
      else if (status.online) dot.dataset.state = "online";
      else if (status.error) dot.dataset.state = "offline";
      else dot.dataset.state = "unknown";
      const ths = stats.ths || tHashes((status.summary || {}).mhs_av);
      $(".miner-card__ths", node).textContent = ths != null ? `${fmtNumber(ths,1)} TH/s` : "— TH/s";
      $(".miner-card__mode", node).textContent = (stats.workmode_name || "").toUpperCase() || "—";
      if (m.id === state.selectedMinerId) node.classList.add("is-active");
      node.addEventListener("click", () => selectMiner(m.id));
      list.appendChild(node);
    });
  }

  function updateFleetSummary() {
    const total = state.miners.length;
    let online = 0, totalTHs = 0, totalW = 0;
    state.miners.forEach(m => {
      const s = m.last_status || {};
      if (s.online) online++;
      const stats = s.stats || {};
      const ths = stats.ths || tHashes((s.summary || {}).mhs_av);
      if (ths) totalTHs += ths;
      if (stats.load_w) totalW += stats.load_w;
    });
    $("#fleetSummary").textContent =
      `${online}/${total} online · ${fmtNumber(totalTHs,1)} TH/s · ${Math.round(totalW)} W`;
  }

  function showEmpty() {
    $('[data-view="empty"]').hidden = false;
    $('[data-view="miner"]').hidden = true;
  }

  function showMinerPane() {
    $('[data-view="empty"]').hidden = true;
    $('[data-view="miner"]').hidden = false;
  }

  // ============== Selection ==============
  async function selectMiner(id) {
    state.selectedMinerId = id;
    showMinerPane();
    renderFleetList();
    try {
      const m = await api(`/api/miners/${id}`);
      paintSelected(m);
      // Load related data
      await Promise.all([loadPools(), loadSchedule()]);
      rebuildActionSelects();
      // Switch to dashboard tab
      activateTab("dashboard");
      // Initial status paint
      const status = m.last_status;
      if (status) paintStatus(status);
      else refreshStatus();
    } catch (e) { toast(e.message, "err"); }
  }

  function paintSelected(m) {
    $("#paneName").textContent = m.name;
    $("#paneAddr").textContent = `${m.host}:${m.port} · ${m.username}`;
    $("#cfgName").value = m.name || "";
    $("#cfgHost").value = m.host || "";
    $("#cfgPort").value = m.port || 4028;
    $("#cfgUser").value = m.username || "root";
    $("#cfgPass").value = "";
    $("#cfgPoll").value = m.poll_seconds || 30;
    $("#cfgEnabled").value = m.enabled === false ? "false" : "true";
    updatePauseButton(m.paused);
    const modeText = m.paused ? "PAUSED" : (m.override ? "OVERRIDE" : "SCHEDULE");
    $("#schedMode").textContent = modeText;
  }

  // ============== Tabs ==============
  function activateTab(name) {
    $$(".tab").forEach(t => t.classList.toggle("is-active", t.dataset.tab === name));
    $$(".tab-panel").forEach(p => {
      const active = p.dataset.tabPanel === name;
      p.classList.toggle("is-active", active);
      p.hidden = !active;
    });
    if (name === "charts") loadCharts();
    if (name === "events") loadEvents();
    if (name === "pools") loadPools();
    if (name === "schedule") loadSchedule();
  }

  document.addEventListener("click", (e) => {
    const tab = e.target.closest(".tab");
    if (tab && tab.dataset.tab) activateTab(tab.dataset.tab);
  });

  // ============== Status / dashboard ==============
  async function refreshStatus() {
    if (!state.selectedMinerId) return;
    try {
      const status = await api(`/api/miners/${state.selectedMinerId}/status`);
      paintStatus(status);
      // Also refresh fleet to update sidebar dots
      const list = await api("/api/miners");
      state.miners = list;
      renderFleetList();
      updateFleetSummary();
    } catch (e) { /* swallow during background polling */ }
  }

  function paintStatus(status) {
    if (!status) return;
    const stats = status.stats || {};
    const summ = status.summary || {};
    $("#kWorkmode").textContent = (stats.workmode_name || "—").toUpperCase();
    $("#kWorkmodeSub").textContent = workmodePower(stats.workmode_name);
    const ths = stats.ths != null ? stats.ths : tHashes(summ.mhs_av);
    $("#kHashrate").textContent = fmtNumber(ths, 1);
    $("#kPower").textContent = stats.load_w != null ? Math.round(stats.load_w) : "—";
    $("#kEfficiency").textContent = (stats.load_w && ths) ? fmtNumber(stats.load_w / ths, 1) : "—";
    $("#kTempMax").textContent = fmtNumber(stats.temp_max, 0);
    $("#kTempChassis").textContent = fmtNumber(stats.temp_chassis, 0);
    $("#kFan").textContent = fmtNumber(stats.fan_pct, 0);
    $("#kUptime").textContent = fmtUptime(summ.elapsed_s);
    $("#kPoolUrl").textContent = stats.pool_url || "—";
    $("#kAccepted").textContent = summ.accepted ?? "—";
    $("#kRejected").textContent = summ.rejected ?? "—";
    $("#kHwErr").textContent = summ.hardware_errors ?? "—";
    $("#kHash5s").textContent = fmtNumber(tHashes(summ.mhs_5s), 2);
    $("#kHash1m").textContent = fmtNumber(tHashes(summ.mhs_1m), 2);

    const sched = status.scheduler || {};
    $("#schedAction").textContent = sched.current_action || "none";
    const src = sched.matched_rule || {};
    let srcText = src.source || "—";
    if (src.rule_name) srcText = `rule: ${src.rule_name}`;
    if (src.source === "override") srcText = `override${src.expires_at ? ` (expires ${formatTime(src.expires_at)})` : " (indefinite)"}`;
    $("#schedSource").textContent = srcText;
    $("#schedApplied").textContent = status.applied_action || "—";
    $("#schedPoll").textContent = formatTime(status.last_poll);

    // Pool-pending warning banner
    const banner = $("#poolPendingBanner");
    if (banner) {
      const pp = status.pool_pending;
      if (pp) {
        const tail = pp.target_url.split("://").pop() || pp.target_url;
        const cur = (pp.active_url || "").split("://").pop() || "unknown";
        banner.innerHTML = `<strong>Pool change pending.</strong> Setpool sent for <code>${tail}</code> but the miner is still on <code>${cur}</code>. The Avalon Q firmware applies pool changes on next reboot — enable <em>reboot after pool switch</em> on the rule, or click reboot manually.`;
        banner.hidden = false;
      } else {
        banner.hidden = true;
      }
    }
  }

  function workmodePower(name) {
    const map = { eco: "~800 W", standard: "~1300 W", super: "~1600 W", standby: "soft-off" };
    return map[name] || "—";
  }

  function updatePauseButton(paused) {
    $("#btnPauseToggle").textContent = paused ? "Resume schedule" : "Pause schedule";
  }

  // ============== Pools ==============
  async function loadPools() {
    if (!state.selectedMinerId) return;
    try {
      state.pools = await api(`/api/miners/${state.selectedMinerId}/pools`);
      renderPools();
      rebuildActionSelects();
    } catch (e) { toast(e.message, "err"); }
  }

  function renderPools() {
    const list = $("#poolList");
    list.innerHTML = "";
    state.pools.forEach(p => list.appendChild(buildPoolEl(p)));
    if (!state.pools.length) {
      list.innerHTML = `<div class="muted small">No pools yet. Add one above to use it in schedule rules.</div>`;
    }
  }

  function buildPoolEl(p) {
    const node = $("#poolTemplate").content.firstElementChild.cloneNode(true);
    node.dataset.poolId = p.id;
    $('[data-pool-id]', node).textContent = p.id;
    $('[data-field="name"]', node).value = p.name || "";
    $('[data-field="url"]', node).value = p.url || "";
    $('[data-field="worker"]', node).value = p.worker || "";
    $('[data-field="worker_password"]', node).value = p.worker_password || "x";
    $('[data-field="notes"]', node).value = p.notes || "";

    node.querySelector('[data-action="save"]').addEventListener("click", async () => {
      try {
        await api(`/api/pools/${p.id}`, {
          method: "PATCH",
          body: {
            name: $('[data-field="name"]', node).value,
            url: $('[data-field="url"]', node).value,
            worker: $('[data-field="worker"]', node).value,
            worker_password: $('[data-field="worker_password"]', node).value,
            notes: $('[data-field="notes"]', node).value,
          },
        });
        toast("Pool saved");
        loadPools();
      } catch (e) { toast(e.message, "err"); }
    });
    node.querySelector('[data-action="delete"]').addEventListener("click", async () => {
      if (!confirm(`Delete pool "${p.name}"? Rules referencing it will break.`)) return;
      try {
        await api(`/api/pools/${p.id}`, { method: "DELETE" });
        loadPools();
      } catch (e) { toast(e.message, "err"); }
    });
    node.querySelector('[data-action="apply"]').addEventListener("click", async () => {
      try {
        const r = await api(`/api/pools/${p.id}/apply`, { method: "POST", body: { reboot: false } });
        if (r.ok) toast(`Applied: ${p.name} (no reboot — takes effect on next miner reboot)`);
        else toast(r.error || "apply failed", "err");
      } catch (e) { toast(e.message, "err"); }
    });
    node.querySelector('[data-action="apply-reboot"]').addEventListener("click", async () => {
      if (!confirm(`Apply "${p.name}" and REBOOT the miner now? In-flight shares will be lost.`)) return;
      try {
        const r = await api(`/api/pools/${p.id}/apply`, { method: "POST", body: { reboot: true } });
        if (r.ok) toast(`Applied: ${p.name} + reboot sent`);
        else toast(r.error || "apply failed", "err");
      } catch (e) { toast(e.message, "err"); }
    });
    return node;
  }

  $("#btnAddPool").addEventListener("click", () => openModal("modalAddPool"));
  $("#btnConfirmAddPool").addEventListener("click", async () => {
    if (!state.selectedMinerId) return;
    try {
      await api(`/api/miners/${state.selectedMinerId}/pools`, {
        method: "POST",
        body: {
          name: $("#poolName").value.trim() || $("#poolUrl").value.trim(),
          url: $("#poolUrl").value.trim(),
          worker: $("#poolWorker").value.trim(),
          worker_password: $("#poolPwd").value.trim() || "x",
          notes: $("#poolNotes").value.trim(),
        },
      });
      ["#poolName","#poolUrl","#poolWorker","#poolNotes"].forEach(s => $(s).value = "");
      $("#poolPwd").value = "x";
      closeModal("modalAddPool");
      loadPools();
      toast("Pool added");
    } catch (e) { toast(e.message, "err"); }
  });

  // ============== Schedule ==============
  async function loadSchedule() {
    if (!state.selectedMinerId) return;
    try {
      const m = await api(`/api/miners/${state.selectedMinerId}`);
      state.schedule = m.schedule || { rules: [], default_action: "none", timezone: "UTC", enabled: true };
      $("#schTimezone").value = state.schedule.timezone || "UTC";
      $("#schEnabled").value = state.schedule.enabled === false ? "false" : "true";
      rebuildActionSelects();
      $("#schDefault").value = state.schedule.default_action || "none";
      const list = $("#ruleList");
      list.innerHTML = "";
      (state.schedule.rules || []).forEach(r => list.appendChild(buildRuleEl(r)));
      if (!$("#tzList").options.length) loadTimezones();
    } catch (e) { toast(e.message, "err"); }
  }

  async function loadTimezones() {
    try {
      const tzs = await api("/api/timezones");
      const dl = $("#tzList");
      tzs.forEach(tz => {
        const opt = document.createElement("option");
        opt.value = tz;
        dl.appendChild(opt);
      });
    } catch { /* non-fatal */ }
  }

  function buildRuleEl(rule = {}) {
    const node = $("#ruleTemplate").content.firstElementChild.cloneNode(true);
    node.dataset.id = rule.id || cryptoRandom();
    $('[data-field="name"]', node).value = rule.name || "";
    $('[data-field="start"]', node).value = rule.start || "16:00";
    $('[data-field="end"]', node).value = rule.end || "19:00";
    $('[data-field="priority"]', node).value = rule.priority ?? 0;
    $('[data-field="enabled"]', node).checked = rule.enabled !== false;

    // Populate the action select for THIS rule
    const actionSel = $('[data-field="action"]', node);
    actionOptions(false).forEach(o => {  // no "none" inside rules; use default for that
      const opt = document.createElement("option");
      opt.value = o.v;
      opt.textContent = o.label;
      actionSel.appendChild(opt);
    });
    actionSel.value = rule.action || "off";

    const days = new Set((rule.days || [0,1,2,3,4]).map(Number));
    $$('[data-field="days"] input', node).forEach(cb => {
      cb.checked = days.has(Number(cb.value));
    });
    if (rule.season_start) {
      const [m, d] = rule.season_start;
      $('[data-field="season_start"]', node).value = `${pad(m)}-${pad(d)}`;
    }
    if (rule.season_end) {
      const [m, d] = rule.season_end;
      $('[data-field="season_end"]', node).value = `${pad(m)}-${pad(d)}`;
    }
    $('[data-field="reboot_after_pool_switch"]', node).value =
      rule.reboot_after_pool_switch ? "true" : "false";
    $('[data-field="chain_workmode"]', node).value = rule.chain_workmode || "";

    // Pool-options visibility: shown only when action is a pool action
    const poolOpts = $('[data-pool-opts]', node);
    function syncPoolOptsVisibility() {
      const isPool = actionSel.value.startsWith("pool:");
      poolOpts.hidden = !isPool;
    }
    actionSel.addEventListener("change", syncPoolOptsVisibility);
    syncPoolOptsVisibility();

    node.querySelector('[data-action="delete"]').addEventListener("click", () => node.remove());
    return node;
  }
  function pad(n) { return String(n).padStart(2, "0"); }
  function cryptoRandom() { return "r_" + Math.random().toString(36).slice(2, 10); }

  function readScheduleFromDOM() {
    const rules = $$("#ruleList .rule").map(node => {
      const days = $$('[data-field="days"] input', node).filter(c => c.checked).map(c => Number(c.value));
      const ss = $('[data-field="season_start"]', node).value.trim();
      const se = $('[data-field="season_end"]', node).value.trim();
      const chain = $('[data-field="chain_workmode"]', node).value || null;
      return {
        id: node.dataset.id || cryptoRandom(),
        name: $('[data-field="name"]', node).value.trim() || "Untitled",
        action: $('[data-field="action"]', node).value,
        days,
        start: $('[data-field="start"]', node).value,
        end: $('[data-field="end"]', node).value,
        enabled: $('[data-field="enabled"]', node).checked,
        priority: Number($('[data-field="priority"]', node).value || 0),
        season_start: parseSeason(ss),
        season_end: parseSeason(se),
        reboot_after_pool_switch: $('[data-field="reboot_after_pool_switch"]', node).value === "true",
        chain_workmode: chain || null,
      };
    });
    return {
      timezone: $("#schTimezone").value.trim() || "UTC",
      enabled: $("#schEnabled").value === "true",
      default_action: $("#schDefault").value,
      rules,
    };
  }
  function parseSeason(s) {
    if (!s) return null;
    const m = s.match(/^(\d{1,2})-(\d{1,2})$/);
    return m ? [Number(m[1]), Number(m[2])] : null;
  }

  $("#btnAddRule").addEventListener("click", () => $("#ruleList").appendChild(buildRuleEl({})));
  $("#btnSaveSchedule").addEventListener("click", async () => {
    if (!state.selectedMinerId) return;
    try {
      const body = readScheduleFromDOM();
      await api(`/api/miners/${state.selectedMinerId}/schedule`, { method: "PUT", body });
      toast(`Saved schedule (${body.rules.length} rule${body.rules.length === 1 ? "" : "s"})`);
      refreshStatus();
    } catch (e) { toast(e.message, "err"); }
  });

  // ============== Pause / override ==============
  $("#btnPauseToggle").addEventListener("click", async () => {
    if (!state.selectedMinerId) return;
    try {
      const m = await api(`/api/miners/${state.selectedMinerId}`);
      const next = !m.paused;
      await api(`/api/miners/${state.selectedMinerId}/pause`, { method: "POST", body: { paused: next } });
      updatePauseButton(next);
      toast(next ? "Paused" : "Resumed");
      refreshFleet();
    } catch (e) { toast(e.message, "err"); }
  });

  async function clearOverride() {
    if (!state.selectedMinerId) return;
    try {
      await api(`/api/miners/${state.selectedMinerId}/override`, { method: "DELETE" });
      toast("Override cleared");
      refreshStatus();
    } catch (e) { toast(e.message, "err"); }
  }
  $("#btnClearOverride").addEventListener("click", clearOverride);
  $("#btnClearOverride2").addEventListener("click", clearOverride);

  $("#btnSetOverride").addEventListener("click", async () => {
    if (!state.selectedMinerId) return;
    const action = $("#ovAction").value;
    const minutes = $("#ovMinutes").value ? Number($("#ovMinutes").value) : null;
    try {
      await api(`/api/miners/${state.selectedMinerId}/override`, {
        method: "POST",
        body: { action, duration_minutes: minutes },
      });
      toast(`Override set: ${action}` + (minutes ? ` for ${minutes}m` : " (indefinite)"));
      refreshStatus();
    } catch (e) { toast(e.message, "err"); }
  });

  // ============== Manual control ==============
  document.addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-cmd]");
    if (!btn || !state.selectedMinerId) return;
    const cmd = btn.dataset.cmd;
    if (cmd === "reboot" && !confirm("Reboot the miner now?")) return;
    try {
      const res = await api(`/api/miners/${state.selectedMinerId}/command/${cmd}`, { method: "POST" });
      if (res.ok) toast(`Sent: ${cmd}`);
      else toast(`${cmd} failed: ${res.error || "see events"}`, "err");
      refreshStatus();
    } catch (err) { toast(err.message, "err"); }
  });

  $("#btnRefresh").addEventListener("click", async () => {
    if (!state.selectedMinerId) return;
    try {
      const status = await api(`/api/miners/${state.selectedMinerId}/refresh`, { method: "POST" });
      paintStatus(status);
      refreshFleet();
      toast("Refreshed");
    } catch (e) { toast(e.message, "err"); }
  });

  // ============== Settings ==============
  $("#btnSaveConn").addEventListener("click", async () => {
    if (!state.selectedMinerId) return;
    try {
      const body = {
        name: $("#cfgName").value.trim(),
        host: $("#cfgHost").value.trim(),
        port: Number($("#cfgPort").value || 4028),
        username: $("#cfgUser").value.trim() || "root",
        poll_seconds: Number($("#cfgPoll").value || 30),
        enabled: $("#cfgEnabled").value === "true",
      };
      const pwd = $("#cfgPass").value;
      if (pwd) body.password = pwd;
      await api(`/api/miners/${state.selectedMinerId}`, { method: "PATCH", body });
      $("#cfgPass").value = "";
      toast("Saved");
      refreshFleet();
    } catch (e) { toast(e.message, "err"); }
  });
  $("#btnTestConn").addEventListener("click", async () => {
    if (!state.selectedMinerId) return;
    try {
      const status = await api(`/api/miners/${state.selectedMinerId}/refresh`, { method: "POST" });
      paintStatus(status);
      if (status.online) toast("Connected and reading status");
      else toast(status.error || "Could not reach miner", "err");
    } catch (e) { toast(e.message, "err"); }
  });
  $("#btnDeleteMiner").addEventListener("click", async () => {
    if (!state.selectedMinerId) return;
    const m = state.miners.find(x => x.id === state.selectedMinerId);
    if (!confirm(`Delete miner "${m && m.name}"? This wipes its schedule, pools, and history.`)) return;
    try {
      await api(`/api/miners/${state.selectedMinerId}`, { method: "DELETE" });
      state.selectedMinerId = null;
      refreshFleet();
      toast("Deleted");
    } catch (e) { toast(e.message, "err"); }
  });

  // ============== Events ==============
  $("#btnReloadEvents").addEventListener("click", loadEvents);
  $("#btnAllEvents").addEventListener("click", async () => {
    try {
      const evts = await api("/api/events?limit=200");
      activateTab("events");
      renderEvents(evts, true);
    } catch (e) { toast(e.message, "err"); }
  });

  async function loadEvents() {
    if (!state.selectedMinerId) return;
    try {
      const evts = await api(`/api/miners/${state.selectedMinerId}/events?limit=200`);
      renderEvents(evts, false);
    } catch (e) { toast(e.message, "err"); }
  }

  function renderEvents(evts, includeMinerCol) {
    const body = $("#eventBody");
    body.innerHTML = "";
    evts.forEach(e => {
      const tr = document.createElement("tr");
      const t = document.createElement("td");
      const k = document.createElement("td");
      const d = document.createElement("td");
      t.textContent = formatTime(e.ts);
      let kindLabel = e.kind;
      if (includeMinerCol && e.miner_id != null) kindLabel = `[m${e.miner_id}] ${e.kind}`;
      k.textContent = kindLabel;
      d.textContent = JSON.stringify(e.data || {});
      tr.appendChild(t); tr.appendChild(k); tr.appendChild(d);
      body.appendChild(tr);
    });
    if (!evts.length) {
      body.innerHTML = `<tr><td colspan="3" class="muted small">No events.</td></tr>`;
    }
  }

  // ============== Charts ==============
  $("#btnReloadCharts").addEventListener("click", loadCharts);

  async function loadCharts() {
    if (!state.selectedMinerId) return;
    try {
      const data = await api(`/api/miners/${state.selectedMinerId}/history`);
      const samples = data.samples || [];
      drawChart("chartHash", samples, "ths", { color: "#ffb547", min: 0 });
      drawChart("chartPower", samples, "load_w", { color: "#6eb6ff", min: 0 });
      drawChart("chartTemp", samples, "temp_max", { color: "#ff8c8c", min: 0 });
      drawChart("chartMode", samples, "workmode", { color: "#6cd57b", min: 0, max: 3, step: true });
    } catch (e) { toast(e.message, "err"); }
  }

  function drawChart(canvasId, samples, field, opts) {
    const canvas = $(`#${canvasId}`);
    if (!canvas) return;
    // Make the canvas crisp on HiDPI
    const ratio = window.devicePixelRatio || 1;
    const cssWidth = canvas.clientWidth;
    const cssHeight = canvas.clientHeight || 200;
    canvas.width = Math.floor(cssWidth * ratio);
    canvas.height = Math.floor(cssHeight * ratio);
    const ctx = canvas.getContext("2d");
    ctx.scale(ratio, ratio);
    ctx.clearRect(0, 0, cssWidth, cssHeight);

    if (!samples.length) {
      ctx.fillStyle = "#6b7480";
      ctx.font = "12px " + getComputedStyle(document.body).getPropertyValue("--font-display");
      ctx.textBaseline = "middle";
      ctx.textAlign = "center";
      ctx.fillText("No data yet — samples appear after the first poll cycle", cssWidth / 2, cssHeight / 2);
      return;
    }

    // Compute extents
    const tsMin = samples[0].ts;
    const tsMax = samples[samples.length - 1].ts;
    const tsRange = Math.max(1, tsMax - tsMin);
    const vals = samples.map(s => s[field]).filter(v => v != null && !isNaN(v));
    let vMin = opts.min != null ? opts.min : Math.min(...vals);
    let vMax = opts.max != null ? opts.max : Math.max(...vals);
    if (vMax === vMin) vMax = vMin + 1;

    const padL = 44, padR = 12, padT = 12, padB = 22;
    const w = cssWidth - padL - padR;
    const h = cssHeight - padT - padB;

    // Grid + axis
    ctx.strokeStyle = "#242a32";
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (let i = 0; i <= 4; i++) {
      const y = padT + (h * i / 4);
      ctx.moveTo(padL, y);
      ctx.lineTo(padL + w, y);
    }
    ctx.stroke();

    // Y labels
    ctx.fillStyle = "#6b7480";
    ctx.font = "10px " + getComputedStyle(document.body).getPropertyValue("--font-display");
    ctx.textBaseline = "middle";
    ctx.textAlign = "right";
    for (let i = 0; i <= 4; i++) {
      const v = vMax - (vMax - vMin) * (i / 4);
      const y = padT + (h * i / 4);
      ctx.fillText(formatAxisVal(v, field), padL - 6, y);
    }

    // X labels (start, mid, end)
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    [0, 0.5, 1].forEach(frac => {
      const ts = tsMin + tsRange * frac;
      const x = padL + w * frac;
      const d = new Date(ts * 1000);
      const lbl = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
      ctx.fillText(lbl, x, padT + h + 4);
    });

    // Data line
    ctx.strokeStyle = opts.color;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    let started = false;
    samples.forEach(s => {
      const v = s[field];
      if (v == null || isNaN(v)) return;
      const x = padL + ((s.ts - tsMin) / tsRange) * w;
      const y = padT + h - ((v - vMin) / (vMax - vMin)) * h;
      if (!started) {
        ctx.moveTo(x, y);
        started = true;
      } else if (opts.step) {
        // step-after: hold previous y, jump x, then drop to new y
        ctx.lineTo(x, ctx._lastY ?? y);
        ctx.lineTo(x, y);
      } else {
        ctx.lineTo(x, y);
      }
      ctx._lastY = y;
    });
    ctx.stroke();

    // Fill below
    ctx.lineTo(padL + w, padT + h);
    ctx.lineTo(padL, padT + h);
    ctx.closePath();
    ctx.fillStyle = opts.color + "22";
    ctx.fill();
  }

  function formatAxisVal(v, field) {
    if (field === "workmode") {
      const labels = ["Eco", "Std", "Sup", "Sby"];
      return labels[Math.round(v)] || String(Math.round(v));
    }
    if (Math.abs(v) >= 100) return String(Math.round(v));
    return v.toFixed(1);
  }

  // ============== Modals ==============
  function openModal(id) {
    const m = $(`#${id}`);
    if (m) m.hidden = false;
  }
  function closeModal(id) {
    const m = $(`#${id}`);
    if (m) m.hidden = true;
  }
  document.addEventListener("click", (e) => {
    const closer = e.target.closest("[data-close]");
    if (closer) {
      const m = closer.closest(".modal");
      if (m) m.hidden = true;
    }
  });

  // ============== Add miner ==============
  $("#btnAddMiner").addEventListener("click", () => openModal("modalAddMiner"));
  $("#btnEmptyAdd").addEventListener("click", () => openModal("modalAddMiner"));
  $("#btnConfirmAdd").addEventListener("click", async () => {
    try {
      const m = await api("/api/miners", {
        method: "POST",
        body: {
          name: $("#addName").value.trim() || $("#addHost").value.trim(),
          host: $("#addHost").value.trim(),
          port: Number($("#addPort").value || 4028),
          username: $("#addUser").value.trim() || "root",
          password: $("#addPass").value || "admin",
          poll_seconds: Number($("#addPoll").value || 30),
        },
      });
      ["#addName","#addHost"].forEach(s => $(s).value = "");
      $("#addPass").value = "admin";
      closeModal("modalAddMiner");
      await refreshFleet();
      selectMiner(m.id);
      toast(`Added miner: ${m.name}`);
    } catch (e) { toast(e.message, "err"); }
  });

  // ============== Scan ==============
  $("#btnScan").addEventListener("click", () => openModal("modalScan"));
  $("#btnEmptyScan").addEventListener("click", () => openModal("modalScan"));
  $("#btnRunScan").addEventListener("click", async () => {
    const cidr = $("#scanCidr").value.trim() || null;
    $("#scanStatus").textContent = "Scanning…";
    $("#scanResults").innerHTML = "";
    try {
      const r = await api("/api/discovery/scan", { method: "POST", body: { cidr } });
      $("#scanStatus").textContent = `Subnet: ${r.subnet_used} · ${r.results.length} found`;
      r.results.forEach(host => {
        const row = document.createElement("div");
        row.className = "scan-row";
        const left = document.createElement("div");
        left.innerHTML = `<div class="scan-row__addr">${host.host}:${host.port}</div>` +
                         `<div class="scan-row__model">${host.miner_model || host.looks_like}</div>`;
        const btn = document.createElement("button");
        btn.className = "btn btn--primary btn--small";
        btn.textContent = "Add";
        btn.addEventListener("click", () => {
          $("#addHost").value = host.host;
          $("#addPort").value = host.port;
          $("#addName").value = host.miner_model || `Avalon @ ${host.host}`;
          closeModal("modalScan");
          openModal("modalAddMiner");
        });
        row.appendChild(left);
        row.appendChild(btn);
        $("#scanResults").appendChild(row);
      });
      if (!r.results.length) {
        $("#scanResults").innerHTML = `<div class="muted small">No CGMiner-API devices found on ${r.subnet_used}.</div>`;
      }
    } catch (e) {
      $("#scanStatus").textContent = "";
      toast(e.message, "err");
    }
  });

  // Pre-populate scan CIDR
  api("/api/discovery/subnet").then(r => {
    if (r.subnet) $("#scanCidr").placeholder = r.subnet + " (auto-detected)";
  }).catch(() => {});

  // ============== Boot ==============
  refreshFleet();
  // Background poll every 15s for the selected miner
  setInterval(() => {
    if (state.selectedMinerId) refreshStatus();
  }, 15000);
})();

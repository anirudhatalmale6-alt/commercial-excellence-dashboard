/**
 * Dashboard controller.
 *
 * One GET /api/dashboard call fills the whole screen. The page then listens on
 * an SSE stream and refetches whenever the backend finishes a sync, so numbers
 * move without the user pressing anything.
 */

import { api, clearToken, getToken, onSync } from './api.js';
import { activityTrend, funnel, money, pct, revenueVsTarget } from './charts.js';

if (!getToken()) location.href = '/login';

const $ = (id) => document.getElementById(id);

const state = { me: null, focus: null, data: null };

/* ---------------------------------------------------------------- helpers */

const STATUS_CLASS = { ok: 'st-ok', degraded: 'st-degraded', down: 'st-down' };
const STATUS_ICON = { ok: '●', degraded: '▲', down: '■' };
const STATUS_WORD = { ok: 'Healthy', degraded: 'Degraded', down: 'Down' };

const KPI_COLOR = {
  good: 'var(--good)',
  warning: 'var(--warning)',
  critical: 'var(--critical)',
  neutral: 'var(--series-1)',
};

function initials(name) {
  return name.split(/\s+/).slice(0, 2).map(p => p[0]).join('').toUpperCase();
}

function ago(iso) {
  if (!iso) return 'never';
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  return `${Math.round(s / 3600)}h ago`;
}

function formatValue(k) {
  if (k.unit === 'currency') return money(k.value);
  if (k.unit === 'percent') return pct(k.value, k.value < 1 && k.value > 0 ? 0 : 0);
  if (k.unit === 'days') return `${Math.round(k.value)}d`;
  return k.value >= 90 ? '99+×' : `${k.value.toFixed(1)}×`;
}

/* ----------------------------------------------------------- integrations */

function renderIntegrations(list) {
  const host = $('integrations');
  host.innerHTML = '';
  let worst = 'ok';
  for (const c of list) {
    if (c.status === 'down') worst = 'down';
    else if (c.status === 'degraded' && worst !== 'down') worst = 'degraded';

    const node = document.createElement('div');
    node.className = 'integration';
    const cls = STATUS_CLASS[c.status];
    const fresh = c.freshness_minutes >= 60
      ? `${Math.round(c.freshness_minutes / 60)}h behind`
      : `${c.freshness_minutes} min behind`;

    node.innerHTML = `
      <span class="dot ${cls}" style="background:currentColor"></span>
      <span class="body">
        <span class="line1">${c.display_name}
          <span class="vendor">${c.vendor}</span>
          <span class="badge ${cls}">${STATUS_ICON[c.status]} ${STATUS_WORD[c.status]}</span>
        </span>
        <span class="line2">${c.records.toLocaleString()} records · ${c.latency_ms} ms · ${fresh} · ${c.message}</span>
      </span>`;
    host.appendChild(node);
  }

  $('integration-note').textContent = worst === 'ok'
    ? 'All three sources answered on the last sync. Every number below is traceable to one of them.'
    : worst === 'degraded'
      ? 'One or more sources is degraded — figures that depend on it are flagged on the tile.'
      : 'A source is down. Figures that depend on it are stale and flagged on the tile.';
}

/* -------------------------------------------------------------- KPI tiles */

function renderKpis(kpis, integrations) {
  const bySource = Object.fromEntries(integrations.map(c => [c.source, c.status]));
  const host = $('kpis');
  host.innerHTML = '';

  for (const k of kpis) {
    const node = document.createElement('div');
    node.className = 'kpi';

    let bar = '';
    if (k.target != null && k.target > 0) {
      const ratio = Math.max(0, Math.min(k.value / k.target, 1));
      bar = `<div class="kpi-bar"><i style="width:${(ratio * 100).toFixed(1)}%;background:${KPI_COLOR[k.status]}"></i></div>`;
    }

    let delta = '';
    if (k.deltaPct != null) {
      const up = k.deltaPct >= 0;
      delta = `<span class="delta ${up ? 'up' : 'down'}">${up ? '▲' : '▼'} ${pct(Math.abs(k.deltaPct))}</span>`;
    }

    const chips = k.sources.map(s => {
      const st = bySource[s];
      const cls = st === 'down' ? 'bad' : st === 'degraded' ? 'warn' : '';
      return `<span class="${cls}" title="${st === 'ok' ? 'source healthy' : 'source ' + st}">${s}</span>`;
    }).join('');

    node.innerHTML = `
      <div class="label">${k.label} ${delta}</div>
      <div class="value" style="${k.status !== 'neutral' ? `color:${KPI_COLOR[k.status]}` : ''}">${formatValue(k)}</div>
      ${bar}
      <div class="hint">${k.hint || ''}</div>
      <div class="src">${chips}</div>`;
    host.appendChild(node);
  }
}

/* ---------------------------------------------------------- action list */

function renderActions(actions) {
  const host = $('actions');
  host.innerHTML = '';
  $('actions-sub').textContent = actions.length
    ? `${actions.length} ranked by value at stake`
    : 'nothing needs attention';

  if (!actions.length) {
    host.innerHTML = '<p class="empty">Nothing needs your attention right now.</p>';
    return;
  }

  for (const a of actions) {
    const node = document.createElement('div');
    node.className = 'action';
    const owner = a.owner_name ? `<span class="pill">${a.owner_name}</span>` : '';
    const due = a.due_in_days == null ? ''
      : a.due_in_days < 0 ? `<span class="pill u-critical">${Math.abs(a.due_in_days)}d overdue</span>`
        : `<span class="pill">${a.due_in_days}d left</span>`;

    node.innerHTML = `
      <span class="rail rail-${a.urgency}"></span>
      <span>
        <p class="title">${a.title}</p>
        <p class="detail">${a.detail}</p>
        <span class="meta">
          <span class="pill u-${a.urgency}">${STATUS_ICON.ok} ${a.urgency}</span>
          <span class="pill">${a.category}</span>
          ${owner}${due}
          <span class="pill src-pill">${a.sources.join(' + ')}</span>
        </span>
      </span>
      <span class="impact">${money(a.impact)}<small>at stake</small></span>`;
    host.appendChild(node);
  }
}

/* --------------------------------------------------------------- tables */

function renderTable(data) {
  const isTeam = data.scope.isTeamView;
  const host = $('table-body');
  host.innerHTML = '';

  if (isTeam && data.leaderboard.rows.length) {
    $('table-title').textContent = 'Team against target';
    $('table-sub').textContent = 'Month to date · click a row to drill in';
    // Bars are scaled to the month target; the tick marks where the rep should
    // be TODAY, so being short of full target mid-month does not read as red.
    const pace = data.leaderboard.paceFraction || 1;
    const rows = data.leaderboard.rows.map(r => {
      const ratio = Math.max(0, Math.min(r.attainment, 1)) * 100;
      const color = r.vsPace >= 0.95 ? 'var(--good)' : r.vsPace >= 0.7 ? 'var(--warning)' : 'var(--critical)';
      return `<tr class="clickable" data-user="${r.user_id}" title="${pct(r.vsPace)} of the pace line">
        <td>${r.name}</td>
        <td>${r.region}</td>
        <td class="num">${money(r.revenue)}</td>
        <td class="num">${money(r.target)}</td>
        <td class="bar-cell"><span class="mini-bar"><i style="width:${ratio.toFixed(1)}%;background:${color}"></i><span class="ref" style="left:${(pace * 100).toFixed(1)}%"></span></span></td>
        <td class="num">${pct(r.attainment)}</td>
      </tr>`;
    }).join('');
    host.innerHTML = `<table class="data leaderboard">
      <thead><tr>
        <th>Rep</th><th>Region</th>
        <th class="num">Revenue MTD</th><th class="num">Target</th>
        <th class="bar-cell">vs pace <span style="font-weight:400;text-transform:none">(mark = today)</span></th>
        <th class="num">%</th>
      </tr></thead>
      <tbody>${rows}</tbody></table>`;
    host.querySelectorAll('tr[data-user]').forEach(tr => {
      tr.addEventListener('click', () => {
        $('focus').value = tr.dataset.user;
        state.focus = tr.dataset.user;
        load();
      });
    });
    return;
  }

  $('table-title').textContent = 'Top accounts';
  $('table-sub').textContent = 'Billed revenue, last 90 days · ERP';
  if (!data.topAccounts.rows.length) {
    host.innerHTML = '<p class="empty">No billed revenue in the last 90 days.</p>';
    return;
  }
  const max = Math.max(...data.topAccounts.rows.map(r => r.revenue), 1);
  const rows = data.topAccounts.rows.map(r => `
    <tr>
      <td>${r.name}</td>
      <td class="bar-cell" style="width:45%"><span class="mini-bar"><i style="width:${((r.revenue / max) * 100).toFixed(1)}%;background:var(--series-1)"></i></span></td>
      <td class="num">${money(r.revenue)}</td>
    </tr>`).join('');
  host.innerHTML = `<table class="data">
    <thead><tr><th>Account</th><th></th><th class="num">Revenue 90d</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

/* ------------------------------------------------------------- rendering */

function render(data) {
  state.data = data;
  $('scope-label').textContent = `${data.scope.label}${data.scope.isTeamView ? ` · ${data.scope.userCount} people` : ''}`;
  $('kpi-note').textContent = `Month to date · updated ${ago(data.lastSyncAt)}`;

  renderIntegrations(data.integrations);
  renderKpis(data.kpis, data.integrations);
  renderActions(data.actions);
  renderTable(data);

  revenueVsTarget($('chart-revenue'), data.revenueTrend.points);
  funnel($('chart-funnel'), data.funnel.stages);
  activityTrend($('chart-activity'), data.activity.weeks);
}

/* ---------------------------------------------------------------- wiring */

async function load() {
  const q = state.focus ? `?focus=${encodeURIComponent(state.focus)}` : '';
  render(await api(`/api/dashboard${q}`));
}

async function boot() {
  state.me = await api('/api/me');
  $('user-name').textContent = state.me.name;
  $('user-role').textContent = state.me.role;
  $('avatar').textContent = initials(state.me.name);

  if (state.me.reports.length) {
    const sel = $('focus');
    sel.innerHTML = `<option value="">Whole team</option>` +
      state.me.reports.map(r => `<option value="${r.id}">${r.name}</option>`).join('');
    sel.addEventListener('change', () => { state.focus = sel.value || null; load(); });
    $('scope-picker').hidden = false;
  }

  await load();

  const syncState = $('sync-state');
  syncState.classList.add('live');
  $('sync-text').textContent = 'live';
  onSync(() => {
    $('sync-text').textContent = 'syncing…';
    load().then(() => { $('sync-text').textContent = 'live'; });
  });

  // Keeps the "updated Xs ago" line honest between syncs.
  setInterval(() => {
    if (state.data) $('kpi-note').textContent = `Month to date · updated ${ago(state.data.lastSyncAt)}`;
  }, 15000);
}

$('refresh').addEventListener('click', async (e) => {
  e.target.disabled = true;
  try { await api('/api/sync', { method: 'POST' }); await load(); }
  finally { e.target.disabled = false; }
});

$('logout').addEventListener('click', () => { clearToken(); location.href = '/login'; });

$('theme').addEventListener('click', () => {
  const cur = document.documentElement.getAttribute('data-theme');
  const next = cur === 'dark' ? 'light' : cur === 'light' ? 'dark' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('ce.theme', next);
});

const savedTheme = localStorage.getItem('ce.theme');
if (savedTheme) document.documentElement.setAttribute('data-theme', savedTheme);

boot().catch(err => {
  document.querySelector('.wrap').innerHTML = `<div class="error" style="margin-top:24px">${err.message}</div>`;
});

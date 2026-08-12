/**
 * Hand-rolled SVG charts.
 *
 * No charting library on purpose: three chart forms, full control over marks
 * and accessibility, and nothing to keep up to date. Every chart renders into a
 * container, re-renders on resize, and ships a hover tooltip.
 *
 * Mark conventions used throughout: 4px rounded data-ends anchored to the
 * baseline, 2px lines, a 2px surface gap between adjacent bars, recessive
 * gridlines, selective direct labels (never a number on every mark).
 */

const NS = 'http://www.w3.org/2000/svg';

/* ------------------------------------------------------------------ utils */

export function money(v, opts = {}) {
  const a = Math.abs(v);
  const sign = v < 0 ? '-' : '';
  if (opts.full) return `${sign}€${a.toLocaleString('en-GB', { maximumFractionDigits: 0 })}`;
  if (a >= 1_000_000) return `${sign}€${(a / 1_000_000).toFixed(a >= 10_000_000 ? 0 : 1)}M`;
  if (a >= 1_000) return `${sign}€${Math.round(a / 1_000)}k`;
  return `${sign}€${Math.round(a)}`;
}

export function pct(v, digits = 0) {
  return `${(v * 100).toFixed(digits)}%`;
}

function el(name, attrs = {}, text) {
  const node = document.createElementNS(NS, name);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (text !== undefined) node.textContent = text;
  return node;
}

function niceMax(v) {
  if (v <= 0) return 1;
  const exp = Math.floor(Math.log10(v));
  const base = Math.pow(10, exp);
  const n = v / base;
  const step = n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10;
  return step * base;
}

/** Column with rounded top corners, square on the baseline. */
function columnPath(x, y, w, h, r = 4) {
  const rr = Math.min(r, w / 2, Math.max(h, 0));
  if (h <= 0.5) return `M${x} ${y + h} h${w}`;
  return `M${x} ${y + h} V${y + rr} a${rr} ${rr} 0 0 1 ${rr} ${-rr} h${w - 2 * rr} a${rr} ${rr} 0 0 1 ${rr} ${rr} V${y + h} Z`;
}

/** Horizontal bar, rounded on the far end only. */
function rowPath(x, y, w, h, r = 4) {
  const rr = Math.min(r, h / 2, Math.max(w, 0));
  if (w <= 0.5) return `M${x} ${y} v${h}`;
  return `M${x} ${y} h${w - rr} a${rr} ${rr} 0 0 1 ${rr} ${rr} v${h - 2 * rr} a${rr} ${rr} 0 0 1 ${-rr} ${rr} H${x} Z`;
}

/* --------------------------------------------------------------- tooltip */

let tipNode;
function tip() {
  if (!tipNode) {
    tipNode = document.createElement('div');
    tipNode.className = 'tooltip';
    document.body.appendChild(tipNode);
  }
  return tipNode;
}

function showTip(evt, html) {
  const t = tip();
  t.innerHTML = html;
  t.classList.add('on');
  const pad = 14;
  const r = t.getBoundingClientRect();
  let x = evt.clientX + pad;
  let y = evt.clientY + pad;
  if (x + r.width > window.innerWidth - 8) x = evt.clientX - r.width - pad;
  if (y + r.height > window.innerHeight - 8) y = evt.clientY - r.height - pad;
  t.style.left = `${Math.max(8, x)}px`;
  t.style.top = `${Math.max(8, y)}px`;
}

function hideTip() {
  if (tipNode) tipNode.classList.remove('on');
}

/** Re-render a chart whenever its container resizes. */
function responsive(container, draw) {
  const run = () => {
    const w = container.clientWidth;
    if (w > 0) draw(w);
  };
  run();
  if (container._ro) container._ro.disconnect();
  container._ro = new ResizeObserver(run);
  container._ro.observe(container);
}

/* ------------------------------------------------- revenue vs target chart */

export function revenueVsTarget(container, points) {
  responsive(container, (width) => {
    container.innerHTML = '';
    const height = 216;
    const m = { top: 12, right: 8, bottom: 26, left: 46 };
    const iw = Math.max(width - m.left - m.right, 60);
    const ih = height - m.top - m.bottom;

    const max = niceMax(Math.max(...points.flatMap(p => [p.actual, p.target]), 1) * 1.1);
    const y = v => m.top + ih - (v / max) * ih;
    const slot = iw / points.length;
    const barW = Math.min(slot - 14, 46);

    const svg = el('svg', { class: 'chart', viewBox: `0 0 ${width} ${height}`, height });

    for (let i = 0; i <= 4; i++) {
      const v = (max / 4) * i;
      svg.appendChild(el('line', { class: i === 0 ? 'axis-line' : 'grid-line', x1: m.left, x2: m.left + iw, y1: y(v), y2: y(v) }));
      svg.appendChild(el('text', { class: 'tick', x: m.left - 8, y: y(v) + 3.5, 'text-anchor': 'end' }, money(v)));
    }

    points.forEach((p, i) => {
      const cx = m.left + slot * i + slot / 2;
      const x = cx - barW / 2;
      const h = Math.max((p.actual / max) * ih, 0);

      svg.appendChild(el('path', {
        class: 'bar',
        d: columnPath(x, y(p.actual), barW, h),
        fill: 'var(--series-1)',
        'fill-opacity': p.partial ? 0.55 : 1,
      }));

      // Target as a 2px rule across the column: one axis, two measures of the
      // same unit — never a second y-scale.
      const ty = y(p.target);
      svg.appendChild(el('line', {
        x1: x - 4, x2: x + barW + 4, y1: ty, y2: ty,
        stroke: 'var(--series-2)', 'stroke-width': 2, 'stroke-linecap': 'round',
      }));

      svg.appendChild(el('text', { class: 'tick', x: cx, y: height - 8, 'text-anchor': 'middle' },
        p.partial ? `${p.label}*` : p.label));

      const hit = el('rect', { class: 'hit', x: m.left + slot * i, y: m.top, width: slot, height: ih });
      const gap = p.actual - p.target;
      hit.addEventListener('mousemove', (e) => showTip(e, `
        <b>${p.label} ${p.period.slice(0, 4)}${p.partial ? ' (month to date)' : ''}</b>
        <div class="row"><span>Actual</span><span>${money(p.actual, { full: true })}</span></div>
        <div class="row"><span>Target</span><span>${money(p.target, { full: true })}</span></div>
        <div class="row"><span>${gap >= 0 ? 'Over' : 'Under'}</span><span>${money(Math.abs(gap), { full: true })}</span></div>`));
      hit.addEventListener('mouseleave', hideTip);
      svg.appendChild(hit);
    });

    container.appendChild(svg);
  });
}

/* ------------------------------------------------------------ funnel chart */

const FUNNEL_RAMP = ['var(--seq-250)', 'var(--seq-350)', 'var(--seq-450)', 'var(--seq-550)', 'var(--seq-650)'];

export function funnel(container, stages) {
  responsive(container, (width) => {
    container.innerHTML = '';
    const rowH = 34;
    const height = stages.length * rowH + 10;
    const labelW = 92;
    const valueW = 74;
    const iw = Math.max(width - labelW - valueW, 60);
    const max = Math.max(...stages.map(s => s.amount), 1);

    const svg = el('svg', { class: 'chart', viewBox: `0 0 ${width} ${height}`, height });

    stages.forEach((s, i) => {
      const y = i * rowH + 6;
      const h = rowH - 14; // leaves a 2px+ surface gap between adjacent bars
      const w = (s.amount / max) * iw;

      svg.appendChild(el('text', { class: 'tick', x: 0, y: y + h / 2 + 4 }, s.label));
      svg.appendChild(el('path', { class: 'bar', d: rowPath(labelW, y, w, h), fill: FUNNEL_RAMP[i] || FUNNEL_RAMP[4] }));
      svg.appendChild(el('text', { class: 'val-label', x: width, y: y + h / 2 + 4, 'text-anchor': 'end' }, money(s.amount)));

      const hit = el('rect', { class: 'hit', x: 0, y, width, height: rowH - 6 });
      hit.addEventListener('mousemove', (e) => showTip(e, `
        <b>${s.label}</b>
        <div class="row"><span>Value</span><span>${money(s.amount, { full: true })}</span></div>
        <div class="row"><span>Deals</span><span>${s.count}</span></div>
        <div class="row"><span>Avg size</span><span>${s.count ? money(s.amount / s.count, { full: true }) : '—'}</span></div>`));
      hit.addEventListener('mouseleave', hideTip);
      svg.appendChild(hit);
    });

    container.appendChild(svg);
  });
}

/* --------------------------------------------------------- activity trend */

export function activityTrend(container, weeks) {
  responsive(container, (width) => {
    container.innerHTML = '';
    const height = 132;
    const m = { top: 12, right: 10, bottom: 22, left: 34 };
    const iw = Math.max(width - m.left - m.right, 60);
    const ih = height - m.top - m.bottom;
    if (!weeks.length) { container.innerHTML = '<p class="empty">No activity recorded</p>'; return; }

    const vals = weeks.map(w => w.meetings + w.demos);
    const max = niceMax(Math.max(...vals, 1) * 1.15);
    const x = i => m.left + (weeks.length === 1 ? iw / 2 : (iw * i) / (weeks.length - 1));
    const y = v => m.top + ih - (v / max) * ih;

    const svg = el('svg', { class: 'chart', viewBox: `0 0 ${width} ${height}`, height });

    for (let i = 0; i <= 2; i++) {
      const v = (max / 2) * i;
      svg.appendChild(el('line', { class: i === 0 ? 'axis-line' : 'grid-line', x1: m.left, x2: m.left + iw, y1: y(v), y2: y(v) }));
      svg.appendChild(el('text', { class: 'tick', x: m.left - 7, y: y(v) + 3.5, 'text-anchor': 'end' }, String(Math.round(v))));
    }

    const d = vals.map((v, i) => `${i === 0 ? 'M' : 'L'}${x(i)} ${y(v)}`).join(' ');
    svg.appendChild(el('path', { d, fill: 'none', stroke: 'var(--series-3)', 'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' }));

    vals.forEach((v, i) => {
      svg.appendChild(el('circle', { cx: x(i), cy: y(v), r: 4, fill: 'var(--series-3)', stroke: 'var(--surface)', 'stroke-width': 2 }));
      const hit = el('circle', { class: 'hit', cx: x(i), cy: y(v), r: 12 });
      const w = weeks[i];
      hit.addEventListener('mousemove', (e) => showTip(e, `
        <b>Week of ${w.week}</b>
        <div class="row"><span>Meetings</span><span>${w.meetings}</span></div>
        <div class="row"><span>Demos</span><span>${w.demos}</span></div>
        <div class="row"><span>Calls</span><span>${w.calls}</span></div>`));
      hit.addEventListener('mouseleave', hideTip);
      svg.appendChild(hit);
    });

    // Only the endpoints get direct labels — never a number on every point.
    const last = weeks[weeks.length - 1];
    svg.appendChild(el('text', { class: 'tick', x: m.left, y: height - 6 }, weeks[0].week.slice(5)));
    svg.appendChild(el('text', { class: 'tick', x: m.left + iw, y: height - 6, 'text-anchor': 'end' }, last.week.slice(5)));

    container.appendChild(svg);
  });
}

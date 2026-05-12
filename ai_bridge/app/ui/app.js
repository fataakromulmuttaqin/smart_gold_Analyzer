/* SmartGold AI Bridge — dashboard client.
   Vanilla ES module, no build step. Polls /api/stats and /api/signals
   every 30 seconds, lets the user click a row to open a detail modal. */

(() => {
  'use strict';

  // ── Element refs ───────────────────────────────────────────────────
  const el = (id) => document.getElementById(id);
  const healthDot  = el('health-dot');
  const healthMeta = el('health-meta');
  const statTotal  = el('stat-total');
  const statExec   = el('stat-execute');
  const statRed    = el('stat-reduce');
  const statSkip   = el('stat-skip');
  const statConf   = el('stat-conf');
  const statNot    = el('stat-notified');
  const statWindow = el('stat-window');
  const fHours     = el('f-hours');
  const fAction    = el('f-action');
  const fSymbol    = el('f-symbol');
  const btnRefresh = el('btn-refresh');
  const tbody      = el('signals-body');
  const modal      = el('modal');
  const modalBody  = el('modal-body');
  const modalTitle = el('modal-title');
  const modalClose = el('modal-close');

  const POLL_MS = 30_000;
  let pollTimer = null;
  let lastSymbolInput = '';

  // ── Small helpers ──────────────────────────────────────────────────
  const fmt = {
    price(v) {
      if (v === null || v === undefined || v === '') return '–';
      const n = Number(v);
      if (!Number.isFinite(n)) return String(v);
      return n.toLocaleString(undefined, {
        minimumFractionDigits: 2,
        maximumFractionDigits: 4,
      });
    },
    conf(v) {
      if (v === null || v === undefined) return '–';
      return Number(v).toFixed(2);
    },
    pct(v) {
      if (v === null || v === undefined) return '–';
      return Number(v).toFixed(2) + ' %';
    },
    when(iso) {
      if (!iso) return '–';
      // SQLite's CURRENT_TIMESTAMP returns 'YYYY-MM-DD HH:MM:SS' (UTC, no Z)
      const s = iso.includes('T') ? iso : iso.replace(' ', 'T') + 'Z';
      const d = new Date(s);
      if (Number.isNaN(d.getTime())) return iso;
      return d.toISOString().replace('T', ' ').replace('Z', '').slice(0, 19);
    },
    windowLabel(h) {
      const n = Number(h);
      if (!Number.isFinite(n)) return '—';
      if (n < 24)   return `last ${n} h`;
      if (n === 24) return 'last 24 h';
      if (n < 24 * 7) return `last ${Math.round(n / 24)} d`;
      return `last ${Math.round(n / 24)} d`;
    },
  };

  async function fetchJSON(path) {
    const resp = await fetch(path, { headers: { Accept: 'application/json' } });
    if (!resp.ok) throw new Error(`HTTP ${resp.status} on ${path}`);
    return resp.json();
  }

  function escapeHtml(v) {
    if (v === null || v === undefined) return '';
    return String(v)
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;')
      .replaceAll("'", '&#39;');
  }

  // ── Health ─────────────────────────────────────────────────────────
  async function refreshHealth() {
    try {
      const data = await fetchJSON('/health');
      healthDot.classList.remove('bad');
      healthDot.classList.add('ok');
      healthDot.title = 'healthy';
      const flags = [
        `model=${data.model}`,
        data.llm_mock_mode ? 'MOCK' : 'LLM-live',
        data.telegram_configured ? 'telegram✓' : 'telegram✗',
        data.newsapi_configured ? 'news✓' : 'news✗',
        `min_conf=${data.min_confidence}`,
      ].join(' · ');
      healthMeta.textContent = flags;
    } catch (e) {
      healthDot.classList.remove('ok');
      healthDot.classList.add('bad');
      healthDot.title = 'unreachable';
      healthMeta.textContent = `health error: ${e.message}`;
    }
  }

  // ── Stats ──────────────────────────────────────────────────────────
  async function refreshStats() {
    const hours = fHours.value;
    try {
      const s = await fetchJSON(`/api/stats?hours=${encodeURIComponent(hours)}`);
      statTotal.textContent = s.total ?? 0;
      statExec.textContent  = s.by_action?.execute ?? 0;
      statRed.textContent   = s.by_action?.reduce  ?? 0;
      statSkip.textContent  = s.by_action?.skip    ?? 0;
      statConf.textContent  = s.avg_confidence === null || s.avg_confidence === undefined
        ? '–'
        : Number(s.avg_confidence).toFixed(2);
      statNot.textContent  = s.notified ?? 0;
      statWindow.textContent = fmt.windowLabel(hours);
    } catch (e) {
      console.error('stats error', e);
    }
  }

  // ── Signals table ──────────────────────────────────────────────────
  function rowHtml(r) {
    const action = (r.decision_action || '').toLowerCase();
    const notified = r.notified ? '✓' : '–';
    return `
      <tr data-id="${r.id}">
        <td class="mono muted">${r.id}</td>
        <td class="mono">${escapeHtml(fmt.when(r.received_at))}</td>
        <td>${escapeHtml(r.symbol)}</td>
        <td>${escapeHtml(r.timeframe)}</td>
        <td>${escapeHtml(r.signal)}</td>
        <td class="mono">${fmt.price(r.price)}</td>
        <td><span class="badge ${action}">${escapeHtml(action || '–')}</span></td>
        <td class="mono">${fmt.conf(r.decision_conf)}</td>
        <td class="mono ${r.notified ? 'ok' : 'muted'}">${notified}</td>
      </tr>`;
  }

  async function refreshSignals() {
    const params = new URLSearchParams({ limit: '100', offset: '0' });
    if (fAction.value) params.set('action', fAction.value);
    const sym = fSymbol.value.trim();
    if (sym) params.set('symbol', sym);
    try {
      const data = await fetchJSON(`/api/signals?${params.toString()}`);
      if (!data.items || data.items.length === 0) {
        tbody.innerHTML = `<tr><td colspan="9" class="empty">no signals in this window</td></tr>`;
        return;
      }
      tbody.innerHTML = data.items.map(rowHtml).join('');
    } catch (e) {
      tbody.innerHTML = `<tr><td colspan="9" class="empty bad">load failed: ${escapeHtml(e.message)}</td></tr>`;
    }
  }

  // ── Detail modal ───────────────────────────────────────────────────
  function openModal(html, title) {
    modalBody.innerHTML = html;
    modalTitle.textContent = title || 'Signal detail';
    modal.hidden = false;
    document.body.style.overflow = 'hidden';
  }
  function closeModal() {
    modal.hidden = true;
    modalBody.innerHTML = '';
    document.body.style.overflow = '';
  }
  modalClose.addEventListener('click', closeModal);
  modal.addEventListener('click', (ev) => { if (ev.target === modal) closeModal(); });
  document.addEventListener('keydown', (ev) => {
    if (!modal.hidden && ev.key === 'Escape') closeModal();
  });

  async function openDetail(id) {
    openModal('<div class="muted">loading…</div>', `Signal #${id}`);
    try {
      const d = await fetchJSON(`/api/signals/${id}`);
      const dec = d.decision || {};
      const ctx = d.context || {};
      const action = (d.decision_action || '').toLowerCase();
      const html = `
        <dl class="kv">
          <dt>Received</dt><dd>${escapeHtml(fmt.when(d.received_at))} UTC</dd>
          <dt>Symbol / TF</dt><dd>${escapeHtml(d.symbol)} · ${escapeHtml(d.timeframe)}</dd>
          <dt>Signal</dt><dd>${escapeHtml(d.signal)}</dd>
          <dt>Price</dt><dd>${fmt.price(d.price)}</dd>
          <dt>Decision</dt><dd><span class="badge ${action}">${escapeHtml(action)}</span>
            <span class="muted"> confidence ${fmt.conf(d.decision_conf)}</span></dd>
          <dt>Notified</dt><dd>${d.notified ? 'yes' : 'no'}</dd>
          <dt>Suggested R:R</dt><dd>${dec.suggested_rr ? '1:' + dec.suggested_rr : '—'}</dd>
          <dt>Suggested stop</dt><dd>${dec.suggested_stop_atr_mult ? dec.suggested_stop_atr_mult + '×ATR' : '—'}</dd>
        </dl>

        <div class="section-title">LLM reasoning</div>
        <div class="reasoning">${escapeHtml(dec.reasoning || '—')}</div>

        ${dec.risk_notes ? `
          <div class="section-title">Risk notes</div>
          <div class="reasoning">${escapeHtml(dec.risk_notes)}</div>
        ` : ''}

        <div class="section-title">Macro context</div>
        <pre class="json">${escapeHtml(JSON.stringify(ctx, null, 2))}</pre>

        <div class="section-title">Raw alert payload</div>
        <pre class="json">${escapeHtml(JSON.stringify(d.alert || {}, null, 2))}</pre>
      `;
      openModal(html, `Signal #${id} — ${escapeHtml(d.symbol || '')}`);
    } catch (e) {
      openModal(`<div class="bad">failed: ${escapeHtml(e.message)}</div>`, `Signal #${id}`);
    }
  }

  tbody.addEventListener('click', (ev) => {
    const tr = ev.target.closest('tr[data-id]');
    if (!tr) return;
    const id = tr.getAttribute('data-id');
    if (id) openDetail(id);
  });

  // ── Polling orchestration ──────────────────────────────────────────
  function refreshAll() {
    refreshHealth();
    refreshStats();
    refreshSignals();
  }
  function schedulePoll() {
    clearInterval(pollTimer);
    pollTimer = setInterval(refreshAll, POLL_MS);
  }
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
      refreshAll();
      schedulePoll();
    } else {
      clearInterval(pollTimer);
    }
  });

  // ── Filter wiring ──────────────────────────────────────────────────
  fHours.addEventListener('change', () => { refreshStats(); });
  fAction.addEventListener('change', () => { refreshSignals(); });
  fSymbol.addEventListener('input', () => {
    // debounce symbol filter so we don't hit the API on every keystroke
    const current = fSymbol.value;
    lastSymbolInput = current;
    setTimeout(() => { if (lastSymbolInput === current) refreshSignals(); }, 400);
  });
  btnRefresh.addEventListener('click', refreshAll);

  // ── Go ─────────────────────────────────────────────────────────────
  refreshAll();
  schedulePoll();
})();

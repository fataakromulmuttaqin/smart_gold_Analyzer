/* SmartGold AI Bridge — dashboard client.
   Vanilla ES module, no build step. Polls /api/stats, /api/signals and
   /api/winrate every 30 seconds, lets the user click a row to open a
   detail modal and record trade outcomes. */

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
  const statOrders = el('stat-orders');
  const statWindow = el('stat-window');
  const fHours     = el('f-hours');
  const fAction    = el('f-action');
  const fSymbol    = el('f-symbol');
  const fWrHours   = el('f-wr-hours');
  const wrBody     = el('wr-body');
  const wrOverall  = el('wr-overall');
  const btnRefresh = el('btn-refresh');
  const tbody      = el('signals-body');
  const modal      = el('modal');
  const modalBody  = el('modal-body');
  const modalTitle = el('modal-title');
  const modalClose = el('modal-close');

  const POLL_MS = 30_000;
  const TABLE_COLSPAN = 11;
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
      return (Number(v) * 100).toFixed(1) + ' %';
    },
    pnl(v) {
      if (v === null || v === undefined || v === '') return '–';
      const n = Number(v);
      if (!Number.isFinite(n)) return String(v);
      const sign = n >= 0 ? '+' : '';
      return sign + n.toFixed(2);
    },
    when(iso) {
      if (!iso) return '–';
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
      return `last ${Math.round(n / 24)} d`;
    },
  };

  async function fetchJSON(path, init) {
    const resp = await fetch(path, {
      headers: { Accept: 'application/json', ...(init?.headers || {}) },
      ...init,
    });
    if (!resp.ok) {
      const body = await resp.text().catch(() => '');
      throw new Error(`HTTP ${resp.status} on ${path}${body ? ' — ' + body.slice(0, 120) : ''}`);
    }
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
        `exec=${data.executor || '?'}`,
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
      statNot.textContent    = s.notified ?? 0;
      statOrders.textContent = s.orders_placed ?? 0;
      statWindow.textContent = fmt.windowLabel(hours);
    } catch (e) {
      console.error('stats error', e);
    }
  }

  // ── Win-rate chart ─────────────────────────────────────────────────
  function renderWinrate(wr) {
    const overall = wr.overall || {};
    const rows = wr.by_signal || [];
    if (!rows.length) {
      wrOverall.textContent = 'awaiting trade outcomes';
      wrBody.innerHTML = `
        <div class="empty muted">
          No closed trades in this window.<br/>
          Record outcomes via the signal detail modal or
          <code>PATCH /api/signals/&lt;id&gt;/outcome</code>.
        </div>`;
      return;
    }

    // Overall summary line
    const wrPct = overall.win_rate == null ? '–' : fmt.pct(overall.win_rate);
    const totalPnlClass = (overall.total_pnl || 0) > 0 ? 'ok' : (overall.total_pnl || 0) < 0 ? 'bad' : 'muted';
    wrOverall.innerHTML =
      `overall: <strong>${wrPct}</strong> ` +
      `(${overall.wins}W / ${overall.losses}L / ${overall.breakevens}BE) ` +
      `· <span class="${totalPnlClass}">P&amp;L ${fmt.pnl(overall.total_pnl)}</span>`;

    // Find largest bar for scaling
    const maxClosed = Math.max(...rows.map((r) => r.closed || 0), 1);

    const rowsHtml = rows.map((r) => {
      const closed = r.closed || 0;
      const wins = r.wins || 0;
      const losses = r.losses || 0;
      const bes = r.breakevens || 0;
      // Segment widths in % of the bar
      const winPct  = closed ? (wins   / closed) * 100 : 0;
      const lossPct = closed ? (losses / closed) * 100 : 0;
      const bePct   = closed ? (bes    / closed) * 100 : 0;
      // The row bar width = closed / maxClosed
      const barFill = (closed / maxClosed) * 100;
      const wrLabel = r.win_rate == null ? '–' : fmt.pct(r.win_rate);
      const pnlCls = (r.total_pnl || 0) > 0 ? 'ok' : (r.total_pnl || 0) < 0 ? 'bad' : 'muted';
      return `
        <div class="wr-row">
          <div class="wr-name mono" title="${escapeHtml(r.signal)}">${escapeHtml(r.signal)}</div>
          <div class="wr-bar" style="width:${barFill.toFixed(1)}%">
            <span class="wr-seg win"  style="width:${winPct.toFixed(1)}%"></span>
            <span class="wr-seg be"   style="width:${bePct.toFixed(1)}%"></span>
            <span class="wr-seg loss" style="width:${lossPct.toFixed(1)}%"></span>
          </div>
          <div class="wr-stats mono">
            <span title="win rate (wins / decisive)">${wrLabel}</span>
            <span class="muted">${wins}/${losses}/${bes}</span>
            <span class="${pnlCls}">${fmt.pnl(r.total_pnl)}</span>
          </div>
        </div>`;
    }).join('');

    wrBody.innerHTML = `
      <div class="wr-legend muted">
        <span><i class="sw-dot win"></i> win</span>
        <span><i class="sw-dot be"></i> breakeven</span>
        <span><i class="sw-dot loss"></i> loss</span>
        <span class="hint">bar length = trade count (relative)</span>
      </div>
      ${rowsHtml}`;
  }

  async function refreshWinrate() {
    try {
      const wr = await fetchJSON(`/api/winrate?hours=${encodeURIComponent(fWrHours.value)}`);
      renderWinrate(wr);
    } catch (e) {
      wrBody.innerHTML = `<div class="empty bad">winrate error: ${escapeHtml(e.message)}</div>`;
    }
  }

  // ── Signals table ──────────────────────────────────────────────────
  function outcomeBadge(outcome) {
    if (!outcome) return '<span class="muted">–</span>';
    const cls = outcome === 'win' ? 'win' : outcome === 'loss' ? 'loss' : 'be';
    return `<span class="badge ${cls}">${escapeHtml(outcome)}</span>`;
  }

  function rowHtml(r) {
    const action = (r.decision_action || '').toLowerCase();
    const notified = r.notified ? '✓' : '–';
    const order = r.execution_placed ? '✓' : '–';
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
        <td class="mono ${r.execution_placed ? 'ok' : 'muted'}">${order}</td>
        <td>${outcomeBadge(r.outcome)}</td>
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
        tbody.innerHTML = `<tr><td colspan="${TABLE_COLSPAN}" class="empty">no signals in this window</td></tr>`;
        return;
      }
      tbody.innerHTML = data.items.map(rowHtml).join('');
    } catch (e) {
      tbody.innerHTML = `<tr><td colspan="${TABLE_COLSPAN}" class="empty bad">load failed: ${escapeHtml(e.message)}</td></tr>`;
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

  async function submitOutcome(id, outcome, pnlInput) {
    const pnlRaw = (pnlInput?.value || '').trim();
    const body = { outcome };
    if (pnlRaw !== '') {
      const pnl = Number(pnlRaw);
      if (Number.isFinite(pnl)) body.pnl = pnl;
    }
    try {
      await fetchJSON(`/api/signals/${id}/outcome`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      closeModal();
      refreshAll();
    } catch (e) {
      alert('Failed to record outcome: ' + e.message);
    }
  }

  async function openDetail(id) {
    openModal('<div class="muted">loading…</div>', `Signal #${id}`);
    try {
      const d = await fetchJSON(`/api/signals/${id}`);
      const dec = d.decision || {};
      const ctx = d.context || {};
      const exec = d.execution || null;
      const action = (d.decision_action || '').toLowerCase();

      // Existing outcome (if any)
      const hasOutcome = Boolean(d.outcome);
      const outcomeHtml = hasOutcome
        ? `${outcomeBadge(d.outcome)} ${d.pnl != null ? `<span class="mono">(PnL ${fmt.pnl(d.pnl)})</span>` : ''} <span class="muted">closed ${escapeHtml(fmt.when(d.closed_at))}</span>`
        : '<span class="muted">not yet recorded</span>';

      const executionHtml = exec ? `
        <div class="section-title">Broker execution</div>
        <pre class="json">${escapeHtml(JSON.stringify(exec, null, 2))}</pre>
      ` : '';

      const outcomeFormHtml = `
        <div class="section-title">Record outcome</div>
        <div class="outcome-form">
          <input id="pnl-input-${id}" type="number" step="0.01"
                 placeholder="PnL (optional)" class="outcome-pnl" />
          <button data-outcome="win"       class="btn-win"  type="button">Mark WIN</button>
          <button data-outcome="loss"      class="btn-loss" type="button">Mark LOSS</button>
          <button data-outcome="breakeven" class="btn-be"   type="button">BE</button>
          ${hasOutcome ? '<span class="muted hint">re-submitting overwrites the current outcome</span>' : ''}
        </div>`;

      const html = `
        <dl class="kv">
          <dt>Received</dt><dd>${escapeHtml(fmt.when(d.received_at))} UTC</dd>
          <dt>Symbol / TF</dt><dd>${escapeHtml(d.symbol)} · ${escapeHtml(d.timeframe)}</dd>
          <dt>Signal</dt><dd>${escapeHtml(d.signal)}</dd>
          <dt>Price</dt><dd>${fmt.price(d.price)}</dd>
          <dt>Decision</dt><dd><span class="badge ${action}">${escapeHtml(action)}</span>
            <span class="muted"> confidence ${fmt.conf(d.decision_conf)}</span></dd>
          <dt>Notified</dt><dd>${d.notified ? 'yes' : 'no'}</dd>
          <dt>Order placed</dt><dd>${d.execution_placed ? 'yes' : 'no'}</dd>
          <dt>Outcome</dt><dd>${outcomeHtml}</dd>
          <dt>Suggested R:R</dt><dd>${dec.suggested_rr ? '1:' + dec.suggested_rr : '—'}</dd>
          <dt>Suggested stop</dt><dd>${dec.suggested_stop_atr_mult ? dec.suggested_stop_atr_mult + '×ATR' : '—'}</dd>
        </dl>

        <div class="section-title">LLM reasoning</div>
        <div class="reasoning">${escapeHtml(dec.reasoning || '—')}</div>

        ${dec.risk_notes ? `
          <div class="section-title">Risk notes</div>
          <div class="reasoning">${escapeHtml(dec.risk_notes)}</div>
        ` : ''}

        ${outcomeFormHtml}
        ${executionHtml}

        <div class="section-title">Macro context</div>
        <pre class="json">${escapeHtml(JSON.stringify(ctx, null, 2))}</pre>

        <div class="section-title">Raw alert payload</div>
        <pre class="json">${escapeHtml(JSON.stringify(d.alert || {}, null, 2))}</pre>
      `;
      openModal(html, `Signal #${id} — ${escapeHtml(d.symbol || '')}`);

      // Wire up the outcome form buttons
      const pnlInput = el(`pnl-input-${id}`);
      modalBody.querySelectorAll('.outcome-form [data-outcome]').forEach((btn) => {
        btn.addEventListener('click', () => {
          const outcome = btn.getAttribute('data-outcome');
          submitOutcome(id, outcome, pnlInput);
        });
      });
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
    refreshWinrate();
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
  fWrHours.addEventListener('change', () => { refreshWinrate(); });
  fSymbol.addEventListener('input', () => {
    const current = fSymbol.value;
    lastSymbolInput = current;
    setTimeout(() => { if (lastSymbolInput === current) refreshSignals(); }, 400);
  });
  btnRefresh.addEventListener('click', refreshAll);

  // ── Go ─────────────────────────────────────────────────────────────
  refreshAll();
  schedulePoll();
})();

/**
 * screens/detail/incident.js — #/history/incidents/:id, one incident as a story (SPEC §12.5).
 *
 * Hero tile with the hypothesis and duration → the sequence as a vertical timeline (same rail as
 * the ladders, one node per alert, a final "Resolved" node) beside the affected services and the
 * incident's alerts (instant Ack with an Undo snackbar). Reloads on incident/alert events for this
 * incident and ticks the "ongoing for" duration while it is open.
 */
import api from '../../api.js';
import { store } from '../../store.js';
import {
  ackedPill, button, el, emptyState, fmt, grid, humanize, keepUnits, replaceKeepingFocus, section, severityStatus, snackbar, statusPill,
  tile,
} from '../../ui.js';

const REFRESH_MS = 60000;
const LABEL_EMOJI = {
  Server: '💻', PostgreSQL: '🐘', Solr: '☀️', 'DSpace Backend': '☕', 'DSpace Frontend': '🖥️', nginx: '🌐',
  'SSL certificate': '🔒', Ports: '🔌', Logs: '📜', Repository: '📚', Assetstore: '🗄️', 'DSpace Search': '🔎', 'Repository site': '🏛️',
};

function worstSeverity(alerts) {
  return alerts.some((a) => a.severity === 'critical') ? 'critical' : 'warning';
}

function durationText(inc, now = Date.now() / 1000) {
  const start = Number(inc.started_ts) || now;
  if (inc.status === 'resolved' && inc.ended_ts) return `lasted ${fmt.duration(Number(inc.ended_ts) - start)}`;
  return `ongoing for ${fmt.duration(Math.max(0, now - start))}`;
}

function componentLabel(id) {
  const comp = store.components().find((c) => c.id === id);
  if (comp) return comp.label;
  if (id === 'server') return 'Server';
  return id ? String(id).replace(/^\w/, (c) => c.toUpperCase()) : '';
}

function timelineItem({ ts, text, status, sub }) {
  const li = el('li', { class: `rung rung--${status}` });
  const node = el('span', { class: 'rung-node', 'aria-hidden': 'true' });
  const main = el('span', 'rung-main', el('span', { class: 'rung-label', text }), sub ? el('span', { class: 'rung-msg', text: sub }) : null);
  const time = el('time', { class: 'tl-time', datetime: new Date(Number(ts) * 1000).toISOString(), text: fmt.clock(ts), title: fmt.dateTime(ts) });
  li.append(node, main, el('span', 'rung-end', time));
  return li;
}

function alertTile(alert, onAck) {
  const status = severityStatus(alert.severity);
  const resolved = alert.status === 'resolved';
  const message = String(alert.message || '').split('\n').join(' · ');
  const when = resolved && alert.resolved_ts
    ? `${fmt.clock(alert.opened_ts)} – ${fmt.clock(alert.resolved_ts)}`
    : alert.opened_ts ? `since ${fmt.clock(alert.opened_ts)}` : '';
  const parts = [componentLabel(alert.component), message, when].filter(Boolean);
  let action;
  if (resolved) action = statusPill('ok', 'Resolved');
  else if (alert.acked_ts) action = ackedPill();
  else {
    action = button({ label: 'Ack', variant: 'ghost', size: 'sm', ariaLabel: `Acknowledge ${alert.title}`, onClick: (e) => { e.preventDefault(); onAck(alert); } });
    action.dataset.action = 'ack';
  }
  const node = tile({
    emoji: status === 'crit' ? '🔴' : '🟡',
    title: keepUnits(alert.title),
    subtitle: el('span', { class: `tile-sub${resolved ? '' : ` tile-sub--${status}`}`, text: humanize(parts.join(' · ')) }),
    actions: action,
    chevron: false,
  });
  node.dataset.key = `alert-${alert.id}`;
  return node;
}

/**
 * Render one incident.
 * @param {HTMLElement} root
 * @param {object} params `{id}`
 * @param {object} ctx
 * @returns {() => void} destroy
 */
export function render(root, params, ctx) {
  const id = Number(params.id);
  const view = grid();
  root.replaceChildren(view);

  const heroWrap = el('div', 'span-12');
  const sequenceSection = section({ title: 'Sequence', subtitle: 'What happened, in order.', layout: 'stack', span: 6, id: 'incident-sequence' });
  const rightCol = el('div', 'span-6', el('div', 'col'));
  const affectedSection = section({ title: 'Affected services', layout: 'stack', id: 'incident-affected' });
  const alertsSection = section({ title: 'Alerts', layout: 'stack', id: 'incident-alerts' });
  const alertsCount = el('span', { class: 'muted', text: '' });
  alertsSection.actionsEl.appendChild(alertsCount);
  rightCol.firstChild.append(affectedSection, alertsSection);
  view.append(heroWrap, sequenceSection, rightCol);

  // status and duration live in the hero tile; the header does not repeat them
  ctx.setControls();

  let incident = null;
  let loading = null;
  const localAcks = new Map();

  function heroSubtitle(inc) {
    const started = fmt.dateTime(inc.started_ts);
    return `Started ${started} · ${durationText(inc)} · ${fmt.plural((inc.alerts || inc.alert_ids || []).length, 'alert')}`;
  }

  function paintHero(inc) {
    const status = inc.status === 'resolved' ? 'ok' : severityStatus(worstSeverity(inc.alerts || []));
    const hero = tile({
      emoji: '🧭', title: inc.hypothesis || inc.title || `Incident #${inc.id}`,
      subtitle: heroSubtitle(inc),
      pill: statusPill(status, inc.status === 'resolved' ? 'Resolved' : 'Ongoing'),
      className: 'incident-hero',
    });
    hero.setAttribute('aria-live', 'off');
    heroWrap.replaceChildren(hero);
  }

  function paintTick() {
    if (!incident || incident.status === 'resolved') return;
    const sub = heroWrap.querySelector('.tile-sub');
    if (sub) sub.textContent = heroSubtitle(incident);
  }

  function paintSequence(inc) {
    const entries = (inc.sequence || []).slice().sort((a, b) => Number(a.ts) - Number(b.ts));
    if (!entries.length) {
      sequenceSection.body.replaceChildren(emptyState('🧭', 'No sequence recorded — the alerts below tell the story.'));
      return;
    }
    const list = el('ol', { class: 'timeline' });
    for (const entry of entries) list.appendChild(timelineItem({ ts: entry.ts, text: keepUnits(entry.text), status: severityStatus(entry.severity) }));
    if (inc.status === 'resolved' && inc.ended_ts) {
      list.appendChild(timelineItem({ ts: inc.ended_ts, text: 'Resolved', status: 'ok', sub: `all alerts cleared · ${durationText(inc)}` }));
    } else {
      list.appendChild(timelineItem({ ts: Date.now() / 1000, text: 'Still open', status: 'unknown', sub: 'resolves when every alert clears' }));
    }
    const card = el('div', { class: 'ladder', role: 'group', 'aria-label': 'Incident sequence' }, list);
    sequenceSection.body.replaceChildren(card);
  }

  function paintAffected(inc) {
    const affected = Array.isArray(inc.affected) ? inc.affected : [];
    const chips = affected.map((label) => el('span', { class: 'pill', role: 'listitem' },
      el('span', { class: 'pill-mark', 'aria-hidden': 'true', text: LABEL_EMOJI[label] || '🧩' }), ' ', label));
    const node = tile({
      emoji: '🕸️', title: affected.length ? fmt.plural(affected.length, 'service') : 'Nothing downstream',
      subtitle: affected.length ? 'depend on what failed and were affected' : 'No other service depends on what failed.',
      className: 'tile--block',
    });
    if (chips.length) node.appendChild(el('div', { class: 'chip-list', role: 'list', 'aria-label': 'Affected services' }, ...chips));
    affectedSection.body.replaceChildren(node);
  }

  async function ack(alert) {
    localAcks.set(alert.id, Date.now() / 1000);
    paintAlerts(incident);
    try {
      await api.ackAlert(alert.id);
      ctx.refreshStatus(); // an acknowledged alert no longer colours the mood: the accent and sidebar follow now
      snackbar.undo('Alert acknowledged', async () => {
        localAcks.delete(alert.id);
        try {
          await api.unackAlert(alert.id);
        } catch (err) {
          snackbar(`Could not undo: ${err.message}`);
        }
        ctx.refreshStatus();
        await load();
        // focus returns to the restored Ack button
        return alertsSection.body.querySelector(`[data-key="alert-${alert.id}"] [data-action="ack"]`);
      });
    } catch (err) {
      localAcks.delete(alert.id);
      paintAlerts(incident);
      snackbar(`Could not acknowledge: ${err.message}`);
    }
  }

  function paintAlerts(inc) {
    const alerts = (inc.alerts || []).map((a) => (localAcks.has(a.id) && !a.acked_ts ? { ...a, acked_ts: localAcks.get(a.id) } : a));
    for (const a of inc.alerts || []) if (a.acked_ts && localAcks.has(a.id)) localAcks.delete(a.id);
    const open = alerts.filter((a) => a.status !== 'resolved').length;
    alertsCount.textContent = alerts.length ? (open ? `${open} of ${alerts.length} still open` : 'all resolved') : '';
    if (!alerts.length) {
      replaceKeepingFocus(alertsSection.body, [emptyState('✅', 'No alerts are attached to this incident.')]);
      return;
    }
    replaceKeepingFocus(alertsSection.body, alerts.map((a) => alertTile(a, ack)));
  }

  function paintAll(inc) {
    incident = inc;
    paintHero(inc);
    paintSequence(inc);
    paintAffected(inc);
    paintAlerts(inc);
  }

  function showGone(message) {
    incident = null;
    view.replaceChildren(el('div', 'span-12', emptyState('🧭', message)));
    ctx.setControls();
  }

  async function load() {
    if (loading) return loading;
    loading = (async () => {
      try {
        const data = await api.incident(id, { signal: ctx.signal });
        if (ctx.signal.aborted) return;
        paintAll(data);
      } catch (err) {
        if (err && err.name === 'AbortError') return;
        if (err && err.status === 404) showGone('This incident is gone — it may have been cleared with the history.');
        else if (!incident) showGone(`Could not load this incident — ${err.message}`);
      } finally {
        loading = null;
      }
    })();
    return loading;
  }

  const offIncident = store.on('sse:incident', (data) => {
    const inc = data && (data.incident || data);
    if (inc && Number(inc.id) === id) load();
  });
  const offAlert = store.on('sse:alert', (data) => {
    const alert = data && data.alert;
    if (alert && (Number(alert.incident_id) === id || (incident && (incident.alert_ids || []).includes(alert.id)))) load();
  });
  const tick = setInterval(paintTick, 1000);
  const timer = setInterval(() => { if (incident && incident.status !== 'resolved') load(); }, REFRESH_MS);

  if (!Number.isInteger(id) || id <= 0) showGone('That is not an incident number — pick one from History.');
  else load();

  return () => {
    offIncident();
    offAlert();
    clearInterval(tick);
    clearInterval(timer);
  };
}

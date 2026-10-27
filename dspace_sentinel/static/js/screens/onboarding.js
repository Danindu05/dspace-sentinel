/**
 * screens/onboarding.js — first-run gate (SPEC §12.5).
 *
 * Step 1 "What runs on this server?" — four persona tiles, single select, "You can change everything later.", Continue.
 * Step 2 "We looked around" — POST /api/actions/discover; findings as tiles with pre-set toggles; Start monitoring
 * (POST /api/actions/setup {persona, config_patch}) or Skip for now (persona only). `ctx.finish(meta)` shows the shell.
 * nginx and SSL start switched on only when discovery found a web server and HTTPS; with no such evidence their rows
 * stay, switched off, reading "not found on this machine" (Skip for now leaves them off too). When discovery failed
 * the chosen option's preset stands. Security looks at this server itself (its ports, firewall, SSH, updates), so it is
 * pre-selected for every option, with "this server" as its evidence.
 */
import api from '../api.js';
import { button, disclosure, el, emptyState, snackbar, toggle } from '../ui.js';

const PERSONAS = [
  { id: 'full', emoji: '🧩', title: 'Full DSpace stack', sub: 'PostgreSQL, Solr, the backend and the Angular frontend all on this machine.' },
  { id: 'backend', emoji: '☕', title: 'Backend only', sub: 'Tomcat, PostgreSQL and Solr here; the frontend runs somewhere else.' },
  { id: 'frontend', emoji: '🖥️', title: 'Frontend only', sub: 'The Angular UI and nginx here; the REST API is on another server.' },
  { id: 'server', emoji: '💻', title: 'Just the server', sub: 'Vitals, ports, security, nginx, SSL and logs. Add DSpace modules whenever you like.' },
];

const ALL = ['ports', 'security', 'postgres', 'solr', 'backend', 'frontend', 'nginx', 'ssl', 'logs', 'repository', 'assetstore', 'incidents'];
const PRESETS = {
  full: ALL,
  backend: ALL.filter((m) => m !== 'frontend'),
  frontend: ['nginx', 'ssl', 'frontend', 'ports', 'security', 'logs', 'backend'],
  server: ['ports', 'security', 'nginx', 'ssl', 'logs'],
};

/**
 * Modules a preset switches on only with discovery's evidence (a web server, HTTPS). Without it the check would
 * start critical on a server opened directly on :4000; the DSpace parts stay on, where "not found" may mean "down".
 */
const NEEDS_EVIDENCE = ['nginx', 'ssl'];
const NOT_FOUND = 'not found on this machine';

/** The port rules each persona sets (mirrors config.PERSONAS): [port, label, expect]. */
const PORT_RULE = { 22: [22, 'SSH', 'open'], 80: [80, 'HTTP', 'open'], 443: [443, 'HTTPS', 'open'], 4000: [4000, 'Angular', 'open'], 8080: [8080, 'Backend', 'open'], 8983: [8983, 'Solr', 'local_only'], 5432: [5432, 'PostgreSQL', 'local_only'] };
const PERSONA_PORTS = {
  full: [PORT_RULE[22], PORT_RULE[80], PORT_RULE[443], PORT_RULE[4000], PORT_RULE[8080], PORT_RULE[8983], PORT_RULE[5432]],
  backend: [PORT_RULE[22], PORT_RULE[80], PORT_RULE[443], PORT_RULE[8080], PORT_RULE[8983], PORT_RULE[5432]],
  frontend: [PORT_RULE[22], PORT_RULE[80], PORT_RULE[443], PORT_RULE[4000]],
  server: [PORT_RULE[22]],
};

/**
 * The port rules "Start monitoring" will apply: discovery's suggestion (the persona's rules narrowed to
 * what listens) when it has one, else the persona's own list.
 */
function expectedPorts(persona, discovery) {
  const suggested = discovery && discovery.suggested_config && discovery.suggested_config.ports;
  if (Array.isArray(suggested) && suggested.length) return suggested;
  return (PERSONA_PORTS[persona] || []).map(([port, label, expect]) => ({ port, label, expect }));
}

/** "Expects 22 SSH, 80 HTTP open · 8983 Solr local only" */
function portsText(rules) {
  const name = (r) => `${r.port}${r.label ? ` ${r.label}` : ''}`;
  const open = rules.filter((r) => r.expect !== 'local_only').map(name);
  const local = rules.filter((r) => r.expect === 'local_only').map(name);
  const parts = [open.length ? `${open.join(', ')} open` : null, local.length ? `${local.join(', ')} local only` : null].filter(Boolean);
  return parts.length ? `Expects ${parts.join(' · ')}.` : 'No port rules — nothing to expect yet.';
}

const MODULES = [
  ['postgres', '🐘', 'PostgreSQL', 'Checks the database really answers queries, not just that the service runs.'],
  ['solr', '☀️', 'Solr', 'Pings every core and times a real search.'],
  ['backend', '☕', 'DSpace Backend', 'REST API, actuator health and its database and Solr dependencies.'],
  ['frontend', '🖥️', 'DSpace Frontend', 'Loads the Angular homepage and times it.'],
  ['nginx', '🌐', 'nginx', 'HTTP and HTTPS through the web server.'],
  ['ssl', '🔒', 'SSL certificate', 'Days until your certificate expires.'],
  ['logs', '📜', 'Logs', 'Counts new errors in DSpace, Tomcat, Solr and nginx logs.'],
  ['repository', '📚', 'Repository stats', 'Items, bitstreams, communities, collections and storage growth.'],
  ['assetstore', '🗄️', 'Assetstore', 'Free space where your files live.'],
  ['ports', '🔌', 'Ports', 'Which ports are open, and which should stay local only.'],
  ['security', '🛡️', 'Security', 'Open ports, firewall, SSH, updates and software support: read-only.'],
  ['incidents', '🧭', 'Incidents', 'Groups related alerts into one story.'],
];

/** The evidence of a module that looks at the server itself: it is always there. */
const THIS_SERVER = 'this server';

/** One line of evidence per module from the discovery result (or null when nothing was found). */
function evidence(id, discovery) {
  const cfg = (discovery && discovery.suggested_config) || {};
  const services = (discovery && discovery.services) || {};
  const ports = (discovery && discovery.ports) || [];
  const dspace = cfg.dspace || {};
  const active = (unit) => (services[unit] === 'active' ? `${unit} active` : null);
  switch (id) {
    case 'postgres': {
      const pg = cfg.postgres || {};
      if (pg.host) return `${pg.host}:${pg.port || 5432}/${pg.database || 'dspace'}`;
      return active(pg.service || 'postgresql') || (ports.some((p) => p.port === 5432) ? 'port 5432 listening' : null);
    }
    case 'solr':
      return (cfg.solr && cfg.solr.url) || active('solr') || (ports.some((p) => p.port === 8983) ? 'port 8983 listening' : null);
    case 'backend':
      return dspace.server_url || (dspace.backend_service ? active(dspace.backend_service) : null) || (ports.some((p) => p.port === 8080) ? 'port 8080 listening' : null);
    case 'frontend':
      return dspace.ui_url || (dspace.frontend_service ? active(dspace.frontend_service) : null) || (ports.some((p) => p.port === 4000) ? 'port 4000 listening' : null);
    case 'nginx':
      return (cfg.nginx && cfg.nginx.service ? active(cfg.nginx.service) : null) || (ports.some((p) => p.port === 80 || p.port === 443) ? 'ports 80/443 listening' : null);
    case 'ssl':
      return dspace.public_url ? `certificate of ${dspace.public_url.replace(/^https?:\/\//, '')}` : ports.some((p) => p.port === 443) ? 'port 443 listening' : null;
    case 'logs':
      return dspace.log_dir || null;
    case 'assetstore':
      return dspace.assetstore_dir || null;
    case 'repository':
      return cfg.postgres && cfg.postgres.host ? 'counts via PostgreSQL' : dspace.server_url ? 'counts via the REST API' : null;
    case 'ports':
      return ports.length ? `${ports.length} listening ${ports.length === 1 ? 'port' : 'ports'} found` : null;
    case 'security':
      return THIS_SERVER;
    default:
      return null;
  }
}

/**
 * Render the onboarding gate.
 * @param {HTMLElement} root
 * @param {object} params
 * @param {object} ctx — uses ctx.finish(meta), ctx.signal
 */
export function render(root, params, ctx) {
  const state = { persona: null, discovery: null, toggles: {}, busy: false };
  const wrap = el('div', 'onboarding');
  root.replaceChildren(wrap);

  function header(step, title) {
    const h1 = el('h1', { text: title, tabIndex: -1 });
    wrap.append(
      el('p', 'onboarding-brand', el('span', { class: 'brand-mark', 'aria-hidden': 'true', text: '🛰️' }), 'DSpace Sentinel'),
      el('p', { class: 'onboarding-step', text: `Step ${step} of 2` }),
      h1,
    );
    return h1;
  }

  function stepOne() {
    wrap.replaceChildren();
    const h1 = header(1, 'What runs on this server?');
    const group = el('div', { class: 'persona-grid', role: 'radiogroup', 'aria-label': 'What runs on this server?' });
    const next = button({ label: 'Continue', variant: 'filled', disabled: !state.persona, onClick: () => stepTwo() });
    const tiles = PERSONAS.map((p) => {
      const node = el('button', { class: 'tile tile--action persona', type: 'button', role: 'radio', 'aria-checked': state.persona === p.id ? 'true' : 'false', dataset: { persona: p.id } },
        el('span', { class: 'tile-icon', 'aria-hidden': 'true', text: p.emoji }),
        el('span', 'tile-body', el('span', { class: 'tile-title', text: p.title }), el('span', { class: 'tile-sub', text: p.sub })),
        el('span', 'tile-end', el('span', { class: 'persona-check', 'aria-hidden': 'true', text: '✓' })),
      );
      node.addEventListener('click', () => choose(p.id));
      node.addEventListener('keydown', (e) => {
        if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft' && e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
        e.preventDefault();
        const idx = PERSONAS.findIndex((x) => x.id === p.id);
        const dir = e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 1 : PERSONAS.length - 1;
        const target = PERSONAS[(idx + dir) % PERSONAS.length];
        choose(target.id);
        tiles[PERSONAS.indexOf(target)].focus();
      });
      return node;
    });
    function choose(id) {
      state.persona = id;
      tiles.forEach((t, i) => {
        const on = PERSONAS[i].id === id;
        t.setAttribute('aria-checked', on ? 'true' : 'false');
        t.tabIndex = on ? 0 : -1;
      });
      next.disabled = false;
    }
    tiles.forEach((t, i) => { t.tabIndex = state.persona ? (PERSONAS[i].id === state.persona ? 0 : -1) : i === 0 ? 0 : -1; });
    group.append(...tiles);
    wrap.append(
      group,
      el('p', { class: 'onboarding-caption', text: 'You can change everything later.' }),
      el('div', 'onboarding-actions', el('span', 'spacer'), next),
    );
    h1.focus({ preventScroll: true });
  }

  async function stepTwo() {
    wrap.replaceChildren();
    const h1 = header(2, 'We looked around');
    const status = el('p', { class: 'onboarding-caption', text: 'Reading the DSpace configuration, listening ports and services…', role: 'status' });
    wrap.append(status);
    h1.focus({ preventScroll: true });
    if (!state.discovery) {
      try {
        state.discovery = await api.discover({ signal: ctx.signal });
      } catch (err) {
        if (err && err.name === 'AbortError') return;
        state.discovery = { error: err.message, findings: [], suggested_config: {}, ports: [], services: {} };
      }
    }
    if (ctx.signal.aborted) return;
    status.remove();
    const d = state.discovery;
    const suggested = (d.suggested_config && d.suggested_config.modules) || null;
    // discovery that ran knows whether there is a web server and HTTPS; when it failed, the preset stands
    const absent = new Set(!d.error && suggested ? NEEDS_EVIDENCE.filter((id) => !suggested[id]) : []);
    const preset = new Set(PRESETS[state.persona] || []);
    const found = MODULES.filter(([id]) => evidence(id, d) !== null || preset.has(id));
    // otherwise the persona is the floor; discovery evidence can switch extra modules on
    state.toggles = {};
    for (const [id] of found) state.toggles[id] = !absent.has(id) && (preset.has(id) || Boolean(suggested && suggested[id]));

    // what was found here, not the server itself (Security's evidence is always "this server")
    const foundHere = found.some(([id]) => id !== 'security' && evidence(id, d) !== null);
    const intro = d.error
      ? `Could not look around (${d.error}). These modules come from your choice; adjust them and start.`
      : foundHere
        ? 'Turn on what Sentinel should watch. Everything found on this machine is pre-selected.'
        : 'Nothing DSpace-specific was found on this machine. These modules come from your choice; you can connect DSpace later in More › DSpace connection.';
    wrap.append(el('p', { class: 'onboarding-caption', text: intro }));

    const list = el('div', 'findings');
    for (const [id, emoji, label, explainer] of found) {
      const detail = absent.has(id) ? NOT_FOUND : evidence(id, d);
      // the Ports row lists the exact rules that will be applied, so they can be reviewed before starting
      const description = id === 'ports' ? portsText(expectedPorts(state.persona, d)) : detail ? `${detail} — ${explainer}` : explainer;
      const row = toggle({
        emoji, label, checked: state.toggles[id], id: `mod-${id}`,
        description,
        onChange: (checked) => { state.toggles[id] = checked; },
      });
      list.appendChild(row);
    }
    if (!found.length) list.appendChild(emptyState('🔍', 'Nothing to switch on yet — Sentinel will watch the server itself.'));
    wrap.appendChild(list);

    if (d.findings && d.findings.length) {
      const ul = el('ul', 'finding-list');
      for (const line of d.findings) ul.appendChild(el('li', { text: line }));
      wrap.appendChild(disclosure({ summary: `What we found (${d.findings.length})`, children: ul }));
    }

    const backBtn = button({ label: '‹ Back', variant: 'ghost', onClick: () => stepOne() });
    const skip = button({ label: 'Skip for now', variant: 'text', onClick: () => finish(false) });
    const start = button({ label: 'Start monitoring', variant: 'filled', busyLabel: 'Starting…', onClick: () => finish(true) });
    wrap.appendChild(el('div', 'onboarding-actions', backBtn, el('span', 'spacer'), skip, start));

    async function finish(applyPatch) {
      if (state.busy) return;
      state.busy = true;
      start.setBusy(true);
      skip.disabled = true;
      backBtn.disabled = true;
      const patch = {};
      if (applyPatch) {
        const cfg = d.suggested_config || {};
        for (const [key, value] of Object.entries(cfg)) if (key !== 'modules') patch[key] = value;
        patch.modules = { ...state.toggles };
      } else if (absent.size) {
        // the chosen option as it is, without what discovery found missing
        patch.modules = Object.fromEntries([...absent].map((id) => [id, false]));
      }
      try {
        const meta = await api.setup({ persona: state.persona || '', config_patch: patch });
        await ctx.finish(meta);
      } catch (err) {
        state.busy = false;
        start.setBusy(false);
        skip.disabled = false;
        backBtn.disabled = false;
        snackbar(`Could not save the setup: ${err.message}`);
      }
    }
  }

  stepOne();
  return () => {};
}

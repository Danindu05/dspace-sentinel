/**
 * screens/more/connection.js — #/more/connection: DSpace connection (SPEC §12.5).
 *
 * URLs, directories and service names for DSpace, PostgreSQL, Solr, nginx and SSL in one
 * single-column form (module groups fold when the module is off). "Detect automatically" runs
 * POST /api/actions/discover, shows what was found and the settings it suggests, and "Apply"
 * writes them through PATCH /api/config. Secrets are masked; sending the mask keeps them.
 */
import api from '../../api.js';
import { store } from '../../store.js';
import { button, disclosure, el, emptyState, select, table, tile, toggle } from '../../ui.js';
import { MASK, errorState, formEl, group, loadConfig, maskAsOne, numberField, readNumber, saveButton, submitPatch, textField } from './form.js';

export const title = 'DSpace connection';

const SSL_MODES = ['disable', 'allow', 'prefer', 'require', 'verify-ca', 'verify-full'].map((v) => ({ value: v, label: v }));
const SECRET_RE = /password|token|secret/i;

/**
 * One list entry as text: a port rule reads "8983 Solr (local only)", a log file "DSpace · /dspace/log/dspace.log
 * (daily files)"; any other object its values joined.
 */
export function listItemText(item) {
  if (item === null || item === undefined) return '';
  if (typeof item !== 'object') return String(item);
  if (item.port !== undefined) return `${item.port}${item.label ? ` ${item.label}` : ''}${item.expect === 'local_only' ? ' (local only)' : ''}`;
  if (item.path !== undefined) return `${item.label || item.id || 'file'} · ${item.path}${item.daily_suffix ? ' (daily files)' : ''}`;
  return Object.values(item).filter((v) => v !== null && v !== undefined && typeof v !== 'object').map(String).join(' ');
}

/** Flatten a nested patch into [path, value] rows (lists as readable text, secrets masked). */
function flatten(obj, prefix = '', out = []) {
  for (const [key, value] of Object.entries(obj || {})) {
    const path = prefix ? `${prefix}.${key}` : key;
    if (value && typeof value === 'object' && !Array.isArray(value)) flatten(value, path, out);
    else if (SECRET_RE.test(key)) out.push({ path, value: MASK });
    // lists of rules or files ("ports", "logs.files") would print as [object Object]
    else if (Array.isArray(value)) out.push({ path, value: value.length ? value.map(listItemText).join(', ') : 'none' });
    else out.push({ path, value: String(value) });
  }
  return out;
}

/**
 * Render the connection settings.
 * @returns {() => void} destroy
 */
export async function render(root, params, ctx) {
  let cfg;
  try {
    cfg = await loadConfig(ctx);
  } catch (err) {
    errorState(root, err);
    return () => {};
  }
  if (!cfg) return () => {};

  const wrap = el('div', 'form form--wide');
  const detected = el('div');
  const formHost = el('div');
  wrap.append(detected, formHost);
  root.replaceChildren(wrap);

  const detect = button({ label: '🔍 Detect automatically', variant: 'ghost', busyLabel: 'Looking around…', onClick: runDiscovery });
  ctx.setControls(detect);

  let fields = null;
  let form = null;
  let pgGroup = null;

  function build(current) {
    const d = current.dspace || {};
    const pg = current.postgres || {};
    const solr = current.solr || {};
    const nginx = current.nginx || {};
    const ssl = current.ssl || {};
    fields = {
      dspace: {
        install_dir: textField({ label: 'Install directory', value: d.install_dir, id: 'c-install', placeholder: '/dspace', help: 'Where config/local.cfg lives; Detect reads it.' }),
        server_url: textField({ label: 'REST API URL', value: d.server_url, id: 'c-server', type: 'url', placeholder: 'http://localhost:8080/server' }),
        ui_url: textField({ label: 'Frontend URL', value: d.ui_url, id: 'c-ui', type: 'url', placeholder: 'http://localhost:4000' }),
        public_url: textField({ label: 'Public URL', value: d.public_url, id: 'c-public', type: 'url', placeholder: 'https://repo.example.org', help: 'Checked through nginx and used for the SSL certificate. Leave empty to skip.' }),
        backend_service: textField({ label: 'Backend service (systemd)', value: d.backend_service, id: 'c-bsvc', placeholder: 'tomcat9', help: 'Empty skips the service rung.' }),
        frontend_service: textField({ label: 'Frontend service (systemd)', value: d.frontend_service, id: 'c-fsvc', placeholder: 'dspace-ui' }),
        assetstore_dir: textField({ label: 'Assetstore directory', value: d.assetstore_dir, id: 'c-assets', placeholder: '/dspace/assetstore' }),
        log_dir: textField({ label: 'Log directory', value: d.log_dir, id: 'c-logs', placeholder: '/dspace/log' }),
        verify_tls: toggle({ emoji: '🔐', label: 'Verify TLS certificates', description: 'Switch off only for self-signed certificates on a private network.', checked: d.verify_tls !== false, id: 'c-tls' }),
      },
      postgres: {
        host: textField({ label: 'Host', value: pg.host, id: 'c-pg-host', placeholder: 'localhost' }),
        port: numberField({ label: 'Port', value: pg.port, id: 'c-pg-port', min: 1, max: 65535 }),
        database: textField({ label: 'Database', value: pg.database, id: 'c-pg-db', placeholder: 'dspace' }),
        user: textField({ label: 'User', value: pg.user, id: 'c-pg-user', placeholder: 'dspace', autocomplete: 'off' }),
        password: textField({ label: 'Password', type: 'password', value: pg.password || '', id: 'c-pg-pass', placeholder: 'Empty uses peer or .pgpass authentication', help: 'Sessions are READ ONLY with a 3 s statement timeout.' }),
        sslmode: select({ options: SSL_MODES, value: pg.sslmode || 'prefer', id: 'c-pg-ssl', ariaLabel: 'SSL mode' }),
        service: textField({ label: 'Service (systemd)', value: pg.service, id: 'c-pg-svc', placeholder: 'postgresql' }),
      },
      solr: {
        url: textField({ label: 'Solr URL', value: solr.url, id: 'c-solr-url', type: 'url', placeholder: 'http://localhost:8983/solr' }),
        cores: textField({ label: 'Cores', value: (solr.cores || []).join(', '), id: 'c-solr-cores', placeholder: 'search, statistics, oai, authority', help: 'Comma-separated; every core is pinged.' }),
        service: textField({ label: 'Service (systemd)', value: solr.service, id: 'c-solr-svc', placeholder: 'solr' }),
      },
      nginx: {
        service: textField({ label: 'Service (systemd)', value: nginx.service, id: 'c-nx-svc', placeholder: 'nginx' }),
        http_url: textField({ label: 'HTTP URL', value: nginx.http_url, id: 'c-nx-http', type: 'url', placeholder: 'http://localhost', help: 'The web server in front of DSpace, not the DSpace UI\'s own port (:4000): the DSpace Frontend check covers that.' }),
        https_url: textField({ label: 'HTTPS URL', value: nginx.https_url, id: 'c-nx-https', type: 'url', placeholder: 'https://repo.example.org', help: 'Empty skips the HTTPS rung.' }),
      },
      ssl: {
        host: textField({ label: 'Certificate host', value: ssl.host, id: 'c-ssl-host', placeholder: 'defaults to the public URL host' }),
        port: numberField({ label: 'Port', value: ssl.port, id: 'c-ssl-port', min: 1, max: 65535 }),
      },
    };
    // a saved password shows as the mask: one unit, with "Saved. Hidden for safety — …" under it
    maskAsOne(fields.postgres.password.input, fields.postgres.password.node);
    const sslField = el('div', 'field', el('label', { class: 'field-label', htmlFor: 'c-pg-ssl', text: 'SSL mode' }), fields.postgres.sslmode);

    const moduleNote = (ids) => (ids.some((id) => store.moduleEnabled(id)) ? '' : ' (module off)');
    form = formEl({ id: 'connection-form', wide: true });
    pgGroup = disclosure({ summary: `PostgreSQL${moduleNote(['postgres', 'repository'])}`, open: store.moduleEnabled('postgres'), children: [
      el('div', 'pair', fields.postgres.host.node, fields.postgres.port.node),
      el('div', 'pair', fields.postgres.database.node, fields.postgres.user.node),
      el('div', 'pair', fields.postgres.password.node, sslField),
      fields.postgres.service.node,
    ] });
    form.append(
      group('DSpace', 'The URLs Sentinel checks and the directories it reads.',
        fields.dspace.install_dir.node,
        el('div', 'pair', fields.dspace.server_url.node, fields.dspace.ui_url.node),
        fields.dspace.public_url.node,
        el('div', 'pair', fields.dspace.backend_service.node, fields.dspace.frontend_service.node),
        el('div', 'pair', fields.dspace.assetstore_dir.node, fields.dspace.log_dir.node),
        fields.dspace.verify_tls),
      pgGroup,
      disclosure({ summary: `Solr${moduleNote(['solr'])}`, open: store.moduleEnabled('solr'), children: [fields.solr.url.node, fields.solr.cores.node, fields.solr.service.node] }),
      disclosure({ summary: `nginx and SSL${moduleNote(['nginx', 'ssl'])}`, open: store.moduleEnabled('nginx') || store.moduleEnabled('ssl'), children: [
        // DSpace opened directly on its own port has no web server to check: the way out, not a URL to bend
        el('p', { class: 'form-note' }, 'No web server on this machine (DSpace opened directly on :4000)? Switch nginx off in ',
          el('a', { href: '#/more/modules', text: 'More › Modules' }), '.'),
        fields.nginx.service.node,
        el('div', 'pair', fields.nginx.http_url.node, fields.nginx.https_url.node),
        el('div', 'pair', fields.ssl.host.node, fields.ssl.port.node),
      ] }),
    );
    const save = saveButton();
    form.appendChild(el('div', 'form-actions', save));
    form.addEventListener('submit', (event) => submit(event, save));
    formHost.replaceChildren(form);
  }

  function collect() {
    const f = fields;
    const text = (fld) => fld.input.value.trim();
    const port = (fld, label) => {
      fld.node.setError('');
      const v = readNumber(fld.input);
      if (v === null || Number.isNaN(v) || v < 1 || v > 65535 || v % 1 !== 0) {
        fld.node.setError(`${label} port must be between 1 and 65535.`);
        return null;
      }
      return v;
    };
    const pgPort = port(f.postgres.port, 'PostgreSQL');
    const sslPort = port(f.ssl.port, 'SSL');
    if (pgPort === null) { f.postgres.port.input.focus(); return null; }
    if (sslPort === null) { f.ssl.port.input.focus(); return null; }
    return {
      dspace: {
        install_dir: text(f.dspace.install_dir), server_url: text(f.dspace.server_url), ui_url: text(f.dspace.ui_url), public_url: text(f.dspace.public_url),
        backend_service: text(f.dspace.backend_service), frontend_service: text(f.dspace.frontend_service),
        assetstore_dir: text(f.dspace.assetstore_dir), log_dir: text(f.dspace.log_dir), verify_tls: f.dspace.verify_tls.input.checked,
      },
      postgres: { host: text(f.postgres.host), port: pgPort, database: text(f.postgres.database), user: text(f.postgres.user), password: f.postgres.password.input.value, sslmode: f.postgres.sslmode.value, service: text(f.postgres.service) },
      solr: { url: text(f.solr.url), cores: text(f.solr.cores).split(',').map((c) => c.trim()).filter(Boolean), service: text(f.solr.service) },
      nginx: { service: text(f.nginx.service), http_url: text(f.nginx.http_url), https_url: text(f.nginx.https_url) },
      ssl: { host: text(f.ssl.host), port: sslPort },
    };
  }

  /**
   * The server keeps a saved PostgreSQL password only for the host, port, database and user it was
   * saved for; re-pointing any of them without typing it again is refused (400). Say so under the
   * password field and put the cursor there, instead of a generic "Not saved" snackbar.
   */
  function passwordAgain(err) {
    if (!err || err.status !== 400 || !/password again/i.test(err.message || '')) return false;
    if (pgGroup) pgGroup.open = true;
    fields.postgres.password.node.setError('Enter the password again — the saved one is only sent to the server it was saved for, and the host, port, database or user changed.');
    fields.postgres.password.input.focus();
    fields.postgres.password.input.select(); // typing replaces the mask
    return true;
  }

  async function submit(event, save) {
    event.preventDefault();
    fields.postgres.password.node.setError('');
    const patch = collect();
    if (!patch) return;
    const saved = await submitPatch({ patch, button: save, label: 'Saved — the next checks use the new connection details.', signal: ctx.signal, onError: passwordAgain });
    if (saved) {
      cfg = saved;
      build(cfg);
    }
  }

  async function runDiscovery() {
    detect.setBusy(true);
    detected.replaceChildren(el('p', { class: 'form-note', role: 'status', text: 'Reading the DSpace configuration, listening ports and services…' }));
    let result;
    try {
      result = await api.discover({ signal: ctx.signal });
    } catch (err) {
      if (err && err.name === 'AbortError') return;
      detected.replaceChildren(emptyState('🔍', `Could not look around — ${err.message}`));
      detect.setBusy(false);
      return;
    }
    detect.setBusy(false);
    if (ctx.signal.aborted) return;
    paintDiscovery(result);
  }

  function paintDiscovery(result) {
    const suggested = { ...(result.suggested_config || {}) };
    delete suggested.modules;
    const rows = flatten(suggested);
    const findings = result.findings || [];
    const card = tile({ emoji: '🔍', title: 'We looked around', subtitle: findings.length ? `${findings.length} ${findings.length === 1 ? 'finding' : 'findings'}${result.detected_persona ? ` · looks like "${result.detected_persona}"` : ''}` : 'Nothing DSpace-specific was found on this machine.', className: 'tile--block' });
    const body = el('div', 'finding-tiles');
    if (findings.length) {
      const ul = el('ul', 'finding-list');
      for (const line of findings) ul.appendChild(el('li', { text: line }));
      body.appendChild(ul);
    }
    const apply = button({ label: 'Apply', variant: 'filled', size: 'sm', busyLabel: 'Applying…', disabled: !rows.length, onClick: async () => {
      const saved = await submitPatch({ patch: suggested, button: apply, label: 'Detected settings applied.', signal: ctx.signal, onError: passwordAgain });
      if (saved) {
        cfg = saved;
        build(cfg);
        detected.replaceChildren();
      }
    } });
    const dismiss = button({ label: 'Dismiss', variant: 'text', size: 'sm', onClick: () => detected.replaceChildren() });
    if (rows.length) {
      body.appendChild(table({
        ariaLabel: 'Suggested settings',
        columns: [{ key: 'path', label: 'Setting', format: (v) => el('span', { class: 'mono', text: v }) }, { key: 'value', label: 'Suggested value', format: (v) => el('span', { class: 'mono', text: v }) }],
        rows,
      }));
      body.appendChild(el('p', { class: 'form-note', text: 'Modules stay as they are — switch them on under More.' }));
    }
    body.appendChild(el('div', 'form-actions', apply, dismiss));
    card.appendChild(body);
    detected.replaceChildren(card);
    card.querySelector('.tile-title').tabIndex = -1;
    card.querySelector('.tile-title').focus({ preventScroll: true });
  }

  build(cfg);
  return () => {};
}

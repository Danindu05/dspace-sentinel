/**
 * screens/detail/security.js — the "Right now" tiles of the Security page (#/stack/security).
 *
 * Security is a read-only look at how exposed this server is: eight rungs, each with metrics of its own
 * (SECURITY-CONTRACT). Its page shows the ladder, with each rung's hint, and these tiles in the order of the
 * rungs: Open ports (a table: Port · Program · Address · Reachable · Expected · Verdict, the ports to look at
 * first, a port's note beside its verdict, the rung's note under the table), Firewall, SSH, Web (with the
 * response headers the answer lacks), Updates, Software (a table:
 * Software · Version · Supported until · Status, with the notes under it), Files and Sentinel. A tile's
 * subtitle is its rung's message, toned like the rung; an informational rung (Sentinel may not read the
 * file, or this system has nothing to read) stays muted, and its tile leaves out values it does not have.
 *
 * component.js passes its tile helpers in (`securityTiles(comp, helpers)`), so the two modules do not import
 * each other.
 */
import { el, fmt, statusLabel, statusPill, table } from '../../ui.js';
import { isInformational, rungMessage } from '../stack.js';

/** The ports to look at first: new and exposed, then those only Tailscale reaches, the expected ones, localhost. */
const VERDICT_RANK = { new: 0, exposed: 1, tailscale: 2, expected: 3, local: 4 };
const EXPECTED_WORDS = { open: 'open', local_only: 'local only' };
/** How a support status reads in the Software table: [pill status (null = a plain pill), words]. */
const SUPPORT = {
  ok: ['ok', 'Supported'],
  soon: [null, 'Ends soon'],
  ended: ['warn', 'Ended'],
  affected: ['warn', 'Affected'],
  unknown: [null, 'Not known'],
};

function asObject(value) {
  return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
}

function asList(value) {
  return Array.isArray(value) ? value.filter((item) => item && typeof item === 'object') : [];
}

function known(value) {
  return value !== null && value !== undefined && value !== '';
}

function rungOf(comp, name) {
  return (comp.chain || []).find((r) => r.id === `security.${name}`) || null;
}

/** A plain pill: the words alone, without a status colour or mark. */
function plainPill(text) {
  return el('span', { class: 'pill', text });
}

/** "local.cfg" from "/dspace/config/local.cfg". */
function baseName(path) {
  const parts = String(path ?? '').split('/').filter(Boolean);
  return parts.length ? parts[parts.length - 1] : String(path ?? '');
}

/**
 * One tile: its rung's message as the subtitle (in the rung's tone; muted when informational), then the
 * key/value list when there is something to list.
 */
function kvTile(h, { emoji, title, rung, pairs = [] }) {
  return h.blockTile({ emoji, title, subtitle: headLine(h, rung) }, pairs.length ? el('div', 'kv-wrap', h.kv(pairs)) : null);
}

function headLine(h, rung) {
  if (!rung) return h.subLine('not checked yet');
  const text = rungMessage(rung) || statusLabel(rung.status);
  return h.subLine(text, isInformational(rung) ? '' : rung.status);
}

// ------------------------------------------------------------------ open ports

function verdictPill(verdict) {
  if (verdict === 'new') return statusPill('warn', 'New');
  if (verdict === 'exposed') return statusPill('warn', 'Exposed');
  if (verdict === 'local') return plainPill('Local');
  if (verdict === 'expected') return plainPill('Expected');
  // reachable only through the Tailscale network (100.64.0.0/10, fd7a:115c:a1e0::/48): not exposed
  if (verdict === 'tailscale') return plainPill('Tailscale');
  return verdict ? plainPill(String(verdict)) : '—';
}

/**
 * The Verdict cell: the pill, and the collector's note on the port beside it ("reachable through Tailscale
 * only", "Tailscale's own port (WireGuard)", "Sentinel"), which drops under the pill when the column is narrow.
 */
function verdictCell(verdict, row) {
  const pill = verdictPill(verdict);
  if (!known(row.note) || pill === '—') return pill;
  return el('span', 'name-tag', pill, el('span', { class: 'muted', text: String(row.note) }));
}

function byAttention(a, b) {
  const rank = (p) => (p.verdict in VERDICT_RANK ? VERDICT_RANK[p.verdict] : 4);
  return rank(a) - rank(b) || (Number(a.port) || 0) - (Number(b.port) || 0) || String(a.proto).localeCompare(String(b.proto));
}

/**
 * Every listening socket: where it listens, whether other machines can reach it, what the port rules expect,
 * and the verdict (expected, Tailscale and local ones plain, exposed and new ones a warning). A UDP port carries
 * a "udp" tag; an address may wrap at its dots and colons (an IPv6 address in a long list). The rung's note
 * (`detail.note`: program names are known only for Sentinel's own user) goes under the table.
 */
function portsTile(comp, m, h) {
  const rung = rungOf(comp, 'ports');
  const rows = asList(m.ports).slice().sort(byAttention);
  const note = rung && rung.detail && typeof rung.detail.note === 'string' ? rung.detail.note.trim() : '';
  const noteNodes = note ? el('ul', { class: 'note-list', 'aria-label': 'Port notes' },
    el('li', null, el('span', { class: 'tile-sub' }, ...(h.hintNodes ? h.hintNodes(note) : [note])))) : null;
  return h.blockTile({ emoji: '🔌', title: 'Open ports', subtitle: headLine(h, rung), className: 'span-full' },
    table({
      ariaLabel: 'Open ports',
      rowKey: (row) => `${row.port}/${row.proto}`,
      columns: [
        {
          key: 'port', label: 'Port',
          format: (v, row) => (row.proto === 'udp'
            ? el('span', 'name-tag', el('span', { text: String(v ?? '—') }), el('span', { class: 'tag', text: 'udp' }))
            : String(v ?? '—')),
        },
        { key: 'process', label: 'Program', format: (v) => (known(v) ? String(v) : '—') },
        {
          key: 'addrs', label: 'Address',
          format: (v) => {
            const addrs = Array.isArray(v) ? v.filter((a) => typeof a === 'string' && a) : [];
            return addrs.length ? el('span', null, ...h.breakable(addrs.join(', '))) : '—';
          },
        },
        { key: 'public', label: 'Reachable', format: (v) => (v ? 'yes' : 'no') },
        { key: 'expected', label: 'Expected', format: (v) => EXPECTED_WORDS[v] || 'no rule' },
        { key: 'verdict', label: 'Verdict', format: (v, row) => verdictCell(v, row) },
      ],
      rows,
      emptyText: rung && isInformational(rung) ? 'No ports to show.' : 'Ports appear after the first check.',
    }),
    noteNodes);
}

// ------------------------------------------------------------------ firewall, SSH, web, updates

/** "Reachable without a rule" is the collector's count (metrics.exposed: exposed and new ports), never recounted here. */
function firewallTile(comp, m, h) {
  const fw = asObject(m.firewall);
  const pairs = [];
  if (fw.active === true) pairs.push(['Host firewall', fw.name ? `${fw.name} (active)` : 'active']);
  else if (fw.active === false) pairs.push(['Host firewall', 'none active']);
  if (fw.active !== null && fw.active !== undefined && typeof m.exposed === 'number') {
    pairs.push(['Reachable without a rule', m.exposed ? fmt.plural(m.exposed, 'port') : 'none']);
  }
  return kvTile(h, { emoji: '🧱', title: 'Firewall', rung: rungOf(comp, 'firewall'), pairs });
}

function sshTile(comp, m, h) {
  const ssh = asObject(m.ssh);
  // OpenSSH's defaults stand in for what sshd_config does not say; without the file they would only guess
  const pairs = ssh.readable === false ? [] : [
    known(ssh.permit_root_login) ? ['Root login', String(ssh.permit_root_login)] : null,
    known(ssh.password_authentication) ? ['Passwords', String(ssh.password_authentication)] : null,
    known(ssh.port) ? ['Port', String(ssh.port), true] : null,
  ].filter(Boolean);
  return kvTile(h, { emoji: '🔑', title: 'SSH', rung: rungOf(comp, 'ssh'), pairs });
}

function webTile(comp, m, h) {
  const web = asObject(m.web);
  const scheme = String(web.scheme || '').toLowerCase();
  const pairs = [['Public address', known(web.public_url) ? String(web.public_url) : 'not set']];
  if (scheme === 'https') pairs.push(['Encryption', 'HTTPS']);
  else if (scheme === 'http') pairs.push(['Encryption', 'none (plain HTTP)']);
  // the headers come with the answer to the public URL: without one nothing was asked
  if (scheme && Array.isArray(web.headers_missing)) {
    pairs.push(['Missing headers', web.headers_missing.length ? web.headers_missing.map(String).join(', ') : 'none']);
  }
  return kvTile(h, { emoji: '🌐', title: 'Web', rung: rungOf(comp, 'web'), pairs });
}

function updatesTile(comp, m, h) {
  const u = asObject(m.updates);
  const pending = known(u.pending) ? Number(u.pending) : null;
  const security = known(u.security) ? Number(u.security) : null;
  const pairs = [];
  if (security !== null) pairs.push(['Security updates', fmt.number(security), true]);
  if (security !== null && pending !== null) pairs.push(['Other updates', fmt.number(Math.max(0, pending - security)), true]);
  else if (pending !== null) pairs.push(['Pending updates', fmt.number(pending), true]);
  if (typeof u.reboot_required === 'boolean') pairs.push(['Reboot required', u.reboot_required ? 'yes' : 'no']);
  if (known(u.source)) pairs.push(['Read from', String(u.source)]);
  return kvTile(h, { emoji: '📦', title: 'Updates', rung: rungOf(comp, 'updates'), pairs });
}

// ------------------------------------------------------------------ software

/**
 * DSpace, Solr, PostgreSQL and the operating system with the day their support ends, and under the table
 * what is wrong with the ones that ended or are affected (a warning) or end soon.
 */
function softwareTile(comp, m, h) {
  const rows = asList(m.software);
  const notes = rows.filter((r) => known(r.note) && r.status !== 'ok' && r.status !== 'unknown').map((r) => el('li', null,
    h.subLine(`${r.name}${known(r.version) ? ` ${r.version}` : ''}: ${r.note}`, r.status === 'ended' || r.status === 'affected' ? 'warn' : '')));
  return h.blockTile({ emoji: '🏷️', title: 'Software', subtitle: headLine(h, rungOf(comp, 'software')), className: 'span-full' },
    table({
      ariaLabel: 'Software',
      columns: [
        { key: 'name', label: 'Software' },
        { key: 'version', label: 'Version', format: (v) => (known(v) ? String(v) : '—') },
        { key: 'until', label: 'Supported until', format: (v) => el('span', { class: 'nowrap', text: known(v) ? String(v) : '—' }) },
        {
          key: 'status', label: 'Status',
          format: (v) => {
            const [status, words] = SUPPORT[v] || SUPPORT.unknown;
            return status ? statusPill(status, words) : plainPill(words);
          },
        },
      ],
      rows,
      emptyText: 'Versions appear once the other services have been checked.',
    }),
    notes.length ? el('ul', { class: 'note-list', 'aria-label': 'Support notes' }, ...notes) : null);
}

// ------------------------------------------------------------------ files, Sentinel

/** "mode 640 · tomcat:tomcat", and "readable by every user" when others may read it. */
function fileWords(f) {
  const owner = known(f.owner) ? `${f.owner}${known(f.group) ? `:${f.group}` : ''}` : '';
  return [known(f.mode) ? `mode ${f.mode}` : '', owner, f.world_readable ? 'readable by every user' : ''].filter(Boolean).join(' · ') || '—';
}

function filesTile(comp, m, h) {
  const pairs = asList(m.files).map((f) => [baseName(f.path), fileWords(f)]);
  return kvTile(h, { emoji: '📄', title: 'Files', rung: rungOf(comp, 'files'), pairs });
}

/**
 * Where Sentinel listens, whether the dashboard login is set and whether a token for scripts is set: from
 * `metrics.sentinel` ({host, login, auth}) when the collector reports it, else from the rung's own words
 * ("with a login"; "with a password" = a token and no login; "without a password" = neither).
 */
function sentinelTile(comp, m, h) {
  const rung = rungOf(comp, 'sentinel');
  const s = asObject(m.sentinel);
  const message = String((rung && rung.message) || '');
  const host = known(s.host) ? String(s.host) : (message.match(/listens on (\S+)/) || [])[1] || null;
  let login = typeof s.login === 'boolean' ? s.login : null;
  let token = typeof s.auth === 'boolean' ? s.auth : null;
  if (/\bwith a login\b/.test(message)) {
    if (login === null) login = true;
  } else if (/\bwithout a password\b/.test(message)) {
    if (login === null) login = false;
    if (token === null) token = false;
  } else if (/\bwith a password\b/.test(message)) {
    if (login === null) login = false;
    if (token === null) token = true;
  }
  const pairs = [
    host ? ['Listens on', host] : null,
    login === null ? null : ['Login', login ? 'set' : 'not set'],
    token === null ? null : ['Token', token ? 'set' : 'not set'],
  ].filter(Boolean);
  return kvTile(h, { emoji: '🛰️', title: 'Sentinel', rung, pairs });
}

/**
 * The Security page's tiles, in the order of its rungs.
 * @param {object} comp the component (its chain and metrics)
 * @param {{blockTile: Function, kv: Function, subLine: Function, breakable: Function, hintNodes: Function}} h
 *   component.js's tile helpers
 * @returns {HTMLElement[]}
 */
export function securityTiles(comp, h) {
  const m = asObject(comp.metrics);
  return [
    portsTile(comp, m, h),
    firewallTile(comp, m, h),
    sshTile(comp, m, h),
    webTile(comp, m, h),
    updatesTile(comp, m, h),
    softwareTile(comp, m, h),
    filesTile(comp, m, h),
    sentinelTile(comp, m, h),
  ];
}

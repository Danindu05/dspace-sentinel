/**
 * screens/more/about.js — #/more/about (SPEC §12.5).
 *
 * Version, Sentinel uptime, retention, database size (when the API reports it), the Export data
 * link and the documentation / GitHub links.
 */
import api, { apiUrl } from '../../api.js';
import { store } from '../../store.js';
import { el, fmt, section, tile } from '../../ui.js';

export const title = 'About';

const REPO = 'https://github.com/Danindu05/dspace-sentinel';

function kv(pairs) {
  const dl = el('dl', 'kv');
  for (const [k, v, numeric] of pairs) {
    if (v === null || v === undefined || v === '') continue;
    dl.appendChild(el('dt', { text: k }));
    dl.appendChild(el('dd', { class: numeric ? 'numeric' : '', text: v }));
  }
  return dl;
}

function externalTile(opts) {
  const node = tile({ ...opts, chevron: true });
  node.target = '_blank';
  node.rel = 'noopener';
  return node;
}

/**
 * Render the About page.
 * @returns {() => void} destroy
 */
export function render(root, params, ctx) {
  const facts = tile({ emoji: '🛰️', title: 'DSpace Sentinel', subtitle: 'Observe-only monitoring for this server. It never writes to DSpace, PostgreSQL or Solr.', className: 'tile--block' });
  const factsBody = el('div', 'kv-wrap');
  facts.appendChild(factsBody);

  const dataSection = section({ title: 'Data', layout: 'stack', id: 'about-data' });
  const exportTile = externalTile({ emoji: '📦', title: 'Export data', subtitle: 'JSON of the last 30 days: samples, checks, alerts, incidents, repository stats and events.', href: String(api.exportUrl('30d')) });
  exportTile.setAttribute('download', 'sentinel-export.json');
  dataSection.body.appendChild(exportTile);

  const linksSection = section({ title: 'Learn more', layout: 'stack', id: 'about-links' });
  linksSection.body.append(
    externalTile({ emoji: '📖', title: 'Documentation', subtitle: 'Configuration, alerts, reports, Telegram and reverse-proxy guides.', href: `${REPO}/tree/main/docs` }),
    externalTile({ emoji: '🐙', title: 'GitHub', subtitle: 'Source code, releases and issues.', href: REPO }),
    externalTile({ emoji: '🔌', title: 'JSON API', subtitle: 'Everything on these screens is also available at api/… (OpenAPI schema).', href: String(apiUrl('api/openapi.json')) }),
  );

  root.replaceChildren(el('div', 'form form--wide', facts, dataSection, linksSection));

  function paint() {
    const meta = store.get('meta') || {};
    const status = store.get('status') || {};
    const up = meta.started_ts ? Date.now() / 1000 - meta.started_ts : null;
    factsBody.replaceChildren(kv([
      ['Version', meta.version ? `v${meta.version}` : '—'],
      ['Host', meta.hostname || status.hostname || '—'],
      ['Mode', meta.demo ? 'Demo data (synthetic server and services)' : 'Live'],
      ['Sentinel uptime', up === null ? '—' : fmt.duration(up), true],
      ['Checks', meta.system_interval ? `server every ${fmt.duration(meta.system_interval)}` : '—'],
      ['Retention', meta.retention_days ? `${fmt.plural(meta.retention_days, 'day')} of raw samples · hourly roll-ups for a year` : '—'],
      ['Database size', meta.db_size_bytes ? fmt.bytes(meta.db_size_bytes) : null, true],
      ['Last check', status.checked_at ? fmt.clock(status.checked_at, true) : '—', true],
    ]));
  }
  paint();
  const timer = setInterval(paint, 1000);
  const off = store.subscribe(['meta', 'status'], paint);
  return () => {
    clearInterval(timer);
    off();
  };
}

/**
 * screens/more/modules.js — module switches (SPEC §12.5 "More").
 *
 * The list is rendered on the More root and on #/more/modules. Each switch applies at once via
 * PATCH /api/modules and offers Undo in a snackbar; a module that is off leaves no trace anywhere.
 * `moduleList(ctx)` is shared with screens/more.js.
 */
import api from '../../api.js';
import { store } from '../../store.js';
import { el, snackbar, toggle } from '../../ui.js';

export const title = 'Modules';

/** [id, emoji, label, one-line explainer] — the exact copy from the SPEC, in its order. */
export const MODULES = [
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

/** Label of a module id. */
export function moduleLabel(id) {
  const row = MODULES.find((m) => m[0] === id);
  return row ? row[2] : id;
}

/**
 * The switch list. Applies instantly, offers Undo, and follows the live module map.
 * @returns {{el: HTMLElement, destroy(): void}}
 */
export function moduleList(ctx) {
  const list = el('div', { class: 'tile-list', role: 'group', 'aria-label': 'Modules' });
  const rows = new Map();
  const current = store.modules();
  for (const [id, emoji, label, description] of MODULES) {
    const row = toggle({ emoji, label, description, checked: Boolean(current[id]), id: `module-${id}`, name: id, onChange: (on) => change(id, on, true) });
    row.dataset.key = `module-${id}`;
    rows.set(id, row);
    list.appendChild(row);
  }

  /** Switch a module; resolves to its switch so Undo can hand focus back to it. */
  async function change(id, on, undoable) {
    const row = rows.get(id);
    const label = moduleLabel(id);
    row.set(on);
    row.input.disabled = true;
    try {
      await api.patchModules({ [id]: on });
      await ctx.refreshMeta();
      const text = `${label} turned ${on ? 'on' : 'off'}`;
      if (undoable) snackbar.undo(text, () => change(id, !on, false));
      else snackbar(text);
    } catch (err) {
      if (err && err.name === 'AbortError') return null;
      row.set(!on);
      snackbar(`Could not switch ${label} ${on ? 'on' : 'off'} — ${err.message}`, { duration: 8000 });
    } finally {
      row.input.disabled = false;
    }
    return row.input;
  }

  const off = store.on('modules', (mods) => {
    if (!mods) return;
    for (const [id, row] of rows) if (id in mods && !row.input.disabled) row.set(Boolean(mods[id]));
  });
  return { el: list, destroy: off };
}

/**
 * Render #/more/modules.
 * @returns {() => void} destroy
 */
export function render(root, params, ctx) {
  const list = moduleList(ctx);
  root.replaceChildren(
    el('div', 'form form--wide',
      el('p', { class: 'lede', text: 'Turn on only what you need. Sentinel stays simple.' }),
      list.el,
    ),
  );
  return () => list.destroy();
}

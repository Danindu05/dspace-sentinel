/**
 * screens/more/appearance.js — #/more/appearance: theme (SPEC §12.5).
 *
 * System / Light / Dark chips (persisted through ctx.setTheme) and a legend of the four mood
 * accents so the colour language is explained once.
 */
import { store } from '../../store.js';
import { chipRow, el, tile } from '../../ui.js';

export const title = 'Appearance';

const MOODS = [
  ['var(--mint)', 'Healthy', 'Every check passes'],
  ['var(--butter)', 'Warning', 'Something needs a look'],
  ['var(--coral)', 'Critical', 'A service is down'],
  ['var(--sky)', 'Unknown', 'Before the first check'],
];

/**
 * Render the appearance settings.
 * @returns {() => void} destroy
 */
export function render(root, params, ctx) {
  const chips = chipRow({
    options: [{ value: 'system', label: 'System' }, { value: 'light', label: 'Light' }, { value: 'dark', label: 'Dark' }],
    value: store.get('theme') || 'system', ariaLabel: 'Theme', name: 'theme',
    onChange: (value) => ctx.setTheme(value),
  });
  const themeTile = tile({ emoji: '🎨', title: 'Theme', subtitle: 'System follows your operating system and switches with it. Saved for this server.', className: 'tile--block' });
  themeTile.appendChild(el('div', { class: 'field' }, chips));

  const moodTile = tile({ emoji: '🎯', title: 'Colour tells you where you are', subtitle: 'The accent follows the current health and recolours the "＋" button, the active tab, chips and toggles. The four pastels stay the same in both themes, so text on them is always dark ink.', className: 'tile--block' });
  const swatches = el('div', { class: 'swatch-row', role: 'list' });
  for (const [color, label, sub] of MOODS) {
    swatches.appendChild(el('div', { class: 'swatch', role: 'listitem', style: { background: color } }, el('b', { text: label }), el('span', { text: sub })));
  }
  moodTile.appendChild(swatches);

  root.replaceChildren(el('div', 'form', themeTile, moodTile));
  const off = store.subscribe('theme', (s) => chips.set(s.theme, { silent: true }));
  return () => off();
}

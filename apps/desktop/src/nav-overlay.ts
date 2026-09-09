import { authenticatedFetch } from './api-auth';
import { applyPanelChrome } from './fx/panel-chrome';
import { staggerIn } from './motion';
import { getOverlays } from './overlay';

const WIDGET_TYPES = ['sleep', 'training', 'tasks', 'calendar', 'nutrition', 'habits', 'brain', 'system', 'skills', 'weather', 'brain_graph'] as const;
const LABELS: Record<string, string> = {
  sleep: 'Schlaf', training: 'Training', tasks: 'Aufgaben',
  calendar: 'Kalender', nutrition: 'Ernährung', habits: 'Habits',
  brain: 'Second Brain', system: 'System', skills: 'Skills', weather: 'Wetter',
  brain_graph: 'Brain-Graph',
};

export function initNavOverlay(baseUrl: string): void {
  const overlay = document.createElement('div');
  overlay.id = 'nav-overlay';
  // Overlay-Kacheln kommen aus der Registry (overlay.ts) — ein neues Overlay
  // taucht hier automatisch auf, sobald es sich registriert hat.
  const overlayTiles = getOverlays()
    .map((o) => `<button class="nav-tile" data-open-event="${o.openEvent}">${o.label}</button>`)
    .join('');
  const tiles = WIDGET_TYPES.map(
    (t) => `<button class="nav-tile" data-widget-type="${t}">${LABELS[t]}</button>`
  ).join('')
    + overlayTiles
    + `<button class="nav-tile" data-widget-type="">Home</button>`;
  overlay.innerHTML = `<div class="nav-grid">${tiles}</div>`;
  document.body.appendChild(overlay);

  const grid = overlay.querySelector<HTMLElement>('.nav-grid')!;
  grid.querySelectorAll<HTMLElement>('.nav-tile').forEach((tile) => {
    applyPanelChrome(tile);
  });

  function close(): void {
    overlay.classList.remove('visible');
  }

  overlay.querySelectorAll<HTMLButtonElement>('.nav-tile').forEach((tile) => {
    tile.addEventListener('click', () => {
      if (tile.dataset.openEvent) {
        document.dispatchEvent(new CustomEvent(tile.dataset.openEvent));
        close();
        return;
      }
      const widgetType = tile.dataset.widgetType;
      if (widgetType) {
        authenticatedFetch(`${baseUrl}/api/ui/select`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ widget_type: widgetType }),
        }).catch(() => {});
      } else {
        authenticatedFetch(`${baseUrl}/api/ui/clear`, { method: 'POST' }).catch(() => {});
      }
      close();
    });
  });

  document.addEventListener('keydown', (e) => {
    const isToggle = (e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k';
    if (isToggle) {
      e.preventDefault();
      const willBeVisible = !overlay.classList.contains('visible');
      overlay.classList.toggle('visible');
      if (willBeVisible) {
        staggerIn(grid.querySelectorAll<HTMLElement>('.nav-tile'), 50);
      }
    } else if (e.key === 'Escape') {
      close();
    }
  });
}

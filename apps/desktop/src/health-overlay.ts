import { authenticatedFetch } from './api-auth';
// Health-Overlay — Vollbild-Ansicht (Ring-Hero + Drilldown), holt sich die
// Domain-Scores selbst von /api/health/scores. Geöffnet per Cmd/Ctrl+K-Kachel
// "Health" oder dem CustomEvent 'open-health' (z.B. aus einem Voice-Intent).
//
// Reine Render-Funktionen (overviewHtml/drilldownHtml) sind vom Fetch/Init
// getrennt und per vitest getestet. Ehrlich bei fehlenden Daten: "—" statt Fake.

import { createOverlay, registerOverlay } from './overlay';

type Domain = { score: number | null; status: string; coverage: number; components: any[] };
type ScoresData = {
  narrative?: string | null;
  body?: { latest: number; delta_30d: number | null; direction: string } | null;
  today?: { date: string; domains: Record<string, Domain> } | null;
  days?: any[];
};

const DOMAIN_ORDER = ['recovery', 'sleep', 'activity'] as const;
const DOMAIN_META: Record<string, { label: string; color: string }> = {
  recovery: { label: 'Recovery', color: '#3ee0c8' },
  sleep: { label: 'Sleep', color: '#8b9cff' },
  activity: { label: 'Activity', color: '#f5c451' },
  body: { label: 'Body', color: '#7fe081' },
};
// Spiegelt domains/health_scores.METRIC_LABELS (bewusste Duplikation über die
// Sprachgrenze, wie LAYOUT_SLOTS in main.ts).
const METRIC_LABELS: Record<string, string> = {
  steps: 'Schritte', active_calories: 'Aktiv-Kalorien', exercise_minutes: 'Trainingsminuten',
  hrv: 'HRV', resting_hr: 'Ruhepuls', sleep_duration: 'Schlafdauer', sleep_deep: 'Tiefschlaf',
  blood_oxygen: 'SpO₂', weight: 'Gewicht',
};

function esc(s: string): string {
  return s.replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' })[c] as string);
}

function ringSvg(score: number | null, color: string, size = 64): string {
  if (score == null) {
    return `<svg viewBox="0 0 100 100" width="${size}" height="${size}"><circle cx="50" cy="50" r="42" fill="none" stroke="#22333a" stroke-width="8" stroke-dasharray="5 6"/><text x="50" y="60" text-anchor="middle" font-size="30" fill="#5a6b73">—</text></svg>`;
  }
  const off = Math.round(264 * (1 - score / 100));
  return `<svg viewBox="0 0 100 100" width="${size}" height="${size}"><circle cx="50" cy="50" r="42" fill="none" stroke="#16262d" stroke-width="8"/><circle cx="50" cy="50" r="42" fill="none" stroke="${color}" stroke-width="8" stroke-linecap="round" stroke-dasharray="264" stroke-dashoffset="${off}" transform="rotate(-90 50 50)"/><text x="50" y="60" text-anchor="middle" font-size="30" fill="#e8fbf7">${Math.round(score)}</text></svg>`;
}

function ringBlock(key: string, dom: Domain | undefined): string {
  const meta = DOMAIN_META[key];
  return `<button class="ho-ring" data-domain="${key}">${ringSvg(dom?.score ?? null, meta.color)}<span class="ho-rlab">${meta.label}</span></button>`;
}

export function overviewHtml(data: ScoresData): string {
  const domains = data?.today?.domains ?? {};
  const rings = DOMAIN_ORDER.map((k) => ringBlock(k, domains[k])).join('');
  const b = data?.body;
  const arrow = b ? ({ down: '↓', up: '↑', stable: '→' }[b.direction] ?? '') : '';
  const bodyTile = b
    ? `<div class="ho-ring ho-body"><div class="ho-bodyval">${b.latest}<span>kg ${arrow}</span></div><span class="ho-rlab">Body</span></div>`
    : '';
  const narr = data?.narrative ? `<div class="ho-narr">🜁 ${esc(data.narrative)}</div>` : '';
  return `<div class="ho-head"><h2>Health</h2><button class="ho-close" data-action="close">✕</button></div>
    <div class="ho-rings">${rings}${bodyTile}</div>${narr}`;
}

// Kompakte HUD-Glance-Variante (rendert im Widget-Grid aus einem Backend-Payload).
export function widgetHtml(p: { domains?: Record<string, { score: number | null; status?: string }> }): string {
  const domains = p?.domains ?? {};
  const rings = DOMAIN_ORDER.map(
    (k) =>
      `<div class="ho-ring">${ringSvg(domains[k]?.score ?? null, DOMAIN_META[k].color, 56)}<span class="ho-rlab">${DOMAIN_META[k].label}</span></div>`,
  ).join('');
  return `<div class="widget-title">🫀 Health</div><div class="ho-rings">${rings}</div>`;
}

export function drilldownHtml(key: string, dom: Domain, _days: any[]): string {
  const meta = DOMAIN_META[key];
  const back = `<button class="ho-back" data-action="overview">‹ Übersicht</button>`;
  if (!dom || dom.status !== 'ok' || dom.score == null) {
    return `<div class="ho-drill">${back}<h2>${meta.label}</h2>
      <div class="ho-empty">— zu wenig Daten. Erscheint, sobald die COROS synchronisiert.</div></div>`;
  }
  const rows = (dom.components ?? [])
    .map((c) => {
      const label = METRIC_LABELS[c.metric] ?? c.metric;
      const sub = c.used && c.score != null ? Math.round(c.score) : '—';
      const width = c.used && c.score != null ? c.score : 0;
      const val = c.value == null ? '—' : c.value;
      return `<div class="ho-wrow"><span class="ho-wlab">${label} <em>${val}</em></span>
        <span class="ho-track"><span class="ho-fill" style="width:${width}%;background:${meta.color}"></span></span>
        <span class="ho-wsc">${sub}<em>·${Math.round(c.weight * 100)}%</em></span></div>`;
    })
    .join('');
  return `<div class="ho-drill">${back}
    <div class="ho-dhead">${ringSvg(dom.score, meta.color, 96)}<h2>${meta.label}</h2></div>
    <div class="ho-why">Woraus sich ${Math.round(dom.score)} ergibt</div>${rows}</div>`;
}

export function initHealthOverlay(baseUrl: string, fetchImpl: typeof fetch = authenticatedFetch): { open: () => void } {
  let data: ScoresData | null = null;
  const { el, open } = createOverlay({
    id: 'health-overlay',
    openEvent: 'open-health',
    background: 'rgba(8,14,18,0.96)',
    render: async (container, { close }) => {
      const showOverview = (): void => {
        container.innerHTML = data ? overviewHtml(data) : '<div class="ho-empty">Lade …</div>';
        container.querySelectorAll<HTMLElement>('.ho-ring[data-domain]').forEach((ring) => {
          ring.addEventListener('click', () => {
            const key = ring.dataset.domain!;
            container.innerHTML = drilldownHtml(key, data!.today!.domains[key], data!.days ?? []);
            container.querySelector('[data-action="overview"]')?.addEventListener('click', showOverview);
          });
        });
        container.querySelector('[data-action="close"]')?.addEventListener('click', close);
      };
      showOverview();
      try {
        data = await (await fetchImpl(`${baseUrl}/api/health/scores`)).json();
        showOverview();
      } catch {
        container.innerHTML = '<div class="ho-empty">Health-Daten nicht erreichbar.</div>';
      }
    },
  });
  el.style.padding = '32px 40px';
  registerOverlay({ key: 'health', label: 'Health', openEvent: 'open-health' });
  return { open };
}

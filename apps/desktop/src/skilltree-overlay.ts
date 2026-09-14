import { authenticatedFetch } from './api-auth';
// Skilltree-Overlay — Achsen-Level + Nodes + adaptive Quests. Holt /api/skilltree
// selbst. Reine Render-Funktionen (overviewHtml/axisDetailHtml/widgetHtml) sind
// vom Fetch getrennt und per vitest getestet. Ehrlich: Level 0 statt Fake.

import { createOverlay, registerOverlay } from './overlay';

type Axis = { axis: string; label: string; xp: number; level: number; trend: number };
type Node = { key: string; label: string; axis: string };
type Quest = { key: string; axis: string; label: string;
               progress: { count: number; pct: number; done: boolean } };
type SkilltreeData = { axes: Axis[]; nodes: Node[]; quests: Quest[] };

const AXIS_COLOR: Record<string, string> = {
  koerper: '#3ee0c8', wissen: '#8b9cff', schaffen: '#f5c451',
  geist: '#c78bff', disziplin: '#7fe081',
};

function esc(s: string): string {
  return s.replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' })[c] as string);
}

function trendArrow(trend: number): string {
  return trend <= -5 ? '↓' : trend >= 5 ? '↑' : '→';
}

function axisRow(a: Axis): string {
  const color = AXIS_COLOR[a.axis] ?? '#8fb';
  return `<div class="st-axis" data-axis="${a.axis}">
    <span class="st-alabel" style="color:${color}">${esc(a.label)}</span>
    <span class="st-alevel">Lv${a.level}</span>
    <span class="st-atrend">${trendArrow(a.trend)}</span></div>`;
}

function questRow(q: Quest): string {
  const pct = Math.round(q.progress.pct * 100);
  return `<div class="st-quest"><span class="st-qlabel">${esc(q.label)}</span>
    <span class="st-qtrack"><span class="st-qfill" style="width:${pct}%"></span></span>
    <span class="st-qpct">${pct}%</span></div>`;
}

export function overviewHtml(data: SkilltreeData): string {
  const axes = (data?.axes ?? []).map(axisRow).join('');
  const quests = (data?.quests ?? []).map(questRow).join('') || '<div class="st-empty">Keine offene Quest.</div>';
  const nodes = (data?.nodes ?? [])
    .map((n) => `<span class="st-node">✦ ${esc(n.label)}</span>`).join('') || '';
  return `<div class="st-head"><h2>Skilltree</h2><button class="ho-close" data-action="close">✕</button></div>
    <div class="st-axes">${axes}</div>
    <div class="st-section">Quests</div><div class="st-quests">${quests}</div>
    ${nodes ? `<div class="st-section">Freigeschaltet</div><div class="st-nodes">${nodes}</div>` : ''}`;
}

export function axisDetailHtml(axis: Axis, quests: Quest[]): string {
  const mine = quests.filter((q) => q.axis === axis.axis).map(questRow).join('')
    || '<div class="st-empty">Keine Quest für diese Achse.</div>';
  return `<div class="st-drill"><button class="ho-back" data-action="overview">‹ Übersicht</button>
    <h2>${esc(axis.label)} — Lv${axis.level}</h2>
    <div class="st-why">XP ${Math.round(axis.xp)} · Trend ${trendArrow(axis.trend)}</div>
    <div class="st-quests">${mine}</div></div>`;
}

export function widgetHtml(p: SkilltreeData): string {
  const axes = (p?.axes ?? [])
    .map((a) => `<span class="st-chip" style="border-color:${AXIS_COLOR[a.axis] ?? '#8fb'}">${esc(a.label)} ${a.level}</span>`)
    .join('');
  return `<div class="widget-title">🌳 Skilltree</div><div class="st-chips">${axes}</div>`;
}

export function initSkilltreeOverlay(baseUrl: string, fetchImpl: typeof fetch = authenticatedFetch): { open: () => void } {
  let data: SkilltreeData | null = null;
  const { el, open } = createOverlay({
    id: 'skilltree-overlay',
    openEvent: 'open-skilltree',
    background: 'rgba(8,14,18,0.96)',
    render: async (container, { close }) => {
      const showOverview = (): void => {
        container.innerHTML = data ? overviewHtml(data) : '<div class="st-empty">Lade …</div>';
        container.querySelectorAll<HTMLElement>('.st-axis[data-axis]').forEach((row) => {
          row.addEventListener('click', () => {
            const axis = data!.axes.find((a) => a.axis === row.dataset.axis)!;
            container.innerHTML = axisDetailHtml(axis, data!.quests);
            container.querySelector('[data-action="overview"]')?.addEventListener('click', showOverview);
          });
        });
        container.querySelector('[data-action="close"]')?.addEventListener('click', close);
      };
      showOverview();
      try {
        data = await (await fetchImpl(`${baseUrl}/api/skilltree`)).json();
        showOverview();
      } catch {
        container.innerHTML = '<div class="st-empty">Skilltree nicht erreichbar.</div>';
      }
    },
  });
  el.style.padding = '32px 40px';
  registerOverlay({ key: 'skilltree', label: 'Skilltree', openEvent: 'open-skilltree' });
  return { open };
}

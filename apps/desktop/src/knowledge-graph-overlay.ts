import { authenticatedFetch } from './api-auth';
// Wissensgraph-Overlay — Vollbild-Ansicht des vereinten Graphen (Notizen +
// Entitäten + Fakten). Holt /api/knowledge/graph, rendert ein Knoten-Kanten-Bild
// mit Kind-Filter + Klick-Detail. Öffnen per Cmd/Ctrl+K-Kachel "Wissen" oder
// CustomEvent 'open-knowledge'. Reine Helfer (filterGraph/neighborsOf) sind getestet.

import { createOverlay, registerOverlay } from './overlay';

export type GNode = { id: string; kind: string; label: string; group: string; color: string; size: number };
export type GEdge = { from: string; to: string; kind: string; label?: string };
export type Graph = { nodes: GNode[]; edges: GEdge[] };

const KINDS = ['note', 'entity', 'fact'] as const;
const KIND_LABELS: Record<string, string> = { note: 'Notizen', entity: 'Entitäten', fact: 'Fakten' };
const EDGE_COLORS: Record<string, string> = {
  link: 'rgba(0,229,255,0.35)', relation: 'rgba(245,196,81,0.4)', mention: 'rgba(126,224,129,0.35)',
};

export function filterGraph(data: Graph, kinds: string[]): Graph {
  if (!kinds || kinds.length === 0) return data;
  const set = new Set(kinds);
  const nodes = data.nodes.filter((n) => set.has(n.kind));
  const present = new Set(nodes.map((n) => n.id));
  const edges = data.edges.filter((e) => present.has(e.from) && present.has(e.to));
  return { nodes, edges };
}

export function neighborsOf(nodeId: string, data: Graph): { id: string; kind: string; dir: 'in' | 'out' }[] {
  const out: { id: string; kind: string; dir: 'in' | 'out' }[] = [];
  for (const e of data.edges) {
    if (e.from === nodeId) out.push({ id: e.to, kind: e.kind, dir: 'out' });
    else if (e.to === nodeId) out.push({ id: e.from, kind: e.kind, dir: 'in' });
  }
  return out;
}

// Force-Directed-Layout (Fruchterman-Reingold): Abstoßung aller Knoten +
// Federzug entlang Kanten + Zentrums-Gravitation. Deterministisch (Kreis-Seed,
// kein Zufall) → reproduzierbar & testbar.
export function forceLayout(
  nodes: GNode[], edges: GEdge[], w: number, h: number, iterations = 180,
): Map<string, { x: number; y: number }> {
  const pos = new Map<string, { x: number; y: number }>();
  if (nodes.length === 0) return pos;
  const idx = new Map(nodes.map((nd) => [nd.id, nd]));
  const adj = edges.filter((e) => idx.has(e.from) && idx.has(e.to));
  const deg = new Map(nodes.map((nd) => [nd.id, 0]));
  for (const e of adj) { deg.set(e.from, deg.get(e.from)! + 1); deg.set(e.to, deg.get(e.to)! + 1); }
  const isolated = nodes.filter((nd) => deg.get(nd.id) === 0);
  const connected = nodes.filter((nd) => deg.get(nd.id)! > 0);

  // Unverbundene Knoten: aufgeräumtes Raster im unteren Streifen (statt von der
  // Physik an die Kante gedrückt zu werden).
  const cols = Math.max(1, Math.floor((w - 80) / 220));
  const rows = Math.ceil(isolated.length / cols);
  const areaH = h - (isolated.length ? rows * 30 + 24 : 0);
  const colW = cols > 1 ? (w - 80) / (cols - 1) : 0;
  isolated.forEach((nd, i) => {
    pos.set(nd.id, { x: 40 + (i % cols) * colW, y: areaH + 26 + Math.floor(i / cols) * 30 });
  });

  const cn = connected.length;
  if (cn === 0) return pos;
  const k = Math.sqrt((w * areaH) / cn) * 0.9;
  connected.forEach((nd, i) => {
    const a = (2 * Math.PI * i) / cn;
    pos.set(nd.id, { x: w / 2 + (w / 3) * Math.cos(a), y: areaH / 2 + (areaH / 3) * Math.sin(a) });
  });
  const ci = new Map(connected.map((nd, i) => [nd.id, i]));
  const iters = cn > 120 ? 60 : iterations;
  let temp = w / 10;
  for (let it = 0; it < iters; it++) {
    const disp = connected.map(() => ({ x: 0, y: 0 }));
    for (let i = 0; i < cn; i++) {
      for (let j = i + 1; j < cn; j++) {
        const pi = pos.get(connected[i].id)!, pj = pos.get(connected[j].id)!;
        const dx = pi.x - pj.x, dy = pi.y - pj.y;
        const d = Math.hypot(dx, dy) || 0.01;
        const f = (k * k) / d;
        disp[i].x += (dx / d) * f; disp[i].y += (dy / d) * f;
        disp[j].x -= (dx / d) * f; disp[j].y -= (dy / d) * f;
      }
    }
    for (const e of adj) {
      const i = ci.get(e.from)!, j = ci.get(e.to)!;
      const pi = pos.get(e.from)!, pj = pos.get(e.to)!;
      const dx = pi.x - pj.x, dy = pi.y - pj.y;
      const d = Math.hypot(dx, dy) || 0.01;
      const f = (d * d) / k;
      disp[i].x -= (dx / d) * f; disp[i].y -= (dy / d) * f;
      disp[j].x += (dx / d) * f; disp[j].y += (dy / d) * f;
    }
    for (let i = 0; i < cn; i++) {
      const p = pos.get(connected[i].id)!;
      disp[i].x += (w / 2 - p.x) * 0.012;
      disp[i].y += (areaH / 2 - p.y) * 0.012;
      const dl = Math.hypot(disp[i].x, disp[i].y) || 0.01;
      p.x += (disp[i].x / dl) * Math.min(dl, temp);
      p.y += (disp[i].y / dl) * Math.min(dl, temp);
      p.x = Math.max(20, Math.min(w - 20, p.x));
      p.y = Math.max(20, Math.min(areaH - 20, p.y));
    }
    temp *= 0.97;
  }
  return pos;
}

function graphSvg(data: Graph): string {
  const W = 1000, H = 680;
  const pos = forceLayout(data.nodes, data.edges, W, H);
  const lines = data.edges.map((e) => {
    const a = pos.get(e.from), b = pos.get(e.to);
    if (!a || !b) return '';
    return `<line x1="${a.x.toFixed(1)}" y1="${a.y.toFixed(1)}" x2="${b.x.toFixed(1)}" y2="${b.y.toFixed(1)}" stroke="${EDGE_COLORS[e.kind] ?? '#555'}" stroke-width="1.2"/>`;
  }).join('');
  const circles = data.nodes.map((n) => {
    const p = pos.get(n.id)!;
    const r = Math.max(5, n.size / 2);
    return `<g class="kg-node" data-node-id="${n.id}" style="cursor:pointer">
      <circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="${r}" fill="${n.color}"/>
      <text x="${p.x.toFixed(1)}" y="${(p.y + r + 9).toFixed(1)}" text-anchor="middle" font-size="9" fill="#cfe6e0">${escapeHtml(n.label)}</text>
    </g>`;
  }).join('');
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" height="100%" preserveAspectRatio="xMidYMid meet">${lines}${circles}</svg>`;
}

function escapeHtml(s: string): string {
  return (s || '').replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' })[c] as string);
}

function detailHtml(nodeId: string, view: Graph): string {
  const node = view.nodes.find((n) => n.id === nodeId);
  if (!node) return '';
  const byId = new Map(view.nodes.map((n) => [n.id, n]));
  const neigh = neighborsOf(nodeId, view)
    .map((x) => `<li>${x.dir === 'in' ? '←' : '→'} ${escapeHtml(byId.get(x.id)?.label ?? x.id)} <em style="opacity:.5">(${x.kind})</em></li>`)
    .join('') || '<li style="opacity:.5">keine Verbindungen</li>';
  return `<div style="font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:#7f96a0">${node.kind} · ${escapeHtml(node.group)}</div>
    <h3 style="margin:4px 0 12px">${escapeHtml(node.label)}</h3>
    <div style="font-size:11px;color:#9fb2ba">Verbindungen</div><ul style="margin:6px 0;padding-left:16px;font-size:12px;line-height:1.6">${neigh}</ul>`;
}

export function initKnowledgeGraphOverlay(baseUrl: string, fetchImpl: typeof fetch = authenticatedFetch): { open: () => void } {
  let data: Graph = { nodes: [], edges: [] };
  const active = new Set<string>(KINDS);

  const { open } = createOverlay({
    id: 'knowledge-overlay',
    openEvent: 'open-knowledge',
    render: async (container, { close }) => {
      const renderView = (detail = ''): void => {
        const view = filterGraph(data, [...active]);
        const chips = KINDS.map((k) =>
          `<button class="kg-chip" data-kind="${k}" style="opacity:${active.has(k) ? 1 : 0.4}">${KIND_LABELS[k]}</button>`).join('');
        container.innerHTML =
          `<div style="display:flex;justify-content:space-between;align-items:center;padding:16px 24px">
             <h2 style="margin:0">Wissensgraph <span style="opacity:.5;font-size:13px">${view.nodes.length} Knoten · ${view.edges.length} Kanten</span></h2>
             <div style="display:flex;gap:8px;align-items:center">${chips}<button data-action="close" style="margin-left:12px">✕</button></div>
           </div>
           <div style="display:flex;height:calc(100% - 64px)">
             <div id="kg-canvas" style="flex:1;min-width:0">${graphSvg(view)}</div>
             <div id="kg-detail" style="width:280px;border-left:1px solid #1c3038;padding:16px;overflow:auto">${detail || '<p style="opacity:.5">Klick einen Knoten für Details.</p>'}</div>
           </div>`;
        container.querySelectorAll<HTMLElement>('.kg-chip').forEach((c) =>
          c.addEventListener('click', () => {
            const k = c.dataset.kind!;
            if (active.has(k)) active.delete(k); else active.add(k);
            renderView();
          }));
        container.querySelector('[data-action="close"]')?.addEventListener('click', close);
        container.querySelectorAll<HTMLElement>('.kg-node').forEach((g) =>
          g.addEventListener('click', () => renderView(detailHtml(g.dataset.nodeId!, view))));
      };
      renderView();
      try {
        data = await (await fetchImpl(`${baseUrl}/api/knowledge/graph`)).json();
        renderView();
      } catch {
        container.innerHTML = '<div style="padding:24px">Wissensgraph nicht erreichbar.</div>';
      }
    },
  });
  registerOverlay({ key: 'knowledge', label: 'Wissen', openEvent: 'open-knowledge' });
  return { open };
}

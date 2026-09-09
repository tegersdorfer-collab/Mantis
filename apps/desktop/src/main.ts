import { authenticatedFetch } from './api-auth';
import { getBaseUrl } from './config';
import { checkBackendHealth } from './backend';
import { deriveHudState } from './hud-state';
import { subscribeUiState } from './ui-state-client';
import type { UiEvent, WidgetSlot } from './ui-state-client';
import { startVoiceCapture } from './voice-capture';
import type { VoiceSegmentResult } from './voice-capture';
import { startVoiceCaptureStream } from './voice-capture-stream';
import { initNavOverlay } from './nav-overlay';
import { initHealthOverlay, widgetHtml as healthWidgetHtml } from './health-overlay';
import { initSkilltreeOverlay, widgetHtml as skilltreeWidgetHtml } from './skilltree-overlay';
import { initKnowledgeGraphOverlay } from './knowledge-graph-overlay';
import { initChatInput } from './chat-input';
import { initSettingsPanel } from './settings-panel';
import { subscribeStatus } from './status-stream';
import { renderAlert } from './alert-overlay';
import { appendToLog } from './conversation-log';
import { playTone, TONES } from './sound-feedback';
import { tweenNumber, drawIn, staggerIn } from './motion';
import { startParticleField } from './fx/particle-field';
import { applyPanelChrome } from './fx/panel-chrome';
import { icon } from './fx/icons';
import { latLonToTile, tileGrid } from './fx/map-tiles';
import { fetchRadarFrameTimes, radarTileUrl } from './fx/radar-frames';

const POLL_INTERVAL_MS = 10_000;

// Spiegelt core/ui_state.py::LAYOUT_PRESETS — bewusste Duplikation über die
// Sprachgrenze (siehe Plan-Nicht-Ziele in Phase 3).
const LAYOUT_SLOTS: Record<string, string[]> = {
  single: ['main'],
  split2: ['main', 'side'],
};

function renderHud(): void {
  const ring = document.getElementById('hud-ring')!;
  const label = document.getElementById('hud-label')!;
  const status = document.getElementById('hud-status')!;

  checkBackendHealth(getBaseUrl()).then((health) => {
    const state = deriveHudState(health, new Date());
    ring.style.color = state.ringColor;
    label.textContent = state.label;
    status.textContent = state.statusLine;
  });
}

function renderBars(
  container: HTMLElement,
  title: string,
  items: { value: number | null; tooltip: string }[],
): void {
  const maxVal = Math.max(1, ...items.map((i) => i.value ?? 0));
  const bars = items
    .map((i) => {
      const heightPx = Math.round(((i.value ?? 0) / maxVal) * 120);
      return `<div class="sleep-bar" style="height:${heightPx}px" title="${i.tooltip}"></div>`;
    })
    .join('');
  const isUpdate = container.dataset.rendered === 'true';
  container.innerHTML = `<div class="widget-title">${title}</div><div class="sleep-bars">${bars}</div>`;
  container.dataset.rendered = 'true';
  if (isUpdate) {
    container.classList.add('charge-pulse');
    container.addEventListener('animationend', () => container.classList.remove('charge-pulse'), { once: true });
  }
  applyPanelChrome(container);
  staggerIn(container.querySelectorAll<HTMLElement>('.sleep-bar'), 40);
}

export function renderList(container: HTMLElement, title: string, lines: string[]): void {
  const items = lines.map((l) => `<div class="list-line">${l}</div>`).join('');
  const isUpdate = container.dataset.rendered === 'true';
  container.innerHTML = `<div class="widget-title">${title}</div><div class="widget-list">${items}</div>`;
  container.dataset.rendered = 'true';
  if (isUpdate) {
    container.classList.add('charge-pulse');
    container.addEventListener('animationend', () => container.classList.remove('charge-pulse'), { once: true });
  }
  applyPanelChrome(container);
  staggerIn(container.querySelectorAll<HTMLElement>('.list-line'), 40);
}

function renderGraph(
  container: HTMLElement,
  title: string,
  nodes: { id: number; label: string; color: string; size: number }[],
  edges: { from: number; to: number }[],
): void {
  const SIZE = 260;
  const CENTER = SIZE / 2;
  const RADIUS = SIZE / 2 - 30;
  const positions = new Map<number, { x: number; y: number }>();
  nodes.forEach((n, i) => {
    const angle = (2 * Math.PI * i) / Math.max(1, nodes.length);
    positions.set(n.id, {
      x: CENTER + RADIUS * Math.cos(angle),
      y: CENTER + RADIUS * Math.sin(angle),
    });
  });

  const edgeLines = edges
    .map((e) => {
      const a = positions.get(e.from);
      const b = positions.get(e.to);
      if (!a || !b) return '';
      return `<line x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}" stroke="rgba(0, 229, 255, 0.2)" stroke-width="1" class="graph-edge" />`;
    })
    .join('');

  const nodeCircles = nodes
    .map((n) => {
      const pos = positions.get(n.id)!;
      return `<circle cx="${pos.x}" cy="${pos.y}" r="${Math.max(4, n.size / 2) + 4}" fill="none" stroke="${n.color}" stroke-opacity="0.25" class="graph-node-halo" />
        <circle cx="${pos.x}" cy="${pos.y}" r="${Math.max(4, n.size / 2)}" fill="${n.color}" class="graph-node" />
        <text x="${pos.x}" y="${pos.y + (n.size / 2) + 10}" text-anchor="middle" font-size="8" fill="#e0f7ff">${n.label}</text>`;
    })
    .join('');

  container.innerHTML = `<div class="widget-title">${title}</div>
    <svg viewBox="0 0 ${SIZE} ${SIZE}" width="${SIZE}" height="${SIZE}">${edgeLines}${nodeCircles}</svg>`;
  applyPanelChrome(container);
  container
    .querySelectorAll<SVGLineElement & { getTotalLength(): number }>('.graph-edge')
    .forEach((edge) => drawIn(edge as unknown as SVGPathElement, 500));
}

export function renderGauge(
  container: HTMLElement,
  title: string,
  metrics: { label: string; pct: number; color: string }[],
): void {
  const RADIUS = 34;
  const CIRC = 2 * Math.PI * RADIUS;

  const gauges = metrics
    .map((m, i) => {
      return `
        <div class="gauge">
          <svg viewBox="0 0 80 80" width="80" height="80">
            <circle class="gauge-track" cx="40" cy="40" r="${RADIUS}" />
            <circle
              class="gauge-value"
              cx="40" cy="40" r="${RADIUS}"
              stroke="${m.color}"
              stroke-dasharray="${CIRC}"
              stroke-dashoffset="${CIRC}"
              data-gauge-id="${i}"
            />
          </svg>
          <div class="gauge-label">${m.label}</div>
          <div class="gauge-value-text" data-gauge-text-id="${i}">0</div>
          <div class="gauge-readout"><span>0</span><span>100</span></div>
        </div>`;
    })
    .join('');

  container.innerHTML = `<div class="widget-title">${title}</div><div class="gauge-row">${gauges}</div>`;
  applyPanelChrome(container, { greeble: true });

  metrics.forEach((m, i) => {
    const textEl = container.querySelector(`[data-gauge-text-id="${i}"]`) as HTMLElement | null;
    if (textEl) {
      tweenNumber(textEl, 0, Math.round(m.pct), 400, (n) => `${n}%`);
    }
    const circleEl = container.querySelector(`[data-gauge-id="${i}"]`) as SVGCircleElement | null;
    if (circleEl) {
      const clamped = Math.max(0, Math.min(100, m.pct));
      const offset = CIRC * (1 - clamped / 100);
      circleEl.setAttribute('stroke-dashoffset', String(offset));
    }
  });
}

const MAP_ZOOM = 8;
const MAP_RADIUS = 1; // 1 => 3x3 grid

function renderWeatherMap(container: HTMLElement, payload: any): void {
  const renderToken = Symbol();
  (container as any)._weatherRenderToken = renderToken;

  const lat = payload.lat;
  const lon = payload.lon;
  const city = payload.city ?? '';
  const now = payload.now ?? {};

  if (typeof lat !== 'number' || typeof lon !== 'number') {
    container.innerHTML = `<div class="widget-title">${icon('weather-cloud')} Wetter — ${city}</div><div class="list-line">Keine Standortdaten verfügbar.</div>`;
    applyPanelChrome(container);
    return;
  }

  const center = latLonToTile(lat, lon, MAP_ZOOM);
  const grid = tileGrid(center.x, center.y, MAP_RADIUS);
  const gridSize = MAP_RADIUS * 2 + 1;

  const baseTiles = grid
    .map(
      (t) =>
        `<img class="map-tile" src="https://tile.openstreetmap.org/${MAP_ZOOM}/${t.x}/${t.y}.png" />`,
    )
    .join('');

  container.innerHTML = `
    <div class="widget-title">${icon('weather-cloud')} Wetter — ${city}</div>
    <div class="map-header">${now.temp ?? '–'}°C (gefühlt ${now.feels ?? '–'}°C), ${now.desc ?? ''}</div>
    <div class="map-grid" style="grid-template-columns: repeat(${gridSize}, 1fr);">
      ${baseTiles}
      <div class="map-radar-layer" style="grid-template-columns: repeat(${gridSize}, 1fr);"></div>
    </div>
    <div class="map-attribution">© OpenStreetMap contributors</div>
  `;
  applyPanelChrome(container);

  const radarLayer = container.querySelector('.map-radar-layer');
  if (!radarLayer) return;

  fetchRadarFrameTimes().then((times) => {
    if ((container as any)._weatherRenderToken !== renderToken) return; // Container wurde inzwischen neu gerendert — dieser Promise ist veraltet
    if (times.length === 0) return; // kein Radar-Overlay verfügbar — Basiskarte bleibt sichtbar
    const frames = times.map(
      (time) =>
        `<div class="map-radar-frame">${grid
          .map(
            (t) =>
              `<img class="map-tile map-radar-tile" src="${radarTileUrl(time, MAP_ZOOM, t.x, t.y)}" />`,
          )
          .join('')}</div>`,
    );
    radarLayer.innerHTML = frames.join('');
    const frameEls = radarLayer.querySelectorAll<HTMLElement>('.map-radar-frame');
    let activeIndex = 0;
    frameEls.forEach((el, i) => {
      el.style.display = i === 0 ? 'grid' : 'none';
    });
    const intervalId = setInterval(() => {
      frameEls[activeIndex].style.display = 'none';
      activeIndex = (activeIndex + 1) % frameEls.length;
      frameEls[activeIndex].style.display = 'grid';
    }, 800);
    // Intervall an das Element hängen, damit es beim nächsten renderWidget-Aufruf
    // für dieses Slot (siehe Step 3 der applyUiEvent-Neuzeichnung) gestoppt werden kann.
    (container as any)._radarIntervalId = intervalId;
  });
}

function renderWidget(container: HTMLElement, slot: WidgetSlot): void {
  const p: any = slot.payload;
  if ((container as any)._radarIntervalId) {
    clearInterval((container as any)._radarIntervalId);
    (container as any)._radarIntervalId = null;
  }
  switch (slot.widget) {
    case 'sleep':
      renderBars(
        container,
        `${icon('sleep')} Schlaf — letzte Nächte`,
        (p.nights ?? []).map((n: any) => ({ value: n.hours, tooltip: `${n.date}: ${n.hours ?? '–'}h` })),
      );
      break;
    case 'training':
      renderBars(
        container,
        `${icon('training')} Training — letzte Einheiten`,
        (p.workouts ?? []).map((w: any) => ({
          value: w.duration_min,
          tooltip: `${w.date}: ${w.title} (${w.duration_min ?? '–'}min)`,
        })),
      );
      break;
    case 'tasks':
      renderList(
        container,
        `${icon('tasks')} Offene Aufgaben`,
        (p.tasks ?? []).map(
          (t: any) =>
            `${t.title}<span class="list-inline-bar"><span class="list-inline-bar-fill" style="width:${t.progress_pct}%"></span></span>`,
        ),
      );
      break;
    case 'calendar':
      renderList(
        container,
        `${icon('calendar')} Anstehende Termine`,
        (p.events ?? []).map((e: any) => `${e.title} — ${e.start}`),
      );
      break;
    case 'habits':
      renderList(
        container,
        `${icon('habit')} Gewohnheiten`,
        (p.habits ?? []).map((h: any) => {
          const milestone = 7;
          const pct = Math.min(100, ((h.streak ?? 0) / milestone) * 100);
          const dotColor = h.streak >= milestone ? 'var(--c-ok)' : 'var(--c-idle-dim)';
          const ringDeg = (pct / 100) * 360;
          return `<span class="list-dot-ring" style="background: conic-gradient(var(--c-active) ${ringDeg}deg, transparent ${ringDeg}deg)"><span class="list-dot" style="background:${dotColor}"></span></span>${h.name} (${h.streak}d)`;
        }),
      );
      break;
    case 'nutrition': {
      const kcalGoal = p.kcal_goal ?? p.kcal ?? 1;
      renderGauge(container, `${icon('nutrition')} Ernährung heute — ${p.kcal ?? 0} kcal`, [
        { label: 'Protein', pct: ((p.protein ?? 0) * 4 * 100) / kcalGoal, color: 'var(--c-ok)' },
        { label: 'Carbs', pct: ((p.carbs ?? 0) * 4 * 100) / kcalGoal, color: 'var(--c-active)' },
        { label: 'Fat', pct: ((p.fat ?? 0) * 9 * 100) / kcalGoal, color: 'var(--c-warn)' },
      ]);
      break;
    }
    case 'system':
      renderGauge(container, `${icon('system')} System-Status`, [
        { label: 'CPU', pct: p.cpu_pct ?? 0, color: 'var(--c-active)' },
        { label: 'RAM', pct: p.ram_pct ?? 0, color: 'var(--c-active)' },
        {
          label: 'Ollama',
          pct: p.ollama_ok ? 100 : 0,
          color: p.ollama_ok ? 'var(--c-ok)' : 'var(--c-error)',
        },
      ]);
      break;
    case 'brain':
      renderList(
        container,
        `${icon('brain')} Second Brain — zuletzt bearbeitet`,
        (p.notes ?? []).map((n: any) => `${n.title} (${n.category})`),
      );
      staggerIn(container.querySelectorAll<HTMLElement>('.list-line'));
      break;
    case 'skills':
      renderList(
        container,
        `${icon('skills')} Skill-Factory — ${p.total_tools} Tools gesamt`,
        (p.dynamic_skills ?? []).length > 0
          ? p.dynamic_skills.map((s: string) => `🛠️ ${s}`)
          : ['Noch keine selbst erstellten Skills.'],
      );
      break;
    case 'weather':
      renderWeatherMap(container, p);
      break;
    case 'brain_graph':
      renderGraph(container, 'Second Brain — Graph', p.nodes ?? [], p.edges ?? []);
      break;
    case 'health':
      container.innerHTML = healthWidgetHtml(p);
      break;
    case 'skilltree':
      container.innerHTML = skilltreeWidgetHtml(p);
      break;
    default:
      container.innerHTML = '<div class="widget-title">unbekannt</div>';
  }
}

function applyUiEvent(evt: UiEvent): void {
  const hud = document.getElementById('hud')!;
  const widgetArea = document.getElementById('widget-area')!;

  if (!evt.layout) {
    hud.style.display = 'flex';
    widgetArea.style.display = 'none';
    widgetArea.innerHTML = '';
    return;
  }

  hud.style.display = 'none';
  widgetArea.style.display = 'grid';
  widgetArea.className = `layout-${evt.layout}`;

  const slotNames = LAYOUT_SLOTS[evt.layout] ?? ['main'];
  widgetArea.innerHTML = slotNames
    .map((name) => `<div class="widget-slot" data-slot="${name}"></div>`)
    .join('');

  for (const name of slotNames) {
    const slotEl = widgetArea.querySelector(`[data-slot="${name}"]`) as HTMLElement;
    const slot = evt.slots[name];
    if (slot) {
      renderWidget(slotEl, slot);
      playTone(TONES.widget);
    } else {
      slotEl.innerHTML = '<div class="widget-title">leer</div>';
    }
  }
}

renderHud();
setInterval(renderHud, POLL_INTERVAL_MS);
subscribeUiState(getBaseUrl(), applyUiEvent);

function renderVoiceStatus(result: VoiceSegmentResult): void {
  const el = document.getElementById('voice-status');
  if (!el) return;
  const marker = result.addressed ? '🎙️ an Mantis' : '🎙️ ignoriert';
  let text = `${marker}: "${result.text}"`;
  if (result.addressed && result.reply) {
    text += `\n🔊 Mantis: "${result.reply}"`;
  }
  el.textContent = text;

  if (result.addressed) {
    appendToLog({ speaker: 'user', text: result.text });
    if (result.reply) appendToLog({ speaker: 'mantis', text: result.reply });
    playTone(TONES.addressed);
  }
}

export async function initVoiceCapture(
  baseUrl: string,
  onSegment: (r: VoiceSegmentResult) => void,
): Promise<() => void> {
  try {
    const res = await authenticatedFetch(`${baseUrl}/api/voice/stream-mode`);
    const { mode } = await res.json();
    return mode === 'websocket'
      ? startVoiceCaptureStream(baseUrl, onSegment)
      : startVoiceCapture(baseUrl, onSegment);
  } catch {
    return startVoiceCapture(baseUrl, onSegment);
  }
}

initVoiceCapture(getBaseUrl(), renderVoiceStatus);
// Overlays zuerst — sie registrieren sich, bevor nav-overlay die Registry liest.
initHealthOverlay(getBaseUrl());
initKnowledgeGraphOverlay(getBaseUrl());
initSkilltreeOverlay(getBaseUrl());
initNavOverlay(getBaseUrl());

export function triggerSpeakingState(durationMs = 2000): void {
  const ring = document.getElementById('hud-ring');
  if (!ring) return;
  ring.classList.add('speaking');
  setTimeout(() => ring.classList.remove('speaking'), durationMs);
}

function renderChatReply(reply: string, userText: string): void {
  triggerSpeakingState();
  const el = document.getElementById('chat-status');
  if (!el) return;
  el.innerHTML = icon('chat');
  el.appendChild(document.createTextNode(` Mantis: "${reply}"`));

  appendToLog({ speaker: 'user', text: userText });
  appendToLog({ speaker: 'mantis', text: reply });
}

initChatInput(getBaseUrl(), renderChatReply);
initSettingsPanel();

function initHudChrome(): void {
  const particleCanvas = document.getElementById('hud-particles') as HTMLCanvasElement | null;
  if (particleCanvas) {
    startParticleField(particleCanvas, { density: 40 });
  }

  const bezelTicks = document.getElementById('hud-bezel-ticks');
  if (bezelTicks) {
    const TICK_COUNT = 36;
    const ticks = Array.from({ length: TICK_COUNT }, (_, i) => {
      const angle = (i / TICK_COUNT) * 2 * Math.PI;
      const cx = 80 + 74 * Math.cos(angle);
      const cy = 80 + 74 * Math.sin(angle);
      const cx2 = 80 + 68 * Math.cos(angle);
      const cy2 = 80 + 68 * Math.sin(angle);
      return `<line x1="${cx}" y1="${cy}" x2="${cx2}" y2="${cy2}" />`;
    }).join('');
    bezelTicks.innerHTML = ticks;
  }

  const infoPanel = document.getElementById('hud-info-panel');
  if (infoPanel) {
    applyPanelChrome(infoPanel);
  }
}

initHudChrome();
subscribeStatus(getBaseUrl(), (evt) => {
  renderAlert(evt);
  if (evt.type === 'autopilot' || evt.type === 'tool_failure') playTone(TONES.alert);
});

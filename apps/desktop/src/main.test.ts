import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderGauge, renderList, triggerSpeakingState, renderHud } from './main';

describe('renderGauge', () => {
  it('rendert ein SVG mit einem Kreis pro Metrik', () => {
    const container = document.createElement('div');
    renderGauge(container, 'System-Status', [
      { label: 'CPU', pct: 45, color: '#00e5ff' },
      { label: 'RAM', pct: 60, color: '#00e5ff' },
    ]);
    expect(container.querySelectorAll('circle.gauge-value').length).toBe(2);
    expect(container.textContent).toContain('System-Status');
  });

  it('rendert 0% als leeren Kreis ohne Fehler', () => {
    const container = document.createElement('div');
    expect(() =>
      renderGauge(container, 'System-Status', [{ label: 'CPU', pct: 0, color: '#00e5ff' }]),
    ).not.toThrow();
  });

  it('setzt stroke-dashoffset nach dem Rendern auf den Zielwert (nicht leer)', () => {
    const container = document.createElement('div');
    renderGauge(container, 'System-Status', [{ label: 'CPU', pct: 50, color: '#00e5ff' }]);
    const circle = container.querySelector('[data-gauge-id="0"]') as SVGCircleElement;
    const dashArray = Number(circle.getAttribute('stroke-dasharray'));
    const dashOffset = Number(circle.getAttribute('stroke-dashoffset'));
    // Bei 50% sollte der Offset ungefähr die Hälfte des Umfangs sein, nicht der volle Umfang.
    expect(dashOffset).toBeCloseTo(dashArray * 0.5, 1);
  });
});

describe('renderList charge-pulse gating', () => {
  it('fügt beim ersten Render KEINE charge-pulse Klasse hinzu', () => {
    const container = document.createElement('div');
    renderList(container, 'Titel', ['Zeile 1']);
    expect(container.classList.contains('charge-pulse')).toBe(false);
    expect(container.dataset.rendered).toBe('true');
  });

  it('fügt bei einem erneuten Render die charge-pulse Klasse hinzu', () => {
    const container = document.createElement('div');
    renderList(container, 'Titel', ['Zeile 1']);
    renderList(container, 'Titel', ['Zeile 2']);
    expect(container.classList.contains('charge-pulse')).toBe(true);
  });
});

describe('triggerSpeakingState', () => {
  beforeEach(() => {
    document.body.innerHTML = '<div id="hud-ring"></div>';
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('fügt die speaking-Klasse sofort hinzu', () => {
    triggerSpeakingState();
    expect(document.getElementById('hud-ring')!.classList.contains('speaking')).toBe(true);
  });

  it('entfernt die speaking-Klasse nach der angegebenen Dauer', () => {
    triggerSpeakingState(2000);
    vi.advanceTimersByTime(2000);
    expect(document.getElementById('hud-ring')!.classList.contains('speaking')).toBe(false);
  });

  it('tut nichts wenn #hud-ring nicht existiert', () => {
    document.body.innerHTML = '';
    expect(() => triggerSpeakingState()).not.toThrow();
  });
});


describe('renderHud', () => {
  afterEach(() => {
    document.body.innerHTML = '';
  });

  it('läuft durch wenn die HUD-Elemente fehlen', async () => {
    document.body.innerHTML = '';
    // Vorher standen hier non-null-Assertions: der Zugriff schlug erst im .then()
    // fehl, also als unbehandelte Promise-Rejection. Deshalb muss das PROMISE
    // geprüft werden — ein rein synchrones expect(...).not.toThrow() wäre hier
    // auch mit dem defekten Stand grün gewesen.
    await expect(renderHud()).resolves.toBeUndefined();
  });

  it('läuft durch wenn nur ein Teil der HUD-Elemente da ist', async () => {
    document.body.innerHTML = '<div id="hud-ring"></div>';  // label + status fehlen
    await expect(renderHud()).resolves.toBeUndefined();
  });
});

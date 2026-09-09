import { describe, it, expect, vi, beforeEach } from 'vitest';
import { initSettingsPanel } from './settings-panel';
import * as config from './config';

describe('settings-panel', () => {
  beforeEach(() => {
    document.body.innerHTML = '';
    localStorage.clear();
    vi.restoreAllMocks();
  });

  it('togglet Sichtbarkeit bei Cmd+,', () => {
    initSettingsPanel();
    const panel = document.getElementById('settings-panel')!;
    expect(panel.classList.contains('visible')).toBe(false);
    document.dispatchEvent(new KeyboardEvent('keydown', { key: ',', metaKey: true }));
    expect(panel.classList.contains('visible')).toBe(true);
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    expect(panel.classList.contains('visible')).toBe(false);
  });

  it('zeigt die aktuelle Basis-URL im Eingabefeld', () => {
    config.setBaseUrl('http://100.1.2.3:7779');
    initSettingsPanel();
    document.dispatchEvent(new KeyboardEvent('keydown', { key: ',', metaKey: true }));
    const input = document.getElementById('settings-base-url') as HTMLInputElement;
    expect(input.value).toBe('http://100.1.2.3:7779');
  });

  it('speichert die neue Basis-URL bei Submit und schließt das Panel', () => {
    initSettingsPanel();
    document.dispatchEvent(new KeyboardEvent('keydown', { key: ',', metaKey: true }));
    const input = document.getElementById('settings-base-url') as HTMLInputElement;
    const form = document.getElementById('settings-form') as HTMLFormElement;
    input.value = 'http://100.9.9.9:7779';
    form.dispatchEvent(new Event('submit', { cancelable: true }));
    expect(config.getBaseUrl()).toBe('http://100.9.9.9:7779');
    const panel = document.getElementById('settings-panel')!;
    expect(panel.classList.contains('visible')).toBe(false);
  });

  it('vergisst das angezeigte Token beim Wechsel der Adresse', () => {
    config.setApiToken('old-server-key');
    initSettingsPanel();
    document.dispatchEvent(new KeyboardEvent('keydown', {key: ',', metaKey: true}));
    const url = document.getElementById('settings-base-url') as HTMLInputElement;
    const token = document.getElementById('settings-api-token') as HTMLInputElement;
    expect(token.type).toBe('password');
    expect(token.value).toBe('old-server-key');
    url.value = 'http://100.64.0.2:7779';
    url.dispatchEvent(new Event('input'));
    expect(token.value).toBe('');
    token.value = 'new-server-key';
    document.getElementById('settings-form')!.dispatchEvent(new Event('submit', {cancelable: true}));
    expect(config.getApiToken()).toBe('new-server-key');
  });

  it('speichert Adresse und Token über einen sichtbaren Submit-Button', () => {
    initSettingsPanel();
    document.dispatchEvent(new KeyboardEvent('keydown', {key: ',', metaKey: true}));
    (document.getElementById('settings-base-url') as HTMLInputElement).value = 'http://100.64.0.2:7779';
    (document.getElementById('settings-api-token') as HTMLInputElement).value = 'test-key';
    const button = document.querySelector<HTMLButtonElement>('#settings-form button[type="submit"]');
    expect(button).not.toBeNull();
    button!.click();
    expect(config.getApiToken()).toBe('test-key');
    expect(document.getElementById('settings-panel')!.classList.contains('visible')).toBe(false);
  });
});

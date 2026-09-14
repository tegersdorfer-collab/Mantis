import { getBaseUrl, setBaseUrl, getApiToken, setApiToken } from './config';

export function initSettingsPanel(): void {
  const panel = document.createElement('div');
  panel.id = 'settings-panel';
  panel.innerHTML = `
    <form id="settings-form">
      <label for="settings-base-url">Backend-Adresse (z.B. Tailscale-Hostname des Macs für Remote-Clients)</label>
      <input type="text" id="settings-base-url" autocomplete="off" /><span class="settings-status-dot"></span>
      <label for="settings-api-token">Zugangsschlüssel (DASHBOARD_TOKEN)</label>
      <input type="password" id="settings-api-token" autocomplete="off" />
      <button type="submit">Speichern</button>
      <div class="settings-hint">Enter zum Speichern &amp; Neuladen · Escape zum Abbrechen</div>
    </form>
  `;
  document.body.appendChild(panel);

  const form = panel.querySelector<HTMLFormElement>('#settings-form')!;
  const input = panel.querySelector<HTMLInputElement>('#settings-base-url')!;
  const tokenInput = panel.querySelector<HTMLInputElement>('#settings-api-token')!;
  input.addEventListener('input', () => { tokenInput.value = ''; });

  function close(): void {
    panel.classList.remove('visible');
  }

  function open(): void {
    input.value = getBaseUrl();
    tokenInput.value = getApiToken();
    panel.classList.add('visible');
    input.focus();
    input.select();
  }

  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const url = input.value.trim();
    if (url) {
      setBaseUrl(url);
      setApiToken(tokenInput.value);
      close();
      location.reload();
    }
  });

  document.addEventListener('keydown', (e) => {
    const isToggle = (e.metaKey || e.ctrlKey) && e.key === ',';
    if (isToggle) {
      e.preventDefault();
      panel.classList.contains('visible') ? close() : open();
    } else if (e.key === 'Escape' && panel.classList.contains('visible')) {
      close();
    }
  });
}

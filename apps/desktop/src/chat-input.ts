import { authenticatedFetch } from './api-auth';
export function initChatInput(
  baseUrl: string,
  onReply: (reply: string, userText: string) => void,
): void {
  const wrap = document.createElement('div');
  wrap.id = 'chat-input-wrap';
  wrap.innerHTML = `<input type="text" id="chat-input" placeholder="Nachricht an Mantis …" autocomplete="off" />`;
  document.body.appendChild(wrap);

  const input = wrap.querySelector<HTMLInputElement>('#chat-input')!;

  input.addEventListener('keydown', async (e) => {
    if (e.key !== 'Enter') return;
    const text = input.value.trim();
    if (!text) return;

    input.value = '';
    input.disabled = true;
    try {
      const res = await authenticatedFetch(`${baseUrl}/api/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text }),
      });
      if (res.status === 401 || res.status === 403) {
        onReply('Zugangsschlüssel prüfen: Einstellungen mit Cmd/Ctrl+, öffnen.', text);
        return;
      }
      if (res.status >= 400) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      onReply(data.response ?? '', text);
    } catch {
      onReply('Fehler beim Senden — Verbindung zu Mantis geprüft?', text);
    } finally {
      input.disabled = false;
      input.focus();
    }
  });
}

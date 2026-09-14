import { describe, it, expect, vi, beforeEach } from 'vitest';
import { initChatInput } from './chat-input';

describe('chat-input', () => {
  beforeEach(() => {
    document.body.innerHTML = '';
    vi.restoreAllMocks();
  });

  it('rendert ein Eingabefeld', () => {
    initChatInput('http://x', () => {});
    const input = document.getElementById('chat-input') as HTMLInputElement;
    expect(input).toBeTruthy();
    expect(input.tagName).toBe('INPUT');
  });

  it('sendet Text bei Enter und ruft onReply mit der Antwort auf', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ json: async () => ({ response: 'Alles klar.' }) });
    vi.stubGlobal('fetch', fetchMock);
    const onReply = vi.fn();
    initChatInput('http://x', onReply);
    const input = document.getElementById('chat-input') as HTMLInputElement;
    input.value = 'Wie war mein Schlaf?';
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter' }));
    await Promise.resolve();
    await Promise.resolve();
    expect(fetchMock).toHaveBeenCalledWith(
      'http://x/api/chat',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ text: 'Wie war mein Schlaf?' }),
      }),
    );
    expect(onReply).toHaveBeenCalledWith('Alles klar.', 'Wie war mein Schlaf?');
    expect(input.value).toBe('');
  });

  it('ignoriert leere Eingaben', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    initChatInput('http://x', () => {});
    const input = document.getElementById('chat-input') as HTMLInputElement;
    input.value = '   ';
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter' }));
    await Promise.resolve();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('erklärt einen abgewiesenen Zugangsschlüssel statt einer leeren Antwort', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{"error":"Authentication required"}', {status: 401})));
    const onReply = vi.fn();
    initChatInput('http://localhost:7779', onReply);
    const input = document.getElementById('chat-input') as HTMLInputElement;
    input.value = 'Hallo';
    input.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter'}));
    await vi.waitFor(() => expect(onReply).toHaveBeenCalledWith(expect.stringContaining('Zugangsschlüssel'), 'Hallo'));
    expect(input.disabled).toBe(false);
  });

  it('meldet Fehler bei Netzwerkproblemen statt zu crashen', async () => {
    const fetchMock = vi.fn().mockRejectedValue(new Error('network down'));
    vi.stubGlobal('fetch', fetchMock);
    const onReply = vi.fn();
    initChatInput('http://x', onReply);
    const input = document.getElementById('chat-input') as HTMLInputElement;
    input.value = 'Test';
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter' }));
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
    expect(onReply).toHaveBeenCalledWith(expect.stringContaining('Fehler'), 'Test');
  });
});

/** Begrenzte Browser-Brücke zwischen Mantis und God's Eye View. */
const ACTIONS = new Set([
  'fly_to_location', 'set_layer_visibility', 'set_visual_style', 'zoom_to_globe',
]);

export function installBridge({ frame = window, allowedOrigins, runAction }) {
  const referrer = frame.document?.referrer;
  let parentOrigin;
  try { parentOrigin = new URL(referrer).origin; } catch { return () => {}; }
  if (!allowedOrigins.includes(parentOrigin) || frame.parent === frame) return () => {};

  const onMessage = async (event) => {
    if (event.source !== frame.parent || event.origin !== parentOrigin) return;
    const command = event.data;
    if (command?.source !== 'mantis' || !/^[a-f0-9]{32}$/.test(command.id)
        || !ACTIONS.has(command.action) || !command.args
        || typeof command.args !== 'object' || Array.isArray(command.args)) return;
    let result;
    try {
      const outcome = await runAction(command.action, command.args);
      result = outcome?.ok === true
        ? { ok: true }
        : { ok: false, error: String(outcome?.error || 'Aktion fehlgeschlagen').slice(0, 200) };
    } catch (error) {
      result = { ok: false, error: String(error?.message || error).slice(0, 200) };
    }
    frame.parent.postMessage({ source: 'gev', id: command.id, result }, parentOrigin);
  };
  frame.addEventListener('message', onMessage);
  frame.parent.postMessage({ source: 'gev', ready: true }, parentOrigin);
  return () => frame.removeEventListener('message', onMessage);
}

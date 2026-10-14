"use strict";

const screen = document.querySelector("#screen");
const timerHost = document.querySelector("#timer-host");
const syncHost = document.querySelector("#sync-host");
const noticeHost = document.querySelector("#notice-host");

let screenMode = "loading";
let appState = null;
let selectedExercise = null;
let editingSet = null;
let finishSummary = null;
let notice = "";
let loginError = "";
let loadError = "";
let histories = {};
let historyRequestCounter = 0;
const historyRequestIds = new Map();
let drafts = new Map();
let queue = readStored("gym_queue", []);
if (!Array.isArray(queue)) queue = [];
queue = queue.filter((item) => item && typeof item.client_id === "string"
  && typeof item.exercise === "string" && Number.isFinite(Number(item.weight_kg))
  && Number.isInteger(Number(item.reps)));
let flushPromise = null;
const activeSetSends = new Set();
const unconfirmedSetIds = new Set(queue.map((item) => item.client_id));
let timerEnd = Number(readStored("gym_timer_end", 0)) || 0;
let timerInterval = null;
let clientCounter = 0;

function readStored(key, fallback) {
  try {
    const stored = localStorage.getItem(key);
    return stored === null ? fallback : JSON.parse(stored);
  } catch (_error) {
    return fallback;
  }
}

function writeStored(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
    return true;
  } catch (_error) {
    return false;
  }
}

function trackSetSend(promise) {
  activeSetSends.add(promise);
  promise.then(
    () => activeSetSends.delete(promise),
    () => activeSetSends.delete(promise),
  );
  return promise;
}

async function waitForSetSends() {
  while (activeSetSends.size) {
    await Promise.allSettled([...activeSetSends]);
  }
}

function removeStored(key) {
  try {
    localStorage.removeItem(key);
  } catch (_error) {
    // Private browsing or storage limits must not block a set from being logged.
  }
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[character]);
}

function parseDecimal(value) {
  const normalized = String(value ?? "").trim().replace(/\s/g, "").replace(",", ".");
  return normalized ? Number(normalized) : Number.NaN;
}

function roundWeight(value) {
  return Math.round(Number(value) * 4) / 4;
}

const WEIGHT_EPSILON = 1e-6;

function formatWeight(value) {
  if (value === null || value === undefined || value === "") return "–";
  return Number(value).toLocaleString("de-DE", { maximumFractionDigits: 2 });
}

function inputWeight(value) {
  return value === null || value === undefined ? "" : String(value).replace(".", ",");
}

function formatDate(value) {
  if (!value) return "";
  const date = new Date(`${String(value).slice(0, 10)}T12:00:00`);
  return Number.isNaN(date.getTime())
    ? String(value)
    : new Intl.DateTimeFormat("de-DE", { day: "numeric", month: "short" }).format(date);
}

function startedAtLabel() {
  if (!appState?.session?.started_at) return "Kein Training offen";
  const started = new Date(appState.session.started_at);
  if (Number.isNaN(started.getTime())) return "Training läuft";
  const time = new Intl.DateTimeFormat("de-DE", { hour: "2-digit", minute: "2-digit" }).format(started);
  return `Training läuft seit ${time}`;
}

function currentExercise() {
  return appState?.exercises?.find((exercise) => exercise.name === selectedExercise) ?? null;
}

function makeDraft(weight, reps, autoFilled) {
  const weightValue = inputWeight(weight);
  const repsValue = String(reps ?? "");
  return {
    client_id: createClientId(),
    weight_kg: weightValue,
    reps: repsValue,
    autoFilled: autoFilled ? { weight_kg: weightValue, reps: repsValue } : null,
    userChanged: { weight_kg: false, reps: false },
  };
}

function draftsFor(exercise) {
  if (!drafts.has(exercise.name)) {
    const remaining = (exercise.prefill ?? []).slice((exercise.logged ?? []).length);
    const initial = remaining.map((set) => makeDraft(set.weight_kg, set.reps, true));
    if (!initial.length && !(exercise.logged ?? []).length && !(exercise.prefill ?? []).length) {
      initial.push(makeDraft("", "", false));
    }
    drafts.set(exercise.name, initial);
  }
  return drafts.get(exercise.name);
}

function updateDraftField(exercise, clientId, field, value) {
  const list = draftsFor(exercise);
  const draftIndex = list.findIndex((entry) => entry.client_id === clientId);
  const draft = list[draftIndex];
  if (!draft) return;
  const previous = draft[field];
  if (previous === value) return;
  draft[field] = value;
  draft.userChanged[field] = true;

  for (let nextIndex = draftIndex + 1; nextIndex < list.length; nextIndex += 1) {
    const next = list[nextIndex];
    if (next.userChanged[field] || next.autoFilled?.[field] !== previous || next[field] !== previous) continue;
    next[field] = value;
    next.autoFilled[field] = value;
    for (const input of screen.querySelectorAll("[data-draft-input]")) {
      if (input.dataset.clientId === next.client_id && input.dataset.field === field) input.value = value;
    }
  }
}

function removeDraft(exercise, clientId) {
  const list = draftsFor(exercise);
  drafts.set(exercise.name, list.filter((draft) => draft.client_id !== clientId));
}

function renderBrand() {
  return `<div class="brand-row">
    <span class="brand-mark"><img src="/icon.png" alt=""><span>Mantis Gym</span></span>
    <span>Grundübungen</span>
  </div>`;
}

function renderHome() {
  const list = (appState?.exercises ?? []).map((exercise) => {
    const done = exercise.logged.length;
    const total = Math.max(1, exercise.prefill.length, done);
    return `<button class="exercise-line" type="button" data-action="open-exercise"
      data-exercise="${escapeHtml(exercise.name)}">
      <span class="exercise-copy">
        <strong>${escapeHtml(exercise.name)}</strong>
        <span>${exercise.rest_s} Sek. Pause</span>
      </span>
      <span class="exercise-progress">${done}/${total} Sätze</span>
    </button>`;
  }).join("");
  const endButton = appState?.session
    ? '<button class="button button-danger" type="button" data-action="finish">Training beenden</button>'
    : "";

  return `${renderBrand()}
    <h1>Training</h1>
    <p class="subhead">Drei Übungen. Satz für Satz.</p>
    <section class="session-strip" aria-label="Trainingsstatus">
      <span class="session-state"><strong>${escapeHtml(startedAtLabel())}</strong>
        <span>${appState?.session ? "Deine Session ist offen." : "Der erste gespeicherte Satz startet sie."}</span>
      </span>
      ${endButton}
    </section>
    <section class="exercise-list" aria-label="Übungen">${list}</section>`;
}

function measureControl(field, value, mode, index, unit, clientId = "") {
  const inputMode = field === "weight_kg" ? "decimal" : "numeric";
  const ariaLabel = field === "weight_kg" ? "Gewicht in Kilogramm" : "Wiederholungen";
  const action = mode === "edit" ? "adjust-edit" : "adjust-draft";
  const fieldName = field === "weight_kg" ? "weight_kg" : "reps";
  const renderedValue = fieldName === "weight_kg" ? inputWeight(value) : String(value ?? "");
  const inputAttribute = mode === "edit"
    ? `data-edit-input data-field="${fieldName}"`
    : `data-draft-input data-client-id="${escapeHtml(clientId)}" data-index="${index}" data-field="${fieldName}"`;
  const clientAttribute = mode === "edit" ? "" : `data-client-id="${escapeHtml(clientId)}"`;
  return `<div class="measure-cell">
    <div class="measure-control">
      <button type="button" aria-label="${fieldName === "weight_kg" ? "Gewicht" : "Wiederholungen"} verringern"
        data-action="${action}" data-field="${fieldName}" data-index="${index}" ${clientAttribute} data-step="-1">−</button>
      <input ${inputAttribute} inputmode="${inputMode}" value="${escapeHtml(renderedValue)}"
        aria-label="${ariaLabel}" autocomplete="off">
      <button type="button" aria-label="${fieldName === "weight_kg" ? "Gewicht" : "Wiederholungen"} erhöhen"
        data-action="${action}" data-field="${fieldName}" data-index="${index}" ${clientAttribute} data-step="1">+</button>
    </div>
    <span class="measure-unit">${unit}</span>
  </div>`;
}

function renderLoggedRow(set) {
  const pending = String(set.id).startsWith("pending:");
  const label = pending ? "Wartet auf Sync" : "Satz bearbeiten";
  return `<div class="set-row set-row-complete">
    <span class="set-number">${escapeHtml(set.set_index)}</span>
    <button class="logged-set" type="button" data-action="edit-set" data-set-id="${escapeHtml(set.id)}"
      aria-label="${label}: ${formatWeight(set.weight_kg)} kg, ${escapeHtml(set.reps)} Wiederholungen">
      <strong>${formatWeight(set.weight_kg)} kg</strong><span>× ${escapeHtml(set.reps)}</span>
      ${pending ? '<span class="pending-label">Wartet auf Sync</span>' : ""}
    </button>
    <span aria-hidden="true" class="set-check">✓</span>
  </div>`;
}

function renderDraftRow(draft, index, setNumber) {
  return `<div class="set-row">
    <span class="set-number">${setNumber}</span>
    ${measureControl("weight_kg", draft.weight_kg, "draft", index, "kg", draft.client_id)}
    ${measureControl("reps", draft.reps, "draft", index, "Wdh.", draft.client_id)}
    <button class="check-button" type="button" data-action="log-draft" data-client-id="${escapeHtml(draft.client_id)}" data-index="${index}"
      aria-label="Satz ${setNumber} speichern">✓</button>
  </div>`;
}

function renderEditPanel(set) {
  if (!editingSet || String(set.id) !== String(editingSet.id)) return "";
  return `<section class="edit-panel" aria-label="Satz bearbeiten">
    <h2>Satz bearbeiten</h2>
    <div class="edit-controls">
      ${measureControl("weight_kg", editingSet.weight_kg, "edit", "", "kg")}
      ${measureControl("reps", editingSet.reps, "edit", "", "Wdh.")}
    </div>
    <div class="edit-actions">
      <button class="button button-primary" type="button" data-action="save-edit">Änderung speichern</button>
      <button class="button button-danger" type="button" data-action="delete-edit">Satz löschen</button>
      <button class="button" type="button" data-action="cancel-edit">Abbrechen</button>
    </div>
  </section>`;
}

function renderHistory(exercise) {
  const items = histories[exercise.name];
  if (items === undefined) return '<p class="quiet-copy">Verlauf wird geladen …</p>';
  if (items.error) return `<p class="quiet-copy">${escapeHtml(items.error)}</p>`;
  if (!items.length) return '<p class="quiet-copy">Noch keine früheren Trainings.</p>';
  return `<div class="history-list">${items.map((item) => {
    const sets = item.sets.map((set) => `${formatWeight(set.weight_kg)}×${set.reps}`).join(" · ");
    return `<div class="history-line"><time>${escapeHtml(formatDate(item.date))}</time>
      <span>${escapeHtml(sets)}</span></div>`;
  }).join("")}</div>`;
}

function renderExercise() {
  const exercise = currentExercise();
  if (!exercise) return renderHome();
  const openDrafts = draftsFor(exercise);
  const completedLabel = exercise.logged.length === 1 ? "Satz erledigt" : "Sätze erledigt";
  const logged = exercise.logged.map(renderLoggedRow).join("");
  const draftRows = openDrafts.map((draft, index) =>
    renderDraftRow(draft, index, exercise.logged.length + index + 1)
  ).join("");
  const editPanel = exercise.logged.map((set) => renderEditPanel(set)).join("");
  return `${renderBrand()}
    <button class="back-button" type="button" data-action="back">‹ Übersicht</button>
    <h1>${escapeHtml(exercise.name)}</h1>
    <p class="subhead">${exercise.logged.length} ${completedLabel} · ${exercise.rest_s} Sek. Pause</p>
    <div class="set-heading" aria-hidden="true"><span>Nr.</span><span>Gewicht</span><span>Wiederholungen</span><span></span></div>
    <section class="set-list" aria-label="Sätze">${logged}${draftRows}</section>
    ${editPanel}
    <button class="button add-set-row" type="button" data-action="add-draft">+ Satz</button>
    <section aria-labelledby="history-title">
      <h2 class="section-label" id="history-title">Letzte Trainings</h2>
      ${renderHistory(exercise)}
    </section>`;
}

function renderLogin() {
  return `${renderBrand()}
    <section class="auth-panel">
      <h1>Dein Training</h1>
      <p class="quiet-copy">Melde dich mit deinem Mantis-Zugangsschlüssel an.</p>
      <form id="login-form">
        <label for="access-token">Zugangsschlüssel</label>
        <input id="access-token" name="token" type="password" autocomplete="off"
          autocapitalize="off" spellcheck="false" required>
        <button class="button button-primary" type="submit">Anmelden</button>
        <p class="inline-error">${escapeHtml(loginError)}</p>
      </form>
    </section>`;
}

function renderSummary() {
  if (!finishSummary) return renderHome();
  const setLabel = finishSummary.sets === 1 ? "Satz" : "Sätze";
  return `${renderBrand()}
    <section class="summary-panel">
      <h1>Training beendet</h1>
      <p class="quiet-copy">Gute Arbeit. Deine Session ist gespeichert.</p>
      <div class="summary-line"><span>Dauer</span><strong>${finishSummary.duration_min} Min.</strong></div>
      <div class="summary-line"><span>${setLabel}</span><strong>${finishSummary.sets}</strong></div>
      <div class="summary-line"><span>Volumen</span><strong>${Number(finishSummary.volume_kg).toLocaleString("de-DE", { maximumFractionDigits: 2 })} kg</strong></div>
      <div class="summary-actions"><button class="button button-primary" type="button" data-action="back-home">Zur Übersicht</button></div>
    </section>`;
}

function render() {
  if (screenMode === "login") screen.innerHTML = renderLogin();
  else if (screenMode === "loading") screen.innerHTML = `${renderBrand()}<div class="loading-panel">Training wird geladen …</div>`;
  else if (screenMode === "error") screen.innerHTML = `${renderBrand()}
    <section class="error-panel"><h1>Verbindung unterbrochen</h1>
      <p class="quiet-copy">${escapeHtml(loadError)} Deine wartenden Sätze bleiben auf diesem Gerät.</p>
      <button class="button button-primary" type="button" data-action="retry">Erneut versuchen</button>
    </section>`;
  else if (screenMode === "summary") screen.innerHTML = renderSummary();
  else if (selectedExercise) screen.innerHTML = renderExercise();
  else screen.innerHTML = renderHome();

  renderSyncBanner();
  renderNotice();
  renderTimer();
}

function renderSyncBanner() {
  const count = queue.length;
  syncHost.innerHTML = count
    ? `<div class="sync-banner">${count} ${count === 1 ? "Satz ist" : "Sätze sind"} noch nicht synchronisiert.</div>`
    : "";
}

function renderNotice() {
  noticeHost.innerHTML = notice ? `<div class="notice-banner">${escapeHtml(notice)}</div>` : "";
}

function timerMarkup(seconds) {
  const minutes = String(Math.floor(seconds / 60)).padStart(2, "0");
  const remainder = String(seconds % 60).padStart(2, "0");
  return `<div class="timer-dock" role="group" aria-label="Pausentimer">
    <span class="timer-copy"><span>Pause</span><strong id="timer-value">${minutes}:${remainder}</strong></span>
    <button class="small-button" type="button" data-action="timer-minus" aria-label="30 Sekunden abziehen">−30</button>
    <button class="small-button" type="button" data-action="timer-plus" aria-label="30 Sekunden hinzufügen">+30</button>
    <button class="small-button timer-skip" type="button" data-action="timer-skip">Überspringen</button>
  </div>`;
}

function renderTimer() {
  document.body.classList.toggle("timer-active", Boolean(timerEnd));
  if (!timerEnd) {
    timerHost.innerHTML = "";
    return;
  }
  const remaining = Math.max(0, Math.ceil((timerEnd - Date.now()) / 1000));
  timerHost.innerHTML = timerMarkup(remaining);
  if (remaining === 0) completeTimer();
}

function completeTimer() {
  if (!timerEnd) return;
  timerEnd = 0;
  removeStored("gym_timer_end");
  document.body.classList.remove("timer-active");
  if (timerInterval !== null) {
    clearInterval(timerInterval);
    timerInterval = null;
  }
  navigator.vibrate?.([300, 150, 300]);
  playTimerTone();
  timerHost.innerHTML = "";
}

function playTimerTone() {
  const AudioContextType = window.AudioContext || window.webkitAudioContext;
  if (!AudioContextType) return;
  try {
    const context = new AudioContextType();
    const oscillator = context.createOscillator();
    const gain = context.createGain();
    oscillator.frequency.value = 660;
    gain.gain.setValueAtTime(0.12, context.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, context.currentTime + 0.3);
    oscillator.connect(gain);
    gain.connect(context.destination);
    oscillator.start();
    oscillator.stop(context.currentTime + 0.3);
    oscillator.onended = () => context.close();
  } catch (_error) {
    // Audio may be disabled by the browser or device.
  }
}

function startTimer(seconds) {
  timerEnd = Date.now() + Math.max(1, seconds) * 1000;
  writeStored("gym_timer_end", timerEnd);
  if (timerInterval !== null) clearInterval(timerInterval);
  timerInterval = setInterval(renderTimer, 1000);
  renderTimer();
}

function adjustTimer(seconds) {
  if (!timerEnd) return;
  timerEnd += seconds * 1000;
  writeStored("gym_timer_end", timerEnd);
  if (timerEnd <= Date.now()) completeTimer();
  else renderTimer();
}

function resumeTimer() {
  if (!timerEnd) return;
  if (timerEnd <= Date.now()) {
    completeTimer();
    return;
  }
  timerInterval = setInterval(renderTimer, 1000);
  renderTimer();
}

function createClientId() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  clientCounter += 1;
  return `offline-${Date.now().toString(36)}-${clientCounter}-${Math.random().toString(36).slice(2)}`;
}

function hydrateQueue() {
  if (!appState?.exercises) return;
  for (const item of queue) {
    const exercise = appState.exercises.find((entry) => entry.name === item.exercise);
    if (!exercise) continue;
    exercise.logged ??= [];
    if (exercise.logged.some((set) => set.id === `pending:${item.client_id}`)) continue;
    exercise.logged.push({
      id: `pending:${item.client_id}`,
      weight_kg: item.weight_kg,
      reps: item.reps,
      set_index: exercise.logged.length + 1,
    });
  }
}

function removePending(clientId) {
  const pendingId = `pending:${clientId}`;
  for (const exercise of appState?.exercises ?? []) {
    exercise.logged = exercise.logged.filter((set) => set.id !== pendingId);
  }
}

function keepSavedSet(item, saved) {
  unconfirmedSetIds.delete(item.client_id);
  const exercise = appState?.exercises?.find((entry) => entry.name === item.exercise);
  if (!exercise || !saved?.id) return;
  removePending(item.client_id);
  const savedSet = {
    id: saved.id,
    weight_kg: saved.weight_kg,
    reps: saved.reps,
    set_index: saved.set_index,
  };
  const existing = exercise.logged.find((set) => set.id === saved.id);
  if (existing) Object.assign(existing, savedSet);
  else exercise.logged.push(savedSet);
  exercise.logged.sort((left, right) => left.set_index - right.set_index);
}

async function reconcileSavedSet(item, saved) {
  const values = {
    weight_kg: roundWeight(item.weight_kg),
    reps: Number(item.reps),
  };
  if (Math.abs(Number(saved.weight_kg) - values.weight_kg) < WEIGHT_EPSILON
    && Number(saved.reps) === values.reps) {
    return { ok: true, saved };
  }

  try {
    const { response, payload } = await requestJson(`/api/sets/${saved.id}`, "PATCH", values);
    if (!response.ok) {
      return {
        ok: false,
        status: response.status,
        permanent: [400, 404, 409, 422].includes(response.status),
        saved,
        message: payload?.error || payload?.detail || `HTTP ${response.status}`,
      };
    }
    const stored = payload && payload.weight_kg !== undefined && payload.reps !== undefined
      ? { ...saved, ...payload }
      : { ...saved, ...values };
    return { ok: true, saved: stored };
  } catch (_error) {
    return { ok: false, message: "Keine Verbindung für die Korrektur." };
  }
}

function permanentCorrectionNotice(result) {
  return `Korrektur nicht möglich, der Server-Wert bleibt gespeichert. ${result.message}`;
}

function correctionFailureNotice(result) {
  return result.message
    ? `Korrektur des gespeicherten Satzes fehlgeschlagen. ${result.message}`
    : "Korrektur des gespeicherten Satzes fehlgeschlagen. Bitte erneut versuchen.";
}

async function refreshState() {
  try {
    const response = await fetch("/api/state", { cache: "no-store" });
    if (response.status === 401 || response.status === 403) {
      screenMode = "login";
      loginError = "Deine Anmeldung ist abgelaufen. Bitte melde dich erneut an.";
      render();
      return false;
    }
    if (!response.ok) return false;
    const previousSessionId = appState?.session?.id ?? null;
    appState = await response.json();
    const nextSessionId = appState?.session?.id ?? null;
    if (previousSessionId !== nextSessionId) {
      drafts = new Map();
      histories = {};
      editingSet = null;
    }
    restoreExerciseView();
    hydrateQueue();
    return true;
  } catch (_error) {
    return false;
  }
}

function restoreExerciseView() {
  if (!appState?.session) {
    selectedExercise = null;
    removeStored("gym_view");
    return;
  }
  if (selectedExercise && appState.exercises.some((exercise) => exercise.name === selectedExercise)) return;
  const savedExercise = readStored("gym_view", null);
  selectedExercise = typeof savedExercise === "string"
    && appState.exercises.some((exercise) => exercise.name === savedExercise)
    ? savedExercise
    : null;
  if (!selectedExercise) removeStored("gym_view");
}

async function loadState() {
  screenMode = "loading";
  render();
  try {
    const response = await fetch("/api/state", { cache: "no-store" });
    if (response.status === 401 || response.status === 403) {
      screenMode = "login";
      loginError = "Bitte melde dich an, um dein Training zu öffnen.";
      render();
      return false;
    }
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const previousSessionId = appState?.session?.id ?? null;
    appState = await response.json();
    screenMode = "app";
    const nextSessionId = appState?.session?.id ?? null;
    if (previousSessionId !== nextSessionId) {
      drafts = new Map();
      histories = {};
      editingSet = null;
    }
    restoreExerciseView();
    loadError = "";
    hydrateQueue();
    render();
    if (selectedExercise) void loadHistory(selectedExercise);
    void flushQueue();
    return true;
  } catch (_error) {
    screenMode = "error";
    loadError = "Der Server ist gerade nicht erreichbar.";
    render();
    return false;
  }
}

function flushQueue() {
  if (flushPromise) return flushPromise;
  if (!queue.length) return Promise.resolve(true);
  if (screenMode !== "app") return Promise.resolve(false);

  const sending = (async () => {
    let serverChanged = false;
    let validationRejected = false;
    while (queue.length) {
      const item = queue[0];
      let response;
      try {
        response = await fetch("/api/sets", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(item),
        });
      } catch (_error) {
        notice = "Keine Verbindung. Deine Sätze warten auf den nächsten Sync.";
        break;
      }

      if (response.ok) {
        const saved = await response.json().catch(() => null);
        if (!saved?.id) {
          notice = "Der Satz konnte nicht bestätigt werden und bleibt für den nächsten Sync gespeichert.";
          break;
        }
        const reconciled = await reconcileSavedSet(item, saved);
        if (!reconciled.ok && reconciled.permanent) {
          queue = queue.slice(1);
          writeStored("gym_queue", queue);
          keepSavedSet(item, reconciled.saved);
          notice = permanentCorrectionNotice(reconciled);
          validationRejected = true;
          serverChanged = true;
          continue;
        }
        if (!reconciled.ok) {
          if (reconciled.status === 401 || reconciled.status === 403) {
            screenMode = "login";
            loginError = "Deine Anmeldung ist abgelaufen. Bitte melde dich erneut an.";
          }
          notice = correctionFailureNotice(reconciled);
          break;
        }
        queue = queue.slice(1);
        writeStored("gym_queue", queue);
        keepSavedSet(item, reconciled.saved);
        serverChanged = true;
        continue;
      }

      if (response.status === 401 || response.status === 403) {
        screenMode = "login";
        loginError = "Deine Anmeldung ist abgelaufen. Bitte melde dich erneut an.";
        notice = "";
        break;
      }

      const failure = await response.json().catch(() => ({}));
      if ([400, 404, 409, 422].includes(response.status)) {
        queue = queue.slice(1);
        writeStored("gym_queue", queue);
        removePending(item.client_id);
        unconfirmedSetIds.delete(item.client_id);
        notice = failure.error || failure.detail || "Der Satz war ungültig und wurde verworfen.";
        validationRejected = true;
        continue;
      }

      notice = response.status >= 500
        ? "Der Server ist gerade nicht erreichbar. Deine Sätze bleiben gespeichert."
        : "Der Satz konnte nicht synchronisiert werden und bleibt gespeichert.";
      break;
    }

    if (serverChanged) {
      await refreshState();
      if (screenMode === "app" && selectedExercise) await loadHistory(selectedExercise);
    }
    return queue.length === 0 && !validationRejected && screenMode === "app";
  })();
  flushPromise = trackSetSend(sending.finally(() => {
    flushPromise = null;
    render();
  }));
  return flushPromise;
}

async function loadHistory(exercise) {
  const requestId = ++historyRequestCounter;
  historyRequestIds.set(exercise, requestId);
  histories[exercise] = undefined;
  render();
  try {
    const query = new URLSearchParams({ exercise, limit: "5" });
    const response = await fetch(`/api/history?${query.toString()}`);
    if (!response.ok) throw new Error("Verlauf konnte nicht geladen werden.");
    const result = await response.json();
    if (historyRequestIds.get(exercise) === requestId) histories[exercise] = result;
  } catch (_error) {
    if (historyRequestIds.get(exercise) === requestId) {
      histories[exercise] = { error: "Verlauf nicht verfügbar." };
    }
  }
  if (selectedExercise === exercise && historyRequestIds.get(exercise) === requestId) render();
}

function addDraft() {
  const exercise = currentExercise();
  if (!exercise) return;
  const list = draftsFor(exercise);
  const source = list[list.length - 1]
    ?? exercise.logged[exercise.logged.length - 1]
    ?? exercise.prefill[exercise.prefill.length - 1]
    ?? { weight_kg: "", reps: "" };
  list.push(makeDraft(source.weight_kg, source.reps, true));
  notice = "";
  render();
}

async function logDraft(clientId) {
  const exercise = currentExercise();
  const list = exercise ? draftsFor(exercise) : [];
  const draft = list.find((entry) => entry.client_id === clientId);
  if (!exercise || !draft) return;
  const weight = roundWeight(parseDecimal(draft.weight_kg));
  const reps = Number(String(draft.reps).trim());
  if (!Number.isFinite(weight) || weight <= 0 || weight > 500 || !Number.isInteger(reps) || reps < 1 || reps > 100) {
    notice = "Gewicht muss über 0 und höchstens 500 kg liegen; Wiederholungen: 1 bis 100.";
    render();
    return;
  }
  const shownWeight = inputWeight(weight);
  if (draft.weight_kg !== shownWeight) {
    draft.weight_kg = shownWeight;
    for (const input of screen.querySelectorAll("[data-draft-input]")) {
      if (input.dataset.clientId === clientId && input.dataset.field === "weight_kg") input.value = shownWeight;
    }
  }

  const item = {
    client_id: clientId,
    exercise: exercise.name,
    weight_kg: weight,
    reps,
  };
  unconfirmedSetIds.add(clientId);

  if (!writeStored("gym_queue", [...queue, item])) {
    const saved = await trackSetSend((async () => {
      try {
        const response = await fetch("/api/sets", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(item),
        });
        if (response.status === 401 || response.status === 403) {
          screenMode = "login";
          loginError = "Deine Anmeldung ist abgelaufen. Bitte melde dich erneut an.";
          notice = "Satz nicht gespeichert. Bitte nach der Anmeldung erneut abhaken.";
          return false;
        }
        if (!response.ok) {
          const failure = await response.json().catch(() => ({}));
          const detail = failure.error || failure.detail;
          notice = detail
            ? `Satz nicht gespeichert. ${detail}`
            : "Satz nicht gespeichert. Bitte Verbindung prüfen und erneut abhaken.";
          return false;
        }

        const confirmation = await response.json().catch(() => null);
        if (!confirmation?.id) {
          notice = "Satz nicht gespeichert. Die Serverbestätigung fehlt; bitte erneut abhaken.";
          return false;
        }
        const reconciled = await reconcileSavedSet(item, confirmation);
        if (!reconciled.ok && reconciled.permanent) {
          keepSavedSet(item, reconciled.saved);
          removeDraft(exercise, clientId);
          notice = permanentCorrectionNotice(reconciled);
          return true;
        }
        if (!reconciled.ok) {
          if (reconciled.status === 401 || reconciled.status === 403) {
            screenMode = "login";
            loginError = "Deine Anmeldung ist abgelaufen. Bitte melde dich erneut an.";
          }
          notice = correctionFailureNotice(reconciled);
          return false;
        }
        keepSavedSet(item, reconciled.saved);
        removeDraft(exercise, clientId);
        notice = "Satz synchronisiert. Die lokale Offline-Speicherung ist nicht verfügbar.";
        startTimer(exercise.rest_s);
        return true;
      } catch (_error) {
        notice = "Satz nicht gespeichert. Bitte Verbindung prüfen und erneut abhaken.";
        return false;
      }
    })());
    if (saved && selectedExercise) await loadHistory(selectedExercise);
    render();
    return;
  }

  queue.push(item);
  exercise.logged.push({
    id: `pending:${clientId}`,
    weight_kg: weight,
    reps,
    set_index: exercise.logged.length + 1,
  });
  removeDraft(exercise, clientId);
  notice = "";
  startTimer(exercise.rest_s);
  render();
  await flushQueue();
}

async function requestJson(path, method, body) {
  const options = { method, headers: {} };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const payload = await response.json().catch(() => null);
  return { response, payload };
}

function changedValue(value, field, direction) {
  const parsed = field === "weight_kg" ? parseDecimal(value) : Number(value);
  if (field === "weight_kg") {
    const next = Math.max(0.25, (Number.isFinite(parsed) ? parsed : 0) + direction * 2.5);
    return String(Math.round(next * 100) / 100).replace(".", ",");
  }
  return String(Math.max(1, (Number.isFinite(parsed) ? parsed : 0) + direction));
}

async function saveEdit() {
  if (!editingSet || String(editingSet.id).startsWith("pending:")) return;
  const weight = parseDecimal(editingSet.weight_kg);
  const reps = Number(String(editingSet.reps).trim());
  if (!Number.isFinite(weight) || weight <= 0 || weight > 500 || !Number.isInteger(reps) || reps < 1 || reps > 100) {
    notice = "Bitte prüfe Gewicht und Wiederholungen.";
    render();
    return;
  }
  try {
    const { response, payload } = await requestJson(`/api/sets/${editingSet.id}`, "PATCH", {
      weight_kg: weight,
      reps,
    });
    if (!response.ok) throw new Error(payload?.error || payload?.detail || "Änderung fehlgeschlagen.");
    editingSet = null;
    notice = "";
    await refreshState();
  } catch (error) {
    notice = error.message || "Änderung fehlgeschlagen.";
  }
  render();
}

async function deleteEdit() {
  if (!editingSet || String(editingSet.id).startsWith("pending:")) return;
  if (!window.confirm("Diesen Satz wirklich löschen?")) return;
  try {
    const response = await fetch(`/api/sets/${editingSet.id}`, { method: "DELETE" });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload.error || payload.detail || "Löschen fehlgeschlagen.");
    }
    editingSet = null;
    notice = "";
    await refreshState();
  } catch (error) {
    notice = error.message || "Löschen fehlgeschlagen.";
  }
  render();
}

async function finishTraining() {
  if (!window.confirm("Training beenden und Session speichern?")) return;
  const synchronized = await flushQueue();
  await waitForSetSends();
  if (!synchronized || queue.length || unconfirmedSetIds.size) {
    if (!notice) notice = "Erst synchronisieren, bevor du das Training beendest.";
    render();
    return;
  }
  try {
    const { response, payload } = await requestJson("/api/finish", "POST");
    if (!response.ok) throw new Error(payload?.error || payload?.detail || "Training konnte nicht beendet werden.");
    finishSummary = payload;
    screenMode = "summary";
    selectedExercise = null;
    removeStored("gym_view");
    notice = "";
  } catch (error) {
    notice = error.message || "Training konnte nicht beendet werden.";
  }
  render();
}

document.addEventListener("input", (event) => {
  const input = event.target;
  if (input.matches("[data-draft-input]")) {
    const exercise = currentExercise();
    if (exercise) updateDraftField(exercise, input.dataset.clientId, input.dataset.field, input.value);
  } else if (input.matches("[data-edit-input]") && editingSet) {
    editingSet[input.dataset.field] = input.value;
  }
});

document.addEventListener("submit", async (event) => {
  if (event.target.id !== "login-form") return;
  event.preventDefault();
  const token = new FormData(event.target).get("token");
  loginError = "";
  try {
    const { response, payload } = await requestJson("/auth/session", "POST", { token });
    if (!response.ok) throw new Error(payload?.detail || payload?.error || "Anmeldung fehlgeschlagen.");
    screenMode = "loading";
    await loadState();
    if (screenMode === "app") await flushQueue();
  } catch (error) {
    loginError = error.message || "Anmeldung fehlgeschlagen.";
    render();
    screen.querySelector("#access-token")?.focus();
  }
});

document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-action]");
  if (!button) return;
  const action = button.dataset.action;

  if (action === "open-exercise") {
    selectedExercise = button.dataset.exercise;
    writeStored("gym_view", selectedExercise);
    editingSet = null;
    render();
    void loadHistory(selectedExercise);
  } else if (action === "back") {
    selectedExercise = null;
    removeStored("gym_view");
    editingSet = null;
    notice = "";
    render();
  } else if (action === "back-home") {
    screenMode = "app";
    finishSummary = null;
    selectedExercise = null;
    removeStored("gym_view");
    await refreshState();
    render();
  } else if (action === "add-draft") {
    addDraft();
  } else if (action === "log-draft") {
    await logDraft(button.dataset.clientId);
  } else if (action === "adjust-draft") {
    const exercise = currentExercise();
    const draft = exercise && draftsFor(exercise).find((entry) => entry.client_id === button.dataset.clientId);
    if (draft) {
      updateDraftField(exercise, draft.client_id, button.dataset.field, changedValue(
        draft[button.dataset.field], button.dataset.field, Number(button.dataset.step),
      ));
      render();
    }
  } else if (action === "edit-set") {
    const exercise = currentExercise();
    const set = exercise?.logged.find((entry) => String(entry.id) === button.dataset.setId);
    if (!set) return;
    if (String(set.id).startsWith("pending:")) {
      notice = "Dieser Satz wird noch synchronisiert.";
    } else {
      editingSet = { ...set, weight_kg: inputWeight(set.weight_kg), reps: String(set.reps) };
      notice = "";
    }
    render();
  } else if (action === "adjust-edit" && editingSet) {
    const field = button.dataset.field;
    editingSet[field] = changedValue(editingSet[field], field, Number(button.dataset.step));
    render();
  } else if (action === "save-edit") {
    await saveEdit();
  } else if (action === "delete-edit") {
    await deleteEdit();
  } else if (action === "cancel-edit") {
    editingSet = null;
    render();
  } else if (action === "finish") {
    await finishTraining();
  } else if (action === "retry") {
    await loadState();
  } else if (action === "timer-minus") {
    adjustTimer(-30);
  } else if (action === "timer-plus") {
    adjustTimer(30);
  } else if (action === "timer-skip") {
    adjustTimer(-Math.ceil((timerEnd - Date.now()) / 1000));
  }
});

async function handleReconnect() {
  if (screenMode === "error") {
    await loadState();
    return;
  }
  await flushQueue();
}

window.addEventListener("online", () => void handleReconnect());
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") void handleReconnect();
});

resumeTimer();
void loadState();

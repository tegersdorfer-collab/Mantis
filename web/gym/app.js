"use strict";

const screen = document.querySelector("#screen");
const timerHost = document.querySelector("#timer-host");
const syncHost = document.querySelector("#sync-host");
const noticeHost = document.querySelector("#notice-host");
const tabBar = document.querySelector("#tab-bar");

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
const DRAFT_STORAGE_KEY = "gym_drafts";
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
let activeTab = "training";
let exerciseDirectory = null;
let exerciseSearch = "";
let exerciseMuscle = "";
let exerciseListRequestId = 0;
let selectedLibraryExerciseId = null;
let exerciseDetail = null;
let workoutHistory = null;
let selectedWorkoutId = null;
let workoutDetail = null;
let historyEditSet = null;
let planList = null;
let selectedPlanId = null;
let planExercisePickerDayId = null;
let sessionExercisePickerOpen = false;
let showNewExerciseForm = false;
let newPlanError = "";

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

function readDraftStore() {
  try {
    const stored = localStorage.getItem(DRAFT_STORAGE_KEY);
    if (stored === null) return {};
    const parsed = JSON.parse(stored);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
  } catch (_error) {
    return null;
  }
}

function writeDraftStore(stored) {
  if (!Object.keys(stored).length) {
    removeStored(DRAFT_STORAGE_KEY);
    return true;
  }
  return writeStored(DRAFT_STORAGE_KEY, stored);
}

function pruneDraftStore() {
  const stored = readDraftStore();
  if (stored === null) return;
  const sessionId = appState?.session?.id;
  const activeSessionKey = sessionId === null || sessionId === undefined ? "pending" : String(sessionId);
  let changed = false;
  for (const key of Object.keys(stored)) {
    if (key === activeSessionKey) continue;
    delete stored[key];
    changed = true;
  }
  if (changed || !Object.keys(stored).length) writeDraftStore(stored);
}

function migratePendingDrafts(sessionId) {
  if (sessionId === null || sessionId === undefined) return;
  const stored = readDraftStore();
  if (stored === null || !isObjectRecord(stored.pending)) return;
  const sessionKey = String(sessionId);
  const pending = stored.pending;
  const active = isObjectRecord(stored[sessionKey]) ? stored[sessionKey] : {};
  for (const [exerciseName, entries] of Object.entries(pending)) {
    const existing = isObjectRecord(active[exerciseName]) ? active[exerciseName] : {};
    active[exerciseName] = { ...entries, ...existing };
  }
  stored[sessionKey] = active;
  delete stored.pending;
  writeDraftStore(stored);
}

function clearDraftsForSession(sessionId) {
  if (sessionId === null || sessionId === undefined) return;
  const stored = readDraftStore();
  if (stored === null) return;
  delete stored[String(sessionId)];
  writeDraftStore(stored);
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

function isObjectRecord(value) {
  return value && typeof value === "object" && !Array.isArray(value);
}

function storedDraftsFor(exercise) {
  const stored = readDraftStore();
  if (!stored) return {};
  const sessionId = appState?.session?.id;
  const sessionKey = sessionId === null || sessionId === undefined ? "pending" : String(sessionId);
  const session = stored[sessionKey];
  const exerciseDrafts = isObjectRecord(session) ? session[exercise.name] : null;
  return isObjectRecord(exerciseDrafts) ? exerciseDrafts : {};
}

function restoreDraftValues(draft, stored) {
  if (!isObjectRecord(stored)) return draft;
  if (typeof stored.client_id === "string" && stored.client_id) {
    draft.client_id = stored.client_id;
  }
  if (typeof stored.weight_kg === "string" || typeof stored.weight_kg === "number") {
    draft.weight_kg = inputWeight(stored.weight_kg);
  }
  if (typeof stored.reps === "string" || typeof stored.reps === "number") {
    draft.reps = String(stored.reps);
  }
  if (isObjectRecord(stored.userChanged)) {
    draft.userChanged = {
      weight_kg: stored.userChanged.weight_kg === true,
      reps: stored.userChanged.reps === true,
    };
  }
  if (isObjectRecord(stored.autoFilled)) {
    draft.autoFilled = {
      weight_kg: inputWeight(stored.autoFilled.weight_kg),
      reps: String(stored.autoFilled.reps ?? ""),
    };
  } else if (stored.autoFilled === null) {
    draft.autoFilled = null;
  }
  return draft;
}

function persistDraftsFor(exercise) {
  const sessionId = appState?.session?.id;
  const stored = readDraftStore();
  if (stored === null) return;

  const sessionKey = sessionId === null || sessionId === undefined ? "pending" : String(sessionId);
  const session = isObjectRecord(stored[sessionKey]) ? stored[sessionKey] : {};
  const openDrafts = drafts.get(exercise.name) ?? [];
  const entries = {};
  openDrafts.forEach((draft, index) => {
    const setIndex = (exercise.logged ?? []).length + index + 1;
    entries[String(setIndex)] = {
      client_id: draft.client_id,
      weight_kg: draft.weight_kg,
      reps: draft.reps,
      autoFilled: draft.autoFilled ? { ...draft.autoFilled } : null,
      userChanged: { ...draft.userChanged },
    };
  });

  if (Object.keys(entries).length) session[exercise.name] = entries;
  else delete session[exercise.name];
  if (Object.keys(session).length) stored[sessionKey] = session;
  else delete stored[sessionKey];
  writeDraftStore(stored);
}

function draftsFor(exercise) {
  if (!drafts.has(exercise.name)) {
    const remaining = (exercise.prefill ?? []).slice((exercise.logged ?? []).length);
    const saved = storedDraftsFor(exercise);
    const loggedCount = (exercise.logged ?? []).length;
    const savedIndices = Object.keys(saved)
      .map(Number)
      .filter((setIndex) => Number.isInteger(setIndex) && setIndex > loggedCount);
    const restoredCount = savedIndices.length
      ? Math.max(...savedIndices) - loggedCount
      : 0;
    const blankFirstSet = !remaining.length && !loggedCount && !(exercise.prefill ?? []).length;
    const targetDrafts = Math.max(0, Number(exercise.target_sets || 0) - loggedCount);
    const draftCount = Math.max(remaining.length, targetDrafts, restoredCount, blankFirstSet ? 1 : 0);
    const initial = [];
    for (let index = 0; index < draftCount; index += 1) {
      const source = remaining[index]
        ?? initial[index - 1]
        ?? exercise.logged?.[exercise.logged.length - 1]
        ?? exercise.prefill?.[exercise.prefill.length - 1]
        ?? { weight_kg: "", reps: "" };
      const draft = makeDraft(source.weight_kg, source.reps, Boolean(
        remaining[index] || index > 0 || exercise.logged?.length || exercise.prefill?.length,
      ));
      initial.push(restoreDraftValues(draft, saved[String(loggedCount + index + 1)]));
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
  persistDraftsFor(exercise);
}

function removeDraft(exercise, clientId) {
  const list = draftsFor(exercise);
  drafts.set(exercise.name, list.filter((draft) => draft.client_id !== clientId));
  persistDraftsFor(exercise);
}

function renderBrand() {
  return `<div class="brand-row">
    <span class="brand-mark"><img src="/icon.png" alt=""><span>Mantis Gym</span></span>
    <span>Krafttraining</span>
  </div>`;
}

function renderHome() {
  const home = appState?.home;
  if (home?.active_plan) {
    const plan = home.active_plan;
    const weekDays = (home.week_days ?? []).map((day) => {
      const date = new Date(`${day.date}T12:00:00`);
      const label = Number.isNaN(date.getTime())
        ? day.label
        : new Intl.DateTimeFormat("de-DE", { weekday: "short" }).format(date);
      const dayNumber = Number.isNaN(date.getTime()) ? "" : date.getDate();
      return `<div class="week-day${day.today ? " is-today" : ""}${day.trained ? " is-trained" : ""}">
        <span>${escapeHtml(label)}</span><strong>${dayNumber}</strong>
        <i aria-label="${day.trained ? "Training absolviert" : "Kein Training"}"></i></div>`;
    }).join("");
    const days = (plan.days ?? []).map((day, index) => {
      const exercises = day.exercises ?? [];
      const preview = exercises.slice(0, 3).map((item) => item.name).join(" · ");
      const isNext = home.next_day?.id === day.id;
      return `<button class="plan-day-card${isNext ? " is-next" : ""}" type="button"
        data-action="start-day" data-day-id="${day.id}" aria-label="${escapeHtml(day.name)} starten">
        <span class="plan-day-number">${index + 1}</span>
        <span class="plan-day-copy"><strong>${escapeHtml(day.name)}</strong>
          <span>${exercises.length} ${exercises.length === 1 ? "Übung" : "Übungen"}${preview ? ` · ${escapeHtml(preview)}` : ""}</span></span>
        <span class="plan-day-arrow">${isNext ? "Nächster Tag" : "›"}</span>
      </button>`;
    }).join("");
    const continueButton = appState.session
      ? `<section class="continue-card"><div><strong>${escapeHtml(appState.session.title || "Training läuft")}</strong>
          <span>Deine Session ist offen.</span></div><button class="button button-primary" type="button" data-action="continue-session">Fortsetzen</button></section>`
      : `<button class="button button-primary free-start" type="button" data-action="start-free">Freies Training</button>`;
    return `${renderBrand()}
      <div class="home-plan-heading"><div><span class="eyebrow">AKTIVER PLAN</span><h1>${escapeHtml(plan.name)}</h1></div>
        <strong class="week-number">Woche ${Number(home.week_number) || 1}</strong></div>
      <section class="week-strip" aria-label="Trainingswoche">${weekDays}</section>
      ${continueButton}
      <div class="section-heading-row"><h2>Plantage</h2><button class="text-button" type="button" data-action="tab" data-tab="plans">Alle Pläne</button></div>
      <section class="plan-day-list" aria-label="Plantage">${days}</section>`;
  }
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
  const continueAction = appState?.session
    ? `<section class="continue-card"><div><strong>${escapeHtml(appState.session.title || "Training läuft")}</strong>
        <span>Deine Session ist offen.</span></div><button class="button button-primary" type="button" data-action="continue-session">Fortsetzen</button></section>`
    : '<button class="button button-primary free-start" type="button" data-action="start-free">Freies Training starten</button>';

  return `${renderBrand()}
    <h1>Training</h1>
    <p class="subhead">Starte frei oder richte deine Trainingstage ein.</p>
    ${continueAction}
    <button class="text-button setup-plan" type="button" data-action="tab" data-tab="plans">Plan anlegen</button>
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
  const exerciseIndex = appState.exercises.findIndex((entry) => entry.name === exercise.name);
  const openDrafts = draftsFor(exercise);
  const completedLabel = exercise.logged.length === 1 ? "Satz erledigt" : "Sätze erledigt";
  const logged = exercise.logged.map(renderLoggedRow).join("");
  const draftRows = openDrafts.map((draft, index) =>
    renderDraftRow(draft, index, exercise.logged.length + index + 1)
  ).join("");
  const editPanel = exercise.logged.map((set) => renderEditPanel(set)).join("");
  const repRange = exercise.rep_min && exercise.rep_max
    ? ` · Ziel ${exercise.rep_min}–${exercise.rep_max} Wdh.`
    : "";
  const sessionActions = appState.session ? `<section class="session-exercise-tools" aria-label="Übungsoptionen">
      <button class="small-button" type="button" data-action="move-session-exercise" data-direction="-1" ${exerciseIndex <= 0 ? "disabled" : ""}>↑ Reihenfolge</button>
      <button class="small-button" type="button" data-action="move-session-exercise" data-direction="1" ${exerciseIndex >= appState.exercises.length - 1 ? "disabled" : ""}>↓ Reihenfolge</button>
      <button class="small-button" type="button" data-action="open-session-exercise-picker">+ Übung</button>
      <button class="small-button" type="button" data-action="skip-session-exercise" ${exercise.skipped ? "disabled" : ""}>Überspringen</button>
    </section>
    <section class="exercise-note"><label for="session-exercise-note">Notiz zu ${escapeHtml(exercise.name)}</label>
      <textarea id="session-exercise-note" data-session-note rows="2" maxlength="1000">${escapeHtml(exercise.note || "")}</textarea>
      <button class="text-button" type="button" data-action="save-session-note">Notiz speichern</button></section>` : "";
  return `${renderBrand()}
    <button class="back-button" type="button" data-action="back">‹ Übersicht</button>
    <h1>${escapeHtml(exercise.name)}</h1>
    <p class="subhead">${exercise.logged.length} ${completedLabel} · ${exercise.target_sets || exercise.prefill.length || 1} Zielsätze${repRange} · ${exercise.rest_s} Sek. Pause</p>
    ${sessionActions}
    <div class="set-heading" aria-hidden="true"><span>Nr.</span><span>Gewicht</span><span>Wiederholungen</span><span></span></div>
    <section class="set-list" aria-label="Sätze">${logged}${draftRows}</section>
    ${editPanel}
    <button class="button add-set-row" type="button" data-action="add-draft">+ Satz</button>
    <section aria-labelledby="history-title">
      <h2 class="section-label" id="history-title">Letzte Trainings</h2>
      ${renderHistory(exercise)}
    </section>${sessionExercisePickerOpen ? renderExercisePicker("session") : ""}`;
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

function renderTabBar() {
  if (!tabBar) return;
  const visible = ["app", "summary"].includes(screenMode);
  tabBar.hidden = !visible;
  if (!visible) {
    tabBar.innerHTML = "";
    return;
  }
  const tabs = [
    ["training", "Training", "⌂"],
    ["history", "Verlauf", "◷"],
    ["exercises", "Übungen", "◉"],
    ["plans", "Plan", "▤"],
  ];
  tabBar.innerHTML = tabs.map(([id, label, icon]) => `<button type="button" class="tab-item${activeTab === id ? " is-active" : ""}"
    data-action="tab" data-tab="${id}" aria-current="${activeTab === id ? "page" : "false"}">
    <span aria-hidden="true">${icon}</span><strong>${label}</strong></button>`).join("");
}

function renderWorkoutHistory() {
  if (workoutDetail) return renderWorkoutDetail();
  if (workoutHistory === null) return `${renderBrand()}<div class="loading-panel">Verlauf wird geladen …</div>`;
  const rows = workoutHistory.length ? workoutHistory.map((workout) => `<button type="button" class="workout-card"
    data-action="open-workout" data-workout-id="${workout.id}">
    <span><strong>${escapeHtml(workout.title)}</strong><small>${escapeHtml(formatDate(workout.date))} · ${workout.duration_min} Min.</small></span>
    <span class="workout-stats">${workout.set_count} Sätze<br>${formatWeight(workout.volume_kg)} kg</span></button>`).join("")
    : '<p class="quiet-copy">Noch keine abgeschlossenen Trainings.</p>';
  return `${renderBrand()}<h1>Verlauf</h1><p class="subhead">Deine vergangenen Trainings.</p><section class="workout-list">${rows}</section>`;
}

function renderWorkoutDetail() {
  const workout = workoutDetail;
  if (!workout) return renderWorkoutHistory();
  const sets = workout.sets.map((set) => {
    const editing = historyEditSet && String(historyEditSet.id) === String(set.id);
    if (editing) {
      return `<div class="history-set editing"><strong>${escapeHtml(set.exercise)} · Satz ${set.set_index}</strong>
        <label>kg<input data-history-edit data-field="weight_kg" value="${escapeHtml(historyEditSet.weight_kg)}" inputmode="decimal"></label>
        <label>Wdh.<input data-history-edit data-field="reps" value="${escapeHtml(historyEditSet.reps)}" inputmode="numeric"></label>
        <button class="small-button" type="button" data-action="save-history-set">Speichern</button>
        <button class="small-button" type="button" data-action="cancel-history-edit">Abbrechen</button></div>`;
    }
    return `<div class="history-set"><span><strong>${escapeHtml(set.exercise)}</strong><small>${set.is_warmup ? "Aufwärmen · " : ""}Satz ${set.set_index}</small></span>
      <span>${formatWeight(set.weight_kg)} kg × ${set.reps}</span>
      <button class="small-button" type="button" data-action="edit-history-set" data-set-id="${set.id}" aria-label="Satz bearbeiten">Bearbeiten</button>
      <button class="small-button" type="button" data-action="delete-history-set" data-set-id="${set.id}" aria-label="Satz löschen">Löschen</button></div>`;
  }).join("");
  return `${renderBrand()}<button class="back-button" type="button" data-action="history-back">‹ Verlauf</button>
    <h1>${escapeHtml(workout.title)}</h1><p class="subhead">${escapeHtml(formatDate(workout.date))} · ${workout.duration_min} Min. · ${workout.set_count} Sätze</p>
    <section class="workout-summary"><span>Volumen</span><strong>${formatWeight(workout.volume_kg)} kg</strong></section>
    <section class="history-sets" aria-label="Sätze">${sets || '<p class="quiet-copy">Keine Sätze gespeichert.</p>'}</section>
    <button class="button button-danger workout-delete" type="button" data-action="delete-workout">Training löschen</button>`;
}

function renderE1rmChart(history, name) {
  const points = [...(history ?? [])].filter((item) => Number.isFinite(Number(item.e1rm_kg))).reverse();
  if (!points.length) return '<p class="quiet-copy">Für diese Übung gibt es noch keine gewichtete e1RM-Historie.</p>';
  const width = 320;
  const height = 150;
  const pad = 22;
  const values = points.map((point) => Number(point.e1rm_kg));
  const min = Math.min(...values);
  const max = Math.max(...values);
  const spread = max - min || 1;
  const coords = values.map((value, index) => ({
    x: points.length === 1 ? width / 2 : pad + index * (width - pad * 2) / (points.length - 1),
    y: height - pad - (value - min) * (height - pad * 2) / spread,
  }));
  const path = coords.map((point, index) => `${index ? "L" : "M"}${point.x.toFixed(1)} ${point.y.toFixed(1)}`).join(" ");
  const circles = coords.map((point, index) => `<circle cx="${point.x.toFixed(1)}" cy="${point.y.toFixed(1)}" r="4"><title>${escapeHtml(points[index].date)} · ${formatWeight(values[index])} kg</title></circle>`).join("");
  return `<div class="chart-wrap"><svg class="e1rm-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="Geschätztes 1RM für ${escapeHtml(name)}">
    <path class="chart-grid" d="M${pad} ${pad}H${width - pad} M${pad} ${height - pad}H${width - pad}"/>
    <path class="chart-line" d="${path}"/>${circles}</svg>
    <div class="chart-labels"><span>${escapeHtml(formatDate(points[0].date))}</span><span>${formatWeight(values.at(-1))} kg e1RM</span><span>${escapeHtml(formatDate(points.at(-1).date))}</span></div></div>`;
}

function renderExerciseLibrary() {
  if (exerciseDetail) {
    const history = exerciseDetail.history ?? [];
    const rows = history.map((item) => {
      const bestSet = item.best_set
        ? `<small class="best-set">Bester Satz: ${formatWeight(item.best_set.weight_kg)} kg × ${item.best_set.reps}</small>`
        : "";
      return `<article class="exercise-history-card"><div class="section-heading-row">
        <strong>${escapeHtml(formatDate(item.date))}</strong><span>${item.e1rm_kg === null ? "" : `${formatWeight(item.e1rm_kg)} kg e1RM`}</span></div>
        ${bestSet}<p>${item.sets.map((set) => `${formatWeight(set.weight_kg)}×${set.reps}`).join(" · ")}</p></article>`;
    }).join("");
    return `${renderBrand()}<button class="back-button" type="button" data-action="library-back">‹ Übungen</button>
      <h1>${escapeHtml(exerciseDetail.name)}</h1><p class="subhead">${escapeHtml(exerciseDetail.muscle || "Muskelgruppe nicht hinterlegt")}
        ${exerciseDetail.equipment ? ` · ${escapeHtml(exerciseDetail.equipment)}` : ""} · ${exerciseDetail.rep_min}–${exerciseDetail.rep_max} Wdh.</p>
      <h2>Geschätztes 1RM</h2>${renderE1rmChart(history, exerciseDetail.name)}
      <h2 class="section-title-gap">Trainingsverlauf</h2>${rows || '<p class="quiet-copy">Noch keine Trainings für diese Übung.</p>'}`;
  }
  const entries = exerciseDirectory ?? [];
  const muscles = [...new Set(entries.map((item) => item.muscle).filter(Boolean))].sort();
  const rows = entries.map((item) => `<button class="library-row" type="button" data-action="open-library-exercise" data-exercise-id="${item.id}">
    <span><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.muscle || "Muskelgruppe offen")}${item.equipment ? ` · ${escapeHtml(item.equipment)}` : ""}</small></span>
    <span aria-hidden="true">›</span></button>`).join("");
  const form = showNewExerciseForm ? `<form id="new-exercise-form" class="inline-form">
    <h2>Eigene Übung</h2><label>Name<input name="name" required maxlength="100"></label>
    <label>Muskelgruppe<input name="muscle" maxlength="60"></label>
    <label>Gerät<select name="equipment"><option value="">Nicht angegeben</option><option value="barbell">Langhantel</option><option value="dumbbell">Kurzhantel</option><option value="machine">Maschine</option><option value="cable">Kabel</option><option value="bodyweight">Körpergewicht</option><option value="other">Sonstiges</option></select></label>
    <div class="form-pair"><label>Wdh. min<input name="rep_min" type="number" min="1" max="100" value="6"></label><label>Wdh. max<input name="rep_max" type="number" min="1" max="100" value="10"></label></div>
    <button class="button button-primary" type="submit">Übung speichern</button><button class="text-button" type="button" data-action="toggle-new-exercise">Abbrechen</button></form>` : "";
  return `${renderBrand()}<div class="section-heading-row"><div><h1>Übungen</h1><p class="subhead">Bibliothek und Verlauf pro Übung.</p></div>
    <button class="small-button" type="button" data-action="toggle-new-exercise">+ Eigene</button></div>
    ${form}<div class="library-filters"><label class="search-field">Suche<input id="library-search" value="${escapeHtml(exerciseSearch)}" placeholder="Übung oder Alias"></label>
      <label>Muskel<select id="library-muscle"><option value="">Alle</option>${muscles.map((muscle) => `<option value="${escapeHtml(muscle)}"${exerciseMuscle === muscle ? " selected" : ""}>${escapeHtml(muscle)}</option>`).join("")}</select></label></div>
    <section class="library-list">${rows || '<p class="quiet-copy">Keine passenden Übungen gefunden.</p>'}</section>`;
}

function renderExercisePicker(kind) {
  const rows = (exerciseDirectory ?? []).map((item) => `<button class="picker-row" type="button" data-action="pick-exercise"
    data-kind="${kind}" data-exercise-id="${item.id}"><span>${escapeHtml(item.name)}</span><small>${escapeHtml(item.muscle || "")}</small></button>`).join("");
  return `<div class="modal-backdrop"><section class="picker-modal" role="dialog" aria-modal="true" aria-label="Übung auswählen">
    <div class="section-heading-row"><h2>Übung hinzufügen</h2><button class="small-button" type="button" data-action="close-exercise-picker">Schließen</button></div>
    <label class="search-field">Suche<input id="picker-search" value="${escapeHtml(exerciseSearch)}" placeholder="Übung suchen"></label>
    <div class="picker-list">${rows || '<p class="quiet-copy">Übungen werden geladen …</p>'}</div>
  </section></div>`;
}

function renderPlanTab() {
  if (planList === null) return `${renderBrand()}<div class="loading-panel">Plan wird geladen …</div>`;
  const active = planList.find((plan) => plan.active);
  const selected = planList.find((plan) => plan.id === selectedPlanId) ?? active ?? planList[0];
  const choices = planList.map((plan) => `<button class="plan-choice${selected?.id === plan.id ? " is-selected" : ""}" type="button"
    data-action="select-plan" data-plan-id="${plan.id}"><span>${escapeHtml(plan.name)}</span>${plan.active ? '<small>Aktiv</small>' : ""}</button>`).join("");
  const planForm = `<form id="new-plan-form" class="inline-form compact-form"><label>Neuer Plan<input name="name" required maxlength="100" placeholder="z. B. Upper / Lower"></label>
    <label>Startdatum<input name="start_date" type="date" value="${new Date().toISOString().slice(0, 10)}"></label>
    <button class="button" type="submit">Plan anlegen</button><p class="inline-error">${escapeHtml(newPlanError)}</p></form>`;
  if (!selected) return `${renderBrand()}<h1>Plan</h1><p class="quiet-copy">Noch kein Plan angelegt.</p>${planForm}`;
  const days = selected.days.map((day, dayIndex) => `<article class="plan-editor-day">
    <div class="section-heading-row"><label class="plan-day-name">Tag<input data-day-name="${day.id}" value="${escapeHtml(day.name)}" maxlength="80"></label>
      <div class="editor-tools"><button class="small-button" type="button" data-action="save-day-name" data-day-id="${day.id}">Speichern</button>
        <button class="small-button" type="button" data-action="move-plan-day" data-plan-id="${selected.id}" data-day-index="${dayIndex}" data-direction="-1" ${dayIndex === 0 ? "disabled" : ""}>↑</button>
        <button class="small-button" type="button" data-action="move-plan-day" data-plan-id="${selected.id}" data-day-index="${dayIndex}" data-direction="1" ${dayIndex === selected.days.length - 1 ? "disabled" : ""}>↓</button>
        <button class="small-button" type="button" data-action="delete-plan-day" data-day-id="${day.id}">Löschen</button></div></div>
      ${(day.exercises ?? []).map((item, itemIndex) => `<div class="plan-exercise-editor">
        <strong>${escapeHtml(item.name)}</strong><div class="plan-exercise-fields">
          <label>Sätze<input type="number" min="1" max="20" data-plan-value="target_sets" data-entry-id="${item.id}" value="${item.target_sets}"></label>
          <label>Wdh. min<input type="number" min="1" max="100" data-plan-value="rep_min" data-entry-id="${item.id}" value="${item.rep_min}"></label>
          <label>Wdh. max<input type="number" min="1" max="100" data-plan-value="rep_max" data-entry-id="${item.id}" value="${item.rep_max}"></label>
          <label>Pause<input type="number" min="0" max="900" step="30" data-plan-value="rest_s" data-entry-id="${item.id}" value="${item.rest_s}"></label></div>
        <div class="editor-tools"><button class="small-button" type="button" data-action="save-plan-exercise" data-entry-id="${item.id}">Speichern</button>
          <button class="small-button" type="button" data-action="move-plan-exercise" data-day-id="${day.id}" data-entry-id="${item.id}" data-entry-index="${itemIndex}" data-direction="-1" ${itemIndex === 0 ? "disabled" : ""}>↑</button>
          <button class="small-button" type="button" data-action="move-plan-exercise" data-day-id="${day.id}" data-entry-id="${item.id}" data-entry-index="${itemIndex}" data-direction="1" ${itemIndex === day.exercises.length - 1 ? "disabled" : ""}>↓</button>
          <button class="small-button" type="button" data-action="delete-plan-exercise" data-entry-id="${item.id}">Entfernen</button></div></div>`).join("")}
      <button class="text-button" type="button" data-action="open-plan-exercise-picker" data-day-id="${day.id}">+ Übung hinzufügen</button></article>`).join("");
  return `${renderBrand()}<h1>Plan bearbeiten</h1><section class="plan-choice-list">${choices}</section>
    <section class="plan-editor-header"><div><span class="eyebrow">${selected.active ? "AKTIVER PLAN" : "PLAN"}</span>
      <input class="plan-title-input" data-plan-name value="${escapeHtml(selected.name)}" maxlength="100" aria-label="Planname">
      <small>Start ${escapeHtml(formatDate(selected.start_date))}</small></div>
      <button class="small-button" type="button" data-action="save-plan-name" data-plan-id="${selected.id}">Name speichern</button>
      ${selected.active ? "" : `<button class="button button-primary" type="button" data-action="activate-plan" data-plan-id="${selected.id}">Aktivieren</button>`}</section>
    <form id="add-day-form" class="inline-form compact-form" data-plan-id="${selected.id}"><label>Neuer Plantag<input name="name" required maxlength="80" placeholder="z. B. Upper C"></label><button class="button" type="submit">Tag hinzufügen</button></form>
    ${days}${planForm}${planExercisePickerDayId !== null ? renderExercisePicker("plan") : ""}`;
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
  else if (activeTab === "training" && selectedExercise) screen.innerHTML = renderExercise();
  else if (activeTab === "training") screen.innerHTML = renderHome();
  else if (activeTab === "history") screen.innerHTML = renderWorkoutHistory();
  else if (activeTab === "exercises") screen.innerHTML = renderExerciseLibrary();
  else if (activeTab === "plans") screen.innerHTML = renderPlanTab();
  else screen.innerHTML = renderHome();

  renderTabBar();
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
    if (exercise.logged.some((set) => set.client_id === item.client_id
      || set.id === `pending:${item.client_id}`)) continue;
    exercise.logged.push({
      id: `pending:${item.client_id}`,
      client_id: item.client_id,
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
      if (previousSessionId !== null || nextSessionId === null) drafts = new Map();
      histories = {};
      editingSet = null;
    }
    restoreExerciseView();
    hydrateQueue();
    if (previousSessionId === null && nextSessionId !== null) {
      migratePendingDrafts(nextSessionId);
      for (const exercise of appState.exercises ?? []) {
        if (drafts.has(exercise.name)) persistDraftsFor(exercise);
      }
    }
    pruneDraftStore();
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
      if (previousSessionId !== null || nextSessionId === null) drafts = new Map();
      histories = {};
      editingSet = null;
    }
    restoreExerciseView();
    hydrateQueue();
    if (previousSessionId === null && nextSessionId !== null) {
      migratePendingDrafts(nextSessionId);
      for (const exercise of appState.exercises ?? []) {
        if (drafts.has(exercise.name)) persistDraftsFor(exercise);
      }
    }
    pruneDraftStore();
    loadError = "";
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

async function apiData(path, method = "GET", body) {
  const { response, payload } = await requestJson(path, method, body);
  if (!response.ok) throw new Error(payload?.error || payload?.detail || `HTTP ${response.status}`);
  return payload;
}

async function loadExerciseList(search = exerciseSearch, muscle = exerciseMuscle, restoreFocus = "") {
  exerciseSearch = search;
  exerciseMuscle = muscle;
  const requestId = ++exerciseListRequestId;
  const query = new URLSearchParams();
  if (search) query.set("search", search);
  if (muscle) query.set("muscle", muscle);
  try {
    const queryString = query.toString();
    const result = await apiData(`/api/exercises${queryString ? `?${queryString}` : ""}`);
    if (requestId !== exerciseListRequestId) return;
    exerciseDirectory = result;
    notice = "";
  } catch (error) {
    if (requestId !== exerciseListRequestId) return;
    exerciseDirectory = [];
    notice = `Übungsbibliothek konnte nicht geladen werden. ${error.message}`;
  }
  render();
  if (restoreFocus) {
    const input = screen.querySelector(`#${restoreFocus}`);
    input?.focus();
    if (input?.setSelectionRange) input.setSelectionRange(input.value.length, input.value.length);
  }
}

async function loadExerciseDetail(exerciseId) {
  try {
    exerciseDetail = await apiData(`/api/exercises/${exerciseId}`);
    selectedLibraryExerciseId = Number(exerciseId);
    notice = "";
  } catch (error) {
    notice = `Übungsverlauf konnte nicht geladen werden. ${error.message}`;
  }
  render();
}

async function loadWorkoutList() {
  workoutDetail = null;
  historyEditSet = null;
  try {
    workoutHistory = await apiData("/api/workouts?limit=50");
    notice = "";
  } catch (error) {
    workoutHistory = [];
    notice = `Verlauf konnte nicht geladen werden. ${error.message}`;
  }
  render();
}

async function loadWorkoutDetail(workoutId) {
  try {
    workoutDetail = await apiData(`/api/workouts/${workoutId}`);
    selectedWorkoutId = Number(workoutId);
    historyEditSet = null;
    notice = "";
  } catch (error) {
    notice = `Training konnte nicht geladen werden. ${error.message}`;
  }
  render();
}

async function loadPlanList(selectId = selectedPlanId) {
  try {
    planList = await apiData("/api/plans");
    selectedPlanId = planList.some((plan) => plan.id === Number(selectId))
      ? Number(selectId)
      : planList.find((plan) => plan.active)?.id ?? planList[0]?.id ?? null;
    notice = "";
  } catch (error) {
    planList = [];
    notice = `Plan konnte nicht geladen werden. ${error.message}`;
  }
  render();
}

async function selectTab(tab) {
  if (!["training", "history", "exercises", "plans"].includes(tab)) return;
  if (screenMode === "summary") {
    screenMode = "app";
    finishSummary = null;
  }
  activeTab = tab;
  selectedExercise = null;
  exerciseDetail = null;
  workoutDetail = null;
  notice = "";
  render();
  if (tab === "history") await loadWorkoutList();
  else if (tab === "exercises") await loadExerciseList();
  else if (tab === "plans") await loadPlanList();
  else {
    await refreshState();
    selectedExercise = null;
    render();
  }
}

async function startDay(dayId) {
  try {
    await apiData("/api/sessions/start", "POST", { day_id: Number(dayId) });
    activeTab = "training";
    selectedExercise = null;
    await loadState();
    const first = appState?.exercises?.find((exercise) => !exercise.skipped);
    if (first) {
      selectedExercise = first.name;
      writeStored("gym_view", selectedExercise);
      void loadHistory(selectedExercise);
    }
    notice = "";
  } catch (error) {
    notice = `Training konnte nicht gestartet werden. ${error.message}`;
  }
  render();
}

async function startFreeTraining() {
  try {
    await apiData("/api/sessions/start", "POST", {});
    activeTab = "training";
    selectedExercise = null;
    await loadState();
    const first = appState?.exercises?.find((exercise) => !exercise.skipped);
    if (first) {
      selectedExercise = first.name;
      writeStored("gym_view", selectedExercise);
      void loadHistory(selectedExercise);
    }
    notice = "";
  } catch (error) {
    notice = `Training konnte nicht gestartet werden. ${error.message}`;
  }
  render();
}

async function continueSession() {
  activeTab = "training";
  await refreshState();
  const exercises = appState?.exercises ?? [];
  const next = exercises.find((exercise) => !exercise.skipped
    && (exercise.logged?.length ?? 0) < Number(exercise.target_sets || exercise.prefill?.length || 1))
    ?? exercises.find((exercise) => !exercise.skipped);
  selectedExercise = next?.name ?? null;
  if (selectedExercise) {
    writeStored("gym_view", selectedExercise);
    void loadHistory(selectedExercise);
  }
  render();
}

async function sendMutation(path, method, body, refresh = "") {
  try {
    const payload = await apiData(path, method, body);
    if (refresh === "state") await refreshState();
    if (refresh === "plans") await loadPlanList(selectedPlanId);
    if (refresh === "history") await loadWorkoutDetail(selectedWorkoutId);
    notice = "";
    return payload ?? true;
  } catch (error) {
    notice = error.message || "Änderung konnte nicht gespeichert werden.";
    render();
    return null;
  }
}

async function saveSessionExerciseNote() {
  const exercise = currentExercise();
  if (!exercise?.session_exercise_id || !appState?.session) return;
  const noteInput = screen.querySelector("[data-session-note]");
  await sendMutation(
    `/api/sessions/${appState.session.id}/exercises/${exercise.session_exercise_id}`,
    "PATCH",
    { note: noteInput?.value ?? exercise.note ?? "" },
    "state",
  );
}

async function savePlanExercise(entryId) {
  const values = {};
  for (const input of screen.querySelectorAll("[data-plan-value]")) {
    if (input.dataset.entryId !== String(entryId)) continue;
    values[input.dataset.planValue] = Number(input.value);
  }
  const updated = await sendMutation(`/api/plan-exercises/${entryId}`, "PATCH", values, "plans");
  if (updated) await refreshState();
}

async function saveHistorySet() {
  if (!historyEditSet || selectedWorkoutId === null) return;
  const weight = parseDecimal(historyEditSet.weight_kg);
  const reps = Number(String(historyEditSet.reps).trim());
  if (!Number.isFinite(weight) || weight <= 0 || weight > 500 || !Number.isInteger(reps) || reps < 1 || reps > 100) {
    notice = "Bitte prüfe Gewicht und Wiederholungen.";
    render();
    return;
  }
  const saved = await sendMutation(
    `/api/workouts/${selectedWorkoutId}/sets/${historyEditSet.id}`,
    "PATCH",
    { weight_kg: weight, reps },
    "history",
  );
  if (saved) {
    historyEditSet = null;
    render();
  }
}

async function deleteHistorySet(setId) {
  if (!window.confirm("Diesen Satz wirklich löschen?")) return;
  await sendMutation(`/api/workouts/${selectedWorkoutId}/sets/${setId}`, "DELETE", undefined, "history");
}

async function deleteSelectedWorkout() {
  if (!selectedWorkoutId || !window.confirm("Dieses Training mit allen Sätzen wirklich löschen?")) return;
  const deleted = await sendMutation(`/api/workouts/${selectedWorkoutId}`, "DELETE");
  if (deleted !== null) {
    workoutDetail = null;
    selectedWorkoutId = null;
    await loadWorkoutList();
  }
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
  persistDraftsFor(exercise);
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
  persistDraftsFor(exercise);
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
        if (!appState?.session) await refreshState();
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
    clearDraftsForSession(appState?.session?.id);
    drafts = new Map();
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
  } else if (input.matches("[data-history-edit]") && historyEditSet) {
    historyEditSet[input.dataset.field] = input.value;
  } else if (input.id === "library-search" || input.id === "picker-search") {
    exerciseSearch = input.value;
    void loadExerciseList(exerciseSearch, exerciseMuscle, input.id);
  } else if (input.id === "library-muscle") {
    exerciseMuscle = input.value;
    void loadExerciseList(exerciseSearch, exerciseMuscle);
  }
});

document.addEventListener("submit", async (event) => {
  const form = event.target;
  if (form.id === "login-form") {
    event.preventDefault();
    const token = new FormData(form).get("token");
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
    return;
  }

  event.preventDefault();
  const formData = new FormData(form);
  if (form.id === "new-exercise-form") {
    const body = {
      name: formData.get("name"),
      muscle: formData.get("muscle") || null,
      equipment: formData.get("equipment") || null,
      rep_min: Number(formData.get("rep_min")),
      rep_max: Number(formData.get("rep_max")),
    };
    const created = await sendMutation("/api/exercises", "POST", body);
    if (created) {
      showNewExerciseForm = false;
      exerciseSearch = "";
      await loadExerciseList();
    }
  } else if (form.id === "new-plan-form") {
    const body = { name: formData.get("name"), start_date: formData.get("start_date") || null };
    const created = await sendMutation("/api/plans", "POST", body);
    if (created) {
      selectedPlanId = created.id;
      newPlanError = "";
      await loadPlanList(created.id);
    }
  } else if (form.id === "add-day-form") {
    const planId = form.dataset.planId;
    const created = await sendMutation(`/api/plans/${planId}/days`, "POST", { name: formData.get("name") });
    if (created) await loadPlanList(planId);
  }
});

document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-action]");
  if (!button) return;
  const action = button.dataset.action;

  if (action === "tab") {
    await selectTab(button.dataset.tab);
  } else if (action === "start-day") {
    await startDay(button.dataset.dayId);
  } else if (action === "start-free") {
    await startFreeTraining();
  } else if (action === "continue-session") {
    await continueSession();
  } else if (action === "open-exercise") {
    activeTab = "training";
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
    activeTab = "training";
    screenMode = "app";
    finishSummary = null;
    selectedExercise = null;
    removeStored("gym_view");
    await refreshState();
    render();
  } else if (action === "skip-session-exercise") {
    const exercise = currentExercise();
    if (!exercise?.session_exercise_id || !appState?.session) return;
    const sorted = [...appState.exercises].sort((a, b) => a.sort_order - b.sort_order);
    const index = sorted.findIndex((entry) => entry.name === exercise.name);
    const updated = await sendMutation(
      `/api/sessions/${appState.session.id}/exercises/${exercise.session_exercise_id}`,
      "PATCH",
      { skipped: true },
      "state",
    );
    if (updated) {
      selectedExercise = sorted.slice(index + 1).find((entry) => !entry.skipped)?.name
        ?? sorted.slice(0, index).find((entry) => !entry.skipped)?.name
        ?? null;
      if (selectedExercise) writeStored("gym_view", selectedExercise);
      else removeStored("gym_view");
      render();
    }
  } else if (action === "move-session-exercise") {
    const exercise = currentExercise();
    if (!exercise?.session_exercise_id || !appState?.session) return;
    const sorted = [...appState.exercises].sort((a, b) => a.sort_order - b.sort_order);
    const index = sorted.findIndex((entry) => entry.name === exercise.name);
    const nextIndex = index + Number(button.dataset.direction);
    if (nextIndex < 0 || nextIndex >= sorted.length) return;
    await sendMutation(
      `/api/sessions/${appState.session.id}/exercises/${exercise.session_exercise_id}`,
      "PATCH",
      { sort_order: nextIndex },
      "state",
    );
  } else if (action === "save-session-note") {
    await saveSessionExerciseNote();
  } else if (action === "open-session-exercise-picker") {
    sessionExercisePickerOpen = true;
    if (exerciseDirectory === null) await loadExerciseList("", "");
    render();
  } else if (action === "open-plan-exercise-picker") {
    planExercisePickerDayId = Number(button.dataset.dayId);
    if (exerciseDirectory === null) await loadExerciseList("", "");
    render();
  } else if (action === "close-exercise-picker") {
    planExercisePickerDayId = null;
    sessionExercisePickerOpen = false;
    render();
  } else if (action === "pick-exercise") {
    const exerciseId = Number(button.dataset.exerciseId);
    const kind = button.dataset.kind;
    const picked = exerciseDirectory?.find((item) => item.id === exerciseId);
    if (!picked) return;
    if (kind === "session" && appState?.session) {
      const added = await sendMutation(
        `/api/sessions/${appState.session.id}/exercises`,
        "POST",
        { exercise_id: exerciseId },
        "state",
      );
      if (added) {
        sessionExercisePickerOpen = false;
        selectedExercise = picked.name;
        writeStored("gym_view", selectedExercise);
        render();
      }
    } else if (kind === "plan" && planExercisePickerDayId !== null) {
      const dayId = planExercisePickerDayId;
      const added = await sendMutation(
        `/api/plan-days/${dayId}/exercises`,
        "POST",
        { exercise_id: exerciseId },
        "plans",
      );
      if (added) {
        planExercisePickerDayId = null;
        await loadPlanList(selectedPlanId);
      }
    }
  } else if (action === "open-library-exercise") {
    await loadExerciseDetail(Number(button.dataset.exerciseId));
  } else if (action === "library-back") {
    exerciseDetail = null;
    await loadExerciseList();
  } else if (action === "toggle-new-exercise") {
    showNewExerciseForm = !showNewExerciseForm;
    render();
  } else if (action === "open-workout") {
    await loadWorkoutDetail(Number(button.dataset.workoutId));
  } else if (action === "history-back") {
    workoutDetail = null;
    await loadWorkoutList();
  } else if (action === "edit-history-set") {
    const item = workoutDetail?.sets.find((entry) => entry.id === Number(button.dataset.setId));
    if (!item) return;
    historyEditSet = { id: item.id, weight_kg: inputWeight(item.weight_kg), reps: String(item.reps) };
    render();
  } else if (action === "save-history-set") {
    await saveHistorySet();
  } else if (action === "cancel-history-edit") {
    historyEditSet = null;
    render();
  } else if (action === "delete-history-set") {
    await deleteHistorySet(Number(button.dataset.setId));
  } else if (action === "delete-workout") {
    await deleteSelectedWorkout();
  } else if (action === "select-plan") {
    selectedPlanId = Number(button.dataset.planId);
    render();
  } else if (action === "activate-plan") {
    const activated = await sendMutation(`/api/plans/${button.dataset.planId}/activate`, "POST", {});
    if (activated) {
      selectedPlanId = Number(button.dataset.planId);
      await loadPlanList(selectedPlanId);
      await refreshState();
    }
  } else if (action === "save-plan-name") {
    const name = screen.querySelector("[data-plan-name]")?.value;
    await sendMutation(`/api/plans/${button.dataset.planId}`, "PATCH", { name }, "plans");
  } else if (action === "save-day-name") {
    const name = screen.querySelector(`[data-day-name="${button.dataset.dayId}"]`)?.value;
    await sendMutation(`/api/plan-days/${button.dataset.dayId}`, "PATCH", { name }, "plans");
  } else if (action === "move-plan-day") {
    const plan = planList?.find((item) => item.id === Number(button.dataset.planId));
    if (!plan) return;
    const dayIds = plan.days.map((item) => item.id);
    const index = Number(button.dataset.dayIndex);
    const target = index + Number(button.dataset.direction);
    if (target < 0 || target >= dayIds.length) return;
    [dayIds[index], dayIds[target]] = [dayIds[target], dayIds[index]];
    await sendMutation(`/api/plans/${plan.id}/days/order`, "PUT", { day_ids: dayIds }, "plans");
  } else if (action === "delete-plan-day") {
    if (!window.confirm("Diesen Plantag mit seinen Übungen entfernen?")) return;
    await sendMutation(`/api/plan-days/${button.dataset.dayId}`, "DELETE", undefined, "plans");
  } else if (action === "save-plan-exercise") {
    await savePlanExercise(Number(button.dataset.entryId));
  } else if (action === "move-plan-exercise") {
    const plan = planList?.find((item) => item.id === selectedPlanId);
    const day = plan?.days.find((item) => item.id === Number(button.dataset.dayId));
    if (!day) return;
    const ids = day.exercises.map((item) => item.id);
    const index = Number(button.dataset.entryIndex);
    const target = index + Number(button.dataset.direction);
    if (target < 0 || target >= ids.length) return;
    [ids[index], ids[target]] = [ids[target], ids[index]];
    await sendMutation(`/api/plan-days/${day.id}/exercises/order`, "PUT", { entry_ids: ids }, "plans");
  } else if (action === "delete-plan-exercise") {
    await sendMutation(`/api/plan-exercises/${button.dataset.entryId}`, "DELETE", undefined, "plans");
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
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || (!sessionExercisePickerOpen && planExercisePickerDayId === null)) return;
  sessionExercisePickerOpen = false;
  planExercisePickerDayId = null;
  render();
});

resumeTimer();
void loadState();

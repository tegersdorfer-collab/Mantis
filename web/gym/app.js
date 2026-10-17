"use strict";

const screen = document.querySelector("#screen");
const timerHost = document.querySelector("#timer-host");
const syncHost = document.querySelector("#sync-host");
const noticeHost = document.querySelector("#notice-host");
const tabBar = document.querySelector("#tab-bar");

let screenMode = "loading";
let appState = null;
let selectedExercise = null;
let trainingSheetClosed = false;
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
let exerciseDetail = null;
let workoutHistory = null;
let selectedWorkoutId = null;
let workoutDetail = null;
let historyEditSet = null;
let planList = null;
let selectedPlanId = null;
let planExercisePickerDayId = null;
let sessionExercisePickerOpen = false;
let sessionExercisePickerKind = "session";
let showNewExerciseForm = false;
let newPlanError = "";
let exerciseMenuOpen = false;
let exerciseNoteOpen = false;
let planMenuOpen = false;
let showWarmups = false;
let tenRmTarget = 10;
let tenRmEditOpen = false;
let overviewData = null;
let overviewError = "";
let overviewWeeks = 4;
let chartMetric = "workouts";
let exerciseDetailTab = "info";
let exerciseDetailMenuOpen = false;
let exerciseMusclesExpanded = false;
let exerciseInstructionsDraft = "";
let measurementFormOpen = false;
let repTargetEditorOpen = false;
let sheetStartY = null;

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

function setUnitLabel(set) {
  return set?.unit === "seconds" ? "s" : "reps";
}

function inputWeight(value) {
  return value === null || value === undefined ? "" : String(value).replace(".", ",");
}

function formatDate(value) {
  if (!value) return "";
  const date = new Date(`${String(value).slice(0, 10)}T12:00:00`);
  return Number.isNaN(date.getTime())
    ? String(value)
    : new Intl.DateTimeFormat("en-US", { day: "numeric", month: "short" }).format(date);
}

function formatEnglishDate(value, options = { weekday: "short", month: "short", day: "numeric" }) {
  if (!value) return "";
  const parsed = new Date(`${String(value).slice(0, 10)}T12:00:00`);
  return Number.isNaN(parsed.getTime())
    ? String(value)
    : new Intl.DateTimeFormat("en-US", options).format(parsed);
}

function formatOrdinalEnglishDate(value) {
  if (!value) return "";
  const parsed = new Date(`${String(value).slice(0, 10)}T12:00:00`);
  if (Number.isNaN(parsed.getTime())) return String(value);
  const day = parsed.getDate();
  const lastTwoDigits = day % 100;
  const suffix = lastTwoDigits >= 11 && lastTwoDigits <= 13
    ? "th"
    : ({ 1: "st", 2: "nd", 3: "rd" }[day % 10] || "th");
  const weekday = new Intl.DateTimeFormat("en-US", { weekday: "short" }).format(parsed);
  const month = new Intl.DateTimeFormat("en-US", { month: "short" }).format(parsed);
  return `${weekday}, ${month} ${day}${suffix}`;
}

function planDayName(title) {
  return String(title || "Training").split(" · ", 1)[0];
}

function localDateString(value = new Date()) {
  const month = String(value.getMonth() + 1).padStart(2, "0");
  const day = String(value.getDate()).padStart(2, "0");
  return `${value.getFullYear()}-${month}-${day}`;
}

function exerciseInitials(exercise) {
  const label = String(exercise?.muscle || exercise?.name || "GY").trim();
  return label.split(/[\s/-]+/).filter(Boolean).slice(0, 2).map((part) => part[0]).join("").toUpperCase();
}

function renderExerciseTile(exercise, { completed = false, selected = false, action = "" } = {}) {
  const contents = `<span class="exercise-tile${selected ? " is-selected" : ""}">${escapeHtml(exerciseInitials(exercise))}${completed ? '<i aria-label="Completed">✓</i>' : ""}</span>`;
  return action
    ? `<button type="button" class="exercise-tile-button" data-action="${action}" data-exercise="${escapeHtml(exercise.name)}" aria-label="${escapeHtml(exercise.name)}">${contents}</button>`
    : contents;
}

function mappedMuscles(value) {
  const source = String(value || "").toLocaleLowerCase("de-DE");
  const mapping = [
    ["chest", /chest|brust|pectoral|pecs?/],
    ["shoulders", /shoulder|schulter|deltoid|delts?/],
    ["biceps", /biceps|bizeps/],
    ["triceps", /triceps|trizeps/],
    ["forearms", /forearm|unterarm/],
    ["abs", /\babs?\b|bauch|core|abdomen/],
    ["quads", /quad|quadrizeps|oberschenkel vorn|\blegs?\b|\bbeine?\b/],
    ["hams", /hamstring|hams|beuger|beinbeuger|\blegs?\b|\bbeine?\b/],
    ["glutes", /glute|gesäß|po\b|\blegs?\b|\bbeine?\b/],
    ["calves", /calf|calves|wade|\blegs?\b|\bbeine?\b/],
    ["lats", /lat|latissimus/],
    ["traps", /trap|trapez/],
    ["lower back", /lower back|unterer rücken|erector/],
    ["neck", /neck|nacken/],
  ];
  const muscles = mapping.filter(([, pattern]) => pattern.test(source)).map(([muscle]) => muscle);
  if (/^(back|rücken|upper back|oberer rücken)$/.test(source.trim())) muscles.push("lats", "traps", "lower back");
  if (/^(arms?|arme?)$/.test(source.trim())) muscles.push("biceps", "triceps", "forearms");
  return [...new Set(muscles)];
}

function muscleLabel(muscle) {
  return ({ chest: "Chest", shoulders: "Shoulders", biceps: "Biceps", triceps: "Triceps",
    forearms: "Forearms", abs: "Abs", quads: "Quads", hams: "Hamstrings", glutes: "Glutes",
    calves: "Calves", lats: "Lats", traps: "Traps", "lower back": "Lower back", neck: "Neck" })[muscle] ?? muscle;
}

function capitalize(text) {
  const value = String(text ?? "");
  return value.charAt(0).toUpperCase() + value.slice(1);
}

function equipmentLabel(equipment) {
  const normalized = String(equipment || "").trim().toLowerCase();
  return ({
    barbell: "Barbell", langhantel: "Barbell", dumbbell: "Dumbbell", kurzhantel: "Dumbbell",
    machine: "Machine", maschine: "Machine", cable: "Cable", kabel: "Cable",
    bodyweight: "Bodyweight", körpergewicht: "Bodyweight", other: "Other",
  })[normalized] ?? (equipment ? String(equipment) : "Equipment not set");
}

function muscleSilhouette(highlighted) {
  const active = new Set(highlighted);
  const region = (name, path) => `<path class="muscle-region${active.has(name) ? " is-highlighted" : ""}" data-muscle="${name}" d="${path}"><title>${muscleLabel(name)}</title></path>`;
  const outline = '<path class="body-outline" d="M71 19c-9 0-15 7-15 17v9c-10 4-20 10-25 20l-9 40 12 3 12-34 7-6-5 52 6 19-5 34 8 33-2 44 12 4 10-45 4-37 5 37 10 45 12-4-2-44 8-33-5-34 6-19-5-52 7 6 12 34 12-3-9-40c-5-10-15-16-25-20v-9c0-10-6-17-15-17z"/>';
  const front = `${outline}<path class="body-detail" d="M56 46h30M60 57h22M67 82v43m10-43v43m-16 18h31"/>${
    region("neck", "M67 30h18v17H67z") + region("shoulders", "M48 47l16 1 2 19-18 3-8-11z M86 48l16-1 8 12-8 11-18-3z") +
    region("chest", "M58 57l18-5 2 22-16 3-9-8z M78 52l18 5 5 12-9 8-16-3z") +
    region("biceps", "M36 69l13 4-8 31-11-3z M99 73l13-4 6 32-11 3z") +
    region("forearms", "M30 104l11 3-7 34-11-3z M108 107l11-3 7 34-11 3z") +
    region("abs", "M66 79h22l3 49-14 7-14-7z") +
    region("quads", "M57 139h20l-2 53-17 20-7-19z M78 139h20l6 54-7 19-17-20z") +
    region("calves", "M54 213l14 3-2 38-9 31-11-2 6-32z M83 216l14-3 2 38 6 32-11 2-9-31z")
  }`;
  const back = `${outline}<path class="body-detail" d="M57 54h30m-20 62h20m-18 28h16"/>${
    region("neck", "M67 30h18v17H67z") + region("traps", "M59 48l17-5 18 5-4 24-14 7-13-7z") +
    region("shoulders", "M48 47l13 2 4 19-16 3-8-11z M94 49l13-2 7 11-8 11-16-3z") +
    region("triceps", "M37 70l13 3-9 31-11-3z M99 73l12-3 7 31-11 3z") +
    region("forearms", "M30 104l11 3-7 34-11-3z M108 107l11-3 7 34-11 3z") +
    region("lats", "M59 73l16 7 1 38-15 8-7-19z M78 80l16-7 5 34-7 19-15-8z") +
    region("lower back", "M65 119h26l3 19-16 8-16-8z") +
    region("glutes", "M59 140h16v25l-13 7-8-14z M78 140h16l5 18-8 14-13-7z") +
    region("hams", "M57 168l18-5v30l-17 20-7-19z M78 163l18 5 6 26-7 19-17-20z") +
    region("calves", "M54 213l14 3-2 38-9 31-11-2 6-32z M83 216l14-3 2 38 6 32-11 2-9-31z")
  }`;
  return `<div class="silhouette-pair" role="img" aria-label="Muscle groups highlighted: ${escapeHtml(highlighted.map(muscleLabel).join(", ") || "none")}">
    <figure><svg viewBox="0 0 160 300" aria-hidden="true">${front}</svg><figcaption>Front</figcaption></figure>
    <figure><svg viewBox="0 0 160 300" aria-hidden="true">${back}</svg><figcaption>Back</figcaption></figure></div>`;
}

function workoutElapsedSeconds() {
  const started = Date.parse(appState?.session?.started_at || "");
  return Number.isFinite(started) ? Math.max(0, Math.floor((Date.now() - started) / 1000)) : 0;
}

function formatElapsed(seconds) {
  if (seconds < 60) return `${seconds} sec`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes} min`;
}

function estimateRm(weight, reps, targetReps = tenRmTarget) {
  const load = parseDecimal(weight);
  const count = Number(reps);
  const target = Number(targetReps);
  if (!Number.isFinite(load) || load <= 0 || !Number.isInteger(count) || count <= 0
    || !Number.isInteger(target) || target <= 0) return null;
  const equivalent = load * (1 + count / 30) / (1 + target / 30);
  return Math.round(equivalent * 100) / 100;
}

function startedAtLabel() {
  if (!appState?.session?.started_at) return "No active workout";
  const started = new Date(appState.session.started_at);
  if (Number.isNaN(started.getTime())) return "Workout in progress";
  const time = new Intl.DateTimeFormat("en-US", { hour: "numeric", minute: "2-digit" }).format(started);
  return `Workout started at ${time}`;
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
  updateDraftTenRm(exercise, draft);

  for (let nextIndex = draftIndex + 1; nextIndex < list.length; nextIndex += 1) {
    const next = list[nextIndex];
    if (next.userChanged[field] || next.autoFilled?.[field] !== previous || next[field] !== previous) continue;
    next[field] = value;
    next.autoFilled[field] = value;
    updateDraftTenRm(exercise, next);
    for (const input of screen.querySelectorAll("[data-draft-input]")) {
      if (input.dataset.clientId === next.client_id && input.dataset.field === field) input.value = value;
    }
  }
  persistDraftsFor(exercise);
}

function updateDraftTenRm(exercise, draft) {
  if (exercise?.target_unit === "seconds") return;
  const value = estimateRm(draft.weight_kg, draft.reps);
  const label = value === null ? "–" : `${formatWeight(value)} kg`;
  for (const field of screen.querySelectorAll("[data-tenrm-row]")) {
    if (field.dataset.tenrmRow === draft.client_id) field.textContent = label;
  }
}

function removeDraft(exercise, clientId) {
  const list = draftsFor(exercise);
  drafts.set(exercise.name, list.filter((draft) => draft.client_id !== clientId));
  persistDraftsFor(exercise);
}

function renderBrand() {
  return "";
}

function renderHome() {
  const home = appState?.home ?? {};
  const plan = home.active_plan;
  const planName = plan?.name || "New plan";
  const suppliedDays = home.week_days ?? [];
  const today = new Date();
  const monday = new Date(today);
  monday.setDate(today.getDate() - ((today.getDay() + 6) % 7));
  const weekDays = (suppliedDays.length ? suppliedDays : Array.from({ length: 7 }, (_item, index) => {
    const date = new Date(monday);
    date.setDate(monday.getDate() + index);
    return { date: localDateString(date), trained: false, today: date.toDateString() === today.toDateString() };
  })).map((day) => {
    const label = formatEnglishDate(day.date, { weekday: "short" }).slice(0, 2);
    const dayNumber = String(day.date || "").slice(8, 10).replace(/^0/, "");
    return `<div class="week-day${day.today ? " is-today" : ""}${day.trained ? " is-trained" : ""}">
      <span>${escapeHtml(label)}</span><strong>${escapeHtml(dayNumber)}</strong>
      <i aria-label="${day.trained ? "Workout completed" : "No workout"}"></i></div>`;
  }).join("");
  const days = (plan?.days ?? []).map((day, index) => {
    const exercises = day.exercises ?? [];
    const tiles = exercises.slice(0, 3).map((item) => renderExerciseTile(item)).join("");
    const more = exercises.length > 3 ? `<span class="exercise-tile more-tile">+${exercises.length - 3}</span>` : "";
    const isNext = home.next_day?.id === day.id;
    return `<button class="plan-day-row${isNext ? " is-next" : ""}" type="button"
      data-action="start-day" data-day-id="${day.id}" aria-label="Start ${escapeHtml(day.name)}">
      <span class="plan-day-number">${index + 1}</span>
      <strong class="home-day-name">${escapeHtml(day.name)}</strong>
      <span class="day-exercise-tiles">${tiles}${more}</span>
    </button>`;
  }).join("");
  const activeBar = appState?.session
    ? `<button class="active-workout-bar" type="button" data-action="continue-session" aria-label="Continue ${escapeHtml(appState.session.title || planName)}">
        <span aria-hidden="true">⚡</span><strong>${escapeHtml(appState.session.title || planName)}</strong>
        <span>${formatElapsed(workoutElapsedSeconds())}</span></button>`
    : "";
  const emptyDay = plan?.days?.length
    ? days
    : `<button class="plan-day-row empty-plan-card" type="button" data-action="start-free"><span class="plan-day-number">+</span>
        <strong class="home-day-name">Start free training</strong></button>`;
  const planMenu = planMenuOpen ? `<div class="home-plan-menu" role="menu" aria-label="Plan options">
      <button type="button" data-action="tab" data-tab="plans">Edit plans</button>
      <button type="button" data-action="create-plan">Create plan</button>
      ${appState?.session ? '<button type="button" data-action="finish">Finish workout</button>' : ""}</div>` : "";
  return `<header class="home-header"><span aria-hidden="true"></span>
    <button class="text-button" type="button" data-action="create-plan">Create</button></header>
    <section class="week-strip" aria-label="Training week">${weekDays}</section>
    <section class="home-plan-heading"><h1>${escapeHtml(planName)}</h1>
      <button class="plan-menu-button" type="button" data-action="open-plan-menu" aria-label="Plan options">•••</button>${planMenu}
      <button class="week-number" type="button" data-action="tab" data-tab="plans">Week ${Number(home.week_number) || 1}</button></section>
    <section class="exercise-list" aria-label="Plan days"><div class="plan-day-list-card"><div class="plan-day-list">${emptyDay}</div></div></section>
    <nav class="home-links" aria-label="More gym tools">
      <button class="text-button" type="button" data-action="tab" data-tab="plans">All plans</button>
      <button class="text-button" type="button" data-action="open-library">Exercises</button>
      <button class="text-button" type="button" data-action="help">Help</button>
    </nav>${activeBar}`;
}

function measureControl(field, value, mode, index, unit, clientId = "") {
  const inputMode = field === "weight_kg" ? "decimal" : "numeric";
  const ariaLabel = field === "weight_kg" ? "Weight in kilograms" : unit === "s" ? "Seconds" : "Repetitions";
  const fieldName = field === "weight_kg" ? "weight_kg" : "reps";
  const renderedValue = fieldName === "weight_kg" ? inputWeight(value) : String(value ?? "");
  const inputAttribute = mode === "edit"
    ? `data-edit-input data-field="${fieldName}"`
    : `data-draft-input data-client-id="${escapeHtml(clientId)}" data-index="${index}" data-field="${fieldName}"`;
  return `<input class="measure-control" ${inputAttribute} inputmode="${inputMode}"
    value="${escapeHtml(renderedValue)}" aria-label="${ariaLabel}" autocomplete="off">`;
}

function renderLoggedRow(set) {
  const pending = String(set.id).startsWith("pending:");
  const label = pending ? "Waiting to sync" : "Edit set";
  const timed = set.unit === "seconds";
  const amountLabel = timed ? "seconds" : "reps";
  const tenRm = timed ? "" : `<span class="tenrm-value">${formatWeight(estimateRm(set.weight_kg, set.reps))} kg</span>`;
  return `<div class="set-row set-row-complete${timed ? " is-timed" : ""}">
    <span class="set-number">${escapeHtml(set.set_index)}</span>
    <button class="logged-set" type="button" data-action="edit-set" data-set-id="${escapeHtml(set.id)}"
      aria-label="${label}: ${formatWeight(set.weight_kg)} kg, ${escapeHtml(set.reps)} ${amountLabel}">
      <strong>${formatWeight(set.weight_kg)} kg</strong><span>× ${escapeHtml(set.reps)}</span>
      ${pending ? '<span class="pending-label">Waiting to sync</span>' : ""}
    </button>${tenRm}<span aria-hidden="true" class="set-check">✓</span>
  </div>`;
}

function maxAmount(entry) {
  return entry && entry.target_unit === "seconds" ? 600 : 100;
}

function repUnit(entry) {
  return entry && entry.target_unit === "seconds" ? "s" : "reps";
}

function targetLabel(entry) {
  if (!entry || !entry.rep_min || !entry.rep_max) return "";
  const span = entry.rep_min === entry.rep_max ? `${entry.rep_min}` : `${entry.rep_min}–${entry.rep_max}`;
  return ` · Target ${span} ${repUnit(entry)}`;
}

function renderDraftRow(draft, index, setNumber, exercise) {
  const timed = exercise?.target_unit === "seconds";
  const unit = timed ? "s" : "reps";
  const value = estimateRm(draft.weight_kg, draft.reps);
  const tenRm = timed ? "" : `<span class="tenrm-value" data-tenrm-value="${value === null ? "–" : formatWeight(value)}" data-tenrm-row="${escapeHtml(draft.client_id)}">${value === null ? "–" : `${formatWeight(value)} kg`}</span>`;
  return `<div class="set-row${timed ? " is-timed" : ""}">
    <span class="set-number">${setNumber}</span>
    ${measureControl("weight_kg", draft.weight_kg, "draft", index, "kg", draft.client_id)}
    ${measureControl("reps", draft.reps, "draft", index, unit, draft.client_id)}
    ${tenRm}
    <button class="check-button" type="button" data-action="log-draft" data-client-id="${escapeHtml(draft.client_id)}" data-index="${index}"
      aria-label="Save set ${setNumber}">✓</button>
  </div>`;
}

function renderEditPanel(set, unit = "reps") {
  if (!editingSet || String(set.id) !== String(editingSet.id)) return "";
  return `<section class="edit-panel" aria-label="Edit set">
    <h2>Edit set</h2>
    <div class="edit-controls">
      ${measureControl("weight_kg", editingSet.weight_kg, "edit", "", "kg")}
      ${measureControl("reps", editingSet.reps, "edit", "", unit)}
    </div>
    <div class="edit-actions">
      <button class="button button-primary" type="button" data-action="save-edit">Save changes</button>
      <button class="button button-danger" type="button" data-action="delete-edit">Delete set</button>
      <button class="button" type="button" data-action="cancel-edit">Cancel</button>
    </div>
  </section>`;
}

function renderHistory(exercise, planName = "") {
  const items = histories[exercise.name];
  if (items === undefined) return '<p class="quiet-copy">Loading workout history …</p>';
  if (items.error) return `<p class="quiet-copy">${escapeHtml(items.error)}</p>`;
  if (!items.length) return '<p class="quiet-copy">No previous workouts.</p>';
  return `<div class="history-list">${items.map((item) => {
    const timed = item.sets.some((set) => set.unit === "seconds");
    const rows = item.sets.map((set, index) => `<div class="history-table-row${timed ? " is-timed" : ""}">
      <span>${escapeHtml(set.set_index ?? index + 1)}</span><span>${formatWeight(set.weight_kg)}</span>
      <span>${escapeHtml(set.reps)}${timed ? " s" : ""}</span>${timed ? "" : `<span>${formatWeight(estimateRm(set.weight_kg, set.reps))}</span>`}</div>`).join("");
    const heading = [formatOrdinalEnglishDate(item.date), item.title ? planDayName(item.title) : planName].filter(Boolean).join(" · ");
    return `<article class="last-workout-card"><header class="last-workout-head">${escapeHtml(heading)}</header>
      <div class="history-table-heading${timed ? " is-timed" : ""}"><span>#</span><span>KG</span><span>${timed ? "SECS" : "REPS"}</span>${timed ? "" : "<span>10RM</span>"}</div>${rows}</article>`;
  }).join("")}</div>`;
}

function renderEmptyTraining() {
  const planName = planDayName(appState?.session?.title || "Training");
  return `${renderHome()}<div class="sheet-backdrop" data-action="close-training" aria-hidden="true"></div>
    <section class="workout-sheet empty-training" role="dialog" aria-modal="true" aria-label="Training">
      <button class="sheet-grabber" type="button" data-action="close-training" aria-label="Close training sheet"><span></span></button>
      <header class="sheet-top"><button class="sheet-close" type="button" data-action="close-training">Close</button>
        <span class="sheet-plan-link">${escapeHtml(planName)}</span></header>
      <h1>Training</h1><p class="quiet-copy">There are no exercises in this workout yet.</p>
      <div class="empty-training-actions"><button class="button button-primary" type="button" data-action="open-session-exercise-picker">Add exercise</button>
        <button class="button button-danger" type="button" data-action="finish">Finish workout</button></div>
      ${sessionExercisePickerOpen ? renderExercisePicker("session") : ""}
    </section>`;
}

function renderExercise() {
  const exercise = currentExercise();
  if (!exercise) return renderHome();
  const exerciseIndex = appState.exercises.findIndex((entry) => entry.name === exercise.name);
  const openDrafts = draftsFor(exercise);
  const timed = exercise.target_unit === "seconds";
  const logged = exercise.logged.map((set) => renderLoggedRow({ ...set, unit: timed ? "seconds" : "reps" })).join("");
  const draftRows = openDrafts.map((draft, index) =>
    renderDraftRow(draft, index, exercise.logged.length + index + 1, exercise)
  ).join("");
  const editPanel = exercise.logged.map((set) => renderEditPanel(set, repUnit(exercise))).join("");
  const sorted = [...(appState.exercises ?? [])].sort((left, right) => left.sort_order - right.sort_order);
  const carousel = sorted.map((item) => renderExerciseTile(item, {
    completed: Boolean(item.logged?.length), selected: item.name === exercise.name, action: "select-exercise",
  })).join("") + (appState?.session
    ? '<button type="button" class="exercise-tile-button" data-action="open-session-exercise-picker" aria-label="Add exercise"><span class="exercise-tile add-exercise-tile">+</span></button>'
    : "");
  const planName = planDayName(appState?.session?.title || appState?.home?.active_plan?.name || "Training");
  const menu = exerciseMenuOpen ? `<div class="exercise-menu" role="menu" aria-label="Exercise options">
    <button type="button" data-action="skip-session-exercise" ${exercise.skipped ? "disabled" : ""}>Skip</button>
    <button type="button" data-action="replace-session-exercise">Replace</button>
    <button type="button" data-action="toggle-session-note">Note</button>
    <button type="button" data-action="remove-session-exercise" class="menu-danger">Remove</button>
    <button type="button" data-action="move-session-exercise" data-direction="-1" ${exerciseIndex <= 0 ? "disabled" : ""}>Move up</button>
    <button type="button" data-action="move-session-exercise" data-direction="1" ${exerciseIndex >= sorted.length - 1 ? "disabled" : ""}>Move down</button>
  </div>` : "";
  const noteEditor = exerciseNoteOpen && appState.session ? `<section class="exercise-note">
      <label for="session-exercise-note">Note for ${escapeHtml(exercise.name)}</label>
      <textarea id="session-exercise-note" data-session-note rows="2" maxlength="1000">${escapeHtml(exercise.note || "")}</textarea>
      <button class="text-button" type="button" data-action="save-session-note">Save note</button></section>` : "";
  const warmup = showWarmups && openDrafts.length ? `<div class="warmup-row"><span>WU</span><span>Warm-up set</span>
      <span>${formatWeight(parseDecimal(openDrafts[0].weight_kg) * 0.5)} kg</span></div>` : "";
  const targetControls = tenRmEditOpen && !timed
    ? `<label class="tenrm-target-field">Target reps<input data-tenrm-target type="number" min="1" max="30" value="${tenRmTarget}" aria-label="10RM target repetitions"></label>`
    : "";
  const tenRmHeading = timed ? "" : `<span class="tenrm-heading"><button type="button" data-action="edit-tenrm-target" aria-label="Edit 10RM target">${tenRmTarget}RM ✎</button>${targetControls}</span>`;
  const amountHeading = timed ? "SECS" : "REPS";
  const target = timed
    ? `${exercise.rep_min || 30} sec`
    : `${exercise.rep_max || exercise.rep_min || 6} reps`;
  const repTargetControls = repTargetEditorOpen ? `<div class="rep-target-editor">
    <label>${timed ? "Seconds" : "Reps"} min<input data-rep-target="rep_min" type="number" min="1" max="${timed ? 600 : 100}" value="${exercise.rep_min || (timed ? 30 : 6)}"></label>
    <label>${timed ? "Seconds" : "Reps"} max<input data-rep-target="rep_max" type="number" min="1" max="${timed ? 600 : 100}" value="${exercise.rep_max || (timed ? 40 : 10)}"></label>
    <button type="button" class="text-button" data-action="save-session-target">Save</button></div>` : "";
  const completedLabel = exercise.logged.length === 1 ? "1 set completed" : `${exercise.logged.length} sets completed`;
  const setCount = exercise.target_sets || exercise.prefill.length || 1;
  return `${renderHome()}<div class="sheet-backdrop" data-action="close-training" aria-hidden="true"></div>
    <section class="workout-sheet" role="dialog" aria-modal="true" aria-label="Training exercise">
      <button class="sheet-grabber" type="button" data-action="close-training" aria-label="Close training sheet"><span></span></button>
      <header class="sheet-top"><button class="sheet-close" type="button" data-action="close-training" aria-label="Close">Close</button>
        <button class="sheet-plan-link" type="button" data-action="tab" data-tab="plans">${escapeHtml(planName)} 🔧</button></header>
      <div class="exercise-carousel" aria-label="Exercises in this workout">${carousel}</div>
      <div class="exercise-title-row"><h1>${escapeHtml(exercise.name)}</h1><span class="sr-only">${completedLabel}</span>
        <button class="plan-menu-button" type="button" data-action="open-exercise-menu" aria-label="Exercise options">•••</button>${menu}</div>
      <p class="exercise-subtitle"><span>${escapeHtml(exercise.equipment || "Machine")} ⇄</span>
        <span>${target}</span><span class="sr-only">${escapeHtml(targetLabel(exercise))}</span>
        <button type="button" data-action="edit-rep-target" aria-label="Edit target repetitions">✎</button></p>${repTargetControls}
      <button class="warmup-toggle${showWarmups ? " is-active" : ""}" type="button" data-action="toggle-warmups">1 warm-up set</button>
      <div class="set-heading${timed ? " is-timed" : ""}" aria-hidden="true"><span>#</span><span>KG</span><span>${amountHeading}</span>${tenRmHeading}<span></span></div>
      <section class="set-list" aria-label="Sets">${warmup}${logged}${draftRows}</section>
      ${editPanel}${noteEditor}
      <div class="sheet-footer"><button class="text-button" type="button" data-action="open-help">Help</button>
        <button class="button add-set-row" type="button" data-action="add-draft">+ Set</button>
        ${appState?.session ? '<button class="text-button finish-session-link" type="button" data-action="finish">Finish</button>' : ""}</div>
      <div class="exercise-history"><strong>Last workout · ${escapeHtml(planName)}</strong>${renderHistory(exercise, planName)}</div>
      ${sessionExercisePickerOpen ? renderExercisePicker(sessionExercisePickerKind) : ""}
    </section>`;
}

function renderLogin() {
  return `${renderBrand()}
    <section class="auth-panel">
      <h1>Training</h1>
      <p class="quiet-copy">Sign in with your Mantis access key.</p>
      <form id="login-form">
        <label for="access-token">Access key</label>
        <input id="access-token" name="token" type="password" autocomplete="off"
          autocapitalize="off" spellcheck="false" required>
        <button class="button button-primary" type="submit">Sign in</button>
        <p class="inline-error">${escapeHtml(loginError)}</p>
      </form>
    </section>`;
}

function renderSummary() {
  if (!finishSummary) return renderHome();
  const setLabel = finishSummary.sets === 1 ? "set" : "sets";
  return `${renderBrand()}
    <section class="summary-panel">
      <h1>Workout complete</h1>
      <p class="quiet-copy">Your workout has been saved.</p>
      <div class="summary-line"><span>Duration</span><strong>${finishSummary.duration_min} min</strong></div>
      <div class="summary-line"><span>${setLabel}</span><strong>${finishSummary.sets}</strong></div>
      <div class="summary-line"><span>Volume</span><strong>${Number(finishSummary.volume_kg).toLocaleString("en-US", { maximumFractionDigits: 2 })} kg</strong></div>
      <div class="summary-actions"><button class="button button-primary" type="button" data-action="back-home">Back to training</button></div>
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
  const icons = {
    training: '<svg viewBox="0 0 24 24"><path d="M4 9v6M7 6v12M17 6v12M20 9v6M7 12h10M4 12H2m20 0h-2"/></svg>',
    history: '<svg viewBox="0 0 24 24"><path d="M4 19V5m0 14h17M7 15l4-4 3 2 6-7"/><circle cx="20" cy="6" r="1"/></svg>',
    profile: '<svg viewBox="0 0 24 24"><circle cx="12" cy="8" r="3.5"/><path d="M5 20c.6-4 3-6 7-6s6.4 2 7 6"/></svg>',
    plans: '<svg viewBox="0 0 24 24"><rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3m-4 4v3"/></svg>',
  };
  const tabs = [["training", "Training"], ["history", "History"], ["profile", "Profile"], ["plans", "Settings"]];
  const selected = activeTab === "exercises" ? "training" : activeTab;
  tabBar.innerHTML = tabs.map(([id, label]) => `<button type="button" class="tab-item${selected === id ? " is-active" : ""}"
    data-action="tab" data-tab="${id}" aria-label="${label}" aria-current="${selected === id ? "page" : "false"}">
    ${icons[id]}</button>`).join("");
}

function renderWeeklyChart() {
  const weekly = overviewData?.weekly ?? [];
  if (!weekly.length) return '<p class="quiet-copy">No weekly training data yet.</p>';
  const values = weekly.map((week) => Number(chartMetric === "workouts" ? week.workout_count : week.volume_kg) || 0);
  const maximum = Math.max(...values, 1);
  const width = 340;
  const height = 160;
  const base = 128;
  const step = (width - 28) / weekly.length;
  const barWidth = Math.max(5, Math.min(34, step * 0.52));
  const bars = values.map((value, index) => {
    const barHeight = value ? Math.max(5, value / maximum * 94) : 3;
    const x = 14 + index * step + (step - barWidth) / 2;
    const y = base - barHeight;
    const label = formatEnglishDate(weekly[index].week_start, { month: "short", day: "numeric" });
    const amount = chartMetric === "workouts" ? `${value} ${value === 1 ? "workout" : "workouts"}` : `${formatWeight(value)} kg`;
    return `<g><rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barWidth.toFixed(1)}" height="${barHeight.toFixed(1)}" rx="${Math.min(6, barWidth / 2)}"/><title>${escapeHtml(label)} · ${amount}</title></g>`;
  }).join("");
  const labels = weekly.map((week, index) => index === 0 || index === weekly.length - 1 || index % Math.ceil(weekly.length / 4) === 0
    ? `<text x="${(14 + index * step + step / 2).toFixed(1)}" y="151" text-anchor="middle">${escapeHtml(formatEnglishDate(week.week_start, { month: "short", day: "numeric" }))}</text>` : "").join("");
  return `<svg class="week-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="Weekly ${chartMetric} chart"><path class="chart-grid" d="M14 ${base}H326 M14 81H326 M14 34H326"/>${bars}${labels}</svg>`;
}

function renderWorkoutHistory() {
  if (workoutDetail) return renderWorkoutDetail();
  if (workoutHistory === null) return `${renderBrand()}<div class="loading-panel">Loading training history …</div>`;
  const rows = workoutHistory.length ? workoutHistory.map((workout) => {
    const previews = (workout.exercises ?? []).slice(0, 3).map((exercise) => renderExerciseTile(exercise, { completed: true })).join("");
    const moreCount = Math.max(0, Number(workout.exercise_count || 0) - (workout.exercises ?? []).length);
    return `<button type="button" class="workout-card" data-action="open-workout" data-workout-id="${workout.id}">
      <span class="workout-copy"><strong>${escapeHtml(planDayName(workout.title))}</strong><small>${escapeHtml(formatOrdinalEnglishDate(workout.date))}</small></span>
      <span class="workout-preview">${previews}${moreCount ? `<span class="exercise-tile more-tile">+${moreCount}</span>` : ""}</span></button>`;
  }).join("") : '<p class="quiet-copy">No completed workouts yet.</p>';
  const workoutCount = Number(overviewData?.workout_count ?? workoutHistory.length);
  const countLabel = `${workoutCount} ${workoutCount === 1 ? "workout" : "workouts"}`;
  return `<section class="history-page sheet-dismissable" role="dialog" aria-modal="true" aria-label="Training history">
    <header class="history-top"><button class="sheet-close" type="button" data-action="close-history">Close</button>
      <button class="text-button" type="button" data-action="share-history">Share</button></header>
    <h1>Training</h1><p class="subhead">${countLabel}</p>
    <section class="history-chart-card" aria-label="Training chart">
      <div class="chart-filter-row" aria-label="Date range">${[[4,"1mo"],[12,"3mo"],[26,"6mo"]].map(([weeks,label]) =>
        `<button class="chart-filter${overviewWeeks === weeks ? " is-active" : ""}" type="button" data-action="set-overview-weeks" data-weeks="${weeks}" aria-pressed="${overviewWeeks === weeks}">${label}</button>`).join("")}</div>
      ${overviewError ? `<p class="quiet-copy">${escapeHtml(overviewError)}</p>` : ""}
      ${renderWeeklyChart()}
      <div class="chart-filter-row chart-metric-row" aria-label="Chart measure">${[["workouts","Workouts"],["volume","Volume"]].map(([metric,label]) =>
        `<button class="chart-filter${chartMetric === metric ? " is-active" : ""}" type="button" data-action="set-chart-metric" data-metric="${metric}" aria-pressed="${chartMetric === metric}">${label}</button>`).join("")}</div>
    </section>
    <section class="workout-list-card" aria-label="Training sessions"><div class="workout-list">${rows}</div></section>
  </section>`;
}

function renderHistorySetDialog() {
  if (!historyEditSet) return "";
  const set = workoutDetail?.sets.find((item) => item.id === historyEditSet.id);
  if (!set) return "";
  const timed = set.unit === "seconds";
  const amountLabel = timed ? "SECS" : "REPS";
  return `<div class="modal-backdrop history-set-backdrop" data-action="cancel-history-edit" aria-hidden="true"></div>
    <section class="history-set-dialog" role="dialog" aria-modal="true" aria-label="Edit set">
      <header><h2>Edit set ${escapeHtml(set.set_index)}</h2><button class="sheet-close" type="button" data-action="cancel-history-edit">Close</button></header>
      <div class="history-set-fields"><label>KG<input data-history-edit data-field="weight_kg" type="text"
          value="${escapeHtml(historyEditSet.weight_kg)}" inputmode="decimal"></label>
        <label>${amountLabel}<input data-history-edit data-field="reps" type="number" step="1"
          value="${escapeHtml(historyEditSet.reps)}" inputmode="numeric"></label></div>
      <div class="history-set-actions"><button class="button button-primary" type="button" data-action="save-history-set">Save changes</button>
        <button class="text-button danger-link" type="button" data-action="delete-history-set" data-set-id="${set.id}">Delete set</button></div>
    </section>`;
}

function renderWorkoutDetail() {
  const workout = workoutDetail;
  if (!workout) return renderWorkoutHistory();
  const groups = new Map();
  for (const set of workout.sets) {
    const key = String(set.exercise_id ?? set.exercise);
    if (!groups.has(key)) groups.set(key, { name: set.exercise, sets: [] });
    groups.get(key).sets.push(set);
  }
  const exercises = [...groups.values()].map((group) => {
    const timed = group.sets.some((set) => set.unit === "seconds");
    const rows = group.sets.map((set) => `<button class="workout-set-row" type="button" data-action="edit-history-set"
      data-set-id="${set.id}" aria-label="Edit set ${set.set_index}: ${formatWeight(set.weight_kg)} kilograms, ${set.reps} ${timed ? "seconds" : "reps"}">
      <span>${escapeHtml(set.set_index)}</span><span>${formatWeight(set.weight_kg)}</span><span>${escapeHtml(set.reps)}</span></button>`).join("");
    return `<article class="workout-exercise-card"><header class="workout-exercise-heading">
        <span class="exercise-library-tile" aria-hidden="true">${escapeHtml(exerciseInitials({ name: group.name }))}</span>
        <strong>${escapeHtml(group.name)}</strong></header>
      <div class="history-table-heading${timed ? " is-timed" : ""}" aria-hidden="true"><span>#</span><span>KG</span><span>${timed ? "SECS" : "REPS"}</span></div>
      <div class="workout-set-list">${rows}</div></article>`;
  }).join("");
  return `<section class="history-page workout-detail-page sheet-dismissable" role="dialog" aria-modal="true" aria-label="Workout details"><header class="history-top"><button class="sheet-close" type="button" data-action="history-back">Close</button>
    <button class="text-button" type="button" data-action="share-workout">Share</button></header>
    <h1>${escapeHtml(planDayName(workout.title))}</h1><p class="subhead">${escapeHtml(formatOrdinalEnglishDate(workout.date))} · ${workout.duration_min} min</p>
    <section class="workout-exercise-list" aria-label="Exercises">${exercises || '<p class="quiet-copy">No sets saved.</p>'}</section>
    <button class="text-button danger-link workout-delete" type="button" data-action="delete-workout">Delete workout</button>
    ${renderHistorySetDialog()}</section>`;
}

function renderE1rmChart(history, name) {
  const points = [...(history ?? [])]
    .filter((item) => item.e1rm_kg !== null && item.e1rm_kg !== undefined
      && Number.isFinite(Number(item.e1rm_kg)))
    .reverse();
  if (!points.length) return '<p class="quiet-copy">No weighted 1RM history for this exercise yet.</p>';
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
  return `<div class="chart-wrap"><svg class="e1rm-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="Estimated 1RM for ${escapeHtml(name)}">
    <path class="chart-grid" d="M${pad} ${pad}H${width - pad} M${pad} ${height - pad}H${width - pad}"/>
    <path class="chart-line" d="${path}"/>${circles}</svg>
    <div class="chart-labels"><span>${escapeHtml(formatDate(points[0].date))}</span><span>${formatWeight(values.at(-1))} kg e1RM</span><span>${escapeHtml(formatDate(points.at(-1).date))}</span></div></div>`;
}

function renderExerciseLibrary() {
  if (exerciseDetail) {
    const history = exerciseDetail.history ?? [];
    const historyUnit = exerciseDetail.unit === "min"
      || history.some((item) => item.sets?.some((set) => set.unit === "seconds"))
      ? "s" : "reps";
    const historyRows = history.map((item) => {
      const bestSet = item.best_set
        ? `<small class="best-set">Best set: ${formatWeight(item.best_set.weight_kg)} kg × ${item.best_set.reps} ${setUnitLabel(item.best_set)}</small>`
        : "";
      return `<article class="exercise-history-card"><div class="section-heading-row">
        <strong>${escapeHtml(formatEnglishDate(item.date))}</strong><span>${item.e1rm_kg === null ? "" : `${formatWeight(item.e1rm_kg)} kg e1RM`}</span></div>
        ${bestSet}<p>${item.sets.map((set) => `${formatWeight(set.weight_kg)}×${set.reps} ${setUnitLabel(set)}`).join(" · ")}</p></article>`;
    }).join("");
    const muscles = mappedMuscles(exerciseDetail.muscle);
    const similarCount = (exerciseDirectory ?? []).filter((item) => item.id !== exerciseDetail.id
      && mappedMuscles(item.muscle).some((muscle) => muscles.includes(muscle))).length;
    const equipment = equipmentLabel(exerciseDetail.equipment);
    const type = exerciseDetail.category || exerciseDetail.exercise_type || "Strength";
    const menu = exerciseDetailMenuOpen ? `<div class="exercise-menu detail-menu" role="menu">
      <button type="button" data-action="exercise-detail-tab" data-detail-tab="info">Info</button>
      <button type="button" data-action="exercise-detail-tab" data-detail-tab="instructions">Instructions</button>
      <button type="button" data-action="exercise-detail-tab" data-detail-tab="history">History</button></div>` : "";
    const lastTraining = history[0] ? `<article class="exercise-history-card"><div class="section-heading-row"><strong>Last training · ${escapeHtml(formatEnglishDate(history[0].date))}</strong></div>
      <p>${history[0].sets.map((set) => `${formatWeight(set.weight_kg)}×${set.reps} ${setUnitLabel(set)}`).join(" · ")}</p></article>` : "";
    const information = `<section class="exercise-info-grid">
      <article class="exercise-property-card"><span>Equipment</span><strong>${escapeHtml(equipment)}</strong></article>
      <article class="exercise-property-card"><span>Type</span><strong>${escapeHtml(type)}</strong></article>
      <article class="exercise-property-card"><span>Metrics</span><strong>Weight + reps</strong></article>
      <article class="exercise-property-card"><span>Weight</span><strong>kg</strong></article>
      <article class="exercise-property-card"><span>Bodyweight</span><strong>0%</strong></article></section>
      <section class="muscle-card"><div class="section-heading-row"><div><span class="eyebrow">MUSCLES</span>
        <strong>${escapeHtml(mappedMuscles(exerciseDetail.muscle).map(muscleLabel).join(", ") || "Not set")}</strong></div>
        <button class="text-button" type="button" data-action="similar-exercises">Similar (${similarCount})</button></div>
        ${muscleSilhouette(muscles)}
        <div class="muscle-labels">${(exerciseMusclesExpanded ? muscles : muscles.slice(0, 3)).map((muscle) => `<span>${escapeHtml(muscleLabel(muscle))}</span>`).join("") || '<span>Muscle group unknown</span>'}</div>
        ${muscles.length > 3 ? `<button class="text-button" type="button" data-action="toggle-muscles">${exerciseMusclesExpanded ? "Show less" : `Show all (${muscles.length})`}</button>` : ""}</section>
      <article class="exercise-property-card rep-property"><span>Rep range</span><strong>${exerciseDetail.rep_min}–${exerciseDetail.rep_max} ${historyUnit}</strong></article>
      <section class="exercise-chart-card"><div class="section-heading-row"><strong>Estimated 1RM</strong><span>kg</span></div>${renderE1rmChart(history, exerciseDetail.name)}</section>${lastTraining}`;
    const instructions = `<section class="instructions-card"><label for="exercise-instructions">Instructions</label>
      <textarea id="exercise-instructions" data-exercise-instructions maxlength="3000" rows="7">${escapeHtml(exerciseInstructionsDraft)}</textarea>
      <button class="button button-primary" type="button" data-action="save-exercise-instructions">Save instructions</button></section>`;
    const historyPanel = `<section class="exercise-detail-history"><h2>History</h2>${historyRows || '<p class="quiet-copy">No workouts for this exercise yet.</p>'}</section>`;
    const panels = { info: information, instructions, history: historyPanel };
    return `<section class="exercise-detail-page sheet-dismissable" role="dialog" aria-modal="true" aria-label="Exercise details">
      <header class="history-top"><button class="sheet-close" type="button" data-action="library-back">Close</button>
        <button class="text-button" type="button" data-action="share-exercise">Share</button></header>
      <div class="exercise-detail-avatar">${escapeHtml(exerciseInitials(exerciseDetail))}</div>
      <nav class="detail-segments" aria-label="Exercise detail sections">${[["info","Info"],["instructions","Instructions"],["history","History"]].map(([tab,label]) =>
        `<button class="detail-segment${exerciseDetailTab === tab ? " is-active" : ""}" type="button" data-action="exercise-detail-tab" data-detail-tab="${tab}" aria-pressed="${exerciseDetailTab === tab}">${label}</button>`).join("")}</nav>
      <div class="exercise-title-row detail-title-row"><h1>${escapeHtml(exerciseDetail.name)}</h1>
        <button class="plan-menu-button" type="button" data-action="exercise-detail-menu" aria-label="Exercise options">•••</button>${menu}</div>
      ${panels[exerciseDetailTab] ?? information}
    </section>`;
  }
  const entries = exerciseDirectory ?? [];
  const muscles = [...new Set(entries.map((item) => item.muscle).filter(Boolean))].sort();
  const filterButtons = [["", "All"], ...muscles.map((muscle) => [muscle, capitalize(muscleLabel(mappedMuscles(muscle)[0]) || muscle)])]
    .map(([muscle, label]) => `<button class="muscle-filter${exerciseMuscle === muscle ? " is-active" : ""}" type="button"
      data-action="filter-library-muscle" data-muscle="${escapeHtml(muscle)}" aria-pressed="${exerciseMuscle === muscle}">${escapeHtml(label)}</button>`)
    .join("");
  const rows = entries.map((item) => `<button class="library-row" type="button" data-action="open-library-exercise" data-exercise-id="${item.id}">
    <span class="exercise-library-tile" aria-hidden="true">${escapeHtml(exerciseInitials(item))}</span>
    <span class="library-row-copy"><strong>${escapeHtml(item.name)}</strong><small>${item.equipment ? escapeHtml(equipmentLabel(item.equipment)) : ""}</small></span>
    <span class="library-row-chevron" aria-hidden="true">›</span></button>`).join("");
  const form = showNewExerciseForm ? `<form id="new-exercise-form" class="inline-form">
    <h2>New exercise</h2><label>Name<input name="name" required maxlength="100"></label>
    <label>Muscle group<input name="muscle" maxlength="60"></label>
    <label>Equipment<select name="equipment"><option value="">Not specified</option><option value="barbell">Barbell</option><option value="dumbbell">Dumbbell</option><option value="machine">Machine</option><option value="cable">Cable</option><option value="bodyweight">Bodyweight</option><option value="other">Other</option></select></label>
    <div class="form-pair"><label>Min reps<input name="rep_min" type="number" min="1" max="100" value="6"></label><label>Max reps<input name="rep_max" type="number" min="1" max="100" value="10"></label></div>
    <button class="button button-primary" type="submit">Save exercise</button><button class="text-button" type="button" data-action="toggle-new-exercise">Cancel</button></form>` : "";
  const emptyCopy = exerciseDirectory === null
    ? "Loading exercises …"
    : exerciseSearch || exerciseMuscle ? "No matching exercises." : "No exercises yet.";
  return `<section class="exercise-library-page sheet-dismissable" role="dialog" aria-modal="true" aria-label="Exercises">
    <header class="history-top"><button class="sheet-close" type="button" data-action="close-library">Close</button>
      <button class="text-button library-add-button" type="button" data-action="toggle-new-exercise" aria-label="Add exercise">＋</button></header>
    <h1>Exercises</h1>
    <label class="library-search"><span class="sr-only">Search exercises</span><input id="library-search" type="search"
      value="${escapeHtml(exerciseSearch)}" placeholder="Search exercises" autocomplete="off"></label>
    <div class="muscle-filter-row" role="group" aria-label="Filter by muscle">${filterButtons}</div>
    ${form}
    <section class="exercise-library-card"><div class="library-list">${rows || `<p class="quiet-copy library-empty">${emptyCopy}</p>`}</div></section>
  </section>`;
}

function renderExercisePicker(kind) {
  const rows = (exerciseDirectory ?? []).map((item) => `<button class="picker-row" type="button" data-action="pick-exercise"
    data-kind="${kind}" data-exercise-id="${item.id}"><span>${escapeHtml(item.name)}</span><small>${escapeHtml(mappedMuscles(item.muscle).map(muscleLabel).join(", "))}</small></button>`).join("");
  const heading = kind === "replace" ? "Replace exercise" : "Add exercise";
  return `<div class="modal-backdrop"><section class="picker-modal exercise-picker" role="dialog" aria-modal="true" aria-label="Choose exercise">
    <div class="section-heading-row"><h2>${heading}</h2><button class="small-button" type="button" data-action="close-exercise-picker">Close</button></div>
    <label class="search-field">Search<input id="picker-search" value="${escapeHtml(exerciseSearch)}" placeholder="Search exercises"></label>
    <div class="picker-list">${rows || '<p class="quiet-copy">Loading exercises …</p>'}</div>
  </section></div>`;
}

function renderPlanTab() {
  if (planList === null) return `${renderBrand()}<div class="loading-panel">Loading plans …</div>`;
  const active = planList.find((plan) => plan.active);
  const selected = planList.find((plan) => plan.id === selectedPlanId) ?? active ?? planList[0];
  const choices = planList.map((plan) => `<button class="plan-choice${selected?.id === plan.id ? " is-selected" : ""}" type="button"
    data-action="select-plan" data-plan-id="${plan.id}"><span>${escapeHtml(plan.name)}</span>${plan.active ? '<small>Active</small>' : ""}</button>`).join("");
  const planForm = `<form id="new-plan-form" class="inline-form compact-form"><label>New plan<input name="name" required maxlength="100" placeholder="e.g. Upper / Lower"></label>
    <label>Start date<input name="start_date" type="date" value="${localDateString()}"></label>
    <button class="button" type="submit">Create plan</button><p class="inline-error">${escapeHtml(newPlanError)}</p></form>`;
  if (!selected) return `${renderBrand()}<h1>Plans</h1><p class="quiet-copy">No plans yet.</p>${planForm}`;
  const days = selected.days.map((day, dayIndex) => `<article class="plan-editor-day">
    <div class="section-heading-row"><label class="plan-day-name">Day<input data-day-name="${day.id}" value="${escapeHtml(day.name)}" maxlength="80"></label>
      <div class="editor-tools"><button class="small-button" type="button" data-action="save-day-name" data-day-id="${day.id}">Save</button>
        <button class="small-button" type="button" data-action="move-plan-day" data-plan-id="${selected.id}" data-day-index="${dayIndex}" data-direction="-1" ${dayIndex === 0 ? "disabled" : ""}>↑</button>
        <button class="small-button" type="button" data-action="move-plan-day" data-plan-id="${selected.id}" data-day-index="${dayIndex}" data-direction="1" ${dayIndex === selected.days.length - 1 ? "disabled" : ""}>↓</button>
        <button class="small-button" type="button" data-action="delete-plan-day" data-day-id="${day.id}">Delete</button></div></div>
      ${(day.exercises ?? []).map((item, itemIndex) => `<div class="plan-exercise-editor">
        <strong>${escapeHtml(item.name)}</strong><div class="plan-exercise-fields">
          <label>Sets<input type="number" min="1" max="20" data-plan-value="target_sets" data-entry-id="${item.id}" value="${item.target_sets}"></label>
          <label>${item.target_unit === "seconds" ? "sec" : "reps"} min<input type="number" min="1" max="${item.target_unit === "seconds" ? 600 : 100}" data-plan-value="rep_min" data-entry-id="${item.id}" value="${item.rep_min}"></label>
          <label>${item.target_unit === "seconds" ? "sec" : "reps"} max<input type="number" min="1" max="${item.target_unit === "seconds" ? 600 : 100}" data-plan-value="rep_max" data-entry-id="${item.id}" value="${item.rep_max}"></label>
          <label>Rest<input type="number" min="0" max="900" step="30" data-plan-value="rest_s" data-entry-id="${item.id}" value="${item.rest_s}"></label></div>
        <div class="editor-tools"><button class="small-button" type="button" data-action="save-plan-exercise" data-entry-id="${item.id}">Save</button>
          <button class="small-button" type="button" data-action="move-plan-exercise" data-day-id="${day.id}" data-entry-id="${item.id}" data-entry-index="${itemIndex}" data-direction="-1" ${itemIndex === 0 ? "disabled" : ""}>↑</button>
          <button class="small-button" type="button" data-action="move-plan-exercise" data-day-id="${day.id}" data-entry-id="${item.id}" data-entry-index="${itemIndex}" data-direction="1" ${itemIndex === day.exercises.length - 1 ? "disabled" : ""}>↓</button>
          <button class="small-button" type="button" data-action="delete-plan-exercise" data-entry-id="${item.id}">Remove</button></div></div>`).join("")}
      <button class="text-button" type="button" data-action="open-plan-exercise-picker" data-day-id="${day.id}">+ Add exercise</button></article>`).join("");
  return `${renderBrand()}<h1>Edit plan</h1><section class="plan-choice-list">${choices}</section>
    <section class="plan-editor-header"><div><span class="eyebrow">${selected.active ? "ACTIVE PLAN" : "PLAN"}</span>
      <input class="plan-title-input" data-plan-name value="${escapeHtml(selected.name)}" maxlength="100" aria-label="Plan name">
      <small>Started ${escapeHtml(formatDate(selected.start_date))}</small></div>
      <button class="small-button" type="button" data-action="save-plan-name" data-plan-id="${selected.id}">Save name</button>
      ${selected.active ? "" : `<button class="button button-primary" type="button" data-action="activate-plan" data-plan-id="${selected.id}">Activate</button>`}</section>
    <form id="add-day-form" class="inline-form compact-form" data-plan-id="${selected.id}"><label>New workout day<input name="name" required maxlength="80" placeholder="e.g. Upper C"></label><button class="button" type="submit">Add day</button></form>
    ${days}${planForm}${planExercisePickerDayId !== null ? renderExercisePicker("plan") : ""}`;
}

function renderProfile() {
  if (!overviewData && !overviewError) return `${renderBrand()}<div class="loading-panel">Loading profile …</div>`;
  const measurement = overviewData?.latest_measurement ?? {};
  const weight = measurement.weight_kg === null || measurement.weight_kg === undefined
    ? "—" : `${formatWeight(measurement.weight_kg)} kg`;
  const bodyFat = measurement.body_fat === null || measurement.body_fat === undefined
    ? "—" : `${formatWeight(measurement.body_fat)}%`;
  const count = Number(overviewData?.workout_count || 0);
  const streak = Number(overviewData?.streak_weeks || 0);
  const home = appState?.home ?? {};
  const plan = home.active_plan;
  const measurementForm = measurementFormOpen ? `<form class="measurement-form" id="new-measurement-form">
    <label>Bodyweight<input name="weight_kg" type="number" min="20" max="500" step="0.1" inputmode="decimal" placeholder="kg"></label>
    <label>Body fat percentage<input name="body_fat" type="number" min="0" max="100" step="0.1" inputmode="decimal" placeholder="%"></label>
    <label>Date<input name="date" type="date" value="${localDateString()}"></label>
    <button class="button button-primary" type="submit">Save measurement</button></form>` : "";
  const activeBar = appState?.session ? `<button class="active-workout-bar" type="button" data-action="continue-session" aria-label="Continue workout">
    <span aria-hidden="true">⚡</span><strong>${escapeHtml(appState.session.title || "Training")}</strong><span>${formatElapsed(workoutElapsedSeconds())}</span></button>` : "";
  return `<section class="profile-page">
    <header class="profile-top"><span></span><button class="text-button" type="button" data-action="profile-settings">Settings</button></header>
    ${overviewError ? `<p class="quiet-copy">${escapeHtml(overviewError)}</p>` : ""}
    <div class="profile-badge" aria-hidden="true"><svg viewBox="0 0 96 96"><path d="M48 5 83 18v28c0 23-14 38-35 47C27 84 13 69 13 46V18z"/><path d="m31 49 11 11 24-27"/></svg></div>
    <section class="profile-stats">
      <article class="profile-stat-card"><span class="stat-symbol"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 9v6m3-9v12m12-12v12m3-9v6M6 12h12M3 12H1m22 0h-2"/></svg></span><strong>${count}</strong><span>${count === 1 ? "workout" : "workouts"}</span></article>
      <article class="profile-stat-card"><span class="stat-symbol"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 22a7 7 0 0 0 7-7c0-3-1.8-5.2-4.6-8.2.1 2.2-.6 3.5-1.8 4.4C12.4 7.8 10.6 4.9 8 2 8.2 6.6 5 9.2 5 15a7 7 0 0 0 7 7Z"/><path d="M12 22a3 3 0 0 0 3-3c0-1.3-.8-2.3-2.3-3.7-.1 1.2-.5 1.7-1.2 2.2-.2-1.2-.9-2.1-1.7-3.1-.1 2-1 2.8-1 4.6a3.2 3.2 0 0 0 3.2 3Z"/></svg></span><strong>${streak}w</strong><span>streak</span></article>
    </section>
    <button class="profile-gym-card" type="button" data-action="profile-settings"><span><strong>Gym</strong><small>${escapeHtml(plan?.name || "Training plans and settings")}</small></span><span>›</span></button>
    <section class="measurement-card"><header><div><span class="eyebrow">BODY</span><h2>Measurements</h2></div>
      <button class="add-circle" type="button" data-action="toggle-measurement-form" aria-label="Add measurement">+</button></header>
      <div class="measurement-values"><div><span>Bodyweight</span><strong>${weight}</strong></div><div><span>Body fat percentage</span><strong>${bodyFat}</strong></div></div>
      ${measurement.date ? `<small class="measurement-date">${escapeHtml(formatEnglishDate(measurement.date))}</small>` : ""}
      ${measurementForm}
    </section>
    <nav class="profile-links" aria-label="Profile links"><button class="text-button" type="button" data-action="toggle-measurement-form">Add measurement</button>
      <button class="text-button" type="button" data-action="profile-settings">Basic info</button>
      <button class="text-button" type="button" data-action="help">Help</button></nav>${activeBar}
  </section>`;
}

function render() {
  if (!appState?.session || (appState.exercises ?? []).length) trainingSheetClosed = false;
  if (screenMode === "login") screen.innerHTML = renderLogin();
  else if (screenMode === "loading") screen.innerHTML = `${renderBrand()}<div class="loading-panel">Loading workout …</div>`;
  else if (screenMode === "error") screen.innerHTML = `${renderBrand()}
    <section class="error-panel"><h1>Connection lost</h1>
      <p class="quiet-copy">${escapeHtml(loadError)} Your pending sets stay on this device.</p>
      <button class="button button-primary" type="button" data-action="retry">Try again</button>
    </section>`;
  else if (screenMode === "summary") screen.innerHTML = renderSummary();
  else if (activeTab === "training" && selectedExercise) screen.innerHTML = renderExercise();
  else if (activeTab === "training" && appState?.session && !trainingSheetClosed && !(appState.exercises ?? []).length) screen.innerHTML = renderEmptyTraining();
  else if (activeTab === "training") screen.innerHTML = renderHome();
  else if (activeTab === "history") screen.innerHTML = renderWorkoutHistory();
  else if (activeTab === "profile") screen.innerHTML = renderProfile();
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
    ? `<div class="sync-banner">${count} ${count === 1 ? "set" : "sets"} waiting to sync.</div>`
    : "";
}

function renderNotice() {
  noticeHost.innerHTML = notice ? `<div class="notice-banner">${escapeHtml(notice)}</div>` : "";
}

function timerMarkup(seconds) {
  const minutes = String(Math.floor(seconds / 60)).padStart(2, "0");
  const remainder = String(seconds % 60).padStart(2, "0");
  return `<div class="timer-dock" role="group" aria-label="Rest timer">
    <span class="timer-copy"><span>Rest</span><strong id="timer-value">${minutes}:${remainder}</strong></span>
    <button class="small-button" type="button" data-action="timer-minus" aria-label="Subtract 30 seconds">−30</button>
    <button class="small-button" type="button" data-action="timer-plus" aria-label="Add 30 seconds">+30</button>
    <button class="small-button timer-skip" type="button" data-action="timer-skip">Skip</button>
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
    return { ok: false, message: "Could not connect to correct the saved set." };
  }
}

function permanentCorrectionNotice(result) {
  return `The saved set could not be corrected. The server value is unchanged. ${result.message}`;
}

function correctionFailureNotice(result) {
  return result.message
    ? `Could not update the saved set. ${result.message}`
    : "Could not update the saved set. Please try again.";
}

async function refreshState() {
  try {
    const response = await fetch("/api/state", { cache: "no-store" });
    if (response.status === 401 || response.status === 403) {
      screenMode = "login";
      loginError = "Your session expired. Please sign in again.";
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
      loginError = "Sign in to open your workouts.";
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
    loadError = "The server is unavailable right now.";
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
        notice = "No connection. Your sets will sync when you're back online.";
        break;
      }

      if (response.ok) {
        const saved = await response.json().catch(() => null);
        if (!saved?.id) {
          notice = "The set could not be confirmed and will stay queued for sync.";
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
            loginError = "Your session expired. Please sign in again.";
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
        loginError = "Your session expired. Please sign in again.";
        notice = "";
        break;
      }

      const failure = await response.json().catch(() => ({}));
      if ([400, 404, 409, 422].includes(response.status)) {
        queue = queue.slice(1);
        writeStored("gym_queue", queue);
        removePending(item.client_id);
        unconfirmedSetIds.delete(item.client_id);
        notice = failure.error || failure.detail || "The set was invalid and has been discarded.";
        validationRejected = true;
        continue;
      }

      notice = response.status >= 500
        ? "The server is unavailable. Your sets are saved on this device."
        : "The set could not sync and remains saved on this device.";
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
    if (!response.ok) throw new Error("Could not load workout history.");
    const result = await response.json();
    if (historyRequestIds.get(exercise) === requestId) histories[exercise] = result;
  } catch (_error) {
    if (historyRequestIds.get(exercise) === requestId) {
      histories[exercise] = { error: "Workout history is unavailable." };
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
    notice = `Could not load the exercise library. ${error.message}`;
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
    exerciseDetailTab = "info";
    exerciseDetailMenuOpen = false;
    exerciseMusclesExpanded = false;
    exerciseInstructionsDraft = exerciseDetail.instructions || "";
    notice = "";
  } catch (error) {
    notice = `Could not load exercise details. ${error.message}`;
  }
  render();
}

async function loadWorkoutList() {
  workoutDetail = null;
  historyEditSet = null;
  const [historyResult, overviewResult] = await Promise.allSettled([
    apiData("/api/workouts?limit=50"),
    apiData(`/api/overview?weeks=${overviewWeeks}`),
  ]);
  if (historyResult.status === "fulfilled") workoutHistory = historyResult.value;
  else {
    workoutHistory = [];
    notice = `Could not load workout history. ${historyResult.reason.message}`;
  }
  if (overviewResult.status === "fulfilled") {
    overviewData = overviewResult.value;
    overviewError = "";
  } else {
    overviewError = `Chart could not load. ${overviewResult.reason.message}`;
  }
  render();
}

async function loadOverview(weeks = overviewWeeks) {
  try {
    overviewData = await apiData(`/api/overview?weeks=${weeks}`);
    overviewError = "";
  } catch (error) {
    overviewError = `Profile data could not load. ${error.message}`;
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
    notice = `Could not load the workout. ${error.message}`;
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
    notice = `Could not load plans. ${error.message}`;
  }
  render();
}

async function selectTab(tab) {
  if (!["training", "history", "profile", "exercises", "plans"].includes(tab)) return;
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
  else if (tab === "profile") await loadOverview();
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
    notice = `Could not start the workout. ${error.message}`;
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
    notice = `Could not start the workout. ${error.message}`;
  }
  render();
}

async function continueSession() {
  activeTab = "training";
  trainingSheetClosed = false;
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
    notice = error.message || "Could not save the change.";
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

async function saveSessionExerciseTarget() {
  const exercise = currentExercise();
  if (!exercise?.session_exercise_id || !appState?.session) return;
  const low = Number(screen.querySelector('[data-rep-target="rep_min"]')?.value);
  const high = Number(screen.querySelector('[data-rep-target="rep_max"]')?.value);
  if (!Number.isInteger(low) || !Number.isInteger(high) || low < 1 || high < low) {
    notice = "Please check the target range.";
    render();
    return;
  }
  const updated = await sendMutation(
    `/api/sessions/${appState.session.id}/exercises/${exercise.session_exercise_id}`,
    "PATCH",
    { rep_min: low, rep_max: high },
    "state",
  );
  if (updated) {
    repTargetEditorOpen = false;
    render();
  }
}

async function saveExerciseInstructions() {
  if (!exerciseDetail) return;
  const input = screen.querySelector("[data-exercise-instructions]");
  const instructions = input?.value ?? exerciseInstructionsDraft;
  const updated = await sendMutation(`/api/exercises/${exerciseDetail.id}`, "PATCH", { instructions });
  if (updated) {
    exerciseDetail = { ...exerciseDetail, ...updated };
    exerciseInstructionsDraft = updated.instructions || instructions;
    notice = "Instructions saved.";
    render();
  }
}

async function shareText(title, text) {
  try {
    if (navigator.share) {
      await navigator.share({ title, text });
      return;
    }
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      notice = "Text export copied to clipboard.";
    } else {
      notice = text;
    }
  } catch (error) {
    if (error?.name !== "AbortError") notice = "Text export could not be shared.";
  }
  render();
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
  const timed = historyEditSet.unit === "seconds";
  const maxAmount = timed ? 600 : 100;
  if (!Number.isFinite(weight) || weight <= 0 || weight > 500
    || !Number.isInteger(reps) || reps < 1 || reps > maxAmount) {
    notice = `Check the weight and ${timed ? "duration" : "reps"}.`;
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
  if (!window.confirm("Delete this set?")) return;
  await sendMutation(`/api/workouts/${selectedWorkoutId}/sets/${setId}`, "DELETE", undefined, "history");
}

async function deleteSelectedWorkout() {
  if (!selectedWorkoutId || !window.confirm("Delete this workout and all its sets?")) return;
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
  const limit = maxAmount(exercise);
  if (!Number.isFinite(weight) || weight <= 0 || weight > 500 || !Number.isInteger(reps) || reps < 1 || reps > limit) {
    notice = `Weight must be above 0 and at most 500 kg; ${limit === 600 ? "seconds" : "reps"}: 1 to ${limit}.`;
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
  navigator.vibrate?.(12);

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
          loginError = "Your session expired. Please sign in again.";
          notice = "Set not saved. Sign in, then tap the checkmark again.";
          return false;
        }
        if (!response.ok) {
          const failure = await response.json().catch(() => ({}));
          const detail = failure.error || failure.detail;
          notice = detail
            ? `Set not saved. ${detail}`
            : "Set not saved. Check your connection and tap the checkmark again.";
          return false;
        }

        const confirmation = await response.json().catch(() => null);
        if (!confirmation?.id) {
          notice = "Set not saved. The server did not confirm it; tap the checkmark again.";
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
            loginError = "Your session expired. Please sign in again.";
          }
          notice = correctionFailureNotice(reconciled);
          return false;
        }
        keepSavedSet(item, reconciled.saved);
        removeDraft(exercise, clientId);
        if (!appState?.session) await refreshState();
        notice = "Set synced. Offline storage is unavailable on this device.";
        startTimer(exercise.rest_s);
        return true;
      } catch (_error) {
        notice = "Set not saved. Check your connection and tap the checkmark again.";
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
  if (!Number.isFinite(weight) || weight <= 0 || weight > 500 || !Number.isInteger(reps) || reps < 1 || reps > maxAmount(currentExercise())) {
    notice = "Check the weight and reps.";
    render();
    return;
  }
  try {
    const { response, payload } = await requestJson(`/api/sets/${editingSet.id}`, "PATCH", {
      weight_kg: weight,
      reps,
    });
    if (!response.ok) throw new Error(payload?.error || payload?.detail || "Could not save the change.");
    editingSet = null;
    notice = "";
    await refreshState();
  } catch (error) {
    notice = error.message || "Could not save the change.";
  }
  render();
}

async function deleteEdit() {
  if (!editingSet || String(editingSet.id).startsWith("pending:")) return;
  if (!window.confirm("Delete this set?")) return;
  try {
    const response = await fetch(`/api/sets/${editingSet.id}`, { method: "DELETE" });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload.error || payload.detail || "Could not delete the set.");
    }
    editingSet = null;
    notice = "";
    await refreshState();
  } catch (error) {
    notice = error.message || "Could not delete the set.";
  }
  render();
}

async function finishTraining() {
  if (!window.confirm("Finish and save this workout?")) return;
  const synchronized = await flushQueue();
  await waitForSetSends();
  if (!synchronized || queue.length || unconfirmedSetIds.size) {
    if (!notice) notice = "Sync your sets before finishing the workout.";
    render();
    return;
  }
  try {
    const { response, payload } = await requestJson("/api/finish", "POST");
    if (!response.ok) throw new Error(payload?.error || payload?.detail || "Could not finish the workout.");
    finishSummary = payload;
    screenMode = "summary";
    selectedExercise = null;
    clearDraftsForSession(appState?.session?.id);
    drafts = new Map();
    removeStored("gym_view");
    notice = "";
  } catch (error) {
    notice = error.message || "Could not finish the workout.";
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

document.addEventListener("change", (event) => {
  const input = event.target;
  if (input.matches("[data-tenrm-target]")) {
    const target = Number(input.value);
    if (Number.isInteger(target) && target >= 1 && target <= 30) tenRmTarget = target;
    tenRmEditOpen = false;
    render();
  }
});

// mantis: A login target must be a path on this origin: "/x" yes, "//host", "/\\host" or a URL no.
function safeReturnPath(value) {
  if (typeof value !== "string" || !value.startsWith("/")) return null;
  if (value.startsWith("//") || /[\\\u0000-\u001f\u007f]/.test(value)) return null;
  return value;
}

document.addEventListener("submit", async (event) => {
  const form = event.target;
  if (form.id === "login-form") {
    event.preventDefault();
    const token = new FormData(form).get("token");
    loginError = "";
    try {
      const { response, payload } = await requestJson("/auth/session", "POST", { token });
      if (!response.ok) throw new Error(payload?.detail || payload?.error || "Sign in failed.");
      // mantis: After the shared Mantis session is set, return to `next` (same-origin paths only);
      // the bare /login page leads to openGym at the root.
      const returnTo = safeReturnPath(new URLSearchParams(window.location.search || "").get("next"))
        ?? (window.location.pathname === "/login" ? "/" : null);
      if (returnTo) {
        window.location.assign(returnTo);
        return;
      }
      screenMode = "loading";
      await loadState();
      if (screenMode === "app") await flushQueue();
    } catch (error) {
      loginError = error.message || "Sign in failed.";
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
  } else if (form.id === "new-measurement-form") {
    const rawWeight = formData.get("weight_kg");
    const rawBodyFat = formData.get("body_fat");
    const body = {
      weight_kg: rawWeight ? Number(rawWeight) : null,
      body_fat: rawBodyFat ? Number(rawBodyFat) : null,
      date: formData.get("date") || null,
    };
    const saved = await sendMutation("/api/measurements", "POST", body);
    if (saved) {
      measurementFormOpen = false;
      await loadOverview();
    }
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
  } else if (action === "close-history") {
    selectedExercise = null;
    removeStored("gym_view");
    await selectTab("training");
  } else if (action === "close-library") {
    exerciseDetail = null;
    activeTab = "training";
    render();
  } else if (action === "filter-library-muscle") {
    await loadExerciseList(exerciseSearch, button.dataset.muscle || "");
  } else if (action === "set-overview-weeks") {
    const weeks = Number(button.dataset.weeks);
    if (![4, 12, 26].includes(weeks)) return;
    overviewWeeks = weeks;
    await loadWorkoutList();
  } else if (action === "set-chart-metric") {
    if (!["workouts", "volume"].includes(button.dataset.metric)) return;
    chartMetric = button.dataset.metric;
    render();
  } else if (action === "share-history") {
    const text = (workoutHistory ?? []).map((workout) =>
      `${formatEnglishDate(workout.date)} — ${workout.title} (${workout.set_count} sets, ${formatWeight(workout.volume_kg)} kg)`
    ).join("\n");
    await shareText("Training history", text || "No completed workouts yet.");
  } else if (action === "share-workout") {
    const workout = workoutDetail;
    if (!workout) return;
    const sets = workout.sets.map((set) => `${set.exercise}: ${formatWeight(set.weight_kg)} kg × ${set.reps} ${setUnitLabel(set)}`).join("\n");
    await shareText(workout.title, `${formatEnglishDate(workout.date)} — ${workout.title}\n${sets}`);
  } else if (action === "profile-settings") {
    activeTab = "plans";
    await loadPlanList();
  } else if (action === "toggle-measurement-form") {
    measurementFormOpen = !measurementFormOpen;
    render();
  } else if (action === "create-plan") {
    planMenuOpen = false;
    activeTab = "plans";
    await loadPlanList();
  } else if (action === "open-plan-menu") {
    planMenuOpen = !planMenuOpen;
    render();
  } else if (action === "open-library") {
    activeTab = "exercises";
    selectedExercise = null;
    exerciseDetail = null;
    await loadExerciseList();
  } else if (action === "help" || action === "open-help") {
    notice = "Tap a day to start it. Log each set with the green checkmark.";
    render();
  } else if (action === "open-exercise") {
    activeTab = "training";
    selectedExercise = button.dataset.exercise;
    writeStored("gym_view", selectedExercise);
    editingSet = null;
    render();
    void loadHistory(selectedExercise);
  } else if (action === "select-exercise") {
    selectedExercise = button.dataset.exercise;
    writeStored("gym_view", selectedExercise);
    editingSet = null;
    exerciseMenuOpen = false;
    render();
    void loadHistory(selectedExercise);
  } else if (action === "close-training") {
    selectedExercise = null;
    trainingSheetClosed = true;
    exerciseMenuOpen = false;
    removeStored("gym_view");
    render();
  } else if (action === "open-exercise-menu") {
    exerciseMenuOpen = !exerciseMenuOpen;
    render();
  } else if (action === "toggle-session-note") {
    exerciseNoteOpen = !exerciseNoteOpen;
    exerciseMenuOpen = false;
    render();
  } else if (action === "toggle-warmups") {
    showWarmups = !showWarmups;
    render();
  } else if (action === "edit-tenrm-target") {
    tenRmEditOpen = !tenRmEditOpen;
    render();
  } else if (action === "edit-rep-target") {
    repTargetEditorOpen = !repTargetEditorOpen;
    render();
  } else if (action === "save-session-target") {
    await saveSessionExerciseTarget();
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
    const moved = await sendMutation(
      `/api/sessions/${appState.session.id}/exercises/${exercise.session_exercise_id}`,
      "PATCH",
      { sort_order: nextIndex },
      "state",
    );
    if (moved) render();
  } else if (action === "save-session-note") {
    await saveSessionExerciseNote();
  } else if (action === "open-session-exercise-picker") {
    sessionExercisePickerOpen = true;
    sessionExercisePickerKind = "session";
    if (exerciseDirectory === null) await loadExerciseList("", "");
    render();
  } else if (action === "replace-session-exercise") {
    sessionExercisePickerOpen = true;
    sessionExercisePickerKind = "replace";
    exerciseMenuOpen = false;
    if (exerciseDirectory === null) await loadExerciseList("", "");
    render();
  } else if (action === "remove-session-exercise") {
    const exercise = currentExercise();
    if (!exercise?.session_exercise_id || !appState?.session) return;
    if (!window.confirm(`Remove ${exercise.name} from this workout?`)) return;
    const ordered = [...(appState.exercises ?? [])].sort((left, right) => left.sort_order - right.sort_order);
    const index = ordered.findIndex((entry) => entry.session_exercise_id === exercise.session_exercise_id);
    const removed = await sendMutation(
      `/api/sessions/${appState.session.id}/exercises/${exercise.session_exercise_id}`,
      "DELETE", undefined, "state",
    );
    if (removed) {
      const remaining = [...(appState?.exercises ?? [])].sort((left, right) => left.sort_order - right.sort_order);
      const next = remaining[index] ?? remaining[index - 1] ?? remaining.find((entry) => !entry.skipped);
      selectedExercise = next?.name ?? null;
      if (selectedExercise) writeStored("gym_view", selectedExercise);
      else removeStored("gym_view");
      render();
    }
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
    if (kind === "replace" && appState?.session) {
      const exercise = currentExercise();
      if (!exercise?.session_exercise_id) return;
      const replaced = await sendMutation(
        `/api/sessions/${appState.session.id}/exercises/${exercise.session_exercise_id}`,
        "PATCH", { exercise_id: exerciseId }, "state",
      );
      if (replaced) {
        sessionExercisePickerOpen = false;
        selectedExercise = picked.name;
        writeStored("gym_view", selectedExercise);
        render();
        void loadHistory(selectedExercise);
      }
    } else if (kind === "session" && appState?.session) {
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
    exerciseDetailTab = "info";
    await loadExerciseList();
  } else if (action === "exercise-detail-tab") {
    exerciseDetailTab = button.dataset.detailTab;
    exerciseDetailMenuOpen = false;
    render();
  } else if (action === "exercise-detail-menu") {
    exerciseDetailMenuOpen = !exerciseDetailMenuOpen;
    render();
  } else if (action === "toggle-muscles") {
    exerciseMusclesExpanded = !exerciseMusclesExpanded;
    render();
  } else if (action === "similar-exercises") {
    exerciseMuscle = exerciseDetail?.muscle || "";
    exerciseDetail = null;
    await loadExerciseList("", exerciseMuscle);
  } else if (action === "save-exercise-instructions") {
    await saveExerciseInstructions();
  } else if (action === "share-exercise") {
    if (!exerciseDetail) return;
    const text = `${exerciseDetail.name}\n${exerciseDetail.muscle || ""}\n${exerciseInstructionsDraft}`.trim();
    await shareText(exerciseDetail.name, text);
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
    historyEditSet = {
      id: item.id, weight_kg: inputWeight(item.weight_kg),
      reps: String(item.reps), unit: item.unit,
    };
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
    if (!window.confirm("Delete this workout day and its exercises?")) return;
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
      notice = "This set is still syncing.";
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
function scrolledToTop(target, sheet) {
  for (let element = target; element; element = element.parentElement) {
    if (Number(element.scrollTop) > 0) return false;
    if (element === sheet) return true;
  }
  return Number(sheet.scrollTop) === 0;
}
document.addEventListener("touchstart", (event) => {
  const sheet = event.target.closest(".sheet-dismissable, .workout-sheet");
  if (!sheet) return;
  const startY = event.changedTouches?.[0]?.clientY;
  sheetStartY = startY !== undefined
    && (event.target.closest(".sheet-grabber") || scrolledToTop(event.target, sheet))
    ? startY : null;
}, { passive: true });
document.addEventListener("touchend", (event) => {
  const endY = event.changedTouches?.[0]?.clientY;
  const distance = sheetStartY === null || endY === undefined ? 0 : endY - sheetStartY;
  sheetStartY = null;
  if (distance < 90 || !event.target.closest(".sheet-dismissable, .workout-sheet")) return;
  if (activeTab === "training") {
    selectedExercise = null;
    trainingSheetClosed = true;
    removeStored("gym_view");
    render();
  } else if (activeTab === "history") {
    if (workoutDetail) workoutDetail = null;
    else activeTab = "training";
    render();
  } else if (activeTab === "exercises") {
    if (exerciseDetail) exerciseDetail = null;
    else activeTab = "training";
    render();
  }
}, { passive: true });
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  if (sessionExercisePickerOpen || planExercisePickerDayId !== null) {
    sessionExercisePickerOpen = false;
    planExercisePickerDayId = null;
    render();
  } else if (exerciseDetailMenuOpen) {
    exerciseDetailMenuOpen = false;
    render();
  } else if (activeTab === "training" && (selectedExercise || appState?.session)) {
    selectedExercise = null;
    trainingSheetClosed = true;
    removeStored("gym_view");
    render();
  } else if (activeTab === "history") {
    if (workoutDetail) workoutDetail = null;
    else activeTab = "training";
    render();
  } else if (activeTab === "exercises") {
    if (exerciseDetail) exerciseDetail = null;
    else activeTab = "training";
    render();
  }
});

resumeTimer();
void loadState();

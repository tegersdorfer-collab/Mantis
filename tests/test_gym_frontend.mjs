import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const appSource = await readFile(new URL("../web/gym/app.js", import.meta.url), "utf8");
const cssSource = await readFile(new URL("../web/gym/app.css", import.meta.url), "utf8");
const htmlSource = await readFile(new URL("../web/gym/index.html", import.meta.url), "utf8");
const manifestSource = await readFile(new URL("../web/gym/manifest.webmanifest", import.meta.url), "utf8");
let generatedClientIdSequence = 0;

function response(status, payload) {
  return {
    status,
    ok: status >= 200 && status < 300,
    json: async () => payload,
  };
}

async function waitFor(predicate) {
  for (let index = 0; index < 50; index += 1) {
    if (predicate()) return true;
    await new Promise((resolve) => setImmediate(resolve));
  }
  return predicate();
}

function makeState({ session = { id: 1, started_at: "2026-10-14T10:00:00Z" }, logged = [], prefill = [], home } = {}) {
  return {
    session,
    exercises: ["Squats", "Bench Press", "Deadlifts"].map((name) => ({
      name,
      rest_s: 180,
      prefill: name === "Squats" ? prefill : [],
      logged: name === "Squats" ? logged : [],
    })),
    ...(home ? { home } : {}),
  };
}

function createFrontend({
  fetch: fetchRequest,
  storage = new Map(),
  failGetItem = () => false,
  failSetItem = () => false,
} = {}) {
  const calls = [];
  const documentListeners = new Map();
  const windowListeners = new Map();
  const draftInputs = [];
  const tenRmValues = [];
  const vibrations = [];
  let fakeNow = Date.parse("2035-10-01T12:00:00Z");
  const intervals = new Map();
  class FakeDate extends Date {
    constructor(...args) { super(...(args.length ? args : [fakeNow])); }
    static now() { return fakeNow; }
  }
  const screen = {
    _innerHTML: "",
    get innerHTML() { return this._innerHTML; },
    set innerHTML(value) {
      this._innerHTML = value;
      draftInputs.length = 0;
      tenRmValues.length = 0;
      for (const match of value.matchAll(/<input\b(?=[^>]*\bdata-draft-input\b)([^>]*)>/g)) {
        const attributes = match[1];
        const attribute = (name) => attributes.match(new RegExp(`\\b${name}="([^"]*)"`))?.[1];
        let currentValue = attribute("value") ?? "";
        draftInputs.push({
          dataset: {
            clientId: attribute("data-client-id"),
            index: attribute("data-index"),
            field: attribute("data-field"),
          },
          matches: (selector) => selector === "[data-draft-input]",
          writeCount: 0,
          get value() { return currentValue; },
          set value(value) { currentValue = String(value); this.writeCount += 1; },
        });
      }
      for (const match of value.matchAll(/<span\b(?=[^>]*\bdata-tenrm-row\b)([^>]*)>([\s\S]*?)<\/span>/g)) {
        const attributes = match[1];
        const rowId = attributes.match(/\bdata-tenrm-row="([^"]*)"/)?.[1];
        let currentText = match[2];
        tenRmValues.push({
          dataset: { row: rowId },
          matches: (selector) => selector === "[data-tenrm-row]",
          get textContent() { return currentText; },
          set textContent(value) { currentText = String(value); },
        });
      }
    },
    querySelector: () => null,
    querySelectorAll(selector) {
      if (selector === "[data-draft-input]") return draftInputs;
      if (selector === "[data-tenrm-row]") return tenRmValues;
      return [];
    },
  };
  const elements = new Map([
    ["#screen", screen],
    ["#timer-host", { innerHTML: "" }],
    ["#sync-host", { innerHTML: "" }],
    ["#notice-host", { innerHTML: "" }],
    ["#tab-bar", { innerHTML: "", hidden: false }],
  ]);
  let reloads = 0;
  let intervalId = 0;
  const context = vm.createContext({
    document: {
      body: { classList: { toggle() {}, remove() {}, add() {} } },
      visibilityState: "visible",
      querySelector: (selector) => elements.get(selector),
      addEventListener: (name, callback) => documentListeners.set(name, callback),
    },
    window: {
      location: { reload() { reloads += 1; } },
      confirm: () => true,
      addEventListener: (name, callback) => windowListeners.set(name, callback),
    },
    navigator: { vibrate: (pattern) => vibrations.push(pattern) },
    localStorage: {
      getItem(key) {
        if (failGetItem(key)) throw new Error("storage unavailable");
        return storage.has(key) ? storage.get(key) : null;
      },
      setItem(key, value) {
        if (failSetItem(key, value)) throw new Error("storage full");
        storage.set(key, value);
      },
      removeItem(key) { storage.delete(key); },
    },
    fetch: async (url, options = {}) => {
      const call = { url, method: options.method || "GET", body: options.body };
      calls.push(call);
      return fetchRequest(url, options, call);
    },
    FormData: class {
      constructor(form) { this.form = form; }
      get(key) { return key === "token" ? this.form.token : (this.form[key] ?? null); }
    },
    URLSearchParams,
    Date: FakeDate,
    crypto: { randomUUID: () => `vm-generated-client-id-${++generatedClientIdSequence}` },
    setInterval(callback, delay) {
      intervalId += 1;
      intervals.set(intervalId, { callback, delay });
      return intervalId;
    },
    clearInterval(id) { intervals.delete(id); },
  });
  vm.runInContext(appSource, context);

  return {
    calls,
    documentListeners,
    elements,
    storage,
    windowListeners,
    get reloads() { return reloads; },
    vibrations,
    click(action, data = {}) {
      const dataset = { action, ...data };
      if (action === "log-draft" && data.index !== undefined) {
        const input = draftInputs.find((entry) => entry.dataset.index === String(data.index)
          && entry.dataset.field === "weight_kg");
        dataset.clientId = input?.dataset.clientId;
      }
      const button = { dataset };
      return documentListeners.get("click")({
        target: { closest: (selector) => selector === "[data-action]" ? button : null },
      });
    },
    input(index, field, value) {
      const input = draftInputs.find((entry) => entry.dataset.index === String(index)
        && entry.dataset.field === field);
      if (!input) throw new Error(`Missing visible draft input: ${index}/${field}`);
      input.value = value;
      input.writeCount = 0;
      documentListeners.get("input")({ target: input });
      return input;
    },
    draftInput(index, field) {
      return draftInputs.find((entry) => entry.dataset.index === String(index)
        && entry.dataset.field === field);
    },
    draftInputs() { return [...draftInputs]; },
    tenRmValue(rowId) { return tenRmValues.find((entry) => entry.dataset.row === rowId)?.textContent; },
    beginTouch({ y = 0, scrollTop = 0, handle = false } = {}) {
      const sheet = { scrollTop };
      const target = {
        closest(selector) {
          if (selector === ".sheet-dismissable, .workout-sheet") return sheet;
          if (selector === ".sheet-grabber") return handle ? {} : null;
          return null;
        },
      };
      documentListeners.get("touchstart")({ target, changedTouches: [{ clientY: y }] });
      return target;
    },
    endTouch(target, y) {
      documentListeners.get("touchend")({ target, changedTouches: [{ clientY: y }] });
    },
    advanceClock(milliseconds) {
      const ticks = Math.floor(milliseconds / 1000);
      for (let index = 0; index < ticks; index += 1) {
        fakeNow += 1000;
        for (const interval of [...intervals.values()]) {
          if (interval.delay === 1000) interval.callback();
        }
      }
      fakeNow += milliseconds % 1000;
    },
    submit(token) {
      return documentListeners.get("submit")({
        target: { id: "login-form", token },
        preventDefault() {},
      });
    },
    run(source) { return vm.runInContext(source, context); },
  };
}

const queuedSet = {
  client_id: "pending-client-id",
  exercise: "Squats",
  weight_kg: 60,
  reps: 10,
};

test("Zeitwerte im Trainingsverlauf werden als Sekunden angezeigt und bearbeitet", async () => {
  const app = createFrontend({
    fetch: async (url) => {
      if (url === "/api/state") return response(200, makeState());
      return response(200, {});
    },
  });

  assert.equal(await waitFor(() => app.elements.get("#screen").innerHTML.includes("class=\"exercise-list\"")), true);
  app.run(`
    activeTab = "history";
    selectedWorkoutId = 42;
    workoutDetail = {
      title: "Synthetic Deadhang", date: "2035-05-20", duration_min: 10,
      set_count: 1, volume_kg: 0,
      sets: [{ id: 7, exercise: "Synthetic Deadhang", set_index: 1,
        weight_kg: 20, reps: 45, unit: "seconds", is_warmup: false }],
    };
    render();
  `);

  assert.match(app.elements.get("#screen").innerHTML, />KG<\/span><span>SECS<\/span>/);
  assert.match(app.elements.get("#screen").innerHTML, /<span>20<\/span><span>45<\/span>/);
  await app.click("edit-history-set", { setId: "7" });
  assert.match(app.elements.get("#screen").innerHTML, /<label>SECS<input data-history-edit/);
  assert.doesNotMatch(app.elements.get("#screen").innerHTML, /Wdh\.<input data-history-edit/);
  await app.run(`historyEditSet.reps = "601"; saveHistorySet()`);
  assert.match(app.elements.get("#notice-host").innerHTML, /duration/);
  assert.equal(app.calls.some((call) => call.method === "PATCH"), false);

  app.run(`
    activeTab = "exercises";
    exerciseDetail = {
      name: "Synthetic Deadhang", muscle: "Rücken", equipment: "bodyweight",
      unit: "min", rep_min: 30, rep_max: 60,
      history: [{ date: "2035-05-20", e1rm_kg: null, best_set: null,
        sets: [{ weight_kg: 20, reps: 45, unit: "seconds" }] }],
    };
    render();
  `);

  assert.match(app.elements.get("#screen").innerHTML, /20×45 s/);
  assert.match(app.elements.get("#screen").innerHTML, /No weighted 1RM history/);
  assert.doesNotMatch(app.elements.get("#screen").innerHTML, /<svg class="e1rm-chart"/);
});

test("401 und 403 lassen Queue-Sätze stehen und ein Login startet den Sync erneut", async () => {
  for (const authStatus of [401, 403]) {
    const state = makeState();
    const storage = new Map([["gym_queue", JSON.stringify([queuedSet])]]);
    let setRequests = 0;
    const app = createFrontend({
      storage,
      fetch: async (url) => {
        if (url === "/api/state") return response(200, state);
        if (url === "/api/sets") {
          setRequests += 1;
          if (setRequests === 1) return response(authStatus, { detail: "Anmeldung abgelaufen" });
          const saved = { ...queuedSet, id: 9, set_index: 1 };
          state.exercises[0].logged.push(saved);
          return response(200, saved);
        }
        if (url === "/auth/session") return response(200, { ok: true });
        throw new Error(`Unexpected request: ${url}`);
      },
    });

    assert.equal(await waitFor(() => app.elements.get("#screen").innerHTML.includes("Access key")), true);
    assert.equal(storage.get("gym_queue"), JSON.stringify([queuedSet]));
    assert.equal(setRequests, 1);

    await app.submit("valid-token");

    assert.equal(await waitFor(() => setRequests === 2), true);
    assert.equal(app.reloads, 0);
    assert.equal(await waitFor(() => storage.get("gym_queue") === "[]"), true);
  }
});

test("Training beenden wartet auf einen laufenden Queue-Sync", async () => {
  const state = makeState();
  const storage = new Map([["gym_queue", JSON.stringify([queuedSet])]]);
  let resolveSetResponse;
  let finishRequested = false;
  const app = createFrontend({
    storage,
    fetch: async (url) => {
      if (url === "/api/state") return response(200, state);
      if (url === "/api/sets") {
        return new Promise((resolve) => { resolveSetResponse = resolve; });
      }
      if (url === "/api/finish") {
        finishRequested = true;
        return response(200, { duration_min: 1, sets: 1, volume_kg: 600 });
      }
      throw new Error(`Unexpected request: ${url}`);
    },
  });

  assert.equal(await waitFor(() => typeof resolveSetResponse === "function"), true);
  const finishing = app.click("finish");
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(finishRequested, false);

  const saved = { ...queuedSet, id: 10, set_index: 1 };
  state.exercises[0].logged.push(saved);
  resolveSetResponse(response(200, saved));
  await finishing;

  assert.equal(finishRequested, true);
  assert.equal(app.elements.get("#screen").innerHTML.includes("Workout complete"), true);
  assert.match(app.elements.get("#screen").innerHTML, /<span>set<\/span><strong>1<\/strong>/);
});

test("Training beenden wird bei nicht leerbarer Queue mit Synchronisationshinweis abgelehnt", async () => {
  const state = makeState();
  const storage = new Map([["gym_queue", JSON.stringify([queuedSet])]]);
  let finishRequested = false;
  const app = createFrontend({
    storage,
    fetch: async (url) => {
      if (url === "/api/state") return response(200, state);
      if (url === "/api/sets") return response(503, { detail: "offline" });
      if (url === "/api/finish") {
        finishRequested = true;
        return response(200, { duration_min: 1, sets: 1, volume_kg: 600 });
      }
      throw new Error(`Unexpected request: ${url}`);
    },
  });

  assert.equal(await waitFor(() => app.elements.get("#sync-host").innerHTML.includes("waiting to sync")), true);
  await app.click("finish");

  assert.equal(finishRequested, false);
  assert.match(app.elements.get("#notice-host").innerHTML, /Sync your sets/);
  assert.equal(storage.get("gym_queue"), JSON.stringify([queuedSet]));
});

test("bei fehlgeschlagener Queue-Persistenz bleibt ein erfolgloser Direktversand als offene Zeile", async () => {
  const app = createFrontend({
    fetch: async (url) => {
      if (url === "/api/state") return response(200, makeState());
      if (url === "/api/history?exercise=Squats&limit=5") return response(200, []);
      if (url === "/api/sets") throw new Error("offline");
      throw new Error(`Unexpected request: ${url}`);
    },
    failSetItem: (key) => key === "gym_queue",
  });

  assert.equal(await waitFor(() => app.elements.get("#screen").innerHTML.includes("class=\"exercise-list\"")), true);
  await app.click("open-exercise", { exercise: "Squats" });
  app.input(0, "weight_kg", "20");
  app.input(0, "reps", "5");
  await app.click("log-draft", { index: "0" });

  assert.equal(app.calls.filter((call) => call.url === "/api/sets").length, 1);
  assert.equal(app.elements.get("#screen").innerHTML.includes("set-row-complete"), false);
  assert.equal(app.elements.get("#screen").innerHTML.includes("data-action=\"log-draft\""), true);
  assert.match(app.elements.get("#notice-host").innerHTML, /set not saved/i);
});

test("bei fehlgeschlagener Queue-Persistenz zeigt ein erfolgreicher Direktversand den gespeicherten Satz", async () => {
  const state = makeState();
  const app = createFrontend({
    fetch: async (url) => {
      if (url === "/api/state") return response(200, state);
      if (url.startsWith("/api/history?")) return response(200, []);
      if (url === "/api/sets") {
        const saved = { ...queuedSet, id: 13, set_index: 1 };
        state.exercises[0].logged.push(saved);
        return response(201, saved);
      }
      throw new Error(`Unexpected request: ${url}`);
    },
    failSetItem: (key) => key === "gym_queue",
  });

  assert.equal(await waitFor(() => app.elements.get("#screen").innerHTML.includes("class=\"exercise-list\"")), true);
  await app.click("open-exercise", { exercise: "Squats" });
  app.input(0, "weight_kg", "60");
  app.input(0, "reps", "10");
  await app.click("log-draft", { index: "0" });

  assert.equal(app.calls.filter((call) => call.url === "/api/sets").length, 1);
  assert.match(app.elements.get("#screen").innerHTML, /data-set-id="13"/);
  assert.equal(app.elements.get("#screen").innerHTML.includes("data-action=\"log-draft\""), false);
});

test("Retry nach verlorenem Direktversand korrigiert den idempotenten Satz vor dem Entfernen des Drafts", async () => {
  const state = makeState({ prefill: [{ weight_kg: 60, reps: 10 }] });
  const serverSets = new Map();
  const postBodies = [];
  const patchBodies = [];
  const app = createFrontend({
    fetch: async (url, options = {}) => {
      if (url === "/api/state") return response(200, state);
      if (url.startsWith("/api/history?")) return response(200, []);
      if (url === "/api/sets" && options.method === "POST") {
        const item = JSON.parse(options.body);
        postBodies.push(item);
        const existing = serverSets.get(item.client_id);
        if (existing) return response(200, existing);
        serverSets.set(item.client_id, { ...item, id: 71, set_index: 1 });
        throw new Error("response lost after server saved the set");
      }
      if (url === "/api/sets/71" && options.method === "PATCH") {
        const patch = JSON.parse(options.body);
        patchBodies.push(patch);
        const [clientId, existing] = serverSets.entries().next().value;
        const saved = { ...existing, ...patch };
        serverSets.set(clientId, saved);
        return response(200, saved);
      }
      throw new Error("Unexpected request: " + url);
    },
    failSetItem: (key) => key === "gym_queue",
  });

  assert.equal(await waitFor(() => app.elements.get("#screen").innerHTML.includes("class=\"exercise-list\"")), true);
  await app.click("open-exercise", { exercise: "Squats" });
  await app.click("log-draft", { index: "0" });
  assert.equal(app.elements.get("#screen").innerHTML.includes('data-action="log-draft"'), true);

  app.input(0, "weight_kg", "70");
  app.input(0, "reps", "8");
  await app.click("log-draft", { index: "0" });

  assert.equal(postBodies.length, 2);
  assert.equal(postBodies[0].client_id, postBodies[1].client_id);
  assert.deepEqual(patchBodies, [{ weight_kg: 70, reps: 8 }]);
  assert.equal(serverSets.size, 1);
  assert.deepEqual(
    { weight_kg: [...serverSets.values()][0].weight_kg, reps: [...serverSets.values()][0].reps },
    { weight_kg: 70, reps: 8 },
  );
  assert.match(app.elements.get("#screen").innerHTML, /data-set-id="71"/);
  assert.equal(app.elements.get("#screen").innerHTML.includes('data-action="log-draft"'), false);
});

test("bei fehlgeschlagenem PATCH bleibt der korrigierte Direktversand-Draft offen", async () => {
  const state = makeState({ prefill: [{ weight_kg: 60, reps: 10 }] });
  let postRequests = 0;
  const app = createFrontend({
    fetch: async (url, options = {}) => {
      if (url === "/api/state") return response(200, state);
      if (url.startsWith("/api/history?")) return response(200, []);
      if (url === "/api/sets" && options.method === "POST") {
        postRequests += 1;
        if (postRequests === 1) throw new Error("response lost after server save");
        return response(200, { client_id: JSON.parse(options.body).client_id, id: 72, weight_kg: 60, reps: 10, set_index: 1 });
      }
      if (url === "/api/sets/72" && options.method === "PATCH") {
        return response(503, { detail: "Patch offline" });
      }
      throw new Error("Unexpected request: " + url);
    },
    failSetItem: (key) => key === "gym_queue",
  });

  assert.equal(await waitFor(() => app.elements.get("#screen").innerHTML.includes("class=\"exercise-list\"")), true);
  await app.click("open-exercise", { exercise: "Squats" });
  await app.click("log-draft", { index: "0" });
  app.input(0, "weight_kg", "70");
  app.input(0, "reps", "8");
  await app.click("log-draft", { index: "0" });

  assert.equal(app.elements.get("#screen").innerHTML.includes('data-action="log-draft"'), true);
  assert.match(app.elements.get("#screen").innerHTML, /value="70"/);
  assert.match(app.elements.get("#screen").innerHTML, /value="8"/);
  assert.match(app.elements.get("#notice-host").innerHTML, /Could not update the saved set/i);
});

test("Queue-Einträge werden vor dem Entfernen auf ihre korrigierten Werte gepatcht", async () => {
  const correctedItem = { ...queuedSet, weight_kg: 70, reps: 8 };
  const storage = new Map([["gym_queue", JSON.stringify([correctedItem])]]);
  const serverSets = new Map([[correctedItem.client_id, {
    ...correctedItem,
    weight_kg: 60,
    reps: 10,
    id: 73,
    set_index: 1,
  }]]);
  const patchBodies = [];
  const app = createFrontend({
    storage,
    fetch: async (url, options = {}) => {
      if (url === "/api/state") return response(200, makeState());
      if (url === "/api/sets" && options.method === "POST") {
        const item = JSON.parse(options.body);
        return response(200, serverSets.get(item.client_id));
      }
      if (url === "/api/sets/73" && options.method === "PATCH") {
        const patch = JSON.parse(options.body);
        patchBodies.push(patch);
        const [clientId, existing] = serverSets.entries().next().value;
        const saved = { ...existing, ...patch };
        serverSets.set(clientId, saved);
        return response(200, saved);
      }
      throw new Error("Unexpected request: " + url);
    },
  });

  assert.equal(await waitFor(() => storage.get("gym_queue") === "[]"), true);
  assert.deepEqual(patchBodies, [{ weight_kg: 70, reps: 8 }]);
  assert.equal(serverSets.size, 1);
  assert.deepEqual(
    { weight_kg: [...serverSets.values()][0].weight_kg, reps: [...serverSets.values()][0].reps },
    { weight_kg: 70, reps: 8 },
  );
});

test("bei fehlgeschlagenem PATCH bleibt ein korrigierter Queue-Eintrag gespeichert", async () => {
  const correctedItem = { ...queuedSet, weight_kg: 70, reps: 8 };
  const storage = new Map([["gym_queue", JSON.stringify([correctedItem])]]);
  const app = createFrontend({
    storage,
    fetch: async (url, options = {}) => {
      if (url === "/api/state") return response(200, makeState());
      if (url === "/api/sets" && options.method === "POST") {
        return response(200, { ...queuedSet, id: 74, set_index: 1 });
      }
      if (url === "/api/sets/74" && options.method === "PATCH") {
        return response(503, { detail: "Patch offline" });
      }
      throw new Error("Unexpected request: " + url);
    },
  });

  assert.equal(await waitFor(() => app.elements.get("#notice-host").innerHTML.includes("Could not update")), true);
  assert.equal(storage.get("gym_queue"), JSON.stringify([correctedItem]));
  assert.match(app.elements.get("#notice-host").innerHTML, /Could not update/i);
});

test("PATCH 409 verwirft den Queue-Eintrag, zeigt Serverwerte und synchronisiert den nächsten Satz", async () => {
  const first = { ...queuedSet, client_id: "first-id", weight_kg: 70, reps: 8 };
  const second = { ...queuedSet, client_id: "second-id", weight_kg: 50, reps: 5 };
  const storage = new Map([["gym_queue", JSON.stringify([first, second])]]);
  const state = makeState();
  const app = createFrontend({
    storage,
    fetch: async (url, options = {}) => {
      if (url === "/api/state") return response(200, state);
      if (url === "/api/sets" && options.method === "POST") {
        const item = JSON.parse(options.body);
        if (item.client_id === "first-id") {
          const stored = { ...queuedSet, id: 81, set_index: 1 };
          state.exercises[0].logged = [stored];
          return response(200, stored);
        }
        const stored = { ...item, id: 82, set_index: 2 };
        state.exercises[0].logged.push(stored);
        return response(201, stored);
      }
      if (url === "/api/sets/81" && options.method === "PATCH") {
        return response(409, { detail: "Training bereits beendet" });
      }
      if (url.startsWith("/api/history?")) return response(200, []);
      throw new Error("Unexpected request: " + url);
    },
  });

  assert.equal(await waitFor(() => storage.get("gym_queue") === "[]"), true);
  assert.equal(app.calls.filter((call) => call.url === "/api/sets" && call.method === "POST").length, 2);
  assert.equal(await waitFor(() => app.elements.get("#notice-host").innerHTML.includes("Training bereits beendet")), true);
  await waitFor(() => app.elements.get("#screen").innerHTML.includes("Squats"));
  assert.deepEqual(
    state.exercises[0].logged.map((set) => [set.id, set.weight_kg]),
    [[81, 60], [82, 50]],
  );
  assert.equal(app.run("appState.exercises[0].logged.some((set) => set.weight_kg === 70)"), false);
});

test("bei PATCH-Netzwerkfehler bleibt der Queue-Eintrag und wird später erneut versucht", async () => {
  const correctedItem = { ...queuedSet, weight_kg: 70, reps: 8 };
  const storage = new Map([["gym_queue", JSON.stringify([correctedItem])]]);
  let patchAttempts = 0;
  const app = createFrontend({
    storage,
    fetch: async (url, options = {}) => {
      if (url === "/api/state") return response(200, makeState());
      if (url === "/api/sets" && options.method === "POST") {
        return response(200, { ...queuedSet, id: 83, set_index: 1 });
      }
      if (url === "/api/sets/83" && options.method === "PATCH") {
        patchAttempts += 1;
        if (patchAttempts === 1) throw new Error("offline");
        return response(200, { ...queuedSet, id: 83, set_index: 1, weight_kg: 70, reps: 8 });
      }
      if (url.startsWith("/api/history?")) return response(200, []);
      throw new Error("Unexpected request: " + url);
    },
  });

  assert.equal(await waitFor(() => patchAttempts === 1), true);
  await waitFor(() => app.elements.get("#notice-host").innerHTML.includes("Korrektur"));
  assert.equal(storage.get("gym_queue"), JSON.stringify([correctedItem]));
  await app.run("flushQueue()");
  assert.equal(patchAttempts, 2);
  assert.equal(storage.get("gym_queue"), "[]");
});

test("62,3 wird auf 62,25 gerundet gesendet, ohne PATCH, und das Feld zeigt 62,25", async () => {
  const state = makeState({ prefill: [{ weight_kg: 60, reps: 10 }] });
  const app = createFrontend({
    fetch: async (url, options = {}) => {
      if (url === "/api/state") return response(200, state);
      if (url.startsWith("/api/history?")) return response(200, []);
      if (url === "/api/sets" && options.method === "POST") {
        const item = JSON.parse(options.body);
        const saved = { ...item, id: 84, set_index: 1 };
        state.exercises[0].logged.push(saved);
        return response(201, saved);
      }
      throw new Error(`Unexpected request: ${url}`);
    },
  });

  assert.equal(await waitFor(() => app.elements.get("#screen").innerHTML.includes("class=\"exercise-list\"")), true);
  await app.click("open-exercise", { exercise: "Squats" });
  const input = app.input(0, "weight_kg", "62,3");
  await app.click("log-draft", { index: "0" });
  await waitFor(() => app.calls.some((call) => call.url === "/api/sets"));
  await waitFor(() => app.run("appState.exercises[0].logged.length") === 1);

  const posts = app.calls.filter((call) => call.url === "/api/sets");
  assert.equal(JSON.parse(posts[0].body).weight_kg, 62.25);
  assert.equal(app.calls.some((call) => call.method === "PATCH"), false);
  assert.equal(input.value, "62,25");
  assert.equal(app.run("appState.exercises[0].logged[0].weight_kg"), 62.25);
});

test("nur definierte Validierungsfehler entfernen Queue-Sätze; andere Status behalten sie", async () => {
  for (const status of [400, 404, 409, 422]) {
    const storage = new Map([["gym_queue", JSON.stringify([queuedSet])]]);
    const app = createFrontend({
      storage,
      fetch: async (url) => {
        if (url === "/api/state") return response(200, makeState());
        if (url === "/api/sets") return response(status, { detail: `Fehler ${status}` });
        throw new Error(`Unexpected request: ${url}`);
      },
    });

    assert.equal(await waitFor(() => storage.get("gym_queue") === "[]"), true);
    assert.match(app.elements.get("#notice-host").innerHTML, new RegExp(`Fehler ${status}`));
  }

  const storage = new Map([["gym_queue", JSON.stringify([queuedSet])]]);
  const app = createFrontend({
    storage,
    fetch: async (url) => {
      if (url === "/api/state") return response(200, makeState());
      if (url === "/api/sets") return response(429, { detail: "try later" });
      throw new Error(`Unexpected request: ${url}`);
    },
  });

  assert.equal(await waitFor(() => app.elements.get("#notice-host").innerHTML.includes("could not sync")), true);
  assert.equal(storage.get("gym_queue"), JSON.stringify([queuedSet]));
});

test("Änderungen an vorbefüllten Werten übernehmen offene Folgezeilen ohne eigene Änderung", async () => {
  const app = createFrontend({
    fetch: async (url) => {
      if (url === "/api/state") return response(200, makeState({
        prefill: [
          { weight_kg: 60, reps: 10 },
          { weight_kg: 60, reps: 10 },
          { weight_kg: 60, reps: 10 },
        ],
      }));
      if (url.startsWith("/api/history?")) return response(200, []);
      throw new Error(`Unexpected request: ${url}`);
    },
  });

  assert.equal(await waitFor(() => app.elements.get("#screen").innerHTML.includes("class=\"exercise-list\"")), true);
  await app.click("open-exercise", { exercise: "Squats" });
  app.input(0, "weight_kg", "62,5");
  app.input(0, "reps", "9");
  assert.equal(app.run('JSON.stringify(drafts.get("Squats").map((draft) => draft.reps))'),
    JSON.stringify(["9", "9", "9"]));
  app.input(1, "reps", "8");
  app.input(0, "reps", "7");

  assert.equal(app.run('JSON.stringify(drafts.get("Squats").map((draft) => [draft.weight_kg, draft.reps]))'),
    JSON.stringify([["62,5", "7"], ["62,5", "8"], ["62,5", "8"]]));
});

test("Input aktualisiert sichtbare Folgefelder und ✓ sendet den angezeigten Wert", async () => {
  const state = makeState({
    prefill: [
      { weight_kg: 60, reps: 10 },
      { weight_kg: 60, reps: 10 },
      { weight_kg: 60, reps: 10 },
    ],
  });
  const app = createFrontend({
    fetch: async (url, options = {}) => {
      if (url === "/api/state") return response(200, state);
      if (url.startsWith("/api/history?")) return response(200, []);
      if (url === "/api/sets") {
        const item = JSON.parse(options.body);
        const saved = { ...item, id: 31, set_index: 1 };
        state.exercises[0].logged.push(saved);
        return response(201, saved);
      }
      throw new Error(`Unexpected request: ${url}`);
    },
  });

  assert.equal(await waitFor(() => app.elements.get("#screen").innerHTML.includes("class=\"exercise-list\"")), true);
  await app.click("open-exercise", { exercise: "Squats" });
  const activeInput = app.input(0, "weight_kg", "62,5");
  const nextInput = app.draftInput(1, "weight_kg");

  assert.equal(activeInput.value, "62,5");
  assert.equal(activeInput.writeCount, 0);
  assert.equal(nextInput.value, "62,5");

  await app.click("log-draft", { index: "1" });

  const sent = app.calls.find((call) => call.url === "/api/sets");
  assert.equal(JSON.parse(sent.body).weight_kg, 62.5);
});

test("parallele Direktversände entfernen gespeicherte Drafts anhand ihrer ID", async () => {
  const state = makeState({
    prefill: [
      { weight_kg: 50, reps: 8 },
      { weight_kg: 60, reps: 6 },
      { weight_kg: 70, reps: 4 },
    ],
  });
  const pendingResponses = [];
  const app = createFrontend({
    fetch: async (url, _options, call) => {
      if (url === "/api/state") return response(200, state);
      if (url.startsWith("/api/history?")) return response(200, []);
      if (url === "/api/sets") {
        const item = JSON.parse(call.body);
        return new Promise((resolve) => pendingResponses.push({ item, resolve }));
      }
      throw new Error(`Unexpected request: ${url}`);
    },
    failSetItem: (key) => key === "gym_queue",
  });

  assert.equal(await waitFor(() => app.elements.get("#screen").innerHTML.includes("class=\"exercise-list\"")), true);
  await app.click("open-exercise", { exercise: "Squats" });
  const saveSecondRow = app.click("log-draft", { index: "1" });
  assert.equal(await waitFor(() => pendingResponses.length === 1), true);
  const saveFirstRow = app.click("log-draft", { index: "0" });
  assert.equal(await waitFor(() => pendingResponses.length === 2), true);

  const firstItem = pendingResponses[1].item;
  pendingResponses[1].resolve(response(201, { ...firstItem, id: 41, set_index: 1 }));
  await saveFirstRow;
  const secondItem = pendingResponses[0].item;
  pendingResponses[0].resolve(response(201, { ...secondItem, id: 42, set_index: 2 }));
  await saveSecondRow;

  const remainingWeights = app.draftInputs().filter((input) => input.dataset.field === "weight_kg");
  const remainingReps = app.draftInputs().filter((input) => input.dataset.field === "reps");
  assert.deepEqual(remainingWeights.map((input) => input.value), ["70"]);
  assert.deepEqual(remainingReps.map((input) => input.value), ["4"]);
  assert.match(app.elements.get("#screen").innerHTML, /data-set-id="41"/);
  assert.match(app.elements.get("#screen").innerHTML, /data-set-id="42"/);
  assert.equal(app.elements.get("#screen").innerHTML.includes('data-action="log-draft"'), true);
});

test("Training beenden wartet auf einen laufenden Direktversand und blockiert bei fehlender Bestätigung", async () => {
  let resolveDirectSend;
  let finishRequested = false;
  const app = createFrontend({
    fetch: async (url) => {
      if (url === "/api/state") return response(200, makeState());
      if (url.startsWith("/api/history?")) return response(200, []);
      if (url === "/api/sets") {
        return new Promise((resolve) => { resolveDirectSend = resolve; });
      }
      if (url === "/api/finish") {
        finishRequested = true;
        return response(200, { duration_min: 1, sets: 1, volume_kg: 600 });
      }
      throw new Error(`Unexpected request: ${url}`);
    },
    failSetItem: (key) => key === "gym_queue",
  });

  assert.equal(await waitFor(() => app.elements.get("#screen").innerHTML.includes("class=\"exercise-list\"")), true);
  await app.click("open-exercise", { exercise: "Squats" });
  app.input(0, "weight_kg", "60");
  app.input(0, "reps", "10");
  const sending = app.click("log-draft", { index: "0" });
  assert.equal(await waitFor(() => typeof resolveDirectSend === "function"), true);

  const finishing = app.click("finish");
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(finishRequested, false);

  resolveDirectSend(response(503, { detail: "offline" }));
  await sending;
  await finishing;

  assert.equal(finishRequested, false);
  assert.match(app.elements.get("#notice-host").innerHTML, /set not saved/i);
});

test("hinzugefügte Satzzeilen unterstützen ebenfalls Änderungen und automatische Übernahme", async () => {
  const app = createFrontend({
    fetch: async (url) => {
      if (url === "/api/state") return response(200, makeState({
        prefill: [{ weight_kg: 60, reps: 10 }],
      }));
      if (url.startsWith("/api/history?")) return response(200, []);
      throw new Error(`Unexpected request: ${url}`);
    },
  });

  assert.equal(await waitFor(() => app.elements.get("#screen").innerHTML.includes("class=\"exercise-list\"")), true);
  await app.click("open-exercise", { exercise: "Squats" });
  await app.click("add-draft");
  app.input(0, "weight_kg", "62,5");
  app.input(1, "reps", "8");

  assert.equal(app.run('JSON.stringify(drafts.get("Squats").map((draft) => [draft.weight_kg, draft.reps]))'),
    JSON.stringify([["62,5", "10"], ["62,5", "8"]]));
});

test("offene Satzentwürfe werden pro Session, Übung und Satzindex nach Neuladen wiederhergestellt", async () => {
  const storage = new Map();
  const state = makeState({ prefill: [
    { weight_kg: 60, reps: 10 },
    { weight_kg: 55, reps: 12 },
  ] });
  const fetch = async (url) => {
    if (url === "/api/state") return response(200, state);
    if (url.startsWith("/api/history?")) return response(200, []);
    throw new Error(`Unexpected request: ${url}`);
  };
  const firstApp = createFrontend({ storage, fetch });

  assert.equal(await waitFor(() => firstApp.elements.get("#screen").innerHTML.includes("class=\"exercise-list\"")), true);
  await firstApp.click("open-exercise", { exercise: "Squats" });
  firstApp.input(0, "weight_kg", "72,5");
  firstApp.input(1, "reps", "7");
  await firstApp.click("add-draft");
  firstApp.input(2, "reps", "6");

  const reloadedApp = createFrontend({ storage, fetch });
  assert.equal(await waitFor(() => reloadedApp.elements.get("#screen").innerHTML.includes("<h1>Squats</h1>")), true);
  assert.equal(reloadedApp.draftInput(0, "weight_kg").value, "72,5");
  assert.equal(reloadedApp.draftInput(1, "reps").value, "7");
  assert.equal(reloadedApp.draftInput(2, "reps").value, "6");
});

test("offene Entwürfe bleiben erhalten, wenn der erste Satz die Session eröffnet", async () => {
  const storage = new Map();
  const state = makeState({
    session: null,
    prefill: [
      { weight_kg: 60, reps: 10 },
      { weight_kg: 55, reps: 12 },
    ],
  });
  const fetch = async (url, options = {}) => {
    if (url === "/api/state") return response(200, structuredClone(state));
    if (url.startsWith("/api/history?")) return response(200, []);
    if (url === "/api/sets") {
      const item = JSON.parse(options.body);
      const saved = { ...item, id: 12, set_index: 1 };
      state.session = { id: 42, started_at: "2026-10-14T10:00:00Z" };
      state.exercises[0].logged.push(saved);
      return response(200, saved);
    }
    throw new Error(`Unexpected request: ${url}`);
  };
  const firstApp = createFrontend({ storage, fetch });

  assert.equal(await waitFor(() => firstApp.elements.get("#screen").innerHTML.includes("class=\"exercise-list\"")), true);
  await firstApp.click("open-exercise", { exercise: "Squats" });
  firstApp.input(1, "weight_kg", "77,5");
  await firstApp.click("log-draft", { index: 0 });
  assert.equal(await waitFor(() => storage.get("gym_queue") === "[]"), true);
  assert.equal(JSON.parse(storage.get("gym_drafts"))["42"].Squats["2"].weight_kg, "77,5");

  const reloadedApp = createFrontend({ storage, fetch });
  assert.equal(await waitFor(() => reloadedApp.elements.get("#screen").innerHTML.includes("<h1>Squats</h1>")), true);
  assert.equal(reloadedApp.draftInput(0, "weight_kg").value, "77,5");
});

test("Entwürfe anderer Sessions werden beim Laden aufgeräumt", async () => {
  const storage = new Map([[
    "gym_drafts",
    JSON.stringify({
      1: { Squats: { 1: { weight_kg: "70", reps: "8" } } },
      2: { Squats: { 1: { weight_kg: "80", reps: "6" } } },
    }),
  ]]);
  const state = makeState({ prefill: [{ weight_kg: 60, reps: 10 }] });
  state.session.id = 2;
  const app = createFrontend({
    storage,
    fetch: async (url) => {
      if (url === "/api/state") return response(200, state);
      if (url.startsWith("/api/history?")) return response(200, []);
      throw new Error(`Unexpected request: ${url}`);
    },
  });

  assert.equal(await waitFor(() => app.elements.get("#screen").innerHTML.includes("class=\"exercise-list\"")), true);

  assert.deepEqual(JSON.parse(storage.get("gym_drafts")), {
    2: { Squats: { 1: { weight_kg: "80", reps: "6" } } },
  });
});

test("Entwürfe werden nach erfolgreichem Loggen und Abschließen der Session gelöscht", async () => {
  const storage = new Map();
  const state = makeState({ prefill: [
    { weight_kg: 60, reps: 10 },
    { weight_kg: 55, reps: 12 },
  ] });
  const app = createFrontend({
    storage,
    fetch: async (url, options = {}) => {
      if (url === "/api/state") return response(200, state);
      if (url.startsWith("/api/history?")) return response(200, []);
      if (url === "/api/sets") {
        const item = JSON.parse(options.body);
        const saved = { ...item, id: 12, set_index: 1 };
        state.exercises[0].logged.push(saved);
        return response(200, saved);
      }
      if (url === "/api/finish") return response(200, { duration_min: 1, sets: 1, volume_kg: 600 });
      throw new Error(`Unexpected request: ${url}`);
    },
  });

  assert.equal(await waitFor(() => app.elements.get("#screen").innerHTML.includes("class=\"exercise-list\"")), true);
  await app.click("open-exercise", { exercise: "Squats" });
  app.input(0, "weight_kg", "62,5");
  assert.equal(storage.has("gym_drafts"), true);
  await app.click("log-draft", { index: 0 });
  assert.equal(await waitFor(() => app.storage.get("gym_queue") === "[]"), true);
  assert.deepEqual(Object.keys(JSON.parse(app.storage.get("gym_drafts"))["1"].Squats), ["2"]);

  await app.click("back");
  await app.click("finish");
  assert.equal(app.elements.get("#screen").innerHTML.includes("Workout complete"), true);
  assert.equal(app.storage.has("gym_drafts"), false);
});

test("client_id eines Direktversands überlebt Reload und verhindert einen doppelten Satz", async () => {
  const storage = new Map();
  const state = makeState({ prefill: [{ weight_kg: 65, reps: 8 }] });
  const serverSets = new Map();
  const posts = [];
  const fetch = async (url, options = {}) => {
    if (url === "/api/state") return response(200, state);
    if (url.startsWith("/api/history?")) return response(200, []);
    if (url === "/api/sets" && options.method === "POST") {
      const item = JSON.parse(options.body);
      posts.push(item);
      const existing = serverSets.get(item.client_id);
      if (existing) return response(200, existing);
      const saved = { ...item, id: 91, set_index: 1 };
      serverSets.set(item.client_id, saved);
      throw new Error("response lost after server saved the set");
    }
    throw new Error(`Unexpected request: ${url}`);
  };
  const firstApp = createFrontend({
    storage,
    fetch,
    failSetItem: (key) => key === "gym_queue",
  });

  assert.equal(await waitFor(() => firstApp.elements.get("#screen").innerHTML.includes("class=\"exercise-list\"")), true);
  await firstApp.click("open-exercise", { exercise: "Squats" });
  firstApp.input(0, "weight_kg", "55");
  await firstApp.click("log-draft", { index: "0" });
  assert.equal(serverSets.size, 1);

  const reloadedApp = createFrontend({
    storage,
    fetch,
    failSetItem: (key) => key === "gym_queue",
  });
  assert.equal(await waitFor(() => reloadedApp.elements.get("#screen").innerHTML.includes("<h1>Squats</h1>")), true);
  await reloadedApp.click("log-draft", { index: "0" });

  assert.equal(posts.length, 2);
  assert.equal(posts[0].client_id, posts[1].client_id);
  assert.equal(serverSets.size, 1);
  assert.deepEqual(
    { weight_kg: [...serverSets.values()][0].weight_kg, reps: [...serverSets.values()][0].reps },
    { weight_kg: 55, reps: 8 },
  );
});

test("Serverstand und Queue mit gleicher client_id belegen nach Reload nur einen Satzindex", async () => {
  const alreadySaved = {
    id: 92,
    client_id: queuedSet.client_id,
    weight_kg: queuedSet.weight_kg,
    reps: queuedSet.reps,
    set_index: 1,
  };
  const state = makeState({
    logged: [alreadySaved],
    prefill: [{ weight_kg: 60, reps: 10 }, { weight_kg: 55, reps: 12 }],
  });
  const storage = new Map([["gym_queue", JSON.stringify([queuedSet])]]);
  let finishPost;
  const app = createFrontend({
    storage,
    fetch: async (url) => {
      if (url === "/api/state") return response(200, state);
      if (url === "/api/sets") return new Promise((resolve) => { finishPost = resolve; });
      if (url.startsWith("/api/history?")) return response(200, []);
      throw new Error(`Unexpected request: ${url}`);
    },
  });

  assert.equal(await waitFor(() => typeof finishPost === "function"), true);
  assert.equal(app.run("appState.exercises[0].logged.length"), 1);
  await app.click("open-exercise", { exercise: "Squats" });
  assert.equal(app.draftInput(0, "weight_kg")?.dataset.index, "0");
  assert.match(app.elements.get("#screen").innerHTML, /class="set-number">2<\/span>/);
  finishPost(response(200, alreadySaved));
  assert.equal(await waitFor(() => storage.get("gym_queue") === "[]"), true);
});

test("Entwürfe vor dem ersten Satz bleiben ohne Session über Reload erhalten", async () => {
  const storage = new Map();
  const fetch = async (url) => {
    if (url === "/api/state") return response(200, makeState({
      session: null,
      prefill: [{ weight_kg: 60, reps: 10 }],
    }));
    if (url.startsWith("/api/history?")) return response(200, []);
    throw new Error(`Unexpected request: ${url}`);
  };
  const firstApp = createFrontend({ storage, fetch });

  assert.equal(await waitFor(() => firstApp.elements.get("#screen").innerHTML.includes("class=\"exercise-list\"")), true);
  await firstApp.click("open-exercise", { exercise: "Squats" });
  firstApp.input(0, "weight_kg", "52,5");
  assert.equal(JSON.parse(storage.get("gym_drafts")).pending.Squats["1"].weight_kg, "52,5");

  const reloadedApp = createFrontend({ storage, fetch });
  assert.equal(await waitFor(() => reloadedApp.elements.get("#screen").innerHTML.includes("class=\"exercise-list\"")), true);
  await reloadedApp.click("open-exercise", { exercise: "Squats" });
  assert.equal(reloadedApp.draftInput(0, "weight_kg").value, "52,5");
});

test("beim Sessionstart werden Queue-Sätze vor dem Reindexieren offener Entwürfe eingerechnet", async () => {
  const queued = { ...queuedSet, client_id: "waiting-before-start", weight_kg: 60, reps: 10 };
  const storage = new Map([["gym_queue", JSON.stringify([queued])]]);
  const initial = makeState({
    session: null,
    prefill: [
      { weight_kg: 65, reps: 8 },
      { weight_kg: 60, reps: 10 },
      { weight_kg: 55, reps: 12 },
    ],
  });
  const started = makeState({
    session: { id: 42, started_at: "2026-10-14T10:00:00Z" },
    prefill: initial.exercises[0].prefill,
  });
  let finishPost;
  let stateRequests = 0;
  const app = createFrontend({
    storage,
    fetch: async (url) => {
      if (url === "/api/state") {
        stateRequests += 1;
        return response(200, stateRequests === 1 ? initial : started);
      }
      if (url === "/api/sets") return new Promise((resolve) => { finishPost = resolve; });
      if (url.startsWith("/api/history?")) return response(200, []);
      throw new Error(`Unexpected request: ${url}`);
    },
  });

  assert.equal(await waitFor(() => typeof finishPost === "function"), true);
  await app.click("open-exercise", { exercise: "Squats" });
  app.input(0, "weight_kg", "57,5");
  await app.run("refreshState()");

  const stored = JSON.parse(storage.get("gym_drafts"));
  assert.equal(stored["42"].Squats["2"].weight_kg, "57,5");
  assert.equal(stored["42"].Squats["3"].reps, "12");
  finishPost(response(200, { ...queued, id: 93, set_index: 1 }));
});

test("nicht verfügbares localStorage blockiert offene Satzentwürfe nicht", async () => {
  const app = createFrontend({
    failGetItem: (key) => key === "gym_drafts",
    failSetItem: (key) => key === "gym_drafts",
    fetch: async (url) => {
      if (url === "/api/state") return response(200, makeState({
        prefill: [{ weight_kg: 60, reps: 10 }],
      }));
      if (url.startsWith("/api/history?")) return response(200, []);
      throw new Error(`Unexpected request: ${url}`);
    },
  });

  assert.equal(await waitFor(() => app.elements.get("#screen").innerHTML.includes("class=\"exercise-list\"")), true);
  await app.click("open-exercise", { exercise: "Squats" });
  app.input(0, "weight_kg", "72,5");

  assert.equal(app.draftInput(0, "weight_kg").value, "72,5");
  assert.equal(app.run('drafts.get("Squats")[0].weight_kg'), "72,5");
});

test("gym_view wird bei offener Session gespeichert und nach Neuladen wiederhergestellt", async () => {
  const storage = new Map();
  const fetch = async (url) => {
    if (url === "/api/state") return response(200, makeState());
    if (url.startsWith("/api/history?")) return response(200, []);
    throw new Error(`Unexpected request: ${url}`);
  };
  const firstApp = createFrontend({ storage, fetch });

  assert.equal(await waitFor(() => firstApp.elements.get("#screen").innerHTML.includes("class=\"exercise-list\"")), true);
  await firstApp.click("open-exercise", { exercise: "Bench Press" });
  assert.equal(storage.get("gym_view"), JSON.stringify("Bench Press"));

  const reloadedApp = createFrontend({ storage, fetch });
  assert.equal(await waitFor(() => reloadedApp.elements.get("#screen").innerHTML.includes("<h1>Bench Press</h1>")), true);
});

test("Fehler beim Lesen oder Schreiben von gym_view blockieren die Übungsansicht nicht", async () => {
  const app = createFrontend({
    failGetItem: (key) => key === "gym_view",
    failSetItem: (key) => key === "gym_view",
    fetch: async (url) => {
      if (url === "/api/state") return response(200, makeState());
      if (url.startsWith("/api/history?")) return response(200, []);
      throw new Error(`Unexpected request: ${url}`);
    },
  });

  assert.equal(await waitFor(() => app.elements.get("#screen").innerHTML.includes("class=\"exercise-list\"")), true);
  await app.click("open-exercise", { exercise: "Bench Press" });
  assert.match(app.elements.get("#screen").innerHTML, /<h1>Bench Press<\/h1>/);
});

test("eine erledigte Einzelserie verwendet den Singular", async () => {
  const app = createFrontend({
    fetch: async (url) => {
      if (url === "/api/state") return response(200, makeState({
        logged: [{ id: 4, weight_kg: 60, reps: 10, set_index: 1 }],
      }));
      if (url.startsWith("/api/history?")) return response(200, []);
      throw new Error(`Unexpected request: ${url}`);
    },
  });

  assert.equal(await waitFor(() => app.elements.get("#screen").innerHTML.includes("class=\"exercise-list\"")), true);
  await app.click("open-exercise", { exercise: "Squats" });

  assert.match(app.elements.get("#screen").innerHTML, /1 set completed/);
  assert.doesNotMatch(app.elements.get("#screen").innerHTML, /1 sets completed/);
});

test("Verlauf wird nach einem synchronisierten Satz neu geladen", async () => {
  const syncedItem = { ...queuedSet, weight_kg: 62.5, reps: 9 };
  const storage = new Map([
    ["gym_queue", JSON.stringify([syncedItem])],
    ["gym_view", JSON.stringify("Squats")],
  ]);
  const state = makeState();
  let historyRequests = 0;
  const app = createFrontend({
    storage,
    fetch: async (url) => {
      if (url === "/api/state") return response(200, state);
      if (url === "/api/sets") {
        const saved = { ...syncedItem, id: 12, set_index: 1 };
        state.exercises[0].logged.push(saved);
        return response(200, saved);
      }
      if (url.startsWith("/api/history?")) {
        historyRequests += 1;
        return response(200, historyRequests === 1 ? [] : [{
          date: "2026-10-14",
          sets: [{ weight_kg: 62.5, reps: 9 }],
        }]);
      }
      throw new Error(`Unexpected request: ${url}`);
    },
  });

  assert.equal(await waitFor(() => /<span>62,5<\/span>\s*<span>9<\/span>/.test(app.elements.get("#screen").innerHTML)), true,
    `historyRequests=${historyRequests}; html=${app.elements.get("#screen").innerHTML}`);
  assert.ok(historyRequests >= 2);
});

test("eine ältere Verlauf-Anfrage überschreibt nicht das Ergebnis nach dem Sync", async () => {
  const storage = new Map([
    ["gym_queue", JSON.stringify([queuedSet])],
    ["gym_view", JSON.stringify("Squats")],
  ]);
  const state = makeState();
  let resolveInitialHistory;
  let historyRequests = 0;
  const app = createFrontend({
    storage,
    fetch: async (url) => {
      if (url === "/api/state") return response(200, state);
      if (url === "/api/sets") {
        const saved = { ...queuedSet, id: 14, set_index: 1 };
        state.exercises[0].logged.push(saved);
        return response(200, saved);
      }
      if (url.startsWith("/api/history?")) {
        historyRequests += 1;
        if (historyRequests === 1) {
          return new Promise((resolve) => { resolveInitialHistory = resolve; });
        }
        return response(200, [{
          date: "2026-10-14",
          sets: [{ weight_kg: 70, reps: 6 }],
        }]);
      }
      throw new Error(`Unexpected request: ${url}`);
    },
  });

  assert.equal(await waitFor(() => /<span>70<\/span>\s*<span>6<\/span>/.test(app.elements.get("#screen").innerHTML)), true);
  assert.equal(typeof resolveInitialHistory, "function");
  resolveInitialHistory(response(200, []));
  await new Promise((resolve) => setImmediate(resolve));

  assert.match(app.elements.get("#screen").innerHTML, /<span>70<\/span>\s*<span>6<\/span>/);
  assert.doesNotMatch(app.elements.get("#screen").innerHTML, /No previous workouts/);
});

test("ein fehlgeschlagener Verlauf zeigt den Hinweis 'Verlauf nicht verfügbar'", async () => {
  const failedHistoryApp = createFrontend({
    fetch: async (url) => {
      if (url === "/api/state") return response(200, makeState());
      if (url.startsWith("/api/history?")) throw new Error("offline");
      throw new Error(`Unexpected request: ${url}`);
    },
  });
  assert.equal(await waitFor(() => failedHistoryApp.elements.get("#screen").innerHTML.includes("class=\"exercise-list\"")), true);
  await failedHistoryApp.click("open-exercise", { exercise: "Squats" });
  assert.equal(await waitFor(() => failedHistoryApp.elements.get("#screen").innerHTML.includes("Workout history is unavailable")), true);
});

test("reconnect reloads state and flushes queued sets from the error screen", async () => {
  const queue = [{
    client_id: "synthetic-offline-id",
    exercise: "Squats",
    weight_kg: 20,
    reps: 5,
  }];
  const exercises = ["Squats", "Bench Press", "Deadlifts"].map((name) => ({
    name,
    rest_s: 180,
    prefill: [],
    logged: [],
  }));
  const state = { session: null, exercises };
  const calls = [];
  const documentListeners = new Map();
  const windowListeners = new Map();
  const elements = new Map([
    ["#screen", { innerHTML: "", querySelector: () => null }],
    ["#timer-host", { innerHTML: "" }],
    ["#sync-host", { innerHTML: "" }],
    ["#notice-host", { innerHTML: "" }],
  ]);
  let firstStateRequest = true;

  const context = {
    document: {
      body: { classList: { toggle() {}, remove() {} } },
      visibilityState: "visible",
      querySelector: (selector) => elements.get(selector),
      addEventListener: (name, callback) => documentListeners.set(name, callback),
    },
    window: {
      location: { reload() {} },
      confirm: () => true,
      addEventListener: (name, callback) => windowListeners.set(name, callback),
    },
    navigator: {},
    localStorage: {
      getItem(key) {
        if (key === "gym_queue") return JSON.stringify(queue);
        if (key === "gym_timer_end") return "0";
        return null;
      },
      setItem() {},
      removeItem() {},
    },
    fetch: async (url, options = {}) => {
      calls.push({ url, method: options.method || "GET" });
      if (url === "/api/state" && firstStateRequest) {
        firstStateRequest = false;
        return response(503, { error: "offline" });
      }
      if (url === "/api/state") return response(200, state);
      if (url === "/api/sets") {
        state.exercises[0].logged.push({ id: 7, weight_kg: 20, reps: 5, set_index: 1 });
        return response(200, {
          id: 7,
          exercise: "Squats",
          weight_kg: 20,
          reps: 5,
          set_index: 1,
          workout_id: 15,
          duplicate: false,
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    },
    setInterval: () => 1,
    clearInterval() {},
  };
  context.globalThis = context;

  vm.runInNewContext(appSource, context);
  const screen = elements.get("#screen");
  assert.equal(await waitFor(() => screen.innerHTML.includes("Connection lost")), true);

  windowListeners.get("online")();

  assert.equal(await waitFor(() => calls.some((call) => call.url === "/api/sets")), true);
  assert.ok(calls.some((call) => call.url === "/api/state" && call.method === "GET"));
  assert.equal(calls.filter((call) => call.url === "/api/sets").length, 1);
  assert.equal(documentListeners.has("visibilitychange"), true);
});

test("Übungssuche lädt Treffer passend zur Eingabe nach", async () => {
  const app = createFrontend({
    fetch: async (url) => {
      if (url === "/api/state") return response(200, makeState());
      if (url === "/api/exercises" || url.startsWith("/api/exercises?")) return response(200, []);
      throw new Error(`Unexpected request: ${url}`);
    },
  });

  assert.equal(await waitFor(() => app.elements.get("#screen").innerHTML.includes("class=\"exercise-list\"")), true);
  await app.click("tab", { tab: "exercises" });
  app.documentListeners.get("input")({
    target: { id: "library-search", value: "bench", matches: () => false },
  });

  assert.equal(await waitFor(() => app.calls.some((call) => call.url === "/api/exercises?search=bench")), true);
});

test("Tabwechsel aus der Trainingszusammenfassung öffnet den gewählten Bereich", async () => {
  const app = createFrontend({
    fetch: async (url) => {
      if (url === "/api/state") return response(200, makeState());
      if (url === "/api/workouts?limit=50") return response(200, []);
      throw new Error(`Unexpected request: ${url}`);
    },
  });

  assert.equal(await waitFor(() => app.elements.get("#screen").innerHTML.includes("class=\"exercise-list\"")), true);
  app.run('screenMode = "summary"; finishSummary = { sets: 1, duration_min: 1, volume_kg: 10 }; render();');
  assert.match(app.elements.get("#screen").innerHTML, /Workout complete/);

  await app.click("tab", { tab: "history" });

  assert.match(app.elements.get("#screen").innerHTML, /No completed workouts yet/);
});

test("offene Session zeigt auch ohne aktiven Plan oben Fortsetzen", async () => {
  const app = createFrontend({
    fetch: async (url) => {
      if (url === "/api/state") return response(200, makeState());
      throw new Error(`Unexpected request: ${url}`);
    },
  });

  assert.equal(await waitFor(() => app.elements.get("#screen").innerHTML.includes("data-action=\"continue-session\"")), true);
  assert.match(app.elements.get("#screen").innerHTML, /Continue/);
});

test("Zielanzeige: Sekunden-Übungen zeigen 's', reine Ziele ohne Spanne", async () => {
  const state = {
    session: { id: 1, started_at: "2026-10-14T10:00:00Z" },
    exercises: [
      { name: "Weighted Deadhang", rest_s: 180, rep_min: 30, rep_max: 40, target_unit: "seconds", prefill: [{ weight_kg: 20, reps: 34 }], logged: [] },
      { name: "Bench Press", rest_s: 180, rep_min: 4, rep_max: 4, target_unit: "reps", prefill: [], logged: [] },
    ],
  };
  const app = createFrontend({
    fetch: async (url) => {
      if (url === "/api/state") return response(200, state);
      if (url.startsWith("/api/history?")) return response(200, []);
      throw new Error(`Unexpected request: ${url}`);
    },
  });
  const screen = () => app.elements.get("#screen").innerHTML;

  assert.equal(await waitFor(() => screen().includes("class=\"exercise-list\"")), true);
  await app.click("open-exercise", { exercise: "Weighted Deadhang" });
  assert.match(screen(), /Target 30–40 s/);
  assert.doesNotMatch(screen(), /Wdh\./);

  await app.click("open-exercise", { exercise: "Bench Press" });
  assert.match(screen(), /Target 4 reps/);
});

test("Sekunden-Übungen lassen bis 600 s zu, Wdh.-Übungen nur bis 100", async () => {
  const state = {
    session: { id: 1, started_at: "2026-10-14T10:00:00Z" },
    exercises: [
      { name: "Weighted Deadhang", rest_s: 180, rep_min: 30, rep_max: 40, target_unit: "seconds", prefill: [], logged: [] },
      { name: "Bench Press", rest_s: 180, rep_min: 4, rep_max: 6, target_unit: "reps", prefill: [], logged: [] },
    ],
  };
  const app = createFrontend({
    fetch: async (url, options) => {
      if (url === "/api/state") return response(200, state);
      if (url.startsWith("/api/history?")) return response(200, []);
      if (url === "/api/sets") {
        const body = JSON.parse(options.body);
        return response(200, { ...body, id: 5, set_index: 1 });
      }
      throw new Error(`Unexpected request: ${url}`);
    },
  });

  assert.equal(await waitFor(() => app.elements.get("#screen").innerHTML.includes("class=\"exercise-list\"")), true);
  await app.click("open-exercise", { exercise: "Bench Press" });
  app.input(0, "weight_kg", "60");
  app.input(0, "reps", "150");
  await app.click("log-draft", { index: "0" });
  assert.equal(app.calls.filter((call) => call.url === "/api/sets").length, 0);

  await app.click("open-exercise", { exercise: "Weighted Deadhang" });
  app.input(0, "weight_kg", "20");
  app.input(0, "reps", "150");
  await app.click("log-draft", { index: "0" });
  assert.equal(app.calls.filter((call) => call.url === "/api/sets").length, 1);
});

test("Start zeigt die Wochenleiste, Plantage und vier Navigation-Icons ohne Text", async () => {
  const home = {
    active_plan: {
      id: 7,
      name: "Lower A",
      active: true,
      days: [{ id: 8, name: "Lower A", exercises: [{ name: "Squats" }, { name: "Leg Press" }] }],
    },
    week_number: 8,
    week_days: [
      { date: "2035-06-11", label: "Mon", trained: true, today: false },
      { date: "2035-06-12", label: "Tue", trained: false, today: true },
    ],
    next_day: { id: 8, name: "Lower A" },
  };
  const app = createFrontend({
    fetch: async (url) => {
      if (url === "/api/state") return response(200, makeState({ home }));
      throw new Error(`Unexpected request: ${url}`);
    },
  });

  assert.equal(await waitFor(() => app.elements.get("#screen").innerHTML.includes("Lower A")), true);
  const screen = app.elements.get("#screen").innerHTML;
  const tabs = app.elements.get("#tab-bar").innerHTML;
  assert.match(screen, /data-action="start-day" data-day-id="8"/);
  assert.match(screen, /Week 8/);
  assert.match(screen, /data-action="open-library"/);
  assert.deepEqual([...tabs.matchAll(/data-tab="([^"]+)"/g)].map((item) => item[1]), [
    "training", "history", "profile", "plans",
  ]);
  assert.doesNotMatch(tabs, /<strong>/);
});

test("Trainings-Sheet rechnet einen Satz als 10RM um und erlaubt ein anderes Ziel", async () => {
  const app = createFrontend({
    fetch: async (url) => {
      if (url === "/api/state") return response(200, makeState({ prefill: [{ weight_kg: 60, reps: 8 }] }));
      if (url.startsWith("/api/history?")) return response(200, []);
      throw new Error(`Unexpected request: ${url}`);
    },
  });
  const screen = () => app.elements.get("#screen").innerHTML;

  assert.equal(await waitFor(() => screen().includes("class=\"exercise-list\"")), true);
  await app.click("open-exercise", { exercise: "Squats" });

  assert.match(screen(), /class="workout-sheet"/);
  assert.match(screen(), /data-tenrm-value="57"/);
  await app.click("edit-tenrm-target");
  assert.match(screen(), /data-tenrm-target/);
  app.run("tenRmTarget = 8; render();");
  assert.match(screen(), /data-tenrm-value="60"/);
  assert.match(screen(), /data-tenrm-target/);
});

test("Zeitübungen im Trainings-Sheet zeigen SECS und keine 10RM-Spalte", async () => {
  const state = {
    session: { id: 1, title: "Lower A", started_at: "2026-10-14T10:00:00Z" },
    exercises: [{
      name: "Weighted Deadhang", rest_s: 180, rep_min: 30, rep_max: 40,
      target_unit: "seconds", prefill: [{ weight_kg: 20, reps: 34 }], logged: [],
    }],
    home: { active_plan: { name: "Lower A", days: [] }, week_days: [] },
  };
  const app = createFrontend({
    fetch: async (url) => {
      if (url === "/api/state") return response(200, state);
      if (url.startsWith("/api/history?")) return response(200, []);
      throw new Error(`Unexpected request: ${url}`);
    },
  });
  const screen = () => app.elements.get("#screen").innerHTML;

  assert.equal(await waitFor(() => screen().includes("class=\"exercise-list\"")), true);
  await app.click("open-exercise", { exercise: "Weighted Deadhang" });

  assert.match(screen(), />SECS</);
  assert.doesNotMatch(screen(), /10RM/);
});

test("gültiges Abhaken gibt ein kurzes Vibrationssignal aus", async () => {
  const app = createFrontend({
    fetch: async (url, options = {}) => {
      if (url === "/api/state") return response(200, makeState({ prefill: [{ weight_kg: 40, reps: 8 }] }));
      if (url.startsWith("/api/history?")) return response(200, []);
      if (url === "/api/sets") {
        return response(200, { id: 91, weight_kg: 40, reps: 8, set_index: 1 });
      }
      throw new Error(`Unexpected request: ${url} ${options.method || "GET"}`);
    },
  });

  assert.equal(await waitFor(() => app.elements.get("#screen").innerHTML.includes("class=\"exercise-list\"")), true);
  await app.click("open-exercise", { exercise: "Squats" });
  await app.click("log-draft", { index: "0" });

  assert.deepEqual(app.vibrations, [12]);
});

test("Verlauf zeigt Wochenchart, Zeitfilter und Trainingskacheln", async () => {
  const workouts = [{
    id: 8, title: "Upper A", date: "2035-06-12", duration_min: 42, set_count: 12,
    exercise_count: 4, volume_kg: 5800,
    exercises: [{ id: 1, name: "Bench Press", muscle: "Chest" }, { id: 2, name: "Cable Row", muscle: "Back" }, { id: 3, name: "Press", muscle: "Shoulders" }],
  }];
  const overview = {
    workout_count: 1, streak_weeks: 1,
    weekly: [{ week_start: "2035-06-11", workout_count: 1, volume_kg: 5800 }],
    latest_measurement: null,
  };
  const app = createFrontend({
    fetch: async (url) => {
      if (url === "/api/state") return response(200, makeState());
      if (url === "/api/workouts?limit=50") return response(200, workouts);
      if (url.startsWith("/api/overview?weeks=")) return response(200, overview);
      throw new Error(`Unexpected request: ${url}`);
    },
  });
  const screen = () => app.elements.get("#screen").innerHTML;

  await waitFor(() => screen().includes('class="exercise-list"'));
  await app.click("tab", { tab: "history" });
  assert.match(screen(), /<h1>Training<\/h1>/);
  assert.match(screen(), /1 workout/);
  assert.match(screen(), /class="week-chart"/);
  assert.match(screen(), /Tue, Jun 12/);
  assert.match(screen(), /\+1/);
  await app.click("set-chart-metric", { metric: "volume" });
  assert.match(screen(), /aria-label="Weekly volume chart"/);
  await app.click("set-overview-weeks", { weeks: "12" });
  assert.ok(app.calls.some((call) => call.url === "/api/overview?weeks=12"));
});

test("Profil zeigt Streak, Messwerte und speichert einen synthetischen Eintrag", async () => {
  const overview = {
    workout_count: 28, streak_weeks: 4, weekly: [],
    latest_measurement: { date: "2035-06-12", weight_kg: 72.5, body_fat: 18.2 },
  };
  const app = createFrontend({
    fetch: async (url) => {
      if (url === "/api/state") return response(200, makeState());
      if (url.startsWith("/api/overview?")) return response(200, overview);
      if (url === "/api/measurements") return response(200, overview.latest_measurement);
      throw new Error(`Unexpected request: ${url}`);
    },
  });
  const screen = () => app.elements.get("#screen").innerHTML;

  await waitFor(() => screen().includes('class="exercise-list"'));
  await app.click("tab", { tab: "profile" });
  assert.match(screen(), /28/);
  assert.match(screen(), /4w/);
  assert.match(screen(), /72,5 kg/);
  assert.match(screen(), /18,2%/);
  await app.click("toggle-measurement-form");
  assert.match(screen(), /id="new-measurement-form"/);
  await app.documentListeners.get("submit")({
    target: { id: "new-measurement-form", weight_kg: "73.1", body_fat: "17.9", date: "2035-06-13" },
    preventDefault() {},
  });
  const request = app.calls.find((call) => call.url === "/api/measurements");
  assert.equal(request.method, "POST");
  assert.deepEqual(JSON.parse(request.body), { weight_kg: 73.1, body_fat: 17.9, date: "2035-06-13" });
});

test("Übungsdetail hebt zugeordnete Muskeln hervor und speichert Instructions", async () => {
  const app = createFrontend({
    fetch: async (url) => {
      if (url === "/api/state") return response(200, makeState());
      if (url === "/api/exercises/77") return response(200, {
        id: 77, name: "Synthetic Press", muscle: "Chest and triceps", equipment: "barbell",
        category: "strength", rep_min: 6, rep_max: 10, instructions: "Keep the wrists straight.", history: [],
      });
      throw new Error(`Unexpected request: ${url}`);
    },
  });
  const screenElement = app.elements.get("#screen");
  await waitFor(() => screenElement.innerHTML.includes('class="exercise-list"'));
  app.run(`activeTab="exercises"; exerciseDirectory=[{id:78,name:"Other",muscle:"Chest"}]; exerciseDetail={
    id:77,name:"Synthetic Press",muscle:"Chest and triceps",equipment:"barbell",category:"strength",
    rep_min:6,rep_max:10,instructions:"Keep the wrists straight.",history:[]};
    exerciseInstructionsDraft="Keep the wrists straight."; render();`);

  assert.match(screenElement.innerHTML, /data-muscle="chest" d="[^"]+"\>.*?<\/path>/);
  assert.match(screenElement.innerHTML, /class="muscle-region is-highlighted" data-muscle="chest"/);
  for (const muscle of ["shoulders", "biceps", "triceps", "forearms", "abs", "quads", "hams", "glutes", "calves", "lats", "traps", "lower back", "neck"]) {
    assert.match(screenElement.innerHTML, new RegExp(`data-muscle="${muscle}"`));
  }
  await app.click("exercise-detail-tab", { detailTab: "instructions" });
  assert.match(screenElement.innerHTML, /Keep the wrists straight\./);
  screenElement.querySelector = () => ({ value: "Pause at the bottom." });
  await app.click("save-exercise-instructions");
  const update = app.calls.find((call) => call.url === "/api/exercises/77");
  assert.equal(update.method, "PATCH");
  assert.deepEqual(JSON.parse(update.body), { instructions: "Pause at the bottom." });
});

test("Session-Übung kann über den Picker ersetzt werden", async () => {
  const state = makeState();
  state.exercises[0].session_exercise_id = 101;
  state.exercises[0].sort_order = 0;
  const app = createFrontend({
    fetch: async (url, options = {}) => {
      if (url === "/api/state") return response(200, state);
      if (url === "/api/history?exercise=Synthetic+Cable+Row&limit=5") return response(200, []);
      if (url === "/api/exercises") return response(200, [{ id: 55, name: "Synthetic Cable Row", muscle: "Back" }]);
      if (url === "/api/sessions/1/exercises/101" && options.method === "PATCH") {
        assert.deepEqual(JSON.parse(options.body), { exercise_id: 55 });
        state.exercises[0].name = "Synthetic Cable Row";
        state.exercises[0].exercise_id = 55;
        return response(200, state.exercises[0]);
      }
      throw new Error(`Unexpected request: ${url} ${options.method || "GET"}`);
    },
  });
  const screen = () => app.elements.get("#screen").innerHTML;

  await waitFor(() => screen().includes('class="exercise-list"'));
  await app.click("open-exercise", { exercise: "Squats" });
  assert.match(screen(), /data-action="open-session-exercise-picker"/);
  await app.click("open-exercise-menu");
  await app.click("replace-session-exercise");
  await app.click("pick-exercise", { kind: "replace", exerciseId: "55" });
  assert.ok(app.calls.some((call) => call.url === "/api/sessions/1/exercises/101" && call.method === "PATCH"));
  assert.match(screen(), /Synthetic Cable Row/);
});

test("Rep-Ziel einer Session-Übung lässt sich speichern", async () => {
  const state = makeState();
  state.exercises[0].session_exercise_id = 101;
  state.exercises[0].sort_order = 0;
  state.exercises[0].rep_min = 6;
  state.exercises[0].rep_max = 10;
  const app = createFrontend({
    fetch: async (url, options = {}) => {
      if (url === "/api/state") return response(200, state);
      if (url.startsWith("/api/history?")) return response(200, []);
      if (url === "/api/sessions/1/exercises/101" && options.method === "PATCH") {
        return response(200, { ...state.exercises[0], rep_min: 8, rep_max: 12 });
      }
      throw new Error(`Unexpected request: ${url} ${options.method || "GET"}`);
    },
  });
  const screenElement = app.elements.get("#screen");

  await waitFor(() => screenElement.innerHTML.includes('class="exercise-list"'));
  await app.click("open-exercise", { exercise: "Squats" });
  await app.click("edit-rep-target");
  assert.match(screenElement.innerHTML, /data-rep-target="rep_min"/);
  screenElement.querySelector = (selector) => ({ value: selector.includes("rep_min") ? "8" : "12" });
  await app.click("save-session-target");
  const update = app.calls.find((call) => call.url === "/api/sessions/1/exercises/101");
  assert.equal(update.method, "PATCH");
  assert.deepEqual(JSON.parse(update.body), { rep_min: 8, rep_max: 12 });
  assert.doesNotMatch(screenElement.innerHTML, /data-rep-target=/);
});

test("Session-Übung kann ohne gespeicherte Sätze entfernt werden", async () => {
  const state = makeState();
  state.exercises[0].session_exercise_id = 101;
  state.exercises[0].sort_order = 0;
  const app = createFrontend({
    fetch: async (url, options = {}) => {
      if (url === "/api/state") return response(200, state);
      if (url === "/api/sessions/1/exercises/101" && options.method === "DELETE") {
        state.exercises = [];
        return response(204, null);
      }
      throw new Error(`Unexpected request: ${url} ${options.method || "GET"}`);
    },
  });
  const screen = () => app.elements.get("#screen").innerHTML;

  await waitFor(() => screen().includes('class="exercise-list"'));
  await app.click("open-exercise", { exercise: "Squats" });
  await app.click("open-exercise-menu");
  await app.click("remove-session-exercise");
  assert.ok(app.calls.some((call) => call.url === "/api/sessions/1/exercises/101" && call.method === "DELETE"));
  assert.match(screen(), /class="workout-sheet empty-training"/);
  assert.match(screen(), /data-action="open-session-exercise-picker"[^>]*>Add exercise</);
  assert.match(screen(), /data-action="finish"[^>]*>Finish workout</);
});

test("AP home shows plan days in one card with compact week labels and English navigation", async () => {
  const home = {
    active_plan: {
      name: "New plan",
      days: [
        { id: 4, name: "Lower A", exercises: [{ name: "Leg Press", muscle: "Quads" }, { name: "RDL", muscle: "Hams" }, { name: "Calf Raise", muscle: "Calves" }, { name: "Plank", muscle: "Abs" }] },
        { id: 5, name: "Upper A", exercises: [{ name: "Bench Press", muscle: "Chest" }] },
      ],
    },
    week_number: 8,
    next_day: { id: 4 },
    week_days: [
      { date: "2035-10-01", today: true, trained: true },
      { date: "2035-10-02", trained: false },
      { date: "2035-10-03", trained: false },
      { date: "2035-10-04", trained: false },
      { date: "2035-10-05", trained: false },
      { date: "2035-10-06", trained: false },
      { date: "2035-10-07", trained: false },
    ],
  };
  const app = createFrontend({
    fetch: async (url) => {
      if (url === "/api/state") return response(200, makeState({ session: null, home }));
      throw new Error(`Unexpected request: ${url}`);
    },
  });
  const screen = () => app.elements.get("#screen").innerHTML;

  assert.equal(await waitFor(() => screen().includes("Lower A")), true);
  assert.match(screen(), /class="plan-day-list-card"/);
  assert.match(screen(), /class="plan-day-row/);
  assert.match(screen(), /Week 8/);
  assert.match(screen(), /All plans/);
  assert.match(screen(), /Exercises/);
  assert.match(screen(), /Help/);
  assert.match(screen(), /\+1/);
  assert.doesNotMatch(screen(), /Mantis Gym|>Gym<|2 exercises|1 exercise/);

  const heading = screen().match(/<section class="home-plan-heading">([\s\S]*?)<\/section>/)?.[1] ?? "";
  assert.ok(heading.indexOf("plan-menu-button") < heading.indexOf("week-number"));
  assert.match(screen(), /week-day is-today is-trained/);
  assert.match(cssSource, /\.home-links\s*\{[^}]*display:\s*grid;[^}]*justify-items:\s*start;/s);
  assert.match(cssSource, /\.week-day\.is-today\s+strong\s*\{[^}]*background:\s*transparent;/s);
});

test("Gym app shell uses English accessibility labels and no legacy brand name", () => {
  assert.match(htmlSource, /<html lang="en">/);
  assert.match(htmlSource, /aria-label="Workout navigation"/);
  assert.match(htmlSource, /<meta name="description" content="Workouts, sets, and history in one place\."/);
  assert.doesNotMatch(htmlSource, /Mantis Gym|Gym-Bereiche/);
  assert.doesNotMatch(manifestSource, /Mantis Gym-Tracker/);
});

test("set entry uses large numeric pills and keeps both fields wide from 360 to 430 px", async () => {
  const app = createFrontend({
    fetch: async (url) => {
      if (url === "/api/state") return response(200, makeState({ prefill: [{ weight_kg: 60, reps: 8 }] }));
      if (url.startsWith("/api/history?")) return response(200, []);
      throw new Error(`Unexpected request: ${url}`);
    },
  });
  const screen = () => app.elements.get("#screen").innerHTML;

  assert.equal(await waitFor(() => screen().includes('class="exercise-list"')), true);
  await app.click("open-exercise", { exercise: "Squats" });

  assert.doesNotMatch(screen(), /data-action="adjust-draft"/);
  assert.match(screen(), /data-draft-input[^>]*inputmode="decimal"/);
  assert.match(screen(), /data-draft-input[^>]*inputmode="numeric"/);

  const grid = cssSource.match(/\.set-heading,\s*\.set-row\s*\{[^}]*grid-template-columns:\s*([^;]+);[^}]*gap:\s*(\d+(?:\.\d+)?)px/s);
  assert.ok(grid, "set rows define a measurable grid");
  const tracks = grid[1].trim().split(/\s+/);
  assert.equal(tracks.length, 5);
  const flexibleCount = tracks.filter((track) => /fr\)$/.test(track)).length;
  const fixedWidth = tracks.reduce((total, track) => total + (Number(track.match(/^(\d+(?:\.\d+)?)px$/)?.[1]) || 0), 0);
  const gapWidth = Number(grid[2]) * (tracks.length - 1);
  for (const viewport of [360, 375, 430]) {
    const sheetPadding = viewport <= 360 ? 14 : 20;
    const contentWidth = viewport - sheetPadding * 2;
    const fieldWidth = (contentWidth - fixedWidth - gapWidth) / flexibleCount;
    assert.ok(fieldWidth >= 80, `${viewport}px gives each KG/REPS pill ${fieldWidth.toFixed(1)}px`);
  }
});

test("typing into a set updates its 10RM and auto-filled following rows immediately", async () => {
  const app = createFrontend({
    fetch: async (url) => {
      if (url === "/api/state") return response(200, makeState({
        prefill: [{ weight_kg: 60, reps: 8 }, { weight_kg: 60, reps: 8 }],
      }));
      if (url.startsWith("/api/history?")) return response(200, []);
      throw new Error(`Unexpected request: ${url}`);
    },
  });
  const screen = () => app.elements.get("#screen").innerHTML;

  assert.equal(await waitFor(() => screen().includes('class="exercise-list"')), true);
  await app.click("open-exercise", { exercise: "Squats" });
  const first = app.draftInput(0, "weight_kg");
  const second = app.draftInput(1, "weight_kg");
  assert.equal(app.tenRmValue(first.dataset.clientId), "57 kg");
  assert.equal(app.tenRmValue(second.dataset.clientId), "57 kg");

  app.input(0, "weight_kg", "70");

  assert.equal(app.tenRmValue(first.dataset.clientId), "66,5 kg");
  assert.equal(app.tenRmValue(second.dataset.clientId), "66,5 kg");
  assert.equal(app.draftInput(1, "weight_kg").value, "70");
});

test("empty active workout offers exercise picker and finish action", async () => {
  const app = createFrontend({
    fetch: async (url) => {
      if (url === "/api/state") return response(200, {
        session: { id: 12, title: "Lower A", started_at: "2035-10-01T12:00:00Z" },
        exercises: [],
        home: { active_plan: { name: "Lower A", days: [] }, week_days: [] },
      });
      if (url === "/api/exercises") return response(200, []);
      throw new Error(`Unexpected request: ${url}`);
    },
  });
  const screen = () => app.elements.get("#screen").innerHTML;

  assert.equal(await waitFor(() => screen().includes('class="exercise-list"') || screen().includes('class="empty-training"')), true);
  assert.match(screen(), /class="workout-sheet empty-training"/);
  assert.match(screen(), /data-action="open-session-exercise-picker"[^>]*>[^<]*Add exercise/);
  assert.match(screen(), /data-action="finish"[^>]*>[^<]*Finish workout/);
  await app.click("open-session-exercise-picker");
  assert.match(screen(), /class="picker-modal exercise-picker"/);
});

test("sheet swipe ignores scrolling gestures but closes from the grip", async () => {
  const app = createFrontend({
    fetch: async (url) => {
      if (url === "/api/state") return response(200, makeState());
      if (url.startsWith("/api/history?")) return response(200, []);
      throw new Error(`Unexpected request: ${url}`);
    },
  });
  const screen = () => app.elements.get("#screen").innerHTML;

  assert.equal(await waitFor(() => screen().includes('class="exercise-list"')), true);
  await app.click("open-exercise", { exercise: "Squats" });
  const scrolledContent = app.beginTouch({ y: 30, scrollTop: 42 });
  app.endTouch(scrolledContent, 150);
  assert.equal(app.run("selectedExercise"), "Squats");

  const grip = app.beginTouch({ y: 30, scrollTop: 42, handle: true });
  app.endTouch(grip, 150);
  assert.equal(app.run("selectedExercise"), null);
});

test("workout history uses plan-day names and the set editor is a compact dialog", async () => {
  const app = createFrontend({
    fetch: async (url) => {
      if (url === "/api/state") return response(200, makeState());
      throw new Error(`Unexpected request: ${url}`);
    },
  });
  const screen = () => app.elements.get("#screen").innerHTML;
  await waitFor(() => screen().includes('class="exercise-list"'));
  app.run(`activeTab = "history"; workoutDetail = {
    title: "Upper B · Day 4 · Week 8 · New plan", date: "2026-10-08", duration_min: 64,
    set_count: 2, volume_kg: 720,
    sets: [
      { id: 71, exercise_id: 5, exercise: "Bench Press", set_index: 1, weight_kg: 60, reps: 8, unit: "reps", is_warmup: false },
      { id: 72, exercise_id: 5, exercise: "Bench Press", set_index: 2, weight_kg: 60, reps: 8, unit: "reps", is_warmup: false },
    ],
  }; render();`);

  assert.match(screen(), /<h1>Upper B<\/h1>/);
  assert.match(screen(), /Thu, Oct 8th · 64 min/);
  assert.match(screen(), /class="workout-exercise-card"/);
  assert.match(screen(), />#<\/span>[\s\S]*>KG<\/span>[\s\S]*>REPS<\/span>/);
  assert.doesNotMatch(screen(), /Day 4|Week 8|data-action="delete-history-set"|>Bearbeiten<|>Löschen</);

  await app.click("edit-history-set", { setId: "71" });
  assert.match(screen(), /class="history-set-dialog"[^>]*role="dialog"/);
  assert.match(screen(), /data-action="delete-history-set"/);
  assert.match(screen(), /data-history-edit[^>]*inputmode="decimal"/);
  assert.match(screen(), /data-history-edit[^>]*inputmode="numeric"/);
});

test("history list shortens workout titles and formats ordinal English dates", async () => {
  const app = createFrontend({
    fetch: async (url) => {
      if (url === "/api/state") return response(200, makeState());
      throw new Error(`Unexpected request: ${url}`);
    },
  });
  const screen = () => app.elements.get("#screen").innerHTML;
  await waitFor(() => screen().includes('class="exercise-list"'));
  app.run(`activeTab = "history"; workoutHistory = [{ id: 8,
    title: "Upper B · Day 4 · Week 8 · New plan", date: "2026-10-08", exercise_count: 1,
    exercises: [{ name: "Bench Press", muscle: "Chest" }],
  }]; overviewData = { workout_count: 1, weekly: [], streak_weeks: 0 }; render();`);

  assert.match(screen(), /<strong>Upper B<\/strong>/);
  assert.match(screen(), /Thu, Oct 8th/);
  assert.doesNotMatch(screen(), /Day 4|Week 8|Mantis Gym/);
  assert.match(screen(), /class="workout-list-card"/);
});

test("exercise library is an English sheet with search, muscle pills and equipment rows", async () => {
  const app = createFrontend({
    fetch: async (url) => {
      if (url === "/api/state") return response(200, makeState());
      throw new Error(`Unexpected request: ${url}`);
    },
  });
  const screen = () => app.elements.get("#screen").innerHTML;
  await waitFor(() => screen().includes('class="exercise-list"'));
  app.run(`activeTab = "exercises"; exerciseDirectory = [
    { id: 41, name: "Bench Press", muscle: "Chest", equipment: "barbell" },
    { id: 42, name: "Lat Pulldown", muscle: "Back", equipment: "cable" },
  ]; render();`);

  assert.match(screen(), /class="exercise-library-page sheet-dismissable"/);
  assert.match(screen(), />Close<\/button>/);
  assert.match(screen(), /<h1>Exercises<\/h1>/);
  assert.match(screen(), /placeholder="Search exercises"/);
  assert.match(screen(), /data-action="filter-library-muscle"/);
  assert.match(screen(), /class="exercise-library-card"/);
  assert.match(screen(), /class="exercise-library-tile"/);
  assert.match(screen(), /<small>Barbell<\/small>/);
  assert.match(cssSource, /\.sheet-dismissable\.history-page,\s*\.sheet-dismissable\.exercise-detail-page,\s*\.sheet-dismissable\.exercise-library-page\s*\{[^}]*position:\s*fixed;/s);
  assert.doesNotMatch(screen(), /Mantis Gym|>Übungen<|>Suche<|>Muskel</);

  const librarySheet = app.beginTouch({ y: 20, scrollTop: 0 });
  app.endTouch(librarySheet, 140);
  assert.equal(app.run("activeTab"), "training");
});

test("pause timer updates once for each fake second", async () => {
  const app = createFrontend({
    fetch: async (url) => {
      if (url === "/api/state") return response(200, makeState());
      throw new Error(`Unexpected request: ${url}`);
    },
  });

  assert.equal(await waitFor(() => app.elements.get("#screen").innerHTML.includes('class="exercise-list"')), true);
  app.run("startTimer(9)");
  const timer = () => app.elements.get("#timer-host").innerHTML;
  assert.match(timer(), />00:09</);
  app.advanceClock(1000);
  assert.match(timer(), />00:08</);
  app.advanceClock(1000);
  assert.match(timer(), />00:07</);
});

test("plan names from untrusted state stay HTML escaped", async () => {
  const dangerousName = '<img src=x onerror="alert(1)">';
  const home = {
    active_plan: { name: "Safe plan", days: [{ id: 1, name: dangerousName, exercises: [] }] },
    week_number: 1,
    week_days: [],
  };
  const app = createFrontend({
    fetch: async (url) => {
      if (url === "/api/state") return response(200, makeState({ session: null, home }));
      throw new Error(`Unexpected request: ${url}`);
    },
  });
  const screen = () => app.elements.get("#screen").innerHTML;

  assert.equal(await waitFor(() => screen().includes("Safe plan")), true);
  assert.match(screen(), /&lt;img src=x onerror=&quot;alert\(1\)&quot;&gt;/);
  assert.doesNotMatch(screen(), /<img src=x/);
});

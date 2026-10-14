import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const appSource = await readFile(new URL("../web/gym/app.js", import.meta.url), "utf8");

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

function makeState({ session = { id: 1, started_at: "2026-10-14T10:00:00Z" }, logged = [], prefill = [] } = {}) {
  return {
    session,
    exercises: ["Squats", "Bench Press", "Deadlifts"].map((name) => ({
      name,
      rest_s: 180,
      prefill: name === "Squats" ? prefill : [],
      logged: name === "Squats" ? logged : [],
    })),
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
  const elements = new Map([
    ["#screen", { innerHTML: "", querySelector: () => null }],
    ["#timer-host", { innerHTML: "" }],
    ["#sync-host", { innerHTML: "" }],
    ["#notice-host", { innerHTML: "" }],
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
    navigator: {},
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
      get(key) { return key === "token" ? this.form.token : null; }
    },
    URLSearchParams,
    crypto: { randomUUID: () => "vm-generated-client-id" },
    setInterval: () => { intervalId += 1; return intervalId; },
    clearInterval() {},
  });
  vm.runInContext(appSource, context);

  return {
    calls,
    documentListeners,
    elements,
    storage,
    windowListeners,
    get reloads() { return reloads; },
    click(action, data = {}) {
      const button = { dataset: { action, ...data } };
      return documentListeners.get("click")({
        target: { closest: (selector) => selector === "[data-action]" ? button : null },
      });
    },
    input(index, field, value) {
      const input = {
        dataset: { index: String(index), field },
        value,
        matches: (selector) => selector === "[data-draft-input]",
      };
      return documentListeners.get("input")({ target: input });
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

    assert.equal(await waitFor(() => app.elements.get("#screen").innerHTML.includes("Zugangsschlüssel")), true);
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
  assert.equal(app.elements.get("#screen").innerHTML.includes("Training beendet"), true);
  assert.match(app.elements.get("#screen").innerHTML, /<span>Satz<\/span><strong>1<\/strong>/);
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

  assert.equal(await waitFor(() => app.elements.get("#sync-host").innerHTML.includes("nicht synchronisiert")), true);
  await app.click("finish");

  assert.equal(finishRequested, false);
  assert.match(app.elements.get("#notice-host").innerHTML, /Erst synchronisieren/);
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

  assert.equal(await waitFor(() => app.elements.get("#screen").innerHTML.includes("Drei Übungen")), true);
  await app.click("open-exercise", { exercise: "Squats" });
  app.input(0, "weight_kg", "20");
  app.input(0, "reps", "5");
  await app.click("log-draft", { index: "0" });

  assert.equal(app.calls.filter((call) => call.url === "/api/sets").length, 1);
  assert.equal(app.elements.get("#screen").innerHTML.includes("set-row-complete"), false);
  assert.equal(app.elements.get("#screen").innerHTML.includes("data-action=\"log-draft\""), true);
  assert.match(app.elements.get("#notice-host").innerHTML, /nicht gespeichert/i);
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

  assert.equal(await waitFor(() => app.elements.get("#screen").innerHTML.includes("Drei Übungen")), true);
  await app.click("open-exercise", { exercise: "Squats" });
  app.input(0, "weight_kg", "60");
  app.input(0, "reps", "10");
  await app.click("log-draft", { index: "0" });

  assert.equal(app.calls.filter((call) => call.url === "/api/sets").length, 1);
  assert.match(app.elements.get("#screen").innerHTML, /data-set-id="13"/);
  assert.equal(app.elements.get("#screen").innerHTML.includes("data-action=\"log-draft\""), false);
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

  assert.equal(await waitFor(() => app.elements.get("#notice-host").innerHTML.includes("nicht synchronisiert")), true);
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

  assert.equal(await waitFor(() => app.elements.get("#screen").innerHTML.includes("Drei Übungen")), true);
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

  assert.equal(await waitFor(() => app.elements.get("#screen").innerHTML.includes("Drei Übungen")), true);
  await app.click("open-exercise", { exercise: "Squats" });
  await app.click("add-draft");
  app.input(0, "weight_kg", "62,5");
  app.input(1, "reps", "8");

  assert.equal(app.run('JSON.stringify(drafts.get("Squats").map((draft) => [draft.weight_kg, draft.reps]))'),
    JSON.stringify([["62,5", "10"], ["62,5", "8"]]));
});

test("gym_view wird bei offener Session gespeichert und nach Neuladen wiederhergestellt", async () => {
  const storage = new Map();
  const fetch = async (url) => {
    if (url === "/api/state") return response(200, makeState());
    if (url.startsWith("/api/history?")) return response(200, []);
    throw new Error(`Unexpected request: ${url}`);
  };
  const firstApp = createFrontend({ storage, fetch });

  assert.equal(await waitFor(() => firstApp.elements.get("#screen").innerHTML.includes("Drei Übungen")), true);
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

  assert.equal(await waitFor(() => app.elements.get("#screen").innerHTML.includes("Drei Übungen")), true);
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

  assert.equal(await waitFor(() => app.elements.get("#screen").innerHTML.includes("Drei Übungen")), true);
  await app.click("open-exercise", { exercise: "Squats" });

  assert.match(app.elements.get("#screen").innerHTML, /1 Satz erledigt/);
  assert.doesNotMatch(app.elements.get("#screen").innerHTML, /1 Sätze erledigt/);
});

test("Verlauf wird nach einem synchronisierten Satz neu geladen", async () => {
  const storage = new Map([
    ["gym_queue", JSON.stringify([queuedSet])],
    ["gym_view", JSON.stringify("Squats")],
  ]);
  const state = makeState();
  let historyRequests = 0;
  const app = createFrontend({
    storage,
    fetch: async (url) => {
      if (url === "/api/state") return response(200, state);
      if (url === "/api/sets") {
        const saved = { ...queuedSet, weight_kg: 62.5, reps: 9, id: 12, set_index: 1 };
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

  assert.equal(await waitFor(() => app.elements.get("#screen").innerHTML.includes("62,5×9")), true,
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

  assert.equal(await waitFor(() => app.elements.get("#screen").innerHTML.includes("70×6")), true);
  assert.equal(typeof resolveInitialHistory, "function");
  resolveInitialHistory(response(200, []));
  await new Promise((resolve) => setImmediate(resolve));

  assert.match(app.elements.get("#screen").innerHTML, /70×6/);
  assert.doesNotMatch(app.elements.get("#screen").innerHTML, /Noch keine früheren Trainings/);
});

test("ein fehlgeschlagener Verlauf zeigt den Hinweis 'Verlauf nicht verfügbar'", async () => {
  const failedHistoryApp = createFrontend({
    fetch: async (url) => {
      if (url === "/api/state") return response(200, makeState());
      if (url.startsWith("/api/history?")) throw new Error("offline");
      throw new Error(`Unexpected request: ${url}`);
    },
  });
  assert.equal(await waitFor(() => failedHistoryApp.elements.get("#screen").innerHTML.includes("Drei Übungen")), true);
  await failedHistoryApp.click("open-exercise", { exercise: "Squats" });
  assert.equal(await waitFor(() => failedHistoryApp.elements.get("#screen").innerHTML.includes("Verlauf nicht verfügbar")), true);
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
  assert.equal(await waitFor(() => screen.innerHTML.includes("Verbindung unterbrochen")), true);

  windowListeners.get("online")();

  assert.equal(await waitFor(() => calls.some((call) => call.url === "/api/sets")), true);
  assert.ok(calls.some((call) => call.url === "/api/state" && call.method === "GET"));
  assert.equal(calls.filter((call) => call.url === "/api/sets").length, 1);
  assert.equal(documentListeners.has("visibilitychange"), true);
});

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

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../tools/spotify/spicetify_ext/mantis-bridge.js", import.meta.url), "utf8");
const defaultServer = "http://127.0.0.1:7779";
const key = "mantis.dashboardToken:" + defaultServer;

function bridge(savedToken = "", savedServer = defaultServer) {
  const sockets = [], menus = [], timers = new Map();
  const storage = new Map([["mantis.backendUrl", savedServer]]);
  if (savedToken) storage.set("mantis.dashboardToken:" + savedServer, savedToken);
  let modal, timerId = 0;
  class Element {
    constructor(tag) { this.tag = tag; this.children = []; this.style = {}; this.value = ""; }
    append(...children) { this.children.push(...children); }
    setAttribute(name, value) { this[name] = value; }
    addEventListener(name, callback) { this[`on${name}`] = callback; }
    focus() {}
  }
  const Spicetify = {
    Player: {}, CosmosAsync: {},
    Menu: { Item: class {
      constructor(name, enabled, callback) { this.callback = callback; }
      register() { menus.push(this); }
    } },
    PopupModal: { display(data) { modal = data.content; }, hide() { modal = null; } },
    showNotification() {},
  };
  vm.runInNewContext(source, {
    window: { Spicetify }, Spicetify, URL,
    document: { createElement: (tag) => new Element(tag) },
    localStorage: {
      getItem: (k) => storage.get(k) || null,
      setItem: (k, v) => storage.set(k, v),
      removeItem: (k) => storage.delete(k),
    },
    WebSocket: class {
      constructor(url, protocols) { this.url = url; this.protocols = protocols; sockets.push(this); }
      close() { this.closed = true; this.onclose?.(); }
      send() {}
    },
    setTimeout(callback) { timers.set(++timerId, callback); return timerId; },
    clearTimeout(id) { timers.delete(id); },
  });
  return {
    sockets, storage, timers,
    save(token, server) {
      assert.equal(menus.length, 1);
      menus[0].callback();
      const children = modal.children.flatMap((el) => [el, ...el.children]);
      const input = children.find((el) => el.tag === "input" && el.type === "password");
      assert.ok(input, "masked token field");
      if (server !== undefined) {
        const serverInput = children.find((el) => el.tag === "input" && el.type === "url");
        assert.ok(serverInput, "server address field");
        serverInput.value = server;
        serverInput.oninput();
        assert.equal(input.value, "", "changing server must clear the old token");
      }
      if (token !== undefined) input.value = token;
      modal.onsubmit({ preventDefault() {} });
    },
  };
}

test("bridge does not connect before a dashboard token is configured", () => {
  const app = bridge();
  assert.equal(app.sockets.length, 0);
  app.save("secret-token");
  assert.equal(app.storage.get(key), "secret-token");
  assert.equal(app.sockets.length, 1);
  assert.deepEqual(Array.from(app.sockets[0].protocols), ["bearer.secret-token"]);
  assert.equal(app.sockets[0].url, "ws://127.0.0.1:7779/spicetify/ws");
});

test("saved token authenticates reconnects without entering the URL", () => {
  const app = bridge("saved-token");
  assert.deepEqual(Array.from(app.sockets[0].protocols), ["bearer.saved-token"]);
  app.sockets[0].onclose();
  assert.equal(app.timers.size, 1);
  [...app.timers.values()][0]();
  assert.deepEqual(Array.from(app.sockets[1].protocols), ["bearer.saved-token"]);
});

test("token rotation closes previous connection and cancels pending reconnect", () => {
  const app = bridge("old-token");
  app.sockets[0].onclose();
  app.save("new-token");
  assert.equal(app.sockets[0].closed, true);
  assert.equal(app.timers.size, 0);
  assert.equal(app.sockets.length, 2);
  assert.deepEqual(Array.from(app.sockets[1].protocols), ["bearer.new-token"]);
  app.save("");
  assert.equal(app.storage.has(key), false);
  assert.equal(app.sockets[1].closed, true);
  assert.equal(app.sockets.length, 2);
  assert.equal(app.timers.size, 0);
});

test("invalid websocket token is rejected without replacing credentials", () => {
  const app = bridge("good-token");
  app.save("invalid token");
  assert.equal(app.storage.get(key), "good-token");
  assert.equal(app.sockets.length, 1);
});

test("changing server never forwards the previous server token", () => {
  const app = bridge("local-token");
  app.save(undefined, "http://100.101.102.103:7779");
  assert.equal(app.sockets.length, 1);
  assert.equal(app.sockets[0].closed, true);
  assert.equal(app.storage.get("mantis.backendUrl"), "http://100.101.102.103:7779");
  app.save("tailnet-token");
  assert.equal(app.sockets[1].url, "ws://100.101.102.103:7779/spicetify/ws");
  assert.deepEqual(Array.from(app.sockets[1].protocols), ["bearer.tailnet-token"]);
  assert.equal(app.storage.get(key), "local-token");
  assert.equal(app.storage.get("mantis.dashboardToken:http://100.101.102.103:7779"), "tailnet-token");
});

test("saved HTTPS server connects securely using its own saved token", () => {
  const app = bridge("https-token", "https://mantis.example.test");
  assert.equal(app.sockets[0].url, "wss://mantis.example.test/spicetify/ws");
  assert.deepEqual(Array.from(app.sockets[0].protocols), ["bearer.https-token"]);
});

for (const server of ["ws://localhost:7779", "javascript:alert(1)", "not a URL",
  "http://user:pass@localhost:7779", "http://localhost:7779?token=secret",
  "http://localhost:7779#fragment", "http://localhost:7779/other"]) {
  test(`invalid server ${server} preserves current connection`, () => {
    const app = bridge("local-token");
    app.save("new-token", server);
    assert.equal(app.storage.get("mantis.backendUrl"), defaultServer);
    assert.equal(app.sockets.length, 1);
    assert.equal(app.sockets[0].closed, undefined);
  });
}

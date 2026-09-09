// Mantis-Bridge — Spicetify-Extension.
//
// Verbindet sich per WebSocket mit Mantis (/spicetify/ws) und beantwortet
// Kommandos über Spotifys interne APIs. Dadurch bekommt Mantis STRUKTURIERTE
// Daten (Suche/Track) direkt aus dem Client — ohne eigenen Developer-Key und
// ohne den blockierten Accessibility-Baum.
//
// Installation:
//   cp mantis-bridge.js ~/.config/spicetify/Extensions/
//   spicetify config extensions mantis-bridge.js
//   spicetify apply
//   Spotify-Profilmenü → Mantis-Verbindung → Server-Adresse + DASHBOARD_TOKEN.
//   Server: DASHBOARD_ALLOWED_ORIGINS muss https://xpui.app.spotify.com enthalten.
//
// (Läuft im Spotify-Client; Server-Adresse standardmäßig 127.0.0.1.)

(function MantisBridge() {
  const DEFAULT_SERVER = "http://127.0.0.1:7779";
  const SERVER_KEY = "mantis.backendUrl";
  const RECONNECT_MS = 5000;
  const TOKEN_PREFIX = "mantis.dashboardToken:";

  // Warten bis Spicetify bereit ist.
  if (!window.Spicetify || !Spicetify.Player || !Spicetify.CosmosAsync ||
      !Spicetify.Menu || !Spicetify.PopupModal) {
    setTimeout(MantisBridge, 500);
    return;
  }

  function nameOf(d) {
    let n = d.name || (d.profile && d.profile.name) || "";
    const arts = d.artists && d.artists.items;
    if (arts && arts.length) {
      n += " — " + arts.map((a) => (a.profile && a.profile.name) || a.name || "")
        .filter(Boolean).join(", ");
    }
    return n || "?";
  }

  function _itemData(it) {
    return (it && it.item && it.item.data) || (it && it.data) || it;
  }

  function _itemsOf(bucket) {
    return (bucket && (bucket.items || bucket.itemsV2)) || [];
  }

  function _hit(it) {
    const d = _itemData(it);
    if (d && d.uri) return { uri: d.uri, name: nameOf(d), typename: d.__typename || "" };
    return null;
  }

  // Parser auf die searchV2-Antwort. searchModalResults liefert i.d.R. NUR
  // `topResultsV2.itemsV2[].item.data` (die Kategorie-Buckets tracksV2/… sind
  // oft leer/abwesend). `typ` erzwingt eine Kategorie; ohne typ wird ein Track
  // bevorzugt (für „spiel X"), sonst der beste Top-Result.
  function pickBest(data, typ) {
    const s = data && (data.searchV2 || data.search || data.searchModalResults || data);
    if (!s) return null;
    const byKind = {
      track: _itemsOf(s.tracksV2 || s.tracks),
      album: _itemsOf(s.albumsV2 || s.albums),
      playlist: _itemsOf(s.playlistsV2 || s.playlists),
      artist: _itemsOf(s.artistsV2 || s.artists),
    };
    const top = _itemsOf(s.topResultsV2 || s.topResults);
    if (typ) {
      for (const it of byKind[typ] || []) { const h = _hit(it); if (h) return h; }
      const re = new RegExp(typ, "i");
      for (const it of top) { const h = _hit(it); if (h && re.test(h.typename)) return h; }
      return null;
    }
    // Ohne typ: Spotifys eigenem Top-Result-Ranking vertrauen (erster brauchbarer
    // Treffer — Track ODER Artist/Album, je nach Relevanz). Nur wenn keine
    // Top-Results da sind, auf die Kategorie-Buckets zurückfallen.
    for (const it of top) { const h = _hit(it); if (h) return h; }
    for (const kind of ["track", "album", "playlist", "artist"]) {
      for (const it of byKind[kind]) { const h = _hit(it); if (h) return h; }
    }
    return null;
  }

  // Wählt die passende Such-Definition. Spotify benennt sie versionsabhängig
  // (searchModalResults / searchDesktop / searchV2 …) — deshalb erst die
  // bekannten Namen, dann irgendeine Definition, deren Name „search" enthält.
  function chooseSearchDef(GQL) {
    const D = GQL.Definitions || {};
    return D.searchModalResults || D.searchDesktop || D.searchV2 || D.search ||
      D[Object.keys(D).find((k) => /search/i.test(k) && /result|desktop|searchV2|^search$/i.test(k))] ||
      D[Object.keys(D).find((k) => /search/i.test(k))] || null;
  }

  function defaultForType(type) {
    if (/^\[/.test(type)) return [];
    if (/^Boolean/i.test(type)) return false;
    if (/^Int/i.test(type)) return 0;
    if (/^Float/i.test(type)) return 0;
    return ""; // String/ID/Enum-Fallback
  }

  // Cache der zusätzlich nötigen (Feature-Flag-)Variablen zwischen Suchen —
  // damit nur die ERSTE Suche die Discovery-Round-Trips macht.
  const _searchExtra = {};

  // Führt die searchModalResults-Query aus. Spotify liefert die Query nur als
  // Persisted-Hash (def.value === null), d.h. der Client kennt die deklarierten
  // Variablen NICHT. Der Server verrät fehlende Pflicht-Variablen aber einzeln
  // per 400 ("missing variable `$x`: for required GraphQL type `Boolean!`").
  // Also: mit den semantischen + gecachten Variablen anfragen und bei jeder
  // Meldung genau die genannte Variable typgerecht ergänzen, bis es klappt.
  async function gqlSearchRequest(GQL, def, query) {
    const vars = Object.assign(
      { searchTerm: query || "", offset: 0, limit: 8, numberOfTopResults: 5 },
      _searchExtra,
    );
    vars.searchTerm = query || "";
    for (let i = 0; i < 30; i++) {
      try {
        return await GQL.Request(def, vars);
      } catch (e) {
        const errs = e && e.response && e.response.body && e.response.body.errors;
        const miss = Array.isArray(errs) && errs.find(
          (x) => x && x.extensions && x.extensions.name &&
            /VARIABLE/i.test(x.extensions.code || ""),
        );
        if (!miss) throw e;
        const name = miss.extensions.name;
        if (name in vars) throw e; // schon gesetzt, trotzdem beklagt → aufgeben
        const tm = /type\s+`?([\w[\]!]+)`?/i.exec(miss.message || "");
        const val = defaultForType(tm ? tm[1] : "Boolean!");
        vars[name] = val;
        _searchExtra[name] = val; // für nächste Suchen merken
      }
    }
    throw new Error("Suche: zu viele fehlende Variablen");
  }

  async function handle(method, params) {
    const P = Spicetify.Player;
    switch (method) {
      case "search": {
        // Interne GraphQL-Suche (externe fetch()->api.spotify.com wird vom Client
        // geblockt: code 429 'Failed to fetch').
        const GQL = Spicetify.GraphQL;
        if (!GQL || !GQL.Definitions) throw new Error("Spicetify.GraphQL fehlt");
        const def = chooseSearchDef(GQL);
        // Ohne Definition oder bei Fehler: als "nicht verfügbar" werfen → Mantis
        // fällt sauber auf den Web-API-Weg zurück (statt Fehler-Spam).
        if (!def) throw new Error("Client-Suche nicht verfügbar");
        let res;
        try {
          res = await gqlSearchRequest(GQL, def, params.query);
        } catch (e) {
          throw new Error("Client-Suche nicht verfügbar (GraphQL): " +
            (e && e.message ? e.message : e));
        }
        return pickBest(res && res.data, params.typ);  // null = kein Treffer
      }
      case "now_playing": {
        const d = P.data || {};
        const item = d.item || (d.track && d.track) || {};
        if (!item.uri) return null;
        const artists = (item.artists || []).map((a) => a.name).join(", ");
        return {
          title: item.name || item.title || "",
          artist: artists,
          album: (item.album && item.album.name) || "",
          playing: !(d.isPaused === true),
        };
      }
      case "play":
        await P.playUri(params.uri);
        return { ok: true };
      case "pause":
        P.pause();
        return { ok: true };
      case "resume":
        P.play();
        return { ok: true };
      case "next":
        P.next();
        return { ok: true };
      case "previous":
        P.back();
        return { ok: true };
      default:
        throw new Error("unbekannte Methode: " + method);
    }
  }

  let ws;
  let reconnectTimer;

  function serverOrigin(value) {
    const url = new URL(value);
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password ||
        url.search || url.hash || url.pathname !== "/") {
      throw new Error("Ungültige Server-Adresse");
    }
    return url.origin;
  }

  function configure() {
    const form = document.createElement("form");
    const serverLabel = document.createElement("label");
    serverLabel.textContent = "Mantis-Server (http:// oder https://)";
    const serverInput = document.createElement("input");
    serverInput.type = "url";
    serverInput.required = true;
    serverInput.value = localStorage.getItem(SERVER_KEY) || DEFAULT_SERVER;
    serverInput.style.width = "100%";
    serverLabel.append(serverInput);
    const label = document.createElement("label");
    label.textContent = "Mantis-Zugangstoken (DASHBOARD_TOKEN)";
    const input = document.createElement("input");
    input.type = "password";
    input.autocomplete = "off";
    input.value = localStorage.getItem(TOKEN_PREFIX + serverInput.value) || "";
    input.style.width = "100%";
    label.append(input);
    serverInput.addEventListener("input", () => { input.value = ""; });
    const hint = document.createElement("p");
    hint.textContent = "Wird lokal in Spotify gespeichert. Leer speichern trennt die Verbindung.";
    const save = document.createElement("button");
    save.type = "submit";
    save.textContent = "Speichern";
    form.append(serverLabel, label, hint, save);
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      let server;
      try {
        server = serverOrigin(serverInput.value.trim());
      } catch (_) {
        Spicetify.showNotification("Server-Adresse muss HTTP(S) ohne Zugangsdaten, Pfad, Query oder Fragment sein.", true);
        return;
      }
      const token = input.value.trim();
      // WebSocket subprotocols must be valid HTTP tokens.
      if (token && !/^[!#$%&'*+.^_`|~0-9A-Za-z-]+$/.test(token)) {
        Spicetify.showNotification("Ungültiges Token: keine Leerzeichen oder Sonderzeichen wie / verwenden.", true);
        return;
      }
      if (token) localStorage.setItem(TOKEN_PREFIX + server, token);
      else localStorage.removeItem(TOKEN_PREFIX + server);
      localStorage.setItem(SERVER_KEY, server);
      clearTimeout(reconnectTimer);
      if (ws) {
        ws.onclose = null;
        ws.close();
      }
      Spicetify.PopupModal.hide();
      connect();
    });
    Spicetify.PopupModal.display({ title: "Mantis-Verbindung", content: form });
    input.focus();
  }

  function connect() {
    let server;
    try {
      server = serverOrigin(localStorage.getItem(SERVER_KEY) || DEFAULT_SERVER);
    } catch (_) {
      return;
    }
    const token = localStorage.getItem(TOKEN_PREFIX + server);
    if (!token) return;
    const url = new URL("/spicetify/ws", server);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    const socket = new WebSocket(url.href, ["bearer." + token]);
    ws = socket;
    socket.onmessage = async (ev) => {
      let msg;
      try {
        msg = JSON.parse(ev.data);
      } catch (e) {
        return;
      }
      try {
        const result = await handle(msg.method, msg.params || {});
        socket.send(JSON.stringify({ id: msg.id, result }));
      } catch (e) {
        socket.send(JSON.stringify({ id: msg.id, error: String(e && e.message ? e.message : e) }));
      }
    };
    socket.onclose = () => { reconnectTimer = setTimeout(connect, RECONNECT_MS); };
    socket.onerror = () => socket.close();
  }
  new Spicetify.Menu.Item("Mantis-Verbindung", false, configure).register();
  connect();
})();

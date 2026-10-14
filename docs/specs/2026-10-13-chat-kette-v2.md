# Spec: Chat-Kette v2 (Haiku 5.5 + ChatGPT-Abo)

> **Überholt seit 2026-10-14:** Diese Spec dokumentiert die frühere Chat-Migration. Die aktuelle
> [`Cloud-Umstellung`](2026-10-14-cloud-umstellung.md) entfernt lokale LLM-Stufen für alle Zwecke;
> lokal bleiben ausschließlich Ollama-Embeddings.

Stand 2026-10-13. Baut auf Teil 2 von `docs/specs/2026-10-13-mantis-gesund.md` auf.
Entscheidung Timo: Reihenfolge

1. **OpenRouter → `openai/gpt-6-luna`** (vorhandenes Guthaben zuerst)
2. **Anthropic-API direkt → `claude-haiku-5-5`** (vorhandene API-Credits)
3. **ChatGPT-Abo → `gpt-6-luna`** über „ChatGPT plan usage“ (Sign in with ChatGPT)
4. **lokal (Ollama)**

`google/gemini-3.1-flash-lite` entfällt als Stufe (Setting bleibt, Default leer).

## Settings
| Setting | Default |
|---|---|
| `CHAT_CLOUD_MODEL` | `openai/gpt-6-luna` (wie bisher) |
| `CHAT_CLOUD_FALLBACK_MODEL` | `""` (leer = Stufe entfällt) |
| `CHAT_ANTHROPIC_MODEL` | `claude-haiku-5-5` (leer = Stufe entfällt; braucht `ANTHROPIC_API_KEY`) |
| `CHAT_CHATGPT_PLAN_ENABLED` | `True` (Stufe nur aktiv, wenn gültige Login-Daten vorliegen) |
| `CHAT_CHATGPT_PLAN_MODEL` | `gpt-6-luna` |
| `CHATGPT_AUTH_FILE` | `~/.config/mantis/chatgpt_auth.json` |

`LLM_LOCAL_ONLY=True` erzwingt weiterhin rein lokal. Die Kette entsteht in
`core/backends/factory.py`; Stufen ohne Konfiguration/Login werden übersprungen und im Startlog
genannt. Cloud-Timeout je Stufe wie bisher `CHAT_CLOUD_TIMEOUT_S`. Bg-Chat-Aufgaben
(`BG_CHAT_TASKS_USE_CLOUD`) nutzen dieselbe Kette. Kosten: Anthropic über bestehende Preistabelle
(Haiku 5.5: Preis aus Anthropic-Antwort/Tabelle ergänzen, als Schätzung markiert), ChatGPT-Abo
als `provider='chatgpt-plan'`, Kosten 0 mit Vermerk „Abo“.

## Stufe 2: Anthropic Haiku 5.5
Bestehendes `ClaudeBackend` (`core/backends/claude.py`) mit Modell aus `CHAT_ANTHROPIC_MODEL`
wiederverwenden. Prüfen, dass Tool-Calling, Streaming, Fehler→Fallback (wie bei OpenRouter:
Timeout, 4xx/5xx, abgebrochener Stream, kaputte Tool-Argumente) und Secret-Schwärzung gelten.

## Stufe 3: ChatGPT-Abo (offizielle Doku, Vorschau)
Quelle: developers.openai.com/siwc/token-sharing-open-source/* (Stand 13.10.2026). Zulässig für
„open-source and locally hosted apps“; Mantis läuft lokal.

**Login (einmalig, CLI):** `python3.14 -m core.auth.chatgpt_login`
- Erstregistrierung: `client_id=dynamic_agent_client`; der Callback liefert eine ausgestellte
  Client-ID (`oaiapp_…`), die gespeichert und ab dann für Login und Token-Austausch verwendet
  wird (nie wieder `dynamic_agent_client` für den Austausch). Kein Client-Secret.
- Authorize: `https://auth.openai.com/api/accounts/authorize` mit `response_type=code`, frischem
  `state`, `nonce`, PKCE `S256`, bei Erstregistrierung zusätzlich `ext_agent_host_id` (stabile
  zufällige Host-ID, gespeichert) und `agent_name_hint=Mantis`.
- Redirect: `http://127.0.0.1:<port>/auth/callback` (exakt `127.0.0.1`, nicht `localhost`;
  Port 1456, weil Codex 1455 nutzt; konfigurierbar).
- Scopes: `openid profile email offline_access resource.invoke chatgpt.tokens.use.direct`,
  `resource=https://api.openai.com/v1`.
- Token-Austausch: POST form-encoded an `https://auth.openai.com/api/accounts/oauth/token`
  (`grant_type=authorization_code`, ausgestellte `client_id`, `code`, `code_verifier`, gleiche
  `redirect_uri`, gleiche `resource`).
- ID-Token prüfen (JWKS-Signatur, `iss=https://auth.openai.com`, `aud`, `exp`, `nonce`) und
  sicherstellen, dass `chatgpt.tokens.use.direct` in den gewährten Scopes steht; sonst Stufe aus.
- Speichern: `CHATGPT_AUTH_FILE`, atomar schreiben, Rechte 0600, Verzeichnis 0700; Inhalt:
  client_id, ext_agent_host_id, access_token, refresh_token, expires_at, scope. Nie loggen
  (in `core/error_redaction.py` aufnehmen).
- Das Login-Skript öffnet den Browser (`webbrowser`) und wartet max. 5 min auf den Callback.

**Refresh:** Access-Token gilt 3600 s, Refresh-Token 30 Tage, rotiert bei jedem Refresh.
POST form-encoded an denselben Token-Endpunkt: `grant_type=refresh_token`, gespeicherte
`client_id`, `refresh_token`, `resource=https://api.openai.com/v1`, **kein** `scope`. Refresh
ca. 5 min vor Ablauf, serialisiert (asyncio.Lock, ein Prozess), Access- und neues Refresh-Token
zusammen atomar speichern.

**Inferenz:** `POST https://api.openai.com/v1/responses` mit `Authorization: Bearer <access>`,
**immer** `stream: true` und `store: false`, kein `previous_response_id`, keine Parameter
`temperature`, `top_p`, `max_output_tokens`, `metadata`, `user`, `truncation`. Erfolg nur nach
`response.completed`; `response.failed`, `response.incomplete` und abgebrochene Streams sind
Fehler. Function-Calling wird unterstützt („when grouped in namespaces or supplied through
`additional_tools` input items“) – Mantis' Tool-Schemas entsprechend im Responses-Format
übergeben und Tool-Calls/-Ergebnisse (function_call / function_call_output) korrekt abbilden.
Hosted Tools (web_search etc.) nicht verwenden.

**Fehlerklassen** (bestimmen, ob die Stufe nur übersprungen oder deaktiviert wird):
- Temporär → Fallback, Login behalten: HTTP 503, `subscription_sharing_usage_unavailable`,
  `subscription_sharing_user_unavailable`, Netzwerk/Timeout.
- Kontingent → Fallback und Stufe pausieren (z. B. 30 min), Login behalten:
  `subscription_sharing_usage_limit_exceeded` (429).
- Permanent bis Re-Login → Fallback, Stufe deaktivieren, einmalig `log.error` „ChatGPT-Login
  erneuern: python3.14 -m core.auth.chatgpt_login“: HTTP 401, `subscription_sharing_invalid_user`,
  Refresh-Fehler `invalid_grant`, `invalid_refresh_token`, `token_expired`,
  `refresh_token_expired`, `refresh_token_invalidated`, `refresh_token_reused`.
- Permanent (Konfig) → Stufe deaktivieren, `log.error`: 403, `subscription_sharing_user_not_eligible`,
  `subscription_sharing_route_not_supported`, `chatpass_v2_*`, `invalid_client`.
- `subscription_sharing_unsupported_capability` (400) → Fallback, `error.param` loggen.
Admission-Fehler können `{"detail": …}` statt `error` liefern → nach HTTP-Status klassifizieren.

## Tests (TDD, HTTP gemockt)
Kettenreihenfolge 1→2→3→4, Stufen ohne Konfig/Login übersprungen, `LLM_LOCAL_ONLY`; Haiku-Stufe
Fallback-Fälle; ChatGPT: PKCE/State/Nonce-Prüfung im Login (Callback mit falschem state
abgelehnt), Token-Austausch-Felder, Scope fehlt → Stufe aus, Datei 0600 + atomar, Refresh-Felder
(ohne scope) + Rotation + Serialisierung, Responses-Request (stream/store, verbotene Parameter
fehlen), Tool-Call-Roundtrip, jede Fehlerklasse oben, Schwärzung der Tokens. Suite + ruff grün.

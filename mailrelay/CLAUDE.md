# Mail relay

The only container that holds a Microsoft Graph credential: a **delegated** refresh token
for `RELAY_MAILBOX` (set in `.env`), created by `relay.py login` (device code) and stored at
`/state/refresh_token` on the `mailrelay_state` volume. It's a credential for a real
person's account, so it's kept out of the internet-facing portal. The portal asks this relay
to send. Tenant setup is in `docs/operations/graph-mail-setup.md`.

## What it accepts

`POST /send`, `Authorization: Bearer $MAIL_RELAY_TOKEN`, JSON with exactly `to`, `subject`,
`body`. Nothing else:

- The sender is `MAIL_SENDER` (`Config.sender`), set by the operator in `.env` and checked
  at startup: one plain address, or the relay refuses to start. **A request can never set
  it**, and a `from` field is a 400. That limit is the reason this service exists. Don't add
  a per-request or per-portal sender, and don't take it from the portal's branding: the
  portal is the internet-facing side.
- The sender is usually a **Microsoft 365 group**, and Graph can't send from a group directly.
  The message goes to `/me/sendMail` (the signed-in account) with `from` set to `MAIL_SENDER`,
  and Exchange checks that the account has Send As on the group. `RELAY_MAILBOX` decides
  which account signs in (and whose Sent Items get the copy); `login` refuses to save a
  token for any other account. The From address stays fixed.
- Graph receives structured JSON with `contentType: "Text"`, not MIME. Microsoft builds the
  headers, so there is nothing for a caller to inject into. Don't switch to MIME sendMail.
- One recipient, plain address only. No lists, no display names, no line breaks in
  `to` or `subject`.
- Unknown fields are a 400, not ignored. A caller that sends `html` or `from` should learn
  right away that they don't work.

Responses: 202 `{sent, id}`, 400 validation, 401 bad token, 413/415/411 transport, 429 rate
limit, 502 Graph or network failure (with Graph's error code, never the address), 503 not
signed in or sign-in expired (fixed by `relay.py login`). `GET /health` reports `signed_in`.

## Rules

- **Standard library only.** No FastAPI, no requests, no msal. The container with the key
  should have as little code in it as possible.
- **Logs carry an id, never an address**, the same rule as the portal's audit log. There
  are tests asserting it.
- The rate limit (`RELAY_RATE_PER_MIN` 20, `RELAY_RATE_PER_HOUR` 300) bounds what a
  compromised portal can send. Graph's own cap is 30 a minute per mailbox. It counts only
  requests that pass auth and validation, and it's in-memory, so a restart resets it.
- **The refresh token rotates.** Microsoft returns a new one on every refresh, and
  `TokenStore.write` saves it atomically with mode 0600. The file is re-read on every
  refresh, not cached, so a `login` in another process takes effect without a restart.
  Don't log it, put it in an env var, or move it into the portal.
- The filesystem is read-only except `/state`.
- Network: `mail_internal` (internal, shared with the portal only) plus `mail_egress`
  (internet, for Microsoft). No published ports. See `compose.yml`.

## The schedule mailer borrows this code

`tools/vol_mail/vol_mail.py` carries a copy of the device-code sign-in and `sendMail` call,
using the same app registration, to send schedule emails (HTML with inline QR images) from an
organizer's machine. It's a copy, not an import, so this container stays self-contained, and
the tool keeps its sign-in in memory only. Nothing about the relay's own rules changed: it
still sends plain text only, for the portal.

## Running it

```bash
pytest test_relay.py -v     # no network
python relay.py             # needs GRAPH_TENANT_ID, GRAPH_CLIENT_ID, RELAY_MAILBOX,
                            # MAIL_RELAY_TOKEN (>= 32 chars); listens on RELAY_PORT (5002)
python relay.py login       # device-code sign-in as RELAY_MAILBOX; RELAY_STATE_DIR (/state)
```

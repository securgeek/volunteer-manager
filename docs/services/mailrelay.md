# Mail relay

The relay is the only container that holds a Microsoft Graph credential. The portal hands it
one plain-text message at a time, and it sends that message as **`MAIL_SENDER`**, the
conference's volunteer address, set in `.env`. It does nothing else.

Code: `mailrelay/relay.py`, standard library only. Reference:
[mailrelay.relay](../reference/mailrelay/relay.md). Setting it up:
[Graph mail setup](../operations/graph-mail-setup.md).

## Why it exists

Sending through Graph needs a credential for a real person's Microsoft account. Keeping it
out of the internet-facing portal means a compromised portal can, at most, ask for plain-text
mail from the one address the operator set, at a limited rate. It can't read mail, choose the sender,
or take the credential.

## What it accepts

`POST /send`, with `Authorization: Bearer $MAIL_RELAY_TOKEN` and a JSON body of exactly
`to`, `subject` and `body`:

- **One recipient**, a plain address: no lists, no display names.
- **A one-line subject**, and a plain-text body.
- **Any other field is refused** with a 400. A caller that sends `html` or `from` finds out
  at once that it doesn't work.

| Status | Meaning |
|---|---|
| 202 | Sent. The body has an id, which is what the logs record. |
| 400 | The message failed validation. |
| 401 | Wrong or missing token. |
| 411, 413, 415 | No length, too large, or not JSON. |
| 429 | Over the rate limit: 20 a minute and 300 an hour. Graph's own cap is 30 a minute. |
| 502 | Graph or the network failed. The error carries Graph's code, never the address. |
| 503 | Not signed in, or the sign-in expired. Run `relay.py login`. |

`GET /health` reports whether it's signed in.

## How it sends

- **The sender is usually a Microsoft 365 group**, and Graph can't send from a group directly.
  So the relay signs in as `RELAY_MAILBOX` and sends with From set to `MAIL_SENDER`. Exchange
  checks that the account has Send As on the group.
- **Graph receives structured JSON, not MIME.** Microsoft builds the headers, so there's
  nothing for a caller to inject into.
- **The sign-in is delegated**: a device-code sign-in once, then a refresh token on the
  `mailrelay_state` volume (mode 0600). Microsoft issues a new refresh token on every use, and
  the relay replaces the file atomically. It re-reads the file on each refresh, so a new
  `login` takes effect without a restart.

## Hardening

- **The container's filesystem is read-only** except `/state`, with every capability dropped
  and `no-new-privileges`.
- **It publishes no ports.** It's on `mail_internal`, which only the portal shares and which
  has no route out, and on `mail_egress`, its way to Microsoft.
- **Logs carry a message id, never an address.** Tests check this.

## Commands

```bash
podman-compose exec mailrelay python relay.py login     # sign in as RELAY_MAILBOX
podman-compose exec portal python mailer.py --test you@example.com   # one message, end to end
(cd mailrelay && python3 -m pytest)                     # tests, no network
```

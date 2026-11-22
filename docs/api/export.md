# BSides Charm Volunteer Export API

A read-only HTTPS API for other servers. It returns the volunteer schedule and the volunteer
list with each person's shifts. Every endpoint is `GET`, and none of them can change anything.

**Base URL:** `https://<admin hostname>`, which the organizers give you with your key

Call it **from a server**. It sends no CORS headers, so a web page on another site can't call
it from the browser.

<!--
  Maintainers: sections 4 and 5 are generated. Each "export-api" marker below is replaced at
  build time (docs/gen_export_api.py) with the endpoint's scope, parameters, response fields
  and an example, all read from backend/routers/export.py. To change them, change the Query
  descriptions or the response models there. Everything else on this page is hand-written.
-->

---

## 1. Getting a key

An organizer creates a key for you and sends it over once. It can't be shown again, so store
it like a password. Send it on every request:

```
Authorization: Bearer bsc_…
```

- **Keys expire.** The default is 30 days and the longest is 365. Ask for a new key before
  yours runs out. There is no renewal: you get a new key and the old one is revoked.
- **A key may be locked to IP addresses or CIDR ranges.** If your server's outbound address
  changes, ask for a new key with the updated list.
- **One key per app or person**, so one can be revoked without affecting the others.
- **Scopes** decide what a key can read:

| Scope | Grants |
|---|---|
| `schedule:read` | `GET /api/export/schedule` |
| `volunteers:read` | `GET /api/export/volunteers` |
| `volunteers:email` | Adds `email_enc` to each volunteer. This is RSA-OAEP/SHA-256 ciphertext in base64, and it can't be read without the organizers' private key. |
| `checkin:codes` | Adds `checkin_code` to each shift. |

Most integrations need `schedule:read` and `volunteers:read`, and nothing else.

---

## 2. Errors

| Status | Meaning |
|---|---|
| `401 {"detail": "Unauthorized"}` | The key is missing, unknown, expired or revoked, it lacks the scope, or your IP isn't allowed. The response is the same in every case, on purpose. |
| `429` | Ten failed attempts from your address within 15 minutes. Wait, then fix the key. Retrying won't help. |
| `400` | A query parameter with a value it doesn't accept, such as `status=bogus`. |
| `422` | A query parameter of the wrong type, such as `assigned=maybe`. The body says which. |

---

## 3. Conventions

Every response starts with `generated_at` (server time, UTC), `event_title` and `event_tz`.
The tables in sections 4 and 5 list every field; these are the rules that apply across them.

**Names**
- `name` is `First L.`, or `Handle [First L.]` when the volunteer gave a handle.
- The parts are also given separately as `first_name`, `last_initial` and `handle`. Any of
  them may be `""`.

**Times**
- `time` is the shift label as the organizers wrote it, for example `"0800-1200"`.
- `start` and `end` are ISO-8601 in the event timezone, with its offset, for example
  `"2027-04-24T08:00:00-04:00"`.
- `start` and `end` are `null` when the day has no date set, or when the label isn't a time
  range.
- `date` is `YYYY-MM-DD`, or `null`.

**Identifiers**
- `slot_key` (`day_time_position`, for example `sat_0800_sec3`) identifies one slot. It stays
  the same as long as the schedule's structure doesn't change.
- `vol_id` is a string. Treat it as opaque.

**Booleans in query strings** accept `1`/`0` and `true`/`false`.

**Scoped fields** (`email_enc`, `checkin_code`) are left out entirely when the key lacks the
scope. With the scope, they are always present, and may be `null`.

---

## 4. `GET /api/export/schedule`

Returns every slot in time order, filled or not.

<!-- export-api: /api/export/schedule -->

---

## 5. `GET /api/export/volunteers`

Returns every volunteer, sorted by name, with their shifts in time order.

<!-- export-api: /api/export/volunteers -->

The example leaves out the scoped fields. A key with `volunteers:email` also gets `email_enc`
on each volunteer, and one with `checkin:codes` gets `checkin_code` on each shift.

---

## 6. Examples

**curl**

```bash
export BSIDES_API_KEY='bsc_…'
BASE=https://<admin hostname>

curl -sf -H "Authorization: Bearer $BSIDES_API_KEY" \
  "$BASE/api/export/schedule?assigned=1" | jq '.slots[] | {start, position, name}'

curl -sf -H "Authorization: Bearer $BSIDES_API_KEY" \
  "$BASE/api/export/volunteers?assigned=1" | jq '.volunteers[] | {name, shifts: (.shifts | length)}'
```

**Python (standard library only)**

```python
import json, os, urllib.request

BASE = "https://<admin hostname>"

def export(path, **params):
    query = "&".join(f"{k}={v}" for k, v in params.items())
    req = urllib.request.Request(
        f"{BASE}{path}?{query}",
        headers={"Authorization": f"Bearer {os.environ['BSIDES_API_KEY']}"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)

for slot in export("/api/export/schedule", assigned=1)["slots"]:
    print(slot["start"], slot["position"], slot["name"])
```

---

## 7. Fair use

- There's no rate limit on successful calls. Polling every few minutes is fine. Polling every
  second isn't.
- Use `generated_at` to see how fresh a response is.
- Every call is logged under the key's label. Failed calls are logged too.
- If a key leaks, tell the organizers so they can revoke it right away.

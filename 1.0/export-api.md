# BSides Charm Volunteer Export API

A read-only HTTPS API for other servers. It returns the volunteer schedule and the volunteer
list with each person's shifts. Every endpoint is `GET`, and none of them can change anything.

**Base URL:** `https://<admin hostname>`, which the organizers give you with your key

Call it **from a server**. It sends no CORS headers, so a web page on another site can't call
it from the browser.

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

Needs the `schedule:read` scope.

| Parameter | Type | Default | Effect |
|---|---|---|---|
| `assigned` | boolean | `false` | Only slots with a volunteer. |
| `day` | string or null | — | Only one day, by `day_key`, for example `sat`. An unknown `day_key` returns an empty list, not an error. |

**Response**

| Field | Type | Description |
|---|---|---|
| `generated_at` | string | Server time of the response, ISO-8601 UTC. |
| `event_title` | string | The event's title from App Settings. |
| `event_tz` | string | IANA timezone that `start` and `end` are given in. |
| `slots` | array of Slot | Every slot in time order. |

**Each item in `slots` (Slot)**

| Field | Type | Description |
|---|---|---|
| `slot_key` | string | `day_time_position`. Stable while the schedule's structure is unchanged. |
| `day_key` | string | The day's key. |
| `day` | string | The day's label. |
| `date` | string or null | `YYYY-MM-DD`, or null when the day has no date. |
| `time_key` | string | The time's key. |
| `time` | string | The shift label as the organizers wrote it. |
| `start` | string or null | ISO-8601 in the event timezone, with its offset. Null when the day has no date or `time` isn't a range. |
| `end` | string or null | Like `start`. A range past midnight ends the next day. |
| `pos_key` | string | The position's key. |
| `position` | string | The position's label. |
| `category` | string | The position's category. |
| `vol_id` | string | The assigned volunteer, or `""` when the slot is empty. Opaque. |
| `checked_in` | boolean | Whether the volunteer has checked in. Always false for an empty slot. |
| `name` | string | The volunteer's display name, or `""` when the slot is empty. |

**Example response**

```json
{
  "generated_at": "2027-04-20T14:03:11.402Z",
  "event_title": "BSides Charm 2027",
  "event_tz": "America/New_York",
  "slots": [
    {
      "slot_key": "sat_0800_sec3",
      "day_key": "sat",
      "day": "Saturday",
      "date": "2027-04-24",
      "time_key": "0800",
      "time": "0800-1200",
      "start": "2027-04-24T08:00:00-04:00",
      "end": "2027-04-24T12:00:00-04:00",
      "pos_key": "sec3",
      "position": "Security 3",
      "category": "Security",
      "vol_id": "1042",
      "checked_in": false,
      "name": "Zero Cool [Jim E.]"
    }
  ]
}
```

---

## 5. `GET /api/export/volunteers`

Returns every volunteer, sorted by name, with their shifts in time order.

Needs the `volunteers:read` scope.

| Parameter | Type | Default | Effect |
|---|---|---|---|
| `status` | string | `"active"` | `active`: approved, and neither cancelled nor inactive. `all`: everyone, including pending signups; check `status`, `cancelled` and `inactive`. Anything else is a 400. |
| `assigned` | boolean | `false` | Only volunteers with at least one shift. |

**Response**

| Field | Type | Description |
|---|---|---|
| `generated_at` | string | Server time of the response, ISO-8601 UTC. |
| `event_title` | string | The event's title from App Settings. |
| `event_tz` | string | IANA timezone that `start` and `end` are given in. |
| `volunteers` | array of Volunteer | Volunteers sorted by name. |

**Each item in `volunteers` (Volunteer)**

| Field | Type | Description |
|---|---|---|
| `vol_id` | string | The volunteer's id. Opaque. |
| `name` | string | `First L.`, or `Handle [First L.]` with a handle. |
| `first_name` | string | May be `""`. |
| `last_initial` | string | May be `""`. |
| `handle` | string | May be `""`. |
| `status` | string | `pending` (signed up, not yet approved) or `active`. |
| `cancelled` | boolean | The volunteer cancelled. |
| `inactive` | boolean | An organizer set the volunteer inactive. |
| `shifts` | array of Shift | Shifts in time order; `[]` when none. |
| `email_enc` | string or null | Only with the `volunteers:email` scope. The email address, RSA-OAEP/SHA-256 encrypted to the organizers' key, base64. Null when none is on file. |

**Each item in `shifts` (Shift)**

| Field | Type | Description |
|---|---|---|
| `slot_key` | string | `day_time_position`. Stable while the schedule's structure is unchanged. |
| `day_key` | string | The day's key. |
| `day` | string | The day's label. |
| `date` | string or null | `YYYY-MM-DD`, or null when the day has no date. |
| `time_key` | string | The time's key. |
| `time` | string | The shift label as the organizers wrote it. |
| `start` | string or null | ISO-8601 in the event timezone, with its offset. Null when the day has no date or `time` isn't a range. |
| `end` | string or null | Like `start`. A range past midnight ends the next day. |
| `pos_key` | string | The position's key. |
| `position` | string | The position's label. |
| `category` | string | The position's category. |
| `checked_in` | boolean | Whether the volunteer has checked in. |
| `checkin_code` | string or null | Only with the `checkin:codes` scope. The check-in code mailed to the volunteer as a QR code. |

**Example response**

```json
{
  "generated_at": "2027-04-20T14:03:11.402Z",
  "event_title": "BSides Charm 2027",
  "event_tz": "America/New_York",
  "volunteers": [
    {
      "vol_id": "1042",
      "name": "Zero Cool [Jim E.]",
      "first_name": "Jim",
      "last_initial": "E",
      "handle": "Zero Cool",
      "status": "active",
      "cancelled": false,
      "inactive": false,
      "shifts": [
        {
          "slot_key": "sat_0800_sec3",
          "day_key": "sat",
          "day": "Saturday",
          "date": "2027-04-24",
          "time_key": "0800",
          "time": "0800-1200",
          "start": "2027-04-24T08:00:00-04:00",
          "end": "2027-04-24T12:00:00-04:00",
          "pos_key": "sec3",
          "position": "Security 3",
          "category": "Security",
          "checked_in": false
        }
      ]
    }
  ]
}
```

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

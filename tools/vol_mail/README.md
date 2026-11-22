# vol_mail — schedule emails with check-in codes

Sends each volunteer their shifts, with a QR code per shift that the check-in desk scans
(Schedule tab → **Check In Volunteer**). Run it on your own machine, usually twice before the
conference: a **draft**, then a **final** after adjustments.

It isn't part of the website. No container copies it, and the site never sends this mail. The
site provides the template (as a package you download) and the data (through the export
API); this tool decrypts addresses with the organizers' private key and sends through
Microsoft Graph.

## Setup (once)

```bash
cd tools/vol_mail
python3 -m venv .venv && . .venv/bin/activate
pip install --upgrade pip         # the venv's own pip can be months old
pip install -r requirements.txt
```

You need:

| What | Where it comes from |
|---|---|
| **An API key** with `volunteers:read`, `volunteers:email`, `checkin:codes` | Admin → API Keys. Save it to a file, e.g. `api_key.txt`. |
| **The template package** | Call for Volunteers → Schedule Email → Download package |
| **`private.pem`** | The organizers' private key (the one Decrypt emails uses) |
| **Graph settings** | The mail relay's app registration (`docs/operations/graph-mail-setup.md`) |

```bash
export GRAPH_TENANT_ID=...        # same as the relay
export GRAPH_CLIENT_ID=...        # same as the relay
export VOL_MAIL_MAILBOX=<organizer>@example.org   # the account with Send As on the sender
export MAIL_SENDER=volunteers@example.org         # the From address; or pass --from
```

The tool signs in with a device code each time you send, and keeps the sign-in in memory
only. Nothing is saved, so there's no token file to protect afterwards.

## Draft, then final

```bash
# 1. Pull the current schedule (the only step that talks to the site).
./vol_mail.py fetch --url https://<site> --key-file api_key.txt --out box.json

# 2. Look at one volunteer's email: opens in a browser (.html) or mail app (.eml).
./vol_mail.py preview --package schedule-email-*.zip --box box.json --vol 1042 --out preview/

# 3. Send a test to yourself first: put your own vol_id in a file.
echo 1042 > me.txt
./vol_mail.py send --package schedule-email-*.zip --box box.json --key private.pem \
    --round test --only me.txt --dry-run
./vol_mail.py send --package schedule-email-*.zip --box box.json --key private.pem --round test

# 4. The draft, to everyone.
./vol_mail.py send ... --round draft --dry-run
./vol_mail.py send ... --round draft

# 5. After adjustments: fetch again, then send the final only to people whose shifts changed.
./vol_mail.py fetch --url https://<site> --key-file api_key.txt --out box.json
./vol_mail.py send ... --round final --only-changed-since draft --dry-run
./vol_mail.py send ... --round final
```

Leave out `--only-changed-since` to send the final to everyone.

**Codes stay valid across rounds.** A shift's code depends only on the volunteer and the slot,
so a draft's code still works if that shift didn't change. A code stops working when its slot
is reassigned, or when someone uses **Rotate key** in App Settings (which voids every code;
send again afterwards).

## Rules the tool enforces

- **A dry run first.** It decrypts every address (catching a wrong key now, not halfway
  through), renders every message, and writes the round's plan. The real run sends exactly
  that plan, and refuses if the package or box file changed since.
- **It asks before sending** (type `send`), unless you pass `--yes`.
- **At most 20 a minute** (`--per-minute`). Graph allows 30 a minute per mailbox, and a 429
  is waited out.
- **Interrupted, or some failed?** Run the same command again. Anyone already sent in that
  round is skipped.
- **Skipped and listed:** volunteers with no shifts or no email on file, and addresses that
  don't decrypt.
- **No addresses on disk.** The state folder (`vol_mail_state/`, `--state`) holds each
  round's `plan.json` and `sent.jsonl` with vol_ids, hashes and results only. Output names
  vol_ids, never addresses. `box.json` holds ciphertext and check-in codes, and is written
  owner-only (0600); delete it when you're done.

## What the email looks like

HTML, with the QR codes and the template's images attached inline. Graph's JSON send takes a
single body, so the plain-text version from the package isn't sent; `preview` writes it
(`.txt`, and inside the `.eml`) so you can read it. Each shift's text version carries the code
as text, which the check-in desk can type into the scanner's manual entry if a code won't
scan.

## Tests

```bash
pip install markdown-it-py pytest   # the tests build a package with the backend's renderer
pytest test_vol_mail.py -v
```

No network. Graph is faked at the HTTP layer; the tests also check that this tool fills in a
package exactly as the site's preview does (`backend/email_render.py`), since this file
carries its own copy of those few rules.

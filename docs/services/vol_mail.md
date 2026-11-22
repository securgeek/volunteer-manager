# vol_mail: schedule emails

`vol_mail` sends each volunteer their shifts, with a QR code per shift for the check-in desk
to scan. It runs on an organizer's own machine, usually twice before the conference: a
**draft**, then a **final** after adjustments.

It isn't part of the website. No container copies it, and the site never sends this mail:

- **The site provides the template**, as a package downloaded from **Call for Volunteers →
  Schedule Email**.
- **The site provides the data**, through the read-only [Export API](../api/export.md).
- **The tool decrypts the addresses** with the organizers' `private.pem`, and sends through
  Microsoft Graph with the same app registration as the [mail relay](mailrelay.md).

Code: `tools/vol_mail/vol_mail.py`. Step-by-step instructions are in
`tools/vol_mail/README.md`, beside the tool. Reference:
[tools.vol_mail.vol_mail](../reference/tools/vol_mail/vol_mail.md).

## The commands

| Command | What it does |
|---|---|
| `fetch` | Downloads the volunteers, with their shifts, check-in codes and encrypted addresses, into `box.json`. The only step that talks to the site. |
| `preview` | Writes one volunteer's message as `.eml`, `.html` and `.txt` files. Sends nothing. |
| `send --dry-run` | Decrypts every address, builds every message, and writes the round's plan. |
| `send` | Sends exactly the plan, after you type `send`. |

## What it needs

| What | Where it comes from |
|---|---|
| An API key with `volunteers:read`, `volunteers:email` and `checkin:codes` | **Admin → API Keys**. Save it to a file named `api_key*`. |
| The template package | **Call for Volunteers → Schedule Email → Download package** |
| `private.pem` | The organizers' private key |
| `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID`, `VOL_MAIL_MAILBOX` | The relay's app registration, and an account with Send As on the sender |
| `MAIL_SENDER` (or `--from`) | The From address, the same as the relay's |

## Rules it enforces

- **A dry run first.** The real run refuses if the package or the box changed since the plan
  was made.
- **Resumable.** If a round is interrupted, or some messages fail, run the same command
  again. Anyone already sent in that round is skipped.
- **Only the changes.** `--only-changed-since draft` sends the final only to volunteers
  whose shifts changed since the draft.
- **At most 20 a minute**, and a 429 from Graph is waited out.
- **No addresses on disk.** The round's state holds vol_ids and hashes only. `box.json`
  holds ciphertext and check-in codes, is written owner-only, and should be deleted
  afterwards.
- **The sign-in stays in memory.** There's no token file to protect afterwards.

## Check-in codes

A shift's code depends only on the volunteer and the slot, so a draft's code still works if
that shift didn't change. A code stops working when its slot is reassigned, or when an
organizer uses **App Settings → Rotate key**, which voids every code. Send again afterwards.

The text version of each shift carries the code as text too, which the desk can type into
the scanner's manual entry if a code won't scan.

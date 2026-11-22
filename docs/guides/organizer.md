# Organizer guide

This guide is for organizers running the event on the admin site. It follows the order of the
season: set up, open signup, approve and schedule volunteers, send schedules, then run the
event days.

The admin site is at the admin hostname, behind a sign-in gate. Its toolbar has three
menus: **Call for Volunteers**, **Admin**, and your own name on the right, with **Profile**
and **Sign Out**. Below the toolbar are three tabs: **Schedule**, **Volunteers** and
**Radios**.

## Roles

| Role | For | Can do |
|---|---|---|
| organizer | the volunteer team | everything |
| checkin | the check-in desk and radio table | check volunteers in, rate them, issue and return radios |
| view | anyone who only needs to look | read only |

## First-time setup

A fresh install opens the setup wizard:

1. **Create the admin account**, the first organizer. This step locks once any account exists.
2. **Sign in.**
3. **Your conference**: its name, and how volunteers can reach you: a contact email, the
   conference website and its code of conduct. Then the logo. The volunteer pages show all of
   them, and leave out anything blank. Everything can be skipped and changed later in
   **Admin → App Settings**.
4. **Set up the schedule.** Either build it in the wizard (days, then categories, then times,
   then a grid of how many people each position needs at each time) or import a CSV.
5. **Import volunteers** from a CSV, or skip this and let them sign up.

Then, before opening signup:

- **Admin → Email Key**: upload the organizers' public key. Signup can't open without it.
  [Secrets](../operations/secrets.md#the-volunteer-email-key-pair) explains the key pair.
- **Admin → App Settings**: set the event timezone, which check-in uses to judge early or
  late.
- **Admin → Users**: add the other organizers, or use **Invite Someone** to send a link that
  lets them make their own account. Invite links last 72 hours.

## Editing the schedule

**Edit Schedule** on the Schedule tab opens the editor:

- **Categories**: their order and chip colors.
- **Days**: each day, its date, and which times it runs.
- **Times**: each time range, and the days it runs on.
- **Grid**: how many of each position at each day and time. Changes are applied as a
  difference. A slot with a volunteer assigned is never removed; the editor lists it
  instead.

## Signup

Open **Call for Volunteers → Volunteer Portal**:

- **Volunteer signup open/closed** is the switch. Signup also shows as closed while there's
  no email key, and as full when no two timeslots with space remain.
- **Overbook by** lets more volunteers pick a timeslot than it has slots, to make up for
  people who drop out. Volunteers see the inflated space. Shift Demand shows the real
  numbers.
- **Early access invites** let one person sign up while signup is closed. Make a link to
  send yourself, or use **Send invites by email**. Each link works once.
- The sync status shows when the portal last answered, and any error. **Sync now** runs a
  pass at once.

### What the volunteer pages say

The same dialog has three more tabs for the words volunteers see. Each has **Save** and
**Reset to default**, and the portal has a saved change within a few seconds.

- **Landing page**: the public call for volunteers. It's three Markdown parts (the intro
  above the two buttons, the main column and the sidebar) plus the opportunity cards, each
  with an icon, a name and a line of text. `{{ conference }}`, `{{ contact_email }}`,
  `{{ conference_url }}` and `{{ code_of_conduct_url }}` fill in from App Settings, and
  `{% if contact_email %}…{% endif %}` leaves a sentence out when it's blank. **Preview**
  shows it as volunteers will see it. Raw HTML, images and links other than web pages and
  email addresses are left out.
- **Questionnaire**: the signup form's questions, in order. For each one, choose its type
  (choice, dropdown, checkbox, acknowledgement or text), the signup step it's on, whether
  it's required, and whether volunteers can change their answer later. Choices can ask a
  follow-up when picked. You can change the questions at any time. Removing one keeps the
  answers already given, and the editor tells you how many there are first. The
  **flexible** question is used by the assignment picker: you can reword or remove it, but
  it stays a checkbox. Don't ask for anything sensitive, because answers are stored as plain
  text.
- **Invite email**: the subject and body of the early-access invite. It's a template, with
  `{{ first_name }}`, `{{ conference }}`, `{{ link }}`, `{{ expires }}` and the App Settings
  values, plus `{% if %}…{% else %}…{% endif %}`. The body must include `{{ link }}`.
  **Preview** fills in an invented volunteer.

**Call for Volunteers → Shift Demand** shows, for each timeslot, how many slots exist, how
many are open, how many volunteers picked it, and how much space is left.

## Approving volunteers

New signups wait at the top of the **Volunteers** tab, under Pending Volunteers, with their
picks, their answers, and any existing volunteer whose name looks similar.

- **Approve** makes them active, so they can be assigned. You can give them a badge number
  as their id at the same time.
- **Make inactive** declines them. They stay on file, so the same address can't simply sign
  up again.

Approval doesn't schedule anyone. Picks stay preferences until you assign them.

## The volunteer list

The rest of the **Volunteers** tab lists everyone, whatever their status, with their picks,
answers and shifts.

- **Make inactive** and **Reinstate** take someone off the schedule pickers and put them back.
  Their assignments stay. While they're inactive, those slots show as needing a replacement.
- **Decrypt emails** asks for the private key, pasted or chosen from a file, and adds an
  Email column. Addresses are decrypted in this browser only. The key isn't sent anywhere,
  and **Forget key** clears it from the page.
- **Add Volunteer** adds someone by hand. Their address is encrypted in the browser before
  it's sent.
- **Purge** deletes an inactive volunteer outright, here and on the portal. Use it to clear
  test volunteers, so their addresses can sign up again from scratch. **Purge inactive**
  deletes every inactive volunteer at once. You type the volunteer's id, or the count, to
  confirm. It can't be undone: their picks, notes, check-ins and ratings go with them, and
  their slots become empty.

Volunteers manage their own registration in the portal. They can change their shifts and
some answers, cancel, and rejoin. A cancelled volunteer's slots show as needing a
replacement.

## Assigning volunteers

On the **Schedule** tab, pick a day and a time, then click a slot. The dialog suggests
volunteers in groups: those who requested that timeslot, those who said they're flexible,
those who are available, and the rest. Anyone already working an overlapping shift that day
is left out.

The banner above the schedule lists what needs attention:

- **Slots needing a replacement**, because their volunteer cancelled or was made inactive.
  **Open** goes to the slot.
- **Dropped shifts**: a volunteer changed their picks in the portal and no longer wants a
  shift they're assigned to. **Unassign** them, or **Keep** the assignment after you've
  talked to them.

## Sending schedules

1. **Call for Volunteers → Schedule Email**: write the message in Markdown, add images,
   and preview it for any volunteer with their real shifts and QR codes.
2. **Download package**.
3. Send with `vol_mail` from your own machine. See [vol_mail](../services/vol_mail.md) and
   `tools/vol_mail/README.md`. Usually that's a draft round, then a final round to the
   people whose shifts changed.

`vol_mail` needs an export API key, made under **Admin → API Keys** with the
`volunteers:read`, `volunteers:email` and `checkin:codes` scopes. A key is shown once, so
save it straight away.

**App Settings → Rotate key** voids every check-in code already sent. Only use it if codes
have leaked, and send the schedules again afterwards.

## At the event

**Check-in.** **Check In Volunteer** on the Schedule tab opens the scanner. Scan the QR code
from the volunteer's schedule email, or type the code. If the volunteer is more than 45
minutes early or 10 minutes late, it asks first: **Check in anyway** records the check-in
with the timing. A slot's dialog has a **Check In** button too.

Checked-in slots glow green. Past slots with nobody checked in pulse red.

**Ratings and notes.** A slot's dialog can rate the volunteer **Good**, **Bad** or
**No-Show**, and holds notes for that slot.

**Radios.** On the **Radios** tab, **Check Out** a radio by scanning its barcode and choosing
the volunteer, or typing a name. A radio registers itself the first time it's scanned.
Scanning a radio someone else holds hands it over. To return radios, press **Check In** and
scan each one.
The tab lists what's out, what's available, and the full log.

## Your own account

**Profile**, under your name in the top right, changes your own password and email. Both
need your current password. It's also where you add a **passkey**, so you can sign in
without a password.

If you forget your password, **Send Reset Link** on the sign-in page mails a link to the
address on your account. An organizer can also set a new password for you under
**Admin → Users**.

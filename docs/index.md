# BSides Charm Volunteer Scheduler

The volunteer scheduler for BSides Charm. Volunteers sign up on the public portal.
Organizers approve them, build the schedule, send each volunteer their shifts, and check
them in at the event.

## Where to start

| If you want to | Read |
|---|---|
| Run it on your own machine, test it, or change it | [Getting started](getting-started.md) |
| Use the admin site to run volunteers for an event | [Organizer guide](guides/organizer.md) |
| Deploy or operate the production site | [Deployment](operations/deployment.md), [Configuration](operations/configuration.md), [Secrets and keys](operations/secrets.md) |
| Set up sending mail through Microsoft 365 | [Graph mail setup](operations/graph-mail-setup.md) |
| Cut a release and publish its docs | [Releasing](operations/releasing.md) |
| Understand one part of the system | [Backend](services/backend.md), [Portal](services/portal.md), [Frontend](services/frontend.md), [Mail relay](services/mailrelay.md), [vol_mail](services/vol_mail.md) |
| Pull data from another server | [Export API](api/export.md) |
| See how the parts talk to each other | [Interaction diagrams](architecture/interactions.md) |
| See the SBOM, known vulnerabilities and bandit's findings | [Security](security/index.md) |
| Know what the lint enforces, and the security design | [Quality & security](development/quality-and-security.md) |
| Look up a function | the **Code reference** in the navigation, starting with [backend.main](reference/backend/main.md) or [portal.main](reference/portal/main.md) |

The Export API guide is also written as one standalone file, `site/export-api.md`, on every
build, to send to integrators.

## The parts

- **Backend**: the scheduler API, on PostgreSQL. The organizers' system of record.
- **Portal**: the volunteer API, on SQLite. A separate application, reached by the
  scheduler over a signed, one-way sync API.
- **Frontend**: one Vue build, serving the call for volunteers, the portal pages and the
  organizer app.
- **Mail relay**: the only holder of the Microsoft Graph credential. It sends the portal's
  plain-text mail.
- **vol_mail**: a command-line tool, run on an organizer's machine, that sends schedule
  emails with check-in QR codes.

## Building these docs

The published docs, one version per release, are at
<https://securgeek.github.io/volunteer-manager/>. To build them yourself:

```bash
python3 -m pip install -r requirements-dev.txt   # includes backend/requirements.txt
mkdocs serve           # live preview at http://127.0.0.1:8000
mkdocs build --strict  # what ./run_lint.sh runs; any warning fails
```

Publishing a release's docs is in [Releasing](operations/releasing.md).

Docstrings are **Google style** (`Args:`, `Returns:`, `Raises:`). pylint's `docparams` check
holds documented parameters to the real signature, so the reference can't silently go stale.

More detailed design notes for each service live beside the code, in the `CLAUDE.md` files.

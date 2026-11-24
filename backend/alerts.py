"""Security alerts: rules over the audit log's events, and email through the portal.

Every audit record passes through `observe()` (registered with `audit.add_observer` by
`start()`). Each rule in `RULES` names the events it watches and what it counts them by (the
client's address, a username, a key id), and fires when it sees `threshold` of them — or
that many *distinct* usernames, for a password spray — inside `window_sec`.

A fired alert:

* is recorded as an `alert.fired` audit event, with the alert's own severity;
* is queued for email to every organizer set to get alerts (`users.audit_alerts`, Admin →
  Users) who has an address in `users.email`, through the portal's `send_admin_mail` (kind `alert`), sent by a
  background task so no request waits on mail;
* starts a **cooldown** for that rule and key (`COOLDOWN_SEC`). Matching events during it are
  only counted. When it ends, the count goes out: with the next alert for the same rule and
  key, or, if none comes, as a follow-up of its own (`flush_held_back`, every
  FLUSH_EVERY_SEC). So a flood is one email straight away and then at most one an hour, and
  no count is ever lost because the activity stopped.

At most `MAX_MAILED_PER_HOUR` alerts are emailed an hour in all; past that they are still
recorded in the audit log, with `mailed: false`.

Two rules look back through `audit_events` rather than counting: a sign-in from a network the
user hasn't signed in from in 30 days (IPv4 /24, IPv6 /64), and an export key used from a new
one. Both stay quiet when there's no history yet, so turning alerts on isn't a burst of
"new network" mail. They need the table, so they're skipped when it isn't in use.

Rules never see `alert.*` events, so an alert can't set off another. State is in memory, like
the rate limits: the backend runs one worker.
"""
import asyncio
import ipaddress
import logging
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field

import db
import portal_sync
from api_auth import rate_key
from audit import add_observer, audit, configured_to_db, remove_observer
from ratelimit import SlidingWindow

log = logging.getLogger("alerts")
# The clock the windows and cooldowns run on. A name of its own so tests can move it without
# touching time.monotonic itself, which asyncio's loop runs on too.
_monotonic = time.monotonic

COOLDOWN_SEC = 3600
FLUSH_EVERY_SEC = 600         # how often held-back counts are checked for a follow-up
MAX_MAILED_PER_HOUR = 15
LOOKBACK_DAYS = 30            # for the "new network" rules (fixed in their SQL below)
MAIL_RETRY_SEC = 60
MAX_FACTS = 10                # the portal accepts at most this many
FACT_MAX = 200                # characters per fact value


def _data(rec: dict, key: str) -> str | None:
    value = (rec.get("data") or {}).get(key)
    return None if value is None else str(value)


def _ip_key(rec: dict) -> str | None:
    return rate_key(rec["ip"]) if rec.get("ip") else None


def network(ip: str | None) -> str | None:
    """The network an address is judged by for "new network": IPv4 /24, IPv6 /64.

    Args:
        ip: An address, or anything else.

    Returns:
        The network in CIDR form, or `ip` itself when it isn't an address.
    """
    if not ip:
        return None
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    prefix = 24 if addr.version == 4 else 64
    return str(ipaddress.ip_network(f"{addr}/{prefix}", strict=False))


@dataclass(frozen=True)
class Rule:
    """One alert rule.

    Attributes:
        rule_id: A stable id, sent to the portal and recorded on `alert.fired`.
        title: One line for the email subject and the audit log.
        severity: The alert's severity, one of audit.SEVERITIES.
        events: The event codes it watches.
        key: What it counts by; None means "don't count this record".
        threshold: How many (or how many distinct, with `distinct`) fire it.
        window_sec: Over how long.
        distinct: Count distinct values of this instead of records.
        when: A further condition on the record.
        lookback: A condition that reads the audit table (see `_new_network`).
    """

    rule_id: str
    title: str
    severity: str
    events: frozenset
    key: Callable
    threshold: int = 1
    window_sec: int = 15 * 60
    distinct: Callable | None = None
    when: Callable | None = None
    lookback: Callable | None = None


# ── the "new network" look-backs ──────────────────────────────────────────────

_PRIOR_LOGIN_IPS_SQL = (
    "SELECT DISTINCT ip FROM audit_events "
    "WHERE event IN ('auth.login.ok', 'auth.passkey.login.ok') AND user_id = ? "
    "AND ts > (NOW() AT TIME ZONE 'UTC') - INTERVAL '30 days' LIMIT 1000;"
)
_PRIOR_KEY_IPS_SQL = (
    "SELECT DISTINCT ip FROM audit_events "
    "WHERE event = 'api_key.use' AND data->>'token_id' = ? "
    "AND ts > (NOW() AT TIME ZONE 'UTC') - INTERVAL '30 days' LIMIT 1000;"
)


def _new_network(sql: str, who: str | None, ip: str | None) -> bool:
    """True when `who` has history in the last 30 days and none of it from `ip`'s network.

    Remembers known (who, network) pairs so a familiar one costs no query next time.
    """
    net = network(ip)
    if not who or not net or not configured_to_db():
        return False
    with _state.lock:
        if (sql, who, net) in _state.known_networks:
            return False
    conn = db.get_conn()
    try:
        rows = conn.execute(sql, (who,)).fetchall()
    finally:
        db.put_conn(conn)
    nets = {network(r["ip"]) for r in rows}
    if not nets or net in nets:
        with _state.lock:
            _state.known_networks.add((sql, who, net))
        return False
    return True


def _login_from_new_network(rec: dict) -> bool:
    return _new_network(_PRIOR_LOGIN_IPS_SQL, rec.get("user_id"), rec.get("ip"))


def _key_from_new_network(rec: dict) -> bool:
    return _new_network(_PRIOR_KEY_IPS_SQL, _data(rec, "token_id"), rec.get("ip"))


# ── the rules ─────────────────────────────────────────────────────────────────

def _per_event(rec: dict) -> str:
    """A key for rules where every event alerts on its own (still under the cooldown and the
    hourly cap): the event and whoever or whatever it concerns."""
    target = (_data(rec, "target") or _data(rec, "target_user_id") or _data(rec, "invite_id")
              or rec.get("actor") or "")
    return f"{rec['event']}|{target}"


RULES: tuple[Rule, ...] = (
    Rule("login.bruteforce.ip", "Many failed sign-ins from one address", "high",
         frozenset({"auth.login.fail"}), _ip_key, threshold=10),
    Rule("login.bruteforce.user", "Many wrong passwords for one account", "high",
         frozenset({"auth.login.fail"}), lambda r: _data(r, "username"), threshold=5,
         when=lambda r: _data(r, "reason") == "bad_password"),
    Rule("login.spray", "Sign-ins tried against many usernames from one address", "high",
         frozenset({"auth.login.fail"}), _ip_key, threshold=5,
         distinct=lambda r: _data(r, "username")),
    Rule("login.new_network", "Sign-in from a new network", "low",
         frozenset({"auth.login.ok", "auth.passkey.login.ok"}), lambda r: r.get("user_id"),
         lookback=_login_from_new_network),
    Rule("setup.create_admin", "First-admin setup was run", "high",
         frozenset({"setup.create_admin"}), _per_event),
    Rule("setup.after_complete", "First-admin setup called on a set-up site", "medium",
         frozenset({"setup.create_admin.refused"}), _ip_key),
    Rule("account.privilege", "An organizer-site account was created or its role changed",
         "medium", frozenset({"user.create", "user.role_change", "invite.accept",
                              "user.audit_alerts"}), _per_event),
    Rule("account.credentials", "An account's password, passkeys or reset address was changed "
         "by someone else", "medium",
         frozenset({"password.reset.by_admin", "passkey.clear_user", "user.email_set",
                    "user.delete"}), _per_event),
    Rule("account.reset", "A password was reset by emailed link", "medium",
         frozenset({"password.reset.complete"}), _per_event),
    Rule("email_key.change", "The volunteer email encryption key was changed", "high",
         frozenset({"email_key.update"}), _per_event),
    Rule("api_key.create", "An export API key was created", "medium",
         frozenset({"api_key.create"}), _per_event),
    Rule("api_key.refused", "Repeated refused export API calls from one address", "medium",
         frozenset({"api_key.refused"}), _ip_key, threshold=5, window_sec=10 * 60),
    Rule("api_key.new_network", "An export API key was used from a new network", "low",
         frozenset({"api_key.use"}), lambda r: _data(r, "token_id"),
         lookback=_key_from_new_network),
    Rule("volunteer.bulk_purge", "Many volunteers purged at once", "medium",
         frozenset({"volunteer.purge"}), lambda r: r.get("actor"), threshold=5,
         window_sec=10 * 60),
    Rule("checkin.key_rotate", "The check-in key was rotated", "low",
         frozenset({"checkin.key_rotate"}), _per_event),
    Rule("security.attack", "Attack patterns in requests from one address", "high",
         frozenset({"security.attack_signature"}), _ip_key),
    Rule("security.suspicious", "Unusual run of errors from one address", "high",
         frozenset({"security.suspicious_client"}), _ip_key),
    Rule("security.sync_auth", "Bad signature on the portal's internal sync API", "critical",
         frozenset({"security.sync_auth_fail"}), _ip_key),
    Rule("portal.anomaly", "Volunteer-page input the pages themselves can't send", "medium",
         frozenset({"portal.anomaly"}), _ip_key),
)
_BY_ID = {r.rule_id: r for r in RULES}
_WATCHED = frozenset().union(*(r.events for r in RULES))
# Any high or critical event that no rule above watches still alerts, under this rule, so a
# new high-severity event code is covered from the day it's added to audit.EVENTS.
_FALLBACK = Rule("severity.high", "A high-severity security event", "high", frozenset(),
                 _per_event)


# ── state ─────────────────────────────────────────────────────────────────────

@dataclass
class _AlertState:
    """Module state: windows, cooldowns, the mail queue. Reset with `reset()`."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    # (rule_id, key) -> deque of (monotonic time, distinct value or None)
    seen: dict = field(default_factory=lambda: defaultdict(deque))
    # (rule_id, key) -> monotonic time the cooldown ends
    cooldown_until: dict = field(default_factory=dict)
    # (rule_id, key) -> matching events held back during the cooldown
    suppressed: dict = field(default_factory=lambda: defaultdict(int))
    known_networks: set = field(default_factory=set)
    mailed: SlidingWindow = field(default_factory=lambda: SlidingWindow(MAX_MAILED_PER_HOUR,
                                                                         3600))
    loop: asyncio.AbstractEventLoop | None = None
    queue: asyncio.Queue | None = None
    worker: asyncio.Task | None = None
    flusher: asyncio.Task | None = None
    # Alerts queued with no running loop (tests, or before start()).
    unsent: list = field(default_factory=list)


_state = _AlertState()


def reset() -> None:
    """Forget every window, cooldown and queued alert (tests)."""
    with _state.lock:
        _state.seen.clear()
        _state.cooldown_until.clear()
        _state.suppressed.clear()
        _state.known_networks.clear()
        _state.unsent.clear()
    _state.mailed.clear()


# ── evaluation ────────────────────────────────────────────────────────────────

def _count(rule: Rule, key: str, rec: dict, now: float) -> int:
    """Add `rec` to the rule's window for `key` and return the count that decides it."""
    q = _state.seen[(rule.rule_id, key)]
    q.append((now, rule.distinct(rec) if rule.distinct else None))
    while q and q[0][0] <= now - rule.window_sec:
        q.popleft()
    if rule.distinct:
        return len({v for _, v in q if v is not None})
    return len(q)


def _evaluate(rule: Rule, rec: dict) -> None:
    if rule.when and not rule.when(rec):
        return
    key = rule.key(rec)
    if key is None:
        return
    if rule.lookback and not rule.lookback(rec):
        return
    now = _monotonic()
    with _state.lock:
        slot = (rule.rule_id, key)
        if _state.cooldown_until.get(slot, 0) > now:
            _state.suppressed[slot] += 1
            return
        count = _count(rule, key, rec, now)
        if count < rule.threshold:
            return
        _state.seen.pop(slot, None)
        _state.cooldown_until[slot] = now + COOLDOWN_SEC
        held_back = _state.suppressed.pop(slot, 0)
    _fire(rule, key, rec, count, held_back)


def observe(rec: dict) -> None:
    """Run every rule over one audit record. Registered with `audit.add_observer`.

    Args:
        rec: An audit record (audit.build_record, through JSON).
    """
    event = rec.get("event", "")
    if event.startswith("alert."):
        return
    for rule in RULES:
        if event in rule.events:
            _evaluate(rule, rec)
    if event not in _WATCHED and rec.get("severity") in ("high", "critical"):
        _evaluate(_FALLBACK, rec)


# ── firing and mail ───────────────────────────────────────────────────────────

@dataclass
class Alert:
    """A fired alert, as queued for mail.

    Attributes:
        rule_id: The rule's id.
        title: The rule's title.
        severity: The rule's severity.
        facts: (label, value) pairs for the email, at most MAX_FACTS.
        held_back: Matching events during the previous cooldown.
        mailed: Whether it's under the hourly cap and so will be emailed.
    """

    rule_id: str
    title: str
    severity: str
    facts: list
    held_back: int = 0
    mailed: bool = True


def _facts(rule: Rule, rec: dict, count: int) -> list:
    """The email's facts: what, when, where from, who, and the event's own details (no
    email addresses: a key holding one is skipped)."""
    facts = [("Event", rec.get("event")), ("When (UTC)", rec.get("ts")),
             ("Client address", rec.get("ip")), ("Signed-in user", rec.get("actor")),
             ("Request", f"{rec.get('method')} {rec.get('path')}" if rec.get("path") else None)]
    if rule.threshold > 1:
        what = "different usernames" if rule.distinct else "events"
        facts.append(("Seen", f"{count} {what} in {rule.window_sec // 60} minutes"))
    for k, v in (rec.get("data") or {}).items():
        if "email" not in k.lower() and v not in (None, ""):
            facts.append((k.replace("_", " ").capitalize(), v))
    return [(label, str(value)[:FACT_MAX]) for label, value in facts
            if value not in (None, "")][:MAX_FACTS]


def _fire(rule: Rule, key: str, rec: dict, count: int, held_back: int) -> None:
    alert = Alert(rule.rule_id, rule.title, rule.severity, _facts(rule, rec, count), held_back)
    alert.mailed = _state.mailed.allow("all")
    audit("alert.fired", None, None, rule.title, severity=rule.severity, rule=rule.rule_id,
          key=key, trigger=rec.get("event"), count=count, held_back=held_back,
          mailed=alert.mailed)
    if not alert.mailed:
        log.warning("alerts: hourly mail cap reached; %s recorded but not mailed", rule.rule_id)
        return
    _queue(alert)


_RECIPIENTS_SQL = (
    "SELECT username, email FROM users WHERE audit_alerts AND role = 'organizer' "
    "AND email IS NOT NULL AND email <> '' ORDER BY username;"
)


def recipients() -> list:
    """Who alerts are mailed to: organizers set to get them (`users.audit_alerts`, Admin →
    Users) who have an email address.

    Returns:
        A list of {"username", "email"} dicts.
    """
    conn = db.get_conn()
    try:
        return [dict(r) for r in conn.execute(_RECIPIENTS_SQL).fetchall()]
    finally:
        db.put_conn(conn)


def _app_title() -> str:
    conn = db.get_conn()
    try:
        return db.get_app_kv(conn, "app_title") or "the conference"
    finally:
        db.put_conn(conn)


async def deliver(alert: Alert, retry: bool = True) -> int:
    """Email one alert to every recipient, retrying each once after MAIL_RETRY_SEC.

    Records `alert.mailed` (how many it reached) or `alert.mail_fail` (whom it didn't, by
    username, never address).

    Args:
        alert: The alert.
        retry: Retry a failed send once. Off for the test alert, whose caller is waiting.

    Returns:
        How many recipients it reached.
    """
    people = await asyncio.to_thread(recipients)
    if not people:
        log.warning("alerts: %s not mailed: no organizer set to get alerts has an email",
                    alert.rule_id)
        audit("alert.mail_fail", None, None, "No one to email the alert to",
              rule=alert.rule_id, reason="no_recipients")
        return 0
    fields = {"app_title": await asyncio.to_thread(_app_title), "rule": alert.rule_id,
              "title": alert.title, "severity": alert.severity,
              "facts": [list(f) for f in alert.facts], "held_back": alert.held_back}
    link = f"{portal_sync.site_origin()}/admin/audit"
    reached = 0
    for person in people:
        for attempt in ((1, 2) if retry else (2,)):
            try:
                await portal_sync.send_admin_mail("alert", person["email"], link, **fields)
                reached += 1
                break
            except Exception as exc:  # pylint: disable=broad-exception-caught
                # Deliberately broad: the portal, the network or the relay can each fail in
                # their own way, and one recipient's failure mustn't stop the others.
                if attempt == 1:
                    await asyncio.sleep(MAIL_RETRY_SEC)
                    continue
                log.error("alerts: %s not mailed to %s: %s", alert.rule_id,
                          person["username"], exc)
                audit("alert.mail_fail", None, None, "A security alert could not be emailed",
                      rule=alert.rule_id, recipient=person["username"], error=str(exc)[:200])
    if reached:
        audit("alert.mailed", None, None, "Security alert emailed", rule=alert.rule_id,
              recipients=reached)
    return reached


def flush_held_back() -> int:
    """Send a follow-up for every rule and key whose cooldown has ended with events held
    back and no new alert to carry them. Each follow-up starts a new cooldown, so a flood
    that keeps going is a summary an hour.

    Returns:
        How many follow-ups fired.
    """
    now = _monotonic()
    due = []
    with _state.lock:
        for slot, count in list(_state.suppressed.items()):
            if count and _state.cooldown_until.get(slot, 0) <= now and slot[0] in _BY_ID:
                due.append((slot, count))
                del _state.suppressed[slot]
                _state.cooldown_until[slot] = now + COOLDOWN_SEC
    for (rule_id, key), count in due:
        rule = _BY_ID[rule_id]
        alert = Alert(rule.rule_id, f"{rule.title} (continued)", rule.severity,
                      [("Rule", rule.rule_id), ("For", str(key)[:FACT_MAX]),
                       ("Held back", f"{count} more in the last hour")], count)
        alert.mailed = _state.mailed.allow("all")
        audit("alert.fired", None, None, alert.title, severity=rule.severity, rule=rule_id,
              key=key, follow_up=True, held_back=count, mailed=alert.mailed)
        if alert.mailed:
            _queue(alert)
    return len(due)


def _queue(alert: Alert) -> None:
    loop, queue = _state.loop, _state.queue
    if loop is not None and queue is not None and loop.is_running():
        loop.call_soon_threadsafe(queue.put_nowait, alert)
    else:
        with _state.lock:
            _state.unsent.append(alert)


async def _flusher() -> None:
    """Run flush_held_back every FLUSH_EVERY_SEC, for as long as the app runs."""
    while True:
        await asyncio.sleep(FLUSH_EVERY_SEC)
        try:
            flush_held_back()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            # Deliberately broad: the loop must outlive one bad pass.
            log.error("alerts: flushing held-back counts failed: %s", exc)


async def _worker() -> None:
    """Mail queued alerts one at a time, for as long as the app runs."""
    while True:
        alert = await _state.queue.get()
        try:
            await deliver(alert)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            # Deliberately broad: the worker must outlive any one alert's failure.
            log.error("alerts: delivering %s failed: %s", alert.rule_id, exc)


def start(loop: asyncio.AbstractEventLoop) -> None:
    """Start watching audit events and mailing alerts. Called from main.lifespan.

    Args:
        loop: The running event loop, which the mail worker runs on.
    """
    _state.loop = loop
    _state.queue = asyncio.Queue()
    _state.worker = loop.create_task(_worker())
    _state.flusher = loop.create_task(_flusher())
    add_observer(observe)
    with _state.lock:
        pending, _state.unsent = _state.unsent, []
    for alert in pending:
        _state.queue.put_nowait(alert)


def stop() -> None:
    """Stop watching and cancel the mail worker (shutdown, tests)."""
    remove_observer(observe)
    for task in (_state.worker, _state.flusher):
        if task is not None:
            task.cancel()
    _state.loop = _state.queue = _state.worker = _state.flusher = None

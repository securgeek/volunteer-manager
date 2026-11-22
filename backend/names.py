"""Taking a stored volunteer name apart, and the export API's name format.

The scheduler keeps one ``volunteers.name``. All three ways in write it the same way:
``First L.`` plus an optional ``(handle)`` — add_vol, the CSV import (routers/setup.py), and
portal signups (portal/routers/signup.display_name). The parts aren't stored separately, so the
export API splits them back out here.

Read from the right, so a two-word first name survives: ``Mary Ann S. (zc)`` is first
``Mary Ann``, initial ``S``, handle ``zc``. A name not in that shape (an older roster, typed by
hand) still gives something sensible: a full last word becomes the initial, and a single word
is a first name alone.
"""
import re

_HANDLE = re.compile(r"^(.*?)\s*\(([^()]*)\)\s*$")
_INITIAL = re.compile(r"^(.*\S)\s+(\S)\.?$")
_LAST_WORD = re.compile(r"^(.*\S)\s+(\S+)$")


def split_name(name: str) -> tuple:
    """(first_name, last_initial, handle) from a stored name; any part may be ''.

    Args:
        name: A stored name such as `"Ada L."`, `"Ada Lovelace"` or `"Ada L. (countess)"`.
            A full last name is cut to its initial.
    """
    rest, handle = (name or "").strip(), ""
    m = _HANDLE.match(rest)
    if m:
        rest, handle = m.group(1).strip(), m.group(2).strip()
    m = _INITIAL.match(rest)
    if m:
        return m.group(1), m.group(2).upper(), handle
    m = _LAST_WORD.match(rest)
    if m:
        return m.group(1), m.group(2)[0].upper(), handle
    return rest, "", handle


def export_name(first: str, last_initial: str, handle: str) -> str:
    """``First L.``, or ``Handle [First L.]`` when there's a handle.

    Args:
        first: The first name.
        last_initial: The last initial, without the dot.
        handle: The volunteer's handle, or `''`.
    """
    base = f"{first} {last_initial}." if first and last_initial else (first or "")
    if handle:
        return f"{handle} [{base}]" if base else handle
    return base

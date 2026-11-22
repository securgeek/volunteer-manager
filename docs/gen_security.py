"""MkDocs hook: the Security pages' generated parts, the SBOM and the bandit results.

Each page holds a marker, replaced on every build:

- ``<!-- security: summary -->`` on security/index.md: the as-of line, open vulnerabilities by
  artifact and severity, and bandit's totals.
- ``<!-- security: sbom -->`` on security/sbom.md: every artifact's components and known
  vulnerabilities, from the CycloneDX files ``tools/sbom.sh`` writes to docs/security/sbom/.
  Those files are the record: run the script after building the images, and commit them.
- ``<!-- security: bandit -->`` on security/bandit.md: bandit run now, at every level, over the
  same files and with the same settings as ``run_lint.sh``.

The SBOM is as of the day the script ran. The bandit results are as of this build. A missing
SBOM is shown as missing, not a build failure, so a fresh clone still builds the docs.
"""
from __future__ import annotations

import json
import re
# Runs bandit and git, with fixed argument lists only.
import subprocess  # nosec B404
import sys
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SBOM_DIR = ROOT / "docs" / "security" / "sbom"
MARKER = re.compile(r"<!--\s*security:\s*(summary|sbom|bandit)\s*-->")

# Bandit runs as run_lint.sh does: each app's code, then its tests with B101 (assert) skipped.
BANDIT_APPS = (("backend", "backend"), ("portal", "portal"), ("mailrelay", "mailrelay"),
               ("vol_mail", "tools/vol_mail"))
SEVERITIES = ("critical", "high", "medium", "low", "unknown")
BANDIT_LEVELS = ("HIGH", "MEDIUM", "LOW")
NOSEC = re.compile(r"#\s*nosec\b(?P<rest>.*)$")
ARTIFACT_TITLES = {
    "backend": "Backend image", "portal": "Portal image", "mailrelay": "Mail relay image",
    "frontend": "Frontend image (nginx)", "postgres": "PostgreSQL image",
    "frontend-npm": "Frontend JavaScript libraries", "vol_mail": "vol_mail (organizer's machine)",
}

_STATE: dict[str, Any] = {}


# ── shared ────────────────────────────────────────────────────────────────────────────────

def _cell(text: Any) -> str:
    """Make text safe for one Markdown table cell.

    Args:
        text: Anything; ``None`` becomes a dash.

    Returns:
        One line, with pipes escaped.
    """
    if text is None or text == "":
        return "—"
    return " ".join(str(text).split()).replace("|", "\\|")


def _when(stamp: str | None) -> str:
    """Format an ISO timestamp as ``2026-11-21 23:50 UTC``.

    Args:
        stamp: An ISO 8601 timestamp, with ``Z`` or an offset, possibly with nanoseconds.

    Returns:
        The time in UTC, or the input unchanged if it doesn't parse.
    """
    if not stamp:
        return "unknown"
    text = re.sub(r"(\.\d{6})\d+", r"\1", stamp.replace("Z", "+00:00"))
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return stamp
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _run(args: list[str]) -> subprocess.CompletedProcess:
    """Run a fixed command from the repo root, capturing its output.

    Args:
        args: The command and its arguments. Never built from user input.

    Returns:
        The finished process. A non-zero exit is the caller's to judge: bandit exits 1
        whenever it reports anything.
    """
    # A fixed argument list, no shell: nothing in it comes from outside this file.
    return subprocess.run(args, cwd=ROOT, capture_output=True, text=True,  # nosec B603
                          encoding="utf-8", check=False)


def _commit() -> str:
    """The checked-out commit, for the as-of line.

    Returns:
        ``commit abc1234``, with ``plus uncommitted changes`` when the tree is dirty, or
        ``''`` outside a git checkout.
    """
    head = _run(["git", "rev-parse", "--short", "HEAD"])
    if head.returncode != 0:
        return ""
    dirty = _run(["git", "status", "--porcelain"]).stdout.strip()
    return f"commit `{head.stdout.strip()}`" + (", plus uncommitted changes" if dirty else "")


def _package_version(name: str) -> str:
    """An installed package's version.

    Args:
        name: The distribution name.

    Returns:
        Its version, or ``unknown``.
    """
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "unknown"


# ── SBOM ──────────────────────────────────────────────────────────────────────────────────

def _severity(vuln: dict) -> str:
    """The severity to show: the rating from the vulnerability's own source, else the highest.

    Args:
        vuln: One CycloneDX vulnerability.

    Returns:
        One of ``SEVERITIES``.
    """
    ratings = vuln.get("ratings") or []
    source = (vuln.get("source") or {}).get("name")
    own = [r for r in ratings if (r.get("source") or {}).get("name") == source]
    found = [str(r.get("severity", "unknown")).lower() for r in (own or ratings)]
    found = [s if s in SEVERITIES else "unknown" for s in found] or ["unknown"]
    return min(found, key=SEVERITIES.index)


def _pkg_type(component: dict) -> str | None:
    """Trivy's package type for a component (``alpine``, ``python-pkg``, ``npm``, ...).

    Args:
        component: One CycloneDX component.

    Returns:
        The type, or ``None`` for the operating system and other non-package entries.
    """
    for prop in component.get("properties") or []:
        if prop.get("name") == "aquasecurity:trivy:PkgType":
            return prop.get("value")
    return None


def _licenses(component: dict) -> str:
    """A component's licences as one string.

    Args:
        component: One CycloneDX component.

    Returns:
        The SPDX ids, names or expressions, comma-separated, or ``''``.
    """
    out = []
    for entry in component.get("licenses") or []:
        lic = entry.get("license") or {}
        out.append(lic.get("id") or lic.get("name") or entry.get("expression") or "")
    return ", ".join(x for x in out if x)


def _load_sbom() -> dict | None:
    """Read meta.json and every artifact's CycloneDX file.

    Returns:
        ``meta`` plus a parsed ``artifacts`` list, or ``None`` when no SBOM has been made.
    """
    meta_path = SBOM_DIR / "meta.json"
    if not meta_path.exists():
        return None
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    artifacts = []
    for art in meta.get("artifacts", []):
        bom = json.loads((SBOM_DIR / art["file"]).read_text(encoding="utf-8"))
        names = {c.get("bom-ref"): (c.get("name"), c.get("version"))
                 for c in bom.get("components") or []}
        packages = [c for c in bom.get("components") or [] if _pkg_type(c)]
        system = [c for c in bom.get("components") or [] if c.get("type") == "operating-system"]
        vulns = []
        for vuln in bom.get("vulnerabilities") or []:
            for hit in vuln.get("affects") or [{}]:
                name, version = names.get(hit.get("ref"), (hit.get("ref"), None))
                vulns.append({"id": vuln.get("id"), "severity": _severity(vuln),
                              "package": name, "version": version,
                              "fix": vuln.get("recommendation") or "",
                              "url": (vuln.get("source") or {}).get("url")})
        vulns.sort(key=lambda v: (SEVERITIES.index(v["severity"]), v["package"] or "", v["id"]))
        artifacts.append({**art, "packages": packages, "vulns": vulns,
                          "os": ", ".join(f"{c.get('name')} {c.get('version')}"
                                          for c in system)})
    return {**meta, "artifacts": artifacts}


def _sbom_asof(sbom: dict | None) -> str:
    """The SBOM's as-of sentence.

    Args:
        sbom: From ``_load_sbom``, or ``None``.

    Returns:
        One Markdown sentence.
    """
    if not sbom:
        return ("**No SBOM has been generated yet.** Build the images, run `tools/sbom.sh`, "
                "and commit `docs/security/sbom/`.")
    return (f"**SBOM and vulnerabilities as of {_when(sbom.get('generated_at'))}**, from trivy "
            f"{sbom.get('trivy_version')} with its vulnerability database of "
            f"{_when(sbom.get('vuln_db_updated_at'))}. New vulnerabilities are published "
            f"daily: run `tools/sbom.sh` again for a current view.")


def _counts(vulns: list[dict]) -> dict[str, int]:
    """Count vulnerabilities by severity.

    Args:
        vulns: Vulnerabilities as ``_load_sbom`` lists them.

    Returns:
        Severity to count, for every severity.
    """
    out = dict.fromkeys(SEVERITIES, 0)
    for vuln in vulns:
        out[vuln["severity"]] += 1
    return out


def _vuln_table(sbom: dict) -> list[str]:
    """The per-artifact count table.

    Args:
        sbom: From ``_load_sbom``.

    Returns:
        Markdown lines.
    """
    lines = ["| Artifact | Components | " + " | ".join(s.capitalize() for s in SEVERITIES) + " |",
             "|---|---:|" + "---:|" * len(SEVERITIES)]
    for art in sbom["artifacts"]:
        counts = _counts(art["vulns"])
        title = ARTIFACT_TITLES.get(art["name"], art["name"])
        lines.append(f"| [{title}](sbom.md#{art['name']}) | {len(art['packages'])} | "
                     + " | ".join(str(counts[s]) for s in SEVERITIES) + " |")
    return lines


def render_sbom() -> str:
    """The SBOM page's generated part.

    Returns:
        Markdown: the as-of line, the summary, then one section per artifact.
    """
    sbom = _load_sbom()
    lines = [_sbom_asof(sbom), ""]
    if not sbom:
        return "\n".join(lines)
    lines += _vuln_table(sbom)
    for art in sbom["artifacts"]:
        title = ARTIFACT_TITLES.get(art["name"], art["name"])
        lines += ["", f"## {title} {{#{art['name']}}}", "",
                  f"From `{art['source']}` ({art['detail']})."
                  + (f" Base system: {art['os']}." if art["os"] else ""),
                  f"CycloneDX file: [{art['file']}](sbom/{art['file']}).", ""]
        if art["vulns"]:
            lines += ["**Known vulnerabilities**", "",
                      "| Severity | ID | Package | Installed | Fix |", "|---|---|---|---|---|"]
            for vuln in art["vulns"]:
                ident = f"[{vuln['id']}]({vuln['url']})" if vuln["url"] else vuln["id"]
                lines.append(f"| {vuln['severity'].capitalize()} | {_cell(ident)} | "
                             f"{_cell(vuln['package'])} | {_cell(vuln['version'])} | "
                             f"{_cell(vuln['fix'])} |")
        else:
            lines.append("No known vulnerabilities.")
        lines += ["", f"**Components ({len(art['packages'])})**", "",
                  "| Name | Version | Type | Licence |", "|---|---|---|---|"]
        for comp in sorted(art["packages"], key=lambda c: (c.get("group") or "", c.get("name"))):
            name = f"{comp['group']}/{comp['name']}" if comp.get("group") else comp.get("name")
            lines.append(f"| {_cell(name)} | {_cell(comp.get('version'))} | "
                         f"{_cell(_pkg_type(comp))} | {_cell(_licenses(comp))} |")
    return "\n".join(lines)


# ── bandit ────────────────────────────────────────────────────────────────────────────────

def _bandit_files(app_dir: str) -> tuple[list[str], list[str]]:
    """Split an app's Python files into code and tests, as run_lint.sh does.

    Args:
        app_dir: The app's directory, relative to the repo root.

    Returns:
        ``(code, tests)``, repo-relative paths. Tests are ``test_*.py`` and ``conftest.py``.
    """
    code, tests = [], []
    for path in sorted((ROOT / app_dir).rglob("*.py")):
        if "__pycache__" in path.parts or ".venv" in path.parts:
            continue
        rel = path.relative_to(ROOT).as_posix()
        (tests if path.name.startswith("test_") or path.name == "conftest.py" else code).append(rel)
    return code, tests


def _bandit(files: list[str], skip_assert: bool) -> dict:
    """Run bandit over some files at every level, with the project's bandit.yaml.

    Args:
        files: Repo-relative paths.
        skip_assert: Skip B101, as for test files.

    Returns:
        Bandit's JSON report, or an empty one for no files.

    Raises:
        RuntimeError: When bandit doesn't produce a report.
    """
    if not files:
        return {"results": [], "metrics": {"_totals": {"loc": 0, "nosec": 0}}}
    args = [sys.executable, "-m", "bandit", "-q", "-f", "json", "-c", "bandit.yaml"]
    if skip_assert:
        args += ["--skip", "B101"]
    proc = _run(args + files)
    try:
        return json.loads(proc.stdout)
    except ValueError as exc:
        raise RuntimeError(f"bandit produced no report: {proc.stderr.strip()[:300]}") from exc


def _reason_above(lines: list[str], index: int) -> str:
    """The comment just above a line: the reason, where it's written there instead.

    Takes the nearest comment line, plus the lines before it that it continues (a line not
    ending in a full stop runs on into the next).

    Args:
        lines: The file's lines.
        index: The 0-based index of the ``# nosec`` line.

    Returns:
        The comment text, or ``''`` when the line above isn't a comment.
    """
    block: list[str] = []
    i = index - 1
    while i >= 0 and lines[i].strip().startswith("#"):
        text = lines[i].strip().lstrip("#").strip()
        if block and text.endswith("."):
            break
        block.insert(0, text)
        i -= 1
    return " ".join(block)


def _clean_reason(text: str) -> str:
    """Strip a leading ``nosec Bxxx —`` from a reason.

    Args:
        text: The comment text.

    Returns:
        The reason alone.
    """
    text = re.sub(r"^nosec\b", "", text.strip()).strip()
    return re.sub(r"^[\sB\d,]*[—–:-]*\s*", "", text)


def _nosec_lines(files: list[str]) -> list[dict]:
    """Every ``# nosec`` in some files, with its reason.

    The reason is the text after the test ids, or else the comment just above the line. A
    ``# nosec`` alone on a comment line suppresses nothing, so it's read only as a reason.

    Args:
        files: Repo-relative paths.

    Returns:
        ``file``, ``line``, ``tests`` and ``reason`` for each one.
    """
    out = []
    for rel in files:
        lines = (ROOT / rel).read_text(encoding="utf-8").splitlines()
        for index, text in enumerate(lines):
            found = NOSEC.search(text)
            if not found or text.strip().startswith("#"):
                continue
            rest = found.group("rest").strip()
            tests = re.findall(r"\bB\d{3}\b", rest)
            reason = _clean_reason(rest) or _clean_reason(_reason_above(lines, index))
            out.append({"file": rel, "line": index + 1, "tests": ", ".join(tests) or "all",
                        "reason": reason})
    return out


def _bandit_results() -> dict:
    """Run bandit over every app once per build, and keep the results for all three markers.

    Returns:
        ``version``, ``as_of``, ``commit``, ``rows`` (one per app part), ``findings`` and
        ``nosec``.
    """
    if "bandit" in _STATE:
        return _STATE["bandit"]
    rows, findings, nosec = [], [], []
    parts = []
    for name, app_dir in BANDIT_APPS:
        code, tests = _bandit_files(app_dir)
        parts += [(name, "code", code, False), (name, "tests", tests, True)]
    parts.append(("repo root", "tests", ["conftest.py"], True))
    for name, kind, files, skip_assert in parts:
        report = _bandit(files, skip_assert)
        counts = {level: 0 for level in BANDIT_LEVELS}
        for result in report.get("results", []):
            counts[result["issue_severity"]] += 1
            findings.append({**result, "app": name, "kind": kind,
                             "filename": Path(result["filename"]).as_posix()})
        totals = report.get("metrics", {}).get("_totals", {})
        accepted = _nosec_lines(files)
        rows.append({"app": name, "kind": kind, "files": len(files),
                     "loc": int(totals.get("loc", 0)), "counts": counts,
                     "nosec": len(accepted)})
        nosec += [{**n, "app": name} for n in accepted]
    findings.sort(key=lambda r: (BANDIT_LEVELS.index(r["issue_severity"]),
                                 BANDIT_LEVELS.index(r["issue_confidence"]),
                                 r["filename"], r["line_number"]))
    _STATE["bandit"] = {
        "version": _package_version("bandit"),
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "commit": _commit(), "rows": rows, "findings": findings, "nosec": nosec,
    }
    return _STATE["bandit"]


def _bandit_asof(res: dict) -> str:
    """The bandit results' as-of sentence.

    Args:
        res: From ``_bandit_results``.

    Returns:
        One Markdown sentence.
    """
    commit = f", {res['commit']}" if res["commit"] else ""
    return f"**Bandit results as of {res['as_of']}**{commit}: bandit {res['version']}, run when " \
           "these docs were built."


def _gate_failures(res: dict) -> int:
    """How many findings would fail the lint (medium or above, at medium confidence or above).

    Args:
        res: From ``_bandit_results``.

    Returns:
        The count. It should be 0, since the lint must pass for a change to land.
    """
    gate = BANDIT_LEVELS[:2]
    return sum(1 for f in res["findings"]
               if f["issue_severity"] in gate and f["issue_confidence"] in gate)


def render_bandit() -> str:
    """The bandit page's generated part.

    Returns:
        Markdown: as-of, the per-app table, every finding, and every accepted ``# nosec``.
    """
    res = _bandit_results()
    lines = [_bandit_asof(res), "",
             f"Findings that fail the lint (medium or high severity, at medium or high "
             f"confidence): **{_gate_failures(res)}**.", "",
             "| App | Part | Files | Lines | High | Medium | Low | `# nosec` |",
             "|---|---|---:|---:|---:|---:|---:|---:|"]
    for row in res["rows"]:
        c = row["counts"]
        lines.append(f"| {row['app']} | {row['kind']} | {row['files']} | {row['loc']} | "
                     f"{c['HIGH']} | {c['MEDIUM']} | {c['LOW']} | {row['nosec']} |")
    lines += ["", "## Every finding", ""]
    if res["findings"]:
        lines += ["Findings at every level, including the low ones the lint lets through.", "",
                  "| Severity | Confidence | Test | Where | What |", "|---|---|---|---|---|"]
        for f in res["findings"]:
            where = f"`{f['filename']}:{f['line_number']}`"
            test = f"[{f['test_id']}]({f['more_info']})" if f.get("more_info") else f["test_id"]
            lines.append(f"| {f['issue_severity'].capitalize()} | "
                         f"{f['issue_confidence'].capitalize()} | {test} {f['test_name']} | "
                         f"{where} | {_cell(f['issue_text'])} |")
    else:
        lines.append("None, at any level.")
    lines += ["", "## Accepted exceptions", "",
              "Each `# nosec` marks one finding accepted where it happens, with the reason "
              "beside it. There are no global skips.", ""]
    if res["nosec"]:
        lines += ["| Where | Test | Reason |", "|---|---|---|"]
        for n in res["nosec"]:
            lines.append(f"| `{n['file']}:{n['line']}` | {n['tests']} | {_cell(n['reason'])} |")
    else:
        lines.append("None.")
    return "\n".join(lines)


def render_summary() -> str:
    """The Security overview's generated part.

    Returns:
        Markdown: both as-of lines, the vulnerability table and bandit's totals.
    """
    sbom = _load_sbom()
    res = _bandit_results()
    lines = ["!!! info \"As of\"", "    " + _sbom_asof(sbom), "",
             "    " + _bandit_asof(res), "", "## Known vulnerabilities", ""]
    lines += _vuln_table(sbom) if sbom else ["No SBOM yet: see [SBOM](sbom.md)."]
    total = {level: sum(r["counts"][level] for r in res["rows"]) for level in BANDIT_LEVELS}
    lines += ["", "## Static analysis (bandit)", "",
              f"{sum(r['files'] for r in res['rows'])} files, "
              f"{sum(r['loc'] for r in res['rows'])} lines. Findings: {total['HIGH']} high, "
              f"{total['MEDIUM']} medium, {total['LOW']} low; **{_gate_failures(res)}** at the "
              f"level that fails the lint. {len(res['nosec'])} accepted exceptions. "
              "[Details](bandit.md)."]
    return "\n".join(lines)


# ── MkDocs hook events ────────────────────────────────────────────────────────────────────

RENDERERS = {"summary": render_summary, "sbom": render_sbom, "bandit": render_bandit}


def on_pre_build(**_: Any) -> None:
    """Forget the last build's bandit run, so ``mkdocs serve`` shows current results.

    Args:
        **_: What MkDocs passes (unused).
    """
    _STATE.clear()


def on_page_markdown(markdown: str, **_: Any) -> str:
    """Expand any security marker on a page; leave pages without one alone.

    Args:
        markdown: The page source.
        **_: The page, config and files MkDocs passes (unused).

    Returns:
        The page source, with each marker replaced.
    """
    return MARKER.sub(lambda m: RENDERERS[m.group(1)](), markdown)

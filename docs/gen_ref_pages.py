"""mkdocs-gen-files script: one code-reference page per Python module.

Runs on every build, so a new module shows up in the reference without anyone editing a list.
Each page is a single mkdocstrings directive (``::: backend.routers.export``) that renders the
module's Google-style docstrings.

The apps are not packages (no ``__init__.py`` at their top level), and backend/ and portal/
both have ``db`` and ``main``. mkdocstrings is pointed at the repo root (``paths: [.]`` in
mkdocs.yml), so each app loads as a namespace package and the two ``db`` modules are
``backend.db`` and ``portal.db``. This is static analysis: nothing here is imported.
"""
from pathlib import Path

import mkdocs_gen_files

ROOT = Path(__file__).resolve().parent.parent

# Directory → section title. Scheduler/ (legacy, reference only) is deliberately absent.
APPS = {
    "backend": "Backend (scheduler API)",
    "portal": "Portal",
    "mailrelay": "Mail relay",
    "tools/vol_mail": "vol_mail (schedule mailer)",
}


def _is_documented(path: Path) -> bool:
    """Skip tests, conftest and caches: the reference is for the application code."""
    return not (path.name.startswith("test_") or path.name == "conftest.py"
                or "__pycache__" in path.parts)


nav = mkdocs_gen_files.Nav()
for app_dir, title in APPS.items():
    for path in sorted((ROOT / app_dir).rglob("*.py")):
        if not _is_documented(path):
            continue
        rel = path.relative_to(ROOT).with_suffix("")
        parts = list(rel.parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
            doc_path = Path(*parts, "index.md")
        else:
            doc_path = rel.with_suffix(".md")
        ident = ".".join(parts)
        # Nav: section title, then the module path inside the app.
        app_parts = len(Path(app_dir).parts)
        nav[(title, *parts[app_parts:]) if parts[app_parts:] else (title,)] = doc_path.as_posix()
        with mkdocs_gen_files.open(Path("reference", doc_path), "w") as fd:
            fd.write(f"::: {ident}\n")
        mkdocs_gen_files.set_edit_path(Path("reference", doc_path), path.relative_to(ROOT))

with mkdocs_gen_files.open("reference/SUMMARY.md", "w") as nav_file:
    nav_file.writelines(nav.build_literate_nav())

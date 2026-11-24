"""MkDocs hook: generate the export API reference from backend/routers/export.py.

docs/api/export.md is the hand-written caller's guide. Wherever it has a marker line

    <!-- export-api: /api/export/schedule -->

this hook puts that endpoint's generated reference: the scope it needs, its query parameters,
its response fields (one table per object, nested lists included) and an example response. All
of it comes from the router's OpenAPI schema, which FastAPI builds from the ``Query``
descriptions and the response models. Change the docs by changing those, not the output.

After the build, the whole page, with the reference filled in, is also written to
``site/export-api.md``: one self-contained file to send to whoever is integrating.

Run directly to print that standalone Markdown without building the site:

    python docs/gen_export_api.py > export-api.md
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PAGE = "api/export.md"
STANDALONE = "export-api.md"
MARKER = re.compile(r"^<!-- export-api: (\S+) -->$", re.MULTILINE)
# Notes to maintainers in the page source; never sent to callers.
MAINTAINER_NOTE = re.compile(r"^<!--\s*Maintainers:.*?-->\n\n?", re.MULTILINE | re.DOTALL)

_STATE: dict[str, str] = {}


def _load_api() -> tuple[dict[str, Any], dict[str, str]]:
    """Return the export router's OpenAPI document and each path's required key scope.

    Only routers/export.py is imported, not main.py, so this needs the backend's packages but
    no database and no environment.

    Returns:
        The OpenAPI document, and a map of path to the scope its ``TokenChecker`` requires.
    """
    backend = str(ROOT / "backend")
    if backend not in sys.path:
        sys.path.insert(0, backend)
    # pylint: disable=import-outside-toplevel  # backend/ is only importable once on sys.path
    from fastapi.openapi.utils import get_openapi

    from api_auth import TokenChecker
    from routers import export

    spec = get_openapi(title="Volunteer Manager export API", version="1", routes=export.router.routes)
    scopes = {}
    for route in export.router.routes:
        for dep in route.dependant.dependencies:
            if isinstance(dep.call, TokenChecker):
                scopes[route.path] = dep.call.scope
    return spec, scopes


def _ref_name(schema: dict[str, Any]) -> str | None:
    ref = schema.get("$ref") or (schema.get("items") or {}).get("$ref")
    return ref.rsplit("/", 1)[-1] if ref else None


def _type(schema: dict[str, Any]) -> str:
    if "anyOf" in schema:
        return " or ".join(_type(s) for s in schema["anyOf"])
    if schema.get("type") == "array":
        inner = _ref_name(schema)
        return f"array of {inner}" if inner else "array"
    if "$ref" in schema:
        return _ref_name(schema) or "object"
    return schema.get("type", "any")


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def _example(name: str, schemas: dict[str, Any], with_scoped: bool = False) -> Any:
    """Build an example object for one schema from its fields' ``examples``.

    Args:
        name: The schema's name in ``components.schemas``.
        schemas: All component schemas.
        with_scoped: Include fields that need an extra scope.

    Returns:
        A JSON-able example object.
    """
    out = {}
    for field, prop in schemas[name]["properties"].items():
        if "x-scope" in prop and not with_scoped:
            continue
        inner = _ref_name(prop)
        if inner and prop.get("type") == "array":
            out[field] = [_example(inner, schemas, with_scoped)]
        elif inner:
            out[field] = _example(inner, schemas, with_scoped)
        else:
            out[field] = (prop.get("examples") or [None])[0]
    return out


def _fields_table(name: str, schemas: dict[str, Any], title: str, lines: list[str]) -> None:
    """Append a field table for one schema, then one for each nested object it lists.

    Args:
        name: The schema's name in ``components.schemas``.
        schemas: All component schemas.
        title: The heading text for this table.
        lines: Output, appended to.
    """
    props = schemas[name]["properties"]
    lines += ["", f"**{title}**", "", "| Field | Type | Description |", "|---|---|---|"]
    nested = []
    for field, prop in props.items():
        desc = prop.get("description", "")
        if "x-scope" in prop:
            desc = f"Only with the `{prop['x-scope']}` scope. {desc}"
        lines.append(f"| `{field}` | {_type(prop)} | {_cell(desc)} |")
        inner = _ref_name(prop)
        if inner:
            nested.append((field, inner))
    for field, inner in nested:
        _fields_table(inner, schemas, f"Each item in `{field}` ({inner})", lines)


def render_endpoint(path: str, spec: dict[str, Any], scopes: dict[str, str]) -> str:
    """Render the generated reference for one endpoint as Markdown.

    Args:
        path: The endpoint's path, as in the marker.
        spec: The OpenAPI document.
        scopes: Map of path to required scope.

    Returns:
        Markdown for the endpoint's scope, parameters, response fields and example.

    Raises:
        KeyError: The router has no GET route at ``path``.
    """
    op = spec["paths"][path]["get"]
    schemas = spec["components"]["schemas"]
    lines = [f"Needs the `{scopes[path]}` scope." if path in scopes else "Needs no key."]

    params = op.get("parameters", [])
    if params:
        lines += ["", "| Parameter | Type | Default | Effect |", "|---|---|---|---|"]
        for p in params:
            schema = p.get("schema", {})
            default = schema.get("default")
            default = "—" if default is None else f"`{json.dumps(default)}`"
            lines.append(f"| `{p['name']}` | {_type(schema)} | {default} | "
                         f"{_cell(p.get('description', ''))} |")

    response = _ref_name(op["responses"]["200"]["content"]["application/json"]["schema"])
    _fields_table(response, schemas, "Response", lines)
    example = json.dumps(_example(response, schemas), indent=2)
    lines += ["", "**Example response**", "", "```json", example, "```"]
    return "\n".join(lines)


def expand(markdown: str) -> str:
    """Expand every ``<!-- export-api: PATH -->`` marker and drop maintainer notes.

    Args:
        markdown: The page source.

    Returns:
        The page with every marker expanded.
    """
    spec, scopes = _load_api()
    markdown = MAINTAINER_NOTE.sub("", markdown)
    return MARKER.sub(lambda m: render_endpoint(m.group(1), spec, scopes), markdown)


# ── MkDocs hook events ────────────────────────────────────────────────────────────────────

def on_page_markdown(markdown: str, page: Any, **_: Any) -> str:
    """Expand the markers on the export API page; leave every other page alone.

    Args:
        markdown: The page source.
        page: The MkDocs page.

    Returns:
        The page source, expanded if it is the export API page.
    """
    if page.file.src_uri != PAGE:
        return markdown
    _STATE["standalone"] = expand(markdown)
    return _STATE["standalone"]


def on_post_build(config: Any, **_: Any) -> None:
    """Write the expanded export API page as one standalone Markdown file in the site.

    Args:
        config: The MkDocs config.
    """
    if "standalone" in _STATE:
        out = Path(config["site_dir"]) / STANDALONE
        out.write_text(_STATE["standalone"], encoding="utf-8")


if __name__ == "__main__":
    sys.stdout.write(expand((ROOT / "docs" / PAGE).read_text(encoding="utf-8")))

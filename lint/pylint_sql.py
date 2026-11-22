"""Pylint plugin: ``dynamic-sql``, SQL text assembled at runtime.

Every SQL statement in this codebase is a complete literal, and values reach it only as bound
parameters. Table and column names can't be parameters, so code that needs a choice picks
between whole literal statements (a dict or tuple of them, or a ``CASE WHEN ?`` flag) instead
of building one. This checker flags anything that builds SQL text:

* an f-string with interpolations whose literal parts contain SQL
* ``+`` or ``%`` where one side is SQL text and the other isn't a literal
* ``.format(...)`` called on SQL text

Joining two literals (``"SELECT a " "FROM t"`` or ``"SELECT a " + "FROM t"``) is still static and
isn't flagged. SQL is recognised by structure, case-sensitively (``SELECT … FROM``, ``INSERT
INTO``, ``UPDATE x SET``, ``DELETE FROM``, ``WHERE``, ``VALUES (``, ...), which is how this
codebase writes it, so English prose in messages doesn't trip it.

Bandit's B608 covers similar ground but missed four of the original 33 sites, so this one is in
.pylintrc's blocking tier. Its tests: lint/test_pylint_sql.py.
"""
from __future__ import annotations

import re

from astroid import nodes
from pylint.checkers import BaseChecker
from pylint.checkers.utils import safe_infer
from pylint.lint import PyLinter

SQL = re.compile(
    r"\bSELECT\b[\s\S]*\bFROM\b"
    r"|\bINSERT\s+(?:OR\s+\w+\s+)?INTO\b"
    r"|\bUPDATE\s+\w+\s+SET\b"
    r"|\bDELETE\s+FROM\b"
    r"|\bWHERE\b"
    r"|\bVALUES\s*\("
    r"|\bON\s+CONFLICT\b"
    r"|\bORDER\s+BY\b"
    r"|\bINTERVAL\s+'"
)


def _literal_text(node: nodes.NodeNG) -> str | None:
    """The text of a string literal, or of a chain of ``+``-joined string literals."""
    if isinstance(node, nodes.Const) and isinstance(node.value, str):
        return node.value
    if isinstance(node, nodes.BinOp) and node.op == "+":
        left, right = _literal_text(node.left), _literal_text(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _text_parts(node: nodes.NodeNG) -> list[str]:
    """Every string the expression is built from: its literals, plus names and attributes that
    infer to a string constant (so ``_COLS + "FROM users"`` sees the SQL in ``_COLS``)."""
    parts = []
    # One walk, in source order, so the pieces join up the way the SQL would.
    for part in node.nodes_of_class((nodes.Const, nodes.Name, nodes.Attribute)):
        value = part if isinstance(part, nodes.Const) else safe_infer(part)
        if isinstance(value, nodes.Const) and isinstance(value.value, str):
            parts.append(value.value)
    return parts


class DynamicSqlChecker(BaseChecker):
    """Flags SQL text built at runtime instead of written as a literal."""

    name = "dynamic-sql"
    msgs = {
        "W9601": (
            "SQL text built at runtime (%s); write the statement as a literal and bind values "
            "with ? parameters",
            "dynamic-sql",
            "Every SQL statement must be a complete literal. Values go in as bound parameters; "
            "choosing a table or column means choosing between whole literal statements.",
        ),
    }

    def visit_joinedstr(self, node: nodes.JoinedStr) -> None:
        """Flag an interpolating f-string whose literal parts are SQL."""
        if not any(isinstance(v, nodes.FormattedValue) for v in node.values):
            return
        text = "".join(v.value for v in node.values
                       if isinstance(v, nodes.Const) and isinstance(v.value, str))
        if SQL.search(text):
            self.add_message("dynamic-sql", node=node, args=("f-string",))

    def visit_binop(self, node: nodes.BinOp) -> None:
        """Flag ``+`` / ``%`` that mixes SQL text with anything but another literal."""
        if node.op not in ("+", "%"):
            return
        # Report a chain once, at its outermost node.
        if isinstance(node.parent, nodes.BinOp) and node.parent.op == node.op:
            return
        if node.op == "+" and _literal_text(node) is not None:
            return                                       # literal + literal: static
        if node.op == "%" and _literal_text(node.right) is not None:
            return                                       # "..." % "literal": static
        if SQL.search("".join(_text_parts(node))):
            self.add_message("dynamic-sql", node=node,
                             args=("concatenation" if node.op == "+" else "% formatting",))

    def visit_call(self, node: nodes.Call) -> None:
        """Flag ``"<sql>".format(...)``."""
        func = node.func
        if isinstance(func, nodes.Attribute) and func.attrname == "format":
            text = _literal_text(func.expr)
            if text is not None and SQL.search(text):
                self.add_message("dynamic-sql", node=node, args=(".format()",))


def register(linter: PyLinter) -> None:
    """Pylint's plugin entry point."""
    linter.register_checker(DynamicSqlChecker(linter))

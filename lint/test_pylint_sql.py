"""Tests for the dynamic-sql pylint plugin (lint/pylint_sql.py). Run by ./run_lint.sh.

    python -m pytest lint/ -q
"""
import os
import sys

import astroid
import pytest
from pylint.testutils import CheckerTestCase

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pylint_sql  # noqa: E402  # pylint: disable=wrong-import-position


FLAGGED = {
    "f-string": 'table = "t"\nq = f"SELECT a FROM {table}"',
    "f-string value": 'n = 5\nq = f"... + INTERVAL \'{n} hours\'"',
    "f-string fragment": 'live = "x"\nq = f"WHERE {live}"',
    "concat with a name": 'COLS = "SELECT a "\nq = COLS + "FROM users"',
    "concat with a call": 'q = "SELECT a FROM t WHERE " + " AND ".join(["x"])',
    "conditional concat": 'r = True\nq = "UPDATE v SET a = 1" + (", b = 2" if r else "")',
    "percent": 'c = "a"\nq = "SELECT %s FROM t" % c',
    "format": 'q = "SELECT {} FROM t".format("a")',
    "insert values": 'n = 3\nq = f"INSERT INTO t (a) VALUES ({n})"',
    "delete": 'table = "t"\nq = "DELETE FROM " + table',
}

CLEAN = {
    "literal": 'q = "SELECT a FROM t WHERE b = ?"',
    "implicit join": 'q = ("SELECT a "\n     "FROM t WHERE b = ?")',
    "literal + literal": 'q = "SELECT a " + "FROM t"',
    "message f-string": 'n = 2\nmsg = f"Imported {n} volunteers (where possible)"',
    "lowercase prose": 'x = 1\nmsg = f"select a slot from the list {x}"',
    "non-sql format": 'msg = "Row {}: skipped".format(3)',
    "non-sql concat": 'name = "x"\nmsg = "Made inactive " + name',
    "number maths": 'a = 1\nb = a + 2',
}


class TestDynamicSql(CheckerTestCase):
    """One flagged message per building site; nothing for static or non-SQL text."""

    CHECKER_CLASS = pylint_sql.DynamicSqlChecker

    def _messages(self, code):
        self.checker.linter.release_messages()
        module = astroid.parse(code)
        self.walk(module)
        return [m.msg_id for m in self.linter.release_messages()]

    @pytest.mark.parametrize("code", FLAGGED.values(), ids=FLAGGED.keys())
    def test_flagged(self, code):
        assert self._messages(code) == ["dynamic-sql"]

    @pytest.mark.parametrize("code", CLEAN.values(), ids=CLEAN.keys())
    def test_clean(self, code):
        assert not self._messages(code)

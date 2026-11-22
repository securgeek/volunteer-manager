"""Test setup for the portal suite: put this directory on sys.path.

The check that stops this suite sharing a process with the scheduler's lives in the
repository-root conftest.py — it has to run after collection, which is later than any conftest
import.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

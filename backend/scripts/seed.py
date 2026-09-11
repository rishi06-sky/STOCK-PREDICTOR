#!/usr/bin/env python
"""Seed reference data. Idempotent -- safe to run repeatedly."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database.seed import seed_reference_data
from app.database.session import session_scope

if __name__ == "__main__":
    with session_scope() as db:
        counts = seed_reference_data(db)
    print("seeded:", counts)

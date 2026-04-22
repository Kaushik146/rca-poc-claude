"""
Regression test for the INC-DEMO-001 off-by-one bug.

This test exists so the `fix-and-test` agent has a concrete failing test to
target when scripts/seed_bug.py has seeded the bug. When the bug is NOT
seeded, the test passes — i.e. the current code is correct.

Run:
  pytest python-inventory-service/tests/test_reserve_boundary.py -q
"""
from __future__ import annotations
import sys
from pathlib import Path

import pytest

THIS = Path(__file__).resolve()
SERVICE_DIR = THIS.parent.parent
sys.path.insert(0, str(SERVICE_DIR))

from app import app, _stock, _stock_lock  # noqa: E402


@pytest.fixture
def client():
    # Reset stock to known state for each test
    with _stock_lock:
        _stock.clear()
        _stock.update({
            "SKU-001": 100,
            "SKU-002": 5,
            "SKU-003": 1,       # last-unit case
            "SKU-NOSTOCK": 0,
        })
    app.testing = True
    return app.test_client()


def test_reserve_last_unit_is_allowed(client):
    """
    INC-DEMO-001: when stock == quantity == 1, the reserve must succeed.
    With the off-by-one bug (stock > quantity), this returns reserved=false
    and this test fails.
    """
    resp = client.post(
        "/reserve",
        json={"product_id": "SKU-003", "quantity": 1},
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 200, resp.data
    body = resp.get_json()
    assert body["reserved"] is True, (
        "last-unit reserve returned false — off-by-one in reserve() boundary check"
    )


def test_reserve_exact_remaining_allowed(client):
    """Reserving exactly the remaining count must succeed (same invariant)."""
    resp = client.post(
        "/reserve",
        json={"product_id": "SKU-002", "quantity": 5},
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 200, resp.data
    assert resp.get_json()["reserved"] is True


def test_reserve_over_stock_refused(client):
    """Oversell must still be refused — the fix must not over-correct. The
    service returns HTTP 409 (conflict) when the reserve cannot be filled."""
    resp = client.post(
        "/reserve",
        json={"product_id": "SKU-002", "quantity": 6},
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 409, resp.data
    assert resp.get_json()["reserved"] is False

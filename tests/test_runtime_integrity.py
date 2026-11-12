from src.db_writer import _availability_state, _payload_captured_at
from src.price_decision import _status_factors


def test_ota_row_requires_explicit_available_signal() -> None:
    assert _availability_state({"room_name": "大床房", "price": 300}) is None
    assert _availability_state({"room_name": "大床房", "price": 300, "status": "success", "available": True}) is True
    assert _availability_state({"room_name": "大床房", "price": 300, "status": "success", "available": "False; remain=9999"}) is False
    assert _availability_state({"room_name": "大床房", "price": 300, "status": "unknown", "available": True}) is None
    assert _availability_state({"room_name": "大床房", "status": "售罄"}) is False


def test_payload_captured_at_requires_timezone() -> None:
    assert _payload_captured_at({"captured_at": "2026-07-16T09:00:00+08:00"}).isoformat() == "2026-07-16T09:00:00+08:00"


def test_inventory_estimate_is_not_treated_as_actual_sales() -> None:
    factors = _status_factors(
        {
            "room_total": 10,
            "occupied_rooms": 8,
            "sold_rooms": 6,
            "available_rooms": 4,
            "occupancy_rate": 0.8,
            "raw": {
                "sales_rate_source": "inventory_estimate_not_order_api",
                "summary": {"actual_sold_rooms": 6, "actual_sales_rate": 0.6},
            },
        }
    )
    assert factors["actual_sales_rate"] is None
    assert factors["actual_sold_rooms"] is None
    assert factors["inventory_occupied_rate"] == 0.6

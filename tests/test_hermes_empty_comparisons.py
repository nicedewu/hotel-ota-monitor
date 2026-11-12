import json

from src.config import HotelConfig
from src.hermes import build_hermes


def _write_capture(directory) -> None:
    directory.mkdir()
    (directory / "ctrip_rooms.json").write_text(
        json.dumps({"metadata": {"page_status": "success"}, "records": []}),
        encoding="utf-8",
    )


def test_build_hermes_accepts_empty_formal_comparisons(tmp_path) -> None:
    own_dir = tmp_path / "own"
    competitor_dir = tmp_path / "competitor"
    output_dir = tmp_path / "report"
    _write_capture(own_dir)
    _write_capture(competitor_dir)

    result = build_hermes(
        "unused.json",
        str(output_dir),
        hotels=[
            HotelConfig("own", "own", "自有酒店", "", str(own_dir)),
            HotelConfig("competitor", "competitor", "竞品酒店", "", str(competitor_dir)),
        ],
        run_id="empty-comparisons-run",
        price_comparisons={
            "comparisons": [],
            "summary_by_room_type": [
                {"variant_key": "esports_double|bathtub=true"}
            ],
        },
    )

    payload = json.loads((output_dir / "hermes_payload.json").read_text(encoding="utf-8"))
    assert result["price_compare_count"] == 0
    assert result["comparison_variant_keys"] == ["esports_double|bathtub=true"]
    assert payload["price_compare"] == []
    assert payload["business_price_compare"]["comparisons"] == []

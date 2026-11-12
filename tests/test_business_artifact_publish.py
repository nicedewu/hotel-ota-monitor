import json

import pytest

from src.multi_hotel import DBWriterError, _publish_business_batch


def _payload() -> dict:
    return {
        "comparison_batch_id": "batch-1",
        "source_run_id": "run-1",
        "comparison_version": "multi-hotel-room-variant-v1",
        "generated_at": "2026-07-25T00:00:00+08:00",
        "summary_by_room_type": [{"variant_key": "esports_double|bathtub=true"}],
    }


def _write_artifacts(root, payload, *, summary_keys=None) -> None:
    (root / "price_compare.json").write_text(json.dumps(payload), encoding="utf-8")
    (root / "hermes_payload.json").write_text(json.dumps({key: payload[key] for key in ("comparison_batch_id", "source_run_id", "comparison_version")}), encoding="utf-8")
    (root / "run_summary.json").write_text(json.dumps({
        **{key: payload[key] for key in ("comparison_batch_id", "source_run_id", "comparison_version")},
        "comparison_variant_keys": summary_keys if summary_keys is not None else ["esports_double|bathtub=true"],
    }), encoding="utf-8")


def test_publish_business_batch_writes_manifest_only_after_consistent_artifacts(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    payload = _payload()
    _write_artifacts(source, payload)

    published = _publish_business_batch(source, payload)

    manifest = json.loads((tmp_path / "output/latest_business_manifest.json").read_text(encoding="utf-8"))
    assert published.name == "batch-1"
    assert manifest["directory"].endswith("output/business_runs/batch-1")


def test_publish_business_batch_rejects_variant_mismatch_without_manifest(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    payload = _payload()
    _write_artifacts(source, payload, summary_keys=[])

    with pytest.raises(DBWriterError, match="variant_key"):
        _publish_business_batch(source, payload)

    assert not (tmp_path / "output/latest_business_manifest.json").exists()

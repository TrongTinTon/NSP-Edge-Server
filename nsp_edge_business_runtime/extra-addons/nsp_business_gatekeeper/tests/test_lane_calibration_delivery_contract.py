from pathlib import Path


def _root():
    return Path(__file__).resolve().parents[1]


def test_lane_calibration_datetime_comparison_is_string_safe():
    source = (_root() / "models" / "api_service.py").read_text(encoding="utf-8")
    assert 'fields.Datetime.to_string(values["read_at"])' not in source
    assert 'fields.Datetime.to_string(first["read_at"])' not in source
    assert 'fields.Datetime.to_datetime(values["read_at"])' in source
    assert 'first["read_at"] == values["read_at"]' in source


def test_controller_lane_calibration_event_response_is_transport_ack_only():
    source = (_root() / "models" / "api_service.py").read_text(encoding="utf-8")
    start = source.index("def api_controller_lane_calibration_events")
    end = source.index("def api_controller_lane_calibration_status", start)
    block = source[start:end]
    assert '"acknowledged": True' in block
    assert '"stored": int(result.get(' not in block


def test_cloud_ignored_is_not_recorded_as_synced():
    source = (_root() / "models" / "sync_business_adapter.py").read_text(encoding="utf-8")
    assert 'ignored = remote_status == "ignored"' in source
    assert '"skipped" if ignored else "synced"' in source
    assert '("status", "in", ("synced", "skipped"))' in source


def test_lane_calibration_api_imports_raw_tid_normalizer():
    source = (_root() / "models" / "api_service.py").read_text(encoding="utf-8")
    assert "from ..services.raw_rfid_tag import normalize_raw_tid" in source
    assert "incoming_tid = normalize_raw_tid(item.get(\"tid\"))" in source



def test_lane_calibration_reader_serial_uses_shared_controller_compatible_normalizer():
    api = (_root() / "models" / "api_service.py").read_text(encoding="utf-8")
    measurement = (_root() / "models" / "measurement.py").read_text(encoding="utf-8")
    helper = (_root() / "services" / "raw_rfid_tag.py").read_text(encoding="utf-8")
    assert "def normalize_reader_serial" in helper
    assert "serial_number = normalize_reader_serial(item.get(\"serial_number\"))" in api
    assert "normalize_reader_serial(node.reader_id.serial_number): node" in api
    assert "normalize_reader_serial(node.reader_id.serial_number)" in measurement


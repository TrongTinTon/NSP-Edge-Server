# -*- coding: utf-8 -*-
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / 'models/parking_detection_event.py').read_text(encoding='utf-8')


def test_ingest_reports_created_logs_and_deferred_processing():
    assert '"parking_logs_created": 0' in SOURCE
    assert '"processing_deferred_lanes": 0' in SOURCE
    assert 'stats["parking_logs_created"] += len(created_logs)' in SOURCE
    assert 'stats["processing_deferred_lanes"] += 1' in SOURCE


def test_only_validation_failures_are_terminalized():
    assert 'except ValidationError:' in SOURCE
    assert 'Parking Antenna Sequence validation failed' in SOURCE
    assert 'failed_vehicle_events.write({"error_code": "processing_error"})' in SOURCE


def test_unexpected_runtime_failure_is_retried_not_dead_lettered():
    marker = 'Parking Antenna Sequence runtime failure deferred for retry'
    assert marker in SOURCE
    tail = SOURCE[SOURCE.index(marker): SOURCE.index(marker) + 700]
    assert 'raise' in tail
    assert 'write({"error_code": "processing_error"})' not in tail


def test_cron_isolates_lane_failures():
    assert 'Parking Detection processor deferred Lane after runtime failure' in SOURCE
    assert 'with self.env.cr.savepoint()' in SOURCE

from pathlib import Path


def _model_source():
    root = Path(__file__).resolve().parents[1]
    return (root / "models" / "parking_detection_event.py").read_text(encoding="utf-8")


def test_unknown_tid_is_dropped_before_topology_resolution_and_persistence():
    model = _model_source()

    prefilter = model.index("known_detections = []")
    topology = model.index("self._resolve_topology_batch(", prefilter)
    candidate_batch = model.index("self.create_idempotent_batch(\n            candidate_values", topology)
    error_batch = model.index("self.create_idempotent_batch(error_values)", candidate_batch)

    assert prefilter < topology < candidate_batch < error_batch
    assert "if not assignment or assignment.tid != tid:" in model[prefilter:topology]
    assert "ignored_unknown_tid_detections += 1" in model[prefilter:topology]
    assert "known_detections.append((payload, assignment))" in model[prefilter:topology]
    assert "controller, known_detections" in model[topology:topology + 300]
    assert '"ignored_unknown_tid_detections": ignored_unknown_tid_detections' in model


def test_stale_cleanup_is_lane_candidate_scoped_and_uses_that_lane_window():
    model = _model_source()
    start = model.index("def _drop_expired_pending")
    end = model.index("def _expire_orphan_user_events", start)
    body = model[start:end]

    assert "cutoff = now - timedelta(seconds=lane.max_sequence_window())" in body
    assert "AND layout_lane_id = %s" in body
    assert "(lane.id, cutoff)" in body
    # Expiring one Lane candidate must not remove sibling copies of the same
    # physical detection that are still alive under another Lane's larger window.
    assert "event_uid" not in body


def test_unknown_tid_rule_does_not_hide_known_assignment_configuration_errors():
    model = _model_source()
    # These remain useful diagnostics because the TID exists in runtime assignment
    # but its target mapping is structurally invalid or inactive.
    assert 'return "invalid_rfid_assignment"' in model
    assert 'return "rfid_assignment_target_inactive"' in model

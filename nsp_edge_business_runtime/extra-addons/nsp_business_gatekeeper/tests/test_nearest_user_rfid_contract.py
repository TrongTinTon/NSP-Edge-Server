from pathlib import Path


def _root():
    return Path(__file__).resolve().parents[1]


def test_checkout_selects_only_nearest_user_read_to_vehicle_completion():
    root = _root()
    detection = (root / "models" / "parking_detection_event.py").read_text(encoding="utf-8")
    business = (root / "models" / "parking_log_business.py").read_text(encoding="utf-8")

    assert "nearest_user_event = candidates[:1]" in detection
    assert "matched_user_events = nearest_user_event" in detection
    assert "abs((event.detected_at - anchor_at).total_seconds())" in detection
    assert "abs((rec.detected_at - event_time).total_seconds())" not in business
    assert "user_event = user_events[:1]" in business
    assert "invalid_supporting_user_detection_count" in business


def test_multiple_user_tags_is_not_emitted_or_exposed_by_runtime_ui():
    root = _root()
    active_runtime_files = [
        root / "models" / "parking_log_business.py",
        root / "models" / "parking_log_live.py",
        root / "models" / "parking_detection_event.py",
        root / "views" / "parking_log_views.xml",
    ]
    for path in active_runtime_files:
        assert "multiple_user_tags" not in path.read_text(encoding="utf-8"), path

    # Keep the old selection value only so pre-10.46 immutable Parking Logs remain
    # readable after upgrade. No new business path may emit it.
    parking_log = (root / "models" / "parking_log.py").read_text(encoding="utf-8")
    assert '("multiple_user_tags", "Multiple User RFID Tags (Legacy)")' in parking_log


def test_any_present_user_resolves_checkout_immediately_without_authorization_wait():
    root = _root()
    detection = (root / "models" / "parking_detection_event.py").read_text(encoding="utf-8")
    business = (root / "models" / "parking_log_business.py").read_text(encoding="utf-8")

    start = detection.index("nearest_user_event = candidates[:1]")
    end = detection.index("try:", start)
    selection_block = detection[start:end]

    assert "nearest_is_authorized" not in selection_block
    assert "if not nearest_user_event:" in selection_block
    assert "if not deadline_reached:" in selection_block
    assert "blocked_tids.add(tid)" in selection_block
    assert "matched_user_events = nearest_user_event" in selection_block
    assert "_authorized_user_borrow_map" not in selection_block
    assert "authorized, borrow = self._resolve_user_authorization(" in business


def test_only_missing_user_waits_until_lane_deadline():
    root = _root()
    detection = (root / "models" / "parking_detection_event.py").read_text(encoding="utf-8")

    start = detection.index("nearest_user_event = candidates[:1]")
    end = detection.index("try:", start)
    selection_block = detection[start:end]

    assert "if not nearest_user_event:" in selection_block
    assert "if not deadline_reached:" in selection_block
    assert "continue" in selection_block
    assert "not nearest_is_authorized" not in selection_block


def test_selected_user_repeated_reads_are_consumed_but_other_users_are_not():
    root = _root()
    detection = (root / "models" / "parking_detection_event.py").read_text(encoding="utf-8")

    assert "consume_user_events = self.browse()" in detection
    assert "consume_user_events = candidates.filtered(" in detection
    assert "event.user_id.id == selected_user.id" in detection
    assert "consume_user_events=consume_user_events" in detection
    assert "group | (consume_user_events or self.browse())" in detection
    assert "consumed_user_ids.update(consume_user_events.ids)" in detection


def test_rssi_is_not_used_for_user_selection():
    root = _root()
    detection = (root / "models" / "parking_detection_event.py").read_text(encoding="utf-8")

    user_selection_start = detection.index("def _user_candidates_from_pool")
    user_selection_end = detection.index("def _drop_expired_pending", user_selection_start)
    user_selection = detection[user_selection_start:user_selection_end]

    assert "rssi" not in user_selection.lower()
    assert "abs((event.detected_at - anchor_at).total_seconds())" in user_selection

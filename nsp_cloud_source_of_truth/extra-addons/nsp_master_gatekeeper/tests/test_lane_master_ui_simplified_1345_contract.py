# -*- coding: utf-8 -*-
from pathlib import Path


def test_lane_master_ui_is_identity_only():
    xml = (Path(__file__).parents[1] / "views" / "parking_lane_views.xml").read_text()
    block = xml.split('id="view_nsp_parking_lane_list"', 1)[1].split('id="action_nsp_parking_lane"', 1)[0]
    assert 'name="code"' not in block
    assert 'name="layout_count"' not in block
    assert 'name="layout_lane_ids"' not in block
    assert 'Parking Layout References' not in block
    assert 'alert alert-info' not in block
    assert 'name="name"' in block
    assert 'name="branch_id"' in block
    assert 'name="active"' in block

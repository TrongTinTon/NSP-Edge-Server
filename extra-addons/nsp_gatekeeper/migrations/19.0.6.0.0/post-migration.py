# -*- coding: utf-8 -*-
from odoo import SUPERUSER_ID, api


def _table_exists(cr, table_name):
    cr.execute("SELECT to_regclass(%s)", ("public.%s" % table_name,))
    return bool(cr.fetchone()[0])


def _column_exists(cr, table_name, column_name):
    cr.execute("""
        SELECT 1
          FROM information_schema.columns
         WHERE table_schema = 'public'
           AND table_name = %s
           AND column_name = %s
    """, (table_name, column_name))
    return bool(cr.fetchone())


def _drop_columns(cr, table_name, columns):
    if not _table_exists(cr, table_name):
        return
    for column in columns:
        if _column_exists(cr, table_name, column):
            cr.execute('ALTER TABLE "%s" DROP COLUMN "%s" CASCADE' % (table_name, column))


def _cleanup_legacy_schema(cr):
    # These fields belonged to the previous analysis/review model and are not
    # part of the operational Measurement Session contract.
    _drop_columns(cr, 'nsp_gate_measurement_session', [
        'name', 'measurement_uid', 'payload_hash', 'measurement_source',
        'operator_name', 'operator_note', 'report_received_at', 'admin_review_note',
        'gate_code', 'branch_id', 'lane_code', 'lane_direction', 'direction', 'state',
        'ended_at', 'notes', 'analysis_max_gap_ms', 'safety_margin_ms',
        'transition_count', 'tid_count', 'antenna_count',
        'min_delta_ms', 'avg_delta_ms', 'max_delta_ms', 'p95_delta_ms',
        'recommended_detection_window_ms', 'recommended_sequence_required',
        'recommended_required_antenna_count', 'recommended_sequence',
        'recommendation_note',
    ])
    _drop_columns(cr, 'nsp_gate_measurement_event', [
        'gate_id', 'lane_id', 'controller_id', 'device_id', 'device_serial',
        'antenna_id', 'antenna_ref_id', 'lane_rule_id', 'effective_direction',
        'read_time', 'sequence', 'rssi', 'phase', 'frequency_mhz', 'sample_uid',
        'tid_type',
    ])
    _drop_columns(cr, 'nsp_gate_measurement_pair_summary', [
        'gate_id', 'lane_id', 'controller_id', 'from_antenna_id', 'to_antenna_id',
        'antenna_pair', 'tid_count', 'min_delta_ms', 'avg_delta_ms', 'max_delta_ms',
        'p95_delta_ms', 'p95_delta_sec', 'avg_delta_sec',
        'min_rssi', 'avg_rssi', 'max_rssi',
    ])
    cr.execute('DROP TABLE IF EXISTS nsp_gate_measurement_transition CASCADE')
    cr.execute('DROP TABLE IF EXISTS nsp_gate_measurement_controller_rel CASCADE')


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    Session = env['nsp.gate.measurement.session'].sudo()
    Mapping = env['nsp.gate.measurement.antenna'].sudo()
    Antenna = env['nsp.device.antenna'].sudo()

    # Rebuild the canonical Measurement Antenna list from migrated events.
    for session in Session.search([]):
        if session.antenna_ids:
            continue
        seen = set()
        vals = []
        for event in session.event_ids:
            key = (event.serial_number, event.antenna_no)
            if key in seen:
                continue
            seen.add(key)
            antenna = Antenna.search([
                ('device_id.controller_id', '=', session.controller_id.id),
                ('device_id.serial_number', '=', event.serial_number),
                ('antenna_id', '=', event.antenna_no),
            ], limit=1)
            if antenna:
                vals.append({'session_id': session.id, 'antenna_ref_id': antenna.id})
        if vals:
            Mapping.with_context(measurement_sync=True).create(vals)

    # Completed legacy sessions retain compact summaries after event cleanup.
    completed = Session.search([
        ('measurement_status', '=', 'completed'),
        ('event_count', '>', 0),
    ])
    completed._build_summaries()

    _cleanup_legacy_schema(cr)

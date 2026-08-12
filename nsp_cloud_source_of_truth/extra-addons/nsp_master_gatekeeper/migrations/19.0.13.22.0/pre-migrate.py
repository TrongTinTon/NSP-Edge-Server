# -*- coding: utf-8 -*-
"""Prepare the Lane master table before ORM makes branch_id required."""


def migrate(cr, version):
    cr.execute("SELECT to_regclass('public.nsp_parking_lane')")
    if not cr.fetchone()[0]:
        return
    cr.execute("ALTER TABLE nsp_parking_lane ADD COLUMN IF NOT EXISTS branch_id INTEGER")
    cr.execute("SELECT to_regclass('public.nsp_parking_area')")
    if not cr.fetchone()[0]:
        return
    cr.execute("""
        SELECT 1 FROM information_schema.columns
         WHERE table_schema='public' AND table_name='nsp_parking_lane'
           AND column_name='parking_area_id'
    """)
    if not cr.fetchone():
        return
    cr.execute("""
        UPDATE nsp_parking_lane lane
           SET branch_id = area.branch_id
          FROM nsp_parking_area area
         WHERE lane.branch_id IS NULL
           AND lane.parking_area_id = area.id
    """)

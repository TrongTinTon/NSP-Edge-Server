# -*- coding: utf-8 -*-

def migrate(cr, version):
    cr.execute("DROP INDEX IF EXISTS core_api_application_kind_idx")
    cr.execute("ALTER TABLE core_api_application DROP COLUMN IF EXISTS application_kind")

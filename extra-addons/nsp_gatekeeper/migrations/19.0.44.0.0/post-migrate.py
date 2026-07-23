# -*- coding: utf-8 -*-

def migrate(cr, version):
    cr.execute("""
        UPDATE nsp_device
           SET name = COALESCE(NULLIF(serial_number, ''), 'RFID Reader')
         WHERE name IS NULL OR BTRIM(name) = '' OR name = 'RFID Reader'
    """)

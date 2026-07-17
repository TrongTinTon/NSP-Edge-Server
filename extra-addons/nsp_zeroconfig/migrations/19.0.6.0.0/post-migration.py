# -*- coding: utf-8 -*-


def migrate(cr, version):
    # Zeroconfig now exposes only Service Type, Discovery Secret Key and Odoo HTTP Port.
    cr.execute("""
        DELETE FROM ir_config_parameter
         WHERE key IN (
            'nsp_zeroconfig.service_name',
            'nsp_zeroconfig.advertised_ip',
            'nsp_zeroconfig.scheme'
         )
    """)
    cr.execute("""
        INSERT INTO ir_config_parameter(key, value, create_uid, create_date, write_uid, write_date)
        SELECT 'nsp_zeroconfig.service_type', '_nsp._tcp.local.', 1, NOW(), 1, NOW()
         WHERE NOT EXISTS (
            SELECT 1 FROM ir_config_parameter WHERE key='nsp_zeroconfig.service_type'
         )
    """)

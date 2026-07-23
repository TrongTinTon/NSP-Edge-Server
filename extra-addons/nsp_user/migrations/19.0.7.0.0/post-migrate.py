# -*- coding: utf-8 -*-


def migrate(cr, version):
    """Cancelled relationships are no longer retained; absence means no friendship."""
    cr.execute("DELETE FROM nsp_user_friendship WHERE state = 'cancelled'")

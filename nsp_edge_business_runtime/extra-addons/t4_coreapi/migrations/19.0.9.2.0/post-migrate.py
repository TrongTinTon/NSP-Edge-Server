# Part of T4 Core API. See LICENSE file for full copyright and licensing details.


def migrate(cr, version):
    # Rate limiting was removed entirely. Drop the obsolete per-application column
    # and stale authentication throttle parameter during module upgrade.
    cr.execute("ALTER TABLE core_api_application DROP COLUMN IF EXISTS rate_limit_per_minute")
    cr.execute("DELETE FROM ir_config_parameter WHERE key = 't4_coreapi.auth_rate_limit_per_ip'")

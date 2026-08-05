# T4 Custom Dashboard

Reusable Odoo 19 dashboard engine used by NSP.

## Design rules

- Dashboard engine contains no NSP business logic.
- Runtime data sources are Python methods only; direct SQL configured from the UI is disabled.
- `stat`, `stat_action`, `chart`, and `kanban_embed` widgets are supported.
- Role-bound dashboards are read-only for normal dashboard users.
- Business modules such as `nsp_it_dashboard` provide Python methods and dashboard records.

## Security

Access requires `T4 Custom Dashboard / Dashboard User` or Settings Administrator.
Dashboard Managers may maintain role-bound dashboard definitions.

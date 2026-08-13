# Source cleanup 19.0.10.38.0

- Removed Timing Tolerance from Parking Layout runtime and sync contracts. Transition timing is only `duration_from_previous` / Max Duration.
- Removed redundant `runtime_state.py` extension because `nsp.device` already owns `active` and `cloud_removed`.
- Removed derived Antenna Sequence storage (`cumulative_time`) and unused `is_first_point` presentation field.
- Removed duplicated `layout_lane_id` and `reader_id` columns from Reader Port; ownership is derived through Reader Configuration.
- Replaced multiple Sequence standalone indexes with one Reader/Port-first topology index while unique constraints continue to serve Lane-first access.
- Restored a partial Detection index on `parking_log_id IS NOT NULL` for reverse source-detection lookups used by Parking Log sync/UI.
- Removed dead Parking Log reason-message construction and unnecessary Parking Area/Lane display compute fields.
- Removed unused Python imports, unused icon assets, and build/test cache artifacts.
- Removed orphaned Measurement live dashboard assets/actions and standalone Measurement Event action/view; Edge keeps the read-only synchronized Lane Calibration form and API/runtime state policy only.
- Removed unused Edge-side Parking Layout export/action helpers and dead API/Measurement compatibility helpers.
- Reduced Live Monitor bus payload to fields actually consumed by the frontend.
- Avoided rebuilding Detection partial indexes during model initialization; index shape changes are migration-owned.
- Removed obsolete Lane Calibration UI/count fields and their ORM compute queries after the old live dashboard was deleted.
- Removed deprecated Vehicle compatibility from the raw Calibration Tag model; Lane Calibration now stores only the raw TID probe.
- Removed unused Parking Area display/computed fields (`is_published`, `whitelist_count`, `configuration_summary`).
- Removed unused Reader Configuration source metadata (`source_type`, `source_revision`).

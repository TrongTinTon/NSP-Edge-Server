# -*- coding: utf-8 -*-
"""Tests cho controller._build_extra_domain + model._normalize_config.

Covers:
  - CustomDashboardController._build_extra_domain (all DOMAIN_BUILDERS types,
    appliesTo whitelist, filterOverrides, empty/null values, unknown type)
  - CustomDashboard._normalize_config (v1 list, v2 dict, invalid input)
"""
from odoo.tests import TransactionCase, tagged
from odoo.addons.t4_custom_dashboard.controllers.main import (
    CustomDashboardController,
    DOMAIN_BUILDERS,
)


@tagged('post_install', '-at_install', 't4_custom_dashboard', 't4_search_panel')
class TestBuildExtraDomain(TransactionCase):
    """Unit tests for _build_extra_domain without HTTP context.

    The method is a plain Python helper on the controller — it only needs
    the positional arguments and does not use `request`, so it is safe to
    call directly from a TransactionCase.
    """

    def setUp(self):
        super().setUp()
        self.controller = CustomDashboardController()

    # ------------------------------------------------------------------
    # Baseline: empty inputs
    # ------------------------------------------------------------------

    def test_empty_search_panel_returns_empty_domain(self):
        """No filter definitions → empty domain."""
        domain = self.controller._build_extra_domain('w1', {}, [], {})
        self.assertEqual(domain, [])

    def test_empty_filter_values_returns_empty_domain(self):
        """Filter def present but no values selected → empty domain."""
        config = [{'id': 'company', 'type': 'company', 'field': 'company_id', 'appliesTo': None}]
        domain = self.controller._build_extra_domain('w1', {}, config, {})
        self.assertEqual(domain, [])

    # ------------------------------------------------------------------
    # company filter
    # ------------------------------------------------------------------

    def test_company_filter_in(self):
        """company type builds (field, 'in', [values]) domain."""
        config = [{'id': 'company', 'type': 'company', 'field': 'company_id', 'appliesTo': None}]
        domain = self.controller._build_extra_domain('w1', {'company': [1, 2]}, config, {})
        self.assertIn(('company_id', 'in', [1, 2]), domain)

    def test_company_filter_single_value_wrapped_in_list(self):
        """Scalar company value is wrapped in list."""
        config = [{'id': 'company', 'type': 'company', 'field': 'company_id', 'appliesTo': None}]
        domain = self.controller._build_extra_domain('w1', {'company': 5}, config, {})
        self.assertIn(('company_id', 'in', [5]), domain)

    # ------------------------------------------------------------------
    # m2m_tree filter
    # ------------------------------------------------------------------

    def test_m2m_tree_uses_child_of(self):
        """m2m_tree type builds (field, 'child_of', ids) domain."""
        config = [{'id': 'location', 'type': 'm2m_tree', 'field': 'location_id', 'appliesTo': None}]
        domain = self.controller._build_extra_domain('w1', {'location': [5]}, config, {})
        self.assertIn(('location_id', 'child_of', [5]), domain)

    # ------------------------------------------------------------------
    # date_range filter
    # ------------------------------------------------------------------

    def test_date_range_builds_pair(self):
        """date_range with both from/to produces two domain clauses."""
        config = [{'id': 'date', 'type': 'date_range', 'field': 'date_order', 'appliesTo': None}]
        domain = self.controller._build_extra_domain(
            'w1',
            {'date': {'from': '2026-01-01', 'to': '2026-01-31'}},
            config,
            {},
        )
        self.assertEqual(len(domain), 2)
        self.assertIn(('date_order', '>=', '2026-01-01'), domain)
        self.assertIn(('date_order', '<=', '2026-01-31'), domain)

    def test_date_range_from_only(self):
        """date_range with only 'from' produces one clause."""
        config = [{'id': 'date', 'type': 'date_range', 'field': 'date_order', 'appliesTo': None}]
        domain = self.controller._build_extra_domain(
            'w1',
            {'date': {'from': '2026-01-01'}},
            config,
            {},
        )
        self.assertEqual(len(domain), 1)
        self.assertIn(('date_order', '>=', '2026-01-01'), domain)

    # ------------------------------------------------------------------
    # appliesTo whitelist
    # ------------------------------------------------------------------

    def test_applies_to_whitelist_skips_unrelated_widget(self):
        """Filter with appliesTo=['w1'] is NOT applied to widget 'w2'."""
        config = [{'id': 'location', 'type': 'm2m_tree', 'field': 'location_id', 'appliesTo': ['w1']}]
        domain = self.controller._build_extra_domain('w2', {'location': [1]}, config, {})
        self.assertEqual(domain, [])

    def test_applies_to_whitelist_includes_target_widget(self):
        """Filter with appliesTo=['w1'] IS applied when widget_id='w1'."""
        config = [{'id': 'location', 'type': 'm2m_tree', 'field': 'location_id', 'appliesTo': ['w1']}]
        domain = self.controller._build_extra_domain('w1', {'location': [1]}, config, {})
        self.assertEqual(len(domain), 1)

    def test_applies_to_null_means_all_widgets(self):
        """appliesTo=None applies the filter to every widget_id."""
        config = [{'id': 'location', 'type': 'm2m_tree', 'field': 'location_id', 'appliesTo': None}]
        domain = self.controller._build_extra_domain('any_widget_id', {'location': [1]}, config, {})
        self.assertEqual(len(domain), 1)

    # ------------------------------------------------------------------
    # filterOverrides
    # ------------------------------------------------------------------

    def test_filter_overrides_replace_default_field(self):
        """filterOverrides[id] replaces the field from the filter def."""
        config = [{'id': 'location', 'type': 'm2m_tree', 'field': 'location_id', 'appliesTo': None}]
        domain = self.controller._build_extra_domain(
            'w1', {'location': [1]}, config, {'location': 'location_dest_id'}
        )
        self.assertIn(('location_dest_id', 'child_of', [1]), domain)
        # Original field must NOT appear
        self.assertNotIn(('location_id', 'child_of', [1]), domain)

    # ------------------------------------------------------------------
    # field=None (pass-through filters like bucket)
    # ------------------------------------------------------------------

    def test_filter_with_null_field_skipped(self):
        """Bucket-style filter (field=None) passes value without building a domain clause."""
        config = [{'id': 'bucket', 'type': 'selection', 'field': None, 'appliesTo': None}]
        domain = self.controller._build_extra_domain('w1', {'bucket': 'week'}, config, {})
        self.assertEqual(domain, [])

    # ------------------------------------------------------------------
    # Empty / null values skipped
    # ------------------------------------------------------------------

    def test_empty_list_value_skipped(self):
        """Empty list value does not generate domain clause."""
        config = [{'id': 'company', 'type': 'company', 'field': 'company_id', 'appliesTo': None}]
        domain = self.controller._build_extra_domain('w1', {'company': []}, config, {})
        self.assertEqual(domain, [])

    def test_none_value_skipped(self):
        """None value does not generate domain clause."""
        config = [{'id': 'company', 'type': 'company', 'field': 'company_id', 'appliesTo': None}]
        domain = self.controller._build_extra_domain('w1', {'company': None}, config, {})
        self.assertEqual(domain, [])

    def test_empty_string_value_skipped(self):
        """Empty string value does not generate domain clause."""
        config = [{'id': 'company', 'type': 'company', 'field': 'company_id', 'appliesTo': None}]
        domain = self.controller._build_extra_domain('w1', {'company': ''}, config, {})
        self.assertEqual(domain, [])

    # ------------------------------------------------------------------
    # Unknown filter type
    # ------------------------------------------------------------------

    def test_unknown_filter_type_silently_skipped(self):
        """Filter type not in DOMAIN_BUILDERS registry is silently ignored."""
        config = [{'id': 'x', 'type': 'unknown_type_xyz', 'field': 'x_field', 'appliesTo': None}]
        domain = self.controller._build_extra_domain('w1', {'x': 'val'}, config, {})
        self.assertEqual(domain, [])

    # ------------------------------------------------------------------
    # DOMAIN_BUILDERS registry completeness
    # ------------------------------------------------------------------

    def test_domain_builders_has_expected_types(self):
        """DOMAIN_BUILDERS must contain the 6 known filter types."""
        expected = {'company', 'm2m', 'm2m_tree', 'date_range', 'selection', 'char'}
        self.assertTrue(expected.issubset(set(DOMAIN_BUILDERS.keys())))

    # ------------------------------------------------------------------
    # Multiple filters combined
    # ------------------------------------------------------------------

    def test_multiple_filters_combined(self):
        """Two filters produce two independent domain clauses."""
        config = [
            {'id': 'company', 'type': 'company', 'field': 'company_id', 'appliesTo': None},
            {'id': 'location', 'type': 'm2m_tree', 'field': 'location_id', 'appliesTo': None},
        ]
        domain = self.controller._build_extra_domain(
            'w1',
            {'company': [1], 'location': [5]},
            config,
            {},
        )
        self.assertEqual(len(domain), 2)


@tagged('post_install', '-at_install', 't4_custom_dashboard', 't4_search_panel')
class TestSchemaNormalize(TransactionCase):
    """Unit tests for CustomDashboard._normalize_config static method."""

    def setUp(self):
        super().setUp()
        self.dashboard = self.env['custom.dashboard']

    def test_v1_array_coerced_to_v2(self):
        """A plain list (v1) is wrapped into version=1 schema."""
        result = self.dashboard._normalize_config([{'id': 'w1'}])
        self.assertEqual(result['version'], 1)
        self.assertIsNone(result['searchPanel'])
        self.assertEqual(len(result['widgets']), 1)
        self.assertEqual(result['widgets'][0]['id'], 'w1')

    def test_v2_dict_preserved(self):
        """A v2 dict is returned as-is with its searchPanel intact."""
        result = self.dashboard._normalize_config({
            'version': 2,
            'searchPanel': [{'id': 'f1'}],
            'widgets': [{'id': 'w1'}],
        })
        self.assertEqual(result['version'], 2)
        self.assertEqual(len(result['searchPanel']), 1)
        self.assertEqual(len(result['widgets']), 1)

    def test_invalid_input_returns_empty(self):
        """Non-list, non-dict input returns safe empty schema."""
        result = self.dashboard._normalize_config('garbage')
        self.assertEqual(result['widgets'], [])
        self.assertIsNone(result['searchPanel'])

    def test_none_input_returns_empty(self):
        """None input returns safe empty schema."""
        result = self.dashboard._normalize_config(None)
        self.assertEqual(result['widgets'], [])
        self.assertIsNone(result['searchPanel'])

    def test_v2_missing_search_panel_defaults_none(self):
        """v2 dict missing 'searchPanel' key defaults to None."""
        result = self.dashboard._normalize_config({'version': 2, 'widgets': []})
        self.assertIsNone(result['searchPanel'])

    def test_v2_missing_widgets_defaults_empty_list(self):
        """v2 dict missing 'widgets' key defaults to empty list."""
        result = self.dashboard._normalize_config({
            'version': 2,
            'searchPanel': None,
        })
        self.assertEqual(result['widgets'], [])

    def test_v2_none_widgets_defaults_empty_list(self):
        """v2 dict with widgets=None coerces to empty list."""
        result = self.dashboard._normalize_config({
            'version': 2,
            'widgets': None,
            'searchPanel': None,
        })
        self.assertEqual(result['widgets'], [])

    def test_empty_list_v1_returns_empty_widgets(self):
        """Empty list (v1 with no widgets) wraps to empty widgets list."""
        result = self.dashboard._normalize_config([])
        self.assertEqual(result['version'], 1)
        self.assertEqual(result['widgets'], [])

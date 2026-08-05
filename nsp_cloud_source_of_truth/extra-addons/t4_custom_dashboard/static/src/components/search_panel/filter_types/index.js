/** @odoo-module **/

/**
 * Filter type registry for t4_custom_dashboard search panel.
 *
 * Each entry is registered by the individual filter component files.
 * This file just re-exports the registry category for convenience
 * and ensures all built-in filter types are bundled by importing them.
 */
import { registry } from "@web/core/registry";

// Side-effect imports: each file self-registers into filterRegistry
import "./company_filter";
import "./m2m_filter";
import "./m2m_tree_filter";
import "./date_range_filter";
import "./selection_filter";

export const filterRegistry = registry.category("t4_dashboard_filters");

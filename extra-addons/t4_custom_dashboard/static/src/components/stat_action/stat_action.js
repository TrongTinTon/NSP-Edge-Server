/** @odoo-module **/

import { Component, useState, onWillUpdateProps, onMounted } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { rpc } from "@web/core/network/rpc";

/**
 * Stat widget với drill-down action.
 *
 * Props (extended từ StatsSummary):
 *   title, icon, bgColor, textColor, value, originalValue, trend, trendValue
 *   action: { model, name, domain_method, domain_params, views, target, context }
 *   widgetId, filters, searchPanelConfig, dataSource (cho resolve domain)
 */
export class StatAction extends Component {
    static template = "t4_custom_dashboard.StatAction";
    static props = {
        "*": { optional: true },
    };

    setup() {
        this.actionService = useService("action");
        this.notification = useService("notification");

        this.state = useState({
            displayValue: this.props.value,
            originalValue: this.props.originalValue,
            isAnimating: false,
            isLoading: false,
        });

        this.previousValue = this._parseValue(this.props.value);

        onMounted(() => {
            this._animateValue(0, this.previousValue, 1000);
        });

        onWillUpdateProps((nextProps) => {
            if (nextProps.value !== this.props.value) {
                const oldValue = this._parseValue(this.props.value);
                const newValue = this._parseValue(nextProps.value);
                if (!isNaN(oldValue) && !isNaN(newValue)) {
                    this._animateValue(oldValue, newValue, 800);
                    this.previousValue = newValue;
                } else {
                    this.state.displayValue = nextProps.value;
                    this.state.originalValue = nextProps.originalValue;
                }
            } else if (nextProps.originalValue !== this.props.originalValue) {
                this.state.originalValue = nextProps.originalValue;
            }
        });
    }

    // ---------------------------------------------------------------
    // Click → drill-down
    // ---------------------------------------------------------------
    async onClick() {
        const action = this.props.action;
        if (!action || (!action.model && !action.action_xml_id)) {
            this.notification.add("Widget chưa cấu hình action drill-down", {
                type: "warning",
            });
            return;
        }
        this.state.isLoading = true;
        try {
            // Path A: dùng action xml_id có sẵn (giữ context HID/RFID + view t4_)
            if (action.action_xml_id) {
                const result = await rpc(
                    "/t4_custom_dashboard/resolve_drill_action",
                    {
                        action_xml_id: action.action_xml_id,
                        domain_method: action.domain_method || null,
                        domain_model: action.domain_model || null,
                        domain_params: action.domain_params || {},
                        filters: this.props.filters || {},
                        search_panel: this.props.searchPanelConfig || [],
                        widget_id: this.props.widgetId,
                        filter_overrides:
                            this.props.dataSource?.filterOverrides || {},
                        extra_context: action.context || null,
                    },
                );
                if (result.error) {
                    this.notification.add(`Lỗi: ${result.error}`, {
                        type: "danger",
                    });
                    return;
                }
                await this.actionService.doAction(result.action);
                return;
            }

            // Path B: build action từ raw fields (legacy / model+domain_method)
            let domain = action.domain || [];
            if (action.domain_method) {
                const result = await rpc(
                    "/t4_custom_dashboard/resolve_action_domain",
                    {
                        model: action.model,
                        domain_method: action.domain_method,
                        domain_model: action.domain_model || null,
                        domain_params: action.domain_params || {},
                        filters: this.props.filters || {},
                        search_panel: this.props.searchPanelConfig || [],
                        widget_id: this.props.widgetId,
                        filter_overrides:
                            this.props.dataSource?.filterOverrides || {},
                    },
                );
                if (result.error) {
                    this.notification.add(`Lỗi: ${result.error}`, {
                        type: "danger",
                    });
                    return;
                }
                domain = result.domain || [];
            }
            await this.actionService.doAction({
                type: "ir.actions.act_window",
                name: action.name || this.props.title,
                res_model: action.model,
                view_mode: action.view_mode || "list,form",
                views: action.views || [
                    [false, "list"],
                    [false, "form"],
                ],
                domain: domain,
                context: action.context || {},
                target: action.target || "current",
            });
        } catch (e) {
            this.notification.add(`Không mở được drill-down: ${e.message}`, {
                type: "danger",
            });
        } finally {
            this.state.isLoading = false;
        }
    }

    // ---------------------------------------------------------------
    // Helpers (copy từ StatsSummary)
    // ---------------------------------------------------------------
    _parseValue(value) {
        if (typeof value === "number") return value;
        if (typeof value === "string") {
            // Parse viết tắt VN ("1.5 tr", "2.3 tỷ", "850 N") + legacy K/M/B.
            let mult = 1;
            if (/tỷ/.test(value)) mult = 1e9;
            else if (/tr/.test(value)) mult = 1e6;
            else if (/N/.test(value)) mult = 1e3;
            else if (value.includes("B")) mult = 1e9;
            else if (value.includes("M")) mult = 1e6;
            else if (value.includes("K")) mult = 1e3;
            const parsed = parseFloat(value.replace(/[^0-9.-]/g, ""));
            return isNaN(parsed) ? 0 : parsed * mult;
        }
        return 0;
    }

    _formatValue(value) {
        if (typeof value !== "number") return { formatted: value, original: null };
        const sign = value < 0 ? "-" : "";
        const n = Math.abs(value);
        let formatted;
        if (n >= 1e9) formatted = `${sign}${parseFloat((n / 1e9).toFixed(2))} tỷ`;
        else if (n >= 1e6) formatted = `${sign}${parseFloat((n / 1e6).toFixed(1))} tr`;
        else if (n >= 1e3) formatted = `${sign}${Math.round(n / 1e3)} N`;
        else formatted = value.toLocaleString("vi-VN", { maximumFractionDigits: 0 });
        const hasFormat = /tỷ|tr|N/.test(formatted);
        return {
            formatted,
            original: hasFormat
                ? value.toLocaleString("vi-VN", { maximumFractionDigits: 0 })
                : null,
        };
    }

    _animateValue(start, end, duration) {
        this.state.isAnimating = true;
        const startTime = performance.now();
        const diff = end - start;
        const ease = (t) => 1 - Math.pow(1 - t, 4);
        const tick = (now) => {
            const elapsed = now - startTime;
            const progress = Math.min(elapsed / duration, 1);
            const current = start + diff * ease(progress);
            const f = this._formatValue(current);
            this.state.displayValue = f.formatted;
            this.state.originalValue = f.original;
            if (progress < 1) requestAnimationFrame(tick);
            else {
                this.state.isAnimating = false;
                const final = this._formatValue(end);
                this.state.displayValue = final.formatted;
                this.state.originalValue = final.original;
            }
        };
        requestAnimationFrame(tick);
    }

    get backgroundColor() {
        return this.props.bgColor || "#7c3aed";
    }
    get textColor() {
        return this.props.textColor || "#ffffff";
    }
    get gradientStyle() {
        const bg = this.backgroundColor;
        const dark = this._darken(bg, 20);
        return `background: linear-gradient(135deg, ${bg} 0%, ${dark} 100%);`;
    }
    _darken(hex, percent) {
        hex = hex.replace("#", "");
        const num = parseInt(hex, 16);
        const amt = Math.round(2.55 * percent);
        const R = Math.max(0, Math.min(255, (num >> 16) - amt));
        const G = Math.max(0, Math.min(255, ((num >> 8) & 0xff) - amt));
        const B = Math.max(0, Math.min(255, (num & 0xff) - amt));
        return "#" + (0x1000000 + R * 0x10000 + G * 0x100 + B).toString(16).slice(1);
    }
    get trendClass() {
        if (this.props.trend === "up") return "trend-up";
        if (this.props.trend === "down") return "trend-down";
        return "trend-neutral";
    }
    get trendIcon() {
        if (this.props.trend === "up") return "fa-arrow-up";
        if (this.props.trend === "down") return "fa-arrow-down";
        return "fa-minus";
    }
}

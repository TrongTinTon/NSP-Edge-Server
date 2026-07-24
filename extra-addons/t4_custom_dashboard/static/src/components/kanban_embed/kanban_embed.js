/** @odoo-module **/

import { Component, useState, useSubEnv, onWillStart, onWillUpdateProps } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { useService } from "@web/core/utils/hooks";
import { View } from "@web/views/view";

/**
 * Embed một view (mặc định kanban) vào dashboard widget cell.
 *
 * Widget config:
 *   {
 *     "type": "kanban_embed",
 *     "data": { "title": "..." },
 *     "embed": {
 *       "model": "t4.picking.summary.card",
 *       "view_xml_id": "t4_sti.view_t4_picking_summary_kanban",
 *       "view_type": "kanban",           // optional, default 'kanban'
 *       "context": {...},
 *       "domain": [...]
 *     }
 *   }
 *
 * Resolve view_xml_id → view_id qua RPC khi mount.
 */
export class KanbanEmbed extends Component {
    static template = "t4_custom_dashboard.KanbanEmbed";
    static components = { View };
    static props = {
        embed: { type: Object },
        title: { type: String, optional: true },
    };

    setup() {
        this.action = useService("action");
        this.orm = useService("orm");

        // Tắt autofocus của SearchBar trong View nhúng. Mặc định Odoo gọi
        // useAutofocus() → input.focus() khi mount → trình duyệt scroll
        // widget (thường nằm dưới fold) vào tầm nhìn, khiến dashboard "nhảy"
        // xuống lúc vừa vào. View con đọc `env.config.disableSearchBarAutofocus`
        // nên override qua sub-env (giữ nguyên các key config khác).
        useSubEnv({
            config: {
                ...this.env.config,
                disableSearchBarAutofocus: true,
            },
        });

        this.state = useState({
            viewId: this.props.embed?.view_id || false,
            viewType: this.props.embed?.view_type || "kanban",
            searchViewId: this.props.embed?.search_view_id || false,
            error: null,
            ready: false,
        });

        onWillStart(async () => {
            await this._resolveViewId(this.props.embed);
            this.state.ready = true;
        });

        onWillUpdateProps(async (nextProps) => {
            const oldXml = this.props.embed?.view_xml_id;
            const newXml = nextProps.embed?.view_xml_id;
            const oldSearch = this.props.embed?.search_view_xml_id;
            const newSearch = nextProps.embed?.search_view_xml_id;
            if (oldXml !== newXml || oldSearch !== newSearch) {
                this.state.ready = false;
                await this._resolveViewId(nextProps.embed);
                this.state.ready = true;
            }
        });
    }

    async _resolveViewId(embed) {
        if (!embed) {
            this.state.error = "Missing embed config";
            return;
        }
        const tasks = [];
        // Main view
        if (embed.view_id) {
            this.state.viewId = embed.view_id;
            this.state.viewType = embed.view_type || "kanban";
        } else if (embed.view_xml_id) {
            tasks.push(
                rpc("/t4_custom_dashboard/resolve_view_id", { xml_id: embed.view_xml_id })
                    .then((r) => {
                        if (r.error) throw new Error(r.error);
                        this.state.viewId = r.view_id;
                        this.state.viewType = embed.view_type || r.view_type || "kanban";
                    })
            );
        } else {
            this.state.viewId = false;
            this.state.viewType = embed.view_type || "kanban";
        }
        // Search view (optional — chỉ resolve khi cần search bar)
        if (embed.search_view_id) {
            this.state.searchViewId = embed.search_view_id;
        } else if (embed.search_view_xml_id) {
            tasks.push(
                rpc("/t4_custom_dashboard/resolve_view_id", { xml_id: embed.search_view_xml_id })
                    .then((r) => {
                        if (r.error) throw new Error(r.error);
                        this.state.searchViewId = r.view_id;
                    })
            );
        } else {
            this.state.searchViewId = false;
        }
        try {
            await Promise.all(tasks);
            this.state.error = null;
        } catch (e) {
            this.state.error = e.message;
        }
    }

    /**
     * Row click handler → load action t4_sti và open form record đó.
     * Hỗ trợ `embed.row_record_field` để remap (vd product.product →
     * product_tmpl_id để mở form template thay vì variant).
     */
    async _onSelectRecord(resId, options = {}) {
        const embed = this.props.embed;
        const actionXmlId = embed.row_action_xml_id;
        if (!actionXmlId) {
            return;
        }
        let targetId = resId;
        const remapField = embed.row_record_field;
        if (remapField) {
            try {
                const records = await this.orm.read(embed.model, [resId], [remapField]);
                const value = records && records[0] && records[0][remapField];
                if (Array.isArray(value)) {
                    targetId = value[0]; // many2one → [id, display_name]
                } else if (value) {
                    targetId = value;
                }
            } catch (e) {
                console.error("KanbanEmbed: remap field read failed", e);
            }
        }
        await this.action.doAction(actionXmlId, {
            additionalContext: { active_id: targetId, active_ids: [targetId] },
            props: { resId: targetId, resIds: [targetId] },
            viewType: "form",
        });
    }

    get viewProps() {
        const showSearch = !!this.props.embed.show_search;
        const baseContext = this.props.embed.context || {};
        // KHÔNG ép create/edit/delete=false qua context: các key này LEAK
        // sang action drill-down (button type=object → doAction) khiến form
        // phiếu mở READ-ONLY kể cả với user có quyền (vd. thủ kho). Để quyền
        // (ACL/ir.rule) tự quyết định nút Mới + khả năng sửa trên view nhúng
        // lẫn drill-down. Summary card ACL của group_user vốn read-only nên
        // kanban nhúng không có nút Mới.
        const props = {
            resModel: this.props.embed.model,
            type: this.state.viewType,
            viewId: this.state.viewId,
            context: { ...baseContext },
            domain: this.props.embed.domain || [],
            display: {
                controlPanel: showSearch ? { layoutActions: false } : false,
                searchPanel: false,
            },
            // Override default action's no-content help (Odoo 19 stock module
            // dùng <t t-call="stock.help_message_template"/> mà View component
            // không xử lý QWeb template trong context embed → render raw HTML
            // as text. Custom help text qua embed.no_content_help nếu cần,
            // mặc định empty để chỉ hiện smiley nocontent state.
            noContentHelp: this.props.embed.no_content_help || "",
            useSampleModel: false,
            noBreadcrumbs: true,
            selectRecord: (resId, opts) => this._onSelectRecord(resId, opts),
        };
        if (showSearch && this.state.searchViewId) {
            props.searchViewId = this.state.searchViewId;
        }
        return props;
    }
}

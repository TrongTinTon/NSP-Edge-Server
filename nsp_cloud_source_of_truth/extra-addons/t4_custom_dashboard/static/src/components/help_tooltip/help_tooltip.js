/** @odoo-module **/

import { Component, useState, useRef, onWillUnmount } from "@odoo/owl";

/**
 * Nút help "?" — HOVER hiện popup tạm (tự tắt khi rời chuột); CLICK ghim popup
 * lại kèm nút X để đóng. Popup portal ra <body> để KHÔNG bị GridStack (dùng CSS
 * transform để xếp card) làm lệch position:fixed → tránh lỗi layout/overflow.
 */
export class HelpTooltip extends Component {
    static template = "t4_custom_dashboard.HelpTooltip";
    static props = {
        text: { type: String },
        color: { type: String, optional: true },  // màu icon (mặc định theo text card)
    };

    setup() {
        this.iconRef = useRef("icon");
        this.state = useState({ open: false, pinned: false, top: 0, left: 0 });
        this._onDocClick = this._onDocClick.bind(this);
        onWillUnmount(() => document.removeEventListener("click", this._onDocClick, true));
    }

    _computePosition() {
        const rect = this.iconRef.el.getBoundingClientRect();
        const width = 280;
        let left = rect.left;
        if (left + width > window.innerWidth - 12) {
            left = window.innerWidth - width - 12;
        }
        this.state.top = rect.bottom + 8;
        this.state.left = Math.max(12, left);
    }

    onMouseEnter() {
        if (this.state.pinned) {
            return;
        }
        this._computePosition();
        this.state.open = true;
    }

    onMouseLeave() {
        if (!this.state.pinned) {
            this.state.open = false;
        }
    }

    onClick(ev) {
        ev.stopPropagation();
        ev.preventDefault();
        if (this.state.pinned) {
            this.close();
            return;
        }
        this._computePosition();
        this.state.open = true;
        this.state.pinned = true;
        document.addEventListener("click", this._onDocClick, true);
    }

    close() {
        this.state.open = false;
        this.state.pinned = false;
        document.removeEventListener("click", this._onDocClick, true);
    }

    _onDocClick(ev) {
        if (this.iconRef.el && this.iconRef.el.contains(ev.target)) {
            return;
        }
        this.close();
    }

    get popupStyle() {
        return `position: fixed; top: ${this.state.top}px; left: ${this.state.left}px; width: 280px; z-index: 1100;`;
    }

    get iconStyle() {
        // opacity do CSS điều khiển (ẩn mặc định, hiện khi hover card/chart).
        const color = this.props.color || "inherit";
        return `cursor: pointer; font-size: 0.8em; color: ${color};`;
    }
}

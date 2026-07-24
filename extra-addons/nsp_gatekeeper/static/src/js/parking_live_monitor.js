/** @odoo-module **/

import { Component, onMounted, onWillUnmount, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

const BUS_EVENT = "nsp_parking_live_transaction";
const MAX_ROWS = 3;

export class NspParkingLiveMonitor extends Component {
    static template = "nsp_gatekeeper.ParkingLiveMonitor";

    setup() {
        this.orm = useService("orm");
        this.busService = useService("bus_service");

        const params = this.props.action?.params || {};
        this.parkingAreaId = Number(params.parking_area_id || 0);
        this.seen = new Set();
        this.flashTimer = null;
        this.clockTimer = null;
        this.reconcileTimer = null;
        this.unsubscribeBus = null;
        this.loadingSnapshot = false;

        this.state = useState({
            parkingAreaName: "",
            branchName: "",
            areaState: "",
            stream: [],
            frozenItem: null,
            flashKey: "",
            capacityConfigured: false,
            motorbikeCapacity: 0,
            motorbikeOccupied: 0,
            availableSlots: null,
            clock: "",
            error: "",
        });

        onMounted(async () => {
            this.tickClock();
            this.clockTimer = setInterval(() => this.tickClock(), 1000);

            this.unsubscribeBus = this.busService.subscribe(BUS_EVENT, (payload) => {
                this.onBusTransaction(payload);
            });

            await this.loadSnapshot({ reset: true });
            // Bus is the realtime transport. This slow reconciliation only heals
            // a browser reconnect or a missed bus message; it is not 1-second polling.
            this.reconcileTimer = setInterval(() => this.loadSnapshot({ reset: false }), 15000);
        });

        onWillUnmount(() => {
            if (this.clockTimer) {
                clearInterval(this.clockTimer);
            }
            if (this.reconcileTimer) {
                clearInterval(this.reconcileTimer);
            }
            if (this.flashTimer) {
                clearTimeout(this.flashTimer);
            }
            if (typeof this.unsubscribeBus === "function") {
                this.unsubscribeBus();
            }
        });
    }

    get areaStateLabel() {
        return {
            draft: "ĐANG CẤU HÌNH",
            operational: "ĐANG VẬN HÀNH",
            maintenance: "BẢO TRÌ",
            blocked: "TẠM KHÓA",
        }[this.state.areaState] || String(this.state.areaState || "").toUpperCase();
    }

    get displayRows() {
        const rows = [];
        if (this.state.frozenItem) {
            rows.push(this._rowDescriptor(this.state.frozenItem, 0, true));
        }
        const remaining = MAX_ROWS - rows.length;
        for (let index = 0; index < remaining; index++) {
            const item = this.state.stream[index] || null;
            rows.push(this._rowDescriptor(item, rows.length, false));
        }
        while (rows.length < MAX_ROWS) {
            rows.push(this._rowDescriptor(null, rows.length, false));
        }
        return rows;
    }

    get slotLabel() {
        if (!this.state.capacityConfigured) {
            return "🛵 Xe máy nội bộ: Chưa cấu hình sức chứa";
        }
        if (Number(this.state.availableSlots || 0) <= 0) {
            return "🛵 Xe máy nội bộ: Hết chỗ";
        }
        return `🛵 Xe máy nội bộ: Còn chỗ • ${this.state.availableSlots}`;
    }

    get slotBadgeClass() {
        if (!this.state.capacityConfigured) {
            return "is-unconfigured";
        }
        return Number(this.state.availableSlots || 0) > 0 ? "is-available" : "is-full";
    }

    _rowDescriptor(item, index, frozen) {
        const key = item
            ? `${item.transaction_uid || item.id}-${frozen ? "frozen" : index}`
            : `empty-${index}`;
        const classes = [];
        if (!item) {
            classes.push("is-empty");
        } else if (frozen || !item.is_valid) {
            classes.push("is-denied", "is-frozen");
        } else {
            classes.push("is-allowed");
        }
        if (item && this.state.flashKey === this._itemKey(item)) {
            classes.push("is-new");
        }
        return { key, item, classes: classes.join(" ") };
    }

    _itemKey(item) {
        return String(item?.transaction_uid || item?.id || "");
    }

    _sameVehicle(left, right) {
        return Boolean(left && right && String(left.vehicle_key || "") === String(right.vehicle_key || ""));
    }

    _applySlotState(payload) {
        if (!payload) {
            return;
        }
        this.state.capacityConfigured = Boolean(payload.capacity_configured);
        this.state.motorbikeCapacity = Number(payload.motorbike_capacity || 0);
        this.state.motorbikeOccupied = Number(payload.motorbike_occupied || 0);
        this.state.availableSlots = payload.available_slots === null || payload.available_slots === undefined
            ? null
            : Number(payload.available_slots);
    }

    _flash(item) {
        this.state.flashKey = this._itemKey(item);
        if (this.flashTimer) {
            clearTimeout(this.flashTimer);
        }
        this.flashTimer = setTimeout(() => {
            this.state.flashKey = "";
        }, 500);
    }

    _trimStream() {
        const maxStreamRows = this.state.frozenItem ? MAX_ROWS - 1 : MAX_ROWS;
        while (this.state.stream.length > maxStreamRows) {
            // Requirement: remove the oldest row from the bottom.
            this.state.stream.pop();
        }
    }

    pushEntry(item, { flash = true } = {}) {
        if (!item || item.event_type !== "check_in") {
            return;
        }
        const key = this._itemKey(item);
        if (!key || this.seen.has(key)) {
            return;
        }
        this.seen.add(key);

        if (!item.is_valid) {
            // A denied entry stays pinned at Row 1. Valid vehicles continue rolling
            // in Rows 2-3 until the same vehicle later receives a valid entry.
            this.state.frozenItem = item;
            this._trimStream();
            if (flash) {
                this._flash(item);
            }
            return;
        }

        if (this.state.frozenItem && this._sameVehicle(this.state.frozenItem, item)) {
            this.state.frozenItem = null;
        }

        // Requirement: newest vehicle goes to the first rolling row.
        this.state.stream.unshift(item);
        this._trimStream();
        if (flash) {
            this._flash(item);
        }
    }

    onBusTransaction(payload) {
        if (!payload || Number(payload.parking_area_id || 0) !== this.parkingAreaId) {
            return;
        }
        this._applySlotState(payload);
        // Check-out changes available slots but is intentionally not shown in the
        // customer-facing entry stream.
        if (payload.event_type === "check_in") {
            this.pushEntry(payload, { flash: true });
        }
    }

    tickClock() {
        this.state.clock = new Date().toLocaleString("vi-VN", {
            hour: "2-digit",
            minute: "2-digit",
            second: "2-digit",
            day: "2-digit",
            month: "2-digit",
            year: "numeric",
            hour12: false,
        });
    }

    async loadSnapshot({ reset = false } = {}) {
        if (!this.parkingAreaId || this.loadingSnapshot) {
            return;
        }
        this.loadingSnapshot = true;
        try {
            const data = await this.orm.call(
                "nsp.parking.area",
                "get_live_monitor_snapshot",
                [this.parkingAreaId, 12]
            );
            if (!data?.found) {
                this.state.error = "Không tìm thấy Parking Operation Configuration.";
                return;
            }
            this.state.parkingAreaName = data.parking_area_name || "";
            this.state.branchName = data.branch_name || "";
            this.state.areaState = data.state || "";
            this._applySlotState(data);

            if (reset) {
                this.state.stream.splice(0);
                this.state.frozenItem = null;
                this.seen.clear();
            }
            for (const item of data.items || []) {
                this.pushEntry(item, { flash: false });
            }
            this.state.error = "";
        } catch (error) {
            this.state.error = error?.message || "Không thể tải dữ liệu Live Monitor.";
        } finally {
            this.loadingSnapshot = false;
        }
    }
}

registry.category("actions").add("nsp_parking_live_monitor", NspParkingLiveMonitor);

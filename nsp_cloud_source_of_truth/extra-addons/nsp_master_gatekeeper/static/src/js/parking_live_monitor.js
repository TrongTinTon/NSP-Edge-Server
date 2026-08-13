/** @odoo-module **/

import { Component, onMounted, onWillUnmount, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

const BUS_EVENT = "nsp_parking_live_log";
const DISPLAY_ROWS = 4;
const DEFAULT_COLUMNS = 2;
const MIN_COLUMNS = 1;
const MAX_COLUMNS = 4;
const MAX_HISTORY = 60;
const MAX_QUEUE = 240;
const MAX_ALERTS = 8;
const VISIBLE_ALERTS = 2;
const ALERT_HOLD_MS = 12000;
const NEW_CARD_HOLD_MS = 12000;
const BURST_WINDOW_MS = 10000;
const BURST_THRESHOLD = 10;
const NORMAL_DRAIN_MS = 650;
const BURST_DRAIN_MS = 900;

export class NspParkingLiveMonitor extends Component {
    static template = "nsp_master_gatekeeper.ParkingLiveMonitor";

    setup() {
        this.orm = useService("orm");
        this.busService = useService("bus_service");

        const params = this.props.action?.params || {};
        this.parkingAreaId = Number(params.parking_area_id || 0);
        this.displayColumnsStorageKey = `nsp.parking.live.columns.${this.parkingAreaId}`;
        this.initialDisplayColumns = this._loadDisplayColumns();

        this.seenKeys = new Set();
        this.seenOrder = [];
        this.entryQueue = [];
        this.pendingBusPayloads = [];
        this.busFlushFrame = null;
        this.flashTimer = null;
        this.clockTimer = null;
        this.reconcileTimer = null;
        this.queueTimer = null;
        this.unsubscribeBus = null;
        this.loadingSnapshot = false;

        this.state = useState({
            parkingAreaName: "",
            branchName: "",
            areaState: "",
            displayColumns: this.initialDisplayColumns,
            settingsOpen: false,
            entries: [],
            alerts: [],
            flashKeys: [],
            pendingCount: 0,
            recentArrivalTimes: [],
            clock: "",
            error: "",
        });

        onMounted(async () => {
            this.tickClock();
            this.clockTimer = setInterval(() => this.tickClock(), 1000);
            this.queueTimer = setInterval(() => this.drainEntryQueue(), NORMAL_DRAIN_MS);

            this.unsubscribeBus = this.busService.subscribe(BUS_EVENT, (payload) => {
                this.queueBusPayload(payload);
            });

            await this.loadSnapshot({ reset: true });
            // Reconciliation is deliberately slow. Realtime data comes from bus_service;
            // this only heals a browser reconnect or missed event.
            this.reconcileTimer = setInterval(() => this.loadSnapshot({ reset: false }), 15000);
        });

        onWillUnmount(() => {
            for (const timer of [this.clockTimer, this.reconcileTimer, this.queueTimer, this.flashTimer]) {
                if (timer) {
                    clearInterval(timer);
                    clearTimeout(timer);
                }
            }
            if (this.busFlushFrame) {
                cancelAnimationFrame(this.busFlushFrame);
            }
            if (typeof this.unsubscribeBus === "function") {
                this.unsubscribeBus();
            }
        });
    }

    get rootClass() {
        return `nsp-parking-live-monitor columns-${this.displayColumns}${this.isBurstMode ? " is-burst" : ""}`;
    }

    get displayColumns() {
        return this._normalizeDisplayColumns(this.state.displayColumns);
    }

    get visibleCapacity() {
        return this.displayColumns * DISPLAY_ROWS;
    }

    get gridStyle() {
        return `--nsp-live-columns: ${this.displayColumns};`;
    }

    get areaStateLabel() {
        return {
            draft: "ĐANG CẤU HÌNH",
            operational: "ĐANG VẬN HÀNH",
            maintenance: "BẢO TRÌ",
            blocked: "TẠM KHÓA",
        }[this.state.areaState] || String(this.state.areaState || "").toUpperCase();
    }

    get visibleEntries() {
        return this.state.entries.slice(0, this.visibleCapacity).map((item, index) => ({
            key: this._itemKey(item),
            item,
            index,
            classes: this.entryCardClass(item),
        }));
    }

    get visibleAlerts() {
        return this.state.alerts.slice(0, VISIBLE_ALERTS);
    }

    get hiddenAlertCount() {
        return Math.max(this.state.alerts.length - VISIBLE_ALERTS, 0);
    }

    get burstCount() {
        const cutoff = Date.now() - BURST_WINDOW_MS;
        return this.state.recentArrivalTimes.filter((value) => value >= cutoff).length;
    }

    get isBurstMode() {
        return this.burstCount >= BURST_THRESHOLD || this.state.pendingCount >= this.visibleCapacity;
    }

    get burstLabel() {
        if (!this.isBurstMode) {
            return `${this.burstCount} lượt / 10 giây`;
        }
        return `CAO ĐIỂM • ${this.burstCount} lượt / 10 giây`;
    }

    get queueLabel() {
        if (!this.state.pendingCount) {
            return `${this.visibleCapacity} xe gần nhất • ${this.displayColumns} cột`;
        }
        return `Đang hiển thị theo đợt • còn ${this.state.pendingCount}`;
    }

    entryCardClass(item) {
        if (!item) {
            return "is-empty";
        }
        const classes = ["is-entry"];
        if (this.isVerifying(item)) {
            classes.push("is-verifying");
        } else if (this.isNewEntry(item)) {
            classes.push("is-new");
        }
        if (item.event_type === "check_out") {
            classes.push("is-check-out");
        }
        return classes.join(" ");
    }

    isNewEntry(item) {
        return item?.event_type === "check_in" && this.state.flashKeys.includes(this._itemKey(item));
    }

    isVerifying(item) {
        const value = String(item?.verification_status || item?.status || "").trim().toLowerCase();
        return ["verifying", "pending", "processing", "in_review"].includes(value);
    }

    entryStatusLabel(item) {
        if (this.isVerifying(item)) {
            return "ĐANG XÁC MINH";
        }
        if (this.isNewEntry(item)) {
            return "MỚI VÀO";
        }
        if (item?.event_type === "check_out") {
            return "ĐÃ RA";
        }
        return "ĐÃ VÀO";
    }

    entryStatusClass(item) {
        if (this.isVerifying(item)) {
            return "is-verifying";
        }
        if (this.isNewEntry(item)) {
            return "is-new";
        }
        return item?.event_type === "check_out" ? "is-out" : "is-in";
    }

    personInitials(item) {
        const name = String(item?.employee_name || "").trim();
        if (!name) {
            return "?";
        }
        return name
            .split(/\s+/)
            .filter(Boolean)
            .slice(-2)
            .map((part) => part.charAt(0).toUpperCase())
            .join("");
    }

    toggleSettings() {
        this.state.settingsOpen = !this.state.settingsOpen;
    }

    selectDisplayColumns(value) {
        this.setDisplayColumns(value);
        this.state.settingsOpen = false;
    }

    _normalizeDisplayColumns(value) {
        const columns = Number.parseInt(value, 10);
        if (!Number.isFinite(columns)) {
            return DEFAULT_COLUMNS;
        }
        return Math.min(MAX_COLUMNS, Math.max(MIN_COLUMNS, columns));
    }

    _loadDisplayColumns() {
        try {
            return this._normalizeDisplayColumns(window.localStorage.getItem(this.displayColumnsStorageKey));
        } catch {
            return DEFAULT_COLUMNS;
        }
    }

    setDisplayColumns(value) {
        const columns = this._normalizeDisplayColumns(value);
        this.state.displayColumns = columns;
        this.state.entries = this.state.entries.slice(0, this.visibleCapacity);
        try {
            window.localStorage.setItem(this.displayColumnsStorageKey, String(columns));
        } catch {
            // Browser storage may be unavailable in private/restricted sessions.
        }
    }

    _eventTimeMs(value) {
        if (!value) {
            return 0;
        }
        const raw = String(value).trim();
        const iso = raw.includes("T") ? raw : raw.replace(" ", "T");
        const normalized = /(?:Z|[+-]\d{2}:?\d{2})$/.test(iso) ? iso : `${iso}Z`;
        const parsed = new Date(normalized);
        return Number.isNaN(parsed.getTime()) ? 0 : parsed.getTime();
    }

    formatEventTime(value) {
        const eventMs = this._eventTimeMs(value);
        if (!eventMs) {
            return "";
        }
        return new Date(eventMs).toLocaleTimeString("vi-VN", {
            hour: "2-digit",
            minute: "2-digit",
            second: "2-digit",
            hour12: false,
        });
    }

    _itemKey(item) {
        return String(item?.log_uid || item?.id || "");
    }

    _vehicleKey(item) {
        return String(item?.vehicle_key || item?.vehicle_id || item?.license_plate || "");
    }

    _markSeen(item) {
        const key = this._itemKey(item);
        if (!key || this.seenKeys.has(key)) {
            return false;
        }
        this.seenKeys.add(key);
        this.seenOrder.push(key);
        while (this.seenOrder.length > 1500) {
            this.seenKeys.delete(this.seenOrder.shift());
        }
        return true;
    }

    queueBusPayload(payload) {
        if (!payload || Number(payload.parking_area_id || 0) !== this.parkingAreaId) {
            return;
        }
        this.pendingBusPayloads.push(payload);
        if (!this.busFlushFrame) {
            this.busFlushFrame = requestAnimationFrame(() => this.flushBusPayloads());
        }
    }

    flushBusPayloads() {
        this.busFlushFrame = null;
        const batch = this.pendingBusPayloads.splice(0);
        if (!batch.length) {
            return;
        }

        const now = Date.now();
        const arrivals = [...this.state.recentArrivalTimes];
        for (const payload of batch) {
            if (!this._markSeen(payload)) {
                continue;
            }
            if (payload.event_type === "check_in") {
                arrivals.push(now);
            }
            this.routeDisplayPayload(payload);
        }
        const cutoff = now - BURST_WINDOW_MS;
        this.state.recentArrivalTimes = arrivals.filter((value) => value >= cutoff).slice(-240);
        this.state.pendingCount = this.entryQueue.length;

        // Low traffic should feel immediate. High traffic is drained as pages so
        // a 20-30 vehicle burst is not visually dropped in a single render frame.
        if (this.entryQueue.length && !this.isBurstMode) {
            this.drainEntryQueue();
        }
    }

    routeDisplayPayload(payload, { fromSnapshot = false } = {}) {
        if (payload.display_kind === "none" || payload.display_kind === "ignore") {
            return;
        }
        if (payload.display_kind === "alert") {
            if (!fromSnapshot || this._isRecent(payload, ALERT_HOLD_MS)) {
                this.addAlert(payload);
            }
            return;
        }
        if (payload.display_kind !== "entry") {
            return;
        }

        this.clearVehicleAlert(payload);
        if (fromSnapshot) {
            const recentCheckIn = payload.event_type === "check_in" && this._isRecent(payload, NEW_CARD_HOLD_MS);
            this.insertEntryNow(payload, { flash: recentCheckIn });
            return;
        }
        this.enqueueEntry(payload);
    }

    enqueueEntry(item) {
        const vehicleKey = this._vehicleKey(item);
        if (vehicleKey) {
            this.entryQueue = this.entryQueue.filter((queued) => this._vehicleKey(queued) !== vehicleKey);
        }
        this.entryQueue.push(item);
        this.entryQueue.sort((left, right) => {
            const leftTime = this._eventTimeMs(left.event_time);
            const rightTime = this._eventTimeMs(right.event_time);
            return leftTime - rightTime || Number(left.id || 0) - Number(right.id || 0);
        });
        if (this.entryQueue.length > MAX_QUEUE) {
            this.entryQueue.splice(0, this.entryQueue.length - MAX_QUEUE);
        }
        this.state.pendingCount = this.entryQueue.length;
    }

    drainEntryQueue() {
        if (!this.entryQueue.length) {
            this.state.pendingCount = 0;
            return;
        }

        const capacity = this.visibleCapacity;
        const burst = this.entryQueue.length >= capacity || this.isBurstMode;
        if (burst) {
            // Page mode uses the Parking Area display configuration. With four rows,
            // 1/2/3/4 columns render 4/8/12/16 vehicles per readable wave.
            const page = this.entryQueue.splice(0, Math.min(capacity, this.entryQueue.length));
            const newestFirst = page.reverse();
            this.state.entries = newestFirst;
            this.flashEntries(newestFirst);
        } else {
            const item = this.entryQueue.shift();
            this.insertEntryNow(item, { flash: true });
        }
        this.state.pendingCount = this.entryQueue.length;

        // When backlog is large, shorten only the page interval; never animate
        // individual vehicles faster than the browser can render.
        if (this.queueTimer) {
            clearInterval(this.queueTimer);
        }
        const delay = this.state.pendingCount >= capacity ? BURST_DRAIN_MS : NORMAL_DRAIN_MS;
        this.queueTimer = setInterval(() => this.drainEntryQueue(), delay);
    }

    insertEntryNow(item, { flash = true } = {}) {
        if (!item) {
            return;
        }
        const vehicleKey = this._vehicleKey(item);
        const current = this.state.entries.filter(
            (row) => !vehicleKey || this._vehicleKey(row) !== vehicleKey
        );
        current.unshift(item);
        while (current.length > MAX_HISTORY) {
            current.pop();
        }
        this.state.entries = current;
        if (flash) {
            this.flashEntries([item]);
        }
    }

    flashEntries(items) {
        const keys = items
            .filter((item) => item?.event_type === "check_in")
            .map((item) => this._itemKey(item))
            .filter(Boolean);
        this.state.flashKeys = [...new Set([...this.state.flashKeys, ...keys])];
        if (this.flashTimer) {
            clearTimeout(this.flashTimer);
        }
        this.flashTimer = setTimeout(() => {
            this.state.flashKeys = [];
        }, NEW_CARD_HOLD_MS);
    }

    addAlert(item) {
        const vehicleKey = this._vehicleKey(item);
        const alert = {
            ...item,
            expires_at: Date.now() + ALERT_HOLD_MS,
        };
        const alerts = this.state.alerts.filter(
            (row) => !vehicleKey || this._vehicleKey(row) !== vehicleKey
        );
        alerts.unshift(alert);
        this.state.alerts = alerts.slice(0, MAX_ALERTS);
    }

    clearVehicleAlert(item) {
        const vehicleKey = this._vehicleKey(item);
        if (!vehicleKey) {
            return;
        }
        this.state.alerts = this.state.alerts.filter(
            (alert) => this._vehicleKey(alert) !== vehicleKey
        );
    }

    _isRecent(item, ageMs) {
        const eventMs = this._eventTimeMs(item?.event_time);
        return Boolean(eventMs) && Date.now() - eventMs <= ageMs;
    }

    tickClock() {
        const now = Date.now();
        this.state.clock = new Date(now).toLocaleString("vi-VN", {
            hour: "2-digit",
            minute: "2-digit",
            second: "2-digit",
            day: "2-digit",
            month: "2-digit",
            year: "numeric",
            hour12: false,
        });
        this.state.recentArrivalTimes = this.state.recentArrivalTimes.filter(
            (value) => value >= now - BURST_WINDOW_MS
        );
        this.state.alerts = this.state.alerts.filter(
            (alert) => Number(alert.expires_at || 0) > now
        );
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
                [this.parkingAreaId, 60]
            );
            if (!data?.found) {
                this.state.error = "Không tìm thấy Parking Operation Configuration.";
                return;
            }

            this.state.parkingAreaName = data.parking_area_name || "";
            this.state.branchName = data.branch_name || "";
            this.state.areaState = data.state || "";

            if (reset) {
                this.state.entries = [];
                this.state.alerts = [];
                this.entryQueue = [];
                this.seenKeys.clear();
                this.seenOrder = [];
            }

            for (const item of data.items || []) {
                if (!this._markSeen(item)) {
                    continue;
                }
                this.routeDisplayPayload(item, { fromSnapshot: true });
            }
            this.state.entries = this.state.entries.slice(0, this.visibleCapacity);
            this.state.pendingCount = this.entryQueue.length;
            this.state.error = "";
        } catch (error) {
            this.state.error = error?.message || "Không thể tải dữ liệu Live Monitor.";
        } finally {
            this.loadingSnapshot = false;
        }
    }
}

registry.category("actions").add("nsp_parking_live_monitor", NspParkingLiveMonitor);

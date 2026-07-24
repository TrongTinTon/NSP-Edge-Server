/** @odoo-module **/
import { useState, onMounted, onWillUnmount } from "@odoo/owl";
// File này quản lý logic bộ đếm giờ (Interval và Countdown).
// Policy version — tăng số này khi muốn force reset preference cho mọi user
// trong DB. Mỗi browser khi load lần đầu sẽ check version trong localStorage,
// nếu khác policy hiện tại thì áp default mới (enabled=true, 30s) rồi mark
// version để không reset lần nữa.
const T4_AUTOREFRESH_POLICY_VERSION = "v2_default_on_30s";

function _applyAutoRefreshPolicy() {
    const currentVersion = localStorage.getItem('dashboard_auto_refresh_policy');
    if (currentVersion !== T4_AUTOREFRESH_POLICY_VERSION) {
        localStorage.setItem('dashboard_auto_refresh_enabled', 'true');
        localStorage.setItem('dashboard_auto_refresh_interval', '30000');
        localStorage.setItem('dashboard_auto_refresh_policy', T4_AUTOREFRESH_POLICY_VERSION);
    }
}

export function useAutoRefresh(callback) {
    // BẬT auto-refresh 30s cho toàn DB (policy version v2).
    // Apply 1 lần per browser — sau đó user vẫn có thể tự toggle off.
    _applyAutoRefreshPolicy();
    const storedEnabled = localStorage.getItem('dashboard_auto_refresh_enabled');
    const storedInterval = parseInt(localStorage.getItem('dashboard_auto_refresh_interval'));
    const state = useState({
        enabled: storedEnabled === null ? true : storedEnabled === 'true',
        interval: storedInterval > 0 ? storedInterval : 30000,
        lastRefresh: null,
        nextRefresh: null,
        countdown: 0,
    });

    let refreshIntervalId = null;
    let countdownIntervalId = null;

    // Tính thời gian refresh tiếp theo
    const _calcNextRefresh = () => {
        const next = new Date();
        next.setMilliseconds(next.getMilliseconds() + state.interval);
        state.nextRefresh = next;
    };

    const start = () => {
        if (!state.enabled) return;
        stop(); // Clear cũ trước khi start mới

        console.log(`Auto-refresh started: every ${state.interval / 1000}s`);
        state.lastRefresh = new Date();
        _calcNextRefresh();

        // Interval chính để reload data
        refreshIntervalId = setInterval(async () => {
            if (state.enabled) {
                console.log('Auto-refreshing dashboard data...');
                state.lastRefresh = new Date();
                
                if (callback) await callback(true); // true = silent reload
                
                _calcNextRefresh();
            }
        }, state.interval);

        // Interval phụ để đếm ngược UI (1s/lần)
        countdownIntervalId = setInterval(() => {
            if (state.enabled && state.nextRefresh) {
                const diff = state.nextRefresh - new Date();
                state.countdown = diff <= 0 ? 0 : Math.floor(diff / 1000);
            } else {
                state.countdown = 0;
            }
        }, 1000);
    };

    const stop = () => {
        if (refreshIntervalId) clearInterval(refreshIntervalId);
        if (countdownIntervalId) clearInterval(countdownIntervalId);
        refreshIntervalId = null;
        countdownIntervalId = null;
        state.nextRefresh = null;
        state.countdown = 0;
        console.log('Auto-refresh stopped');
    };

    const toggle = () => {
        state.enabled = !state.enabled;
        localStorage.setItem('dashboard_auto_refresh_enabled', state.enabled);
        state.enabled ? start() : stop();
    };

    const setIntervalTime = (seconds) => {
        state.interval = seconds * 1000;
        localStorage.setItem('dashboard_auto_refresh_interval', state.interval);
        if (state.enabled) start(); // Restart với interval mới
    };

    const getTimeText = () => {
        if (!state.enabled || !state.nextRefresh) return "Off";
        const c = state.countdown;
        if (c <= 0) return "Refreshing...";
        if (c < 60) return `${c}s`;
        return `${Math.floor(c / 60)}m ${c % 60}s`;
    };

    onMounted(() => {
        if (state.enabled) start();
    });

    onWillUnmount(() => {
        stop();
    });

    return {
        state,
        start,
        stop,
        toggle,
        setIntervalTime,
        getTimeText,
        manualRefreshTrigger: () => {
            state.lastRefresh = new Date();
            if (state.enabled) {
                 // Reset interval nếu user bấm refresh tay
                _calcNextRefresh();
                stop();
                start();
            }
        }
    };
}
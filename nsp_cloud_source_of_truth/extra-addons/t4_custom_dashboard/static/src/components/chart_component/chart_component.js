/** @odoo-module **/

import {
    Component,
    useRef,
    onMounted,
    onWillStart,
    onWillUnmount,
    onWillUpdateProps,
    useState,
} from "@odoo/owl";
import { loadBundle } from "@web/core/assets";
import { useService } from "@web/core/utils/hooks";
import { rpc } from "@web/core/network/rpc";

// Plugin vẽ NHÃN GIÁ TRỊ trên đầu mỗi cột/điểm. Chart.js của Odoo không bundle
// chartjs-plugin-datalabels nên tự vẽ ở afterDatasetsDraw. Chỉ chạy khi
// options.plugins.t4DataLabels.enabled = true (gate per-chart). `format` là hàm
// (đi qua mergeOptions không bị JSON-clone nên giữ nguyên).
const T4DataLabelsPlugin = {
    id: "t4DataLabels",
    afterDatasetsDraw(chart, _args, opts) {
        if (!opts || !opts.enabled) {
            return;
        }
        const { ctx } = chart;
        const fmt = typeof opts.format === "function" ? opts.format : (v) => v;
        const isPie =
            chart.config && (chart.config.type === "pie" || chart.config.type === "doughnut");
        ctx.save();
        ctx.font = opts.font || "700 11px sans-serif";
        ctx.fillStyle = opts.color || "#374151";
        ctx.textAlign = "center";

        // stackMode: cột CHỒNG → mỗi stack chỉ 1 nhãn = TỔNG (đã + sẽ), vẽ ở
        // đỉnh segment trên cùng. Tránh nhãn chồng lên ranh giới các phần.
        if (opts.stackMode) {
            const datasets = chart.data.datasets;
            const count = datasets.length ? (datasets[0].data || []).length : 0;
            for (let idx = 0; idx < count; idx++) {
                const stacks = {};
                datasets.forEach((dataset, di) => {
                    const meta = chart.getDatasetMeta(di);
                    if (meta.hidden) {
                        return;
                    }
                    const value = dataset.data[idx];
                    const el = meta.data[idx];
                    if (value === null || value === undefined || !el) {
                        return;
                    }
                    const sid = dataset.stack || `__${di}`;
                    const s = stacks[sid] || { total: 0, topY: Infinity, x: el.x };
                    s.total += value;
                    if (el.y < s.topY) {
                        s.topY = el.y;
                        s.x = el.x;
                    }
                    stacks[sid] = s;
                });
                Object.values(stacks).forEach((s) => {
                    if (!s.total) {
                        return;
                    }
                    ctx.textBaseline = "bottom";
                    ctx.fillText(fmt(s.total), s.x, s.topY - 4);
                });
            }
            ctx.restore();
            return;
        }

        chart.data.datasets.forEach((dataset, di) => {
            const meta = chart.getDatasetMeta(di);
            if (meta.hidden) {
                return;
            }
            // PIE/DOUGHNUT: vẽ SỐ TIỀN + % ngay trên mỗi lát (tại tâm cung).
            const total = isPie
                ? dataset.data.reduce((a, b) => a + (Math.abs(b) || 0), 0)
                : 0;
            meta.data.forEach((element, idx) => {
                const value = dataset.data[idx];
                if (value === null || value === undefined || value === 0) {
                    return;
                }
                if (isPie) {
                    const pos = element.tooltipPosition
                        ? element.tooltipPosition()
                        : element;
                    const pct = total ? Math.round((value / total) * 100) : 0;
                    ctx.textBaseline = "middle";
                    ctx.fillText(fmt(value), pos.x, pos.y - 7);
                    ctx.fillText(pct + "%", pos.x, pos.y + 8);
                    return;
                }
                const negative = value < 0;
                ctx.textBaseline = negative ? "top" : "bottom";
                ctx.fillText(fmt(value), element.x, element.y + (negative ? 4 : -4));
            });
        });
        ctx.restore();
    },
};

// Plugin vẽ VẠCH DỌC tại KỲ HIỆN TẠI (mốc "hôm nay") trên biểu đồ thời gian.
// Bật qua options.plugins.t4CurrentMarker = {enabled, index, color, lineWidth}.
// index = vị trí cột/điểm của kỳ chứa hôm nay (tính ở ChartUtils.currentPeriodIndex).
const T4CurrentMarkerPlugin = {
    id: "t4CurrentMarker",
    afterDatasetsDraw(chart, _args, opts) {
        if (!opts || !opts.enabled || opts.index == null || opts.index < 0) {
            return;
        }
        const meta = chart.getDatasetMeta(0);
        const el = meta && meta.data && meta.data[opts.index];
        if (!el) {
            return;
        }
        const { top, bottom } = chart.chartArea;
        const ctx = chart.ctx;
        ctx.save();
        ctx.beginPath();
        ctx.setLineDash([4, 3]);
        ctx.lineWidth = opts.lineWidth || 2;
        ctx.strokeStyle = opts.color || "rgba(148,163,184,0.9)";
        ctx.moveTo(el.x, top);
        ctx.lineTo(el.x, bottom);
        ctx.stroke();
        ctx.restore();
    },
};

// Plugin vẽ NHÃN GIÁ TRỊ ở ĐUÔI mỗi thanh NGANG (horizontal bar). Khác
// T4DataLabelsPlugin (căn giữa-trên cho cột dọc): ở hbar element.x là đầu mút
// thanh (vị trí giá trị), element.y là tâm thanh → vẽ chữ bên PHẢI đầu thanh,
// canh giữa theo chiều dọc. Bật qua options.plugins.t4HBarLabels.enabled.
const T4HBarLabelsPlugin = {
    id: "t4HBarLabels",
    afterDatasetsDraw(chart, _args, opts) {
        if (!opts || !opts.enabled) {
            return;
        }
        const { ctx } = chart;
        const fmt = typeof opts.format === "function" ? opts.format : (v) => v;
        ctx.save();
        ctx.font = opts.font || "700 11px sans-serif";
        ctx.fillStyle = opts.color || "#475569";
        ctx.textAlign = "left";
        ctx.textBaseline = "middle";
        chart.data.datasets.forEach((dataset, di) => {
            const meta = chart.getDatasetMeta(di);
            if (meta.hidden) {
                return;
            }
            meta.data.forEach((element, idx) => {
                const value = dataset.data[idx];
                if (value === null || value === undefined || value === 0) {
                    return;
                }
                ctx.fillText(fmt(value), element.x + 6, element.y);
            });
        });
        ctx.restore();
    },
};

export class ChartComponent extends Component {
    static template = "t4_custom_dashboard.ChartComponent";
    static props = {
        "*": { optional: true },
    };

    setup() {
        this.canvasRef = useRef("canvas");
        this.chart = null;
        this.chartRendered = false;
        this.renderTimeout = null;
        this.updateTimeout = null;
        this.isDrilling = false;

        // Drill-down services (optional — chỉ dùng khi props.action có)
        try {
            this.actionService = useService("action");
            this.notification = useService("notification");
        } catch (_e) {
            this.actionService = null;
            this.notification = null;
        }

        this.state = useState({
            isAnimating: false,
            animationProgress: 0,
        });
        
        onWillStart(async () => {
            await loadBundle("web.chartjs_lib");
        });

        onMounted(() => {
            const doRender = () => {
                this.renderTimeout = setTimeout(() => {
                    this.renderChart();
                }, 50);
            };

            // Đảm bảo web font đã load xong trước khi vẽ lên canvas
            if (document.fonts && document.fonts.ready) {
                document.fonts.ready.then(() => {
                    doRender();
                });
            } else {
                doRender();
            }
        });

        onWillUpdateProps((nextProps) => {
            // Chỉ update nếu có thay đổi thực sự
            if (this.chart && this.hasPropsChanged(nextProps)) {
                // Clear pending updates để tránh duplicate
                if (this.updateTimeout) {
                    clearTimeout(this.updateTimeout);
                }
                
                // Debounce để tránh update quá nhiều lần
                this.updateTimeout = setTimeout(() => {
                    this.updateChart(nextProps);
                }, 50); // Giảm từ 100ms xuống 50ms cho responsive hơn
            }
        });

        onWillUnmount(() => {
            if (this.renderTimeout) {
                clearTimeout(this.renderTimeout);
            }
            if (this.updateTimeout) {
                clearTimeout(this.updateTimeout);
            }
            if (this.scrollTimeout) {
                clearTimeout(this.scrollTimeout);
            }
            this.destroyChart();
        });
    }

    hasPropsChanged(nextProps) {
        // Check type change
        if (this.props.type !== nextProps.type) {
            return true;
        }
        
        // So sánh data - chỉ so sánh structure quan trọng
        const currentLabels = this.props.data?.labels || [];
        const nextLabels = nextProps.data?.labels || [];
        
        if (currentLabels.length !== nextLabels.length) {
            return true;
        }
        
        const currentDatasets = this.props.data?.datasets || [];
        const nextDatasets = nextProps.data?.datasets || [];
        
        if (currentDatasets.length !== nextDatasets.length) {
            return true;
        }
        
        // So sánh values trong datasets
        for (let i = 0; i < currentDatasets.length; i++) {
            const currentValues = JSON.stringify(currentDatasets[i]?.data || []);
            const nextValues = JSON.stringify(nextDatasets[i]?.data || []);
            
            if (currentValues !== nextValues) {
                return true;
            }
        }
        
        return false;
    }

    updateChart(nextProps) {
        if (!this.chart) {
            console.warn('Chart not initialized yet');
            return;
        }

        try {
            // Nếu type thay đổi -> phải destroy và render lại
            if (this.props.type !== nextProps.type) {
                console.log('Chart type changed, re-rendering...');
                this.destroyChart();
                setTimeout(() => {
                    this.renderChart();
                }, 50);
                return;
            }

            const newData = nextProps.data;
            
            // Kiểm tra structure change lớn
            if (this.hasLargeStructureChange(newData)) {
                console.log('Large structure change detected, re-rendering...');
                this.destroyChart();
                setTimeout(() => {
                    this.renderChart();
                }, 50);
                return;
            }
            
            // Update data - Chart.js sẽ tự animate
            console.log('Updating chart data...');
            
            // Update labels
            this.chart.data.labels = [...newData.labels];
            
            // Update datasets
            newData.datasets.forEach((newDataset, index) => {
                if (this.chart.data.datasets[index]) {
                    // Update existing dataset
                    const existingDataset = this.chart.data.datasets[index];
                    existingDataset.data = [...newDataset.data];
                    existingDataset.label = newDataset.label;
                    existingDataset.backgroundColor = newDataset.backgroundColor;
                    existingDataset.borderColor = newDataset.borderColor;
                    existingDataset.borderWidth = newDataset.borderWidth;
                    
                    // Copy other properties
                    if (newDataset.fill !== undefined) existingDataset.fill = newDataset.fill;
                    if (newDataset.tension !== undefined) existingDataset.tension = newDataset.tension;
                    if (newDataset.hoverOffset !== undefined) existingDataset.hoverOffset = newDataset.hoverOffset;
                    if (newDataset.hoverBorderWidth !== undefined) existingDataset.hoverBorderWidth = newDataset.hoverBorderWidth;
                } else {
                    // Add new dataset
                    this.chart.data.datasets.push({ ...newDataset });
                }
            });
            
            // Remove excess datasets
            if (this.chart.data.datasets.length > newData.datasets.length) {
                this.chart.data.datasets.splice(newData.datasets.length);
            }
            
            // Update options if provided
            if (nextProps.options) {
                this.chart.options = this.mergeOptions(this.chart.options, nextProps.options);
            }
            
            // Set animating state
            this.state.isAnimating = true;
            
            // Update with appropriate animation
            const animationMode = this.getAnimationMode(nextProps.type);
            this.chart.update(animationMode);
            
            console.log('Chart updated successfully');
            
        } catch (error) {
            console.error("Error updating chart:", error);
            // Fallback: destroy và render lại
            this.destroyChart();
            setTimeout(() => {
                this.renderChart();
            }, 50);
        }
    }

    hasLargeStructureChange(newData) {
        const oldLabelsCount = this.chart.data.labels?.length || 0;
        const newLabelsCount = newData.labels?.length || 0;
        
        // Nếu số labels thay đổi quá 50% -> coi là structure change lớn
        const labelChangePercent = Math.abs(oldLabelsCount - newLabelsCount) / Math.max(oldLabelsCount, 1);
        
        if (labelChangePercent > 0.5) {
            return true;
        }
        
        // Nếu số datasets thay đổi
        const oldDatasetsCount = this.chart.data.datasets?.length || 0;
        const newDatasetsCount = newData.datasets?.length || 0;
        
        if (oldDatasetsCount !== newDatasetsCount) {
            return true;
        }
        
        return false;
    }

    getAnimationMode(chartType) {
        // 'active' mode cho smooth transition
        // 'resize' mode cho resize events
        // 'none' mode cho no animation
        
        // Tất cả types đều dùng 'active' để có animation mượt
        return 'active';
    }

    destroyChart() {
        if (this.chart) {
            try {
                this.chart.destroy();
                this.chart = null;
                this.chartRendered = false;
                this.state.isAnimating = false;
                this.state.animationProgress = 0;
            } catch (error) {
                console.error("Error destroying chart:", error);
            }
        }
    }

    renderChart() {
        this.destroyChart();

        if (!this.canvasRef.el) {
            console.warn('Canvas ref not ready, retrying...');
            this.renderTimeout = setTimeout(() => {
                this.renderChart();
            }, 100);
            return;
        }

        try {
            const ctx = this.canvasRef.el.getContext("2d");
            
            if (!ctx) {
                console.error('Could not get canvas context');
                return;
            }

            const hasDrillDown = !!(
                this.props.action &&
                (this.props.action.model || this.props.action.action_xml_id)
            );
            const defaultOptions = {
                responsive: true,
                maintainAspectRatio: false,
                onHover: hasDrillDown
                    ? (event, elements, chart) => {
                          if (event?.native?.target) {
                              event.native.target.style.cursor = elements.length
                                  ? "pointer"
                                  : "default";
                          }
                      }
                    : undefined,
                onClick: hasDrillDown
                    ? (event, elements, chart) => {
                          if (!elements || !elements.length) return;
                          const el = elements[0];
                          if (el && typeof el.index === "number") {
                              this._onChartElementClick(el.index);
                          }
                      }
                    : undefined,
                animation: {
                    duration: 1000,
                    easing: 'easeInOutQuart',
                    animateRotate: true,
                    animateScale: true,
                    onProgress: (context) => {
                        if (context.initial) {
                            this.state.animationProgress = context.currentStep / context.numSteps;
                        }
                    },
                    onComplete: (context) => {
                        this.state.isAnimating = false;
                        this.state.animationProgress = 1;
                        if (!context.initial) {
                            console.log('Chart animation completed');
                        }
                    }
                },
                transitions: {
                    active: {
                        animation: {
                            duration: 800,
                            easing: 'easeInOutQuart',
                        }
                    },
                },
                plugins: {
                    legend: {
                        position: "top",
                        labels: {
                            usePointStyle: true,
                            padding: 10,
                        }
                    },
                    tooltip: {
                        enabled: true,
                        mode: 'nearest',
                        intersect: false,
                        animation: {
                            duration: 200,
                        },
                        backgroundColor: 'rgba(0, 0, 0, 0.8)',
                        padding: 12,
                        cornerRadius: 6,
                    }
                },
            };

            // Merge options
            const finalOptions = this.props.options 
                ? this.mergeOptions(defaultOptions, this.props.options)
                : defaultOptions;

            // Set animating state
            this.state.isAnimating = true;

            // Apply font from theme
            try {
                const rootStyle = window.getComputedStyle(document.documentElement);
                const t4Font = rootStyle.getPropertyValue('--t4-font-family');
                let bodyFont = t4Font ? t4Font.trim() : "";
                
                if (!bodyFont) {
                    bodyFont = window.getComputedStyle(document.body).fontFamily;
                }

                console.log("ChartComponent: t4_theme font resolved to ->", bodyFont);
                
                if (bodyFont && !bodyFont.includes('var(')) {
                    Chart.defaults.font.family = bodyFont;
                }
                // MÀU CHỮ canvas (legend/trục/tiêu đề) theo theme → SÁNG ở dark
                // mode, TỐI ở light. Lấy màu chữ KẾ THỪA TẠI CHÍNH CANVAS (nằm
                // trong card đã theme) — KHÔNG dùng document.body (dark mode Odoo
                // áp qua container, body.color vẫn tối → legend bị đen).
                const inheritedColor = this.canvasRef.el
                    ? window.getComputedStyle(this.canvasRef.el).color
                    : null;
                if (inheritedColor) {
                    Chart.defaults.color = inheritedColor;
                }
            } catch (e) {
                // Ignore
                console.error("ChartComponent: Failed to resolve t4_theme font", e);
            }
            
            // Create chart
            this.chart = new Chart(ctx, {
                type: this.props.type,
                plugins: [T4DataLabelsPlugin, T4CurrentMarkerPlugin, T4HBarLabelsPlugin],
                // Clone data để Chart.js không mutate reactive props, NHƯNG giữ
                // nguyên các giá trị HÀM (scriptable) như segment.borderColor /
                // pointBackgroundColor. JSON.parse(JSON.stringify()) cũ làm RỚT
                // hàm → đường/điểm không đổi màu theo dấu (chỉ phần fill — vốn là
                // data thuần — mới đổi). Xem cloneChartData.
                data: this.cloneChartData(this.props.data),
                options: finalOptions,
            });
            
            this.chartRendered = true;
            console.log(`Chart rendered: ${this.props.type}`);
            this._scheduleScrollToCurrent();
            
        } catch (error) {
            console.error("Error creating chart:", error);
            this.chartRendered = false;
            this.state.isAnimating = false;
        }
    }

    // Clone {labels, datasets} một tầng đủ sâu để Chart.js không ghi đè lên
    // reactive props, NHƯNG giữ reference cho HÀM scriptable (segment.borderColor,
    // pointBackgroundColor, backgroundColor có thể là hàm/mảng...). Mảng được
    // copy nông; object lồng (vd dataset.segment, dataset.fill) copy nông —
    // hàm bên trong được giữ nguyên.
    cloneChartData(data) {
        if (!data || typeof data !== "object") {
            return data;
        }
        const cloneVal = (v) => {
            if (typeof v === "function") return v;
            if (Array.isArray(v)) return v.slice();
            if (v && typeof v === "object") {
                const o = {};
                for (const k in v) o[k] = v[k]; // giữ hàm lồng (vd segment.borderColor)
                return o;
            }
            return v;
        };
        const datasets = (data.datasets || []).map((ds) => {
            const out = {};
            for (const k in ds) out[k] = cloneVal(ds[k]);
            return out;
        });
        return {
            ...data,
            labels: Array.isArray(data.labels) ? data.labels.slice() : data.labels,
            datasets,
        };
    }

    mergeOptions(defaultOpts, customOpts) {
        const merged = { ...defaultOpts };
        
        for (const key in customOpts) {
            if (customOpts[key] && typeof customOpts[key] === 'object' && !Array.isArray(customOpts[key])) {
                merged[key] = this.mergeOptions(merged[key] || {}, customOpts[key]);
            } else {
                merged[key] = customOpts[key];
            }
        }
        
        return merged;
    }

    get canvasHeight() {
        return this.props.height || 300;
    }

    // Cuộn container (overflow-x) sao cho VẠCH kỳ hiện tại nằm giữa khung. Chỉ
    // áp khi biểu đồ rộng hơn khung (vd biểu đồ cột mini trong stat_chart nhiều kỳ).
    _scheduleScrollToCurrent() {
        const opts = this.props.options?.plugins?.t4CurrentMarker;
        if (!opts || !opts.enabled || opts.index == null || opts.index < 0) {
            return;
        }
        if (this.scrollTimeout) {
            clearTimeout(this.scrollTimeout);
        }
        // Chờ Chart.js layout xong (x các cột mới có giá trị).
        this.scrollTimeout = setTimeout(() => this._scrollToCurrentMarker(), 150);
    }

    _scrollToCurrentMarker() {
        try {
            const opts = this.props.options?.plugins?.t4CurrentMarker;
            if (!this.chart || !opts || opts.index == null) {
                return;
            }
            const meta = this.chart.getDatasetMeta(0);
            const el = meta && meta.data && meta.data[opts.index];
            if (!el) {
                return;
            }
            // Tìm tổ tiên có overflow-x cuộn được (auto/scroll) VÀ rộng hơn khung.
            // Giới hạn độ sâu + check overflow-x để KHÔNG vô tình cuộn cả trang.
            let p = this.canvasRef.el?.parentElement;
            let container = null;
            for (let depth = 0; p && depth < 6; depth++, p = p.parentElement) {
                const ox = window.getComputedStyle(p).overflowX;
                if ((ox === "auto" || ox === "scroll") && p.scrollWidth > p.clientWidth + 1) {
                    container = p;
                    break;
                }
            }
            if (!container) {
                return;
            }
            const target = el.x - container.clientWidth / 2;
            container.scrollLeft = Math.max(
                0, Math.min(target, container.scrollWidth - container.clientWidth));
        } catch (_e) {
            // im lặng — auto-scroll chỉ là tiện ích
        }
    }

    // ---------------------------------------------------------------------
    // Drill-down: click chart element → resolve domain → open list view
    // ---------------------------------------------------------------------
    async _onChartElementClick(index) {
        if (this.isDrilling) return;
        const action = this.props.action;
        if (!action || (!action.model && !action.action_xml_id) || !this.actionService) return;

        // Resolve clicked entity from chart payload
        const ids = this.props.dataIds || [];
        const dates = this.props.dataDates || [];
        const clickedId = ids[index] !== undefined ? ids[index] : null;
        const clickedDate = dates[index] !== undefined ? dates[index] : null;
        if (clickedId === null && clickedDate === null) {
            if (this.notification) {
                this.notification.add(
                    "Chart chưa cung cấp id/date cho drill-down",
                    { type: "warning" },
                );
            }
            return;
        }

        this.isDrilling = true;
        try {
            const params = { ...(action.domain_params || {}) };
            if (clickedId !== null) params.clicked_id = clickedId;
            if (clickedDate !== null) params.clicked_date = clickedDate;

            // Path A: action_xml_id có sẵn (giữ context HID/RFID + view t4_)
            if (action.action_xml_id) {
                const result = await rpc(
                    "/t4_custom_dashboard/resolve_drill_action",
                    {
                        action_xml_id: action.action_xml_id,
                        domain_method: action.domain_method || null,
                        domain_model: action.domain_model || null,
                        domain_params: params,
                        filters: this.props.filters || {},
                        search_panel: this.props.searchPanelConfig || [],
                        widget_id: this.props.widgetId,
                        filter_overrides:
                            this.props.dataSource?.filterOverrides || {},
                        extra_context: action.context || null,
                    },
                );
                if (result.error) {
                    if (this.notification) {
                        this.notification.add(`Lỗi: ${result.error}`, {
                            type: "danger",
                        });
                    }
                    return;
                }
                await this.actionService.doAction(result.action);
                return;
            }

            // Path B: build action từ raw fields (legacy)
            let domain = action.domain || [];
            if (action.domain_method) {
                const result = await rpc(
                    "/t4_custom_dashboard/resolve_action_domain",
                    {
                        model: action.model,
                        domain_method: action.domain_method,
                        domain_model: action.domain_model || null,
                        domain_params: params,
                        filters: this.props.filters || {},
                        search_panel: this.props.searchPanelConfig || [],
                        widget_id: this.props.widgetId,
                        filter_overrides:
                            this.props.dataSource?.filterOverrides || {},
                    },
                );
                if (result.error) {
                    if (this.notification) {
                        this.notification.add(`Lỗi: ${result.error}`, {
                            type: "danger",
                        });
                    }
                    return;
                }
                domain = result.domain || [];
            }
            await this.actionService.doAction({
                type: "ir.actions.act_window",
                name: action.name || this.props.title || "Drill-down",
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
            if (this.notification) {
                this.notification.add(
                    `Không mở được drill-down: ${e.message || e}`,
                    { type: "danger" },
                );
            }
        } finally {
            this.isDrilling = false;
        }
    }
}
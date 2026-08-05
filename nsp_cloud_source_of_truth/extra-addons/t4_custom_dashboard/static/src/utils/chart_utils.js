/** @odoo-module **/

// File này chứa toàn bộ logic xử lý dữ liệu, màu sắc và format số.
export class ChartUtils {
    // Tạo mảng màu theo tỉ lệ vàng
    static getChartColors(count) {
        const colors = [];
        const goldenRatio = 0.618033988749895;

        for (let i = 0; i < count; i++) {
            const hue = (i * goldenRatio) % 1.0;
            let saturation, lightness;
            if (i % 3 === 0) {
                saturation = 85; lightness = 55;
            } else if (i % 3 === 1) {
                saturation = 75; lightness = 65;
            } else {
                saturation = 90; lightness = 50;
            }
            colors.push(`hsl(${hue * 360}, ${saturation}%, ${lightness}%)`);
        }
        return colors;
    }

    // Tạo màu trong suốt
    static getTransparentColor(hexColor, opacity) {
        let hex = hexColor.replace("#", "");
        if (hex.length === 3) {
            hex = hex.split("").map((char) => char + char).join("");
        }
        const r = parseInt(hex.substring(0, 2), 16);
        const g = parseInt(hex.substring(2, 4), 16);
        const b = parseInt(hex.substring(4, 6), 16);
        return `rgba(${r}, ${g}, ${b}, ${opacity})`;
    }

    // Màu chữ theo THEME (sáng ở dark mode, tối ở light) cho legend/trục/tiêu đề
    // canvas Chart.js — KHÔNG chỉ dựa Chart.defaults.color (legend generateLabels
    // tuỳ biến đôi khi không kế thừa). Đọc màu chữ của body.
    static _themeTextColor() {
        try {
            const c = window.getComputedStyle(document.body).color;
            return c || "#374151";
        } catch (e) {
            return "#374151";
        }
    }

    // Lõi format số kiểu VIỆT NAM dùng chung (card + tick trục):
    // tỷ (1e9), tr (triệu, 1e6), N (nghìn, 1e3). Giữ dấu âm.
    static formatVN(value) {
        if (typeof value !== "number" || isNaN(value)) return String(value);
        const sign = value < 0 ? "-" : "";
        const n = Math.abs(value);
        if (n >= 1e9) return `${sign}${parseFloat((n / 1e9).toFixed(2))} tỷ`;
        if (n >= 1e6) return `${sign}${parseFloat((n / 1e6).toFixed(1))} tr`;
        if (n >= 1e3) return `${sign}${Math.round(n / 1e3)} N`;
        return value.toLocaleString("vi-VN", { maximumFractionDigits: 0 });
    }

    // Vị trí cột/điểm của KỲ HIỆN TẠI (kỳ chứa hôm nay) trong mảng dates
    // ('YYYY-MM-DD' = mốc đầu mỗi kỳ). = cột mới nhất có mốc <= hôm nay. Toàn
    // tương lai → 0. Dùng cho vạch "hiện tại" + auto-scroll.
    static currentPeriodIndex(dates) {
        if (!Array.isArray(dates) || !dates.length) return -1;
        const t = new Date();
        const today = `${t.getFullYear()}-${String(t.getMonth() + 1).padStart(2, "0")}-${String(t.getDate()).padStart(2, "0")}`;
        let best = -1;
        let bestVal = "";
        for (let i = 0; i < dates.length; i++) {
            const d = dates[i];
            if (typeof d === "string" && d <= today && d >= bestVal) {
                best = i;
                bestVal = d;
            }
        }
        return best === -1 ? 0 : best;
    }

    // Format số cho card stat (trả kèm giá trị gốc cho tooltip).
    static formatNumber(value) {
        if (typeof value !== "number") return { formatted: value, original: null };
        const formatted = ChartUtils.formatVN(value);
        // Có viết tắt (tỷ/tr/N) → giữ giá trị gốc đầy đủ làm tooltip.
        const hasFormatting = /tỷ|tr|N/.test(formatted);
        return {
            formatted: formatted,
            original: hasFormatting
                ? value.toLocaleString("vi-VN", { maximumFractionDigits: 0 })
                : null,
        };
    }

    // Logic chính: Biến đổi dữ liệu thô từ server thành cấu hình ChartJS
    static processWidgetDisplayData(widget, widgetData, chartState) {
        if (!widgetData) return widget.data;

        // Xử lý lỗi
        if (widgetData.error) {
            return {
                ...widget.data,
                value: "Error",
                error: widgetData.error,
            };
        }

        // Widget "stat_chart": thẻ KPI NÂNG CẤP — value bên trái + biểu đồ cột
        // (theo ngày/kỳ) bên phải, click cột → drill-down hóa đơn/bill của kỳ đó.
        if (widget.type === "stat_chart") {
            return this._processStatChartDisplayData(widget, widgetData);
        }

        // Xử lý Widget Thống kê (Stat) — bao gồm stat_action (clickable drill-down)
        if (widget.type === "stat" || widget.type === "stat_action") {
            let formattedData = { formatted: widgetData.value, original: null };

            if (typeof widgetData.value === "number") {
                formattedData = this.formatNumber(widgetData.value);
            }
            return {
                ...widget.data,
                value: formattedData.formatted,
                // originalValue: dòng "(giá trị đầy đủ)" dưới số viết tắt. Tắt khi
                // widget.data.showOriginalValue === false (mặc định BẬT).
                originalValue: widget.data.showOriginalValue === false
                    ? null : formattedData.original,
                trend: widgetData.trend,
                trendValue: widgetData.trend_value,
                bgColor: widget.data.bgColor,
                textColor: widget.data.textColor,
                // stat_action: pass action config + dataSource cho drill-down
                action: widget.action || null,
                dataSource: widget.dataSource || null,
            };
        }

        // Xử lý Widget Biểu đồ (Chart)
        else if (widget.type === "chart") {
            // Dual-bar: 2 cột CÙNG MỘT trục y (vd Tiền thu dương / Tiền chi âm)
            // → gộp 2 chuỗi vào 1 biểu đồ, thu phía trên trục 0, chi phía dưới.
            if (widget.data.chartType === "dual_bar") {
                return this._processDualBarDisplayData(widget, widgetData, chartState);
            }

            // Grouped-stacked bar: mỗi kỳ có 2 cột (vd Thu/Chi) CẠNH nhau, mỗi
            // cột CHỒNG 2 phần — "đã" (tô đậm, dưới) + "chưa" (tô GẠCH chéo, trên).
            if (widget.data.chartType === "grouped_stacked_bar") {
                return this._processGroupedStackedBarDisplayData(widget, widgetData, chartState);
            }

            // Multi-series CÙNG trục y: server trả `series: [{label, values,
            // backgroundColor?, borderColor?}]` (vd Phiếu Hoàn Thành chia
            // Nhập/Xuất/Điều chuyển). Bar/line + toggle stack; KHÔNG sort
            // (chuỗi thời gian); pie/doughnut không áp → ép về bar.
            if (widgetData && Array.isArray(widgetData.series)) {
                return this._processMultiSeriesDisplayData(widget, widgetData, chartState);
            }

            // Combo (dual-axis) chart: data có values_left/values_right
            // hoặc widget config chartType='combo'. Bypass logic single-axis.
            const isCombo =
                widget.data.chartType === "combo" ||
                (widgetData && (widgetData.values_left || widgetData.values_right));
            if (isCombo) {
                return this._processComboDisplayData(widget, widgetData, chartState);
            }

            const state = chartState || {
                currentType: widget.data.chartType || "bar",
                sortOrder: "desc",
                isStacked: true,
                topLimit: widget.data.topLimit || null, // 🆕 Thêm topLimit
            };

            let labels = widgetData.labels || [];
            let values = widgetData.values || [];
            let ids = widgetData.ids || [];
            let dates = widgetData.dates || [];
            // forecast[i] = true → điểm DỰ BÁO (vẽ nét đứt). Cash Flow dùng để
            // phân biệt phần thực tế (liền) với phần dự báo tương lai (đứt).
            let forecastFlags = widgetData.forecast || [];
            const yAxisLabel = widgetData.suggested_y_label || widget.data.yAxisLabel || "";
            const xAxisLabel = widgetData.suggested_x_label || widget.data.xAxisLabel || "";

            // Xử lý label đa ngôn ngữ
            labels = labels.map((label) => {
                if (typeof label === "object" && label !== null) {
                    return label.vi_VN || label.en_US || Object.values(label)[0] || "Unknown";
                }
                return label;
            });

            // Logic Sort (chỉ áp dụng cho Bar/Line)
            if (state.currentType !== "pie" && state.currentType !== "doughnut") {
                const combined = labels.map((label, index) => ({
                    label: label,
                    value: values[index],
                    id: ids[index] !== undefined ? ids[index] : null,
                    date: dates[index] !== undefined ? dates[index] : null,
                    forecast: forecastFlags[index] !== undefined ? forecastFlags[index] : false,
                }));

                if (state.sortOrder) {
                    combined.sort((a, b) => {
                        return state.sortOrder === "asc" ? a.value - b.value : b.value - a.value;
                    });
                }

                labels = combined.map((item) => item.label);
                values = combined.map((item) => item.value);
                ids = combined.map((item) => item.id);
                dates = combined.map((item) => item.date);
                forecastFlags = combined.map((item) => item.forecast);
            }

            // Xây dựng dataset cho ChartJS
            const isPie = state.currentType === "pie" || state.currentType === "doughnut";

            const dataset = {
                label: yAxisLabel,
                data: values,
                backgroundColor: isPie
                    ? this.getChartColors(values.length)
                    : (state.currentType === "line" && state.isStacked
                        ? this.getTransparentColor(widget.data.chartColors?.border || "#5b21b6", 0.5)
                        : widget.data.chartColors?.background || "#7c3aed"),
                borderColor: isPie ? "#ffffff" : widget.data.chartColors?.border || "#5b21b6",
                borderWidth: 2,
                fill: state.currentType === "line" && state.isStacked,
                tension: 0,
                // Đường gấp khúc VUÔNG (stepped) khi widget.data.stepped = true.
                stepped: state.currentType === "line" && widget.data.stepped ? true : false,
                hoverOffset: isPie ? 15 : 0,
                pointStyle: 'circle',
                pointRadius: 3,
                pointHoverRadius: 4,
            };

            // signColor: tô ĐỎ phần nằm DƯỚI trục 0 (âm), giữ màu gốc cho phần
            // dương. Dùng cho biểu đồ Cash Flow. Áp cho cả LINE và BAR.
            if (widget.data.signColor && state.currentType === "line") {
                const baseBorder = widget.data.chartColors?.border || "#5b21b6";
                const negColor = widget.data.negativeColor || "#dc2626";
                // fill (vùng diện tích) CHỈ bật khi đang ở chế độ "area" (isStacked)
                // → nút fa-area-chart bật/tắt được vùng tô. Đường + điểm vẫn đổi
                // màu theo dấu dù tắt area.
                dataset.fill = state.isStacked
                    ? {
                          target: "origin",
                          above: this.getTransparentColor(baseBorder, 0.15),
                          below: this.getTransparentColor(negColor, 0.25),
                      }
                    : false;
                // segment.borderColor (đỏ khi âm) + borderDash (đứt khi dự báo)
                // CÙNG áp trên 1 đoạn → đoạn dự báo-âm sẽ là nét ĐỨT ĐỎ.
                dataset.segment = {
                    borderColor: (ctx) =>
                        (ctx.p0.parsed.y < 0 || ctx.p1.parsed.y < 0) ? negColor : baseBorder,
                    borderDash: (ctx) =>
                        forecastFlags[ctx.p1DataIndex] ? [6, 5] : undefined,
                };
                dataset.pointBackgroundColor = (ctx) =>
                    (ctx.parsed && ctx.parsed.y < 0) ? negColor : baseBorder;
                // Điểm dự báo: viền rỗng để phân biệt với điểm thực tế.
                dataset.pointStyle = (ctx) =>
                    forecastFlags[ctx.dataIndex] ? "rectRot" : "circle";
            }
            // signColor cho BAR: tô từng cột ĐỎ nếu giá trị âm (dùng MẢNG màu —
            // data thuần, không phải hàm → sống sót qua clone). Theo index của
            // `values` đã sort ở trên nên khớp dataset.data.
            else if (widget.data.signColor && state.currentType === "bar") {
                const baseBg = widget.data.chartColors?.background || "#7c3aed";
                const baseBorder = widget.data.chartColors?.border || "#5b21b6";
                const negColor = widget.data.negativeColor || "#dc2626";
                dataset.backgroundColor = values.map((v) => (v < 0 ? negColor : baseBg));
                dataset.borderColor = values.map((v) => (v < 0 ? negColor : baseBorder));
            }

            const chartData = {
                labels: labels,
                datasets: [dataset],
            };

            // Vạch "kỳ hiện tại" (opt-in qua widget.data.currentMarker) cho biểu đồ
            // chuỗi thời gian (vd Cash Flow). Bỏ qua khi đã sort lại theo giá trị.
            const singleOptions = this._getChartOptions(state.currentType, labels, yAxisLabel, xAxisLabel);

            // PIE/DOUGHNUT có nhãn giá trị (widget.data.pieValueLabels): hiện SỐ
            // TIỀN + % trên từng lát (plugin t4DataLabels vẽ arc), legend lên TRÊN,
            // và đổi TIÊU ĐỀ card = "<title>: <tổng>" (vd "Doanh thu: 530 tr" —
            // doanh thu = tổng các lát chi phí + lợi nhuận).
            let pieTitle = widget.data.title;
            if (isPie && widget.data.pieValueLabels) {
                singleOptions.plugins = singleOptions.plugins || {};
                singleOptions.plugins.t4DataLabels = {
                    enabled: true,
                    color: "#ffffff",
                    font: "700 11px sans-serif",
                    format: (v) => ChartUtils.formatVN(v),
                };
                if (singleOptions.plugins.legend) {
                    singleOptions.plugins.legend.position = "top";
                }
                if (widgetData.revenue !== undefined && widgetData.revenue !== null) {
                    pieTitle = `${widget.data.title}: ${ChartUtils.formatVN(widgetData.revenue)}`;
                }
            }

            // Biểu đồ cột NGANG (ranking top-N): widget.data.horizontal=true. Chart.js
            // v4 KHÔNG có type "horizontalBar" — vẫn type 'bar', đổi indexAxis='y'.
            // Hoán trục: GIÁ TRỊ → trục X (format VN, từ 0); DANH MỤC → trục Y (nhãn
            // dài đọc ngang, không cắt 15 ký tự như trục X dọc). y.reverse=true → hạng
            // cao nhất nằm TRÊN cùng (khớp sort 'desc' mặc định). CHỈ áp khi đang là
            // 'bar' (user bấm đổi sang line/pie/doughnut → quay về dọc bình thường).
            if (widget.data.horizontal && state.currentType === "bar") {
                singleOptions.indexAxis = "y";
                // Chừa lề phải cho nhãn giá trị ở đuôi thanh khỏi bị cắt.
                singleOptions.layout = {
                    ...(singleOptions.layout || {}),
                    padding: { ...(singleOptions.layout?.padding || {}), right: 48 },
                };
                const valueTitle = singleOptions.scales?.y?.title || { display: false };
                const catTitle = singleOptions.scales?.x?.title || { display: false };
                singleOptions.scales = {
                    x: {
                        beginAtZero: true,
                        title: valueTitle,
                        ticks: {
                            callback: (v) => ChartUtils.formatVN(v),
                            font: { size: 11 },
                        },
                    },
                    y: {
                        reverse: true,
                        title: catTitle,
                        ticks: {
                            autoSkip: false,
                            font: { size: 11 },
                            callback: function (value) {
                                const label = this.getLabelForValue(value);
                                return label.length > 28 ? label.substring(0, 28) + "…" : label;
                            },
                        },
                    },
                };
                // Nhãn GIÁ TRỊ ở đuôi mỗi thanh (format VN) — vẽ ngang nên dùng plugin
                // chuyên cho hbar (T4DataLabelsPlugin chỉ căn cho cột dọc).
                singleOptions.plugins = singleOptions.plugins || {};
                singleOptions.plugins.t4HBarLabels = {
                    enabled: true,
                    color: "#475569",
                    font: "700 11px sans-serif",
                    format: (v) => ChartUtils.formatVN(v),
                };
                // Ranking 1 chuỗi → ẩn legend thừa (tiêu đề trục giá trị đã đủ nghĩa).
                singleOptions.plugins.legend = { display: false };
            }
            if (widget.data.currentMarker && !state.sortOrder) {
                singleOptions.plugins = singleOptions.plugins || {};
                singleOptions.plugins.t4CurrentMarker = {
                    enabled: true,
                    index: this.currentPeriodIndex(dates),
                    color: widget.data.negativeColor || "#64748b",
                    lineWidth: 2,
                };
            }

            // Trả về config hoàn chỉnh
            return {
                ...widget.data,
                title: pieTitle,
                currentChartType: state.currentType,
                sortOrder: state.sortOrder,
                isStacked: state.isStacked ?? true,
                topLimit: state.topLimit ?? null, // 🆕
                chartData: chartData,
                chartColors: widget.data.chartColors,
                chartOptions: singleOptions,
                // 🆕 Drill-down payload
                dataIds: ids,
                dataDates: dates,
                action: widget.action || null,
                dataSource: widget.dataSource || null,
            };
        }

        return widget.data;
    }

    // -----------------------------------------------------------------
    // Multi-series chart: N chuỗi CÙNG trục y (server trả widgetData.series)
    // -----------------------------------------------------------------
    static _processMultiSeriesDisplayData(widget, widgetData, chartState) {
        const state = chartState || {
            currentType: widget.data.chartType || "bar",
            sortOrder: null,
            isStacked: widget.data.isStacked ?? false,
            topLimit: widget.data.topLimit || null,
        };
        // Pie/doughnut không có nghĩa với N chuỗi thời gian → ép bar.
        const type =
            state.currentType === "pie" || state.currentType === "doughnut"
                ? "bar"
                : state.currentType;

        const labels = (widgetData.labels || []).map((label) => {
            if (typeof label === "object" && label !== null) {
                return label.vi_VN || label.en_US || Object.values(label)[0] || "Unknown";
            }
            return label;
        });
        const dates = widgetData.dates || [];
        const palette = ["#2563eb", "#dc2626", "#16a34a", "#f59e0b", "#a855f7", "#0891b2"];
        const datasets = (widgetData.series || []).map((serie, i) => {
            const bg = serie.backgroundColor || palette[i % palette.length];
            const border = serie.borderColor || bg;
            return {
                label: serie.label || `Chuỗi ${i + 1}`,
                data: serie.values || [],
                backgroundColor:
                    type === "line" ? this.getTransparentColor(border, 0.15) : bg,
                borderColor: border,
                borderWidth: 2,
                fill: false,
                tension: 0,
                pointStyle: "circle",
                pointRadius: 3,
                pointHoverRadius: 4,
            };
        });

        const yAxisLabel = widgetData.suggested_y_label || widget.data.yAxisLabel || "";
        const xAxisLabel = widgetData.suggested_x_label || widget.data.xAxisLabel || "";
        const options = this._getChartOptions(type, labels, yAxisLabel, xAxisLabel);
        // Legend N chuỗi — bấm được để ẩn/hiện từng loại.
        options.plugins = options.plugins || {};
        options.plugins.legend = { display: true, position: "top" };
        // Toggle CỘT CHỒNG qua nút stack có sẵn (bar only).
        if (type === "bar" && options.scales) {
            if (options.scales.x) options.scales.x.stacked = !!state.isStacked;
            if (options.scales.y) options.scales.y.stacked = !!state.isStacked;
        }

        return {
            ...widget.data,
            currentChartType: type,
            sortOrder: null,
            isStacked: state.isStacked ?? false,
            topLimit: state.topLimit ?? null,
            chartData: { labels, datasets },
            chartColors: widget.data.chartColors,
            chartOptions: options,
            dataIds: widgetData.ids || [],
            dataDates: dates,
            action: widget.action || null,
            dataSource: widget.dataSource || null,
        };
    }

    // -----------------------------------------------------------------
    // Dual-bar chart: 2 cột CÙNG 1 trục y (thu dương phía trên / chi âm phía dưới)
    // -----------------------------------------------------------------
    static _processDualBarDisplayData(widget, widgetData, chartState) {
        let labels = (widgetData.labels || []).map((label) => {
            if (typeof label === "object" && label !== null) {
                return label.vi_VN || label.en_US || Object.values(label)[0] || "Unknown";
            }
            return label;
        });
        const valuesLeft = widgetData.values_left || [];
        const valuesRight = widgetData.values_right || [];
        const labelLeft =
            widgetData.suggested_label_left || widget.data.label_left || "Chuỗi 1";
        const labelRight =
            widgetData.suggested_label_right || widget.data.label_right || "Chuỗi 2";

        const leftBg = widget.data.chartColors?.background || "#16a34a";
        const leftBorder = widget.data.chartColors?.border || "#15803d";
        const rightBg = widget.data.chartColorsRight?.background || "#dc2626";
        const rightBorder = widget.data.chartColorsRight?.border || "#b91c1c";

        // Chế độ CỘT CHỒNG (opt-in): server trả thêm values_left_planned /
        // values_right_planned → mỗi cột chồng phần ĐÃ (đặc, dưới) + SẼ (nhạt,
        // trên). Không có 2 mảng này → giữ nguyên dual-bar 2 dataset như cũ
        // (không phá các dashboard khác đang dùng chartType="dual_bar").
        const hasPlanned = !!(
            widgetData.values_left_planned || widgetData.values_right_planned
        );

        let datasets;
        if (hasPlanned) {
            const valuesLeftPlanned = widgetData.values_left_planned || [];
            const valuesRightPlanned = widgetData.values_right_planned || [];
            const labelLeftPlanned =
                widgetData.label_left_planned || widget.data.label_left_planned || "Sẽ thu";
            const labelRightPlanned =
                widgetData.label_right_planned || widget.data.label_right_planned || "Sẽ chi";
            datasets = [
                {
                    type: "bar", label: labelLeft, data: valuesLeft, stack: "thu",
                    backgroundColor: leftBg, borderColor: leftBorder, borderWidth: 1,
                },
                {
                    type: "bar", label: labelLeftPlanned, data: valuesLeftPlanned, stack: "thu",
                    backgroundColor: ChartUtils.getTransparentColor(leftBg, 0.35),
                    borderColor: leftBorder, borderWidth: 1, borderDash: [4, 3],
                },
                {
                    type: "bar", label: labelRight, data: valuesRight, stack: "chi",
                    backgroundColor: rightBg, borderColor: rightBorder, borderWidth: 1,
                },
                {
                    type: "bar", label: labelRightPlanned, data: valuesRightPlanned, stack: "chi",
                    backgroundColor: ChartUtils.getTransparentColor(rightBg, 0.35),
                    borderColor: rightBorder, borderWidth: 1, borderDash: [4, 3],
                },
            ];
        } else {
            datasets = [
                {
                    type: "bar", label: labelLeft, data: valuesLeft,
                    backgroundColor: leftBg, borderColor: leftBorder, borderWidth: 1,
                },
                {
                    type: "bar", label: labelRight, data: valuesRight,
                    backgroundColor: rightBg, borderColor: rightBorder, borderWidth: 1,
                },
            ];
        }

        // Bật nhãn GIÁ TRỊ trên đầu mỗi cột (số tiền) — format kiểu VN.
        const dualOptions = this._getChartOptions(
            "bar", labels, widget.data.yAxisLabel || "", widget.data.xAxisLabel || ""
        );
        dualOptions.plugins = {
            ...(dualOptions.plugins || {}),
            // stackMode → 1 nhãn TỔNG (đã+sẽ) mỗi cột; thường → nhãn từng cột.
            t4DataLabels: {
                enabled: true, stackMode: hasPlanned, format: (v) => ChartUtils.formatVN(v),
            },
        };
        // Cột chồng cần bật stacked cho CẢ 2 trục (thiếu 1 → vẽ 4 cột rời).
        if (hasPlanned) {
            dualOptions.scales = dualOptions.scales || {};
            dualOptions.scales.x = { ...(dualOptions.scales.x || {}), stacked: true };
            dualOptions.scales.y = { ...(dualOptions.scales.y || {}), stacked: true };
        }
        // Chừa khoảng trên để nhãn số trên đỉnh cột cao nhất không bị cắt.
        dualOptions.layout = { ...(dualOptions.layout || {}), padding: { top: 20 } };
        // Vạch "kỳ hiện tại" (opt-in qua widget.data.currentMarker).
        if (widget.data.currentMarker) {
            dualOptions.plugins.t4CurrentMarker = {
                enabled: true,
                index: this.currentPeriodIndex(widgetData.dates || []),
                color: "#64748b",
                lineWidth: 2,
            };
        }

        return {
            ...widget.data,
            currentChartType: "bar",
            // isCombo: true → template ẩn nút đổi loại/sort (data nhiều dataset).
            isCombo: true,
            sortOrder: null,
            isStacked: hasPlanned,
            chartData: { labels, datasets },
            chartColors: widget.data.chartColors,
            chartOptions: dualOptions,
            dataIds: widgetData.ids || [],
            dataDates: widgetData.dates || [],
            action: widget.action || null,
            dataSource: widget.dataSource || null,
        };
    }

    // -----------------------------------------------------------------
    // Tạo CanvasPattern sọc chéo (tô gạch) màu `color` trên nền tint nhạt.
    // Dùng cho phần "chưa thu/chi" của stacked_bar. Trả PATTERN (object opaque)
    // → khi đặt trong MẢNG backgroundColor sẽ sống sót qua cloneChartData
    // (Array.slice giữ reference; đặt trực tiếp object sẽ bị copy nông mất pattern).
    // -----------------------------------------------------------------
    static _makeStripePattern(color) {
        const size = 8;
        const canvas = document.createElement("canvas");
        canvas.width = size;
        canvas.height = size;
        const c = canvas.getContext("2d");
        if (!c) {
            return color;
        }
        // Nền tint nhạt của màu category để cột vẫn nhận ra Thu (xanh)/Chi (đỏ).
        let tint = color;
        try {
            tint = this.getTransparentColor(color, 0.12);
        } catch (_e) {
            tint = "rgba(0,0,0,0.06)";
        }
        c.fillStyle = tint;
        c.fillRect(0, 0, size, size);
        // Sọc chéo màu category (vẽ lặp để phủ kín ô khi repeat).
        c.strokeStyle = color;
        c.lineWidth = 1.5;
        c.beginPath();
        c.moveTo(0, size);
        c.lineTo(size, 0);
        c.moveTo(-size / 2, size / 2);
        c.lineTo(size / 2, -size / 2);
        c.moveTo(size / 2, size * 1.5);
        c.lineTo(size * 1.5, size / 2);
        c.stroke();
        return c.createPattern(canvas, "repeat");
    }

    // -----------------------------------------------------------------
    // Grouped-stacked bar: mỗi kỳ (label) có 2 cột CẠNH nhau (stack "thu"/"chi"),
    // mỗi cột chồng "đã" (tô ĐẬM, dưới) + "chưa" (tô GẠCH chéo, trên).
    // 4 dataset: thu_done/thu_remaining (stack "thu", xanh), chi_done/
    // chi_remaining (stack "chi", đỏ). Pattern gạch là HÀM trả CanvasPattern để
    // sống sót qua cloneChartData (đặt object trực tiếp sẽ bị copy nông mất).
    // -----------------------------------------------------------------
    static _processGroupedStackedBarDisplayData(widget, widgetData, chartState) {
        const labels = (widgetData.labels || []).map((label) => {
            if (typeof label === "object" && label !== null) {
                return label.vi_VN || label.en_US || Object.values(label)[0] || "Unknown";
            }
            return label;
        });
        const colorThu = widget.data.chartColors?.background || "#16a34a";
        const colorThuBorder = widget.data.chartColors?.border || "#15803d";
        const colorChi = widget.data.chartColorsRight?.background || "#dc2626";
        const colorChiBorder = widget.data.chartColorsRight?.border || "#b91c1c";
        const thuHatch = this._makeStripePattern(colorThu);
        const chiHatch = this._makeStripePattern(colorChi);

        const lThuDone = widgetData.label_thu_done || "Đã thu";
        const lThuRem = widgetData.label_thu_remaining || "Chưa thu";
        const lChiDone = widgetData.label_chi_done || "Đã chi";
        const lChiRem = widgetData.label_chi_remaining || "Chưa chi";

        // Pattern theo cảm nhận, phần "chắc/nặng" để tô ĐẶC:
        //  - THU: "đã thu" (thu rồi → chắc chắn) = ĐẶC; "chưa thu" = gạch chéo.
        //  - CHI: "đã chi" (xong → nhẹ) = gạch chéo; "chưa chi" (nợ → nặng) = ĐẶC.
        // Pattern là HÀM trả CanvasPattern (sống sót cloneChartData — KHÔNG đặt
        // object trực tiếp).
        const datasets = [
            {
                type: "bar", label: lThuDone, data: widgetData.values_thu_done || [],
                backgroundColor: colorThu, borderColor: colorThuBorder,
                borderWidth: 1, stack: "thu",
            },
            {
                type: "bar", label: lThuRem, data: widgetData.values_thu_remaining || [],
                backgroundColor: () => thuHatch, borderColor: colorThuBorder,
                borderWidth: 1, stack: "thu",
            },
            {
                type: "bar", label: lChiDone, data: widgetData.values_chi_done || [],
                backgroundColor: () => chiHatch, borderColor: colorChiBorder,
                borderWidth: 1, stack: "chi",
            },
            {
                type: "bar", label: lChiRem, data: widgetData.values_chi_remaining || [],
                backgroundColor: colorChi, borderColor: colorChiBorder,
                borderWidth: 1, stack: "chi",
            },
        ];

        const opts = this._getChartOptions(
            "bar", labels, widget.data.yAxisLabel || "", widget.data.xAxisLabel || ""
        );
        // STACK cả 2 trục: dataset cùng `stack` chồng lên nhau; khác `stack` → cột
        // cạnh nhau (Thu | Chi) trong mỗi kỳ.
        opts.scales.x.stacked = true;
        opts.scales.y.stacked = true;
        opts.scales.y.beginAtZero = true;
        opts.plugins = opts.plugins || {};
        // KHÔNG vẽ nhãn số trên cột: với 12 kỳ × 4 series (cột mảnh, sát nhau)
        // nhãn chồng lên nhau → rối. Dùng TOOLTIP khi hover để đọc giá trị.
        opts.plugins.t4DataLabels = { enabled: false };
        // Legend 4 mục (đậm/gạch × thu/chi) — BẤM ĐƯỢC để ẩn/hiện từng series.
        // Hữu ích khi 1 series quá lớn (vd "Chưa chi") che các series nhỏ: ẩn nó
        // đi để xem chi tiết phần còn lại.
        // Swatch khớp dataset: THU (đã=đặc, chưa=gạch), CHI (đã=gạch, chưa=đặc).
        const _legendItems = [
            { text: lThuDone, fillStyle: colorThu, strokeStyle: colorThuBorder, datasetIndex: 0 },
            { text: lThuRem, fillStyle: thuHatch, strokeStyle: colorThuBorder, datasetIndex: 1 },
            { text: lChiDone, fillStyle: chiHatch, strokeStyle: colorChiBorder, datasetIndex: 2 },
            { text: lChiRem, fillStyle: colorChi, strokeStyle: colorChiBorder, datasetIndex: 3 },
        ];
        opts.plugins.legend = {
            position: "top",
            onClick: (e, legendItem, legend) => {
                const ci = legend.chart;
                const idx = legendItem.datasetIndex;
                if (idx == null) {
                    return;
                }
                ci.setDatasetVisibility(idx, !ci.isDatasetVisible(idx));
                ci.update();
            },
            labels: {
                usePointStyle: false,
                generateLabels: (chart) => {
                    // Item từ generateLabels KHÔNG tự kế thừa Chart.defaults.color
                    // → phải set fontColor cho từng item. Lấy màu ĐÃ RESOLVE của
                    // chart (= Chart.defaults.color đặt từ canvas → sáng ở dark mode).
                    const c = (chart.options && chart.options.color) || "#374151";
                    return _legendItems.map((it) => ({
                        ...it,
                        fontColor: c,
                        lineWidth: 1,
                        // Gạch ngang khi series đang ẩn (UX chuẩn của Chart.js).
                        hidden: !chart.isDatasetVisible(it.datasetIndex),
                    }));
                },
            },
        };
        // Tooltip GỘP theo kỳ: hover 1 cột hiện CẢ "đã" lẫn "chưa" (mode index),
        // ẩn dòng giá trị = 0 cho gọn.
        opts.interaction = { mode: "index", intersect: false };
        opts.plugins.tooltip = {
            mode: "index",
            intersect: false,
            filter: (item) => item.parsed.y != null && item.parsed.y !== 0,
            callbacks: {
                label: (ctx) => `${ctx.dataset.label || ""}: ${ChartUtils.formatVN(ctx.parsed.y)}`,
            },
        };
        // Vạch "kỳ hiện tại" (opt-in qua widget.data.currentMarker).
        if (widget.data.currentMarker) {
            opts.plugins.t4CurrentMarker = {
                enabled: true,
                index: this.currentPeriodIndex(widgetData.dates || []),
                color: "#64748b",
                lineWidth: 2,
            };
        }

        return {
            ...widget.data,
            currentChartType: "bar",
            isCombo: true,
            sortOrder: null,
            isStacked: true,
            chartData: { labels, datasets },
            chartColors: widget.data.chartColors,
            chartOptions: opts,
            dataIds: widgetData.ids || [],
            dataDates: widgetData.dates || [],
            action: widget.action || null,
            dataSource: widget.dataSource || null,
        };
    }

    // -----------------------------------------------------------------
    // stat_chart: thẻ KPI nâng cấp = value (trái) + biểu đồ cột mini (phải).
    // Server trả {value, labels, values, dates, ids?}. Cột tô trắng-mờ để
    // nổi trên nền card màu; click cột → drill-down (ChartComponent dùng dates).
    // -----------------------------------------------------------------
    static _processStatChartDisplayData(widget, widgetData) {
        const textColor = widget.data.textColor || "#ffffff";
        let formatted = { formatted: widgetData.value, original: null };
        if (typeof widgetData.value === "number") {
            formatted = this.formatNumber(widgetData.value);
        }
        const labels = (widgetData.labels || []).map((label) => {
            if (typeof label === "object" && label !== null) {
                return label.vi_VN || label.en_US || Object.values(label)[0] || "Unknown";
            }
            return label;
        });
        const values = widgetData.values || [];
        const dates = widgetData.dates || [];
        const barColor = widget.data.chartColors?.background || "rgba(255,255,255,0.85)";
        const chartData = {
            labels,
            datasets: [
                {
                    type: "bar",
                    label: widget.data.title || "",
                    data: values,
                    backgroundColor: barColor,
                    borderColor: barColor,
                    borderWidth: 0,
                    borderRadius: 3,
                    maxBarThickness: 28,
                },
            ],
        };
        return {
            ...widget.data,
            value: formatted.formatted,
            originalValue: widget.data.showOriginalValue === false
                ? null : formatted.original,
            title: widget.data.title,
            help: widget.data.help,
            icon: widget.data.icon,
            bgColor: widget.data.bgColor,
            textColor,
            chartData,
            chartOptions: this._getMiniBarOptions(textColor, this.currentPeriodIndex(dates)),
            // Nhiều cột → đặt bề rộng tối thiểu để cuộn NGANG (mỗi cột ~52px);
            // ít cột (≤6) → 0 = vừa khít khung. Template bọc trong vùng overflow-x.
            chartMinWidth: labels.length > 6 ? labels.length * 52 : 0,
            dataDates: widgetData.dates || [],
            dataIds: widgetData.ids || [],
            action: widget.action || null,
            dataSource: widget.dataSource || null,
        };
    }

    // Options cho biểu đồ cột MINI trong stat_chart: không legend/tiêu đề,
    // tick trục X màu chữ card, ẩn trục Y, tooltip + NHÃN số trên cột (format VN).
    static _getMiniBarOptions(textColor, currentIndex = -1) {
        return {
            responsive: true,
            maintainAspectRatio: false,
            // Chừa trên để nhãn số trên đỉnh cột không bị cắt.
            layout: { padding: { top: 18 } },
            plugins: {
                legend: { display: false },
                tooltip: {
                    callbacks: {
                        label: (ctx) => ChartUtils.formatVN(ctx.parsed.y),
                    },
                },
                // Số tiền trên đầu mỗi cột, màu theo chữ card (trắng).
                t4DataLabels: {
                    enabled: true,
                    color: textColor,
                    font: "700 10px sans-serif",
                    format: (v) => ChartUtils.formatVN(v),
                },
                // Vạch dọc tại kỳ hiện tại + auto-scroll về giữa (component lo cuộn).
                t4CurrentMarker: {
                    enabled: currentIndex >= 0,
                    index: currentIndex,
                    color: textColor,
                    lineWidth: 2,
                },
            },
            scales: {
                x: {
                    grid: { display: false },
                    border: { display: false },
                    ticks: { color: textColor, font: { size: 10 } },
                },
                y: {
                    display: false,
                    beginAtZero: true,
                    grid: { display: false },
                },
            },
        };
    }

    // -----------------------------------------------------------------
    // Combo (dual-axis) chart: bar yLeft + line yRight
    // -----------------------------------------------------------------
    static _processComboDisplayData(widget, widgetData, chartState) {
        const state = chartState || {
            currentType: "combo",
            sortOrder: null,
            isStacked: false,
            topLimit: widget.data.topLimit || null,
        };

        let labels = widgetData.labels || [];
        let valuesLeft = widgetData.values_left || [];
        let valuesRight = widgetData.values_right || [];
        let ids = widgetData.ids || [];
        let dates = widgetData.dates || [];

        // Label fallback chain: server suggested → widget config → default
        const labelLeft =
            widgetData.suggested_label_left ||
            widget.data.label_left ||
            "Trục Trái";
        const labelRight =
            widgetData.suggested_label_right ||
            widget.data.label_right ||
            "Trục Phải";
        const xLabel =
            widgetData.suggested_x_label || widget.data.xAxisLabel || "";

        // Resolve label đa ngôn ngữ (giống single-axis)
        labels = labels.map((label) => {
            if (typeof label === "object" && label !== null) {
                return label.vi_VN || label.en_US || Object.values(label)[0] || "Unknown";
            }
            return label;
        });

        // Sort by left value (desc) khi user toggle. Combo widget không
        // hiển thị sort UI ở v1, nhưng giữ logic sẵn cho v2.
        if (state.sortOrder === "asc" || state.sortOrder === "desc") {
            const combined = labels.map((label, idx) => ({
                label,
                left: valuesLeft[idx] ?? 0,
                right: valuesRight[idx] ?? 0,
                id: ids[idx] !== undefined ? ids[idx] : null,
                date: dates[idx] !== undefined ? dates[idx] : null,
            }));
            combined.sort((a, b) =>
                state.sortOrder === "asc" ? a.left - b.left : b.left - a.left,
            );
            labels = combined.map((c) => c.label);
            valuesLeft = combined.map((c) => c.left);
            valuesRight = combined.map((c) => c.right);
            ids = combined.map((c) => c.id);
            dates = combined.map((c) => c.date);
        }

        const colorLeftBg =
            widget.data.chartColors?.background || "#7c3aed";
        const colorLeftBorder =
            widget.data.chartColors?.border || "#5b21b6";
        const colorRightBg =
            widget.data.chartColorsRight?.background || "#0ea5e9";
        const colorRightBorder =
            widget.data.chartColorsRight?.border || "#0369a1";

        const datasets = [
            {
                type: "bar",
                label: labelLeft,
                data: valuesLeft,
                backgroundColor: colorLeftBg,
                borderColor: colorLeftBorder,
                borderWidth: 1,
                yAxisID: "yLeft",
                order: 2,
            },
            {
                type: "line",
                label: labelRight,
                data: valuesRight,
                backgroundColor: this.getTransparentColor(colorRightBorder, 0.2),
                borderColor: colorRightBorder,
                borderWidth: 2,
                fill: false,
                tension: 0.3,
                pointStyle: "circle",
                pointRadius: 4,
                pointHoverRadius: 6,
                yAxisID: "yRight",
                order: 1,
            },
        ];

        const chartData = { labels, datasets };

        return {
            ...widget.data,
            // Combo dùng base type 'bar'; mỗi dataset tự khai báo type riêng.
            currentChartType: "bar",
            // Disable UI toggle cho combo (xử lý ở template qua chartType === 'combo')
            isCombo: true,
            sortOrder: state.sortOrder ?? null,
            isStacked: false,
            topLimit: state.topLimit ?? null,
            chartData,
            chartColors: widget.data.chartColors,
            chartOptions: this._getComboChartOptions(
                labels,
                labelLeft,
                labelRight,
                xLabel,
            ),
            dataIds: ids,
            dataDates: dates,
            action: widget.action || null,
            dataSource: widget.dataSource || null,
        };
    }

    static _getComboChartOptions(labels, labelLeft, labelRight, xLabel) {
        let bodyFont = "inherit";
        try {
            const rootStyle = window.getComputedStyle(document.documentElement);
            const t4Font = rootStyle.getPropertyValue("--t4-font-family");
            if (t4Font) {
                bodyFont = t4Font.trim();
            } else {
                bodyFont = window.getComputedStyle(document.body).fontFamily || "inherit";
            }
            if (bodyFont && bodyFont.includes("var(")) {
                bodyFont = "inherit";
            }
        } catch (e) {}

        const shouldRotate = labels.length > 5;
        return {
            responsive: true,
            maintainAspectRatio: false,
            animation: { duration: 1000, easing: "easeInOutQuart" },
            interaction: { mode: "index", intersect: false },
            plugins: {
                legend: {
                    position: "top",
                    // NO-OP onClick: chặn toggle dataset mặc định của Chart.js gây
                    // lỗi "Cannot convert object to primitive value" khi click legend
                    // trên combo (2 dataset khác trục). Giữ nguyên hiển thị.
                    onClick: () => {},
                    labels: {
                        font: { size: 12, family: bodyFont },
                        usePointStyle: true,
                    },
                },
                tooltip: {
                    bodyFont: { family: bodyFont },
                    titleFont: { family: bodyFont },
                    callbacks: {
                        label(context) {
                            const dsLabel = context.dataset.label || "";
                            const value = context.parsed.y;
                            if (value === null || value === undefined) return "";
                            return `${dsLabel}: ${value.toLocaleString("vi-VN")}`;
                        },
                    },
                },
            },
            scales: {
                x: {
                    title: {
                        display: !!xLabel,
                        text: xLabel,
                        font: { size: 13, weight: "bold", family: bodyFont },
                    },
                    ticks: {
                        maxRotation: shouldRotate ? 45 : 0,
                        minRotation: 0,
                        font: { size: 11, family: bodyFont },
                        callback(value) {
                            const label = this.getLabelForValue(value);
                            return label.length > 15 ? label.substring(0, 15) + "..." : label;
                        },
                    },
                },
                yLeft: {
                    type: "linear",
                    position: "left",
                    title: {
                        display: !!labelLeft,
                        text: labelLeft,
                        font: { size: 13, weight: "bold", family: bodyFont },
                    },
                    ticks: {
                        callback: (v) => ChartUtils.formatVN(v),
                        font: { size: 11, family: bodyFont },
                    },
                    beginAtZero: true,
                },
                yRight: {
                    type: "linear",
                    position: "right",
                    title: {
                        display: !!labelRight,
                        text: labelRight,
                        font: { size: 13, weight: "bold", family: bodyFont },
                    },
                    ticks: {
                        callback: (v) => ChartUtils.formatVN(v),
                        font: { size: 11, family: bodyFont },
                    },
                    beginAtZero: true,
                    grid: { drawOnChartArea: false },
                },
            },
        };
    }

    // Helper tạo Options cho ChartJS
    static _getChartOptions(type, labels, yLabel, xLabel) {
        let bodyFont = 'inherit';
        try {
            const rootStyle = window.getComputedStyle(document.documentElement);
            const t4Font = rootStyle.getPropertyValue('--t4-font-family');
            if (t4Font) {
                bodyFont = t4Font.trim();
            } else {
                bodyFont = window.getComputedStyle(document.body).fontFamily || 'inherit';
            }
            if (bodyFont && bodyFont.includes('var(')) {
                bodyFont = 'inherit';
            }
        } catch (e) {}

        const isPie = type === "pie" || type === "doughnut";

        if (isPie) {
            return {
                responsive: true,
                maintainAspectRatio: false,
                animation: { duration: 1000, easing: 'easeInOutQuart', animateRotate: true, animateScale: true },
                plugins: {
                    legend: { position: "right", align: "center", labels: { boxWidth: 15, padding: 15, font: { size: 12, family: bodyFont }, usePointStyle: true } },
                    tooltip: {
                        bodyFont: { family: bodyFont },
                        titleFont: { family: bodyFont },
                        callbacks: {
                            label: function (context) {
                                let label = context.label || "";
                                if (label) label += ": ";
                                if (context.parsed !== null) {
                                    label += context.parsed.toLocaleString();
                                    const total = context.dataset.data.reduce((a, b) => a + b, 0);
                                    const percentage = ((context.parsed / total) * 100).toFixed(1);
                                    label += ` (${percentage}%)`;
                                }
                                return label;
                            },
                        },
                    },
                },
            };
        }

        // Options cho Bar/Line
        const shouldRotate = labels.length > 5;
        return {
            responsive: true,
            maintainAspectRatio: false,
            animation: { duration: 1000, easing: 'easeInOutQuart' },
            plugins: {
                legend: { position: "top", labels: { font: { size: 12, family: bodyFont }, usePointStyle: true } },
                tooltip: {
                    bodyFont: { family: bodyFont },
                    titleFont: { family: bodyFont },
                    callbacks: {
                        label: function (context) {
                            const tooltipLabel = yLabel || context.dataset.label || "";
                            return context.parsed.y !== null
                                ? (tooltipLabel ? `${tooltipLabel}: ${context.parsed.y.toLocaleString()}` : context.parsed.y.toLocaleString())
                                : "";
                        },
                    },
                },
            },
            scales: {
                x: {
                    title: { display: !!xLabel, text: xLabel, font: { size: 13, weight: "bold", family: bodyFont } },
                    ticks: {
                        maxRotation: shouldRotate ? 45 : 0,
                        minRotation: 0,
                        font: { size: 11, family: bodyFont },
                        callback: function (value) {
                            const label = this.getLabelForValue(value);
                            return label.length > 15 ? label.substring(0, 15) + "..." : label;
                        },
                    },
                },
                y: {
                    title: { display: !!yLabel, text: yLabel, font: { size: 13, weight: "bold", family: bodyFont } },
                    ticks: { callback: (value) => ChartUtils.formatVN(value), font: { size: 11, family: bodyFont } },
                    beginAtZero: true,
                },
            },
        };
    }
}
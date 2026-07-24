/** @odoo-module **/

import { Component, useState, onWillUpdateProps, useRef, onMounted } from "@odoo/owl";
import { HelpTooltip } from "../help_tooltip/help_tooltip";

export class StatsSummary extends Component {
    static template = "t4_custom_dashboard.StatsSummary";
    static components = { HelpTooltip };
    static props = {
        "*": { optional: true },
    };

    setup() {
        this.valueRef = useRef("valueElement");
        this.state = useState({
            displayValue: this.props.value,
            originalValue: this.props.originalValue,
            isAnimating: false,
        });

        this.previousValue = this.parseValue(this.props.value);
        onMounted(() => {
            // Initial animation
            this.animateValue(0, this.previousValue, 1000);
        });

        onWillUpdateProps((nextProps) => {
            // Animate khi value thay đổi
            if (nextProps.value !== this.props.value) {
                const oldValue = this.parseValue(this.props.value);
                const newValue = this.parseValue(nextProps.value);
                if (!isNaN(oldValue) && !isNaN(newValue)) {
                    this.animateValue(oldValue, newValue, 800);
                    this.previousValue = newValue;
                } else {
                    // Nếu không phải số, chỉ fade in/out
                    this.fadeTransition(nextProps.value, nextProps.originalValue);
                }
            } else if (nextProps.originalValue !== this.props.originalValue) {
                this.state.originalValue = nextProps.originalValue;
            }
        });
    }

    parseValue(value) {
        if (typeof value === 'number') {
            return value;
        }

        if (typeof value === 'string') {
            // Parse số đã viết tắt kiểu VN ("1.5 tr", "2.3 tỷ", "850 N") + legacy K/M/B.
            let mult = 1;
            if (/tỷ/.test(value)) mult = 1e9;
            else if (/tr/.test(value)) mult = 1e6;
            else if (/N/.test(value)) mult = 1e3;
            else if (value.includes('B')) mult = 1e9;
            else if (value.includes('M')) mult = 1e6;
            else if (value.includes('K')) mult = 1e3;

            const parsed = parseFloat(value.replace(/[^0-9.-]/g, ''));
            return isNaN(parsed) ? 0 : parsed * mult;
        }

        return 0;
    }

    formatValue(value) {
        if (typeof value !== "number") return { formatted: value, original: null };

        const sign = value < 0 ? "-" : "";
        const n = Math.abs(value);
        let formatted;
        if (n >= 1e9) formatted = `${sign}${parseFloat((n / 1e9).toFixed(2))} tỷ`;
        else if (n >= 1e6) formatted = `${sign}${parseFloat((n / 1e6).toFixed(1))} tr`;
        else if (n >= 1e3) formatted = `${sign}${Math.round(n / 1e3)} N`;
        else formatted = value.toLocaleString('vi-VN', { maximumFractionDigits: 0 });

        const hasFormatting = /tỷ|tr|N/.test(formatted);
        return {
            formatted: formatted,
            original: hasFormatting ? value.toLocaleString('vi-VN', { maximumFractionDigits: 0 }) : null
        };
    }

    animateValue(start, end, duration) {
        this.state.isAnimating = true;
        const startTime = performance.now();
        const difference = end - start;

        const easeOutQuart = (t) => 1 - Math.pow(1 - t, 4);

        const animate = (currentTime) => {
            const elapsed = currentTime - startTime;
            const progress = Math.min(elapsed / duration, 1);
            const easedProgress = easeOutQuart(progress);

            const currentValue = start + (difference * easedProgress);
            const formatted = this.formatValue(currentValue);

            this.state.displayValue = formatted.formatted;
            this.state.originalValue = formatted.original;

            if (progress < 1) {
                requestAnimationFrame(animate);
            } else {
                this.state.isAnimating = false;
                const finalFormatted = this.formatValue(end);
                this.state.displayValue = finalFormatted.formatted;
                this.state.originalValue = finalFormatted.original;
            }
        };

        requestAnimationFrame(animate);
    }

    fadeTransition(newValue, newOriginalValue) {
        this.state.isAnimating = true;

        // Fade out
        setTimeout(() => {
            this.state.displayValue = newValue;
            if (newOriginalValue !== undefined) {
                this.state.originalValue = newOriginalValue;
            }
            // Fade in
            setTimeout(() => {
                this.state.isAnimating = false;
            }, 150);
        }, 150);
    }

    get backgroundColor() {
        return this.props.bgColor || '#7c3aed';
    }

    get textColor() {
        return this.props.textColor || '#ffffff';
    }

    get gradientStyle() {
        const bgColor = this.backgroundColor;
        const darkerColor = this.darkenColor(bgColor, 20);
        return `background: linear-gradient(135deg, ${bgColor} 0%, ${darkerColor} 100%);`;
    }

    darkenColor(hex, percent) {
        hex = hex.replace('#', '');
        const num = parseInt(hex, 16);
        const amt = Math.round(2.55 * percent);
        const R = Math.max(0, Math.min(255, (num >> 16) - amt));
        const G = Math.max(0, Math.min(255, ((num >> 8) & 0x00FF) - amt));
        const B = Math.max(0, Math.min(255, (num & 0x0000FF) - amt));

        return '#' + (0x1000000 + R * 0x10000 + G * 0x100 + B).toString(16).slice(1);
    }

    get trendClass() {
        if (this.props.trend === 'up') return 'trend-up';
        if (this.props.trend === 'down') return 'trend-down';
        return 'trend-neutral';
    }

    get trendIcon() {
        if (this.props.trend === 'up') return 'fa-arrow-up';
        if (this.props.trend === 'down') return 'fa-arrow-down';
        return 'fa-minus';
    }
}
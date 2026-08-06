/** @odoo-module **/

export function playScanTone(success) {
    try {
        const AudioContext = window.AudioContext || window.webkitAudioContext;
        if (!AudioContext) {
            return;
        }
        const context = new AudioContext();
        const oscillator = context.createOscillator();
        const gain = context.createGain();
        oscillator.type = "sine";
        oscillator.frequency.value = success ? 880 : 220;
        gain.gain.setValueAtTime(0.0001, context.currentTime);
        gain.gain.exponentialRampToValueAtTime(0.06, context.currentTime + 0.01);
        gain.gain.exponentialRampToValueAtTime(0.0001, context.currentTime + 0.11);
        oscillator.connect(gain);
        gain.connect(context.destination);
        oscillator.start();
        oscillator.stop(context.currentTime + 0.12);
        oscillator.addEventListener("ended", () => context.close());
    } catch {
        // Visual feedback remains authoritative when browser audio is blocked.
    }
}

export function refocusAndSelect(inputRef) {
    window.setTimeout(() => {
        inputRef.el?.focus();
        inputRef.el?.select();
    }, 0);
}

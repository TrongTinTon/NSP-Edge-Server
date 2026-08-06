/** @odoo-module **/

/**
 * Return the canonical representation used by the RFID whitelist backend.
 *
 * Rules:
 * - Unicode NFKC normalization (full-width scanner characters become ASCII)
 * - trim surrounding whitespace
 * - uppercase
 * - remove one leading 0x prefix
 * - remove whitespace and common visual separators: colon, underscore, hyphen
 */
export function normalizeRfidTid(value) {
    let tid = String(value || "").normalize("NFKC").trim().toUpperCase();
    if (tid.startsWith("0X")) {
        tid = tid.slice(2);
    }
    return tid.replace(/[\s:_\-\u200B-\u200D\uFEFF]+/g, "");
}

/** How a Transfer Call tool's number is sent to the telephony provider.
 * Mirrors api/services/telephony/number_format.py so the "Will dial" preview
 * matches what is sent. */

export type TransferNumberFormat = "auto" | "keep_zero" | "with_91" | "with_plus_91" | "as_typed";

export const TRANSFER_NUMBER_FORMATS: { value: TransferNumberFormat; label: string; hint: string }[] = [
    { value: "auto", label: "Automatic", hint: "The provider's own handling. VoiceLink sends 10 digits (mobiles)." },
    { value: "keep_zero", label: "Keep 0 prefix", hint: "0 + 10 digits, e.g. landlines like 080…" },
    { value: "with_91", label: "With 91", hint: "91 + 10 digits" },
    { value: "with_plus_91", label: "With +91", hint: "+91 + 10 digits" },
    { value: "as_typed", label: "Exactly as typed", hint: "Toll-free (1800…), extensions, special numbers" },
];

const SEPARATORS = /[\s\-().]/g;
const PHONE_LIKE = /^\+?\d+$/;

function national(digits: string): string {
    if (digits.length === 12 && digits.startsWith("91")) return digits.slice(2);
    if (digits.length === 13 && digits.startsWith("091")) return digits.slice(3);
    if (digits.length === 11 && digits.startsWith("0")) return digits.slice(1);
    return digits;
}

/** VoiceLink's own reduction ("auto"): the bare 10-digit form. */
function voicelinkAuto(cleaned: string): string {
    const digits = cleaned.replace(/\D/g, "");
    if (digits.length === 12 && digits.startsWith("91")) return digits.slice(2);
    if (digits.length === 11 && digits.startsWith("0")) return digits.slice(1);
    return digits;
}

/** The number VoiceLink will dial, or null when it isn't a plain phone number
 * (SIP endpoint, template) so there's nothing to preview. */
export function previewTransferNumber(destination: string, format: TransferNumberFormat): string | null {
    const cleaned = (destination || "").trim().replace(SEPARATORS, "");
    if (!PHONE_LIKE.test(cleaned)) return null;
    if (format === "auto") return voicelinkAuto(cleaned) || null;
    if (format === "as_typed") return cleaned;
    const core = national(cleaned.replace(/^\+/, ""));
    if (core.length !== 10) return cleaned;
    if (format === "keep_zero") return `0${core}`;
    if (format === "with_91") return `91${core}`;
    return `+91${core}`;
}

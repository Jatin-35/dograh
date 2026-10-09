import { describe, expect, it } from "vitest";

import { previewTransferNumber, type TransferNumberFormat } from "./transferNumberFormat";

// Same cases as api/tests/test_transfer_number_format.py, plus "auto", which
// here previews VoiceLink's own 10-digit reduction.
const CASES: [string, TransferNumberFormat, string | null][] = [
    ["08043061549", "auto", "8043061549"],
    ["08043061549", "keep_zero", "08043061549"],
    ["8043061549", "keep_zero", "08043061549"],
    ["+91 80 4306 1549", "keep_zero", "08043061549"],
    ["918043061549", "keep_zero", "08043061549"],
    ["08043061549", "with_91", "918043061549"],
    ["08043061549", "with_plus_91", "+918043061549"],
    ["080-4306-1549", "as_typed", "08043061549"],
    ["9876543210", "with_plus_91", "+919876543210"],
    ["+919876543210", "keep_zero", "09876543210"],
    ["18002026666", "keep_zero", "18002026666"],
    ["18002026666", "as_typed", "18002026666"],
    ["PJSIP/1234", "keep_zero", null],
    ["{{initial_context.transfer_destination}}", "keep_zero", null],
];

describe("previewTransferNumber", () => {
    it.each(CASES)("%s with %s → %s", (typed, format, expected) => {
        expect(previewTransferNumber(typed, format)).toBe(expected);
    });
});

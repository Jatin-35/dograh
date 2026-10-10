"use client";

import { Plus, Trash2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { cleanValueMaps } from "@/lib/webhookSync";

type ValueMaps = Record<string, Record<string, string>>;
interface Row {
    from: string;
    to: string;
}
interface Table {
    field: string;
    rows: Row[];
}

const MAX_TABLES = 20;
const MAX_ROWS = 200;

function toTables(maps: ValueMaps | undefined): Table[] {
    return Object.entries(maps ?? {}).map(([field, rules]) => ({
        field,
        rows: Object.entries(rules).map(([from, to]) => ({ from, to })),
    }));
}

function toMaps(tables: Table[]): ValueMaps {
    const maps: ValueMaps = {};
    for (const table of tables) {
        maps[table.field] = { ...(maps[table.field] ?? {}) };
        for (const row of table.rows) maps[table.field][row.from] = row.to;
    }
    return cleanValueMaps(maps);
}

/** Rows from text copied out of a spreadsheet: one row per line, the two
 *  columns separated by a tab (or a comma when there is no tab). */
export function parsePastedRows(text: string): Row[] {
    return text
        .split(/\r?\n/)
        .map((line) => {
            const parts = line.includes("\t") ? line.split("\t") : line.split(",");
            return { from: (parts[0] ?? "").trim(), to: (parts[1] ?? "").trim() };
        })
        .filter((row) => row.from || row.to);
}

interface ValueMapsCardProps {
    value: ValueMaps | undefined;
    onChange: (maps: ValueMaps) => void;
    /** Variable names from the current preview, offered as field names. */
    fieldSuggestions: string[];
    /** The value each field currently resolves to in the preview. */
    previewValue: (field: string) => string | undefined;
}

export function ValueMapsCard({ value, onChange, fieldSuggestions, previewValue }: ValueMapsCardProps) {
    // Rows are edited locally so a half-typed row (blank on one side) or two
    // rows that briefly share a value don't vanish; the parent gets only
    // complete rows.
    const [tables, setTables] = useState<Table[]>(() => toTables(value));
    const emitted = useRef(JSON.stringify(cleanValueMaps(value)));

    // Follow outside changes (Discard, a reload), not our own edits.
    useEffect(() => {
        const incoming = JSON.stringify(cleanValueMaps(value));
        if (incoming !== emitted.current) {
            emitted.current = incoming;
            setTables(toTables(value));
        }
    }, [value]);

    const update = (next: Table[]) => {
        setTables(next);
        const maps = toMaps(next);
        emitted.current = JSON.stringify(maps);
        onChange(maps);
    };

    const setTable = (index: number, table: Table) => update(tables.map((t, i) => (i === index ? table : t)));

    const addTable = () => {
        const used = new Set(tables.map((t) => t.field));
        const field = ["source", ...fieldSuggestions].find((name) => !used.has(name)) ?? "";
        update([...tables, { field, rows: [{ from: "", to: "" }] }]);
    };

    return (
        <Card>
            <CardHeader>
                <CardTitle>Value translations</CardTitle>
                <CardDescription>
                    Change what the CRM sends before the lead is saved and called. For example, a source of{" "}
                    <code>FB Leads ad</code> can reach the agent as <code>{"{{source}}"}</code> = Facebook. Case,
                    spaces and punctuation are ignored when matching; values without a row are kept as sent.
                </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
                {tables.map((table, tableIndex) => {
                    const current = table.field ? previewValue(table.field) : undefined;
                    const incomplete = table.rows.filter((row) => !row.from.trim() !== !row.to.trim()).length;
                    return (
                        <div key={tableIndex} className="space-y-2 rounded-md border p-3">
                            <div className="flex flex-wrap items-center gap-2">
                                <Label htmlFor={`value-map-field-${tableIndex}`} className="shrink-0">
                                    Field
                                </Label>
                                <Input
                                    id={`value-map-field-${tableIndex}`}
                                    list="value-map-fields"
                                    value={table.field}
                                    onChange={(e) => setTable(tableIndex, { ...table, field: e.target.value })}
                                    placeholder="e.g. source"
                                    className="w-48 font-mono text-sm"
                                />
                                {current !== undefined && (
                                    <span className="text-sm text-muted-foreground">
                                        In the sample this becomes <span className="font-medium">{current}</span>
                                    </span>
                                )}
                                <Button
                                    variant="ghost"
                                    size="sm"
                                    className="ml-auto"
                                    onClick={() => update(tables.filter((_, i) => i !== tableIndex))}
                                >
                                    <Trash2 className="mr-2 h-4 w-4" />
                                    Remove table
                                </Button>
                            </div>

                            <div className="grid grid-cols-[1fr_1fr_auto] gap-2 text-xs font-medium text-muted-foreground">
                                <span>CRM sends</span>
                                <span>Update as</span>
                                <span className="w-9" />
                            </div>
                            {table.rows.map((row, rowIndex) => (
                                <div key={rowIndex} className="grid grid-cols-[1fr_1fr_auto] items-center gap-2">
                                    <Input
                                        value={row.from}
                                        aria-label="CRM sends"
                                        placeholder="e.g. FB Leads ad"
                                        onChange={(e) => {
                                            const rows = [...table.rows];
                                            rows[rowIndex] = { ...row, from: e.target.value };
                                            setTable(tableIndex, { ...table, rows });
                                        }}
                                        onPaste={(e) => {
                                            const text = e.clipboardData.getData("text");
                                            if (!/[\t\n]/.test(text)) return;
                                            e.preventDefault();
                                            const pasted = parsePastedRows(text);
                                            const rows = [...table.rows];
                                            // Replace this row (and a blank one) with the pasted rows.
                                            rows.splice(rowIndex, row.from || row.to ? 0 : 1, ...pasted);
                                            setTable(tableIndex, { ...table, rows: rows.slice(0, MAX_ROWS) });
                                        }}
                                    />
                                    <Input
                                        value={row.to}
                                        aria-label="Update as"
                                        placeholder="e.g. Facebook"
                                        onChange={(e) => {
                                            const rows = [...table.rows];
                                            rows[rowIndex] = { ...row, to: e.target.value };
                                            setTable(tableIndex, { ...table, rows });
                                        }}
                                    />
                                    <Button
                                        variant="ghost"
                                        size="icon"
                                        aria-label="Remove row"
                                        onClick={() =>
                                            setTable(tableIndex, {
                                                ...table,
                                                rows: table.rows.filter((_, i) => i !== rowIndex),
                                            })
                                        }
                                    >
                                        <Trash2 className="h-4 w-4" />
                                    </Button>
                                </div>
                            ))}
                            {!table.field.trim() && (
                                <p className="text-sm text-destructive">Name the field, or this table won&apos;t be saved.</p>
                            )}
                            {incomplete > 0 && (
                                <p className="text-sm text-destructive">
                                    {incomplete === 1 ? "1 row is" : `${incomplete} rows are`} missing a value on one
                                    side and won&apos;t be saved until both are filled.
                                </p>
                            )}
                            <div className="flex flex-wrap items-center gap-3">
                                <Button
                                    variant="outline"
                                    size="sm"
                                    disabled={table.rows.length >= MAX_ROWS}
                                    onClick={() =>
                                        setTable(tableIndex, { ...table, rows: [...table.rows, { from: "", to: "" }] })
                                    }
                                >
                                    <Plus className="mr-2 h-4 w-4" />
                                    Add row
                                </Button>
                                <span className="text-xs text-muted-foreground">
                                    Tip: copy two columns from Excel and paste them into a &quot;CRM sends&quot; box.
                                </span>
                            </div>
                        </div>
                    );
                })}

                <datalist id="value-map-fields">
                    {fieldSuggestions.map((name) => (
                        <option key={name} value={name} />
                    ))}
                </datalist>

                <Button variant="outline" size="sm" onClick={addTable} disabled={tables.length >= MAX_TABLES}>
                    <Plus className="mr-2 h-4 w-4" />
                    {tables.length === 0 ? "Add a translation table" : "Translate another field"}
                </Button>
            </CardContent>
        </Card>
    );
}

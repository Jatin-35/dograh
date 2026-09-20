'use client';

import { Check, Copy } from 'lucide-react';
import { useState } from 'react';

import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';

import { StructuredData } from './StructuredData';

type Annotations = Record<string, unknown>;

function isRecord(value: unknown): value is Record<string, unknown> {
    return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/**
 * A QA reviewer's answer is stored as the raw text it returned, often wrapped in
 * a markdown fence. Read it back into an object for display; null if it is not JSON.
 */
export function parseQaResponse(text: unknown): Record<string, unknown> | null {
    if (typeof text !== 'string') return null;
    const fenced = text.match(/```(?:json)?\s*([\s\S]*?)\s*```/);
    const candidate = (fenced ? fenced[1] : text).trim();
    try {
        const parsed = JSON.parse(candidate);
        return isRecord(parsed) ? parsed : null;
    } catch {
        return null;
    }
}

function NodeResult({ name, result }: { name: string; result: Record<string, unknown> }) {
    const parsed = parseQaResponse(result.raw_response);
    // The four fields the QA task stores itself, used when the raw text is not JSON.
    const stored = {
        summary: result.summary,
        overall_sentiment: result.overall_sentiment,
        score: result.score,
        tags: result.tags,
    };

    return (
        <section className="space-y-2 rounded-md border border-border p-3">
            <h4 className="text-sm font-semibold text-foreground">{name}</h4>
            {typeof result.error === 'string' && <p className="text-sm text-destructive">{result.error}</p>}
            <StructuredData value={parsed ?? stored} />
            {!parsed && typeof result.raw_response === 'string' && result.raw_response && (
                <pre className="max-h-48 overflow-auto rounded-md bg-muted p-2 text-xs">{result.raw_response}</pre>
            )}
        </section>
    );
}

function FormattedView({ annotations }: { annotations: Annotations }) {
    return (
        <div className="space-y-4">
            {Object.entries(annotations).map(([key, value]) => {
                if (key.startsWith('qa_') && isRecord(value)) {
                    if (value.skipped) {
                        return (
                            <p key={key} className="text-sm text-muted-foreground">
                                QA analysis was skipped{typeof value.reason === 'string' ? `: ${value.reason}` : ''}.
                            </p>
                        );
                    }
                    const nodeResults = isRecord(value.node_results) ? value.node_results : {};
                    const entries = Object.entries(nodeResults).filter(([, result]) => isRecord(result));
                    if (entries.length === 0) {
                        return (
                            <p key={key} className="text-sm text-muted-foreground">
                                {typeof value.error === 'string' ? `QA analysis failed: ${value.error}` : 'No QA results.'}
                            </p>
                        );
                    }
                    return (
                        <div key={key} className="space-y-3">
                            {entries.map(([nodeKey, result]) => (
                                <NodeResult
                                    key={nodeKey}
                                    name={String((result as Record<string, unknown>).node_name || `Node ${nodeKey}`)}
                                    result={result as Record<string, unknown>}
                                />
                            ))}
                        </div>
                    );
                }
                return (
                    <section key={key} className="space-y-1">
                        <StructuredData value={{ [key]: value }} />
                    </section>
                );
            })}
        </div>
    );
}

export function QAResultsCard({ annotations }: { annotations: Annotations }) {
    const [copied, setCopied] = useState(false);
    const json = JSON.stringify(annotations, null, 2);

    const handleCopy = async () => {
        try {
            await navigator.clipboard.writeText(json);
            setCopied(true);
            setTimeout(() => setCopied(false), 2000);
        } catch {
            // Clipboard can be unavailable (insecure context); nothing to recover.
        }
    };

    return (
        <Card className="border-border">
            <Tabs defaultValue="formatted">
                <CardHeader className="flex-row items-center justify-between gap-4 space-y-0 pb-3">
                    <CardTitle className="text-lg">QA Results</CardTitle>
                    <TabsList>
                        <TabsTrigger value="formatted">Formatted</TabsTrigger>
                        <TabsTrigger value="json">JSON</TabsTrigger>
                    </TabsList>
                </CardHeader>
                <CardContent>
                    <TabsContent value="formatted" className="mt-0">
                        <FormattedView annotations={annotations} />
                    </TabsContent>
                    <TabsContent value="json" className="mt-0 space-y-2">
                        <div className="flex justify-end">
                            <Button variant="ghost" size="sm" onClick={handleCopy} className="gap-2">
                                {copied ? <Check className="h-4 w-4" /> : <Copy className="h-4 w-4" />}
                                {copied ? 'Copied' : 'Copy'}
                            </Button>
                        </div>
                        <pre className="max-h-[32rem] overflow-auto rounded-md bg-muted p-3 text-sm">{json}</pre>
                    </TabsContent>
                </CardContent>
            </Tabs>
        </Card>
    );
}

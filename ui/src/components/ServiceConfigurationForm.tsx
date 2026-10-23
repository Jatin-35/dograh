"use client";

import { ChevronDown, ExternalLink, Plus, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useForm } from "react-hook-form";

import { getDefaultConfigurationsApiV1UserConfigurationsDefaultsGet } from '@/client/sdk.gen';
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { VoiceSelector } from "@/components/VoiceSelector";
import { brandDocsLink } from "@/constants/brand";
import { LANGUAGE_DISPLAY_NAMES } from "@/constants/languages";
import { useUserConfig } from "@/context/UserConfigContext";
import type { ModelOverrides } from "@/types/workflow-configurations";

export type ServiceSegment = "llm" | "tts" | "stt" | "embeddings" | "realtime";

type FieldValue = string | number | boolean | string[] | null;

interface SchemaProperty {
    type?: string;
    default?: FieldValue;
    enum?: string[];
    examples?: string[];
    model_options?: Record<string, string[]>;
    allow_custom_input?: boolean;
    $ref?: string;
    description?: string;
    format?: string;
    multiline?: boolean;
    docs_url?: string;
    docs_label?: string;
    /** Optional fields (`int | None`) carry their type here instead of `type`. */
    anyOf?: SchemaProperty[];
    items?: SchemaProperty;
    /** A list field picked from `examples`, shown as removable chips. */
    multi_select?: boolean;
    /** Shown in the collapsed "Advanced" section. */
    advanced?: boolean;
    /** Shown only while each named field holds the given value. */
    visible_when?: Record<string, FieldValue>;
    /** Display label; defaults to the field name. */
    label?: string;
    /** Groups the field under a titled box (e.g. "Transcriber", "Voice"). */
    section?: string;
}

/** The field's type, looking through `anyOf` for optional fields. */
function effectiveType(schema: SchemaProperty | undefined): string | undefined {
    if (!schema) return undefined;
    if (schema.type) return schema.type;
    return schema.anyOf?.find(option => option.type && option.type !== "null")?.type;
}

function isNumericType(type: string | undefined): boolean {
    return type === "number" || type === "integer";
}

function fieldLabel(field: string, schema: SchemaProperty | undefined): string {
    return schema?.label || field.replace(/_/g, " ");
}

export interface ProviderSchema {
    title?: string;
    description?: string;
    provider_docs_url?: string;
    properties: Record<string, SchemaProperty>;
    required?: string[];
    $defs?: Record<string, SchemaProperty>;
    [key: string]: unknown;
}

interface FormValues {
    [key: string]: FieldValue;
}

export interface ServiceConfigurationDefaults {
    llm: Record<string, ProviderSchema>;
    tts: Record<string, ProviderSchema>;
    stt: Record<string, ProviderSchema>;
    embeddings: Record<string, ProviderSchema>;
    realtime?: Record<string, ProviderSchema>;
    default_providers: Partial<Record<ServiceSegment, string>>;
}

const STANDARD_TABS: { key: ServiceSegment; label: string }[] = [
    { key: "llm", label: "LLM" },
    { key: "tts", label: "Voice" },
    { key: "stt", label: "Transcriber" },
    { key: "embeddings", label: "Embedding" },
];

const REALTIME_TABS: { key: ServiceSegment; label: string }[] = [
    { key: "realtime", label: "Realtime Model" },
    { key: "llm", label: "LLM" },
    { key: "embeddings", label: "Embedding" },
];

const OVERRIDE_STANDARD_TABS: { key: ServiceSegment; label: string }[] = [
    { key: "llm", label: "LLM" },
    { key: "tts", label: "Voice" },
    { key: "stt", label: "Transcriber" },
];

const OVERRIDE_REALTIME_TABS: { key: ServiceSegment; label: string }[] = [
    { key: "realtime", label: "Realtime Model" },
    { key: "llm", label: "LLM" },
];

// Display names for Sarvam voices
const VOICE_DISPLAY_NAMES: Record<string, string> = {
    "anushka": "Anushka (Female)",
    "manisha": "Manisha (Female)",
    "vidya": "Vidya (Female)",
    "arya": "Arya (Female)",
    "abhilash": "Abhilash (Male)",
    "karun": "Karun (Male)",
    "hitesh": "Hitesh (Male)",
};

export interface ServiceConfigurationFormProps {
    mode: 'global' | 'override';
    currentOverrides?: ModelOverrides;
    onSave: (config: Record<string, unknown>) => Promise<void>;
    /** Text for the submit button. Defaults to "Save Configuration". */
    submitLabel?: string;
    configurationDefaults?: ServiceConfigurationDefaults | null;
    initialConfig?: Record<string, unknown> | null;
    /**
     * When set, locks the realtime/pipeline mode to this value and hides the
     * in-form toggle. The v2 editor uses this to surface realtime
     * ("Speech to Speech") and pipeline (BYOK) as separate top-level tabs.
     * Leave undefined to keep the user-controllable toggle (legacy + overrides).
     */
    forceRealtime?: boolean;
}

function getProviderDisplayName(
    provider: string | undefined,
    providerSchema: ProviderSchema | undefined,
): string | undefined {
    if (!provider) return provider;
    return providerSchema?.title || provider;
}

function getGlobalSummary(
    config: Record<string, unknown> | null | undefined,
    providerSchema: ProviderSchema | undefined,
): string {
    if (!config) return "Not configured";
    const provider = config.provider as string | undefined;
    const model = config.model as string | undefined;
    if (!provider) return "Not configured";
    const providerLabel = getProviderDisplayName(provider, providerSchema);
    return model ? `${providerLabel} / ${model}` : providerLabel || provider;
}

function getSchemaDropdownOptions(
    schema: SchemaProperty | undefined,
    modelValue?: string,
): string[] | undefined {
    let dropdownOptions = schema?.enum || schema?.examples;

    if (schema?.model_options && modelValue && schema.model_options[modelValue]) {
        dropdownOptions = schema.model_options[modelValue];
    }

    return dropdownOptions;
}

export function ServiceConfigurationForm({
    mode,
    currentOverrides,
    onSave,
    submitLabel,
    configurationDefaults,
    initialConfig,
    forceRealtime,
}: ServiceConfigurationFormProps) {
    const [apiError, setApiError] = useState<string | null>(null);
    const [isSaving, setIsSaving] = useState(false);
    const [isRealtime, setIsRealtime] = useState(forceRealtime ?? false);
    const { userConfig } = useUserConfig();
    const [schemas, setSchemas] = useState<Record<ServiceSegment, Record<string, ProviderSchema>>>({
        llm: {},
        tts: {},
        stt: {},
        embeddings: {},
        realtime: {},
    });
    const [serviceProviders, setServiceProviders] = useState<Record<ServiceSegment, string>>({
        llm: "",
        tts: "",
        stt: "",
        embeddings: "",
        realtime: "",
    });
    const [apiKeys, setApiKeys] = useState<Record<ServiceSegment, string[]>>({
        llm: [""],
        tts: [""],
        stt: [""],
        embeddings: [""],
        realtime: [""],
    });
    const [isCustomInput, setIsCustomInput] = useState<Record<string, boolean>>({});

    // Override-specific state: which services have the override toggle enabled
    const [enabledOverrides, setEnabledOverrides] = useState<Record<string, boolean>>({
        llm: false,
        tts: false,
        stt: false,
        realtime: false,
    });

    const {
        register,
        handleSubmit,
        formState: { },
        reset,
        getValues,
        setValue,
        watch
    } = useForm();

    // Build effective config source: overlay overrides onto global config
    const configSource = useMemo(() => {
        const baseConfig = initialConfig ?? userConfig;
        if (mode === 'global' || !currentOverrides) return baseConfig;
        // Merge overrides onto global config for form initialization
        const merged = { ...baseConfig } as Record<string, unknown>;
        const overrideServices: (keyof ModelOverrides)[] = ["llm", "tts", "stt", "realtime"];
        for (const svc of overrideServices) {
            if (svc === "is_realtime") continue;
            const overrideVal = currentOverrides[svc];
            if (overrideVal && typeof overrideVal === "object") {
                const globalVal = (baseConfig as Record<string, unknown> | null)?.[svc] as Record<string, unknown> | undefined;
                merged[svc] = { ...globalVal, ...overrideVal };
            }
        }
        if (currentOverrides.is_realtime !== undefined) {
            merged.is_realtime = currentOverrides.is_realtime;
        }
        return merged as typeof userConfig;
    }, [mode, userConfig, currentOverrides, initialConfig]);

    useEffect(() => {
        const fetchConfigurations = async () => {
            let defaultsData = configurationDefaults;
            if (!defaultsData) {
                const response = await getDefaultConfigurationsApiV1UserConfigurationsDefaultsGet();
                if (!response.data) {
                    console.error("Failed to fetch configurations");
                    return;
                }
                defaultsData = response.data as unknown as ServiceConfigurationDefaults;
            }

            const realtimeSchemas = (defaultsData.realtime || {}) as Record<string, ProviderSchema>;
            const pickDefaultProvider = (
                service: ServiceSegment,
                schemaMap: Record<string, ProviderSchema>,
            ) => {
                const preferred = defaultsData.default_providers?.[service];
                if (preferred && schemaMap[preferred]) return preferred;
                return Object.keys(schemaMap)[0] || "";
            };

            setSchemas({
                llm: defaultsData.llm,
                tts: defaultsData.tts,
                stt: defaultsData.stt,
                embeddings: defaultsData.embeddings,
                realtime: realtimeSchemas,
            });

            // Restore realtime toggle (skip when the parent locks the mode)
            const configData = configSource as Record<string, unknown> | null;
            if (forceRealtime === undefined && configData?.is_realtime) {
                setIsRealtime(true);
            }

            const defaultValues: Record<string, FieldValue> = {};
            const selectedProviders: Record<ServiceSegment, string> = {
                llm: pickDefaultProvider("llm", defaultsData.llm),
                tts: pickDefaultProvider("tts", defaultsData.tts),
                stt: pickDefaultProvider("stt", defaultsData.stt),
                embeddings: pickDefaultProvider("embeddings", defaultsData.embeddings),
                realtime: "",
            };

            const realtimeProviderKeys = Object.keys(realtimeSchemas);
            if (realtimeProviderKeys.length > 0) {
                selectedProviders.realtime = realtimeProviderKeys[0];
            }

            const loadedApiKeys: Record<ServiceSegment, string[]> = {
                llm: [""],
                tts: [""],
                stt: [""],
                embeddings: [""],
                realtime: [""],
            };

            const setServicePropertyValues = (service: ServiceSegment) => {
                const src = service === "realtime"
                    ? (configSource as Record<string, unknown> | null)?.realtime as Record<string, unknown> | undefined
                    : (configSource as Record<string, unknown> | null)?.[service] as Record<string, unknown> | undefined;

                const schemaSource = service === "realtime"
                    ? realtimeSchemas
                    : defaultsData[service as "llm" | "tts" | "stt" | "embeddings"] as Record<string, ProviderSchema> | undefined;

                if (src?.provider) {
                    Object.entries(src).forEach(([field, value]) => {
                        if (field === "api_key") {
                            if (mode === 'override') {
                                // In override mode, only load API keys from the override itself
                                const overrideVal = currentOverrides?.[service as keyof ModelOverrides];
                                const overrideApiKey = overrideVal && typeof overrideVal === "object"
                                    ? (overrideVal as Record<string, unknown>).api_key
                                    : undefined;
                                if (overrideApiKey) {
                                    loadedApiKeys[service] = Array.isArray(overrideApiKey)
                                        ? overrideApiKey as string[]
                                        : [overrideApiKey as string];
                                } else {
                                    loadedApiKeys[service] = [""];
                                }
                            } else {
                                if (Array.isArray(value)) {
                                    loadedApiKeys[service] = (value as string[]).length > 0 ? value as string[] : [""];
                                } else {
                                    loadedApiKeys[service] = value ? [value as string] : [""];
                                }
                            }
                        } else if (field !== "provider") {
                            defaultValues[`${service}_${field}`] = value as FieldValue;
                        }
                    });
                    selectedProviders[service] = src.provider as string;
                    const properties = schemaSource?.[selectedProviders[service]]?.properties as Record<string, SchemaProperty>;
                    if (properties) {
                        Object.entries(properties).forEach(([field, schema]) => {
                            const key = `${service}_${field}`;
                            if (field !== "provider" && field !== "api_key" && schema.default !== undefined && !(key in defaultValues)) {
                                defaultValues[key] = schema.default;
                            }
                        });
                    }
                } else {
                    const properties = schemaSource?.[selectedProviders[service]]?.properties as Record<string, SchemaProperty>;
                    if (properties) {
                        Object.entries(properties).forEach(([field, schema]) => {
                            if (field !== "provider" && schema.default !== undefined) {
                                defaultValues[`${service}_${field}`] = schema.default;
                            }
                        });
                    }
                }
            };

            setServicePropertyValues("llm");
            setServicePropertyValues("tts");
            setServicePropertyValues("stt");
            setServicePropertyValues("embeddings");
            setServicePropertyValues("realtime");

            // Detect custom inputs
            const detectedCustomInput: Record<string, boolean> = {};
            const allSchemas = { ...defaultsData, realtime: realtimeSchemas } as unknown as Record<string, Record<string, ProviderSchema>>;
            (["llm", "tts", "stt", "embeddings", "realtime"] as ServiceSegment[]).forEach(service => {
                const provider = selectedProviders[service];
                const providerSchema = allSchemas[service]?.[provider];
                if (!providerSchema) return;

                const src = service === "realtime"
                    ? (configSource as Record<string, unknown> | null)?.realtime as Record<string, unknown> | undefined
                    : (configSource as Record<string, unknown> | null)?.[service] as Record<string, unknown> | undefined;

                Object.entries(providerSchema.properties).forEach(([field, schema]) => {
                    const actualSchema = (schema as SchemaProperty).$ref && providerSchema.$defs
                        ? providerSchema.$defs[(schema as SchemaProperty).$ref!.split('/').pop() || '']
                        : schema as SchemaProperty;

                    if (!actualSchema?.allow_custom_input) return;

                    const savedValue = src?.[field] as string | undefined;
                    const modelValue = src?.model as string | undefined;
                    const dropdownOptions = getSchemaDropdownOptions(actualSchema, modelValue);
                    if (savedValue && dropdownOptions && !dropdownOptions.includes(savedValue)) {
                        detectedCustomInput[`${service}_${field}`] = true;
                    }
                });
            });

            // Initialize override toggles
            if (mode === 'override') {
                setEnabledOverrides({
                    llm: !!currentOverrides?.llm,
                    tts: !!currentOverrides?.tts,
                    stt: !!currentOverrides?.stt,
                    realtime: !!currentOverrides?.realtime,
                });
            }

            reset(defaultValues);
            setApiKeys(loadedApiKeys);
            setServiceProviders(selectedProviders);
            setIsCustomInput(detectedCustomInput);
        };
        fetchConfigurations();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [reset, configSource, configurationDefaults]);

    // Reset voice when TTS model changes if the provider has model-dependent voice options
    const ttsModel = watch("tts_model");
    useEffect(() => {
        const voiceSchema = schemas?.tts?.[serviceProviders.tts]?.properties?.voice;
        const modelOptions = voiceSchema?.model_options;
        if (!modelOptions || !ttsModel) return;

        const validVoices = modelOptions[ttsModel as string];
        const currentVoice = getValues("tts_voice") as string;
        const isCustomVoice = !!isCustomInput.tts_voice;
        if (validVoices && currentVoice && !validVoices.includes(currentVoice) && !isCustomVoice) {
            setValue("tts_voice", validVoices[0], { shouldDirty: true });
        }
    }, [ttsModel, serviceProviders.tts, setValue, getValues, schemas, isCustomInput.tts_voice]);

    // Reset language when STT model changes if the provider has model-dependent language options
    const sttModel = watch("stt_model");
    useEffect(() => {
        const languageSchema = schemas?.stt?.[serviceProviders.stt]?.properties?.language;
        const modelOptions = languageSchema?.model_options;
        if (!modelOptions || !sttModel) return;

        const validLanguages = modelOptions[sttModel as string];
        const currentLanguage = getValues("stt_language") as string;
        if (validLanguages && currentLanguage && !validLanguages.includes(currentLanguage)) {
            setValue("stt_language", validLanguages[0], { shouldDirty: true });
        }
    }, [sttModel, serviceProviders.stt, setValue, getValues, schemas]);

    const handleProviderChange = (service: ServiceSegment, providerName: string) => {
        if (!providerName) return;

        const currentValues = getValues();
        const preservedValues: Record<string, FieldValue> = {};

        Object.keys(currentValues).forEach(key => {
            if (!key.startsWith(`${service}_`)) {
                preservedValues[key] = currentValues[key];
            }
        });

        if (schemas?.[service]?.[providerName]) {
            const providerSchema = schemas[service][providerName];
            Object.entries(providerSchema.properties).forEach(([field, schema]: [string, SchemaProperty]) => {
                if (field !== "provider" && schema.default !== undefined) {
                    preservedValues[`${service}_${field}`] = schema.default;
                }
            });
        }

        preservedValues[`${service}_provider`] = providerName;
        reset(preservedValues);
        setServiceProviders(prev => ({ ...prev, [service]: providerName }));
        setApiKeys(prev => ({ ...prev, [service]: [""] }));

        setIsCustomInput(prev => {
            const next = { ...prev };
            Object.keys(next).forEach(key => {
                if (key.startsWith(`${service}_`)) delete next[key];
            });
            return next;
        });
    };

    const buildServiceConfig = (service: ServiceSegment, data: FormValues) => {
        const config: Record<string, FieldValue> = {
            provider: serviceProviders[service],
        };
        const keys = apiKeys[service].map(k => k.trim()).filter(k => k.length > 0);
        if (keys.length > 0) {
            config.api_key = mode === 'override' ? keys[0] : keys;
        }
        Object.entries(data).forEach(([property, value]) => {
            if (!property.startsWith(`${service}_`)) return;
            const field = property.slice(service.length + 1);
            if (field === "api_key" || field === "provider") return;
            // An emptied number input reads as NaN; send it as "not set".
            config[field] = typeof value === "number" && Number.isNaN(value) ? null : value;
        });
        return config;
    };

    const onSubmit = async (data: FormValues) => {
        setApiError(null);
        setIsSaving(true);

        try {
            if (mode === 'override') {
                // Build model_overrides for enabled services only
                const modelOverrides: Record<string, unknown> = {};
                const services = isRealtime ? ["realtime", "llm"] : ["llm", "tts", "stt"];
                for (const svc of services) {
                    if (enabledOverrides[svc]) {
                        modelOverrides[svc] = buildServiceConfig(svc as ServiceSegment, data);
                    }
                }
                // Include is_realtime if it differs from global
                const globalIsRealtime = !!(userConfig as Record<string, unknown> | null)?.is_realtime;
                if (isRealtime !== globalIsRealtime) {
                    modelOverrides.is_realtime = isRealtime;
                }
                await onSave({
                    model_overrides: Object.keys(modelOverrides).length > 0 ? modelOverrides : undefined,
                });
            } else {
                // Global mode: save all services
                const saveConfig: Record<string, unknown> = {
                    llm: buildServiceConfig("llm", data),
                    tts: buildServiceConfig("tts", data),
                    stt: buildServiceConfig("stt", data),
                    is_realtime: isRealtime,
                };
                if (serviceProviders.realtime) {
                    saveConfig.realtime = buildServiceConfig("realtime", data);
                }
                const embeddingsKeys = apiKeys.embeddings.map(k => k.trim()).filter(k => k.length > 0);
                if (embeddingsKeys.length > 0) {
                    saveConfig.embeddings = buildServiceConfig("embeddings", data);
                }
                await onSave(saveConfig);
            }
            setApiError(null);
        } catch (error: unknown) {
            if (error instanceof Error) {
                setApiError(error.message);
            } else {
                setApiError('An unknown error occurred');
            }
        } finally {
            setIsSaving(false);
        }
    };

    const getConfigFields = (service: ServiceSegment): string[] => {
        const currentProvider = serviceProviders[service];
        const providerSchema = schemas?.[service]?.[currentProvider];
        if (!providerSchema) return [];
        return Object.keys(providerSchema.properties).filter(
            field => field !== "provider" && field !== "api_key"
        );
    };

    const resolveFieldSchema = (providerSchema: ProviderSchema, field: string): SchemaProperty | undefined => {
        const schema = providerSchema.properties[field];
        return schema?.$ref && providerSchema.$defs
            ? providerSchema.$defs[schema.$ref.split('/').pop() || '']
            : schema;
    };

    const isFieldVisible = (service: ServiceSegment, schema: SchemaProperty | undefined) => {
        if (!schema?.visible_when) return true;
        return Object.entries(schema.visible_when).every(
            ([other, expected]) => (watch(`${service}_${other}`) ?? false) === expected,
        );
    };

    const renderFieldBlock = (service: ServiceSegment, field: string, providerSchema: ProviderSchema) => {
        const schema = resolveFieldSchema(providerSchema, field);
        const fullWidth = schema?.multiline || schema?.multi_select;
        return (
            <div key={field} className={`space-y-2 ${fullWidth ? "col-span-2" : ""}`}>
                <Label className={schema?.label ? "" : "capitalize"} htmlFor={`${service}_${field}`}>
                    {fieldLabel(field, schema)}
                </Label>
                {renderField(service, field, providerSchema)}
            </div>
        );
    };

    const renderServiceFields = (service: ServiceSegment) => {
        const currentProvider = serviceProviders[service];
        const providerSchema = schemas?.[service]?.[currentProvider];
        const availableProviders = schemas?.[service] ? Object.keys(schemas[service]) : [];
        const configFields = getConfigFields(service);
        const shownFields = providerSchema
            ? configFields.slice(1).filter(field => isFieldVisible(service, resolveFieldSchema(providerSchema, field)))
            : [];
        const mainFields = shownFields.filter(field => !resolveFieldSchema(providerSchema!, field)?.advanced);
        const advancedFields = shownFields.filter(field => resolveFieldSchema(providerSchema!, field)?.advanced);
        // A provider whose fields name sections (e.g. Inworld's Transcriber /
        // LLM / Voice) is laid out as one titled box per section, in the order
        // the sections first appear; its first field joins its section rather
        // than sitting beside the provider.
        const sectionOf = (field: string) =>
            providerSchema ? resolveFieldSchema(providerSchema, field)?.section : undefined;
        const sectioned = configFields.some(field => sectionOf(field));
        const sectionFields = providerSchema && sectioned
            ? configFields.filter(field => isFieldVisible(service, resolveFieldSchema(providerSchema, field)))
            : [];
        const declaredOrder = Array.isArray(providerSchema?.sections) ? (providerSchema.sections as string[]) : [];
        const sectionNames = [...new Set(sectionFields.map(field => sectionOf(field) ?? "General"))].sort(
            (a, b) => (declaredOrder.indexOf(a) + 1 || Infinity) - (declaredOrder.indexOf(b) + 1 || Infinity),
        );

        return (
            <div className="space-y-6">
                <div className="grid grid-cols-2 gap-4">
                    <div className="space-y-2">
                        <Label>Provider</Label>
                        <Select
                            value={currentProvider}
                            onValueChange={(providerName) => {
                                handleProviderChange(service, providerName);
                            }}
                        >
                            <SelectTrigger className="w-full">
                                <SelectValue placeholder="Select provider" />
                            </SelectTrigger>
                            <SelectContent>
                                {availableProviders.map((provider) => (
                                    <SelectItem key={provider} value={provider}>
                                        {getProviderDisplayName(provider, schemas?.[service]?.[provider])}
                                    </SelectItem>
                                ))}
                            </SelectContent>
                        </Select>
                        {(providerSchema?.description || providerSchema?.provider_docs_url) && (
                            <p className="text-xs text-muted-foreground">
                                {providerSchema?.description}{" "}
                                {providerSchema?.provider_docs_url && (
                                    <a
                                        href={providerSchema.provider_docs_url}
                                        target="_blank"
                                        rel="noopener noreferrer"
                                        className="inline-flex items-center gap-0.5 underline"
                                    >
                                        Learn more <ExternalLink className="h-3 w-3" />
                                    </a>
                                )}
                            </p>
                        )}
                    </div>

                    {currentProvider && providerSchema && configFields[0] && !sectioned && (
                        <div className="space-y-2">
                            <Label className="capitalize">
                                {fieldLabel(configFields[0], resolveFieldSchema(providerSchema, configFields[0]))}
                            </Label>
                            {renderField(service, configFields[0], providerSchema)}
                        </div>
                    )}
                </div>

                {currentProvider && providerSchema && sectioned && sectionNames.map((name) => {
                    const fields = sectionFields.filter(field => (sectionOf(field) ?? "General") === name);
                    const main = fields.filter(field => !resolveFieldSchema(providerSchema, field)?.advanced);
                    const more = fields.filter(field => resolveFieldSchema(providerSchema, field)?.advanced);
                    return (
                        <section key={name} aria-label={name} className="space-y-4 rounded-md border p-4">
                            <h4 className="text-sm font-semibold">{name}</h4>
                            {main.length > 0 && (
                                <div className="grid grid-cols-2 gap-4">
                                    {main.map((field) => renderFieldBlock(service, field, providerSchema))}
                                </div>
                            )}
                            {more.length > 0 && (
                                <Collapsible>
                                    <CollapsibleTrigger className="group flex items-center gap-1 text-xs font-medium text-muted-foreground">
                                        More {name.toLowerCase()} options
                                        <ChevronDown className="h-3 w-3 transition-transform group-data-[state=open]:rotate-180" />
                                    </CollapsibleTrigger>
                                    <CollapsibleContent className="pt-3">
                                        <div className="grid grid-cols-2 gap-4">
                                            {more.map((field) => renderFieldBlock(service, field, providerSchema))}
                                        </div>
                                    </CollapsibleContent>
                                </Collapsible>
                            )}
                        </section>
                    );
                })}

                {currentProvider && providerSchema && !sectioned && mainFields.length > 0 && (
                    <div className="grid grid-cols-2 gap-4">
                        {mainFields.map((field) => renderFieldBlock(service, field, providerSchema))}
                    </div>
                )}

                {currentProvider && providerSchema && !sectioned && advancedFields.length > 0 && (
                    <Collapsible className="rounded-md border">
                        <CollapsibleTrigger className="group flex w-full items-center justify-between px-3 py-2 text-sm font-medium">
                            Advanced
                            <ChevronDown className="h-4 w-4 transition-transform group-data-[state=open]:rotate-180" />
                        </CollapsibleTrigger>
                        <CollapsibleContent className="border-t p-3">
                            <div className="grid grid-cols-2 gap-4">
                                {advancedFields.map((field) => renderFieldBlock(service, field, providerSchema))}
                            </div>
                        </CollapsibleContent>
                    </Collapsible>
                )}

                {currentProvider && providerSchema && providerSchema.properties.api_key && (
                    <div className="space-y-2">
                        <Label>{mode === 'override' ? 'API Key (leave empty to use global)' : 'API Key(s)'}</Label>
                        {renderFieldDescription("api_key", providerSchema)}
                        {apiKeys[service].map((key, index) => (
                            <div key={index} className="flex gap-2">
                                <Input
                                    type="text"
                                    placeholder="Enter API key"
                                    value={key}
                                    onChange={(e) => {
                                        const newKeys = [...apiKeys[service]];
                                        newKeys[index] = e.target.value;
                                        setApiKeys(prev => ({ ...prev, [service]: newKeys }));
                                    }}
                                />
                                {apiKeys[service].length > 1 && (
                                    <Button
                                        type="button"
                                        variant="ghost"
                                        size="icon"
                                        className="shrink-0"
                                        onClick={() => {
                                            setApiKeys(prev => ({
                                                ...prev,
                                                [service]: prev[service].filter((_, i) => i !== index),
                                            }));
                                        }}
                                    >
                                        <X className="h-4 w-4" />
                                    </Button>
                                )}
                            </div>
                        ))}
                        {mode !== 'override' && (
                            <Button
                                type="button"
                                variant="outline"
                                size="sm"
                                onClick={() => {
                                    setApiKeys(prev => ({
                                        ...prev,
                                        [service]: [...prev[service], ""],
                                    }));
                                }}
                            >
                                <Plus className="h-4 w-4 mr-1" /> Add API Key
                            </Button>
                        )}
                    </div>
                )}
            </div>
        );
    };

    const renderFieldDescription = (field: string, providerSchema: ProviderSchema) => {
        const schema = providerSchema.properties[field];
        if (!schema) return null;
        const actualSchema = schema.$ref && providerSchema.$defs
            ? providerSchema.$defs[schema.$ref.split('/').pop() || '']
            : schema;
        if (!actualSchema?.description && !actualSchema?.docs_url) return null;
        return (
            <p className="text-xs text-muted-foreground">
                {actualSchema?.description}{" "}
                {actualSchema?.docs_url && (
                    <a
                        href={brandDocsLink(actualSchema.docs_url)}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center gap-0.5 underline"
                    >
                        {actualSchema.docs_label ?? "Supported languages"} <ExternalLink className="h-3 w-3" />
                    </a>
                )}
            </p>
        );
    };

    const renderField = (service: ServiceSegment, field: string, providerSchema: ProviderSchema) => {
        return (
            <>
                {renderFieldInput(service, field, providerSchema)}
                {renderFieldDescription(field, providerSchema)}
            </>
        );
    };

    const renderFieldInput = (service: ServiceSegment, field: string, providerSchema: ProviderSchema) => {
        const schema = providerSchema.properties[field];
        const actualSchema = schema.$ref && providerSchema.$defs
            ? providerSchema.$defs[schema.$ref.split('/').pop() || '']
            : schema;
        const dropdownOptions = getSchemaDropdownOptions(
            actualSchema,
            watch(`${service}_model`) as string | undefined,
        );
        const fieldKey = `${service}_${field}`;
        const type = effectiveType(actualSchema);

        if (type === "boolean") {
            return (
                <div className="flex h-9 items-center">
                    <Switch
                        id={fieldKey}
                        checked={!!watch(fieldKey)}
                        onCheckedChange={(checked) => setValue(fieldKey, checked, { shouldDirty: true })}
                    />
                </div>
            );
        }

        if (type === "array" && actualSchema?.multi_select) {
            const selected = (watch(fieldKey) as string[] | null | undefined) ?? [];
            const options = (dropdownOptions ?? []).filter(option => !selected.includes(option));
            const displayName = (value: string) => LANGUAGE_DISPLAY_NAMES[value] || value;
            const update = (next: string[]) => setValue(fieldKey, next, { shouldDirty: true });
            return (
                <div className="space-y-2">
                    <div className="flex min-h-9 flex-wrap items-center gap-1.5 rounded-md border px-2 py-1.5">
                        {selected.length === 0 && (
                            <span className="text-sm text-muted-foreground">None selected</span>
                        )}
                        {selected.map(value => (
                            <Badge key={value} variant="secondary" className="gap-1">
                                {displayName(value)}
                                <button
                                    type="button"
                                    aria-label={`Remove ${displayName(value)}`}
                                    className="rounded-sm opacity-70 hover:opacity-100"
                                    onClick={() => update(selected.filter(item => item !== value))}
                                >
                                    <X className="h-3 w-3" />
                                </button>
                            </Badge>
                        ))}
                    </div>
                    {options.length > 0 && (
                        <Select
                            value=""
                            onValueChange={(value) => {
                                if (value) update([...selected, value]);
                            }}
                        >
                            <SelectTrigger className="w-full" aria-label={`Add ${fieldLabel(field, actualSchema)}`}>
                                <SelectValue placeholder="Add..." />
                            </SelectTrigger>
                            <SelectContent>
                                {options.map(value => (
                                    <SelectItem key={value} value={value}>
                                        {displayName(value)}
                                    </SelectItem>
                                ))}
                            </SelectContent>
                        </Select>
                    )}
                </div>
            );
        }

        if (!actualSchema?.type && isNumericType(type) && !dropdownOptions?.length) {
            // An optional number: empty means "not set" rather than 0 or NaN.
            const bounds = actualSchema?.anyOf?.find(option => isNumericType(option.type)) as
                (SchemaProperty & { minimum?: number; maximum?: number }) | undefined;
            return (
                <Input
                    id={fieldKey}
                    type="number"
                    step={type === "integer" ? 1 : "any"}
                    min={bounds?.minimum}
                    max={bounds?.maximum}
                    placeholder="Default"
                    {...register(fieldKey, {
                        setValueAs: (value) =>
                            value === "" || value === null || value === undefined ? null : Number(value),
                    })}
                />
            );
        }

        if (service === "tts" && field === "voice" && !actualSchema?.allow_custom_input) {
            if (!dropdownOptions) {
                return (
                    <VoiceSelector
                        provider={serviceProviders.tts}
                        value={watch(`${service}_${field}`) as string || ""}
                        onChange={(voiceId) => {
                            setValue(`${service}_${field}`, voiceId, { shouldDirty: true });
                        }}
                        model={watch("tts_model") as string || undefined}
                    />
                );
            }
        }

        if (actualSchema?.allow_custom_input && dropdownOptions && dropdownOptions.length > 0) {
            const currentValue = watch(fieldKey) as string || "";
            const options = dropdownOptions;

            if (isCustomInput[fieldKey]) {
                return (
                    <div className="space-y-2">
                        <Input
                            id={fieldKey}
                            type="text"
                            placeholder={`Enter ${field}`}
                            value={currentValue}
                            onChange={(e) => {
                                setValue(fieldKey, e.target.value, { shouldDirty: true });
                            }}
                        />
                        <div className="flex items-center space-x-2">
                            <Checkbox
                                id={`custom-input-${fieldKey}`}
                                checked={true}
                                onCheckedChange={(checked) => {
                                    setIsCustomInput(prev => ({ ...prev, [fieldKey]: checked as boolean }));
                                    if (!checked && options.length > 0) {
                                        setValue(fieldKey, options[0], { shouldDirty: true });
                                    }
                                }}
                            />
                            <Label htmlFor={`custom-input-${fieldKey}`} className="text-sm font-normal cursor-pointer">
                                Enter Custom Value
                            </Label>
                        </div>
                    </div>
                );
            }

            return (
                <div className="space-y-2">
                    <Select
                        value={currentValue}
                        onValueChange={(value) => {
                            if (!value) return;
                            setValue(fieldKey, value, { shouldDirty: true });
                        }}
                    >
                        <SelectTrigger id={fieldKey} className="w-full">
                            <SelectValue placeholder={`Select ${field}`} />
                        </SelectTrigger>
                        <SelectContent>
                            {options.map((value: string) => (
                                <SelectItem key={value} value={value}>
                                    {value}
                                </SelectItem>
                            ))}
                        </SelectContent>
                    </Select>
                    <div className="flex items-center space-x-2">
                        <Checkbox
                            id={`custom-input-${fieldKey}-dropdown`}
                            checked={false}
                            onCheckedChange={(checked) => {
                                setIsCustomInput(prev => ({ ...prev, [fieldKey]: checked as boolean }));
                            }}
                        />
                        <Label htmlFor={`custom-input-${fieldKey}-dropdown`} className="text-sm font-normal cursor-pointer">
                            Enter Custom Value
                        </Label>
                    </div>
                </div>
            );
        }

        if (dropdownOptions && dropdownOptions.length > 0) {
            const getDisplayName = (value: string) => {
                if (field === "language") {
                    return LANGUAGE_DISPLAY_NAMES[value] || value;
                }
                if (field === "voice") {
                    return VOICE_DISPLAY_NAMES[value] || value.charAt(0).toUpperCase() + value.slice(1);
                }
                return value;
            };

            return (
                <Select
                    value={watch(`${service}_${field}`) as string || ""}
                    onValueChange={(value) => {
                        if (!value) return;
                        setValue(`${service}_${field}`, value, { shouldDirty: true });
                    }}
                >
                    <SelectTrigger id={fieldKey} className="w-full">
                        <SelectValue placeholder={`Select ${field}`} />
                    </SelectTrigger>
                    <SelectContent>
                        {dropdownOptions.map((value: string) => (
                            <SelectItem key={value} value={value}>
                                {getDisplayName(value)}
                            </SelectItem>
                        ))}
                    </SelectContent>
                </Select>
            );
        }

        if (actualSchema?.multiline) {
            return (
                <Textarea
                    id={fieldKey}
                    rows={6}
                    className="font-mono text-xs"
                    placeholder={`Enter ${field}`}
                    {...register(`${service}_${field}`, {
                        required: service !== "embeddings" && providerSchema.required?.includes(field),
                    })}
                />
            );
        }

        return (
            <Input
                id={fieldKey}
                type={actualSchema?.type === "number" ? "number" : "text"}
                {...(actualSchema?.type === "number" && { step: "any" })}
                placeholder={`Enter ${field}`}
                {...register(`${service}_${field}`, {
                    required: service !== "embeddings" && providerSchema.required?.includes(field),
                    valueAsNumber: actualSchema?.type === "number"
                })}
            />
        );
    };

    const handleOverrideToggle = (service: string, enabled: boolean) => {
        setEnabledOverrides(prev => ({ ...prev, [service]: enabled }));
    };

    const renderOverrideToggle = (service: ServiceSegment, label: string) => {
        const globalVal = (userConfig as Record<string, unknown> | null)?.[service] as Record<string, unknown> | null | undefined;
        const isEnabled = enabledOverrides[service];
        const globalProvider = globalVal?.provider as string | undefined;
        const globalProviderSchema = globalProvider ? schemas?.[service]?.[globalProvider] : undefined;

        return (
            <div className="flex items-center justify-between p-3 border rounded-md bg-muted/20 mb-4">
                <div className="space-y-0.5">
                    <Label htmlFor={`override-${service}`} className="text-sm cursor-pointer font-medium">
                        Override {label}
                    </Label>
                    {!isEnabled && (
                        <p className="text-xs text-muted-foreground">
                            Using global: {getGlobalSummary(globalVal, globalProviderSchema)}
                        </p>
                    )}
                </div>
                <Switch
                    id={`override-${service}`}
                    checked={isEnabled}
                    onCheckedChange={(checked) => handleOverrideToggle(service, checked)}
                />
            </div>
        );
    };

    const getVisibleTabs = () => {
        if (mode === 'override') {
            return isRealtime ? OVERRIDE_REALTIME_TABS : OVERRIDE_STANDARD_TABS;
        }
        return isRealtime ? REALTIME_TABS : STANDARD_TABS;
    };

    const visibleTabs = getVisibleTabs();
    const defaultTab = isRealtime ? "realtime" : "llm";

    return (
        <form onSubmit={handleSubmit(onSubmit)}>
            {/* Realtime toggle — hidden when the parent locks the mode (v2 tabs) */}
            {forceRealtime === undefined && (
                <div className="flex items-center justify-between mb-4 p-4 border rounded-lg">
                    <div>
                        <Label htmlFor="realtime-toggle" className="text-sm font-medium">
                            Realtime Mode
                        </Label>
                        <p className="text-xs text-muted-foreground mt-0.5">
                            Uses a single speech-to-speech model (no separate STT/TTS). An LLM is still required for variable extraction and QA.
                        </p>
                    </div>
                    <Switch
                        id="realtime-toggle"
                        checked={isRealtime}
                        onCheckedChange={setIsRealtime}
                    />
                </div>
            )}

            <Card>
                <CardContent className="pt-6">
                    <Tabs key={defaultTab} defaultValue={defaultTab} className="w-full">
                        <TabsList className="grid w-full mb-6" style={{ gridTemplateColumns: `repeat(${visibleTabs.length}, 1fr)` }}>
                            {visibleTabs.map(({ key, label }) => (
                                <TabsTrigger key={key} value={key}>
                                    {label}
                                </TabsTrigger>
                            ))}
                        </TabsList>

                        {visibleTabs.map(({ key, label }) => (
                            <TabsContent key={key} value={key} className="mt-0">
                                {mode === 'override' && renderOverrideToggle(key, label)}
                                {(mode === 'global' || enabledOverrides[key]) && renderServiceFields(key)}
                            </TabsContent>
                        ))}
                    </Tabs>
                </CardContent>
            </Card>

            {apiError && <p className="text-red-500 mt-4">{apiError}</p>}

            <Button type="submit" className="w-full mt-6" disabled={isSaving}>
                {isSaving ? "Saving..." : (submitLabel || "Save Configuration")}
            </Button>
        </form>
    );
}

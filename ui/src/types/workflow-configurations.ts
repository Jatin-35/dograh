import type {
    AmbientNoiseConfigurationDefaults,
    OrganizationAiModelConfigurationV2,
    WorkflowConfigurationDefaults as GeneratedWorkflowConfigurationDefaults,
} from "@/client/types.gen";

export type WorkflowConfigurationDefaults = GeneratedWorkflowConfigurationDefaults;

export type AmbientNoiseConfiguration = Omit<
    AmbientNoiseConfigurationDefaults,
    "enabled" | "volume"
> & {
    enabled: boolean;
    volume: number;
    // Named here because Omit<> over a type with an index signature drops them.
    play_mode?: AmbientNoiseConfigurationDefaults["play_mode"];
    start_delay_secs?: number;
    sound?: AmbientNoiseConfigurationDefaults["sound"];
    storage_key?: string;
    storage_backend?: string;
    original_filename?: string;
};

export type AmbientPlayMode = NonNullable<AmbientNoiseConfigurationDefaults["play_mode"]>;
export type AmbientSound = NonNullable<AmbientNoiseConfigurationDefaults["sound"]>;
export const DEFAULT_AMBIENT_START_DELAY_SECS = 0.5;

export const AMBIENT_PLAY_MODE_OPTIONS: Array<{
    value: AmbientPlayMode;
    label: string;
    description: string;
}> = [
    {
        value: "whole_call",
        label: "Whole call",
        description: "Plays from the moment the call connects until it ends.",
    },
    {
        value: "thinking",
        label: "While the agent is thinking",
        description:
            "Plays after the caller finishes speaking until the agent replies, including tool calls. Silent while either side talks.",
    },
    {
        value: "tool_calls",
        label: "Only during tool calls",
        description:
            "Plays only while the agent waits on a tool (a lookup or API call). End-call and transfer tools don't count.",
    },
];

export const AMBIENT_SOUND_OPTIONS: Array<{ value: AmbientSound; label: string }> = [
    { value: "office", label: "Office ambience" },
    { value: "typing", label: "Keyboard typing" },
];

export type TurnStopStrategy = NonNullable<GeneratedWorkflowConfigurationDefaults["turn_stop_strategy"]>;
export type TurnStartStrategy = NonNullable<GeneratedWorkflowConfigurationDefaults["turn_start_strategy"]>;
export type NoiseReduction = NonNullable<GeneratedWorkflowConfigurationDefaults["noise_reduction"]>;
export const DEFAULT_TURN_START_MIN_WORDS = 2;

export const NOISE_REDUCTION_OPTIONS: Array<{
    value: NoiseReduction;
    label: string;
    description: string;
}> = [
    {
        value: 'off',
        label: 'Off',
        description: "The caller's audio is used as received.",
    },
    {
        value: 'rnnoise',
        label: 'RNNoise (steady background noise)',
        description: 'Removes steady background noise (fans, traffic, hum) from the caller before the agent hears it. Free; adds a little server CPU per call.',
    },
];

export const TURN_START_STRATEGY_OPTIONS: Array<{
    value: TurnStartStrategy;
    label: string;
    description: string;
}> = [
    {
        value: 'default',
        label: 'Voice activity',
        description: 'Use the platform default: external STT turn signals when available, otherwise local VAD.',
    },
    {
        value: 'min_words',
        label: 'Minimum words (recommended)',
        description: 'Wait for a minimum number of transcribed words before interrupting bot speech, so short acknowledgements like "haan" or "mhm" do not cut the bot off.',
    },
];

export interface VoicemailDetectionConfiguration {
    enabled: boolean;
    use_workflow_llm: boolean;
    provider?: string;
    model?: string;
    api_key?: string;
    system_prompt?: string;
    long_speech_timeout: number;  // seconds cutoff for long speech detection
}

export const DEFAULT_VOICEMAIL_DETECTION_CONFIGURATION: VoicemailDetectionConfiguration = {
    enabled: false,
    use_workflow_llm: true,
    long_speech_timeout: 8.0,
};

export interface TranscriptConfiguration {
    include_end_timestamps: boolean;
}

export const DEFAULT_TRANSCRIPT_CONFIGURATION: TranscriptConfiguration = {
    include_end_timestamps: false,
};

export interface ModelOverrides {
    llm?: {
        provider?: string;
        model?: string;
        api_key?: string;
        [key: string]: unknown;
    };
    tts?: {
        provider?: string;
        model?: string;
        voice?: string;
        api_key?: string;
        [key: string]: unknown;
    };
    stt?: {
        provider?: string;
        model?: string;
        api_key?: string;
        [key: string]: unknown;
    };
    realtime?: {
        provider?: string;
        model?: string;
        voice?: string;
        api_key?: string;
        [key: string]: unknown;
    };
    is_realtime?: boolean;
}

type WorkflowConfigurationBase = Omit<
    GeneratedWorkflowConfigurationDefaults,
    | "ambient_noise_configuration"
    | "max_call_duration"
    | "max_user_idle_timeout"
    | "smart_turn_stop_secs"
    | "turn_start_strategy"
    | "turn_start_min_words"
    | "turn_stop_strategy"
    | "dictionary"
    | "context_compaction_enabled"
    | "noise_reduction"
>;

export type WorkflowConfigurations = WorkflowConfigurationBase & {
    ambient_noise_configuration: AmbientNoiseConfiguration;
    max_call_duration: number;  // Maximum call duration in seconds
    max_user_idle_timeout: number;  // Maximum user idle time in seconds
    smart_turn_stop_secs: number;  // Timeout in seconds for incomplete turn detection
    turn_start_strategy: TurnStartStrategy;  // Strategy for detecting start of user turn/interruption
    turn_start_min_words: number;  // Minimum transcribed words required for minimum-word interruptions
    turn_stop_strategy: TurnStopStrategy;  // Strategy for detecting end of user turn
    dictionary?: string;  // Comma-separated words for voice agent to listen for
    voicemail_detection?: VoicemailDetectionConfiguration;
    transcript_configuration: TranscriptConfiguration;
    context_compaction_enabled: boolean;  // Summarize context on node transitions to remove stale tool calls
    noise_reduction: NoiseReduction;  // Caller-side noise suppression before STT / the realtime model
    model_overrides?: ModelOverrides;  // Per-workflow model configuration overrides
    model_configuration_v2_override?: OrganizationAiModelConfigurationV2;  // Full v2 model configuration override
    [key: string]: unknown;  // Allow additional properties for future configurations
};

const FALLBACK_WORKFLOW_CONFIGURATIONS: WorkflowConfigurations = {
    ambient_noise_configuration: {
        enabled: false,
        volume: 0.3,
        play_mode: "whole_call",
        start_delay_secs: DEFAULT_AMBIENT_START_DELAY_SECS,
        sound: "office"
    },
    max_call_duration: 300,
    max_user_idle_timeout: 10,  // 10 seconds
    smart_turn_stop_secs: 2,  // 2 seconds
    turn_start_strategy: 'min_words',  // Two words to interrupt (upstream dograh default)
    turn_start_min_words: DEFAULT_TURN_START_MIN_WORDS,
    turn_stop_strategy: 'transcription',  // Default to transcription-based detection
    dictionary: '',
    transcript_configuration: DEFAULT_TRANSCRIPT_CONFIGURATION,
    context_compaction_enabled: false,
    noise_reduction: 'off',
};

export function resolveWorkflowConfigurations(
    configurations?: Partial<WorkflowConfigurations> | null,
    defaults?: WorkflowConfigurationDefaults | null,
): WorkflowConfigurations {
    return {
        ...FALLBACK_WORKFLOW_CONFIGURATIONS,
        ...defaults,
        ...configurations,
        ambient_noise_configuration: {
            ...FALLBACK_WORKFLOW_CONFIGURATIONS.ambient_noise_configuration,
            ...defaults?.ambient_noise_configuration,
            ...configurations?.ambient_noise_configuration,
        },
        max_call_duration:
            configurations?.max_call_duration
            ?? defaults?.max_call_duration
            ?? FALLBACK_WORKFLOW_CONFIGURATIONS.max_call_duration,
        max_user_idle_timeout:
            configurations?.max_user_idle_timeout
            ?? defaults?.max_user_idle_timeout
            ?? FALLBACK_WORKFLOW_CONFIGURATIONS.max_user_idle_timeout,
        smart_turn_stop_secs:
            configurations?.smart_turn_stop_secs
            ?? defaults?.smart_turn_stop_secs
            ?? FALLBACK_WORKFLOW_CONFIGURATIONS.smart_turn_stop_secs,
        turn_start_strategy:
            configurations?.turn_start_strategy
            ?? defaults?.turn_start_strategy
            ?? FALLBACK_WORKFLOW_CONFIGURATIONS.turn_start_strategy,
        turn_start_min_words:
            configurations?.turn_start_min_words
            ?? defaults?.turn_start_min_words
            ?? FALLBACK_WORKFLOW_CONFIGURATIONS.turn_start_min_words,
        turn_stop_strategy:
            configurations?.turn_stop_strategy
            ?? defaults?.turn_stop_strategy
            ?? FALLBACK_WORKFLOW_CONFIGURATIONS.turn_stop_strategy,
        dictionary:
            configurations?.dictionary
            ?? defaults?.dictionary
            ?? FALLBACK_WORKFLOW_CONFIGURATIONS.dictionary,
        context_compaction_enabled:
            configurations?.context_compaction_enabled
            ?? defaults?.context_compaction_enabled
            ?? FALLBACK_WORKFLOW_CONFIGURATIONS.context_compaction_enabled,
        noise_reduction:
            configurations?.noise_reduction
            ?? defaults?.noise_reduction
            ?? FALLBACK_WORKFLOW_CONFIGURATIONS.noise_reduction,
        transcript_configuration: {
            ...DEFAULT_TRANSCRIPT_CONFIGURATION,
            ...(defaults?.transcript_configuration as Partial<TranscriptConfiguration> | undefined),
            ...(configurations?.transcript_configuration as Partial<TranscriptConfiguration> | undefined),
        },
    };
}

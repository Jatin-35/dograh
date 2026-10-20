import { Loader2, Pause, Play, Upload, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { getSignedUrlApiV1S3SignedUrlGet } from "@/client/sdk.gen";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { cn } from "@/lib/utils";
import {
    AMBIENT_PLAY_MODE_OPTIONS,
    AMBIENT_SOUND_OPTIONS,
    AMBIENT_VOLUME_MAX,
    AMBIENT_VOLUME_MIN,
    type AmbientNoiseConfiguration,
    type AmbientPlayMode,
    type AmbientSound,
    clampAmbientVolume,
    DEFAULT_AMBIENT_START_DELAY_SECS,
} from "@/types/workflow-configurations";

const PREVIEW_SECS = 6;
const START_DELAY_MAX = 5;

export interface AmbientNoiseUpload {
    isUploading: boolean;
    error: string | null;
    onUpload: (file: File) => void;
    onRemove: () => void;
}

interface AmbientNoiseFieldsProps {
    config: AmbientNoiseConfiguration;
    onChange: (update: Partial<AmbientNoiseConfiguration>) => void;
    /** Custom-file upload; omit where uploading isn't offered. */
    upload?: AmbientNoiseUpload;
}

type SoundChoice = AmbientSound | "custom";

/** Plays a few seconds of the chosen sound at the chosen volume. */
function useAmbientPreview() {
    const [playing, setPlaying] = useState(false);
    const [loading, setLoading] = useState(false);
    const contextRef = useRef<AudioContext | null>(null);
    const sourceRef = useRef<AudioBufferSourceNode | null>(null);
    const elementRef = useRef<HTMLAudioElement | null>(null);
    const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

    const stop = useCallback(() => {
        sourceRef.current?.stop();
        sourceRef.current = null;
        elementRef.current?.pause();
        elementRef.current = null;
        if (timerRef.current) clearTimeout(timerRef.current);
        timerRef.current = null;
        setPlaying(false);
    }, []);

    useEffect(() => stop, [stop]);

    const play = useCallback(
        async (config: AmbientNoiseConfiguration, choice: SoundChoice) => {
            stop();
            setLoading(true);
            try {
                const volume = clampAmbientVolume(config.volume);
                if (choice === "custom" && config.storage_key) {
                    // Uploaded files come from object storage; a plain audio
                    // element plays them without needing CORS (capped at 100%).
                    const result = await getSignedUrlApiV1S3SignedUrlGet({
                        query: { key: config.storage_key, storage_backend: config.storage_backend },
                    });
                    if (!result.data?.url) throw new Error("Could not load the uploaded file");
                    const audio = new Audio(result.data.url);
                    audio.volume = Math.min(1, volume);
                    elementRef.current = audio;
                    await audio.play();
                } else {
                    // Built-in sounds are served by the app itself, so Web Audio
                    // can apply the full volume range, including above 100%.
                    const context = contextRef.current ?? new AudioContext();
                    contextRef.current = context;
                    const response = await fetch(`/sounds/ambient-${choice}-preview.wav`);
                    const buffer = await context.decodeAudioData(await response.arrayBuffer());
                    const source = context.createBufferSource();
                    const gain = context.createGain();
                    source.buffer = buffer;
                    gain.gain.value = volume;
                    source.connect(gain).connect(context.destination);
                    source.onended = () => setPlaying(false);
                    sourceRef.current = source;
                    source.start();
                }
                setPlaying(true);
                timerRef.current = setTimeout(stop, PREVIEW_SECS * 1000);
            } finally {
                setLoading(false);
            }
        },
        [stop],
    );

    return { playing, loading, play, stop };
}

/** Everything under "Use Ambient Noise": when, which sound, how loud, how soon. */
export function AmbientNoiseFields({ config, onChange, upload }: AmbientNoiseFieldsProps) {
    const fileInputRef = useRef<HTMLInputElement>(null);
    const preview = useAmbientPreview();
    const [previewError, setPreviewError] = useState<string | null>(null);

    const playMode: AmbientPlayMode = config.play_mode ?? "whole_call";
    const playModeOption = AMBIENT_PLAY_MODE_OPTIONS.find((option) => option.value === playMode);
    const choice: SoundChoice = config.storage_key ? "custom" : (config.sound ?? "office");
    const volumePercent = Math.round(clampAmbientVolume(config.volume) * 100);
    const startDelay = config.start_delay_secs ?? DEFAULT_AMBIENT_START_DELAY_SECS;
    const choices: Array<{ value: SoundChoice; label: string }> = [
        ...AMBIENT_SOUND_OPTIONS,
        ...(upload || config.storage_key ? [{ value: "custom" as const, label: "Custom file" }] : []),
    ];

    const chooseSound = (value: SoundChoice) => {
        preview.stop();
        if (value === "custom") {
            if (!config.storage_key) fileInputRef.current?.click();
            return;
        }
        // Clearing the file here (not via upload.onRemove) also works where
        // uploading isn't offered, such as the configurations dialog.
        onChange({ sound: value, storage_key: undefined, storage_backend: undefined, original_filename: undefined });
    };

    const togglePreview = async () => {
        setPreviewError(null);
        if (preview.playing) {
            preview.stop();
            return;
        }
        try {
            await preview.play(config, choice);
        } catch {
            setPreviewError("Couldn't play the preview");
        }
    };

    return (
        <div className="space-y-5">
            <div className="space-y-2">
                <Label htmlFor="ambient-play-mode" className="text-xs">When to play</Label>
                <Select value={playMode} onValueChange={(value) => onChange({ play_mode: value as AmbientPlayMode })}>
                    <SelectTrigger id="ambient-play-mode" className="w-full">
                        <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                        {AMBIENT_PLAY_MODE_OPTIONS.map((option) => (
                            <SelectItem key={option.value} value={option.value}>
                                {option.label}
                            </SelectItem>
                        ))}
                    </SelectContent>
                </Select>
                {playModeOption && <p className="text-xs text-muted-foreground">{playModeOption.description}</p>}
            </div>

            <div className="space-y-2">
                <Label className="text-xs">Sound</Label>
                <div className="flex flex-wrap items-center gap-2">
                    <div role="radiogroup" aria-label="Sound" className="inline-flex rounded-md border p-0.5">
                        {choices.map((option) => (
                            <button
                                key={option.value}
                                type="button"
                                role="radio"
                                aria-checked={choice === option.value}
                                onClick={() => chooseSound(option.value)}
                                className={cn(
                                    "rounded px-3 py-1.5 text-sm transition-colors",
                                    choice === option.value
                                        ? "bg-primary text-primary-foreground"
                                        : "text-muted-foreground hover:bg-muted",
                                )}
                            >
                                {option.label}
                            </button>
                        ))}
                    </div>
                    <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        onClick={togglePreview}
                        disabled={preview.loading || (choice === "custom" && !config.storage_key)}
                    >
                        {preview.loading ? (
                            <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
                        ) : preview.playing ? (
                            <Pause className="mr-1.5 h-3.5 w-3.5" />
                        ) : (
                            <Play className="mr-1.5 h-3.5 w-3.5" />
                        )}
                        {preview.playing ? "Stop" : "Preview"}
                    </Button>
                </div>

                {choice === "custom" && config.storage_key && (
                    <div className="flex items-center gap-2 rounded-md border bg-muted/10 p-2">
                        <code className="flex-1 truncate rounded bg-muted px-1.5 py-0.5 font-mono text-xs">
                            {config.original_filename || "Custom audio"}
                        </code>
                        {upload && (
                            <Button
                                type="button"
                                size="sm"
                                variant="ghost"
                                className="h-6 w-6 shrink-0 p-0"
                                aria-label="Remove custom file"
                                onClick={() => {
                                    preview.stop();
                                    upload.onRemove();
                                }}
                            >
                                <X className="h-3.5 w-3.5" />
                            </Button>
                        )}
                    </div>
                )}
                {upload && (
                    <>
                        <input
                            ref={fileInputRef}
                            type="file"
                            accept="audio/*"
                            className="hidden"
                            onChange={(e) => {
                                const file = e.target.files?.[0];
                                if (file) upload.onUpload(file);
                                e.target.value = "";
                            }}
                        />
                        {upload.isUploading && (
                            <p className="flex items-center text-xs text-muted-foreground">
                                <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> Uploading...
                            </p>
                        )}
                        {!config.storage_key && !upload.isUploading && (
                            <button
                                type="button"
                                onClick={() => fileInputRef.current?.click()}
                                className="flex items-center text-xs text-muted-foreground underline-offset-2 hover:underline"
                            >
                                <Upload className="mr-1.5 h-3.5 w-3.5" /> Or upload your own audio file (max 10 MB)
                            </button>
                        )}
                        {upload.error && <p className="text-xs text-destructive">{upload.error}</p>}
                    </>
                )}
                {previewError && <p className="text-xs text-destructive">{previewError}</p>}
                {playMode !== "whole_call" && choice === "office" && (
                    <p className="text-xs text-muted-foreground">
                        Office ambience is a soft background hum and is hard to hear in short waits. Keyboard typing
                        at 100% works best here.
                    </p>
                )}
            </div>

            <div className="space-y-2">
                <div className="flex items-baseline justify-between">
                    <Label htmlFor="ambient-volume" className="text-xs">Volume</Label>
                    <span className="text-xs font-medium tabular-nums">{volumePercent}%</span>
                </div>
                <input
                    id="ambient-volume"
                    type="range"
                    min={AMBIENT_VOLUME_MIN}
                    max={AMBIENT_VOLUME_MAX}
                    step={0.05}
                    value={clampAmbientVolume(config.volume)}
                    onChange={(e) => onChange({ volume: clampAmbientVolume(parseFloat(e.target.value)) })}
                    className="w-full cursor-pointer accent-primary"
                />
                <div className="relative h-4 text-[11px] text-muted-foreground">
                    <span className="absolute left-0">{Math.round(AMBIENT_VOLUME_MIN * 100)}%</span>
                    <span
                        className="absolute -translate-x-1/2"
                        style={{ left: `${((1 - AMBIENT_VOLUME_MIN) / (AMBIENT_VOLUME_MAX - AMBIENT_VOLUME_MIN)) * 100}%` }}
                    >
                        100%
                    </span>
                    <span className="absolute right-0">{Math.round(AMBIENT_VOLUME_MAX * 100)}%</span>
                </div>
            </div>

            {playMode !== "whole_call" && (
                <div className="space-y-2">
                    <div className="flex items-baseline justify-between">
                        <Label htmlFor="ambient-start-delay" className="text-xs">Start after</Label>
                        <span className="text-xs font-medium tabular-nums">{startDelay.toFixed(1)} s</span>
                    </div>
                    <input
                        id="ambient-start-delay"
                        type="range"
                        min={0}
                        max={START_DELAY_MAX}
                        step={0.1}
                        value={startDelay}
                        onChange={(e) => onChange({ start_delay_secs: parseFloat(e.target.value) })}
                        className="w-full cursor-pointer accent-primary"
                    />
                    <p className="text-xs text-muted-foreground">
                        The sound starts only once a pause lasts this long, so quick replies stay silent.
                    </p>
                </div>
            )}
        </div>
    );
}

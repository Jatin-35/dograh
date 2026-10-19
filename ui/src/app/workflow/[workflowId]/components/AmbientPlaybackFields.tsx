import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import {
    AMBIENT_PLAY_MODE_OPTIONS,
    AMBIENT_SOUND_OPTIONS,
    type AmbientNoiseConfiguration,
    type AmbientPlayMode,
    type AmbientSound,
    DEFAULT_AMBIENT_START_DELAY_SECS,
} from "@/types/workflow-configurations";

interface AmbientPlaybackFieldsProps {
    config: AmbientNoiseConfiguration;
    onChange: (update: Partial<AmbientNoiseConfiguration>) => void;
    /** Hide the built-in sound picker (e.g. when a custom file is uploaded). */
    showSoundPicker?: boolean;
}

/** When the ambient sound plays, after how long, and which built-in sound. */
export function AmbientPlaybackFields({ config, onChange, showSoundPicker = true }: AmbientPlaybackFieldsProps) {
    const playMode: AmbientPlayMode = config.play_mode ?? "whole_call";
    const selected = AMBIENT_PLAY_MODE_OPTIONS.find((option) => option.value === playMode);

    return (
        <>
            <div className="space-y-2">
                <Label htmlFor="ambient-play-mode" className="text-xs">When to play</Label>
                <Select value={playMode} onValueChange={(value) => onChange({ play_mode: value as AmbientPlayMode })}>
                    <SelectTrigger id="ambient-play-mode">
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
                {selected && <p className="text-xs text-muted-foreground">{selected.description}</p>}
            </div>

            {playMode !== "whole_call" && (
                <div className="space-y-2">
                    <Label htmlFor="ambient-start-delay" className="text-xs">Start after (seconds)</Label>
                    <Input
                        id="ambient-start-delay"
                        type="number"
                        step="0.1"
                        min="0"
                        max="5"
                        value={config.start_delay_secs ?? DEFAULT_AMBIENT_START_DELAY_SECS}
                        onChange={(e) => {
                            const value = parseFloat(e.target.value);
                            if (!isNaN(value)) onChange({ start_delay_secs: Math.min(5, Math.max(0, value)) });
                        }}
                    />
                    <p className="text-xs text-muted-foreground">
                        The sound starts only once a pause lasts this long, so quick replies stay silent.
                    </p>
                </div>
            )}

            {showSoundPicker && (
                <div className="space-y-2">
                    <Label htmlFor="ambient-sound" className="text-xs">Sound</Label>
                    <Select
                        value={config.sound ?? "office"}
                        onValueChange={(value) => onChange({ sound: value as AmbientSound })}
                    >
                        <SelectTrigger id="ambient-sound">
                            <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                            {AMBIENT_SOUND_OPTIONS.map((option) => (
                                <SelectItem key={option.value} value={option.value}>
                                    {option.label}
                                </SelectItem>
                            ))}
                        </SelectContent>
                    </Select>
                </div>
            )}
        </>
    );
}

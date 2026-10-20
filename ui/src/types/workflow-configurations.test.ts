import { describe, expect, it } from 'vitest';

import { clampAmbientVolume, resolveWorkflowConfigurations } from './workflow-configurations';

describe('ambient noise settings on load', () => {
    it.each([
        [0, 0.1],
        [0.05, 0.1],
        [3, 1.5],
        [1.6, 1.5],
        [0.3, 0.3],
        [1.5, 1.5],
    ])('a stored volume of %s loads as %s so saving the page is accepted', (stored, loaded) => {
        const resolved = resolveWorkflowConfigurations({
            ambient_noise_configuration: { enabled: true, volume: stored },
        });
        expect(resolved.ambient_noise_configuration.volume).toBe(loaded);
    });

    it('keeps the new play-mode fields and an uploaded file as stored', () => {
        const resolved = resolveWorkflowConfigurations({
            ambient_noise_configuration: {
                enabled: true,
                volume: 1,
                play_mode: 'tool_calls',
                start_delay_secs: 0.3,
                sound: 'typing',
                storage_key: 'ambient/x.mp3',
                original_filename: 'x.mp3',
            },
        });
        expect(resolved.ambient_noise_configuration).toMatchObject({
            play_mode: 'tool_calls',
            start_delay_secs: 0.3,
            sound: 'typing',
            storage_key: 'ambient/x.mp3',
        });
    });

    it('fills defaults for an agent that never set ambient noise', () => {
        const resolved = resolveWorkflowConfigurations({});
        expect(resolved.ambient_noise_configuration).toMatchObject({
            enabled: false,
            volume: 0.3,
            play_mode: 'whole_call',
            sound: 'office',
        });
    });

    it('treats a non-number volume as the default', () => {
        expect(clampAmbientVolume(Number.NaN)).toBe(0.3);
        expect(clampAmbientVolume(Number('loud'))).toBe(0.3);
    });
});

import { act, fireEvent, render, screen } from '@testing-library/react';
import { useState } from 'react';
import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import type { AmbientNoiseConfiguration } from '@/types/workflow-configurations';

const h = vi.hoisted(() => ({
    signedUrl: vi.fn(),
    started: [] as Array<{ gain: number }>,
}));

vi.mock('@/client/sdk.gen', () => ({
    getSignedUrlApiV1S3SignedUrlGet: (...args: unknown[]) => h.signedUrl(...args),
}));

import { AmbientNoiseFields, type AmbientNoiseUpload } from './AmbientNoiseFields';

beforeAll(() => {
    globalThis.ResizeObserver = class {
        observe() {}
        unobserve() {}
        disconnect() {}
    };
    Element.prototype.hasPointerCapture ??= () => false;
    Element.prototype.releasePointerCapture ??= () => {};
    Element.prototype.scrollIntoView ??= () => {};
    // Web Audio stand-in: records the gain each preview plays at.
    class FakeAudioContext {
        destination = {};
        createBufferSource() {
            const node = {
                buffer: null,
                onended: null as null | (() => void),
                connect: (gain: { connect: () => void }) => gain,
                start: () => h.started.push({ gain: currentGain.gain.value }),
                stop: () => {},
            };
            return node;
        }
        createGain() {
            currentGain = { gain: { value: 1 }, connect: () => ({}) };
            return currentGain;
        }
        async decodeAudioData() {
            return {};
        }
    }
    let currentGain = { gain: { value: 1 }, connect: () => ({}) };
    vi.stubGlobal('AudioContext', FakeAudioContext);
    vi.stubGlobal('fetch', vi.fn(async () => ({ arrayBuffer: async () => new ArrayBuffer(8) })));
});

beforeEach(() => {
    h.started.length = 0;
    h.signedUrl.mockReset();
    vi.mocked(fetch).mockClear();
});

const BASE: AmbientNoiseConfiguration = { enabled: true, volume: 1.0, play_mode: 'thinking', sound: 'typing' };

function Harness({ initial = BASE, upload }: { initial?: AmbientNoiseConfiguration; upload?: AmbientNoiseUpload }) {
    const [config, setConfig] = useState(initial);
    return (
        <>
            <AmbientNoiseFields config={config} onChange={(u) => setConfig((c) => ({ ...c, ...u }))} upload={upload} />
            <pre data-testid="config">{JSON.stringify(config)}</pre>
        </>
    );
}

const current = () => JSON.parse(screen.getByTestId('config').textContent!) as AmbientNoiseConfiguration;

describe('AmbientNoiseFields', () => {
    it('shows volume as a percentage and saves it in the 0.1-1.5 range', () => {
        render(<Harness />);
        expect(screen.getByText('100%', { selector: 'span.tabular-nums' })).toBeTruthy();
        fireEvent.change(screen.getByLabelText('Volume'), { target: { value: '1.5' } });
        expect(current().volume).toBe(1.5);
        expect(screen.getByText('150%', { selector: 'span.tabular-nums' })).toBeTruthy();
        fireEvent.change(screen.getByLabelText('Volume'), { target: { value: '0.05' } });
        expect(current().volume).toBe(0.1);
    });

    it('picks a built-in sound with one click', () => {
        render(<Harness />);
        fireEvent.click(screen.getByRole('radio', { name: 'Office ambience' }));
        expect(current().sound).toBe('office');
        expect(screen.getByRole('radio', { name: 'Office ambience' }).getAttribute('aria-checked')).toBe('true');
    });

    it('only shows "Start after" for the pause modes', () => {
        const { unmount } = render(<Harness />);
        expect(screen.getByLabelText('Start after')).toBeTruthy();
        fireEvent.change(screen.getByLabelText('Start after'), { target: { value: '0.3' } });
        expect(current().start_delay_secs).toBe(0.3);
        unmount();
        render(<Harness initial={{ ...BASE, play_mode: 'whole_call' }} />);
        expect(screen.queryByLabelText('Start after')).toBeNull();
    });

    it('recommends typing only when office is chosen for short waits', () => {
        const { unmount } = render(<Harness initial={{ ...BASE, sound: 'office' }} />);
        expect(screen.getByText(/hard to hear in short waits/)).toBeTruthy();
        unmount();
        render(<Harness initial={{ ...BASE, sound: 'office', play_mode: 'whole_call' }} />);
        expect(screen.queryByText(/hard to hear in short waits/)).toBeNull();
    });

    it('previews a built-in sound at the chosen volume, above 100% too', async () => {
        render(<Harness initial={{ ...BASE, volume: 1.5 }} />);
        await act(async () => {
            fireEvent.click(screen.getByRole('button', { name: /Preview/ }));
        });
        expect(fetch).toHaveBeenCalledWith('/sounds/ambient-typing-preview.wav');
        expect(h.started).toEqual([{ gain: 1.5 }]);
        expect(screen.getByRole('button', { name: /Stop/ })).toBeTruthy();
    });

    it('offers upload, shows the uploaded file and removes only the file', () => {
        const onUpload = vi.fn();
        const onRemove = vi.fn();
        const upload = { isUploading: false, error: null, onUpload, onRemove };
        const { unmount } = render(<Harness upload={upload} />);
        const input = document.querySelector('input[type=file]') as HTMLInputElement;
        const file = new File(['x'], 'hold.mp3', { type: 'audio/mpeg' });
        fireEvent.change(input, { target: { files: [file] } });
        expect(onUpload).toHaveBeenCalledWith(file);
        unmount();

        render(
            <Harness
                upload={upload}
                initial={{ ...BASE, storage_key: 'ambient/x.mp3', storage_backend: 'minio', original_filename: 'hold.mp3' }}
            />,
        );
        expect(screen.getByRole('radio', { name: 'Custom file' }).getAttribute('aria-checked')).toBe('true');
        expect(screen.getByText('hold.mp3')).toBeTruthy();
        fireEvent.click(screen.getByRole('button', { name: 'Remove custom file' }));
        expect(onRemove).toHaveBeenCalled();
    });

    it.each([
        ['the settings page', { isUploading: false, error: null, onUpload: vi.fn(), onRemove: vi.fn() }],
        ['the configurations dialog (no upload)', undefined],
    ])('switching from a custom file to a built-in sound removes the file on %s', (_where, upload) => {
        render(
            <Harness
                upload={upload}
                initial={{ ...BASE, storage_key: 'ambient/x.mp3', storage_backend: 'minio', original_filename: 'hold.mp3' }}
            />,
        );
        fireEvent.click(screen.getByRole('radio', { name: 'Office ambience' }));
        const saved = current();
        expect(saved.sound).toBe('office');
        expect(saved.storage_key).toBeUndefined();
        expect(saved.original_filename).toBeUndefined();
        expect(screen.getByRole('radio', { name: 'Office ambience' }).getAttribute('aria-checked')).toBe('true');
    });

    it('hides the custom option where uploading is not offered', () => {
        render(<Harness />);
        expect(screen.queryByRole('radio', { name: 'Custom file' })).toBeNull();
        expect(screen.queryByText(/upload your own/i)).toBeNull();
    });
});

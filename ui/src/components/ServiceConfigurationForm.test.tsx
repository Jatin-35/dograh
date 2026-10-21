import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeAll, describe, expect, it, vi } from 'vitest';

import {
    type ProviderSchema,
    type ServiceConfigurationDefaults,
    ServiceConfigurationForm,
} from './ServiceConfigurationForm';
import { STT_SCHEMAS } from './ServiceConfigurationForm.fixtures';

vi.mock('@/context/UserConfigContext', () => ({ useUserConfig: () => ({ userConfig: null }) }));
vi.mock('@/client/sdk.gen', () => ({ getDefaultConfigurationsApiV1UserConfigurationsDefaultsGet: vi.fn() }));
vi.mock('@/components/VoiceSelector', () => ({ VoiceSelector: () => null }));

beforeAll(() => {
    // jsdom has no resize observer, pointer capture or scrolling; Radix Switch and Select need them.
    globalThis.ResizeObserver ??= class {
        observe() {}
        unobserve() {}
        disconnect() {}
    };
    Element.prototype.hasPointerCapture ??= () => false;
    Element.prototype.releasePointerCapture ??= () => {};
    Element.prototype.scrollIntoView ??= () => {};
});

const DEFAULTS = {
    llm: {},
    tts: {},
    stt: STT_SCHEMAS as unknown as Record<string, ProviderSchema>,
    embeddings: {},
    realtime: {},
    default_providers: { stt: 'soniox' },
} as ServiceConfigurationDefaults;

function renderForm(sttConfig?: Record<string, unknown>) {
    const onSave = vi.fn().mockResolvedValue(undefined);
    render(
        <ServiceConfigurationForm
            mode="global"
            onSave={onSave}
            configurationDefaults={DEFAULTS}
            initialConfig={sttConfig ? { stt: sttConfig } : {}}
            forceRealtime={false}
        />,
    );
    fireEvent.mouseDown(screen.getByRole('tab', { name: 'Transcriber' }));
    fireEvent.click(screen.getByRole('tab', { name: 'Transcriber' }));
    return onSave;
}

async function choose(trigger: HTMLElement, optionName: string) {
    fireEvent.keyDown(trigger, { key: 'ArrowDown' });
    const option = await screen.findByRole('option', { name: optionName });
    fireEvent.keyDown(option, { key: 'Enter' });
}

function chips() {
    return screen.queryAllByRole('button', { name: /^Remove / }).map(b => b.getAttribute('aria-label'));
}

async function save(onSave: ReturnType<typeof vi.fn>) {
    fireEvent.click(screen.getByRole('button', { name: 'Save Configuration' }));
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    return onSave.mock.calls[0][0].stt as Record<string, unknown>;
}

function openAdvanced() {
    fireEvent.click(screen.getByRole('button', { name: 'Advanced' }));
}

describe('Soniox settings in the model form', () => {
    it('shows the main settings with Hinglish defaults and hides the advanced ones', async () => {
        renderForm();
        expect(await screen.findByText('Languages')).toBeTruthy();
        expect(chips()).toEqual(['Remove Hindi', 'Remove English']);
        expect(screen.getByRole('switch', { name: 'Strict languages' }).getAttribute('aria-checked')).toBe('false');
        expect(screen.getByLabelText('Domain context')).toBeTruthy();
        // Advanced is collapsed, and its tuning fields need the switch.
        expect(screen.queryByRole('switch', { name: 'Use Soniox turn detection' })).toBeNull();
        expect(screen.queryByText('Max wait (ms)')).toBeNull();
    });

    it('adds and removes languages and saves them with the other main settings', async () => {
        const onSave = renderForm();
        await screen.findByText('Languages');
        await choose(screen.getByRole('combobox', { name: 'Add Languages' }), 'Marathi');
        expect(chips()).toEqual(['Remove Hindi', 'Remove English', 'Remove Marathi']);
        fireEvent.click(screen.getByRole('button', { name: 'Remove English' }));
        expect(chips()).toEqual(['Remove Hindi', 'Remove Marathi']);
        fireEvent.click(screen.getByRole('switch', { name: 'Strict languages' }));
        fireEvent.change(screen.getByLabelText('Domain context'), {
            target: { value: 'Think Gas PNG customer support' },
        });

        const stt = await save(onSave);
        expect(stt.provider).toBe('soniox');
        expect(stt.model).toBe('stt-rt-v5');
        expect(stt.languages).toEqual(['hi', 'mr']);
        expect(stt.strict_languages).toBe(true);
        expect(stt.domain_context).toBe('Think Gas PNG customer support');
        expect(stt.use_soniox_turn_detection).toBe(false);
    });

    it('lists only languages not already picked', async () => {
        renderForm();
        await screen.findByText('Languages');
        fireEvent.keyDown(screen.getByRole('combobox', { name: 'Add Languages' }), { key: 'ArrowDown' });
        const listbox = await screen.findByRole('listbox');
        const names = within(listbox).getAllByRole('option').map(o => o.textContent);
        expect(names).not.toContain('Hindi');
        expect(names).not.toContain('English');
        expect(names[0]).toBe('Marathi');
        expect(names).toContain('Tamil');
    });

    it('shows the turn tuning only once Soniox turn detection is on, and saves blanks as not set', async () => {
        const onSave = renderForm();
        await screen.findByText('Languages');
        openAdvanced();
        const turnSwitch = await screen.findByRole('switch', { name: 'Use Soniox turn detection' });
        expect(screen.queryByText('Max wait (ms)')).toBeNull();

        fireEvent.click(turnSwitch);
        const maxWait = await screen.findByLabelText('Max wait (ms)');
        expect(screen.getByLabelText('Sensitivity')).toBeTruthy();
        expect(screen.getByLabelText('Speed level')).toBeTruthy();
        fireEvent.change(maxWait, { target: { value: '1200' } });
        fireEvent.change(screen.getByLabelText('Speed level'), { target: { value: '2' } });

        const stt = await save(onSave);
        expect(stt.use_soniox_turn_detection).toBe(true);
        expect(stt.max_endpoint_delay_ms).toBe(1200);
        expect(stt.endpoint_speed_level).toBe(2);
        expect(stt.endpoint_sensitivity).toBeNull(); // left blank: Soniox's default
    });

    it('turning the switch back off hides the tuning again', async () => {
        renderForm();
        await screen.findByText('Languages');
        openAdvanced();
        const turnSwitch = await screen.findByRole('switch', { name: 'Use Soniox turn detection' });
        fireEvent.click(turnSwitch);
        expect(await screen.findByLabelText('Max wait (ms)')).toBeTruthy();
        fireEvent.click(turnSwitch);
        await waitFor(() => expect(screen.queryByLabelText('Max wait (ms)')).toBeNull());
    });

    it('loads saved settings back into the form', async () => {
        const onSave = renderForm({
            provider: 'soniox',
            model: 'stt-rt-v5',
            languages: ['mr', 'hi'],
            strict_languages: true,
            domain_context: 'Vectus water tanks',
            use_soniox_turn_detection: true,
            max_endpoint_delay_ms: 1500,
            endpoint_sensitivity: null,
            endpoint_speed_level: null,
        });
        await screen.findByText('Languages');
        expect(chips()).toEqual(['Remove Marathi', 'Remove Hindi']);
        expect(screen.getByRole('switch', { name: 'Strict languages' }).getAttribute('aria-checked')).toBe('true');
        expect((screen.getByLabelText('Domain context') as HTMLTextAreaElement).value).toBe('Vectus water tanks');
        openAdvanced();
        expect((await screen.findByLabelText('Max wait (ms)') as HTMLInputElement).value).toBe('1500');

        const stt = await save(onSave);
        expect(stt.languages).toEqual(['mr', 'hi']);
        expect(stt.max_endpoint_delay_ms).toBe(1500);
        expect(stt.endpoint_sensitivity).toBeNull();
    });

    it('lets every language be removed (auto-detect)', async () => {
        const onSave = renderForm();
        await screen.findByText('Languages');
        fireEvent.click(screen.getByRole('button', { name: 'Remove Hindi' }));
        fireEvent.click(screen.getByRole('button', { name: 'Remove English' }));
        expect(screen.getByText('None selected')).toBeTruthy();
        const stt = await save(onSave);
        expect(stt.languages).toEqual([]);
    });
});

describe('other providers are unchanged', () => {
    it('Deepgram still shows its dropdowns and saves plain values', async () => {
        const onSave = renderForm({ provider: 'deepgram', model: 'nova-3', language: 'hi' });
        await screen.findByText('language');
        expect(screen.queryByText('Advanced')).toBeNull();
        expect(screen.queryByRole('switch')).toBeNull();
        const stt = await save(onSave);
        expect(stt).toMatchObject({ provider: 'deepgram', model: 'nova-3', language: 'hi' });
    });
});

import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeAll, describe, expect, it, vi } from 'vitest';

import {
    type ProviderSchema,
    type ServiceConfigurationDefaults,
    ServiceConfigurationForm,
} from './ServiceConfigurationForm';

vi.mock('@/context/UserConfigContext', () => ({ useUserConfig: () => ({ userConfig: null }) }));
vi.mock('@/client/sdk.gen', () => ({ getDefaultConfigurationsApiV1UserConfigurationsDefaultsGet: vi.fn() }));
vi.mock('@/components/VoiceSelector', () => ({ VoiceSelector: () => null }));

beforeAll(() => {
    globalThis.ResizeObserver ??= class {
        observe() {}
        unobserve() {}
        disconnect() {}
    };
    Element.prototype.hasPointerCapture ??= () => false;
    Element.prototype.releasePointerCapture ??= () => {};
    Element.prototype.scrollIntoView ??= () => {};
});

// The backend's InworldRealtimeLLMConfiguration schema, as the API serves it.
const INWORLD_SCHEMA = {
    "description": "One Inworld session runs speech-to-text, any LLM through Inworld's router (OpenAI, Google, Anthropic...), and Inworld voices. Switch the LLM with the model field.",
    "properties": {
        "provider": {
            "const": "inworld_realtime",
            "default": "inworld_realtime",
            "title": "Provider",
            "type": "string"
        },
        "api_key": {
            "anyOf": [
                {
                    "type": "string"
                },
                {
                    "items": {
                        "type": "string"
                    },
                    "type": "array"
                }
            ],
            "title": "Api Key"
        },
        "model": {
            "allow_custom_input": true,
            "default": "openai/gpt-4.1-mini",
            "description": "LLM, as <provider>/<model> through Inworld's router.",
            "examples": [
                "openai/gpt-4.1-mini",
                "openai/gpt-4.1-nano",
                "openai/gpt-5-mini",
                "google/gemini-2.5-flash-lite",
                "google/gemini-2.5-flash",
                "anthropic/claude-haiku-4-5",
                "inworld/models/gemma-4-31b-it",
                "inworld/models/gemma-4-26b-a4b-it"
            ],
            "section": "LLM",
            "title": "Model",
            "type": "string"
        },
        "stt_model": {
            "allow_custom_input": true,
            "default": "inworld/inworld-stt-1",
            "description": "Speech-to-text model. Soniox heard Hindi brand names best in tests but ends turns more slowly.",
            "examples": [
                "inworld/inworld-stt-1",
                "soniox/stt-rt-v5",
                "soniox/stt-rt-v4",
                "inworld/inworld-stt-1-preview",
                "assemblyai/universal-streaming-multilingual",
                "assemblyai/u3-rt-pro"
            ],
            "label": "Model",
            "section": "Transcriber",
            "title": "Stt Model",
            "type": "string"
        },
        "language": {
            "allow_custom_input": true,
            "anyOf": [
                {
                    "type": "string"
                },
                {
                    "type": "null"
                }
            ],
            "default": "hi",
            "description": "Language the caller speaks (hi, en...). Sent to speech-to-text and the voice.",
            "examples": [
                "hi",
                "en"
            ],
            "section": "Transcriber",
            "title": "Language"
        },
        "expected_words": {
            "anyOf": [
                {
                    "type": "string"
                },
                {
                    "type": "null"
                }
            ],
            "default": null,
            "description": "Words callers are likely to say, comma-separated: brand, product and place names (e.g. Vectus, Puff, Cool, Samba). Helps speech-to-text hear them correctly.",
            "label": "Expected words",
            "section": "Transcriber",
            "title": "Expected Words"
        },
        "temperature": {
            "anyOf": [
                {
                    "maximum": 2.0,
                    "minimum": 0.0,
                    "type": "number"
                },
                {
                    "type": "null"
                }
            ],
            "default": null,
            "description": "Lower is steadier and more literal. Empty uses the model's default.",
            "section": "LLM",
            "title": "Temperature"
        },
        "tts_model": {
            "allow_custom_input": true,
            "default": "inworld-tts-2",
            "description": "Voice model. Flash is faster and cheaper.",
            "examples": [
                "inworld-tts-2",
                "inworld-tts-2-flash"
            ],
            "label": "Model",
            "section": "Voice",
            "title": "Tts Model",
            "type": "string"
        },
        "voice": {
            "allow_custom_input": true,
            "default": "Riya",
            "description": "Inworld voice.",
            "examples": [
                "Riya",
                "Aanya",
                "Meher",
                "Seema",
                "Aarav",
                "Manoj",
                "Ashley",
                "Sarah",
                "Clive"
            ],
            "section": "Voice",
            "title": "Voice",
            "type": "string"
        },
        "speed": {
            "advanced": true,
            "anyOf": [
                {
                    "maximum": 1.5,
                    "minimum": 0.25,
                    "type": "number"
                },
                {
                    "type": "null"
                }
            ],
            "default": null,
            "description": "Speaking speed, 0.25-1.5. Empty uses 1.0.",
            "section": "Voice",
            "title": "Speed"
        },
        "delivery_mode": {
            "advanced": true,
            "default": "default",
            "description": "stable is the most consistent; creative the most expressive (TTS-2 only).",
            "enum": [
                "default",
                "stable",
                "balanced",
                "creative"
            ],
            "label": "Delivery",
            "section": "Voice",
            "title": "Delivery Mode",
            "type": "string"
        },
        "eagerness": {
            "default": "low",
            "description": "How soon the bot decides the caller has finished. Inworld's presets wait at most 2 s (low), 1 s (medium) or 0.8 s (high) of silence.",
            "enum": [
                "low",
                "medium",
                "high"
            ],
            "label": "Eagerness",
            "section": "Turn-taking & noise",
            "title": "Eagerness",
            "type": "string"
        },
        "speech_threshold": {
            "anyOf": [
                {
                    "maximum": 1.0,
                    "minimum": 0.0,
                    "type": "number"
                },
                {
                    "type": "null"
                }
            ],
            "default": null,
            "description": "0-1; how clearly sound must be speech to count as the caller. Raise it (0.5-0.7) if background noise or echo interrupts the bot or keeps it waiting. Empty uses the eagerness preset (0.25-0.35).",
            "label": "Speech threshold",
            "section": "Turn-taking & noise",
            "title": "Speech Threshold"
        },
        "min_silence_ms": {
            "advanced": true,
            "anyOf": [
                {
                    "maximum": 5000,
                    "minimum": 20,
                    "type": "integer"
                },
                {
                    "type": "null"
                }
            ],
            "default": null,
            "description": "Shortest silence (ms) before the caller's turn can end. Empty uses the eagerness preset (750 / 400 / 160).",
            "label": "Silence before reply (ms)",
            "section": "Turn-taking & noise",
            "title": "Min Silence Ms"
        },
        "max_silence_ms": {
            "advanced": true,
            "anyOf": [
                {
                    "maximum": 5000,
                    "minimum": 20,
                    "type": "integer"
                },
                {
                    "type": "null"
                }
            ],
            "default": null,
            "description": "Silence (ms) after which the turn ends even if unsure. Empty uses the eagerness preset (2000 / 1000 / 800).",
            "label": "Longest wait (ms)",
            "section": "Turn-taking & noise",
            "title": "Max Silence Ms"
        },
        "turn_confidence": {
            "advanced": true,
            "anyOf": [
                {
                    "maximum": 1.0,
                    "minimum": 0.0,
                    "type": "number"
                },
                {
                    "type": "null"
                }
            ],
            "default": null,
            "description": "0-1; how sure Inworld must be that the caller finished before replying. Empty uses the eagerness preset (0.90 / 0.75 / 0.65).",
            "label": "End-of-turn confidence",
            "section": "Turn-taking & noise",
            "title": "Turn Confidence"
        }
    },
    "provider_docs_url": "https://docs.inworld.ai/api-reference/realtimeAPI/realtime/realtime-websocket",
    "required": [
        "api_key"
    ],
    "sections": [
        "Transcriber",
        "LLM",
        "Voice",
        "Turn-taking & noise"
    ],
    "title": "Inworld Realtime",
    "type": "object"
} as unknown as ProviderSchema;

const LLM_SCHEMA = {
    title: 'OpenAI',
    properties: {
        provider: { type: 'string', default: 'openai' },
        api_key: { type: 'string' },
        model: { type: 'string', default: 'gpt-4.1' },
    },
} as unknown as ProviderSchema;

const DEFAULTS = {
    llm: { openai: LLM_SCHEMA },
    tts: {},
    stt: {},
    embeddings: {},
    realtime: { inworld_realtime: INWORLD_SCHEMA },
    default_providers: { realtime: 'inworld_realtime', llm: 'openai' },
} as ServiceConfigurationDefaults;

function renderInworld() {
    render(
        <ServiceConfigurationForm
            mode="global"
            onSave={vi.fn().mockResolvedValue(undefined)}
            configurationDefaults={DEFAULTS}
            initialConfig={{ is_realtime: true, realtime: { provider: 'inworld_realtime', api_key: 'k' } }}
            forceRealtime={true}
        />,
    );
}

describe('a provider whose fields name sections', () => {
    it('shows one box per section, in the declared order', () => {
        renderInworld();
        const boxes = screen.getAllByRole('region').map(box => box.getAttribute('aria-label'));
        expect(boxes).toEqual(['Transcriber', 'LLM', 'Voice', 'Turn-taking & noise']);
    });

    it('puts each field in its section, with tuning behind "More options"', () => {
        renderInworld();
        const transcriber = screen.getByRole('region', { name: 'Transcriber' });
        expect(within(transcriber).getByText('Expected words')).toBeTruthy();
        const turnTaking = screen.getByRole('region', { name: 'Turn-taking & noise' });
        expect(within(turnTaking).getByText('Speech threshold')).toBeTruthy();
        expect(within(turnTaking).getByText('Eagerness')).toBeTruthy();
        expect(within(turnTaking).queryByText('Longest wait (ms)')).toBeNull();

        fireEvent.click(within(turnTaking).getByText('More turn-taking & noise options'));
        expect(within(turnTaking).getByText('Longest wait (ms)')).toBeTruthy();
    });

    it('does not repeat the LLM model beside the provider', () => {
        renderInworld();
        const llm = screen.getByRole('region', { name: 'LLM' });
        expect(within(llm).getByText('model')).toBeTruthy();
        expect(screen.getAllByText('model')).toHaveLength(1);
    });
});

describe('switching between the LLM tab and the Inworld sections', () => {
    it('keeps every Inworld setting and saves both tabs together', async () => {
        const onSave = vi.fn().mockResolvedValue(undefined);
        render(
            <ServiceConfigurationForm
                mode="global"
                onSave={onSave}
                configurationDefaults={DEFAULTS}
                initialConfig={{
                    is_realtime: true,
                    realtime: { provider: 'inworld_realtime', api_key: 'k' },
                    llm: { provider: 'openai', api_key: 'llm-key', model: 'gpt-4.1' },
                }}
                forceRealtime={true}
            />,
        );
        fireEvent.change(document.getElementById('realtime_expected_words')!, {
            target: { value: 'Vectus, Puff' },
        });
        fireEvent.change(document.getElementById('realtime_speech_threshold')!, {
            target: { value: '0.6' },
        });

        for (const tab of ['LLM', 'Realtime Model', 'LLM', 'Realtime Model']) {
            fireEvent.mouseDown(screen.getByRole('tab', { name: tab }));
            fireEvent.click(screen.getByRole('tab', { name: tab }));
        }

        expect((document.getElementById('realtime_expected_words') as HTMLInputElement).value).toBe('Vectus, Puff');
        expect((document.getElementById('realtime_speech_threshold') as HTMLInputElement).value).toBe('0.6');

        fireEvent.click(screen.getByRole('button', { name: 'Save Configuration' }));
        await waitFor(() => expect(onSave).toHaveBeenCalled());
        const saved = onSave.mock.calls[0][0];
        expect(saved.realtime.expected_words).toBe('Vectus, Puff');
        expect(Number(saved.realtime.speech_threshold)).toBe(0.6);
        expect(saved.realtime.provider).toBe('inworld_realtime');
        expect(saved.llm.provider).toBe('openai');
        expect(saved.llm.model).toBe('gpt-4.1');
    });
});

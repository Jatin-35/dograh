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
        "noise_reduction": {
            "default": "off",
            "description": "Inworld cleans the caller's audio before deciding they are speaking. far_field suits phone lines and speakerphones, near_field headsets.",
            "enum": [
                "off",
                "near_field",
                "far_field"
            ],
            "label": "Noise reduction",
            "section": "Transcriber",
            "title": "Noise Reduction",
            "type": "string"
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
        "max_reply_tokens": {
            "anyOf": [
                {
                    "maximum": 4096,
                    "minimum": 1,
                    "type": "integer"
                },
                {
                    "type": "null"
                }
            ],
            "default": null,
            "description": "Longest reply the LLM may write, in tokens. Empty leaves it to the model.",
            "label": "Max reply tokens",
            "section": "LLM",
            "title": "Max Reply Tokens"
        },
        "reasoning_effort": {
            "advanced": true,
            "default": "none",
            "description": "Thinking before replying, for models that can. none is fastest; higher adds delay.",
            "enum": [
                "none",
                "minimal",
                "low",
                "medium",
                "high"
            ],
            "label": "Reasoning effort",
            "section": "LLM",
            "title": "Reasoning Effort",
            "type": "string"
        },
        "top_p": {
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
            "description": "Nucleus sampling, 0-1. Empty uses the model's default.",
            "label": "Top P",
            "section": "LLM",
            "title": "Top P"
        },
        "frequency_penalty": {
            "advanced": true,
            "anyOf": [
                {
                    "maximum": 2.0,
                    "minimum": -2.0,
                    "type": "number"
                },
                {
                    "type": "null"
                }
            ],
            "default": null,
            "description": "Positive values discourage repeating the same words.",
            "label": "Frequency penalty",
            "section": "LLM",
            "title": "Frequency Penalty"
        },
        "presence_penalty": {
            "advanced": true,
            "anyOf": [
                {
                    "maximum": 2.0,
                    "minimum": -2.0,
                    "type": "number"
                },
                {
                    "type": "null"
                }
            ],
            "default": null,
            "description": "Positive values nudge the model toward new topics.",
            "label": "Presence penalty",
            "section": "LLM",
            "title": "Presence Penalty"
        },
        "memory": {
            "advanced": true,
            "default": false,
            "description": "Inworld summarises the conversation every few turns so details from early in a long call are not lost.",
            "label": "Conversation memory",
            "section": "LLM",
            "title": "Memory",
            "type": "boolean"
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
        "audio_start": {
            "advanced": true,
            "default": "fast_start",
            "description": "How the voice splits text before speaking. fast_start starts soonest; sentence and full_turn sound more natural but start later.",
            "enum": [
                "fast_start",
                "auto",
                "balanced",
                "sentence",
                "full_turn"
            ],
            "label": "Audio start",
            "section": "Voice",
            "title": "Audio Start",
            "type": "string"
        },
        "turn_detection": {
            "default": "semantic_vad",
            "description": "Who decides when the caller has finished. semantic_vad and server_vad: Inworld does (any sound it takes for speech can cancel the bot's reply). dograh_local: Dograh does, from the caller's noise-filtered audio; a turn always ends on silence, and the caller interrupts the bot only with real words. Works with any speech-to-text model.",
            "enum": [
                "semantic_vad",
                "server_vad",
                "dograh_local"
            ],
            "label": "Turn detection",
            "section": "Turn-taking & noise",
            "title": "Turn Detection",
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
            "type": "string",
            "visible_when": {
                "turn_detection": "semantic_vad"
            }
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
            "title": "Speech Threshold",
            "visible_when": {
                "turn_detection": "semantic_vad"
            }
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
            "title": "Min Silence Ms",
            "visible_when": {
                "turn_detection": "semantic_vad"
            }
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
            "title": "Max Silence Ms",
            "visible_when": {
                "turn_detection": "semantic_vad"
            }
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
            "title": "Turn Confidence",
            "visible_when": {
                "turn_detection": "semantic_vad"
            }
        },
        "server_threshold": {
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
            "description": "0-1; how loud sound must be to count as speech with server_vad. Higher ignores more background noise. Empty uses 0.5.",
            "label": "Speech threshold",
            "section": "Turn-taking & noise",
            "title": "Server Threshold",
            "visible_when": {
                "turn_detection": "server_vad"
            }
        },
        "server_silence_ms": {
            "anyOf": [
                {
                    "maximum": 5000,
                    "minimum": 100,
                    "type": "integer"
                },
                {
                    "type": "null"
                }
            ],
            "default": null,
            "description": "Silence (ms) before the caller's turn ends with server_vad. Empty uses 1000.",
            "label": "Silence before reply (ms)",
            "section": "Turn-taking & noise",
            "title": "Server Silence Ms",
            "visible_when": {
                "turn_detection": "server_vad"
            }
        },
        "server_prefix_padding_ms": {
            "advanced": true,
            "anyOf": [
                {
                    "maximum": 2000,
                    "minimum": 0,
                    "type": "integer"
                },
                {
                    "type": "null"
                }
            ],
            "default": null,
            "description": "Audio (ms) kept from before speech starts, so first syllables are not lost. Empty uses 200.",
            "label": "Speech padding (ms)",
            "section": "Turn-taking & noise",
            "title": "Server Prefix Padding Ms",
            "visible_when": {
                "turn_detection": "server_vad"
            }
        },
        "local_min_words": {
            "default": 2,
            "description": "While the bot is speaking, words the caller must say before it stops. Noise and breaths have no words, so they can't cut it off.",
            "label": "Words to interrupt",
            "maximum": 6,
            "minimum": 1,
            "section": "Turn-taking & noise",
            "title": "Local Min Words",
            "type": "integer",
            "visible_when": {
                "turn_detection": "dograh_local"
            }
        },
        "local_silence_secs": {
            "default": 0.6,
            "description": "Silence (seconds) after the caller stops before the bot replies. Higher lets callers pause mid-sentence.",
            "label": "Silence before reply (s)",
            "maximum": 3.0,
            "minimum": 0.2,
            "section": "Turn-taking & noise",
            "title": "Local Silence Secs",
            "type": "number",
            "visible_when": {
                "turn_detection": "dograh_local"
            }
        },
        "local_barge_in_secs": {
            "advanced": true,
            "default": 1.2,
            "description": "While the bot is speaking, unbroken speech (seconds) that also interrupts it, for when no transcript arrives in time.",
            "label": "Speech to interrupt (s)",
            "maximum": 5.0,
            "minimum": 0.3,
            "section": "Turn-taking & noise",
            "title": "Local Barge In Secs",
            "type": "number",
            "visible_when": {
                "turn_detection": "dograh_local"
            }
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

        fireEvent.click(within(turnTaking).getByText('More Turn-taking & noise options'));
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

describe('choosing the turn detection type', () => {
    it('swaps semantic tuning for server VAD tuning', async () => {
        render(
            <ServiceConfigurationForm
                mode="global"
                onSave={vi.fn().mockResolvedValue(undefined)}
                configurationDefaults={DEFAULTS}
                initialConfig={{ is_realtime: true, realtime: { provider: 'inworld_realtime', api_key: 'k' } }}
                forceRealtime={true}
            />,
        );
        const turnTaking = screen.getByRole('region', { name: 'Turn-taking & noise' });
        expect(within(turnTaking).getByText('Eagerness')).toBeTruthy();
        expect(within(turnTaking).queryByText('Speech padding (ms)')).toBeNull();
        expect(document.getElementById('realtime_server_threshold')).toBeNull();

        const trigger = within(turnTaking).getAllByRole('combobox')[0];
        fireEvent.keyDown(trigger, { key: 'ArrowDown' });
        fireEvent.keyDown(await screen.findByRole('option', { name: 'server_vad' }), { key: 'Enter' });

        await waitFor(() => expect(within(turnTaking).queryByText('Eagerness')).toBeNull());
        expect(document.getElementById('realtime_server_threshold')).toBeTruthy();
        expect(document.getElementById('realtime_speech_threshold')).toBeNull();
    });

    it('shows noise reduction in the Transcriber box and memory under More LLM options', () => {
        renderInworld();
        const transcriber = screen.getByRole('region', { name: 'Transcriber' });
        expect(within(transcriber).getByText('Noise reduction')).toBeTruthy();
        const llm = screen.getByRole('region', { name: 'LLM' });
        expect(within(llm).queryByText('Conversation memory')).toBeNull();
        fireEvent.click(within(llm).getByText('More LLM options'));
        expect(within(llm).getByText('Conversation memory')).toBeTruthy();
        expect(within(llm).getByText('Reasoning effort')).toBeTruthy();
    });
});

describe('choosing Dograh-driven turns', () => {
    it('shows the Dograh controls and hides the Inworld turn tuning', async () => {
        render(
            <ServiceConfigurationForm
                mode="global"
                onSave={vi.fn().mockResolvedValue(undefined)}
                configurationDefaults={DEFAULTS}
                initialConfig={{ is_realtime: true, realtime: { provider: 'inworld_realtime', api_key: 'k' } }}
                forceRealtime={true}
            />,
        );
        const turnTaking = screen.getByRole('region', { name: 'Turn-taking & noise' });
        expect(within(turnTaking).queryByText('Words to interrupt')).toBeNull();

        const trigger = within(turnTaking).getAllByRole('combobox')[0];
        fireEvent.keyDown(trigger, { key: 'ArrowDown' });
        fireEvent.keyDown(await screen.findByRole('option', { name: 'dograh_local' }), { key: 'Enter' });

        await waitFor(() => expect(within(turnTaking).getByText('Words to interrupt')).toBeTruthy());
        expect(within(turnTaking).getByText('Silence before reply (s)')).toBeTruthy();
        expect(within(turnTaking).queryByText('Eagerness')).toBeNull();
        expect(document.getElementById('realtime_speech_threshold')).toBeNull();
        expect(document.getElementById('realtime_server_threshold')).toBeNull();
    });
});

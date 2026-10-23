import { describe, expect, it, vi } from 'vitest';

vi.mock('@/client/sdk.gen', () => ({}));
vi.mock('@/context/UserConfigContext', () => ({ useUserConfig: () => ({ userConfig: null }) }));

import { buildByokConfigurationBody, byokConfigToLegacyShape, preferredMode } from './AIModelConfigurationV2Editor';
import type { ServiceConfigurationDefaults } from './ServiceConfigurationForm';

const keyed = { required: ['api_key'], properties: {} };
const DEFAULTS = {
    llm: { openai: keyed },
    tts: { elevenlabs: keyed },
    stt: { deepgram: keyed },
    embeddings: {},
    realtime: { inworld_realtime: keyed, openai_realtime: keyed },
    default_providers: {},
} as unknown as ServiceConfigurationDefaults;

const LLM = { provider: 'openai', api_key: 'llm-key', model: 'gpt-4.1' };
const STT = { provider: 'deepgram', api_key: 'dg-key', model: 'nova-3-general' };
const TTS = { provider: 'elevenlabs', api_key: 'el-key', model: 'eleven_flash_v2_5' };

describe('saving a realtime configuration', () => {
    it('keeps a custom transcriber and voice when both are chosen', () => {
        const body = buildByokConfigurationBody({
            is_realtime: true,
            realtime: { provider: 'inworld_realtime', api_key: 'k', stt_source: 'custom', tts_source: 'custom' },
            llm: LLM, stt: STT, tts: TTS,
        }, DEFAULTS);
        expect(body.byok?.mode).toBe('realtime');
        const realtime = body.byok?.realtime as Record<string, unknown>;
        expect(realtime.stt).toEqual(STT);
        expect(realtime.tts).toEqual(TTS);
    });

    it('leaves them out when Inworld hears and speaks', () => {
        const body = buildByokConfigurationBody({
            is_realtime: true,
            realtime: { provider: 'inworld_realtime', api_key: 'k', stt_source: 'inworld', tts_source: 'inworld' },
            llm: LLM, stt: STT, tts: TTS,
        }, DEFAULTS);
        const realtime = body.byok?.realtime as Record<string, unknown>;
        expect(realtime.stt).toBeUndefined();
        expect(realtime.tts).toBeUndefined();
    });

    it('leaves them out for other realtime providers', () => {
        const body = buildByokConfigurationBody({
            is_realtime: true,
            realtime: { provider: 'openai_realtime', api_key: 'k', stt_source: 'custom', tts_source: 'custom' },
            llm: LLM, stt: STT, tts: TTS,
        }, DEFAULTS);
        expect((body.byok?.realtime as Record<string, unknown>).stt).toBeUndefined();
    });

    it('refuses to save when a custom transcriber has no key', () => {
        expect(() => buildByokConfigurationBody({
            is_realtime: true,
            realtime: { provider: 'inworld_realtime', api_key: 'k', stt_source: 'custom', tts_source: 'custom' },
            llm: LLM, stt: { provider: 'deepgram', model: 'nova-3-general' }, tts: TTS,
        }, DEFAULTS)).toThrow('stt configuration is required');
    });

    it('pipeline (BYOK) saving is unchanged', () => {
        const body = buildByokConfigurationBody({ is_realtime: false, llm: LLM, stt: STT, tts: TTS }, DEFAULTS);
        expect(body.byok?.mode).toBe('pipeline');
        expect(body.byok?.pipeline).toMatchObject({ llm: LLM, stt: STT, tts: TTS });
    });
});

describe('loading a saved realtime configuration', () => {
    it('brings back its transcriber and voice', () => {
        const shape = byokConfigToLegacyShape({
            mode: 'byok',
            byok: { mode: 'realtime', realtime: { realtime: { provider: 'inworld_realtime' }, llm: LLM, stt: STT, tts: TTS } },
        });
        expect(shape).toMatchObject({ is_realtime: true, stt: STT, tts: TTS, llm: LLM });
    });
});

describe('choosing the transcriber and voice separately', () => {
    it('a custom transcriber alone saves only the transcriber', () => {
        const body = buildByokConfigurationBody({
            is_realtime: true,
            realtime: { provider: 'inworld_realtime', api_key: 'k', stt_source: 'custom', tts_source: 'inworld' },
            llm: LLM, stt: STT, tts: TTS,
        }, DEFAULTS);
        const realtime = body.byok?.realtime as Record<string, unknown>;
        expect(realtime.stt).toEqual(STT);
        expect(realtime.tts).toBeUndefined();
    });

    it('a custom voice alone saves only the voice', () => {
        const body = buildByokConfigurationBody({
            is_realtime: true,
            realtime: { provider: 'inworld_realtime', api_key: 'k', stt_source: 'inworld', tts_source: 'custom' },
            llm: LLM, stt: STT, tts: TTS,
        }, DEFAULTS);
        const realtime = body.byok?.realtime as Record<string, unknown>;
        expect(realtime.stt).toBeUndefined();
        expect(realtime.tts).toEqual(TTS);
    });
});

describe('agents saved before the separate choice', () => {
    it('open as custom transcriber and custom voice', () => {
        const shape = byokConfigToLegacyShape({
            mode: 'byok',
            byok: { mode: 'realtime', realtime: {
                realtime: { provider: 'inworld_realtime', stt_tts_source: 'dograh', turn_detection: 'dograh_local' },
                llm: LLM, stt: STT, tts: TTS,
            } },
        });
        const realtime = shape?.realtime as Record<string, unknown>;
        expect(realtime).toMatchObject({ stt_source: 'custom', tts_source: 'custom', turn_detection: 'local' });
        expect(realtime.stt_tts_source).toBeUndefined();
    });

    it('other realtime providers are left as they were', () => {
        const shape = byokConfigToLegacyShape({
            mode: 'byok',
            byok: { mode: 'realtime', realtime: { realtime: { provider: 'openai_realtime', voice: 'marin' }, llm: LLM } },
        });
        expect(shape?.realtime).toEqual({ provider: 'openai_realtime', voice: 'marin' });
    });
});

describe('which tab a saved configuration opens on', () => {
    const realtime = (provider: string) => ({
        mode: 'byok', byok: { mode: 'realtime', realtime: { realtime: { provider }, llm: LLM } },
    });

    it('Inworld Realtime opens on its own tab', () => {
        expect(preferredMode(realtime('inworld_realtime'), null)).toBe('inworld');
    });

    it('other realtime providers open on Speech to Speech', () => {
        expect(preferredMode(realtime('openai_realtime'), null)).toBe('realtime');
    });

    it('an Inworld agent with no saved choice yet, read from its effective config, opens on the Inworld tab', () => {
        expect(preferredMode(null, { is_realtime: true, realtime: { provider: 'inworld_realtime' } })).toBe('inworld');
    });

    it('BYOK and BotrixAI are unchanged', () => {
        expect(preferredMode({ mode: 'byok', byok: { mode: 'pipeline' } }, null)).toBe('byok');
        expect(preferredMode({ mode: 'dograh' }, null)).toBe('dograh');
    });
});

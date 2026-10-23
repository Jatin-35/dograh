import { describe, expect, it, vi } from 'vitest';

vi.mock('@/client/sdk.gen', () => ({}));
vi.mock('@/context/UserConfigContext', () => ({ useUserConfig: () => ({ userConfig: null }) }));

import { buildByokConfigurationBody, byokConfigToLegacyShape } from './AIModelConfigurationV2Editor';
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
    it('keeps our transcriber and voice when Inworld uses them', () => {
        const body = buildByokConfigurationBody({
            is_realtime: true,
            realtime: { provider: 'inworld_realtime', api_key: 'k', stt_tts_source: 'dograh' },
            llm: LLM, stt: STT, tts: TTS,
        }, DEFAULTS);
        expect(body.byok?.mode).toBe('realtime');
        const realtime = body.byok?.realtime as Record<string, unknown>;
        expect(realtime.stt).toEqual(STT);
        expect(realtime.tts).toEqual(TTS);
    });

    it('leaves them out when Inworld hears and speaks itself', () => {
        const body = buildByokConfigurationBody({
            is_realtime: true,
            realtime: { provider: 'inworld_realtime', api_key: 'k', stt_tts_source: 'inworld' },
            llm: LLM, stt: STT, tts: TTS,
        }, DEFAULTS);
        const realtime = body.byok?.realtime as Record<string, unknown>;
        expect(realtime.stt).toBeUndefined();
        expect(realtime.tts).toBeUndefined();
    });

    it('leaves them out for other realtime providers', () => {
        const body = buildByokConfigurationBody({
            is_realtime: true,
            realtime: { provider: 'openai_realtime', api_key: 'k', stt_tts_source: 'dograh' },
            llm: LLM, stt: STT, tts: TTS,
        }, DEFAULTS);
        expect((body.byok?.realtime as Record<string, unknown>).stt).toBeUndefined();
    });

    it('refuses to save when our transcriber is chosen but has no key', () => {
        expect(() => buildByokConfigurationBody({
            is_realtime: true,
            realtime: { provider: 'inworld_realtime', api_key: 'k', stt_tts_source: 'dograh' },
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

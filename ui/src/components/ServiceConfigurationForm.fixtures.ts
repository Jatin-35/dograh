// Generated from the backend registry (SonioxSTTConfiguration, DeepgramSTTConfiguration).
// Regenerate if those settings change.
export const STT_SCHEMAS = {
  "soniox": {
    "description": "Real-time transcription in 60+ languages, including Hindi and other Indian languages, with Hindi-English switching mid-sentence.",
    "properties": {
      "provider": {
        "const": "soniox",
        "default": "soniox",
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
        "default": "stt-rt-v5",
        "description": "Soniox real-time model. stt-rt-v5 is the latest.",
        "examples": [
          "stt-rt-v5",
          "stt-rt-v4"
        ],
        "title": "Model",
        "type": "string"
      },
      "languages": {
        "default": [
          "hi",
          "en"
        ],
        "description": "Languages callers speak. Pick Hindi and English for Hinglish. Leave empty to detect any language.",
        "examples": [
          "en",
          "hi",
          "mr",
          "gu",
          "bn",
          "pa",
          "ta",
          "te",
          "kn",
          "ml",
          "ur",
          "af",
          "ar",
          "az",
          "be",
          "bg",
          "bs",
          "ca",
          "cs",
          "cy",
          "da",
          "de",
          "el",
          "es",
          "et",
          "eu",
          "fa",
          "fi",
          "fr",
          "gl",
          "he",
          "hr",
          "hu",
          "id",
          "it",
          "ja",
          "kk",
          "ko",
          "lt",
          "lv",
          "mk",
          "ms",
          "nl",
          "no",
          "pl",
          "pt",
          "ro",
          "ru",
          "sk",
          "sl",
          "sq",
          "sr",
          "sv",
          "sw",
          "th",
          "tl",
          "tr",
          "uk",
          "vi",
          "zh"
        ],
        "items": {
          "type": "string"
        },
        "label": "Languages",
        "multi_select": true,
        "title": "Languages",
        "type": "array"
      },
      "strict_languages": {
        "default": false,
        "description": "Only transcribe the languages above. Works best with a single language; for Hinglish callers leave it off, as Soniox may write English spoken with a Hindi accent in the wrong script.",
        "label": "Strict languages",
        "title": "Strict Languages",
        "type": "boolean"
      },
      "domain_context": {
        "anyOf": [
          {
            "maxLength": 8000,
            "type": "string"
          },
          {
            "type": "null"
          }
        ],
        "default": null,
        "description": "A short description of the calls, e.g. 'Think Gas PNG customer support: complaints, meter readings, BP numbers'. The agent's dictionary words are sent too.",
        "label": "Domain context",
        "multiline": true,
        "title": "Domain Context"
      },
      "use_soniox_turn_detection": {
        "advanced": true,
        "default": false,
        "description": "Let Soniox decide when the caller has finished speaking, from what they said. Off uses the agent's own turn detection.",
        "label": "Use Soniox turn detection",
        "title": "Use Soniox Turn Detection",
        "type": "boolean"
      },
      "max_endpoint_delay_ms": {
        "advanced": true,
        "anyOf": [
          {
            "maximum": 3000,
            "minimum": 500,
            "type": "integer"
          },
          {
            "type": "null"
          }
        ],
        "default": null,
        "description": "Longest wait (ms) before Soniox ends the turn. Empty uses Soniox's default.",
        "label": "Max wait (ms)",
        "title": "Max Endpoint Delay Ms",
        "visible_when": {
          "use_soniox_turn_detection": true
        }
      },
      "endpoint_sensitivity": {
        "advanced": true,
        "anyOf": [
          {
            "maximum": 1.0,
            "minimum": -1.0,
            "type": "number"
          },
          {
            "type": "null"
          }
        ],
        "default": null,
        "description": "-1 to 1; higher ends turns sooner. Empty uses Soniox's default.",
        "label": "Sensitivity",
        "title": "Endpoint Sensitivity",
        "visible_when": {
          "use_soniox_turn_detection": true
        }
      },
      "endpoint_speed_level": {
        "advanced": true,
        "anyOf": [
          {
            "maximum": 3,
            "minimum": 0,
            "type": "integer"
          },
          {
            "type": "null"
          }
        ],
        "default": null,
        "description": "0-3; higher answers sooner. Empty uses Soniox's default.",
        "label": "Speed level",
        "title": "Endpoint Speed Level",
        "visible_when": {
          "use_soniox_turn_detection": true
        }
      }
    },
    "provider_docs_url": "https://soniox.com/docs/stt/models",
    "required": [
      "api_key"
    ],
    "title": "Soniox",
    "type": "object"
  },
  "deepgram": {
    "properties": {
      "provider": {
        "const": "deepgram",
        "default": "deepgram",
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
        "default": "nova-3-general",
        "description": "Deepgram STT model.",
        "examples": [
          "nova-3-general",
          "flux-general-en",
          "flux-general-multi"
        ],
        "title": "Model",
        "type": "string"
      },
      "language": {
        "default": "multi",
        "description": "Language code. 'multi' enables Nova-3 auto-detect and omits language hints for Flux multilingual auto-detect.",
        "examples": [
          "multi",
          "ar",
          "ar-AE",
          "ar-SA",
          "ar-QA",
          "ar-KW",
          "ar-SY",
          "ar-LB",
          "ar-PS",
          "ar-JO",
          "ar-EG",
          "ar-SD",
          "ar-TD",
          "ar-MA",
          "ar-DZ",
          "ar-TN",
          "ar-IQ",
          "ar-IR",
          "be",
          "bn",
          "bs",
          "bg",
          "ca",
          "cs",
          "da",
          "da-DK",
          "de",
          "de-CH",
          "el",
          "en",
          "en-US",
          "en-AU",
          "en-GB",
          "en-IN",
          "en-NZ",
          "es",
          "es-419",
          "et",
          "fa",
          "fi",
          "fr",
          "fr-CA",
          "he",
          "hi",
          "hr",
          "hu",
          "id",
          "it",
          "ja",
          "kn",
          "ko",
          "ko-KR",
          "lt",
          "lv",
          "mk",
          "mr",
          "ms",
          "nl",
          "nl-BE",
          "no",
          "pl",
          "pt",
          "pt-BR",
          "pt-PT",
          "ro",
          "ru",
          "sk",
          "sl",
          "sr",
          "sv",
          "sv-SE",
          "ta",
          "te",
          "th",
          "tl",
          "tr",
          "uk",
          "ur",
          "vi",
          "zh-CN",
          "zh-TW"
        ],
        "model_options": {
          "flux-general-en": [
            "en"
          ],
          "flux-general-multi": [
            "multi",
            "de",
            "en",
            "es",
            "fr",
            "hi",
            "it",
            "ja",
            "nl",
            "pt",
            "ru"
          ],
          "nova-3-general": [
            "multi",
            "ar",
            "ar-AE",
            "ar-SA",
            "ar-QA",
            "ar-KW",
            "ar-SY",
            "ar-LB",
            "ar-PS",
            "ar-JO",
            "ar-EG",
            "ar-SD",
            "ar-TD",
            "ar-MA",
            "ar-DZ",
            "ar-TN",
            "ar-IQ",
            "ar-IR",
            "be",
            "bn",
            "bs",
            "bg",
            "ca",
            "cs",
            "da",
            "da-DK",
            "de",
            "de-CH",
            "el",
            "en",
            "en-US",
            "en-AU",
            "en-GB",
            "en-IN",
            "en-NZ",
            "es",
            "es-419",
            "et",
            "fa",
            "fi",
            "fr",
            "fr-CA",
            "he",
            "hi",
            "hr",
            "hu",
            "id",
            "it",
            "ja",
            "kn",
            "ko",
            "ko-KR",
            "lt",
            "lv",
            "mk",
            "mr",
            "ms",
            "nl",
            "nl-BE",
            "no",
            "pl",
            "pt",
            "pt-BR",
            "pt-PT",
            "ro",
            "ru",
            "sk",
            "sl",
            "sr",
            "sv",
            "sv-SE",
            "ta",
            "te",
            "th",
            "tl",
            "tr",
            "uk",
            "ur",
            "vi",
            "zh-CN",
            "zh-TW"
          ]
        },
        "title": "Language",
        "type": "string"
      }
    },
    "required": [
      "api_key"
    ],
    "title": "Deepgram",
    "type": "object"
  }
} as const;

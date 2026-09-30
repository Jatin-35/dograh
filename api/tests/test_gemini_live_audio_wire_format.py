"""Gemini Live must receive the caller's audio labelled ``mimeType``.

On 2026-09-30 a rebuild pulled google-genai 2.25.0 (the pipecat extra only
bounds it ``<3``), which sends ``mime_type`` instead, and Gemini Live stopped
transcribing callers in prod. ``api/requirements.txt`` pins the version; this
fails if a future bump changes the wire format again.
"""

import asyncio
import json

from google.genai import _api_client, live, types


def test_realtime_audio_is_sent_with_camel_case_mime_type():
    sent = []

    class _Socket:
        async def send(self, message):
            sent.append(json.loads(message))

    session = live.AsyncSession(
        api_client=_api_client.BaseApiClient(api_key="test"), websocket=_Socket()
    )
    asyncio.run(
        session.send_realtime_input(
            audio=types.Blob(data=b"\x00\x01", mime_type="audio/pcm;rate=16000")
        )
    )

    assert sent == [
        {
            "realtime_input": {
                "audio": {"data": "AAE=", "mimeType": "audio/pcm;rate=16000"}
            }
        }
    ]

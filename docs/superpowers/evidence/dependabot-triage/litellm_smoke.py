"""Smoke-test the pipeline's LiteLLM adapters against the installed litellm.

Run from pipeline/: uv run --locked python <this file>.
Uses litellm's mock_response, so no network or API key is needed.
"""

import json
import tempfile
from importlib.metadata import version
from pathlib import Path

import litellm

from pipeline.adapters import litellm_clients as lc

MODEL = "anthropic/claude-haiku-4-5"

print("litellm", version("litellm"))
frame = Path(tempfile.mkdtemp()) / "f.jpg"
frame.write_bytes(b"\xff\xd8\xff\xd9")

request = lc.build_vision_request(MODEL, [frame], "score")
scene = {"score": 7, "location": "outdoor", "people": True}
response = litellm.completion(**request, mock_response=json.dumps(scene))
print("vision content:", response.choices[0].message.content)

real_completion = litellm.completion
mocked = '{"score":3,"location":"indoor","people":false}'
litellm.completion = lambda **kw: real_completion(**kw, mock_response=mocked)
client = lc.LiteLLMVisionClient(MODEL)
print("LiteLLMVisionClient.score_frames:", client.score_frames([frame], "p"))

verbose = {"duration": 10, "segments": [{"start": 0, "end": 4}], "text": "hi!"}
print("verbose->result:", lc.transcription_result_from_verbose(verbose))

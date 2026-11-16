"""A local stand-in for OpenAI's /v1/audio/transcriptions, backed by faster-whisper.

Only for evidence runs with no OpenAI key: the pipeline's real
``LiteLLMTranscriber`` points at it through ``OPENAI_API_BASE`` and gets a real
Whisper transcription (faster-whisper's ``base.en``) in OpenAI's
``verbose_json`` shape, so the adapter, the request and the feature extraction
all run unchanged. Not part of the product.

    python local_whisper_server.py --port 8765
"""

import argparse
import cgi
import json
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from faster_whisper import WhisperModel

MODEL = WhisperModel("base.en", device="cpu", compute_type="int8")


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        if not self.path.endswith("/audio/transcriptions"):
            self.send_error(404)
            return
        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={"REQUEST_METHOD": "POST"},
        )
        upload = form["file"]
        with tempfile.NamedTemporaryFile(suffix=".wav") as f:
            f.write(upload.file.read())
            f.flush()
            segments, info = MODEL.transcribe(f.name)
            segments = list(segments)
        body = {
            "task": "transcribe",
            "language": info.language,
            "duration": info.duration,
            "text": "".join(s.text for s in segments).strip(),
            "segments": [
                {"id": i, "start": s.start, "end": s.end, "text": s.text}
                for i, s in enumerate(segments)
            ],
        }
        print(f"transcribed {info.duration:.1f}s: {body['text']!r}", flush=True)
        payload = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()

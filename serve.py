"""Local preview only. No browser launch, dependencies or remote setup."""
from http.server import HTTPServer, SimpleHTTPRequestHandler
import os
from pathlib import Path
from urllib.parse import unquote, urlsplit

from sheetaudit.api import Dependencies
from sheetaudit.budget import FileLedger
from sheetaudit.http import post
from sheetaudit.llm.client import HttpChatClient

ROOT = Path(__file__).resolve().parent
SAMPLE_NAMES = {"club_membership_fees", "small_business_budget", "event_budget"}


def make_server(address=("127.0.0.1", 4174), *, deps=None, env=None, bind_and_activate=True):
    if deps is None:
        # The preview writes accounting only, never uploaded workbooks.
        ledger_path = os.environ.get("X129_LEDGER_PATH", str(ROOT / ".ek-preview-ledger.jsonl"))
        # Live calls took up to 10.7 s (2026-10-24); keep this under the platform's function limit.
        deps = Dependencies(FileLedger(ledger_path), HttpChatClient(timeout_s=55, max_retries=0))

    class Handler(SimpleHTTPRequestHandler):
        extensions_map = {**SimpleHTTPRequestHandler.extensions_map, ".mjs": "text/javascript"}

        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(ROOT / "web"), **kwargs)

        def log_message(self, *args):
            pass  # Do not log request bodies, query strings or credentials.

        def list_directory(self, path):
            self.send_error(404)
            return None

        def translate_path(self, path):
            path = unquote(urlsplit(path).path)
            parts = path.strip("/").split("/")
            if len(parts) == 2 and parts[0] == "samples" and parts[1].endswith(".xlsx"):
                if parts[1][:-5] in SAMPLE_NAMES:
                    return str(ROOT / "samples" / parts[1])
            if len(parts) == 3 and parts[:2] == ["samples", "precomputed"] and parts[2].endswith(".json"):
                if parts[2][:-5] in SAMPLE_NAMES:
                    return str(ROOT / "samples" / "precomputed" / parts[2])
            return super().translate_path(path)

        def do_POST(self):
            post(self, deps, env)

    return HTTPServer(address, Handler, bind_and_activate=bind_and_activate)


if __name__ == "__main__":
    with make_server() as server:
        print("Exception Keeper preview: http://127.0.0.1:4174", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass

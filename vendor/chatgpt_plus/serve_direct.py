import os
import sys
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from server import Handler
from standalone_core.fingerprint_store import optimize_fingerprint_store


class QuietHandler(Handler):
    def log_message(self, _format, *_args):
        return


if __name__ == "__main__":
    port = int(os.environ.get("REG_FACTORY_PLUS_PORT", "5601"))
    optimize_fingerprint_store("US")
    ThreadingHTTPServer(("127.0.0.1", port), QuietHandler).serve_forever()

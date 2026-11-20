"""Serve the workspace from the pinned deployment entry point."""

import os
from waitress import serve

from .web import create_app


def main():
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    threads = int(os.environ.get("WAITRESS_THREADS", "4"))
    print(f"FieldTrace listening at http://{host}:{port}", flush=True)
    serve(create_app(), host=host, port=port, threads=threads,
          max_request_body_size=68 * 1024 * 1024, channel_timeout=80)


if __name__ == "__main__":
    main()

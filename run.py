#!/usr/bin/env python3
"""Start FaceMatch.  python run.py  ->  http://127.0.0.1:8765"""
import multiprocessing
import os
import signal
import threading
import webbrowser


def main():
    from facematch import server
    host = os.environ.get("FACEMATCH_HOST", "127.0.0.1")
    port = int(os.environ.get("FACEMATCH_PORT", "8765"))
    try:
        from facematch.models_dl import download_base
        download_base(print)          # first run: fetch the small face models if they are missing
    except Exception as e:  # noqa: BLE001
        print(f"  Could not download the face models ({e}). Run: python download_models.py")
    app = server.init_app()
    from waitress import serve
    url = f"http://{'localhost' if host in ('0.0.0.0', '::') else host}:{port}"
    print(f"\n  FaceMatch running at {url}   (Ctrl+C to stop)")
    if server.SETUP_CODE:
        print("  First run: create the admin account in the browser."
              + (f"\n  Setup code (needed when not on this computer): {server.SETUP_CODE}" if host not in ("127.0.0.1", "localhost", "::1") else ""))
    print()
    if not os.environ.get("FACEMATCH_NO_BROWSER") and host in ("127.0.0.1", "localhost", "::1"):
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    signal.signal(signal.SIGTERM, lambda *_: (server.shutdown(), os._exit(0)))
    try:
        serve(app, host=host, port=port, threads=16, channel_timeout=3600)
    finally:
        server.shutdown()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()

"""Serve the public landing independently of the voice application."""

import argparse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--operator-url", default="http://127.0.0.1:7860/operator")
    args = parser.parse_args()

    class LandingHandler(SimpleHTTPRequestHandler):
        def __init__(self, *arguments, **kwargs):
            super().__init__(*arguments, directory=str(STATIC), **kwargs)

        def send_head(self):
            path = urlsplit(self.path).path
            if path == "/operator":
                self.send_response(302)
                self.send_header("Location", args.operator_url)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            if path in ("/", "/landing", "/landing/"):
                self.path = "/landing.html"
            elif path in ("/static/landing.css", "/static/landing.js", "/static/golos.ttf"):
                self.path = path.removeprefix("/static")
            elif path.startswith("/static/brand/"):
                filename = path.removeprefix("/static/brand/")
                if "/" in filename or "%" in filename or not (STATIC / "brand" / filename).is_file():
                    self.send_error(404)
                    return None
                self.path = "/brand/" + filename
            else:
                self.send_error(404)
                return None
            return super().send_head()

    server = ThreadingHTTPServer((args.host, args.port), LandingHandler)
    print(f"Landing: http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

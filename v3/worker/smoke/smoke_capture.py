"""Phase 1 smoke test: prove a real Chromium can screenshot a localhost page
from inside Snowpark Container Services, and land the PNG on a Snowflake stage.

This deliberately does NOT touch the accelerator. It answers one question and
nothing else: can this platform run a browser and write the result back? Every
later step (the real capture worker) rests on that answer, so it is worth
isolating.

What it proves, in order:
  1. Chromium launches in the container.
  2. A local HTTP server can be started and bound (SiS cannot do this -- it is
     the whole reason for the container).
  3. Chromium can reach that localhost port and render it.
  4. The captured PNG is genuinely NON-BLANK -- checked by pixel content, not
     by file size, because a blank white page is a perfectly valid large PNG.
  5. The result can be written to @WBR_DB.DBX.VALIDATION_CAPTURE_STAGE.

Exit codes: 0 all checks passed, 1 a check failed (reason printed as JSON).
"""

from __future__ import annotations

import http.server
import json
import os
import socket
import sys
import threading
import time

STAGE = os.environ.get(
    "SMOKE_STAGE", "@WBR_DB.DBX.VALIDATION_CAPTURE_STAGE/smoke-test")
OUT_DIR = os.environ.get("SMOKE_OUT", "/tmp/smoke")
IMAGE_TAG = os.environ.get("SMOKE_IMAGE_TAG", "unknown")

# Deliberately colourful and text-heavy: a blank-detection check that passes on
# a plain white page proves nothing, so the page must have real content.
PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>SPCS smoke</title>
<style>
 body{margin:0;font-family:system-ui,sans-serif;background:#0b1b2b;color:#fff}
 .wrap{padding:48px}
 h1{font-size:44px;margin:0 0 8px}
 .grid{display:flex;gap:16px;margin-top:28px}
 .box{flex:1;height:180px;border-radius:12px;display:flex;align-items:center;
      justify-content:center;font-size:28px;font-weight:600;color:#08111c}
 .a{background:#37d4a7}.b{background:#ffc857}.c{background:#ff6b6b}.d{background:#7cc6ff}
 code{background:#12293f;padding:2px 8px;border-radius:6px}
</style></head><body><div class="wrap">
<h1>Snowpark Container Services — browser smoke test</h1>
<p>Rendered by real Chromium inside SPCS at <code>%s</code></p>
<div class="grid">
  <div class="box a">ALPHA</div><div class="box b">BRAVO</div>
  <div class="box c">CHARLIE</div><div class="box d">DELTA</div>
</div></div></body></html>""" % time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime())


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(port: int):
    body = PAGE.encode("utf-8")

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):                                       # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):                          # keep logs clean
            return

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def non_blank(png_path: str):
    """(ok, detail). A screenshot can be large and still be an empty page, so
    this looks at PIXELS: how many distinct colours, and how much of the image
    the single most common colour occupies."""
    from PIL import Image

    with Image.open(png_path) as im:
        im = im.convert("RGB")
        width, height = im.size
        colours = im.getcolors(maxcolors=1_000_000)
    if colours is None:                    # more colours than the cap = rich
        return True, {"width": width, "height": height, "distinct_colours": ">1000000"}
    total = sum(count for count, _ in colours)
    dominant = max(count for count, _ in colours)
    share = dominant / total if total else 1.0
    ok = len(colours) >= 8 and share < 0.98
    return ok, {"width": width, "height": height,
                "distinct_colours": len(colours),
                "dominant_colour_share": round(share, 4)}


def snowflake_session():
    """SPCS service identity -- no stored credential, no interactive step."""
    from snowflake.snowpark import Session

    with open("/snowflake/session/token", encoding="utf-8") as fh:
        token = fh.read()
    return Session.builder.configs({
        "host": os.environ["SNOWFLAKE_HOST"],
        "account": os.environ["SNOWFLAKE_ACCOUNT"],
        "token": token,
        "authenticator": "oauth",
        "database": os.environ.get("SNOWFLAKE_DATABASE", "WBR_DB"),
        "schema": os.environ.get("SNOWFLAKE_SCHEMA", "DBX"),
        "warehouse": os.environ.get("SNOWFLAKE_WAREHOUSE", "POWERHOUSE"),
    }).create()


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    result = {"image_tag": IMAGE_TAG, "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    png_path = os.path.join(OUT_DIR, "smoke.png")

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:                                    # noqa: BLE001
        result.update(ok=False, stage="import_playwright", error=str(exc))
        print(json.dumps(result)); return 1

    port = _free_port()
    serve(port)
    url = f"http://127.0.0.1:{port}/"
    result["url"] = url

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox"])
            result["chromium_version"] = browser.version
            page = browser.new_page(viewport={"width": 1600, "height": 900},
                                    device_scale_factor=2)
            page.goto(url, wait_until="networkidle", timeout=30_000)
            page.wait_for_timeout(400)          # let fonts settle
            page.screenshot(path=png_path, full_page=True)
            browser.close()
    except Exception as exc:                                    # noqa: BLE001
        result.update(ok=False, stage="capture",
                      error=f"{type(exc).__name__}: {exc}")
        print(json.dumps(result)); return 1

    ok, detail = non_blank(png_path)
    result.update(png_bytes=os.path.getsize(png_path), **detail)
    if not ok:
        result.update(ok=False, stage="blank_check",
                      error="screenshot is blank or near-uniform")
        print(json.dumps(result)); return 1

    try:
        session = snowflake_session()
        result["snowflake_role"] = session.sql("SELECT CURRENT_ROLE()").collect()[0][0]
        manifest_path = os.path.join(OUT_DIR, "smoke_manifest.json")
        result.update(ok=True, stage="complete",
                      finished_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=1)
        for path in (png_path, manifest_path):
            session.file.put(path, STAGE, auto_compress=False, overwrite=True)
        session.close()
    except Exception as exc:                                    # noqa: BLE001
        result.update(ok=False, stage="stage_upload",
                      error=f"{type(exc).__name__}: {exc}")
        print(json.dumps(result)); return 1

    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""A tiny shop API (standard library only) that shows perf-agent-lab is not tied to JPetStore.

It has a deliberate concurrency bug: order ids are allocated with a read-then-increment that is not atomic, so two
requests arriving together can get the same id. Under load the checkout then fails with DuplicateOrderError, and the
pipeline should trace that failure to `next_order_id` in this file.

Run it by hand:  python app.py --port 8081
"""
import argparse
import json
import logging
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

log = logging.getLogger("shop")

PRODUCTS = {i: {"id": i, "name": f"Product {i}", "price": 5 + i} for i in range(1, 51)}
ORDERS: dict[int, dict] = {}
_next_order_id = 1000


class DuplicateOrderError(Exception):
    pass


def next_order_id() -> int:
    global _next_order_id
    current = _next_order_id          # read the counter ...
    time.sleep(0.002)                 # ... (a slow lookup in a real service) ...
    _next_order_id = current + 1      # ... then increment: two threads can read the same value
    return current


def place_order(product_id: int) -> int:
    order_id = next_order_id()
    if order_id in ORDERS:
        raise DuplicateOrderError(f"order id {order_id} already exists")
    ORDERS[order_id] = {"id": order_id, "product_id": product_id, "ts": time.time()}
    return order_id


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):          # keep the app log about the app, not about every request
        pass

    def _send(self, status: int, body) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        if url.path == "/health":
            self._send(200, {"status": "ok"})
        elif url.path == "/products":
            self._send(200, {"products": list(PRODUCTS.values())[:20]})
        elif url.path == "/product":
            product = PRODUCTS.get(int(q.get("id", ["0"])[0]))
            self._send(200 if product else 404, product or {"error": "no such product"})
        elif url.path == "/search":
            term = q.get("q", [""])[0].lower()
            self._send(200, {"results": [p for p in PRODUCTS.values() if term in p["name"].lower()][:10]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if urlparse(self.path).path != "/checkout":
            return self._send(404, {"error": "not found"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            order_id = place_order(int(body.get("product_id", 1)))
            self._send(200, {"order_id": order_id, "status": "confirmed"})
        except Exception:                                  # noqa: BLE001 - log the traceback, answer 500
            log.exception("checkout failed")
            self._send(500, {"error": "checkout failed"})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8081)
    args = ap.parse_args()
    logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    log.info("shop listening on port %s", args.port)
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()

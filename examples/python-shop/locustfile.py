"""Locust script for the sample Python shop: three user journeys. Every response is validated, not just its status code."""
import random

from locust import HttpUser, between, task

import recorder  # noqa: F401  (required: writes the per-request log the pipeline analyses)


class Shopper(HttpUser):
    wait_time = between(0.2, 1.0)

    def check(self, r, expect):
        if r.status_code != 200:
            r.failure(f"HTTP {r.status_code}")
        elif expect not in r.text:
            r.failure(f"response without '{expect}'")

    @task(5)
    def browse(self):
        with self.client.get("/products", name="GET /products", catch_response=True) as r:
            self.check(r, "products")
        with self.client.get(f"/product?id={random.randint(1, 50)}", name="GET /product", catch_response=True) as r:
            self.check(r, "price")

    @task(2)
    def search(self):
        with self.client.get(f"/search?q={random.choice(['product 1', 'product 2', 'product 3'])}", name="GET /search", catch_response=True) as r:
            self.check(r, "results")

    @task(2)
    def checkout(self):
        with self.client.post("/checkout", json={"product_id": random.randint(1, 50)}, name="POST /checkout", catch_response=True) as r:
            self.check(r, "order_id")

"""A deliberately STALE Locust script, used by `--demo-heal` to demonstrate self-healing.

It targets URLs and form fields from an older JPetStore (Struts-style *.do pages), so every request fails the smoke
test. The repair agent has to discover the real Stripes URLs (Catalog.action?...), fix the parameters and checks, and
prove the fix by passing the smoke test again.
"""
import random

from locust import HttpUser, between, task

import recorder  # noqa: F401  (required: writes the per-request log)

CATEGORIES = ["FISH", "DOGS", "CATS", "REPTILES", "BIRDS"]


class OldShopper(HttpUser):
    wait_time = between(0.5, 1.5)

    @task(3)
    def browse_category(self):
        cat = random.choice(CATEGORIES)
        with self.client.get(f"/shop/viewCategory.do?categoryId={cat}", name="GET viewCategory.do", catch_response=True) as r:
            if r.status_code != 200 or "Category" not in r.text:
                r.failure(f"category page broken: HTTP {r.status_code}")

    @task(2)
    def view_product(self):
        with self.client.get("/shop/viewProduct.do?productId=FI-SW-01", name="GET viewProduct.do", catch_response=True) as r:
            if r.status_code != 200:
                r.failure(f"product page broken: HTTP {r.status_code}")

    @task(1)
    def login(self):
        with self.client.post("/shop/signon.do", data={"username": "j2ee", "password": "j2ee"}, name="POST signon.do", catch_response=True) as r:
            if r.status_code != 200:
                r.failure(f"login broken: HTTP {r.status_code}")

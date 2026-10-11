"""Locust script for JPetStore (Stripes) updated to use current URLs and form fields.

The original script targeted legacy *.do URLs and failed the smoke test.
This version uses the current Stripes routes under the /jpetstore context.
"""
import random

from locust import HttpUser, between, task

import recorder  # noqa: F401  (required: writes the per-request log)

# Category identifiers used by the application
CATEGORIES = ["BIRDS", "CATS", "DOGS", "FISH", "REPTILES"]

# A sample product that exists in the catalog
SAMPLE_PRODUCT_ID = "AV-CB-01"


class Shopper(HttpUser):
    wait_time = between(0.5, 1.5)

    @task(3)
    def browse_category(self):
        cat = random.choice(CATEGORIES)
        with self.client.get(
            f"/catalog/viewCategory?categoryId={cat}",
            name="GET viewCategory",
            catch_response=True,
        ) as r:
            if r.status_code != 200:
                r.failure(f"category page broken: HTTP {r.status_code}")

    @task(2)
    def view_product(self):
        with self.client.get(
            f"/catalog/viewProduct?productId={SAMPLE_PRODUCT_ID}",
            name="GET viewProduct",
            catch_response=True,
        ) as r:
            if r.status_code != 200:
                r.failure(f"product page broken: HTTP {r.status_code}")

    @task(1)
    def login(self):
        # The login form expects a 'signon' field in addition to username and password
        payload = {
            "username": "j2ee",
            "password": "j2ee",
            "signon": "Sign On",
        }
        with self.client.post(
            "/account/signon",
            data=payload,
            name="POST signon",
            catch_response=True,
        ) as r:
            if r.status_code != 200:
                r.failure(f"login broken: HTTP {r.status_code}")

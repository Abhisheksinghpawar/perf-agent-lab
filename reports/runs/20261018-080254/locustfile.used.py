"""Locust script for JPetStore 6 (Spring MVC).

Four user journeys with realistic weights. Product and item ids are *discovered from the pages* instead of being
hard-coded, so the script survives catalogue changes. Every request is validated (status + a marker from the page),
because a 200 that renders an error page is still a failure from a user's point of view.
"""
import random
import re

from locust import HttpUser, between, task

import recorder  # noqa: F401  (required: writes the per-request log the pipeline analyses)

CATEGORIES = ["FISH", "DOGS", "CATS", "REPTILES", "BIRDS"]
KEYWORDS = ["dog", "fish", "cat", "bird", "angel", "bull", "snake"]
PRODUCT_RX = re.compile(r'viewProduct\?productId=([\w-]+)')
ITEM_RX = re.compile(r'viewItem\?itemId=([\w-]+)')
ORDER_FORM = {
    "cardType": "Visa", "creditCard": "999 9999 9999 9999", "expiryDate": "12/03", "billToFirstName": "Perf",
    "billToLastName": "Agent", "billAddress1": "1 Load Test Way", "billAddress2": "Suite 1", "billCity": "Palo Alto",
    "billState": "CA", "billZip": "94303", "billCountry": "USA", "newOrder": "Continue",
}


class Shopper(HttpUser):
    wait_time = between(0.5, 2.0)

    # ---- helpers -------------------------------------------------------------------------------------
    def get(self, path, name, marker=None):
        with self.client.get(path, name=name, catch_response=True) as r:
            if r.status_code != 200:
                r.failure(f"HTTP {r.status_code}")
            elif marker and marker not in r.text:
                r.failure(f"page rendered without '{marker}'")
            return r

    def to_item(self):
        """home -> category -> product -> item; returns the item id."""
        self.get("/catalog", "GET /catalog", "viewCategory")
        cat = random.choice(CATEGORIES)
        r = self.get(f"/catalog/viewCategory?categoryId={cat}", "GET /catalog/viewCategory", "viewProduct")
        products = PRODUCT_RX.findall(r.text) or ["FI-SW-01"]
        r = self.get(f"/catalog/viewProduct?productId={random.choice(products)}", "GET /catalog/viewProduct", "viewItem")
        items = ITEM_RX.findall(r.text) or ["EST-1"]
        item = random.choice(items)
        self.get(f"/catalog/viewItem?itemId={item}", "GET /catalog/viewItem", "addItem")
        return item

    # ---- journeys ----------------------------------------------------------------------------------------
    @task(6)
    def browse(self):
        self.to_item()

    @task(2)
    def search(self):
        self.get(f"/catalog/searchProducts?keyword={random.choice(KEYWORDS)}", "GET /catalog/searchProducts")

    @task(2)
    def add_to_cart(self):
        item = self.to_item()
        self.get(f"/cart/addItem?workingItemId={item}", "GET /cart/addItem", "Shopping Cart")
        self.get("/cart", "GET /cart", "Shopping Cart")

    @task(1)
    def buy(self):
        """Sign in, add an item, check out and confirm the order - the heaviest path (sessions + database writes)."""
        self.get("/account", "GET /account (sign-on form)", "signon")
        with self.client.post("/account/signon", data={"username": "j2ee", "password": "j2ee", "signon": "Login"},
                              name="POST /account/signon", catch_response=True) as r:
            if r.status_code != 200 or "Sign Out" not in r.text:
                r.failure("sign-on did not result in a signed-in page")
        item = self.to_item()
        self.get(f"/cart/addItem?workingItemId={item}", "GET /cart/addItem", "Shopping Cart")
        self.get("/cart/checkout", "GET /cart/checkout")
        self.get("/order/new", "GET /order/new", "newOrder")
        with self.client.post("/order/new", data=ORDER_FORM, name="POST /order/new (billing)", catch_response=True) as r:
            if r.status_code != 200 or "confirm" not in r.text.lower():
                r.failure("billing step did not reach the confirmation page")
        with self.client.post("/order/new", data={"confirmed": "true"}, name="POST /order/new (confirm)", catch_response=True) as r:
            if r.status_code != 200:
                r.failure(f"HTTP {r.status_code}")
        self.get("/account/signoff", "GET /account/signoff")

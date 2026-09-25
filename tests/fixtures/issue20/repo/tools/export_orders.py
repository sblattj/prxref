"""Export orders to the reporting service."""
import requests

TIMEOUT_SECONDS = 10


def build_payload(orders):
    """Shape the orders for the reporting endpoint."""
    return {"orders": list(orders)}


def unused_helper():
    return None


def export(orders, url):
    payload = build_payload(orders)
    return requests.post(url, json=payload, timeout=TIMEOUT_SECONDS)

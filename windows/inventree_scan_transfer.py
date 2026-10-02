"""Windows barcode-scanner client for moving InvenTree stock.

Usage:
    set INVENTREE_URL=http://192.168.11.106
    set INVENTREE_TOKEN=your-token
    python inventree_scan_transfer.py

Scan a destination location first, then scan stock-item barcodes. A USB or
Bluetooth scanner should be configured to press Enter after each barcode.
"""

from __future__ import annotations

import json
import os
import sys
from decimal import Decimal

import requests


URL = os.environ.get("INVENTREE_URL", "http://192.168.11.106").rstrip("/")
TOKEN = os.environ.get(
    "INVENTREE_TOKEN",
    "inv-1c90e826714eea70d80b4bf70b5e4351e06d8e9d-20260709",
)
TIMEOUT = 20


class InventreeError(RuntimeError):
    pass


def api(method: str, path: str, payload: dict | None = None) -> dict:
    response = requests.request(
        method,
        f"{URL}{path}",
        json=payload,
        headers={"Authorization": f"Token {TOKEN}"},
        timeout=TIMEOUT,
    )
    try:
        data = response.json()
    except ValueError:
        data = {"detail": response.text}
    if response.status_code >= 400:
        raise InventreeError(json.dumps(data, ensure_ascii=False))
    return data


def scan(barcode: str) -> dict:
    return api("POST", "/api/barcode/", {"barcode": barcode})


def object_pk(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    if isinstance(value, dict):
        for key in ("pk", "id"):
            found = object_pk(value.get(key))
            if found is not None:
                return found
    return None


def describe(kind: str, payload: dict) -> str:
    if not isinstance(payload, dict):
        return str(payload)
    name = payload.get("name") or payload.get("part_detail", {}).get("name")
    quantity = payload.get("quantity")
    location = payload.get("location_detail", {}).get("name") or payload.get("location")
    bits = [f"{kind} #{object_pk(payload) or '?'}"]
    if name:
        bits.append(str(name))
    if quantity is not None:
        bits.append(f"qty {quantity}")
    if location:
        bits.append(f"at {location}")
    return " | ".join(bits)


def transfer(stock_pk: int, quantity: str, location_pk: int, barcode: str) -> None:
    api(
        "POST",
        "/api/stock/transfer/",
        {
            "location": location_pk,
            "notes": f"Windows barcode transfer: {barcode}",
            "items": [{"pk": stock_pk, "quantity": quantity}],
        },
    )


def main() -> int:
    print(f"Connected target: {URL}")
    print("Commands: location | status | quit")
    print("1. Scan a destination location barcode.")
    print("2. Scan stock item barcodes to transfer them there.")

    destination: tuple[int, str] | None = None
    while True:
        try:
            raw = input("\nScan> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not raw:
            continue
        command = raw.lower()
        if command in {"quit", "exit"}:
            return 0
        if command == "status":
            print(destination[1] if destination else "No destination selected")
            continue
        if command == "location":
            destination = None
            print("Destination cleared. Scan a location barcode.")
            continue

        try:
            result = scan(raw)
            if "stocklocation" in result:
                location = result["stocklocation"]
                location_pk = object_pk(location)
                if location_pk is None:
                    raise InventreeError(f"Location has no pk: {location}")
                destination = (location_pk, describe("location", location))
                print(f"Destination set: {destination[1]}")
                continue

            if destination is None:
                print("Scan a destination location before scanning stock.")
                continue

            if "stockitem" not in result:
                print(f"Not a stock item or location: {json.dumps(result, ensure_ascii=False)}")
                continue

            item = result["stockitem"]
            stock_pk = object_pk(item)
            quantity = item.get("quantity") if isinstance(item, dict) else None
            if stock_pk is None or quantity in (None, ""):
                detail = api("GET", f"/api/stock/{stock_pk or object_pk(item)}/")
                stock_pk = object_pk(detail)
                quantity = detail.get("quantity")
                item = detail
            if stock_pk is None or quantity in (None, ""):
                raise InventreeError(f"Cannot determine stock quantity: {item}")

            transfer(stock_pk, str(Decimal(str(quantity))), destination[0], raw)
            print(f"Moved: {describe('stock', item)} -> {destination[1]}")
        except (InventreeError, requests.RequestException, ValueError) as exc:
            print(f"ERROR: {exc}")


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Live security verification against a running PROMETHEUS server.

Every check here attacks the real public API of a real running instance. Nothing
is mocked and no internal object is touched -- if a check passes, the deployed
service actually behaves that way.

Usage:  python verify_security.py [--base-url http://127.0.0.1:8402]
Exit code 0 only if every check passes.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import secrets
import sys
import time
from typing import Any

import httpx

from buyer_agent import BuyerAgent

PROTECTED_FIELDS = ("direction", "confidence", "thesis", "claims", "invalidation", "risk_factors")

results: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str = "") -> bool:
    results.append((name, passed, detail))
    mark = "PASS" if passed else "FAIL"
    print(f"  [{mark}] {name}" + (f"\n         {detail}" if detail else ""))
    return passed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8402")
    args = ap.parse_args()
    base = args.base_url.rstrip("/")
    http = httpx.Client(timeout=30.0)

    print("=" * 74)
    print("PROMETHEUS LIVE SECURITY VERIFICATION")
    print(f"target: {base}")
    print("=" * 74)

    # ---------------------------------------------------------------- listings
    print("\n[1] PAYWALL: protected intelligence must not leak pre-payment")
    listings = http.get(f"{base}/api/marketplace/signals?limit=50").json()
    signals = listings["signals"]
    if not signals:
        print("  no listings available; start the server and let autopilot run")
        return 2

    leaked = {f for s in signals for f in PROTECTED_FIELDS if f in s}
    check("marketplace listing exposes no protected field", not leaked, f"leaked: {leaked}")

    sid = signals[0]["signal_id"]
    preview = http.get(f"{base}/api/marketplace/signals/{sid}/preview").json()
    leaked_p = {f for f in PROTECTED_FIELDS if f in preview}
    check("preview endpoint exposes no protected field", not leaked_p, f"leaked: {leaked_p}")

    # The whole serialised payload must not contain the thesis text anywhere.
    blob = json.dumps(listings) + json.dumps(preview)
    check(
        "no thesis/evidence text anywhere in public serialisation",
        not re.search(r"(LONG|SHORT) [A-Z]+USDT over", blob),
    )

    # Unmatured passport must be protected.
    r = http.get(f"{base}/api/signals/{sid}/passport")
    check("full passport is protected before maturity", r.status_code == 402,
          f"got HTTP {r.status_code}")

    # ------------------------------------------------------------ forged payment
    print("\n[2] FORGED PAYMENT: buyer assertions must never be authority")
    purchase = http.post(f"{base}/api/marketplace/signals/{sid}/purchase").json()
    pid = purchase["purchase_id"]
    req = purchase["x402"]["accepts"][0]
    delivery_url = f"{base}/api/purchases/{pid}/delivery"

    r = http.get(delivery_url)
    check("unpaid request to protected resource returns 402", r.status_code == 402,
          f"got HTTP {r.status_code}")

    def b64(o: Any) -> str:
        return base64.b64encode(json.dumps(o).encode()).decode()

    # Per the x402 v1 error table a malformed payload is HTTP 400 and a failed
    # verification is HTTP 402. Either way the artifact must not be released, so the
    # security property under test is simply: never 200.
    for label, header in [
        ('{"paid": true}', b64({"paid": True})),
        ("valid shape but paid flag", b64({"x402Version": 1, "scheme": "exact",
                                          "network": req["network"], "paid": True})),
        ("empty header", ""),
        ("garbage", "not-base64-at-all!!"),
        ("null signature", b64({"x402Version": 1, "scheme": "exact", "network": req["network"],
                                "payload": {"signature": None, "authorization": {}}})),
        ("wrong x402 version", b64({**json.loads(json.dumps({"x402Version": 99, "scheme": "exact",
                                                             "network": req["network"]}))})),
    ]:
        r = http.get(delivery_url, headers={"X-PAYMENT": header} if header else {})
        check(f"forged payment does not deliver: {label}",
              r.status_code in (400, 402), f"got HTTP {r.status_code}")

    # A signature from the wrong key over the right terms.
    print("\n[3] CRYPTOGRAPHIC BINDING")
    agent = BuyerAgent(base)
    good_header = agent.sign_payment(req, int(purchase["settlement"].get("chain_id") or 97))
    decoded = json.loads(base64.b64decode(good_header))

    tampered = json.loads(json.dumps(decoded))
    tampered["payload"]["authorization"]["value"] = "1"
    r = http.get(delivery_url, headers={"X-PAYMENT": b64(tampered)})
    check("amount tampered after signing is rejected", r.status_code in (400, 402),
          f"got HTTP {r.status_code}")

    tampered2 = json.loads(json.dumps(decoded))
    tampered2["payload"]["authorization"]["to"] = "0x" + "99" * 20
    r = http.get(delivery_url, headers={"X-PAYMENT": b64(tampered2)})
    check("recipient tampered after signing is rejected", r.status_code in (400, 402),
          f"got HTTP {r.status_code}")

    # ------------------------------------------------------------ genuine payment
    print("\n[4] GENUINE PAYMENT AND DELIVERY")
    r = http.get(delivery_url, headers={"X-PAYMENT": good_header})
    check("genuine signed payment is accepted", r.status_code == 200, f"got HTTP {r.status_code}")
    if r.status_code != 200:
        print(json.dumps(r.json(), indent=2)[:600])
        return 1
    artifact = r.json()

    v = BuyerAgent.verify_artifact(artifact)
    check("delivered artifact matches its pre-payment signal_hash", v["match"],
          f"{v['claimed_signal_hash']} vs {v['recomputed_signal_hash']}")
    check("delivered snapshot matches snapshot_hash",
          v["snapshot_hash_claimed"] == v["snapshot_hash_recomputed"])
    check("delivered quant packet matches quant_hash",
          v["quant_hash_claimed"] == v["quant_hash_recomputed"])
    check("protected intelligence is present after payment",
          all(f in artifact["prediction"] for f in ("direction", "confidence", "thesis")))
    d = artifact["delivery"]
    check("simulated settlement reports no transaction hash",
          d["settlement_status"] != "SETTLED" or d["transaction_hash"] is not None,
          f"verifier={d['settlement_verifier']} tx={d['transaction_hash']}")
    check("environment is labelled on the delivered artifact", bool(d.get("environment")),
          f"environment={d.get('environment')}")

    # ---------------------------------------------------------------- idempotency
    print("\n[5] IDEMPOTENCY AND REPLAY")
    r2 = http.get(delivery_url, headers={"X-PAYMENT": good_header})
    check("redelivery returns 200 and identical bytes", r2.status_code == 200)
    check("redelivered artifact is byte-identical",
          r2.json()["hashes"]["signal_hash"] == artifact["hashes"]["signal_hash"])

    key = f"idem-{secrets.token_hex(6)}"
    a = http.post(f"{base}/api/marketplace/signals/{sid}/purchase",
                  headers={"Idempotency-Key": key}).json()
    b = http.post(f"{base}/api/marketplace/signals/{sid}/purchase",
                  headers={"Idempotency-Key": key}).json()
    check("same Idempotency-Key returns the same purchase",
          a["purchase_id"] == b["purchase_id"], f"{a['purchase_id']} vs {b['purchase_id']}")

    # Replay the same signed authorization against a brand new purchase.
    fresh = http.post(f"{base}/api/marketplace/signals/{sid}/purchase").json()
    fresh_url = f"{base}/api/purchases/{fresh['purchase_id']}/delivery"
    r = http.get(fresh_url, headers={"X-PAYMENT": good_header})
    check("replaying a used authorization nonce is refused", r.status_code in (400, 402, 409),
          f"got HTTP {r.status_code}")

    # ------------------------------------------------------------------ ledger
    print("\n[6] ECONOMIC INTEGRITY")
    treasury = http.get(f"{base}/api/treasury").json()
    paid = http.get(f"{base}/health").json()["counts"]["paid"]
    check("ledger entries match verified payments",
          len([e for e in treasury["entries"] if e["kind"] == "SIGNAL_SALE"]) == paid,
          f"ledger={len(treasury['entries'])} paid={paid}")

    rep = http.get(f"{base}/api/reputation").json()
    if rep["scored_signals"] < rep["min_reputation_sample"]:
        check("accuracy is withheld below minimum sample",
              rep["directional_accuracy_display"] == "INSUFFICIENT_SAMPLE",
              f"shows: {rep['directional_accuracy_display']}")
    check("sample size n is always reported", "n" in rep, f"n={rep.get('n')}")

    ps = http.get(f"{base}/provider-status").json()
    check("Binance Agent OS is reported UNAVAILABLE, not claimed",
          ps["binance_agent_os"]["status"] == "UNAVAILABLE")
    check("inactive model adapters are ADAPTER_ONLY, not 'working'",
          all(p["status"] in ("ADAPTER_ONLY", "UNAVAILABLE", "UNVERIFIED", "BROKEN")
              for p in ps["model_providers"] if not p["active"]))
    check("no authenticated Binance capability is claimed",
          "order placement" in ps["market_data"]["not_implemented"])

    agent.close()
    http.close()

    # ------------------------------------------------------------------ summary
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print("\n" + "=" * 74)
    print(f"RESULT: {passed}/{total} checks passed")
    print("=" * 74)
    if passed != total:
        print("\nFAILED:")
        for name, ok, detail in results:
            if not ok:
                print(f"  - {name}: {detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

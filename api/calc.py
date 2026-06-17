"""
Vercel Python Serverless Function — Permit Filing Fee Calculator
----------------------------------------------------------------
Callable by both the web UI (index.html) and the Marcy Fee-Development
Assistant skill. Single source of truth for filing-fee math.

  GET /api/calc?muni=nyc&cost=28399&stories=under&landmark=0&ahv_days=0&oer=0&tpp=0
  GET /api/calc?muni=wp&cost=40000

Returns JSON:
  { "ok": true, "municipality": "NYC DOB", "inputs": {...},
    "line_items": [ {"label","amount","note"} ... ], "total": <float> }

The math is a faithful port of calcNYC / calcWP in index.html (the live,
proven logic), plus two optional skill-facing line items the public UI does
not surface: TPP (Tenant Protection Plan, $500 flat) and OER (already in UI).
Pure functions (calc_nyc / calc_wp) are unit-testable via the CLI at the
bottom — run `python api/calc.py nyc 28399` locally.

NOTE (for fee-model reconciliation): the NYC total includes the 5% Safety
Factor the live calculator applies. fee-model.md separately describes a ~10%
COGS markup on pass-through filing fees. These are different adjustments —
confirm with Jake which governs before the skill bills COGS. This endpoint
returns raw fees (+5% safety, exposed as a line item); it does NOT apply the
10% COGS markup. ACP-5 (asbestos sub-cost) is intentionally out of scope here
— it is an estimate the skill adds, not a filing fee.
"""

import json
import math
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs


# ---------------------------------------------------------------------------
# Pure calc functions (faithful port of index.html calcNYC / calcWP)
# ---------------------------------------------------------------------------

def _r2(n):
    """Round to 2 decimals, returning a float (JSON-friendly)."""
    return round(float(n) + 0.0, 2)


def calc_nyc(cost, stories_under7=True, landmark=False, ahv_days=0,
             oer=False, tpp=False):
    """NYC DOB Alteration Type 2 filing fees. Mirrors calcNYC()."""
    items = []

    # --- Base filing fee (tiered) ---
    if cost <= 3000:
        base = 225.0
    elif stories_under7:
        tier2 = math.floor((min(cost, 5000) - 3000) / 1000) * 20
        tier3 = math.ceil((cost - 5000) / 1000) * 10.30 if cost > 5000 else 0
        base = 225 + tier2 + tier3
    else:
        # 7+ stories: $10.30/k from $3k onward
        base = 225 + math.ceil((cost - 3000) / 1000) * 10.30

    record = 166.0
    service = (base + record) * 0.02
    total = base + record + service
    items.append({"label": "Base Filing Fee", "amount": _r2(base), "note": ""})
    items.append({"label": "Record Management Fee", "amount": _r2(record), "note": "flat"})
    items.append({"label": "Service Fee", "amount": _r2(service), "note": "2%"})

    # --- Landmark (LPC) ---
    if landmark:
        lfee = 95.0 if cost <= 25000 else 95 + math.ceil((cost - 25000) / 1000) * 5
        lsvc = lfee * 0.02
        total += lfee + lsvc
        items.append({"label": "LPC Landmark Fee", "amount": _r2(lfee), "note": ""})
        items.append({"label": "LPC Service Fee", "amount": _r2(lsvc), "note": "2%"})

    # --- After-Hours Variance ---
    if ahv_days and ahv_days > 0:
        ahv_app = math.ceil(ahv_days / 3) * 130
        ahv_daily = ahv_days * 240
        total += ahv_app + ahv_daily
        items.append({"label": "AHV Application Fee", "amount": _r2(ahv_app),
                      "note": f"{ahv_days} day{'s' if ahv_days > 1 else ''}"})
        items.append({"label": "AHV Daily Fee", "amount": _r2(ahv_daily),
                      "note": f"{ahv_days} × $240"})

    # --- Safety factor (5%) — applied before the flat government fees.
    # The live UI folds this into the total without showing a row; the
    # endpoint exposes it explicitly so the breakdown reconciles to `total`.
    safety = total * 0.05
    total += safety
    items.append({"label": "Safety Factor", "amount": _r2(safety), "note": "5%"})

    # --- OER Filing (flat, not subject to safety) ---
    if oer:
        oer_fee = 485.0
        total += oer_fee
        items.append({"label": "OER Filing Fee", "amount": _r2(oer_fee), "note": "flat"})

    # --- TPP (Tenant Protection Plan, flat — skill-facing, not in public UI) ---
    if tpp:
        tpp_fee = 500.0
        total += tpp_fee
        items.append({"label": "TPP Filing Fee", "amount": _r2(tpp_fee), "note": "flat"})

    return {
        "ok": True,
        "municipality": "NYC DOB",
        "inputs": {
            "cost": cost, "stories_under7": stories_under7, "landmark": landmark,
            "ahv_days": ahv_days, "oer": oer, "tpp": tpp,
        },
        "line_items": items,
        "total": _r2(total),
    }


def calc_wp(cost):
    """White Plains filing fee. Mirrors calcWP(): $100 + $16 per $1k over $1k."""
    rounded = math.ceil(cost / 1000) * 1000
    fee = 100.0 if rounded <= 1000 else 100 + ((rounded - 1000) / 1000) * 16
    return {
        "ok": True,
        "municipality": "White Plains",
        "inputs": {"cost": cost, "rounded": rounded},
        "line_items": [
            {"label": "Project Cost (rounded to $1k)", "amount": _r2(rounded), "note": ""},
            {"label": "Filing Fee", "amount": _r2(fee), "note": "$100 + $16 per $1k over $1k"},
        ],
        "total": _r2(fee),
    }


# ---------------------------------------------------------------------------
# Param parsing
# ---------------------------------------------------------------------------

def _flag(params, key):
    v = params.get(key, ["0"])[0].strip().lower()
    return v in ("1", "true", "yes", "on")


def _num(params, key, default=0.0):
    try:
        return float(params.get(key, [default])[0])
    except (ValueError, TypeError):
        return default


def compute(params):
    """Dispatch on muni. params is a dict of lists (from parse_qs)."""
    muni = params.get("muni", ["nyc"])[0].strip().lower()
    cost = _num(params, "cost", 0)
    if cost <= 0:
        return 400, {"ok": False, "error": "missing_param",
                     "message": "Provide a positive ?cost="}

    if muni in ("wp", "white_plains", "whiteplains"):
        return 200, calc_wp(cost)

    if muni in ("nyc", "nyc_dob", "dob"):
        stories = params.get("stories", ["under"])[0].strip().lower()
        return 200, calc_nyc(
            cost,
            stories_under7=(stories != "over"),
            landmark=_flag(params, "landmark"),
            ahv_days=int(_num(params, "ahv_days", 0)),
            oer=_flag(params, "oer"),
            tpp=_flag(params, "tpp"),
        )

    return 400, {"ok": False, "error": "bad_muni",
                 "message": "muni must be 'nyc' or 'wp'"}


# ---------------------------------------------------------------------------
# Vercel handler (mirrors dob-lookup/api/lookup.py)
# ---------------------------------------------------------------------------

def _send_json(h, status, data):
    body = json.dumps(data).encode("utf-8")
    h.send_response(status)
    h.send_header("Content-Type", "application/json")
    h.send_header("Content-Length", str(len(body)))
    h.send_header("Access-Control-Allow-Origin", "*")
    h.end_headers()
    h.wfile.write(body)


class handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.end_headers()

    def do_GET(self):
        try:
            params = parse_qs(urlparse(self.path).query)
            status, data = compute(params)
            _send_json(self, status, data)
        except Exception:
            _send_json(self, 500, {"ok": False, "error": "server_error",
                                   "message": "An unexpected error occurred."})


# ---------------------------------------------------------------------------
# Local CLI for testing the math without Vercel:
#   python api/calc.py nyc 28399
#   python api/calc.py nyc 28399 over landmark oer tpp
#   python api/calc.py wp 40000
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    if not args:
        print("usage: python api/calc.py <nyc|wp> <cost> [over] [landmark] [oer] [tpp] [ahv=N]")
        sys.exit(1)
    muni = args[0]
    cost = float(args[1]) if len(args) > 1 else 0
    rest = [a.lower() for a in args[2:]]
    ahv = 0
    for a in rest:
        if a.startswith("ahv="):
            ahv = int(a.split("=", 1)[1])
    if muni == "wp":
        result = calc_wp(cost)
    else:
        result = calc_nyc(
            cost,
            stories_under7=("over" not in rest),
            landmark=("landmark" in rest),
            ahv_days=ahv,
            oer=("oer" in rest),
            tpp=("tpp" in rest),
        )
    print(json.dumps(result, indent=2))

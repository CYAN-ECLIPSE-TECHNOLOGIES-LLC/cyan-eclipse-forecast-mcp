#!/usr/bin/env python3
"""Eclipse Forecast MCP — v0.1.0, phase M0 (Polymarket data
adapter, Wing B).

Powered by Cyan Eclipse Technologies (cetech.ai).

Descriptor (mandatory with the name, spec section 12.1):
Eclipse Forecast — the Polymarket adapter: the VENUE'S prices,
with their criteria and liquidity attached. The forecast is the
venue's priced output, never Cyan Eclipse's prediction.

Posture (spec section 4.2): Polymarket Global = Grade C (data)
+ Grade X (US trading). This adapter is READ-ONLY over public
data shapes: Gamma for discovery + criteria text, CLOB public
for books/prices. The US trading surface is ABSENT — there is
no order, trade, account, or API-key tool for a geoblocked
route to land on (spec 8.2: circumvention is a named refusal,
and here it is an absent capability). Polymarket US (QCX) is a
separate venue and a later phase; its data never blends with
Global's in one Quote.

TRANSPORT SEAM (M0 fixture form, stated plainly): venue reads
run against fixtures shaped like the real Gamma / CLOB public
API responses, loaded behind load_fixtures(). The live public-
read transport drops in behind the same seam at a later
verification step; it is labeled not_tested in the manifest.
No network calls exist in this build.

Every output carries money_class: REAL (Polymarket data is
real-money venue data — the label travels even though this
adapter moves no money).

Core seam: instruments, books, quotes, and resolutions are
normalized through Eclipse Book's book_engine against the
SHARED suite state ($ECLIPSE_BOOK_STATE or the Book core's
data/state.json), loaded fresh at every call.

M0 SCOPE: fixtures only. No accounts, no keys, no orders, no
certification claimed — bench pending.

Transport: MCP over stdio, stdlib only.
"""
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).parent
CORE_DIR = HERE.parent / "eclipse-book-mcp"
sys.path.insert(0, str(CORE_DIR))

import book_engine as eng  # noqa: E402
from book_engine import ToolError  # noqa: E402

VENUE = "polymarket_global"
MONEY_CLASS = "REAL"

# Gamma native status vocabulary -> core lifecycle. Anything
# outside this map surfaces as UNMAPPED with the native string
# preserved (spec 5.2), never coerced.
GAMMA_STATUS_MAP = {"active": "OPEN", "closed": "CLOSED"}


def fixture_dir() -> Path:
    return Path(os.environ.get(
        "ECLIPSE_FORECAST_FIXTURES", HERE / "fixtures"))


def load_fixtures(set_name="base") -> dict:
    d = fixture_dir()
    gamma_file = "gamma_markets.json" if set_name == "base" \
        else f"gamma_markets_{set_name}.json"
    out = {"markets": json.loads((d / gamma_file).read_text())["markets"],
           "books": json.loads((d / "clob_books.json").read_text())["books"],
           "resolutions": json.loads(
               (d / "resolutions.json").read_text())["resolutions"]}
    return out


def core_state_path() -> Path:
    return Path(os.environ.get(
        "ECLIPSE_BOOK_STATE", CORE_DIR / "data" / "state.json"))


def load_core_state() -> dict:
    path = core_state_path()
    if path.exists():
        return json.loads(path.read_text())
    state = eng.empty_state()
    seed = json.loads((CORE_DIR / "fixtures.json").read_text())
    eng.ensure_seeded(state, seed)
    save_core_state(state)
    return state


def save_core_state(state: dict) -> None:
    path = core_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=1))


# ------------------------------------------------------------- translation
def _json_list(value):
    if isinstance(value, list):
        return value
    return json.loads(value)


def gamma_native_status(native: dict) -> str:
    if native.get("closed"):
        return "closed"
    if native.get("active"):
        return "active"
    return "unknown"


def parse_gamma_market(native: dict) -> dict:
    """Gamma market -> canonical Instrument spec (core engine
    input). Token-id <-> outcome mapping is pinned from the
    venue's own arrays, never inferred from price (spec 12.1)."""
    outcomes = _json_list(native["outcomes"])
    token_ids = _json_list(native["clobTokenIds"])
    status = gamma_native_status(native)
    return {
        "venue": VENUE, "native_id": native["conditionId"],
        "native_symbol": native.get("slug"),
        "kind": "EVENT_CONTRACT", "title": native["question"],
        "underlying": native.get("groupItemTitle"),
        "outcomes": outcomes,
        "token_ids": token_ids,
        "resolution_criteria": {
            "text": native["description"],
            "source_ref": f"gamma:{native['conditionId']}"},
        "resolution_source": native.get("resolutionSource"),
        "trading_window": {"open": native.get("startDate"),
                           "close": native.get("endDate")},
        "tick": native.get("tickSize", "0.01"), "min_size": "1",
        "currency": "USD", "money_class": MONEY_CLASS,
        "native_status": status,
        "status_mapped": GAMMA_STATUS_MAP.get(status),
        "provenance": {"feed": "gamma-fixture",
                       "fetched_at": "2026-10-25T12:00:00Z",
                       "data_rights": {
                           "source": "Polymarket Gamma public API "
                                     "(fixture-shaped in M0)",
                           "basis": "account-holder + editorial "
                                    "consumption, no redistribution "
                                    "(spec 6)",
                           "method": "fixture file behind the "
                                     "transport seam",
                           "retention": "operational snapshots; "
                                        "own ledger permanent"}}}


def parse_clob_book(native_book: dict, instrument_id: str,
                    outcome: str) -> dict:
    """CLOB book -> canonical Book via the core builder (the
    same builder Strike uses — normalization parity lives in
    the core, spec 5.2)."""
    return eng.build_book(
        instrument_id, VENUE, "CLOB", native_book.get("bids", []),
        native_book.get("asks", []), native_book["timestamp"],
        tick=native_book.get("tick_size", "0.01"),
        native_ref=native_book.get("hash"))


# ------------------------------------------------------------------- tools
def _sync(state, set_name):
    done = state.setdefault("adapter_syncs", {}).setdefault(
        "forecast", [])
    if set_name in done:
        return {"synced": [], "count": 0, "already_synced": True,
                "criteria_changed_events": [],
                "money_class": MONEY_CLASS}
    fx = load_fixtures(set_name)
    synced, events = [], []
    books = fx["books"]
    for native in fx["markets"]:
        spec_in = parse_gamma_market(native)
        result = eng.ingest_instrument(state, spec_in)
        inst = result["instrument"]
        if result["criteria_changed"]:
            events.append(result["criteria_changed"])
        outcomes = spec_in["outcomes"]
        token_ids = spec_in["token_ids"]
        for outcome, token in zip(outcomes, token_ids):
            native_book = books.get(token)
            if native_book is None:
                continue
            book = parse_clob_book(native_book,
                                   inst["instrument_id"], outcome)
            book["outcome"] = outcome
            eng.ingest_book(state, book)
        synced.append(inst["instrument_id"])
    for res in fx["resolutions"]:
        iid = eng.instrument_id_for(VENUE, "EVENT_CONTRACT",
                                    res["condition_id"])
        if iid in state["instruments"]:
            eng.resolution_report(state, {
                "instrument_id": iid, "outcome": res["outcome"],
                "source": res.get("source"),
                "evidence_ref": res.get("evidence_ref"),
                "disputed": res.get("disputed", False)})
    done.append(set_name)
    return {"synced": synced, "count": len(synced),
            "criteria_changed_events": events,
            "money_class": MONEY_CLASS}


def t_forecast_sync(state, args):
    return _sync(state, args.get("fixture_set", "base"))


def t_market_list(state, args):
    _sync(state, "base")
    out = [{"instrument_id": i["instrument_id"], "title": i["title"],
            "status": i["status"], "native_status": i["native_status"],
            "money_class": i["money_class"],
            "resolution_source": i["resolution_source"]}
           for i in state["instruments"].values()
           if i["venue"] == VENUE]
    return {"markets": out, "count": len(out),
            "money_class": MONEY_CLASS}


def t_market_get(state, args):
    _sync(state, "base")
    inst = state["instruments"].get(args["instrument_id"])
    if inst is None or inst["venue"] != VENUE:
        raise ToolError(f"MARKET_NOT_FOUND: {args['instrument_id']}")
    return {"market": eng.guard_no_probability(inst),
            "money_class": MONEY_CLASS}


def t_book_get(state, args):
    _sync(state, "base")
    book = eng.get_book(state, args["instrument_id"],
                        args.get("outcome", "Yes"))
    return {"book": book, "money_class": MONEY_CLASS}


def t_quote_get(state, args):
    _sync(state, "base")
    quote = eng.quote_get(state, args["instrument_id"], args["size"],
                          outcome=args.get("outcome", "Yes"),
                          as_of=args.get("as_of"))
    return {"quote": quote, "money_class": MONEY_CLASS}


def t_resolution_get(state, args):
    _sync(state, "base")
    inst = state["instruments"].get(args["instrument_id"])
    if inst is None or inst["venue"] != VENUE:
        raise ToolError(f"MARKET_NOT_FOUND: {args['instrument_id']}")
    return {"resolution": eng.resolution_view(inst),
            "money_class": MONEY_CLASS}


def t_venue_status(state, args):
    _sync(state, "base")
    status = eng.venue_status(state, VENUE, as_of=args.get("as_of"))
    status["transport"] = "fixtures (Gamma/CLOB-shaped); live " \
                          "public reads not_tested in M0"
    status["us_trading_surface"] = "ABSENT"
    return status


def t_receipt_list(state, args):
    limit = args.get("limit")
    receipts = state["receipts"]
    if limit is not None:
        receipts = receipts[-int(limit):]
    return {"receipts": receipts, "count": len(state["receipts"]),
            "chain_valid": eng.verify_receipt_chain(state["receipts"]),
            "money_class": MONEY_CLASS}


_P_INSTRUMENT = {"type": "string", "description": "Canonical instrument id as returned by market_list or forecast_sync, e.g. 'B:polymarket_global:cond-fed-dec-2026'. Must belong to the Polymarket Global venue; other venues' ids fail with MARKET_NOT_FOUND."}
_P_OUTCOME = {"type": "string", "description": "Outcome label exactly as it appears in the market's outcomes array, e.g. 'Yes' or 'No'. Defaults to 'Yes' when omitted."}
_P_AS_OF = {"type": "string", "description": "Optional ISO-8601 timestamp used for fee-schedule freshness evaluation, e.g. '2026-10-25T13:00:00Z'. Omit to evaluate against the fixture clock."}

TOOLS = {
    "forecast_sync": (
        t_forecast_sync,
        "Sync a Polymarket fixture set into the shared Eclipse Book core state: Gamma-shaped markets become canonical Instruments (resolution criteria verbatim and hash-pinned), CLOB-shaped books become canonical Books, and resolution fixtures become Resolution records. Use this to load the 'edited' criteria-change set explicitly; do NOT use it for live venue data — this build reads fixture files only, makes no network calls, and needs no API key or account. Behaviour: mutating but idempotent — it writes the shared Book state file, and re-syncing the same set is a no-op that reports already_synced. Every other tool in this server auto-syncs the 'base' set, so an explicit sync is only needed for 'edited'. Returns {synced: [instrument_id], count, already_synced, criteria_changed_events: [...], money_class: 'REAL'}; criteria_changed_events names every market whose criteria text changed versus the pinned version.",
        {"fixture_set": {"type": "string", "description": "Fixture set to load: 'base' (default) or 'edited' (the criteria-change scenario set). Any other value fails because its fixture file does not exist."}}, []),
    "market_list": (
        t_market_list,
        "List Polymarket Global markets as canonical Instruments with mapped status, preserved native status, and the named resolution source for each market. Use this first to discover instrument_id values for market_get, book_get, quote_get, and resolution_get; do NOT use it when you need one market's full criteria text — use market_get instead. Behaviour: read-only over fixture data shaped like the Polymarket Gamma public API; no network calls, no auth, no trading surface exists in this server at all. Auto-syncs the 'base' fixture set on first call. Returns {markets: [{instrument_id, title, status, native_status, money_class, resolution_source}], count, money_class: 'REAL'}.",
        {}, []),
    "market_get": (
        t_market_get,
        "Fetch one Polymarket Global market's full canonical Instrument, including its verbatim resolution criteria, criteria version history, and current resolution state. Use this when the exact settlement wording matters; use market_list to browse or discover ids, book_get for order-book depth, and resolution_get for only the resolution record. Behaviour: read-only, fixture data only, no auth. Returns {market: <instrument with criteria and history>, money_class: 'REAL'}; an unknown or non-Polymarket instrument_id fails with MARKET_NOT_FOUND.",
        {"instrument_id": _P_INSTRUMENT}, ["instrument_id"]),
    "book_get": (
        t_book_get,
        "Fetch the canonical order book for one outcome of one Polymarket Global market: price levels, computed spread, and a depth summary. Use this to judge liquidity before quoting; use quote_get instead when you need the all-in cost at a specific size, and market_get for criteria. Behaviour: read-only, fixture data shaped like the Polymarket CLOB public API, no auth. Returns {book: <levels, spread, depth summary>, money_class: 'REAL'}.",
        {"instrument_id": _P_INSTRUMENT,
         "outcome": _P_OUTCOME},
        ["instrument_id"]),
    "quote_get": (
        t_quote_get,
        "Price one outcome of one Polymarket Global market at a stated size: executable price, all-in cost including fees, a liquidity flag, and the market's verbatim resolution criteria attached. Use this for a size-aware price; use book_get for raw levels and market_get for criteria alone. The returned price is a venue price, never a probability estimate. Behaviour: read-only, fixture data only, no auth, no order is placed — this server has no trading tools. Returns {quote: <price, all-in cost, liquidity flag, criteria>, money_class: 'REAL'}.",
        {"instrument_id": _P_INSTRUMENT,
         "size": {"type": "string", "description": "Size to price, as a decimal string in the market's contract units, e.g. '100'. Required; the quote walks the book to this size, so larger sizes can change the price and liquidity flag."}, "outcome": _P_OUTCOME,
         "as_of": _P_AS_OF},
        ["instrument_id", "size"]),
    "resolution_get": (
        t_resolution_get,
        "Fetch the resolution record for one Polymarket Global market: status REPORTED, VERIFIED, or DISPUTED, with the named resolution source and evidence reference. Use this to check whether and how a market settled; use market_get when you also need the criteria text and version history. An unverified or reported-only resolution is data, not settled fact. Behaviour: read-only, fixture data only, no auth. Returns {resolution: <status, source, evidence ref>, money_class: 'REAL'}; an unknown instrument_id fails with MARKET_NOT_FOUND.",
        {"instrument_id": _P_INSTRUMENT}, ["instrument_id"]),
    "venue_status": (
        t_venue_status,
        "Report this Polymarket adapter's health: transport-seam state and fee-schedule freshness, plus the standing fact us_trading_surface = ABSENT. Use this to confirm what this build can and cannot do before relying on its data; it returns no market data — use market_list for that. Behaviour: read-only, no auth. Returns the venue status object extended with transport ('fixtures (Gamma/CLOB-shaped); live public reads not_tested in M0') and us_trading_surface: 'ABSENT'.",
        {"as_of": _P_AS_OF}, []),
    "receipt_list": (
        t_receipt_list,
        "List receipts from the shared Eclipse Book suite ledger (hash-chained) as this adapter sees it, with a chain-validity check. Use this to audit what the shared core state recorded across adapters; it is not market data — use market_list or book_get for that. Behaviour: read-only, no auth. Returns {receipts: [...], count: <total receipts in ledger>, chain_valid: <bool>, money_class: 'REAL'}; when limit is given, receipts holds only the most recent entries while count still reports the full total.",
        {"limit": {"type": "integer", "description": "Optional maximum number of most-recent receipts to return, e.g. 10. Omit to return the entire ledger."}}, []),
}

# Capability denylist, stated in code (spec 8.2/12.1): the US
# trading surface is ABSENT. A geoblock-circumvention-shaped
# request has no tool to land on.
FORBIDDEN_TOOL_NAMES = {
    "place_order", "post_order", "create_order", "order_place",
    "buy", "sell", "trade", "create_api_key", "api_key_create",
    "withdraw", "withdraw_funds", "deposit", "set_allowance",
    "vpn_route", "geo_bypass", "proxy_route", "account_get",
    "balance_get", "positions_get_private",
}


TITLES = {
    "forecast_sync": "Sync Polymarket Fixture Set into Book Core",
    "market_list": "List Polymarket Global Markets",
    "market_get": "Get One Polymarket Market with Criteria",
    "book_get": "Get Market Outcome Order Book",
    "quote_get": "Price a Market Outcome at a Size",
    "resolution_get": "Get Market Resolution Record",
    "venue_status": "Polymarket Adapter Status",
    "receipt_list": "List Shared Suite Ledger Receipts",
}

_READ = {"readOnlyHint": True, "destructiveHint": False,
         "idempotentHint": True, "openWorldHint": False}
ANNOTATIONS = {
    "forecast_sync": {"readOnlyHint": False, "destructiveHint": False,
                      "idempotentHint": True, "openWorldHint": False},
    "market_list": dict(_READ), "market_get": dict(_READ),
    "book_get": dict(_READ), "quote_get": dict(_READ),
    "resolution_get": dict(_READ), "venue_status": dict(_READ),
    "receipt_list": dict(_READ),
}

_S = {"type": "string"}
_SN = {"type": ["string", "null"]}
_MC = {"type": "string", "enum": ["REAL"],
       "description": "Money class of the data: Polymarket venue data is real-money data, so every output is labelled REAL even though this adapter moves no money."}
_LEVEL = {"type": "object",
          "properties": {"price": _S, "size": _S},
          "required": ["price", "size"]}
_DEPTH = {"type": "object",
          "properties": {
              "size_at_best_bid": _S, "size_at_best_ask": _S,
              "notional_at_best_ask": _S,
              "size_within_5_ticks_bid": _S,
              "size_within_5_ticks_ask": _S},
          "required": ["size_at_best_bid", "size_at_best_ask",
                       "notional_at_best_ask", "size_within_5_ticks_bid",
                       "size_within_5_ticks_ask"]}
_RESOLUTION = {"type": "object",
               "properties": {
                   "state": _S, "outcome": _SN, "source": _SN,
                   "evidence_ref": _SN,
                   "criteria_match": {"type": ["boolean", "null"]},
                   "history": {"type": "array", "items": {"type": "object"}}},
               "required": ["state", "outcome", "source", "evidence_ref",
                            "criteria_match", "history"]}
_INSTRUMENT = {"type": "object",
               "properties": {
                   "instrument_id": _S, "venue": _S, "native_id": _S,
                   "native_symbol": _SN, "kind": _S, "title": _S,
                   "underlying": _SN,
                   "outcomes": {"type": "array", "items": _S},
                   "resolution_source": _SN,
                   "trading_window": {
                       "type": "object",
                       "properties": {"open": _SN, "close": _SN}},
                   "tick": _SN, "min_size": _SN, "currency": _S,
                   "money_class": _S, "status": _S, "native_status": _SN,
                   "provenance": {"type": "object"},
                   "resolution": _RESOLUTION,
                   "resolution_criteria": _S,
                   "resolution_criteria_source_ref": _SN,
                   "criteria_versions": {
                       "type": "array",
                       "items": {"type": "object",
                                 "properties": {"hash": _S, "text": _S,
                                                "source_ref": _SN,
                                                "first_seen": _S},
                                 "required": ["hash", "text", "first_seen"]}}},
               "required": ["instrument_id", "venue", "native_id", "kind",
                            "title", "outcomes", "currency", "money_class",
                            "status", "provenance", "resolution",
                            "resolution_criteria", "criteria_versions"]}
_BOOK = {"type": "object",
         "properties": {
             "instrument_id": _S, "venue": _S, "as_of": _S,
             "book_type": _S,
             "bids": {"type": "array", "items": _LEVEL},
             "asks": {"type": "array", "items": _LEVEL},
             "native_ref": _SN, "outcome": _S,
             "depth_summary": _DEPTH, "spread_bps": _SN},
         "required": ["instrument_id", "venue", "as_of", "book_type",
                      "bids", "asks", "native_ref", "outcome",
                      "depth_summary", "spread_bps"]}
_COST = {"type": "object",
         "properties": {"total": _S, "per_unit": _S, "fee_amount": _S,
                        "fee_schedule_ref": _S},
         "required": ["total", "per_unit", "fee_amount", "fee_schedule_ref"]}
_COST_OR_UNVERIFIED = {"anyOf": [_COST, {"type": "string",
                                         "enum": ["FEE_UNVERIFIED"]}]}
_QUOTE = {"type": "object",
          "properties": {
              "instrument_id": _S, "venue": _S, "outcome": _S, "as_of": _S,
              "book_as_of": _S, "bid": _S, "ask": _S, "mid": _S,
              "mid_basis": _S, "spread_bps": _SN, "size": _S,
              "currency": _S, "money_class": _S, "liquidity_flag": _S,
              "depth_summary": _DEPTH, "fee_status": _S,
              "fee_schedule_ref": _SN, "book_stale": {"type": "boolean"},
              "all_in_buy_cost": _COST_OR_UNVERIFIED,
              "all_in_sell_value": _COST_OR_UNVERIFIED,
              "settlement_fee_model": {"type": "object"},
              "max_payout_for_size": _S,
              "resolution_criteria": _S, "resolution_criteria_hash": _S,
              "resolution_source": _SN},
          "required": ["instrument_id", "venue", "outcome", "as_of",
                       "book_as_of", "bid", "ask", "mid", "spread_bps",
                       "size", "currency", "money_class", "liquidity_flag",
                       "depth_summary", "fee_status", "fee_schedule_ref",
                       "book_stale", "all_in_buy_cost", "all_in_sell_value",
                       "resolution_criteria", "resolution_criteria_hash",
                       "resolution_source"]}
_RECEIPT = {"type": "object",
            "properties": {
                "seq": {"type": "integer"}, "ts": _S, "type": _S,
                "prev_hash": _S, "detail": {"type": "object"}, "hash": _S},
            "required": ["seq", "ts", "type", "prev_hash", "detail", "hash"]}

OUTPUT_SCHEMAS = {
    "forecast_sync": {
        "type": "object",
        "properties": {
            "synced": {"type": "array", "items": _S},
            "count": {"type": "integer"},
            "already_synced": {"type": "boolean",
                               "description": "Present and true only when the set was already synced; absent on a first sync."},
            "criteria_changed_events": {
                "type": "array",
                "items": {"type": "object",
                          "properties": {"type": _S, "instrument_id": _S,
                                         "old_hash": _S, "new_hash": _S},
                          "required": ["type", "instrument_id", "old_hash",
                                       "new_hash"]}},
            "money_class": _MC},
        "required": ["synced", "count", "criteria_changed_events",
                     "money_class"]},
    "market_list": {
        "type": "object",
        "properties": {
            "markets": {"type": "array", "items": {
                "type": "object",
                "properties": {"instrument_id": _S, "title": _S,
                               "status": _S, "native_status": _S,
                               "money_class": _S, "resolution_source": _SN},
                "required": ["instrument_id", "title", "status",
                             "native_status", "money_class",
                             "resolution_source"]}},
            "count": {"type": "integer"}, "money_class": _MC},
        "required": ["markets", "count", "money_class"]},
    "market_get": {
        "type": "object",
        "properties": {"market": _INSTRUMENT, "money_class": _MC},
        "required": ["market", "money_class"]},
    "book_get": {
        "type": "object",
        "properties": {"book": _BOOK, "money_class": _MC},
        "required": ["book", "money_class"]},
    "quote_get": {
        "type": "object",
        "properties": {"quote": _QUOTE, "money_class": _MC},
        "required": ["quote", "money_class"]},
    "resolution_get": {
        "type": "object",
        "properties": {
            "resolution": {"type": "object",
                           "properties": {
                               "instrument_id": _S, "state": _S,
                               "verification": _S, "outcome": _SN,
                               "source": _SN, "named_resolution_source": _SN,
                               "evidence_ref": _SN,
                               "criteria_match": {"type": ["boolean", "null"]},
                               "history": {"type": "array",
                                           "items": {"type": "object"}}},
                           "required": ["instrument_id", "state",
                                        "verification", "outcome", "source",
                                        "named_resolution_source",
                                        "evidence_ref", "criteria_match",
                                        "history"]},
            "money_class": _MC},
        "required": ["resolution", "money_class"]},
    "venue_status": {
        "type": "object",
        "properties": {
            "venue": _S, "as_of": _S, "capability": _S, "order_tools": _S,
            "account_tools": _S, "instruments": {"type": "integer"},
            "fee_schedule_ref": _SN, "fee_status": _S,
            "chain_valid": {"type": "boolean"}, "transport": _S,
            "us_trading_surface": _S},
        "required": ["venue", "as_of", "capability", "order_tools",
                     "account_tools", "instruments", "fee_schedule_ref",
                     "fee_status", "chain_valid", "transport",
                     "us_trading_surface"]},
    "receipt_list": {
        "type": "object",
        "properties": {
            "receipts": {"type": "array", "items": _RECEIPT},
            "count": {"type": "integer"},
            "chain_valid": {"type": "boolean"}, "money_class": _MC},
        "required": ["receipts", "count", "chain_valid", "money_class"]},
}

def tool_list():
    return [{"name": name, "title": TITLES[name], "description": description,
             "inputSchema": {"type": "object", "properties": props,
                             "required": required},
             "outputSchema": OUTPUT_SCHEMAS[name],
             "annotations": dict(ANNOTATIONS[name],
                                 title=TITLES[name])}
            for name, (_, description, props, required) in TOOLS.items()]



def handle(req: dict):
    method, rid = req.get("method", ""), req.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": req.get("params", {}).get(
                "protocolVersion", "2025-03-26"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "eclipse-forecast",
                           "version": "0.1.0"}}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid,
                "result": {"tools": tool_list()}}
    if method == "tools/call":
        name = req["params"]["name"]
        args = req["params"].get("arguments", {})
        if name in FORBIDDEN_TOOL_NAMES or name not in TOOLS:
            return {"jsonrpc": "2.0", "id": rid, "error": {
                "code": -32602,
                "message": f"unknown or line-forbidden tool {name}"}}
        state = load_core_state()
        try:
            out = TOOLS[name][0](state, args)
            save_core_state(state)
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text",
                             "text": json.dumps(out, indent=1)}]}}
        except ToolError as exc:
            save_core_state(state)
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text",
                             "text": json.dumps({"error": str(exc)})}],
                "isError": True}}
    if rid is not None:
        return {"jsonrpc": "2.0", "id": rid, "error": {
            "code": -32601, "message": f"method not found: {method}"}}
    return None


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            response = handle(json.loads(line))
        except Exception as exc:  # never crash the transport
            response = {"jsonrpc": "2.0", "id": None, "error": {
                "code": -32602, "message": "MALFORMED_CONTAINER: argument \'request\'"}}
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()

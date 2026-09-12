# -*- coding: utf-8 -*-
"""
Memecoin Intelligence Telegram Bot v4
Full trading intelligence card: KB + DexScreener + GoPlus audit
"""

import os, json, logging, base64, time
import requests as req
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
DS_API_KEY = os.environ["DATA_SOLUTIONS_API_KEY"]
DS_BASE_URL = "https://api.transpose.io/sql/analytical"
KB_GITHUB_URL = os.environ.get("KB_GITHUB_URL", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "ivndrmwn/memecoin-bot")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CHAINS = {
    "eth": "ethereum", "ethereum": "ethereum", "base": "base",
    "bsc": "bnb_smart_chain", "bnb": "bnb_smart_chain", "bnb_smart_chain": "bnb_smart_chain",
    "arb": "arbitrum_one", "arbitrum": "arbitrum_one",
    "poly": "polygon", "polygon": "polygon",
    "op": "optimism", "optimism": "optimism",
    "avax": "avalanche_c_chain", "linea": "linea", "zksync": "zksync",
}

# DexScreener chain IDs
DEX_CHAINS = {
    "ethereum": "ethereum", "base": "base", "bnb_smart_chain": "bsc",
    "arbitrum_one": "arbitrum", "polygon": "polygon", "optimism": "optimism",
    "avalanche_c_chain": "avalanche", "linea": "linea", "zksync": "zksync",
}

KB = {}


# ══════════════════════════════════════════════════════════════
# EXTERNAL APIs (DexScreener + GoPlus)
# ══════════════════════════════════════════════════════════════

def get_dexscreener(contract, chain):
    """Get market data from DexScreener."""
    dex_chain = DEX_CHAINS.get(chain, chain)
    try:
        r = req.get(f"https://api.dexscreener.com/latest/dex/tokens/{contract}", timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        pairs = data.get("pairs", [])
        if not pairs:
            return None
        # Find the pair on the right chain with most liquidity
        best = None
        for p in pairs:
            if p.get("chainId", "").lower() == dex_chain.lower():
                if not best or (p.get("liquidity", {}).get("usd", 0) or 0) > (best.get("liquidity", {}).get("usd", 0) or 0):
                    best = p
        if not best:
            best = pairs[0]
        return {
            "price_usd": float(best.get("priceUsd", 0) or 0),
            "mc": best.get("marketCap") or best.get("fdv") or 0,
            "liquidity": best.get("liquidity", {}).get("usd", 0) or 0,
            "volume_24h": best.get("volume", {}).get("h24", 0) or 0,
            "volume_1h": best.get("volume", {}).get("h1", 0) or 0,
            "price_change_5m": best.get("priceChange", {}).get("m5", 0) or 0,
            "price_change_1h": best.get("priceChange", {}).get("h1", 0) or 0,
            "price_change_6h": best.get("priceChange", {}).get("h6", 0) or 0,
            "price_change_24h": best.get("priceChange", {}).get("h24", 0) or 0,
            "buys_24h": best.get("txns", {}).get("h24", {}).get("buys", 0) or 0,
            "sells_24h": best.get("txns", {}).get("h24", {}).get("sells", 0) or 0,
            "pair_url": best.get("url", ""),
            "dex": best.get("dexId", ""),
            "pair_address": best.get("pairAddress", ""),
        }
    except Exception as e:
        logger.warning(f"DexScreener failed: {e}")
        return None


def get_goplus_audit(contract, chain):
    """Get security audit from GoPlus."""
    chain_ids = {"ethereum": "1", "base": "8453", "bnb_smart_chain": "56",
                 "arbitrum_one": "42161", "polygon": "137", "optimism": "10",
                 "avalanche_c_chain": "43114", "linea": "59144", "zksync": "324"}
    chain_id = chain_ids.get(chain, "1")
    try:
        r = req.get(f"https://api.gopluslabs.com/api/v1/token_security/{chain_id}?contract_addresses={contract}", timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        result = data.get("result", {})
        token_data = result.get(contract.lower(), {})
        if not token_data:
            return None
        return {
            "is_honeypot": token_data.get("is_honeypot") == "1",
            "has_blacklist": token_data.get("is_blacklisted") == "1",
            "can_mint": token_data.get("is_mintable") == "1",
            "buy_tax": float(token_data.get("buy_tax", 0) or 0) * 100,
            "sell_tax": float(token_data.get("sell_tax", 0) or 0) * 100,
            "is_proxy": token_data.get("is_proxy") == "1",
            "is_open_source": token_data.get("is_open_source") == "1",
            "holder_count": int(token_data.get("holder_count", 0) or 0),
            "top10_pct": float(token_data.get("top_10_holder_rate", 0) or 0) * 100,
        }
    except Exception as e:
        logger.warning(f"GoPlus failed: {e}")
        return None


# ══════════════════════════════════════════════════════════════
# KB MANAGEMENT
# ══════════════════════════════════════════════════════════════

def load_kb():
    if KB_GITHUB_URL:
        try:
            r = req.get(KB_GITHUB_URL, timeout=10)
            if r.status_code == 200: return r.json()
        except: pass
    kb_env = os.environ.get("KB_DATA", "")
    if kb_env:
        try: return json.loads(kb_env)
        except: pass
    return {"deployers": {}, "dev_adjacent": {}, "insider_wallets": {}, "smart_wallets": {},
            "holder_tracker": {}, "scan_history": [], "metadata": {"investigations_completed": 0, "tokens_investigated": []}}


def save_kb():
    if not GITHUB_TOKEN: return False
    try:
        headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
        kb_json = json.dumps(KB, separators=(",", ":"), ensure_ascii=True)
        content_b64 = base64.b64encode(kb_json.encode()).decode()
        r = req.get(f"https://api.github.com/repos/{GITHUB_REPO}/contents/kb_data.txt", headers=headers, timeout=10)
        payload = {"message": "KB auto-update", "content": content_b64}
        if r.status_code == 200: payload["sha"] = r.json().get("sha")
        r2 = req.put(f"https://api.github.com/repos/{GITHUB_REPO}/contents/kb_data.txt", json=payload, headers=headers, timeout=15)
        return r2.status_code in (200, 201)
    except: return False


def ds_query(sql):
    resp = req.post(DS_BASE_URL, json={"sql": sql},
                    headers={"Content-Type": "application/json", "X-API-Key": DS_API_KEY}, timeout=120)
    resp.raise_for_status()
    return resp.json()


def get_kb_lookup():
    addrs = {}
    for addr, data in KB.get("deployers", {}).items():
        addrs[addr] = {"tag": "DEV", "label": data.get("label", "?"), "risk": data.get("risk", "?")}
    for addr, data in KB.get("dev_adjacent", {}).items():
        addrs[addr] = {"tag": data.get("tag", "DEV_ADJ"), "label": data.get("label", "?")}
    for addr, data in KB.get("insider_wallets", {}).items():
        addrs[addr] = {"tag": "INSIDER", "label": data.get("label", "?"), "profit": data.get("profit_usd", 0)}
    for addr, data in KB.get("smart_wallets", {}).items():
        addrs[addr] = {"tag": "SMART_MONEY", "label": data.get("label", "?"), "category": data.get("category", "?")}
    return addrs


# ══════════════════════════════════════════════════════════════
# LEARNING ENGINE
# ══════════════════════════════════════════════════════════════

def learn_from_scan(report, chain):
    changed = False
    symbol = report.get("symbol", "?")
    KB.setdefault("deployers", {})
    for d in report.get("_deployers_raw", []):
        addr = d["address"]
        if addr not in KB["deployers"]:
            KB["deployers"][addr] = {"tag": "DEV", "risk": "unknown", "label": f"{symbol} deployer ({chain})",
                "chain": chain, "first_seen": d.get("first_mint", "?"), "tokens_deployed": [symbol], "notes": "Auto-discovered."}
            changed = True
        else:
            if symbol not in KB["deployers"][addr].get("tokens_deployed", []):
                KB["deployers"][addr].setdefault("tokens_deployed", []).append(symbol)
                count = len(KB["deployers"][addr]["tokens_deployed"])
                if count >= 3:
                    KB["deployers"][addr].update({"risk": "medium", "pattern": "serial_factory", "label": f"Serial deployer ({count} tokens)"})
                changed = True

    for d in report.get("_deployers_raw", []):
        try:
            r = ds_query(f"SELECT DISTINCT asset_symbol FROM {chain}.transfers_clustered WHERE receiver_address = '{d['address']}' AND sender_address = '0x0000000000000000000000000000000000000000' LIMIT 10")
            for row in r.get("results", []):
                t = row["asset_symbol"]
                if t != symbol and d["address"] in KB["deployers"]:
                    existing = KB["deployers"][d["address"]].setdefault("tokens_deployed", [])
                    if t not in existing: existing.append(t); changed = True
                    if len(existing) >= 3:
                        KB["deployers"][d["address"]].update({"risk": "medium", "pattern": "serial_factory", "label": f"Serial deployer ({len(existing)} tokens)"})
        except: pass

    KB.setdefault("holder_tracker", {})
    whale_alerts = []
    for h in report.get("holders", []):
        addr = h["address"]
        if addr in KB.get("deployers", {}) or addr in KB.get("dev_adjacent", {}) or h.get("is_pool"): continue
        tracker = KB["holder_tracker"].setdefault(addr, {"tokens": [], "name": "", "category": ""})
        if symbol not in tracker["tokens"]:
            tracker["tokens"].append(symbol)
            if h.get("name"): tracker["name"] = h["name"]
            if h.get("category"): tracker["category"] = h["category"]
            changed = True
        tc = len(tracker["tokens"])
        if tc >= 3 and addr not in KB.get("smart_wallets", {}):
            KB.setdefault("smart_wallets", {})
            name = tracker.get("name", "")
            cat = tracker.get("category", "")
            label = f"{name} [exchange] (in {tc} tokens)" if "exchange" in cat.lower() else (f"{name} (in {tc} tokens)" if name else f"Smart wallet (in {tc} tokens)")
            KB["smart_wallets"][addr] = {"tag": "SMART_MONEY", "category": "auto_detected", "label": label, "cross_token": tracker["tokens"], "notes": f"Auto-promoted: {tc} tokens."}
            changed = True
            whale_alerts.append(f"🐋 NEW smart wallet: {label}")
        elif tc >= 3 and addr in KB.get("smart_wallets", {}):
            KB["smart_wallets"][addr]["cross_token"] = tracker["tokens"]
        if addr in KB.get("smart_wallets", {}) and len(tracker["tokens"]) > 1 and tracker["tokens"][-1] == symbol:
            whale_alerts.append(f"🐋 {KB['smart_wallets'][addr].get('label', addr[:16])} is in {symbol}")
    report["whale_alerts"] = whale_alerts

    dist = report.get("distribution", {})
    if dist.get("free", 0) > 3 and dist.get("pool", 0) < 10:
        KB.setdefault("dev_adjacent", {})
        for d in report.get("_distribution_raw", []):
            addr = d.get("address", "")
            if not addr or addr in KB.get("deployers", {}): continue
            rcat = d.get("category", ""); rname = d.get("name", "")
            if any(x in rcat.lower() for x in ["exchange"]) or any(x in rname.lower() for x in ["burn", "uniswap", "pancakeswap"]): continue
            pct = d.get("pct", 0)
            if pct > 1 and addr not in KB["dev_adjacent"]:
                KB["dev_adjacent"][addr] = {"tag": "DEV_HOLDER", "label": f"{symbol} free recipient ({pct:.0f}%)",
                    "linked_deployer": report.get("_deployers_raw", [{}])[0].get("address", "?")}
                changed = True

    KB.setdefault("scan_history", [])
    KB["scan_history"].append({"symbol": symbol, "chain": chain, "risk": report.get("risk_score", "?"),
        "time": time.strftime("%Y-%m-%d %H:%M"), "kb_hits": len(report.get("dev_signals", [])) + len(report.get("insider_signals", [])) + len(report.get("smart_signals", []))})
    if len(KB["scan_history"]) > 50: KB["scan_history"] = KB["scan_history"][-50:]

    meta = KB.setdefault("metadata", {})
    investigated = meta.setdefault("tokens_investigated", [])
    if symbol not in investigated: investigated.append(symbol); meta["investigations_completed"] = len(investigated); changed = True
    if changed: save_kb()
    return changed


# ══════════════════════════════════════════════════════════════
# SCANNER
# ══════════════════════════════════════════════════════════════

def scan_token(contract, chain):
    ca = contract.lower()
    ca_frag = ca[2:] if ca.startswith("0x") else ca
    kbl = get_kb_lookup()
    report = {"contract": ca, "chain": chain, "risk_score": "unknown",
              "dev_signals": [], "insider_signals": [], "smart_signals": [],
              "distribution_signals": [], "deployer_info": [], "distribution_detail": [],
              "holders": [], "_deployers_raw": [], "_distribution_raw": [], "whale_alerts": []}

    # Find token
    try:
        r = ds_query(f"SELECT DISTINCT asset_symbol, asset_id FROM {chain}.transfers_clustered WHERE LOWER(asset_id) LIKE '%{ca_frag}%' LIMIT 1")
        if not r.get("results"): return {"error": f"Token not found on {chain}"}
        asset_id = r["results"][0]["asset_id"]; report["symbol"] = r["results"][0]["asset_symbol"]
    except Exception as e:
        return {"error": f"Search failed: {str(e)[:80]}"}

    # DexScreener market data
    report["market"] = get_dexscreener(ca, chain)

    # GoPlus audit
    report["audit"] = get_goplus_audit(ca, chain)

    # Stats from Data Solutions
    try:
        r = ds_query(f"SELECT COUNT(*) as txs, MIN(transaction_timestamp) as first_tx, MAX(transaction_timestamp) as last_tx, SUM(amount_usd) as volume FROM {chain}.transfers_clustered WHERE asset_id = '{asset_id}'")
        row = r["results"][0]
        report["stats"] = {"transfers": row["txs"], "volume_usd": row["volume"] or 0, "first": str(row["first_tx"])[:10], "last": str(row["last_tx"])[:10]}
    except: pass

    # Deployer
    deployers = []; total_supply = 0
    try:
        r = ds_query(f"SELECT receiver_address, SUM(amount_asset) as minted, MIN(transaction_timestamp) as first_mint FROM {chain}.transfers_clustered WHERE asset_id = '{asset_id}' AND sender_address = '0x0000000000000000000000000000000000000000' GROUP BY receiver_address ORDER BY minted DESC LIMIT 3")
        for row in r["results"]:
            m = row["minted"] or 0; total_supply += m
            d = {"address": row["receiver_address"], "minted": m, "first_mint": str(row["first_mint"])[:10]}
            if row["receiver_address"] in kbl:
                k = kbl[row["receiver_address"]]; d["kb_tag"] = k["tag"]; d["kb_label"] = k["label"]
                report["dev_signals"].append(f"Known deployer: {k['label']} [risk: {k.get('risk','?')}]")
            deployers.append(d); report["_deployers_raw"].append(d)
        report["total_supply"] = total_supply
    except: pass

    for d in deployers:
        pct = d["minted"] / total_supply * 100 if total_supply > 0 else 0
        tag_str = f" [{d['kb_tag']}: {d['kb_label']}]" if "kb_tag" in d else ""
        report["deployer_info"].append(f"{d['address'][:12]}...{d['address'][-4:]} minted {pct:.0f}% on {d['first_mint']}{tag_str}")

    # Distribution
    if deployers:
        try:
            d_addr = deployers[0]["address"]
            r = ds_query(f"SELECT receiver_address, receiver_name, receiver_category, SUM(amount_asset) as total_sent FROM {chain}.transfers_clustered WHERE sender_address = '{d_addr}' AND asset_id = '{asset_id}' GROUP BY receiver_address, receiver_name, receiver_category ORDER BY total_sent DESC LIMIT 10")
            supply = total_supply or 1; burn = pool = exchange = 0; free = 0
            for row in r["results"]:
                total = row["total_sent"] or 0; pct = total / supply * 100
                rname = row["receiver_name"] or ""; rcat = row["receiver_category"] or ""
                addr_short = row["receiver_address"][:12] + "..." + row["receiver_address"][-4:]
                report["_distribution_raw"].append({"address": row["receiver_address"], "name": rname, "category": rcat, "pct": pct})
                if "burn" in rname.lower() or row["receiver_address"].startswith("0x00000000000000000000000000000000000"):
                    burn += pct; report["distribution_detail"].append(f"Burned: {pct:.1f}%")
                elif "exchange" in rcat.lower():
                    exchange += pct; report["distribution_detail"].append(f"{rname or addr_short} [exchange]: {pct:.1f}%")
                elif any(x in rname.lower() for x in ["uniswap", "pancakeswap", "sushiswap", "aerodrome"]):
                    pool += pct; report["distribution_detail"].append(f"{rname or addr_short} [DEX]: {pct:.1f}%")
                else:
                    report["distribution_detail"].append(f"{rname or addr_short} [{rcat or '?'}]: {pct:.1f}%")
                    if row["receiver_address"] != d_addr: free += 1
                if row["receiver_address"] in kbl:
                    k = kbl[row["receiver_address"]]
                    if "DEV" in k["tag"]: report["dev_signals"].append(f"Sent {pct:.1f}% to {k['label']}")
                    elif k["tag"] == "INSIDER": report["insider_signals"].append(f"Sent {pct:.1f}% to {k['label']}")
                    elif k["tag"] == "SMART_MONEY": report["smart_signals"].append(f"Sent {pct:.1f}% to {k['label']}")
            report["distribution"] = {"burn": round(burn, 1), "pool": round(pool, 1), "exchange": round(exchange, 1), "free": free}
            if free > 3 and pool < 10: report["distribution_signals"].append(f"{free} wallets received free tokens, only {pool:.0f}% to DEX")
        except: pass

    # Top holders
    try:
        r = ds_query(f"""SELECT address, balance, name, category FROM (
            SELECT address, SUM(received) - SUM(sent) as balance FROM (
                SELECT receiver_address as address, SUM(amount_asset) as received, 0 as sent FROM {chain}.transfers_clustered WHERE asset_id = '{asset_id}' AND receiver_address != '0x0000000000000000000000000000000000000000' GROUP BY receiver_address
                UNION ALL SELECT sender_address as address, 0 as received, SUM(amount_asset) as sent FROM {chain}.transfers_clustered WHERE asset_id = '{asset_id}' AND sender_address != '0x0000000000000000000000000000000000000000' GROUP BY sender_address
            ) t GROUP BY address) t2
            LEFT JOIN (SELECT receiver_address as address, receiver_name as name, receiver_category as category FROM {chain}.transfers_clustered WHERE asset_id = '{asset_id}' AND receiver_name IS NOT NULL GROUP BY receiver_address, receiver_name, receiver_category) n USING (address)
            WHERE balance > 0 ORDER BY balance DESC LIMIT 10""")
        supply = total_supply or 1
        for row in r["results"]:
            bal = row["balance"] or 0; pct = bal / supply * 100
            h = {"address": row["address"], "addr_short": row["address"][:12] + "..." + row["address"][-4:],
                 "pct": round(pct, 1), "name": row.get("name") or "", "category": row.get("category") or ""}
            if pct > 100: h["is_pool"] = True
            if row["address"] in kbl:
                k = kbl[row["address"]]; h["kb_tag"] = k["tag"]; h["kb_label"] = k["label"]
                if "DEV" in k["tag"]: report["dev_signals"].append(f"{k['label']} holds {pct:.1f}%")
                elif k["tag"] == "INSIDER": report["insider_signals"].append(f"{k['label']} holds {pct:.1f}%")
                elif k["tag"] == "SMART_MONEY": report["smart_signals"].append(f"{k['label']} holds {pct:.1f}%")
            report["holders"].append(h)
    except: pass

    # Risk score
    has_dev = len(report["dev_signals"]) > 0; has_insider = len(report["insider_signals"]) > 0
    has_smart = len(report["smart_signals"]) > 0; has_dist = len(report["distribution_signals"]) > 0
    audit = report.get("audit") or {}
    if audit.get("is_honeypot"): report["risk_score"] = "critical"; report.setdefault("distribution_signals", []).append("HONEYPOT detected")
    elif has_insider and has_dev: report["risk_score"] = "critical"
    elif has_dist and has_dev: report["risk_score"] = "critical"
    elif has_dist: report["risk_score"] = "high"
    elif audit.get("sell_tax", 0) > 5: report["risk_score"] = "high"
    elif has_dev: report["risk_score"] = "medium"
    elif has_smart: report["risk_score"] = "low_with_smart_money"
    else: report["risk_score"] = "clean"
    report["kb_size"] = len(kbl)

    # Concentration
    real_h = [h for h in report["holders"] if not h.get("is_pool") and h.get("pct", 0) <= 100][:5]
    report["concentration"] = sum(h.get("pct", 0) for h in real_h)

    # Learn
    report["kb_updated"] = learn_from_scan(report, chain)
    return report


# ══════════════════════════════════════════════════════════════
# REPORT FORMATTER
# ══════════════════════════════════════════════════════════════

def fmt_change(pct):
    if pct > 0: return f"▲{pct:.1f}%"
    elif pct < 0: return f"▼{abs(pct):.1f}%"
    else: return "—"

def fmt_num(n):
    if n >= 1_000_000: return f"${n/1_000_000:.1f}M"
    elif n >= 1_000: return f"${n/1_000:.1f}k"
    else: return f"${n:.0f}"


def format_report(r):
    if "error" in r: return f"Error: {r['error']}"
    risk = r.get("risk_score", "unknown")
    icon = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low_with_smart_money": "🟢", "clean": "⚪"}.get(risk, "⚫")
    sym = r.get("symbol", "?")
    chain = r.get("chain", "?")
    ca = r.get("contract", "")

    lines = [f"{icon} {sym} on {chain}", f"Risk: {risk.upper()}"]

    # Risk reasons
    dev = r.get("dev_signals", []); ins = r.get("insider_signals", []); sm = r.get("smart_signals", [])
    dist_s = r.get("distribution_signals", [])
    audit = r.get("audit") or {}
    if audit.get("is_honeypot"): lines.append("  → 🚫 HONEYPOT DETECTED")
    if dist_s:
        for s in dist_s: lines.append(f"  → {s}")
    if dev and not ins: lines.append(f"  → {len(set(dev))} deployer/dev signals")
    if ins: lines.append(f"  → Known insider detected")
    if sm: lines.append(f"  → {len(set(sm))} smart money wallets holding")
    if not dev and not ins and not dist_s and not audit.get("is_honeypot"):
        lines.append("  → No known risks detected")

    # Market data (DexScreener)
    mkt = r.get("market")
    if mkt:
        lines += ["", "💲 MARKET"]
        lines.append(f"  MC: {fmt_num(mkt['mc'])} · Liq: {fmt_num(mkt['liquidity'])}")
        lines.append(f"  5m: {fmt_change(mkt['price_change_5m'])} · 1h: {fmt_change(mkt['price_change_1h'])} · 6h: {fmt_change(mkt['price_change_6h'])} · 24h: {fmt_change(mkt['price_change_24h'])}")
        lines.append(f"  Vol 1h: {fmt_num(mkt['volume_1h'])} · 24h: {fmt_num(mkt['volume_24h'])}")
        lines.append(f"  24h: {mkt['buys_24h']} buys / {mkt['sells_24h']} sells")

    # Audit (GoPlus)
    if audit:
        lines += ["", "🛡 AUDIT"]
        hp = "🚫 HONEYPOT" if audit.get("is_honeypot") else "✅ Not honeypot"
        bl = "⚠️ Has blacklist" if audit.get("has_blacklist") else "✅ No blacklist"
        mint = "⚠️ Can mint" if audit.get("can_mint") else "✅ Cannot mint"
        src = "✅ Verified source" if audit.get("is_open_source") else "❓ Unverified"
        lines.append(f"  {hp} · {bl}")
        lines.append(f"  {mint} · {src}")
        lines.append(f"  Buy tax: {audit.get('buy_tax', 0):.1f}% · Sell tax: {audit.get('sell_tax', 0):.1f}%")
        if audit.get("holder_count"):
            lines.append(f"  Holders: {audit['holder_count']:,} · Top 10: {audit.get('top10_pct', 0):.0f}%")

    # Concentration (from our data)
    conc = r.get("concentration", 0)
    if conc > 0:
        grade = "🔴 Very concentrated" if conc > 80 else ("🟠 Concentrated" if conc > 50 else ("🟡 Moderate" if conc > 25 else "🟢 Distributed"))
        lines.append(f"\n🏦 Top 5 hold {conc:.0f}% — {grade}")

    # On-chain stats
    stats = r.get("stats", {})
    if stats:
        lines += ["", "📊 ON-CHAIN"]
        lines.append(f"  Transfers: {stats.get('transfers', 0):,}")
        lines.append(f"  Active: {stats.get('first', '')} → {stats.get('last', '')}")

    # Deployer
    di = r.get("deployer_info", [])
    if di:
        lines += ["", "🔨 DEPLOYER"]
        lines += [f"  {d}" for d in di]

    # Distribution
    dd = r.get("distribution_detail", [])
    if dd:
        lines += ["", "💰 DISTRIBUTION"]
        lines += [f"  {d}" for d in dd[:6]]

    # Warnings
    if dist_s:
        lines += ["", "⚠️ WARNING"]
        lines += [f"  {s}" for s in dist_s]

    # Whale alerts
    wa = r.get("whale_alerts", [])
    if wa: lines += [""]; lines += wa[:5]

    # Dev / Insider / Smart
    if dev:
        lines += ["", "🚨 KNOWN DEV"]
        lines += [f"  • {s}" for s in sorted(set(dev))[:5]]
    if ins:
        lines += ["", "🚩 KNOWN INSIDER"]
        lines += [f"  • {s}" for s in sorted(set(ins))[:5]]
    if sm:
        lines += ["", "🧠 SMART MONEY"]
        lines += [f"  • {s}" for s in sorted(set(sm))[:5]]

    # Top holders
    holders = r.get("holders", [])
    if holders:
        lines += ["", "👑 TOP HOLDERS"]
        for h in holders[:5]:
            pct = h.get("pct", 0)
            if h.get("is_pool"): label = f"🔄 Pool/Router"
            elif h.get("kb_tag", "").startswith("DEV"): label = f"🚨 {h.get('kb_label', 'DEV')}"
            elif h.get("kb_tag") == "INSIDER": label = f"🚩 {h.get('kb_label', 'INSIDER')}"
            elif h.get("kb_tag") == "SMART_MONEY": label = f"🧠 {h.get('kb_label', 'SMART')}"
            elif h.get("name"): label = h["name"] + (f" [{h['category']}]" if h.get("category") else "")
            else: label = h.get("addr_short", "")
            lines.append(f"  {pct}% — {label}")

    # Footer
    total_hits = len(set(dev)) + len(set(ins)) + len(set(sm))
    lines += [""]
    lines.append(f"📋 {r.get('kb_size', 0)} tracked wallets" + (f" · {total_hits} matches" if total_hits > 0 else ""))

    # Links
    dex_chain = DEX_CHAINS.get(chain, chain)
    lines.append(f"\n🔗 DexScreener · GMGN · Basedbot")
    lines.append(f"https://dexscreener.com/{dex_chain}/{ca}")

    if r.get("kb_updated"): lines.append("\n📝 KB updated")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════
# TELEGRAM COMMANDS
# ══════════════════════════════════════════════════════════════

async def scan_command(update, context):
    args = context.args
    if len(args) < 1:
        await update.message.reply_text("Usage: /scan <contract> <chain>\nChains: eth, base, bsc, arb, poly, op, avax")
        return
    contract = args[0]
    chain_input = args[1] if len(args) > 1 else "ethereum"
    chain = CHAINS.get(chain_input.lower(), chain_input.lower())
    if not contract.startswith("0x") or len(contract) != 42:
        await update.message.reply_text("Invalid address. Must be 0x + 40 hex chars.")
        return
    msg = await update.message.reply_text(f"⏳ Scanning {contract[:10]}...{contract[-6:]} on {chain}...")
    try:
        result = scan_token(contract, chain)
        text = format_report(result)
        if len(text) > 4096: text = text[:4090] + "\n..."
        await msg.edit_text(text, disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"Scan failed: {e}")
        await msg.edit_text(f"Scan failed: {str(e)[:200]}")


async def kb_command(update, context):
    total = sum(len(KB.get(s, {})) for s in ["deployers", "dev_adjacent", "insider_wallets", "smart_wallets"])
    d = len(KB.get("deployers", {})); da = len(KB.get("dev_adjacent", {}))
    i = len(KB.get("insider_wallets", {})); s = len(KB.get("smart_wallets", {}))
    ht = len(KB.get("holder_tracker", {})); tokens = KB.get("metadata", {}).get("tokens_investigated", [])
    serial = sum(1 for x in KB.get("deployers", {}).values() if len(x.get("tokens_deployed", [])) >= 3)
    auto = [v for v in KB.get("smart_wallets", {}).values() if "Auto" in v.get("notes", "")]
    lines = ["📋 Knowledge Base", "", f"  🔨 Deployers: {d}" + (f" ({serial} serial)" if serial else ""),
        f"  👥 Dev-adjacent: {da}", f"  🚩 Insiders: {i}", f"  🧠 Smart money: {s}",
        f"  👁 Holders tracked: {ht}", f"  📊 Total tagged: {total}", f"  🪙 Tokens: {len(tokens)}", ""]
    if tokens: lines.append(f"  {', '.join(tokens[-8:])}")
    if auto: lines += ["", f"  🆕 Auto-discovered: {len(auto)} smart wallets"]
    await update.message.reply_text("\n".join(lines))


async def history_command(update, context):
    history = KB.get("scan_history", [])
    if not history: await update.message.reply_text("No scans yet."); return
    icons = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low_with_smart_money": "🟢", "clean": "⚪"}
    lines = ["📜 Scan History", ""]
    for scan in history[-20:]:
        ic = icons.get(scan.get("risk", ""), "⚫")
        hits = scan.get("kb_hits", 0); hs = f" · {hits} hits" if hits > 0 else ""
        lines.append(f"  {ic} {scan.get('symbol','?')} on {scan.get('chain','?')} — {scan.get('risk','?').upper()}{hs}")
        lines.append(f"      {scan.get('time', '')}")
    await update.message.reply_text("\n".join(lines))


async def feed_command(update, context):
    sw = KB.get("smart_wallets", {}); tracker = KB.get("holder_tracker", {})
    if not sw: await update.message.reply_text("No smart wallets yet. Scan more tokens."); return
    lines = ["🧠 Smart Money Feed", ""]
    for addr, data in sw.items():
        cross = data.get("cross_token", []) or tracker.get(addr, {}).get("tokens", [])
        if len(cross) >= 2:
            lines.append(f"  • {data.get('label', addr[:16])}")
            lines.append(f"    {', '.join(cross[-6:])}")
            lines.append("")
    if len(lines) <= 2: lines.append("  Keep scanning. Need 3+ tokens per wallet.")
    await update.message.reply_text("\n".join(lines))


async def reload_command(update, context):
    global KB; KB = load_kb()
    total = sum(len(KB.get(s, {})) for s in ["deployers", "dev_adjacent", "insider_wallets", "smart_wallets"])
    await update.message.reply_text(f"🔄 KB reloaded: {total} wallets, {len(KB.get('holder_tracker', {}))} tracked, {len(KB.get('metadata', {}).get('tokens_investigated', []))} tokens")


async def start_command(update, context):
    await update.message.reply_text(
        "🔬 Memecoin Scanner v4\n\n"
        "/scan <contract> <chain> — full scan\n"
        "/kb — knowledge base stats\n"
        "/history — last 20 scans\n"
        "/feed — smart money activity\n"
        "/reload — refresh KB\n\n"
        "Features: DexScreener market data, GoPlus audit,\n"
        "smart wallet detection, whale alerts, serial deployer\n"
        "detection, insider flagging, concentration score.\n\n"
        "Chains: eth, base, bsc, arb, poly, op, avax")


def main():
    global KB; KB = load_kb()
    total = sum(len(KB.get(s, {})) for s in ["deployers", "dev_adjacent", "insider_wallets", "smart_wallets"])
    logger.info(f"KB: {total} wallets"); logger.info("Bot v4 started.")
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    for cmd, fn in [("start", start_command), ("scan", scan_command), ("kb", kb_command),
                    ("history", history_command), ("feed", feed_command), ("reload", reload_command)]:
        app.add_handler(CommandHandler(cmd, fn))
    app.run_polling()

if __name__ == "__main__":
    main()

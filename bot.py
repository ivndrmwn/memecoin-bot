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
    if not GITHUB_TOKEN:
        logger.warning("GitHub push skipped: GITHUB_TOKEN is empty")
        return False
    try:
        logger.info(f"GitHub push starting. Repo: {GITHUB_REPO}")
        headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
        kb_json = json.dumps(KB, separators=(",", ":"), ensure_ascii=True)
        content_b64 = base64.b64encode(kb_json.encode()).decode()
        logger.info(f"GitHub push: KB size {len(kb_json)} bytes")
        r = req.get(f"https://api.github.com/repos/{GITHUB_REPO}/contents/kb_data.txt", headers=headers, timeout=10)
        logger.info(f"GitHub GET status: {r.status_code}")
        payload = {"message": "KB auto-update", "content": content_b64}
        if r.status_code == 200:
            payload["sha"] = r.json().get("sha")
        elif r.status_code == 404:
            logger.info("GitHub: file does not exist yet, creating")
        else:
            logger.warning(f"GitHub GET failed: {r.status_code} {r.text[:200]}")
        r2 = req.put(f"https://api.github.com/repos/{GITHUB_REPO}/contents/kb_data.txt", json=payload, headers=headers, timeout=15)
        logger.info(f"GitHub PUT status: {r2.status_code}")
        if r2.status_code in (200, 201):
            logger.info("GitHub push SUCCESS")
            return True
        else:
            logger.warning(f"GitHub push FAILED: {r2.status_code} {r2.text[:200]}")
            return False
    except Exception as e:
        logger.error(f"GitHub push ERROR: {e}")
        return False


def ds_query(sql):
    resp = req.post(DS_BASE_URL, json={"sql": sql},
                    headers={"Content-Type": "application/json", "X-API-Key": DS_API_KEY}, timeout=120)
    resp.raise_for_status()
    return resp.json()


def get_kb_lookup():
    addrs = {}
    for addr, data in KB.get("deployers", {}).items():
        addrs[addr] = {"tag": "DEV", "label": data.get("label", "?"), "risk": data.get("risk", "?"), "confidence": data.get("confidence", 0)}
    for addr, data in KB.get("dev_adjacent", {}).items():
        addrs[addr] = {"tag": data.get("tag", "DEV_ADJ"), "label": data.get("label", "?"), "confidence": 0}
    for addr, data in KB.get("insider_wallets", {}).items():
        addrs[addr] = {"tag": "INSIDER", "label": data.get("label", "?"), "profit": data.get("profit_usd", 0), "confidence": 0}
    for addr, data in KB.get("smart_wallets", {}).items():
        addrs[addr] = {"tag": "SMART_MONEY", "label": data.get("label", "?"), "category": data.get("category", "?"), "confidence": data.get("confidence", 0)}
    return addrs


def get_confidence(addr):
    """Get confidence score (0-10) for a wallet."""
    scores = KB.get("wallet_scores", {})
    if addr in scores:
        s = scores[addr]
        total = s.get("wins", 0) + s.get("losses", 0)
        if total >= 2:
            return round(s["wins"] / total * 10, 1)
    return 0


def update_confidence_scores():
    """Recalculate confidence scores for all tracked wallets based on token outcomes."""
    tracker = KB.get("holder_tracker", {})
    token_mcs = KB.get("token_mcs", {})
    scores = KB.setdefault("wallet_scores", {})

    for addr, data in tracker.items():
        wins = 0
        losses = 0
        for token in data.get("tokens", []):
            mc_data = token_mcs.get(token, {})
            entry_mc = mc_data.get("entry_mc", {}).get(addr, 0)
            current_mc = mc_data.get("current_mc", 0)
            if entry_mc > 0 and current_mc > 0:
                if current_mc >= entry_mc:
                    wins += 1
                else:
                    losses += 1
        total = wins + losses
        if total >= 1:
            scores[addr] = {"wins": wins, "losses": losses, "total": total, "win_rate": round(wins / total, 2)}

    # Update confidence in smart_wallets
    for addr, data in KB.get("smart_wallets", {}).items():
        if addr in scores:
            data["confidence"] = round(scores[addr]["wins"] / max(scores[addr]["total"], 1) * 10, 1)


def record_token_mc(symbol, mc, holders):
    """Record market cap for confidence tracking."""
    if mc and mc > 0:
        mcs = KB.setdefault("token_mcs", {})
        if symbol not in mcs:
            mcs[symbol] = {"current_mc": mc, "entry_mc": {}}
        mcs[symbol]["current_mc"] = mc
        # Record entry MC for wallets seeing this token for the first time
        for h in holders:
            addr = h.get("address", "")
            if addr and addr not in mcs[symbol]["entry_mc"] and not h.get("is_pool"):
                mcs[symbol]["entry_mc"][addr] = mc


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

    # Learn bundle wallets (tag as dev-adjacent)
    KB.setdefault("bundle_tracker", {})
    for b in report.get("bundle", []):
        addr = b["address"]
        if addr not in KB.get("deployers", {}) and b.get("bought_pct", 0) >= 1:
            bt = KB["bundle_tracker"].setdefault(addr, {"tokens": [], "total_pct": 0})
            if symbol not in bt["tokens"]:
                bt["tokens"].append(symbol)
                bt["total_pct"] += b["bought_pct"]
                changed = True
            # If bundled in 2+ tokens, flag as suspicious
            if len(bt["tokens"]) >= 2 and addr not in KB.get("dev_adjacent", {}):
                KB.setdefault("dev_adjacent", {})
                KB["dev_adjacent"][addr] = {
                    "tag": "DEV_BUNDLER",
                    "label": f"Serial bundler ({len(bt['tokens'])} tokens, {bt['total_pct']:.0f}% avg)",
                    "tokens_bundled": bt["tokens"],
                    "notes": "Auto-discovered: bundled at launch in multiple tokens.",
                }
                changed = True
                logger.info(f"KB+: serial bundler {addr[:16]}... ({len(bt['tokens'])} tokens)")

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
            conf = get_confidence(addr)
            conf_str = f" ({conf}/10)" if conf > 0 else ""
            whale_alerts.append(f"🐋 {KB['smart_wallets'][addr].get('label', addr[:16])}{conf_str} is in {symbol}")
    report["whale_alerts"] = whale_alerts

    # Record market cap for confidence tracking
    mkt = report.get("market") or {}
    mc = mkt.get("mc", 0)
    if mc:
        record_token_mc(symbol, mc, report.get("holders", []))
        update_confidence_scores()

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

    # Bundle detection: find wallets that bought in the first minute of launch
    bundle = []
    try:
        first_tx = report.get("stats", {}).get("first", "")
        if first_tx and asset_id:
            r = ds_query(f"""SELECT receiver_address, SUM(amount_asset) as bought, COUNT(*) as txs,
                MIN(transaction_timestamp) as first_buy
                FROM {chain}.transfers_clustered
                WHERE asset_id = '{asset_id}'
                  AND transaction_timestamp >= '{first_tx}'
                  AND transaction_timestamp < '{first_tx[:10]}T{first_tx[11:13]}:{str(int(first_tx[14:16])+5).zfill(2)}:00'
                  AND sender_address != '0x0000000000000000000000000000000000000000'
                  AND receiver_address != '0x0000000000000000000000000000000000000000'
                GROUP BY receiver_address
                ORDER BY first_buy ASC
                LIMIT 20""")
            deployer_addrs = set(d["address"] for d in deployers)
            supply = total_supply or 1
            for row in r.get("results", []):
                addr = row["receiver_address"]
                if addr in deployer_addrs: continue
                bought = row["bought"] or 0
                pct = bought / supply * 100
                if pct < 0.1: continue
                b = {"address": addr, "addr_short": addr[:12] + "..." + addr[-4:],
                     "bought_pct": round(pct, 1), "txs": row["txs"],
                     "first_buy": str(row["first_buy"])[:19]}
                if addr in kbl:
                    k = kbl[addr]
                    b["kb_tag"] = k["tag"]; b["kb_label"] = k["label"]
                bundle.append(b)
            report["bundle"] = bundle
            if len(bundle) >= 3:
                total_bundle_pct = sum(b["bought_pct"] for b in bundle)
                report["distribution_signals"].append(f"{len(bundle)} wallets bundled at launch ({total_bundle_pct:.0f}% of supply)")
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
                conf = get_confidence(row["address"])
                h["confidence"] = conf
                conf_str = f" ({conf}/10)" if conf > 0 else ""
                if "DEV" in k["tag"]: report["dev_signals"].append(f"{k['label']} holds {pct:.1f}%")
                elif k["tag"] == "INSIDER": report["insider_signals"].append(f"{k['label']} holds {pct:.1f}%")
                elif k["tag"] == "SMART_MONEY": report["smart_signals"].append(f"{k['label']}{conf_str} holds {pct:.1f}%")
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

    # Bundle
    bundle = r.get("bundle", [])
    if bundle:
        lines += ["", "🎯 LAUNCH BUNDLE (first buyers)"]
        for b in bundle[:5]:
            pct = b.get("bought_pct", 0)
            addr = b.get("addr_short", "")
            if b.get("kb_tag", "").startswith("DEV"): label = f"🚨 {b.get('kb_label', 'DEV')}"
            elif b.get("kb_tag") == "INSIDER": label = f"🚩 {b.get('kb_label', '')}"
            elif b.get("kb_tag") == "SMART_MONEY":
                conf = get_confidence(b["address"])
                conf_str = f" ({conf}/10)" if conf > 0 else ""
                label = f"🧠 {b.get('kb_label', '')}{conf_str}"
            else: label = addr
            lines.append(f"  {pct}% — {label} · {b.get('first_buy', '')[-8:]}")

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
            conf = h.get("confidence", 0)
            conf_badge = f" ⭐{conf}/10" if conf >= 5 else (f" {conf}/10" if conf > 0 else "")
            if h.get("is_pool"): label = f"🔄 Pool/Router"
            elif h.get("kb_tag", "").startswith("DEV"): label = f"🚨 {h.get('kb_label', 'DEV')}"
            elif h.get("kb_tag") == "INSIDER": label = f"🚩 {h.get('kb_label', 'INSIDER')}"
            elif h.get("kb_tag") == "SMART_MONEY": label = f"🧠 {h.get('kb_label', 'SMART')}{conf_badge}"
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


async def batch_command(update, context):
    """Batch scan multiple tokens. Usage: /batch base 0xABC 0xDEF 0xGHI"""
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: /batch <chain> <contract1> <contract2> ...\n\n"
            "Example:\n/batch base 0xABC...123 0xDEF...456 0xGHI...789\n\n"
            "Scans all tokens, learns from each, pushes KB once at the end.")
        return

    chain_input = args[0]
    chain = CHAINS.get(chain_input.lower(), chain_input.lower())
    contracts = [a for a in args[1:] if a.startswith("0x") and len(a) == 42]

    if not contracts:
        await update.message.reply_text("No valid contract addresses found. Each must be 0x + 40 hex chars.")
        return

    msg = await update.message.reply_text(f"⏳ Batch scanning {len(contracts)} tokens on {chain}...")

    results = []
    icons = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low_with_smart_money": "🟢", "clean": "⚪"}

    for i, contract in enumerate(contracts):
        try:
            await msg.edit_text(f"⏳ Scanning {i+1}/{len(contracts)}: {contract[:10]}...{contract[-6:]}")
            result = scan_token(contract, chain)
            sym = result.get("symbol", "?")
            risk = result.get("risk_score", "?")
            icon = icons.get(risk, "⚫")
            hits = len(result.get("dev_signals", [])) + len(result.get("insider_signals", [])) + len(result.get("smart_signals", []))
            whales = len(result.get("whale_alerts", []))
            results.append(f"  {icon} {sym} — {risk.upper()}" + (f" · {hits} KB hits" if hits else "") + (f" · {whales} 🐋" if whales else ""))
        except Exception as e:
            results.append(f"  ⚫ {contract[:10]}... — ERROR: {str(e)[:40]}")

    # Summary
    total = sum(len(KB.get(s, {})) for s in ["deployers", "dev_adjacent", "insider_wallets", "smart_wallets"])
    tracked = len(KB.get("holder_tracker", {}))
    tokens_count = len(KB.get("metadata", {}).get("tokens_investigated", []))

    lines = [f"📦 Batch Scan Complete: {len(contracts)} tokens on {chain}", ""]
    lines += results
    lines += ["", f"📋 KB: {total} tagged · {tracked} holders tracked · {tokens_count} tokens"]
    lines.append("📝 KB updated and pushed to GitHub")

    text = "\n".join(lines)
    if len(text) > 4096: text = text[:4090] + "\n..."
    await msg.edit_text(text)


async def topscan_command(update, context):
    """Auto-discover and scan top tokens on a chain. Usage: /topscan base 20"""
    args = context.args
    if len(args) < 1:
        await update.message.reply_text(
            "Usage: /topscan <chain> [count]\n\n"
            "Auto-discovers the most traded tokens and scans them.\n"
            "Default: top 20 tokens.\n\n"
            "Example:\n/topscan base 30\n/topscan bsc 20")
        return

    chain_input = args[0]
    chain = CHAINS.get(chain_input.lower(), chain_input.lower())
    count = min(int(args[1]), 50) if len(args) > 1 and args[1].isdigit() else 20

    msg = await update.message.reply_text(f"⏳ Discovering top {count} memecoins on {chain}...")

    # Exclude known non-memecoins across all chains
    exclude = (
        "'ETH','WETH','USDC','USDT','DAI','WBTC','WBNB','BUSD','BNB','SOL','MATIC','ARB','OP',"
        "'cbBTC','USDbC','cbETH','rETH','wstETH','stETH','AERO','WELL','OVN','USD+',"
        "'CBBTC','SAND','CBXRP','USDE','VVV','MORPHO','CBETH','WSTETH','EURC',"
        "'CBMEGA','CBADA','ZEN','CBDOGE','USAD','SERV','CBLTC','WEETH','ZRO',"
        "'AWETH','MSUSD','COMP','UNI','LINK','SNX','BAL','GRT','CRV','SUSHI',"
        "'AAVE','MKR','LDO','RPL','TBTC','MSETH','USDS','UXRP','CBHYPE','CBZEC',"
        "'eUSD','msUSD','sUSD','rsETH','WUSD','BRZ','BTCB','BETH','CAKE','XVS',"
        "'BAKE','ALPACA','BSW','TUSD','FDUSD','SFM','SAFEMOON','XRP','ADA',"
        "'DOT','AVAX','ATOM','FIL','ICP','NEAR','APT','SUI','SEI','TIA',"
        "'PYTH','JUP','RAY','ORCA','MSOL','JSOL','BONK','WIF','JTO','W',"
        "'RENDER','FET','AGIX','OCEAN','TAO','KAS','INJ','TRX','TON','XLM'"
    )

    # Fetch with volume filter to get real tokens only
    try:
        r = ds_query(f"""
            SELECT asset_symbol, asset_id, COUNT(*) as txs, SUM(amount_usd) as vol
            FROM {chain}.transfers_clustered
            WHERE transaction_timestamp >= '2026-06-01'
              AND asset_symbol NOT IN ({exclude})
              AND asset_id NOT LIKE '%native%'
              AND LENGTH(asset_symbol) BETWEEN 2 AND 12
            GROUP BY asset_symbol, asset_id
            HAVING txs > 5000 AND vol > 100000
            ORDER BY vol DESC LIMIT {count * 3}
        """)
    except Exception as e:
        await msg.edit_text(f"Discovery failed: {str(e)[:100]}")
        return

    if not r.get("results"):
        await msg.edit_text(f"No memecoins found on {chain}.")
        return

    # Filter: only ASCII names, no empty, no known DeFi patterns
    filtered = []
    for row in r["results"]:
        sym = row["asset_symbol"]
        if not sym or not sym.strip(): continue
        if not sym.isascii(): continue
        if len(sym.strip()) < 2: continue
        if any(x in sym.upper() for x in ["AERO","UNI-V","CLOB","POS","WETH","USD","LP","POOL","SWAP","WRAPPED"]): continue
        if sym.startswith("cb") or sym.startswith("CB") or sym.startswith("SY-"): continue
        filtered.append(row)
        if len(filtered) >= count: break

    if not filtered:
        await msg.edit_text(f"No memecoins found on {chain} after filtering.")
        return

    tokens = filtered
    await msg.edit_text(f"⏳ Found {len(tokens)} tokens on {chain}. Scanning...")

    results = []
    icons = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low_with_smart_money": "🟢", "clean": "⚪"}
    already = set(KB.get("metadata", {}).get("tokens_investigated", []))

    for i, token in enumerate(tokens):
        sym = token["asset_symbol"]
        aid = token["asset_id"]
        ca = "0x" + aid.split(":")[-1] if ":" in aid else aid
        new_flag = " 🆕" if sym not in already else ""

        try:
            if i % 5 == 0:
                await msg.edit_text(f"⏳ Scanning {i+1}/{len(tokens)}: {sym}...")
            result = scan_token(ca, chain)
            risk = result.get("risk_score", "?")
            icon = icons.get(risk, "⚫")
            hits = len(result.get("dev_signals", [])) + len(result.get("insider_signals", [])) + len(result.get("smart_signals", []))
            whales = len(result.get("whale_alerts", []))
            line = f"  {icon} {sym} — {risk.upper()}"
            if hits: line += f" · {hits} hits"
            if whales: line += f" · {whales} 🐋"
            line += new_flag
            results.append(line)
        except Exception as e:
            results.append(f"  ⚫ {sym} — ERROR")

    # Summary
    total = sum(len(KB.get(s, {})) for s in ["deployers", "dev_adjacent", "insider_wallets", "smart_wallets"])
    tracked = len(KB.get("holder_tracker", {}))
    tokens_count = len(KB.get("metadata", {}).get("tokens_investigated", []))
    new_smart = [v for v in KB.get("smart_wallets", {}).values() if "Auto" in v.get("notes", "")]

    lines = [f"📦 Top Scan Complete: {len(tokens)} tokens on {chain}", ""]
    lines += results
    lines += ["", f"📋 KB: {total} tagged · {tracked} holders tracked · {tokens_count} tokens"]
    if new_smart:
        lines.append(f"🧠 {len(new_smart)} auto-discovered smart wallets")
    lines.append("📝 KB updated and pushed to GitHub")

    text = "\n".join(lines)
    if len(text) > 4096: text = text[:4090] + "\n..."
    await msg.edit_text(text)


async def scores_command(update, context):
    """Show wallet confidence scores."""
    scores = KB.get("wallet_scores", {})
    sw = KB.get("smart_wallets", {})
    tracker = KB.get("holder_tracker", {})

    if not scores:
        await update.message.reply_text("No confidence scores yet. Need more scans for the bot to calculate win rates.")
        return

    # Merge scores with labels
    scored = []
    for addr, s in scores.items():
        total = s.get("total", 0)
        if total < 2: continue
        win_rate = s.get("win_rate", 0)
        conf = round(win_rate * 10, 1)
        label = ""
        if addr in sw:
            label = sw[addr].get("label", addr[:16])
        elif addr in tracker:
            name = tracker[addr].get("name", "")
            label = name if name else addr[:16] + "..."
        else:
            label = addr[:16] + "..."
        scored.append((conf, label, s["wins"], s["losses"], total))

    scored.sort(reverse=True)

    lines = ["⭐ Wallet Confidence Scores", "", "  Score | W/L  | Wallet"]
    lines.append("  " + "-" * 50)

    for conf, label, wins, losses, total in scored[:20]:
        bar = "🟩" * int(conf) + "⬜" * (10 - int(conf))
        lines.append(f"  {conf:>4}/10 | {wins}W/{losses}L | {label}")

    if not scored:
        lines.append("  Need 2+ tokens per wallet to score.")

    total_scored = len(scored)
    high_conf = sum(1 for c, *_ in scored if c >= 7)
    lines += ["", f"  {total_scored} wallets scored · {high_conf} high confidence (7+)"]

    await update.message.reply_text("\n".join(lines))


async def monitor_command(update, context):
    """Start/stop monitoring smart wallets for new buys."""
    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage:\n"
            "/monitor on <chain> [minutes] — start monitoring\n"
            "/monitor off — stop monitoring\n"
            "/monitor status — check if running\n\n"
            "Example: /monitor on base 10\n"
            "Checks top smart wallets every 10 minutes.\n"
            "Alerts when they buy new tokens.")
        return

    action = args[0].lower()

    if action == "off":
        jobs = context.job_queue.get_jobs_by_name("monitor")
        for job in jobs: job.schedule_removal()
        await update.message.reply_text("🔴 Monitor stopped.")
        return

    if action == "status":
        jobs = context.job_queue.get_jobs_by_name("monitor")
        if jobs:
            await update.message.reply_text(f"🟢 Monitor running. {len(jobs)} job(s) active.")
        else:
            await update.message.reply_text("🔴 Monitor not running. Use /monitor on base")
        return

    if action == "on":
        chain_input = args[1] if len(args) > 1 else "base"
        chain = CHAINS.get(chain_input.lower(), chain_input.lower())
        interval = int(args[2]) if len(args) > 2 and args[2].isdigit() else 10
        interval = max(5, min(interval, 60))

        # Remove existing jobs
        jobs = context.job_queue.get_jobs_by_name("monitor")
        for job in jobs: job.schedule_removal()

        # Store chat_id and chain for the job
        context.job_queue.run_repeating(
            monitor_job,
            interval=interval * 60,
            first=10,
            data={"chat_id": update.effective_chat.id, "chain": chain},
            name="monitor",
        )
        sw_count = len(KB.get("smart_wallets", {}))
        await update.message.reply_text(
            f"🟢 Monitor started\n"
            f"  Chain: {chain}\n"
            f"  Interval: every {interval} min\n"
            f"  Tracking: {sw_count} smart wallets\n\n"
            f"You will get alerts when tracked wallets buy new tokens.")


async def monitor_job(context):
    """Background job: check smart wallets for new token buys."""
    data = context.job.data
    chat_id = data["chat_id"]
    chain = data["chain"]

    sw = KB.get("smart_wallets", {})
    if not sw: return

    # Get top 10 smart wallets by confidence
    scored = []
    for addr, wdata in sw.items():
        conf = get_confidence(addr)
        if conf >= 3 or len(wdata.get("cross_token", [])) >= 3:
            scored.append((addr, wdata, conf))
    scored.sort(key=lambda x: x[2], reverse=True)
    top_wallets = scored[:10]

    if not top_wallets: return

    alerts = []
    known_tokens = set(KB.get("metadata", {}).get("tokens_investigated", []))

    for addr, wdata, conf in top_wallets:
        try:
            r = ds_query(f"""
                SELECT DISTINCT asset_symbol, asset_id
                FROM {chain}.transfers_clustered
                WHERE receiver_address = '{addr}'
                  AND transaction_timestamp >= '{time.strftime("%Y-%m-%d", time.gmtime(time.time() - 3600))}'
                  AND asset_id NOT LIKE '%native%'
                  AND amount_asset > 0
                LIMIT 5
            """)
            for row in r.get("results", []):
                sym = row["asset_symbol"]
                if sym and sym not in known_tokens and sym.isascii() and len(sym) <= 12:
                    conf_str = f" ({conf}/10)" if conf > 0 else ""
                    label = wdata.get("label", addr[:16])
                    ca = "0x" + row["asset_id"].split(":")[-1]
                    alerts.append(f"🚨 {label}{conf_str} bought {sym}\n   CA: {ca}")
        except:
            pass

    if alerts:
        msg = "🔔 SMART WALLET ALERT\n\n" + "\n\n".join(alerts[:5])
        await context.bot.send_message(chat_id=chat_id, text=msg)


async def exits_command(update, context):
    """Check if deployers/bundlers of a token are selling (exit signals)."""
    args = context.args
    if len(args) < 1:
        await update.message.reply_text("Usage: /exits <contract> <chain>\n\nChecks if deployer and early bundler wallets are selling.")
        return

    contract = args[0].lower()
    chain_input = args[1] if len(args) > 1 else "base"
    chain = CHAINS.get(chain_input.lower(), chain_input.lower())
    ca_frag = contract[2:] if contract.startswith("0x") else contract

    msg = await update.message.reply_text(f"⏳ Checking exit signals...")

    try:
        # Find the token
        r = ds_query(f"SELECT DISTINCT asset_symbol, asset_id FROM {chain}.transfers_clustered WHERE LOWER(asset_id) LIKE '%{ca_frag}%' LIMIT 1")
        if not r.get("results"):
            await msg.edit_text("Token not found.")
            return
        asset_id = r["results"][0]["asset_id"]
        symbol = r["results"][0]["asset_symbol"]

        # Find deployer
        r2 = ds_query(f"SELECT receiver_address, SUM(amount_asset) as minted FROM {chain}.transfers_clustered WHERE asset_id = '{asset_id}' AND sender_address = '0x0000000000000000000000000000000000000000' GROUP BY receiver_address ORDER BY minted DESC LIMIT 1")
        if not r2.get("results"):
            await msg.edit_text("No deployer found.")
            return
        deployer = r2["results"][0]["receiver_address"]
        total_supply = r2["results"][0]["minted"] or 1

        # Check deployer sells (recent 7 days)
        r3 = ds_query(f"""
            SELECT sender_address, SUM(amount_asset) as sold, COUNT(*) as txs,
                   MAX(transaction_timestamp) as last_sell
            FROM {chain}.transfers_clustered
            WHERE asset_id = '{asset_id}'
              AND transaction_timestamp >= '{time.strftime("%Y-%m-%d", time.gmtime(time.time() - 604800))}'
              AND sender_address != '0x0000000000000000000000000000000000000000'
            GROUP BY sender_address
            ORDER BY sold DESC
            LIMIT 15
        """)

        lines = [f"📤 Exit Signals: {symbol}", ""]

        # Check if deployer sold
        deployer_sold = False
        for row in r3.get("results", []):
            if row["sender_address"] == deployer:
                sold_pct = (row["sold"] or 0) / total_supply * 100
                lines.append(f"🚨 DEPLOYER SOLD {sold_pct:.1f}% in last 7 days")
                lines.append(f"   Last sell: {str(row['last_sell'])[:19]}")
                deployer_sold = True
                break

        if not deployer_sold:
            lines.append("✅ Deployer has NOT sold in last 7 days")

        # Top sellers
        lines += ["", "📤 TOP SELLERS (7 days)"]
        kbl = get_kb_lookup()
        for row in r3.get("results", [])[:8]:
            addr = row["sender_address"]
            sold = row["sold"] or 0
            pct = sold / total_supply * 100
            addr_short = addr[:12] + "..." + addr[-4:]

            label = addr_short
            if addr in kbl:
                k = kbl[addr]
                if "DEV" in k["tag"]: label = f"🚨 {k['label']}"
                elif k["tag"] == "INSIDER": label = f"🚩 {k['label']}"
                elif k["tag"] == "SMART_MONEY": label = f"🧠 {k['label']}"
            elif addr == deployer:
                label = f"🚨 DEPLOYER"

            # Check if this is a bundler
            bt = KB.get("bundle_tracker", {}).get(addr)
            if bt: label = f"🎯 Bundler ({len(bt.get('tokens', []))} tokens)"

            lines.append(f"  {pct:.1f}% — {label} · {row['txs']} sells · last: {str(row['last_sell'])[-8:]}")

        lines += ["", f"Supply: {total_supply:,.0f} {symbol}"]

        text = "\n".join(lines)
        if len(text) > 4096: text = text[:4090] + "\n..."
        await msg.edit_text(text)

    except Exception as e:
        await msg.edit_text(f"Error: {str(e)[:200]}")


async def start_command(update, context):
    await update.message.reply_text(
        "🔬 Memecoin Scanner v6\n\n"
        "SCAN\n"
        "/scan <contract> <chain> — full scan\n"
        "/batch <chain> <ca1> <ca2> ... — batch scan\n"
        "/topscan <chain> [count] — auto-discover\n\n"
        "SIGNALS\n"
        "/exits <contract> <chain> — who is selling?\n"
        "/monitor on <chain> [min] — smart wallet alerts\n"
        "/monitor off — stop alerts\n\n"
        "INTEL\n"
        "/kb — knowledge base\n"
        "/scores — confidence rankings\n"
        "/history — scan history\n"
        "/feed — smart money activity\n"
        "/reload — refresh KB\n\n"
        "Auto-learns: deployers, bundlers, smart wallets,\n"
        "insiders, confidence scores, exit signals.\n"
        "DexScreener + GoPlus on every scan.\n\n"
        "Chains: eth, base, bsc, arb, poly, op, avax")


def main():
    global KB; KB = load_kb()
    total = sum(len(KB.get(s, {})) for s in ["deployers", "dev_adjacent", "insider_wallets", "smart_wallets"])
    logger.info(f"KB: {total} wallets"); logger.info("Bot v6 started.")
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    for cmd, fn in [("start", start_command), ("scan", scan_command), ("kb", kb_command),
                    ("history", history_command), ("feed", feed_command), ("reload", reload_command),
                    ("batch", batch_command), ("topscan", topscan_command), ("scores", scores_command),
                    ("monitor", monitor_command), ("exits", exits_command)]:
        app.add_handler(CommandHandler(cmd, fn))
    app.run_polling()

if __name__ == "__main__":
    main()

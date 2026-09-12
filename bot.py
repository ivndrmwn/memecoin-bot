# -*- coding: utf-8 -*-
"""
Memecoin Intelligence Telegram Bot v3
Self-learning + risk explanation + concentration score + lifecycle + history + feed + whale alerts
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

KB = {}


# ══════════════════════════════════════════════════════════════
# KB MANAGEMENT
# ══════════════════════════════════════════════════════════════

def load_kb():
    if KB_GITHUB_URL:
        try:
            r = req.get(KB_GITHUB_URL, timeout=10)
            if r.status_code == 200:
                return r.json()
        except:
            pass
    kb_env = os.environ.get("KB_DATA", "")
    if kb_env:
        try:
            return json.loads(kb_env)
        except:
            pass
    return {"deployers": {}, "dev_adjacent": {}, "insider_wallets": {}, "smart_wallets": {},
            "holder_tracker": {}, "scan_history": [], "metadata": {"investigations_completed": 0, "tokens_investigated": []}}


def save_kb():
    if not GITHUB_TOKEN:
        return False
    try:
        headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
        kb_json = json.dumps(KB, separators=(",", ":"), ensure_ascii=True)
        content_b64 = base64.b64encode(kb_json.encode()).decode()
        r = req.get(f"https://api.github.com/repos/{GITHUB_REPO}/contents/kb_data.txt", headers=headers, timeout=10)
        payload = {"message": "KB auto-update", "content": content_b64}
        if r.status_code == 200:
            payload["sha"] = r.json().get("sha")
        r2 = req.put(f"https://api.github.com/repos/{GITHUB_REPO}/contents/kb_data.txt", json=payload, headers=headers, timeout=15)
        return r2.status_code in (200, 201)
    except:
        return False


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

    # Learn deployers
    KB.setdefault("deployers", {})
    for d in report.get("_deployers_raw", []):
        addr = d["address"]
        if addr not in KB["deployers"]:
            KB["deployers"][addr] = {
                "tag": "DEV", "risk": "unknown", "label": f"{symbol} deployer ({chain})",
                "chain": chain, "first_seen": d.get("first_mint", "?"),
                "tokens_deployed": [symbol], "notes": "Auto-discovered.",
            }
            changed = True
        else:
            if symbol not in KB["deployers"][addr].get("tokens_deployed", []):
                KB["deployers"][addr].setdefault("tokens_deployed", []).append(symbol)
                count = len(KB["deployers"][addr]["tokens_deployed"])
                if count >= 3:
                    KB["deployers"][addr]["risk"] = "medium"
                    KB["deployers"][addr]["pattern"] = "serial_factory"
                    KB["deployers"][addr]["label"] = f"Serial deployer ({count} tokens)"
                changed = True

    # Check serial deployer
    for d in report.get("_deployers_raw", []):
        try:
            r = ds_query(f"SELECT DISTINCT asset_symbol FROM {chain}.transfers_clustered WHERE receiver_address = '{d['address']}' AND sender_address = '0x0000000000000000000000000000000000000000' LIMIT 10")
            for row in r.get("results", []):
                t = row["asset_symbol"]
                if t != symbol and d["address"] in KB["deployers"]:
                    existing = KB["deployers"][d["address"]].setdefault("tokens_deployed", [])
                    if t not in existing:
                        existing.append(t)
                        changed = True
                    if len(existing) >= 3:
                        KB["deployers"][d["address"]]["risk"] = "medium"
                        KB["deployers"][d["address"]]["pattern"] = "serial_factory"
                        KB["deployers"][d["address"]]["label"] = f"Serial deployer ({len(existing)} tokens)"
        except:
            pass

    # Track holders for smart wallet detection
    KB.setdefault("holder_tracker", {})
    whale_alerts = []
    for h in report.get("holders", []):
        addr = h["address"]
        if addr in KB.get("deployers", {}) or addr in KB.get("dev_adjacent", {}) or h.get("is_pool"):
            continue
        tracker = KB["holder_tracker"].setdefault(addr, {"tokens": [], "name": "", "category": ""})
        if symbol not in tracker["tokens"]:
            tracker["tokens"].append(symbol)
            if h.get("name"): tracker["name"] = h["name"]
            if h.get("category"): tracker["category"] = h["category"]
            changed = True

        # Auto-promote to smart money at 3+ tokens
        tc = len(tracker["tokens"])
        if tc >= 3 and addr not in KB.get("smart_wallets", {}):
            KB.setdefault("smart_wallets", {})
            name = tracker.get("name", "")
            cat = tracker.get("category", "")
            if "exchange" in cat.lower():
                label = f"{name} [exchange] (in {tc} tokens)"
            elif name:
                label = f"{name} (in {tc} tokens)"
            else:
                label = f"Smart wallet (in {tc} tokens)"
            KB["smart_wallets"][addr] = {
                "tag": "SMART_MONEY", "category": "auto_detected",
                "label": label, "cross_token": tracker["tokens"],
                "notes": f"Auto-promoted: {tc} tokens.",
            }
            changed = True
            whale_alerts.append(f"🐋 NEW smart wallet detected: {label}")
        elif tc >= 3 and addr in KB.get("smart_wallets", {}):
            KB["smart_wallets"][addr]["cross_token"] = tracker["tokens"]

        # Whale alert: known smart wallet entering a new token
        if addr in KB.get("smart_wallets", {}) and len(tracker["tokens"]) > 1 and tracker["tokens"][-1] == symbol:
            sw_label = KB["smart_wallets"][addr].get("label", addr[:16])
            whale_alerts.append(f"🐋 {sw_label} is in {symbol}")

    report["whale_alerts"] = whale_alerts

    # Learn free token recipients
    dist = report.get("distribution", {})
    if dist.get("free", 0) > 3 and dist.get("pool", 0) < 10:
        KB.setdefault("dev_adjacent", {})
        for d in report.get("_distribution_raw", []):
            addr = d.get("address", "")
            if not addr or addr in KB.get("deployers", {}):
                continue
            rcat = d.get("category", "")
            rname = d.get("name", "")
            if any(x in rcat.lower() for x in ["exchange"]) or any(x in rname.lower() for x in ["burn", "uniswap", "pancakeswap"]):
                continue
            pct = d.get("pct", 0)
            if pct > 1 and addr not in KB["dev_adjacent"]:
                KB["dev_adjacent"][addr] = {
                    "tag": "DEV_HOLDER", "label": f"{symbol} free recipient ({pct:.0f}%)",
                    "linked_deployer": report.get("_deployers_raw", [{}])[0].get("address", "?"),
                }
                changed = True

    # Save scan to history
    KB.setdefault("scan_history", [])
    KB["scan_history"].append({
        "symbol": symbol, "chain": chain, "risk": report.get("risk_score", "?"),
        "time": time.strftime("%Y-%m-%d %H:%M"), "kb_hits": len(report.get("dev_signals", [])) + len(report.get("insider_signals", [])) + len(report.get("smart_signals", [])),
    })
    if len(KB["scan_history"]) > 50:
        KB["scan_history"] = KB["scan_history"][-50:]

    # Track token
    meta = KB.setdefault("metadata", {})
    investigated = meta.setdefault("tokens_investigated", [])
    if symbol not in investigated:
        investigated.append(symbol)
        meta["investigations_completed"] = len(investigated)
        changed = True

    if changed:
        save_kb()
    return changed


# ══════════════════════════════════════════════════════════════
# ANALYSIS HELPERS
# ══════════════════════════════════════════════════════════════

def calc_concentration(holders, total_supply):
    """Top 5 holder concentration score."""
    if not holders or not total_supply:
        return 0, ""
    real_holders = [h for h in holders if not h.get("is_pool")][:5]
    top5_pct = sum(h.get("pct", 0) for h in real_holders)
    if top5_pct > 80:
        grade = "🔴 Very concentrated"
    elif top5_pct > 50:
        grade = "🟠 Concentrated"
    elif top5_pct > 25:
        grade = "🟡 Moderate"
    else:
        grade = "🟢 Well distributed"
    return top5_pct, grade


def calc_lifecycle(stats):
    """Token lifecycle stage."""
    if not stats:
        return "⚫ Unknown"
    txs = stats.get("transfers", 0)
    first = stats.get("first", "")
    last = stats.get("last", "")
    vol = stats.get("volume_usd", 0)

    # Age in days (rough)
    try:
        from datetime import datetime
        f = datetime.strptime(first[:10], "%Y-%m-%d")
        l = datetime.strptime(last[:10], "%Y-%m-%d")
        age = (l - f).days
        today = datetime.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d")
        days_since_last = (today - l).days
    except:
        age = 0
        days_since_last = 0

    if days_since_last > 7:
        return "💀 Dead (no activity in 7+ days)"
    elif age < 7:
        return "🆕 New (<7 days old)"
    elif age < 30:
        return "📈 Early stage (< 1 month)"
    elif txs > 1000000:
        return "🏛 Established (1M+ transfers)"
    else:
        return "📊 Active"


def explain_risk(report):
    """Generate human-readable risk explanation."""
    reasons = []
    dev = report.get("dev_signals", [])
    ins = report.get("insider_signals", [])
    sm = report.get("smart_signals", [])
    dist = report.get("distribution_signals", [])
    dist_data = report.get("distribution", {})

    if ins and dev:
        reasons.append("Known insider AND deployer detected in this token")
    if dist:
        reasons.append(f"{dist_data.get('free', 0)} wallets got free tokens, only {dist_data.get('pool', 0)}% to DEX")
    if dev and not ins:
        reasons.append(f"{len(set(dev))} deployer/dev signals found in KB")
    if sm:
        reasons.append(f"{len(set(sm))} smart money wallets holding")
    if dist_data.get("burn", 0) > 40:
        reasons.append(f"{dist_data['burn']}% supply burned")
    if not reasons:
        reasons.append("No known deployers, insiders, or suspicious distribution")
    return reasons


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
        if not r.get("results"):
            return {"error": f"Token not found on {chain}"}
        asset_id = r["results"][0]["asset_id"]
        report["symbol"] = r["results"][0]["asset_symbol"]
    except Exception as e:
        return {"error": f"Search failed: {str(e)[:80]}"}

    # Stats
    try:
        r = ds_query(f"SELECT COUNT(*) as txs, MIN(transaction_timestamp) as first_tx, MAX(transaction_timestamp) as last_tx, SUM(amount_usd) as volume FROM {chain}.transfers_clustered WHERE asset_id = '{asset_id}'")
        row = r["results"][0]
        report["stats"] = {"transfers": row["txs"], "volume_usd": row["volume"] or 0, "first": str(row["first_tx"])[:10], "last": str(row["last_tx"])[:10]}
    except:
        pass

    # Deployer
    deployers = []
    total_supply = 0
    try:
        r = ds_query(f"SELECT receiver_address, SUM(amount_asset) as minted, COUNT(*) as mints, MIN(transaction_timestamp) as first_mint FROM {chain}.transfers_clustered WHERE asset_id = '{asset_id}' AND sender_address = '0x0000000000000000000000000000000000000000' GROUP BY receiver_address ORDER BY minted DESC LIMIT 3")
        for row in r["results"]:
            m = row["minted"] or 0
            total_supply += m
            d = {"address": row["receiver_address"], "minted": m, "first_mint": str(row["first_mint"])[:10]}
            if row["receiver_address"] in kbl:
                k = kbl[row["receiver_address"]]
                d["kb_tag"] = k["tag"]; d["kb_label"] = k["label"]
                report["dev_signals"].append(f"Known deployer: {k['label']} [risk: {k.get('risk','?')}]")
            deployers.append(d)
            report["_deployers_raw"].append(d)
        report["total_supply"] = total_supply
    except:
        pass

    for d in deployers:
        pct = d["minted"] / total_supply * 100 if total_supply > 0 else 0
        addr_short = d["address"][:12] + "..." + d["address"][-4:]
        tag_str = f" [{d['kb_tag']}: {d['kb_label']}]" if "kb_tag" in d else ""
        report["deployer_info"].append(f"{addr_short} minted {pct:.0f}% on {d['first_mint']}{tag_str}")

    # Distribution
    if deployers:
        try:
            d_addr = deployers[0]["address"]
            r = ds_query(f"SELECT receiver_address, receiver_name, receiver_category, SUM(amount_asset) as total_sent FROM {chain}.transfers_clustered WHERE sender_address = '{d_addr}' AND asset_id = '{asset_id}' GROUP BY receiver_address, receiver_name, receiver_category ORDER BY total_sent DESC LIMIT 10")
            supply = total_supply or 1
            burn = pool = exchange = 0; free = 0
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
            if free > 3 and pool < 10:
                report["distribution_signals"].append(f"{free} wallets received free tokens, only {pool:.0f}% to DEX")
        except:
            pass

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
    except:
        pass

    # Risk score
    has_dev = len(report["dev_signals"]) > 0; has_insider = len(report["insider_signals"]) > 0
    has_smart = len(report["smart_signals"]) > 0; has_dist = len(report["distribution_signals"]) > 0
    if has_insider and has_dev: report["risk_score"] = "critical"
    elif has_dist and has_dev: report["risk_score"] = "critical"
    elif has_dist: report["risk_score"] = "high"
    elif has_dev: report["risk_score"] = "medium"
    elif has_smart: report["risk_score"] = "low_with_smart_money"
    else: report["risk_score"] = "clean"
    report["kb_size"] = len(kbl)

    # Concentration + lifecycle + risk explanation
    report["concentration"], report["concentration_grade"] = calc_concentration(report["holders"], total_supply)
    report["lifecycle"] = calc_lifecycle(report.get("stats"))
    report["risk_reasons"] = explain_risk(report)

    # Learn
    report["kb_updated"] = learn_from_scan(report, chain)
    return report


# ══════════════════════════════════════════════════════════════
# REPORT FORMATTER
# ══════════════════════════════════════════════════════════════

def format_report(r):
    if "error" in r:
        return f"Error: {r['error']}"
    risk = r.get("risk_score", "unknown")
    icon = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low_with_smart_money": "🟢", "clean": "⚪"}.get(risk, "⚫")

    lines = [f"{icon} {r.get('symbol','?')} on {r.get('chain','?')}", f"Risk: {risk.upper()}"]

    # Risk explanation
    for reason in r.get("risk_reasons", []):
        lines.append(f"  → {reason}")

    # Lifecycle
    lines.append(f"\n{r.get('lifecycle', '')}")

    # Stats
    stats = r.get("stats", {})
    if stats:
        lines += ["", "📊 STATS", f"  Transfers: {stats.get('transfers',0):,}", f"  Volume: ${stats.get('volume_usd',0):,.0f}", f"  Active: {stats.get('first','')} → {stats.get('last','')}"]

    # Concentration
    conc = r.get("concentration", 0)
    grade = r.get("concentration_grade", "")
    if conc > 0:
        lines.append(f"\n🏦 CONCENTRATION: Top 5 hold {conc:.0f}% — {grade}")

    # Deployer
    di = r.get("deployer_info", [])
    if di:
        lines += ["", "🔨 DEPLOYER"]
        lines += [f"  {d}" for d in di]

    # Distribution
    dd = r.get("distribution_detail", [])
    if dd:
        lines += ["", "💰 DISTRIBUTION"]
        lines += [f"  {d}" for d in dd[:8]]

    # Warnings
    ds = r.get("distribution_signals", [])
    if ds:
        lines += ["", "⚠️ WARNING"]
        lines += [f"  {s}" for s in ds]

    # Whale alerts
    wa = r.get("whale_alerts", [])
    if wa:
        lines += [""]
        lines += wa

    # Dev / Insider / Smart
    dev = r.get("dev_signals", [])
    if dev:
        lines += ["", "🚨 KNOWN DEV / DEPLOYER"]
        lines += [f"  • {s}" for s in sorted(set(dev))]
    ins = r.get("insider_signals", [])
    if ins:
        lines += ["", "🚩 KNOWN INSIDER"]
        lines += [f"  • {s}" for s in sorted(set(ins))]
    sm = r.get("smart_signals", [])
    if sm:
        lines += ["", "🧠 SMART MONEY"]
        lines += [f"  • {s}" for s in sorted(set(sm))]

    # Top holders
    holders = r.get("holders", [])
    if holders:
        lines += ["", "👑 TOP HOLDERS"]
        for h in holders[:7]:
            pct = h.get("pct", 0)
            if h.get("is_pool"): label = f"🔄 Pool/Router ({h.get('addr_short','')})"
            elif h.get("kb_tag", "").startswith("DEV"): label = f"🚨 {h.get('kb_label','DEV')}"
            elif h.get("kb_tag") == "INSIDER": label = f"🚩 {h.get('kb_label','INSIDER')}"
            elif h.get("kb_tag") == "SMART_MONEY": label = f"🧠 {h.get('kb_label','SMART')}"
            elif h.get("name"): label = h["name"] + (f" [{h['category']}]" if h.get("category") else "")
            else: label = h.get("addr_short", "")
            lines.append(f"  {pct}% — {label}")

    # Footer
    total_hits = len(set(dev)) + len(set(ins)) + len(set(sm))
    lines += ["", f"📋 {r.get('kb_size',0)} tracked wallets"]
    lines.append(f"🔍 {total_hits} matches" if total_hits > 0 else "✅ Clean — no known devs, insiders, or smart money")
    if r.get("kb_updated"):
        lines.append("📝 KB updated with new findings")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════
# TELEGRAM COMMANDS
# ══════════════════════════════════════════════════════════════

async def scan_command(update, context):
    args = context.args
    if len(args) < 1:
        await update.message.reply_text("Usage: /scan <contract> <chain>\nChains: eth, base, bsc, arb, poly, op, avax, linea, zksync")
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
        await msg.edit_text(format_report(result))
    except Exception as e:
        logger.error(f"Scan failed: {e}")
        await msg.edit_text(f"Scan failed: {str(e)[:200]}")


async def kb_command(update, context):
    total = sum(len(KB.get(s, {})) for s in ["deployers", "dev_adjacent", "insider_wallets", "smart_wallets"])
    deployers = len(KB.get("deployers", {}))
    dev_adj = len(KB.get("dev_adjacent", {}))
    insiders = len(KB.get("insider_wallets", {}))
    smart = len(KB.get("smart_wallets", {}))
    tracked = len(KB.get("holder_tracker", {}))
    tokens = KB.get("metadata", {}).get("tokens_investigated", [])
    serial = sum(1 for d in KB.get("deployers", {}).values() if len(d.get("tokens_deployed", [])) >= 3)
    auto_sw = [v for v in KB.get("smart_wallets", {}).values() if "Auto" in v.get("notes", "")]

    lines = ["📋 Knowledge Base", "",
        f"  🔨 Deployers: {deployers}" + (f" ({serial} serial)" if serial else ""),
        f"  👥 Dev-adjacent: {dev_adj}", f"  🚩 Insiders: {insiders}",
        f"  🧠 Smart money: {smart}", f"  👁 Holders tracked: {tracked}",
        f"  📊 Total tagged: {total}", f"  🪙 Tokens: {len(tokens)}", ""]
    if tokens:
        lines.append(f"  Recent: {', '.join(tokens[-8:])}")
    if auto_sw:
        lines += ["", f"  🆕 Auto-discovered: {len(auto_sw)} smart wallets"]
        for sw in auto_sw[:5]:
            lines.append(f"    • {sw['label']}")
    await update.message.reply_text("\n".join(lines))


async def history_command(update, context):
    history = KB.get("scan_history", [])
    if not history:
        await update.message.reply_text("No scans yet.")
        return
    risk_icons = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low_with_smart_money": "🟢", "clean": "⚪"}
    lines = ["📜 Scan History (last 20)", ""]
    for scan in history[-20:]:
        icon = risk_icons.get(scan.get("risk", ""), "⚫")
        hits = scan.get("kb_hits", 0)
        hit_str = f" ({hits} hits)" if hits > 0 else ""
        lines.append(f"  {icon} {scan.get('symbol','?')} on {scan.get('chain','?')} — {scan.get('risk','?').upper()}{hit_str}")
        lines.append(f"      {scan.get('time','')}")
    await update.message.reply_text("\n".join(lines))


async def feed_command(update, context):
    """Show what tracked smart wallets have been buying across scans."""
    sw = KB.get("smart_wallets", {})
    tracker = KB.get("holder_tracker", {})
    if not sw:
        await update.message.reply_text("No smart wallets tracked yet. Scan more tokens.")
        return
    lines = ["🧠 Smart Money Feed", ""]
    for addr, data in sw.items():
        cross = data.get("cross_token", [])
        if not cross:
            t = tracker.get(addr, {})
            cross = t.get("tokens", [])
        if len(cross) >= 2:
            label = data.get("label", addr[:16])
            lines.append(f"  • {label}")
            lines.append(f"    Tokens: {', '.join(cross[-6:])}")
            lines.append("")
    if len(lines) <= 2:
        lines.append("  No multi-token smart wallets yet. Keep scanning.")
    await update.message.reply_text("\n".join(lines))


async def reload_command(update, context):
    global KB
    KB = load_kb()
    total = sum(len(KB.get(s, {})) for s in ["deployers", "dev_adjacent", "insider_wallets", "smart_wallets"])
    tracked = len(KB.get("holder_tracker", {}))
    tokens = len(KB.get("metadata", {}).get("tokens_investigated", []))
    await update.message.reply_text(f"🔄 KB reloaded\n  {total} tagged wallets\n  {tracked} holders tracked\n  {tokens} tokens")


async def start_command(update, context):
    await update.message.reply_text(
        "🔬 Memecoin Intelligence Scanner v3\n\n"
        "/scan <contract> <chain> — full token scan\n"
        "/kb — knowledge base stats\n"
        "/history — last 20 scans\n"
        "/feed — smart money activity\n"
        "/reload — refresh KB from GitHub\n\n"
        "Auto-learns: deployers, serial factories,\n"
        "smart wallets (3+ tokens), insiders, whale alerts.\n\n"
        "Chains: eth, base, bsc, arb, poly, op, avax"
    )


def main():
    global KB
    KB = load_kb()
    total = sum(len(KB.get(s, {})) for s in ["deployers", "dev_adjacent", "insider_wallets", "smart_wallets"])
    logger.info(f"KB: {total} wallets")
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("scan", scan_command))
    app.add_handler(CommandHandler("kb", kb_command))
    app.add_handler(CommandHandler("history", history_command))
    app.add_handler(CommandHandler("feed", feed_command))
    app.add_handler(CommandHandler("reload", reload_command))
    logger.info("Bot v3 started.")
    app.run_polling()


if __name__ == "__main__":
    main()

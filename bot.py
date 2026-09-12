# -*- coding: utf-8 -*-
"""
Memecoin Intelligence Telegram Bot
Self-learning: every scan adds new deployers to the KB and pushes to GitHub.
"""

import os
import json
import logging
import base64
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
    "eth": "ethereum", "ethereum": "ethereum",
    "base": "base",
    "bsc": "bnb_smart_chain", "bnb": "bnb_smart_chain", "bnb_smart_chain": "bnb_smart_chain",
    "arb": "arbitrum_one", "arbitrum": "arbitrum_one",
    "poly": "polygon", "polygon": "polygon",
    "op": "optimism", "optimism": "optimism",
    "avax": "avalanche_c_chain",
    "linea": "linea", "zksync": "zksync",
}

# Global mutable KB
KB = {}


def load_kb():
    """Load KB from GitHub, then env var, then empty."""
    if KB_GITHUB_URL:
        try:
            r = req.get(KB_GITHUB_URL, timeout=10)
            if r.status_code == 200:
                kb = r.json()
                total = sum(len(kb.get(s, {})) for s in ["deployers", "dev_adjacent", "insider_wallets", "smart_wallets"])
                logger.info(f"KB loaded from GitHub: {total} wallets")
                return kb
        except Exception as e:
            logger.warning(f"GitHub KB load failed: {e}")
    kb_env = os.environ.get("KB_DATA", "")
    if kb_env:
        try:
            return json.loads(kb_env)
        except:
            pass
    return {"deployers": {}, "dev_adjacent": {}, "insider_wallets": {}, "smart_wallets": {},
            "metadata": {"investigations_completed": 0, "tokens_investigated": []}, "patterns": {}}


def save_kb_to_github():
    """Push the current KB to GitHub."""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        logger.info("GitHub push skipped: no token or repo configured")
        return False
    try:
        headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
        kb_json = json.dumps(KB, separators=(",", ":"), ensure_ascii=True)
        content_b64 = base64.b64encode(kb_json.encode()).decode()

        r = req.get(f"https://api.github.com/repos/{GITHUB_REPO}/contents/kb_data.txt", headers=headers, timeout=10)
        if r.status_code == 200:
            sha = r.json().get("sha")
            payload = {"message": f"Bot KB update: {len(kb_json):,} bytes", "content": content_b64, "sha": sha}
        else:
            payload = {"message": f"Bot KB create: {len(kb_json):,} bytes", "content": content_b64}

        r2 = req.put(f"https://api.github.com/repos/{GITHUB_REPO}/contents/kb_data.txt",
                      json=payload, headers=headers, timeout=15)
        if r2.status_code in (200, 201):
            logger.info("KB pushed to GitHub")
            return True
        else:
            logger.warning(f"GitHub push failed: {r2.status_code} {r2.text[:100]}")
            return False
    except Exception as e:
        logger.warning(f"GitHub push error: {e}")
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


def learn_from_scan(report):
    """Extract new deployers from scan results and add to KB."""
    changed = False
    symbol = report.get("symbol", "?")
    chain = report.get("chain", "?")

    # Learn new deployers
    for d in report.get("_deployers_raw", []):
        addr = d["address"]
        if addr not in KB.get("deployers", {}):
            if "deployers" not in KB:
                KB["deployers"] = {}
            KB["deployers"][addr] = {
                "tag": "DEV",
                "risk": "unknown",
                "label": f"{symbol} deployer ({chain})",
                "chain": chain,
                "first_seen": d.get("first_mint", "?"),
                "tokens_deployed": [symbol],
                "notes": f"Auto-discovered by Telegram bot scan.",
            }
            changed = True
            logger.info(f"KB: Added deployer {addr[:16]}... for {symbol}")
        else:
            existing = KB["deployers"][addr]
            if symbol not in existing.get("tokens_deployed", []):
                existing.setdefault("tokens_deployed", []).append(symbol)
                changed = True

    # Track investigated tokens
    meta = KB.setdefault("metadata", {})
    investigated = meta.setdefault("tokens_investigated", [])
    if symbol not in investigated:
        investigated.append(symbol)
        meta["investigations_completed"] = len(investigated)
        changed = True

    if changed:
        save_kb_to_github()

    return changed


def scan_token(contract, chain):
    ca = contract.lower()
    ca_frag = ca[2:] if ca.startswith("0x") else ca
    kbl = get_kb_lookup()
    report = {"contract": ca, "chain": chain, "risk_score": "unknown",
              "dev_signals": [], "insider_signals": [], "smart_signals": [],
              "distribution_signals": [], "deployer_info": [], "distribution_detail": [],
              "holders": [], "_deployers_raw": []}

    try:
        r = ds_query(f"SELECT DISTINCT asset_symbol, asset_id FROM {chain}.transfers_clustered WHERE LOWER(asset_id) LIKE '%{ca_frag}%' LIMIT 1")
        if not r.get("results"):
            return {"error": f"Token not found on {chain}"}
        asset_id = r["results"][0]["asset_id"]
        report["symbol"] = r["results"][0]["asset_symbol"]
    except Exception as e:
        return {"error": f"Search failed: {str(e)[:80]}"}

    try:
        r = ds_query(f"SELECT COUNT(*) as txs, MIN(transaction_timestamp) as first_tx, MAX(transaction_timestamp) as last_tx, SUM(amount_usd) as volume FROM {chain}.transfers_clustered WHERE asset_id = '{asset_id}'")
        row = r["results"][0]
        report["stats"] = {"transfers": row["txs"], "volume_usd": row["volume"] or 0, "first": str(row["first_tx"])[:10], "last": str(row["last_tx"])[:10]}
    except:
        pass

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
                d["kb_tag"] = k["tag"]
                d["kb_label"] = k["label"]
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

    if deployers:
        try:
            d_addr = deployers[0]["address"]
            r = ds_query(f"SELECT receiver_address, receiver_name, receiver_category, SUM(amount_asset) as total_sent FROM {chain}.transfers_clustered WHERE sender_address = '{d_addr}' AND asset_id = '{asset_id}' GROUP BY receiver_address, receiver_name, receiver_category ORDER BY total_sent DESC LIMIT 10")
            supply = total_supply or 1
            burn = pool = exchange = 0
            free = 0
            for row in r["results"]:
                total = row["total_sent"] or 0
                pct = total / supply * 100
                rname = row["receiver_name"] or ""
                rcat = row["receiver_category"] or ""
                addr_short = row["receiver_address"][:12] + "..." + row["receiver_address"][-4:]
                if "burn" in rname.lower() or row["receiver_address"].startswith("0x00000000000000000000000000000000000"):
                    burn += pct
                    report["distribution_detail"].append(f"Burned: {pct:.1f}%")
                elif "exchange" in rcat.lower():
                    exchange += pct
                    report["distribution_detail"].append(f"{rname or addr_short} [exchange]: {pct:.1f}%")
                elif any(x in rname.lower() for x in ["uniswap", "pancakeswap", "sushiswap", "aerodrome"]):
                    pool += pct
                    report["distribution_detail"].append(f"{rname or addr_short} [DEX]: {pct:.1f}%")
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

    try:
        r = ds_query(f"""SELECT address, balance, name, category FROM (
            SELECT address, SUM(received) - SUM(sent) as balance FROM (
                SELECT receiver_address as address, SUM(amount_asset) as received, 0 as sent FROM {chain}.transfers_clustered WHERE asset_id = '{asset_id}' AND receiver_address != '0x0000000000000000000000000000000000000000' GROUP BY receiver_address
                UNION ALL
                SELECT sender_address as address, 0 as received, SUM(amount_asset) as sent FROM {chain}.transfers_clustered WHERE asset_id = '{asset_id}' AND sender_address != '0x0000000000000000000000000000000000000000' GROUP BY sender_address
            ) t GROUP BY address) t2
            LEFT JOIN (
                SELECT receiver_address as address, receiver_name as name, receiver_category as category
                FROM {chain}.transfers_clustered WHERE asset_id = '{asset_id}' AND receiver_name IS NOT NULL
                GROUP BY receiver_address, receiver_name, receiver_category
            ) n USING (address)
            WHERE balance > 0 ORDER BY balance DESC LIMIT 10""")
        supply = total_supply or 1
        for row in r["results"]:
            bal = row["balance"] or 0
            pct = bal / supply * 100
            name = row.get("name") or ""
            cat = row.get("category") or ""
            addr_short = row["address"][:12] + "..." + row["address"][-4:]
            h = {"address": row["address"], "addr_short": addr_short, "pct": round(pct, 1), "name": name, "category": cat}
            if pct > 100: h["is_pool"] = True
            if row["address"] in kbl:
                k = kbl[row["address"]]
                h["kb_tag"] = k["tag"]
                h["kb_label"] = k["label"]
                if "DEV" in k["tag"]: report["dev_signals"].append(f"{k['label']} holds {pct:.1f}%")
                elif k["tag"] == "INSIDER": report["insider_signals"].append(f"{k['label']} holds {pct:.1f}%")
                elif k["tag"] == "SMART_MONEY": report["smart_signals"].append(f"{k['label']} holds {pct:.1f}%")
            report["holders"].append(h)
    except:
        pass

    has_dev = len(report["dev_signals"]) > 0
    has_insider = len(report["insider_signals"]) > 0
    has_smart = len(report["smart_signals"]) > 0
    has_dist = len(report["distribution_signals"]) > 0
    if has_insider and has_dev: report["risk_score"] = "critical"
    elif has_dist and has_dev: report["risk_score"] = "critical"
    elif has_dist: report["risk_score"] = "high"
    elif has_dev: report["risk_score"] = "medium"
    elif has_smart: report["risk_score"] = "low_with_smart_money"
    else: report["risk_score"] = "clean"
    report["kb_size"] = len(kbl)

    # Learn from this scan
    learned = learn_from_scan(report)
    report["kb_updated"] = learned

    return report


def format_report(r):
    if "error" in r:
        return f"Error: {r['error']}"
    risk = r.get("risk_score", "unknown")
    icon = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low_with_smart_money": "🟢", "clean": "⚪"}.get(risk, "⚫")
    lines = [f"{icon} {r.get('symbol','?')} on {r.get('chain','?')}", f"Risk: {risk.upper()}"]
    stats = r.get("stats", {})
    if stats:
        lines += ["", "📊 STATS", f"  Transfers: {stats.get('transfers',0):,}", f"  Volume: ${stats.get('volume_usd',0):,.0f}", f"  Active: {stats.get('first','')} → {stats.get('last','')}"]
    di = r.get("deployer_info", [])
    if di:
        lines += ["", "🔨 DEPLOYER"]
        lines += [f"  {d}" for d in di]
    dd = r.get("distribution_detail", [])
    if dd:
        lines += ["", "💰 DISTRIBUTION"]
        lines += [f"  {d}" for d in dd[:8]]
    ds = r.get("distribution_signals", [])
    if ds:
        lines += ["", "⚠️ WARNING"]
        lines += [f"  {s}" for s in ds]
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
    holders = r.get("holders", [])
    if holders:
        lines += ["", "👑 TOP HOLDERS"]
        for h in holders[:7]:
            pct = h.get("pct", 0)
            if h.get("is_pool"): label = f"🔄 DEX Pool/Router ({h.get('addr_short','')})"
            elif h.get("kb_tag", "").startswith("DEV"): label = f"🚨 {h.get('kb_label','DEV')}"
            elif h.get("kb_tag") == "INSIDER": label = f"🚩 {h.get('kb_label','INSIDER')}"
            elif h.get("kb_tag") == "SMART_MONEY": label = f"🧠 {h.get('kb_label','SMART')}"
            elif h.get("name"): label = h["name"] + (f" [{h['category']}]" if h.get("category") else "")
            else: label = h.get("addr_short", "")
            lines.append(f"  {pct}% — {label}")
    total_hits = len(set(dev)) + len(set(ins)) + len(set(sm))
    lines += ["", f"📋 {r.get('kb_size',0)} tracked wallets"]
    lines.append(f"🔍 {total_hits} matches" if total_hits > 0 else "✅ No known devs, insiders, or smart money")
    if r.get("kb_updated"):
        lines.append("📝 KB updated with new findings")
    return "\n".join(lines)


async def scan_command(update, context):
    args = context.args
    if len(args) < 1:
        await update.message.reply_text("Usage: /scan <contract> <chain>\n\nChains: eth, base, bsc, arb, poly, op, avax, linea, zksync\n\nExample:\n/scan 0xfb5B838b6cfEEdC2873aB27866079AC55363D37E bsc")
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


async def reload_command(update, context):
    global KB
    KB = load_kb()
    total = sum(len(KB.get(s, {})) for s in ["deployers", "dev_adjacent", "insider_wallets", "smart_wallets"])
    tokens = len(KB.get("metadata", {}).get("tokens_investigated", []))
    await update.message.reply_text(f"🔄 KB reloaded\n  {total} tracked wallets\n  {tokens} tokens investigated")


async def kb_command(update, context):
    total = sum(len(KB.get(s, {})) for s in ["deployers", "dev_adjacent", "insider_wallets", "smart_wallets"])
    deployers = len(KB.get("deployers", {}))
    insiders = len(KB.get("insider_wallets", {}))
    smart = len(KB.get("smart_wallets", {}))
    tokens = KB.get("metadata", {}).get("tokens_investigated", [])
    await update.message.reply_text(
        f"📋 Knowledge Base\n\n"
        f"  Deployers: {deployers}\n"
        f"  Insiders: {insiders}\n"
        f"  Smart money: {smart}\n"
        f"  Total tracked: {total}\n"
        f"  Tokens: {len(tokens)}\n\n"
        f"  {', '.join(tokens[-10:])}"
    )


async def start_command(update, context):
    await update.message.reply_text(
        "🔬 Memecoin Intelligence Scanner\n\n"
        "/scan <contract> <chain> — scan a token\n"
        "/kb — show knowledge base stats\n"
        "/reload — refresh KB from GitHub\n\n"
        "Chains: eth, base, bsc, arb, poly, op, avax\n\n"
        "Every scan auto-learns new deployers."
    )


def main():
    global KB
    KB = load_kb()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("scan", scan_command))
    app.add_handler(CommandHandler("reload", reload_command))
    app.add_handler(CommandHandler("kb", kb_command))
    logger.info("Bot started.")
    app.run_polling()


if __name__ == "__main__":
    main()

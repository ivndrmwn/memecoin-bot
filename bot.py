"""
Memecoin Intelligence Telegram Bot
Calls Data Solutions API directly.
"""

import os
import json
import logging
import requests as req
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
DS_API_KEY = os.environ["DATA_SOLUTIONS_API_KEY"]
DS_BASE_URL = "https://api.transpose.io/sql/analytical"

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

KB = json.loads(os.environ.get("KB_DATA", "{}"))


def ds_query(sql):
    resp = req.post(
        DS_BASE_URL,
        json={"sql": sql},
        headers={"Content-Type": "application/json", "X-API-Key": DS_API_KEY},
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()


def get_kb_lookup():
    """Build a lookup with tag, category, and label for each KB address."""
    addrs = {}
    for addr, data in KB.get("deployers", {}).items():
        addrs[addr] = {"tag": "DEV", "label": data.get("label", "?"), "risk": data.get("risk", "?")}
    for addr, data in KB.get("dev_adjacent", {}).items():
        addrs[addr] = {"tag": data.get("tag", "DEV_ADJACENT"), "label": data.get("label", "?")}
    for addr, data in KB.get("insider_wallets", {}).items():
        addrs[addr] = {"tag": "INSIDER", "label": data.get("label", "?"), "profit": data.get("profit_usd", 0)}
    for addr, data in KB.get("smart_wallets", {}).items():
        addrs[addr] = {"tag": "SMART_MONEY", "label": data.get("label", "?"), "category": data.get("category", "?")}
    return addrs


def scan_token(contract, chain):
    ca = contract.lower()
    ca_frag = ca[2:] if ca.startswith("0x") else ca
    kb_lookup = get_kb_lookup()

    report = {
        "contract": ca, "chain": chain, "risk_score": "unknown",
        "dev_signals": [], "insider_signals": [], "smart_signals": [], "distribution_signals": [],
        "holders": [], "distribution": {},
    }

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
        r = ds_query(f"SELECT receiver_address, SUM(amount_asset) as minted, COUNT(*) as mints FROM {chain}.transfers_clustered WHERE asset_id = '{asset_id}' AND sender_address = '0x0000000000000000000000000000000000000000' GROUP BY receiver_address ORDER BY minted DESC LIMIT 3")
        for row in r["results"]:
            m = row["minted"] or 0
            total_supply += m
            d = {"address": row["receiver_address"], "minted": m}
            if row["receiver_address"] in kb_lookup:
                kb = kb_lookup[row["receiver_address"]]
                d["kb"] = kb
                report["dev_signals"].append(f"{kb['label']} [risk: {kb.get('risk','?')}]")
            deployers.append(d)
        report["total_supply"] = total_supply
    except:
        pass

    # Distribution
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
                if "burn" in rname.lower() or row["receiver_address"].startswith("0x00000000000000000000000000000000000"):
                    burn += pct
                elif "exchange" in rcat.lower():
                    exchange += pct
                elif any(x in rname.lower() for x in ["uniswap", "pancakeswap", "sushiswap", "aerodrome"]):
                    pool += pct
                elif row["receiver_address"] != d_addr:
                    free += 1
                if row["receiver_address"] in kb_lookup:
                    kb = kb_lookup[row["receiver_address"]]
                    tag = kb["tag"]
                    if tag in ("DEV", "DEV_DISTRIBUTOR", "DEV_HOLDER", "DEV_HOP", "DEV_HELPER", "DEV_GAS_FUNDER"):
                        report["dev_signals"].append(f"Sent {pct:.1f}% to {kb['label']}")
                    elif tag == "INSIDER":
                        report["insider_signals"].append(f"Sent {pct:.1f}% to {kb['label']}")
                    elif tag == "SMART_MONEY":
                        report["smart_signals"].append(f"Sent {pct:.1f}% to {kb['label']}")
            report["distribution"] = {"burn": round(burn, 1), "pool": round(pool, 1), "exchange": round(exchange, 1), "free_recipients": free}
            if free > 3 and pool < 10:
                report["distribution_signals"].append(f"{free} wallets received free tokens, only {pool:.0f}% went to DEX pools")
        except:
            pass

    # Top holders
    try:
        r = ds_query(f"""SELECT address, balance FROM (
            SELECT address, SUM(received) - SUM(sent) as balance FROM (
                SELECT receiver_address as address, SUM(amount_asset) as received, 0 as sent FROM {chain}.transfers_clustered WHERE asset_id = '{asset_id}' AND receiver_address != '0x0000000000000000000000000000000000000000' GROUP BY receiver_address
                UNION ALL
                SELECT sender_address as address, 0 as received, SUM(amount_asset) as sent FROM {chain}.transfers_clustered WHERE asset_id = '{asset_id}' AND sender_address != '0x0000000000000000000000000000000000000000' GROUP BY sender_address
            ) t GROUP BY address) t2 WHERE balance > 0 ORDER BY balance DESC LIMIT 10""")
        supply = total_supply or 1
        for row in r["results"]:
            bal = row["balance"] or 0
            pct = bal / supply * 100
            h = {"address": row["address"], "pct": round(pct, 1)}
            if row["address"] in kb_lookup:
                kb = kb_lookup[row["address"]]
                h["tag"] = kb["tag"]
                h["label"] = kb["label"]
                if kb["tag"] in ("DEV", "DEV_DISTRIBUTOR", "DEV_HOLDER", "DEV_HOP", "DEV_HELPER", "DEV_GAS_FUNDER"):
                    report["dev_signals"].append(f"{kb['label']} holds {pct:.1f}%")
                elif kb["tag"] == "INSIDER":
                    report["insider_signals"].append(f"{kb['label']} holds {pct:.1f}%")
                elif kb["tag"] == "SMART_MONEY":
                    report["smart_signals"].append(f"{kb['label']} holds {pct:.1f}%")
            report["holders"].append(h)
    except:
        pass

    # Risk score
    has_dev = len(report["dev_signals"]) > 0
    has_insider = len(report["insider_signals"]) > 0
    has_smart = len(report["smart_signals"]) > 0
    has_dist = len(report["distribution_signals"]) > 0

    if has_insider and has_dev:
        report["risk_score"] = "critical"
    elif has_dist and has_dev:
        report["risk_score"] = "critical"
    elif has_dist:
        report["risk_score"] = "high"
    elif has_dev:
        report["risk_score"] = "medium"
    elif has_smart:
        report["risk_score"] = "low_with_smart_money"
    else:
        report["risk_score"] = "clean"

    report["kb_size"] = len(kb_lookup)
    return report


def format_report(r):
    if "error" in r:
        return f"Error: {r['error']}"

    emoji = {"critical": "\U0001f534", "high": "\U0001f7e0", "medium": "\U0001f7e1", "low_with_smart_money": "\U0001f7e2", "clean": "\u26aa"}.get(r.get("risk_score", ""), "\u26ab")

    lines = []
    lines.append(f"{emoji} {r.get('symbol','?')} on {r.get('chain','?')}")
    lines.append(f"Risk: {r.get('risk_score','?').upper()}")
    lines.append("")

    # Stats
    stats = r.get("stats", {})
    if stats:
        lines.append(f"\U0001f4ca Stats")
        lines.append(f"  Transfers: {stats.get('transfers',0):,}")
        lines.append(f"  Volume: ${stats.get('volume_usd',0):,.0f}")
        lines.append(f"  Active: {stats.get('first','')} to {stats.get('last','')}")
        lines.append("")

    # Distribution
    dist = r.get("distribution", {})
    if dist:
        lines.append(f"\U0001f4b0 Supply Distribution")
        lines.append(f"  Burned: {dist.get('burn',0)}%")
        lines.append(f"  DEX pool: {dist.get('pool',0)}%")
        lines.append(f"  Exchange: {dist.get('exchange',0)}%")
        if dist.get("free_recipients", 0) > 0:
            lines.append(f"  Free recipients: {dist['free_recipients']}")
        lines.append("")

    # Distribution warnings
    dist_signals = r.get("distribution_signals", [])
    if dist_signals:
        lines.append(f"\u26a0\ufe0f Distribution Warning")
        for s in dist_signals:
            lines.append(f"  {s}")
        lines.append("")

    # Dev signals
    dev = r.get("dev_signals", [])
    if dev:
        lines.append(f"\U0001f6a8 Dev / Deployer Matches")
        seen = set()
        for s in dev:
            if s not in seen:
                lines.append(f"  - {s}")
                seen.add(s)
        lines.append("")

    # Insider signals
    insider = r.get("insider_signals", [])
    if insider:
        lines.append(f"\U0001f6a9 Insider Matches")
        seen = set()
        for s in insider:
            if s not in seen:
                lines.append(f"  - {s}")
                seen.add(s)
        lines.append("")

    # Smart money signals
    smart = r.get("smart_signals", [])
    if smart:
        lines.append(f"\U0001f9e0 Smart Money Detected")
        seen = set()
        for s in smart:
            if s not in seen:
                lines.append(f"  - {s}")
                seen.add(s)
        lines.append("")

    # Top holders
    holders = r.get("holders", [])
    if holders:
        lines.append(f"\U0001f451 Top Holders")
        for h in holders[:7]:
            addr = h["address"][:12] + "..." + h["address"][-4:]
            pct = h.get("pct", 0)
            tag = h.get("tag", "")
            label = h.get("label", "")
            if tag in ("DEV", "DEV_DISTRIBUTOR", "DEV_HOLDER", "DEV_HOP"):
                marker = f" \U0001f6a8 DEV: {label}"
            elif tag == "INSIDER":
                marker = f" \U0001f6a9 INSIDER: {label}"
            elif tag == "SMART_MONEY":
                marker = f" \U0001f9e0 {label}"
            else:
                marker = ""
            lines.append(f"  {addr} {pct}%{marker}")
        lines.append("")

    # Footer
    total_hits = len(dev) + len(insider) + len(smart)
    lines.append(f"KB: {r.get('kb_size',0)} tracked wallets | {total_hits} matches")
    if not dev and not insider and not smart and not dist_signals:
        lines.append("No known devs, insiders, or smart money found.")

    return "\n".join(lines)


async def scan_command(update, context):
    args = context.args
    if len(args) < 1:
        await update.message.reply_text(
            "Usage: /scan <contract> <chain>\n\n"
            "Chains: eth, base, bsc, arb, poly, op, avax, linea, zksync\n\n"
            "Example:\n/scan 0xfb5B838b6cfEEdC2873aB27866079AC55363D37E bsc"
        )
        return

    contract = args[0]
    chain_input = args[1] if len(args) > 1 else "ethereum"
    chain = CHAINS.get(chain_input.lower(), chain_input.lower())

    if not contract.startswith("0x") or len(contract) != 42:
        await update.message.reply_text("Invalid address. Must be 0x + 40 hex chars.")
        return

    msg = await update.message.reply_text(f"Scanning {contract[:10]}...{contract[-6:]} on {chain}...")
    try:
        result = scan_token(contract, chain)
        await msg.edit_text(format_report(result))
    except Exception as e:
        logger.error(f"Scan failed: {e}")
        await msg.edit_text(f"Scan failed: {str(e)[:200]}")


async def start_command(update, context):
    await update.message.reply_text(
        "Memecoin Intelligence Scanner\n\n"
        "Usage: /scan <contract> <chain>\n\n"
        "Chains: eth, base, bsc, arb, poly, op, avax\n\n"
        "Example:\n/scan 0xfb5B838b6cfEEdC2873aB27866079AC55363D37E bsc"
    )


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("scan", scan_command))
    logger.info("Bot started.")
    app.run_polling()


if __name__ == "__main__":
    main()

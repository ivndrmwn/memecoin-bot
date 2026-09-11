"""
Memecoin Intelligence Telegram Bot
Calls Data Solutions API directly. No workflow middleman.
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

# Knowledge base embedded as JSON
KB = json.loads(os.environ.get("KB_DATA", "{}"))


def ds_query(sql):
    """Run a Data Solutions SQL query."""
    resp = req.post(
        DS_BASE_URL,
        json={"sql": sql},
        headers={"Content-Type": "application/json", "X-API-Key": DS_API_KEY},
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()


def get_kb_addrs():
    addrs = {}
    for section in ["deployers", "dev_adjacent", "insider_wallets", "smart_wallets"]:
        for addr, data in KB.get(section, {}).items():
            addrs[addr] = f"{data.get('tag', '?')}:{data.get('label', '?')}"
    return addrs


def scan_token(contract, chain):
    """Run the full scan."""
    ca = contract.lower()
    ca_frag = ca[2:] if ca.startswith("0x") else ca
    kb_addrs = get_kb_addrs()
    report = {"contract": ca, "chain": chain, "risk_score": "unknown", "signals": [], "kb_hits": []}

    # Find token
    try:
        r = ds_query(f"SELECT DISTINCT asset_symbol, asset_id FROM {chain}.transfers_clustered WHERE LOWER(asset_id) LIKE '%{ca_frag}%' LIMIT 1")
        if not r.get("results"):
            return {"error": f"Token not found on {chain}"}
        asset_id = r["results"][0]["asset_id"]
        symbol = r["results"][0]["asset_symbol"]
        report["symbol"] = symbol
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
    try:
        r = ds_query(f"SELECT receiver_address, SUM(amount_asset) as minted, COUNT(*) as mints FROM {chain}.transfers_clustered WHERE asset_id = '{asset_id}' AND sender_address = '0x0000000000000000000000000000000000000000' GROUP BY receiver_address ORDER BY minted DESC LIMIT 3")
        total_supply = 0
        deployers = []
        for row in r["results"]:
            m = row["minted"] or 0
            total_supply += m
            deployers.append({"address": row["receiver_address"], "minted": m})
            if row["receiver_address"] in KB.get("deployers", {}):
                entry = KB["deployers"][row["receiver_address"]]
                report["signals"].append(f"KNOWN DEPLOYER: {entry.get('label')} [{entry.get('risk')}]")
                report["kb_hits"].append({"type": "known_deployer", "label": entry.get("label")})
        report["deployer"] = deployers
        report["total_supply"] = total_supply
    except:
        deployers = []
        total_supply = 0

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
                if row["receiver_address"] in kb_addrs:
                    tag = kb_addrs[row["receiver_address"]]
                    report["signals"].append(f"DEPLOYER SENT TO KB WALLET: {tag} ({pct:.1f}%)")
                    report["kb_hits"].append({"type": "deployer_to_kb", "tag": tag})
            report["distribution"] = {"burn": round(burn, 1), "pool": round(pool, 1), "exchange": round(exchange, 1), "free_recipients": free}
            if free > 3 and pool < 10:
                report["signals"].append(f"INSIDER DISTRIBUTION: {free} free recipients")
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
        holders = []
        for row in r["results"]:
            bal = row["balance"] or 0
            pct = bal / supply * 100
            h = {"address": row["address"][:16] + "...", "pct": round(pct, 1)}
            if row["address"] in kb_addrs:
                tag = kb_addrs[row["address"]]
                h["kb"] = tag
                if "INSIDER" in tag or "DEV" in tag:
                    report["signals"].append(f"KB INSIDER/DEV HOLDING: {tag} ({pct:.1f}%)")
                elif "SMART" in tag:
                    report["signals"].append(f"SMART MONEY: {tag} ({pct:.1f}%)")
                report["kb_hits"].append({"type": "holder", "tag": tag, "pct": round(pct, 1)})
            holders.append(h)
        report["holders"] = holders
    except:
        pass

    # Risk score
    signals = report["signals"]
    if any("INSIDER" in s and "DEV" in s for s in signals):
        report["risk_score"] = "critical"
    elif any("KNOWN DEPLOYER" in s and "critical" in s for s in signals):
        report["risk_score"] = "critical"
    elif any("INSIDER DISTRIBUTION" in s for s in signals):
        report["risk_score"] = "high"
    elif any("KNOWN DEPLOYER" in s for s in signals):
        report["risk_score"] = "medium"
    elif any("SMART MONEY" in s for s in signals):
        report["risk_score"] = "low_with_smart_money"
    else:
        report["risk_score"] = "clean"

    report["kb_size"] = len(kb_addrs)
    return report


def format_report(r):
    if "error" in r:
        return f"Error: {r['error']}"

    emoji = {"critical": "\U0001f534", "high": "\U0001f7e0", "medium": "\U0001f7e1", "low_with_smart_money": "\U0001f7e2", "clean": "\u26aa"}.get(r.get("risk_score", ""), "\u26ab")
    lines = [f"{emoji} {r.get('symbol','?')} on {r.get('chain','?')} — Risk: {r.get('risk_score','?').upper()}", ""]

    stats = r.get("stats", {})
    if stats:
        lines.append(f"Transfers: {stats.get('transfers',0):,} | Volume: ${stats.get('volume_usd',0):,.0f}")
        lines.append(f"Active: {stats.get('first','')} to {stats.get('last','')}")

    dist = r.get("distribution", {})
    if dist:
        lines.append(f"Burn: {dist.get('burn',0)}% | Pool: {dist.get('pool',0)}% | Exchange: {dist.get('exchange',0)}%")

    signals = r.get("signals", [])
    if signals:
        lines.append(f"\nSignals ({len(signals)}):")
        for s in signals[:10]:
            lines.append(f"  - {s}")

    holders = r.get("holders", [])
    if holders:
        lines.append(f"\nTop holders:")
        for h in holders[:5]:
            kb = f" ({h['kb']})" if "kb" in h else ""
            lines.append(f"  {h['address']} {h['pct']}%{kb}")

    lines.append(f"\nKB: {r.get('kb_size',0)} tracked wallets | {len(r.get('kb_hits',[]))} matches")
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

    msg = await update.message.reply_text(f"Scanning {contract[:10]}...{contract[-6:]} on {chain}...")
    try:
        result = scan_token(contract, chain)
        await msg.edit_text(format_report(result))
    except Exception as e:
        logger.error(f"Scan failed: {e}")
        await msg.edit_text(f"Scan failed: {str(e)[:200]}")


async def start_command(update, context):
    await update.message.reply_text("Memecoin Intelligence Scanner\n\nUsage: /scan <contract> <chain>\n\nChains: eth, base, bsc, arb, poly, op, avax\n\nExample:\n/scan 0xfb5B838b6cfEEdC2873aB27866079AC55363D37E bsc")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("scan", scan_command))
    logger.info("Bot started.")
    app.run_polling()


if __name__ == "__main__":
    main()

"""
Memecoin Intelligence Telegram Bot

Connects to the Chainalysis Memecoin Scanner workflow.
Drop a contract address in the chat and get a risk report.

Setup:
  1. Create a Telegram bot via @BotFather, get the token
  2. Get your Chainalysis Workflows API key
  3. Set both in the .env file or as environment variables
  4. pip install python-telegram-bot requests python-dotenv
  5. python bot.py

Usage in Telegram:
  /scan 0xABC...DEF base
  /scan 0xABC...DEF bnb_smart_chain
  /scan 0xABC...DEF ethereum
"""

import os
import time
import logging
import requests
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()

# ── Configuration ────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAINALYSIS_API_KEY = os.environ["CHAINALYSIS_API_KEY"]
WORKFLOW_SLUG = "memecoin-scanner"
WORKFLOWS_BASE_URL = "https://api.chainalysis.com/workflows"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Supported chains ─────────────────────────────────────────
CHAINS = {
    "eth": "ethereum",
    "ethereum": "ethereum",
    "base": "base",
    "bsc": "bnb_smart_chain",
    "bnb": "bnb_smart_chain",
    "bnb_smart_chain": "bnb_smart_chain",
    "arb": "arbitrum_one",
    "arbitrum": "arbitrum_one",
    "arbitrum_one": "arbitrum_one",
    "poly": "polygon",
    "polygon": "polygon",
    "op": "optimism",
    "optimism": "optimism",
    "avax": "avalanche_c_chain",
    "avalanche": "avalanche_c_chain",
    "linea": "linea",
    "zksync": "zksync",
}


# ── Workflow API calls ───────────────────────────────────────
def invoke_workflow(contract: str, chain: str) -> dict:
    """Invoke the memecoin scanner workflow and wait for results."""
    headers = {
        "Authorization": f"Bearer {CHAINALYSIS_API_KEY}",
        "Content-Type": "application/json",
    }

    # Start execution
    resp = requests.post(
        f"{WORKFLOWS_BASE_URL}/v1/workflows/{WORKFLOW_SLUG}/executions",
        json={"input": {"contract": contract, "chain": chain}},
        headers=headers,
    )
    resp.raise_for_status()
    execution = resp.json()
    exec_id = execution["id"]

    # Poll for completion (max 2 minutes)
    for _ in range(40):
        time.sleep(3)
        resp = requests.get(
            f"{WORKFLOWS_BASE_URL}/v1/workflows/{WORKFLOW_SLUG}/executions/{exec_id}",
            headers=headers,
        )
        resp.raise_for_status()
        result = resp.json()
        if result["status"] != "RUNNING":
            return result

    return {"status": "TIMED_OUT", "output": {"error": "Scan timed out after 2 minutes"}}


def format_report(output: dict) -> str:
    """Format the workflow output into a readable Telegram message."""
    if "error" in output:
        return f"Error: {output['error']}"

    lines = []
    symbol = output.get("symbol", "?")
    chain = output.get("chain", "?")
    risk = output.get("risk_score", "unknown")

    # Risk emoji
    risk_emoji = {
        "critical": "🔴",
        "high": "🟠",
        "medium": "🟡",
        "low_with_smart_money": "🟢",
        "clean": "⚪",
    }.get(risk, "⚫")

    lines.append(f"{risk_emoji} {symbol} on {chain} — Risk: {risk.upper()}")
    lines.append("")

    # Stats
    stats = output.get("stats", {})
    if stats:
        txs = stats.get("total_transfers", 0)
        vol = stats.get("total_volume_usd", 0)
        lines.append(f"Transfers: {txs:,} | Volume: ${vol:,.0f}")

    # Distribution
    dist = output.get("distribution", {})
    if dist:
        lines.append(
            f"Burn: {dist.get('burn_pct', 0)}% | "
            f"Pool: {dist.get('pool_pct', 0)}% | "
            f"Exchange: {dist.get('exchange_pct', 0)}%"
        )
        free = dist.get("free_recipients", 0)
        if free > 0:
            lines.append(f"Free recipients: {free}")

    # Signals
    signals = output.get("signals", [])
    if signals:
        lines.append("")
        lines.append(f"Signals ({len(signals)}):")
        for s in signals:
            lines.append(f"  - {s}")

    # KB hits
    kb_hits = output.get("kb_hits", [])
    if kb_hits:
        lines.append("")
        lines.append(f"KB Matches ({len(kb_hits)}):")
        for h in kb_hits:
            hit_type = h.get("type", "?")
            label = h.get("label", h.get("tag", "?"))
            lines.append(f"  - [{hit_type}] {label}")

    # Top holders (first 5)
    holders = output.get("top_holders", [])
    if holders:
        lines.append("")
        lines.append("Top holders:")
        for h in holders[:5]:
            addr = h.get("address", "?")[:16]
            pct = h.get("pct", 0)
            kb_tag = h.get("kb_tag", "")
            tag_str = f" ({kb_tag})" if kb_tag else ""
            lines.append(f"  {addr}... {pct}%{tag_str}")

    # KB stats
    kb_stats = output.get("kb_stats", {})
    if kb_stats:
        lines.append("")
        lines.append(
            f"KB: {kb_stats.get('total_kb_addresses', 0)} tracked wallets | "
            f"{kb_stats.get('deployers_in_kb', 0)} deployers | "
            f"{kb_stats.get('smart_wallets_in_kb', 0)} smart wallets"
        )

    return "\n".join(lines)


# ── Telegram handlers ────────────────────────────────────────
async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /scan command."""
    args = context.args
    if len(args) < 1:
        await update.message.reply_text(
            "Usage: /scan <contract_address> <chain>\n\n"
            "Chains: eth, base, bsc, arb, poly, op, avax, linea, zksync\n\n"
            "Example: /scan 0xfb5B838b6cfEEdC2873aB27866079AC55363D37E bsc"
        )
        return

    contract = args[0]
    chain_input = args[1] if len(args) > 1 else "ethereum"
    chain = CHAINS.get(chain_input.lower(), chain_input.lower())

    # Validate contract format
    if not contract.startswith("0x") or len(contract) != 42:
        await update.message.reply_text("Invalid contract address. Must be 0x followed by 40 hex characters.")
        return

    # Send "scanning" message
    msg = await update.message.reply_text(f"Scanning {contract[:10]}...{contract[-6:]} on {chain}...")

    try:
        result = invoke_workflow(contract, chain)
        output = result.get("output", {})

        if result["status"] == "SUCCEEDED":
            report = format_report(output)
        else:
            report = f"Scan failed: {result.get('error', result['status'])}"

        await msg.edit_text(report)

    except Exception as e:
        logger.error(f"Scan failed: {e}")
        await msg.edit_text(f"Scan failed: {str(e)}")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    await update.message.reply_text(
        "Memecoin Intelligence Scanner\n\n"
        "Drop a token contract and I will check it against our database "
        "of known deployers, insiders, and smart money.\n\n"
        "Usage: /scan <contract_address> <chain>\n\n"
        "Chains: eth, base, bsc, arb, poly, op, avax, linea, zksync\n\n"
        "Example:\n"
        "/scan 0xfb5B838b6cfEEdC2873aB27866079AC55363D37E bsc"
    )


# ── Main ─────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("scan", scan_command))

    logger.info("Bot started. Listening for /scan commands...")
    app.run_polling()


if __name__ == "__main__":
    main()

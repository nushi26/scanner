import os
import logging
import time
import requests
import json
import asyncio
from telegram import Bot, Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from dotenv import load_dotenv
from solana.rpc.async_api import AsyncClient  # Add this import

# Load environment variables from .env file
load_dotenv()

# Create bot_log.txt, dev_wallets.txt, and large_transactions.txt if they don't exist
open('bot_log.txt', 'a').close()
open('dev_wallets.txt', 'a').close()
open('large_transactions.txt', 'a').close()

# Configure logging
logging.basicConfig(
    filename='bot_log.txt',
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# Constants
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
LARGE_TX_FILE = "large_transactions.txt"

# Add these new constants
FIXED_AMOUNTS = [3.99, 5.99, 6.99, 4.99, 9.99, 8.99, 1.99]
NEAR_99_THRESHOLDS = [0.01, 0.02, 0.03, 0.04, 0.05, 0.06]
LARGE_TX_THRESHOLD = 50
RETRY_DELAY = 10  # 2 seconds delay between processing transactions

# Ensure that required environment variables are set
if not all([SOLANA_RPC_URL, TELEGRAM_BOT_TOKEN, CHAT_ID]):
    raise ValueError("Missing required environment variables. Please check your .env file.")

# Wallet address to exchange name mapping
EXCHANGE_NAMES = {
    "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9": "Binance 2",
    "5VCwKtCXgCJ6kit5FybXjvriW3xELsFDhYrPSqtJNmcD": "OKX"
}

# Store developer wallets and their target amounts
DEV_WALLETS = {}

# Load existing dev wallets from file
def load_dev_wallets():
    global DEV_WALLETS
    try:
        with open('dev_wallets.txt', 'r') as f:
            DEV_WALLETS = json.load(f)
    except json.JSONDecodeError:
        DEV_WALLETS = {}
    logging.info(f"Loaded DEV_WALLETS: {DEV_WALLETS}")

# Save dev wallets to file
def save_dev_wallets():
    with open('dev_wallets.txt', 'w') as f:
        json.dump(DEV_WALLETS, f)
    logging.info(f"Saved DEV_WALLETS: {DEV_WALLETS}")

# Load existing dev wallets at script start
load_dev_wallets()

# Telegram bot instance
bot = Bot(TELEGRAM_BOT_TOKEN)

async def create_dev_wallet(dev_wallet, amount):
    DEV_WALLETS[dev_wallet] = float(amount)
    save_dev_wallets()
    logging.info(f"Added dev wallet {dev_wallet} with target amount {amount} SOL")

from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed

async def fetch_recent_transactions(client, address):
    try:
        response = await client.get_signatures_for_address(address, limit=10)
        return response['result']
    except Exception as e:
        logging.error(f"Error fetching recent transactions: {e}")
        raise

async def fetch_transaction_details(client, tx_signature):
    try:
        response = await client.get_confirmed_transaction(tx_signature)
        return response['result']
    except Exception as e:
        logging.error(f"Error fetching transaction details: {e}")
        raise

async def process_transaction(tx_details, exchange_address):
    if tx_details is None:
        logging.warning(f"Received None for tx_details for exchange address {exchange_address}")
        return

    try:
        transaction = tx_details['transaction']
        meta = tx_details['meta']

        if meta is None:
            logging.warning(f"Transaction metadata is None for {transaction['signatures'][0]}")
            return

        pre_balances = meta['preBalances']
        post_balances = meta['postBalances']

        amount = abs((post_balances[0] - pre_balances[0]) / 1e9)  # Convert lamports to SOL
        sender_wallet = transaction['message']['accountKeys'][0]
        recipient_wallet = transaction['message']['accountKeys'][1]
        tx_signature = transaction['signatures'][0]

        logging.info(f"Transaction {tx_signature}: Amount {amount} SOL, From {sender_wallet} to {recipient_wallet}")

        alert_sent = False

        # Check for dev wallet amounts
        if amount in DEV_WALLETS.values():
            await send_alert(amount, tx_signature, recipient_wallet, exchange_address, "Dev Wallet")
            alert_sent = True

        # Check for fixed amounts
        if not alert_sent and amount in FIXED_AMOUNTS:
            await send_alert(amount, tx_signature, recipient_wallet, exchange_address, "Fixed Amount")
            alert_sent = True

        # Check for amounts near .99
        if not alert_sent and any(abs(amount % 1 - 0.99) <= threshold for threshold in NEAR_99_THRESHOLDS):
            await send_alert(amount, tx_signature, recipient_wallet, exchange_address, "Near .99")
            alert_sent = True

        # Check for large transactions
        if not alert_sent and amount >= LARGE_TX_THRESHOLD:
            await send_alert(amount, tx_signature, recipient_wallet, exchange_address, "Large Transaction")
            save_large_transaction(amount, tx_signature, recipient_wallet, exchange_address)
            alert_sent = True

        if alert_sent:
            logging.info(f"Alert sent for transaction {tx_signature}")
        else:
            logging.info(f"No alert sent for transaction {tx_signature}")

    except Exception as e:
        logging.error(f"Error processing transaction: {e}")

async def main():
    async with AsyncClient(SOLANA_RPC_URL) as client:
        while True:
            try:
                for exchange_address in EXCHANGE_NAMES.keys():
                    recent_txs = await fetch_recent_transactions(client, exchange_address)
                    for tx in recent_txs:
                        tx_details = await fetch_transaction_details(client, tx['signature'])
                        await process_transaction(tx_details, exchange_address)
                        await asyncio.sleep(RETRY_DELAY)
            except Exception as e:
                logging.error(f"Error in main loop: {e}")
                await asyncio.sleep(60)  # Wait for 1 minute before retrying

async def send_alert(amount, tx_signature, recipient_wallet, exchange_address, alert_type):
    exchange_name = EXCHANGE_NAMES.get(exchange_address, exchange_address)
    
    # Get current time in Europe/Berlin timezone
    from datetime import datetime
    from pytz import timezone
    berlin_time = datetime.now(timezone('Europe/Berlin')).strftime('%Y-%m-%d %H:%M:%S %Z')
    
    alert_message = f"ALERT | {exchange_name} | {alert_type}\n"
    alert_message += f"{amount:.4f} SOL | {berlin_time}\n\n"
    alert_message += f"TX: https://explorer.solana.com/tx/{tx_signature}\n"
    alert_message += f"WALLET: https://explorer.solana.com/address/{recipient_wallet}"
    
    await bot.send_message(chat_id=CHAT_ID, text=alert_message)
    logging.info(f"Alert sent for transaction {tx_signature}")

def save_large_transaction(amount, tx_signature, recipient_wallet, exchange_address):
    exchange_name = EXCHANGE_NAMES.get(exchange_address, exchange_address)
    with open(LARGE_TX_FILE, 'a') as f:
        f.write(f"{amount:.4f} SOL,{exchange_name},{tx_signature},{recipient_wallet}\n")
    logging.info(f"Large transaction saved: {amount:.4f} SOL, {exchange_name}")

async def main():
    async with AsyncClient(SOLANA_RPC_URL) as client:
        while True:
            try:
                for exchange_address in EXCHANGE_NAMES.keys():
                    recent_txs = await fetch_recent_transactions(client, exchange_address)
                    for tx in recent_txs:
                        tx_details = await fetch_transaction_details(client, tx['signature'])
                        await process_transaction(tx_details, exchange_address)
                        await asyncio.sleep(RETRY_DELAY)
            except Exception as e:
                logging.error(f"Error in main loop: {e}")
                await asyncio.sleep(60)  # Wait for 1 minute before retrying

async def handle_create_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not update.message or not update.message.text:
            logging.error("Received update without message or text")
            return

        args = context.args
        if len(args) != 2:
            await update.message.reply_text("Usage: /create <dev_wallet> <amount_in_SOL>")
            return

        dev_wallet = args[0]
        amount = float(args[1])

        await create_dev_wallet(dev_wallet, amount)
        await update.message.reply_text(f"Dev wallet {dev_wallet} added with target amount {amount} SOL")
        
        print("Created new searche's for the dev's.")
        print("Added to the dev_wallets.txt")
        print("Added to the transaction's to find in the exchange's addresses.")
        print("-" * 50)
        
        # Update the displayed amounts
        print_current_amounts()

    except ValueError:
        await update.message.reply_text("Invalid amount. Please provide a valid number.")
        logging.error(f"Invalid amount provided: {args[1]}")
    except Exception as e:
        logging.exception(f"Error in handle_create_command: {str(e)}")
        await update.message.reply_text("An error occurred while processing your command.")

def print_current_amounts():
    amounts = "; ".join(f"{amount}" for amount in DEV_WALLETS.values())
    fixed_amounts = "; ".join(f"{amount}" for amount in FIXED_AMOUNTS)
    near_99_thresholds = "; ".join(f"{threshold}" for threshold in NEAR_99_THRESHOLDS)
    print(f"Dev wallet amounts: {amounts}")
    print(f"Fixed amounts: {fixed_amounts}")
    print(f"Near .99 thresholds: {near_99_thresholds}")
    print(f"Large transaction threshold: {LARGE_TX_THRESHOLD}+ SOL")
    print("-" * 58)

if __name__ == '__main__':
    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Add handler for /create command
    application.add_handler(CommandHandler("create", handle_create_command))
    
    # Start the bot
    asyncio.get_event_loop().run_until_complete(application.initialize())
    asyncio.get_event_loop().run_until_complete(application.start())
    asyncio.get_event_loop().create_task(application.updater.start_polling())
    
    print("Bot is running.")
    print("Starting scan exchange address'es for the right amount of SOL from dev_wallets.txt")
    print("Also tracking fixed amounts, amounts near .99, and large transactions.")
    
    # Display current amounts to search
    print_current_amounts()
    
    # Start the transaction scanning process
    asyncio.get_event_loop().create_task(main())
    
    try:
        asyncio.get_event_loop().run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        # Stop the bot gracefully
        asyncio.get_event_loop().run_until_complete(application.updater.stop())
        asyncio.get_event_loop().run_until_complete(application.stop())
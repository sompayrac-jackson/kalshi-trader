import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from kalshi_client import KalshiClient
import config

client = KalshiClient(api_key_id=config.KALSHI_API_KEY, private_key_path=config.PRIVATE_KEY_PATH)

# Verify auth by checking balance
balance = client.get_balance()
cents = balance.get("balance", 0)
print(f"Account balance: ${cents / 100:.2f}\n")

# Fetch open ATP and WTA match markets
print("Fetching open tennis match markets...")
markets = client.get_tennis_markets(status="open")
print(f"Found {len(markets)} open tennis markets\n")

print(f"{'Ticker':<50} {'YES bid':>9} {'YES ask':>9}  Title")
print("-" * 110)
for m in markets[:30]:
    ticker  = m.get("ticker", "")
    title   = m.get("title", "")[:55]
    bid     = m.get("yes_bid_dollars")
    ask     = m.get("yes_ask_dollars")
    bid_str = f"${float(bid):.2f}" if bid else "  -  "
    ask_str = f"${float(ask):.2f}" if ask else "  -  "
    print(f"{ticker:<50} {bid_str:>9} {ask_str:>9}  {title}")
        
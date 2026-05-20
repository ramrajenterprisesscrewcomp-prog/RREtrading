from dotenv import load_dotenv
import os

load_dotenv()

ANGEL_API_KEY     = os.getenv("ANGEL_API_KEY", "")
ANGEL_CLIENT_ID   = os.getenv("ANGEL_CLIENT_ID", "")
ANGEL_PASSWORD    = os.getenv("ANGEL_PASSWORD", "")
ANGEL_TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET", "")
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY", "")
TSR_EMAIL         = os.getenv("TSR_EMAIL", "")
TSR_PASSWORD      = os.getenv("TSR_PASSWORD", "")

NSE_BASE_URL = "https://www.nseindia.com"
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.nseindia.com/",
    "Connection": "keep-alive",
    "X-Requested-With": "XMLHttpRequest",
}

SCREENER_BASE_URL = "https://www.screener.in/company"
SCREENER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.screener.in/",
}

CACHE_TTL_STOCK    = 300    # 5 min — market data
CACHE_TTL_FNO_LIST = 28800  # 8 hours — F&O membership list
CACHE_TTL_SCREENER = 3600   # 1 hour — fundamentals
CACHE_TTL_NEWS     = 1800   # 30 min — news articles

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8291092862:AAEou4Jz8OPom5uYxiFPcufZQZo_3tHIcEc")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "-5073639718")

# OpenAI daily spend hard limit in USD (default $0.50)
OPENAI_DAILY_LIMIT_USD = float(os.getenv("OPENAI_DAILY_LIMIT_USD", "0.50"))

# Supabase
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

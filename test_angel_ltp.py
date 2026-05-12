import sys
sys.path.insert(0, r'C:\Users\Admin\Desktop\RRE TRADING BOT')
from services.angel_service import _get_smart_obj, _get_instrument_df, _parse_angel_expiry
import pandas as pd
from datetime import date

df = _get_instrument_df()
mask = (
    (df['name'].str.strip().str.upper() == 'NIFTY') &
    (df['exch_seg'].str.upper() == 'NFO') &
    (df['instrumenttype'].str.upper() == 'OPTIDX')
)
opts = df[mask].copy()
opts['expiry_dt'] = opts['expiry'].apply(_parse_angel_expiry)
future = opts[opts['expiry_dt'] >= date.today()].sort_values('expiry_dt')
# Get a CE and PE near ATM
near = future[pd.to_numeric(future['strike'],errors='coerce').between(2400000, 2480000)].head(4)
obj = _get_smart_obj()
for _, row in near.iterrows():
    token = str(row['token'])
    sym = str(row['symbol'])
    try:
        r = obj.ltpData('NFO', sym, token)
        print(f'{sym}: {r}')
    except Exception as e:
        print(f'{sym}: ERROR {e}')

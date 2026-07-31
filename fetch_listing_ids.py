import gspread
from oauth2client.service_account import ServiceAccountCredentials
import requests
import json
import os
import base64

# ================= AUTH =================
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]

creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
client = gspread.authorize(creds)

sheet = client.open("YRA_Full_Feed_Master").sheet1
data = sheet.get_all_records()

# ================= ONBUY AUTH =================
def get_headers():
    return {
        "Authorization": "Basic " + base64.b64encode(
            f"{os.getenv('ONBUY_CONSUMER_KEY')}:{os.getenv('ONBUY_SECRET_KEY')}".encode()
        ).decode(),
        "Content-Type": "application/json"
    }

# ================= FETCH LISTINGS =================
def fetch_all_listings():
    url = "https://api.onbuy.com/v2/listings"

    res = requests.get(url, headers=get_headers())

    if res.status_code != 200:
        print("ERROR:", res.text)
        return []

    data = res.json()

    return data.get("listings", [])

# ================= MAIN =================
print("Fetching listings from OnBuy...")

listings = fetch_all_listings()

print(f"Total listings fetched: {len(listings)}")

# Create SKU → listing_id map
listing_map = {}

for item in listings:
    sku = str(item.get("sku", "")).strip()
    listing_id = item.get("listing_id")

    if sku and listing_id:
        listing_map[sku] = listing_id

# ================= UPDATE SHEET =================
for i, row in enumerate(data, start=2):

    sku = str(row.get("SKU", "")).strip()

    listing_id = listing_map.get(sku)

    if listing_id:
        sheet.update(
            range_name=f"U{i}",
            values=[[listing_id]]
        )
        print(f"{i} | SKU {sku} → Listing ID {listing_id}")
    else:
        print(f"{i} | SKU {sku} → NOT FOUND")

print("DONE")

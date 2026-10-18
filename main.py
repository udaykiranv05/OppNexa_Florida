

import json
import os

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SPREADSHEET_ID = (
    "1T_cg_DBR5WG7vrQZuNqV4hP6BC_BY5sMpwx1v7j5IoU"
)

TRACKER_GID = 1466561086
MANUAL_REVIEW_GID = 53817621

def main():
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        raise RuntimeError(
            "Missing GOOGLE_SERVICE_ACCOUNT_JSON secret"
        )

    service_account_info = json.loads(raw)

    credentials = Credentials.from_service_account_info(
        service_account_info,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets"
        ],
    )

    sheets = build(
        "sheets",
        "v4",
        credentials=credentials,
        cache_discovery=False,
    )

    spreadsheet = sheets.spreadsheets().get(
        spreadsheetId=SPREADSHEET_ID,
        fields="sheets(properties(sheetId,title))",
    ).execute()

    tabs = {
        item["properties"]["sheetId"]:
        item["properties"]["title"]
        for item in spreadsheet.get("sheets", [])
    }

    for gid in [TRACKER_GID, MANUAL_REVIEW_GID]:
        if gid not in tabs:
            raise RuntimeError(
                f"Worksheet gid {gid} was not found"
            )

        title = tabs[gid]
        result = sheets.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range=f"'{title}'!1:1",
        ).execute()

        headers = result.get("values", [[]])[0]

        print(f"Connected to tab: {title}")
        print(f"Header count: {len(headers)}")

    print("Google Sheets connection test passed.")
    print("No spreadsheet cells were modified.")

if __name__ == "__main__":
    main()

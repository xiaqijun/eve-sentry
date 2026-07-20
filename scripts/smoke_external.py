"""Verify production outbound dependencies without printing credentials."""

import os

import httpx


def main() -> None:
    token_response = httpx.post(
        os.environ.get("QQ_TOKEN_URL", "https://bots.qq.com/app/getAppAccessToken"),
        json={
            "appId": os.environ["QQ_APP_ID"],
            "clientSecret": os.environ["QQ_APP_SECRET"],
        },
        timeout=15,
    )
    token_response.raise_for_status()
    if not token_response.json().get("access_token"):
        raise RuntimeError("QQ response did not include an access token")
    print("QQ_TOKEN_OK")

    esi_response = httpx.get("https://esi.evetech.net/latest/status/", timeout=20)
    esi_response.raise_for_status()
    print("ESI_OK")

    zkill_response = httpx.get(
        "https://zkillboard.com/api/stats/characterID/268946627/",
        headers={
            "User-Agent": os.environ["ZKILL_USER_AGENT"],
            "Accept-Encoding": "gzip",
        },
        timeout=30,
    )
    zkill_response.raise_for_status()
    print("ZKILL_OK")


if __name__ == "__main__":
    main()

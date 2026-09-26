from __future__ import annotations

"""Facebook Page and Instagram Business publishing through the Meta Graph API.

Configuration (environment):
  META_APP_ID, META_APP_SECRET   - from developers.facebook.com
  META_GRAPH_VERSION             - default v23.0
  SOCIAL_SECRET_KEY              - encrypts stored tokens / signs links (see utils/secret_box.py)
  LANDCHECK_API_PUBLIC_URL       - public API address (used for the OAuth redirect and image links)

Nothing here runs unless the app id and secret are set, so the rest of the product is unaffected while
the Meta app is still in review."""

import base64
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import requests

from app.services.estates.marketing_common import api_url
from app.utils.secret_box import secret_configured

SCOPES = ["pages_show_list", "pages_manage_posts", "pages_read_engagement", "instagram_basic", "instagram_content_publish", "business_management"]
TIMEOUT = 30


class MetaError(RuntimeError):
    def __init__(self, message: str, *, code: int | None = None, needs_reconnect: bool = False):
        super().__init__(message)
        self.code = code
        self.needs_reconnect = needs_reconnect


def graph_version() -> str:
    return str(os.getenv("META_GRAPH_VERSION") or "v23.0").strip()


def _graph(path: str) -> str:
    return f"https://graph.facebook.com/{graph_version()}/{path.lstrip('/')}"


def app_id() -> str:
    return str(os.getenv("META_APP_ID") or "").strip()


def app_secret() -> str:
    return str(os.getenv("META_APP_SECRET") or "").strip()


def configured() -> bool:
    return bool(app_id() and app_secret() and secret_configured())


def redirect_uri() -> str:
    return f"{api_url()}/estates/marketing/social/meta/callback"


def oauth_url(state: str) -> str:
    query = urlencode({"client_id": app_id(), "redirect_uri": redirect_uri(), "state": state, "response_type": "code", "scope": ",".join(SCOPES)})
    return f"https://www.facebook.com/{graph_version()}/dialog/oauth?{query}"


def _raise_for(response: requests.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code >= 400 or "error" in data:
        error = data.get("error") or {}
        code = error.get("code")
        message = str(error.get("error_user_msg") or error.get("message") or f"Meta returned HTTP {response.status_code}")
        # 190 = the token is invalid, expired or revoked - the company has to reconnect.
        raise MetaError(message, code=code, needs_reconnect=code == 190)
    return data


def _get(path: str, **params: Any) -> dict[str, Any]:
    return _raise_for(requests.get(_graph(path), params=params, timeout=TIMEOUT))


def _post(path: str, **data: Any) -> dict[str, Any]:
    return _raise_for(requests.post(_graph(path), data=data, timeout=TIMEOUT))


def exchange_code(code: str) -> dict[str, Any]:
    """Authorisation code -> long-lived user token and the Pages (with page tokens and linked Instagram
    Business accounts) that user manages. Page tokens obtained from a long-lived user token do not expire."""
    short = _get("oauth/access_token", client_id=app_id(), client_secret=app_secret(), redirect_uri=redirect_uri(), code=code)
    long_lived = _get("oauth/access_token", grant_type="fb_exchange_token", client_id=app_id(), client_secret=app_secret(), fb_exchange_token=short["access_token"])
    user_token = long_lived["access_token"]
    me = _get("me", fields="id,name", access_token=user_token)
    pages = []
    url = "me/accounts"
    params: dict[str, Any] = {"fields": "id,name,access_token,instagram_business_account{id,username,name}", "limit": 50, "access_token": user_token}
    for _ in range(4):  # a handful of pages is plenty
        data = _get(url, **params)
        pages.extend(data.get("data") or [])
        paging = data.get("paging") or {}
        after = (paging.get("cursors") or {}).get("after")
        if not paging.get("next") or not after:
            break
        params["after"] = after
    expires_in = int(long_lived.get("expires_in") or 0)
    return {
        "facebook_user_id": me.get("id"),
        "facebook_user_name": me.get("name"),
        "user_token_expires_at": (datetime.now(timezone.utc) + timedelta(seconds=expires_in)) if expires_in else None,
        "pages": pages,
    }


def publish_facebook_photo(page_id: str, page_token: str, image_url: str, caption: str) -> dict[str, str]:
    data = _post(f"{page_id}/photos", url=image_url, caption=caption, published="true", access_token=page_token)
    post_id = str(data.get("post_id") or data.get("id") or "")
    return {"external_id": post_id, "url": f"https://www.facebook.com/{post_id}" if post_id else ""}


def publish_instagram_image(ig_user_id: str, token: str, image_url: str, caption: str, *, story: bool = False) -> dict[str, str]:
    """Two-step publish: create a media container, wait until Instagram has fetched the image, publish it."""
    payload: dict[str, Any] = {"image_url": image_url, "access_token": token}
    if story:
        payload["media_type"] = "STORIES"
    else:
        payload["caption"] = caption
    container = _post(f"{ig_user_id}/media", **payload)["id"]
    for _ in range(12):
        status = _get(container, fields="status_code", access_token=token).get("status_code")
        if status == "FINISHED":
            break
        if status in ("ERROR", "EXPIRED"):
            raise MetaError("Instagram could not process the image. Please try again.")
        time.sleep(2)
    else:
        raise MetaError("Instagram is still processing the image. It was not published; try again in a minute.")
    published = _post(f"{ig_user_id}/media_publish", creation_id=container, access_token=token)
    media_id = str(published.get("id") or "")
    permalink = ""
    try:
        permalink = str(_get(media_id, fields="permalink", access_token=token).get("permalink") or "")
    except MetaError:
        pass
    return {"external_id": media_id, "url": permalink}


def parse_signed_request(signed_request: str) -> dict[str, Any] | None:
    """Meta's deauthorize / data-deletion callbacks send `signature.payload` signed with the app secret."""
    try:
        encoded_sig, payload = signed_request.split(".", 1)

        def unb64(value: str) -> bytes:
            return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

        expected = hmac.new(app_secret().encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest()
        if not hmac.compare_digest(unb64(encoded_sig), expected):
            return None
        return json.loads(unb64(payload).decode("utf-8"))
    except Exception:
        return None

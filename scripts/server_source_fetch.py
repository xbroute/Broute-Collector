"""Use the existing DNS-pinned redirect checks for every server-side source."""
from urllib.parse import urljoin
import time

from collector import SourceFetchError
from telegram_managed_sources import _fetch_one_hop, normalize_subscription_url, REDIRECT_STATUSES, MAX_REDIRECTS, SourceValidationError


def fetch_public_text(url, timeout, max_size, retries, user_agent):
    for attempt in range(retries + 1):
        try:
            current = normalize_subscription_url(url)
            for redirects in range(MAX_REDIRECTS + 1):
                status, headers, body = _fetch_one_hop(current, timeout=timeout, max_size=max_size, user_agent=user_agent)
                if status in REDIRECT_STATUSES:
                    if redirects >= MAX_REDIRECTS or not headers.get("location"):
                        raise SourceValidationError("invalid redirect")
                    current = normalize_subscription_url(urljoin(current, headers["location"]))
                    continue
                if not 200 <= status < 300 or len(body) > max_size:
                    raise SourceValidationError("source rejected")
                # A successful empty static source must still remove old records.
                return body.decode("utf-8", errors="ignore")
        except SourceValidationError:
            if attempt == retries:
                raise SourceFetchError("public source could not be fetched safely") from None
            time.sleep(1)
    raise SourceFetchError("public source could not be fetched safely")

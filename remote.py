import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import requests


HEADERS = {"User-Agent": "UH-Homes-Selections/1.0"}


def public_url(url):
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Only public HTTP or HTTPS product links are supported.")
    if parsed.port not in (None, 80, 443):
        raise ValueError("Unsupported URL port.")
    addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(entry[4][0]).is_global for entry in addresses):
        raise ValueError("Private network URLs are not supported.")
    return url


def fetch(url, limit=5_000_000):
    for _ in range(6):
        public_url(url)
        with requests.get(url, headers=HEADERS, timeout=(5, 15), allow_redirects=False, stream=True) as response:
            if response.is_redirect or response.is_permanent_redirect:
                url = urljoin(url, response.headers["Location"])
                continue
            response.raise_for_status()
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > limit:
                    raise ValueError("Remote file is too large.")
                chunks.append(chunk)
            return b"".join(chunks), response.url, response.headers.get("Content-Type", "")
    raise ValueError("Too many redirects.")


def fetch_html(url):
    content, final_url, content_type = fetch(url)
    if "html" not in content_type.lower():
        return "", final_url
    return content, final_url

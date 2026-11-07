"""Fetch a public web page as plain text for human review."""
import http.client
import ipaddress
import re
import socket
import ssl
import time
from html.parser import HTMLParser
from threading import BoundedSemaphore
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from fastapi import HTTPException

MAX_BYTES = 1_000_000
MAX_TEXT = 12_000
REDIRECTS = {301, 302, 303, 307, 308}
FETCH_SLOTS = BoundedSemaphore(4)
TLS_CONTEXT = ssl.create_default_context()
GLOBAL_IPV6 = ipaddress.ip_network("2000::/3")


def normalize_url(raw):
    raw = raw.strip()
    if len(raw) > 2048 or "\\" in raw or re.search(r"[\x00-\x20\x7f]", raw):
        raise HTTPException(422, "Enter a valid public website URL.")
    if "://" not in raw:
        raw = "https://" + raw
    try:
        url = urlsplit(raw)
        host = (url.hostname or "").rstrip(".").encode("idna").decode("ascii")
        port = 443 if url.scheme == "https" else 80
        if (
            url.scheme not in ("http", "https") or url.username is not None
            or not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", host)
            or url.port not in (None, port)
        ):
            raise ValueError
        path = quote(url.path or "/", safe="/%:@!$&'()*+,;=-._~")
        query = quote(url.query, safe="/?%:@!$&'()*+,;=-._~")
        canonical = urlunsplit((url.scheme, host, path, query, ""))
        if len(canonical) > 2048:
            raise ValueError
    except (ValueError, UnicodeError) as exc:
        raise HTTPException(422, "Use a public HTTP or HTTPS website URL without credentials or a custom port.") from exc
    return canonical


def public_addresses(host, port):
    records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses = tuple(dict.fromkeys(record[4][0] for record in records))
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global or ip.is_multicast or (
            ip.version == 6 and (ip not in GLOBAL_IPV6 or ip.sixtofour is not None or ip.teredo is not None)
        ):
            raise HTTPException(422, "Only public websites can be imported.")
    if not addresses:
        raise HTTPException(422, "The website address could not be resolved.")
    return sorted(addresses, key=lambda address: ":" in address)


class PageText(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template", "head", "nav", "iframe"}
    BLOCK = {"p", "div", "br", "li", "section", "article", "h1", "h2", "h3", "h4", "tr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = []
        self.body = []
        self.skipped = []
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self.in_title = True
        if tag in self.SKIP:
            self.skipped.append(tag)
        if not self.skipped and tag in self.BLOCK:
            self.body.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if self.skipped and tag == self.skipped[-1]:
            self.skipped.pop()
        if not self.skipped and tag in self.BLOCK:
            self.body.append("\n")

    def handle_data(self, data):
        if self.in_title:
            self.title.append(data)
        elif not self.skipped:
            self.body.append(data)

    def result(self, host):
        for parts in (self.title, self.body):
            parts[:] = [re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", part) for part in parts]
        lines = (" ".join(line.split()) for line in "".join(self.body).splitlines())
        content = "\n".join(dict.fromkeys(line for line in lines if line))[:MAX_TEXT].strip()
        if len(content) < 20:
            raise HTTPException(422, "This page has too little readable text. Try a Services or About page.")
        title = " ".join("".join(self.title).split())
        if len(title) < 3:
            title = host
        return title[:120], content


def fetch_page(raw):
    if not FETCH_SLOTS.acquire(blocking=False):
        raise HTTPException(429, "Website imports are busy. Try again shortly.")
    try:
        return _fetch_page(raw)
    except (OSError, http.client.HTTPException) as exc:
        raise HTTPException(422, "Could not read this website. Check the address or paste your business details.") from exc
    finally:
        FETCH_SLOTS.release()


def _fetch_page(raw):
    current = normalize_url(raw)
    deadline = time.monotonic() + 12

    def remaining():
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise TimeoutError
        return min(4, seconds)

    for _ in range(4):
        url = urlsplit(current)
        host = url.hostname
        tls = url.scheme == "https"
        port = 443 if tls else 80
        addresses = public_addresses(host, port)
        connection = http.client.HTTPConnection(host, port, timeout=remaining())

        # Connect to a validated address while retaining the original hostname for Host and TLS.
        def connect():
            for address in addresses[:4]:
                family = socket.AF_INET6 if ":" in address else socket.AF_INET
                sock = socket.socket(family, socket.SOCK_STREAM)
                try:
                    sock.settimeout(remaining())
                    sock.connect((address, port))
                except OSError:
                    sock.close()
                    continue
                connection.sock = sock
                if tls:
                    connection.sock = TLS_CONTEXT.wrap_socket(sock, server_hostname=host)
                return
            raise OSError("No public address was reachable")

        connection.connect = connect
        try:
            target = url.path + ("?" + url.query if url.query else "")
            connection.request("GET", target, headers={
                "User-Agent": "CallPilot Website Import/1.0",
                "Accept": "text/html, text/plain",
                "Accept-Encoding": "identity",
                "Connection": "close",
            })
            with connection.getresponse() as response:
                if response.status in REDIRECTS:
                    location = response.headers.get("Location")
                    if not location:
                        raise HTTPException(422, "The website returned an incomplete redirect.")
                    current = normalize_url(urljoin(current, location))
                    continue
                if response.status != 200:
                    raise HTTPException(422, "The website did not allow this page to be read.")
                media_type = response.headers.get_content_type()
                if media_type not in {"text/html", "application/xhtml+xml", "text/plain"} or (
                    response.headers.get("Content-Encoding", "identity").lower() != "identity"
                ):
                    raise HTTPException(422, "Choose a public HTML or plain-text page.")
                payload = bytearray()
                while len(payload) <= MAX_BYTES:
                    remaining()
                    chunk = response.read1(min(16_384, MAX_BYTES + 1 - len(payload)))
                    if not chunk:
                        break
                    payload.extend(chunk)
                if len(payload) > MAX_BYTES:
                    raise HTTPException(422, "This page is too large. Try a shorter page.")
                charset = response.headers.get_content_charset() or "utf-8"
                if charset not in {"utf-8", "utf8", "ascii", "iso-8859-1", "windows-1252"}:
                    charset = "utf-8"
                body = payload.decode(charset, errors="replace")
        finally:
            connection.close()

        page = PageText()
        if media_type == "text/plain":
            page.body.append(body)
        else:
            page.feed(body)
            page.close()
        title, content = page.result(host)
        return {"title": title, "content": content, "source_url": current}
    raise HTTPException(422, "The website redirected too many times. Enter the final page address.")

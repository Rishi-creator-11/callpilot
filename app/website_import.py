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
MAX_COMBINED = 11_900
EXTRA_PAGES = 4
SITE_BUDGET_SECONDS = 28
USEFUL_LINK = re.compile(r"about|service|product|pricing|price|rates|contact|faq|hours|shipping|turnaround|quote|location|menu|team|order|how-it-works", re.I)
UI_ONLY = re.compile(
    r"^(skip to content|search.*|sign in|log in|login|menu|cart|close|subscribe|request a quote|get a quote|shop .*|view all|"
    r"learn more|read more|home|next|previous|accept|decline|cookies?.*|privacy policy|terms.*)$", re.I)
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
    SKIP = {"script", "style", "noscript", "svg", "template", "head", "nav", "iframe", "footer", "aside",
            "form", "select", "option", "dialog"}
    BLOCK = {"p", "div", "br", "li", "section", "article", "h1", "h2", "h3", "h4", "tr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = []
        self.body = []
        self.skipped = []
        self.in_title = False
        self.links = []
        self._link = None

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            self._link = [href, []] if href else None
        if tag == "title":
            self.in_title = True
        if tag in self.SKIP:
            self.skipped.append(tag)
        if not self.skipped and tag in self.BLOCK:
            self.body.append("\n")

    INLINE = {"span", "a", "strong", "b", "em", "i", "small", "label", "td", "th", "button", "time", "cite"}

    def handle_endtag(self, tag):
        if tag in self.INLINE and not self.skipped:
            self.body.append(" ")
        if tag == "a" and self._link:
            self.links.append((self._link[0], " ".join("".join(self._link[1]).split())))
            self._link = None
        if tag == "title":
            self.in_title = False
        if self.skipped and tag == self.skipped[-1]:
            self.skipped.pop()
        if not self.skipped and tag in self.BLOCK:
            self.body.append("\n")

    def handle_data(self, data):
        if self._link is not None:
            self._link[1].append(data)
        if self.in_title:
            self.title.append(data)
        elif not self.skipped:
            self.body.append(data)

    def result(self, host):
        for parts in (self.title, self.body):
            parts[:] = [re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", part) for part in parts]
        lines = (" ".join(line.split()) for line in "".join(self.body).splitlines())
        # Drop menu and button leftovers: keep real sentences, headings, numbers and times.
        useful = (line for line in lines if line and not UI_ONLY.match(line)
                  and (len(line.split()) >= 3 or any(ch.isdigit() for ch in line) or len(line) >= 14))
        content = "\n".join(dict.fromkeys(useful))[:MAX_TEXT].strip()
        if len(content) < 20:
            raise HTTPException(422, "This page has too little readable text. Try a Services or About page.")
        title = " ".join("".join(self.title).split())
        if len(title) < 3:
            title = host
        return title[:120], content


def same_site_links(base_url, links, limit):
    base = urlsplit(base_url)
    host = base.hostname.removeprefix("www.")
    chosen, seen = [], {base.path.rstrip("/") or "/"}
    for href, text in links:
        try:
            target = urlsplit(urljoin(base_url, href))
        except ValueError:
            continue
        if target.scheme not in ("http", "https") or (target.hostname or "").removeprefix("www.") != host:
            continue
        path = target.path.rstrip("/") or "/"
        if path in seen or re.search(r"\.(pdf|jpe?g|png|gif|webp|zip|mp4|svg)$", path, re.I):
            continue
        if USEFUL_LINK.search(path) or USEFUL_LINK.search(text or ""):
            seen.add(path)
            chosen.append(urlunsplit((target.scheme, target.netloc, target.path, target.query, "")))
        if len(chosen) >= limit:
            break
    return chosen


def fetch_page(raw, extra_pages=EXTRA_PAGES):
    """Read the page and a few clearly useful pages on the same site (about, services, pricing, contact)."""
    if not FETCH_SLOTS.acquire(blocking=False):
        raise HTTPException(429, "Website imports are busy. Try again shortly.")
    try:
        started = time.monotonic()
        page = _fetch_page(raw, with_links=True)
        links = page.pop("links")
        parts = [page["content"]]
        for extra in same_site_links(page["source_url"], links, extra_pages):
            if time.monotonic() - started > SITE_BUDGET_SECONDS:
                break
            try:
                more = _fetch_page(extra)
            except (OSError, http.client.HTTPException, HTTPException):
                continue
            parts.append("— " + more["title"] + " —\n" + more["content"])
        if len(parts) > 1:
            lines = list(dict.fromkeys(line for part in parts for line in part.splitlines() if line.strip()))
            page["content"] = "\n".join(lines)[:MAX_COMBINED].strip()
        return page
    except (OSError, http.client.HTTPException) as exc:
        raise HTTPException(422, "Could not read this website. Check the address or paste your business details.") from exc
    finally:
        FETCH_SLOTS.release()


def _download(raw, allow_xml=False):
    """Safely download one public page. Returns (final_url, host, media_type, body_text)."""
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
                allowed = {"text/html", "application/xhtml+xml", "text/plain"} | ({"application/xml", "text/xml"} if allow_xml else set())
                if media_type not in allowed or (
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

        return current, host, media_type, body
    raise HTTPException(422, "The website redirected too many times. Enter the final page address.")


def parse_page(current, host, media_type, body):
    page = PageText()
    if media_type == "text/plain":
        page.body.append(body)
    else:
        page.feed(body)
        page.close()
    title, content = page.result(host)
    return page, {"title": title, "content": content, "source_url": current}


def _fetch_page(raw, with_links=False):
    current, host, media_type, body = _download(raw)
    page, result = parse_page(current, host, media_type, body)
    if with_links:
        result["links"] = page.links
    return result

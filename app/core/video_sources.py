import ipaddress
import os
import re
import socket
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
TRAILING_SHARE_PUNCTUATION = ")]}>,，。！？；：、"
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}
DOUYIN_HOSTS = {
    "douyin.com",
    "www.douyin.com",
    "v.douyin.com",
    "iesdouyin.com",
    "www.iesdouyin.com",
}
BILIBILI_HOSTS = {
    "bilibili.com",
    "www.bilibili.com",
    "m.bilibili.com",
    "b23.tv",
}
BLOCKED_HOSTNAMES = {"localhost", "localhost.localdomain"}
SYNTHETIC_DNS_NETWORK = ipaddress.ip_network("198.18.0.0/15")


@dataclass(frozen=True)
class VideoSource:
    kind: str
    value: str
    original: str


def parse_video_source(source: str) -> VideoSource:
    original = str(source or "").strip()
    if not original:
        raise ValueError("source is required")

    if original.startswith("gs://") or "/v1beta/files/" in original:
        return VideoSource("gemini", original, original)

    match = URL_PATTERN.search(original)
    if match:
        url = match.group(0).rstrip(TRAILING_SHARE_PUNCTUATION)
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower()
        if parsed.scheme.lower() not in {"http", "https"} or not hostname:
            raise ValueError("Video URL must use public HTTP or HTTPS")
        if parsed.username or parsed.password:
            raise ValueError("Video URL must not contain embedded credentials")
        validate_public_hostname(hostname, resolve_dns=False)
        if hostname in YOUTUBE_HOSTS:
            return VideoSource("youtube", url, original)
        if hostname in DOUYIN_HOSTS:
            return VideoSource("douyin", url, original)
        if hostname in BILIBILI_HOSTS or hostname.endswith(".bilibili.com"):
            return VideoSource("bilibili", url, original)
        return VideoSource("web", url, original)

    path = Path(original).expanduser().resolve()
    if path.is_file():
        return VideoSource("local", str(path), original)
    raise ValueError(f"Video file does not exist: {path}")


def validate_public_url(url: str, *, resolve_dns: bool = True) -> None:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        raise ValueError("Video URL must use public HTTP or HTTPS")
    if parsed.username or parsed.password:
        raise ValueError("Video URL must not contain embedded credentials")
    validate_public_hostname(hostname, resolve_dns=resolve_dns)


def validate_public_hostname(hostname: str, *, resolve_dns: bool = True) -> None:
    normalized = hostname.rstrip(".").lower()
    if (
        normalized in BLOCKED_HOSTNAMES
        or normalized.endswith(".localhost")
        or normalized.endswith(".local")
    ):
        raise ValueError("Private or local video URLs are not allowed")

    try:
        literal = ipaddress.ip_address(normalized)
    except ValueError:
        literal = None
    if literal is not None:
        if not _is_allowed_public_address(literal):
            raise ValueError("Private or local video URLs are not allowed")
        return

    if not resolve_dns:
        return
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(normalized, None, type=socket.SOCK_STREAM)
        }
    except socket.gaierror as exc:
        raise ValueError(f"Unable to resolve video host: {normalized}") from exc
    if not addresses:
        raise ValueError(f"Unable to resolve video host: {normalized}")
    if not any(
        _is_allowed_public_address(ipaddress.ip_address(address))
        for address in addresses
    ):
        raise ValueError("Video host resolves to a private or local address")


def _is_allowed_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if address.is_global:
        return True
    allow_synthetic = os.getenv("VIDEO_ALLOW_SYNTHETIC_DNS", "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    return allow_synthetic and address in SYNTHETIC_DNS_NETWORK

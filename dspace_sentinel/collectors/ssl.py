"""SSL certificate collector: days until expiry, issuer, subject and SANs (SPEC section 6).

The certificate is fetched with a real TLS handshake using SNI (``ssl.SSLSocket`` /
``getpeercert``). When the verified handshake fails (expired, self-signed, hostname
mismatch) the chain is fetched again *without* verification so the expiry date is still
known; the certificate bytes are decoded by a small X.509/DER reader because
``getpeercert()`` returns an empty dict for unverified peers.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import ipaddress
import logging
import socket
import ssl as _ssl
import time
from dataclasses import asdict, dataclass, field
from typing import Any, ClassVar

from dspace_sentinel.collectors.base import Collector, CollectorContext, CollectorResult
from dspace_sentinel.collectors.services import rung, url_parts
from dspace_sentinel.health import threshold_status

log = logging.getLogger(__name__)

CERT_TIMEOUT_S = 5.0

_OID_NAMES: dict[str, str] = {
    "2.5.4.3": "CN",
    "2.5.4.6": "C",
    "2.5.4.7": "L",
    "2.5.4.8": "ST",
    "2.5.4.10": "O",
    "2.5.4.11": "OU",
}
_OID_SAN = "2.5.29.17"
_TAG_SEQUENCE = 0x30
_TAG_SET = 0x31
_TAG_OID = 0x06
_TAG_OCTET_STRING = 0x04
_TAG_UTCTIME = 0x17
_TAG_GENERALIZEDTIME = 0x18
_TAG_BMPSTRING = 0x1E
_TAG_CTX0 = 0xA0
_TAG_CTX3 = 0xA3
_TAG_SAN_DNS = 0x82
_TAG_SAN_IP = 0x87


@dataclass
class CertInfo:
    """What :func:`fetch_certificate` learned about a server certificate."""

    host: str
    port: int
    subject: dict[str, str] = field(default_factory=dict)
    issuer: dict[str, str] = field(default_factory=dict)
    not_before: float | None = None
    not_after: float | None = None
    sans: list[str] = field(default_factory=list)
    verified: bool = False
    verify_error: str | None = None
    tls_version: str | None = None
    latency_ms: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Plain dict form."""
        return asdict(self)


# ------------------------------------------------------------------------- DER reader


def _tlv(data: bytes, pos: int) -> tuple[int, bytes, int]:
    """``(tag, value, next_pos)`` of the DER TLV at ``pos`` (single-byte tags only)."""
    if pos + 2 > len(data):
        raise ValueError("truncated DER")
    tag = data[pos]
    if tag & 0x1F == 0x1F:
        raise ValueError("multi-byte DER tags are not supported")
    length = data[pos + 1]
    pos += 2
    if length & 0x80:
        n = length & 0x7F
        if n == 0 or n > 4 or pos + n > len(data):
            raise ValueError("bad DER length")
        length = int.from_bytes(data[pos : pos + n], "big")
        pos += n
    end = pos + length
    if end > len(data):
        raise ValueError("truncated DER value")
    return tag, data[pos:end], end


def _children(value: bytes) -> list[tuple[int, bytes]]:
    out: list[tuple[int, bytes]] = []
    pos = 0
    while pos < len(value):
        tag, inner, pos = _tlv(value, pos)
        out.append((tag, inner))
    return out


def _decode_oid(value: bytes) -> str:
    if not value:
        return ""
    first = value[0]
    parts = [str(first // 40), str(first % 40)]
    acc = 0
    for byte in value[1:]:
        acc = (acc << 7) | (byte & 0x7F)
        if not byte & 0x80:
            parts.append(str(acc))
            acc = 0
    return ".".join(parts)


def _decode_string(tag: int, value: bytes) -> str:
    if tag == _TAG_BMPSTRING:
        return value.decode("utf-16-be", "replace")
    return value.decode("utf-8", "replace")


def _decode_name(value: bytes) -> dict[str, str]:
    """An X.501 ``Name`` (SEQUENCE of RDN SETs) → ``{"CN": ..., "O": ..., ...}``."""
    out: dict[str, str] = {}
    for tag, rdn in _children(value):
        if tag != _TAG_SET:
            continue
        for atag, attr in _children(rdn):
            if atag != _TAG_SEQUENCE:
                continue
            fields = _children(attr)
            if len(fields) < 2 or fields[0][0] != _TAG_OID:
                continue
            oid = _decode_oid(fields[0][1])
            key = _OID_NAMES.get(oid, oid)
            out.setdefault(key, _decode_string(fields[1][0], fields[1][1]))
    return out


def _decode_time(tag: int, value: bytes) -> float | None:
    text = value.decode("ascii", "replace").strip()
    try:
        if tag == _TAG_UTCTIME:
            # YYMMDDHHMM[SS]Z — two-digit years: 50-99 → 19xx, 00-49 → 20xx (RFC 5280)
            year = int(text[:2])
            year += 1900 if year >= 50 else 2000
            rest = text[2:].rstrip("Z")
            if len(rest) == 8:
                rest += "00"
            dt = _dt.datetime.strptime(f"{year}{rest}", "%Y%m%d%H%M%S")
        elif tag == _TAG_GENERALIZEDTIME:
            core = text.rstrip("Z").split(".")[0]
            if len(core) == 12:
                core += "00"
            dt = _dt.datetime.strptime(core, "%Y%m%d%H%M%S")
        else:
            return None
    except ValueError:
        return None
    return dt.replace(tzinfo=_dt.timezone.utc).timestamp()


def _decode_sans(extensions: bytes) -> list[str]:
    sans: list[str] = []
    seq = _children(extensions)
    if not seq or seq[0][0] != _TAG_SEQUENCE:
        return sans
    for tag, ext in _children(seq[0][1]):
        if tag != _TAG_SEQUENCE:
            continue
        fields = _children(ext)
        if not fields or fields[0][0] != _TAG_OID or _decode_oid(fields[0][1]) != _OID_SAN:
            continue
        octets = next((v for t, v in fields[1:] if t == _TAG_OCTET_STRING), None)
        if octets is None:
            continue
        names = _children(octets)
        if not names or names[0][0] != _TAG_SEQUENCE:
            continue
        for ntag, name in _children(names[0][1]):
            if ntag == _TAG_SAN_DNS:
                sans.append(name.decode("ascii", "replace"))
            elif ntag == _TAG_SAN_IP and len(name) in (4, 16):
                sans.append(str(ipaddress.ip_address(name)))
    return sans


def parse_der_certificate(der: bytes) -> dict[str, Any]:
    """Decode subject, issuer, validity and SANs from a DER-encoded X.509 certificate.

    Raises ``ValueError`` for anything that is not a certificate.
    """
    tag, cert, _ = _tlv(der, 0)
    if tag != _TAG_SEQUENCE:
        raise ValueError("not a certificate")
    top = _children(cert)
    if not top or top[0][0] != _TAG_SEQUENCE:
        raise ValueError("no TBSCertificate")
    fields = _children(top[0][1])
    idx = 1 if fields and fields[0][0] == _TAG_CTX0 else 0
    if len(fields) < idx + 6:
        raise ValueError("TBSCertificate too short")
    issuer = _decode_name(fields[idx + 2][1])
    validity = _children(fields[idx + 3][1])
    subject = _decode_name(fields[idx + 4][1])
    not_before = _decode_time(*validity[0]) if len(validity) > 0 else None
    not_after = _decode_time(*validity[1]) if len(validity) > 1 else None
    extensions = next((v for t, v in fields[idx + 5 :] if t == _TAG_CTX3), None)
    sans = _decode_sans(extensions) if extensions else []
    return {
        "subject": subject,
        "issuer": issuer,
        "not_before": not_before,
        "not_after": not_after,
        "sans": sans,
    }


# ------------------------------------------------------------------- getpeercert dict

_PEERCERT_KEYS = {
    "commonName": "CN",
    "organizationName": "O",
    "organizationalUnitName": "OU",
    "countryName": "C",
    "localityName": "L",
    "stateOrProvinceName": "ST",
}


def _name_from_peercert(rdns: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    for rdn in rdns or ():
        for key, value in rdn:
            out.setdefault(_PEERCERT_KEYS.get(key, key), str(value))
    return out


def parse_peercert(cert: dict[str, Any]) -> dict[str, Any]:
    """Normalise ``SSLSocket.getpeercert()`` output to the same shape as :func:`parse_der_certificate`."""

    def _time(key: str) -> float | None:
        raw = cert.get(key)
        if not raw:
            return None
        try:
            return float(_ssl.cert_time_to_seconds(raw))
        except ValueError:
            return None

    sans = [str(value) for kind, value in cert.get("subjectAltName", ()) if kind in ("DNS", "IP Address")]
    return {
        "subject": _name_from_peercert(cert.get("subject")),
        "issuer": _name_from_peercert(cert.get("issuer")),
        "not_before": _time("notBefore"),
        "not_after": _time("notAfter"),
        "sans": sans,
    }


def format_name(name: dict[str, str]) -> str | None:
    """``{"CN": "R11", "O": "Let's Encrypt"}`` → ``"R11 (Let's Encrypt)"``."""
    cn = name.get("CN")
    org = name.get("O")
    if cn and org and org != cn:
        return f"{cn} ({org})"
    return cn or org or (next(iter(name.values())) if name else None)


# ------------------------------------------------------------------------ the fetch


def _handshake(
    host: str, port: int, timeout: float, verify: bool
) -> tuple[bytes, dict[str, Any] | None, str | None]:
    context = _ssl.create_default_context()
    if not verify:
        context.check_hostname = False
        context.verify_mode = _ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=timeout) as sock:
        with context.wrap_socket(sock, server_hostname=host) as tls:
            der = tls.getpeercert(binary_form=True) or b""
            info = tls.getpeercert() if verify else None
            return der, info, tls.version()


def fetch_certificate(
    host: str, port: int = 443, timeout: float = CERT_TIMEOUT_S, verify: bool = True
) -> CertInfo:
    """Fetch and decode the server certificate of ``host:port`` (blocking; run it in a thread).

    A failed verification falls back to an unverified handshake and records ``verify_error``.
    Raises ``OSError`` (refused, timeout, DNS) or ``ssl.SSLError`` (no TLS on that port).
    """
    started = time.perf_counter()
    verify_error: str | None = None
    verified = False
    try:
        der, peercert, tls_version = _handshake(host, port, timeout, verify)
        verified = verify
    except _ssl.SSLCertVerificationError as exc:
        verify_error = (
            getattr(exc, "verify_message", None) or str(exc)
        ).strip() or "certificate verification failed"
        der, peercert, tls_version = _handshake(host, port, timeout, False)
    latency = round((time.perf_counter() - started) * 1000.0, 2)
    info = CertInfo(
        host=host, port=port, verified=verified, verify_error=verify_error, tls_version=tls_version
    )
    decoded: dict[str, Any] | None = None
    if peercert:
        decoded = parse_peercert(peercert)
    if der and (decoded is None or decoded.get("not_after") is None):
        try:
            decoded = parse_der_certificate(der)
        except ValueError as exc:
            log.debug("could not decode certificate of %s:%s: %s", host, port, exc)
    if decoded:
        info.subject = decoded["subject"]
        info.issuer = decoded["issuer"]
        info.not_before = decoded["not_before"]
        info.not_after = decoded["not_after"]
        info.sans = list(decoded["sans"])
    info.latency_ms = latency
    return info


def describe_fetch_error(exc: BaseException, host: str, port: int, timeout: float = CERT_TIMEOUT_S) -> str:
    """Short human reason for a failed certificate fetch."""
    where = f"{host}:{port}"
    if isinstance(exc, asyncio.TimeoutError | socket.timeout | TimeoutError):
        return f"timed out after {timeout:g} s ({where})"
    if isinstance(exc, ConnectionRefusedError):
        return f"connection refused ({where})"
    if isinstance(exc, socket.gaierror):
        return f"DNS lookup failed ({host})"
    if isinstance(exc, _ssl.SSLError):
        reason = getattr(exc, "reason", None) or str(exc)
        return f"TLS handshake failed: {str(reason).replace('_', ' ').lower()} ({where})"
    if isinstance(exc, OSError):
        reason = exc.strerror or str(exc) or exc.__class__.__name__
        return f"{reason.lower()} ({where})"
    return f"{exc.__class__.__name__}: {exc} ({where})"


def target_host(cfg: Any) -> str:
    """``ssl.host``, else the host of ``dspace.public_url``, else ``""``."""
    host = (cfg.ssl.host or "").strip()
    if host:
        return host
    public = (cfg.dspace.public_url or "").strip()
    if public:
        return url_parts(public).host
    return ""


class SslCollector(Collector):
    """``ssl`` 🔒: one rung, ``ssl.cert`` "Certificate", with days_left / issuer / subject / SANs."""

    id: ClassVar[str] = "ssl"
    interval_key: ClassVar[str] = "http_interval"
    label: ClassVar[str] = "SSL certificate"
    emoji: ClassVar[str] = "🔒"

    async def collect(self, ctx: CollectorContext) -> CollectorResult:
        """Fetch the certificate in a worker thread (5 s cap) and grade it against ``ssl_expiry_days``."""
        ts = ctx.now
        host = target_host(ctx.cfg)
        port = int(ctx.cfg.ssl.port)
        rung_id, label = "ssl.cert", "Certificate"
        if not host:
            check = rung(
                rung_id,
                label,
                "unknown",
                ts,
                message="no host configured (set ssl.host or dspace.public_url)",
            )
            return CollectorResult(checks=[check], metrics={"days_left": None, "host": None, "port": port})
        verify = bool(ctx.cfg.dspace.verify_tls)
        try:
            info = await asyncio.wait_for(
                asyncio.to_thread(fetch_certificate, host, port, CERT_TIMEOUT_S, verify),
                timeout=CERT_TIMEOUT_S + 1.0,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            check = rung(
                rung_id,
                label,
                "crit",
                ts,
                message=describe_fetch_error(exc, host, port),
                host=host,
                port=port,
            )
            return CollectorResult(checks=[check], metrics={"days_left": None, "host": host, "port": port})

        th = ctx.cfg.alerts.ssl_expiry_days
        days_left: float | None = None
        not_after_iso: str | None = None
        if info.not_after is not None:
            days_left = round((info.not_after - ts) / 86400.0, 1)
            not_after_iso = _dt.datetime.fromtimestamp(info.not_after, tz=_dt.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        status = threshold_status(days_left, th.warning, th.critical, inverted=True)
        if days_left is None:
            message = "could not read the expiry date"
        elif days_left < 0:
            message = f"expired {abs(days_left):.0f} days ago"
        else:
            message = f"expires in {days_left:.0f} days"
        if info.verify_error and verify:
            status = "crit"
            message = f"{info.verify_error} · {message}"
        elif info.verify_error:
            message = f"{message} (not verified: {info.verify_error})"
        check = rung(
            rung_id,
            label,
            status,
            ts,
            latency_ms=info.latency_ms,
            value=days_left,
            message=message,
            host=host,
            port=port,
            verified=info.verified,
            tls_version=info.tls_version,
        )
        metrics: dict[str, Any] = {
            "days_left": days_left,
            "not_after": not_after_iso,
            "issuer": format_name(info.issuer),
            "subject": format_name(info.subject),
            "sans": info.sans,
            "host": host,
            "port": port,
            "verified": info.verified,
            "verify_error": info.verify_error,
            "tls_version": info.tls_version,
        }
        return CollectorResult(checks=[check], metrics=metrics)


__all__ = [
    "CERT_TIMEOUT_S",
    "CertInfo",
    "SslCollector",
    "describe_fetch_error",
    "fetch_certificate",
    "format_name",
    "parse_der_certificate",
    "parse_peercert",
    "target_host",
]

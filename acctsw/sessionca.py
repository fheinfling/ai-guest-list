"""Per-session certificate authority for local TLS interception of a fixed host list.

A local HTTPS proxy terminates TLS for api.anthropic.com for ONE Claude Code session. That session
trusts the CA only through its own environment (NODE_EXTRA_CA_CERTS and friends); nothing
system-wide (Keychain, trust store) is touched.

The CA and leaf use EC P-256 keys and live at most two days. The CA is CA:TRUE pathlen:0 with
keyCertSign/cRLSign and a nameConstraints extension permitting only the given DNS names. Both
LibreSSL (macOS /usr/bin/openssl) and OpenSSL 3 accept that from a config file. The leaf key is
loaded into the server SSLContext and every key file is deleted at once, so no private key
exists on disk after construction.
"""
from __future__ import annotations

import atexit
import os
import re
import shutil
import ssl
import subprocess
import tempfile
from pathlib import Path

_TIMEOUT_S = 20
_DAYS = 2
_FALLBACK_ROOTS = "/etc/ssl/cert.pem"
_KEYCHAINS = ("/Library/Keychains/System.keychain", "/System/Library/Keychains/SystemRootCertificates.keychain")
_CERT_RE = re.compile(r"-----BEGIN (?P<t>TRUSTED )?CERTIFICATE-----.*?-----END (?(t)TRUSTED )CERTIFICATE-----", re.S)


class SessionCAError(RuntimeError):
    pass


def _read_text(path: str | None) -> str | None:
    if not path:
        return None
    try:
        text = Path(path).read_text()
    except (OSError, UnicodeError):
        return None
    return text if _certs(text) else None


def _join(*pems: str) -> str:
    return "".join(p if p.endswith("\n") else p + "\n" for p in pems)


def _certs(text: str | None) -> list[str]:
    """Certificate blocks (plain or OpenSSL TRUSTED) only; never keys or other PEM types."""
    return [m.group(0) for m in _CERT_RE.finditer(text or "")]


def _norm(block: str) -> str:
    return "".join(block.split())


def _keychain_roots(security: str) -> str | None:
    try:
        r = subprocess.run([security, "find-certificate", "-a", "-p", *_KEYCHAINS], capture_output=True,
                           timeout=_TIMEOUT_S, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.decode("utf-8", "replace")


class SessionCA:
    def __init__(self, hosts=("api.anthropic.com",), *, openssl: str = "/usr/bin/openssl",
                 security: str = "/usr/bin/security", tmp_root=None):
        self._security = security
        hosts = tuple(hosts)
        if not hosts or any(not h or not all(c.isalnum() or c in "-." for c in h) for h in hosts):
            raise SessionCAError("invalid host list")
        self._closed = False
        self._dir: Path | None = None
        self._ctx: ssl.SSLContext | None = None
        try:
            self._dir = Path(tempfile.mkdtemp(prefix="acctsw-ca-", dir=str(tmp_root) if tmp_root else None))
            os.chmod(self._dir, 0o700)
        except OSError as e:
            raise SessionCAError(f"cannot create private dir: {e}") from e
        atexit.register(self.close)
        try:
            self._build(hosts, openssl)
        except BaseException:
            self.close()
            raise

    def _openssl(self, openssl: str, *args: str) -> None:
        d = self._dir
        assert d is not None
        env = {"PATH": "/usr/bin:/bin", "OPENSSL_CONF": str(d / "empty.cnf")}
        try:
            r = subprocess.run([openssl, *args], cwd=d, env=env, capture_output=True,
                               timeout=_TIMEOUT_S, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as e:
            raise SessionCAError(f"openssl unavailable: {type(e).__name__}") from e
        if r.returncode != 0:
            # stderr of key-generating commands never holds key material; keep only the first line
            first = r.stderr.decode("utf-8", "replace").strip().splitlines()[:1]
            raise SessionCAError(f"openssl {args[0]} failed: {first[0] if first else r.returncode}")

    def _build(self, hosts: tuple[str, ...], openssl: str) -> None:
        d = self._dir
        assert d is not None
        san = ",".join(f"DNS:{h}" for h in hosts)
        permitted = "\n".join(f"permitted;DNS.{i}={h}" for i, h in enumerate(hosts))
        (d / "empty.cnf").write_text("")
        (d / "ca.cnf").write_text(
            "[req]\ndistinguished_name=dn\nprompt=no\nx509_extensions=v3_ca\n[dn]\nCN=ai guest list session CA\n"
            "[v3_ca]\nbasicConstraints=critical,CA:TRUE,pathlen:0\nkeyUsage=critical,keyCertSign,cRLSign\n"
            "subjectKeyIdentifier=hash\nnameConstraints=critical,@nc\n[nc]\n" + permitted + "\n")
        (d / "leaf.cnf").write_text(
            f"[req]\ndistinguished_name=dn\nprompt=no\n[dn]\nCN={hosts[0]}\n"
            "[ext]\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\n"
            "authorityKeyIdentifier=keyid\nsubjectKeyIdentifier=hash\n"
            f"extendedKeyUsage=serverAuth\nsubjectAltName={san}\n")
        old_umask = os.umask(0o077)
        try:
            for name in ("ca", "leaf"):
                self._openssl(openssl, "ecparam", "-genkey", "-name", "prime256v1", "-noout", "-out", f"{name}.key")
            self._openssl(openssl, "req", "-new", "-x509", "-sha256", "-key", "ca.key", "-days", str(_DAYS),
                          "-config", "ca.cnf", "-out", "ca.crt")
            self._openssl(openssl, "req", "-new", "-key", "leaf.key", "-config", "leaf.cnf", "-out", "leaf.csr")
            self._openssl(openssl, "x509", "-req", "-sha256", "-in", "leaf.csr", "-CA", "ca.crt", "-CAkey", "ca.key",
                          "-CAcreateserial", "-days", str(_DAYS), "-extfile", "leaf.cnf", "-extensions", "ext",
                          "-out", "leaf.crt")
        finally:
            os.umask(old_umask)
        self._ca_pem = (d / "ca.crt").read_text()
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.set_alpn_protocols(["http/1.1"])
            (d / "chain.pem").write_text(_join((d / "leaf.crt").read_text(), self._ca_pem))
            ctx.load_cert_chain(str(d / "chain.pem"), str(d / "leaf.key"))
        except (ssl.SSLError, OSError) as e:
            raise SessionCAError(f"cannot load leaf: {type(e).__name__}") from e
        self._ctx = ctx
        for p in list(d.iterdir()):
            if p.suffix in (".key", ".csr", ".srl", ".cnf") or p.name == "chain.pem":
                p.unlink()
        (d / "ca.crt").unlink()
        self._write(d / "ca.pem", self._ca_pem)

    def _write(self, path: Path, text: str) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(path, 0o600)

    def server_context(self) -> ssl.SSLContext:
        if self._ctx is None:
            raise SessionCAError("closed")
        return self._ctx

    def ca_pem(self) -> str:
        return self._ca_pem

    def bundle_paths(self, environ) -> dict[str, str]:
        if self._dir is None or self._closed:
            raise SessionCAError("closed")
        node_user = _read_text(environ.get("NODE_EXTRA_CA_CERTS"))
        base = (_read_text(environ.get("SSL_CERT_FILE"))
                or _read_text(ssl.get_default_verify_paths().cafile)
                or _read_text(_FALLBACK_ROOTS))
        texts = [base, _keychain_roots(self._security)]
        texts += [_read_text(environ.get(v)) for v in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")]
        texts.append(self._ca_pem)
        seen: set[str] = set()
        blocks: list[str] = []
        for block in (b for t in texts for b in _certs(t)):
            key = _norm(block)
            if key not in seen:
                seen.add(key)
                blocks.append(block)
        node = self._dir / "node.pem"
        full = self._dir / "full.pem"
        self._write(node, _join(*([node_user] if node_user else []), self._ca_pem))
        self._write(full, _join(*blocks))
        return {"NODE_EXTRA_CA_CERTS": str(node), "SSL_CERT_FILE": str(full),
                "REQUESTS_CA_BUNDLE": str(full), "CURL_CA_BUNDLE": str(full)}

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        atexit.unregister(self.close)
        if self._dir is not None:
            shutil.rmtree(self._dir, ignore_errors=True)

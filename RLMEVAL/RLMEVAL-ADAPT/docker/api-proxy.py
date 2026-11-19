"""Forward only to the configured model endpoint, from the internal bridge."""
import argparse
import json
from http.client import HTTPConnection, HTTPSConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import select
import socket
from urllib.parse import urlsplit


class ModelProxy(BaseHTTPRequestHandler):
    def do_CONNECT(self):
        if self.server.endpoint.scheme != "https" or self.path.lower() != self.server.authority:
            host = self.path.split(":", 1)[0]
            if host and all(c.isascii() and (c.isalnum() or c in ".-") for c in host):
                self.log_message("Rejected CONNECT host %s", host)
            self.send_error(403)
            return
        self.forward()

    def do_POST(self):
        if self.server.codex_limit and self.path == "/v1/responses":
            self.forward_responses()
            return
        self.forward_http()

    def forward_responses(self):
        try:
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or "Transfer-Encoding" in self.headers:
                raise ValueError()
            size = int(lengths[0])
            if not 0 < size <= 64 * 1024 * 1024:
                raise ValueError()
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ValueError()
        except (ValueError, RecursionError):
            self.send_error(400)
            return
        body["max_output_tokens"] = self.server.codex_limit
        endpoint = self.server.endpoint
        proxy = self.server.upstream
        cls = HTTPSConnection if endpoint.scheme == "https" else HTTPConnection
        conn = cls(proxy.hostname if proxy else endpoint.hostname,
                   (proxy.port or 80) if proxy else self.server.port, timeout=60)
        path = endpoint.path.rstrip("/") + "/responses"
        if proxy and endpoint.scheme == "https":
            conn.set_tunnel(endpoint.hostname, self.server.port)
        elif proxy:
            path = f"http://{endpoint.netloc}{path}"
        headers = {k: v for k, v in self.headers.items() if k.lower() not in {
            "host", "connection", "proxy-authorization", "proxy-connection",
            "content-length", "transfer-encoding", "accept-encoding"}}
        started = False
        try:
            conn.connect()
            conn.sock.settimeout(None)
            conn.request("POST", path, json.dumps(body).encode(), headers)
            response = conn.getresponse()
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() not in {"connection", "transfer-encoding", "keep-alive"}:
                    self.send_header(key, value)
            self.end_headers()
            started = True
            while chunk := response.read1(65536):
                self.wfile.write(chunk)
        except (OSError, HTTPException):
            if not started:
                self.send_error(502)
        finally:
            conn.close()

    def do_GET(self):
        self.forward_http()

    def forward_http(self):
        target = urlsplit(self.path)
        if (self.server.endpoint.scheme != "http" or target.scheme != "http"
                or target.netloc.lower() != self.server.authority
                or not target.path.startswith(self.server.api_prefix)
                or "%" in target.path or "\\" in target.path
                or {".", ".."}.intersection(target.path.split("/"))
                or "Transfer-Encoding" in self.headers):
            self.send_error(403)
            return
        try:
            lengths = self.headers.get_all("Content-Length", ["0"])
            if len(lengths) != 1:
                raise ValueError()
            size = int(lengths[0])
            if not 0 <= size <= 64 * 1024 * 1024:
                raise ValueError()
        except ValueError:
            self.send_error(400)
            return
        path = target.path + ("?" + target.query if target.query else "")
        headers = [(key, value) for key, value in self.headers.items()
                   if key.lower() not in {"connection", "proxy-connection", "proxy-authorization", "host", "upgrade", "expect", "content-length"}
                   and "\r" not in value and "\n" not in value]
        headers += [("Host", self.server.authority), ("Connection", "close")]
        headers.append(("Content-Length", str(size)))
        if self.server.upstream:
            path = f"http://{self.server.authority}{path}"
        request = f"{self.command} {path} HTTP/1.1\r\n"
        request += "".join(f"{key}: {value}\r\n" for key, value in headers) + "\r\n"
        try:
            upstream = self.connect_upstream()
        except OSError:
            self.send_error(502)
            return
        with upstream:
            upstream.settimeout(None)
            upstream.sendall(request.encode("iso-8859-1") + self.rfile.read(size))
            self.close_connection = True
            while chunk := upstream.recv(65536):
                self.connection.sendall(chunk)

    def connect_upstream(self):
        endpoint = self.server.endpoint
        proxy = self.server.upstream
        if proxy:
            if endpoint.scheme == "http":
                return socket.create_connection((proxy.hostname, proxy.port or 80), timeout=60)
            connection = HTTPConnection(proxy.hostname, proxy.port or 80, timeout=60)
            connection.set_tunnel(endpoint.hostname, self.server.port)
            connection.connect()
            return connection.sock
        return socket.create_connection((endpoint.hostname, self.server.port), timeout=60)

    def forward(self):
        try:
            upstream = self.connect_upstream()
        except OSError:
            self.send_error(502)
            return
        with upstream:
            upstream.settimeout(None)
            self.send_response(200, "Connection established")
            self.end_headers()
            self.close_connection = True
            sockets = (self.connection, upstream)
            while True:
                ready, _, _ = select.select(sockets, (), ())
                for source in ready:
                    data = source.recv(65536)
                    if not data:
                        return
                    (upstream if source is self.connection else self.connection).sendall(data)

    def log_request(self, code="-", size="-"):
        # URLs can contain credentials. Record only request method and status.
        self.log_message("%s %s", self.command, code)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("endpoint")
    parser.add_argument("listen")
    parser.add_argument("--codex-limit", type=int)
    args = parser.parse_args()
    endpoint = urlsplit(args.endpoint)
    if endpoint.scheme not in {"http", "https"} or not endpoint.hostname or endpoint.username:
        parser.error("Expected a model API URL without credentials")
    upstream = urlsplit(os.environ["RLMEVAL_UPSTREAM_PROXY"]) if os.environ.get("RLMEVAL_UPSTREAM_PROXY") else None
    if upstream and (upstream.scheme != "http" or not upstream.hostname or upstream.username):
        parser.error("Expected an unauthenticated HTTP upstream proxy")
    with ThreadingHTTPServer((args.listen, 18080), ModelProxy) as server:
        server.codex_limit = args.codex_limit
        server.endpoint = endpoint
        server.upstream = upstream
        server.port = endpoint.port or (443 if endpoint.scheme == "https" else 80)
        server.authority = f"{endpoint.hostname}:{server.port}" if endpoint.scheme == "https" else endpoint.netloc.lower()
        server.api_prefix = endpoint.path.rstrip("/") + "/" if endpoint.path.strip("/") else "/v1/"
        server.serve_forever()


if __name__ == "__main__":
    main()

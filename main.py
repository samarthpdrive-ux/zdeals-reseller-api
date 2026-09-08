"""ZDeals Bot Wasmer Reseller API Gateway.

This service is the public API boundary.  It proxies authenticated requests to
the configured delivery backend without exposing its address or credentials.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import os
import re
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Empty, LifoQueue
from typing import Any
from urllib.parse import parse_qs, urlsplit


BOT_INTERNAL_URL = os.environ.get("BOT_INTERNAL_URL", "").strip().rstrip("/")
BOT_INTERNAL_SECRET = os.environ.get("BOT_INTERNAL_SECRET", "").strip()
BACKEND_CA_FILE = os.environ.get(
    "BACKEND_CA_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "ca.pem"),
)
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "80"))
MAX_BODY_BYTES = 64 * 1024
# The read budget is deliberately five seconds: combined with the three-second
# connection budget, a non-streaming backend response cannot keep a client
# waiting for the old 30-second default timeout.
BACKEND_TIMEOUT = (3.0, 5.0)  # connect, read
PRODUCT_CACHE_TTL = 30.0

PUBLIC_V1_ORDER_PATH = re.compile(r"^/api/v1/order/[1-9][0-9]*$")
PUBLIC_ORDER_PATH = re.compile(r"^/api/reseller/orders/([1-9][0-9]*)$")
LOG = logging.getLogger("zdeals.gateway")


def json_bytes(data: Any) -> bytes:
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def configured() -> bool:
    return bool(BOT_INTERNAL_URL and BOT_INTERNAL_SECRET)


def build_backend_ssl_context() -> ssl.SSLContext:
    """Use a bundled public CA root if the Wasmer runtime has no CA store."""
    return ssl.create_default_context(cafile=BACKEND_CA_FILE)


BACKEND_SSL_CONTEXT = build_backend_ssl_context()


def backend_url(path: str, query: str = "") -> str:
    """Build a backend URL from a fixed, application-owned route."""
    if not path.startswith("/"):
        raise ValueError("Backend path must start with '/'.")
    return f"{BOT_INTERNAL_URL}{path}" + (f"?{query}" if query else "")


def safe_content_type(value: str | None) -> str:
    """Do not reflect arbitrary/internal upstream headers to public clients."""
    if value and value.lower().split(";", 1)[0].strip() in {
        "application/json",
        "application/problem+json",
        "text/plain",
        "text/html",
    }:
        return value
    return "application/json; charset=utf-8"


def contains_internal_data(body: bytes) -> bool:
    """Fail closed if an upstream error accidentally includes private values."""
    sensitive = (BOT_INTERNAL_URL, BOT_INTERNAL_SECRET)
    return any(value and value.encode("utf-8") in body for value in sensitive)


@dataclass(frozen=True)
class CachedProducts:
    expires_at: float
    body: bytes
    content_type: str


class ProductCache:
    """Short, per-reseller cache.  Keys are hashes so API keys are not retained."""

    def __init__(self) -> None:
        self._entries: dict[str, CachedProducts] = {}
        self._lock = threading.Lock()

    @staticmethod
    def key(api_key: str) -> str:
        return hashlib.sha256(api_key.encode("utf-8")).hexdigest()

    def get(self, api_key: str) -> CachedProducts | None:
        now = time.monotonic()
        key = self.key(api_key)
        with self._lock:
            entry = self._entries.get(key)
            if entry and entry.expires_at > now:
                return entry
            self._entries.pop(key, None)
        return None

    def put(self, api_key: str, body: bytes, content_type: str) -> None:
        entry = CachedProducts(time.monotonic() + PRODUCT_CACHE_TTL, body, content_type)
        with self._lock:
            self._entries[self.key(api_key)] = entry


PRODUCT_CACHE = ProductCache()


@dataclass(frozen=True)
class BackendResponse:
    status: int
    body: bytes
    content_type: str | None


class BackendTimeout(Exception):
    """The backend exceeded the connection or response-read budget."""


class BackendConnectionPool:
    """Persistent HTTP/HTTPS connections using Python's standard library only."""

    def __init__(self, maxsize: int = 20) -> None:
        self._connections: LifoQueue[http.client.HTTPConnection] = LifoQueue(maxsize)

    def _new_connection(self) -> http.client.HTTPConnection:
        parsed = urlsplit(BOT_INTERNAL_URL)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise OSError("Invalid backend configuration.")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise OSError("Backend base URL may not include a path or query.")

        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError as error:
            raise OSError("Backend port is invalid.") from error
        if parsed.scheme == "https":
            return http.client.HTTPSConnection(
                parsed.hostname,
                port=port,
                timeout=BACKEND_TIMEOUT[0],
                context=BACKEND_SSL_CONTEXT,
            )

        return http.client.HTTPConnection(
            parsed.hostname,
            port=port,
            timeout=BACKEND_TIMEOUT[0],
        )

    def _release(self, connection: http.client.HTTPConnection) -> None:
        try:
            self._connections.put_nowait(connection)
        except Exception:
            connection.close()

    def request(
        self,
        method: str,
        path: str,
        query: str,
        headers: dict[str, str],
        body: bytes | None,
    ) -> BackendResponse:
        try:
            connection = self._connections.get_nowait()
        except Empty:
            connection = self._new_connection()

        try:
            if connection.sock is None:
                connection.timeout = BACKEND_TIMEOUT[0]

            target = path + (f"?{query}" if query else "")
            connection.request(method, target, body=body, headers=headers)

            if connection.sock is not None:
                connection.sock.settimeout(BACKEND_TIMEOUT[1])

            response = connection.getresponse()
            result = BackendResponse(
                status=response.status,
                body=response.read(),
                content_type=response.getheader("Content-Type"),
            )
            should_close = response.will_close

        except socket.timeout as error:
            connection.close()
            raise BackendTimeout() from error

        except (OSError, http.client.HTTPException):
            connection.close()
            raise

        if should_close:
            connection.close()
        else:
            self._release(connection)

        return result


BACKEND_POOL = BackendConnectionPool()


class GatewayHandler(BaseHTTPRequestHandler):
    server_version = "ZDealsBotResellerAPI/2.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: object) -> None:
        # BaseHTTPRequestHandler can include arbitrary request text in its log.
        # We log only the request method/path and never headers or bodies.
        return

    def send_bytes(self, status: int, body: bytes, content_type: str = "application/json; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", safe_content_type(content_type))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status: int, data: dict[str, Any]) -> None:
        self.send_bytes(status, json_bytes(data))

    def error(self, status: int, code: str, message: str) -> None:
        self.send_json(status, {"success": False, "error": code, "message": message})

    def unavailable(self, status: int = 502) -> None:
        self.error(
            status,
            "delivery_service_unavailable",
            "The delivery service is temporarily unavailable.",
        )

    def get_api_key(self) -> str:
        """Accept current Bearer usage and the gateway's documented legacy forms."""
        authorization = self.headers.get("Authorization", "").strip()
        if authorization:
            if authorization.lower().startswith("bearer "):
                return authorization[7:].strip()
            return authorization  # legacy Authorization: AK_xxx compatibility
        return self.headers.get("X-API-Key", "").strip()

    def require_api_key(self) -> str | None:
        api_key = self.get_api_key()
        if not api_key:
            self.error(401, "missing_api_key", "Provide an API key using Authorization: Bearer <API_KEY>.")
            return None
        return api_key

    def read_body(self) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.error(400, "invalid_content_length", "Invalid request body length.")
            return None
        if length < 0:
            self.error(400, "invalid_content_length", "Invalid request body length.")
            return None
        if length > MAX_BODY_BYTES:
            self.error(413, "request_too_large", "Request body is too large.")
            return None
        return self.rfile.read(length)

    def send_backend_response(self, response: BackendResponse) -> None:
        # Never turn an upstream redirect into a public redirect.  In particular,
        # Location is intentionally not copied from the upstream response.
        if 300 <= response.status < 400 or contains_internal_data(response.body):
            self.unavailable()
            return
        self.send_bytes(response.status, response.body, response.content_type)

    def forward(
        self,
        method: str,
        internal_path: str,
        api_key: str,
        body: bytes | None = None,
        query: str = "",
        cache_products: bool = False,
    ) -> None:
        if not configured():
            self.unavailable(503)
            return

        if cache_products and not query:
            cached = PRODUCT_CACHE.get(api_key)
            if cached:
                self.send_bytes(200, cached.body, cached.content_type)
                LOG.info("[PROXY] %s %s -> 200 in 0.00s (cache)", method, internal_path)
                return

        headers = {
            "Accept": "application/json",
            "X-API-Key": api_key,
            "X-Internal-Bot-Secret": BOT_INTERNAL_SECRET,
        }
        if body is not None:
            headers["Content-Type"] = "application/json"

        started = time.monotonic()
        try:
            response = BACKEND_POOL.request(
                method=method,
                path=internal_path,
                query=query,
                headers=headers,
                body=body,
            )
        except BackendTimeout:
            elapsed = time.monotonic() - started
            LOG.warning("[PROXY] %s %s -> timeout in %.2fs", method, internal_path, elapsed)
            self.unavailable(504)
            return
        except (OSError, http.client.HTTPException) as error:
            elapsed = time.monotonic() - started
            tls_detail = ""
            if isinstance(error, ssl.SSLCertVerificationError):
                tls_detail = (
                    f" verify_code={error.verify_code}"
                    f" verify_message={error.verify_message!r}"
                )
            LOG.warning(
                "[PROXY] %s %s -> unavailable (%s%s) in %.2fs",
                method,
                internal_path,
                type(error).__name__,
                tls_detail,
                elapsed,
            )
            self.unavailable(502)
            return

        elapsed = time.monotonic() - started
        LOG.info("[PROXY] %s %s -> %s in %.2fs", method, internal_path, response.status, elapsed)
        if cache_products and not query and response.status == 200 and not contains_internal_data(response.body):
            PRODUCT_CACHE.put(api_key, response.body, safe_content_type(response.content_type))
        self.send_backend_response(response)

    def validated_order_body(self, body: bytes) -> bytes | None:
        """Retain existing v1 validation while passing modern order JSON unchanged."""
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.error(400, "invalid_json", "Request body must contain valid JSON.")
            return None
        if not isinstance(data, dict):
            self.error(400, "invalid_order", "Request body must be a JSON object.")
            return None

        service_id = data.get("service_id", data.get("product_id"))
        client_order_id = data.get("client_order_id", data.get("external_order_id"))
        quantity = data.get("quantity", 1)
        delivery_telegram_id = data.get("delivery_telegram_id")
        try:
            service_id = int(service_id)
            quantity = int(quantity)
            if delivery_telegram_id is not None:
                delivery_telegram_id = int(delivery_telegram_id)
        except (TypeError, ValueError):
            self.error(400, "invalid_order", "service_id/product_id and quantity must be integers.")
            return None
        if service_id <= 0 or quantity <= 0:
            self.error(400, "invalid_order", "service_id and quantity must be greater than zero.")
            return None
        if not client_order_id:
            self.error(400, "invalid_order", "client_order_id or external_order_id is required.")
            return None
        if len(str(client_order_id)) > 80:
            self.error(400, "invalid_order", "client_order_id is too long.")
            return None
        if delivery_telegram_id is not None and delivery_telegram_id <= 0:
            self.error(400, "invalid_order", "delivery_telegram_id must be greater than zero.")
            return None

        # Modern requests retain their exact bytes.  For documented legacy field
        # names, add the canonical field names expected by the Render API while
        # preserving the supplied values and all extra fields.
        if "service_id" in data and "client_order_id" in data:
            return body
        normalized = dict(data)
        normalized.setdefault("service_id", service_id)
        normalized.setdefault("client_order_id", str(client_order_id))
        normalized.setdefault("quantity", quantity)
        if delivery_telegram_id is not None:
            normalized["delivery_telegram_id"] = delivery_telegram_id
        return json_bytes(normalized)

    def handle_v1(self, method: str) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path
        api_key = self.require_api_key()
        if api_key is None:
            return
        if method == "GET":
            if path == "/api/v1/products":
                self.forward("GET", path, api_key, query=parsed.query, cache_products=True)
                return
            if path in {"/api/v1/me", "/api/v1/orders"}:
                self.forward("GET", path, api_key, query=parsed.query)
                return
            if PUBLIC_V1_ORDER_PATH.fullmatch(path):
                self.forward("GET", path, api_key, query=parsed.query)
                return
        elif method == "POST" and path == "/api/v1/order":
            body = self.read_body()
            if body is not None:
                body = self.validated_order_body(body)
            if body is not None:
                self.forward("POST", path, api_key, body=body)
            return
        self.error(404, "not_found", "Endpoint not found.")

    def handle_legacy(self, method: str) -> None:
        parsed = urlsplit(self.path)
        api_key = self.require_api_key()
        if api_key is None:
            return
        action = parse_qs(parsed.query).get("action", [""])[0].lower()
        if method == "GET":
            routes = {"products": "/api/v1/products", "balance": "/api/v1/me", "orders": "/api/v1/orders"}
            if action in routes:
                self.forward("GET", routes[action], api_key, cache_products=action == "products")
                return
            if action == "order":
                self.error(400, "invalid_request", "Use /api/reseller/orders/{order_id} for a single order.")
                return
            self.error(404, "invalid_action", "Use action=products, balance, or orders.")
            return
        if method == "POST" and action == "order":
            body = self.read_body()
            if body is not None:
                body = self.validated_order_body(body)
            if body is not None:
                self.forward("POST", "/api/v1/order", api_key, body=body)
            return
        self.error(404, "invalid_action", "Invalid reseller action.")

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path
        if path == "/health":
            self.send_json(200, {"status": "ok", "service": "ZDeals Reseller API"})
        elif path == "/":
            self.home()
        elif path == "/docs":
            self.docs()
        elif path == "/openapi.json":
            self.send_json(200, self.openapi_spec())
        elif path.startswith("/api/v1/"):
            self.handle_v1("GET")
        elif path == "/api/reseller":
            self.handle_legacy("GET")
        elif path in {"/api/reseller/products", "/api/reseller/balance", "/api/reseller/orders"}:
            action = {"/api/reseller/products": "products", "/api/reseller/balance": "balance", "/api/reseller/orders": "orders"}[path]
            original = self.path
            self.path = f"/api/reseller?action={action}"
            try:
                self.handle_legacy("GET")
            finally:
                self.path = original
        elif match := PUBLIC_ORDER_PATH.fullmatch(path):
            api_key = self.require_api_key()
            if api_key is not None:
                self.forward("GET", f"/api/v1/order/{match.group(1)}", api_key, query=parsed.query)
        else:
            self.error(404, "not_found", "Endpoint not found.")

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path == "/api/v1/order":
            self.handle_v1("POST")
        elif path in {"/api/reseller", "/api/reseller/order"}:
            self.handle_legacy("POST")
        else:
            self.error(404, "not_found", "Endpoint not found.")

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-API-Key")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def home(self) -> None:
        page = """<!doctype html><html><head><meta charset=\"utf-8\"><title>ZDeals Bot Developer API</title></head><body><h1>ZDeals Bot Developer API</h1><p>Wasmer public reseller API gateway.</p><ul><li>GET /health</li><li>GET /api/v1/me</li><li>GET /api/v1/products</li><li>GET /api/v1/orders</li><li>GET /api/v1/order/{order_id}</li><li>POST /api/v1/order</li></ul><p>Open <a href=\"/docs\">/docs</a> for interactive API documentation.</p></body></html>"""
        self.send_bytes(200, page.encode("utf-8"), "text/html; charset=utf-8")

    def docs(self) -> None:
        page = """<!doctype html><html><head><meta charset=\"utf-8\"><title>ZDeals Bot API</title><link rel=\"stylesheet\" href=\"https://unpkg.com/swagger-ui-dist@5/swagger-ui.css\"></head><body><div id=\"swagger-ui\"></div><script src=\"https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js\"></script><script>window.ui=SwaggerUIBundle({url:'/openapi.json',dom_id:'#swagger-ui',deepLinking:true,persistAuthorization:false,displayRequestDuration:true,tryItOutEnabled:true});</script></body></html>"""
        self.send_bytes(200, page.encode("utf-8"), "text/html; charset=utf-8")

    def openapi_spec(self) -> dict[str, Any]:
        bearer = [{"ApiKey": []}]
        responses = {"401": {"description": "Invalid API key"}}
        return {
            "openapi": "3.0.3",
            "info": {"title": "ZDeals Bot Developer API", "version": "2.1.0", "description": "Use Authorization: Bearer YOUR_API_KEY."},
            "servers": [{"url": "/"}],
            "components": {"securitySchemes": {"ApiKey": {"type": "http", "scheme": "bearer", "bearerFormat": "AK_xxxxxxxxx"}}},
            "paths": {
                "/health": {"get": {"summary": "Health check", "responses": {"200": {"description": "OK"}}}},
                "/api/v1/me": {"get": {"summary": "Get wallet/account", "security": bearer, "responses": {"200": {"description": "Account"}, **responses}}},
                "/api/v1/products": {"get": {"summary": "List products", "security": bearer, "responses": {"200": {"description": "Products"}, **responses}}},
                "/api/v1/orders": {"get": {"summary": "List orders", "security": bearer, "responses": {"200": {"description": "Orders"}, **responses}}},
                "/api/v1/order/{order_id}": {"get": {"summary": "Get order", "security": bearer, "parameters": [{"name": "order_id", "in": "path", "required": True, "schema": {"type": "integer"}}], "responses": {"200": {"description": "Order"}, "404": {"description": "Order not found"}, **responses}}},
                "/api/v1/order": {"post": {"summary": "Create order", "security": bearer, "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object"}}}}, "responses": {"200": {"description": "Order created"}, "400": {"description": "Invalid order"}, "404": {"description": "Product not found"}, **responses}}},
            },
        }


class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    LOG.info("ZDeals Bot Wasmer Reseller API starting on %s:%s; backend configured=%s", HOST, PORT, configured())
    GatewayServer((HOST, PORT), GatewayHandler).serve_forever()

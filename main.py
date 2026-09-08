"""
ZDeals Bot Wasmer Reseller API Gateway.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

import requests
from requests.adapters import HTTPAdapter


BOT_INTERNAL_URL = os.environ.get("BOT_INTERNAL_URL", "").strip().rstrip("/")
BOT_INTERNAL_SECRET = os.environ.get("BOT_INTERNAL_SECRET", "").strip()

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "80"))

MAX_BODY_BYTES = 64 * 1024
BACKEND_TIMEOUT = (3.0, 5.0)
PRODUCT_CACHE_TTL = 30.0

PUBLIC_V1_ORDER_PATH = re.compile(r"^/api/v1/order/[1-9][0-9]*$")
PUBLIC_ORDER_PATH = re.compile(r"^/api/reseller/orders/([1-9][0-9]*)$")

LOG = logging.getLogger("zdeals.gateway")


def json_bytes(data: Any) -> bytes:
    return json.dumps(
        data,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def configured() -> bool:
    return bool(BOT_INTERNAL_URL and BOT_INTERNAL_SECRET)


def backend_url(path: str, query: str = "") -> str:
    if not path.startswith("/"):
        raise ValueError("Invalid backend path.")

    url = f"{BOT_INTERNAL_URL}{path}"

    if query:
        url += f"?{query}"

    return url


def safe_content_type(value: str | None) -> str:
    if value and value.lower().split(";", 1)[0].strip() in {
        "application/json",
        "application/problem+json",
        "text/plain",
        "text/html",
    }:
        return value

    return "application/json; charset=utf-8"


def contains_internal_data(body: bytes) -> bool:
    return any(
        value and value.encode("utf-8") in body
        for value in (
            BOT_INTERNAL_URL,
            BOT_INTERNAL_SECRET,
        )
    )


@dataclass(frozen=True)
class CachedProducts:
    expires_at: float
    body: bytes
    content_type: str


class ProductCache:
    def __init__(self) -> None:
        self._entries: dict[str, CachedProducts] = {}
        self._lock = threading.Lock()

    @staticmethod
    def key(api_key: str) -> str:
        return hashlib.sha256(
            api_key.encode("utf-8")
        ).hexdigest()

    def get(self, api_key: str) -> CachedProducts | None:
        key = self.key(api_key)

        with self._lock:
            entry = self._entries.get(key)

            if entry and entry.expires_at > time.monotonic():
                return entry

            self._entries.pop(key, None)

        return None

    def put(
        self,
        api_key: str,
        body: bytes,
        content_type: str,
    ) -> None:
        with self._lock:
            self._entries[self.key(api_key)] = CachedProducts(
                expires_at=time.monotonic() + PRODUCT_CACHE_TTL,
                body=body,
                content_type=content_type,
            )


PRODUCT_CACHE = ProductCache()


def build_backend_session() -> requests.Session:
    session = requests.Session()

    adapter = HTTPAdapter(
        pool_connections=10,
        pool_maxsize=20,
        max_retries=0,
    )

    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.trust_env = False

    return session


BACKEND_SESSION = build_backend_session()


class GatewayHandler(BaseHTTPRequestHandler):
    server_version = "ZDealsBotResellerAPI/2.2"
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def send_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str = "application/json; charset=utf-8",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", safe_content_type(content_type))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def send_json(
        self,
        status: int,
        data: dict[str, Any],
    ) -> None:
        self.send_bytes(status, json_bytes(data))

    def error(
        self,
        status: int,
        code: str,
        message: str,
    ) -> None:
        self.send_json(
            status,
            {
                "success": False,
                "error": code,
                "message": message,
            },
        )

    def unavailable(self, status: int = 502) -> None:
        self.error(
            status,
            "delivery_service_unavailable",
            "The delivery service is temporarily unavailable.",
        )

    def get_api_key(self) -> str:
        authorization = self.headers.get(
            "Authorization",
            "",
        ).strip()

        if authorization:
            if authorization.lower().startswith("bearer "):
                return authorization[7:].strip()

            return authorization

        return self.headers.get("X-API-Key", "").strip()

    def require_api_key(self) -> str | None:
        api_key = self.get_api_key()

        if not api_key:
            self.error(
                401,
                "missing_api_key",
                "Provide an API key using Authorization: Bearer <API_KEY>.",
            )
            return None

        return api_key

    def read_body(self) -> bytes | None:
        try:
            length = int(
                self.headers.get("Content-Length", "0")
            )
        except ValueError:
            self.error(
                400,
                "invalid_content_length",
                "Invalid request body length.",
            )
            return None

        if length < 0:
            self.error(
                400,
                "invalid_content_length",
                "Invalid request body length.",
            )
            return None

        if length > MAX_BODY_BYTES:
            self.error(
                413,
                "request_too_large",
                "Request body is too large.",
            )
            return None

        return self.rfile.read(length)

    def send_backend_response(
        self,
        response: requests.Response,
    ) -> None:
        if (
            300 <= response.status_code < 400
            or contains_internal_data(response.content)
        ):
            self.unavailable()
            return

        self.send_bytes(
            response.status_code,
            response.content,
            response.headers.get("Content-Type"),
        )

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
                self.send_bytes(
                    200,
                    cached.body,
                    cached.content_type,
                )

                LOG.info(
                    "[PROXY] %s %s -> 200 in 0.00s (cache)",
                    method,
                    internal_path,
                )
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
            response = BACKEND_SESSION.request(
                method=method,
                url=backend_url(internal_path, query),
                data=body,
                headers=headers,
                timeout=BACKEND_TIMEOUT,
                allow_redirects=False,
            )

        except requests.Timeout:
            elapsed = time.monotonic() - started

            LOG.warning(
                "[PROXY] %s %s -> timeout in %.2fs",
                method,
                internal_path,
                elapsed,
            )

            self.unavailable(504)
            return

        except requests.RequestException:
            elapsed = time.monotonic() - started

            LOG.warning(
                "[PROXY] %s %s -> unavailable in %.2fs",
                method,
                internal_path,
                elapsed,
            )

            self.unavailable(502)
            return

        elapsed = time.monotonic() - started

        LOG.info(
            "[PROXY] %s %s -> %s in %.2fs",
            method,
            internal_path,
            response.status_code,
            elapsed,
        )

        if (
            cache_products
            and not query
            and response.status_code == 200
            and not contains_internal_data(response.content)
        ):
            PRODUCT_CACHE.put(
                api_key,
                response.content,
                safe_content_type(
                    response.headers.get("Content-Type")
                ),
            )

        self.send_backend_response(response)

    def validated_order_body(self, body: bytes) -> bytes | None:
        try:
            data = json.loads(body.decode("utf-8"))

        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
        ):
            self.error(
                400,
                "invalid_json",
                "Request body must contain valid JSON.",
            )
            return None

        if not isinstance(data, dict):
            self.error(
                400,
                "invalid_order",
                "Request body must be a JSON object.",
            )
            return None

        service_id = data.get(
            "service_id",
            data.get("product_id"),
        )

        client_order_id = data.get(
            "client_order_id",
            data.get("external_order_id"),
        )

        quantity = data.get("quantity", 1)
        delivery_telegram_id = data.get(
            "delivery_telegram_id"
        )

        try:
            service_id = int(service_id)
            quantity = int(quantity)

            if delivery_telegram_id is not None:
                delivery_telegram_id = int(
                    delivery_telegram_id
                )

        except (TypeError, ValueError):
            self.error(
                400,
                "invalid_order",
                "service_id/product_id and quantity must be integers.",
            )
            return None

        if service_id <= 0 or quantity <= 0:
            self.error(
                400,
                "invalid_order",
                "service_id and quantity must be greater than zero.",
            )
            return None

        if not client_order_id:
            self.error(
                400,
                "invalid_order",
                "client_order_id or external_order_id is required.",
            )
            return None

        if len(str(client_order_id)) > 80:
            self.error(
                400,
                "invalid_order",
                "client_order_id is too long.",
            )
            return None

        if (
            delivery_telegram_id is not None
            and delivery_telegram_id <= 0
        ):
            self.error(
                400,
                "invalid_order",
                "delivery_telegram_id must be greater than zero.",
            )
            return None

        if (
            "service_id" in data
            and "client_order_id" in data
        ):
            return body

        normalized = dict(data)
        normalized.setdefault("service_id", service_id)
        normalized.setdefault(
            "client_order_id",
            str(client_order_id),
        )
        normalized.setdefault("quantity", quantity)

        if delivery_telegram_id is not None:
            normalized["delivery_telegram_id"] = (
                delivery_telegram_id
            )

        return json_bytes(normalized)

    def handle_v1(self, method: str) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path

        api_key = self.require_api_key()

        if api_key is None:
            return

        if method == "GET":
            if path == "/api/v1/products":
                self.forward(
                    "GET",
                    path,
                    api_key,
                    query=parsed.query,
                    cache_products=True,
                )
                return

            if path in {
                "/api/v1/me",
                "/api/v1/orders",
            }:
                self.forward(
                    "GET",
                    path,
                    api_key,
                    query=parsed.query,
                )
                return

            if PUBLIC_V1_ORDER_PATH.fullmatch(path):
                self.forward(
                    "GET",
                    path,
                    api_key,
                    query=parsed.query,
                )
                return

        if method == "POST" and path == "/api/v1/order":
            body = self.read_body()

            if body is not None:
                body = self.validated_order_body(body)

            if body is not None:
                self.forward(
                    "POST",
                    path,
                    api_key,
                    body=body,
                )

            return

        self.error(404, "not_found", "Endpoint not found.")

    def handle_legacy(self, method: str) -> None:
        parsed = urlsplit(self.path)

        api_key = self.require_api_key()

        if api_key is None:
            return

        action = parse_qs(
            parsed.query
        ).get("action", [""])[0].lower()

        if method == "GET":
            routes = {
                "products": "/api/v1/products",
                "balance": "/api/v1/me",
                "orders": "/api/v1/orders",
            }

            if action in routes:
                self.forward(
                    "GET",
                    routes[action],
                    api_key,
                    cache_products=action == "products",
                )
                return

            if action == "order":
                self.error(
                    400,
                    "invalid_request",
                    "Use /api/reseller/orders/{order_id} for a single order.",
                )
                return

            self.error(
                404,
                "invalid_action",
                "Use action=products, balance, or orders.",
            )
            return

        if method == "POST" and action == "order":
            body = self.read_body()

            if body is not None:
                body = self.validated_order_body(body)

            if body is not None:
                self.forward(
                    "POST",
                    "/api/v1/order",
                    api_key,
                    body=body,
                )

            return

        self.error(
            404,
            "invalid_action",
            "Invalid reseller action.",
        )

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path

        if path == "/health":
            self.send_json(
                200,
                {
                    "status": "ok",
                    "service": "ZDeals Reseller API",
                },
            )
            return

        if path == "/":
            self.home()
            return

        if path == "/docs":
            self.docs()
            return

        if path == "/openapi.json":
            self.send_json(200, self.openapi_spec())
            return

        if path.startswith("/api/v1/"):
            self.handle_v1("GET")
            return

        if path == "/api/reseller":
            self.handle_legacy("GET")
            return

        legacy_paths = {
            "/api/reseller/products": "products",
            "/api/reseller/balance": "balance",
            "/api/reseller/orders": "orders",
        }

        if path in legacy_paths:
            original_path = self.path
            self.path = (
                f"/api/reseller?action={legacy_paths[path]}"
            )

            try:
                self.handle_legacy("GET")

            finally:
                self.path = original_path

            return

        match = PUBLIC_ORDER_PATH.fullmatch(path)

        if match:
            api_key = self.require_api_key()

            if api_key is not None:
                self.forward(
                    "GET",
                    f"/api/v1/order/{match.group(1)}",
                    api_key,
                    query=parsed.query,
                )

            return

        self.error(404, "not_found", "Endpoint not found.")

    def do_POST(self) -> None:
        path = urlsplit(self.path).path

        if path == "/api/v1/order":
            self.handle_v1("POST")
            return

        if path in {
            "/api/reseller",
            "/api/reseller/order",
        }:
            self.handle_legacy("POST")
            return

        self.error(404, "not_found", "Endpoint not found.")

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header(
            "Access-Control-Allow-Methods",
            "GET, POST, OPTIONS",
        )
        self.send_header(
            "Access-Control-Allow-Headers",
            "Authorization, Content-Type, X-API-Key",
        )
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def home(self) -> None:
        page = """
<!doctype html>
<html>
<head>
    <meta charset="utf-8">
    <title>ZDeals Bot Developer API</title>
</head>
<body>
    <h1>ZDeals Bot Developer API</h1>
    <p>Wasmer public reseller API gateway.</p>
    <ul>
        <li>GET /health</li>
        <li>GET /api/v1/me</li>
        <li>GET /api/v1/products</li>
        <li>GET /api/v1/orders</li>
        <li>GET /api/v1/order/{order_id}</li>
        <li>POST /api/v1/order</li>
    </ul>
    <p>Open <a href="/docs">/docs</a> for API documentation.</p>
</body>
</html>
""".strip()

        self.send_bytes(
            200,
            page.encode("utf-8"),
            "text/html; charset=utf-8",
        )

    def docs(self) -> None:
        page = """
<!doctype html>
<html>
<head>
    <meta charset="utf-8">
    <title>ZDeals Bot API</title>
    <link rel="stylesheet"
          href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css">
</head>
<body>
    <div id="swagger-ui"></div>
    <script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
    <script>
        window.ui = SwaggerUIBundle({
            url: "/openapi.json",
            dom_id: "#swagger-ui",
            deepLinking: true,
            persistAuthorization: false,
            displayRequestDuration: true,
            tryItOutEnabled: true
        });
    </script>
</body>
</html>
""".strip()

        self.send_bytes(
            200,
            page.encode("utf-8"),
            "text/html; charset=utf-8",
        )

    def openapi_spec(self) -> dict[str, Any]:
        bearer = [{"ApiKey": []}]

        return {
            "openapi": "3.0.3",
            "info": {
                "title": "ZDeals Bot Developer API",
                "version": "2.2.0",
                "description": (
                    "Use Authorization: Bearer YOUR_API_KEY."
                ),
            },
            "servers": [{"url": "/"}],
            "components": {
                "securitySchemes": {
                    "ApiKey": {
                        "type": "http",
                        "scheme": "bearer",
                        "bearerFormat": "AK_xxxxxxxxx",
                    }
                }
            },
            "paths": {
                "/health": {
                    "get": {
                        "summary": "Health check",
                        "responses": {
                            "200": {
                                "description": "OK"
                            }
                        },
                    }
                },
                "/api/v1/me": {
                    "get": {
                        "summary": "Get wallet/account",
                        "security": bearer,
                        "responses": {
                            "200": {
                                "description": "Account"
                            },
                            "401": {
                                "description": "Invalid API key"
                            },
                        },
                    }
                },
                "/api/v1/products": {
                    "get": {
                        "summary": "List products",
                        "security": bearer,
                        "responses": {
                            "200": {
                                "description": "Products"
                            },
                            "401": {
                                "description": "Invalid API key"
                            },
                        },
                    }
                },
                "/api/v1/orders": {
                    "get": {
                        "summary": "List orders",
                        "security": bearer,
                        "responses": {
                            "200": {
                                "description": "Orders"
                            },
                            "401": {
                                "description": "Invalid API key"
                            },
                        },
                    }
                },
                "/api/v1/order/{order_id}": {
                    "get": {
                        "summary": "Get one order",
                        "security": bearer,
                        "parameters": [
                            {
                                "name": "order_id",
                                "in": "path",
                                "required": True,
                                "schema": {
                                    "type": "integer"
                                },
                            }
                        ],
                        "responses": {
                            "200": {
                                "description": "Order"
                            },
                            "401": {
                                "description": "Invalid API key"
                            },
                            "404": {
                                "description": "Order not found"
                            },
                        },
                    }
                },
                "/api/v1/order": {
                    "post": {
                        "summary": "Create order",
                        "security": bearer,
                        "requestBody": {
                            "required": True,
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object"
                                    }
                                }
                            },
                        },
                        "responses": {
                            "200": {
                                "description": "Order created"
                            },
                            "400": {
                                "description": "Invalid order"
                            },
                            "401": {
                                "description": "Invalid API key"
                            },
                            "404": {
                                "description": "Product not found"
                            },
                        },
                    }
                },
            },
        }


class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    LOG.info(
        "ZDeals Bot Wasmer Reseller API starting on %s:%s; backend configured=%s",
        HOST,
        PORT,
        configured(),
    )

    GatewayServer(
        (HOST, PORT),
        GatewayHandler,
    ).serve_forever()

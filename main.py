"""Public Wasmer gateway for NomanBot reseller API.

This app intentionally stores no secrets in source code.  It accepts public
requests at ``/api/v1/*`` and forwards only permitted routes to the bot's
private delivery bridge, adding the secret that proves the request came from
this Wasmer app.
"""

from __future__ import annotations

import html
import json
import os
import re
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


BOT_INTERNAL_URL = os.environ.get("BOT_INTERNAL_URL", "").rstrip("/")
BOT_INTERNAL_SECRET = os.environ.get("BOT_INTERNAL_SECRET", "")
PUBLIC_CORS_ORIGIN = os.environ.get("PUBLIC_CORS_ORIGIN", "").rstrip("/")
MAX_BODY_BYTES = 64 * 1024

_ORDER_PATH = re.compile(r"^/api/v1/order/[1-9][0-9]*$")
_PUBLIC_ORDER_PATH = re.compile(r"^/api/reseller/orders/([1-9][0-9]*)$")


def _configured() -> bool:
    return bool(BOT_INTERNAL_URL and BOT_INTERNAL_SECRET)


def _allowed_route(method: str, path: str) -> bool:
    if method == "GET":
        return path in {"/api/v1/me", "/api/v1/products", "/api/v1/orders"} or bool(_ORDER_PATH.fullmatch(path))
    return method == "POST" and path == "/api/v1/order"


def _json_bytes(data: dict) -> bytes:
    return json.dumps(data, separators=(",", ":")).encode("utf-8")


class GatewayHandler(BaseHTTPRequestHandler):
    server_version = "NomanWasmerGateway/1.0"

    def log_message(self, format: str, *args: object) -> None:
        # Do not log authorization headers, URLs, or request bodies.
        print("gateway", self.command, self.path.split("?", 1)[0], args[1] if len(args) > 1 else "")

    def _cors_origin(self) -> str | None:
        origin = self.headers.get("Origin", "").rstrip("/")
        return origin if PUBLIC_CORS_ORIGIN and origin == PUBLIC_CORS_ORIGIN else None

    def _send(self, status: int, body: bytes, content_type: str = "application/json; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        origin = self._cors_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, code: str, message: str) -> None:
        self._send(status, _json_bytes({"success": False, "error": code, "message": message}))

    def do_OPTIONS(self) -> None:
        origin = self._cors_origin()
        if not origin:
            self._error(403, "cors_not_allowed", "Browser access is not enabled for this origin.")
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-API-Key")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Vary", "Origin")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/":
            self._home()
            return
        if parsed.path == "/health":
            self._send(200, _json_bytes({"status": "ok", "service": "Wasmer reseller gateway"}))
            return
        if parsed.path == "/docs":
            self._docs()
            return
        if parsed.path == "/openapi.json":
            self._send(200, _json_bytes(self._openapi_spec()))
            return
        if parsed.path == "/api/reseller":
            self._reseller_api("GET", parsed)
            return
        public_action = {
            "/api/reseller/products": "products",
            "/api/reseller/balance": "balance",
            "/api/reseller/orders": "orders",
        }.get(parsed.path)
        if public_action:
            self._reseller_api("GET", parsed, action_override=public_action)
            return
        order_match = _PUBLIC_ORDER_PATH.fullmatch(parsed.path)
        if order_match:
            self._reseller_api("GET", parsed, action_override="order", order_id=order_match.group(1))
            return
        self._proxy("GET")

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/api/reseller":
            self._reseller_api("POST", parsed)
            return
        if parsed.path == "/api/reseller/order":
            self._reseller_api("POST", parsed, action_override="order")
            return
        self._proxy("POST")

    def _home(self) -> None:
        page = """<!doctype html><html><head><meta charset=\"utf-8\"><title>Reseller API</title></head>
<body><h1>Reseller API</h1><p>Open <a href=\"/docs\">/docs</a> for interactive developer documentation.</p></body></html>"""
        self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")

    def _docs(self) -> None:
        # Swagger UI is loaded in the visitor's browser. The gateway remains a
        # dependency-free standard-library Python application on Wasmer.
        page = """<!doctype html><html><head><meta charset=\"utf-8\">
<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
<title>NomanBot Reseller API</title>
<link rel=\"stylesheet\" href=\"https://unpkg.com/swagger-ui-dist@5/swagger-ui.css\">
<style>body{margin:0;background:#fafafa}.topbar{display:none}</style></head>
<body><div id=\"swagger-ui\"></div>
<script src=\"https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js\"></script>
<script>window.ui=SwaggerUIBundle({url:'/openapi.json',dom_id:'#swagger-ui',deepLinking:true,persistAuthorization:false,displayRequestDuration:true,tryItOutEnabled:true});</script>
</body></html>"""
        self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")

    def _openapi_spec(self) -> dict:
        """Public OpenAPI document rendered by /docs (Swagger UI)."""
        error = {"$ref": "#/components/schemas/ErrorResponse"}
        success = {"description": "Successful response"}
        return {
            "openapi": "3.0.3",
            "info": {
                "title": "NomanBot Reseller API",
                "version": "1.0.0",
                "description": (
                    "Sell NomanBot products from your own bot or website. Your API key uses "
                    "your Telegram wallet balance and your custom product rates. Keep it on "
                    "your server; never put it in browser JavaScript.\\n\\n"
                    "Rate limit: 3 requests per second per API key. Orders are safe to retry "
                    "when you reuse the same external_order_id."
                ),
            },
            "servers": [{"url": "/", "description": "This Wasmer API gateway"}],
            "tags": [{"name": "Reseller API", "description": "Authenticated wholesale reseller endpoints"}],
            "paths": {
                "/api/reseller/products": {
                    "get": {
                        "tags": ["Reseller API"], "summary": "List products", "operationId": "listProducts",
                        "security": [{"bearerAuth": []}],
                        "responses": {"200": {**success, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ProductsResponse"}}}}, "401": {"description": "Missing or invalid API key", "content": {"application/json": {"schema": error}}}},
                    }
                },
                "/api/reseller/balance": {
                    "get": {
                        "tags": ["Reseller API"], "summary": "Get wallet balance", "operationId": "getBalance",
                        "security": [{"bearerAuth": []}],
                        "responses": {"200": {**success, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/BalanceResponse"}}}}, "401": {"description": "Missing or invalid API key", "content": {"application/json": {"schema": error}}}},
                    }
                },
                "/api/reseller/order": {
                    "post": {
                        "tags": ["Reseller API"], "summary": "Create an order", "operationId": "createOrder",
                        "description": "external_order_id must be unique for every purchase. Reuse it only when retrying the same order after a timeout.",
                        "security": [{"bearerAuth": []}],
                        "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/CreateOrderRequest"}}}},
                        "responses": {"200": {**success, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/OrderResponse"}}}}, "400": {"description": "Invalid order or insufficient balance", "content": {"application/json": {"schema": error}}}, "404": {"description": "Product not found", "content": {"application/json": {"schema": error}}}},
                    }
                },
                "/api/reseller/orders": {
                    "get": {
                        "tags": ["Reseller API"], "summary": "List your orders", "operationId": "listOrders",
                        "security": [{"bearerAuth": []}],
                        "responses": {"200": {**success, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/OrdersResponse"}}}}},
                    }
                },
                "/api/reseller/orders/{order_id}": {
                    "get": {
                        "tags": ["Reseller API"], "summary": "Get one order", "operationId": "getOrder",
                        "security": [{"bearerAuth": []}],
                        "parameters": [{"name": "order_id", "in": "path", "required": True, "schema": {"type": "integer", "minimum": 1}, "example": 123}],
                        "responses": {"200": {**success, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/OrderResponse"}}}}, "404": {"description": "Order not found", "content": {"application/json": {"schema": error}}}},
                    }
                },
            },
            "components": {
                "securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "AK_your_api_key", "description": "Paste your Telegram-generated API key."}},
                "schemas": {
                    "Product": {"type": "object", "properties": {"service_id": {"type": "string", "example": "330001"}, "name": {"type": "string", "example": "Test Product"}, "description": {"type": "string"}, "category": {"type": "string", "example": "streaming"}, "price": {"type": "string", "example": "0.50"}, "currency": {"type": "string", "example": "USDT"}, "stock": {"type": "integer", "example": 10}, "preorder": {"type": "boolean", "example": False}, "delivery_type": {"type": "string", "enum": ["automatic", "manual", "hybrid"]}}},
                    "ProductsResponse": {"type": "object", "properties": {"success": {"type": "boolean", "example": True}, "services": {"type": "array", "items": {"$ref": "#/components/schemas/Product"}}}},
                    "BalanceResponse": {"type": "object", "properties": {"chat_id": {"type": "integer", "example": 123456789}, "first_name": {"type": "string", "example": "Reseller"}, "wallet_balance": {"type": "string", "example": "12.50"}, "currency": {"type": "string", "example": "USDT"}}},
                    "CreateOrderRequest": {"type": "object", "required": ["product_id", "quantity", "external_order_id"], "properties": {"product_id": {"type": "integer", "example": 330001}, "quantity": {"type": "integer", "minimum": 1, "maximum": 100, "default": 1}, "external_order_id": {"type": "string", "example": "website-order-10001", "description": "Unique ID generated by your own website or bot."}, "delivery_telegram_id": {"type": "integer", "nullable": True, "example": 123456789, "description": "Optional. For manual products, the customer must start your Delivery Bot first. If omitted, delivery is sent only to the admin."}}},
                    "Order": {"type": "object", "properties": {"order_id": {"type": "string", "example": "123"}, "service_id": {"type": "string", "example": "330001"}, "service": {"type": "string", "example": "Test Product"}, "quantity": {"type": "integer", "example": 1}, "amount": {"type": "string", "example": "0.50"}, "currency": {"type": "string", "example": "USDT"}, "status": {"type": "string", "example": "completed"}, "delivery_type": {"type": "string", "example": "automatic"}, "delivery_destination": {"type": "string", "example": "api_response"}, "delivery_status": {"type": "string", "nullable": True}, "delivered_products": {"type": "array", "items": {"type": "string"}}, "created_at": {"type": "string", "format": "date-time"}}},
                    "OrderResponse": {"type": "object", "properties": {"success": {"type": "boolean", "example": True}, "idempotent_replay": {"type": "boolean", "example": False}, "order": {"$ref": "#/components/schemas/Order"}}},
                    "OrdersResponse": {"type": "object", "properties": {"success": {"type": "boolean", "example": True}, "page": {"type": "integer", "example": 1}, "limit": {"type": "integer", "example": 50}, "total_orders": {"type": "integer", "example": 1}, "total_pages": {"type": "integer", "example": 1}, "orders": {"type": "array", "items": {"$ref": "#/components/schemas/Order"}}}},
                    "ErrorResponse": {"type": "object", "properties": {"success": {"type": "boolean", "example": False}, "error": {"type": "string", "example": "insufficient_balance"}, "message": {"type": "string", "example": "Insufficient wallet balance."}}},
                },
            },
        }

    def _read_body(self) -> bytes | None:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError:
            self._error(400, "invalid_content_length", "Invalid request body length.")
            return None
        if length < 0 or length > MAX_BODY_BYTES:
            self._error(413, "request_too_large", "Request body is too large.")
            return None
        return self.rfile.read(length)

    def _customer_api_key(self) -> str:
        """Read the documented Bearer key (with X-API-Key compatibility)."""
        authorization = self.headers.get("Authorization", "").strip()
        if authorization.lower().startswith("bearer "):
            return authorization[7:].strip()
        return self.headers.get("X-API-Key", "").strip()

    def _forward(self, method: str, internal_path: str, api_key: str, body: bytes | None = None) -> None:
        """Forward a verified public request to the hidden Render bot bridge."""
        target = BOT_INTERNAL_URL + internal_path
        headers = {
            "Accept": "application/json",
            "X-API-Key": api_key,
            "X-Internal-Bot-Secret": BOT_INTERNAL_SECRET,
        }
        if method == "POST":
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(target, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                response_body = response.read()
                content_type = response.headers.get("Content-Type", "application/json; charset=utf-8")
                self._send(response.status, response_body, content_type)
        except urllib.error.HTTPError as error:
            response_body = error.read() or _json_bytes({"success": False, "error": "bot_error"})
            content_type = error.headers.get("Content-Type", "application/json; charset=utf-8")
            self._send(error.code, response_body, content_type)
        except (urllib.error.URLError, TimeoutError, OSError):
            self._error(502, "delivery_service_unavailable", "The delivery service is temporarily unavailable. Retry orders with the same external_order_id.")

    def _reseller_api(self, method: str, parsed, action_override: str | None = None, order_id: str | None = None) -> None:
        """Public provider-style API: action=products, balance, or order."""
        if not _configured():
            self._error(503, "gateway_not_configured", "The reseller gateway is not configured yet.")
            return
        api_key = self._customer_api_key()
        if not api_key:
            self._error(401, "missing_api_key", "Use Authorization: Bearer AK_your_api_key.")
            return

        action = action_override or parse_qs(parsed.query).get("action", [""])[0].lower()
        if method == "GET":
            if action == "order" and order_id:
                self._forward("GET", f"/internal/v1/order/{order_id}", api_key)
                return
            internal_path = {
                "products": "/internal/v1/products",
                "balance": "/internal/v1/me",
                "orders": "/internal/v1/orders",
            }.get(action)
            if not internal_path:
                self._error(404, "invalid_action", "Use action=products, balance, or orders.")
                return
            self._forward("GET", internal_path, api_key)
            return

        if method != "POST" or action != "order":
            self._error(404, "invalid_action", "Use POST with action=order.")
            return
        raw_body = self._read_body()
        if raw_body is None:
            return
        try:
            payload = json.loads(raw_body.decode("utf-8"))
            product_id = int(payload["product_id"])
            quantity = int(payload.get("quantity", 1))
            external_order_id = str(payload["external_order_id"])
            delivery_telegram_id = payload.get("delivery_telegram_id")
            if delivery_telegram_id is not None:
                delivery_telegram_id = int(delivery_telegram_id)
        except (ValueError, TypeError, KeyError, UnicodeDecodeError, json.JSONDecodeError):
            self._error(400, "invalid_order", "Send product_id, quantity, and external_order_id as JSON.")
            return
        if product_id <= 0 or quantity <= 0 or not external_order_id or (
            delivery_telegram_id is not None and delivery_telegram_id <= 0
        ):
            self._error(400, "invalid_order", "product_id, quantity, external_order_id, and delivery_telegram_id must be valid.")
            return
        bot_payload = _json_bytes({
            "service_id": product_id,
            "quantity": quantity,
            "client_order_id": external_order_id,
            "delivery_telegram_id": delivery_telegram_id,
        })
        self._forward("POST", "/internal/v1/order", api_key, bot_payload)

    def _proxy(self, method: str) -> None:
        parsed = urlsplit(self.path)
        if not _allowed_route(method, parsed.path):
            self._error(404, "not_found", "Endpoint not found.")
            return
        if not _configured():
            self._error(503, "gateway_not_configured", "The reseller gateway is not configured yet.")
            return
        api_key = self.headers.get("X-API-Key", "").strip()
        if not api_key:
            self._error(401, "missing_api_key", "Provide X-API-Key.")
            return
        body = self._read_body() if method == "POST" else None
        if method == "POST" and body is None:
            return

        # The private path is deliberately different from the public path.
        internal_path = "/internal/v1" + parsed.path.removeprefix("/api/v1")
        target = BOT_INTERNAL_URL + internal_path
        if parsed.query:
            target += "?" + parsed.query
        headers = {
            "Accept": "application/json",
            "X-API-Key": api_key,
            "X-Internal-Bot-Secret": BOT_INTERNAL_SECRET,
        }
        if method == "POST":
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(target, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                response_body = response.read()
                content_type = response.headers.get("Content-Type", "application/json; charset=utf-8")
                self._send(response.status, response_body, content_type)
        except urllib.error.HTTPError as error:
            response_body = error.read() or _json_bytes({"success": False, "error": "bot_error"})
            content_type = error.headers.get("Content-Type", "application/json; charset=utf-8")
            self._send(error.code, response_body, content_type)
        except (urllib.error.URLError, TimeoutError, OSError):
            self._error(502, "delivery_service_unavailable", "The delivery service is temporarily unavailable. Retry with the same client_order_id.")


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "80"))
    print("Starting Wasmer reseller gateway")
    ThreadingHTTPServer((host, port), GatewayHandler).serve_forever()

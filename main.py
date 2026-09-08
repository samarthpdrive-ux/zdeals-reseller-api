
"""
ZDeals Bot - Wasmer Reseller API Gateway

PUBLIC:
    /api/v1/me
    /api/v1/products
    /api/v1/order
    /api/v1/orders
    /api/v1/order/{order_id}

COMPATIBILITY:
    /api/reseller?action=products
    /api/reseller?action=balance
    /api/reseller?action=orders
    /api/reseller?action=order

The Wasmer gateway does NOT access the database directly.

Architecture:

    Client
       |
       | HTTPS
       v
    Wasmer
       |
       | X-API-Key
       | X-Internal-Bot-Secret
       v
    Render FastAPI
       |
       v
    Database / Telegram / Delivery

Environment variables required on Wasmer:

    BOT_INTERNAL_URL
    BOT_INTERNAL_SECRET

Example:

    BOT_INTERNAL_URL=https://your-render-app.onrender.com
    BOT_INTERNAL_SECRET=your_internal_secret

IMPORTANT:
    Never put BOT_INTERNAL_SECRET in client code.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


# ============================================================
# CONFIGURATION
# ============================================================

BOT_INTERNAL_URL = (
    os.environ.get(
        "BOT_INTERNAL_URL",
        "",
    )
    .strip()
    .rstrip("/")
)

BOT_INTERNAL_SECRET = (
    os.environ.get(
        "BOT_INTERNAL_SECRET",
        "",
    )
    .strip()
)

HOST = os.environ.get(
    "HOST",
    "0.0.0.0",
)

PORT = int(
    os.environ.get(
        "PORT",
        "80",
    )
)

MAX_BODY_BYTES = 64 * 1024


# ============================================================
# ROUTES
# ============================================================

PUBLIC_V1_ORDER_PATH = re.compile(
    r"^/api/v1/order/[1-9][0-9]*$"
)

PUBLIC_ORDER_PATH = re.compile(
    r"^/api/reseller/orders/([1-9][0-9]*)$"
)


def configured() -> bool:
    return bool(
        BOT_INTERNAL_URL
        and BOT_INTERNAL_SECRET
    )


def json_bytes(data: dict) -> bytes:
    return json.dumps(
        data,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


# ============================================================
# HANDLER
# ============================================================

class GatewayHandler(
    BaseHTTPRequestHandler
):

    server_version = (
        "ZDealsBotResellerAPI/2.0"
    )

    # --------------------------------------------------------
    # Logging
    # --------------------------------------------------------

    def log_message(
        self,
        format: str,
        *args: object,
    ) -> None:

        # Never log Authorization,
        # API keys, or request bodies.

        print(
            "gateway",
            self.command,
            self.path.split("?", 1)[0],
            args[1] if len(args) > 1 else "",
        )

    # --------------------------------------------------------
    # Response
    # --------------------------------------------------------

    def send_json(
        self,
        status: int,
        data: dict,
    ) -> None:

        self.send_response(status)

        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8",
        )

        body = json_bytes(data)

        self.send_header(
            "Content-Length",
            str(len(body)),
        )

        self.send_header(
            "Cache-Control",
            "no-store",
        )

        self.send_header(
            "X-Content-Type-Options",
            "nosniff",
        )

        self.send_header(
            "Referrer-Policy",
            "no-referrer",
        )

        self.end_headers()

        self.wfile.write(body)

    # --------------------------------------------------------
    # Error
    # --------------------------------------------------------

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

    # ========================================================
    # AUTHENTICATION
    # ========================================================

    def get_api_key(self) -> str:

        # Preferred:
        #
        # Authorization: Bearer AK_xxxxx
        #

        authorization = (
            self.headers
            .get("Authorization", "")
            .strip()
        )

        if authorization:

            if authorization.lower().startswith(
                "bearer "
            ):

                return authorization[7:].strip()

            # Also accept:
            #
            # Authorization: AK_xxxxx
            #

            return authorization

        # Compatibility:
        #
        # X-API-Key: AK_xxxxx
        #

        return (
            self.headers
            .get("X-API-Key", "")
            .strip()
        )

    # ========================================================
    # BODY
    # ========================================================

    def read_body(self) -> bytes | None:

        raw_length = self.headers.get(
            "Content-Length",
            "0",
        )

        try:

            length = int(
                raw_length
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

        return self.rfile.read(
            length
        )

    # ========================================================
    # FORWARD TO RENDER
    # ========================================================

    def forward(
        self,
        method: str,
        internal_path: str,
        api_key: str,
        body: bytes | None = None,
        query: str = "",
    ) -> None:

        if not configured():

            self.error(
                503,
                "gateway_not_configured",
                (
                    "Wasmer gateway is not configured. "
                    "Set BOT_INTERNAL_URL and "
                    "BOT_INTERNAL_SECRET."
                ),
            )

            return

        target = (
            BOT_INTERNAL_URL
            + internal_path
        )

        if query:

            target += "?" + query

        headers = {
            "Accept": (
                "application/json"
            ),

            "X-API-Key": api_key,

            "X-Internal-Bot-Secret":
                BOT_INTERNAL_SECRET,
        }

        if body is not None:

            headers[
                "Content-Type"
            ] = "application/json"

        request = urllib.request.Request(
            target,
            data=body,
            headers=headers,
            method=method,
        )

        try:

            with urllib.request.urlopen(
                request,
                timeout=30,
            ) as response:

                response_body = (
                    response.read()
                )

                content_type = (
                    response.headers.get(
                        "Content-Type",
                        "application/json; charset=utf-8",
                    )
                )

                self.send_response(
                    response.status
                )

                self.send_header(
                    "Content-Type",
                    content_type,
                )

                self.send_header(
                    "Content-Length",
                    str(len(response_body)),
                )

                self.send_header(
                    "Cache-Control",
                    "no-store",
                )

                self.end_headers()

                self.wfile.write(
                    response_body
                )

        except urllib.error.HTTPError as error:

            try:

                response_body = (
                    error.read()
                )

            except Exception:

                response_body = b""

            if not response_body:

                response_body = json_bytes(
                    {
                        "success": False,
                        "error": "bot_error",
                        "message": (
                            "The backend returned "
                            "an HTTP error."
                        ),
                    }
                )

            self.send_response(
                error.code
            )

            self.send_header(
                "Content-Type",
                "application/json; charset=utf-8",
            )

            self.send_header(
                "Content-Length",
                str(len(response_body)),
            )

            self.end_headers()

            self.wfile.write(
                response_body
            )

        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
        ) as error:

            print(
                "Backend connection error:",
                repr(error),
            )

            self.error(
                502,
                "delivery_service_unavailable",
                (
                    "The Render delivery service "
                    "could not be reached."
                ),
            )

    # ========================================================
    # PUBLIC V1 ROUTING
    # ========================================================

    def handle_v1(
        self,
        method: str,
    ) -> None:

        parsed = urlsplit(
            self.path
        )

        path = parsed.path

        # ----------------------------------------------------
        # Allowed GET
        # ----------------------------------------------------

        if method == "GET":

            if path == "/api/v1/products":

                self.forward(
                    "GET",
                    "/internal/v1/products",
                    self.get_api_key(),
                    query=parsed.query,
                )

                return

            if path == "/api/v1/me":

                self.forward(
                    "GET",
                    "/internal/v1/me",
                    self.get_api_key(),
                    query=parsed.query,
                )

                return

            if path == "/api/v1/orders":

                self.forward(
                    "GET",
                    "/internal/v1/orders",
                    self.get_api_key(),
                    query=parsed.query,
                )

                return

            match = (
                PUBLIC_V1_ORDER_PATH.fullmatch(
                    path
                )
            )

            if match:

                order_id = match.group(
                    0
                ).rsplit(
                    "/",
                    1,
                )[-1]

                self.forward(
                    "GET",
                    f"/internal/v1/order/{order_id}",
                    self.get_api_key(),
                    query=parsed.query,
                )

                return

            self.error(
                404,
                "not_found",
                "Endpoint not found.",
            )

            return

        # ----------------------------------------------------
        # POST ORDER
        # ----------------------------------------------------

        if method == "POST" and path == "/api/v1/order":

            api_key = self.get_api_key()

            if not api_key:

                self.error(
                    401,
                    "missing_api_key",
                    (
                        "Provide your API key using "
                        "Authorization: Bearer <API_KEY>."
                    ),
                )

                return

            body = self.read_body()

            if body is None:

                return

            try:

                data = json.loads(
                    body.decode("utf-8")
                )

            except (
                UnicodeDecodeError,
                json.JSONDecodeError,
            ):

                self.error(
                    400,
                    "invalid_json",
                    "Request body must contain valid JSON.",
                )

                return

            # ------------------------------------------------
            # Accept BOTH naming conventions
            #
            # New:
            #
            # service_id
            # client_order_id
            #
            # Old:
            #
            # product_id
            # external_order_id
            # ------------------------------------------------

            service_id = data.get(
                "service_id"
            )

            if service_id is None:

                service_id = data.get(
                    "product_id"
                )

            client_order_id = data.get(
                "client_order_id"
            )

            if client_order_id is None:

                client_order_id = data.get(
                    "external_order_id"
                )

            quantity = data.get(
                "quantity",
                1,
            )

            delivery_telegram_id = data.get(
                "delivery_telegram_id"
            )

            try:

                service_id = int(
                    service_id
                )

                quantity = int(
                    quantity
                )

                if delivery_telegram_id is not None:

                    delivery_telegram_id = int(
                        delivery_telegram_id
                    )

            except (
                TypeError,
                ValueError,
            ):

                self.error(
                    400,
                    "invalid_order",
                    (
                        "service_id/product_id and "
                        "quantity must be integers."
                    ),
                )

                return

            if service_id <= 0:

                self.error(
                    400,
                    "invalid_order",
                    "service_id must be greater than zero.",
                )

                return

            if quantity <= 0:

                self.error(
                    400,
                    "invalid_order",
                    "quantity must be greater than zero.",
                )

                return

            if not client_order_id:

                self.error(
                    400,
                    "invalid_order",
                    (
                        "client_order_id or "
                        "external_order_id is required."
                    ),
                )

                return

            client_order_id = str(
                client_order_id
            )

            if len(
                client_order_id
            ) > 80:

                self.error(
                    400,
                    "invalid_order",
                    "client_order_id is too long.",
                )

                return

            if delivery_telegram_id is not None:

                if delivery_telegram_id <= 0:

                    self.error(
                        400,
                        "invalid_order",
                        (
                            "delivery_telegram_id "
                            "must be greater than zero."
                        ),
                    )

                    return

            bot_payload = json_bytes(
                {
                    "service_id": service_id,
                    "quantity": quantity,
                    "client_order_id": (
                        client_order_id
                    ),
                    "delivery_telegram_id": (
                        delivery_telegram_id
                    ),
                }
            )

            self.forward(
                "POST",
                "/internal/v1/order",
                api_key,
                bot_payload,
            )

            return

        self.error(
            404,
            "not_found",
            "Endpoint not found.",
        )

    # ========================================================
    # LEGACY /api/reseller COMPATIBILITY
    # ========================================================

    def handle_legacy(
        self,
        method: str,
    ) -> None:

        parsed = urlsplit(
            self.path
        )

        api_key = self.get_api_key()

        if not api_key:

            self.error(
                401,
                "missing_api_key",
                (
                    "Provide your API key using "
                    "Authorization: Bearer <API_KEY>."
                ),
            )

            return

        action = (
            parse_qs(
                parsed.query
            )
            .get(
                "action",
                [""],
            )[0]
            .lower()
        )

        # ----------------------------------------------------
        # GET
        # ----------------------------------------------------

        if method == "GET":

            if action == "products":

                self.forward(
                    "GET",
                    "/internal/v1/products",
                    api_key,
                )

                return

            if action == "balance":

                self.forward(
                    "GET",
                    "/internal/v1/me",
                    api_key,
                )

                return

            if action == "orders":

                self.forward(
                    "GET",
                    "/internal/v1/orders",
                    api_key,
                )

                return

            if action == "order":

                self.error(
                    400,
                    "invalid_request",
                    (
                        "Use "
                        "/api/reseller/orders/{order_id} "
                        "for a single order."
                    ),
                )

                return

            self.error(
                404,
                "invalid_action",
                (
                    "Use action=products, "
                    "balance, or orders."
                ),
            )

            return

        # ----------------------------------------------------
        # POST ORDER
        # ----------------------------------------------------

        if method == "POST" and action == "order":

            # Redirect the old endpoint into
            # the new order implementation.

            body = self.read_body()

            if body is None:

                return

            try:

                data = json.loads(
                    body.decode("utf-8")
                )

            except (
                UnicodeDecodeError,
                json.JSONDecodeError,
            ):

                self.error(
                    400,
                    "invalid_json",
                    "Request body must contain valid JSON.",
                )

                return

            service_id = data.get(
                "service_id"
            )

            if service_id is None:

                service_id = data.get(
                    "product_id"
                )

            client_order_id = data.get(
                "client_order_id"
            )

            if client_order_id is None:

                client_order_id = data.get(
                    "external_order_id"
                )

            try:

                service_id = int(
                    service_id
                )

                quantity = int(
                    data.get(
                        "quantity",
                        1,
                    )
                )

            except (
                TypeError,
                ValueError,
            ):

                self.error(
                    400,
                    "invalid_order",
                    (
                        "Send product_id/service_id "
                        "and quantity."
                    ),
                )

                return

            if not client_order_id:

                self.error(
                    400,
                    "invalid_order",
                    (
                        "external_order_id or "
                        "client_order_id is required."
                    ),
                )

                return

            delivery_telegram_id = (
                data.get(
                    "delivery_telegram_id"
                )
            )

            if delivery_telegram_id is not None:

                try:

                    delivery_telegram_id = int(
                        delivery_telegram_id
                    )

                except ValueError:

                    self.error(
                        400,
                        "invalid_order",
                        "Invalid delivery_telegram_id.",
                    )

                    return

            bot_payload = json_bytes(
                {
                    "service_id": service_id,
                    "quantity": quantity,
                    "client_order_id": str(
                        client_order_id
                    ),
                    "delivery_telegram_id": (
                        delivery_telegram_id
                    ),
                }
            )

            self.forward(
                "POST",
                "/internal/v1/order",
                api_key,
                bot_payload,
            )

            return

        self.error(
            404,
            "invalid_action",
            "Invalid reseller action.",
        )

    # ========================================================
    # GET
    # ========================================================

    def do_GET(self) -> None:

        parsed = urlsplit(
            self.path
        )

        path = parsed.path

        # ----------------------------------------------------
        # Health
        # ----------------------------------------------------

        if path == "/health":

            self.send_json(
                200,
                {
                    "status": "ok",
                    "service": (
                        "ZDeals Bot Wasmer "
                        "Reseller API"
                    ),
                    "version": "2.0",
                },
            )

            return

        # ----------------------------------------------------
        # Root
        # ----------------------------------------------------

        if path == "/":

            self.home()

            return

        # ----------------------------------------------------
        # Docs
        # ----------------------------------------------------

        if path == "/docs":

            self.docs()

            return

        # ----------------------------------------------------
        # OpenAPI
        # ----------------------------------------------------

        if path == "/openapi.json":

            self.send_json(
                200,
                self.openapi_spec(),
            )

            return

        # ----------------------------------------------------
        # New API
        # ----------------------------------------------------

        if path.startswith(
            "/api/v1/"
        ):

            self.handle_v1(
                "GET"
            )

            return

        # ----------------------------------------------------
        # Legacy API
        # ----------------------------------------------------

        if path == "/api/reseller":

            self.handle_legacy(
                "GET"
            )

            return

        if path in {
            "/api/reseller/products",
            "/api/reseller/balance",
            "/api/reseller/orders",
        }:

            action = {
                "/api/reseller/products":
                    "products",

                "/api/reseller/balance":
                    "balance",

                "/api/reseller/orders":
                    "orders",
            }[path]

            fake_query = (
                f"/api/reseller"
                f"?action={action}"
            )

            original = self.path

            self.path = fake_query

            try:

                self.handle_legacy(
                    "GET"
                )

            finally:

                self.path = original

            return

        match = PUBLIC_ORDER_PATH.fullmatch(
            path
        )

        if match:

            order_id = match.group(
                1
            )

            self.forward(
                "GET",
                f"/internal/v1/order/{order_id}",
                self.get_api_key(),
                query=parsed.query,
            )

            return

        self.error(
            404,
            "not_found",
            "Endpoint not found.",
        )

    # ========================================================
    # POST
    # ========================================================

    def do_POST(self) -> None:

        parsed = urlsplit(
            self.path
        )

        path = parsed.path

        if path == "/api/v1/order":

            self.handle_v1(
                "POST"
            )

            return

        if path == "/api/reseller":

            self.handle_legacy(
                "POST"
            )

            return

        if path == "/api/reseller/order":

            self.handle_legacy(
                "POST"
            )

            return

        self.error(
            404,
            "not_found",
            "Endpoint not found.",
        )

    # ========================================================
    # OPTIONS
    # ========================================================

    def do_OPTIONS(self) -> None:

        self.send_response(
            HTTPStatus.NO_CONTENT
        )

        self.send_header(
            "Access-Control-Allow-Origin",
            "*",
        )

        self.send_header(
            "Access-Control-Allow-Methods",
            "GET, POST, OPTIONS",
        )

        self.send_header(
            "Access-Control-Allow-Headers",
            (
                "Authorization, "
                "Content-Type, "
                "X-API-Key"
            ),
        )

        self.send_header(
            "Access-Control-Max-Age",
            "600",
        )

        self.end_headers()

    # ========================================================
    # HOME
    # ========================================================

    def home(self) -> None:

        page = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>ZDeals Bot Developer API</title>
<meta name="viewport"
      content="width=device-width, initial-scale=1">
<style>
body {
    font-family: Arial, sans-serif;
    max-width: 900px;
    margin: 50px auto;
    padding: 20px;
    line-height: 1.6;
}
code {
    background: #f1f1f1;
    padding: 3px 6px;
    border-radius: 4px;
}
</style>
</head>
<body>

<h1>ZDeals Bot Developer API</h1>

<p>
Wasmer public reseller API gateway.
</p>

<h2>API</h2>

<ul>
<li><code>GET /health</code></li>
<li><code>GET /api/v1/me</code></li>
<li><code>GET /api/v1/products</code></li>
<li><code>GET /api/v1/orders</code></li>
<li><code>GET /api/v1/order/{order_id}</code></li>
<li><code>POST /api/v1/order</code></li>
</ul>

<p>
Open <a href="/docs">/docs</a>
for interactive API documentation.
</p>

</body>
</html>
"""

        self.send_response(
            200
        )

        self.send_header(
            "Content-Type",
            "text/html; charset=utf-8",
        )

        body = page.encode(
            "utf-8"
        )

        self.send_header(
            "Content-Length",
            str(len(body)),
        )

        self.end_headers()

        self.wfile.write(
            body
        )

    # ========================================================
    # DOCS
    # ========================================================

    def docs(self) -> None:

        page = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport"
      content="width=device-width, initial-scale=1">

<title>ZDeals Bot API</title>

<link
rel="stylesheet"
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
"""

        self.send_response(
            200
        )

        self.send_header(
            "Content-Type",
            "text/html; charset=utf-8",
        )

        body = page.encode(
            "utf-8"
        )

        self.send_header(
            "Content-Length",
            str(len(body)),
        )

        self.end_headers()

        self.wfile.write(
            body
        )

    # ========================================================
    # OPENAPI
    # ========================================================

    def openapi_spec(self) -> dict:

        return {

            "openapi": "3.0.3",

            "info": {
                "title":
                    "ZDeals Bot Developer API",

                "version":
                    "2.0.0",

                "description": (
                    "ZDeals Bot reseller API. "
                    "Use Authorization: Bearer "
                    "YOUR_API_KEY."
                ),
            },

            "servers": [
                {
                    "url": "/",
                }
            ],

            "components": {

                "securitySchemes": {

                    "ApiKey": {

                        "type":
                            "http",

                        "scheme":
                            "bearer",

                        "bearerFormat":
                            "AK_xxxxxxxxx",
                    }
                },

                "schemas": {

                    "Error": {

                        "type":
                            "object",

                        "properties": {

                            "success": {
                                "type":
                                    "boolean"
                            },

                            "error": {
                                "type":
                                    "string"
                            },

                            "message": {
                                "type":
                                    "string"
                            },
                        },
                    },

                    "OrderRequest": {

                        "type":
                            "object",

                        "required": [
                            "service_id",
                            "quantity",
                            "client_order_id",
                        ],

                        "properties": {

                            "service_id": {
                                "type":
                                    "integer",

                                "example":
                                    1,
                            },

                            "quantity": {
                                "type":
                                    "integer",

                                "minimum":
                                    1,

                                "maximum":
                                    100,

                                "example":
                                    1,
                            },

                            "client_order_id": {
                                "type":
                                    "string",

                                "example":
                                    "TEST-10001",
                            },

                            "delivery_telegram_id": {
                                "type":
                                    "integer",

                                "nullable":
                                    True,

                                "example":
                                    123456789,
                            },
                        },
                    },
                },
            },

            "paths": {

                "/health": {

                    "get": {

                        "summary":
                            "Health check",

                        "responses": {

                            "200": {
                                "description":
                                    "OK"
                            }
                        },
                    }
                },

                "/api/v1/me": {

                    "get": {

                        "summary":
                            "Get wallet/account",

                        "security": [
                            {
                                "ApiKey": []
                            }
                        ],

                        "responses": {

                            "200": {
                                "description":
                                    "Account"
                            },

                            "401": {
                                "description":
                                    "Invalid API key"
                            },
                        },
                    }
                },

                "/api/v1/products": {

                    "get": {

                        "summary":
                            "List products",

                        "security": [
                            {
                                "ApiKey": []
                            }
                        ],

                        "responses": {

                            "200": {
                                "description":
                                    "Products"
                            },

                            "401": {
                                "description":
                                    "Invalid API key"
                            },
                        },
                    }
                },

                "/api/v1/orders": {

                    "get": {

                        "summary":
                            "List orders",

                        "security": [
                            {
                                "ApiKey": []
                            }
                        ],

                        "responses": {

                            "200": {
                                "description":
                                    "Orders"
                            }
                        },
                    }
                },

                "/api/v1/order/{order_id}": {

                    "get": {

                        "summary":
                            "Get order",

                        "security": [
                            {
                                "ApiKey": []
                            }
                        ],

                        "parameters": [

                            {
                                "name":
                                    "order_id",

                                "in":
                                    "path",

                                "required":
                                    True,

                                "schema": {
                                    "type":
                                        "integer"
                                },
                            }
                        ],

                        "responses": {

                            "200": {
                                "description":
                                    "Order"
                            },

                            "404": {
                                "description":
                                    "Order not found"
                            },
                        },
                    }
                },

                "/api/v1/order": {

                    "post": {

                        "summary":
                            "Create order",

                        "security": [
                            {
                                "ApiKey": []
                            }
                        ],

                        "requestBody": {

                            "required":
                                True,

                            "content": {

                                "application/json": {

                                    "schema": {
                                        "$ref":
                                            "#/components/schemas/OrderRequest"
                                    }
                                }
                            },
                        },

                        "responses": {

                            "200": {
                                "description":
                                    "Order created"
                            },

                            "400": {
                                "description":
                                    "Invalid order"
                            },

                            "401": {
                                "description":
                                    "Invalid API key"
                            },

                            "404": {
                                "description":
                                    "Product not found"
                            },
                        },
                    }
                },
            },
        }


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    print(
        "=================================================="
    )

    print(
        "ZDeals Bot Wasmer Reseller API"
    )

    print(
        "=================================================="
    )

    print(
        f"Host: {HOST}"
    )

    print(
        f"Port: {PORT}"
    )

    print(
        "Backend configured:",
        configured(),
    )

    if BOT_INTERNAL_URL:

        print(
            "Backend:",
            BOT_INTERNAL_URL,
        )

    else:

        print(
            "WARNING: BOT_INTERNAL_URL is missing"
        )

    if not BOT_INTERNAL_SECRET:

        print(
            "WARNING: BOT_INTERNAL_SECRET is missing"
        )

    print(
        "=================================================="
    )

    server = ThreadingHTTPServer(
        (
            HOST,
            PORT,
        ),
        GatewayHandler,
    )

    server.serve_forever()

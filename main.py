from __future__ import annotations

import json
import os
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import requests


# ============================================================
# CONFIGURATION
# ============================================================

# Render backend.
#
# IMPORTANT:
# This must be the Render service URL only.
#
# Correct:
# https://zdealsbot.onrender.com
#
# Wrong:
# https://zdealsbot.onrender.com/health
# https://zdealsbot.onrender.com/api/v1
# http://127.0.0.1:10000
#
BOT_URL = os.getenv(
    "BOT_INTERNAL_URL",
    "https://zdealsbot.onrender.com",
).rstrip("/")


# Wasmer listens on PORT.
PORT = int(
    os.getenv(
        "PORT",
        "8080",
    )
)


# ============================================================
# ENVIRONMENT / INTERNAL SECRET
# ============================================================

def get_env_value(name: str) -> str:
    """
    Read an environment variable.

    Wasmer normally provides environment variables directly.
    The .env fallback is useful when running this file locally.
    """

    value = os.getenv(name)

    if value:
        return value.strip().strip('"').strip("'")

    project_dir = Path(
        os.getenv(
            "PROJECT_DIR",
            ".",
        )
    )

    env_file = project_dir / ".env"

    if not env_file.exists():
        return ""

    try:
        for line in env_file.read_text(
            encoding="utf-8"
        ).splitlines():

            line = line.strip()

            if not line:
                continue

            if line.startswith("#"):
                continue

            if "=" not in line:
                continue

            key, value = line.split(
                "=",
                1,
            )

            if key.strip() == name:
                return (
                    value
                    .strip()
                    .strip('"')
                    .strip("'")
                )

    except Exception:
        return ""

    return ""


INTERNAL_SECRET = get_env_value(
    "BOT_INTERNAL_SECRET"
)

# Backward-compatible fallback.
if not INTERNAL_SECRET:
    INTERNAL_SECRET = get_env_value(
        "INTERNAL_API_SECRET"
    )


# ============================================================
# STARTUP VALIDATION
# ============================================================

print("=" * 60)
print("ZDeals Reseller API - Wasmer Gateway")
print("=" * 60)
print(
    "Render backend:",
    BOT_URL,
)
print(
    "Port:",
    PORT,
)

if not INTERNAL_SECRET:
    print(
        "WARNING: BOT_INTERNAL_SECRET is not configured."
    )
    print(
        "The gateway will still start."
    )
else:
    print(
        "Internal secret: configured"
    )

print("=" * 60)


# ============================================================
# RENDER ROUTES
# ============================================================
#
# Your currently deployed Render application exposes these:
#
# GET  /api/v1/products
# GET  /api/v1/me
# GET  /api/v1/orders
# POST /api/v1/order
#
# Therefore Wasmer must call these paths.
#
# ============================================================

RENDER_ROUTES = {
    "products": "/api/v1/products",
    "balance": "/api/v1/me",
    "orders": "/api/v1/orders",
    "order": "/api/v1/order",
}


# ============================================================
# HTTP SESSION
# ============================================================

SESSION = requests.Session()

SESSION.headers.update(
    {
        "User-Agent": "ZDeals-Reseller-Gateway/2.0",
        "Accept": "application/json",
    }
)


# ============================================================
# GATEWAY
# ============================================================

class Gateway(BaseHTTPRequestHandler):

    # --------------------------------------------------------
    # SERVER INFORMATION
    # --------------------------------------------------------

    server_version = "ZDealsResellerGateway/2.0"

    # --------------------------------------------------------
    # LOGGING
    # --------------------------------------------------------

    def log_message(
        self,
        format_string,
        *args,
    ):
        print(
            "%s - %s"
            % (
                self.address_string(),
                format_string % args,
            ),
            flush=True,
        )

    # --------------------------------------------------------
    # JSON RESPONSE
    # --------------------------------------------------------

    def send_json(
        self,
        status: int,
        data,
    ):
        try:
            body = json.dumps(
                data,
                ensure_ascii=False,
            ).encode("utf-8")

        except Exception:
            body = json.dumps(
                {
                    "success": False,
                    "error": "response_serialization_error",
                }
            ).encode("utf-8")

            status = 500

        self.send_response(status)

        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8",
        )

        self.send_header(
            "Content-Length",
            str(len(body)),
        )

        self.send_header(
            "Cache-Control",
            "no-store",
        )

        self.end_headers()

        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    # --------------------------------------------------------
    # API KEY
    # --------------------------------------------------------

    def get_api_key(self) -> str:

        authorization = self.headers.get(
            "Authorization",
            "",
        ).strip()

        if not authorization:
            return ""

        # Expected:
        #
        # Authorization: Bearer YOUR_API_KEY
        #

        if authorization.lower().startswith(
            "bearer "
        ):
            return authorization[7:].strip()

        # Also allow direct Authorization values.
        return authorization

    # --------------------------------------------------------
    # INTERNAL HEADERS
    # --------------------------------------------------------

    def backend_headers(
        self,
        api_key: str,
    ) -> dict:

        headers = {
            "Accept": "application/json",
            "X-API-Key": api_key,
        }

        # Keep sending the secret.
        #
        # If the current Render /api/v1 router does not use it,
        # it is simply ignored.
        #
        # It also allows you to secure the backend later without
        # changing the Wasmer client.
        if INTERNAL_SECRET:
            headers[
                "X-Internal-Bot-Secret"
            ] = INTERNAL_SECRET

        return headers

    # --------------------------------------------------------
    # FORWARD GET
    # --------------------------------------------------------

    def forward_get(
        self,
        path: str,
        api_key: str,
    ):

        url = BOT_URL + path

        print(
            f"GET {url}",
            flush=True,
        )

        try:

            response = SESSION.get(
                url,
                headers=self.backend_headers(
                    api_key
                ),
                timeout=60,
            )

            print(
                f"Render response: {response.status_code}",
                flush=True,
            )

            return response

        except requests.exceptions.Timeout as error:

            print(
                "Render request timeout:",
                repr(error),
                flush=True,
            )

            return None

        except requests.exceptions.RequestException as error:

            print(
                "Render connection error:",
                repr(error),
                flush=True,
            )

            return None

        except Exception as error:

            print(
                "Unexpected backend error:",
                repr(error),
                flush=True,
            )

            traceback.print_exc()

            return None

    # --------------------------------------------------------
    # FORWARD POST
    # --------------------------------------------------------

    def forward_post(
        self,
        path: str,
        api_key: str,
        payload: dict,
    ):

        url = BOT_URL + path

        print(
            f"POST {url}",
            flush=True,
        )

        try:

            response = SESSION.post(
                url,
                headers=self.backend_headers(
                    api_key
                ),
                json=payload,
                timeout=60,
            )

            print(
                f"Render response: {response.status_code}",
                flush=True,
            )

            return response

        except requests.exceptions.Timeout as error:

            print(
                "Render request timeout:",
                repr(error),
                flush=True,
            )

            return None

        except requests.exceptions.RequestException as error:

            print(
                "Render connection error:",
                repr(error),
                flush=True,
            )

            return None

        except Exception as error:

            print(
                "Unexpected backend error:",
                repr(error),
                flush=True,
            )

            traceback.print_exc()

            return None

    # --------------------------------------------------------
    # RESPONSE FROM RENDER
    # --------------------------------------------------------

    def return_backend_response(
        self,
        response,
    ):

        if response is None:

            self.send_json(
                502,
                {
                    "success": False,
                    "error": "delivery_service_unavailable",
                    "message": (
                        "The Render delivery service "
                        "could not be reached."
                    ),
                },
            )

            return

        content_type = response.headers.get(
            "Content-Type",
            "",
        ).lower()

        try:

            if (
                "application/json"
                in content_type
            ):
                data = response.json()

            else:
                data = {
                    "success": response.ok,
                    "message": response.text,
                }

        except Exception:

            data = {
                "success": response.ok,
                "message": response.text,
            }

        self.send_json(
            response.status_code,
            data,
        )

    # ========================================================
    # GET
    # ========================================================

    def do_GET(self):

        parsed = urlsplit(
            self.path
        )

        path = parsed.path

        query = parse_qs(
            parsed.query
        )

        # ----------------------------------------------------
        # HEALTH
        # ----------------------------------------------------

        if path == "/health":

            self.send_json(
                200,
                {
                    "status": "ok",
                    "service": (
                        "ZDeals Reseller API Gateway"
                    ),
                    "backend": BOT_URL,
                },
            )

            return

        # ----------------------------------------------------
        # ROOT
        # ----------------------------------------------------

        if path == "/":

            self.send_json(
                200,
                {
                    "status": "ok",
                    "service": (
                        "ZDeals Reseller API Gateway"
                    ),
                    "version": "2.0",
                },
            )

            return

        # ----------------------------------------------------
        # PUBLIC PRODUCTS
        #
        # /api/v1/products
        #
        # ----------------------------------------------------

        if path == "/api/v1/products":

            api_key = self.get_api_key()

            if not api_key:

                self.send_json(
                    401,
                    {
                        "success": False,
                        "error": "missing_api_key",
                        "message": (
                            "Authorization header is required."
                        ),
                    },
                )

                return

            response = self.forward_get(
                RENDER_ROUTES["products"],
                api_key,
            )

            self.return_backend_response(
                response
            )

            return

        # ----------------------------------------------------
        # PUBLIC BALANCE
        #
        # /api/v1/me
        #
        # ----------------------------------------------------

        if path == "/api/v1/me":

            api_key = self.get_api_key()

            if not api_key:

                self.send_json(
                    401,
                    {
                        "success": False,
                        "error": "missing_api_key",
                        "message": (
                            "Authorization header is required."
                        ),
                    },
                )

                return

            response = self.forward_get(
                RENDER_ROUTES["balance"],
                api_key,
            )

            self.return_backend_response(
                response
            )

            return

        # ----------------------------------------------------
        # PUBLIC ORDERS
        #
        # /api/v1/orders
        #
        # ----------------------------------------------------

        if path == "/api/v1/orders":

            api_key = self.get_api_key()

            if not api_key:

                self.send_json(
                    401,
                    {
                        "success": False,
                        "error": "missing_api_key",
                        "message": (
                            "Authorization header is required."
                        ),
                    },
                )

                return

            response = self.forward_get(
                RENDER_ROUTES["orders"],
                api_key,
            )

            self.return_backend_response(
                response
            )

            return

        # ----------------------------------------------------
        # SINGLE ORDER
        #
        # /api/v1/order/{order_id}
        #
        # ----------------------------------------------------

        if path.startswith(
            "/api/v1/order/"
        ):

            api_key = self.get_api_key()

            if not api_key:

                self.send_json(
                    401,
                    {
                        "success": False,
                        "error": "missing_api_key",
                    },
                )

                return

            order_id = path.split(
                "/api/v1/order/",
                1,
            )[1].strip()

            if not order_id:

                self.send_json(
                    400,
                    {
                        "success": False,
                        "error": "missing_order_id",
                    },
                )

                return

            backend_path = (
                "/api/v1/order/"
                + order_id
            )

            response = self.forward_get(
                backend_path,
                api_key,
            )

            self.return_backend_response(
                response
            )

            return

        # ----------------------------------------------------
        # LEGACY ACTION ROUTING
        #
        # Keep support for:
        #
        # /api/reseller?action=products
        # /api/reseller?action=balance
        # /api/reseller?action=orders
        #
        # ----------------------------------------------------

        if path == "/api/reseller":

            api_key = self.get_api_key()

            if not api_key:

                self.send_json(
                    401,
                    {
                        "success": False,
                        "error": "missing_api_key",
                    },
                )

                return

            action = query.get(
                "action",
                [""],
            )[0]

            route = RENDER_ROUTES.get(
                action
            )

            if not route:

                self.send_json(
                    404,
                    {
                        "success": False,
                        "error": "invalid_action",
                    },
                )

                return

            response = self.forward_get(
                route,
                api_key,
            )

            self.return_backend_response(
                response
            )

            return

        # ----------------------------------------------------
        # NOT FOUND
        # ----------------------------------------------------

        self.send_json(
            404,
            {
                "success": False,
                "error": "not_found",
                "path": path,
            },
        )

    # ========================================================
    # POST
    # ========================================================

    def do_POST(self):

        parsed = urlsplit(
            self.path
        )

        path = parsed.path

        query = parse_qs(
            parsed.query
        )

        # ----------------------------------------------------
        # API KEY
        # ----------------------------------------------------

        api_key = self.get_api_key()

        if not api_key:

            self.send_json(
                401,
                {
                    "success": False,
                    "error": "missing_api_key",
                    "message": (
                        "Authorization header is required."
                    ),
                },
            )

            return

        # ----------------------------------------------------
        # READ BODY
        # ----------------------------------------------------

        try:

            content_length = int(
                self.headers.get(
                    "Content-Length",
                    "0",
                )
            )

        except ValueError:

            content_length = 0

        if content_length <= 0:

            self.send_json(
                400,
                {
                    "success": False,
                    "error": "empty_request_body",
                },
            )

            return

        try:

            raw_body = self.rfile.read(
                content_length
            )

            data = json.loads(
                raw_body.decode("utf-8")
            )

            if not isinstance(
                data,
                dict,
            ):
                raise ValueError(
                    "JSON body must be an object"
                )

        except Exception as error:

            self.send_json(
                400,
                {
                    "success": False,
                    "error": "invalid_json",
                    "message": str(error),
                },
            )

            return

        # ----------------------------------------------------
        # NORMALIZE ORDER
        #
        # Public Wasmer API:
        #
        # product_id
        # quantity
        # external_order_id
        # delivery_telegram_id
        #
        # Render API:
        #
        # service_id
        # quantity
        # client_order_id
        # delivery_telegram_id
        #
        # ----------------------------------------------------

        try:

            if "product_id" not in data:

                raise ValueError(
                    "product_id is required"
                )

            if "external_order_id" not in data:

                raise ValueError(
                    "external_order_id is required"
                )

            bot_order = {
                "service_id": int(
                    data["product_id"]
                ),
                "quantity": int(
                    data.get(
                        "quantity",
                        1,
                    )
                ),
                "client_order_id": str(
                    data["external_order_id"]
                ),
            }

            if data.get(
                "delivery_telegram_id"
            ) is not None:

                bot_order[
                    "delivery_telegram_id"
                ] = int(
                    data[
                        "delivery_telegram_id"
                    ]
                )

        except Exception as error:

            self.send_json(
                400,
                {
                    "success": False,
                    "error": "invalid_order",
                    "message": str(error),
                },
            )

            return

        # ----------------------------------------------------
        # ORDER
        #
        # /api/v1/order
        #
        # ----------------------------------------------------

        if path == "/api/v1/order":

            response = self.forward_post(
                RENDER_ROUTES["order"],
                api_key,
                bot_order,
            )

            self.return_backend_response(
                response
            )

            return

        # ----------------------------------------------------
        # LEGACY
        #
        # /api/reseller?action=order
        #
        # ----------------------------------------------------

        if path == "/api/reseller":

            action = query.get(
                "action",
                [""],
            )[0]

            if action != "order":

                self.send_json(
                    404,
                    {
                        "success": False,
                        "error": "invalid_action",
                    },
                )

                return

            response = self.forward_post(
                RENDER_ROUTES["order"],
                api_key,
                bot_order,
            )

            self.return_backend_response(
                response
            )

            return

        # ----------------------------------------------------
        # NOT FOUND
        # ----------------------------------------------------

        self.send_json(
            404,
            {
                "success": False,
                "error": "not_found",
                "path": path,
            },
        )


# ============================================================
# SERVER
# ============================================================

def main():

    server = ThreadingHTTPServer(
        (
            "0.0.0.0",
            PORT,
        ),
        Gateway,
    )

    print(
        "Wasmer gateway started successfully.",
        flush=True,
    )

    print(
        f"Listening on 0.0.0.0:{PORT}",
        flush=True,
    )

    print(
        f"Render backend: {BOT_URL}",
        flush=True,
    )

    print(
        "Products:",
        f"{BOT_URL}/api/v1/products",
        flush=True,
    )

    print(
        "Balance:",
        f"{BOT_URL}/api/v1/me",
        flush=True,
    )

    print(
        "Orders:",
        f"{BOT_URL}/api/v1/orders",
        flush=True,
    )

    print(
        "Order:",
        f"{BOT_URL}/api/v1/order",
        flush=True,
    )

    server.serve_forever()


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()

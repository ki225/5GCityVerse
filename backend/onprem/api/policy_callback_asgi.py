"""Direct HTTP/1 + h2c AF callbacks, preserving real peer identity.

This listener requires pinned Hypercorn dependencies in its deployment image.
It does not trust proxy headers, advertise OAuth, or synthesize PCF callbacks.
"""
import argparse
import asyncio
import hmac
import ipaddress
import json
import os

from backend.onprem.storage.callbacks import CallbackStore, recovery_fence_enabled


def create_app(store, admin_token):
    if len(admin_token) < 32:
        raise ValueError("Management secret required")

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return

        async def reply(code, payload=None):
            data = b"" if payload is None else json.dumps(payload).encode()
            await send({"type": "http.response.start", "status": code,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"content-length", str(len(data)).encode())]})
            await send({"type": "http.response.body", "body": data})

        try:
            path, method = scope.get("path", ""), scope.get("method")
            if scope.get("query_string"):
                return await reply(404)
            if method == "GET" and path == "/health":
                return await reply(200, {"service": "onprem-af-callback", "auth": "internal-management-guard",
                                         "oauthClaimed": False, "transport": "http1-h2c"})
            if method == "GET" and path == "/internal/recovery-fence" and store.recovery_fence:
                peer = (scope.get("client") or ("", 0))[0]
                try:
                    local = ipaddress.ip_address(peer).is_loopback
                except ValueError:
                    local = False
                if not local:
                    return await reply(403)
                return await reply(200, await asyncio.to_thread(store.fence_receipt))
            parts = path.split("/")
            is_admin = len(parts) == 4 and parts[1:3] == ["admin", "actions"]
            is_notification = len(parts) == 4 and parts[1] == "notifications"
            if not is_admin and not is_notification:
                return await reply(404)
            if is_admin:
                authorizations = [v for k, v in scope.get("headers", []) if k.lower() == b"authorization"]
                if len(authorizations) != 1 or not hmac.compare_digest(authorizations[0], ("Bearer " + admin_token).encode()):
                    return await reply(403)
                if method == "GET":
                    record = await asyncio.to_thread(store.read, parts[3])
                    return await reply(200 if record else 404, record)
            if method != "POST":
                return await reply(405)
            if store.recovery_fence:
                return await reply(503)
            if is_notification:
                peer = (scope.get("client") or ("", 0))[0]
                if peer not in store.peers:  # Never use X-Forwarded-For.
                    return await reply(403)
            body = bytearray()
            while True:
                message = await asyncio.wait_for(receive(), timeout=10)
                if message["type"] == "http.disconnect":
                    return
                if message["type"] != "http.request":
                    return await reply(400)
                body.extend(message.get("body", b""))
                if len(body) > 65536:
                    return await reply(413)
                if not message.get("more_body", False):
                    break
            if not body:
                return await reply(413)
            if is_admin:
                value = json.loads(body)
                if not isinstance(value, dict):
                    return await reply(400)
                token = await asyncio.to_thread(store.register, parts[3], value["fingerprint"], value.get("uri"))
                return await reply(200, {"path": "/notifications/" + parts[3] + "/" + token})
            code = await asyncio.to_thread(store.receive, peer, parts[2], parts[3], bytes(body))
            return await reply(code)
        except asyncio.TimeoutError:
            return await reply(408)
        except (ValueError, KeyError, TypeError, UnicodeDecodeError):
            return await reply(400)
        except Exception:
            # Do not log paths or request data: capability tokens are in paths.
            return await reply(503)

    return app


def main():
    from hypercorn.asyncio import serve
    from hypercorn.config import Config
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9082)
    parser.add_argument("--state", default="/state/callback.sqlite")
    args = parser.parse_args()
    store = CallbackStore(args.state, os.environ.get("CITYVERSE_AF_CALLBACK_SECRET", ""),
                          os.environ.get("CITYVERSE_AF_PCF_IPS", "").split(","),
                          os.environ.get("CITYVERSE_AF_PCF_RESOURCE_BASE"),
                          recovery_fence=recovery_fence_enabled(), pod_uid=os.environ.get("CITYVERSE_AF_POD_UID"))
    config = Config()
    config.bind = [f"{args.host}:{args.port}"]
    config.accesslog = None
    config.errorlog = None
    config.h2_max_concurrent_streams = 32
    config.keep_alive_timeout = 5
    config.read_timeout = 10
    asyncio.run(serve(create_app(store, os.environ.get("CITYVERSE_AF_ADMIN_TOKEN", "")), config))


if __name__ == "__main__":
    main()

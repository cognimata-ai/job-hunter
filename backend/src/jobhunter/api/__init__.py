"""The HTTP API (spec §14): `jobhunter.api.app:app` is what uvicorn serves.

Layering, outermost first: `JobHunterApp` → `EdgeMiddleware` (request id, security headers, metrics, the
last-resort 500) → the FastAPI app (Starlette's `ServerErrorMiddleware` → `ExceptionMiddleware` → routes).
"""

"""Cloudflare staging entrypoint for CallPilot.

This deployable Worker is intentionally isolated from the live Vapi/Stripe API
until the persistent FastAPI endpoints have been ported and tested.
"""
from fastapi import FastAPI, Request, Response
from workers import asgi

app=FastAPI(title="CallPilot Cloudflare staging",docs_url=None,redoc_url=None,openapi_url=None)
Default=asgi.entrypoint(app)

@app.get("/health")
async def health():
    return {"status":"ok","environment":"cloudflare-staging",
            "backend_ready":False,"phone_routing_live":False,
            "payments_live":False}


@app.get("/_spike/async-http")
async def spike_async_http():
    import httpx
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r=await c.get("https://api.github.com/zen")
        return {"ok":True,"status":r.status_code}
    except Exception as e:
        return {"ok":False,"error":type(e).__name__+": "+str(e)[:160]}

@app.get("/_spike/fernet")
async def spike_fernet():
    try:
        from cryptography.fernet import Fernet
        k=Fernet.generate_key(); f=Fernet(k)
        return {"ok":f.decrypt(f.encrypt(b"x"))==b"x"}
    except Exception as e:
        return {"ok":False,"error":type(e).__name__+": "+str(e)[:160]}

@app.get("/_spike/sync-endpoint")
def spike_sync_endpoint():
    return {"ok":True}

@app.get("/_spike/sqlite")
async def spike_sqlite():
    try:
        import sqlite3
        c=sqlite3.connect(":memory:"); c.execute("select 1")
        return {"ok":True}
    except Exception as e:
        return {"ok":False,"error":type(e).__name__+": "+str(e)[:160]}

@app.get("/api/{path:path}")
async def api_unavailable(path:str):
    return Response(content='{"detail":"CallPilot production API is not migrated to this staging Worker"}',
                    media_type="application/json",status_code=503)

@app.get("/{path:path}")
async def static_assets(path:str,request:Request):
    asset="/index.html" if not path else "/"+path
    # Status page is deliberately used instead of the live sign-in/dashboard.
    try:
        response=await request.scope["env"].ASSETS.fetch("https://assets.local"+asset)
        return Response(content=await response.bytes(),status_code=response.status,
                        headers=response.headers)
    except Exception:
        return Response(content="Cloudflare staging assets unavailable",status_code=503)

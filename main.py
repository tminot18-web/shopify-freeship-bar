# main.py
import os
import base64
import hmac
import hashlib
import secrets
import urllib.parse
from typing import Dict, Optional
from datetime import datetime

import httpx
from fastapi import FastAPI, Request, HTTPException, Depends, Header
from fastapi.responses import RedirectResponse, Response, HTMLResponse, JSONResponse
from sqlalchemy import create_engine, Column, String, Integer, Boolean, DateTime
from sqlalchemy.orm import sessionmaker, Session, declarative_base

# JWT (session token) verification
import jwt  # PyJWT

app = FastAPI(title="Free Shipping Bar")

# ====== Config / ENV ======
def _get_env(name: str) -> str:
    v = os.getenv(name, "").strip()
    if not v:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return v

CLIENT_ID = _get_env("SHOPIFY_API_KEY")       # also the JWT 'aud'
CLIENT_SECRET = _get_env("SHOPIFY_API_SECRET")
APP_URL = _get_env("APP_URL").rstrip("/")     # e.g., https://xxxx.ngrok-free.app

# Keep OAuth state in-memory (dev only)
STATE_CACHE: Dict[str, str] = {}

API_VERSION = "2025-07"
SCOPES = "write_script_tags,read_script_tags"

# ====== Persistence ======
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./app.db")
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class Shop(Base):
    __tablename__ = "shops"
    shop = Column(String, primary_key=True)      # tim-freeship.myshopify.com
    access_token = Column(String, nullable=False)
    installed_at = Column(DateTime, default=datetime.utcnow)
    uninstalled = Column(Boolean, default=False)

class Settings(Base):
    __tablename__ = "settings"
    shop = Column(String, primary_key=True)
    threshold_cents = Column(Integer, default=5000)  # $50
    banner_text = Column(String, default="You're ${remaining} away from FREE shipping!")  # global fallback
    bg = Column(String, default="#111827")
    fg = Column(String, default="#ffffff")
    position = Column(String, default="top")         # "top" | "bottom"
    top_text = Column(String, nullable=True)         # specific copy for top
    bottom_text = Column(String, nullable=True)      # specific copy for bottom

Base.metadata.create_all(bind=engine)

# idempotent, lightweight migration for SQLite
with engine.connect() as conn:
    try:
        cols = [row[1] for row in conn.exec_driver_sql("PRAGMA table_info(settings);")]
        if "position" not in cols:
            conn.exec_driver_sql("ALTER TABLE settings ADD COLUMN position TEXT DEFAULT 'top';")
        if "top_text" not in cols:
            conn.exec_driver_sql("ALTER TABLE settings ADD COLUMN top_text TEXT")
        if "bottom_text" not in cols:
            conn.exec_driver_sql("ALTER TABLE settings ADD COLUMN bottom_text TEXT")
    except Exception:
        pass

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ====== Helpers ======
def _install_authorize_url(shop: str, state: str) -> str:
    params = {
        "client_id": CLIENT_ID,
        "scope": SCOPES,
        "redirect_uri": f"{APP_URL}/callback",
        "state": state,
    }
    return f"https://{shop}/admin/oauth/authorize?{urllib.parse.urlencode(params)}"

async def _exchange_token(shop: str, code: str) -> str:
    url = f"https://{shop}/admin/oauth/access_token"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, json={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "code": code})
        if r.status_code != 200:
            raise HTTPException(status_code=400, detail=f"Token exchange failed: {r.text}")
        token = r.json().get("access_token")
        if not token:
            raise HTTPException(status_code=400, detail="No access_token in response")
        return token

async def _create_script_tag(shop: str, access_token: str) -> str:
    # Include ?shop= in src so widget can load per-shop settings
    src = f"{APP_URL}/widget.js?shop={urllib.parse.quote(shop)}"
    headers = {"X-Shopify-Access-Token": access_token, "Content-Type": "application/json", "Accept": "application/json"}
    list_url = f"https://{shop}/admin/api/{API_VERSION}/script_tags.json"

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(list_url, headers=headers)
        if r.status_code != 200:
            raise HTTPException(status_code=400, detail=f"List ScriptTags failed: {r.text}")
        for tag in r.json().get("script_tags", []) or []:
            if tag.get("src") == src:
                return src
        r2 = await client.post(list_url, headers=headers, json={"script_tag": {"event": "onload", "src": src}})
        if r2.status_code not in (200, 201):
            raise HTTPException(status_code=400, detail=f"Create ScriptTag failed: {r2.text}")
        return src

def _shop_from_host_param(host: str) -> Optional[str]:
    """Shopify embeds pass ?host=base64url(shop-domain/admin)."""
    try:
        pad = "=" * ((4 - (len(host) % 4)) % 4)
        decoded = base64.urlsafe_b64decode((host + pad).encode()).decode()
        domain = decoded.split("/")[0]
        if domain.endswith(".myshopify.com"):
            return domain
    except Exception:
        pass
    return None

def _verify_shopify_jwt_and_get_shop(authorization: str) -> str:
    """
    Verify Shopify session token (JWT) from Authorization: Bearer <token>.
    Returns shop domain string on success, raises HTTPException otherwise.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    try:
        payload = jwt.decode(
            token,
            CLIENT_SECRET,
            algorithms=["HS256"],
            options={"require": ["exp", "nbf", "iss", "dest", "aud"]},
            audience=CLIENT_ID,
        )
        dest = payload.get("dest") or ""
        # dest looks like "https://your-shop.myshopify.com"
        shop = dest.replace("https://", "").replace("http://", "")
        if shop.endswith("/admin"):
            shop = shop[:-6]
        if not shop.endswith(".myshopify.com"):
            raise HTTPException(status_code=401, detail="Bad token dest")
        return shop
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")

def _verify_webhook_hmac(hmac_header: str, body: bytes) -> bool:
    if not hmac_header:
        return False
    digest = hmac.new(CLIENT_SECRET.encode("utf-8"), body, hashlib.sha256).digest()
    calc = base64.b64encode(digest).decode()
    # constant-time compare
    return hmac.compare_digest(calc, hmac_header)

# ====== Routes ======
@app.get("/")
async def root(host: Optional[str] = None):
    # If Shopify embeds, route to embedded admin
    if host:
        return RedirectResponse(f"/admin?host={urllib.parse.quote(host)}")
    return {"ok": True, "message": "Free Shipping Bar app"}

@app.get("/install")
async def install(shop: str):
    if not shop.endswith(".myshopify.com"):
        raise HTTPException(status_code=400, detail="Invalid shop parameter")
    state = secrets.token_urlsafe(32)
    STATE_CACHE[shop] = state
    return RedirectResponse(_install_authorize_url(shop, state))

@app.get("/callback")
async def callback(shop: str, code: str, state: str, db: Session = Depends(get_db)):
    saved = STATE_CACHE.get(shop)
    if not saved or saved != state:
        raise HTTPException(status_code=400, detail="Invalid or missing OAuth state")

    token = await _exchange_token(shop, code)

    # Upsert shop + default settings
    shop_row = db.get(Shop, shop)
    if shop_row:
        shop_row.access_token = token
        shop_row.uninstalled = False
    else:
        db.add(Shop(shop=shop, access_token=token))
    if not db.get(Settings, shop):
        db.add(Settings(shop=shop))
    db.commit()

    # Ensure ScriptTag
    src = await _create_script_tag(shop, token)

    # Register app/uninstalled webhook (idempotent enough for dev)
    headers = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            await c.post(
                f"https://{shop}/admin/api/{API_VERSION}/webhooks.json",
                headers=headers,
                json={
                    "webhook": {
                        "topic": "app/uninstalled",
                        "address": f"{APP_URL}/webhooks/app_uninstalled",
                        "format": "json",
                    }
                },
            )
    except Exception:
        pass  # don't block on webhook errors in dev

    return Response(content=f"ScriptTag installed. src={src}", media_type="text/plain; charset=utf-8")

# ---------- Embedded Admin (App Bridge + session token) ----------
@app.get("/admin")
async def admin(shop: Optional[str] = None, host: Optional[str] = None):
    """
    Minimal embedded admin that uses App Bridge to get a session token
    and then calls /api/settings (GET/POST) with Authorization: Bearer <token>.
    """
    # Shopify passes ?host=... in embedded context
    host_q = host or ""
    # If opened directly (not embedded), allow ?shop=...
    if not host_q and shop and shop.endswith(".myshopify.com"):
        # synthesize host param for App Bridge init
        host_q = base64.urlsafe_b64encode(f"{shop}/admin".encode()).decode().rstrip("=")

    html = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Free Shipping Bar – Admin</title>
  <script src="https://unpkg.com/@shopify/app-bridge@3"></script>
  <script src="https://unpkg.com/@shopify/app-bridge-utils@3"></script>
  <style>
    body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; padding: 20px; max-width: 720px; margin: auto; }}
    label {{ display:block; margin-top:12px; font-weight:600; }}
    input, textarea, select {{ width:100%; padding:10px; border:1px solid #d1d5db; border-radius:8px; }}
    button {{ margin-top:16px; padding:12px 14px; border:0; border-radius:10px; background:#111827; color:#fff; font-weight:600; cursor:pointer; }}
    .row {{ display:flex; gap:12px; }}
    .row > div {{ flex:1; }}
    .hint {{ color:#6b7280; font-size:12px; }}
  </style>
</head>
<body>
  <h1>Free Shipping Bar – Admin</h1>
  <p class="hint">If fields don't load, ensure the app has access and you're opening from Shopify Admin.</p>

  <form id="form">
    <label>Free shipping threshold (USD)</label>
    <input id="threshold" type="number" min="0" step="0.01" value="50.00" />

    <label>Position</label>
    <select id="position">
      <option value="top">Top</option>
      <option value="bottom">Bottom</option>
    </select>

    <label>Banner text (Top)</label>
    <textarea id="text_top" rows="2">You're {{remaining}} away from FREE shipping!</textarea>

    <label>Banner text (Bottom)</label>
    <textarea id="text_bottom" rows="2">Add {{remaining}} to your cart to get FREE shipping.</textarea>

    <div class="row">
      <div>
        <label>Background color</label>
        <input id="bg" type="text" value="#111827" />
      </div>
      <div>
        <label>Text color</label>
        <input id="fg" type="text" value="#ffffff" />
      </div>
    </div>

    <button type="submit">Save</button>
    <p class="hint">Changes take effect immediately on the storefront widget.</p>
  </form>

  <script>
    (async function() {{
      const host = {host_q!r};
      const apiKey = {CLIENT_ID!r};

      // Initialize App Bridge
      const app = window.appBridge.createApp({{ apiKey, host }});
      const {{ getSessionToken }} = window.appBridgeUtils;

      async function authFetch(url, opts={{}}) {{
        const token = await getSessionToken(app);
        const headers = Object.assign({{}}, opts.headers || {{}}, {{ Authorization: "Bearer " + token }});
        return fetch(url, Object.assign({{}}, opts, {{ headers }}));
      }}

      // Prefill settings
      try {{
        const r = await authFetch('/api/settings');
        if (r.ok) {{
          const s = await r.json();
          document.getElementById('threshold').value = (s.threshold_cents/100).toFixed(2);
          document.getElementById('position').value = s.position || 'top';
          document.getElementById('text_top').value = s.top_text || s.banner_text || "You're {{remaining}} away from FREE shipping!";
          document.getElementById('text_bottom').value = s.bottom_text || s.banner_text || "Add {{remaining}} to your cart to get FREE shipping.";
          document.getElementById('bg').value = s.bg || '#111827';
          document.getElementById('fg').value = s.fg || '#ffffff';
        }}
      }} catch (e) {{ console.error(e); }}

      // Save handler -> POST /api/settings (JSON)
      document.getElementById('form').addEventListener('submit', async (ev) => {{
        ev.preventDefault();
        const payload = {{
          threshold: parseFloat(document.getElementById('threshold').value || '0'),
          position: document.getElementById('position').value,
          text_top: document.getElementById('text_top').value,
          text_bottom: document.getElementById('text_bottom').value,
          bg: document.getElementById('bg').value,
          fg: document.getElementById('fg').value
        }};
        const r = await authFetch('/api/settings', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify(payload)
        }});
        if (r.ok) {{
          alert('Saved!');
        }} else {{
          alert('Save failed');
        }}
      }});
    }})();
  </script>
</body>
</html>
"""
    return HTMLResponse(html)

# ---------- Authenticated Settings API (requires session token) ----------
def require_shopify(db: Session = Depends(get_db), authorization: str = Header(None)):
    shop = _verify_shopify_jwt_and_get_shop(authorization)
    row = db.get(Shop, shop)
    if not row or row.uninstalled:
        raise HTTPException(status_code=403, detail="Shop not installed")
    return shop

@app.get("/api/settings")
async def get_settings(shop: str = Depends(require_shopify), db: Session = Depends(get_db)):
    s = db.get(Settings, shop) or Settings(shop=shop)
    return {
        "shop": shop,
        "threshold_cents": s.threshold_cents or 5000,
        "banner_text": s.banner_text or "You're ${remaining} away from FREE shipping!",
        "bg": s.bg or "#111827",
        "fg": s.fg or "#ffffff",
        "position": s.position or "top",
        "top_text": s.top_text,
        "bottom_text": s.bottom_text,
    }

@app.post("/api/settings")
async def save_settings(payload: dict, shop: str = Depends(require_shopify), db: Session = Depends(get_db)):
    """
    Accepts JSON body (from embedded admin via authenticatedFetch):
    {{
      threshold: number (USD),
      position: "top"|"bottom",
      text_top: str,
      text_bottom: str,
      bg: str,
      fg: str
    }}
    """
    row = db.get(Settings, shop) or Settings(shop=shop)
    db.add(row)

    threshold = float(payload.get("threshold") or 0)
    row.threshold_cents = int(round(threshold * 100))
    row.position = "bottom" if payload.get("position") == "bottom" else "top"
    row.top_text = (payload.get("text_top") or "").strip()
    row.bottom_text = (payload.get("text_bottom") or "").strip()
    row.bg = (payload.get("bg") or "#111827").strip()
    row.fg = (payload.get("fg") or "#ffffff").strip()
    if not row.banner_text:
        row.banner_text = "You're ${remaining} away from FREE shipping!"
    db.commit()
    return JSONResponse({"ok": True})

# ---------- Webhooks ----------
@app.post("/webhooks/app_uninstalled")
async def app_uninstalled(
    request: Request,
    db: Session = Depends(get_db),
    x_shopify_hmac_sha256: str = Header(None),
    x_shopify_shop_domain: str = Header(None),
):
    body = await request.body()
    if not _verify_webhook_hmac(x_shopify_hmac_sha256, body):
        raise HTTPException(status_code=401, detail="Invalid HMAC")
    shop = (x_shopify_shop_domain or "").strip()
    if shop:
        row = db.get(Shop, shop)
        if row:
            row.uninstalled = True
            db.commit()
    return Response(status_code=200)

# ---------- Widget (public) ----------
@app.get("/widget.js")
async def widget_js(shop: Optional[str] = None, db: Session = Depends(get_db)):
    # defaults
    threshold_cents = 5000
    fallback_text = "You're ${remaining} away from FREE shipping!"
    bg = "#111827"
    fg = "#ffffff"
    position = "top"
    top_text = fallback_text
    bottom_text = fallback_text

    if shop:
        s = db.get(Settings, shop)
        if s:
            threshold_cents = s.threshold_cents or threshold_cents
            bg = s.bg or bg
            fg = s.fg or fg
            position = s.position or position
            fallback = s.banner_text or fallback_text
            top_text = s.top_text or fallback
            bottom_text = s.bottom_text or fallback

    js = rf"""(function () {{
  var THRESHOLD_CENTS = {threshold_cents};
  var POLL_MS = 2000;
  var TOP_TEXT = {top_text!r};
  var BOTTOM_TEXT = {bottom_text!r};
  var BG = {bg!r};
  var FG = {fg!r};
  var POSITION = {position!r};
  var BANNER_TEXT = POSITION === 'bottom' ? BOTTOM_TEXT : TOP_TEXT;

  var css = "\
#fsb-bar{{position:fixed;left:0;right:0;z-index:99999;background:linear-gradient(90deg," + BG + "," + BG + ");color:" + FG + ";font-family:-apple-system,Inter,Roboto,Arial,sans-serif;box-shadow:0 2px 8px rgba(0,0,0,.15)}}\
#fsb-inner{{max-width:1200px;margin:0 auto;padding:10px 16px;display:flex;align-items:center;gap:14px}}\
#fsb-msg{{font-size:14px;white-space:nowrap}}\
#fsb-progress-wrap{{flex:1;height:8px;background:rgba(255,255,255,.15);border-radius:999px;overflow:hidden}}\
#fsb-progress{{height:100%;width:0;background:#10b981;transition:width .35s ease}}\
#fsb-close{{cursor:pointer;background:transparent;border:0;color:" + FG + ";opacity:.75;font-size:16px}}\
body.fsb-pushed-top{{margin-top:42px !important}}\
body.fsb-pushed-bottom{{margin-bottom:42px !important}}\
@media (max-width:640px){{body.fsb-pushed-top{{margin-top:48px !important}} body.fsb-pushed-bottom{{margin-bottom:48px !important}}}}";

  function injectCSS() {{
    if (document.getElementById('fsb-style')) return;
    var s = document.createElement('style'); s.id = 'fsb-style';
    s.appendChild(document.createTextNode(css)); document.head.appendChild(s);
  }}

  function mountBar() {{
    if (document.getElementById('fsb-bar')) return;
    var bar = document.createElement('div'); bar.id = 'fsb-bar';
    bar.style[POSITION === 'bottom' ? 'bottom' : 'top'] = '0';
    bar.innerHTML = '\
<div id="fsb-inner">\
  <div id="fsb-msg">Checking cart…</div>\
  <div id="fsb-progress-wrap"><div id="fsb-progress"></div></div>\
  <button id="fsb-close" aria-label="Close">✕</button>\
</div>';
    document.body.appendChild(bar);
    document.body.classList.add(POSITION === 'bottom' ? 'fsb-pushed-bottom' : 'fsb-pushed-top');
    document.getElementById('fsb-close').addEventListener('click', function() {{
      bar.parentNode.removeChild(bar);
      document.body.classList.remove('fsb-pushed-top','fsb-pushed-bottom');
    }});
  }}

  function formatMoney(cents) {{
    try {{
      var fmt = new Intl.NumberFormat(undefined, {{ style: 'currency', currency: (window.Shopify && Shopify.currency && Shopify.currency.active) || 'USD' }});
      return fmt.format((cents || 0) / 100);
    }} catch(e) {{
      return '$' + ((cents || 0)/100).toFixed(2);
    }}
  }}

  function fillTemplate(tpl, vars) {{
    return tpl.replace(/{{\s*remaining\s*}}/gi, vars.remaining || '');
  }}

  function updateBar(totalCents) {{
    var msg = document.getElementById('fsb-msg');
    var prog = document.getElementById('fsb-progress');
    if (!msg || !prog) return;

    var remainingCents = Math.max(0, THRESHOLD_CENTS - (totalCents || 0));
    var pct = Math.max(0, Math.min(1, (totalCents || 0) / THRESHOLD_CENTS));
    prog.style.width = (pct * 100).toFixed(1) + '%';

    if (remainingCents > 0) {{
      msg.textContent = fillTemplate(BANNER_TEXT, {{ remaining: formatMoney(remainingCents) }});
    }} else {{
      msg.textContent = "🎉 You unlocked FREE shipping!";
      prog.style.background = '#22c55e';
    }}
  }}

  async function getCartTotalCents() {{
    try {{
      var res = await fetch('/cart.js', {{ credentials: 'same-origin' }});
      if (!res.ok) throw new Error('cart.js ' + res.status);
      var cart = await res.json();
      return cart && typeof cart.total_price === 'number' ? cart.total_price : 0;
    }} catch (e) {{ return 0; }}
  }}

  async function tick() {{ updateBar(await getCartTotalCents()); }}

  injectCSS(); mountBar(); tick(); setInterval(tick, POLL_MS);
}})();"""
    return Response(js, media_type="application/javascript; charset=utf-8")

@app.get("/health")
async def health():
    return {"ok": True}

# ====== Dev server ======
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)


# main.py
import os
import secrets
import urllib.parse
from typing import Dict, Optional

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse, Response

app = FastAPI(title="Free Shipping Bar")

# =========================
# Config / ENV (with fallbacks)
# =========================
def _get_first_env(*names: str, required: bool = True, default: Optional[str] = None) -> str:
    for n in names:
        v = os.getenv(n, "").strip()
        if v:
            return v
    if required and default is None:
        raise RuntimeError(f"Missing required environment variable: one of {', '.join(names)}")
    return default or ""

CLIENT_ID = _get_first_env("SHOPIFY_API_KEY", "SHOPIFY_CLIENT_ID")
CLIENT_SECRET = _get_first_env("SHOPIFY_API_SECRET", "SHOPIFY_CLIENT_SECRET")
APP_URL = _get_first_env("APP_URL")  # e.g., https://shopify-freeship-bar.onrender.com

# Note: pick a stable, supported Shopify API version
API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2024-10")
SCOPES = os.getenv("SHOPIFY_SCOPES", "write_script_tags,read_script_tags")

# Widget settings (safe defaults)
THRESHOLD_USD = float(os.getenv("THRESHOLD_USD", "50"))
PROGRESS_TEXT = os.getenv("PROGRESS_TEXT", "You're {remaining} away from FREE shipping!")
UNLOCKED_TEXT = os.getenv("UNLOCKED_TEXT", "🎉 You unlocked FREE shipping!")
BAR_POSITION = os.getenv("BAR_POSITION", "top").lower()  # "top" | "bottom"
BG_COLOR = os.getenv("BG_COLOR", "#111827")
TEXT_COLOR = os.getenv("TEXT_COLOR", "#ffffff")

# OAuth state (in-memory for dev)
STATE_CACHE: Dict[str, str] = {}

# =========================
# Helpers
# =========================
def _install_authorize_url(shop: str, state: str) -> str:
    redirect_uri = f"{APP_URL}/callback"
    params = {
        "client_id": CLIENT_ID,
        "scope": SCOPES,
        "redirect_uri": redirect_uri,
        "state": state,
    }
    return f"https://{shop}/admin/oauth/authorize?{urllib.parse.urlencode(params)}"


async def _exchange_token(shop: str, code: str) -> str:
    """Exchange auth code for an access token."""
    url = f"https://{shop}/admin/oauth/access_token"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            url,
            json={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "code": code},
        )
        if r.status_code != 200:
            raise HTTPException(status_code=400, detail=f"Token exchange failed: {r.text}")
        data = r.json()
        token = data.get("access_token")
        if not token:
            raise HTTPException(status_code=400, detail="No access_token in response")
        return token


async def _create_script_tag(shop: str, access_token: str) -> str:
    """
    Ensure a ScriptTag that loads our /widget.js exists.
    Returns the src that was set.
    """
    src = f"{APP_URL}/widget.js"
    headers = {
        "X-Shopify-Access-Token": access_token,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    list_url = f"https://{shop}/admin/api/{API_VERSION}/script_tags.json"

    async with httpx.AsyncClient(timeout=30) as client:
        # Check existing tags
        r = await client.get(list_url, headers=headers)
        if r.status_code != 200:
            raise HTTPException(status_code=400, detail=f"List ScriptTags failed: {r.text}")
        tags = r.json().get("script_tags", []) or []
        for tag in tags:
            if tag.get("src") == src:
                return src

        # Create new ScriptTag
        payload = {"script_tag": {"event": "onload", "src": src}}
        r2 = await client.post(list_url, headers=headers, json=payload)
        if r2.status_code not in (200, 201):
            raise HTTPException(status_code=400, detail=f"Create ScriptTag failed: {r2.text}")
        return src


def _is_valid_shop(shop: str) -> bool:
    return shop.endswith(".myshopify.com") and len(shop.split(".")) >= 3

# =========================
# Routes
# =========================
@app.get("/")
def root():
    return {"ok": True, "message": "Free Shipping Bar app"}

@app.get("/install")
def install(shop: str):
    """
    Start OAuth by redirecting to Shopify's authorize screen.
    /install?shop=your-store.myshopify.com
    """
    if not _is_valid_shop(shop):
        raise HTTPException(status_code=400, detail="Invalid shop parameter")
    state = secrets.token_urlsafe(32)
    STATE_CACHE[shop] = state
    return RedirectResponse(_install_authorize_url(shop, state))

@app.get("/callback")
async def callback(request: Request, shop: str, code: str, state: str):
    """
    Shopify redirects here. Verify state, exchange code, and create ScriptTag.
    """
    if not _is_valid_shop(shop):
        raise HTTPException(status_code=400, detail="Invalid shop parameter")

    saved = STATE_CACHE.get(shop)
    if not saved or saved != state:
        raise HTTPException(status_code=400, detail="Invalid or missing OAuth state")

    token = await _exchange_token(shop, code)
    src = await _create_script_tag(shop, token)

    return Response(
        content=f"ScriptTag installed. src={src}",
        media_type="text/plain; charset=utf-8",
    )

@app.get("/widget.js")
def widget_js():
    """
    Always returns JS (no crashes). Pulls copy/colors/position/threshold from env.
    - Fixes double-$ by sanitizing PROGRESS_TEXT if it contains a literal '$' before {remaining}.
    - Uses test subtotal mock if window.__FREE_SHIP_BAR_TEST_SUBTOTAL_CENTS is set.
    """
    threshold_cents = max(0, round(THRESHOLD_USD * 100))
    pos = "bottom" if BAR_POSITION == "bottom" else "top"

    # Sanitize template: if someone set PROGRESS_TEXT="You're ${remaining} away..."
    # convert to "You're {remaining} away..." to avoid "$" + Intl formatter duplication.
    progress_template = PROGRESS_TEXT.replace("${remaining}", "{remaining}")

    # Build JS safely; values are embedded as literals
    js = f"""(function(){{
  var THRESHOLD_CENTS = {threshold_cents};
  var POS = {pos!r};                // "top" | "bottom"
  var BG = {BG_COLOR!r};
  var FG = {TEXT_COLOR!r};
  var PROGRESS = {progress_template!r}; // supports {{remaining}}
  var UNLOCKED = {UNLOCKED_TEXT!r};

  function injectCSS(){{
    if (document.getElementById('fsb-style')) return;
    var s=document.createElement('style'); s.id='fsb-style';
    s.textContent = `
      #fsb-bar{{position:fixed;left:0;right:0;` + POS + `:0;z-index:999999;background:`+BG+`;color:`+FG+`;
        font-family:-apple-system,Inter,Roboto,Arial,sans-serif;box-shadow:0 2px 8px rgba(0,0,0,.15)}}
      #fsb-wrap{{max-width:1200px;margin:0 auto;padding:10px 16px;display:flex;gap:12px;align-items:center}}
      #fsb-msg{{font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
      #fsb-prog{{flex:1;height:8px;background:rgba(255,255,255,.15);border-radius:999px;overflow:hidden}}
      #fsb-fill{{height:100%;width:0;background:#10b981;transition:width .35s ease}}
      body.fsb-padded-top{{margin-top:48px}}
      body.fsb-padded-bottom{{margin-bottom:48px}}
      @media (max-width:640px){{body.fsb-padded-top{{margin-top:52px}} body.fsb-padded-bottom{{margin-bottom:52px}}}}
    `;
    document.head.appendChild(s);
  }}

  function mount(){{
    if (document.getElementById('fsb-bar')) return;
    var bar=document.createElement('div'); bar.id='fsb-bar';
    bar.innerHTML = `
      <div id="fsb-wrap">
        <div id="fsb-msg">Checking cart…</div>
        <div id="fsb-prog"><div id="fsb-fill"></div></div>
      </div>`;
    document.body.appendChild(bar);
    if (POS === 'top') document.body.classList.add('fsb-padded-top');
    else document.body.classList.add('fsb-padded-bottom');
  }}

  function fmt(cents){{
    try {{
      var cur=(window.Shopify&&Shopify.currency&&Shopify.currency.active)||'USD';
      return new Intl.NumberFormat(undefined,{{style:'currency',currency:cur}}).format((cents||0)/100);
    }} catch(e){{ return '$'+((cents||0)/100).toFixed(2); }}
  }}

  function applyTemplate(amountStr){{
    // Ensure we don't accidentally have a literal '$' before the placeholder
    var tpl = PROGRESS.replace('${{remaining}}','{{remaining}}');
    return tpl.replace('{{remaining}}', amountStr);
  }}

  function update(total){{
    var msg=document.getElementById('fsb-msg');
    var fill=document.getElementById('fsb-fill');
    if(!msg||!fill) return;
    var rem=Math.max(0, THRESHOLD_CENTS-(total||0));
    var pct = THRESHOLD_CENTS > 0 ? Math.max(0, Math.min(1,(total||0)/THRESHOLD_CENTS)) : 1;
    fill.style.width=(pct*100).toFixed(1)+'%';
    if(rem>0) msg.textContent = applyTemplate(fmt(rem));
    else {{
      msg.textContent = UNLOCKED;
      fill.style.background = '#22c55e';
    }}
  }}

  async function fetchCartTotal(){{
    try {{
      var r=await fetch('/cart.js',{{credentials:'same-origin'}});
      if(!r.ok) throw 0;
      var c=await r.json();
      return typeof c.total_price==='number'?c.total_price:0;
    }} catch(e){{ return 0; }}
  }}

  async function cartTotal(){{
    // If the smoke test page set a mock subtotal, prefer it
    if (typeof window.__FREE_SHIP_BAR_TEST_SUBTOTAL_CENTS === 'number') {{
      return Math.max(0, window.__FREE_SHIP_BAR_TEST_SUBTOTAL_CENTS|0);
    }}
    return await fetchCartTotal();
  }}

  async function tick(){{
    update(await cartTotal());
  }}

  injectCSS(); mount(); tick(); setInterval(tick, 2500);
}})();"""
    return Response(js, media_type="application/javascript; charset=utf-8")

@app.get("/health")
def health():
    return {"ok": True}

# ========= Local dev ========
if __name__ == "__main__":
    # Run: uvicorn main:app --reload --port 8000
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)


# main.py
import os
import secrets
import urllib.parse
from typing import Dict, Optional

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse, Response

app = FastAPI(title="Free Shipping Bar")

# =========================
# Config / ENV (with fallbacks)
# =========================
def _get_first_env(*names: str, required: bool = True, default: Optional[str] = None) -> str:
    """Return the first non-empty env var among names."""
    for n in names:
        v = os.getenv(n, "").strip()
        if v:
            return v
    if required and default is None:
        raise RuntimeError(f"Missing required environment variable: one of {', '.join(names)}")
    return default or ""

# Compatible with either SHOPIFY_API_KEY/SECRET or SHOPIFY_CLIENT_ID/SECRET
CLIENT_ID = _get_first_env("SHOPIFY_API_KEY", "SHOPIFY_CLIENT_ID")
CLIENT_SECRET = _get_first_env("SHOPIFY_API_SECRET", "SHOPIFY_CLIENT_SECRET")
APP_URL = _get_first_env("APP_URL")  # e.g., https://shopify-freeship-bar.onrender.com

# Use a stable, supported Shopify API version
API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2024-10")
SCOPES = os.getenv("SHOPIFY_SCOPES", "write_script_tags,read_script_tags")

# Widget settings (safe defaults)
THRESHOLD_USD = float(os.getenv("THRESHOLD_USD", "50"))
PROGRESS_TEXT = os.getenv("PROGRESS_TEXT", "You're {remaining} away from FREE shipping!")
UNLOCKED_TEXT = os.getenv("UNLOCKED_TEXT", "🎉 You unlocked FREE shipping!")
BAR_POSITION = os.getenv("BAR_POSITION", "top").lower()  # "top" | "bottom"
BG_COLOR = os.getenv("BG_COLOR", "#111827")
TEXT_COLOR = os.getenv("TEXT_COLOR", "#ffffff")

# Optional free gift: set FREE_GIFT_VARIANT_ID to enable CTA (0 disables)
FREE_GIFT_VARIANT_ID = int(os.getenv("FREE_GIFT_VARIANT_ID", "0") or 0)

# OAuth state (in-memory; fine for dev/demo)
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
    url = f"https://{shop}/admin/oauth/access_token"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            url,
            json={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "code": code},
        )
        if r.status_code != 200:
            raise HTTPException(status_code=400, detail=f"Token exchange failed: {r.text}")
        token = r.json().get("access_token")
        if not token:
            raise HTTPException(status_code=400, detail="No access_token in response")
        return token

async def _create_script_tag(shop: str, access_token: str) -> str:
    """Ensure a ScriptTag that loads our /widget.js exists. Returns the src."""
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
    """Start OAuth: /install?shop=your-store.myshopify.com"""
    if not _is_valid_shop(shop):
        raise HTTPException(status_code=400, detail="Invalid shop parameter")
    state = secrets.token_urlsafe(32)
    STATE_CACHE[shop] = state
    return RedirectResponse(_install_authorize_url(shop, state))

@app.get("/callback")
async def callback(request: Request, shop: str, code: str, state: str):
    """Finish OAuth, create ScriptTag."""
    if not _is_valid_shop(shop):
        raise HTTPException(status_code=400, detail="Invalid shop parameter")
    saved = STATE_CACHE.get(shop)
    if not saved or saved != state:
        raise HTTPException(status_code=400, detail="Invalid or missing OAuth state")
    token = await _exchange_token(shop, code)
    src = await _create_script_tag(shop, token)
    return Response(content=f"ScriptTag installed. src={src}", media_type="text/plain; charset=utf-8")

@app.get("/widget.js")
def widget_js():
    """
    Returns storefront JS:
      - Currency formatting with Intl (fixes double-$).
      - Polls /cart.js; also supports local smoke-test via window.__FREE_SHIP_BAR_TEST_SUBTOTAL_CENTS.
      - Top/bottom placement with body padding.
      - Optional 'Add free gift' CTA when threshold is reached.
    """
    threshold_cents = max(0, round(THRESHOLD_USD * 100))
    pos = "bottom" if BAR_POSITION == "bottom" else "top"

    # Avoid '$' + Intl duplication if someone sets PROGRESS_TEXT="... ${remaining} ..."
    progress_template = PROGRESS_TEXT.replace("${remaining}", "{remaining}")
    gift_variant = max(0, FREE_GIFT_VARIANT_ID)

    # Build the JS payload
    js = f"""(function(){{
  var THRESHOLD_CENTS = {threshold_cents};
  var POS = {pos!r};                 // "top" | "bottom"
  var BG = {BG_COLOR!r};
  var FG = {TEXT_COLOR!r};
  var PROGRESS = {progress_template!r}; // supports {{remaining}}
  var UNLOCKED = {UNLOCKED_TEXT!r};
  var GIFT_VARIANT = {gift_variant};    // 0 disables CTA

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
      #fsb-cta{{display:none;margin-left:8px;padding:6px 10px;border:0;border-radius:8px;background:#22c55e;color:#fff;cursor:pointer}}
      body.fsb-padded-top{{margin-top:48px}} body.fsb-padded-bottom{{margin-bottom:48px}}
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
        <button id="fsb-cta" type="button">Add free gift</button>
      </div>`;
    document.body.appendChild(bar);
    if (POS === 'top') document.body.classList.add('fsb-padded-top');
    else document.body.classList.add('fsb-padded-bottom');

    var cta = document.getElementById('fsb-cta');
    if (cta) cta.addEventListener('click', addGift);
  }}

  function fmt(cents){{
    try {{
      var cur=(window.Shopify&&Shopify.currency&&Shopify.currency.active)||'USD';
      return new Intl.NumberFormat(undefined,{{style:'currency',currency:cur}}).format((cents||0)/100);
    }} catch(e){{ return '$'+((cents||0)/100).toFixed(2); }}
  }}

  function applyTemplate(amountStr){{
    var tpl = PROGRESS.replace('${{remaining}}','{{remaining}}');
    return tpl.replace('{{remaining}}', amountStr);
  }}

  function showCTA(show){{
    var cta = document.getElementById('fsb-cta');
    if (!cta) return;
    if (show && GIFT_VARIANT) cta.style.display = 'inline-block';
    else cta.style.display = 'none';
  }}

  async function addGift(){{
    if(!GIFT_VARIANT) return;
    try {{
      // Preferred JSON API
      var r = await fetch('/cart/add.js', {{
        method:'POST',
        headers:{{'Content-Type':'application/json'}},
        credentials:'same-origin',
        body: JSON.stringify({{ id: GIFT_VARIANT, quantity: 1 }})
      }});
      if (!r.ok) throw new Error('add.js failed');
      await r.json();
      location.href='/cart';
    }} catch(e) {{
      // Fallback GET
      location.href = '/cart/add?id=' + GIFT_VARIANT + '&quantity=1';
    }}
  }}

  function update(total){{
    var msg=document.getElementById('fsb-msg');
    var fill=document.getElementById('fsb-fill');
    if(!msg||!fill) return;
    var rem=Math.max(0, THRESHOLD_CENTS-(total||0));
    var pct = THRESHOLD_CENTS > 0 ? Math.max(0, Math.min(1,(total||0)/THRESHOLD_CENTS)) : 1;
    fill.style.width=(pct*100).toFixed(1)+'%';
    if(rem>0) {{
      msg.textContent = applyTemplate(fmt(rem));
      showCTA(false);
    }} else {{
      msg.textContent = UNLOCKED;
      fill.style.background = '#22c55e';
      showCTA(true);
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
    // Local smoke-test override
    if (typeof window.__FREE_SHIP_BAR_TEST_SUBTOTAL_CENTS === 'number') {{
      return Math.max(0, window.__FREE_SHIP_BAR_TEST_SUBTOTAL_CENTS|0);
    }}
    return await fetchCartTotal();
  }}

  async function tick(){{ update(await cartTotal()); }}

  injectCSS(); mount(); tick(); setInterval(tick, 2500);
}})();"""
    return Response(js, media_type="application/javascript; charset=utf-8")

@app.get("/health")
def health():
    return {"ok": True}

# ========= Local dev ========
if __name__ == "__main__":
    # Run locally with: uvicorn main:app --reload --port 8000
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)


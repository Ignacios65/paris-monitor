#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Monitor de precios Paris.cl + Falabella con alertas via ntfy.sh
"""

import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from statistics import median

import requests

# ------------------------------------------------------------------ #
#  CONFIGURACIÓN
# ------------------------------------------------------------------ #

CONFIG_PATH = os.environ.get("FALABELLA_CONFIG", "config.json")


def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    else:
        cfg = {}

    cfg["ntfy_tema"] = os.environ.get("NTFY_TEMA", cfg.get("ntfy_tema", ""))

    urls_env = os.environ.get("URLS", "")
    if urls_env:
        cfg["urls"] = [u.strip() for u in urls_env.split(",") if u.strip()]
    cfg.setdefault("urls", [])

    paris_env = os.environ.get("PARIS_TERMINOS", "")
    if paris_env:
        cfg["paris_terminos"] = [t.strip() for t in paris_env.split(",") if t.strip()]
    cfg.setdefault("paris_terminos", [])

    cfg.setdefault("intervalo_minutos", int(os.environ.get("INTERVALO_MINUTOS", 30)))
    cfg.setdefault("umbral_error",      float(os.environ.get("UMBRAL_ERROR", 0.80)))
    cfg.setdefault("umbral_descuento",  float(os.environ.get("UMBRAL_DESCUENTO", 0.65)))
    cfg.setdefault("umbral_caida",      float(os.environ.get("UMBRAL_CAIDA", 0.60)))
    cfg.setdefault("precio_minimo_clp", int(os.environ.get("PRECIO_MINIMO_CLP", 1000)))
    cfg.setdefault("min_muestras",      int(os.environ.get("MIN_MUESTRAS", 5)))
    cfg.setdefault("pausa_entre_urls_seg", int(os.environ.get("PAUSA_ENTRE_URLS_SEG", 5)))
    cfg.setdefault("headless", True)

    if not cfg.get("ntfy_tema") or "PEGA_AQUI" in cfg["ntfy_tema"]:
        sys.exit("Falta el tema de ntfy. Configura la variable de entorno NTFY_TEMA.")
    if not cfg.get("urls") and not cfg.get("paris_terminos"):
        sys.exit("No hay URLs ni terminos de Paris configurados.")
    return cfg


# ------------------------------------------------------------------ #
#  BASE DE DATOS
# ------------------------------------------------------------------ #

DB_PATH = os.environ.get("FALABELLA_DB", "precios.db")


def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS precios (
            sku    TEXT,
            ts     TEXT,
            precio INTEGER,
            normal INTEGER,
            nombre TEXT,
            url    TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS alertas (
            clave TEXT PRIMARY KEY,
            ts    TEXT
        )
    """)
    con.commit()
    return con


def registrar_precio(con, p):
    con.execute(
        "INSERT INTO precios (sku, ts, precio, normal, nombre, url) VALUES (?,?,?,?,?,?)",
        (p["sku"], datetime.now(timezone.utc).isoformat(),
         p["precio"], p["normal"], p["nombre"], p["url"]),
    )
    con.commit()


def precio_referencia(con, sku, min_muestras=5):
    filas = con.execute(
        "SELECT precio FROM precios WHERE sku=? ORDER BY ts DESC LIMIT 60", (sku,)
    ).fetchall()
    precios = [r[0] for r in filas if r[0]]
    if len(precios) < min_muestras:
        return None
    return median(precios)


def ya_alertado(con, clave):
    return con.execute("SELECT 1 FROM alertas WHERE clave=?", (clave,)).fetchone() is not None


def marcar_alertado(con, clave):
    con.execute("INSERT OR REPLACE INTO alertas (clave, ts) VALUES (?,?)",
                (clave, datetime.now(timezone.utc).isoformat()))
    con.commit()


# ------------------------------------------------------------------ #
#  PARIS — API JSON directa (sin Playwright)
# ------------------------------------------------------------------ #

PARIS_API = "https://be-paris-backend-cl-ms-search.ccom.paris.cl/products/"


def fetch_paris(termino, max_paginas=3):
    """Consulta el API JSON de Paris directamente, sin Playwright."""
    productos = []
    vistos = set()
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0 Safari/537.36"),
        "Accept": "application/json",
        "Referer": "https://www.paris.cl/",
    }
    for pagina in range(1, max_paginas + 1):
        try:
            r = requests.get(
                PARIS_API,
                params={"term": termino, "page": pagina, "pageSize": 50},
                headers=headers,
                timeout=20,
            )
            if not r.ok:
                break
            data = r.json()
            items = data.get("products") or data.get("items") or data.get("results") or []
            if not items:
                for k, v in data.items():
                    if isinstance(v, list) and v and isinstance(v[0], dict):
                        items = v
                        break
            if not items:
                break
            for p in items:
                sku = str(p.get("sku") or p.get("productId") or p.get("id") or "")
                if not sku or sku in vistos:
                    continue
                vistos.add(sku)
                precio = p.get("price") or p.get("currentPrice") or p.get("salePrice")
                normal = p.get("originalPrice") or p.get("normalPrice") or precio
                nombre = p.get("title") or p.get("name") or p.get("displayName") or ""
                url    = p.get("url") or p.get("productUrl") or ""
                if url and url.startswith("/"):
                    url = "https://www.paris.cl" + url
                if nombre and precio:
                    productos.append({
                        "sku":    f"paris_{sku}",
                        "nombre": str(nombre)[:120],
                        "precio": int(precio),
                        "normal": int(normal) if normal else int(precio),
                        "url":    url,
                    })
        except Exception as e:
            print(f"  ! Error Paris ({termino} p{pagina}): {e}")
            break
    return productos


# ------------------------------------------------------------------ #
#  FALABELLA — Playwright (intercepta JSON de la web)
# ------------------------------------------------------------------ #

def ensure_chromium():
    import subprocess
    chromium_path = os.path.expanduser("~/.cache/ms-playwright")
    alt_path = "/ms-playwright"
    path = chromium_path if os.path.exists(chromium_path) else alt_path
    found = False
    for root, dirs, files in os.walk(path):
        for f in files:
            if "chrome" in f.lower() or "chromium" in f.lower():
                found = True
                break
        if found:
            break
    if not found:
        print("Chromium no encontrado, instalando...")
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
        print("Chromium instalado OK")
    else:
        print("Chromium ya instalado, continuando...")


def fetch_payloads(url, timeout_ms=45000, headless=True):
    ensure_chromium()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("Falta Playwright.")

    try:
        from playwright_stealth import stealth_sync
        use_stealth = True
    except ImportError:
        use_stealth = False

    payloads = []

    def on_response(resp):
        ct = (resp.headers or {}).get("content-type", "")
        if "application/json" not in ct:
            return
        try:
            payloads.append(resp.json())
        except Exception:
            pass

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless,
                                     args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0 Safari/537.36"),
            locale="es-CL",
            viewport={"width": 1366, "height": 900},
        )
        page = ctx.new_page()
        if use_stealth:
            stealth_sync(page)
        page.on("response", on_response)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(5000)
        except Exception as e:
            print(f"  ! aviso al cargar {url}: {e}")
        try:
            prev_count = 0
            sin_cambio = 0
            for _ in range(20):
                page.mouse.wheel(0, 5000)
                page.wait_for_timeout(2000)
                count = page.evaluate("""() => {
                    const scripts = Array.from(document.querySelectorAll('script:not([src])'));
                    for (const s of scripts) {
                        try {
                            const data = JSON.parse(s.textContent);
                            const entity = data.mainEntity || data;
                            if (entity['@type'] === 'ItemList' && entity.itemListElement)
                                return entity.itemListElement.length;
                        } catch(e) {}
                    }
                    return document.querySelectorAll('[class*="pod-product"],[class*="product-card"],.pod').length;
                }""")
                if count == prev_count:
                    sin_cambio += 1
                    if sin_cambio >= 3:
                        break
                else:
                    sin_cambio = 0
                prev_count = count
        except Exception:
            pass

        try:
            nxt = page.eval_on_selector("#__NEXT_DATA__", "el => el.textContent")
            if nxt:
                payloads.append(json.loads(nxt))
        except Exception:
            pass

        try:
            schema_products = page.evaluate("""() => {
                const scripts = Array.from(document.querySelectorAll('script:not([src])'));
                for (const s of scripts) {
                    try {
                        const data = JSON.parse(s.textContent);
                        const entity = data.mainEntity || data;
                        if (entity['@type'] === 'ItemList' && entity.itemListElement) {
                            const items = entity.itemListElement
                                .map(item => item.item || item)
                                .filter(p => p.offers && p.offers.price);
                            const pods = Array.from(document.querySelectorAll(
                                '[class*="pod-product"], [class*="productPod"], ' +
                                '[class*="product-card"], [class*="pod_product"], .pod'
                            ));
                            return items.map((p, i) => {
                                const sku = String(p.sku || '');
                                let pod = sku ? document.querySelector(
                                    '[data-id="' + sku + '"], [data-sku="' + sku + '"]') : null;
                                if (!pod) pod = pods[i] || null;
                                let precio = p.offers.price;
                                if (pod) {
                                    const podText = pod.textContent || '';
                                    if (podText.includes('x un')) {
                                        const allPrices = Array.from(podText.matchAll(/\\$(\\s*[\\d.]+)/g))
                                            .map(m => parseInt(m[1].replace(/\\./g, '')))
                                            .filter(v => v >= 1000);
                                        const packCandidates = allPrices.filter(v => v > precio);
                                        if (packCandidates.length > 0)
                                            precio = Math.min(...packCandidates);
                                    }
                                }
                                const normalEl = pod ? pod.querySelector('.ui-line-through') : null;
                                const normalRaw = normalEl ? normalEl.textContent.trim() : null;
                                let normal = normalRaw ? parseInt(normalRaw.replace(/[^\\d]/g, '')) : null;
                                if (normal && normal > precio * 8) normal = null;
                                return {
                                    sku: p.sku || p.name,
                                    nombre: p.name,
                                    precio: precio,
                                    normal: normal || precio,
                                    url: p.url || p.offers.url || ''
                                };
                            });
                        }
                    } catch(e) {}
                }
                return null;
            }""")
            if schema_products:
                payloads.append({"_schema_products": schema_products})
        except Exception:
            pass

        browser.close()
    return payloads


# ------------------------------------------------------------------ #
#  PARSER
# ------------------------------------------------------------------ #

NAME_KEYS = ("displayName", "productName", "name", "title")
ID_KEYS   = ("skuId", "productId", "sku", "id")
URL_KEYS  = ("url", "productUrl", "purl", "link")


def normalizar_precio(valor):
    if isinstance(valor, (list, tuple)) and valor:
        valor = valor[0]
    if isinstance(valor, (int, float)):
        return int(valor)
    if not isinstance(valor, str):
        return None
    digitos = re.sub(r"[^\d]", "", valor)
    return int(digitos) if digitos else None


def precios_de_producto(prod):
    candidatos = []
    prices = prod.get("prices")
    if isinstance(prices, list):
        for it in prices:
            if isinstance(it, dict):
                v = normalizar_precio(it.get("price"))
                if v:
                    candidatos.append(v)
    for k in ("price", "currentPrice", "salePrice", "internetPrice", "normalPrice"):
        v = normalizar_precio(prod.get(k))
        if v:
            candidatos.append(v)
    if not candidatos:
        return None, None
    return min(candidatos), max(candidatos)


def first_key(d, keys):
    for k in keys:
        if k in d and d[k]:
            return d[k]
    return None


def extraer_productos(obj, encontrados=None, vistos=None):
    if encontrados is None:
        encontrados, vistos = [], set()

    if isinstance(obj, dict) and "_schema_products" in obj:
        for item in obj["_schema_products"]:
            if item.get("sku") and item["sku"] not in vistos:
                vistos.add(item["sku"])
                encontrados.append(item)
        return encontrados

    if isinstance(obj, dict):
        nombre = first_key(obj, NAME_KEYS)
        oferta, normal = precios_de_producto(obj)
        if nombre and oferta:
            sku = str(first_key(obj, ID_KEYS) or nombre)
            if sku not in vistos:
                vistos.add(sku)
                url = first_key(obj, URL_KEYS) or ""
                if url and url.startswith("/"):
                    url = "https://www.falabella.com" + url
                encontrados.append({
                    "sku":    sku,
                    "nombre": str(nombre)[:120],
                    "precio": oferta,
                    "normal": normal or oferta,
                    "url":    url,
                })
        for v in obj.values():
            extraer_productos(v, encontrados, vistos)
    elif isinstance(obj, list):
        for v in obj:
            extraer_productos(v, encontrados, vistos)
    return encontrados


# ------------------------------------------------------------------ #
#  DETECCIÓN DE ERRORES DE PRECIO
# ------------------------------------------------------------------ #

def evaluar(con, prod, cfg):
    precio, normal = prod["precio"], prod["normal"]
    if precio <= 0 or precio < cfg.get("precio_minimo_clp", 1000):
        return None

    motivos = []
    nivel = None

    if normal and normal > 0:
        desc = 1 - precio / normal
        if desc >= cfg.get("umbral_error", 0.80):
            motivos.append(f"{desc*100:.0f}% bajo el precio normal "
                           f"(${normal:,} -> ${precio:,})".replace(",", "."))
            nivel = "super"
        elif desc >= cfg.get("umbral_descuento", 0.65):
            motivos.append(f"{desc*100:.0f}% bajo el precio normal "
                           f"(${normal:,} -> ${precio:,})".replace(",", "."))
            nivel = "normal"

    ref = precio_referencia(con, prod["sku"], cfg.get("min_muestras", 5))
    if ref:
        caida = 1 - precio / ref
        if caida >= cfg.get("umbral_caida", 0.60):
            motivos.append(f"{caida*100:.0f}% bajo su precio habitual "
                           f"(~${int(ref):,} -> ${precio:,})".replace(",", "."))
            if nivel is None:
                nivel = "normal"

    return (nivel, " | ".join(motivos)) if motivos else None


# ------------------------------------------------------------------ #
#  NTFY
# ------------------------------------------------------------------ #

def enviar_ntfy(cfg, titulo, cuerpo, url_producto="", super_alerta=False):
    tema = cfg["ntfy_tema"]
    headers = {
        "Title":    titulo.encode("utf-8"),
        "Priority": "max" if super_alerta else "high",
        "Tags":     "rotating_light,fire,moneybag" if super_alerta else "moneybag",
    }
    if url_producto:
        headers["Click"] = url_producto
    try:
        r = requests.post(f"https://ntfy.sh/{tema}",
                          data=cuerpo.encode("utf-8"),
                          headers=headers, timeout=20)
        if not r.ok:
            print(f"  ! ntfy {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"  ! Error ntfy: {e}")


# ------------------------------------------------------------------ #
#  CICLO PRINCIPAL
# ------------------------------------------------------------------ #

def procesar_productos(con, cfg, productos, prefijo=""):
    alertas = 0
    for prod in productos:
        registrar_precio(con, prod)
        resultado = evaluar(con, prod, cfg)
        if resultado:
            nivel, motivo = resultado
            clave = f"{prod['sku']}@{prod['precio']}"
            if not ya_alertado(con, clave):
                marcar_alertado(con, clave)
                alertas += 1
                if nivel == "super":
                    titulo = f"{prefijo}ERROR PRECIO: {prod['nombre'][:45]}"
                else:
                    titulo = f"{prefijo}Oferta: {prod['nombre'][:50]}"
                cuerpo = (f"Precio: ${prod['precio']:,}\n{motivo}").replace(",", ".")
                enviar_ntfy(cfg, titulo, cuerpo, prod.get("url", ""),
                            super_alerta=(nivel == "super"))
                print(f"  ALERTA ({nivel}): {prod['nombre']} -> ${prod['precio']}")
    return alertas


def revisar_una_vez(con, cfg):
    total, alertas = 0, 0
    pausa = cfg.get("pausa_entre_urls_seg", 5)

    # --- Paris (API directa) ---
    for termino in cfg.get("paris_terminos", []):
        print(f"[{datetime.now():%H:%M:%S}] Paris: {termino}")
        productos = fetch_paris(termino)
        unicos = list({p["sku"]: p for p in productos}.values())
        print(f"  -> {len(unicos)} productos detectados")
        total += len(unicos)
        alertas += procesar_productos(con, cfg, unicos, prefijo="PARIS ")
        time.sleep(pausa)

    # --- Falabella (Playwright) ---
    for url in cfg.get("urls", []):
        print(f"[{datetime.now():%H:%M:%S}] Revisando: {url}")
        productos = []
        for payload in fetch_payloads(url, headless=cfg.get("headless", True)):
            productos.extend(extraer_productos(payload))
        unicos = list({p["sku"]: p for p in productos}.values())
        print(f"  -> {len(unicos)} productos detectados")
        total += len(unicos)
        alertas += procesar_productos(con, cfg, unicos)
        time.sleep(pausa)

    print(f"  Resumen: {total} productos, {alertas} alertas nuevas\n")


def main():
    cfg = load_config()
    con = init_db()
    intervalo = cfg.get("intervalo_minutos", 30)

    if "--test" in sys.argv:
        enviar_ntfy(cfg, "Monitor OK", "Las notificaciones funcionan correctamente.")
        print("Notificacion de prueba enviada.")
        return

    if "--once" in sys.argv:
        revisar_una_vez(con, cfg)
        return

    print(f"Monitor iniciado. Revisando cada {intervalo} min. (Ctrl+C para salir)\n")
    enviar_ntfy(cfg, "Monitor iniciado", "El monitor de precios esta activo.")
    while True:
        try:
            revisar_una_vez(con, cfg)
        except KeyboardInterrupt:
            print("Saliendo.")
            break
        except Exception as e:
            print(f"  ! Error en el ciclo: {e}")
        time.sleep(intervalo * 60)
if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Monitor de precios de Falabella con alertas via ntfy.sh
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
    # Si existe config.json lo carga, si no construye la config desde variables de entorno
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    else:
        cfg = {}

    # Variables de entorno tienen prioridad sobre el archivo
    cfg["ntfy_tema"] = os.environ.get("NTFY_TEMA", cfg.get("ntfy_tema", ""))

    # URLs desde variable de entorno (separadas por coma) o desde el archivo
    urls_env = os.environ.get("URLS", "")
    if urls_env:
        cfg["urls"] = [u.strip() for u in urls_env.split(",") if u.strip()]

    # Valores por defecto para el resto de opciones
    cfg.setdefault("intervalo_minutos", int(os.environ.get("INTERVALO_MINUTOS", 30)))
    cfg.setdefault("umbral_descuento", float(os.environ.get("UMBRAL_DESCUENTO", 0.80)))
    cfg.setdefault("umbral_caida", float(os.environ.get("UMBRAL_CAIDA", 0.60)))
    cfg.setdefault("precio_minimo_clp", int(os.environ.get("PRECIO_MINIMO_CLP", 1000)))
    cfg.setdefault("min_muestras", int(os.environ.get("MIN_MUESTRAS", 5)))
    cfg.setdefault("pausa_entre_urls_seg", int(os.environ.get("PAUSA_ENTRE_URLS_SEG", 5)))
    cfg.setdefault("headless", True)

    if not cfg.get("ntfy_tema") or "PEGA_AQUI" in cfg["ntfy_tema"]:
        sys.exit("Falta el tema de ntfy. Configura la variable de entorno NTFY_TEMA.")
    if not cfg.get("urls"):
        sys.exit("No hay URLs para monitorear. Configura la variable de entorno URLS.")
    return cfg


# ------------------------------------------------------------------ #
#  BASE DE DATOS (historial de precios + alertas ya enviadas)
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
#  OBTENCIÓN DE DATOS (Playwright intercepta el JSON de la web)
# ------------------------------------------------------------------ #

def ensure_chromium():
    """Instala Chromium automaticamente si no esta disponible (necesario en Railway)."""
    import subprocess
    chromium_path = os.path.expanduser("~/.cache/ms-playwright")
    alt_path = "/ms-playwright"
    path = chromium_path if os.path.exists(chromium_path) else alt_path
    # busca si ya hay algun ejecutable de chromium
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
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=True
        )
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
        browser = pw.chromium.launch(headless=headless, args=["--disable-blink-features=AutomationControlled"])
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
            for _ in range(3):
                page.mouse.wheel(0, 4000)
                page.wait_for_timeout(1200)
        except Exception:
            pass

        # Plan B: datos embebidos en __NEXT_DATA__ (Next.js)
        try:
            nxt = page.eval_on_selector("#__NEXT_DATA__", "el => el.textContent")
            if nxt:
                payloads.append(json.loads(nxt))
        except Exception:
            pass

        # Plan B2: schema.org ItemList + precio normal del DOM (Paris.cl y similares)
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
                            // Precio tachado (precio normal) desde el DOM
                            const normalEls = Array.from(document.querySelectorAll('.ui-line-through'))
                                .filter(el => el.textContent.trim().startsWith('$'));
                            return items.map((p, i) => {
                                const normalRaw = normalEls[i] ? normalEls[i].textContent.trim() : null;
                                const normal = normalRaw ? parseInt(normalRaw.replace(/[^\\d]/g, '')) : p.offers.price;
                                return {
                                    sku: p.sku || p.name,
                                    nombre: p.name,
                                    precio: p.offers.price,
                                    normal: normal,
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

        # Plan C: extraer productos del DOM (para tiendas que renderizan en HTML)
        try:
            dom_products = page.evaluate("""() => {
                const results = [];
                const links = document.querySelectorAll('a[href]');
                for (const a of links) {
                    const text = a.textContent || '';
                    if (!text.includes('$') || text.length > 600 || text.length < 20) continue;
                    const priceMatches = text.match(/\\$[\\d.,]+/g);
                    if (!priceMatches || priceMatches.length === 0) continue;
                    const href = a.href || '';
                    if (!href.includes('.cl/') && !href.includes('.com/')) continue;
                    results.push({href: href, text: text});
                }
                return results;
            }""")
            if dom_products:
                payloads.append({"_dom_products": dom_products})
        except Exception:
            pass

        browser.close()
    return payloads


# ------------------------------------------------------------------ #
#  PARSER: extrae productos de cualquier estructura JSON encontrada
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


def parsear_dom_product(item):
    text = item.get("text", "")
    href = item.get("href", "")
    precios_raw = re.findall(r"\$\d{1,3}(?:\.\d{3})+", text)
    if not precios_raw:
        return None
    precios = []
    for p in precios_raw:
        digitos = re.sub(r"[^\d]", "", p)
        if digitos:
            precios.append(int(digitos))
    precios = [p for p in precios if p >= 1000]
    if not precios:
        return None
    for pat in precios_raw:
        text = text.replace(pat, " ")
    nombre = re.sub(r"\s+", " ", text).strip()
    for basura in ("Vista Previa", "Agregar al carro", "Despacho Gratis",
                   "Retiro en tienda", "Ver producto", "Añadir", "Agregar",
                   "cuotas sin interés", "6 cuotas sin interés"):
        nombre = nombre.replace(basura, "")
    nombre = re.sub(r"\d+%", "", nombre)
    nombre = re.sub(r"\(\d+\)", "", nombre)
    nombre = re.sub(r"\s+", " ", nombre).strip()
    if len(nombre) < 5:
        return None
    sku = re.sub(r"[^\w]", "", href.split("/")[-1])[:60] or nombre
    return {
        "sku": sku,
        "nombre": nombre[:120],
        "precio": min(precios),
        "normal": max(precios),
        "url": href,
    }


def extraer_productos(obj, encontrados=None, vistos=None):
    if encontrados is None:
        encontrados, vistos = [], set()

    if isinstance(obj, dict) and "_schema_products" in obj:
        for item in obj["_schema_products"]:
            if item.get("sku") and item.get("sku") not in vistos:
                vistos.add(item["sku"])
                encontrados.append(item)
        return encontrados

    if isinstance(obj, dict) and "_dom_products" in obj:
        for item in obj["_dom_products"]:
            prod = parsear_dom_product(item)
            if prod and prod["sku"] not in vistos:
                vistos.add(prod["sku"])
                encontrados.append(prod)
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
#  DETECCIÓN DE POSIBLES ERRORES DE PRECIO
# ------------------------------------------------------------------ #

def evaluar(con, prod, cfg):
    precio, normal = prod["precio"], prod["normal"]
    if precio <= 0:
        return None
    if precio < cfg.get("precio_minimo_clp", 1000):
        return None

    motivos = []
    nivel = None  # "super" o "normal"

    if normal and normal > 0:
        desc = 1 - precio / normal
        if desc >= cfg.get("umbral_error", 0.80):
            motivos.append(f"{desc*100:.0f}% bajo el precio normal "
                           f"(${normal:,} -> ${precio:,})".replace(",", "."))
            nivel = "super"
        elif desc >= cfg.get("umbral_descuento", 0.65):
            motivos.append(f"{desc*100:.0f}% bajo el precio normal "
                           f"(${normal:,} -> ${precio:,})".replace(",", "."))
            if nivel is None:
                nivel = "normal"

    ref = precio_referencia(con, prod["sku"], cfg.get("min_muestras", 5))
    if ref:
        caida = 1 - precio / ref
        if caida >= cfg.get("umbral_caida", 0.60):
            motivos.append(f"{caida*100:.0f}% bajo su precio habitual "
                           f"(~${int(ref):,} -> ${precio:,})".replace(",", "."))
            if nivel is None:
                nivel = "normal"

    if not motivos:
        return None
    return nivel, " | ".join(motivos)


# ------------------------------------------------------------------ #
#  NTFY
# ------------------------------------------------------------------ #

def enviar_ntfy(cfg, titulo, cuerpo, url_producto="", super_alerta=False):
    tema = cfg["ntfy_tema"]
    headers = {
        "Title":    titulo.encode("utf-8"),
        "Priority": "max" if super_alerta else "high",
        "Tags":     "rotating_light,rotating_light,fire,moneybag" if super_alerta else "moneybag",
    }
    if url_producto:
        headers["Click"] = url_producto

    try:
        r = requests.post(
            f"https://ntfy.sh/{tema}",
            data=cuerpo.encode("utf-8"),
            headers=headers,
            timeout=20,
        )
        if not r.ok:
            print(f"  ! ntfy respondio {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"  ! Error enviando a ntfy: {e}")


# ------------------------------------------------------------------ #
#  CICLO PRINCIPAL
# ------------------------------------------------------------------ #

def revisar_una_vez(con, cfg):
    total, alertas = 0, 0
    for url in cfg["urls"]:
        print(f"[{datetime.now():%H:%M:%S}] Revisando: {url}")
        productos = []
        for payload in fetch_payloads(url, headless=cfg.get("headless", True)):
            productos.extend(extraer_productos(payload))
        unicos = {p["sku"]: p for p in productos}.values()
        print(f"  -> {len(unicos)} productos detectados")

        for prod in unicos:
            total += 1
            registrar_precio(con, prod)
            resultado = evaluar(con, prod, cfg)
            if resultado:
                nivel, motivo = resultado
                clave = f"{prod['sku']}@{prod['precio']}"
                if not ya_alertado(con, clave):
                    marcar_alertado(con, clave)
                    alertas += 1
                    if nivel == "super":
                        titulo = f"POSIBLE ERROR DE PRECIO: {prod['nombre'][:45]}"
                    else:
                        titulo = f"Oferta interesante: {prod['nombre'][:50]}"
                    cuerpo = (f"Precio: ${prod['precio']:,}\n"
                              f"{motivo}").replace(",", ".")
                    enviar_ntfy(cfg, titulo, cuerpo, prod.get("url", ""), super_alerta=(nivel == "super"))
                    print(f"  ALERTA ({nivel}): {prod['nombre']} -> ${prod['precio']}")

        time.sleep(cfg.get("pausa_entre_urls_seg", 5))
    print(f"  Resumen: {total} productos, {alertas} alertas nuevas\n")


def main():
    cfg  = load_config()
    con  = init_db()
    intervalo = cfg.get("intervalo_minutos", 30)

    if "--test" in sys.argv:
        enviar_ntfy(cfg, "Monitor OK", "Las notificaciones funcionan correctamente.")
        print("Notificacion de prueba enviada.")
        return

    if "--once" in sys.argv:
        revisar_una_vez(con, cfg)
        return

    print(f"Monitor iniciado. Revisando cada {intervalo} min. (Ctrl+C para salir)\n")
    enviar_ntfy(cfg, "Monitor iniciado", "El monitor de precios Falabella esta activo.")
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
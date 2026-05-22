# agent/main.py — Serveur FastAPI + Webhook WhatsApp pour Tima / SAMANTAN

import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
from dotenv import load_dotenv

from agent.brain import generar_respuesta
from agent.memory import inicializar_db, guardar_mensaje, obtener_historial
from agent.providers import obtener_proveedor

load_dotenv(override=True)

ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
log_level = logging.DEBUG if ENVIRONMENT == "development" else logging.INFO
logging.basicConfig(level=log_level)
logger = logging.getLogger("agentkit")

proveedor = obtener_proveedor()
PORT = int(os.getenv("PORT", 8000))


@asynccontextmanager
async def lifespan(app: FastAPI):
    await inicializar_db()
    logger.info("Base de données initialisée")
    logger.info(f"Tima (SAMANTAN) démarrée sur le port {PORT}")
    logger.info(f"Fournisseur WhatsApp : {proveedor.__class__.__name__}")

    # ── Tâches de démarrage en arrière-plan (sans bloquer le démarrage) ─────────
    import asyncio

    async def _scraper_background():
        try:
            from agent.web_scraper import scraper_samantan
            logger.info("Scraping SAMANTAN en arrière-plan...")
            await asyncio.wait_for(scraper_samantan(), timeout=60.0)
            logger.info("Contenu SAMANTAN chargé ✓")
        except asyncio.TimeoutError:
            logger.warning("Scraping SAMANTAN timeout (60s) — Tima fonctionne sans le site")
        except Exception as e:
            logger.warning(f"Scraping SAMANTAN échoué : {e} — Tima fonctionne normalement")

    async def _prechauffer_catalogue():
        try:
            from agent.web_scraper import prechauffer_catalogue
            await asyncio.wait_for(prechauffer_catalogue(), timeout=55.0)
        except asyncio.TimeoutError:
            logger.warning("Préchauffage catalogue timeout (55s) — le cache sera rempli au 1er appel")
        except Exception as e:
            logger.warning(f"Préchauffage catalogue : {e} — Tima fonctionne normalement")

    async def _scraper_pages():
        try:
            from agent.web_scraper import scraper_pages_samantan
            logger.info("Scraping pages SAMANTAN (ordonnances, réseau)...")
            await asyncio.wait_for(scraper_pages_samantan(), timeout=90.0)
            logger.info("Pages SAMANTAN mémorisées ✓")
        except asyncio.TimeoutError:
            logger.warning("Scraper pages timeout (90s)")
        except Exception as e:
            logger.warning(f"Scraper pages : {e}")

    asyncio.create_task(_scraper_background())
    asyncio.create_task(_prechauffer_catalogue())
    asyncio.create_task(_scraper_pages())

    async def _rafraichir_catalogue_24h():
        """Rafraîchit le catalogue toutes les 24h en arrière-plan."""
        while True:
            await asyncio.sleep(24 * 60 * 60)  # attendre 24h
            logger.info("Rafraîchissement catalogue SAMANTAN (cycle 24h)...")
            try:
                from agent.web_scraper import prechauffer_catalogue
                await asyncio.wait_for(prechauffer_catalogue(), timeout=120.0)
                logger.info("Catalogue rafraîchi ✓")
            except asyncio.TimeoutError:
                logger.warning("Rafraîchissement catalogue timeout — prochain cycle dans 24h")
            except Exception as e:
                logger.warning(f"Rafraîchissement catalogue échoué : {e}")

    asyncio.create_task(_rafraichir_catalogue_24h())

    yield


app = FastAPI(
    title="Tima — Agent WhatsApp SAMANTAN",
    version="1.0.0",
    lifespan=lifespan
)


@app.get("/")
async def health_check():
    return {"status": "ok", "agent": "Tima", "business": "SAMANTAN"}


@app.get("/inspect-formulaire")
async def inspect_formulaire():
    """
    Analyse le formulaire /nouvelle-ordonnance sans le soumettre.
    Retourne les champs, sélecteurs d'opticiens, et structure complète.
    """
    import httpx, os
    from bs4 import BeautifulSoup

    SAMANTAN_URL = os.getenv("SAMANTAN_SITE_URL", "https://samantan.net")
    email = os.getenv("SAMANTAN_LOGIN_EMAIL")
    pwd = os.getenv("SAMANTAN_LOGIN_PASSWORD")

    async with httpx.AsyncClient(follow_redirects=False, timeout=20.0) as c:
        # Login
        await c.get(f"{SAMANTAN_URL}/connexion-samantan", timeout=10.0)
        r = await c.post(
            f"{SAMANTAN_URL}/connexion-samantan",
            data={"_method": "POST", "data[User][email]": email, "data[User][password]": pwd},
            timeout=15.0,
        )
        if r.status_code != 302:
            return {"error": f"Login échoué : {r.status_code}"}

        # GET formulaire
        r2 = await c.get(f"{SAMANTAN_URL}/nouvelle-ordonnance", follow_redirects=True, timeout=15.0)
        if "connexion" in str(r2.url):
            return {"error": "Redirigé vers login — accès refusé"}

        soup = BeautifulSoup(r2.text, "html.parser")

        # Extraire tous les champs
        champs = {}
        for form in soup.find_all("form"):
            action = form.get("action", "")
            method = form.get("method", "")
            champs[f"form[action={action}]"] = {"method": method, "fields": {}}
            for el in form.find_all(["input", "select", "textarea"]):
                name = el.get("name", "")
                if not name:
                    continue
                if el.name == "select":
                    options = [
                        {"value": o.get("value", ""), "label": o.get_text(strip=True)}
                        for o in el.find_all("option")
                    ]
                    champs[f"form[action={action}]"]["fields"][name] = {
                        "type": "select", "options": options[:30]
                    }
                else:
                    champs[f"form[action={action}]"]["fields"][name] = {
                        "type": el.get("type", "text"),
                        "value": el.get("value", ""),
                    }

        # Chercher sélecteur opticien
        opticiens = []
        for sel in soup.find_all("select"):
            opts = sel.find_all("option")
            if len(opts) > 3 and any(
                kw in (sel.get("name", "") + " ".join(o.get_text() for o in opts)).lower()
                for kw in ["opticien", "client", "user", "pharmacie", "boutique"]
            ):
                opticiens.append({
                    "select_name": sel.get("name"),
                    "options": [{"value": o.get("value"), "label": o.get_text(strip=True)} for o in opts]
                })

        return {
            "url_finale": str(r2.url),
            "page_chars": len(r2.text),
            "formulaires": champs,
            "selecteurs_opticiens_detectes": opticiens,
            "tous_les_selects": [
                {"name": s.get("name"), "options_count": len(s.find_all("option"))}
                for s in soup.find_all("select")
            ]
        }


@app.get("/test-login")
async def test_login():
    """Diagnostique pas-à-pas la connexion à samantan.net."""
    import httpx, os, time
    SAMANTAN_URL = os.getenv("SAMANTAN_SITE_URL", "https://samantan.net")
    email = os.getenv("SAMANTAN_LOGIN_EMAIL", "NON_DEFINI")
    pwd = os.getenv("SAMANTAN_LOGIN_PASSWORD", "NON_DEFINI")
    cat_url = (
        f"{SAMANTAN_URL}/liste-detaillee-des-produits"
        "?laboratoire_id=all&traitement=all&statut=1&stock=0"
    )
    result = {
        "credentials": {
            "email": email,
            "password_length": len(pwd),
            "password_ok": pwd not in ("NON_DEFINI", "5M4BIY7", ""),
        },
        "steps": {}
    }
    try:
        t0 = time.monotonic()
        async with httpx.AsyncClient(follow_redirects=False, timeout=20.0) as c:
            # Étape 1 — GET page login
            r1 = await c.get(f"{SAMANTAN_URL}/connexion-samantan", timeout=10.0)
            result["steps"]["1_get_login"] = {"status": r1.status_code, "ms": int((time.monotonic()-t0)*1000)}

            # Étape 2 — POST login
            t1 = time.monotonic()
            r2 = await c.post(
                f"{SAMANTAN_URL}/connexion-samantan",
                data={
                    "_method": "POST",
                    "data[User][email]": email,
                    "data[User][password]": pwd,
                },
                timeout=15.0
            )
            location = r2.headers.get("location", "")
            login_ok = r2.status_code == 302 and "connexion" not in location
            result["steps"]["2_post_login"] = {
                "status": r2.status_code,
                "location": location,
                "login_success": login_ok,
                "cookies": list(c.cookies.keys()),
                "ms": int((time.monotonic()-t1)*1000)
            }

            # Étape 3 — GET catalogue (seulement si login ok)
            if login_ok:
                t2 = time.monotonic()
                r3 = await c.get(cat_url, follow_redirects=True, timeout=25.0)
                result["steps"]["3_get_catalogue"] = {
                    "status": r3.status_code,
                    "final_url": str(r3.url),
                    "content_chars": len(r3.text),
                    "redirected_to_login": "connexion" in str(r3.url),
                    "ms": int((time.monotonic()-t2)*1000)
                }
                # Étape 4 — GET ordonnances
                t3 = time.monotonic()
                ord_url = f"{SAMANTAN_URL}/liste-des-ordonnances"
                r4 = await c.get(ord_url, follow_redirects=True, timeout=15.0)
                result["steps"]["4_get_ordonnances"] = {
                    "status": r4.status_code,
                    "final_url": str(r4.url),
                    "content_chars": len(r4.text),
                    "redirected_to_login": "connexion" in str(r4.url),
                    "ms": int((time.monotonic()-t3)*1000)
                }
            else:
                result["steps"]["3_get_catalogue"] = "skipped — login failed"
                result["steps"]["4_get_ordonnances"] = "skipped — login failed"
    except Exception as e:
        result["error"] = str(e)
    return result


@app.get("/debug")
async def debug():
    """État interne du serveur — utile pour diagnostiquer en production."""
    from agent.web_scraper import _catalogue_cache, _CACHE_TTL_SECS
    import time
    cache_age = int(time.monotonic() - _catalogue_cache["ts"]) if _catalogue_cache["data"] else None
    return {
        "provider": proveedor.__class__.__name__,
        "meta_phone_id": os.getenv("META_PHONE_NUMBER_ID", "NON CONFIGURÉ"),
        "meta_token_set": bool(os.getenv("META_ACCESS_TOKEN")),
        "anthropic_key_set": bool(os.getenv("ANTHROPIC_API_KEY")),
        "catalogue_cache": {
            "loaded": _catalogue_cache["data"] is not None,
            "size_chars": len(_catalogue_cache["data"]) if _catalogue_cache["data"] else 0,
            "age_seconds": cache_age,
            "ttl_seconds": int(_CACHE_TTL_SECS),
        },
        "environment": ENVIRONMENT,
    }


@app.get("/simuler-prix")
async def simuler_prix():
    """
    Simule une ordonnance test pour chaque opticien du réseau SAMANTAN
    et extrait les prix affichés — sans créer de commande réelle.

    Ordonnance test : OD/OG Sph 0.00 / Cyl -0.25 / Axe 90 | Add +1.00 | DIP 32/32 | Haut 20/20

    Sauvegarde les résultats dans knowledge/prix_opticiens.md
    (automatiquement chargé dans le contexte de Tima).
    """
    from agent.web_scraper import simuler_prix_par_opticien
    try:
        resultat = await simuler_prix_par_opticien()
        return resultat
    except Exception as e:
        logger.error(f"Erreur simuler_prix : {e}")
        return {"erreur": str(e)}


@app.get("/webhook")
async def webhook_verificacion(request: Request):
    """Vérification GET du webhook (requis par Meta, no-op pour Twilio)."""
    resultado = await proveedor.validar_webhook(request)
    if resultado is not None:
        return PlainTextResponse(str(resultado))
    return {"status": "ok"}


@app.post("/webhook")
async def webhook_handler(request: Request):
    """
    Reçoit les messages WhatsApp, génère une réponse avec Claude
    et la renvoie au client via Twilio.
    """
    try:
        mensajes = await proveedor.parsear_webhook(request)

        for msg in mensajes:
            if msg.es_propio or not msg.texto:
                continue

            logger.info(f"Message de {msg.telefono} : {msg.texto}")

            historial = await obtener_historial(msg.telefono)
            respuesta = await generar_respuesta(msg.texto, historial)

            await guardar_mensaje(msg.telefono, "user", msg.texto)
            await guardar_mensaje(msg.telefono, "assistant", respuesta)

            await proveedor.enviar_mensaje(msg.telefono, respuesta)
            logger.info(f"Réponse envoyée à {msg.telefono} : {respuesta[:80]}...")

        return {"status": "ok"}

    except Exception as e:
        logger.error(f"Erreur webhook : {e}")
        raise HTTPException(status_code=500, detail=str(e))

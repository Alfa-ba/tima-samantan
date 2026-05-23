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

    async def _scraper_prix_background():
        """Scrape les prix opticiens au démarrage (après les autres tâches)."""
        try:
            await asyncio.sleep(30)  # laisser le catalogue se charger d'abord
            logger.info("Scraping prix opticiens au démarrage...")
            await asyncio.wait_for(_run_scraping_prix(limite=0), timeout=600.0)
            logger.info("Prix opticiens chargés ✓")
        except asyncio.TimeoutError:
            logger.warning("Scraping prix opticiens timeout (10min)")
        except Exception as e:
            logger.warning(f"Scraping prix opticiens : {e}")

    asyncio.create_task(_scraper_prix_background())

    async def _maintenance_2h_quotidienne():
        """
        Maintenance quotidienne à 2h du matin (UTC = heure Dakar) :
          1. Bascule Tima sur claude-sonnet-4-6 (plus puissant pour les mises à jour)
          2. Rafraîchit : catalogue, prix opticiens, pages SAMANTAN, collaborateurs
          3. À 2h30 → retour à claude-haiku-4-5 (économique pour les conversations)

        (Dakar = UTC+0, donc 2h local = 2h UTC sur Railway)
        """
        from datetime import datetime, timedelta
        from agent.brain import set_modele

        while True:
            # ── Calculer le temps jusqu'au prochain 2h00 ──────────────────────
            maintenant = datetime.utcnow()
            prochain_2h = maintenant.replace(hour=2, minute=0, second=0, microsecond=0)
            if maintenant >= prochain_2h:
                prochain_2h += timedelta(days=1)

            attente_sec = (prochain_2h - maintenant).total_seconds()
            h = int(attente_sec // 3600)
            m = int((attente_sec % 3600) // 60)
            logger.info(
                f"Prochaine maintenance Tima : "
                f"{prochain_2h.strftime('%Y-%m-%d %H:%M')} UTC "
                f"(dans {h}h{m:02d}min)"
            )

            await asyncio.sleep(attente_sec)

            # ══════════════════════════════════════════════════════════════════
            # 2h00 UTC — DÉBUT MAINTENANCE
            # ══════════════════════════════════════════════════════════════════
            logger.info("=== MAINTENANCE 2h00 UTC — Tima passe en mode Sonnet 4.6 ===")

            # ── Étape 1 : Basculer sur Sonnet 4.6 ─────────────────────────────
            set_modele("claude-sonnet-4-6")
            logger.info("Modèle → claude-sonnet-4-6 ✓")

            # ── Étape 2 : Rafraîchissement catalogue ──────────────────────────
            try:
                from agent.web_scraper import prechauffer_catalogue
                logger.info("Rafraîchissement catalogue SAMANTAN...")
                await asyncio.wait_for(prechauffer_catalogue(), timeout=120.0)
                logger.info("Catalogue mis à jour ✓")
            except Exception as e:
                logger.warning(f"Catalogue 2h : {e}")

            # ── Étape 3 : Rafraîchissement prix opticiens ─────────────────────
            logger.info("Rafraîchissement prix opticiens...")
            try:
                await asyncio.wait_for(_run_scraping_prix(limite=0), timeout=600.0)
                logger.info("Prix opticiens mis à jour ✓")
            except asyncio.TimeoutError:
                logger.warning("Rafraîchissement prix 2h : timeout (10min)")
            except Exception as e:
                logger.warning(f"Rafraîchissement prix 2h : {e}")

            # ── Étape 4 : Pages SAMANTAN (ordonnances, réseau opticiens) ──────
            try:
                from agent.web_scraper import scraper_pages_samantan
                logger.info("Rafraîchissement pages SAMANTAN...")
                await asyncio.wait_for(scraper_pages_samantan(), timeout=90.0)
                logger.info("Pages SAMANTAN mises à jour ✓")
            except Exception as e:
                logger.warning(f"Pages SAMANTAN 2h : {e}")

            # ── Étape 5 : Collaborateurs / utilisateurs autorisés ─────────────
            try:
                from agent.web_scraper import scraper_collaborateurs
                logger.info("Rafraîchissement liste collaborateurs...")
                await asyncio.wait_for(scraper_collaborateurs(), timeout=60.0)
                logger.info("Collaborateurs mis à jour ✓")
            except Exception as e:
                logger.warning(f"Collaborateurs 2h : {e}")

            # ── Invalider le cache system prompt (nouvelles données chargées) ──
            try:
                from agent.brain import invalider_cache_system_prompt
                invalider_cache_system_prompt()
                logger.info("Cache system prompt invalidé ✓")
            except Exception as e:
                logger.warning(f"Invalidation cache : {e}")

            logger.info("=== MAINTENANCE 2h00 UTC — Mises à jour terminées ===")

            # ── Attendre 30 minutes avant de rebasculer sur Haiku ─────────────
            logger.info("Attente 30 minutes avant retour Haiku 4.5...")
            await asyncio.sleep(30 * 60)

            # ══════════════════════════════════════════════════════════════════
            # 2h30 UTC — RETOUR HAIKU 4.5
            # ══════════════════════════════════════════════════════════════════
            set_modele("claude-haiku-4-5")
            logger.info("=== 2h30 UTC — Modèle → claude-haiku-4-5 (mode conversation) ✓ ===")

    asyncio.create_task(_maintenance_2h_quotidienne())

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
    from pathlib import Path
    import time
    cache_age = int(time.monotonic() - _catalogue_cache["ts"]) if _catalogue_cache["data"] else None

    # ── État des fichiers knowledge/ ──────────────────────────────────────────
    knowledge_dir = Path("knowledge")
    fichiers_knowledge = {}
    if knowledge_dir.exists():
        for f in sorted(knowledge_dir.glob("*.md")):
            try:
                stat = f.stat()
                fichiers_knowledge[f.name] = {
                    "taille_bytes": stat.st_size,
                    "taille_kb": round(stat.st_size / 1024, 1),
                }
            except Exception:
                fichiers_knowledge[f.name] = {"erreur": "inaccessible"}

    # ── System prompt size estimate ────────────────────────────────────────────
    try:
        from agent.brain import cargar_system_prompt
        sp = cargar_system_prompt()
        system_prompt_chars = len(sp)
    except Exception as e:
        system_prompt_chars = f"erreur: {e}"

    return {
        "provider": proveedor.__class__.__name__,
        "meta_phone_id": os.getenv("META_PHONE_NUMBER_ID", "NON CONFIGURÉ"),
        "meta_token_set": bool(os.getenv("META_ACCESS_TOKEN")),
        "meta_token_debut": (os.getenv("META_ACCESS_TOKEN") or "")[:20] + "..." if os.getenv("META_ACCESS_TOKEN") else None,
        "anthropic_key_set": bool(os.getenv("ANTHROPIC_API_KEY")),
        "catalogue_cache": {
            "loaded": _catalogue_cache["data"] is not None,
            "size_chars": len(_catalogue_cache["data"]) if _catalogue_cache["data"] else 0,
            "age_seconds": cache_age,
            "ttl_seconds": int(_CACHE_TTL_SECS),
        },
        "knowledge_files": fichiers_knowledge,
        "system_prompt_chars": system_prompt_chars,
        "prix_status": _prix_status,
        "environment": ENVIRONMENT,
    }


@app.get("/test-claude-direct")
async def test_claude_direct(model: str = "claude-3-5-haiku-20241022"):
    """Test minimal de l'API Claude — teste le modèle passé en paramètre."""
    import anthropic
    key = os.getenv("ANTHROPIC_API_KEY", "")
    try:
        c = anthropic.AsyncAnthropic(api_key=key)
        r = await c.messages.create(
            model=model,
            max_tokens=100,
            messages=[{"role": "user", "content": "Dis juste: OK"}]
        )
        return {
            "status": "ok",
            "model": model,
            "reponse": r.content[0].text if r.content else "",
            "key_debut": key[:20] + "...",
        }
    except Exception as e:
        return {
            "status": "erreur",
            "model": model,
            "type": type(e).__name__,
            "message": str(e),
            "key_debut": key[:20] + "...",
        }


@app.get("/test-claude-full")
async def test_claude_full():
    """Reproduit l'appel exact de generar_respuesta (system+cache+tools+Haiku) sans masquer l'erreur."""
    import anthropic
    from agent.brain import cargar_system_prompt, TOOLS
    key = os.getenv("ANTHROPIC_API_KEY", "")
    try:
        c = anthropic.AsyncAnthropic(api_key=key)
        sp = cargar_system_prompt()
        r = await c.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1024,
            system=[{"type": "text", "text": sp, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": "Salut"}],
            tools=TOOLS
        )
        txt = next((b.text for b in r.content if getattr(b, "type", "") == "text"), "")
        return {"status": "ok", "stop_reason": r.stop_reason, "reponse": txt[:300]}
    except Exception as e:
        return {"status": "erreur", "type": type(e).__name__, "message": str(e)}


@app.get("/test-tima")
async def test_tima(message: str = "Bonjour, quels sont vos progressifs ?"):
    """
    Teste la réponse de Tima directement (sans WhatsApp).
    Appelle Claude et retourne la réponse — utile pour déboguer sans smartphone.

    Exemples :
      /test-tima?message=Quels sont les progressifs disponibles ?
      /test-tima?message=Quels sont mes prix pour OPTIQUE PONTY ?
    """
    from agent.brain import generar_respuesta
    try:
        respuesta = await generar_respuesta(message, [])
        return {
            "message_entrant": message,
            "reponse_tima": respuesta,
            "longueur": len(respuesta),
        }
    except Exception as e:
        logger.error(f"test-tima erreur : {e}")
        return {"erreur": str(e), "type": type(e).__name__, "message_entrant": message}


## ── Suivi du scraping prix ────────────────────────────────────────────────────
_prix_status: dict = {
    "en_cours": False,
    "termine": False,
    "opticiens_traites": 0,
    "total": 0,
    "erreur": None,
    "debut": None,
    "fin": None,
}


async def _run_scraping_prix(limite: int = 0):
    """Tâche de fond : scrape tous les prix et met à jour le statut."""
    import asyncio
    from datetime import datetime
    from agent.web_scraper import scraper_prix_opticiens

    global _prix_status
    _prix_status["en_cours"] = True
    _prix_status["termine"] = False
    _prix_status["erreur"] = None
    _prix_status["debut"] = datetime.now().strftime("%H:%M:%S")
    _prix_status["fin"] = None

    try:
        logger.info(f"Scraping prix opticiens démarré en arrière-plan (limite={limite or 'tous'})")
        resultat = await scraper_prix_opticiens(limite=limite)
        _prix_status["opticiens_traites"] = resultat.get("opticiens_traites", 0)
        _prix_status["termine"] = True
        logger.info(f"Scraping prix terminé : {_prix_status['opticiens_traites']} opticiens")
    except Exception as e:
        _prix_status["erreur"] = str(e)
        logger.error(f"Erreur scraping prix : {e}")
    finally:
        _prix_status["en_cours"] = False
        _prix_status["fin"] = datetime.now().strftime("%H:%M:%S")


@app.get("/scraper-prix-opticiens")
async def scraper_prix_opticiens_endpoint(limite: int = 0):
    """
    Lance le scraping des prix en ARRIÈRE-PLAN et retourne immédiatement.
    Le scraping de 145 opticiens prend ~4 min — pas de timeout HTTP.

    Paramètre optionnel : ?limite=5 pour tester sur 5 opticiens.
    Suivi de la progression : GET /status-prix
    """
    import asyncio

    global _prix_status

    if _prix_status["en_cours"]:
        return {
            "status": "deja_en_cours",
            "message": f"Scraping en cours : {_prix_status['opticiens_traites']} opticiens traités",
            "suivi": "/status-prix",
        }

    asyncio.create_task(_run_scraping_prix(limite=limite))

    return {
        "status": "demarre",
        "message": (
            f"Scraping des {'tous les' if not limite else str(limite)} opticiens "
            f"lancé en arrière-plan."
        ),
        "suivi": "Vérifie la progression sur /status-prix",
        "fichier": "knowledge/prix_opticiens.md (disponible quand terminé)",
    }


@app.get("/status-prix")
async def status_prix():
    """Suivi du scraping des prix opticiens lancé par /scraper-prix-opticiens."""
    from pathlib import Path
    fichier_ok = Path("knowledge/prix_opticiens.md").exists()
    taille = Path("knowledge/prix_opticiens.md").stat().st_size if fichier_ok else 0
    return {
        **_prix_status,
        "fichier_sauvegarde": fichier_ok,
        "fichier_taille_bytes": taille,
    }


@app.get("/extraire-prix-js")
async def extraire_prix_js():
    """
    Parse la page /nouvelle-ordonnance (7 MB) pour extraire les données de prix
    embarquées dans les scripts JavaScript — sans soumettre aucun formulaire.
    Sauvegarde le résultat dans knowledge/prix_opticiens.md
    """
    import httpx, json, re as re2
    from bs4 import BeautifulSoup
    from datetime import datetime

    SAMANTAN_URL_D = os.getenv("SAMANTAN_SITE_URL", "https://samantan.net")
    email = os.getenv("SAMANTAN_LOGIN_EMAIL")
    pwd   = os.getenv("SAMANTAN_LOGIN_PASSWORD")

    async with httpx.AsyncClient(follow_redirects=False, timeout=60.0) as c:
        # Login
        await c.get(f"{SAMANTAN_URL_D}/connexion-samantan", timeout=10.0)
        r_l = await c.post(
            f"{SAMANTAN_URL_D}/connexion-samantan",
            data={"_method": "POST",
                  "data[User][email]": email,
                  "data[User][password]": pwd},
            timeout=15.0,
        )
        if r_l.status_code != 302:
            return {"erreur": f"Login échoué : {r_l.status_code}"}

        # GET la page du formulaire (7 MB)
        r_form = await c.get(
            f"{SAMANTAN_URL_D}/nouvelle-ordonnance",
            follow_redirects=True, timeout=50.0
        )
        html = r_form.text
        soup = BeautifulSoup(html, "html.parser")

        # ── 1. Chercher tous les blocs JSON dans les scripts ───────────────────
        json_trouves = []
        for script in soup.find_all("script"):
            txt = script.get_text()
            if not txt.strip():
                continue
            kws = ["prix", "price", "tarif", "produit", "opticien",
                   "montant", "total", "amount", "catalogue"]
            if not any(kw in txt.lower() for kw in kws):
                continue

            # Chercher des tableaux/objets JSON assignés à des variables
            # Patterns : var X = {...}  /  var X = [...]  /  const X = ...
            for pat in [
                r'(?:var|let|const)\s+(\w+)\s*=\s*(\{[\s\S]{20,5000}\})',
                r'(?:var|let|const)\s+(\w+)\s*=\s*(\[[\s\S]{20,5000}\])',
            ]:
                for m in re2.finditer(pat, txt):
                    varname = m.group(1)
                    raw     = m.group(2)
                    try:
                        obj = json.loads(raw)
                        json_trouves.append({
                            "variable": varname,
                            "type": type(obj).__name__,
                            "taille": len(obj) if isinstance(obj, (dict, list)) else None,
                            "apercu": str(obj)[:500],
                        })
                    except Exception:
                        # Pas du JSON valide, garder le texte brut
                        json_trouves.append({
                            "variable": varname,
                            "type": "string_raw",
                            "apercu": raw[:300],
                        })

        # ── 2. Chercher des balises <script> contenant des objets de prix ──────
        # Patterns alternatifs : window.X = ... / app.X = ...
        for pat in [
            r'window\.(\w+)\s*=\s*(\{[\s\S]{20,5000}\})',
            r'window\.(\w+)\s*=\s*(\[[\s\S]{20,5000}\])',
            r'app\.(\w+)\s*=\s*(\{[\s\S]{20,5000}\})',
        ]:
            for m in re2.finditer(pat, html):
                varname = m.group(1)
                raw = m.group(2)
                try:
                    obj = json.loads(raw)
                    json_trouves.append({
                        "variable": f"window.{varname}",
                        "type": type(obj).__name__,
                        "taille": len(obj) if isinstance(obj, (dict, list)) else None,
                        "apercu": str(obj)[:500],
                    })
                except Exception:
                    pass

        # ── 3. Chercher les data-* HTML contenant des prix ────────────────────
        data_attrs = []
        for el in soup.find_all(True):
            for attr, val in el.attrs.items():
                if not attr.startswith("data-"):
                    continue
                if not isinstance(val, str):
                    continue
                if any(kw in attr.lower() for kw in ["prix", "price", "tarif", "amount"]):
                    data_attrs.append({"attr": attr, "val": val[:200], "tag": el.name})
                elif re2.search(r'\d{3,}', val) and any(
                    kw in attr.lower() for kw in ["produit", "product", "id", "cost"]
                ):
                    data_attrs.append({"attr": attr, "val": val[:200], "tag": el.name})

        # ── 4. Chercher des tableaux HTML de prix ─────────────────────────────
        tables_prix = []
        for table in soup.find_all("table"):
            headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
            if not any(kw in " ".join(headers) for kw in [
                "prix", "price", "montant", "tarif", "fcfa", "total"
            ]):
                continue
            rows = []
            for tr in table.find_all("tr")[:20]:
                cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
                if cells:
                    rows.append(cells)
            if rows:
                tables_prix.append({"headers": headers, "rows": rows})

        # ── 5. Texte brut contenant des montants FCFA ─────────────────────────
        fcfa_matches = re2.findall(
            r'[\d\s]{3,}(?:FCFA|F\.CFA|CFA)',
            html, re2.IGNORECASE
        )
        fcfa_uniques = list(dict.fromkeys(f.strip() for f in fcfa_matches))[:50]

        # ── 6. Sauvegarder ce qu'on a trouvé ─────────────────────────────────
        lignes_md = [
            "# Prix par opticien — SAMANTAN (extraction JS)",
            f"_Extrait le {datetime.now().strftime('%Y-%m-%d %H:%M')}_",
            f"_Page formulaire : {len(html)} chars_",
            "",
            "## Variables JavaScript trouvées",
        ]
        if json_trouves:
            for j in json_trouves[:20]:
                lignes_md.append(
                    f"- `{j['variable']}` ({j['type']}"
                    + (f", {j['taille']} éléments" if j.get("taille") else "")
                    + f") : {j['apercu'][:200]}"
                )
        else:
            lignes_md.append("_Aucune variable JSON trouvée_")

        if tables_prix:
            lignes_md.append("\n## Tableaux de prix HTML")
            for t in tables_prix:
                lignes_md.append(f"Headers : {t['headers']}")
                for row in t["rows"][:10]:
                    lignes_md.append(f"  • {' | '.join(row)}")

        if fcfa_uniques:
            lignes_md.append("\n## Montants FCFA trouvés dans la page")
            for f in fcfa_uniques:
                lignes_md.append(f"  • {f}")

        from pathlib import Path
        kdir = Path("knowledge")
        kdir.mkdir(exist_ok=True)
        (kdir / "prix_opticiens.md").write_text("\n".join(lignes_md), encoding="utf-8")

        return {
            "page_chars": len(html),
            "scripts_avec_prix": len(json_trouves),
            "json_variables": json_trouves[:30],
            "data_attrs_prix": data_attrs[:20],
            "tables_html_prix": tables_prix[:5],
            "montants_fcfa": fcfa_uniques[:30],
            "fichier_sauvegarde": "knowledge/prix_opticiens.md",
        }


@app.get("/debug-post-formulaire")
async def debug_post_formulaire():
    """
    Diagnostic : soumet le formulaire /nouvelle-ordonnance pour le 1er opticien
    et retourne le corps brut de la réponse (erreurs CakePHP, validation, etc.)
    Sans créer de commande — uniquement pour voir ce que le serveur renvoie.
    """
    import httpx
    from bs4 import BeautifulSoup

    SAMANTAN_URL_D = os.getenv("SAMANTAN_SITE_URL", "https://samantan.net")
    email = os.getenv("SAMANTAN_LOGIN_EMAIL")
    pwd = os.getenv("SAMANTAN_LOGIN_PASSWORD")

    async with httpx.AsyncClient(follow_redirects=False, timeout=30.0) as c:
        # ── Login ──────────────────────────────────────────────────────────────
        await c.get(f"{SAMANTAN_URL_D}/connexion-samantan", timeout=10.0)
        r_login = await c.post(
            f"{SAMANTAN_URL_D}/connexion-samantan",
            data={"_method": "POST",
                  "data[User][email]": email,
                  "data[User][password]": pwd},
            timeout=15.0,
        )
        if r_login.status_code != 302:
            return {"erreur": f"Login échoué : {r_login.status_code}"}

        # ── GET formulaire ─────────────────────────────────────────────────────
        r_form = await c.get(
            f"{SAMANTAN_URL_D}/nouvelle-ordonnance",
            follow_redirects=True, timeout=15.0
        )
        soup = BeautifulSoup(r_form.text, "html.parser")

        # ── Extraire TOUS les champs du formulaire (y compris _Token) ──────────
        form_el = soup.find("form")
        payload: dict[str, str] = {}
        all_fields_info = []

        if form_el:
            for el in form_el.find_all(["input", "select", "textarea"]):
                name = el.get("name", "")
                if not name:
                    continue
                if el.name == "input":
                    t = el.get("type", "text").lower()
                    val = el.get("value", "")
                    payload[name] = val
                    all_fields_info.append({
                        "name": name, "type": t, "value": val[:80]
                    })
                elif el.name == "select":
                    opts = el.find_all("option")
                    all_opts = [{"v": o.get("value",""), "l": o.get_text(strip=True)} for o in opts]
                    first_val = next((o["v"] for o in all_opts if o["v"].strip()), "")
                    payload[name] = first_val
                    all_fields_info.append({
                        "name": name, "type": "select",
                        "options_count": len(opts),
                        "first_value": first_val,
                        "options_preview": all_opts[:5]
                    })
                elif el.name == "textarea":
                    payload[name] = ""
                    all_fields_info.append({"name": name, "type": "textarea"})

        # ── Trouver le 1er opticien ────────────────────────────────────────────
        opticien_field = None
        opticien_val = None
        opticien_label = None
        for info in all_fields_info:
            if info.get("type") == "select" and info.get("options_count", 0) > 3:
                n_lower = info["name"].lower()
                if any(kw in n_lower for kw in ["user", "opticien", "client"]):
                    opticien_field = info["name"]
                    opticien_val = info["first_value"]
                    opticien_label = (info.get("options_preview") or [{}])[1].get("l", "?") \
                        if len(info.get("options_preview", [])) > 1 else "premier"
                    break

        if opticien_field:
            payload[opticien_field] = opticien_val

        # ── POST avec le payload complet ───────────────────────────────────────
        r_post = await c.post(
            f"{SAMANTAN_URL_D}/nouvelle-ordonnance",
            data=payload,
            follow_redirects=True,
            timeout=20.0,
        )

        # Extraire le texte utile de la réponse
        soup_rep = BeautifulSoup(r_post.text, "html.parser")
        # Chercher les messages d'erreur CakePHP
        erreurs_cake = []
        for el in soup_rep.select(".error-message, .alert, .alert-danger, .error, .errors, .flash-message"):
            t = el.get_text(strip=True)
            if t:
                erreurs_cake.append(t[:200])

        # Texte brut (premier 6000 chars pour diagnostic)
        for tag in soup_rep(["script", "style", "meta", "link", "svg", "img"]):
            tag.decompose()
        texte_brut = soup_rep.get_text(separator="\n", strip=True)
        lignes_utiles = [l.strip() for l in texte_brut.split("\n") if len(l.strip()) > 3]

        return {
            "login_status": r_login.status_code,
            "form_get_status": r_form.status_code,
            "form_chars": len(r_form.text),
            "champs_formulaire": all_fields_info,
            "payload_envoye": {k: v[:60] for k, v in payload.items()},
            "post_status": r_post.status_code,
            "post_url_finale": str(r_post.url),
            "post_chars": len(r_post.text),
            "erreurs_detectees": erreurs_cake,
            "opticien_utilise": {"champ": opticien_field, "valeur": opticien_val, "label": opticien_label},
            "reponse_texte": "\n".join(lignes_utiles[:120]),
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


@app.get("/pause/{telefono}")
async def pause_conversation(telefono: str):
    """
    ⏸️ Met Tima en pause pour un client — l'équipe SAMANTAN prend le relais.
    Appeler : GET /pause/221XXXXXXXX
    """
    tel = telefono.strip().lstrip("+")
    _conversations_humain[tel] = True
    logger.info(f"⏸️ Relais humain activé manuellement pour {tel}")
    return {
        "status": "pause",
        "client": tel,
        "message": f"Tima en pause pour {tel}. Envoyer GET /resume/{tel} pour reprendre.",
    }


@app.get("/resume/{telefono}")
async def resume_conversation(telefono: str):
    """
    ✅ Rend le relais à Tima pour un client.
    Appeler : GET /resume/221XXXXXXXX
    """
    tel = telefono.strip().lstrip("+")
    if tel in _conversations_humain:
        del _conversations_humain[tel]
        logger.info(f"✅ Relais rendu à Tima pour {tel}")
        return {"status": "reprise", "client": tel, "message": f"Tima reprend pour {tel}."}
    return {"status": "deja_actif", "client": tel, "message": "Tima était déjà active pour ce client."}


@app.get("/relais")
async def liste_relais():
    """Liste toutes les conversations actuellement en pause (relais humain actif)."""
    return {
        "conversations_en_pause": list(_conversations_humain.keys()),
        "total": len(_conversations_humain),
        "info": "Tima est silencieuse pour ces numéros. GET /resume/{tel} pour reprendre.",
    }


@app.get("/webhook")
async def webhook_verificacion(request: Request):
    """Vérification GET du webhook (requis par Meta, no-op pour Twilio)."""
    resultado = await proveedor.validar_webhook(request)
    if resultado is not None:
        return PlainTextResponse(str(resultado))
    return {"status": "ok"}


# ── Déduplication des messages (évite les doublons Meta) ──────────────────────
_messages_traites: set = set()

# ── Relais humain — conversations en pause (gérées par l'équipe SAMANTAN) ─────
# Clé : numéro du client | Valeur : True = humain aux commandes, Tima en pause
_conversations_humain: dict[str, bool] = {}

SIGNAL_REPRISE = "#"  # L'équipe envoie "#" pour rendre le relais à Tima


import re as _re

def _est_message_samantan(texte: str) -> bool:
    """
    RÈGLE SIMPLE : Si SAMANTAN parle avec un client (opticien), Tima ne répond pas.

    Détecte tous les messages envoyés par le système SAMANTAN aux opticiens :
    notifications d'ordonnances, livraisons, statuts, etc.
    """
    if not texte:
        return False
    texte_lower = texte.strip().lower()

    # ── Règle 1 : référence d'ordonnance ORD-XXXXX (signature SAMANTAN) ───────
    if _re.search(r'\bORD-[A-Z0-9]{4,}\b', texte):
        return True

    # ── Règle 2 : signature de fin SAMANTAN ───────────────────────────────────
    if "samantan vous remercie" in texte_lower:
        return True

    # ── Règle 3 : mots-clés des notifications SAMANTAN ────────────────────────
    marqueurs = [
        "transmis au labo",
        "arrivées dans nos locaux",
        "arriveront à dakar",
        "à livrer dans la journée",
        "merci de prendre en charge",
        "ordonnances ci-dessous",
        "références ci-dessous",
        "l'équipe samantan",
        "mis à l'état",
        "état transmis",
    ]
    if any(m in texte_lower for m in marqueurs):
        return True

    return False


async def _traiter_message(msg, prov) -> None:
    """Traite un message en arrière-plan — Claude + envoi réponse."""
    try:
        # ── Ignorer les messages SAMANTAN → clients (notifications auto) ─────────
        if _est_message_samantan(msg.texto):
            logger.info(
                f"Message SAMANTAN ignoré (notification auto) "
                f"de {msg.telefono} : '{msg.texto[:60]}'"
            )
            return  # Pas de réponse, pas de sauvegarde

        logger.info(f"Traitement message de {msg.telefono} : {msg.texto}")
        historial = await obtener_historial(msg.telefono)
        respuesta = await generar_respuesta(msg.texto, historial, telefono=msg.telefono)
        await guardar_mensaje(msg.telefono, "user", msg.texto)
        await guardar_mensaje(msg.telefono, "assistant", respuesta)
        await prov.enviar_mensaje(msg.telefono, respuesta)
        logger.info(f"Réponse envoyée à {msg.telefono} : {respuesta[:80]}...")
    except Exception as e:
        logger.error(f"Erreur traitement message {msg.telefono} : {e}")


@app.post("/webhook")
async def webhook_handler(request: Request):
    """
    Reçoit les messages WhatsApp et retourne 200 IMMÉDIATEMENT à Meta.
    Le traitement (Claude + envoi) se fait en arrière-plan.
    Déduplication par message_id pour éviter les doublons lors des retries Meta.
    """
    import asyncio

    try:
        mensajes = await proveedor.parsear_webhook(request)

        for msg in mensajes:
            if msg.es_propio or not msg.texto:
                continue

            # ── Déduplication : ignorer si déjà traité ─────────────────────────
            if msg.mensaje_id and msg.mensaje_id in _messages_traites:
                logger.info(f"Message {msg.mensaje_id} déjà traité — doublon ignoré")
                continue

            if msg.mensaje_id:
                _messages_traites.add(msg.mensaje_id)
                if len(_messages_traites) > 500:
                    plus_vieux = next(iter(_messages_traites))
                    _messages_traites.discard(plus_vieux)

            # ── Vérifier si Tima est en pause pour ce client (relais humain) ───
            if _conversations_humain.get(msg.telefono):
                logger.info(
                    f"⏸️  Tima en pause pour {msg.telefono} — relais humain actif"
                )
                continue

            # ── Traitement en arrière-plan → Meta reçoit 200 en < 1 seconde ───
            asyncio.create_task(_traiter_message(msg, proveedor))

        # Retour immédiat à Meta — évite les retries et doublons
        return {"status": "ok"}

    except Exception as e:
        logger.error(f"Erreur webhook : {e}")
        return {"status": "ok"}  # Toujours 200 à Meta, même en cas d'erreur

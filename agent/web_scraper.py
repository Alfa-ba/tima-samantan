# agent/web_scraper.py — Accès au site SAMANTAN pour enrichir Tima

import os
import re
import time
import asyncio
import logging
import httpx
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(override=True)
logger = logging.getLogger("agentkit")

SAMANTAN_URL = os.getenv("SAMANTAN_SITE_URL", "https://samantan.net")
LOGIN_EMAIL = os.getenv("SAMANTAN_LOGIN_EMAIL", "tima@samantan.com")
LOGIN_PASSWORD = os.getenv("SAMANTAN_LOGIN_PASSWORD", "5M4BIY7")
KNOWLEDGE_FILE = Path("knowledge/samantan_web.md")

# ── Cache catalogue en mémoire (évite 50s de réseau à chaque message) ─────────
_catalogue_cache: dict = {"data": None, "ts": 0.0}
_CACHE_TTL_SECS: float = 25 * 60 * 60  # 25h (cycle de refresh = 24h, légère marge)

# ── Cache ordonnances (TTL court : les commandes changent souvent) ──────────────
_ordonnances_cache: dict = {"data": None, "ts": 0.0}
_ORDONNANCES_TTL_SECS: float = 5 * 60  # 5 minutes

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}


def _extraire_texte(html: str) -> str:
    """Extrait le texte propre d'une page HTML."""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "meta", "link", "svg",
                         "img", "noscript", "iframe", "header", "footer", "nav"]):
            tag.decompose()
        texte = soup.get_text(separator=" ", strip=True)
        texte = re.sub(r'\s+', ' ', texte).strip()
        return texte
    except Exception as e:
        logger.warning(f"Erreur extraction texte HTML : {e}")
        return ""


def _extraire_produits_actifs(html: str) -> str:
    """
    Extrait UNIQUEMENT les produits actifs/disponibles d'une page catalogue.
    Ignore les produits en rupture, inactifs ou désactivés.
    """
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")

        produits_actifs = []

        # ── Stratégie 1 : WooCommerce standard ─────────────────────────────
        # Chercher les produits avec classe 'instock' ou sans 'outofstock'
        produits = soup.select(
            "li.product, .product-item, .wc-block-grid__product, "
            "article.product, .produit, .catalogue-item"
        )

        for produit in produits:
            classes = " ".join(produit.get("class", []))

            # Ignorer les produits hors stock ou inactifs
            if any(mot in classes.lower() for mot in [
                "outofstock", "out-of-stock", "inactif", "inactive",
                "rupture", "epuise", "disabled", "unavailable"
            ]):
                continue

            # Nettoyer et extraire le texte du produit
            for tag in produit(["script", "style", "svg", "img"]):
                tag.decompose()
            texte = produit.get_text(separator=" ", strip=True)
            texte = re.sub(r'\s+', ' ', texte).strip()

            if len(texte) > 20:
                produits_actifs.append(f"• {texte}")

        # ── Stratégie 2 : Tableaux de produits ────────────────────────────
        if not produits_actifs:
            tables = soup.find_all("table")
            for table in tables:
                rows = table.find_all("tr")
                for row in rows:
                    texte = row.get_text(separator=" ", strip=True)
                    texte = re.sub(r'\s+', ' ', texte).strip()
                    # Ignorer les lignes qui mentionnent rupture/inactif
                    if any(mot in texte.lower() for mot in [
                        "rupture", "indisponible", "inactif", "out of stock"
                    ]):
                        continue
                    if len(texte) > 20:
                        produits_actifs.append(f"• {texte}")

        # ── Stratégie 3 : Fallback — texte général filtré ─────────────────
        if not produits_actifs:
            for tag in soup(["script", "style", "meta", "link", "svg",
                             "img", "noscript", "iframe", "header", "footer", "nav"]):
                tag.decompose()
            texte_complet = soup.get_text(separator="\n", strip=True)
            lignes = []
            for ligne in texte_complet.split("\n"):
                ligne = ligne.strip()
                if len(ligne) < 10:
                    continue
                if any(mot in ligne.lower() for mot in [
                    "rupture", "indisponible", "inactif", "out of stock",
                    "épuisé", "non disponible"
                ]):
                    continue
                lignes.append(ligne)
            return "\n".join(lignes[:150])

        return "\n".join(produits_actifs)

    except Exception as e:
        logger.warning(f"Erreur extraction produits actifs : {e}")
        return ""


async def scraper_samantan() -> str:
    """
    Accède au site SAMANTAN (pages publiques + authentifié) et extrait le contenu.
    Sauvegarde le résultat dans knowledge/samantan_web.md pour que Tima y ait accès.
    """
    contenu_pages = []

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=30.0,
        headers=HEADERS
    ) as client:

        # ── Étape 1 : Pages publiques ──────────────────────────────────────
        pages_publiques = [
            ("Accueil SAMANTAN", "/"),
            ("Produits", "/produits"),
            ("À propos", "/a-propos"),
            ("Contact", "/contact"),
            ("Catalogue", "/catalogue"),
        ]

        for nom, chemin in pages_publiques:
            try:
                r = await client.get(f"{SAMANTAN_URL}{chemin}", timeout=15.0)
                if r.status_code == 200:
                    texte = _extraire_texte(r.text)
                    if len(texte) > 200:
                        contenu_pages.append(
                            f"### {nom} ({SAMANTAN_URL}{chemin})\n{texte[:3000]}"
                        )
                        logger.info(f"Page publique récupérée : {nom}")
            except Exception as e:
                logger.warning(f"Page {chemin} inaccessible : {e}")

        # ── Étape 2 : Connexion authentifiée ──────────────────────────────
        try:
            # Récupérer le formulaire de login pour les tokens CSRF éventuels
            r_login_page = await client.get(
                f"{SAMANTAN_URL}/connexion-samantan", timeout=15.0
            )

            # Tenter la connexion avec les vrais champs
            login_data = {
                "_method": "POST",
                "data[User][email]": LOGIN_EMAIL,
                "data[User][password]": LOGIN_PASSWORD,
            }

            r_login = await client.post(
                f"{SAMANTAN_URL}/connexion-samantan",
                data=login_data,
                timeout=20.0
            )

            if r_login.status_code in [200, 302]:
                logger.info("Connexion SAMANTAN tentée — vérification des pages protégées")

                # Pages accessibles après connexion
                pages_auth = [
                    ("Mon compte", "/mon-compte"),
                    ("Boutique", "/boutique"),
                    ("Commandes", "/commandes"),
                    ("Tarifs", "/tarifs"),
                ]

                for nom, chemin in pages_auth:
                    try:
                        r = await client.get(
                            f"{SAMANTAN_URL}{chemin}", timeout=15.0
                        )
                        if r.status_code == 200:
                            texte = _extraire_texte(r.text)
                            if len(texte) > 200:
                                contenu_pages.append(
                                    f"### {nom} — espace pro ({SAMANTAN_URL}{chemin})\n{texte[:3000]}"
                                )
                                logger.info(f"Page authentifiée récupérée : {nom}")
                    except Exception as e:
                        logger.warning(f"Page auth {chemin} inaccessible : {e}")

        except Exception as e:
            logger.warning(f"Connexion authentifiée échouée : {e}")

    # ── Sauvegarde du résultat ─────────────────────────────────────────────
    if not contenu_pages:
        logger.warning("Aucun contenu récupéré depuis SAMANTAN")
        return ""

    resultat = "\n\n".join(contenu_pages)

    KNOWLEDGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    KNOWLEDGE_FILE.write_text(
        f"# Contenu récupéré depuis {SAMANTAN_URL}\n\n{resultat}",
        encoding="utf-8"
    )
    logger.info(f"Contenu SAMANTAN sauvegardé ({len(resultat)} caractères) → {KNOWLEDGE_FILE}")
    return resultat


def _extraire_liens_produits(html: str, base_url: str) -> list[tuple[str, str]]:
    """
    Extrait les liens vers les pages de détail des produits actifs.
    Retourne une liste de (nom_produit, url_produit).
    """
    liens = []
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")

        # Chercher les liens produits dans différentes structures
        selecteurs = [
            "li.product a.woocommerce-loop-product__link",
            "li.product h2 a",
            "li.product a[href]",
            ".product-item a[href]",
            ".catalogue-item a[href]",
            "article.product a[href]",
            ".produit a[href]",
            "a.product-link",
        ]

        vus = set()
        for sel in selecteurs:
            elements = soup.select(sel)
            for el in elements:
                href = el.get("href", "")
                nom = el.get_text(strip=True) or el.get("title", "")

                # Construire URL absolue
                if href.startswith("/"):
                    url = f"{base_url}{href}"
                elif href.startswith("http"):
                    url = href
                else:
                    continue

                # Éviter les doublons et les URLs non-produit
                if url in vus:
                    continue
                if any(x in url for x in ["#", "?add-to-cart", "/cart", "/panier"]):
                    continue

                vus.add(url)
                if not nom:
                    nom = url.split("/")[-1].replace("-", " ").title()
                liens.append((nom, url))

        logger.info(f"Liens produits trouvés : {len(liens)}")
    except Exception as e:
        logger.warning(f"Erreur extraction liens produits : {e}")

    return liens


def _extraire_details_produit(html: str) -> str:
    """
    Extrait TOUS les détails d'une page produit individuelle.
    Récupère : nom, description, caractéristiques, spécifications techniques,
    matière, traitement, disponibilité, etc.
    """
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")

        details = []

        # ── Nom du produit ─────────────────────────────────────────────────
        for sel in ["h1.product_title", "h1.entry-title", ".product-title h1",
                    "h1", ".product__title"]:
            el = soup.select_one(sel)
            if el:
                nom = el.get_text(strip=True)
                if nom:
                    details.append(f"Produit : {nom}")
                    break

        # ── Statut / disponibilité ─────────────────────────────────────────
        for sel in [".stock", ".availability", ".product-availability",
                    ".woocommerce-product-details__short-description .stock"]:
            el = soup.select_one(sel)
            if el:
                statut = el.get_text(strip=True)
                if statut:
                    details.append(f"Disponibilité : {statut}")
                    break

        # ── Description courte ─────────────────────────────────────────────
        for sel in [".woocommerce-product-details__short-description",
                    ".product-short-description", ".short-description",
                    ".product__description--short"]:
            el = soup.select_one(sel)
            if el:
                texte = el.get_text(separator=" ", strip=True)
                texte = re.sub(r'\s+', ' ', texte).strip()
                if texte:
                    details.append(f"Description : {texte}")
                    break

        # ── Description longue / onglets ───────────────────────────────────
        for sel in [".woocommerce-Tabs-panel--description",
                    "#tab-description", ".product-description",
                    ".woocommerce-product-details", ".product__content"]:
            el = soup.select_one(sel)
            if el:
                for tag in el(["script", "style", "img", "svg"]):
                    tag.decompose()
                texte = el.get_text(separator="\n", strip=True)
                texte = re.sub(r'\n{3,}', '\n\n', texte).strip()
                if len(texte) > 50:
                    details.append(f"Détails :\n{texte[:2000]}")
                    break

        # ── Attributs / caractéristiques techniques ────────────────────────
        for sel in [".woocommerce-product-attributes",
                    ".product-attributes", ".product__attributes",
                    "table.variations", ".product-details-table"]:
            table = soup.select_one(sel)
            if table:
                rows = table.find_all("tr")
                specs = []
                for row in rows:
                    label = row.find(["th", "td"])
                    valeur = row.find_all(["th", "td"])
                    if len(valeur) >= 2:
                        l = valeur[0].get_text(strip=True)
                        v = valeur[1].get_text(strip=True)
                        if l and v:
                            specs.append(f"  • {l} : {v}")
                if specs:
                    details.append("Caractéristiques techniques :\n" + "\n".join(specs))
                    break

        # ── Catégories / tags ──────────────────────────────────────────────
        for sel in [".posted_in", ".product_meta .cat-links",
                    ".product-categories", ".woocommerce-product-details__categories"]:
            el = soup.select_one(sel)
            if el:
                texte = el.get_text(strip=True)
                if texte:
                    details.append(f"Catégorie : {texte}")
                    break

        # ── Fallback : extraire tout le contenu principal ──────────────────
        if len(details) <= 1:
            for sel in [".product", "main", "article", "#content"]:
                el = soup.select_one(sel)
                if el:
                    for tag in el(["script", "style", "img", "svg", "nav",
                                   "header", "footer", ".related", ".upsells"]):
                        tag.decompose()
                    texte = el.get_text(separator="\n", strip=True)
                    texte = re.sub(r'\n{3,}', '\n\n', texte).strip()
                    if len(texte) > 100:
                        details.append(texte[:3000])
                        break

        return "\n".join(details) if details else ""

    except Exception as e:
        logger.warning(f"Erreur extraction détails produit : {e}")
        return ""


CATALOGUE_ACTIFS_URL = (
    f"{SAMANTAN_URL}/liste-detaillee-des-produits"
    "?laboratoire_id=all&traitement=all&statut=1&stock=0"
)


async def _connecter_samantan(client: httpx.AsyncClient) -> bool:
    """Se connecte à samantan.net — champs exacts du formulaire CakePHP."""
    try:
        await client.get(f"{SAMANTAN_URL}/connexion-samantan", timeout=10.0)
        r = await client.post(
            f"{SAMANTAN_URL}/connexion-samantan",
            data={
                "_method": "POST",
                "data[User][email]": LOGIN_EMAIL,
                "data[User][password]": LOGIN_PASSWORD,
            },
            timeout=15.0
        )
        # Succès = redirection 302 vers / (pas vers /connexion-samantan)
        succes = r.status_code == 302 and "connexion" not in r.headers.get("location", "connexion")
        logger.info(f"Connexion SAMANTAN : status={r.status_code} succès={succes}")
        return succes
    except Exception as e:
        logger.warning(f"Connexion SAMANTAN échouée : {e}")
        return False


async def _fetch_catalogue_raw() -> str:
    """
    Fetch brut du catalogue SAMANTAN depuis samantan.net.
    Retourne tous les produits actifs SANS filtre.
    N'appelle PAS directement — utilise fetch_catalogue_samantan() pour le cache.
    """
    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=30.0,
        headers=HEADERS
    ) as client:

        # ── Étape 1 : Login ────────────────────────────────────────────────
        # GET la page login d'abord (cookies de session initiaux)
        await client.get(f"{SAMANTAN_URL}/connexion-samantan", timeout=10.0)
        r_login = await client.post(
            f"{SAMANTAN_URL}/connexion-samantan",
            data={
                "_method": "POST",
                "data[User][email]": LOGIN_EMAIL,
                "data[User][password]": LOGIN_PASSWORD,
            },
            timeout=15.0
        )
        location = r_login.headers.get("location", "")
        # Succès = 302 vers une page qui n'est pas /connexion-samantan
        # Certains serveurs CakePHP retournent aussi 200 avec redirection JS
        login_ok = (r_login.status_code == 302 and "connexion" not in location) \
                   or (r_login.status_code == 200 and location == "")
        logger.info(
            f"Login : status={r_login.status_code} | location='{location}' | "
            f"cookies={list(client.cookies.keys())} | success={login_ok}"
        )

        # ── Étape 2 : Charger la page des produits actifs ──────────────────
        try:
            r = await client.get(CATALOGUE_ACTIFS_URL, follow_redirects=True, timeout=25.0)
            if r.status_code != 200 or "connexion" in str(r.url):
                logger.error(
                    f"Catalogue inaccessible : {r.status_code} | {r.url} — "
                    f"Login ok={login_ok}, email={LOGIN_EMAIL}, pwd_len={len(LOGIN_PASSWORD)}"
                )
                return "Catalogue momentanément inaccessible. Consulter www.samantan.net"
            html_catalogue = r.text
            logger.info(f"Catalogue chargé : {len(html_catalogue)} caractères")
        except Exception as e:
            logger.error(f"Erreur chargement catalogue : {e}")
            return "Catalogue momentanément inaccessible. Consulter www.samantan.net"

        # ── Étape 3 : Parser les produits actifs depuis le texte ──────────
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html_catalogue, "html.parser")
        all_text = soup.get_text(separator="\n", strip=True)
        lignes = all_text.split("\n")

        produits = []
        for i, ligne in enumerate(lignes):
            ligne = ligne.strip()
            if ligne == "ACTIF":
                # Remonter pour trouver la référence et le nom du produit
                contexte = [l.strip() for l in lignes[max(0, i-5):i+1] if l.strip()]
                if len(contexte) >= 3:
                    # Format : Ref | Nom | Traitements | Prix | ACTIF
                    ref = next((l for l in contexte if l.startswith("PRDD-")), "")
                    nom = next((l for l in contexte if l.startswith("SAM ")), "")
                    prix = next((l for l in contexte if l.replace(" ", "").isdigit()), "")
                    traitements = next((l for l in contexte if "HC" in l or "HMC" in l), "")

                    if nom:
                        produits.append({
                            "ref": ref,
                            "nom": nom,
                            "traitements": traitements,
                            "prix": prix,
                        })

        logger.info(f"Produits actifs extraits : {len(produits)}")

        if not produits:
            return "Aucun produit actif trouvé sur samantan.net"

        # ── Étape 4 : Formater la liste ────────────────────────────────────
        lignes_resultat = [f"[{len(produits)} PRODUITS ACTIFS — SAMANTAN]\n"]
        for p in produits:
            ligne_prod = f"• {p['nom']}"
            if p['ref']:
                ligne_prod += f" (Réf: {p['ref']})"
            if p['prix']:
                ligne_prod += f" — Prix base: {p['prix']} FCFA"
            if p['traitements']:
                ligne_prod += f"\n  Traitements dispo: {p['traitements']}"
            lignes_resultat.append(ligne_prod)

        return "\n".join(lignes_resultat)


# ── Wrapper public avec cache ──────────────────────────────────────────────────

async def fetch_catalogue_samantan(recherche: str = "") -> str:
    """
    Retourne le catalogue SAMANTAN actif avec cache 30 minutes.

    Ordre de priorité :
      1. Cache mémoire frais (< 30 min)  → instantané
      2. Fetch temps réel avec timeout 8s → réseau
      3. Cache périmé ou message fallback → si fetch échoue

    Args:
        recherche: Filtre optionnel (ex: 'progressif', 'transitions')
    """
    global _catalogue_cache
    now = time.monotonic()

    # ── 1. Cache frais ─────────────────────────────────────────────────────────
    if _catalogue_cache["data"] and (now - _catalogue_cache["ts"]) < _CACHE_TTL_SECS:
        age = int(now - _catalogue_cache["ts"])
        logger.info(f"Catalogue SAMANTAN depuis cache ({age}s / {int(_CACHE_TTL_SECS)}s TTL)")
        data = _catalogue_cache["data"]

    else:
        # ── 2. Fetch temps réel (8s max pour rester dans le timeout webhook Meta) ──
        logger.info("Fetch catalogue SAMANTAN (cache absent ou expiré)...")
        try:
            data = await asyncio.wait_for(_fetch_catalogue_raw(), timeout=8.0)
            if data and len(data) > 50:
                _catalogue_cache["data"] = data
                _catalogue_cache["ts"] = now
                logger.info(f"Cache catalogue mis à jour ({len(data)} chars)")
            else:
                data = (
                    _catalogue_cache["data"]
                    or "Catalogue momentanément inaccessible. Consultez www.samantan.net"
                )
        except asyncio.TimeoutError:
            logger.warning("Catalogue fetch timeout (8s) — fallback cache périmé ou message")
            data = (
                _catalogue_cache["data"]
                or "Catalogue momentanément inaccessible. Consultez www.samantan.net"
            )
        except Exception as e:
            logger.error(f"Erreur fetch catalogue : {e}")
            data = (
                _catalogue_cache["data"]
                or "Catalogue momentanément inaccessible. Consultez www.samantan.net"
            )

    # ── 3. Filtrer par mot-clé si demandé ─────────────────────────────────────
    if recherche and data and "inaccessible" not in data:
        lignes = data.split("\n")
        header = lignes[0] if lignes else ""
        filtrées = [header]
        i = 1
        while i < len(lignes):
            ligne = lignes[i]
            if ligne.startswith("•"):
                if recherche.lower() in ligne.lower():
                    filtrées.append(ligne)
                    # Inclure la ligne Traitements qui suit si présente
                    if i + 1 < len(lignes) and lignes[i + 1].strip().startswith("Traitements"):
                        filtrées.append(lignes[i + 1])
                        i += 1
            i += 1
        if len(filtrées) > 1:
            return "\n".join(filtrées)

    return data


async def prechauffer_catalogue() -> None:
    """
    Préchauffer le cache catalogue au démarrage ET mémoriser dans knowledge/.
    Le fichier knowledge/catalogue_samantan.md est intégré automatiquement
    dans le system prompt de Tima à chaque requête.
    """
    global _catalogue_cache
    logger.info("Préchauffage cache catalogue SAMANTAN...")
    try:
        data = await _fetch_catalogue_raw()
        if data and len(data) > 100:
            # ── Cache mémoire ──────────────────────────────────────────────────
            _catalogue_cache["data"] = data
            _catalogue_cache["ts"] = time.monotonic()
            logger.info(f"Cache catalogue préchauffé ✓ ({len(data)} chars)")

            # ── Mémorisation permanente dans knowledge/ ────────────────────────
            # brain.py charge automatiquement tous les fichiers .md de knowledge/
            # dans le system prompt → Tima connaît le catalogue sans appeler le tool
            from datetime import datetime
            knowledge_dir = KNOWLEDGE_FILE.parent
            knowledge_dir.mkdir(parents=True, exist_ok=True)
            catalogue_file = knowledge_dir / "catalogue_samantan.md"
            catalogue_file.write_text(
                f"# Catalogue SAMANTAN — Produits actifs\n"
                f"_Mis à jour : {datetime.now().strftime('%Y-%m-%d %H:%M')}_\n\n"
                f"{data}",
                encoding="utf-8"
            )
            logger.info(f"Catalogue mémorisé dans {catalogue_file} ✓")
        else:
            logger.warning(
                f"Préchauffage catalogue : résultat trop court "
                f"({len(data) if data else 0} chars) — "
                f"vérifier SAMANTAN_LOGIN_EMAIL et SAMANTAN_LOGIN_PASSWORD sur Railway"
            )
    except Exception as e:
        logger.warning(f"Préchauffage catalogue échoué : {e} — Tima fonctionne normalement")


async def scraper_pages_samantan() -> None:
    """
    Scrape et mémorise dans knowledge/ les pages importantes de SAMANTAN :
      - /nouvelle-ordonnance        → formulaire_ordonnance.md
      - /mon-reseau-d-opticiens     → reseau_opticiens.md

    Fait un seul login puis scrape toutes les pages.
    Appelé en tâche de fond au démarrage.
    """
    pages = [
        ("formulaire_ordonnance.md", "/nouvelle-ordonnance",       "Formulaire nouvelle ordonnance SAMANTAN"),
        ("reseau_opticiens.md",      "/mon-reseau-d-opticiens",    "Réseau d'opticiens SAMANTAN"),
    ]

    knowledge_dir = KNOWLEDGE_FILE.parent
    knowledge_dir.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient(follow_redirects=False, timeout=20.0, headers=HEADERS) as client:
        # ── Login ──────────────────────────────────────────────────────────────
        await client.get(f"{SAMANTAN_URL}/connexion-samantan", timeout=10.0)
        r_login = await client.post(
            f"{SAMANTAN_URL}/connexion-samantan",
            data={
                "_method": "POST",
                "data[User][email]": LOGIN_EMAIL,
                "data[User][password]": LOGIN_PASSWORD,
            },
            timeout=15.0,
        )
        if r_login.status_code not in [200, 302]:
            logger.warning(f"scraper_pages_samantan : login échoué ({r_login.status_code})")
            return
        logger.info("scraper_pages_samantan : login OK")

        # ── Scrape chaque page ─────────────────────────────────────────────────
        for filename, path, title in pages:
            try:
                r = await client.get(
                    f"{SAMANTAN_URL}{path}", follow_redirects=True, timeout=20.0
                )
                if r.status_code == 200 and "connexion" not in str(r.url):
                    texte = _extraire_texte(r.text)
                    if len(texte) > 100:
                        (knowledge_dir / filename).write_text(
                            f"# {title}\n"
                            f"_Source : {SAMANTAN_URL}{path}_\n\n"
                            f"{texte[:8000]}",
                            encoding="utf-8",
                        )
                        logger.info(f"Mémorisé : {title} ({len(texte)} chars) → {filename}")
                    else:
                        logger.warning(f"Page '{title}' : contenu trop court ({len(texte)} chars)")
                else:
                    logger.warning(f"Page '{title}' inaccessible : {r.status_code} | {r.url}")
            except Exception as e:
                logger.warning(f"Page '{title}' : {e}")


# ── Ordonnances SAMANTAN ───────────────────────────────────────────────────────

ORDONNANCES_URL = f"{SAMANTAN_URL}/liste-des-ordonnances"


async def _fetch_ordonnances_raw() -> str:
    """Fetch brut de la liste des ordonnances depuis samantan.net."""
    async with httpx.AsyncClient(
        follow_redirects=False, timeout=20.0, headers=HEADERS
    ) as client:
        # ── Login ──────────────────────────────────────────────────────────────
        await client.get(f"{SAMANTAN_URL}/connexion-samantan", timeout=10.0)
        r_login = await client.post(
            f"{SAMANTAN_URL}/connexion-samantan",
            data={
                "_method": "POST",
                "data[User][email]": LOGIN_EMAIL,
                "data[User][password]": LOGIN_PASSWORD,
            },
            timeout=15.0,
        )
        logger.info(f"Ordonnances login : {r_login.status_code}")

        # ── GET liste des ordonnances ──────────────────────────────────────────
        r = await client.get(ORDONNANCES_URL, follow_redirects=True, timeout=15.0)
        if r.status_code != 200 or "connexion" in str(r.url):
            logger.error(f"Ordonnances inaccessibles : {r.status_code} | {r.url}")
            return "Liste des ordonnances momentanément inaccessible."

        logger.info(f"Ordonnances chargées : {len(r.text)} chars")

        # ── Parser le tableau ──────────────────────────────────────────────────
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.text, "html.parser")

        lignes_resultat = []

        # Chercher les tableaux
        tables = soup.find_all("table")
        for table in tables:
            # En-tête
            entetes = []
            thead = table.find("thead")
            if thead:
                entetes = [th.get_text(strip=True) for th in thead.find_all(["th", "td"])]

            # Lignes de données
            tbody = table.find("tbody") or table
            rows = tbody.find_all("tr")
            for row in rows:
                cells = row.find_all(["td", "th"])
                valeurs = [c.get_text(strip=True) for c in cells]
                valeurs = [v for v in valeurs if v]
                if not valeurs:
                    continue
                if entetes and len(entetes) == len(valeurs):
                    ligne = " | ".join(f"{e}: {v}" for e, v in zip(entetes, valeurs))
                else:
                    ligne = " | ".join(valeurs)
                if len(ligne) > 10:
                    lignes_resultat.append(f"• {ligne}")

        if lignes_resultat:
            return f"[ORDONNANCES SAMANTAN — {len(lignes_resultat)} entrées]\n" + "\n".join(lignes_resultat[:100])

        # Fallback : texte général
        for tag in soup(["script", "style", "meta", "nav", "header", "footer", "img", "svg"]):
            tag.decompose()
        texte = soup.get_text(separator="\n", strip=True)
        lignes = [l.strip() for l in texte.split("\n") if len(l.strip()) > 5]
        return "\n".join(lignes[:150])


async def fetch_ordonnances_samantan(recherche: str = "") -> str:
    """
    Retourne la liste des ordonnances SAMANTAN avec cache 5 minutes.
    Filtre par recherche si fournie (référence, nom client, statut...).
    """
    global _ordonnances_cache
    now = time.monotonic()

    # ── Cache frais ────────────────────────────────────────────────────────────
    if _ordonnances_cache["data"] and (now - _ordonnances_cache["ts"]) < _ORDONNANCES_TTL_SECS:
        age = int(now - _ordonnances_cache["ts"])
        logger.info(f"Ordonnances depuis cache ({age}s)")
        data = _ordonnances_cache["data"]
    else:
        logger.info("Fetch ordonnances SAMANTAN...")
        try:
            data = await asyncio.wait_for(_fetch_ordonnances_raw(), timeout=12.0)
            if data and len(data) > 50:
                _ordonnances_cache["data"] = data
                _ordonnances_cache["ts"] = now
                logger.info(f"Cache ordonnances mis à jour ({len(data)} chars)")
        except asyncio.TimeoutError:
            logger.warning("Ordonnances fetch timeout (12s)")
            data = (
                _ordonnances_cache["data"]
                or "Liste des ordonnances momentanément inaccessible."
            )
        except Exception as e:
            logger.error(f"Erreur fetch ordonnances : {e}")
            data = (
                _ordonnances_cache["data"]
                or "Liste des ordonnances momentanément inaccessible."
            )

    # ── Filtrer par recherche ──────────────────────────────────────────────────
    if recherche and data and "inaccessible" not in data:
        lignes = data.split("\n")
        filtrées = [l for l in lignes if not l.startswith("•") or recherche.lower() in l.lower()]
        if len(filtrées) > 1:
            return "\n".join(filtrées)

    return data


# ── Prix personnalisés par opticien ───────────────────────────────────────────

async def scraper_prix_opticiens(limite: int = 0) -> dict:
    """
    Récupère les prix personnalisés pour chaque opticien du réseau SAMANTAN.
    URL directe : /opticiens/prix-personaliser/{opticien_id}

    Workflow :
      1. Login une seule fois
      2. GET /nouvelle-ordonnance → extraire les 145 opticiens (id + nom)
      3. Pour chaque opticien → GET /opticiens/prix-personaliser/{id}
      4. Parser le tableau de prix de la page
      5. Sauvegarder knowledge/prix_opticiens.md

    Args:
        limite: Nombre max d'opticiens à traiter (0 = tous)

    Returns:
        dict avec résultats + chemin fichier sauvegardé
    """
    from bs4 import BeautifulSoup
    from datetime import datetime

    resultats = []

    async with httpx.AsyncClient(
        follow_redirects=False, timeout=30.0, headers=HEADERS
    ) as client:

        # ── Login ──────────────────────────────────────────────────────────────
        await client.get(f"{SAMANTAN_URL}/connexion-samantan", timeout=10.0)
        r_login = await client.post(
            f"{SAMANTAN_URL}/connexion-samantan",
            data={
                "_method": "POST",
                "data[User][email]": LOGIN_EMAIL,
                "data[User][password]": LOGIN_PASSWORD,
            },
            timeout=15.0,
        )
        if r_login.status_code not in [200, 302]:
            return {"erreur": f"Login échoué : {r_login.status_code}", "resultats": []}
        logger.info("scraper_prix_opticiens : login OK")

        # ── Extraire la liste des opticiens depuis le formulaire ───────────────
        r_form = await client.get(
            f"{SAMANTAN_URL}/nouvelle-ordonnance",
            follow_redirects=True,
            timeout=40.0,
        )
        soup_form = BeautifulSoup(r_form.text, "html.parser")

        opticiens: list[dict] = []
        for sel_el in soup_form.find_all("select"):
            name = sel_el.get("name", "")
            if "opticien" not in name.lower() and "opticien_id" not in name.lower():
                continue
            for opt in sel_el.find_all("option"):
                val = opt.get("value", "").strip()
                label = opt.get_text(strip=True)
                if val and val != "--":
                    opticiens.append({"id": val, "nom": label})
            if opticiens:
                break

        if not opticiens:
            return {
                "erreur": "Liste opticiens introuvable dans /nouvelle-ordonnance",
                "resultats": [],
            }

        if limite and limite > 0:
            opticiens = opticiens[:limite]

        logger.info(f"scraper_prix_opticiens : {len(opticiens)} opticiens à traiter")

        # ── Scraper /opticiens/prix-personaliser/{id} pour chaque opticien ─────
        for i, opticien in enumerate(opticiens):
            url_prix = f"{SAMANTAN_URL}/opticiens/prix-personaliser/{opticien['id']}"
            try:
                r = await client.get(url_prix, follow_redirects=True, timeout=20.0)

                if r.status_code != 200 or "connexion" in str(r.url):
                    logger.warning(
                        f"  [{i+1}/{len(opticiens)}] {opticien['nom']} : "
                        f"HTTP {r.status_code} | {r.url}"
                    )
                    resultats.append({
                        "nom": opticien["nom"], "id": opticien["id"],
                        "erreur": f"HTTP {r.status_code}", "prix": [],
                    })
                    continue

                soup_prix = BeautifulSoup(r.text, "html.parser")

                # ── Parser les tableaux de prix ────────────────────────────────
                prix_produits: list[dict | str] = []

                for table in soup_prix.find_all("table"):
                    entetes = [th.get_text(strip=True) for th in table.find_all("th")]
                    for row in table.find_all("tr"):
                        cells = [td.get_text(strip=True) for td in row.find_all("td")]
                        cells = [c for c in cells if c]
                        if not cells:
                            continue
                        if entetes and len(entetes) == len(cells):
                            prix_produits.append(dict(zip(entetes, cells)))
                        elif cells:
                            prix_produits.append(" | ".join(cells))

                # ── Fallback : texte général ───────────────────────────────────
                if not prix_produits:
                    for tag in soup_prix(["script", "style", "nav",
                                          "header", "footer", "img", "svg"]):
                        tag.decompose()
                    texte = soup_prix.get_text(separator="\n", strip=True)
                    prix_produits = [
                        l.strip() for l in texte.split("\n")
                        if len(l.strip()) > 3
                    ][:60]

                resultats.append({
                    "nom": opticien["nom"],
                    "id": opticien["id"],
                    "url": url_prix,
                    "prix": prix_produits,
                    "page_chars": len(r.text),
                    "erreur": None,
                })
                logger.info(
                    f"  [{i+1}/{len(opticiens)}] ✓ {opticien['nom']} "
                    f"({len(prix_produits)} entrées, {len(r.text)} chars)"
                )

                await asyncio.sleep(0.4)  # politesse envers samantan.net

            except Exception as e:
                logger.warning(f"  [{i+1}] Erreur {opticien['nom']} : {e}")
                resultats.append({
                    "nom": opticien["nom"], "id": opticien["id"],
                    "erreur": str(e), "prix": [],
                })

    # ── Construire et sauvegarder knowledge/prix_opticiens.md ─────────────────
    from datetime import datetime

    lignes_md = [
        "# Prix personnalisés par opticien — SAMANTAN",
        f"_Extrait le {datetime.now().strftime('%Y-%m-%d %H:%M')}_",
        f"_Source : {SAMANTAN_URL}/opticiens/prix-personaliser/{{id}}_",
        f"_Total opticiens : {len(resultats)}_",
        "",
    ]

    for r in resultats:
        lignes_md.append(f"## {r['nom']} (ID: {r['id']})")
        if r.get("erreur"):
            lignes_md.append(f"- Erreur : {r['erreur']}")
        elif r.get("prix"):
            for p in r["prix"]:
                if isinstance(p, dict):
                    lignes_md.append(
                        "  • " + " | ".join(f"{k}: {v}" for k, v in p.items())
                    )
                else:
                    lignes_md.append(f"  • {p}")
        else:
            lignes_md.append("  _(aucun prix trouvé)_")
        lignes_md.append("")

    knowledge_dir = KNOWLEDGE_FILE.parent
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    prix_file = knowledge_dir / "prix_opticiens.md"
    prix_file.write_text("\n".join(lignes_md), encoding="utf-8")
    logger.info(f"Prix opticiens sauvegardés → {prix_file} ✓ ({len(resultats)} opticiens)")

    return {
        "opticiens_traites": len(resultats),
        "resultats": resultats,
        "fichier_sauvegarde": "knowledge/prix_opticiens.md",
    }


# ── Simulation de prix par opticien (ancienne méthode via formulaire) ──────────

async def simuler_prix_par_opticien() -> dict:
    """
    Pour chaque opticien du réseau SAMANTAN :
      1. Soumet une ordonnance test standard sur /nouvelle-ordonnance (étape 1)
      2. Capture le prix affiché dans la réponse (page récapitulatif/preview)
      3. NE confirme PAS la commande — aucune commande réelle créée
      4. Sauvegarde knowledge/prix_opticiens.md avec les résultats

    Ordonnance test utilisée :
      OD : Sph +0.00 / Cyl -0.25 / Axe 90
      OG : Sph +0.00 / Cyl -0.25 / Axe 90
      Addition : +1.00 | DIP : 32/32 | Hauteur : 20/20

    Retourne un dict avec résultats bruts + chemin du fichier sauvegardé.
    """
    from bs4 import BeautifulSoup
    from datetime import datetime

    # Valeurs Rx de test — universelles, ne génèrent pas d'erreur de validation
    RX_TEST = {
        "sph": "0.00",
        "cyl": "-0.25",
        "axe": "90",
        "add": "1.00",
        "dip_od": "32",
        "dip_og": "32",
        "hauteur_od": "20",
        "hauteur_og": "20",
    }

    resultats = []

    async with httpx.AsyncClient(
        follow_redirects=False, timeout=30.0, headers=HEADERS
    ) as client:

        # ── Login ──────────────────────────────────────────────────────────────
        await client.get(f"{SAMANTAN_URL}/connexion-samantan", timeout=10.0)
        r_login = await client.post(
            f"{SAMANTAN_URL}/connexion-samantan",
            data={
                "_method": "POST",
                "data[User][email]": LOGIN_EMAIL,
                "data[User][password]": LOGIN_PASSWORD,
            },
            timeout=15.0,
        )
        if r_login.status_code not in [200, 302]:
            return {"erreur": f"Login échoué : {r_login.status_code}", "resultats": []}
        logger.info("simuler_prix_par_opticien : login OK")

        # ── Charger le formulaire /nouvelle-ordonnance ─────────────────────────
        r_form = await client.get(
            f"{SAMANTAN_URL}/nouvelle-ordonnance",
            follow_redirects=True,
            timeout=15.0,
        )
        if r_form.status_code != 200 or "connexion" in str(r_form.url):
            return {
                "erreur": f"Formulaire inaccessible : {r_form.status_code} | {r_form.url}",
                "resultats": [],
            }

        soup = BeautifulSoup(r_form.text, "html.parser")

        # ── Trouver le sélecteur d'opticien ───────────────────────────────────
        opticien_field_name = None
        opts_opticiens = []

        for sel_el in soup.find_all("select"):
            name = sel_el.get("name", "")
            opts = sel_el.find_all("option")
            real_opts = [o for o in opts if o.get("value", "").strip()]

            if len(real_opts) < 2:
                continue

            name_lower = name.lower()
            opts_text = " ".join(o.get_text(strip=True).lower() for o in opts)

            is_opticien_field = (
                any(kw in name_lower for kw in ["user", "opticien", "client", "boutique"])
                or any(kw in opts_text for kw in [
                    "opticien", "boutique", "lunetterie", "optique", "vision",
                    "pharmacie", "sante", "santé"
                ])
            )

            if is_opticien_field:
                opticien_field_name = name
                opts_opticiens = [
                    {"value": o.get("value", ""), "label": o.get_text(strip=True)}
                    for o in real_opts
                ]
                logger.info(
                    f"Sélecteur opticien : '{name}' | {len(opts_opticiens)} opticiens"
                )
                break

        if not opticien_field_name:
            # Fallback : prendre le plus grand select (probablement la liste clients)
            biggest = max(
                soup.find_all("select"),
                key=lambda s: len(s.find_all("option")),
                default=None,
            )
            if biggest and len(biggest.find_all("option")) > 3:
                opticien_field_name = biggest.get("name", "")
                opts_opticiens = [
                    {"value": o.get("value", ""), "label": o.get_text(strip=True)}
                    for o in biggest.find_all("option")
                    if o.get("value", "").strip()
                ]
                logger.info(
                    f"Fallback sélecteur : '{opticien_field_name}' | {len(opts_opticiens)} options"
                )

        if not opticien_field_name or not opts_opticiens:
            return {
                "erreur": "Sélecteur d'opticien introuvable dans le formulaire",
                "selects_trouves": [
                    {"name": s.get("name"), "options": len(s.find_all("option"))}
                    for s in soup.find_all("select")
                ],
                "resultats": [],
            }

        # ── Construire le payload de base depuis tous les champs du form ───────
        form_el = soup.find("form")
        base_payload: dict[str, str] = {}

        if form_el:
            for el in form_el.find_all(["input", "select", "textarea"]):
                name = el.get("name", "")
                if not name:
                    continue
                if el.name == "input":
                    t = el.get("type", "text").lower()
                    if t in ["hidden", "text", "number", "submit"]:
                        base_payload[name] = el.get("value", "")
                elif el.name == "select":
                    first = next(
                        (o.get("value", "") for o in el.find_all("option")
                         if o.get("value", "").strip()),
                        "",
                    )
                    base_payload[name] = first
                elif el.name == "textarea":
                    base_payload[name] = el.get_text(strip=True)

        # Toujours inclure _method=POST (CakePHP)
        base_payload.setdefault("_method", "POST")

        # ── Injecter les valeurs Rx test dans les champs correspondants ────────
        # Mapping : fragment de nom de champ → valeur Rx
        RX_MAPPING = [
            (["sph_od", "sphere_od", "s_od", "sph_d", "sphere_d", "od_sph", "od_sphere"], RX_TEST["sph"]),
            (["sph_og", "sphere_og", "s_og", "sph_g", "sphere_g", "og_sph", "og_sphere"], RX_TEST["sph"]),
            (["cyl_od", "cylindre_od", "cy_od", "cyl_d", "od_cyl", "od_cyl"], RX_TEST["cyl"]),
            (["cyl_og", "cylindre_og", "cy_og", "cyl_g", "og_cyl"], RX_TEST["cyl"]),
            (["axe_od", "axis_od", "ax_od", "axe_d", "od_axe"], RX_TEST["axe"]),
            (["axe_og", "axis_og", "ax_og", "axe_g", "og_axe"], RX_TEST["axe"]),
            (["add_od", "addition_od", "add"], RX_TEST["add"]),
            (["add_og", "addition_og"], RX_TEST["add"]),
            (["dip_od", "dp_od", "dipod", "diod"], RX_TEST["dip_od"]),
            (["dip_og", "dp_og", "dipog", "diog"], RX_TEST["dip_og"]),
            (["dip"], "64"),  # binoculaire
            (["hauteur_od", "height_od", "haut_od", "h_od"], RX_TEST["hauteur_od"]),
            (["hauteur_og", "height_og", "haut_og", "h_og"], RX_TEST["hauteur_og"]),
        ]

        for field_name in list(base_payload.keys()):
            # Extraire la partie significative du nom CakePHP : data[Model][field] → field
            key_part = field_name.split("[")[-1].rstrip("]").lower()
            for fragments, value in RX_MAPPING:
                if any(frag in key_part or key_part in frag for frag in fragments):
                    base_payload[field_name] = value
                    break

        logger.info(
            f"Payload de base construit : {len(base_payload)} champs | "
            f"opticiens à simuler : {len(opts_opticiens)}"
        )

        # ── Simulation pour chaque opticien ───────────────────────────────────
        for opticien in opts_opticiens[:30]:  # max 30 opticiens
            payload = dict(base_payload)
            payload[opticien_field_name] = opticien["value"]

            logger.info(
                f"→ Simulation : {opticien['label']} (id={opticien['value']})"
            )

            try:
                r_sim = await client.post(
                    f"{SAMANTAN_URL}/nouvelle-ordonnance",
                    data=payload,
                    follow_redirects=True,
                    timeout=20.0,
                )

                soup_rep = BeautifulSoup(r_sim.text, "html.parser")

                # ── Extraire les prix de la réponse ───────────────────────────
                prix_trouves: list[str] = []

                # 1. Texte contenant FCFA / CFA
                for tag in soup_rep.find_all(
                    string=re.compile(r'\d[\d\s]*(?:F\.?CFA|FCFA|CFA)', re.IGNORECASE)
                ):
                    t = tag.strip()
                    if t and len(t) < 200:
                        prix_trouves.append(t)

                # 2. Éléments avec class "prix", "price", "montant", "total"
                for css in [".prix", ".price", ".montant", ".total", ".amount",
                            "#prix", "#total", "#montant", ".cout", ".tarif"]:
                    for el in soup_rep.select(css):
                        t = el.get_text(strip=True)
                        if t and re.search(r'\d', t):
                            prix_trouves.append(f"[{css}] {t}")

                # 3. Colonnes de tableau labellisées "prix / price / montant / total"
                for table in soup_rep.find_all("table"):
                    headers = [th.get_text(strip=True).lower()
                               for th in table.find_all("th")]
                    for i, h in enumerate(headers):
                        if any(kw in h for kw in [
                            "prix", "price", "montant", "total", "coût", "tarif", "fcfa"
                        ]):
                            for row in table.find_all("tr"):
                                cells = row.find_all("td")
                                if i < len(cells):
                                    val = cells[i].get_text(strip=True)
                                    if val and re.search(r'\d{3,}', val):
                                        prix_trouves.append(f"[tableau:{h}] {val}")

                # 4. Fallback : tout nombre ≥ 4 chiffres dans le body
                if not prix_trouves:
                    for tag in soup_rep.find_all(
                        string=re.compile(r'\b\d{4,}\b')
                    ):
                        parent = tag.parent
                        parent_class = " ".join(
                            parent.get("class", []) if parent else []
                        ).lower()
                        if any(kw in parent_class for kw in [
                            "prix", "price", "cost", "montant", "total", "amount"
                        ]):
                            prix_trouves.append(tag.strip())

                # ── Détecter si c'est une page de confirmation (étape 2) ───────
                page_texte = soup_rep.get_text(separator=" ", strip=True).lower()
                est_confirmation = any(kw in page_texte for kw in [
                    "confirmer", "confirmation", "récapitulatif",
                    "résumé", "valider la commande", "étape 2",
                    "step 2", "please confirm", "submit order",
                ])

                # Enlever les doublons
                prix_uniques = list(dict.fromkeys(p for p in prix_trouves if p))

                resultats.append({
                    "opticien": opticien["label"],
                    "id": opticien["value"],
                    "status": r_sim.status_code,
                    "url_finale": str(r_sim.url),
                    "est_confirmation": est_confirmation,
                    "prix": prix_uniques[:15],
                    "page_chars": len(r_sim.text),
                    "erreur": None,
                })

                if est_confirmation:
                    logger.info(
                        f"  ✓ Page confirmation détectée — simulation ARRÊTÉE "
                        f"(pas de commande réelle)"
                    )
                else:
                    logger.info(
                        f"  → {r_sim.status_code} | {len(prix_uniques)} prix trouvés"
                    )

                await asyncio.sleep(0.8)  # Politesse envers le serveur

            except Exception as e:
                logger.warning(f"Erreur simulation {opticien['label']} : {e}")
                resultats.append({
                    "opticien": opticien["label"],
                    "id": opticien["value"],
                    "status": None,
                    "prix": [],
                    "erreur": str(e),
                })

    # ── Sauvegarder dans knowledge/prix_opticiens.md ───────────────────────────
    if resultats:
        from datetime import datetime
        lignes = [
            "# Prix par opticien — SAMANTAN",
            f"_Simulation effectuée le {datetime.now().strftime('%Y-%m-%d %H:%M')}_",
            "_Ordonnance test : OD/OG Sph 0.00 / Cyl -0.25 / Axe 90 | Add +1.00 | DIP 32/32 | Haut 20/20_",
            f"_Opticiens simulés : {len(resultats)}_",
            "",
        ]
        for r in resultats:
            lignes.append(f"## {r['opticien']} (ID: {r.get('id', '?')})")
            if r.get("erreur"):
                lignes.append(f"- Erreur : {r['erreur']}")
            elif r.get("prix"):
                lignes.append("- Prix détectés :")
                for p in r["prix"]:
                    lignes.append(f"  • {p}")
                if r.get("est_confirmation"):
                    lignes.append("  _(page de confirmation — simulation arrêtée avant envoi)_")
            else:
                lignes.append(
                    f"- Aucun prix détecté "
                    f"(status: {r.get('status')}, chars: {r.get('page_chars')})"
                )
            lignes.append("")

        knowledge_dir = KNOWLEDGE_FILE.parent
        knowledge_dir.mkdir(parents=True, exist_ok=True)
        prix_file = knowledge_dir / "prix_opticiens.md"
        prix_file.write_text("\n".join(lignes), encoding="utf-8")
        logger.info(f"Prix opticiens sauvegardés → {prix_file} ✓ ({len(resultats)} opticiens)")

    return {
        "opticiens_simules": len(resultats),
        "champ_opticien": opticien_field_name,
        "resultats": resultats,
        "fichier_sauvegarde": "knowledge/prix_opticiens.md" if resultats else None,
    }

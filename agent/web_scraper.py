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

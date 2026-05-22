# agent/web_scraper.py — Accès au site SAMANTAN pour enrichir Tima

import os
import re
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

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}


def _extraire_texte(html: str) -> str:
    """Extrait le texte propre d'une page HTML."""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
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
        soup = BeautifulSoup(html, "lxml")

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
        soup = BeautifulSoup(html, "lxml")

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
        soup = BeautifulSoup(html, "lxml")

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


async def fetch_catalogue_samantan(recherche: str = "") -> str:
    """
    Accède à la liste détaillée des produits ACTIFS de samantan.net en temps réel.
    URL : /liste-detaillee-des-produits?statut=1
    Entre dans chaque fiche produit pour récupérer tous les détails.

    Args:
        recherche: Mot-clé pour filtrer les produits (ex: "progressif", "transitions")

    Returns:
        Texte avec les fiches détaillées de tous les produits actifs
    """
    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=30.0,
        headers=HEADERS
    ) as client:

        # ── Étape 1 : Login (POST → 302 avec cookie de session) ───────────
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
        logger.info(f"Login : {r_login.status_code} | {r_login.headers.get('location', '')}")

        # ── Étape 2 : Charger la page des produits actifs ──────────────────
        try:
            r = await client.get(CATALOGUE_ACTIFS_URL, follow_redirects=True, timeout=25.0)
            if r.status_code != 200 or "connexion" in str(r.url):
                logger.error(f"Catalogue inaccessible : {r.status_code} | {r.url}")
                return "Catalogue momentanément inaccessible. Consulter www.samantan.net"
            html_catalogue = r.text
            logger.info(f"Catalogue chargé : {len(html_catalogue)} caractères")
        except Exception as e:
            logger.error(f"Erreur chargement catalogue : {e}")
            return "Catalogue momentanément inaccessible. Consulter www.samantan.net"

        # ── Étape 3 : Parser les produits actifs depuis le texte ──────────
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html_catalogue, "lxml")
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
                        # Filtrer par recherche si spécifiée
                        if recherche and recherche.lower() not in nom.lower():
                            continue
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

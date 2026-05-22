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

            # Tenter la connexion
            login_data = {
                "email": LOGIN_EMAIL,
                "password": LOGIN_PASSWORD,
                "log": LOGIN_EMAIL,
                "pwd": LOGIN_PASSWORD,
                "username": LOGIN_EMAIL,
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


async def fetch_catalogue_samantan(recherche: str = "") -> str:
    """
    Accède au menu Catalogue de samantan.net en temps réel et retourne
    les informations produits. Appelé par Tima quand un client pose
    une question sur les produits SAMANTAN.

    Args:
        recherche: Mot-clé de recherche produit (ex: "progressif", "transitions")

    Returns:
        Texte avec les informations produits du catalogue
    """
    contenu = []

    # URLs catalogue à essayer
    pages_catalogue = [
        ("Catalogue", "/catalogue"),
        ("Produits", "/produits"),
        ("Boutique", "/boutique"),
        ("Shop", "/shop"),
        ("Verres progressifs", "/catalogue/progressifs"),
        ("Verres Transitions", "/catalogue/transitions"),
        ("Verres unifocaux", "/catalogue/unifocaux"),
    ]

    # Si recherche spécifique, ajouter des URLs ciblées
    if recherche:
        mot = recherche.lower().replace(" ", "-")
        pages_catalogue += [
            (f"Recherche : {recherche}", f"/catalogue/{mot}"),
            (f"Produit : {recherche}", f"/produits/{mot}"),
            (f"Catégorie : {recherche}", f"/?s={recherche}"),
        ]

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=25.0,
        headers=HEADERS
    ) as client:

        # Connexion d'abord
        try:
            await client.get(f"{SAMANTAN_URL}/connexion-samantan", timeout=10.0)
            login_data = {
                "email": LOGIN_EMAIL,
                "password": LOGIN_PASSWORD,
                "log": LOGIN_EMAIL,
                "pwd": LOGIN_PASSWORD,
                "username": LOGIN_EMAIL,
            }
            await client.post(
                f"{SAMANTAN_URL}/connexion-samantan",
                data=login_data,
                timeout=15.0
            )
            logger.info("Connexion SAMANTAN pour catalogue...")
        except Exception as e:
            logger.warning(f"Connexion catalogue : {e}")

        # Scraper les pages catalogue — produits actifs uniquement
        for nom, chemin in pages_catalogue:
            try:
                r = await client.get(f"{SAMANTAN_URL}{chemin}", timeout=15.0)
                if r.status_code == 200:
                    # Utiliser l'extracteur de produits actifs
                    texte = _extraire_produits_actifs(r.text)
                    if len(texte) > 150:
                        contenu.append(
                            f"[Produits actifs — {nom}]\n{texte[:4000]}"
                        )
                        logger.info(f"Produits actifs récupérés : {nom}")
                        if len(contenu) >= 3:
                            break
            except Exception as e:
                logger.debug(f"Page catalogue {chemin} : {e}")

    if not contenu:
        return "Catalogue momentanément inaccessible. Consulter www.samantan.net directement."

    return "\n\n".join(contenu)

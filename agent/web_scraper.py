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
        # Supprimer les balises inutiles
        for tag in soup(["script", "style", "meta", "link", "svg",
                         "img", "noscript", "iframe", "header", "footer", "nav"]):
            tag.decompose()
        texte = soup.get_text(separator=" ", strip=True)
        # Nettoyer espaces multiples
        texte = re.sub(r'\s+', ' ', texte).strip()
        return texte
    except Exception as e:
        logger.warning(f"Erreur extraction texte HTML : {e}")
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

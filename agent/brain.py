# agent/brain.py — Cerveau de Tima : connexion avec l'API Claude + tool calling

import os
import yaml
import logging
from pathlib import Path
from anthropic import AsyncAnthropic
from dotenv import load_dotenv

load_dotenv(override=True)
logger = logging.getLogger("agentkit")

client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# ── Modèle actif (basculé dynamiquement par le scheduler) ─────────────────────
# Par défaut : Haiku 4.5 (économique)
# À 2h AM → Sonnet 4.6 (mises à jour SAMANTAN)
# À 2h30 AM → retour Haiku 4.5
_MODELE_PAR_DEFAUT = "claude-haiku-4-5"


def get_modele_actif() -> str:
    """Retourne le modèle Claude actuellement actif pour Tima."""
    return os.getenv("TIMA_MODEL", _MODELE_PAR_DEFAUT)


def set_modele(modele: str) -> None:
    """Change le modèle actif de Tima (appelé par le scheduler)."""
    os.environ["TIMA_MODEL"] = modele
    logger.info(f"Modèle Tima basculé → {modele}")

# ── Outil : accès au catalogue SAMANTAN ───────────────────────────────────────
TOOLS = [
    {
        "name": "consulter_catalogue_samantan",
        "description": (
            "Accède au catalogue SAMANTAN en temps réel pour vérifier les produits actifs, "
            "références, gammes et disponibilités. Utiliser si le client pose une question "
            "très spécifique sur un produit ou si les infos du contexte ne suffisent pas."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "recherche": {
                    "type": "string",
                    "description": "Type de produit recherché (ex: 'progressif', 'transitions', 'unifocal')"
                }
            },
            "required": []
        }
    },
    {
        "name": "consulter_prix_opticien",
        "description": (
            "Consulte les prix personnalisés d'un opticien spécifique : tarifs par produit, "
            "paliers de remise mensuels, paramétrage 2ème/3ème paire, pactoïe. "
            "Utiliser quand un opticien demande ses prix, ses remises ou ses avantages."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nom_opticien": {
                    "type": "string",
                    "description": "Nom de l'opticien ou de la boutique (ex: 'OPTIQUE PONTY', 'JUNIOR OPTIQUE')"
                }
            },
            "required": []
        }
    },
    {
        "name": "consulter_ordonnances_samantan",
        "description": (
            "Accède à la liste des ordonnances/commandes du site SAMANTAN en temps réel. "
            "Utiliser quand un client demande l'état d'une commande, le suivi d'une "
            "ordonnance, des informations sur les prescriptions en cours, ou l'historique "
            "des commandes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "recherche": {
                    "type": "string",
                    "description": "Référence de commande, nom du client, statut ou autre terme de recherche"
                }
            },
            "required": []
        }
    }
]


def cargar_config_prompts() -> dict:
    """Lit la configuration depuis config/prompts.yaml."""
    try:
        with open("config/prompts.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.error("config/prompts.yaml introuvable")
        return {}


def cargar_knowledge() -> str:
    """
    Charge les fichiers de knowledge/ dans le system prompt de Tima.

    Règles de taille pour garder le contexte Claude sous contrôle :
      - prix_opticiens.md  → EXCLU du system prompt (trop grand, ~125 KB)
                             accessible uniquement via l'outil consulter_prix_opticien
      - catalogue_samantan.md → limité à 12 000 chars (résumé des produits actifs)
      - Autres fichiers    → limités à 8 000 chars chacun
      - Total knowledge    → plafonné à 30 000 chars
    """
    knowledge_dir = Path("knowledge")
    contenus = []
    total_chars = 0
    LIMITE_TOTALE = 30_000

    # Fichiers exclus du system prompt (trop grands, gérés par outils)
    EXCLUS = {"prix_opticiens.md"}

    # Limites par fichier
    LIMITES = {
        "catalogue_samantan.md": 12_000,
    }
    LIMITE_DEFAUT = 8_000

    if not knowledge_dir.exists():
        return ""

    for fichier in sorted(knowledge_dir.glob("*.md")):
        if fichier.name.startswith(".") or fichier.name in EXCLUS:
            if fichier.name in EXCLUS:
                logger.info(f"Knowledge exclu (outil dédié) : {fichier.name}")
            continue

        if total_chars >= LIMITE_TOTALE:
            logger.info(f"Limite knowledge atteinte ({LIMITE_TOTALE} chars) — {fichier.name} ignoré")
            break

        try:
            texte = fichier.read_text(encoding="utf-8").strip()
            if not texte or len(texte) < 50:
                continue

            limite = LIMITES.get(fichier.name, LIMITE_DEFAUT)
            if len(texte) > limite:
                texte = texte[:limite] + f"\n\n_(... tronqué à {limite} chars)_"

            contenus.append(f"\n\n---\n{texte}")
            total_chars += len(texte)
            logger.info(f"Knowledge chargé : {fichier.name} ({len(texte)} chars)")
        except Exception as e:
            logger.warning(f"Impossible de lire {fichier} : {e}")

    logger.info(f"Knowledge total : {total_chars} chars / {LIMITE_TOTALE} max")
    return "\n".join(contenus)


# ── Cache du system prompt en mémoire (recalculé uniquement si les fichiers changent) ──
_system_prompt_cache: str | None = None


def cargar_system_prompt() -> str:
    global _system_prompt_cache
    if _system_prompt_cache is not None:
        return _system_prompt_cache

    config = cargar_config_prompts()
    system_prompt = config.get(
        "system_prompt",
        "Tu es Tima, assistante de SAMANTAN. Réponds en français."
    )

    # Enrichir avec le contenu du site SAMANTAN (knowledge/)
    knowledge = cargar_knowledge()
    if knowledge:
        system_prompt += (
            "\n\n## Contenu récupéré depuis le site SAMANTAN\n"
            "Utilise ces informations pour répondre aux questions sur SAMANTAN.\n"
            f"{knowledge}"
        )

    _system_prompt_cache = system_prompt
    logger.info(f"System prompt mis en cache ({len(system_prompt)} chars)")
    return system_prompt


def invalider_cache_system_prompt():
    """Invalide le cache du system prompt (appeler après une mise à jour des knowledge files)."""
    global _system_prompt_cache
    _system_prompt_cache = None
    logger.info("Cache system prompt invalidé")


def obtener_mensaje_error() -> str:
    config = cargar_config_prompts()
    return config.get("error_message", "Désolée, problème technique. Veuillez réessayer.")


def obtener_mensaje_fallback() -> str:
    config = cargar_config_prompts()
    return config.get("fallback_message", "Désolée, je n'ai pas bien compris. Tu peux reformuler ?")


def _extraire_texte(content: list) -> str:
    """
    Extrait le texte du premier bloc TextBlock dans une liste de blocs Anthropic.
    Sûr même si le premier bloc est un ToolUseBlock ou si la liste est vide.
    """
    for bloc in content:
        if hasattr(bloc, "type") and bloc.type == "text" and hasattr(bloc, "text"):
            return bloc.text
    return ""


async def _executer_outil(nom: str, parametres: dict) -> str:
    """Exécute un outil demandé par Claude et retourne le résultat."""

    if nom == "consulter_catalogue_samantan":
        from agent.web_scraper import fetch_catalogue_samantan
        recherche = parametres.get("recherche", "")
        logger.info(f"Tima consulte le catalogue SAMANTAN (recherche: '{recherche}')")
        return await fetch_catalogue_samantan(recherche)

    if nom == "consulter_prix_opticien":
        nom_opticien = parametres.get("nom_opticien", "").strip()
        logger.info(f"Tima consulte les prix de : '{nom_opticien}'")
        return _chercher_prix_opticien(nom_opticien)

    if nom == "consulter_ordonnances_samantan":
        from agent.web_scraper import fetch_ordonnances_samantan
        recherche = parametres.get("recherche", "")
        logger.info(f"Tima consulte les ordonnances SAMANTAN (recherche: '{recherche}')")
        return await fetch_ordonnances_samantan(recherche)

    return "Outil inconnu."


def _chercher_prix_opticien(nom_opticien: str) -> str:
    """
    Recherche les prix d'un opticien dans knowledge/prix_opticiens.md.
    Retourne la section correspondante ou les 5 premiers opticiens si nom vide.
    """
    try:
        fichier = Path("knowledge/prix_opticiens.md")
        if not fichier.exists():
            return "Fichier de prix non disponible — relance /scraper-prix-opticiens."

        contenu = fichier.read_text(encoding="utf-8")
        sections = contenu.split("\n## ")

        if not nom_opticien:
            # Retourner les 3 premiers opticiens comme exemple
            exemples = sections[1:4] if len(sections) > 1 else []
            return "Exemples de prix :\n\n## " + "\n\n## ".join(exemples) if exemples else contenu[:3000]

        # Recherche partielle insensible à la casse
        nom_lower = nom_opticien.lower()
        for section in sections[1:]:
            titre = section.split("\n")[0].lower()
            if nom_lower in titre:
                return "## " + section[:4000]

        # Pas trouvé → proposer les noms disponibles
        noms = [s.split("\n")[0] for s in sections[1:] if s.strip()][:20]
        return (
            f"Opticien '{nom_opticien}' non trouvé.\n"
            f"Opticiens disponibles : {', '.join(noms)}"
        )
    except Exception as e:
        logger.warning(f"Erreur lecture prix opticiens : {e}")
        return "Impossible de lire les prix pour l'instant."


async def _telecharger_image_base64(url: str) -> tuple[str, str] | None:
    """
    Télécharge une image depuis une URL et retourne (base64, media_type).
    Retourne None si échec.
    """
    import base64
    import httpx
    try:
        async with httpx.AsyncClient(timeout=20.0) as http:
            r = await http.get(url)
            if r.status_code != 200:
                logger.warning(f"Image inaccessible : HTTP {r.status_code}")
                return None
            content_type = r.headers.get("content-type", "image/jpeg")
            # Normaliser le media_type pour Claude
            if "png" in content_type:
                media_type = "image/png"
            elif "webp" in content_type:
                media_type = "image/webp"
            elif "gif" in content_type:
                media_type = "image/gif"
            else:
                media_type = "image/jpeg"
            b64 = base64.standard_b64encode(r.content).decode("utf-8")
            logger.info(f"Image téléchargée : {len(r.content)} bytes ({media_type})")
            return b64, media_type
    except Exception as e:
        logger.warning(f"Erreur téléchargement image : {e}")
        return None


async def generar_respuesta(
    mensaje: str,
    historial: list[dict],
    telefono: str = "",
    imagen_url: str = "",
) -> str:
    """
    Génère une réponse avec l'API Claude.
    Si le client pose une question sur les produits, Claude appelle automatiquement
    l'outil 'consulter_catalogue_samantan' pour accéder au site en temps réel.

    Args:
        mensaje: Message du client
        historial: Historique de la conversation

    Returns:
        Réponse générée par Claude
    """
    # Si pas d'image et message trop court → fallback
    if not imagen_url and (not mensaje or len(mensaje.strip()) < 2):
        return obtener_mensaje_fallback()

    # ── Identification de l'utilisateur ────────────────────────────────────────
    utilisateur_info = ""
    if telefono:
        try:
            from agent.web_scraper import identifier_utilisateur
            utilisateur = identifier_utilisateur(telefono)
            if utilisateur:
                nom = utilisateur.get("nom", "")
                role = utilisateur.get("role", "opticien")
                utilisateur_info = (
                    f"\n\n## Utilisateur identifié\n"
                    f"- Nom : {nom}\n"
                    f"- Téléphone : {telefono}\n"
                    f"- Rôle : {role}\n"
                    f"- ⚠️ RÈGLE ABSOLUE : Ne jamais divulguer les informations "
                    f"d'autres clients/opticiens à cet utilisateur. "
                    f"Quand il demande ses prix, ne montrer QUE les prix de '{nom}'."
                )
                logger.info(f"Utilisateur identifié : {nom} ({role}) — {telefono}")
            else:
                # Utilisateur non reconnu — accès limité
                utilisateur_info = (
                    f"\n\n## Utilisateur non identifié\n"
                    f"- Téléphone : {telefono}\n"
                    f"- ⚠️ Cet utilisateur n'est pas dans la liste des collaborateurs SAMANTAN. "
                    f"Ne pas divulguer d'informations confidentielles (prix, ordonnances, clients). "
                    f"Répondre uniquement aux questions générales sur SAMANTAN."
                )
                logger.info(f"Utilisateur non reconnu : {telefono}")
        except Exception as e:
            logger.warning(f"Erreur identification utilisateur {telefono} : {e}")

    system_prompt = cargar_system_prompt()
    if utilisateur_info:
        system_prompt = system_prompt + utilisateur_info

    mensajes = []
    for msg in historial:
        mensajes.append({"role": msg["role"], "content": msg["content"]})

    # ── Construction du message courant (avec image si présente) ───────────────
    if imagen_url:
        img = await _telecharger_image_base64(imagen_url)
        if img:
            b64, media_type = img
            contenu_user = [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": b64,
                    },
                },
                {
                    "type": "text",
                    "text": mensaje.strip() or (
                        "Voici une photo. Si c'est une ordonnance optique, lis attentivement "
                        "et extrais les valeurs : OD (Sph/Cyl/Axe), OG (Sph/Cyl/Axe), "
                        "Addition, DIP, hauteur. Présente-les clairement et demande "
                        "confirmation avant toute commande."
                    ),
                },
            ]
            mensajes.append({"role": "user", "content": contenu_user})
        else:
            # Échec téléchargement image → traiter comme texte seul
            mensajes.append({
                "role": "user",
                "content": mensaje.strip() or "J'ai reçu une image mais je n'arrive pas à l'ouvrir. Peux-tu la renvoyer ?",
            })
    else:
        mensajes.append({"role": "user", "content": mensaje})

    try:
        # ── Appel Claude avec outils (boucle multi-tours) ──────────────────
        messages_en_cours = mensajes[:]
        MAX_TOURS = 5  # sécurité anti-boucle infinie

        # Prompt caching Anthropic : le system prompt est mis en cache côté serveur
        # → -90% sur les tokens d'entrée après le 1er appel (cache valide 5 minutes)
        system_avec_cache = [
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"}
            }
        ]

        modele = get_modele_actif()
        logger.info(f"Modèle actif : {modele}")

        response = await client.messages.create(
            model=modele,
            max_tokens=1024,
            system=system_avec_cache,
            messages=messages_en_cours,
            tools=TOOLS
        )

        for tour in range(MAX_TOURS):
            # ── Réponse textuelle directe → on retourne ────────────────────
            if response.stop_reason != "tool_use":
                respuesta = _extraire_texte(response.content)
                if not respuesta:
                    respuesta = obtener_mensaje_fallback()
                logger.info(
                    f"Réponse finale tour {tour} "
                    f"({response.usage.input_tokens} in / {response.usage.output_tokens} out)"
                )
                return respuesta

            # ── Claude veut utiliser un ou plusieurs outils ────────────────
            logger.info(f"Tour {tour+1} : Claude utilise un outil...")

            # Collecter TOUS les blocs tool_use de cette réponse
            tool_blocks = [b for b in response.content if b.type == "tool_use"]

            if not tool_blocks:
                break

            # Exécuter chaque outil et construire les tool_results
            tool_results = []
            for tool_block in tool_blocks:
                resultat = await _executer_outil(tool_block.name, tool_block.input)
                logger.info(
                    f"  ↳ {tool_block.name} → {len(resultat)} chars"
                )
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_block.id,
                    "content": resultat
                })

            # Ajouter la réponse assistant + les résultats des outils
            messages_en_cours = messages_en_cours + [
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": tool_results}
            ]

            # Appel suivant avec les résultats des outils (cache system prompt réutilisé)
            response = await client.messages.create(
                model=modele,
                max_tokens=1024,
                system=system_avec_cache,
                messages=messages_en_cours,
                tools=TOOLS
            )

        # ── Sécurité : si on a épuisé les tours ───────────────────────────
        respuesta = _extraire_texte(response.content)
        if not respuesta:
            respuesta = obtener_mensaje_error()
        return respuesta

    except Exception as e:
        logger.error(f"Erreur API Claude : {type(e).__name__}: {e}")
        return obtener_mensaje_error()

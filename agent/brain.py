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

# ── Outil : accès au catalogue SAMANTAN ───────────────────────────────────────
TOOLS = [
    {
        "name": "consulter_catalogue_samantan",
        "description": (
            "Accède au menu Catalogue du site web SAMANTAN en temps réel pour "
            "récupérer les informations produits (descriptions, gammes, disponibilités). "
            "À utiliser OBLIGATOIREMENT quand un client pose une question sur les produits, "
            "les verres, les gammes ou les disponibilités de SAMANTAN."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "recherche": {
                    "type": "string",
                    "description": "Type de produit recherché (ex: 'progressif', 'transitions', 'unifocal', 'photochromique')"
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
    """Charge les fichiers de knowledge/ pour enrichir le contexte de Tima."""
    knowledge_dir = Path("knowledge")
    contenus = []

    if not knowledge_dir.exists():
        return ""

    for fichier in sorted(knowledge_dir.glob("*.md")):
        if fichier.name.startswith("."):
            continue
        try:
            texte = fichier.read_text(encoding="utf-8").strip()
            if texte and len(texte) > 50:
                contenus.append(f"\n\n---\n{texte}")
                logger.info(f"Knowledge chargé : {fichier.name} ({len(texte)} caractères)")
        except Exception as e:
            logger.warning(f"Impossible de lire {fichier} : {e}")

    return "\n".join(contenus)


def cargar_system_prompt() -> str:
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

    return system_prompt


def obtener_mensaje_error() -> str:
    config = cargar_config_prompts()
    return config.get("error_message", "Désolée, problème technique. Veuillez réessayer.")


def obtener_mensaje_fallback() -> str:
    config = cargar_config_prompts()
    return config.get("fallback_message", "Désolée, je n'ai pas bien compris. Tu peux reformuler ?")


async def _executer_outil(nom: str, parametres: dict) -> str:
    """Exécute un outil demandé par Claude et retourne le résultat."""
    if nom == "consulter_catalogue_samantan":
        from agent.web_scraper import fetch_catalogue_samantan
        recherche = parametres.get("recherche", "")
        logger.info(f"Tima consulte le catalogue SAMANTAN (recherche: '{recherche}')")
        return await fetch_catalogue_samantan(recherche)
    return "Outil inconnu."


async def generar_respuesta(mensaje: str, historial: list[dict]) -> str:
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
    if not mensaje or len(mensaje.strip()) < 2:
        return obtener_mensaje_fallback()

    system_prompt = cargar_system_prompt()

    mensajes = []
    for msg in historial:
        mensajes.append({"role": msg["role"], "content": msg["content"]})
    mensajes.append({"role": "user", "content": mensaje})

    try:
        # ── Appel Claude avec outils ───────────────────────────────────────
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=system_prompt,
            messages=mensajes,
            tools=TOOLS
        )

        # ── Si Claude veut utiliser un outil ──────────────────────────────
        if response.stop_reason == "tool_use":
            logger.info("Claude utilise un outil...")

            # Trouver le bloc tool_use
            tool_block = next(
                (b for b in response.content if b.type == "tool_use"), None
            )

            if tool_block:
                # Exécuter l'outil
                resultat_outil = await _executer_outil(
                    tool_block.name,
                    tool_block.input
                )
                logger.info(f"Résultat outil ({len(resultat_outil)} caractères) récupéré")

                # Continuer la conversation avec le résultat de l'outil
                mensajes_avec_outil = mensajes + [
                    {"role": "assistant", "content": response.content},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_block.id,
                                "content": resultat_outil
                            }
                        ]
                    }
                ]

                response2 = await client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=1024,
                    system=system_prompt,
                    messages=mensajes_avec_outil,
                    tools=TOOLS
                )

                respuesta = response2.content[0].text
                logger.info(f"Réponse avec catalogue ({response2.usage.output_tokens} tokens)")
                return respuesta

        # ── Réponse directe sans outil ─────────────────────────────────────
        respuesta = response.content[0].text
        logger.info(f"Réponse directe ({response.usage.input_tokens} in / {response.usage.output_tokens} out)")
        return respuesta

    except Exception as e:
        logger.error(f"Erreur API Claude : {e}")
        return obtener_mensaje_error()

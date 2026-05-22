# agent/brain.py — Cerveau de Tima : connexion avec l'API Claude

import os
import yaml
import logging
from pathlib import Path
from anthropic import AsyncAnthropic
from dotenv import load_dotenv

load_dotenv(override=True)
logger = logging.getLogger("agentkit")

client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


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
    system_prompt = config.get("system_prompt", "Tu es Tima, assistante de SAMANTAN. Réponds en français.")

    # Enrichir avec le contenu du site SAMANTAN (knowledge/)
    knowledge = cargar_knowledge()
    if knowledge:
        system_prompt += f"\n\n## Contenu récupéré depuis le site SAMANTAN\nUtilise ces informations pour répondre aux questions sur SAMANTAN, ses produits et services.\n{knowledge}"

    return system_prompt


def obtener_mensaje_error() -> str:
    config = cargar_config_prompts()
    return config.get("error_message", "Désolée, problème technique. Veuillez réessayer.")


def obtener_mensaje_fallback() -> str:
    config = cargar_config_prompts()
    return config.get("fallback_message", "Désolée, je n'ai pas compris. Pouvez-vous reformuler ?")


async def generar_respuesta(mensaje: str, historial: list[dict]) -> str:
    """
    Génère une réponse avec l'API Claude.

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
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=system_prompt,
            messages=mensajes
        )
        respuesta = response.content[0].text
        logger.info(f"Réponse générée ({response.usage.input_tokens} in / {response.usage.output_tokens} out)")
        return respuesta

    except Exception as e:
        logger.error(f"Erreur API Claude : {e}")
        return obtener_mensaje_error()

# agent/transcriber.py — Transcription des vocaux WhatsApp avec OpenAI Whisper

import os
import io
import httpx
import logging

logger = logging.getLogger("agentkit")


async def transcrire_vocal_meta(media_id: str, access_token: str) -> str:
    """
    Télécharge un vocal depuis Meta et le transcrit avec OpenAI Whisper.

    Args:
        media_id: L'ID du fichier audio dans Meta
        access_token: Le token d'accès Meta

    Returns:
        Le texte transcrit, ou chaîne vide en cas d'erreur
    """
    openai_key = os.getenv("OPENAI_API_KEY")
    if not openai_key:
        logger.warning("OPENAI_API_KEY non configurée — transcription désactivée")
        return ""

    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=openai_key)

        # Étape 1 : Récupérer l'URL de téléchargement depuis Meta
        async with httpx.AsyncClient() as http:
            r = await http.get(
                f"https://graph.facebook.com/v21.0/{media_id}",
                headers={"Authorization": f"Bearer {access_token}"}
            )
            if r.status_code != 200:
                logger.error(f"Erreur récupération URL média : {r.text}")
                return ""
            media_url = r.json().get("url", "")

        if not media_url:
            logger.error("URL média vide")
            return ""

        # Étape 2 : Télécharger le fichier audio
        async with httpx.AsyncClient() as http:
            r = await http.get(
                media_url,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=30.0
            )
            if r.status_code != 200:
                logger.error(f"Erreur téléchargement audio : {r.text}")
                return ""
            audio_bytes = r.content

        # Étape 3 : Transcrire avec Whisper
        audio_file = io.BytesIO(audio_bytes)
        audio_file.name = "vocal.ogg"  # Meta envoie les vocaux en format OGG

        transcript = await client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
        )

        texte = transcript.text.strip()
        logger.info(f"Vocal transcrit ({len(texte)} caractères) : {texte[:80]}...")
        return texte

    except Exception as e:
        logger.error(f"Erreur transcription vocal : {e}")
        return ""

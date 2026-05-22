# agent/providers/meta.py — Adaptateur pour Meta WhatsApp Cloud API

import os
import logging
import httpx
from fastapi import Request
from fastapi.responses import PlainTextResponse
from agent.providers.base import ProveedorWhatsApp, MensajeEntrante

logger = logging.getLogger("agentkit")


class ProveedorMeta(ProveedorWhatsApp):
    """Fournisseur WhatsApp via Meta Cloud API."""

    def __init__(self):
        self.access_token = os.getenv("META_ACCESS_TOKEN")
        self.phone_number_id = os.getenv("META_PHONE_NUMBER_ID")
        self.verify_token = os.getenv("META_VERIFY_TOKEN", "samantan-tima-2024")
        self.api_version = "v21.0"

    async def validar_webhook(self, request: Request):
        """Meta requiert une vérification GET avec hub.verify_token."""
        params = request.query_params
        mode = params.get("hub.mode")
        token = params.get("hub.verify_token")
        challenge = params.get("hub.challenge")
        if mode == "subscribe" and token == self.verify_token:
            return int(challenge)
        return None

    async def parsear_webhook(self, request: Request) -> list[MensajeEntrante]:
        """Parse le payload de Meta Cloud API (texte + vocaux)."""
        body = await request.json()
        mensajes = []
        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                for msg in value.get("messages", []):
                    tipo = msg.get("type")

                    if tipo == "text":
                        # Message texte normal
                        mensajes.append(MensajeEntrante(
                            telefono=msg.get("from", ""),
                            texto=msg.get("text", {}).get("body", ""),
                            mensaje_id=msg.get("id", ""),
                            es_propio=False,
                        ))

                    elif tipo == "audio":
                        # Vocal WhatsApp — télécharger et transcrire
                        media_id = msg.get("audio", {}).get("id", "")
                        token_actuel = os.getenv("META_ACCESS_TOKEN") or self.access_token
                        if media_id and token_actuel:
                            from agent.transcriber import transcrire_vocal_meta
                            texto = await transcrire_vocal_meta(media_id, token_actuel)
                            if texto:
                                logger.info(f"Vocal transcrit de {msg.get('from', '')} : {texto[:60]}...")
                                mensajes.append(MensajeEntrante(
                                    telefono=msg.get("from", ""),
                                    texto=texto,
                                    mensaje_id=msg.get("id", ""),
                                    es_propio=False,
                                ))
                            else:
                                logger.warning("Vocal reçu mais transcription échouée ou vide")

        return mensajes

    async def enviar_mensaje(self, telefono: str, mensaje: str) -> bool:
        """Envoie un message via Meta WhatsApp Cloud API."""
        # Lire le token frais à chaque envoi (resilient aux rotations de token sur Railway)
        access_token = os.getenv("META_ACCESS_TOKEN") or self.access_token
        if not access_token or not self.phone_number_id:
            logger.warning("META_ACCESS_TOKEN ou META_PHONE_NUMBER_ID non configurés")
            return False
        url = f"https://graph.facebook.com/{self.api_version}/{self.phone_number_id}/messages"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "messaging_product": "whatsapp",
            "to": telefono,
            "type": "text",
            "text": {"body": mensaje},
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(url, json=payload, headers=headers)
            if r.status_code != 200:
                logger.error(
                    f"Erreur Meta API : {r.status_code} — {r.text[:300]}\n"
                    f"Token utilisé : {access_token[:20]}..."
                )
                if r.status_code == 401:
                    logger.error(
                        "TOKEN EXPIRÉ — Mets à jour META_ACCESS_TOKEN sur Railway : "
                        "https://developers.facebook.com/tools/accesstoken/"
                    )
            else:
                logger.info(f"Message WhatsApp envoyé à {telefono} ✓")
            return r.status_code == 200

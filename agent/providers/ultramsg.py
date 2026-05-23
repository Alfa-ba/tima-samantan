# agent/providers/ultramsg.py — Adaptateur pour UltraMsg WhatsApp API

import os
import logging
import httpx
from fastapi import Request
from fastapi.responses import PlainTextResponse
from agent.providers.base import ProveedorWhatsApp, MensajeEntrante

logger = logging.getLogger("agentkit")

# Debug : stocke les derniers payloads bruts pour diagnostic (/debug-webhooks)
DERNIERS_WEBHOOKS: list = []


class ProveedorUltraMsg(ProveedorWhatsApp):
    """
    Fournisseur WhatsApp via UltraMsg (https://ultramsg.com).
    Plan gratuit disponible — pas de token qui expire.

    Variables requises dans Railway :
      ULTRAMSG_INSTANCE_ID  → ex: instance12345
      ULTRAMSG_TOKEN        → token UltraMsg de l'instance
    """

    def __init__(self):
        self.instance_id = os.getenv("ULTRAMSG_INSTANCE_ID", "")
        self.token = os.getenv("ULTRAMSG_TOKEN", "")
        self.base_url = f"https://api.ultramsg.com/{self.instance_id}"

    async def validar_webhook(self, request: Request):
        """UltraMsg n'a pas de vérification GET — retourne None."""
        return None

    async def parsear_webhook(self, request: Request) -> list[MensajeEntrante]:
        """
        Parse le payload UltraMsg.
        Format :
          { "data": { "id": "...", "from": "221XXXXXXXX@c.us",
                      "body": "texte", "type": "chat", ... },
            "event_type": "message_received" }
        """
        mensajes = []
        try:
            body = await request.json()
        except Exception:
            # UltraMsg peut envoyer du form-data
            try:
                form = await request.form()
                body = dict(form)
            except Exception as e:
                logger.warning(f"UltraMsg payload illisible : {e}")
                return []

        # ── Debug : garder les 10 derniers payloads bruts ─────────────────────
        try:
            DERNIERS_WEBHOOKS.append(body)
            if len(DERNIERS_WEBHOOKS) > 10:
                DERNIERS_WEBHOOKS.pop(0)
        except Exception:
            pass

        event_type = body.get("event_type", "")

        # Ignorer les statuts de livraison (ack, delivery, etc.)
        if event_type and "message" not in event_type.lower():
            return []

        data = body.get("data", {})
        if not data:
            return []

        msg_type = data.get("type", "")
        msg_from = data.get("from", "")
        msg_id   = data.get("id", "")
        msg_body = data.get("body", "").strip()

        # Ignorer les messages envoyés par nous-mêmes
        if data.get("fromMe") or data.get("from_me"):
            return []

        # Normaliser le numéro : "221XXXXXXXX@c.us" → "221XXXXXXXX"
        telefono = msg_from.split("@")[0] if "@" in msg_from else msg_from

        if msg_type == "chat" and msg_body:
            mensajes.append(MensajeEntrante(
                telefono=telefono,
                texto=msg_body,
                mensaje_id=msg_id,
                es_propio=False,
            ))
        elif msg_type == "image":
            # Message avec image (ex: photo d'ordonnance) — UltraMsg met l'URL dans 'media'
            media_url = data.get("media", "") or data.get("body", "")
            caption = data.get("caption", "")
            logger.info(f"Image reçue de {telefono} : {media_url[:80]}")
            mensajes.append(MensajeEntrante(
                telefono=telefono,
                texto=caption,  # légende éventuelle
                mensaje_id=msg_id,
                es_propio=False,
                imagen_url=media_url,
            ))
        elif msg_type in ["ptt", "audio"]:
            # Message vocal — pas de transcription UltraMsg pour l'instant
            logger.info(f"Message vocal reçu de {telefono} (non transcrit)")

        return mensajes

    async def enviar_mensaje(self, telefono: str, mensaje: str) -> bool:
        """
        Envoie un message via UltraMsg API.
        POST https://api.ultramsg.com/{instance}/messages/chat
        """
        # Lire token frais à chaque envoi
        token = os.getenv("ULTRAMSG_TOKEN") or self.token
        instance = os.getenv("ULTRAMSG_INSTANCE_ID") or self.instance_id

        if not token or not instance:
            logger.warning("ULTRAMSG_TOKEN ou ULTRAMSG_INSTANCE_ID non configurés")
            return False

        url = f"https://api.ultramsg.com/{instance}/messages/chat"
        payload = {
            "token": token,
            "to": telefono,
            "body": mensaje,
            "priority": "10",
        }

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.post(url, data=payload)
                if r.status_code == 200:
                    resp = r.json()
                    if resp.get("sent") == "true" or resp.get("sent") is True:
                        logger.info(f"Message UltraMsg envoyé à {telefono} ✓")
                        return True
                    else:
                        logger.error(f"UltraMsg erreur envoi : {resp}")
                        return False
                else:
                    logger.error(f"UltraMsg HTTP {r.status_code} : {r.text[:200]}")
                    return False
        except Exception as e:
            logger.error(f"UltraMsg exception : {e}")
            return False

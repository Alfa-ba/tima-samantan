# agent/providers/__init__.py — Factory de fournisseurs WhatsApp

import os
from agent.providers.base import ProveedorWhatsApp


def obtener_proveedor() -> ProveedorWhatsApp:
    """Retourne le fournisseur WhatsApp configuré dans .env."""
    proveedor = os.getenv("WHATSAPP_PROVIDER", "").lower()

    if not proveedor:
        raise ValueError("WHATSAPP_PROVIDER non configuré dans .env. Utilisez : meta ou twilio")

    if proveedor == "meta":
        from agent.providers.meta import ProveedorMeta
        return ProveedorMeta()
    elif proveedor == "twilio":
        from agent.providers.twilio import ProveedorTwilio
        return ProveedorTwilio()
    elif proveedor == "ultramsg":
        from agent.providers.ultramsg import ProveedorUltraMsg
        return ProveedorUltraMsg()
    else:
        raise ValueError(f"Fournisseur non supporté : {proveedor}. Utilisez : meta, twilio ou ultramsg")

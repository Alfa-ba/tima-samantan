# agent/providers/base.py — Classe de base pour les fournisseurs WhatsApp

from abc import ABC, abstractmethod
from dataclasses import dataclass
from fastapi import Request


@dataclass
class MensajeEntrante:
    """Message normalisé — même format quel que soit le fournisseur."""
    telefono: str
    texto: str
    mensaje_id: str
    es_propio: bool
    imagen_url: str = ""  # URL de l'image si le message en contient une (ordonnance, etc.)


class ProveedorWhatsApp(ABC):
    """Interface que chaque fournisseur WhatsApp doit implémenter."""

    @abstractmethod
    async def parsear_webhook(self, request: Request) -> list[MensajeEntrante]:
        """Extrait et normalise les messages du payload webhook."""
        ...

    @abstractmethod
    async def enviar_mensaje(self, telefono: str, mensaje: str) -> bool:
        """Envoie un message texte. Retourne True si succès."""
        ...

    async def validar_webhook(self, request: Request) -> dict | int | None:
        """Vérification GET du webhook (uniquement Meta). Retourne réponse ou None."""
        return None

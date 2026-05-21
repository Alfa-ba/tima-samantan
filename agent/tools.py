# agent/tools.py — Outils métier de Tima pour SAMANTAN

import os
import yaml
import logging
from datetime import datetime

logger = logging.getLogger("agentkit")


def cargar_info_negocio() -> dict:
    """Charge les informations du business depuis business.yaml."""
    try:
        with open("config/business.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        logger.error("config/business.yaml introuvable")
        return {}


def obtener_horario() -> dict:
    """Retourne les horaires d'ouverture."""
    return {
        "horaire": "24h/24, 7j/7",
        "est_ouvert": True,
    }


def buscar_en_knowledge(consulta: str) -> str:
    """Recherche dans les fichiers de /knowledge."""
    resultados = []
    knowledge_dir = "knowledge"

    if not os.path.exists(knowledge_dir):
        return "Aucun fichier de connaissance disponible."

    for archivo in os.listdir(knowledge_dir):
        ruta = os.path.join(knowledge_dir, archivo)
        if archivo.startswith(".") or not os.path.isfile(ruta):
            continue
        try:
            with open(ruta, "r", encoding="utf-8") as f:
                contenido = f.read()
                if consulta.lower() in contenido.lower():
                    resultados.append(f"[{archivo}]: {contenido[:500]}")
        except (UnicodeDecodeError, IOError):
            continue

    if resultados:
        return "\n---\n".join(resultados)
    return "Aucune information spécifique trouvée dans mes fichiers."


def registrar_lead(telefono: str, nombre_boutique: str, responsable: str,
                   email: str, email_facturation: str = "") -> dict:
    """Enregistre un nouveau prospect / client opticien."""
    lead = {
        "telefono": telefono,
        "nombre_boutique": nombre_boutique,
        "responsable": responsable,
        "email": email,
        "email_facturation": email_facturation or email,
        "fecha_registro": datetime.utcnow().isoformat(),
        "estado": "nouveau_prospect",
    }
    logger.info(f"Nouveau prospect enregistré : {nombre_boutique} — {responsable}")
    return lead


def preparar_commande(telefono: str, productos: list[dict]) -> dict:
    """Prépare une commande avec les détails produits collectés."""
    commande = {
        "telefono": telefono,
        "productos": productos,
        "fecha": datetime.utcnow().isoformat(),
        "estado": "en_attente_confirmation",
    }
    logger.info(f"Commande préparée pour {telefono} : {len(productos)} produit(s)")
    return commande


def obtener_info_tarifs() -> str:
    """Retourne le message standard sur les tarifs personnalisés."""
    return (
        "Les tarifs SAMANTAN sont personnalisés pour chaque opticien. "
        "Connectez-vous sur www.samantan.net avec votre compte professionnel "
        "pour consulter vos prix. Si vous n'avez pas encore de compte, "
        "contactez-nous au +221 77 543 43 16 ou par WhatsApp au +221 76 133 35 33."
    )


def obtener_info_livraison() -> str:
    """Retourne les informations de livraison."""
    return (
        "SAMANTAN livre partout — national et international ! "
        "Pour les délais et frais de livraison selon votre localisation, "
        "contactez notre équipe au +221 77 543 43 16."
    )

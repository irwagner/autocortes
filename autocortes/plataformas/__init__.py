"""Integrações com as redes: API oficial, Upload-Post ou postagem à mão (tarefas no painel)."""

from __future__ import annotations

from ..config import Config
from .base import Entrega, ErroPublicacao, Plataforma, Resultado

__all__ = ["Entrega", "ErroPublicacao", "Plataforma", "Resultado", "criar"]


def criar(nome: str, cfg: Config) -> Plataforma:
    """A rede com o envio de [<rede>].envio ("oficial", "upload_post", "navegador" ou "manual")."""
    envio = cfg[nome].get("envio")
    if envio == "manual":
        from .manual import ViaManual

        return ViaManual(nome, cfg)
    if envio == "navegador":
        from .navegador import ViaNavegador

        return ViaNavegador(nome, cfg)
    if envio == "upload_post":
        from .upload_post import ViaUploadPost

        return ViaUploadPost(nome, cfg)
    if nome == "youtube":
        from .youtube import YouTube

        return YouTube(cfg)
    if nome == "tiktok":
        from .tiktok import TikTok

        return TikTok(cfg)
    if nome == "instagram":
        from .instagram import Instagram

        return Instagram(cfg)
    if nome == "facebook":
        from .facebook import Facebook

        return Facebook(cfg)
    raise ValueError(f"plataforma desconhecida: {nome}")

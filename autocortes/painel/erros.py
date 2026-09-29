"""Erro HTTP com status e dados extras para o painel."""

from __future__ import annotations


class ErroHttp(Exception):
    def __init__(self, status: int, mensagem: str, extra: dict | None = None):
        super().__init__(mensagem)
        self.status = status
        self.extra = extra or {}

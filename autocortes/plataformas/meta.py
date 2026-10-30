"""Base comum da Graph API da Meta (Instagram e Página do Facebook): URLs, chamadas e erros."""

from __future__ import annotations

from .base import ErroPublicacao, Plataforma, json_seguro

# instabilidade da Meta: tenta de novo
TEMPORARIOS = {1, 2}
# limites de uso da API (nada saiu): a rede espera antes de tentar de novo
LIMITES = {4, 17, 32, 341, 613}
# 368 = ação considerada abusiva ou proibida: insistir piora. Para até você testar a conta.
BLOQUEIOS_PERSISTENTES = {368}
# subcódigos do Instagram: limite de 24 h, conta restrita e problemas do próprio vídeo
SUB_LIMITE_24H = 2207042
SUB_PERSISTENTES = {2207050, 2207051}  # conta inativa ou restrita; ação bloqueada como spam
SUB_DO_VIDEO = {2207004, 2207005, 2207006, 2207009, 2207010, 2207013, 2207023, 2207026}


class GraphMeta(Plataforma):
    graph_url = "https://graph.facebook.com"

    @property
    def versao(self) -> str:
        return str(self.conf["versao_api"])

    def _url(self, caminho: str) -> str:
        return f"{self.graph_url}/{self.versao}/{caminho.lstrip('/')}"

    def _erro(self, http: int, erro: dict, rede: str = "Instagram") -> None:
        codigo, sub = erro.get("code"), erro.get("error_subcode")
        msg = f"{rede}: {erro.get('message') or f'HTTP {http}'} (código {codigo}{f'/{sub}' if sub else ''})"
        if codigo in BLOQUEIOS_PERSISTENTES or sub in SUB_PERSISTENTES:
            raise ErroPublicacao(msg + ". A Meta bloqueou a ação: resolva na conta e teste de novo em Redes sociais",
                                 "bloqueio", persistente=True)
        if codigo in (10, 102, 190) or (isinstance(codigo, int) and 200 <= codigo < 300):
            raise ErroPublicacao(msg, "bloqueio")  # token inválido ou permissão faltando
        if sub == SUB_LIMITE_24H:
            raise ErroPublicacao(msg + " (limite de posts em 24 h)", "temporario", ambiente=True, pausa_seg=6 * 3600)
        if codigo in LIMITES or http == 429:
            raise ErroPublicacao(msg, "temporario", ambiente=True, pausa_seg=3600)
        if sub in SUB_DO_VIDEO:
            raise ErroPublicacao(msg, "corte")
        # instabilidade ou erro que eu não conheço: tenta de novo (o limite de falhas do corte segura)
        raise ErroPublicacao(msg, "temporario")

    def _graph(self, metodo: str, caminho: str, token: str | None = None, rede: str = "Instagram", **params) -> dict:
        params["access_token"] = token or self.token().get("access_token")
        if metodo == "GET":
            r = self.requisicao("GET", self._url(caminho), params=params)
        else:
            r = self.requisicao("POST", self._url(caminho), data=params)
        dados = json_seguro(r)
        if r.status_code >= 400 or "error" in dados:
            self._erro(r.status_code, dados.get("error") if isinstance(dados.get("error"), dict) else {}, rede)
        return dados

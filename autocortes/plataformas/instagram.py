"""Instagram Reels pela API do Instagram com Login do Facebook (upload resumível de arquivo local)."""

from __future__ import annotations

import getpass
import json
import threading
import time
from pathlib import Path

from ..textos import Conteudo, limitar, limitar_hashtags
from .base import ErroPublicacao, LeitorProgresso, Plataforma, Resultado, esperar, json_seguro

PERMISSOES = (
    "instagram_basic, instagram_content_publish, instagram_manage_insights, pages_show_list, "
    "pages_read_engagement, business_management"
)
# para postar o Reel também na Página do Facebook ([instagram].pagina_facebook)
PERMISSAO_PAGINA = "pages_manage_posts"
METRICAS = "views,likes,comments,shares,saved,reach"
TEMPORARIOS = {1, 2, 4, 17, 32, 341, 368, 613}
# o Instagram passou a aceitar no máximo 5 hashtags por post (dez/2025, segundo a imprensa)
MAX_HASHTAGS = 5
# Reels da Página do Facebook pela API: de 3 a 90 s, até 30 por Página em 24 h
FACEBOOK_MAX_SEG = 90


class Instagram(Plataforma):
    nome = "instagram"
    rotulo = "Instagram"
    graph_url = "https://graph.facebook.com"
    rupload_url = "https://rupload.facebook.com/ig-api-upload"
    rupload_facebook_url = "https://rupload.facebook.com/video-upload"

    @property
    def versao(self) -> str:
        return str(self.conf["versao_api"])

    def _url(self, caminho: str) -> str:
        return f"{self.graph_url}/{self.versao}/{caminho.lstrip('/')}"

    def pronta(self) -> tuple[bool, str]:
        tok = self.token()
        if not tok.get("access_token") or not tok.get("ig_user_id"):
            return False, "falta conectar a conta"
        return True, "ok"

    def pagina(self) -> str | None:
        """Nome da Página do Facebook ligada à conta (recebe o Reel com [instagram].pagina_facebook)."""
        return self.token().get("pagina_nome")

    # ------------------------------------------------------------ Graph API
    def _erro(self, http: int, erro: dict, rede: str = "Instagram") -> None:
        codigo, sub = erro.get("code"), erro.get("error_subcode")
        msg = f"{rede}: {erro.get('message') or f'HTTP {http}'} (código {codigo}{f'/{sub}' if sub else ''})"
        if codigo in (10, 102, 190) or (isinstance(codigo, int) and 200 <= codigo < 300):
            tipo = "bloqueio"  # token inválido ou permissão faltando
        elif codigo in TEMPORARIOS or sub == 2207042 or http >= 500 or http == 429:
            tipo = "temporario"  # limites de uso, instabilidade, limite de publicação em 24h
        else:
            tipo = "corte"
        raise ErroPublicacao(msg, tipo)

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

    # ------------------------------------------------------------ login
    def _credenciais(self) -> tuple[str, str]:
        app_id, segredo = str(self.conf["app_id"]).strip(), str(self.conf["app_secret"]).strip()
        if not (app_id and segredo):
            raise ErroPublicacao("Preencha o App ID e a chave secreta do app do Instagram", "bloqueio")
        return app_id, segredo

    def autorizar(self) -> None:
        app_id, _ = self._credenciais()
        permissoes = PERMISSOES + (f", {PERMISSAO_PAGINA}" if self.conf["pagina_facebook"] else "")
        print(
            "\nPara conectar o Instagram:\n"
            "  1. Abra https://developers.facebook.com/tools/explorer/\n"
            f"  2. Em 'App da Meta' escolha o app {app_id}\n"
            "  3. Em 'Usuário ou Página' escolha 'Obter token de acesso do usuário'\n"
            f"  4. Adicione as permissões: {permissoes}\n"
            "  5. Clique em 'Generate Access Token', autorize a Página e a conta do Instagram\n"
            "  6. Copie o token gerado\n"
        )
        curto = getpass.getpass("Cole o token (não aparece na tela) e tecle Enter: ").strip()
        paginas = self.trocar_token(curto)
        escolhida = paginas[0]
        if len(paginas) > 1:
            for i, p in enumerate(paginas, 1):
                print(f"  {i}) {p['name']} -> @{p['instagram_business_account'].get('username')}")
            while True:
                opcao = input("Número da conta que vai receber os Reels: ").strip()
                if opcao.isdigit() and 1 <= int(opcao) <= len(paginas):
                    escolhida = paginas[int(opcao) - 1]
                    break
        print(f"Instagram conectado: {self.salvar_pagina(escolhida)}")

    def trocar_token(self, curto: str) -> list[dict]:
        """Troca o token curto do Graph API Explorer e lista as Páginas com Instagram profissional."""
        app_id, segredo = self._credenciais()
        curto = curto.strip()
        if not curto:
            raise ErroPublicacao("Nenhum token informado", "bloqueio")
        r = self.requisicao(
            "GET",
            self._url("oauth/access_token"),
            params={
                "grant_type": "fb_exchange_token",
                "client_id": app_id,
                "client_secret": segredo,
                "fb_exchange_token": curto,
            },
        )
        dados = json_seguro(r)
        if r.status_code >= 400 or "access_token" not in dados:
            self._erro(r.status_code, dados.get("error") if isinstance(dados.get("error"), dict) else {})
        token_longo = dados["access_token"]

        contas = self._graph(
            "GET",
            "me/accounts",
            token=token_longo,
            fields="id,name,access_token,instagram_business_account{id,username}",
            limit=100,
        )
        paginas = [p for p in contas.get("data", []) if p.get("instagram_business_account")]
        if not paginas:
            raise ErroPublicacao(
                "Nenhuma Página com Instagram profissional vinculado apareceu para esse token. "
                "Confira as permissões marcadas e o vínculo Página - Instagram.",
                "bloqueio",
            )
        return paginas

    def salvar_pagina(self, escolhida: dict) -> str:
        ig = escolhida["instagram_business_account"]
        # token de Página gerado a partir de token de usuário de longa duração não expira
        self.salvar_token(
            {
                "access_token": escolhida["access_token"],
                "ig_user_id": ig["id"],
                "ig_username": ig.get("username"),
                "pagina_id": escolhida["id"],
                "pagina_nome": escolhida.get("name"),
                "obtido_em": time.time(),
            }
        )
        conta = self.verificar()
        self.lembrar_conta(conta)
        return conta

    def conta(self) -> str | None:
        tok = self.token()
        return tok.get("conta") or (f"@{tok['ig_username']}" if tok.get("ig_username") else None)

    def verificar(self) -> str:
        tok = self.token()
        if not tok.get("ig_user_id"):
            raise ErroPublicacao("Instagram sem conta conectada", "bloqueio")
        perfil = self._graph("GET", tok["ig_user_id"], fields="username")
        texto = f"@{perfil.get('username')}"
        try:
            limite = self._graph("GET", f"{tok['ig_user_id']}/content_publishing_limit", fields="quota_usage,config")
            uso = (limite.get("data") or [{}])[0]
            texto += f" (posts via API nas últimas 24h: {uso.get('quota_usage', '?')}/{(uso.get('config') or {}).get('quota_total', '?')})"
        except ErroPublicacao:
            pass
        if self.conf["pagina_facebook"]:
            texto += self._conferir_pagina(tok)
        return texto

    def _conferir_pagina(self, tok: dict) -> str:
        """Confere se o token pode postar na Página (permissão pages_manage_posts)."""
        pagina = tok.get("pagina_nome") or tok.get("pagina_id") or "?"
        try:
            app_id, segredo = self._credenciais()
            dados = self._graph("GET", "debug_token", token=f"{app_id}|{segredo}", input_token=tok.get("access_token"))
        except ErroPublicacao:
            return f" · Página {pagina}"
        escopos = (dados.get("data") or {}).get("scopes") if isinstance(dados.get("data"), dict) else None
        if isinstance(escopos, list) and PERMISSAO_PAGINA not in escopos:
            return f" · falta a permissão {PERMISSAO_PAGINA} para postar na Página {pagina}: conecte de novo"
        return f" · Página {pagina}"

    # ------------------------------------------------------------ métricas
    def estatisticas(self, ids: list[str]) -> dict[str, dict]:
        """Métricas de cada Reel (precisa da permissão instagram_manage_insights no token)."""
        saida: dict[str, dict] = {}
        for media_id in ids:
            dados = self._graph("GET", f"{media_id}/insights", metric=METRICAS)
            valores: dict[str, int] = {}
            for item in dados.get("data") or []:
                valor = (item.get("total_value") or {}).get("value")
                if valor is None and item.get("values"):
                    valor = item["values"][0].get("value")
                if isinstance(valor, (int, float)):
                    valores[str(item.get("name"))] = int(valor)
            saida[media_id] = {
                "visualizacoes": valores.get("views"),
                "curtidas": valores.get("likes"),
                "comentarios": valores.get("comments"),
                "compartilhamentos": valores.get("shares"),
                "extra": {k: v for k, v in valores.items() if k in ("saved", "reach")},
            }
        return saida

    # ------------------------------------------------------------ publicação
    def publicar(self, arquivo: Path, conteudo: Conteudo, parar: threading.Event | None = None) -> Resultado:
        tok = self.token()
        ig = tok.get("ig_user_id")
        if not ig or not tok.get("access_token"):
            raise ErroPublicacao("Instagram sem conta conectada", "bloqueio")

        parametros = {
            "media_type": "REELS",
            "upload_type": "resumable",
            "caption": limitar(limitar_hashtags(conteudo.descricao or conteudo.titulo, MAX_HASHTAGS), 2200),
            "thumb_offset": str(int(self.conf["capa_ms"])),
        }
        if self.conf["trial_reels"]:
            # reel de teste: aparece primeiro só para quem não segue a conta
            parametros["trial_params"] = json.dumps({"graduation_strategy": self.conf["trial_graduacao"]})
        else:
            parametros["share_to_feed"] = "true" if self.conf["compartilhar_no_feed"] else "false"
        conteiner = self._graph("POST", f"{ig}/media", **parametros)
        container_id = conteiner.get("id")
        if not container_id:
            raise ErroPublicacao("Instagram não devolveu o id do contêiner", "temporario")
        destino = conteiner.get("uri") or f"{self.rupload_url}/{self.versao}/{container_id}"

        tamanho = arquivo.stat().st_size
        with LeitorProgresso(arquivo) as f:
            r = self.requisicao(
                "POST",
                destino,
                headers={"Authorization": f"OAuth {tok['access_token']}", "offset": "0", "file_size": str(tamanho)},
                data=f,
                timeout=(20, 900),
            )
        resposta = json_seguro(r)
        if r.status_code >= 400 or not resposta.get("success"):
            info = resposta.get("debug_info") if isinstance(resposta.get("debug_info"), dict) else {}
            tipo = "temporario" if r.status_code >= 500 or info.get("retriable") else "corte"
            detalhe = f"{info.get('type') or ''} {info.get('message') or ''}".strip() or r.text[:200]
            raise ErroPublicacao(f"Instagram: upload falhou (HTTP {r.status_code}) {detalhe}", tipo)

        self._aguardar_processamento(container_id, parar)

        media_id = None
        for tentativa in range(5):
            try:
                media_id = self._graph("POST", f"{ig}/media_publish", creation_id=container_id).get("id")
                break
            except ErroPublicacao as e:
                if "9007" in str(e) and tentativa < 4:  # mídia ainda não está pronta
                    esperar(parar, 15)
                    continue
                raise
        if not media_id:
            raise ErroPublicacao("Instagram não devolveu o id da publicação", "temporario")
        url = None
        try:
            url = self._graph("GET", media_id, fields="permalink").get("permalink")
        except ErroPublicacao:
            pass
        return Resultado(media_id, url)

    def _aguardar_processamento(self, container_id: str, parar) -> None:
        limite = time.monotonic() + 15 * 60
        while time.monotonic() < limite:
            esperar(parar, 10)
            d = self._graph("GET", container_id, fields="status_code,status")
            codigo = d.get("status_code")
            if codigo in ("FINISHED", "PUBLISHED"):
                return
            if codigo == "ERROR":
                raise ErroPublicacao(f"Instagram não conseguiu processar o vídeo: {d.get('status')}", "corte")
            if codigo == "EXPIRED":
                raise ErroPublicacao("O contêiner do Instagram expirou", "temporario")
        raise ErroPublicacao("O Instagram demorou demais para processar o vídeo", "temporario")

    # ------------------------------------------------------------ Página do Facebook
    def publicar_facebook(self, arquivo: Path, conteudo: Conteudo, parar: threading.Event | None = None,
                          ao_avancar=None) -> Resultado:
        """O mesmo Reel na Página do Facebook ligada à conta (API de Reels da Página, com o token dela).

        ao_avancar(video_id, url) é chamado depois do início do envio (url None) e quando o Facebook
        aceita a publicação: se o envio for interrompido depois disso, o Reel já existe e não é
        enviado de novo.
        """
        tok = self.token()
        pagina, token = tok.get("pagina_id"), tok.get("access_token")
        if not pagina or not token:
            raise ErroPublicacao("Facebook: conecte o Instagram de novo para postar na Página", "bloqueio")
        inicio = self._graph("POST", f"{pagina}/video_reels", rede="Facebook", upload_phase="start")
        video_id = str(inicio.get("video_id") or "")
        if not video_id:
            raise ErroPublicacao("Facebook não devolveu o id do vídeo", "temporario")
        if ao_avancar:
            ao_avancar(video_id, None)
        destino = inicio.get("upload_url") or f"{self.rupload_facebook_url}/{self.versao}/{video_id}"

        tamanho = arquivo.stat().st_size
        with LeitorProgresso(arquivo) as f:
            r = self.requisicao(
                "POST",
                destino,
                headers={"Authorization": f"OAuth {token}", "offset": "0", "file_size": str(tamanho)},
                data=f,
                timeout=(20, 900),
            )
        resposta = json_seguro(r)
        if r.status_code >= 400 or not resposta.get("success"):
            info = resposta.get("debug_info") if isinstance(resposta.get("debug_info"), dict) else {}
            tipo = "temporario" if r.status_code >= 500 or info.get("retriable") else "corte"
            detalhe = f"{info.get('type') or ''} {info.get('message') or ''}".strip() or r.text[:200]
            raise ErroPublicacao(f"Facebook: envio do vídeo falhou (HTTP {r.status_code}) {detalhe}", tipo)

        fim = self._graph(
            "POST", f"{pagina}/video_reels", rede="Facebook", upload_phase="finish", video_id=video_id,
            video_state="PUBLISHED", description=limitar(conteudo.descricao or conteudo.titulo, 2200),
            title=limitar(conteudo.titulo, 255),
        )
        if fim.get("success") is False:
            raise ErroPublicacao("Facebook não aceitou publicar o Reel", "temporario")
        if ao_avancar:
            ao_avancar(video_id, f"https://www.facebook.com/reel/{video_id}")
        observacao = self._aguardar_reel(video_id, parar)
        return Resultado(video_id, self._link_reel(video_id), observacao)

    def _aguardar_reel(self, video_id: str, parar) -> str | None:
        """Espera o Facebook processar e publicar o Reel (até 10 min; depois ele termina sozinho)."""
        limite = time.monotonic() + 10 * 60
        while time.monotonic() < limite:
            esperar(parar, 10)
            try:
                d = self._graph("GET", video_id, rede="Facebook", fields="status")
            except ErroPublicacao as e:
                if e.tipo == "temporario":
                    continue
                raise
            status = d.get("status") if isinstance(d.get("status"), dict) else {}
            fases = [status.get(k) if isinstance(status.get(k), dict) else {}
                     for k in ("uploading_phase", "processing_phase", "publishing_phase")]
            falha = next((f for f in fases if f.get("status") == "error"), None)
            if status.get("video_status") == "error" or falha is not None:
                erro = (falha or {}).get("error")
                motivo = erro.get("message") if isinstance(erro, dict) else None
                raise ErroPublicacao(f"Facebook não conseguiu processar o Reel{f': {motivo}' if motivo else ''}", "corte")
            if status.get("video_status") == "expired":
                raise ErroPublicacao("O envio do Reel para o Facebook expirou", "temporario")
            publicacao = fases[2]
            if publicacao.get("status") == "complete" or publicacao.get("publish_status") == "published":
                return None
        return "o Facebook ainda estava processando o Reel"

    def _link_reel(self, video_id: str) -> str:
        try:
            link = str(self._graph("GET", video_id, rede="Facebook", fields="permalink_url").get("permalink_url") or "")
        except ErroPublicacao:
            link = ""
        if link.startswith("/"):  # a API devolve o caminho sem o domínio
            link = "https://www.facebook.com" + link
        return link if link.startswith("https://") else f"https://www.facebook.com/reel/{video_id}"

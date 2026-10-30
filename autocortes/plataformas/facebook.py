"""Reels na Página do Facebook pela API oficial (API de Reels da Página, com o token da Página)."""

from __future__ import annotations

import getpass
import threading
import time
from pathlib import Path

from ..segredos import ler_token
from ..textos import Conteudo
from .base import Entrega, ErroPublicacao, LeitorProgresso, Resultado, esperar, json_seguro
from .meta import GraphMeta

PERMISSOES = "pages_show_list, pages_read_engagement, pages_manage_posts, business_management"
PERMISSAO_POSTAR = "pages_manage_posts"
# Reels da Página pela API: de 3 a 90 s, até 30 por Página em 24 h
MIN_SEG = 3
MAX_SEG = 90
FACEBOOK_MAX_SEG = MAX_SEG


class Facebook(GraphMeta):
    nome = "facebook"
    rotulo = "Facebook"
    rupload_facebook_url = "https://rupload.facebook.com/video-upload"

    # ------------------------------------------------------------ login
    def token(self) -> dict:
        """O login próprio da Página; sem ele, o da Página ligada ao Instagram (o mesmo login de antes
        do Facebook separado), a não ser que você tenha desconectado aqui."""
        proprio = ler_token(self.arquivo_token)
        if proprio.get("access_token"):
            return proprio
        if proprio.get("sem_herdar"):
            return {}
        ig = ler_token(self.cfg.pasta_tokens / "instagram.json")
        if ig.get("access_token") and ig.get("pagina_id"):
            return {"access_token": ig["access_token"], "pagina_id": str(ig["pagina_id"]),
                    "pagina_nome": ig.get("pagina_nome"), "herdado": True}
        return proprio

    def conta(self) -> str | None:
        tok = self.token()
        if tok.get("herdado"):
            return f"Página {tok.get('pagina_nome') or tok.get('pagina_id')} (login do Instagram)"
        if tok.get("conta"):
            return tok["conta"]
        return f"Página {tok['pagina_nome']}" if tok.get("pagina_nome") else None

    def lembrar_conta(self, descricao: str) -> None:
        # o login herdado é do Instagram: copiar para cá deixaria uma cópia que envelhece
        if self.token().get("herdado"):
            return
        super().lembrar_conta(descricao)

    def desconectar(self) -> None:
        self.arquivo_token.unlink(missing_ok=True)
        # sem isto o login do Instagram voltaria a valer e o Desconectar não desconectaria
        self.salvar_token({"sem_herdar": True})

    def _credenciais(self) -> tuple[str, str]:
        app_id, segredo = str(self.conf["app_id"]).strip(), str(self.conf["app_secret"]).strip()
        if not (app_id and segredo):
            ig = self.cfg["instagram"]
            app_id, segredo = str(ig["app_id"]).strip(), str(ig["app_secret"]).strip()
        if not (app_id and segredo):
            raise ErroPublicacao("Preencha o App ID e a chave secreta do app da Meta (em Facebook ou no Instagram)",
                                 "bloqueio")
        return app_id, segredo

    def pronta(self) -> tuple[bool, str]:
        tok = self.token()
        if not tok.get("access_token") or not tok.get("pagina_id"):
            return False, "falta conectar a Página"
        esperado = str(self.conf["pagina_id"]).strip()
        if esperado and esperado != str(tok["pagina_id"]):
            quem = str(self.conf["pagina_nome"]).strip() or esperado
            return False, (f"o login conectado é da Página {tok.get('pagina_nome') or tok['pagina_id']}; "
                           f"conecte de novo e escolha {quem}")
        return True, "ok"

    def autorizar(self) -> None:
        app_id, _ = self._credenciais()
        print(
            "\nPara conectar a Página do Facebook:\n"
            "  1. Abra https://developers.facebook.com/tools/explorer/\n"
            f"  2. Em 'App da Meta' escolha o app {app_id}\n"
            "  3. Em 'Usuário ou Página' escolha 'Obter token de acesso do usuário'\n"
            f"  4. Adicione as permissões: {PERMISSOES}\n"
            "  5. Clique em 'Generate Access Token' e autorize a Página\n"
            "  6. Copie o token gerado\n"
        )
        curto = getpass.getpass("Cole o token (não aparece na tela) e tecle Enter: ").strip()
        paginas = self.trocar_token(curto)
        escolhida = paginas[0]
        if len(paginas) > 1:
            for i, p in enumerate(paginas, 1):
                print(f"  {i}) {p.get('name')} (id {p.get('id')})")
            while True:
                opcao = input("Número da Página que vai receber os Reels: ").strip()
                if opcao.isdigit() and 1 <= int(opcao) <= len(paginas):
                    escolhida = paginas[int(opcao) - 1]
                    break
        print(f"Facebook conectado: {self.salvar_pagina(escolhida)}")
        print(f"Informe pagina_id = \"{escolhida.get('id')}\" em [facebook] para o AutoCortes conferir a Página.")

    def trocar_token(self, curto: str) -> list[dict]:
        """Troca o token curto do Graph API Explorer e lista as Páginas em que você pode publicar."""
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
            self._erro(r.status_code, dados.get("error") if isinstance(dados.get("error"), dict) else {}, "Facebook")
        contas = self._graph("GET", "me/accounts", token=dados["access_token"], rede="Facebook",
                             fields="id,name,access_token,tasks", limit=100)
        paginas = []
        for p in contas.get("data") or []:
            if not isinstance(p, dict) or not p.get("access_token") or not str(p.get("id") or "").isdigit():
                continue
            tarefas = p.get("tasks")
            if isinstance(tarefas, list) and "CREATE_CONTENT" not in tarefas:
                continue  # sem permissão de criar conteúdo nesta Página
            paginas.append(p)
        if not paginas:
            raise ErroPublicacao(
                "Nenhuma Página que você administra apareceu para esse token (confira as permissões: "
                f"{PERMISSOES})", "bloqueio",
            )
        return paginas

    def salvar_pagina(self, escolhida: dict) -> str:
        # token de Página gerado a partir de token de usuário de longa duração não expira
        self.salvar_token({
            "access_token": escolhida["access_token"],
            "pagina_id": str(escolhida["id"]),
            "pagina_nome": escolhida.get("name"),
            "obtido_em": time.time(),
        })
        conta = self.verificar()
        self.lembrar_conta(conta)
        return conta

    def verificar(self) -> str:
        tok = self.token()
        if not tok.get("access_token") or not tok.get("pagina_id"):
            raise ErroPublicacao("Facebook sem Página conectada", "bloqueio")
        dados = self._graph("GET", str(tok["pagina_id"]), rede="Facebook", fields="name")
        texto = f"Página {dados.get('name') or tok.get('pagina_nome') or tok['pagina_id']}"
        if tok.get("herdado"):
            texto += " (login do Instagram)"
        try:
            app_id, segredo = self._credenciais()
            depuracao = self._graph("GET", "debug_token", token=f"{app_id}|{segredo}", rede="Facebook",
                                    input_token=tok["access_token"])
        except ErroPublicacao:
            return texto
        info = depuracao.get("data") if isinstance(depuracao.get("data"), dict) else {}
        escopos = info.get("scopes")
        if isinstance(escopos, list) and PERMISSAO_POSTAR not in escopos:
            texto += f" · falta a permissão {PERMISSAO_POSTAR}: conecte de novo"
        return texto

    # ------------------------------------------------------------ publicação
    def publicar(self, arquivo: Path, conteudo: Conteudo, parar: threading.Event | None = None,
                 entrega: Entrega | None = None) -> Resultado:
        """O Reel na Página (start, envio do arquivo e finish com video_state=PUBLISHED).

        A entrega é aceita quando o Facebook aceita publicar (com o link): se o envio for
        interrompido depois disso, o Reel já existe e não é enviado de novo.
        """
        from .manual import textos

        entrega = entrega if entrega is not None else Entrega()
        tok = self.token()
        pagina, token = tok.get("pagina_id"), tok.get("access_token")
        if not pagina or not token:
            raise ErroPublicacao("Facebook: conecte a Página em Redes sociais", "bloqueio")
        t = textos(self.cfg, "facebook", conteudo)
        inicio = self._graph("POST", f"{pagina}/video_reels", token=token, rede="Facebook", upload_phase="start")
        video_id = str(inicio.get("video_id") or "")
        if not video_id:
            raise ErroPublicacao("Facebook não devolveu o id do vídeo", "temporario")
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
            "POST", f"{pagina}/video_reels", token=token, rede="Facebook", upload_phase="finish",
            video_id=video_id, video_state="PUBLISHED", description=t["descricao"], title=t["titulo"],
        )
        if fim.get("success") is False:
            raise ErroPublicacao("Facebook não aceitou publicar o Reel", "temporario")
        entrega.aceitou(video_id, f"https://www.facebook.com/reel/{video_id}")
        observacao = self._aguardar_reel(video_id, token, parar, entrega)
        return Resultado(video_id, self._link_reel(video_id, token), observacao)

    def _aguardar_reel(self, video_id: str, token: str, parar, entrega: Entrega) -> str | None:
        """Espera o Facebook processar e publicar o Reel (até 10 min; depois ele termina sozinho).

        Se o Facebook avisar que não conseguiu (erro no processamento, envio expirado), o Reel não
        saiu: a entrega é desfeita e o envio pode ser repetido.
        """
        limite = time.monotonic() + 10 * 60
        while time.monotonic() < limite:
            esperar(parar, 10)
            try:
                d = self._graph("GET", video_id, token=token, rede="Facebook", fields="status")
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
                entrega.desfazer()
                raise ErroPublicacao(f"Facebook não conseguiu processar o Reel{f': {motivo}' if motivo else ''}", "corte")
            if status.get("video_status") == "expired":
                entrega.desfazer()
                raise ErroPublicacao("O envio do Reel para o Facebook expirou", "temporario")
            publicacao = fases[2]
            if publicacao.get("status") == "complete" or publicacao.get("publish_status") == "published":
                return None
        return "o Facebook ainda estava processando o Reel"

    def _link_reel(self, video_id: str, token: str) -> str:
        try:
            link = str(self._graph("GET", video_id, token=token, rede="Facebook",
                                   fields="permalink_url").get("permalink_url") or "")
        except ErroPublicacao:
            link = ""
        if link.startswith("/"):  # a API devolve o caminho sem o domínio
            link = "https://www.facebook.com" + link
        return link if link.startswith("https://") else f"https://www.facebook.com/reel/{video_id}"

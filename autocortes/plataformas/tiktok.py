"""TikTok pela Content Posting API (Direct Post ou rascunho na caixa de entrada)."""

from __future__ import annotations

import json
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

import requests

from ..textos import Conteudo, limitar, tamanho_utf16
from ..util import log
from .base import ErroPublicacao, LeitorProgresso, Plataforma, Resultado, esperar, json_seguro
from .oauth import LoginOAuth, ServidorRetorno, pkce

ESCOPOS = "user.info.basic,video.publish,video.upload"
MB = 1024 * 1024

# error.code da API -> ação
ERROS = {
    "access_token_invalid": "bloqueio",
    "scope_not_authorized": "bloqueio",
    "spam_risk_user_banned_from_posting": "bloqueio",
    "unaudited_client_can_only_post_to_private_accounts": "bloqueio",
    "privacy_level_option_mismatch": "bloqueio",
    "url_ownership_unverified": "bloqueio",
    "rate_limit_exceeded": "temporario",
    "spam_risk_too_many_posts": "temporario",
    "spam_risk_too_many_pending_share": "temporario",
    "reached_active_user_cap": "temporario",
    "internal_error": "temporario",
    "invalid_param": "corte",
}
DICAS = {
    "unaudited_client_can_only_post_to_private_accounts": "app não auditado: a conta do TikTok precisa estar privada",
    "privacy_level_option_mismatch": "a privacidade escolhida não está disponível para esta conta",
    "spam_risk_too_many_posts": "limite diário de posts via API atingido",
    "spam_risk_too_many_pending_share": "há 5 rascunhos pendentes: finalize-os no app",
    "reached_active_user_cap": "limite diário de usuários do app atingido",
}
# fail_reason do status -> ação
FALHAS = {
    "internal": "temporario",
    "publish_cancelled": "temporario",
    "video_pull_failed": "temporario",
    "spam_risk_too_many_posts": "temporario",
    "spam_risk_user_banned_from_posting": "bloqueio",
    "auth_removed": "bloqueio",
}


def plano_de_partes(tamanho: int) -> tuple[int, int]:
    """(chunk_size, total_chunk_count) seguindo as regras do Media Transfer Guide.

    Até 64 MB vai inteiro; acima disso partes de 10 MB e a última absorve o resto.
    """
    if tamanho <= 64 * MB:
        return tamanho, 1
    parte = 10 * MB
    return parte, tamanho // parte


class TikTok(Plataforma):
    nome = "tiktok"
    rotulo = "TikTok"
    auth_url = "https://www.tiktok.com/v2/auth/authorize/"
    api_url = "https://open.tiktokapis.com"

    def _credenciais(self) -> tuple[str, str]:
        chave, segredo = str(self.conf["client_key"]).strip(), str(self.conf["client_secret"]).strip()
        if not (chave and segredo):
            raise ErroPublicacao("Preencha o Client key e o Client secret do app do TikTok", "bloqueio")
        return chave, segredo

    def pronta(self) -> tuple[bool, str]:
        if not (self.conf["client_key"] and self.conf["client_secret"]):
            return False, "falta preencher o Client key e o Client secret"
        tok = self.token()
        if not tok.get("refresh_token"):
            return False, "falta conectar a conta"
        if tok.get("refresh_expira_em", 0) < time.time():
            return False, "o login expirou, conecte a conta de novo"
        return True, "ok"

    # ------------------------------------------------------------ login
    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{int(self.conf['porta_redirect'])}/callback/"

    def preparar_login(self) -> LoginOAuth:
        chave, _ = self._credenciais()
        servidor = ServidorRetorno(porta=int(self.conf["porta_redirect"]), caminho="/callback/")
        verificador, desafio = pkce(desafio_hex=True)
        estado = secrets.token_urlsafe(24)
        url = self.auth_url + "?" + urlencode(
            {
                "client_key": chave,
                "response_type": "code",
                "scope": ESCOPOS,
                "redirect_uri": servidor.redirect_uri,
                "state": estado,
                "code_challenge": desafio,
                "code_challenge_method": "S256",
            }
        )
        return LoginOAuth(url, servidor, verificador, estado)

    def autorizar(self) -> None:
        login = self.preparar_login()
        p = login.servidor.aguardar(login.url, login.estado)
        print(f"TikTok conectado: {self.concluir_login(p['code'], login.servidor.redirect_uri, login.verificador)}")

    def concluir_login(self, codigo: str, redirect_uri: str, verificador: str) -> str:
        chave, segredo = self._credenciais()
        dados = self._pedir_token(
            {
                "client_key": chave,
                "client_secret": segredo,
                "code": codigo,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
                "code_verifier": verificador,
            }
        )
        self._guardar(dados)
        conta = self.verificar()
        faltando = {"video.publish", "video.upload"} - set(str(dados.get("scope", "")).split(","))
        if faltando:
            conta += f" (atenção: o TikTok não concedeu {', '.join(sorted(faltando))})"
        self.lembrar_conta(conta)
        return conta

    def _pedir_token(self, formulario: dict) -> dict:
        r = self.requisicao(
            "POST",
            f"{self.api_url}/v2/oauth/token/",
            data=formulario,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cache-Control": "no-cache"},
        )
        dados = json_seguro(r)
        if r.status_code != 200 or "access_token" not in dados:
            erro = dados.get("error") or f"HTTP {r.status_code}"
            tipo = "temporario" if r.status_code >= 500 or r.status_code == 429 else "bloqueio"
            raise ErroPublicacao(f"TikTok: falha no token ({erro} {dados.get('error_description') or ''})".strip(), tipo)
        return dados

    def _guardar(self, dados: dict) -> None:
        agora = time.time()
        anterior = self.token()
        self.salvar_token(
            {
                "access_token": dados["access_token"],
                "refresh_token": dados["refresh_token"],
                "expira_em": agora + int(dados.get("expires_in") or 86400),
                "refresh_expira_em": agora + int(dados.get("refresh_expires_in") or 31536000),
                "open_id": dados.get("open_id"),
                "escopo": dados.get("scope"),
                "conta": anterior.get("conta") if anterior.get("open_id") == dados.get("open_id") else None,
            }
        )

    def _access_token(self, forcar: bool = False) -> str:
        tok = self.token()
        if not tok.get("refresh_token"):
            raise ErroPublicacao("TikTok sem conta conectada", "bloqueio")
        if not forcar and tok.get("access_token") and tok.get("expira_em", 0) - 300 > time.time():
            return tok["access_token"]
        chave, segredo = self._credenciais()
        dados = self._pedir_token(
            {
                "client_key": chave,
                "client_secret": segredo,
                "grant_type": "refresh_token",
                "refresh_token": tok["refresh_token"],
            }
        )
        self._guardar(dados)  # o TikTok pode devolver um refresh_token novo
        return dados["access_token"]

    def _api(self, caminho: str, corpo: dict | None = None, renovar: bool = True) -> dict:
        r = self.requisicao(
            "POST",
            f"{self.api_url}{caminho}",
            headers={
                "Authorization": f"Bearer {self._access_token()}",
                "Content-Type": "application/json; charset=UTF-8",
            },
            data=json.dumps(corpo or {}).encode("utf-8"),
        )
        dados = json_seguro(r)
        erro = dados.get("error") if isinstance(dados.get("error"), dict) else {}
        codigo = erro.get("code") or ("ok" if r.status_code == 200 else f"http_{r.status_code}")
        if codigo == "ok":
            return dados.get("data") or {}
        if codigo == "access_token_invalid" and renovar:
            self._access_token(forcar=True)
            return self._api(caminho, corpo, renovar=False)
        tipo = ERROS.get(codigo) or ("temporario" if r.status_code >= 500 or r.status_code == 429 else "corte")
        dica = DICAS.get(codigo)
        raise ErroPublicacao(f"TikTok: {codigo}{' - ' + dica if dica else ''} {erro.get('message') or ''}".strip(), tipo)

    def verificar(self) -> str:
        info = self._api("/v2/post/publish/creator_info/query/")
        opcoes = ", ".join(info.get("privacy_level_options") or [])
        return f"@{info.get('creator_username')} ({info.get('creator_nickname')}); privacidades: {opcoes}"

    # ------------------------------------------------------------ publicação
    def publicar(self, arquivo: Path, conteudo: Conteudo, parar: threading.Event | None = None) -> Resultado:
        tamanho = arquivo.stat().st_size
        parte, total = plano_de_partes(tamanho)
        fonte = {"source": "FILE_UPLOAD", "video_size": tamanho, "chunk_size": parte, "total_chunk_count": total}
        legenda = conteudo.descricao or conteudo.titulo
        while tamanho_utf16(legenda) > 2200:
            legenda = limitar(legenda, len(legenda) - 20)

        usuario = None
        if self.conf["modo"] == "rascunho":
            dados = self._api("/v2/post/publish/inbox/video/init/", {"source_info": fonte})
        else:
            info = self._api("/v2/post/publish/creator_info/query/")
            opcoes = info.get("privacy_level_options") or []
            privacidade = self.conf["privacidade"]
            if privacidade not in opcoes:
                raise ErroPublicacao(
                    f"TikTok: privacidade {privacidade} indisponível para esta conta (opções: {', '.join(opcoes)})",
                    "bloqueio",
                )
            maximo = info.get("max_video_post_duration_sec")
            if maximo and conteudo.duracao > float(maximo):
                raise ErroPublicacao(f"TikTok: vídeo de {conteudo.duracao:.0f}s passa do limite da conta ({maximo}s)", "corte")
            usuario = info.get("creator_username")
            post = {
                "title": legenda,
                "privacy_level": privacidade,
                "disable_comment": (not self.conf["permitir_comentarios"]) or bool(info.get("comment_disabled")),
                "disable_duet": (not self.conf["permitir_duet"]) or bool(info.get("duet_disabled")),
                "disable_stitch": (not self.conf["permitir_stitch"]) or bool(info.get("stitch_disabled")),
                "video_cover_timestamp_ms": int(self.conf["capa_ms"]),
                "brand_content_toggle": False,
                "brand_organic_toggle": False,
                "is_aigc": False,
            }
            dados = self._api("/v2/post/publish/video/init/", {"post_info": post, "source_info": fonte})

        publish_id, upload_url = dados.get("publish_id"), dados.get("upload_url")
        if not publish_id or not upload_url:
            raise ErroPublicacao("TikTok não devolveu publish_id/upload_url", "temporario")
        self._enviar(upload_url, arquivo, tamanho, parte, total)
        return self._acompanhar(publish_id, usuario, parar)

    def _enviar(self, url: str, arquivo: Path, tamanho: int, parte: int, total: int) -> None:
        for i in range(total):
            ini = i * parte
            fim = tamanho - 1 if i == total - 1 else ini + parte - 1
            for tentativa in range(1, 4):
                try:
                    with LeitorProgresso(arquivo, inicio=ini, fim=fim + 1, total=tamanho) as bloco:
                        r = self.sessao.put(
                            url,
                            data=bloco,
                            headers={
                                "Content-Type": "video/mp4",
                                "Content-Length": str(fim - ini + 1),
                                "Content-Range": f"bytes {ini}-{fim}/{tamanho}",
                            },
                            timeout=(20, 600),
                        )
                except requests.RequestException as e:
                    if tentativa == 3:
                        raise ErroPublicacao(f"TikTok: falha de rede no upload ({e.__class__.__name__})", "temporario") from e
                    time.sleep(5 * tentativa)
                    continue
                if r.status_code in (200, 201, 206):
                    break
                if r.status_code >= 500 and tentativa < 3:
                    time.sleep(5 * tentativa)
                    continue
                # 403 = upload_url expirou; 404/416 = sessão perdida: recomeça no próximo horário
                tipo = "temporario" if r.status_code in (403, 404, 416) or r.status_code >= 500 else "corte"
                raise ErroPublicacao(f"TikTok: upload da parte {i + 1}/{total} falhou (HTTP {r.status_code})", tipo)
        log.info("TikTok: vídeo enviado (%d parte(s)), aguardando processamento", total)

    def _acompanhar(self, publish_id: str, usuario: str | None, parar) -> Resultado:
        limite = time.monotonic() + 20 * 60
        status = ""
        while time.monotonic() < limite:
            esperar(parar, 6)  # limite da API: 30 consultas/min
            try:
                d = self._api("/v2/post/publish/status/fetch/", {"publish_id": publish_id})
            except ErroPublicacao as e:
                if e.tipo == "temporario":
                    continue
                raise
            status = d.get("status", "")
            if status == "FAILED":
                motivo = d.get("fail_reason") or "desconhecido"
                raise ErroPublicacao(f"TikTok recusou o vídeo: {motivo}", FALHAS.get(motivo, "corte"))
            if status == "SEND_TO_USER_INBOX":
                return Resultado(publish_id, None, "na caixa de entrada do TikTok: finalize a postagem no app")
            if status == "PUBLISH_COMPLETE":
                ids = d.get("publicaly_available_post_id") or d.get("publicly_available_post_id") or []
                if ids and usuario:
                    return Resultado(str(ids[0]), f"https://www.tiktok.com/@{usuario}/video/{ids[0]}")
                return Resultado(publish_id, None, "publicado (privado ou ainda em moderação)")
        return Resultado(publish_id, None, f"enviado; o TikTok ainda estava processando ({status or 'sem status'})")

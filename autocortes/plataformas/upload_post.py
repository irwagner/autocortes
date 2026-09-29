"""Envio pelo Upload-Post: serviço já auditado pelas redes, que publica em público (inclusive no TikTok).

Documentação: https://docs.upload-post.com. As contas das redes são conectadas no
site do Upload-Post, dentro de um "perfil"; aqui só vão a chave da API e o nome do perfil.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from pathlib import Path

import requests

from ..config import ROTULOS, Config
from ..textos import Conteudo, limitar, limitar_hashtags, tamanho_utf16
from ..util import log
from .base import ErroPublicacao, LeitorProgresso, Plataforma, Resultado, esperar, json_seguro

API = "https://api.upload-post.com"
PAINEL = "https://app.upload-post.com/manage-users"


class CorpoMultipart:
    """Corpo multipart/form-data montado em fluxo: o vídeo não é carregado inteiro na memória
    e o progresso do envio aparece no painel."""

    def __init__(self, campos: list[tuple[str, str]], campo_arquivo: str, caminho: Path, tipo: str = "video/mp4"):
        fronteira = uuid.uuid4().hex
        self.content_type = f"multipart/form-data; boundary={fronteira}"
        partes = []
        for nome, valor in campos:
            partes.append(
                f'--{fronteira}\r\nContent-Disposition: form-data; name="{nome}"\r\n\r\n'.encode()
                + str(valor).encode("utf-8") + b"\r\n"
            )
        nome_arquivo = re.sub(r'["\\\r\n]', "_", caminho.name)
        partes.append(
            f'--{fronteira}\r\nContent-Disposition: form-data; name="{campo_arquivo}"; '
            f'filename="{nome_arquivo}"\r\nContent-Type: {tipo}\r\n\r\n'.encode()
        )
        self._antes = b"".join(partes)
        self._depois = f"\r\n--{fronteira}--\r\n".encode()
        self._arquivo = LeitorProgresso(caminho)
        self._pos = 0
        self._fase = 0
        self._tamanho = len(self._antes) + len(self._arquivo) + len(self._depois)

    def __len__(self) -> int:
        return self._tamanho

    def read(self, n: int = -1) -> bytes:
        n = 1 << 20 if n is None or n < 0 else n
        saida = b""
        while len(saida) < n and self._fase < 3:
            falta = n - len(saida)
            if self._fase == 0:
                bloco = self._antes[self._pos:self._pos + falta]
                self._pos += len(bloco)
                if self._pos >= len(self._antes):
                    self._fase, self._pos = 1, 0
            elif self._fase == 1:
                bloco = self._arquivo.read(falta)
                if not bloco:
                    self._fase = 2
            else:
                bloco = self._depois[self._pos:self._pos + falta]
                self._pos += len(bloco)
                if self._pos >= len(self._depois):
                    self._fase = 3
            saida += bloco
        return saida

    def close(self) -> None:
        self._arquivo.close()


def _classificar_falha(texto: str) -> str:
    t = texto.lower()
    if any(k in t for k in ("reauth", "expired", "token", "not configured", "reconnect", "privacy", "banned")):
        return "bloqueio"
    if any(k in t for k in ("duration", "format", "resolution", "too long", "too short", "aspect", "codec")):
        return "corte"
    return "temporario"


def _url_publica(valor) -> str | None:
    return valor if isinstance(valor, str) and valor.startswith("https://") else None


class ViaUploadPost(Plataforma):
    """Uma rede (youtube, tiktok ou instagram) publicada pelo Upload-Post."""

    via = "upload_post"

    def __init__(self, nome: str, cfg: Config):
        self.nome = nome
        self.rotulo = f"{ROTULOS[nome]} (Upload-Post)"
        super().__init__(cfg)
        self.up = cfg["upload_post"]
        self.api = API

    # ------------------------------------------------------------ conta
    @property
    def arquivo_token(self) -> Path:
        return self.cfg.pasta_tokens / "upload_post.json"

    def conta(self) -> str | None:
        return (self.token().get("contas") or {}).get(self.nome)

    def lembrar_conta(self, descricao: str) -> None:
        dados = self.token()
        dados.setdefault("contas", {})[self.nome] = descricao
        self.salvar_token(dados)

    def desconectar(self) -> None:
        dados = self.token()
        (dados.get("contas") or {}).pop(self.nome, None)
        self.salvar_token(dados)

    def _chave(self) -> str:
        chave = str(self.up["api_key"]).strip()
        if not chave or not str(self.up["perfil"]).strip():
            raise ErroPublicacao("Preencha a chave da API e o perfil do Upload-Post", "bloqueio")
        return chave

    def _cabecalhos(self) -> dict:
        return {"Authorization": f"Apikey {self._chave()}"}

    def pronta(self) -> tuple[bool, str]:
        if not str(self.up["api_key"]).strip():
            return False, "falta a chave da API do Upload-Post"
        if not str(self.up["perfil"]).strip():
            return False, "falta o nome do perfil do Upload-Post"
        return True, "ok"

    def _erro_http(self, r: requests.Response, alvo: str | None = None) -> None:
        alvo = alvo or self.nome
        dados = json_seguro(r)
        msg = str(dados.get("message") or dados.get("error") or r.text[:200] or f"HTTP {r.status_code}")
        if r.status_code == 401:
            raise ErroPublicacao("Upload-Post: chave da API inválida ou expirada", "bloqueio")
        if r.status_code == 403:
            raise ErroPublicacao(f"Upload-Post recusou: {msg}", "bloqueio")  # ex.: TikTok fora do plano grátis
        if r.status_code == 404:
            raise ErroPublicacao(f"Upload-Post: perfil '{self.up['perfil']}' não encontrado", "bloqueio")
        if r.status_code == 429:
            raise ErroPublicacao(f"Upload-Post: limite do plano atingido ({msg})", "bloqueio")
        if r.status_code >= 500:
            raise ErroPublicacao(f"Upload-Post fora do ar ({r.status_code}): {msg}", "temporario")
        if "available_pages" in dados:  # mais de uma Página do Facebook no perfil
            raise ErroPublicacao(
                "Upload-Post: o perfil tem mais de uma Página do Facebook. Informe o ID da Página em "
                "Redes sociais > Instagram > Opções dos posts", "bloqueio",
            )
        if "invalid_platforms" in dados or "None of the requested platforms" in msg:
            raise ErroPublicacao(
                f"Upload-Post: a conta do {ROTULOS[alvo]} não está conectada no perfil "
                f"'{self.up['perfil']}'. Conecte em {PAINEL}", "bloqueio",
            )
        tipo = "bloqueio" if "privacy" in msg.lower() else "corte"
        raise ErroPublicacao(f"Upload-Post: {msg}", tipo)

    def autorizar(self) -> None:
        print(
            "\nPara postar pelo Upload-Post:\n"
            "  1. Crie a conta em https://www.upload-post.com e gere a chave da API\n"
            f"  2. Em {PAINEL}, crie um perfil e conecte nele o {ROTULOS[self.nome]}\n"
            "  3. Preencha [upload_post].api_key e perfil no config.toml\n"
            f"  4. Em [{self.nome}], use envio = \"upload_post\"\n"
        )
        print(f"{self.rotulo}: {self.verificar()}")

    def verificar(self) -> str:
        cab = self._cabecalhos()
        r = self.requisicao("GET", f"{self.api}/api/uploadposts/me", headers=cab)
        if r.status_code != 200:
            self._erro_http(r)
        plano = json_seguro(r).get("plan") or "?"
        perfil = str(self.up["perfil"]).strip()
        r = self.requisicao("GET", f"{self.api}/api/uploadposts/users/{requests.utils.quote(perfil, safe='')}", headers=cab)
        if r.status_code != 200:
            self._erro_http(r)
        contas = (json_seguro(r).get("profile") or {}).get("social_accounts") or {}
        conta = contas.get(self.nome)
        if not conta:
            raise ErroPublicacao(
                f"A conta do {ROTULOS[self.nome]} não está conectada no perfil '{perfil}' do Upload-Post. "
                f"Conecte em {PAINEL}", "bloqueio",
            )
        if isinstance(conta, dict) and conta.get("reauth_required"):
            raise ErroPublicacao(f"Reconecte o {ROTULOS[self.nome]} no painel do Upload-Post ({PAINEL})", "bloqueio")
        nome = (conta.get("handle") or conta.get("display_name") or conta.get("username")) if isinstance(conta, dict) else None
        texto = f"{'@' + str(nome).lstrip('@') if nome else 'conta conectada'} via Upload-Post (plano {plano})"
        self.lembrar_conta(texto)
        return texto

    # ------------------------------------------------------------ publicação
    def _campos(self, conteudo: Conteudo, request_id: str) -> list[tuple[str, str]]:
        c = self.conf
        legenda = conteudo.descricao or conteudo.titulo
        campos: list[tuple[str, str]] = [
            ("user", str(self.up["perfil"]).strip()),
            ("platform[]", self.nome),
            ("title", limitar(conteudo.titulo, 100)),
            ("async_upload", "true"),
            ("request_id", request_id),
        ]
        if self.nome == "youtube":
            from .youtube import metadados

            meta = metadados(c, conteudo)
            campos += [
                ("youtube_title", meta["snippet"]["title"]),
                ("youtube_description", meta["snippet"]["description"]),
                ("categoryId", meta["snippet"]["categoryId"]),
                ("privacyStatus", meta["status"]["privacyStatus"]),
                ("selfDeclaredMadeForKids", "false"),
                ("containsSyntheticMedia", "false"),
                ("embeddable", "true"),
            ]
            campos += [("tags[]", tag) for tag in meta["snippet"]["tags"]]
        elif self.nome == "tiktok":
            while tamanho_utf16(legenda) > 2200:
                legenda = limitar(legenda, len(legenda) - 20)
            campos += [
                ("tiktok_title", legenda),
                ("privacy_level", c["privacidade"]),
                ("post_mode", "MEDIA_UPLOAD" if c["modo"] == "rascunho" else "DIRECT_POST"),
                ("disable_comment", str(not c["permitir_comentarios"]).lower()),
                ("disable_duet", str(not c["permitir_duet"]).lower()),
                ("disable_stitch", str(not c["permitir_stitch"]).lower()),
                ("cover_timestamp", str(int(c["capa_ms"]))),
                ("brand_content_toggle", "false"),
                ("brand_organic_toggle", "false"),
            ]
        else:
            from .instagram import MAX_HASHTAGS

            campos += [
                ("instagram_title", limitar(limitar_hashtags(legenda, MAX_HASHTAGS), 2200)),
                ("media_type", "REELS"),
                ("thumb_offset", str(int(c["capa_ms"]))),
            ]
            if c["trial_reels"]:
                modo = ("TRIAL_REELS_SHARE_TO_FOLLOWERS_IF_LIKED" if c["trial_graduacao"] == "SS_PERFORMANCE"
                        else "TRIAL_REELS_DONT_SHARE_TO_FOLLOWERS")
                campos.append(("share_mode", modo))
            else:
                campos.append(("share_to_feed", "true" if c["compartilhar_no_feed"] else "false"))
        return campos

    def publicar(self, arquivo: Path, conteudo: Conteudo, parar: threading.Event | None = None) -> Resultado:
        request_id = f"autocortes-{uuid.uuid4().hex}"
        return self._enviar(self._campos(conteudo, request_id), arquivo, request_id, self.nome, parar)

    def publicar_facebook(self, arquivo: Path, conteudo: Conteudo, parar: threading.Event | None = None,
                          ao_avancar=None) -> Resultado:
        """O mesmo Reel na Página do Facebook conectada no perfil do Upload-Post."""
        request_id = f"autocortes-{uuid.uuid4().hex}"
        campos: list[tuple[str, str]] = [
            ("user", str(self.up["perfil"]).strip()),
            ("platform[]", "facebook"),
            ("title", limitar(conteudo.titulo, 100)),
            ("facebook_title", limitar(conteudo.titulo, 255)),
            ("facebook_description", limitar(conteudo.descricao or conteudo.titulo, 2200)),
            ("facebook_media_type", "REELS"),
            ("async_upload", "true"),
            ("request_id", request_id),
        ]
        pagina = str(self.cfg["instagram"].get("facebook_pagina_id") or "").strip()
        if pagina:
            campos.append(("facebook_page_id", pagina))
        return self._enviar(campos, arquivo, request_id, "facebook", parar)

    def _enviar(self, campos: list[tuple[str, str]], arquivo: Path, request_id: str, alvo: str, parar) -> Resultado:
        corpo = CorpoMultipart(campos, "video", arquivo)
        cabecalhos = {**self._cabecalhos(), "Content-Type": corpo.content_type, "Idempotency-Key": request_id}
        try:
            r = self.sessao.post(f"{self.api}/api/upload", data=corpo, headers=cabecalhos, timeout=(20, 900))
        except requests.RequestException as e:
            # o arquivo pode ter chegado mesmo sem resposta: consulta pelo request_id antes de desistir
            log.warning("%s: sem resposta do envio (%s); consultando o andamento", self.rotulo, e.__class__.__name__)
            return self._acompanhar(request_id, parar, alvo, tolerar_ausente=True)
        finally:
            corpo.close()
        if r.status_code not in (200, 201, 202):
            self._erro_http(r, alvo)
        dados = json_seguro(r)
        if dados.get("success") is False:
            self._erro_http(r, alvo)
        resultados = dados.get("results")
        if isinstance(resultados, dict) and alvo in resultados:  # respondeu na hora
            return self._resultado(resultados[alvo], request_id, alvo)
        return self._acompanhar(dados.get("request_id") or request_id, parar, alvo)

    def _acompanhar(self, request_id: str, parar, alvo: str, tolerar_ausente: bool = False) -> Resultado:
        limite = time.monotonic() + 30 * 60
        inicio = time.monotonic()
        while time.monotonic() < limite:
            esperar(parar, 8)
            try:
                r = self.sessao.get(f"{self.api}/api/uploadposts/status", params={"request_id": request_id},
                                    headers=self._cabecalhos(), timeout=(20, 60))
            except requests.RequestException:
                continue
            dados = json_seguro(r)
            status = str(dados.get("status") or "")
            if r.status_code == 404 or status == "not_found":
                if time.monotonic() - inicio < 120 or not tolerar_ausente:
                    continue
                raise ErroPublicacao("Upload-Post não recebeu o vídeo (sem resposta do envio)", "temporario")
            if r.status_code >= 400:
                self._erro_http(r, alvo)
            for item in dados.get("results") or []:
                if isinstance(item, dict) and item.get("platform") == alvo:
                    estado = str(item.get("status") or "")
                    if item.get("success") or estado == "completed":
                        return self._resultado(item, request_id, alvo)
                    if estado in ("failed", "skipped") or item.get("skipped") or item.get("success") is False:
                        if estado not in ("retryable", "queued", "processing"):
                            return self._resultado(item, request_id, alvo)
            if status == "failed":
                raise ErroPublicacao(f"Upload-Post: {dados.get('message') or 'o envio falhou'}", "temporario")
        return Resultado(request_id, None, "enviado ao Upload-Post; ainda estava processando")

    def _resultado(self, item: dict, request_id: str, alvo: str) -> Resultado:
        if item.get("skipped"):
            raise ErroPublicacao(
                f"A conta do {ROTULOS[alvo]} não está conectada no perfil do Upload-Post. Conecte em {PAINEL}",
                "bloqueio",
            )
        if not item.get("success") and str(item.get("status") or "") != "completed":
            motivo = str(item.get("error") or item.get("message") or "falha sem detalhes")
            raise ErroPublicacao(f"Upload-Post ({ROTULOS[alvo]}): {motivo}", _classificar_falha(motivo))
        url = _url_publica(item.get("url")) or _url_publica(item.get("post_url"))
        id_remoto = str(item.get("post_id") or item.get("video_id") or item.get("video_reel_id")
                        or item.get("media_id") or item.get("publish_id") or request_id)
        obs = None
        if item.get("fallback_to_inbox"):
            obs = "o TikTok atingiu o limite diário: o vídeo foi para a caixa de entrada do app"
        elif alvo == "tiktok" and self.conf["modo"] == "rascunho":
            obs = "enviado para os rascunhos do TikTok: finalize a postagem no app"
        return Resultado(id_remoto, url, obs)

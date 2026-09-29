"""YouTube Shorts pela YouTube Data API v3 (upload resumível)."""

from __future__ import annotations

import json
import secrets
import threading
import time
from fractions import Fraction
from pathlib import Path
from urllib.parse import urlencode

import requests

from ..midia import fracao
from ..textos import Conteudo, limitar
from ..util import log
from .base import ErroPublicacao, LeitorProgresso, Plataforma, Resultado, esperar, json_seguro
from .oauth import LoginOAuth, ServidorRetorno, pkce

ESCOPOS = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]
LIMITE_TEMPORARIO = {
    "quotaExceeded", "uploadLimitExceeded", "rateLimitExceeded", "userRateLimitExceeded", "dailyLimitExceeded",
}


def metadados(conf: dict, conteudo: Conteudo) -> dict:
    """snippet/status do vídeo (usado pela API oficial e pelo envio via Upload-Post)."""
    titulo = conteudo.titulo.replace("<", "").replace(">", "").strip() or "Corte"
    if conf["adicionar_shorts"] and "#shorts" not in (titulo + conteudo.descricao).lower():
        if len(titulo) + 8 <= 100:
            titulo += " #shorts"
    titulo = limitar(titulo, 100)
    descricao = _limitar_bytes(conteudo.descricao.replace("<", "").replace(">", ""), 4900)

    tags: list[str] = []
    total = 0
    for bruto in list(conf["tags"]) + [h.lstrip("#") for h in conteudo.hashtags]:
        tag = str(bruto).replace("<", "").replace(">", "").replace(",", " ").strip()
        if not tag or tag.lower() in {t.lower() for t in tags}:
            continue
        if total + len(tag) + 3 > 450:
            break
        tags.append(tag)
        total += len(tag) + 3
    return {
        "snippet": {
            "title": titulo,
            "description": descricao,
            "tags": tags,
            "categoryId": str(conf["categoria"]),
        },
        "status": {
            "privacyStatus": conf["privacidade"],
            "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": False,
            "embeddable": True,
        },
    }


def _limitar_bytes(texto: str, maximo: int) -> str:
    while len(texto.encode("utf-8")) > maximo:
        texto = texto[: max(0, len(texto) - 40)]
    return texto


def motivo_nao_short(sonda: dict, max_segundos: int) -> str | None:
    """Por que o vídeo (resposta do ffprobe) não seria um Short; None quando é.

    Short é vídeo vertical ou quadrado de até 3 min; o limite aqui é o de [youtube].max_segundos.
    """
    video = next(
        (s for s in sonda.get("streams") or []
         if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")),
        None,
    )
    if video is None:
        return "o arquivo não tem vídeo"
    largura, altura = int(video.get("width") or 0), int(video.get("height") or 0)
    if not largura or not altura:
        return None  # sem as dimensões não dá para conferir: deixa o YouTube decidir
    sar = fracao(video.get("sample_aspect_ratio"), Fraction(1)) or Fraction(1)
    giro = 0.0
    for dado in video.get("side_data_list") or []:
        if isinstance(dado, dict) and dado.get("rotation") is not None:
            giro = float(dado["rotation"])
    if not giro and (video.get("tags") or {}).get("rotate"):
        giro = float(video["tags"]["rotate"])
    exibida_l, exibida_a = largura * sar, Fraction(altura)
    if round(abs(giro)) % 180 == 90:
        exibida_l, exibida_a = exibida_a, exibida_l
    if exibida_l > exibida_a * Fraction(101, 100):
        return f"o vídeo é horizontal ({largura}x{altura})"
    try:
        duracao = float((sonda.get("format") or {}).get("duration") or video.get("duration") or 0)
    except (TypeError, ValueError):
        duracao = 0.0
    if duracao > max_segundos + 0.5:
        return f"tem {duracao:.0f} s e o limite do YouTube está em {max_segundos} s"
    return None


class YouTube(Plataforma):
    nome = "youtube"
    rotulo = "YouTube"
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth"
    token_url = "https://oauth2.googleapis.com/token"
    api_url = "https://www.googleapis.com/youtube/v3"
    upload_url = "https://www.googleapis.com/upload/youtube/v3/videos"

    def _credenciais(self) -> tuple[str, str]:
        cid, segredo = str(self.conf["client_id"]).strip(), str(self.conf["client_secret"]).strip()
        if not (cid and segredo):
            raise ErroPublicacao("Preencha o Client ID e o Client secret do projeto do Google", "bloqueio")
        return cid, segredo

    def pronta(self) -> tuple[bool, str]:
        if not (self.conf["client_id"] and self.conf["client_secret"]):
            return False, "falta preencher o Client ID e o Client secret"
        if not self.token().get("refresh_token"):
            return False, "falta conectar a conta"
        return True, "ok"

    # ------------------------------------------------------------ login
    def preparar_login(self) -> LoginOAuth:
        cid, _ = self._credenciais()
        servidor = ServidorRetorno(porta=int(self.conf.get("porta_redirect") or 0), caminho="/")
        verificador, desafio = pkce()
        estado = secrets.token_urlsafe(24)
        url = self.auth_url + "?" + urlencode(
            {
                "client_id": cid,
                "redirect_uri": servidor.redirect_uri,
                "response_type": "code",
                "scope": " ".join(ESCOPOS),
                "access_type": "offline",
                "prompt": "consent",
                "code_challenge": desafio,
                "code_challenge_method": "S256",
                "state": estado,
            }
        )
        return LoginOAuth(url, servidor, verificador, estado)

    def autorizar(self) -> None:
        login = self.preparar_login()
        p = login.servidor.aguardar(login.url, login.estado)
        print(f"YouTube conectado: {self.concluir_login(p['code'], login.servidor.redirect_uri, login.verificador)}")

    def concluir_login(self, codigo: str, redirect_uri: str, verificador: str) -> str:
        cid, segredo = self._credenciais()
        r = self.requisicao(
            "POST",
            self.token_url,
            data={
                "code": codigo,
                "client_id": cid,
                "client_secret": segredo,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": verificador,
            },
        )
        dados = json_seguro(r)
        if r.status_code != 200 or "access_token" not in dados:
            raise ErroPublicacao(
                f"Falha ao obter o token do YouTube: {dados.get('error_description') or dados.get('error') or r.status_code}",
                "bloqueio",
            )
        if not dados.get("refresh_token"):
            raise ErroPublicacao(
                "O Google não devolveu refresh_token. Remova o acesso do app em "
                "https://myaccount.google.com/permissions e rode o login de novo.",
                "bloqueio",
            )
        self.salvar_token(
            {
                "access_token": dados["access_token"],
                "refresh_token": dados["refresh_token"],
                "expira_em": time.time() + int(dados.get("expires_in") or 3600),
                "escopo": dados.get("scope"),
            }
        )
        conta = self.verificar()
        self.lembrar_conta(conta)
        return conta

    def _access_token(self) -> str:
        tok = self.token()
        if not tok.get("refresh_token"):
            raise ErroPublicacao("YouTube sem conta conectada", "bloqueio")
        if tok.get("access_token") and tok.get("expira_em", 0) - 120 > time.time():
            return tok["access_token"]
        cid, segredo = self._credenciais()
        r = self.requisicao(
            "POST",
            self.token_url,
            data={
                "client_id": cid,
                "client_secret": segredo,
                "refresh_token": tok["refresh_token"],
                "grant_type": "refresh_token",
            },
        )
        dados = json_seguro(r)
        if r.status_code != 200 or "access_token" not in dados:
            erro = dados.get("error")
            if erro in ("invalid_grant", "invalid_client", "unauthorized_client"):
                raise ErroPublicacao(
                    f"O login do YouTube expirou ou foi revogado ({erro}). Conecte a conta de novo",
                    "bloqueio",
                )
            raise ErroPublicacao(f"Falha ao renovar o token do YouTube: {erro or r.status_code}", "temporario")
        tok["access_token"] = dados["access_token"]
        tok["expira_em"] = time.time() + int(dados.get("expires_in") or 3600)
        if dados.get("refresh_token"):
            tok["refresh_token"] = dados["refresh_token"]
        self.salvar_token(tok)
        return tok["access_token"]

    def _cabecalhos(self) -> dict:
        return {"Authorization": f"Bearer {self._access_token()}"}

    def _checar(self, r: requests.Response) -> dict:
        dados = json_seguro(r)
        if r.status_code < 400:
            return dados
        erro = dados.get("error") if isinstance(dados.get("error"), dict) else {}
        motivos = {e.get("reason") for e in erro.get("errors", []) if isinstance(e, dict)}
        msg = f"YouTube {r.status_code}: {erro.get('message') or r.text[:200]}"
        if r.status_code == 401:
            raise ErroPublicacao(msg + " (conecte a conta de novo)", "bloqueio")
        if motivos & LIMITE_TEMPORARIO or r.status_code == 429 or r.status_code >= 500:
            raise ErroPublicacao(msg, "temporario")
        if r.status_code == 403:
            raise ErroPublicacao(msg, "bloqueio")
        raise ErroPublicacao(msg, "corte")

    def verificar(self) -> str:
        r = self.requisicao(
            "GET", f"{self.api_url}/channels", params={"part": "snippet", "mine": "true"}, headers=self._cabecalhos()
        )
        itens = self._checar(r).get("items") or []
        if not itens:
            return "login ok, mas a conta não tem canal do YouTube"
        return f"canal '{itens[0]['snippet']['title']}'"

    # ------------------------------------------------------------ publicação
    def _metadados(self, conteudo: Conteudo) -> dict:
        return metadados(self.conf, conteudo)

    def estatisticas(self, ids: list[str]) -> dict[str, dict]:
        """Visualizações, curtidas e comentários (1 unidade de cota a cada 50 vídeos)."""
        saida: dict[str, dict] = {}
        for i in range(0, len(ids), 50):
            lote = ids[i:i + 50]
            r = self.requisicao(
                "GET", f"{self.api_url}/videos", params={"part": "statistics", "id": ",".join(lote)},
                headers=self._cabecalhos(),
            )
            for item in self._checar(r).get("items") or []:
                est = item.get("statistics") or {}
                saida[item.get("id")] = {
                    "visualizacoes": int(est.get("viewCount") or 0),
                    "curtidas": int(est.get("likeCount") or 0),
                    "comentarios": int(est.get("commentCount") or 0),
                    "compartilhamentos": None,
                }
        return saida

    def publicar(self, arquivo: Path, conteudo: Conteudo, parar: threading.Event | None = None) -> Resultado:
        tamanho = arquivo.stat().st_size
        r = self.requisicao(
            "POST",
            self.upload_url,
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers={
                **self._cabecalhos(),
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Length": str(tamanho),
                "X-Upload-Content-Type": "video/mp4",
            },
            data=json.dumps(self._metadados(conteudo)).encode("utf-8"),
        )
        self._checar(r)
        sessao = r.headers.get("Location")
        if not sessao:
            raise ErroPublicacao("YouTube não devolveu o endereço de upload", "temporario")

        video = self._enviar(sessao, arquivo, tamanho, parar)
        video_id = video.get("id")
        if not video_id:
            raise ErroPublicacao("YouTube não devolveu o id do vídeo", "temporario")
        privacidade = (video.get("status") or {}).get("privacyStatus")
        obs = None
        if privacidade and privacidade != self.conf["privacidade"]:
            obs = f"enviado como {privacidade} (projetos sem auditoria da API ficam privados)"
        return Resultado(video_id, f"https://youtube.com/shorts/{video_id}", obs)

    def _enviar(self, sessao: str, arquivo: Path, tamanho: int, parar) -> dict:
        offset = 0
        for tentativa in range(1, 7):
            try:
                with LeitorProgresso(arquivo, inicio=offset) as f:
                    cabecalhos = {**self._cabecalhos(), "Content-Type": "video/mp4"}
                    if offset:
                        cabecalhos["Content-Range"] = f"bytes {offset}-{tamanho - 1}/{tamanho}"
                    r = self.sessao.put(sessao, data=f, headers=cabecalhos, timeout=(20, 900))
                if r.status_code in (200, 201):
                    return json_seguro(r)
                if r.status_code not in (308, 408, 429, 500, 502, 503, 504):
                    self._checar(r)
                    raise ErroPublicacao(f"YouTube recusou o upload (HTTP {r.status_code})", "temporario")
                log.warning("YouTube: upload respondeu HTTP %s; retomando", r.status_code)
            except requests.RequestException as e:
                log.warning("YouTube: upload interrompido (%s); retomando", e.__class__.__name__)
            esperar(parar, min(60, 5 * 2 ** (tentativa - 1)))
            try:  # pergunta ao YouTube quantos bytes já chegaram
                q = self.sessao.put(
                    sessao,
                    headers={**self._cabecalhos(), "Content-Range": f"bytes */{tamanho}", "Content-Length": "0"},
                    timeout=(20, 60),
                )
            except requests.RequestException:
                continue
            if q.status_code in (200, 201):
                return json_seguro(q)
            if q.status_code == 308:
                faixa = q.headers.get("Range")  # "bytes=0-12345"
                offset = int(faixa.rsplit("-", 1)[1]) + 1 if faixa else 0
            elif q.status_code == 404:
                raise ErroPublicacao("A sessão de upload do YouTube expirou", "temporario")
        raise ErroPublicacao("Upload do YouTube falhou depois de várias tentativas", "temporario")

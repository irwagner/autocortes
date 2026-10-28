"""Rotas JSON do painel e entrega de mídia (vídeos, miniaturas e prévia)."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Any, Callable

from .. import (__version__, agenda, analise, canais, canais_conta, config, criacao, edicao, ferramentas, gravador,
                ia, metricas, modelos_visuais, perfis, planejador, roteiro, voz)
from ..config import ENVIOS, PLATAFORMAS, ROTULOS, ErroConfig
from ..midia import ErroMidia, Interrompido, base_ffmpeg, executar
from ..navegador import ErroNavegador
from ..plataformas import ErroPublicacao, criar
from ..plataformas import manual as tarefas_manual
from ..plataformas.navegador import Gravacao
from ..produtor import EXTENSOES, TRAVA_ANALISE, TRAVA_EDICAO, Produtor, escrever_textos_ia, varrer_biblioteca
from ..publicador import MAX_TENTATIVAS_FACEBOOK, TRAVAS_REDE, Publicador, ler_bloqueios
from ..textos import fonte_dos_textos, ler_metadados, montar_conteudo, texto_topo, textos_ia, titulo_do_arquivo
from ..util import ATIVIDADES, LOG_MEMORIA, escrever_atomico, ler_json, log, salvar_json
from . import sistema
from .erros import ErroHttp

ABAS = ("revisao", "fila", "candidatos", "publicados", "descartados")
# mudanças que só valem depois de reiniciar o programa
REINICIAR = (("geral", "pasta_dados"), ("painel", "porta"))
LIMITE_UPLOAD = 60 * 1024**3
LIMITE_MOLDURA = 30 * 1024**2
TRAVA_PREVIA = threading.Lock()
TRAVA_MINIATURA = threading.Lock()


# ---------------------------------------------------------------- infraestrutura

@dataclass
class Contexto:
    painel: Any
    corpo: dict
    consulta: dict
    tratador: Any
    _conn: sqlite3.Connection | None = field(default=None, repr=False)

    @property
    def cfg(self):
        return self.painel.cfg

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = self.painel.conectar()
        return self._conn

    def fechar(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


ROTAS: list[tuple[str, re.Pattern, Callable, bool]] = []


def rota(metodo: str, padrao: str, bruto: bool = False):
    def registrar(funcao):
        ROTAS.append((metodo, re.compile(padrao), funcao, bruto))
        return funcao

    return registrar


def encontrar(metodo: str, caminho: str):
    for m, padrao, funcao, bruto in ROTAS:
        if m == metodo:
            achado = padrao.fullmatch(caminho)
            if achado:
                return funcao, achado.groups(), bruto
    return None


def _exigir(condicao: bool, mensagem: str, status: int = 400) -> None:
    if not condicao:
        raise ErroHttp(status, mensagem)


def _int(valor, padrao: int | None = None) -> int | None:
    try:
        return int(str(valor).strip())
    except (TypeError, ValueError):
        return padrao


def _marc(itens) -> str:
    return ",".join("?" * len(itens))


def _contagem(conn: sqlite3.Connection, tabela: str) -> dict:
    return dict(conn.execute(f"SELECT status, COUNT(*) FROM {tabela} GROUP BY status").fetchall())


def _contagem_filmes(conn: sqlite3.Connection, tipo: str = "filme") -> dict:
    """Situação dos filmes (ou das pautas). Os dois moram na mesma tabela: contar tudo junto
    mostrava as pautas como "filmes na fila de análise"."""
    return dict(conn.execute("SELECT status, COUNT(*) FROM filmes WHERE tipo=? GROUP BY status", (tipo,)).fetchall())


# O /estado é pedido a cada poucos segundos. O plano dos próximos dias e o resumo das redes (que
# recria as plataformas e decifra os logins) ficam guardados por alguns segundos; qualquer POST do
# painel joga fora o que estava guardado (`invalidar_cache`), então o que você muda aparece na hora.
CACHE_ESTADO_SEG = 4.0
_TRAVA_CACHE = threading.Lock()


def _em_cache(painel, chave: str, calcular: Callable[[], Any]):
    import copy

    with _TRAVA_CACHE:
        cache = getattr(painel, "cache_estado", None)
        if cache is None:
            cache = painel.cache_estado = {}
        versao = getattr(painel, "versao_cache", 0)
        item = cache.get(chave)
        if item is not None and item[1] == versao and time.monotonic() - item[0] < CACHE_ESTADO_SEG:
            return copy.deepcopy(item[2])
    valor = calcular()
    with _TRAVA_CACHE:
        if getattr(painel, "versao_cache", 0) == versao:  # um POST no meio da conta: não guarda o que ficou velho
            cache[chave] = (time.monotonic(), versao, copy.deepcopy(valor))
    return valor


def invalidar_cache(painel) -> None:
    """Chamado depois de cada POST da API (ver servidor._tratar)."""
    with _TRAVA_CACHE:
        painel.versao_cache = getattr(painel, "versao_cache", 0) + 1
        cache = getattr(painel, "cache_estado", None)
        if cache:
            cache.clear()


def _urls(corte_id: int, arquivo: str | None, versao: float | None = None) -> tuple[str | None, str | None]:
    """URLs de vídeo e miniatura (a miniatura é gerada na hora se faltar)."""
    if not arquivo or not Path(arquivo).exists():
        return None, None
    v = f"?v={int(versao or 0)}"
    return f"/media/corte/{corte_id}.mp4{v}", f"/media/corte/{corte_id}.jpg{v}"


def _dentro(caminho: Path, base: Path) -> bool:
    try:
        return caminho.resolve().is_relative_to(base.resolve())
    except OSError:
        return False


def _apagar_arquivos(arquivo: str | None) -> None:
    if not arquivo:
        return
    for extensao in (".mp4", ".ass", ".jpg"):
        try:
            Path(arquivo).with_suffix(extensao).unlink(missing_ok=True)
        except OSError as e:  # arquivo aberto (ex.: sendo enviado agora)
            log.warning("Não consegui apagar %s: %s", Path(arquivo).with_suffix(extensao).name, e)


def _corte(conn: sqlite3.Connection, corte_id) -> sqlite3.Row:
    linha = conn.execute(
        "SELECT c.*, f.titulo AS filme_titulo, f.ano AS filme_ano, f.caminho AS filme_caminho, f.tipo AS filme_tipo "
        "FROM cortes c JOIN filmes f ON f.id = c.filme_id WHERE c.id=?",
        (int(corte_id),),
    ).fetchone()
    _exigir(linha is not None, "Corte não encontrado", 404)
    return linha


def _filme(conn: sqlite3.Connection, filme_id) -> sqlite3.Row:
    linha = conn.execute("SELECT * FROM filmes WHERE id=?", (int(filme_id),)).fetchone()
    _exigir(linha is not None, "Filme não encontrado", 404)
    return linha


# tarefas do painel em andamento (postar agora, editar, criar, IA, download): o encerramento espera
# por elas, senão um envio era cortado no meio e ficava "enviando" até a próxima abertura
_TAREFAS: set[threading.Thread] = set()
_TRAVA_TAREFAS = threading.Lock()


def _em_segundo_plano(nome: str, trabalho: Callable[[], None]) -> None:
    def rodar():
        try:
            trabalho()
        except Interrompido:
            pass
        except Exception:
            log.exception("Erro em tarefa do painel (%s)", nome)
        finally:
            ATIVIDADES.limpar()
            with _TRAVA_TAREFAS:
                _TAREFAS.discard(threading.current_thread())

    thread = threading.Thread(target=rodar, name=nome, daemon=True)
    with _TRAVA_TAREFAS:
        _TAREFAS.add(thread)
    thread.start()


def tarefas_rodando() -> list[str]:
    with _TRAVA_TAREFAS:
        return sorted(t.name for t in _TAREFAS if t.is_alive())


def ocupado(painel) -> bool:
    """Este processo está no meio de algo que não pode ser cortado: editando, analisando, criando,
    postando, baixando dos canais, gravando um roteiro, conectando uma conta (de uma rede, ou a janela
    da conta separada dos canais) ou numa tarefa do painel. Vai no /saude, e a atualização automática
    espera todos os perfis ficarem livres."""
    if TRAVA_EDICAO.locked() or TRAVA_ANALISE.locked() or canais.TRAVA.locked():
        return True
    if any(trava.locked() for trava in TRAVAS_REDE.values()) or tarefas_rodando():
        return True
    if getattr(painel, "gravacoes", None):
        return True
    conta = getattr(painel, "conta_canais", None)
    if conta is not None and conta.ativa:
        return True
    return any(s.estado in ("aguardando", "escolher") for s in getattr(painel, "logins", {}).values())


def esperar_tarefas(prazo: float) -> list[str]:
    """Espera as tarefas do painel terminarem, até o prazo. Devolve o nome das que ficaram rodando."""
    limite = time.monotonic() + prazo
    while True:
        with _TRAVA_TAREFAS:
            vivas = [t for t in _TAREFAS if t.is_alive()]
        restante = limite - time.monotonic()
        if not vivas or restante <= 0:
            return sorted(t.name for t in vivas)
        vivas[0].join(min(restante, 1.0))


# ---------------------------------------------------------------- visão geral

def _facebook_ligado(cfg) -> bool:
    """O Reel do Instagram vai também para a Página do Facebook (pela API oficial ou pelo Upload-Post)."""
    ig = cfg["instagram"]
    # Instagram só de imagens: não há Reel para repetir na Página
    return bool(ig["ativo"] and ig.get("videos", True) and ig["pagina_facebook"]
                and ig["envio"] in ("oficial", "upload_post"))


def _bloqueios(ctx: Contexto) -> dict[str, dict]:
    """Redes pausadas: as do banco (valem com o motor desligado) e as sem configuração, do motor."""
    motor = ctx.painel.motor
    memoria = dict(motor.publicador.bloqueadas) if motor.publicador is not None and motor.rodando else {}
    return {**memoria, **ler_bloqueios(ctx.conn)}


def _resumo_redes(ctx: Contexto) -> dict:
    cfg = ctx.cfg
    bloqueadas = _bloqueios(ctx)
    agora = datetime.now()
    esperando = dict(ctx.conn.execute(
        "SELECT plataforma, COUNT(*) FROM postagens WHERE status='aguardando' GROUP BY plataforma").fetchall())
    a_conferir = dict(ctx.conn.execute(
        "SELECT plataforma, COUNT(*) FROM postagens WHERE status='conferir' GROUP BY plataforma").fetchall())
    saida = {}
    for rede in PLATAFORMAS:
        plataforma = criar(rede, cfg)
        pronta, motivo = plataforma.pronta()
        bloqueio = bloqueadas.get(rede)
        proximo = None
        videos = bool(cfg[rede].get("videos", True))
        if cfg[rede]["ativo"] and videos:
            # rede pausada: o próximo horário é o primeiro depois da pausa; parada até você
            # resolver (login, conta, configuração), não há próximo
            ate = bloqueio.get("ate") if bloqueio and not cfg.simulacao else 0
            if ate is not None:
                depois = max(agora, datetime.fromtimestamp(float(ate))) if ate else agora
                proximo = agenda.proximo_horario(cfg, rede, depois)
        saida[rede] = {
            "ativo": bool(cfg[rede]["ativo"]),
            "via": plataforma.via,
            "pronta": pronta,
            "motivo": motivo,
            "conta": plataforma.conta(),
            "bloqueada": bloqueio.get("motivo") if bloqueio else None,
            # tipo: bloqueio, cota, recusas, rede ou configuracao; ate None = até você testar a conta
            "bloqueio": {k: bloqueio.get(k) for k in ("tipo", "desde", "ate", "motivo")} if bloqueio else None,
            "proximo": proximo.timestamp() if proximo else None,
            # rede só de imagens: nenhum vídeo por semana (os horários de imagem são outra conta)
            "por_semana": agenda.posts_por_semana(cfg, rede) if videos else 0,
            "tarefas": esperando.get(rede, 0),
            "conferir": a_conferir.get(rede, 0),
        }
        if rede == "instagram":
            saida[rede]["facebook"] = {
                "ativo": _facebook_ligado(cfg),
                "pagina": plataforma.pagina() if hasattr(plataforma, "pagina") else None,
            }
        if cfg[rede]["envio"] == "navegador":
            texto, origem = gravador.carregar_com_origem(cfg, rede)
            saida[rede]["roteiro"] = {
                "tem": bool(texto),
                # "perfil" = o seu (dados/roteiros); "padrao" = o que vem com a instalação
                "origem": origem,
                "tem_padrao": gravador.arquivo_padrao(rede).is_file(),
                **(roteiro.resumo(texto, rede) if texto else {}),
            }
            saida[rede]["gravando"] = rede in ctx.painel.gravacoes
    return saida


def _tarefas(conn: sqlite3.Connection, limite: int = 30) -> list[dict]:
    """Postagens à mão esperando você, das mais antigas para as mais novas."""
    itens = []
    for p in conn.execute(
        "SELECT p.id, p.plataforma, p.horario, p.criado_em, p.manual, c.id AS corte_id, c.parte, c.arquivo, "
        "c.renderizado_em, f.titulo AS filme FROM postagens p JOIN cortes c ON c.id = p.corte_id "
        "JOIN filmes f ON f.id = c.filme_id WHERE p.status='aguardando' ORDER BY p.criado_em, p.id LIMIT ?",
        (limite,),
    ):
        video, miniatura = _urls(p["corte_id"], p["arquivo"], p["renderizado_em"])
        itens.append({
            "id": p["id"], "rede": p["plataforma"], "horario": p["horario"], "quando": p["criado_em"],
            "manual": bool(p["manual"]), "corte_id": p["corte_id"], "parte": p["parte"], "filme": p["filme"],
            "video": video, "miniatura": miniatura,
        })
    return itens


def _a_conferir(conn: sqlite3.Connection, limite: int = 30) -> list[dict]:
    """Posts que a rede recebeu sem confirmar: você diz se saíram (nada é repetido sozinho)."""
    itens = []
    for p in conn.execute(
        "SELECT p.id, p.plataforma, p.url, p.erro, p.criado_em, p.manual, c.id AS corte_id, c.parte, c.arquivo, "
        "c.renderizado_em, f.titulo AS filme FROM postagens p JOIN cortes c ON c.id = p.corte_id "
        "JOIN filmes f ON f.id = c.filme_id WHERE p.status='conferir' ORDER BY p.criado_em, p.id LIMIT ?",
        (limite,),
    ):
        video, miniatura = _urls(p["corte_id"], p["arquivo"], p["renderizado_em"])
        itens.append({
            "id": p["id"], "rede": p["plataforma"], "url": p["url"], "erro": p["erro"], "quando": p["criado_em"],
            "manual": bool(p["manual"]), "corte_id": p["corte_id"], "parte": p["parte"], "filme": p["filme"],
            "video": video, "miniatura": miniatura,
        })
    return itens


def _anexar_midia(conn: sqlite3.Connection, itens: list[dict]) -> None:
    ids = sorted({i["corte_id"] for i in itens if i.get("corte_id")})
    if not ids:
        return
    linhas = {
        r["id"]: r
        for r in conn.execute(f"SELECT id, arquivo, renderizado_em FROM cortes WHERE id IN ({_marc(ids)})", ids)
    }
    for item in itens:
        linha = linhas.get(item.get("corte_id"))
        if linha is not None:
            item["video"], item["miniatura"] = _urls(linha["id"], linha["arquivo"], linha["renderizado_em"])


def _ultimas_postagens(conn: sqlite3.Connection, limite: int) -> list[dict]:
    itens = []
    for p in conn.execute(
        "SELECT p.id, p.plataforma, p.status, p.url, p.erro, p.criado_em, p.manual, c.id AS corte_id, c.parte, "
        "c.arquivo, c.renderizado_em, f.titulo AS filme FROM postagens p JOIN cortes c ON c.id = p.corte_id "
        "JOIN filmes f ON f.id = c.filme_id ORDER BY p.id DESC LIMIT ?",
        (limite,),
    ):
        video, miniatura = _urls(p["corte_id"], p["arquivo"], p["renderizado_em"])
        itens.append({
            "id": p["id"], "rede": p["plataforma"], "estado": p["status"], "url": p["url"], "erro": p["erro"],
            "quando": p["criado_em"], "manual": bool(p["manual"]), "corte_id": p["corte_id"], "parte": p["parte"],
            "filme": p["filme"], "video": video, "miniatura": miniatura,
        })
    return itens


def _alertas(ctx: Contexto, filmes: dict, cortes: dict, cobertura: dict, redes: dict, pautas: dict | None = None,
             n_conferir: int | None = None) -> list[dict]:
    cfg, motor = ctx.cfg, ctx.painel.motor
    alertas: list[dict] = []
    pautas = pautas or {}

    def add(nivel: str, texto: str, destino: str | None = None) -> None:
        alertas.append({"nivel": nivel, "texto": texto, "destino": destino})

    total_filmes = sum(v for k, v in filmes.items() if k != "ausente")
    criando = bool(cfg["criacao"]["ativo"])
    # com a criação ligada, pautas e temas também viram vídeo: sem filme nenhum ainda há o que postar
    tem_o_que_criar = criando and (any(v for k, v in pautas.items() if k != "ausente") or bool(criacao.temas(cfg)))
    if not motor.rodando:
        add("aviso", "O motor está desligado: nada será editado nem postado.", "motor")
    if cfg.simulacao:
        add("info", "Modo simulação ligado: os vídeos são gerados, mas nada é publicado.", "redes")
    if cfg["agenda"]["pausado"]:
        add("aviso", "As postagens estão pausadas.", "agenda")
    if total_filmes == 0 and not tem_o_que_criar:
        add("aviso", "Nenhum filme na pasta nem tema na Criação. Adicione filmes ou temas para começar."
            if criando else "Nenhum filme na pasta. Adicione filmes para começar.", "filmes")
    if filmes.get("erro"):
        add("erro", f"{filmes['erro']} filme(s) com erro na análise.", "filmes")
    if pautas.get("erro"):
        add("erro", f"{pautas['erro']} pauta(s) não viraram vídeo por causa de um erro.", "criacao")
    if cortes.get("revisao"):
        add("info", f"{cortes['revisao']} corte(s) aguardando sua aprovação.", "cortes?aba=revisao")
    ativas = [r for r, v in redes.items() if v["ativo"]]
    if not ativas:
        add("erro", "Nenhuma rede social ativa.", "redes")
    for rede in ativas:
        v = redes[rede]
        if v["bloqueada"] and not cfg.simulacao:
            b = v["bloqueio"] or {}
            quando = " até você testar a conta em Redes sociais" if b.get("ate") is None and b.get("tipo") != "configuracao" \
                else (f" até {datetime.fromtimestamp(b['ate']).strftime('%d/%m %H:%M')}" if b.get("ate") else "")
            add("erro", f"{ROTULOS[rede]} pausado{quando}: {v['bloqueada']}", "redes")
        elif not v["pronta"]:
            add("info" if cfg.simulacao else "erro", f"{ROTULOS[rede]}: {v['motivo']}.", "redes")
    conferir = n_conferir if n_conferir is not None else sum(v.get("conferir", 0) for v in redes.values())
    if conferir:
        add("aviso", f"{conferir} post(s) a conferir: a rede recebeu o post, mas eu não vi a confirmação. "
            "Diga se saiu no Início.", "inicio")
    if "tiktok" in ativas and cfg["tiktok"]["privacidade"] == "SELF_ONLY":
        if redes["tiktok"]["via"] == "upload_post":
            add("aviso", "O TikTok posta pelo Upload-Post, mas a privacidade está em \"Só eu\". "
                "Troque para \"Todos\" para publicar em público.", "redes")
        elif redes["tiktok"]["via"] == "oficial":
            add("info", "TikTok pela API oficial: os posts ficam visíveis só para você. Para postar em "
                "público, use o envio pelo Upload-Post em Redes sociais.", "redes")
    pelo_navegador = [r for r in ativas if redes[r]["via"] == "navegador"]
    if pelo_navegador and cfg["navegador"]["ensaio"] and not cfg.simulacao:
        add("info", "Ensaio ligado: o envio pelo navegador preenche tudo e para antes de publicar.", "redes")
    if "youtube" in ativas:
        limite = int(cfg["youtube"]["max_segundos"])
        if planejador.sem_cortes_possiveis(cfg, "youtube"):
            add("erro", f"Nenhum corte cabe no YouTube: a duração mínima dos cortes ({cfg['cortes']['duracao_min']} s) "
                f"passa do limite dos Shorts ({limite} s). Ajuste em Configurações > Cortes ou em Redes sociais.",
                "redes")
        elif float(cfg["cortes"]["duracao_max"]) > limite + 0.5:
            add("info", f"Cortes com mais de {limite} s ficam fora do YouTube, que recebe só Shorts.", "redes")
    if _facebook_ligado(cfg):
        ultima = ctx.conn.execute(
            "SELECT corte_id, status, erro, criado_em FROM postagens WHERE plataforma='facebook' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if ultima is not None and ultima["status"] in ("pulado", "falhou") and time.time() - ultima["criado_em"] < 3 * 86400:
            tentativas = ctx.conn.execute(
                "SELECT COUNT(*) FROM postagens WHERE plataforma='facebook' AND corte_id=? "
                "AND status IN ('falhou', 'interrompido')", (ultima["corte_id"],),
            ).fetchone()[0]
            repete = ultima["status"] == "falhou" and tentativas < MAX_TENTATIVAS_FACEBOOK
            texto = "ainda não foi para a Página do Facebook (vai tentar de novo)" if repete else \
                "não foi para a Página do Facebook"
            add("aviso", f"O último Reel {texto}: {ultima['erro'] or 'erro sem detalhes'}", "redes")
    dias = cobertura.get("dias")
    if dias is not None and dias < 3 and (total_filmes or tem_o_que_criar):
        if dias < 1:
            quanto = "menos de 1 dia"
        else:
            numero = f"{dias:.1f}".rstrip("0").rstrip(".").replace(".", ",")  # 1,7 em vez de 1.7
            quanto = "só 1 dia" if numero == "1" else f"só {numero} dias"
        if criando:
            add("aviso", f"Conteúdo para {quanto} de postagem. Adicione mais filmes ou temas na Criação.", "criacao")
        else:
            add("aviso", f"Conteúdo para {quanto} de postagem. Adicione mais filmes.", "filmes")
    if cfg["transcricao"]["fonte"] != "nenhuma":
        runtime = ferramentas.runtime_whisper(cfg)
        falha = _falha_runtime(ctx)
        if not runtime["pronto"]:
            add("erro", f"Falta o Microsoft Visual C++ x64 ({', '.join(runtime['faltando'])}): sem ele o Whisper e o "
                "detector de voz não abrem, e os filmes novos esperam na fila de análise. Veja como instalar em "
                "Configurações > Transcrição.", "config?aba=transcricao")
        elif falha:
            add("erro", f"O Windows não abriu o Whisper ou o detector de voz (código 0x{falha['codigo']:08X}): os "
                "filmes novos esperam e eu tento de novo mais tarde. Veja o que fazer em Configurações > Transcrição.",
                "config?aba=transcricao")
        elif not ferramentas.caminho_modelo(cfg).exists():
            # o download espera o Visual C++: o aviso só aparece quando ele já está no lugar
            add("info", f"O modelo de transcrição '{cfg['transcricao']['modelo']}' será baixado na primeira análise.",
                "config?aba=transcricao")
    problema = edicao.problema_moldura(cfg)
    if problema:
        add("erro", f"{problema}: os cortes estão saindo sem ela. Escolha outra no Estúdio.", "estudio")
    conta_canais = canais_conta.publico(cfg)
    if conta_canais["situacao"] == "caiu":  # mesmo com o "Baixar sozinho" desligado: o Olhar agora usa a conta
        add("aviso", f"A conta separada dos canais do YouTube precisa entrar de novo: "
            f"{conta_canais['motivo'] or 'os cookies dela não servem mais'}. "
            "Entre de novo em Filmes > Canais do YouTube.", "filmes?aba=canais")
    if cfg["canais"]["ativo"]:
        pausa_canais = canais.pausa(ctx.conn, cfg)
        if pausa_canais:
            ate = datetime.fromtimestamp(float(pausa_canais["ate"])).strftime("%d/%m %H:%M")
            texto = f"Os downloads dos canais do YouTube estão pausados até {ate}: {pausa_canais['motivo']}."
            if pausa_canais["tipo"] == "robo" and conta_canais["situacao"] == "nenhuma":
                texto += " Para não depender disso, conecte uma conta separada em Filmes > Canais do YouTube (opcional)."
            add("aviso", texto, "filmes?aba=canais")
    return alertas


@rota("GET", r"/estado")
def estado(ctx: Contexto):
    cfg, conn, motor = ctx.cfg, ctx.conn, ctx.painel.motor
    sucesso = planejador.status_sucesso(cfg)
    inicio_dia = datetime.combine(date.today(), dtime()).timestamp()
    filmes = _contagem_filmes(conn, "filme")
    pautas = _contagem_filmes(conn, "pauta")
    cortes = _contagem(conn, "cortes")
    hoje = dict(conn.execute(
        f"SELECT plataforma, COUNT(*) FROM postagens WHERE status IN ({_marc(sucesso)}) AND criado_em >= ? "
        "GROUP BY plataforma",
        (*sucesso, inicio_dia),
    ).fetchall())
    plano = _em_cache(ctx.painel, "plano", lambda: planejador.montar_plano(cfg, conn, dias=3))
    proximos = [i for i in plano["itens"] if i["tipo"] == "horario" and i["estado"] != "perdido"][:6]
    _anexar_midia(conn, proximos)
    redes = _em_cache(ctx.painel, "redes", lambda: _resumo_redes(ctx))
    cobertura = plano["cobertura"]
    na_fila = max((v["na_fila"] for v in cobertura["por_rede"].values()), default=cortes.get("pronto", 0))
    tarefas = _tarefas(conn)
    # inclui o Reel da Página do Facebook, que não é uma das redes da agenda
    n_conferir = conn.execute("SELECT COUNT(*) FROM postagens WHERE status='conferir'").fetchone()[0]
    return {
        "versao": __version__,
        "agora": time.time(),
        # fechando para atualizar: a janela avisa e se recarrega sozinha quando a versão nova abrir
        "atualizando": bool(getattr(ctx.painel, "atualizando", False)),
        "motor": {"rodando": motor.rodando, "parando": motor.parando, "desde": motor.iniciado_em},
        "simulacao": cfg.simulacao,
        "pausado": bool(cfg["agenda"]["pausado"]),
        "aprovacao": bool(cfg["geral"]["exigir_aprovacao"]),
        "numeros": {
            "filmes": sum(v for k, v in filmes.items() if k != "ausente"),
            "filmes_na_fila": filmes.get("novo", 0) + filmes.get("analisando", 0),
            "filmes_erro": filmes.get("erro", 0),
            # vídeos criados do zero: pautas esperando virar vídeo (ou virando agora) e com erro
            "pautas_na_fila": pautas.get("novo", 0) + pautas.get("criando", 0),
            "pautas_erro": pautas.get("erro", 0),
            "na_fila": na_fila,
            "revisao": cortes.get("revisao", 0),
            "candidatos": cortes.get("candidato", 0) + cortes.get("renderizando", 0),
            "hoje": sum(hoje.values()),
            "hoje_por_rede": hoje,
            "tarefas": sum(v["tarefas"] for v in redes.values()),
            "conferir": n_conferir,
        },
        "cobertura": cobertura,
        "proximos": proximos,
        "tarefas": tarefas,
        "conferir": _a_conferir(conn),
        "ultimas": _ultimas_postagens(conn, 8),
        "atividades": sorted(ATIVIDADES.listar(), key=lambda a: a["desde"]),
        "redes": redes,
        "desempenho": metricas.resumo(conn),
        "ia": ia.disponivel(cfg),
        "alertas": _alertas(ctx, filmes, cortes, cobertura, redes, pautas, n_conferir),
    }


@rota("POST", r"/motor")
def motor(ctx: Contexto):
    acao = ctx.corpo.get("acao")
    m = ctx.painel.motor
    if acao == "iniciar":
        m.iniciar()
    elif acao == "parar":
        threading.Thread(target=m.parar, name="parar-motor", daemon=True).start()
        time.sleep(0.2)
    else:
        raise ErroHttp(400, "Ação inválida")
    return {"rodando": m.rodando, "parando": m.parando}


# ---------------------------------------------------------------- agenda

@rota("GET", r"/plano")
def plano(ctx: Contexto):
    dias = max(1, min(21, _int(ctx.consulta.get("dias"), 7)))
    resultado = planejador.montar_plano(ctx.cfg, ctx.conn, dias=dias)
    _anexar_midia(ctx.conn, resultado["itens"])
    return resultado


@rota("POST", r"/agenda/pausa")
def pausar_agenda(ctx: Contexto):
    pausado = bool(ctx.corpo.get("pausado"))
    _salvar(ctx, {"agenda": {"pausado": pausado}})
    log.info("Postagens %s pelo painel", "pausadas" if pausado else "retomadas")
    return {"pausado": pausado}


# ---------------------------------------------------------------- configuração

def _falha_runtime(ctx: Contexto) -> dict | None:
    """O Windows recusou abrir o Whisper ou o detector de voz (código de saída) e o motor espera."""
    motor = ctx.painel.motor
    produtor = motor.produtor if motor.rodando else None
    falha = produtor.falha_runtime() if produtor is not None else None
    # a falha sem código é a falta das DLLs, que o runtime_whisper já mostra sozinho
    return falha if falha and falha["codigo"] is not None else None


def _meta_config(ctx: Contexto) -> dict:
    cfg = ctx.cfg
    modelos = {
        nome: {"descricao": descricao, "baixado": ferramentas.caminho_modelo(cfg, nome).exists()}
        for nome, descricao in ferramentas.MODELOS.items()
    }
    atual = str(cfg["transcricao"]["modelo"])
    if atual not in modelos:
        modelos[atual] = {"descricao": "modelo personalizado", "baixado": ferramentas.caminho_modelo(cfg).exists()}
    cli, vad = ferramentas.caminhos_whisper(cfg)
    return {
        "versao": __version__,
        "caminho": str(cfg.caminho),
        "pastas": {"filmes": str(cfg.pasta_filmes), "dados": str(cfg.pasta_dados), "cortes": str(cfg.pasta_cortes)},
        "modelos": modelos,
        "whisper_instalado": cli.exists() and vad.exists(),
        # Visual C++ x64 do Whisper: as DLLs que faltam e o "não abriu" do motor (falha)
        "whisper_runtime": {**ferramentas.runtime_whisper(cfg), "falha": _falha_runtime(ctx)},
        "fontes": sistema.fontes_disponiveis(cfg),
        "inicializacao": sistema.inicializacao_ativa(),
        # a entrada da inicialização é de outra pasta (instalação movida): ligar de novo corrige
        "inicializacao_outra": sistema.inicializacao_de_outra_pasta(),
        # placa de vídeo e o tamanho de modelo que cabe nela (dica na aba IA)
        "ia_hardware": sistema.info_ia(),
        "presets": agenda.PRESETS,
        "envios": ENVIOS,
        "max_tarefas": tarefas_manual.MAX_TAREFAS_POR_REDE,
        "tiktok_redirect": f"http://127.0.0.1:{int(cfg['tiktok']['porta_redirect'])}/callback/",
        # opções já salvas que só valem depois de fechar e abrir o AutoCortes
        "reiniciar": sorted(ctx.painel.reinicio),
    }


def _salvar(ctx: Contexto, alteracoes: dict) -> list[str]:
    painel = ctx.painel
    with painel.trava_config:
        antes = {(s, k): painel.cfg[s][k] for s, k in REINICIAR}
        try:
            novo = config.salvar(painel.cfg, alteracoes)
        except ErroConfig as e:
            raise ErroHttp(400, "Algumas configurações são inválidas", {"erros": e.erros}) from e
        reiniciar = []
        for s, k in REINICIAR:  # continuam valendo os valores atuais até reiniciar
            chave = f"{s}.{k}"
            if isinstance(alteracoes.get(s), dict) and k in alteracoes[s]:
                if novo[s][k] != antes[(s, k)]:
                    if painel.reinicio.get(chave) != novo[s][k]:
                        reiniciar.append(chave)
                    painel.reinicio[chave] = novo[s][k]
                else:
                    painel.reinicio.pop(chave, None)
            novo[s][k] = antes[(s, k)]
        painel.motor.aplicar_config(novo)
        for rede in PLATAFORMAS:
            if rede in alteracoes:
                painel.motor.rede_atualizada(rede)
        if isinstance(alteracoes.get("edicao"), dict):
            try:
                modelos_visuais.sincronizar(painel.cfg)  # o modelo em uso guarda o visual novo
            except modelos_visuais.ErroModelo as e:  # o config já foi salvo; o arquivo dos modelos fica para depois
                log.warning("O modelo em uso não foi atualizado em modelos_visuais.json: %s", e)
    return reiniciar


def _config_painel(ctx: Contexto) -> dict:
    """Config para o painel, já com os valores salvos que só valem depois de reiniciar."""
    dados = ctx.cfg.para_painel()
    for chave, valor in ctx.painel.reinicio.items():
        secao, nome = chave.split(".", 1)
        dados[secao][nome] = valor
    return dados


@rota("GET", r"/config")
def ler_config(ctx: Contexto):
    return {"config": _config_painel(ctx), "meta": _meta_config(ctx)}


@rota("POST", r"/config")
def salvar_config(ctx: Contexto):
    alteracoes = ctx.corpo.get("alteracoes")
    _exigir(isinstance(alteracoes, dict) and bool(alteracoes), "Nada para salvar")
    with ctx.painel.trava_config:
        visual = alteracoes.get("edicao") if isinstance(alteracoes.get("edicao"), dict) else {}
        ativo = modelos_visuais.nome_ativo(ctx.cfg)
        if "modelo" in visual and str(visual["modelo"]).strip() != ativo:
            # só o nome mudaria, e o visual guardado com esse nome seria trocado pelo atual
            raise ErroHttp(400, "Para trocar de modelo visual, use o Estúdio")
        esperado = ctx.corpo.get("modelo_esperado")
        if esperado and str(esperado) != ativo and any(k in modelos_visuais.CHAVES for k in visual):
            raise ErroHttp(409, f"O modelo em uso mudou para {ativo} em outra janela do painel. Recarregue a "
                                "página para ver o visual dele antes de salvar.")
        reiniciar = _salvar(ctx, alteracoes)
    log.info("Configurações salvas pelo painel")
    return {"config": _config_painel(ctx), "meta": _meta_config(ctx), "reiniciar": reiniciar}


def _filme_de_exemplo(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """O filme analisado mais recente (usado na prévia e no Estúdio)."""
    return conn.execute(
        "SELECT id, caminho, titulo, ano, duracao FROM filmes WHERE tipo='filme' AND status='analisado' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()


def _quadro_exemplo(ctx: Contexto) -> Path | None:
    return _quadro_do_filme(ctx.cfg, ctx.conn)


def _quadro_do_filme(cfg, conn: sqlite3.Connection) -> Path | None:
    """Um quadro de um filme de verdade (sem as tarjas) para a prévia do estilo."""
    filme = _filme_de_exemplo(conn)
    if filme is None or not Path(filme["caminho"]).exists():
        return None
    info = ler_json(analise.pasta_do_filme(cfg, filme["id"]) / "info.json")
    if not info:
        return None
    w, h, x, y = info["crop"]
    # o recorte entra no nome: depois de uma reanálise com outro recorte, sai um quadro novo
    destino = cfg.pasta_previa / f"quadro_{filme['id']}_{w}x{h}_{x}_{y}.png"
    with edicao.TRAVA_QUADROS:  # a geometria, a prévia e a mídia podem pedir o mesmo quadro ao mesmo tempo
        if destino.exists():
            return destino
        momento = float(info.get("duracao") or filme["duracao"] or 60) * 0.35
        destino.parent.mkdir(parents=True, exist_ok=True)
        temporario = destino.with_name(destino.stem + ".parcial.png")
        r = executar(
            base_ffmpeg(cfg["ferramentas"]["ffmpeg"])
            + ["-loglevel", "error", "-ss", f"{momento:.2f}", "-i", filme["caminho"],
               "-map", f"0:{info['video']['indice']}", "-frames:v", "1",
               "-vf", f"crop={w}:{h}:{x}:{y},scale=iw*sar:ih,setsar=1,scale='min(1920,iw)':-2",
               "-update", "1", str(temporario)],
            baixa_prioridade=False,
            timeout=90,
        )
        if r.codigo != 0 or not temporario.exists():
            temporario.unlink(missing_ok=True)
            return None
        os.replace(temporario, destino)
        for antigo in cfg.pasta_previa.glob(f"quadro_{filme['id']}*.png"):
            if antigo != destino and not antigo.name.endswith(".parcial.png"):
                antigo.unlink(missing_ok=True)  # quadros de um recorte antigo do mesmo filme
    return destino


@rota("POST", r"/previa")
def previa(ctx: Contexto):
    alteracoes = ctx.corpo.get("alteracoes")
    try:
        temporario = config.simular(ctx.cfg, alteracoes if isinstance(alteracoes, dict) else {})
    except ErroConfig as e:
        raise ErroHttp(400, "Configuração inválida para a prévia", {"erros": e.erros}) from e
    with TRAVA_PREVIA:
        quadro = _quadro_exemplo(ctx)
        filme = _filme_de_exemplo(ctx.conn)
        topo = texto_topo(temporario, filme, 1) if filme is not None else None  # o mesmo título do Estúdio
        try:
            edicao.previa_estilo(temporario, ctx.cfg.pasta_previa / "previa.jpg", quadro, topo)
        except ErroMidia as e:
            raise ErroHttp(500, str(e)) from e
    return {"url": f"/media/previa.jpg?v={int(time.time() * 1000)}", "quadro_real": quadro is not None}


# ---------------------------------------------------------------- estúdio: modelos visuais e molduras

def _relativo(cfg, caminho: Path) -> str:
    """Caminho como fica no config: relativo à pasta do AutoCortes quando dá."""
    try:
        return caminho.resolve().relative_to(cfg.raiz.resolve()).as_posix()
    except ValueError:
        return caminho.resolve().as_posix()


def _url_moldura(cfg, caminho: Path) -> str:
    versao = int(caminho.stat().st_mtime)
    if _dentro(caminho, cfg.pasta_molduras) and caminho.parent.resolve() == cfg.pasta_molduras.resolve():
        from urllib.parse import quote

        return f"/media/moldura/{quote(caminho.name)}?v={versao}"
    return f"/media/moldura-atual?v={versao}"


def _moldura_json(cfg, caminho: Path) -> dict:
    dados = {"arquivo": _relativo(cfg, caminho), "nome": caminho.stem, "existe": caminho.is_file()}
    if not dados["existe"]:
        return dados
    try:
        info = edicao.info_moldura(cfg, caminho)
    except (ErroMidia, OSError) as e:
        return {**dados, "erro": str(e)}
    janela = info["janela"]
    return {**dados, "url": _url_moldura(cfg, caminho), "largura": info["largura"], "altura": info["altura"],
            "janela": {"x": janela.x, "y": janela.y, "w": janela.w, "h": janela.h} if janela else None}


def _molduras(cfg) -> list[dict]:
    pasta = cfg.pasta_molduras
    arquivos = sorted(pasta.glob("*.png"), key=lambda p: p.name.lower()) if pasta.is_dir() else []
    saida = [_moldura_json(cfg, p) for p in arquivos]
    atual = str(cfg["edicao"]["moldura"]).strip()
    if atual and not any(Path(p).resolve() == cfg.caminho_de(atual).resolve() for p in arquivos):
        saida.append({**_moldura_json(cfg, cfg.caminho_de(atual)), "arquivo": atual, "fora_da_pasta": True})
    return saida


def _modelos_json(cfg) -> dict:
    modelos = modelos_visuais.listar(cfg)
    ativo = modelos_visuais.nome_ativo(cfg)
    return {
        "ativo": ativo,
        "modelos": [{"nome": n, "resumo": modelos_visuais.resumo(v), "ativo": n == ativo} for n, v in modelos.items()],
    }


def _estudio_json(ctx: Contexto) -> dict:
    cfg = ctx.cfg
    try:
        modelos = _modelos_json(cfg)
    except modelos_visuais.ErroModelo as e:
        raise ErroHttp(e.status, str(e)) from e
    return {**modelos, "molduras": _molduras(cfg), "pasta_molduras": str(cfg.pasta_molduras),
            "chaves": list(modelos_visuais.CHAVES), "config": _config_painel(ctx), "meta": _meta_config(ctx)}


@rota("GET", r"/estudio")
def estudio(ctx: Contexto):
    with ctx.painel.trava_config:  # a lista de modelos acompanha o config salvo
        return _estudio_json(ctx)


@rota("POST", r"/estudio/geometria")
def estudio_geometria(ctx: Contexto):
    """Onde ficam o filme, a moldura, o título e a legenda com as alterações ainda não salvas."""
    alteracoes = ctx.corpo.get("alteracoes")
    try:
        temporario = config.simular(ctx.cfg, alteracoes if isinstance(alteracoes, dict) else {})
    except ErroConfig as e:
        raise ErroHttp(400, "Configuração inválida", {"erros": e.erros}) from e
    cfg = ctx.cfg
    filme = _filme_de_exemplo(ctx.conn)
    topo = texto_topo(temporario, filme, 1) if filme is not None else edicao.topo_de_exemplo(temporario)
    try:
        quadro = _quadro_exemplo(ctx) or edicao.quadro_exemplo(cfg, cfg.pasta_previa / "previa.jpg")
        info = edicao.info_do_quadro(temporario, quadro)
        g, arquivo_moldura, moldura = edicao.geometria_do_corte(temporario, info, topo)
    except (ErroMidia, OSError) as e:
        raise ErroHttp(500, f"Não consegui montar a tela do Estúdio: {e}") from e
    e = temporario["edicao"]
    largura, altura = int(e["largura"]), int(e["altura"])
    texto_max = int(largura * edicao.LARGURA_LEGENDA)
    return {
        "largura": largura, "altura": altura,
        "geometria": edicao.geometria_json(g),
        "textos": edicao.amostra_textos(temporario, g, topo),
        "quadro": {"url": f"/media/quadro.png?v={int(quadro.stat().st_mtime)}", "largura": info["crop"][0],
                   "altura": info["crop"][1], "real": filme is not None and quadro.name != "quadro_exemplo.png"},
        "moldura": ({**_moldura_json(temporario, arquivo_moldura)} if arquivo_moldura is not None else None),
        # moldura escolhida que não entra no vídeo (não existe, não abre ou não tem área transparente)
        "moldura_problema": edicao.problema_moldura(temporario) if arquivo_moldura is None else None,
        "seguro": {"topo": int(altura * edicao.SEGURO_TOPO), "base": int(altura * edicao.SEGURO_BASE),
                   "esquerda": (largura - texto_max) // 2, "direita": (largura + texto_max) // 2},
        "filme": filme["titulo"] if filme is not None else None,
    }


def _mudar_modelo(ctx: Contexto, nome: str) -> None:
    valores = modelos_visuais.valores(ctx.cfg, nome)
    _salvar(ctx, {"edicao": {**valores, "modelo": nome}})


# As rotas dos modelos seguram a trava do config do começo ao fim: o arquivo dos modelos e o
# config.toml mudam juntos, sem outra requisição no meio.

@rota("POST", r"/modelos/usar")
def usar_modelo(ctx: Contexto):
    nome = str(ctx.corpo.get("nome") or "")
    with ctx.painel.trava_config:
        try:
            _mudar_modelo(ctx, nome)
        except modelos_visuais.ErroModelo as e:
            raise ErroHttp(e.status, str(e)) from e
        log.info("Modelo visual em uso: %s (vale para os próximos cortes editados)", nome)
        return _estudio_json(ctx)


@rota("POST", r"/modelos/criar")
def criar_modelo(ctx: Contexto):
    base = ctx.corpo.get("base") or None
    with ctx.painel.trava_config:
        try:
            nome = modelos_visuais.validar_nome(ctx.corpo.get("nome"))
            modelos_visuais.criar(ctx.cfg, nome, str(base) if base else None)
        except modelos_visuais.ErroModelo as e:
            raise ErroHttp(e.status, str(e)) from e
        try:
            _mudar_modelo(ctx, nome)
        except BaseException as e:
            try:  # não deixa um modelo criado pela metade
                modelos_visuais.excluir(ctx.cfg, nome)
            except modelos_visuais.ErroModelo:
                pass
            if isinstance(e, modelos_visuais.ErroModelo):
                raise ErroHttp(e.status, str(e)) from e
            raise
        log.info("Modelo visual criado: %s", nome)
        return _estudio_json(ctx)


@rota("POST", r"/modelos/renomear")
def renomear_modelo(ctx: Contexto):
    antigo = str(ctx.corpo.get("nome") or "")
    with ctx.painel.trava_config:
        era_ativo = antigo == modelos_visuais.nome_ativo(ctx.cfg)
        try:
            novo = modelos_visuais.renomear(ctx.cfg, antigo, ctx.corpo.get("novo"))
        except modelos_visuais.ErroModelo as e:
            raise ErroHttp(e.status, str(e)) from e
        if era_ativo:
            try:
                _salvar(ctx, {"edicao": {"modelo": novo}})
            except BaseException:
                modelos_visuais.listar(ctx.cfg)  # o config ficou com o nome antigo: o arquivo volta junto
                raise
        ctx.conn.execute("UPDATE cortes SET modelo=? WHERE modelo=?", (novo, antigo))  # os cortes acompanham
        log.info("Modelo visual renomeado: %s -> %s", antigo, novo)
        return _estudio_json(ctx)


@rota("POST", r"/modelos/excluir")
def excluir_modelo(ctx: Contexto):
    nome = str(ctx.corpo.get("nome") or "")
    with ctx.painel.trava_config:
        try:
            modelos_visuais.excluir(ctx.cfg, nome)
        except modelos_visuais.ErroModelo as e:
            raise ErroHttp(e.status, str(e)) from e
        log.info("Modelo visual excluído: %s", nome)
        return _estudio_json(ctx)


@rota("POST", r"/molduras/enviar", bruto=True)
def enviar_moldura(ctx: Contexto):
    tratador, cfg = ctx.tratador, ctx.cfg
    tratador.close_connection = True
    from urllib.parse import unquote

    tamanho = _int(tratador.headers.get("Content-Length"), -1)
    _exigir(tamanho is not None and tamanho > 0, "Arquivo vazio ou sem tamanho informado", 411)
    # grande demais: o servidor lê e descarta o corpo antes de responder (até 256 MB)
    _exigir(tamanho <= LIMITE_MOLDURA, "Imagem grande demais (máximo 30 MB)", 413)
    # lê tudo antes de validar: assim a resposta de erro chega ao navegador em vez de derrubar a conexão
    tratador._corpo_lido = True
    corpo = tratador.rfile.read(tamanho)
    _exigir(len(corpo) == tamanho, "O envio foi interrompido")
    tratador.close_connection = False
    try:
        # "%" some do nome: o FFmpeg lê "moldura%d.png" como uma sequência de imagens
        nome = _nome_seguro(unquote(tratador.headers.get("X-Nome-Arquivo", "")).replace("%", "_"))
    except ErroHttp:
        raise ErroHttp(400, "Nome de arquivo inválido") from None
    _exigir(Path(nome).suffix.lower() == ".png", "A moldura precisa ser uma imagem .png com a área do vídeo transparente")
    _exigir(corpo[:8] == b"\x89PNG\r\n\x1a\n" and corpo[12:16] == b"IHDR", "O arquivo não é um PNG de verdade")
    largura, altura = int.from_bytes(corpo[16:20], "big"), int.from_bytes(corpo[20:24], "big")
    # tamanho declarado no cabeçalho: um PNG pequeno pode dizer 20000 x 20000 e ocupar gigabytes ao abrir
    _exigir(100 <= largura <= 8192 and 100 <= altura <= 8192,
            f"A imagem tem {largura} x {altura} pixels; use de 100 a 8192 de cada lado (o ideal é 1080 x 1920)")

    pasta = cfg.pasta_molduras
    pasta.mkdir(parents=True, exist_ok=True)
    temporario = pasta / f".envio-{secrets.token_hex(6)}.parcial"
    temporario.write_bytes(corpo)
    destino = None
    try:
        base, extensao = os.path.splitext(nome)
        for n in range(1, 1000):
            candidato = pasta / (nome if n == 1 else f"{base} ({n}){extensao}")
            try:
                os.rename(temporario, candidato)  # no Windows falha se já existir: nunca troca outra moldura
            except FileExistsError:
                continue
            destino = candidato
            break
    finally:
        if destino is None:
            temporario.unlink(missing_ok=True)
    _exigir(destino is not None, "Já existem arquivos demais com esse nome", 409)
    dados = _moldura_json(cfg, destino)
    if dados.get("erro") or not dados.get("janela"):
        destino.unlink(missing_ok=True)
        if dados.get("erro"):
            raise ErroHttp(400, f"Não consegui ler a imagem: {dados['erro']}")
        raise ErroHttp(400, "Esta imagem não tem área transparente para o vídeo. Deixe transparente (sem cor, "
                            "com canal alfa) a parte onde o filme deve aparecer e envie de novo.")
    log.info("Moldura recebida pelo painel: %s", destino.name)
    with ctx.painel.trava_config:
        return {"moldura": dados, **_estudio_json(ctx)}


def _cfg_ia(ctx: Contexto) -> config.Config:
    """Config com os campos de IA que ainda não foram salvos (para testar antes de salvar)."""
    alteracoes = ctx.corpo.get("alteracoes")
    try:
        return config.simular(ctx.cfg, alteracoes if isinstance(alteracoes, dict) else {})
    except ErroConfig as e:
        raise ErroHttp(400, "Configuração da IA inválida", {"erros": e.erros}) from e


@rota("POST", r"/ia/testar")
def testar_ia(ctx: Contexto):
    try:
        return {"mensagem": ia.testar(_cfg_ia(ctx))}
    except ia.ErroIA as e:
        raise ErroHttp(400, f"IA: {e}") from e


@rota("POST", r"/ia/modelos")
def modelos_ia(ctx: Contexto):
    try:
        return {"modelos": ia.listar_modelos(_cfg_ia(ctx))}
    except ia.ErroIA as e:
        raise ErroHttp(400, f"IA: {e}") from e


@rota("POST", r"/ferramentas/baixar")
def baixar_ferramentas(ctx: Contexto):
    cfg = ctx.cfg
    modelo = str(ctx.corpo.get("modelo") or cfg["transcricao"]["modelo"])
    _exigir(bool(re.fullmatch(r"[A-Za-z0-9._-]+", modelo)), "Modelo inválido")
    _exigir(not any(t.name == "baixar" and t.is_alive() for t in threading.enumerate()), "Já existe um download em andamento", 409)
    runtime = ferramentas.runtime_whisper(cfg)  # sem o Visual C++, o whisper.cpp não abriria: nem baixa
    _exigir(runtime["pronto"], runtime["mensagem"], 409)

    def trabalho():
        try:
            ferramentas.garantir_whisper(cfg)
            ferramentas.garantir_vad(cfg)
            ferramentas.garantir_modelo(cfg, modelo)
            log.info("Transcrição pronta: whisper.cpp e modelo '%s' disponíveis", modelo)
        except ErroMidia as e:
            log.error("Download falhou: %s", e)

    _em_segundo_plano("baixar", trabalho)
    return {"ok": True}


# ---------------------------------------------------------------- cortes

def _postagens_por_corte(conn: sqlite3.Connection, feitos: set[str]) -> tuple[dict, dict]:
    """A última postagem do vídeo de cada corte por rede (as de imagem não contam) e as redes já feitas."""
    ultima: dict[int, dict[str, sqlite3.Row]] = {}
    feitas: dict[int, set[str]] = {}
    for p in conn.execute(
        "SELECT id, corte_id, plataforma, status, url, erro, criado_em, via, visualizacoes, curtidas, comentarios "
        "FROM postagens WHERE midia='video' ORDER BY id"
    ):
        ultima.setdefault(p["corte_id"], {})[p["plataforma"]] = p
        if p["status"] in feitos:
            feitas.setdefault(p["corte_id"], set()).add(p["plataforma"])
    return ultima, feitas


def _aba(c: sqlite3.Row, ativas: list[str], feitas: set[str]) -> str:
    status = c["status"]
    if status == "revisao":
        return "revisao"
    if status in ("candidato", "renderizando"):
        return "candidatos"
    if status in ("descartado", "erro"):
        return "descartados"
    if status == "concluido":
        return "publicados"
    return "publicados" if ativas and all(r in feitas for r in ativas) else "fila"


def _corte_json(c: sqlite3.Row, ultima: dict) -> dict:
    video, miniatura = _urls(c["id"], c["arquivo"], c["renderizado_em"])
    try:
        detalhes = json.loads(c["detalhes"] or "{}")
    except ValueError:
        detalhes = {}
    chaves = set(c.keys())
    return {
        "id": c["id"], "filme_id": c["filme_id"], "filme": c["filme_titulo"], "parte": c["parte"],
        # vídeo criado do zero: o card não mostra trecho nem nota, que só fazem sentido para corte de filme
        "criado": bool(detalhes.get("criado")) or (c["filme_tipo"] == "pauta" if "filme_tipo" in chaves else False),
        "inicio": c["inicio"], "fim": c["fim"], "duracao": round(c["fim"] - c["inicio"], 1),
        "pontuacao": round(c["pontuacao"], 2), "detalhes": detalhes, "frase": c["frase"], "status": c["status"],
        "prioridade": c["prioridade"], "erro": c["erro"], "renderizado_em": c["renderizado_em"],
        "video": video, "miniatura": miniatura,
        "postagens": {
            rede: {"id": p["id"], "estado": p["status"], "url": p["url"], "quando": p["criado_em"], "erro": p["erro"],
                   "via": p["via"], "visualizacoes": p["visualizacoes"], "curtidas": p["curtidas"],
                   "comentarios": p["comentarios"]}
            for rede, p in ultima.items()
        },
        "visualizacoes": sum(p["visualizacoes"] or 0 for p in ultima.values()),
        "textos_ia": bool(textos_ia(c)),
        "modelo": c["modelo"] if "modelo" in c.keys() else None,
    }


@rota("GET", r"/cortes")
def listar_cortes(ctx: Contexto):
    cfg, conn = ctx.cfg, ctx.conn
    aba = ctx.consulta.get("aba", "fila")
    _exigir(aba in ABAS, "Aba inválida")
    filme = _int(ctx.consulta.get("filme"))
    limite = max(1, min(400, _int(ctx.consulta.get("limite"), 150)))
    # rede só de imagens não recebe vídeo: não pode segurar o corte em "Na fila"
    ativas = cfg.redes_de_video()
    # a tarefa à mão ainda não foi postada, e o post a conferir pode não ter saído: o corte continua
    # em "Na fila" até você marcar
    ultima, feitas = _postagens_por_corte(conn, set(planejador.status_concluidos(cfg)) - {"aguardando", "conferir"})

    sql = ("SELECT c.*, f.titulo AS filme_titulo, f.ano AS filme_ano, f.tipo AS filme_tipo "
           "FROM cortes c JOIN filmes f ON f.id = c.filme_id"
           + (" WHERE c.filme_id = ?" if filme else ""))
    contagem = {a: 0 for a in ABAS}
    itens = []
    for c in conn.execute(sql, (filme,) if filme else ()):
        qual = _aba(c, ativas, feitas.get(c["id"], set()))
        contagem[qual] += 1
        if qual == aba:
            itens.append(c)

    def ultima_postagem(c) -> float:
        return max((p["criado_em"] for p in ultima.get(c["id"], {}).values()), default=0)

    ordem = {
        "revisao": lambda c: (c["fila_em"] or 0, c["id"]),
        "fila": lambda c: (-c["prioridade"], c["fila_em"] or 0, c["id"]),
        "candidatos": lambda c: (-c["prioridade"], -c["pontuacao"]),
        "publicados": lambda c: -ultima_postagem(c),
        "descartados": lambda c: -c["id"],
    }[aba]
    itens.sort(key=ordem)
    filmes = [dict(r) for r in conn.execute(
        "SELECT id, titulo FROM filmes WHERE id IN (SELECT DISTINCT filme_id FROM cortes) ORDER BY titulo")]
    return {
        "aba": aba,
        "contagem": contagem,
        "itens": [_corte_json(c, ultima.get(c["id"], {})) for c in itens[:limite]],
        "total": len(itens),
        "filmes": filmes,
        "ativas": ativas,
        "facebook": _facebook_ligado(cfg),
        # a tela só oferece a seleção em lote quando a API tem a rota dela: com o AutoCortes antigo ainda
        # aberto, a página nova (lida do disco) mandaria o "Editar de novo" para o refazer-fila de antes,
        # que não conhece "ids" e refaria a fila inteira
        "lote": True,
    }


def _corte_detalhe(ctx: Contexto, corte_id) -> dict:
    cfg, conn = ctx.cfg, ctx.conn
    c = _corte(conn, corte_id)
    ultima, _ = _postagens_por_corte(conn, set(planejador.status_concluidos(cfg)))
    dados = _corte_json(c, ultima.get(c["id"], {}))
    dados["criado"] = c["filme_tipo"] == "pauta"
    filme = {"caminho": c["filme_caminho"], "titulo": c["filme_titulo"], "ano": c["filme_ano"]}
    base = dict(c)
    if not base.get("parte"):  # candidato: mostra o número que a parte deve receber
        base["parte"] = (conn.execute("SELECT MAX(parte) FROM cortes WHERE filme_id=?", (c["filme_id"],)).fetchone()[0] or 0) + 1
    efetivo = montar_conteudo(cfg, filme, base)
    # sem o texto manual: o que sai se o campo ficar vazio (texto da IA, se houver, ou o modelo)
    padrao = montar_conteudo(cfg, filme, {**base, "titulo_custom": None, "descricao_custom": None})
    ia_textos = textos_ia(c)
    dados["textos"] = {
        "titulo": efetivo.titulo, "descricao": efetivo.descricao, "hashtags": efetivo.hashtags,
        "titulo_padrao": padrao.titulo, "descricao_padrao": padrao.descricao,
        "titulo_custom": c["titulo_custom"] or "", "descricao_custom": c["descricao_custom"] or "",
        "parte_prevista": base["parte"],
        "fonte": fonte_dos_textos(c),
        "ia": ia_textos or None,
        "ia_disponivel": ia.disponivel(cfg),
    }
    dados["historico"] = [
        {"rede": p["plataforma"], "estado": p["status"], "url": p["url"], "erro": p["erro"],
         "quando": p["criado_em"], "manual": bool(p["manual"])}
        for p in conn.execute("SELECT * FROM postagens WHERE corte_id=? ORDER BY id DESC", (c["id"],))
    ]
    dados["ativas"] = cfg.plataformas_ativas()
    dados["envios"] = {rede: cfg[rede]["envio"] for rede in PLATAFORMAS}
    dados["facebook"] = _facebook_ligado(cfg)
    dados["youtube_max"] = int(cfg["youtube"]["max_segundos"])
    return dados


@rota("GET", r"/cortes/(\d+)")
def detalhe_corte(ctx: Contexto, corte_id):
    return _corte_detalhe(ctx, corte_id)


def _gerar_textos_em_segundo_plano(painel, corte_id: int) -> None:
    def trabalho():
        conn = painel.conectar()
        try:
            ATIVIDADES.definir(f"IA escrevendo os textos do corte {corte_id}")
            escrever_textos_ia(painel.cfg, conn, corte_id)
        except ia.ErroIA as e:
            log.warning("IA não escreveu os textos do corte %s: %s", corte_id, e)
        finally:
            conn.close()

    _em_segundo_plano(f"ia-{corte_id}", trabalho)


def _editar_em_segundo_plano(painel, corte_id: int) -> None:
    def trabalho():
        conn = painel.conectar()
        try:
            corte = conn.execute("SELECT * FROM cortes WHERE id=?", (corte_id,)).fetchone()
            if corte is not None:
                Produtor(painel.cfg, painel.encerrar).renderizar(conn, corte)
        finally:
            conn.close()

    _em_segundo_plano(f"edicao-{corte_id}", trabalho)


def _resolver_tarefas(cfg, conn: sqlite3.Connection, corte_id: int, pular: str | None = None) -> None:
    """Apaga as cópias das tarefas à mão do corte na pasta sincronizada (e pula as tarefas, se pedido)."""
    for t in conn.execute("SELECT id, plataforma FROM postagens WHERE corte_id=? AND status='aguardando'",
                          (corte_id,)).fetchall():
        tarefas_manual.apagar_copias(cfg, t["plataforma"], t["id"])
        if pular:
            conn.execute("UPDATE postagens SET status='pulado', erro=?, atualizado_em=? WHERE id=?",
                         (pular, time.time(), t["id"]))


def _resolver_tarefas_do_corte(ctx: Contexto, corte_id: int, pular: str | None = None) -> None:
    _resolver_tarefas(ctx.cfg, ctx.conn, corte_id, pular)


def _aplicar_acao(ctx: Contexto, c: sqlite3.Row, acao, registrar: bool = True) -> None:
    """Aplica uma ação a um corte. É a regra única do botão do corte e da seleção em lote (/cortes/lote).

    registrar=False cala a linha de cada corte no registro: o lote escreve uma linha só, com o resumo.
    Nenhuma ação apaga a linha do corte (o histórico das postagens iria junto, e o trecho voltaria a
    ser escolhido numa nova análise).
    """
    conn = ctx.conn
    status = c["status"]
    criado = c["filme_tipo"] == "pauta"  # vídeo criado do zero: quem regera é a pauta, não o editor de cortes
    if criado and acao in ("reeditar", "editar_agora", "restaurar"):
        raise ErroHttp(400, "Este vídeo foi criado de uma pauta: use \"Criar de novo\" para gerar outro")
    if acao in ("descartar", "reeditar", "recriar", "apagar_video"):
        # o arquivo está subindo para uma rede agora: apagar no meio não desfaz o post (as imagens
        # ficam em dados/imagens e não dependem do .mp4)
        if conn.execute("SELECT 1 FROM postagens WHERE corte_id=? AND midia='video' AND status='enviando' LIMIT 1",
                        (c["id"],)).fetchone():
            raise ErroHttp(409, "Este vídeo está sendo enviado agora. Espere o envio terminar.", {"motivo": "enviando"})
    if acao == "recriar":
        _exigir(criado, "Só vídeo criado de uma pauta pode ser gerado de novo")
        _exigir(status in ("revisao", "pronto", "erro", "descartado"),
                "Este vídeo não pode ser criado de novo agora (ele já saiu ou está sendo postado)")
        _apagar_arquivos(c["arquivo"])
        _resolver_tarefas_do_corte(ctx, c["id"], pular="vídeo vai ser criado de novo")
        conn.execute("UPDATE cortes SET status='descartado', arquivo=NULL WHERE id=?", (c["id"],))
        conn.execute(
            "UPDATE filmes SET status='novo', esgotado=0, erro=NULL, tentativas=0, atualizado_em=? "
            "WHERE id=? AND status != 'criando'",
            (time.time(), c["filme_id"]),
        )
        if registrar:
            log.info("Pauta '%s' vai gerar outro vídeo (o corte %s foi descartado)", c["filme_titulo"], c["id"])
        return
    if acao == "aprovar":
        _exigir(status == "revisao", "Só cortes aguardando aprovação podem ser aprovados")
        conn.execute("UPDATE cortes SET status='pronto' WHERE id=?", (c["id"],))
        if registrar:
            log.info("Corte %s aprovado pelo painel", c["id"])
    elif acao == "descartar":
        _exigir(status in ("candidato", "revisao", "pronto", "erro"), "Este corte não pode ser descartado agora")
        _apagar_arquivos(c["arquivo"])
        conn.execute("UPDATE cortes SET status='descartado', arquivo=NULL WHERE id=?", (c["id"],))
        _resolver_tarefas_do_corte(ctx, c["id"], pular="corte descartado")
        if registrar:
            log.info("Corte %s descartado pelo painel", c["id"])
    elif acao == "restaurar":
        _exigir(status in ("descartado", "erro"), "Este corte não está descartado")
        # volta para o fim da fila quando for editado de novo
        conn.execute("UPDATE cortes SET status='candidato', arquivo=NULL, erro=NULL, tentativas=0, fila_em=NULL "
                     "WHERE id=?", (c["id"],))
    elif acao in ("reeditar", "editar_agora"):
        if acao == "reeditar":
            _exigir(status in ("revisao", "pronto", "erro", "descartado"), "Este corte não pode ser reeditado agora")
            _apagar_arquivos(c["arquivo"])
            _resolver_tarefas_do_corte(ctx, c["id"])  # a cópia na pasta sincronizada ficaria com o vídeo antigo
            # um corte que estava na fila continua no mesmo lugar depois de editado de novo
            lugar = "" if status in ("revisao", "pronto") else ", fila_em=NULL"
            conn.execute(f"UPDATE cortes SET status='candidato', arquivo=NULL, erro=NULL, tentativas=0{lugar} "
                         "WHERE id=?", (c["id"],))
        else:
            _exigir(status == "candidato", "Este corte já foi editado")
        _editar_em_segundo_plano(ctx.painel, c["id"])
    elif acao == "priorizar":
        maximo = conn.execute("SELECT COALESCE(MAX(prioridade), 0) FROM cortes").fetchone()[0]
        conn.execute("UPDATE cortes SET prioridade=? WHERE id=?", (maximo + 1, c["id"]))
    elif acao == "normal":
        conn.execute("UPDATE cortes SET prioridade=0 WHERE id=?", (c["id"],))
    elif acao == "gerar_textos":
        _exigir(ia.disponivel(ctx.cfg), "Ligue a IA em Configurações > IA para textos")
        _exigir(not any(t.name == f"ia-{c['id']}" and t.is_alive() for t in threading.enumerate()),
                "A IA já está escrevendo os textos deste corte", 409)
        _gerar_textos_em_segundo_plano(ctx.painel, c["id"])
    elif acao == "limpar_textos_ia":
        # marca em vez de apagar: o motor não escreve de novo sozinho (o botão "Gerar com IA" ainda escreve)
        conn.execute("UPDATE cortes SET ia_textos=? WHERE id=?", (json.dumps({"descartado": True}), c["id"]))
    elif acao == "apagar_video":
        # libera o disco de um corte publicado: o corte, o caminho do arquivo e o histórico ficam, como no
        # apagar_apos_postar (o caminho guardado deixa apagar depois um vídeo que ficou para trás)
        _exigir(status == "concluido", "Só o vídeo de um corte publicado pode ser apagado")
        video = Path(c["arquivo"]) if c["arquivo"] else None
        _exigir(video is not None and video.exists(), "O vídeo deste corte já foi apagado")
        # a mesma regra da rota de mídia: um banco copiado de outro lugar não apaga nada fora deste perfil
        _exigir(_dentro(video, ctx.cfg.pasta_dados), "O vídeo fica fora da pasta de dados deste perfil: não apago")
        _apagar_arquivos(c["arquivo"])
        if video.exists():
            raise ErroHttp(409, "O vídeo está aberto em outro programa. Feche e tente de novo.",
                           {"motivo": "arquivo_aberto"})
        if registrar:
            log.info("Vídeo do corte %s apagado pelo painel (o corte e o histórico ficam)", c["id"])
    else:
        raise ErroHttp(400, "Ação desconhecida")


@rota("POST", r"/cortes/(\d+)/acao")
def acao_corte(ctx: Contexto, corte_id):
    c = _corte(ctx.conn, corte_id)
    _aplicar_acao(ctx, c, ctx.corpo.get("acao"))
    return _corte_detalhe(ctx, c["id"])


@rota("POST", r"/cortes/aprovar-todos")
def aprovar_todos(ctx: Contexto):
    n = ctx.conn.execute("UPDATE cortes SET status='pronto' WHERE status='revisao'").rowcount
    log.info("%d corte(s) aprovados pelo painel", n)
    return {"aprovados": n}


# o que a seleção da tela Cortes aplica de uma vez (o "Editar de novo" vai pelo /cortes/refazer-fila)
ACOES_LOTE = ("descartar", "restaurar", "aprovar", "priorizar", "normal", "apagar_video")
MAX_LOTE = 500
ROTULO_LOTE = {
    "descartar": "descartados", "restaurar": "restaurados", "aprovar": "aprovados",
    "priorizar": "com prioridade", "normal": "sem prioridade", "apagar_video": "com o vídeo apagado",
}


def _ids_do_pedido(valor) -> list[int]:
    """Os cortes escolhidos na tela: de 1 a MAX_LOTE números de corte, sem repetição."""
    _exigir(isinstance(valor, list) and 1 <= len(valor) <= MAX_LOTE, f'Mande de 1 a {MAX_LOTE} cortes em "ids"')
    # true, 3.0 e "3" não são números de corte; fora do intervalo o SQLite nem aceita o número (viraria erro 500)
    _exigir(all(type(i) is int and 0 < i < 2**63 for i in valor), 'Cada item de "ids" é o número de um corte')
    _exigir(len(set(valor)) == len(valor), 'Há cortes repetidos em "ids"')
    return list(valor)


@rota("POST", r"/cortes/lote")
def acao_em_lote(ctx: Contexto):
    """A mesma ação em vários cortes de uma vez (a seleção da tela Cortes).

    Cada corte passa pela regra do botão dele (_aplicar_acao) e fica confirmado na hora: o que um
    corte não deixa (está sendo enviado, a situação não permite, não existe) vai em "falhas" e não
    impede os outros.
    """
    acao = ctx.corpo.get("acao")
    _exigir(acao in ACOES_LOTE, "Ação desconhecida")
    ids = _ids_do_pedido(ctx.corpo.get("ids"))
    conn = ctx.conn
    ordem = ids
    if acao == "priorizar":
        # cada um ganha a maior prioridade + 1: indo do último da fila para o primeiro, os escolhidos
        # passam na frente de todos sem trocar a ordem que já tinham entre eles
        linhas = {r["id"]: r for r in conn.execute(
            f"SELECT id, prioridade, fila_em FROM cortes WHERE id IN ({_marc(ids)})", ids)}

        def lugar(i: int) -> tuple:
            r = linhas.get(i)
            return (-r["prioridade"], r["fila_em"] or 0, i) if r is not None else (0, 0, i)

        ordem = sorted(ids, key=lugar, reverse=True)
    feitos: set[int] = set()
    falhas: dict[int, dict] = {}
    for corte_id in ordem:
        try:
            _aplicar_acao(ctx, _corte(conn, corte_id), acao, registrar=False)
        except ErroHttp as e:
            falhas[corte_id] = {"id": corte_id, "erro": str(e), **e.extra}
        else:
            feitos.add(corte_id)
    feitos_lista = [i for i in ids if i in feitos]
    falhas_lista = [falhas[i] for i in ids if i in falhas]
    if falhas_lista:
        motivos = list(dict.fromkeys(f["erro"] for f in falhas_lista))
        log.info("%d corte(s) %s pelo painel, de uma vez; %d não: %s%s", len(feitos_lista), ROTULO_LOTE[acao],
                 len(falhas_lista), "; ".join(motivos[:3]), " ..." if len(motivos) > 3 else "")
    else:
        log.info("%d corte(s) %s pelo painel, de uma vez", len(feitos_lista), ROTULO_LOTE[acao])
    return {"acao": acao, "feitos": feitos_lista, "falhas": falhas_lista}


def _soltar_para_reeditar(cfg, conn: sqlite3.Connection, corte_id: int, parar: threading.Event,
                          prazo: float = 900) -> bool:
    """Tira um corte da fila para editar de novo, sem disputar com um envio em andamento.

    Com as travas de todas as redes na mão nenhum post começa no meio da troca: o publicador não
    acha o arquivo apagado (o corte virava "arquivo editado não encontrado") nem sobe o vídeo velho.
    """
    pegas: list[threading.Lock] = []
    limite = time.monotonic() + prazo
    try:
        for trava in TRAVAS_REDE.values():
            while not trava.acquire(timeout=1):
                if parar.is_set() or time.monotonic() > limite:
                    return False
            pegas.append(trava)
        c = conn.execute("SELECT arquivo, status FROM cortes WHERE id=?", (corte_id,)).fetchone()
        if c is None or c["status"] not in ("revisao", "pronto"):
            return False
        if conn.execute("SELECT 1 FROM postagens WHERE corte_id=? AND midia='video' AND status='enviando' LIMIT 1",
                        (corte_id,)).fetchone():
            return False
        _apagar_arquivos(c["arquivo"])
        _resolver_tarefas(cfg, conn, corte_id)  # a cópia na pasta sincronizada ficaria com o vídeo antigo
        # fila_em fica: o corte volta para o mesmo lugar da fila
        conn.execute("UPDATE cortes SET status='candidato', arquivo=NULL, erro=NULL, tentativas=0 WHERE id=?",
                     (corte_id,))
        return True
    finally:
        for trava in pegas:
            trava.release()


@rota("POST", r"/cortes/refazer-fila")
def refazer_fila(ctx: Contexto):
    """Edita de novo, com o visual em uso, os cortes que esperam na fila (e em revisão).

    Com "ids" (o "Editar de novo" da seleção na tela Cortes), só esses; os escolhidos que não estão
    na fila nem aguardando aprovação voltam em "fora". Sem "ids", a fila inteira.
    Um de cada vez: só o corte que está sendo editado sai da fila, e ele volta para o mesmo lugar.
    Vídeo criado de pauta fica de fora (quem gera outro é a Criação).
    """
    _exigir(not any(t.name == "refazer-fila" and t.is_alive() for t in threading.enumerate()),
            "A fila já está sendo refeita", 409)
    escolhidos = _ids_do_pedido(ctx.corpo["ids"]) if "ids" in ctx.corpo else None
    ids = [r["id"] for r in ctx.conn.execute(
        "SELECT c.id FROM cortes c JOIN filmes f ON f.id = c.filme_id "
        "WHERE c.status IN ('revisao', 'pronto') AND f.tipo = 'filme' AND c.arquivo IS NOT NULL "
        "ORDER BY c.prioridade DESC, c.fila_em, c.id")]
    fora: list[int] = []
    if escolhidos is not None:
        na_fila = set(ids)
        fora = [i for i in escolhidos if i not in na_fila]
        pedidos = set(escolhidos)
        ids = [i for i in ids if i in pedidos]  # na ordem da fila
        _exigir(bool(ids), "Nenhum dos cortes escolhidos está na fila ou aguardando aprovação")
    else:
        _exigir(bool(ids), "Não há cortes na fila para refazer")
    painel = ctx.painel

    def trabalho():
        conn = painel.conectar()
        feitos = 0
        try:
            for n, corte_id in enumerate(ids, 1):
                if painel.encerrar.is_set():
                    break
                ATIVIDADES.definir(f"Editando de novo os cortes escolhidos: corte {n} de {len(ids)}"
                                   if escolhidos is not None else
                                   f"Refazendo a fila com o visual atual: corte {n} de {len(ids)}")
                if not _soltar_para_reeditar(painel.cfg, conn, corte_id, painel.encerrar):
                    continue  # saiu da fila, foi postado ou está subindo agora
                corte = conn.execute("SELECT * FROM cortes WHERE id=?", (corte_id,)).fetchone()
                if corte is not None and Produtor(painel.cfg, painel.encerrar).renderizar(conn, corte):
                    feitos += 1
            if escolhidos is not None:
                log.info("Cortes escolhidos editados de novo com o visual atual: %d de %d", feitos, len(ids))
            else:
                log.info("Fila refeita com o visual atual: %d de %d corte(s) editados de novo", feitos, len(ids))
        finally:
            conn.close()

    # o pedido vai para o registro antes de a edição começar (senão o fim podia aparecer antes dele)
    if escolhidos is not None:
        log.info("Editando de novo %d corte(s) escolhidos com o visual atual (pedido pelo painel)%s", len(ids),
                 f"; {len(fora)} ficaram de fora (não estão na fila nem aguardando aprovação)" if fora else "")
    else:
        log.info("Refazendo %d corte(s) da fila com o visual atual (pedido pelo painel)", len(ids))
    _em_segundo_plano("refazer-fila", trabalho)
    return {"cortes": len(ids), "ids": ids, "fora": fora}


@rota("POST", r"/cortes/(\d+)/textos")
def textos_corte(ctx: Contexto, corte_id):
    c = _corte(ctx.conn, corte_id)
    titulo = str(ctx.corpo.get("titulo") or "").strip()[:300]
    descricao = str(ctx.corpo.get("descricao") or "").strip()[:5000]
    ctx.conn.execute(
        "UPDATE cortes SET titulo_custom=?, descricao_custom=? WHERE id=?", (titulo or None, descricao or None, c["id"])
    )
    return _corte_detalhe(ctx, c["id"])


@rota("POST", r"/cortes/(\d+)/postar")
def postar_corte(ctx: Contexto, corte_id):
    cfg, painel = ctx.cfg, ctx.painel
    rede = ctx.corpo.get("rede")
    _exigir(rede in PLATAFORMAS or rede == "facebook", "Rede inválida")
    base = "instagram" if rede == "facebook" else rede  # o Reel da Página usa a conta do Instagram
    _exigir(bool(cfg[base]["ativo"]), f"O {ROTULOS[base]} está desativado. Ative em Redes sociais.")
    _exigir(bool(cfg[base].get("videos", True)), "Esta rede está só com imagens: ligue Vídeos em Redes sociais")
    if rede == "facebook":
        _exigir(cfg["instagram"]["envio"] != "manual",
                "Com o Instagram postado à mão, ligue \"Compartilhar no Facebook\" no app ao postar o Reel")
        _exigir(_facebook_ligado(cfg), "Ligue \"Postar também na Página do Facebook\" nas opções do Instagram")
    c = _corte(ctx.conn, corte_id)
    _exigir(
        c["status"] in ("revisao", "pronto", "concluido") and bool(c["arquivo"]) and Path(c["arquivo"]).exists(),
        "Este corte ainda não foi editado",
    )
    if rede == "youtube":
        limite = int(cfg["youtube"]["max_segundos"])
        _exigir(c["fim"] - c["inicio"] <= limite + 0.5,
                f"Este corte tem {c['fim'] - c['inicio']:.0f} s e o YouTube recebe só Shorts de até {limite} s "
                "(o limite fica em Redes sociais > YouTube)")
    a_mao = rede != "facebook" and cfg[rede]["envio"] == "manual"
    if not cfg.simulacao and not a_mao:
        pronta, motivo = criar(base, cfg).pronta()
        _exigir(pronta, f"{ROTULOS[base]}: {motivo}")
        # a rede pausada pelo motor também vale aqui: "postar agora" não fura um bloqueio
        bloqueio = ler_bloqueios(ctx.conn).get(base)
        if bloqueio and bloqueio.get("tipo") != "rede":
            ate = bloqueio.get("ate")
            quando = ("até você testar a conta em Redes sociais" if ate is None
                      else f"até {datetime.fromtimestamp(ate).strftime('%d/%m %H:%M')}")
            raise ErroHttp(409, f"O {ROTULOS[base]} está pausado {quando}: {bloqueio.get('motivo')}")
    _exigir(not TRAVAS_REDE[rede].locked(), f"Já existe um envio para o {ROTULOS[rede]} em andamento", 409)
    if c["status"] == "revisao":  # postar na mão também aprova
        ctx.conn.execute("UPDATE cortes SET status='pronto' WHERE id=?", (c["id"],))
    corte_num = c["id"]

    if a_mao:  # nada é enviado: a tarefa sai na hora e o painel abre o vídeo e os textos
        corte = ctx.conn.execute("SELECT * FROM cortes WHERE id=?", (corte_num,)).fetchone()
        resultado = Publicador(cfg, painel.encerrar).postar(ctx.conn, rede, corte, manual=True)
        tarefa = ctx.conn.execute(
            "SELECT id FROM postagens WHERE corte_id=? AND plataforma=? AND status='aguardando' ORDER BY id DESC LIMIT 1",
            (corte_num, rede),
        ).fetchone()
        if tarefa is None:
            ultima = ctx.conn.execute("SELECT erro FROM postagens WHERE corte_id=? AND plataforma=? ORDER BY id DESC "
                                      "LIMIT 1", (corte_num, rede)).fetchone()
            motivo = ultima["erro"] if ultima is not None and ultima["erro"] else resultado
            raise ErroHttp(400, f"{ROTULOS[rede]}: a tarefa não foi criada ({motivo})")
        return {"ok": True, "tarefa": tarefa["id"], "simulacao": cfg.simulacao}

    def trabalho():
        conn = painel.conectar()
        try:
            corte = conn.execute("SELECT * FROM cortes WHERE id=?", (corte_num,)).fetchone()
            publicador = Publicador(painel.cfg, painel.encerrar)
            resultado = publicador.postar(conn, rede, corte, manual=True)
            if resultado == "ocupado":
                log.warning("%s: já havia um envio em andamento; tente de novo em instantes", ROTULOS[rede])
            publicador.atualizar_concluidos(conn)
        finally:
            conn.close()

    _em_segundo_plano(f"postar-{rede}", trabalho)
    return {"ok": True, "simulacao": cfg.simulacao}


# ---------------------------------------------------------------- postagem à mão

def _tarefa(conn: sqlite3.Connection, tarefa_id) -> sqlite3.Row:
    linha = conn.execute(
        "SELECT p.*, c.filme_id, c.parte, c.inicio, c.fim, c.frase, c.arquivo, c.renderizado_em, c.titulo_custom, "
        "c.descricao_custom, c.ia_textos, c.status AS corte_status, f.caminho, f.titulo AS filme_titulo, f.ano "
        "FROM postagens p JOIN cortes c ON c.id = p.corte_id JOIN filmes f ON f.id = c.filme_id "
        "WHERE p.id=? AND p.via='manual'",
        (int(tarefa_id),),
    ).fetchone()
    _exigir(linha is not None, "Tarefa não encontrada", 404)
    return linha


@rota("GET", r"/tarefas/(\d+)")
def detalhe_tarefa(ctx: Contexto, tarefa_id):
    cfg = ctx.cfg
    t = _tarefa(ctx.conn, tarefa_id)
    rede = t["plataforma"]
    _exigir(rede in PLATAFORMAS, "Rede desconhecida", 404)
    filme = {"caminho": t["caminho"], "titulo": t["filme_titulo"], "ano": t["ano"]}
    corte = {k: t[k] for k in ("parte", "inicio", "fim", "frase", "titulo_custom", "descricao_custom", "ia_textos")}
    conteudo = montar_conteudo(cfg, filme, corte)
    video, miniatura = _urls(t["corte_id"], t["arquivo"], t["renderizado_em"])
    copia = tarefas_manual.copia_da_tarefa(cfg, rede, t["id"]) if t["status"] == "aguardando" else None
    return {
        "id": t["id"], "rede": rede, "estado": t["status"], "horario": t["horario"], "quando": t["criado_em"],
        "manual": bool(t["manual"]), "corte_id": t["corte_id"], "filme": t["filme_titulo"], "parte": t["parte"],
        "duracao": round(t["fim"] - t["inicio"], 1), "url": t["url"], "erro": t["erro"],
        "video": video, "miniatura": miniatura, "arquivo": Path(t["arquivo"]).name if t["arquivo"] else None,
        "editando": t["corte_status"] in ("candidato", "renderizando"),
        "copia": str(copia) if copia else None,
        **tarefas_manual.pacote(cfg, rede, conteudo),
    }


def _fechar_tarefa(ctx: Contexto, tarefa_id, status: str, url: str | None = None, erro: str | None = None) -> dict:
    t = _tarefa(ctx.conn, tarefa_id)
    feito = ctx.conn.execute(
        "UPDATE postagens SET status=?, url=?, erro=?, atualizado_em=? WHERE id=? AND status='aguardando'",
        (status, url, erro, time.time(), t["id"]),
    ).rowcount
    _exigir(feito == 1, "Esta tarefa já foi resolvida", 409)
    tarefas_manual.apagar_copias(ctx.cfg, t["plataforma"], t["id"])
    Publicador(ctx.cfg, ctx.painel.encerrar).atualizar_concluidos(ctx.conn)
    return {"ok": True, "id": t["id"], "rede": t["plataforma"], "estado": status}


@rota("POST", r"/postagens/(\d+)/conferir")
def conferir_postagem(ctx: Contexto, postagem_id):
    """Você conferiu na rede o post que eu não vi sair.

    Saiu: fica publicado (com o link, se você colar). Não saiu: conta como uma falha e o corte
    volta para a fila daquela rede.
    """
    saiu = ctx.corpo.get("saiu")
    _exigir(isinstance(saiu, bool), "Diga se o post saiu")
    url = str(ctx.corpo.get("url") or "").strip()
    _exigir(not url or bool(re.fullmatch(r"https://[^\s<>\"']{4,490}", url)),
            "O link do post deve começar com https:// (ou deixe em branco)")
    p = ctx.conn.execute("SELECT id, plataforma, erro FROM postagens WHERE id=?", (int(postagem_id),)).fetchone()
    _exigir(p is not None, "Postagem não encontrada", 404)
    if saiu:
        feito = ctx.conn.execute(
            "UPDATE postagens SET status='publicado', url=COALESCE(?, url), erro=?, atualizado_em=? "
            "WHERE id=? AND status='conferir'",
            (url or None, "você conferiu na rede: saiu", time.time(), p["id"]),
        ).rowcount
    else:
        feito = ctx.conn.execute(
            "UPDATE postagens SET status='falhou', erro=?, atualizado_em=? WHERE id=? AND status='conferir'",
            (f"Você conferiu na rede: não saiu. {p['erro'] or ''}".strip()[:600], time.time(), p["id"]),
        ).rowcount
    _exigir(feito == 1, "Este post já foi conferido", 409)
    Publicador(ctx.cfg, ctx.painel.encerrar).atualizar_concluidos(ctx.conn)
    log.info("%s: você conferiu o post %s (%s)", ROTULOS.get(p["plataforma"], p["plataforma"]), p["id"],
             "saiu" if saiu else "não saiu; o corte volta para a fila")
    return {"ok": True, "id": p["id"], "rede": p["plataforma"], "estado": "publicado" if saiu else "falhou"}


@rota("POST", r"/tarefas/(\d+)/feito")
def tarefa_feita(ctx: Contexto, tarefa_id):
    url = str(ctx.corpo.get("url") or "").strip()
    _exigir(not url or bool(re.fullmatch(r"https://[^\s<>\"']{4,490}", url)),
            "O link do post deve começar com https:// (ou deixe em branco)")
    resposta = _fechar_tarefa(ctx, tarefa_id, "publicado", url=url or None)
    log.info("%s: você marcou a tarefa %s como postada%s", ROTULOS.get(resposta["rede"], resposta["rede"]),
             resposta["id"], f" ({url})" if url else "")
    return resposta


@rota("POST", r"/tarefas/(\d+)/pular")
def pular_tarefa(ctx: Contexto, tarefa_id):
    resposta = _fechar_tarefa(ctx, tarefa_id, "pulado", erro="pulado por você")
    log.info("%s: tarefa %s pulada pelo painel", ROTULOS.get(resposta["rede"], resposta["rede"]), resposta["id"])
    return resposta


# ---------------------------------------------------------------- filmes

def _arquivo_meta(caminho: Path) -> Path:
    for candidato in (caminho.with_name(caminho.name + ".json"), caminho.with_suffix(".json")):
        if candidato.exists():
            return candidato
    return caminho.with_name(caminho.name + ".json")


def _gravar_meta(caminho: Path, mudancas: dict) -> dict:
    arquivo = _arquivo_meta(caminho)
    atual = ler_metadados(caminho)
    for chave, valor in mudancas.items():
        if valor in (None, "", [], False) and chave != "ignorar":
            atual.pop(chave, None)
        elif chave == "ignorar" and not valor:
            atual.pop(chave, None)
        else:
            atual[chave] = valor
    if atual:
        salvar_json(arquivo, atual)
    else:
        arquivo.unlink(missing_ok=True)
    return atual


@rota("GET", r"/filmes")
def listar_filmes(ctx: Contexto):
    cfg, conn = ctx.cfg, ctx.conn
    contagens: dict[int, dict] = {}
    for r in conn.execute("SELECT filme_id, status, COUNT(*) AS n FROM cortes GROUP BY filme_id, status"):
        contagens.setdefault(r["filme_id"], {})[r["status"]] = r["n"]
    capas = {
        r["filme_id"]: (r["id"], r["arquivo"], r["renderizado_em"])
        for r in conn.execute(
            "SELECT filme_id, id, arquivo, renderizado_em FROM cortes WHERE arquivo IS NOT NULL ORDER BY renderizado_em")
    }
    itens = []
    for f in conn.execute(
        "SELECT * FROM filmes WHERE tipo='filme' "
        "ORDER BY CASE status WHEN 'analisando' THEN 0 WHEN 'novo' THEN 1 WHEN 'erro' THEN 2 "
        "ELSE 3 END, id DESC"
    ):
        caminho = Path(f["caminho"])
        meta = ler_metadados(caminho)
        legenda = ler_json(analise.pasta_do_filme(cfg, f["id"]) / "legenda.json", {}) or {}
        cont = contagens.get(f["id"], {})
        capa = capas.get(f["id"])
        itens.append({
            "id": f["id"], "titulo": f["titulo"], "ano": f["ano"], "status": f["status"], "erro": f["erro"],
            "duracao": f["duracao"], "tamanho": f["tamanho"], "arquivo": caminho.name, "criado_em": f["criado_em"],
            "esgotado": bool(f["esgotado"]),
            "cortes": {
                "candidatos": cont.get("candidato", 0) + cont.get("renderizando", 0),
                "revisao": cont.get("revisao", 0),
                "prontos": cont.get("pronto", 0),
                "publicados": cont.get("concluido", 0),
                "descartados": cont.get("descartado", 0) + cont.get("erro", 0),
            },
            "legenda": legenda.get("fonte"),
            "idioma": legenda.get("idioma"),
            "meta": {k: meta[k] for k in ("titulo", "ano", "hashtags", "idioma", "ignorar") if k in meta},
            "miniatura": _urls(*capa)[1] if capa else None,
        })
    return {"filmes": itens, "pasta": str(cfg.pasta_filmes), "extensoes": sorted(EXTENSOES)}


@rota("POST", r"/filmes/(\d+)/acao")
def acao_filme(ctx: Contexto, filme_id):
    cfg, conn = ctx.cfg, ctx.conn
    f = _filme(conn, filme_id)
    acao = ctx.corpo.get("acao")
    caminho = Path(f["caminho"])
    # a pauta mora na mesma tabela, mas não tem análise: reanalisar apagaria a pasta errada
    _exigir(f["tipo"] == "filme" or acao == "mostrar", "Isto é uma pauta de vídeo criado: use a tela Criação")
    if acao in ("reanalisar", "reanalisar_tudo"):
        _exigir(f["status"] != "analisando", "O filme está sendo analisado agora")
        conn.execute("DELETE FROM cortes WHERE filme_id=? AND status='candidato'", (f["id"],))
        pasta = analise.pasta_do_filme(cfg, f["id"])
        if acao == "reanalisar_tudo":
            shutil.rmtree(pasta, ignore_errors=True)
        else:  # mantém cenas/volume/voz em cache e refaz só as legendas e a seleção
            for nome in ("transcricao.json", "legenda.json"):
                (pasta / nome).unlink(missing_ok=True)
        conn.execute(
            "UPDATE filmes SET status='novo', erro=NULL, tentativas=0, esgotado=0, atualizado_em=? WHERE id=?",
            (time.time(), f["id"]),
        )
        log.info("'%s' voltou para a fila de análise", f["titulo"])
    elif acao in ("ignorar", "reativar"):
        _gravar_meta(caminho, {"ignorar": acao == "ignorar"})
        if acao == "ignorar":
            novo = "ignorado"
        else:
            tem_cortes = conn.execute("SELECT 1 FROM cortes WHERE filme_id=? LIMIT 1", (f["id"],)).fetchone()
            novo = "analisado" if tem_cortes else "novo"
        conn.execute("UPDATE filmes SET status=?, atualizado_em=? WHERE id=?", (novo, time.time(), f["id"]))
    elif acao == "mostrar":
        try:
            sistema.mostrar_arquivo(caminho)
        except OSError as e:
            raise ErroHttp(500, f"Não consegui abrir o Explorer: {e}") from e
    else:
        raise ErroHttp(400, "Ação desconhecida")
    return {"ok": True}


@rota("POST", r"/filmes/(\d+)/meta")
def meta_filme(ctx: Contexto, filme_id):
    conn = ctx.conn
    f = _filme(conn, filme_id)
    _exigir(f["tipo"] == "filme", "Isto é uma pauta de vídeo criado: edite o texto dela na tela Criação")
    caminho = Path(f["caminho"])
    titulo = str(ctx.corpo.get("titulo") or "").strip()[:150]
    ano_txt = str(ctx.corpo.get("ano") or "").strip()
    _exigir(not ano_txt or bool(re.fullmatch(r"\d{4}", ano_txt)), "O ano deve ter 4 números (ex.: 1999)")
    ano = _int(ano_txt) if ano_txt else None
    _exigir(ano is None or 1880 <= ano <= 2100, "Ano inválido")
    brutas = ctx.corpo.get("hashtags") or []
    _exigir(isinstance(brutas, list), "As hashtags devem vir em lista")
    # "#filme de terror" viraria #filme mais o texto solto: espaço e # repetido saem
    hashtags = list(dict.fromkeys("#" + t for t in (re.sub(r"[\s#]+", "", str(h)) for h in brutas) if t))[:20]
    _exigir(all(len(t) <= 61 for t in hashtags), "Hashtag comprida demais (máximo 60 letras)")
    idioma = str(ctx.corpo.get("idioma") or "").strip().lower()
    _exigir(idioma in ("", "auto") or bool(re.fullmatch(r"[a-z]{2,3}", idioma)), "Idioma inválido")
    _gravar_meta(caminho, {
        "titulo": titulo or None, "ano": ano, "hashtags": hashtags,
        "idioma": idioma if idioma not in ("", "auto") else None,
    })
    titulo_arquivo, ano_arquivo = titulo_do_arquivo(caminho.name)
    conn.execute("UPDATE filmes SET titulo=?, ano=? WHERE id=?", (titulo or titulo_arquivo, ano or ano_arquivo, f["id"]))
    return {"ok": True}


# nomes de dispositivo do Windows (valem mesmo com extensão: "nul.tar.png")
_RESERVADOS = ({"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
               | {f"{p}{i}" for p in ("COM", "LPT") for i in (*"0123456789", "¹", "²", "³")})


def _nome_seguro(nome: str) -> str:
    nome = Path(nome.replace("\\", "/")).name
    nome = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", nome).strip(" .")
    base, extensao = os.path.splitext(nome)
    _exigir(bool(base), "Nome de arquivo inválido")
    if base.split(".", 1)[0].rstrip(" ").upper() in _RESERVADOS:
        base = "_" + base
    return base[:150] + extensao[:10]


def _mover_sem_trocar(origem: Path, destino: Path) -> Path:
    """Põe o arquivo no lugar com o primeiro nome livre ("nome (2).mp4"...), sem nunca trocar outro.

    No Windows, os.rename falha se o destino existir: dois envios com o mesmo nome ao mesmo tempo
    não gravam um por cima do outro.
    """
    for n in range(1, 1000):
        candidato = destino if n == 1 else destino.with_name(f"{destino.stem} ({n}){destino.suffix}")
        if candidato.exists():
            continue
        try:
            os.rename(origem, candidato)
        except FileExistsError:
            continue
        return candidato
    raise ErroHttp(409, "Já existem arquivos demais com esse nome")


@rota("POST", r"/filmes/enviar", bruto=True)
def enviar_filme(ctx: Contexto):
    tratador, cfg = ctx.tratador, ctx.cfg
    tratador.close_connection = True  # em caso de erro não sobra corpo não lido na conexão
    from urllib.parse import unquote

    tamanho = _int(tratador.headers.get("Content-Length"), -1)
    pasta = cfg.pasta_filmes
    try:
        nome = _nome_seguro(unquote(tratador.headers.get("X-Nome-Arquivo", "")))
        _exigir(tamanho is not None and tamanho > 0, "Arquivo vazio ou sem tamanho informado", 411)
        _exigir(tamanho <= LIMITE_UPLOAD, "Arquivo grande demais", 413)
        extensao = Path(nome).suffix.lower()
        _exigir(extensao in EXTENSOES or extensao == ".srt", f"Formato não suportado: {extensao or 'sem extensão'}")
        pasta.mkdir(parents=True, exist_ok=True)
        _exigir(shutil.disk_usage(pasta).free > tamanho + 512 * 1024**2, "Espaço insuficiente no disco", 507)
    except (ErroHttp, OSError) as e:
        # recusado antes de começar: o arquivo é lido e jogado fora, senão o navegador não vê a mensagem
        tratador.descartar_corpo(maximo=LIMITE_UPLOAD, prazo=600)
        if isinstance(e, OSError):
            raise ErroHttp(500, f"Não consegui usar a pasta de filmes ({pasta}): {e}") from e
        raise

    parcial = pasta / f".envio-{secrets.token_hex(6)}.parcial"
    try:
        arquivo = open(parcial, "wb")
    except OSError as e:
        tratador.descartar_corpo(maximo=LIMITE_UPLOAD, prazo=600)
        raise ErroHttp(500, f"Não consegui criar o arquivo na pasta de filmes ({pasta}): {e}") from e
    tratador._corpo_lido = True  # daqui em diante esta rota lê o corpo inteiro, mesmo se der erro
    recebido = 0
    erro_disco: OSError | None = None
    try:
        try:
            while recebido < tamanho:
                bloco = tratador.rfile.read(min(1024 * 1024, tamanho - recebido))
                if not bloco:
                    break
                recebido += len(bloco)
                if erro_disco is None:
                    try:
                        arquivo.write(bloco)
                    except OSError as e:
                        erro_disco = e  # continua lendo até o fim, para a mensagem chegar ao navegador
        finally:
            try:
                arquivo.close()
            except OSError as e:
                erro_disco = erro_disco or e
        if erro_disco is not None:
            raise ErroHttp(507, f"Não consegui gravar o arquivo na pasta de filmes: {erro_disco}")
        _exigir(recebido == tamanho, "O envio foi interrompido")
        destino = _mover_sem_trocar(parcial, pasta / nome)
    except BaseException:
        parcial.unlink(missing_ok=True)
        raise
    antigo = time.time() - 180  # a varredura ignora arquivos modificados há menos de 1 min
    os.utime(destino, (antigo, antigo))
    novos = 0
    if extensao != ".srt":
        novos = varrer_biblioteca(cfg, ctx.conn)
        ctx.painel.motor.pedir_varredura()
    log.info("Arquivo recebido pelo painel: %s (%d MB)", destino.name, tamanho // 2**20)
    return {"ok": True, "arquivo": destino.name, "novos": novos}


@rota("POST", r"/filmes/varrer")
def varrer(ctx: Contexto):
    novos = varrer_biblioteca(ctx.cfg, ctx.conn)
    ctx.painel.motor.pedir_varredura()
    return {"novos": novos}


# ---------------------------------------------------------------- canais do YouTube (Filmes)

_REGRAS_CANAIS = ("verificar_a_cada_horas", "max_idade_dias", "minutos_min", "minutos_max", "incluir_ao_vivo",
                  "max_por_canal", "max_na_fila", "resolucao", "legendas", "espaco_minimo_gb", "pasta")


def _canais_json(ctx: Contexto) -> dict:
    cfg, conn = ctx.cfg, ctx.conn
    return {
        "ativo": bool(cfg["canais"]["ativo"]),
        "canais": canais.listar(conn),
        "downloads": canais.downloads_recentes(conn),
        "regras": {k: cfg["canais"][k] for k in _REGRAS_CANAIS},
        "verificado_em": canais.ultima_verificacao(conn),
        "proxima": canais.proxima_verificacao(cfg, conn),
        "pausa": canais.pausa(conn, cfg),
        "so_conta": canais.janela_so_conta(conn, cfg),
        "conta": canais_conta.publico(cfg),
        "conexao": _conexao_conta(ctx.painel),
        "rodando": canais.TRAVA.locked(),
        "motor": ctx.painel.motor.rodando,
        "na_fila": canais.na_fila(conn),
        "pasta": str(canais.pasta_canais(cfg)),
        "ferramenta": ferramentas.situacao_ytdlp(cfg),
    }


def _conexao_conta(painel) -> dict | None:
    """A janela de conectar a conta separada (a última aberta), ou None."""
    sessao = getattr(painel, "conta_canais", None)
    return sessao.publico() if sessao is not None else None


def _canal(conn: sqlite3.Connection, canal_id) -> sqlite3.Row:
    linha = conn.execute("SELECT * FROM canais WHERE id=?", (int(canal_id),)).fetchone()
    _exigir(linha is not None, "Canal não encontrado", 404)
    return linha


@rota("GET", r"/canais")
def listar_canais(ctx: Contexto):
    return _canais_json(ctx)


@rota("POST", r"/canais/novo")
def novo_canal(ctx: Contexto):
    try:
        canais.adicionar(ctx.conn, str(ctx.corpo.get("url") or ""))
    except ValueError as e:
        raise ErroHttp(400, str(e)) from e
    return _canais_json(ctx)


@rota("POST", r"/canais/(\d+)/acao")
def acao_canal(ctx: Contexto, canal_id):
    conn = ctx.conn
    c = _canal(conn, canal_id)
    acao = ctx.corpo.get("acao")
    if acao in ("pausar", "ativar"):
        conn.execute("UPDATE canais SET ativo=? WHERE id=?", (1 if acao == "ativar" else 0, c["id"]))
    elif acao == "remover":
        # os vídeos já baixados ficam na pasta de filmes (já são filmes como os outros), e o
        # histórico de downloads fica sem canal: adicionar o canal de novo não baixa tudo outra vez
        conn.execute("DELETE FROM canais WHERE id=?", (c["id"],))
        log.info("Canal do YouTube removido pelo painel: %s", c["url"])
    else:
        raise ErroHttp(400, "Ação desconhecida")
    return _canais_json(ctx)


@rota("POST", r"/canais/verificar")
def verificar_canais(ctx: Contexto):
    """Olha os canais agora (todos os ativos, ou um), em segundo plano. Durante a pausa depois de um
    aviso do YouTube, responde 409 (motivo "pausa"): só passa com {"forcar": true}, que o painel manda
    depois de perguntar (insistir costuma estender o bloqueio)."""
    _exigir(not canais.TRAVA.locked(), "Os canais já estão sendo olhados agora", 409)
    so_canal = _int(ctx.corpo.get("canal")) if ctx.corpo.get("canal") not in (None, "") else None
    if so_canal is not None:
        _exigir(bool(_canal(ctx.conn, so_canal)["ativo"]), "Este canal está pausado: ative antes de olhar")
    else:
        _exigir(ctx.conn.execute("SELECT 1 FROM canais WHERE ativo=1 LIMIT 1").fetchone() is not None,
                "Nenhum canal ativo para olhar")
    forcar = ctx.corpo.get("forcar") is True
    p = canais.pausa(ctx.conn, ctx.cfg)
    if p and not forcar:
        ate = datetime.fromtimestamp(float(p["ate"])).strftime("%d/%m às %H:%M")
        raise ErroHttp(409, f"Os downloads dos canais estão parados até {ate}: {p['motivo']}", {
            "motivo": "pausa", "ate": p["ate"], "desde": p["desde"], "tipo": p["tipo"], "aviso": p["motivo"],
            "detalhe": p["detalhe"], "vezes": p["vezes"],
        })
    painel = ctx.painel

    def trabalho():
        if not canais.TRAVA.acquire(blocking=False):
            return
        conn = painel.conectar()
        try:
            resumo = canais.verificar(painel.cfg, conn, painel.encerrar, so_canal=so_canal, forcar=forcar,
                                      ao_baixar=painel.motor.pedir_varredura)
            if resumo["baixados"] and not painel.motor.rodando:
                # com o motor desligado ninguém varre a pasta: o vídeo já aparece na lista de filmes
                try:
                    varrer_biblioteca(painel.cfg, conn)
                except sqlite3.IntegrityError:
                    pass  # outra varredura acabou de inserir o mesmo arquivo
        except canais.ErroCanal as e:
            log.warning("Canais: %s", e)
        finally:
            conn.close()
            canais.TRAVA.release()

    _em_segundo_plano("canais-agora", trabalho)
    return {"ok": True}


@rota("POST", r"/downloads/(\d+)/acao")
def acao_download(ctx: Contexto, download_id):
    conn = ctx.conn
    d = conn.execute("SELECT * FROM downloads WHERE id=?", (int(download_id),)).fetchone()
    _exigir(d is not None, "Download não encontrado", 404)
    acao = ctx.corpo.get("acao")
    if acao == "tentar":
        _exigir(d["status"] in ("erro", "pulado"), "Este vídeo não está com erro nem foi pulado")
        # sem o registro, ele volta a ser um vídeo novo na próxima olhada (se ainda servir nas regras)
        conn.execute("DELETE FROM downloads WHERE id=?", (d["id"],))
    else:
        raise ErroHttp(400, "Ação desconhecida")
    return _canais_json(ctx)


# a conta separada para baixar (opcional): só quando o YouTube pede, por robô ou idade
_TRAVA_CONTA_CANAIS = threading.Lock()  # dois cliques em "Entrar" não abrem duas janelas


@rota("GET", r"/canais/conta")
def ver_conta_canais(ctx: Contexto):
    return {"conta": canais_conta.publico(ctx.cfg),
            "conexao": _conexao_conta(ctx.painel) or {"estado": "nenhum", "mensagem": "", "desde": None}}


@rota("POST", r"/canais/conta/conectar")
def conectar_conta_canais(ctx: Contexto):
    """Abre um Chrome à parte, com perfil novo e temporário, no login do Google. Com uma janela já
    aberta, devolve a mesma (não abre outra)."""
    painel = ctx.painel
    with _TRAVA_CONTA_CANAIS:
        atual = painel.conta_canais
        if atual is not None and atual.ativa:
            return atual.publico()
        if atual is not None and atual.thread is not None and atual.thread.is_alive():
            # a janela anterior já foi cancelada, mas ainda está fechando: o perfil temporário é o
            # mesmo, e o fim dela apagaria a pasta da janela nova
            atual.thread.join(20)
        sessao = canais_conta.Conexao(painel.cfg, ao_conectar=lambda: _depois_de_conectar(painel))
        # já no "abrindo": o fechamento do AutoCortes a cancela e o api.ocupado a conta
        painel.conta_canais = sessao
        try:
            sessao.iniciar()
        except BaseException as e:
            if painel.conta_canais is sessao:
                painel.conta_canais = atual  # a que não abriu não fica no lugar da anterior
            if isinstance(e, (ErroNavegador, canais_conta.ErroConta)):
                raise ErroHttp(400, str(e)) from e
            raise
    return sessao.publico()


def _depois_de_conectar(painel) -> None:
    """Com a conta conectada, a pausa do aviso de robô vira a janela "só com a conta"."""
    conn = painel.conectar()
    try:
        if canais.conta_reconectada(conn):
            log.info("Canais: com a conta separada, a pausa do aviso de robô virou a janela em que as olhadas "
                     "vão direto com ela")
        if canais.pausa(conn, painel.cfg) is None:
            # o "conecte uma conta" / "precisa entrar de novo" do canal já não vale até a próxima olhada
            conn.execute("UPDATE canais SET erro=NULL WHERE erro LIKE '%conecte uma conta separada%' "
                         "OR erro LIKE '%conta separada precisa entrar de novo%'")
    finally:
        conn.close()


@rota("POST", r"/canais/conta/cancelar")
def cancelar_conta_canais(ctx: Contexto):
    sessao = ctx.painel.conta_canais
    if sessao is not None:
        sessao.cancelar()
    return {"ok": True}


@rota("POST", r"/canais/conta/esquecer")
def esquecer_conta_canais(ctx: Contexto):
    """Apaga os cookies guardados e o que tiver sobrado da janela temporária (a conta continua no Google)."""
    sessao = ctx.painel.conta_canais
    if sessao is not None:
        sessao.cancelar()
    canais_conta.esquecer(ctx.cfg)
    log.info("Canais: conta separada esquecida pelo painel")
    return _canais_json(ctx)


# ---------------------------------------------------------------- redes sociais

class SessaoLogin:
    """Login em andamento de uma rede (o navegador volta para o servidor temporário em 127.0.0.1)."""

    def __init__(self, rede: str):
        self.rede = rede
        self.estado = "aguardando"  # aguardando | escolher | ok | erro | cancelado
        self.mensagem = ""
        self.url: str | None = None
        self.paginas: list[dict] = []
        self.login = None
        self.thread: threading.Thread | None = None

    def cancelar(self) -> None:
        if self.estado not in ("aguardando", "escolher"):
            return
        self.estado = "cancelado"
        self.paginas = []
        if self.login is not None:
            self.login.servidor.cancelar()
        if self.thread is not None:
            self.thread.join(timeout=5)

    def publico(self) -> dict:
        dados = {"estado": self.estado, "mensagem": self.mensagem, "url": self.url}
        if self.estado == "escolher":
            dados["contas"] = [
                {"indice": i, "pagina": p.get("name"),
                 "usuario": (p.get("instagram_business_account") or {}).get("username")}
                for i, p in enumerate(self.paginas)
            ]
        return dados


def _rede(rede: str) -> str:
    _exigir(rede in PLATAFORMAS, "Rede inválida", 404)
    return rede


@rota("GET", r"/redes")
def redes(ctx: Contexto):
    cfg, conn = ctx.cfg, ctx.conn
    resumo = _resumo_redes(ctx)
    sucesso = planejador.status_sucesso(cfg)
    for rede, dados in resumo.items():
        sessao = ctx.painel.logins.get(rede)
        dados["login"] = sessao.publico() if sessao else None
        dados["postados_24h"] = conn.execute(
            f"SELECT COUNT(*) FROM postagens WHERE plataforma=? AND status IN ({_marc(sucesso)}) AND criado_em>=?",
            (rede, *sucesso, time.time() - 86400),
        ).fetchone()[0]
    return {"redes": resumo, "simulacao": cfg.simulacao, "meta": _meta_config(ctx), "config": _config_painel(ctx)}


@rota("POST", r"/redes/(\w+)/login")
def iniciar_login(ctx: Contexto, rede):
    rede = _rede(rede)
    _exigir(
        ctx.cfg[rede]["envio"] != "upload_post",
        f"O {ROTULOS[rede]} está configurado para postar pelo Upload-Post: conecte a conta no site do "
        "Upload-Post e use \"Testar conexão\".",
    )
    _exigir(ctx.cfg[rede]["envio"] != "manual",
            f"O {ROTULOS[rede]} é postado à mão: não há conta para conectar. As tarefas aparecem no Início.")
    if ctx.cfg[rede]["envio"] == "navegador":
        # abre a janela do navegador do AutoCortes na tela de login: você entra na conta uma vez
        try:
            url = criar(rede, ctx.cfg).abrir_para_login()
        except ErroNavegador as e:
            raise ErroHttp(400, str(e)) from e
        log.info("%s: janela do navegador aberta para login", ROTULOS[rede])
        return {"estado": "navegador", "url": url, "mensagem":
                "Entre na conta na janela que abriu. A sessão fica salva no perfil do AutoCortes."}
    painel = ctx.painel
    anterior = painel.logins.pop(rede, None)
    if anterior is not None:
        anterior.cancelar()
    plataforma = criar(rede, ctx.cfg)
    sessao = SessaoLogin(rede)

    if rede == "instagram":
        try:
            paginas = plataforma.trocar_token(str(ctx.corpo.get("token") or ""))
            if len(paginas) == 1:
                sessao.mensagem = plataforma.salvar_pagina(paginas[0])
                sessao.estado = "ok"
                painel.motor.rede_atualizada(rede, persistente=True)
                log.info("Instagram conectado: %s", sessao.mensagem)
            else:
                sessao.paginas, sessao.estado = paginas, "escolher"
        except ErroPublicacao as e:
            raise ErroHttp(400, str(e)) from e
        painel.logins[rede] = sessao
        return sessao.publico()

    try:
        login = plataforma.preparar_login()
    except ErroPublicacao as e:
        raise ErroHttp(400, str(e)) from e
    sessao.login, sessao.url = login, login.url
    login.servidor.iniciar()

    def esperar():
        try:
            p = login.servidor.esperar(login.estado, timeout=600)
            sessao.mensagem = plataforma.concluir_login(p["code"], login.servidor.redirect_uri, login.verificador)
            sessao.estado = "ok"
            painel.motor.rede_atualizada(rede, persistente=True)
            log.info("%s conectado: %s", ROTULOS[rede], sessao.mensagem)
        except ErroPublicacao as e:
            if sessao.estado != "cancelado":
                sessao.estado, sessao.mensagem = "erro", str(e)
                log.warning("Login do %s falhou: %s", ROTULOS[rede], e)
        except Exception as e:
            if sessao.estado != "cancelado":
                sessao.estado, sessao.mensagem = "erro", f"{e.__class__.__name__}: {e}"
                log.exception("Erro no login do %s", ROTULOS[rede])

    sessao.thread = threading.Thread(target=esperar, name=f"login-{rede}", daemon=True)
    sessao.thread.start()
    painel.logins[rede] = sessao
    return sessao.publico()


@rota("GET", r"/redes/(\w+)/login")
def estado_login(ctx: Contexto, rede):
    sessao = ctx.painel.logins.get(_rede(rede))
    return sessao.publico() if sessao else {"estado": "nenhum", "mensagem": "", "url": None}


@rota("POST", r"/redes/(\w+)/cancelar")
def cancelar_login(ctx: Contexto, rede):
    sessao = ctx.painel.logins.pop(_rede(rede), None)
    if sessao is not None:
        sessao.cancelar()
    return {"ok": True}


@rota("POST", r"/redes/instagram/conta")
def escolher_conta(ctx: Contexto):
    sessao = ctx.painel.logins.get("instagram")
    _exigir(sessao is not None and sessao.estado == "escolher", "Nenhuma escolha de conta pendente")
    indice = _int(ctx.corpo.get("indice"), -1)
    _exigir(0 <= indice < len(sessao.paginas), "Conta inválida")
    try:
        sessao.mensagem = criar("instagram", ctx.cfg).salvar_pagina(sessao.paginas[indice])
    except ErroPublicacao as e:
        raise ErroHttp(400, str(e)) from e
    sessao.estado, sessao.paginas = "ok", []
    ctx.painel.motor.rede_atualizada("instagram", persistente=True)
    log.info("Instagram conectado: %s", sessao.mensagem)
    return sessao.publico()


# ---------------------------------------------------------------- aprender a postar (navegador)

def _exigir_navegador(ctx: Contexto, rede: str) -> None:
    _exigir(ctx.cfg[rede]["envio"] == "navegador",
            f"O {ROTULOS[rede]} não está com o envio pelo navegador. Troque a forma de envio e salve.")


@rota("POST", r"/redes/(\w+)/gravar")
def iniciar_gravacao(ctx: Contexto, rede):
    """Abre a janela na página de envio e começa a anotar o que você faz."""
    rede = _rede(rede)
    _exigir_navegador(ctx, rede)
    painel = ctx.painel
    anterior = painel.gravacoes.pop(rede, None)
    if anterior is not None:
        anterior.cancelar()
    sessao = Gravacao(ctx.cfg, rede)
    try:
        sessao.iniciar()
    except ErroNavegador as e:
        raise ErroHttp(400, str(e)) from e
    painel.gravacoes[rede] = sessao
    return {"ok": True, **sessao.situacao()}


@rota("GET", r"/redes/(\w+)/gravar")
def estado_gravacao(ctx: Contexto, rede):
    sessao = ctx.painel.gravacoes.get(_rede(rede))
    return sessao.situacao() if sessao else {"rede": rede, "passos": 0, "erro": None, "parada": True}


@rota("POST", r"/redes/(\w+)/gravar/fim")
def concluir_gravacao(ctx: Contexto, rede):
    rede = _rede(rede)
    sessao = ctx.painel.gravacoes.pop(rede, None)
    _exigir(sessao is not None, "Nenhuma gravação em andamento nesta rede")
    try:
        resumo = sessao.concluir()
    except ErroNavegador as e:
        raise ErroHttp(400, str(e)) from e
    return {"ok": True, **resumo}


@rota("POST", r"/redes/(\w+)/gravar/cancelar")
def cancelar_gravacao(ctx: Contexto, rede):
    sessao = ctx.painel.gravacoes.pop(_rede(rede), None)
    if sessao is not None:
        sessao.cancelar()
    return {"ok": True}


@rota("POST", r"/redes/(\w+)/gravar/desfazer")
def desfazer_passo(ctx: Contexto, rede):
    sessao = ctx.painel.gravacoes.get(_rede(rede))
    _exigir(sessao is not None, "Nenhuma gravação em andamento nesta rede")
    return sessao.desfazer()


@rota("POST", r"/redes/(\w+)/gravar/recomecar")
def recomecar_gravacao(ctx: Contexto, rede):
    sessao = ctx.painel.gravacoes.get(_rede(rede))
    _exigir(sessao is not None, "Nenhuma gravação em andamento nesta rede")
    log.info("%s: gravação reiniciada pelo painel", ROTULOS[rede])
    return sessao.recomecar()


@rota("POST", r"/redes/(\w+)/roteiro/apagar")
def apagar_roteiro(ctx: Contexto, rede):
    """Apaga o roteiro do perfil. O padrão da instalação nunca é apagado: com ele, é para lá que se volta."""
    rede = _rede(rede)
    gravador.arquivo_roteiro(ctx.cfg, rede).unlink(missing_ok=True)
    volta = "padrao" if gravador.arquivo_padrao(rede).is_file() else "passos"
    log.info("%s: roteiro do perfil apagado pelo painel (volta a valer: %s)", ROTULOS[rede],
             "o roteiro padrão" if volta == "padrao" else "os passos fixos")
    return {"ok": True, "volta": volta}


@rota("GET", r"/redes/(\w+)/roteiro")
def ler_roteiro(ctx: Contexto, rede):
    """O texto do roteiro que vale, para você ver e editar. Salvar sempre grava no perfil."""
    rede = _rede(rede)
    texto, origem = gravador.carregar_com_origem(ctx.cfg, rede)
    texto = texto or ""
    return {
        "rede": rede,
        "texto": texto,
        "origem": origem,
        "resumo": roteiro.resumo(texto, rede) if texto else None,
        "comandos": roteiro.AJUDA,
        "papeis": sorted(roteiro.PAPEIS_DA_REDE.get(rede, ())),
        "arquivo": str(gravador.arquivo_roteiro(ctx.cfg, rede)),
        "arquivo_padrao": str(gravador.arquivo_padrao(rede)),
        "tem_padrao": gravador.arquivo_padrao(rede).is_file(),
    }


@rota("POST", r"/redes/(\w+)/roteiro")
def salvar_roteiro(ctx: Contexto, rede):
    """Grava o roteiro editado, recusando o que eu não consigo executar."""
    rede = _rede(rede)
    texto = str(ctx.corpo.get("texto") or "")
    _exigir(len(texto) <= 40000, "Roteiro grande demais")
    instrucoes, erros = roteiro.analisar(texto, rede)
    if erros:
        raise ErroHttp(400, "O roteiro tem linhas que eu não consigo executar", {"erros": erros})
    gravador.gravar_roteiro(ctx.cfg, rede, texto.replace("\r\n", "\n"))
    log.info("%s: roteiro editado pelo painel (%d linhas de ação)", ROTULOS[rede], len(instrucoes))
    return {"ok": True, "resumo": roteiro.resumo(texto, rede)}


@rota("POST", r"/redes/(\w+)/testar")
def testar_rede(ctx: Contexto, rede):
    plataforma = criar(_rede(rede), ctx.cfg)
    if plataforma.via != "navegador":
        # no navegador é o próprio teste que confirma a sessão e guarda a conta: não exigir antes
        pronta, motivo = plataforma.pronta()
        _exigir(pronta, f"{ROTULOS[rede]}: {motivo}")
    try:
        conta = plataforma.verificar()
    except ErroPublicacao as e:
        raise ErroHttp(400, str(e)) from e
    plataforma.lembrar_conta(conta)
    # a conta respondeu: vale até para o bloqueio que não passa sozinho (verificação, conta restrita)
    ctx.painel.motor.rede_atualizada(rede, persistente=True)
    return {"conta": conta}


@rota("POST", r"/redes/(\w+)/desconectar")
def desconectar_rede(ctx: Contexto, rede):
    criar(_rede(rede), ctx.cfg).desconectar()
    ctx.painel.motor.rede_atualizada(rede)
    log.info("%s desconectado pelo painel", ROTULOS[rede])
    return {"ok": True}


# ---------------------------------------------------------------- registro e sistema

@rota("GET", r"/logs")
def logs(ctx: Contexto):
    desde = max(0, _int(ctx.consulta.get("desde"), 0))
    return {"itens": LOG_MEMORIA.desde(desde, 600), "ultimo": LOG_MEMORIA.ultimo_id}


@rota("POST", r"/sistema/abrir")
def abrir(ctx: Contexto):
    cfg = ctx.cfg
    alvo = ctx.corpo.get("alvo")
    pastas = {"filmes": cfg.pasta_filmes, "cortes": cfg.pasta_cortes, "logs": cfg.pasta_logs, "dados": cfg.pasta_dados,
              "canais": canais.pasta_canais(cfg)}
    try:
        if alvo == "config":
            sistema.mostrar_arquivo(cfg.caminho)
        elif alvo in pastas:
            pastas[alvo].mkdir(parents=True, exist_ok=True)  # a dos canais só existe depois do primeiro download
            sistema.abrir_pasta(pastas[alvo])
        else:
            raise ErroHttp(400, "Pasta desconhecida")
    except OSError as e:
        raise ErroHttp(500, f"Não consegui abrir: {e}") from e
    return {"ok": True}


@rota("POST", r"/sistema/inicializacao")
def inicializacao(ctx: Contexto):
    ativo = bool(ctx.corpo.get("ativo"))
    try:
        sistema.definir_inicializacao(ativo)
    except OSError as e:
        raise ErroHttp(500, f"Não consegui alterar a inicialização do Windows: {e}") from e
    log.info("Iniciar com o Windows: %s", "ligado" if ativo else "desligado")
    return {"ativo": sistema.inicializacao_ativa()}


@rota("POST", r"/sistema/encerrar")
def encerrar(ctx: Contexto):
    log.info("Encerramento pedido pelo painel")
    threading.Timer(0.3, ctx.painel.encerrar.set).start()
    return {"ok": True}


# ---------------------------------------------------------------- atualização pelo GitHub

@rota("GET", r"/atualizacao")
def ver_atualizacao(ctx: Contexto):
    from .. import atualizacao

    return atualizacao.situacao(ctx.painel.perfil_principal)


@rota("POST", r"/atualizacao/verificar")
def procurar_atualizacao(ctx: Contexto):
    from .. import atualizacao

    atualizacao.verificar()
    return atualizacao.situacao(ctx.painel.perfil_principal)


@rota("POST", r"/atualizacao/aplicar")
def aplicar_atualizacao(ctx: Contexto):
    """Atualiza agora: fecha os perfis abertos, troca a versão e abre de novo (a janela se recarrega)."""
    from .. import atualizacao

    situacao = atualizacao.situacao(ctx.painel.perfil_principal)
    _exigir(situacao["disponivel"], "Não há versão nova para instalar. Procure de novo.")
    motivo = atualizacao.motivo_para_esperar(ctx.painel, contar_uso=False)
    _exigir(motivo is None, f"Agora não dá: {motivo}. Tente de novo em alguns minutos.", 409)
    atualizacao.iniciar_troca(ctx.painel)
    return {"ok": True}


@rota("POST", r"/atualizacao/opcoes")
def opcoes_atualizacao(ctx: Contexto):
    from .. import atualizacao

    corpo = {k: v for k, v in (ctx.corpo or {}).items() if k in ("atualizar_sozinho", "atualizar_a_cada_dias")}
    try:
        perfis.salvar_instalacao(corpo)
    except ErroConfig as e:
        raise ErroHttp(400, str(e)) from e
    except OSError as e:
        raise ErroHttp(500, f"Não consegui gravar o perfis.toml: {e}") from e
    return atualizacao.situacao(ctx.painel.perfil_principal)


# ---------------------------------------------------------------- criação (pautas e temas)

def _pauta(conn: sqlite3.Connection, pauta_id) -> sqlite3.Row:
    linha = conn.execute("SELECT * FROM filmes WHERE id=? AND tipo='pauta'", (int(pauta_id),)).fetchone()
    _exigir(linha is not None, "Pauta não encontrada", 404)
    return linha


def _pauta_json(cfg, conn: sqlite3.Connection, f: sqlite3.Row) -> dict:
    cortes = conn.execute(
        "SELECT id, status, arquivo, fim, renderizado_em FROM cortes WHERE filme_id=? ORDER BY id DESC",
        (f["id"],),
    ).fetchall()
    feitos = [c for c in cortes if c["status"] != "descartado"]
    ultimo = feitos[0] if feitos else None
    caminho = Path(f["caminho"])
    return {
        "id": f["id"], "titulo": f["titulo"], "arquivo": caminho.name, "status": f["status"],
        "erro": f["erro"], "duracao": f["duracao"], "criado_em": f["criado_em"],
        "videos": len(feitos),
        "corte": ({"id": ultimo["id"], "status": ultimo["status"],
                   "miniatura": _urls(ultimo["id"], ultimo["arquivo"], ultimo["renderizado_em"])[1],
                   "duracao": round(ultimo["fim"] or 0, 1)} if ultimo else None),
        "existe": caminho.is_file(),
    }


@rota("GET", r"/pautas")
def listar_pautas(ctx: Contexto):
    cfg, conn = ctx.cfg, ctx.conn
    criacao_mod = criacao
    linhas = conn.execute(
        "SELECT * FROM filmes WHERE tipo='pauta' "
        "ORDER BY CASE status WHEN 'erro' THEN 0 WHEN 'criando' THEN 1 WHEN 'novo' THEN 2 ELSE 3 END, id DESC"
    ).fetchall()
    fonte = str(cfg["estoque"]["fonte"])
    if fonte == "pasta":
        pasta_material = criacao_mod.estoque.pasta_material(cfg)
        material_ok = pasta_material.is_dir() and any(
            p.suffix.lower() in (criacao_mod.estoque.EXTENSOES_VIDEO | criacao_mod.estoque.EXTENSOES_IMAGEM)
            for p in pasta_material.rglob("*") if p.is_file())
        material = {"fonte": fonte, "ok": material_ok, "pasta": str(pasta_material)}
    else:
        material = {"fonte": fonte, "ok": bool(cfg["estoque"][f"{fonte}_chaves"]), "pasta": ""}
    temas_texto, temas_versao = criacao_mod.ler_temas_cru(cfg)
    return {
        "pautas": [_pauta_json(cfg, conn, f) for f in linhas],
        "temas": criacao_mod.temas(cfg),
        # o arquivo como está, com os comentários e os "# feito:" (é ele que a caixa edita)
        "temas_texto": temas_texto,
        "temas_versao": temas_versao,
        "pasta": str(criacao_mod.pasta_pautas(cfg)),
        "arquivo_temas": criacao_mod.ARQUIVO_TEMAS,
        "ativo": bool(cfg["criacao"]["ativo"]),
        "prioridade": str(cfg["criacao"]["prioridade"]),
        "ia": ia.disponivel(cfg),
        "material": material,
        "vozes": [{"nome": n, "rotulo": r} for n, r in voz.VOZES_SUGERIDAS],
        "voz_atual": str(cfg["voz"]["voz"]),
        "duracao_alvo": int(cfg["criacao"]["duracao_alvo_seg"]),
    }


@rota("POST", r"/pautas/nova")
def criar_pauta(ctx: Contexto):
    cfg, conn = ctx.cfg, ctx.conn
    tema = str(ctx.corpo.get("tema") or "").strip()
    _exigir(bool(tema), "Escreva o tema ou o título da pauta")
    _exigir(len(tema) <= 120, "Tema comprido demais")
    if ctx.corpo.get("com_ia"):
        _exigir(ia.disponivel(cfg), "Ligue a IA em Configurações > IA para ela escrever o roteiro")
        try:
            arquivo = criacao.pauta_do_tema(cfg, conn, tema)
        except criacao.ErroCriacao as e:
            raise ErroHttp(400, str(e)) from e
    else:
        arquivo = criacao.pasta_pautas(cfg) / f"{slug_pauta(tema)}.txt"
        _exigir(not arquivo.exists(), "Já existe uma pauta com esse nome")
        criacao.escrever_pauta(arquivo, criacao.Pauta(titulo=tema, texto="", tema=tema))
        criacao.sincronizar(cfg, conn)
    linha = conn.execute("SELECT * FROM filmes WHERE caminho=?", (str(arquivo.resolve()),)).fetchone()
    _exigir(linha is not None, "A pauta foi gravada, mas não entrou no banco (confira o formato)", 500)
    return {"pauta": _pauta_json(cfg, conn, linha)}


def slug_pauta(texto: str) -> str:
    from ..util import slug

    return slug(texto, 50)


@rota("GET", r"/pautas/(\d+)")
def ler_pauta_painel(ctx: Contexto, pauta_id):
    f = _pauta(ctx.conn, pauta_id)
    caminho = Path(f["caminho"])
    dados = _pauta_json(ctx.cfg, ctx.conn, f)
    dados["texto"] = caminho.read_text(encoding="utf-8-sig") if caminho.is_file() else ""
    dados["caminho"] = str(caminho)
    try:
        dados["avisos"] = criacao.ler_pauta(caminho).avisos if caminho.is_file() else []
    except criacao.ErroCriacao:
        dados["avisos"] = []
    return dados


@rota("POST", r"/pautas/(\d+)")
def salvar_pauta_painel(ctx: Contexto, pauta_id):
    cfg, conn = ctx.cfg, ctx.conn
    f = _pauta(conn, pauta_id)
    texto = str(ctx.corpo.get("texto") or "")
    _exigir(len(texto) <= 20000, "Pauta grande demais")
    _exigir(f["status"] != "criando", "Esta pauta está virando vídeo agora: espere terminar para editar", 409)
    caminho = Path(f["caminho"])
    anterior = caminho.read_text(encoding="utf-8-sig") if caminho.is_file() else ""
    escrever_atomico(caminho, texto.replace("\r\n", "\n"))
    try:
        criacao.ler_pauta(caminho)
    except criacao.ErroCriacao as e:
        escrever_atomico(caminho, anterior)  # volta o que estava lá: pauta inválida não fica salva
        raise ErroHttp(400, str(e)) from e
    criacao.sincronizar(cfg, conn)
    return ler_pauta_painel(ctx, pauta_id)


@rota("POST", r"/pautas/(\d+)/acao")
def acao_pauta(ctx: Contexto, pauta_id):
    cfg, conn = ctx.cfg, ctx.conn
    f = _pauta(conn, pauta_id)
    acao = ctx.corpo.get("acao")
    if acao == "criar_agora":
        _exigir(f["status"] != "criando", "Esta pauta já está virando vídeo agora", 409)
        with _TRAVA_FILA_CRIACAO:
            _exigir(f["id"] not in _CRIACOES_NA_FILA, "Este vídeo já está na fila para ser criado", 409)
            _CRIACOES_NA_FILA.add(f["id"])
        conn.execute("UPDATE filmes SET status='novo', esgotado=0, erro=NULL, tentativas=0, atualizado_em=? "
                     "WHERE id=?", (time.time(), f["id"]))
        _criar_em_segundo_plano(ctx.painel, f["id"])
    elif acao == "escrever_com_ia":
        _exigir(f["status"] != "criando", "Esta pauta está virando vídeo agora: espere terminar", 409)
        _exigir(ia.disponivel(cfg), "Ligue a IA em Configurações > IA")
        caminho = Path(f["caminho"])
        try:
            pauta = criacao.ler_pauta(caminho)
            criacao.completar_com_ia(cfg, criacao.Pauta(
                titulo=pauta.titulo, texto="", termos=pauta.termos, voz=pauta.voz, musica=pauta.musica,
                topo=pauta.topo, tema=pauta.tema or pauta.titulo, arquivo=caminho))
        except criacao.ErroCriacao as e:
            raise ErroHttp(400, str(e)) from e
        criacao.sincronizar(cfg, conn)
    elif acao == "excluir":
        _exigir(f["status"] != "criando", "Esta pauta está virando vídeo agora: espere terminar para excluir", 409)
        with _TRAVA_FILA_CRIACAO:
            _exigir(f["id"] not in _CRIACOES_NA_FILA,
                    "Esta pauta está na fila do \"Criar agora\": espere o vídeo sair para excluir", 409)
        try:
            Path(f["caminho"]).unlink(missing_ok=True)
        except OSError as e:
            raise ErroHttp(500, f"Não consegui apagar o arquivo da pauta: {e}") from e
        tem_video = conn.execute("SELECT 1 FROM cortes WHERE filme_id=? LIMIT 1", (f["id"],)).fetchone()
        if tem_video:  # os vídeos já feitos continuam na fila: a pauta só sai da lista
            conn.execute("UPDATE filmes SET status='ausente', atualizado_em=? WHERE id=?", (time.time(), f["id"]))
        else:
            conn.execute("DELETE FROM filmes WHERE id=?", (f["id"],))
        log.info("Pauta '%s' excluída pelo painel", f["titulo"])
    elif acao == "mostrar":
        try:
            sistema.mostrar_arquivo(Path(f["caminho"]))
        except OSError as e:
            raise ErroHttp(500, f"Não consegui abrir o Explorer: {e}") from e
    else:
        raise ErroHttp(400, "Ação desconhecida")
    return {"ok": True}


# pautas com um "Criar agora" esperando a vez (um pedido por pauta)
_CRIACOES_NA_FILA: set[int] = set()
_TRAVA_FILA_CRIACAO = threading.Lock()


def _criar_em_segundo_plano(painel, pauta_id: int) -> None:
    def trabalho():
        try:
            conn = painel.conectar()
            try:
                linha = conn.execute("SELECT * FROM filmes WHERE id=?", (pauta_id,)).fetchone()
                if linha is None:
                    return
                if TRAVA_EDICAO.locked():
                    ATIVIDADES.definir(f"'{linha['titulo']}' espera a edição atual terminar para virar vídeo")
                # a mesma trava do motor: uma edição ou criação por vez, e a pauta é reservada lá dentro
                with TRAVA_EDICAO:
                    linha = conn.execute("SELECT * FROM filmes WHERE id=?", (pauta_id,)).fetchone()
                    if linha is None or painel.encerrar.is_set():
                        return
                    ATIVIDADES.definir(f"Criando o vídeo '{linha['titulo']}'")
                    criacao.criar_do_banco(painel.cfg.copia(), conn, linha, painel.encerrar)
            finally:
                conn.close()
        finally:
            with _TRAVA_FILA_CRIACAO:
                _CRIACOES_NA_FILA.discard(pauta_id)

    _em_segundo_plano(f"criacao-{pauta_id}", trabalho)


@rota("POST", r"/pautas/temas")
def salvar_temas(ctx: Contexto):
    """Grava o arquivo da fila de temas inteiro, com os comentários, se ele não mudou desde que abriu."""
    cfg = ctx.cfg
    texto = str(ctx.corpo.get("texto") or "")
    _exigir(len(texto) <= 20000, "Lista grande demais")
    versao = ctx.corpo.get("versao")
    try:
        texto_novo, versao_nova = criacao.salvar_temas_cru(cfg, texto, None if versao is None else str(versao))
    except criacao.TemasMudaram as e:
        raise ErroHttp(409, "A fila de temas mudou enquanto você editava (o motor usou um tema). Carreguei a "
                       "versão nova: confira e salve de novo.", {"texto": e.texto, "versao": e.versao}) from e
    return {"temas": criacao.temas(cfg), "temas_texto": texto_novo, "temas_versao": versao_nova}


# ---------------------------------------------------------------- perfis (nichos)

def _perfil(slug: str):
    """O perfil da URL ('principal' = o config.toml da instalação)."""
    try:
        return perfis.encontrar("" if slug == "principal" else slug)
    except ErroConfig as e:
        raise ErroHttp(404, str(e)) from e


def _perfis_erro(e: ErroConfig) -> ErroHttp:
    return ErroHttp(400, str(e), {"erros": getattr(e, "erros", None) or [str(e)]})


@rota("GET", r"/perfis")
def listar_perfis(ctx: Contexto):
    lista = perfis.listar()
    estados = perfis.situacoes(lista)  # em paralelo: um perfil fechado não atrasa os outros
    atual = ctx.cfg.caminho
    saida = []
    for p in lista:
        dados = p.para_painel()
        dados["situacao"] = estados.get(p.slug, {"rodando": False, "outro": False})
        try:
            dados["atual"] = p.config.resolve() == atual.resolve()
        except OSError:
            dados["atual"] = False
        if dados["atual"]:  # este processo: o /saude só responde depois, mas ele está aberto agora
            dados["situacao"] = {"rodando": True, "outro": False, "desde": ctx.painel.iniciado_em,
                                 "motor": ctx.painel.motor.rodando, "porta": ctx.painel.porta}
        _url_aberta(dados)
        saida.append(dados)
    return {
        "perfis": saida,
        "instalacao": perfis.instalacao(),
        "conflitos": perfis.conflitos(lista),
        "max": perfis.MAX_PERFIS,
        "abertos": sum(1 for d in saida if d["situacao"]["rodando"]),
    }


def _url_aberta(dados: dict) -> None:
    """Perfil aberto: o link vai para a porta em que ele está de verdade.

    Trocar a porta com o perfil aberto só vale na próxima abertura; até lá o painel dele continua
    na porta antiga, e o link para a nova não abria nada.
    """
    situacao = dados.get("situacao") or {}
    porta = situacao.get("porta")
    if situacao.get("rodando") and porta:
        dados["url"] = f"http://127.0.0.1:{int(porta)}/"
        dados["porta_aberta"] = int(porta)


@rota("POST", r"/perfis/novo")
def criar_perfil(ctx: Contexto):
    try:
        p = perfis.criar(str(ctx.corpo.get("nome") or ""))
    except ErroConfig as e:
        raise _perfis_erro(e) from e
    except OSError as e:
        raise ErroHttp(500, f"Não consegui criar a pasta do perfil: {e}") from e
    return {"perfil": p.para_painel()}


@rota("POST", r"/perfis/opcoes")
def opcoes_perfis(ctx: Contexto):
    try:
        dados = perfis.salvar_instalacao(ctx.corpo or {})
    except ErroConfig as e:
        raise _perfis_erro(e) from e
    except OSError as e:
        raise ErroHttp(500, f"Não consegui gravar o perfis.toml: {e}") from e
    return {"instalacao": dados}


@rota("POST", r"/perfis/([\w-]{1,40})/abrir")
def abrir_perfil(ctx: Contexto, slug):
    p = _perfil(slug)
    try:
        return perfis.iniciar(p)
    except ErroConfig as e:
        raise _perfis_erro(e) from e


@rota("POST", r"/perfis/([\w-]{1,40})/fechar")
def fechar_perfil(ctx: Contexto, slug):
    p = _perfil(slug)
    if p.config.resolve() == ctx.cfg.caminho.resolve():
        raise ErroHttp(400, "Este é o perfil que você está vendo: use 'Fechar o AutoCortes' na lateral")
    try:
        return perfis.parar(p)
    except ErroConfig as e:
        raise _perfis_erro(e) from e


@rota("POST", r"/perfis/([\w-]{1,40})/salvar")
def salvar_perfil(ctx: Contexto, slug):
    p = _perfil(slug)
    try:
        novo = perfis.atualizar(p, ctx.corpo or {})
    except ErroConfig as e:
        raise _perfis_erro(e) from e
    dados = novo.para_painel()
    try:
        dados["atual"] = novo.config.resolve() == ctx.cfg.caminho.resolve()
    except OSError:
        dados["atual"] = False
    if dados["atual"]:
        dados["situacao"] = {"rodando": True, "outro": False, "desde": ctx.painel.iniciado_em,
                             "motor": ctx.painel.motor.rodando, "porta": ctx.painel.porta}
    else:
        dados["situacao"] = perfis.situacao(novo)
    _url_aberta(dados)
    mudou_porta = (ctx.corpo or {}).keys() & {"porta", "porta_navegador"} and (
        novo.porta != p.porta or novo.porta_navegador != p.porta_navegador)
    if dados["situacao"].get("rodando") and mudou_porta:
        dados["aviso"] = "A porta nova vale quando você fechar e abrir este perfil."
    return {"perfil": dados}


@rota("GET", r"/perfis/([\w-]{1,40})/conteudo")
def conteudo_perfil(ctx: Contexto, slug):
    return perfis.conteudo(_perfil(slug))


@rota("POST", r"/perfis/([\w-]{1,40})/excluir")
def excluir_perfil(ctx: Contexto, slug):
    p = _perfil(slug)
    confirmacao = str((ctx.corpo or {}).get("confirmacao") or "").strip()
    _exigir(confirmacao.lower() == p.nome.strip().lower(),
            f"Para excluir, escreva o nome do perfil exatamente: {p.nome}")
    try:
        return {"ok": True, "apagado": perfis.excluir(p.slug)}
    except ErroConfig as e:
        raise _perfis_erro(e) from e


# ---------------------------------------------------------------- mídia

def midia(painel, caminho: str, tratador) -> None:
    cfg = painel.cfg
    if caminho == "previa.jpg":
        arquivo = cfg.pasta_previa / "previa.jpg"
        _exigir(arquivo.exists(), "Prévia ainda não gerada", 404)
        return tratador.enviar_arquivo(arquivo, "image/jpeg")
    if caminho == "quadro.png":  # quadro de exemplo do Estúdio
        conn = painel.conectar()
        try:
            arquivo = _quadro_do_filme(cfg, conn)
        finally:
            conn.close()
        arquivo = arquivo or cfg.pasta_previa / "quadro_exemplo.png"
        _exigir(arquivo.exists(), "Quadro de exemplo ainda não gerado", 404)
        return tratador.enviar_arquivo(arquivo, "image/png")
    if caminho.startswith("moldura/"):  # só arquivos da pasta molduras
        nome = caminho[len("moldura/"):]
        _exigir(bool(re.fullmatch(r"[^/\\:*?\"<>|\x00-\x1f]+\.png", nome, re.I)), "Moldura não encontrada", 404)
        arquivo = cfg.pasta_molduras / nome
        _exigir(arquivo.is_file() and arquivo.resolve().parent == cfg.pasta_molduras.resolve(),
                "Moldura não encontrada", 404)
        return tratador.enviar_arquivo(arquivo, "image/png")
    if caminho == "moldura-atual":  # a moldura do config, mesmo fora da pasta molduras
        arquivo = edicao.caminho_moldura(cfg)
        _exigir(arquivo is not None and arquivo.suffix.lower() == ".png", "Moldura não encontrada", 404)
        return tratador.enviar_arquivo(arquivo, "image/png")
    achado = re.fullmatch(r"corte/(\d+)\.(mp4|jpg)", caminho)
    _exigir(achado is not None, "Mídia não encontrada", 404)
    conn = painel.conectar()
    try:
        linha = conn.execute("SELECT arquivo FROM cortes WHERE id=?", (int(achado.group(1)),)).fetchone()
    finally:
        conn.close()
    _exigir(linha is not None and bool(linha["arquivo"]), "Mídia não encontrada", 404)
    video = Path(linha["arquivo"])
    _exigir(_dentro(video, cfg.pasta_dados), "Arquivo fora da pasta de dados", 403)
    if achado.group(2) == "mp4":
        _exigir(video.exists(), "Vídeo não encontrado", 404)
        return tratador.enviar_arquivo(video, "video/mp4")
    miniatura = video.with_suffix(".jpg")
    if not miniatura.exists() and video.exists():
        with TRAVA_MINIATURA:
            if not miniatura.exists():
                edicao.gerar_miniatura(cfg, video, 2.0)
    _exigir(miniatura.exists(), "Miniatura não encontrada", 404)
    return tratador.enviar_arquivo(miniatura, "image/jpeg")

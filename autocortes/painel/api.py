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

from .. import (__version__, agenda, analise, config, criacao, edicao, ferramentas, gravador, ia, metricas,
                modelos_visuais, perfis, planejador, roteiro, voz)
from ..config import ENVIOS, PLATAFORMAS, ROTULOS, ErroConfig
from ..midia import ErroMidia, Interrompido, base_ffmpeg, executar
from ..navegador import ErroNavegador
from ..plataformas import ErroPublicacao, criar
from ..plataformas import manual as tarefas_manual
from ..plataformas.navegador import Gravacao
from ..produtor import EXTENSOES, Produtor, escrever_textos_ia, varrer_biblioteca
from ..publicador import MAX_TENTATIVAS_FACEBOOK, TRAVAS_REDE, Publicador
from ..textos import fonte_dos_textos, ler_metadados, montar_conteudo, texto_topo, textos_ia, titulo_do_arquivo
from ..util import ATIVIDADES, LOG_MEMORIA, ler_json, log, salvar_json
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

    threading.Thread(target=rodar, name=nome, daemon=True).start()


# ---------------------------------------------------------------- visão geral

def _facebook_ligado(cfg) -> bool:
    """O Reel do Instagram vai também para a Página do Facebook (pela API oficial ou pelo Upload-Post)."""
    ig = cfg["instagram"]
    return bool(ig["ativo"] and ig["pagina_facebook"] and ig["envio"] in ("oficial", "upload_post"))


def _resumo_redes(ctx: Contexto) -> dict:
    cfg, motor = ctx.cfg, ctx.painel.motor
    bloqueadas = dict(motor.publicador.bloqueadas) if motor.publicador is not None and motor.rodando else {}
    agora = datetime.now()
    esperando = dict(ctx.conn.execute(
        "SELECT plataforma, COUNT(*) FROM postagens WHERE status='aguardando' GROUP BY plataforma").fetchall())
    saida = {}
    for rede in PLATAFORMAS:
        plataforma = criar(rede, cfg)
        pronta, motivo = plataforma.pronta()
        proximo = agenda.proximo_horario(cfg, rede, agora) if cfg[rede]["ativo"] else None
        saida[rede] = {
            "ativo": bool(cfg[rede]["ativo"]),
            "via": plataforma.via,
            "pronta": pronta,
            "motivo": motivo,
            "conta": plataforma.conta(),
            "bloqueada": bloqueadas.get(rede, (None, 0))[0],
            "proximo": proximo.timestamp() if proximo else None,
            "por_semana": agenda.posts_por_semana(cfg, rede),
            "tarefas": esperando.get(rede, 0),
        }
        if rede == "instagram":
            saida[rede]["facebook"] = {
                "ativo": _facebook_ligado(cfg),
                "pagina": plataforma.pagina() if hasattr(plataforma, "pagina") else None,
            }
        if cfg[rede]["envio"] == "navegador":
            texto = gravador.carregar(cfg, rede)
            saida[rede]["roteiro"] = {"tem": bool(texto), **(roteiro.resumo(texto, rede) if texto else {})}
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


def _alertas(ctx: Contexto, filmes: dict, cortes: dict, cobertura: dict, redes: dict) -> list[dict]:
    cfg, motor = ctx.cfg, ctx.painel.motor
    alertas: list[dict] = []

    def add(nivel: str, texto: str, destino: str | None = None) -> None:
        alertas.append({"nivel": nivel, "texto": texto, "destino": destino})

    total_filmes = sum(v for k, v in filmes.items() if k != "ausente")
    if not motor.rodando:
        add("aviso", "O motor está desligado: nada será editado nem postado.", "motor")
    if cfg.simulacao:
        add("info", "Modo simulação ligado: os vídeos são gerados, mas nada é publicado.", "redes")
    if cfg["agenda"]["pausado"]:
        add("aviso", "As postagens estão pausadas.", "agenda")
    if total_filmes == 0:
        add("aviso", "Nenhum filme na pasta. Adicione filmes para começar.", "filmes")
    if filmes.get("erro"):
        add("erro", f"{filmes['erro']} filme(s) com erro na análise.", "filmes")
    if cortes.get("revisao"):
        add("info", f"{cortes['revisao']} corte(s) aguardando sua aprovação.", "cortes?aba=revisao")
    ativas = [r for r, v in redes.items() if v["ativo"]]
    if not ativas:
        add("erro", "Nenhuma rede social ativa.", "redes")
    for rede in ativas:
        v = redes[rede]
        if v["bloqueada"]:
            add("erro", f"{ROTULOS[rede]} pausado: {v['bloqueada']}", "redes")
        elif not v["pronta"]:
            add("info" if cfg.simulacao else "erro", f"{ROTULOS[rede]}: {v['motivo']}.", "redes")
    if "tiktok" in ativas and cfg["tiktok"]["privacidade"] == "SELF_ONLY" and redes["tiktok"]["via"] == "oficial":
        if redes["tiktok"]["via"] == "upload_post":
            add("aviso", "O TikTok posta pelo Upload-Post, mas a privacidade está em \"Só eu\". "
                "Troque para \"Todos\" para publicar em público.", "redes")
        else:
            add("info", "TikTok pela API oficial: os posts ficam visíveis só para você. Para postar em "
                "público, use o envio pelo Upload-Post em Redes sociais.", "redes")
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
    if dias is not None and dias < 3 and total_filmes:
        if dias < 1:
            quanto = "menos de 1 dia"
        else:
            numero = f"{dias:.1f}".rstrip("0").rstrip(".").replace(".", ",")  # 1,7 em vez de 1.7
            quanto = "só 1 dia" if numero == "1" else f"só {numero} dias"
        add("aviso", f"Conteúdo para {quanto} de postagem. Adicione mais filmes.", "filmes")
    if cfg["transcricao"]["fonte"] != "nenhuma" and not ferramentas.caminho_modelo(cfg).exists():
        add("info", f"O modelo de transcrição '{cfg['transcricao']['modelo']}' será baixado na primeira análise.",
            "config?aba=transcricao")
    problema = edicao.problema_moldura(cfg)
    if problema:
        add("erro", f"{problema}: os cortes estão saindo sem ela. Escolha outra no Estúdio.", "estudio")
    return alertas


@rota("GET", r"/estado")
def estado(ctx: Contexto):
    cfg, conn, motor = ctx.cfg, ctx.conn, ctx.painel.motor
    sucesso = planejador.status_sucesso(cfg)
    inicio_dia = datetime.combine(date.today(), dtime()).timestamp()
    filmes = _contagem(conn, "filmes")
    cortes = _contagem(conn, "cortes")
    hoje = dict(conn.execute(
        f"SELECT plataforma, COUNT(*) FROM postagens WHERE status IN ({_marc(sucesso)}) AND criado_em >= ? "
        "GROUP BY plataforma",
        (*sucesso, inicio_dia),
    ).fetchall())
    plano = planejador.montar_plano(cfg, conn, dias=3)
    proximos = [i for i in plano["itens"] if i["tipo"] == "horario" and i["estado"] != "perdido"][:6]
    _anexar_midia(conn, proximos)
    redes = _resumo_redes(ctx)
    cobertura = plano["cobertura"]
    na_fila = max((v["na_fila"] for v in cobertura["por_rede"].values()), default=cortes.get("pronto", 0))
    tarefas = _tarefas(conn)
    return {
        "versao": __version__,
        "agora": time.time(),
        "motor": {"rodando": motor.rodando, "parando": motor.parando, "desde": motor.iniciado_em},
        "simulacao": cfg.simulacao,
        "pausado": bool(cfg["agenda"]["pausado"]),
        "aprovacao": bool(cfg["geral"]["exigir_aprovacao"]),
        "numeros": {
            "filmes": sum(v for k, v in filmes.items() if k != "ausente"),
            "filmes_na_fila": filmes.get("novo", 0) + filmes.get("analisando", 0),
            "filmes_erro": filmes.get("erro", 0),
            "na_fila": na_fila,
            "revisao": cortes.get("revisao", 0),
            "candidatos": cortes.get("candidato", 0) + cortes.get("renderizando", 0),
            "hoje": sum(hoje.values()),
            "hoje_por_rede": hoje,
            "tarefas": sum(v["tarefas"] for v in redes.values()),
        },
        "cobertura": cobertura,
        "proximos": proximos,
        "tarefas": tarefas,
        "ultimas": _ultimas_postagens(conn, 8),
        "atividades": sorted(ATIVIDADES.listar(), key=lambda a: a["desde"]),
        "redes": redes,
        "desempenho": metricas.resumo(conn),
        "ia": ia.disponivel(cfg),
        "alertas": _alertas(ctx, filmes, cortes, cobertura, redes),
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
        "fontes": sistema.fontes_disponiveis(cfg),
        "inicializacao": sistema.inicializacao_ativa(),
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
    _exigir(tamanho <= LIMITE_MOLDURA, "Imagem grande demais (máximo 30 MB)", 413)
    # lê tudo antes de validar: assim a resposta de erro chega ao navegador em vez de derrubar a conexão
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
    ultima: dict[int, dict[str, sqlite3.Row]] = {}
    feitas: dict[int, set[str]] = {}
    for p in conn.execute(
        "SELECT id, corte_id, plataforma, status, url, erro, criado_em, via, visualizacoes, curtidas, comentarios "
        "FROM postagens ORDER BY id"
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
    ativas = cfg.plataformas_ativas()
    # a tarefa à mão ainda não foi postada: o corte continua em "Na fila" até você marcar
    ultima, feitas = _postagens_por_corte(conn, set(planejador.status_concluidos(cfg)) - {"aguardando"})

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


def _resolver_tarefas_do_corte(ctx: Contexto, corte_id: int, pular: str | None = None) -> None:
    """Apaga as cópias das tarefas à mão do corte na pasta sincronizada (e pula as tarefas, se pedido)."""
    for t in ctx.conn.execute("SELECT id, plataforma FROM postagens WHERE corte_id=? AND status='aguardando'",
                              (corte_id,)).fetchall():
        tarefas_manual.apagar_copias(ctx.cfg, t["plataforma"], t["id"])
        if pular:
            ctx.conn.execute("UPDATE postagens SET status='pulado', erro=?, atualizado_em=? WHERE id=?",
                             (pular, time.time(), t["id"]))


@rota("POST", r"/cortes/(\d+)/acao")
def acao_corte(ctx: Contexto, corte_id):
    conn = ctx.conn
    c = _corte(conn, corte_id)
    status, acao = c["status"], ctx.corpo.get("acao")
    criado = c["filme_tipo"] == "pauta"  # vídeo criado do zero: quem regera é a pauta, não o editor de cortes
    if criado and acao in ("reeditar", "editar_agora", "restaurar"):
        raise ErroHttp(400, "Este vídeo foi criado de uma pauta: use \"Criar de novo\" para gerar outro")
    if acao == "recriar":
        _exigir(criado, "Só vídeo criado de uma pauta pode ser gerado de novo")
        _apagar_arquivos(c["arquivo"])
        _resolver_tarefas_do_corte(ctx, c["id"], pular="vídeo vai ser criado de novo")
        conn.execute("UPDATE cortes SET status='descartado', arquivo=NULL WHERE id=?", (c["id"],))
        conn.execute(
            "UPDATE filmes SET status='novo', esgotado=0, erro=NULL, tentativas=0, atualizado_em=? WHERE id=?",
            (time.time(), c["filme_id"]),
        )
        log.info("Pauta '%s' vai gerar outro vídeo (o corte %s foi descartado)", c["filme_titulo"], c["id"])
        return _corte_detalhe(ctx, c["id"])
    if acao == "aprovar":
        _exigir(status == "revisao", "Só cortes aguardando aprovação podem ser aprovados")
        conn.execute("UPDATE cortes SET status='pronto' WHERE id=?", (c["id"],))
        log.info("Corte %s aprovado pelo painel", c["id"])
    elif acao == "descartar":
        _exigir(status in ("candidato", "revisao", "pronto", "erro"), "Este corte não pode ser descartado agora")
        _apagar_arquivos(c["arquivo"])
        conn.execute("UPDATE cortes SET status='descartado', arquivo=NULL WHERE id=?", (c["id"],))
        _resolver_tarefas_do_corte(ctx, c["id"], pular="corte descartado")
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
    else:
        raise ErroHttp(400, "Ação desconhecida")
    return _corte_detalhe(ctx, c["id"])


@rota("POST", r"/cortes/aprovar-todos")
def aprovar_todos(ctx: Contexto):
    n = ctx.conn.execute("UPDATE cortes SET status='pronto' WHERE status='revisao'").rowcount
    log.info("%d corte(s) aprovados pelo painel", n)
    return {"aprovados": n}


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
    caminho = Path(f["caminho"])
    titulo = str(ctx.corpo.get("titulo") or "").strip()[:150]
    ano_txt = str(ctx.corpo.get("ano") or "").strip()
    ano = _int(ano_txt) if ano_txt else None
    _exigir(ano is None or 1880 <= ano <= 2100, "Ano inválido")
    hashtags = [str(h).strip() for h in (ctx.corpo.get("hashtags") or []) if str(h).strip()][:20]
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


def _sem_colisao(destino: Path) -> Path:
    if not destino.exists():
        return destino
    for n in range(2, 1000):
        candidato = destino.with_name(f"{destino.stem} ({n}){destino.suffix}")
        if not candidato.exists():
            return candidato
    raise ErroHttp(409, "Já existem arquivos demais com esse nome")


@rota("POST", r"/filmes/enviar", bruto=True)
def enviar_filme(ctx: Contexto):
    tratador, cfg = ctx.tratador, ctx.cfg
    tratador.close_connection = True  # em caso de erro não sobra corpo não lido na conexão
    from urllib.parse import unquote

    nome = _nome_seguro(unquote(tratador.headers.get("X-Nome-Arquivo", "")))
    tamanho = _int(tratador.headers.get("Content-Length"), -1)
    _exigir(tamanho is not None and tamanho > 0, "Arquivo vazio ou sem tamanho informado", 411)
    _exigir(tamanho <= LIMITE_UPLOAD, "Arquivo grande demais", 413)
    extensao = Path(nome).suffix.lower()
    _exigir(extensao in EXTENSOES or extensao == ".srt", f"Formato não suportado: {extensao or 'sem extensão'}")
    pasta = cfg.pasta_filmes
    pasta.mkdir(parents=True, exist_ok=True)
    _exigir(shutil.disk_usage(pasta).free > tamanho + 512 * 1024**2, "Espaço insuficiente no disco", 507)

    destino = _sem_colisao(pasta / nome)
    parcial = destino.with_name(destino.name + ".parcial")
    recebido = 0
    try:
        with open(parcial, "wb") as arquivo:
            while recebido < tamanho:
                bloco = tratador.rfile.read(min(1024 * 1024, tamanho - recebido))
                if not bloco:
                    break
                arquivo.write(bloco)
                recebido += len(bloco)
        _exigir(recebido == tamanho, "O envio foi interrompido")
        os.replace(parcial, destino)
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
                painel.motor.rede_atualizada(rede)
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
            painel.motor.rede_atualizada(rede)
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
    ctx.painel.motor.rede_atualizada("instagram")
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
    rede = _rede(rede)
    gravador.arquivo_roteiro(ctx.cfg, rede).unlink(missing_ok=True)
    log.info("%s: roteiro aprendido apagado pelo painel", ROTULOS[rede])
    return {"ok": True}


@rota("GET", r"/redes/(\w+)/roteiro")
def ler_roteiro(ctx: Contexto, rede):
    """O texto do roteiro, para você ver e editar."""
    rede = _rede(rede)
    texto = gravador.carregar(ctx.cfg, rede) or ""
    return {
        "rede": rede,
        "texto": texto,
        "resumo": roteiro.resumo(texto, rede) if texto else None,
        "comandos": roteiro.AJUDA,
        "papeis": sorted(roteiro.PAPEIS_DA_REDE.get(rede, ())),
        "arquivo": str(gravador.arquivo_roteiro(ctx.cfg, rede)),
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
    destino = gravador.arquivo_roteiro(ctx.cfg, rede)
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(texto if texto.endswith("\n") else texto + "\n", encoding="utf-8")
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
    ctx.painel.motor.rede_atualizada(rede)
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
    pastas = {"filmes": cfg.pasta_filmes, "cortes": cfg.pasta_cortes, "logs": cfg.pasta_logs, "dados": cfg.pasta_dados}
    try:
        if alvo == "config":
            sistema.mostrar_arquivo(cfg.caminho)
        elif alvo in pastas:
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
        "ORDER BY CASE status WHEN 'erro' THEN 0 WHEN 'novo' THEN 1 ELSE 2 END, id DESC"
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
    return {
        "pautas": [_pauta_json(cfg, conn, f) for f in linhas],
        "temas": criacao_mod.temas(cfg),
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
    return dados


@rota("POST", r"/pautas/(\d+)")
def salvar_pauta_painel(ctx: Contexto, pauta_id):
    cfg, conn = ctx.cfg, ctx.conn
    f = _pauta(conn, pauta_id)
    texto = str(ctx.corpo.get("texto") or "")
    _exigir(len(texto) <= 20000, "Pauta grande demais")
    caminho = Path(f["caminho"])
    anterior = caminho.read_text(encoding="utf-8-sig") if caminho.is_file() else ""
    temporario = caminho.with_name(caminho.name + ".tmp")
    temporario.write_text(texto.replace("\r\n", "\n"), encoding="utf-8")
    temporario.replace(caminho)
    try:
        criacao.ler_pauta(caminho)
    except criacao.ErroCriacao as e:
        caminho.write_text(anterior, encoding="utf-8")  # volta o que estava lá: pauta inválida não fica salva
        raise ErroHttp(400, str(e)) from e
    criacao.sincronizar(cfg, conn)
    return ler_pauta_painel(ctx, pauta_id)


@rota("POST", r"/pautas/(\d+)/acao")
def acao_pauta(ctx: Contexto, pauta_id):
    cfg, conn = ctx.cfg, ctx.conn
    f = _pauta(conn, pauta_id)
    acao = ctx.corpo.get("acao")
    if acao == "criar_agora":
        _exigir(not TRAVA_CRIACAO.locked(), "Já existe um vídeo sendo criado agora", 409)
        conn.execute("UPDATE filmes SET status='novo', esgotado=0, erro=NULL, tentativas=0, atualizado_em=? "
                     "WHERE id=?", (time.time(), f["id"]))
        _criar_em_segundo_plano(ctx.painel, f["id"])
    elif acao == "escrever_com_ia":
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
        Path(f["caminho"]).unlink(missing_ok=True)
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


TRAVA_CRIACAO = threading.Lock()


def _criar_em_segundo_plano(painel, pauta_id: int) -> None:
    def trabalho():
        if not TRAVA_CRIACAO.acquire(blocking=False):
            return
        conn = painel.conectar()
        try:
            linha = conn.execute("SELECT * FROM filmes WHERE id=?", (pauta_id,)).fetchone()
            if linha is not None:
                ATIVIDADES.definir(f"Criando o vídeo '{linha['titulo']}'")
                criacao.criar_do_banco(painel.cfg, conn, linha, painel.encerrar)
        finally:
            conn.close()
            TRAVA_CRIACAO.release()

    _em_segundo_plano(f"criacao-{pauta_id}", trabalho)


@rota("POST", r"/pautas/temas")
def salvar_temas(ctx: Contexto):
    cfg = ctx.cfg
    texto = str(ctx.corpo.get("texto") or "")
    _exigir(len(texto) <= 20000, "Lista grande demais")
    alvo = criacao.arquivo_temas(cfg)
    alvo.parent.mkdir(parents=True, exist_ok=True)
    temporario = alvo.with_name(alvo.name + ".tmp")
    temporario.write_text(texto.replace("\r\n", "\n").strip() + "\n", encoding="utf-8")
    temporario.replace(alvo)
    return {"temas": criacao.temas(cfg)}


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
                                 "motor": ctx.painel.motor.rodando}
        saida.append(dados)
    return {
        "perfis": saida,
        "instalacao": perfis.instalacao(),
        "conflitos": perfis.conflitos(lista),
        "max": perfis.MAX_PERFIS,
        "abertos": sum(1 for d in saida if d["situacao"]["rodando"]),
    }


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
    dados["situacao"] = perfis.situacao(novo)
    if perfis.situacao(novo)["rodando"] and (ctx.corpo or {}).keys() & {"porta", "porta_navegador"}:
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

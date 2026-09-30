"""Linha de comando: python -m autocortes <comando>."""

from __future__ import annotations

import argparse
import sys
import threading
from datetime import datetime
from pathlib import Path

from . import __version__

AJUDA = """\
Comandos principais:
  painel                abre o painel no navegador com o loop ligado (padrão)
  init                  cria o config.toml e as pastas
  verificar [--online]  confere FFmpeg, whisper, fonte e logins das redes
  auth <rede>           conecta youtube, tiktok ou instagram pelo terminal (Kwai e Bilibili são à mão)
  rodar                 loop contínuo sem painel (modo terminal)
  status                resumo de filmes, cortes e próximas postagens

Comandos manuais:
  baixar                baixa whisper.cpp e modelos agora
  analisar [--filme N]  analisa filmes novos (ou o filme N) sem postar
  renderizar [-n N]     edita os próximos N cortes
  postar <rede>         publica o próximo corte pronto agora, fora da agenda (à mão: cria a tarefa)
  cortes [--filme N]    lista os cortes e notas
  reanalisar N          descarta candidatos do filme N e analisa de novo
"""


def _carregar(args):
    from .config import RAIZ, ErroConfig, carregar, criar_config_se_faltar
    from .util import configurar_log, log

    caminho = Path(args.config).resolve() if args.config else RAIZ / "config.toml"
    criado = False
    if not args.config:
        criado = criar_config_se_faltar(caminho)
    try:
        cfg = carregar(caminho)
    except ErroConfig as e:
        print(e, file=sys.stderr)
        raise SystemExit(2) from e
    configurar_log(cfg.pasta_logs, args.detalhado)
    if criado:
        log.info("config.toml criado a partir do exemplo (modo simulação ligado).")
    return cfg


def _conn(cfg):
    from . import db

    return db.conectar(cfg.banco)


# ---------------------------------------------------------------- comandos

def cmd_init(args) -> int:
    cfg = _carregar(args)
    for pasta in (cfg.pasta_filmes, cfg.pasta_dados, cfg.pasta_modelos):
        pasta.mkdir(parents=True, exist_ok=True)
    print(f"Configuração: {cfg.caminho}")
    print(f"Coloque os filmes em: {cfg.pasta_filmes}")
    print("Depois rode: python -m autocortes verificar")
    return 0


def cmd_verificar(args) -> int:
    from . import ferramentas
    from .config import PLATAFORMAS
    from .midia import ErroMidia, filtros_disponiveis, versao_ffmpeg
    from .plataformas import ErroPublicacao, criar
    from .produtor import EXTENSOES

    cfg = _carregar(args)
    problemas = 0

    def linha(ok: bool | None, texto: str) -> None:
        nonlocal problemas
        marca = {True: "[ OK ]", False: "[FALHA]", None: "[ -- ]"}[ok]
        if ok is False:
            problemas += 1
        print(f"{marca} {texto}")

    print(f"AutoCortes {__version__}  |  modo: {'SIMULAÇÃO' if cfg.simulacao else 'REAL'}  |  {cfg.caminho}\n")
    try:
        linha(True, versao_ffmpeg(cfg["ferramentas"]["ffmpeg"]))
        filtros = filtros_disponiveis(cfg["ferramentas"]["ffmpeg"])
        faltando = {"ass", "scdet", "cropdetect", "astats", "loudnorm", "boxblur"} - filtros
        linha(not faltando, "Filtros do FFmpeg" + (f" faltando: {', '.join(sorted(faltando))}" if faltando else ""))
    except ErroMidia as e:
        linha(False, f"FFmpeg: {e}")
    try:
        versao_ffmpeg(cfg["ferramentas"]["ffprobe"])
        linha(True, "FFprobe")
    except ErroMidia as e:
        linha(False, f"FFprobe: {e}")

    cli, vad = ferramentas.caminhos_whisper(cfg)
    if cfg["transcricao"]["fonte"] == "nenhuma":
        linha(None, "Transcrição desligada")
    else:
        linha(True if cli.exists() and vad.exists() else None,
              f"whisper.cpp: {cli if cli.exists() else 'será baixado no primeiro uso (ou rode: baixar)'}")
        modelo = ferramentas.caminho_modelo(cfg)
        linha(True if modelo.exists() else None,
              f"Modelo '{cfg['transcricao']['modelo']}': {modelo if modelo.exists() else 'será baixado no primeiro uso'}")

    fonte = Path(str(cfg["edicao"]["fonte_arquivo"]))
    linha(fonte.exists(), f"Fonte das legendas: {fonte}")

    filmes = [p for p in cfg.pasta_filmes.rglob("*") if p.suffix.lower() in EXTENSOES] if cfg.pasta_filmes.exists() else []
    linha(True if filmes else None, f"Pasta de filmes {cfg.pasta_filmes}: {len(filmes)} vídeo(s)")

    print()
    for nome in PLATAFORMAS:
        if not cfg[nome]["ativo"]:
            linha(None, f"{nome}: desativado")
            continue
        if cfg[nome]["envio"] == "manual":
            linha(None, f"{nome}: postagem à mão (as tarefas aparecem no Início do painel)")
            continue
        plataforma = criar(nome, cfg)
        if cfg[nome]["envio"] == "navegador" and not args.online:
            pronta, motivo = plataforma.pronta()
            linha(True if pronta else None, f"{nome}: pelo navegador ({'sessão salva' if pronta else motivo})")
            continue
        pronta, motivo = plataforma.pronta()
        if not pronta:
            linha(None if cfg.simulacao else False, f"{nome}: {motivo}")
            continue
        if args.online:
            try:
                linha(True, f"{nome}: {plataforma.verificar()}")
            except ErroPublicacao as e:
                linha(False, f"{nome}: {e}")
        else:
            linha(True, f"{nome}: login salvo (use --online para testar)")
    print()
    if cfg.simulacao:
        print("Modo simulação: os vídeos são gerados, mas nada é enviado. Troque [geral].simulacao para false quando quiser publicar.")
    return 1 if problemas else 0


def cmd_baixar(args) -> int:
    from . import ferramentas

    cfg = _carregar(args)
    ferramentas.garantir_whisper(cfg)
    ferramentas.garantir_vad(cfg)
    ferramentas.garantir_modelo(cfg)
    print("Ferramentas de transcrição prontas.")
    return 0


def cmd_auth(args) -> int:
    from .plataformas import ErroPublicacao, criar

    cfg = _carregar(args)
    try:
        criar(args.rede, cfg).autorizar()
    except ErroPublicacao as e:
        print(f"Erro: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelado.")
        return 1
    return 0


def cmd_rodar(args) -> int:
    from .loop import rodar
    from .modelos_visuais import conciliar_config

    cfg = _carregar(args)
    conciliar_config(cfg)  # nome do modelo visual trocado à mão no config.toml
    return rodar(cfg)


def cmd_painel(args) -> int:
    from .painel.servidor import iniciar_painel

    cfg = _carregar(args)
    abrir = cfg["painel"]["abrir_navegador"] and not getattr(args, "sem_navegador", False)
    return iniciar_painel(
        cfg,
        abrir_navegador=abrir,
        iniciar_motor=not getattr(args, "motor_desligado", False),
        porta=getattr(args, "porta", None),
    )


def cmd_status(args) -> int:
    from . import agenda
    from .config import PLATAFORMAS
    from .loop import resumo
    from .plataformas import criar
    from .util import fmt_data, fmt_tempo

    cfg = _carregar(args)
    conn = _conn(cfg)
    print(f"Modo: {'SIMULAÇÃO' if cfg.simulacao else 'REAL'}\n")
    filmes = conn.execute("SELECT * FROM filmes ORDER BY id").fetchall()
    print(f"Filmes ({len(filmes)}):")
    for f in filmes:
        cont = dict(conn.execute("SELECT status, COUNT(*) FROM cortes WHERE filme_id=? GROUP BY status", (f["id"],)).fetchall())
        extra = f" erro: {f['erro']}" if f["status"] == "erro" else ""
        duracao = fmt_tempo(f["duracao"]) if f["duracao"] else "?"
        print(
            f"  #{f['id']:<3} {f['titulo'][:40]:<40} {f['status']:<10} {duracao:>8}  "
            f"candidatos {cont.get('candidato', 0)}, prontos {cont.get('pronto', 0)}, concluídos {cont.get('concluido', 0)}{extra}"
        )
    print("\nRedes:")
    agora = datetime.now()
    for nome in PLATAFORMAS:
        if not cfg[nome]["ativo"]:
            print(f"  {nome:<10} desativado")
            continue
        pronta, motivo = criar(nome, cfg).pronta()
        h = agenda.proximo_horario(cfg, nome, agora)
        n24 = conn.execute(
            "SELECT COUNT(*) FROM postagens WHERE plataforma=? AND status IN ('publicado','simulado') AND criado_em>=?",
            (nome, agora.timestamp() - 86400),
        ).fetchone()[0]
        if cfg[nome]["envio"] == "manual":
            esperando = conn.execute(
                "SELECT COUNT(*) FROM postagens WHERE plataforma=? AND status='aguardando'", (nome,)
            ).fetchone()[0]
            situacao = f"à mão: {esperando} tarefa(s) esperando"
        elif cfg[nome]["envio"] == "navegador":
            situacao = f"pelo navegador: {'sessão salva' if pronta else motivo}"
        else:
            situacao = "login ok" if pronta else motivo
        print(f"  {nome:<10} {situacao:<45} próximo: {h.strftime('%d/%m %H:%M') if h else '-'}  últimas 24h: {n24}")
    print("\nÚltimas postagens:")
    for p in conn.execute(
        "SELECT p.*, c.parte, f.titulo FROM postagens p JOIN cortes c ON c.id=p.corte_id JOIN filmes f ON f.id=c.filme_id "
        "ORDER BY p.id DESC LIMIT 10"
    ):
        detalhe = p["url"] or p["erro"] or ""
        print(f"  {fmt_data(p['criado_em'])} {p['plataforma']:<10} {p['status']:<12} {p['titulo'][:25]} parte {p['parte']}  {detalhe[:90]}")
    print("\n" + resumo(cfg, conn))
    return 0


def cmd_analisar(args) -> int:
    from .produtor import Produtor, varrer_biblioteca

    cfg = _carregar(args)
    conn = _conn(cfg)
    varrer_biblioteca(cfg, conn)
    produtor = Produtor(cfg, threading.Event())
    if args.filme:
        filmes = conn.execute("SELECT * FROM filmes WHERE id=?", (args.filme,)).fetchall()
    else:
        filmes = conn.execute("SELECT * FROM filmes WHERE status='novo' ORDER BY id").fetchall()
    if not filmes:
        print("Nenhum filme novo para analisar (coloque vídeos em %s)." % cfg.pasta_filmes)
        return 0
    for f in filmes:
        n = produtor.analisar(conn, f)
        print(f"#{f['id']} {f['titulo']}: {n} cortes candidatos")
    return 0


def cmd_reanalisar(args) -> int:
    import shutil

    from .analise import pasta_do_filme

    cfg = _carregar(args)
    conn = _conn(cfg)
    filme = conn.execute("SELECT * FROM filmes WHERE id=?", (args.filme,)).fetchone()
    if not filme:
        print(f"Filme #{args.filme} não existe.")
        return 1
    conn.execute("DELETE FROM cortes WHERE filme_id=? AND status='candidato'", (filme["id"],))
    pasta = pasta_do_filme(cfg, filme["id"])
    if args.tudo:
        shutil.rmtree(pasta, ignore_errors=True)
    else:  # mantém cenas/volume/fala, refaz só as legendas (igual ao botão do painel)
        for nome in ("transcricao.json", "legenda.json"):
            (pasta / nome).unlink(missing_ok=True)
    conn.execute("UPDATE filmes SET status='novo', erro=NULL, tentativas=0, esgotado=0 WHERE id=?", (filme["id"],))
    print(f"'{filme['titulo']}' volta para a fila de análise.")
    return 0


def cmd_renderizar(args) -> int:
    from .produtor import Produtor

    cfg = _carregar(args)
    conn = _conn(cfg)
    produtor = Produtor(cfg, threading.Event())
    feitos = 0
    for _ in range(args.n):
        corte = produtor.proximo_candidato(conn)
        if corte is None:
            print("Não há cortes candidatos (rode: analisar).")
            break
        if produtor.renderizar(conn, corte):
            feitos += 1
    print(f"{feitos} corte(s) editado(s) em {cfg.pasta_cortes}")
    return 0


def cmd_postar(args) -> int:
    import threading as th

    from .publicador import Publicador

    cfg = _carregar(args)
    if args.real:
        cfg["geral"]["simulacao"] = False
    conn = _conn(cfg)
    publicador = Publicador(cfg, th.Event())
    if args.corte:
        corte = conn.execute("SELECT * FROM cortes WHERE id=? AND arquivo IS NOT NULL", (args.corte,)).fetchone()
    else:
        corte = publicador.proximo_corte(conn, args.rede)
    if corte is None:
        print("Nenhum corte pronto para essa rede (rode: renderizar).")
        return 1
    status = publicador.postar(conn, args.rede, corte, manual=True)  # fora da agenda, como o "postar agora"
    if status == "aguardando":
        print("Resultado: tarefa de postagem à mão criada (veja o vídeo e os textos no Início do painel)")
        return 0
    print(f"Resultado: {status}")
    return 0 if status in ("publicado", "simulado") else 1


def cmd_cortes(args) -> int:
    import json

    from .util import fmt_tempo

    cfg = _carregar(args)
    conn = _conn(cfg)
    sql = "SELECT c.*, f.titulo FROM cortes c JOIN filmes f ON f.id=c.filme_id"
    params: tuple = ()
    if args.filme:
        sql += " WHERE c.filme_id=?"
        params = (args.filme,)
    sql += " ORDER BY c.filme_id, c.pontuacao DESC LIMIT ?"
    for c in conn.execute(sql, params + (args.limite,)):
        det = json.loads(c["detalhes"] or "{}")
        print(
            f"#{c['id']:<5} {c['titulo'][:22]:<22} {fmt_tempo(c['inicio']):>8}-{fmt_tempo(c['fim']):<8} "
            f"nota {c['pontuacao']:.2f} {c['status']:<11} parte {c['parte'] or '-':<3} "
            f"fala {det.get('fala', 0):.2f} picos {det.get('picos', 0):.2f}  {(c['frase'] or '')[:50]}"
        )
    return 0


# ---------------------------------------------------------------- entrada

def main(argv: list[str] | None = None) -> int:
    if sys.version_info < (3, 11):
        print("O AutoCortes precisa do Python 3.11 ou mais novo.", file=sys.stderr)
        return 2
    from .config import PLATAFORMAS
    from .util import preparar_console

    preparar_console()
    parser = argparse.ArgumentParser(
        prog="python -m autocortes",
        description="Corta filmes, edita em formato vertical e posta nas redes em loop.",
        epilog=AJUDA,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", help="caminho de outro config.toml")
    parser.add_argument("--detalhado", action="store_true", help="log com detalhes (comandos do FFmpeg etc.)")
    parser.add_argument("--version", action="version", version=f"AutoCortes {__version__}")
    sub = parser.add_subparsers(dest="comando", metavar="comando")

    p = sub.add_parser("painel", help="abre o painel no navegador (padrão)")
    p.add_argument("--sem-navegador", action="store_true", help="não abre o navegador")
    p.add_argument("--motor-desligado", action="store_true", help="abre o painel com o loop desligado")
    p.add_argument("--porta", type=int, help="porta do painel (padrão: [painel].porta)")
    p.set_defaults(func=cmd_painel)
    sub.add_parser("init", help="cria config.toml e pastas").set_defaults(func=cmd_init)
    p = sub.add_parser("verificar", help="confere a instalação e os logins")
    p.add_argument("--online", action="store_true", help="testa os logins nas APIs")
    p.set_defaults(func=cmd_verificar)
    sub.add_parser("baixar", help="baixa whisper.cpp e modelos").set_defaults(func=cmd_baixar)
    p = sub.add_parser("auth", help="conecta uma rede social")
    p.add_argument("rede", choices=list(PLATAFORMAS))
    p.set_defaults(func=cmd_auth)
    sub.add_parser("rodar", help="loop contínuo").set_defaults(func=cmd_rodar)
    sub.add_parser("status", help="resumo geral").set_defaults(func=cmd_status)
    p = sub.add_parser("analisar", help="analisa filmes novos agora")
    p.add_argument("--filme", type=int, help="id do filme (veja em status)")
    p.set_defaults(func=cmd_analisar)
    p = sub.add_parser("reanalisar", help="refaz a análise de um filme")
    p.add_argument("filme", type=int)
    p.add_argument("--tudo", action="store_true", help="apaga também cenas/volume/fala em cache")
    p.set_defaults(func=cmd_reanalisar)
    p = sub.add_parser("renderizar", help="edita os próximos cortes agora")
    p.add_argument("-n", type=int, default=1)
    p.set_defaults(func=cmd_renderizar)
    p = sub.add_parser("postar", help="publica o próximo corte agora (redes à mão: cria a tarefa)")
    p.add_argument("rede", choices=list(PLATAFORMAS))
    p.add_argument("--corte", type=int, help="id de um corte específico")
    p.add_argument("--real", action="store_true", help="publica de verdade mesmo com simulacao = true")
    p.set_defaults(func=cmd_postar)
    p = sub.add_parser("cortes", help="lista cortes")
    p.add_argument("--filme", type=int)
    p.add_argument("--limite", type=int, default=50)
    p.set_defaults(func=cmd_cortes)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args.func = cmd_painel  # sem comando: abre o painel
    elif args.func is cmd_postar and args.real:
        print("Atenção: --real publica de verdade mesmo com o modo simulação ligado.")
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\nInterrompido.")
        return 130

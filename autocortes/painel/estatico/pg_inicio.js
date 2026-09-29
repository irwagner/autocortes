/* AutoCortes - painel: Início */
"use strict";

const ICONE_ALERTA = { erro: "erro", aviso: "alerta", info: "info" };

function atributosAbrirCorte(corteId) {
  return corteId ? h`data-acao="abrir-corte" data-corte="${corteId}" role="button" tabindex="0"` : "";
}

function alertasHtml(alertas) {
  if (!alertas || !alertas.length) return "";
  return h`<div class="card alertas"><div class="lista">${alertas.map((a) => {
    let attrs = "";
    if (a.destino === "motor") attrs = h`data-acao="motor-ligar" role="button" tabindex="0"`;
    else if (a.destino) attrs = h`data-acao="ir" data-destino="${a.destino}" role="button" tabindex="0"`;
    return h`<div class="alerta-item ${a.nivel}${a.destino ? " clicavel" : ""}" ${attrs}>${ic(ICONE_ALERTA[a.nivel] || "info")}<span>${a.texto}</span>${a.destino ? ic("seta-dir") : ""}</div>`;
  })}</div></div>`;
}

function kpi(cor, icone, rotulo, valorTexto, sub) {
  return h`<div class="card kpi" style="--cor-kpi:${cor}"><div class="kpi-topo"><span class="kpi-ic">${ic(icone)}</span>${rotulo}</div><div class="kpi-valor">${valorTexto}</div><div class="kpi-sub" title="${sub}">${sub}</div></div>`;
}

function kpisHtml(e) {
  const n = e.numeros;
  const c = e.cobertura;
  const filaSub = n.revisao
    ? `${plural(n.revisao, "corte aguardando", "cortes aguardando")} aprovação`
    : plural(n.candidatos, "trecho encontrado para editar", "trechos encontrados para editar");
  const baixo = c.dias !== null && c.dias < 3;
  const hojeSub = [...ORDEM_REDES, "facebook"].filter((r) => n.hoje_por_rede[r]).map((r) => `${REDES[r].rotulo} ${n.hoje_por_rede[r]}`).join(" · ")
    || (e.simulacao ? "no modo simulação" : "nenhum ainda");
  let filmesSub = "todos analisados";
  if (!n.filmes) filmesSub = "adicione filmes para começar";
  else if (n.filmes_na_fila) filmesSub = `${n.filmes_na_fila} na fila de análise`;
  else if (n.filmes_erro) filmesSub = `${n.filmes_erro} com erro na análise`;
  return [
    kpi("#ffb627", "tesoura", "Na fila", fmtNum(n.na_fila), filaSub),
    kpi(baixo ? "#f87171" : "#34d399", "calendario", "Conteúdo para", c.dias === null ? "—" : fmtDias(c.dias),
      c.consumo_diario ? `${fmtNum(c.consumo_diario, 1)} cortes novos por dia` : "nenhuma rede ativa"),
    kpi("#60a5fa", "aviao", e.simulacao ? "Simulados hoje" : "Posts hoje", fmtNum(n.hoje), hojeSub),
    kpi("#a78bfa", "filme", "Filmes", fmtNum(n.filmes), filmesSub),
  ];
}

function proximoHtml(i) {
  const titulo = i.filme ? `${i.filme} · Parte ${i.parte}` : "Sem corte pronto ainda";
  let detalhe = "";
  if (i.previsto) detalhe = "será editado antes do horário";
  else if (i.sem_corte) detalhe = "adicione filmes para não perder o horário";
  const estado = i.estado === "agendado" ? null : ESTADO_HORARIO[i.estado];
  return h`<div class="item-post${i.corte_id ? " clicavel" : ""}" ${atributosAbrirCorte(i.corte_id)}>${miniaturaHtml(i.miniatura, i.sem_corte ? "alerta" : "tesoura")}<div class="item-texto"><b>${titulo}</b><small>${logoRede(i.rede)}<span>${REDES[i.rede].rotulo}${detalhe ? ` · ${detalhe}` : ""}</span></small></div><div class="item-lado">${estado ? badge(estado, true) : h`<small>${fmtRelativo(i.quando)}</small>`}<small>${fmtQuando(i.quando)}</small></div></div>`;
}

function proximosHtml(e) {
  const ativas = ORDEM_REDES.filter((r) => e.redes[r] && e.redes[r].ativo);
  if (!ativas.length) return vazioBloco("rede", "Nenhuma rede ativa", "Ative pelo menos uma rede em Redes sociais.");
  if (!e.proximos.length) return vazioBloco("calendario", "Nada agendado nos próximos dias", "Confira os horários e os dias da semana na Agenda.");
  return h`<div class="lista">${e.proximos.map(proximoHtml)}</div>`;
}

function iconeAtividade(a) {
  if (a.pct !== null && a.pct !== undefined) return h`<span class="giro"></span>`;
  if (/^Estoque completo/.test(a.texto)) return ic("ok");
  if (/^(Sem trechos|Erro)/.test(a.texto)) return ic("alerta");
  return ic("relogio");
}

function agoraHtml(e) {
  const itens = e.atividades || [];
  const motor = e.motor.rodando && e.motor.desde
    ? h`<p class="nota rodape-card">Motor ligado ${fmtRelativo(e.motor.desde)}${e.pausado ? " · postagens pausadas" : ""}.</p>`
    : "";
  if (!e.motor.rodando && !itens.length) {
    return h`${vazioBloco("pausa", "Motor desligado", "Ligue o motor para analisar, editar e postar.")}<div class="centro"><button class="btn primario" data-acao="motor-ligar">${ic("play")}Ligar motor</button></div>`;
  }
  if (!itens.length) return h`${vazioBloco("ok", "Tudo em dia", "Nada sendo processado agora.")}${motor}`;
  return h`<div class="lista">${itens.map((a) => h`<div class="atividade"><div class="atv-topo">${iconeAtividade(a)}<span class="texto">${a.texto}</span><small>${fmtRelativo(a.desde)}</small></div>${a.pct !== null && a.pct !== undefined ? h`<div class="atv-barra"><div class="barra"><i style="width:${a.pct}%"></i></div><small>${Math.round(a.pct)}%</small></div>` : ""}</div>`)}</div>${motor}`;
}

function ultimaHtml(p) {
  const falhou = !["publicado", "simulado", "aguardando"].includes(p.estado);
  const rede = REDES[p.rede] || { rotulo: p.rede };
  return h`<div class="item-post clicavel" ${atributosAbrirCorte(p.corte_id)}>${miniaturaHtml(p.miniatura)}<div class="item-texto"><b>${p.filme} · Parte ${p.parte}</b><small>${logoRede(p.rede)}<span class="${falhou && p.erro ? "erro-texto" : ""}">${rede.rotulo}${p.manual ? " · fora da agenda" : ""}${p.erro ? ` · ${p.erro}` : ""}</span></small></div><div class="item-lado">${badge(estadoPost(p.estado), true)}<small>${fmtRelativo(p.quando)}</small></div></div>`;
}

/* ------------------------------------------------------------ postagem à mão */

function tarefaItemHtml(t) {
  const quando = t.horario ? `horário das ${fmtHora(t.horario)}` : "pedida por você";
  return h`<div class="item-post clicavel" data-acao="abrir-tarefa" data-tarefa="${t.id}" role="button" tabindex="0" aria-label="Postar ${t.filme}, parte ${t.parte}, no ${REDES[t.rede].rotulo}">${miniaturaHtml(t.miniatura)}<div class="item-texto"><b>${t.filme} · Parte ${t.parte}</b><small>${logoRede(t.rede)}<span>${REDES[t.rede].rotulo} · ${quando}</span></small></div><div class="item-lado"><small>${fmtRelativo(t.quando)}</small><span class="link">Postar${ic("seta-dir")}</span></div></div>`;
}

function tarefasHtml(e) {
  const lista = e.tarefas || [];
  if (!lista.length) return "";
  const total = e.numeros.tarefas || lista.length;
  return h`<div class="card tarefas-card"><div class="card-topo"><h3>${ic("lista")}Para postar à mão</h3>${badge([plural(total, "tarefa", "tarefas"), "acento"], true)}</div>
    <p class="nota">Estas redes não postam sozinhas. Abra a tarefa, baixe o vídeo, copie os textos, poste no app ou no site e marque "Já postei".</p>
    <div class="lista">${lista.map(tarefaItemHtml)}</div></div>`;
}

const Tarefa = { id: null, dados: null };

function campoTarefaHtml(c, i) {
  if (c.lista) { // tags que a rede pede uma por vez: cada uma se copia sozinha
    return h`<div class="campo campo-tarefa"><span class="campo-topo"><b>${c.rotulo}</b><small class="texto-suave">${plural(c.lista.length, "tag", "tags")}</small><button type="button" class="btn pequeno" data-acao="copiar" data-texto="${c.texto}">${ic("copiar")}Copiar todas</button></span>
      <div class="tags-copiar">${c.lista.map((t) => h`<button type="button" class="tag-copiar" data-acao="copiar" data-texto="${t}" title="Copiar ${t}">${t}${ic("copiar")}</button>`)}</div>${c.nota ? h`<small class="texto-suave">${c.nota}</small>` : ""}</div>`;
  }
  const excedeu = c.limite && c.tamanho > c.limite;
  const linhas = Math.min(8, Math.max(1, Math.ceil(c.texto.length / 60) + (c.texto.match(/\n/g) || []).length));
  return h`<div class="campo campo-tarefa"><span class="campo-topo"><b>${c.rotulo}</b><small class="${excedeu ? "erro-texto" : "texto-suave"}">${c.limite ? `${fmtNum(c.tamanho)}/${fmtNum(c.limite)}` : plural(c.tamanho, "caractere", "caracteres")}</small><button type="button" class="btn pequeno" data-acao="copiar-campo" data-alvo="tarefa-campo-${i}">${ic("copiar")}Copiar</button></span>
    <textarea id="tarefa-campo-${i}" rows="${linhas}" readonly spellcheck="false" aria-label="${c.rotulo}">${c.texto}</textarea>${c.nota ? h`<small class="texto-suave">${c.nota}</small>` : ""}</div>`;
}

function tarefaHtml(t) {
  const info = REDES[t.rede];
  const aberta = t.estado === "aguardando";
  const video = t.video
    ? h`<video src="${midia(t.video)}" ${t.miniatura ? h`poster="${midia(t.miniatura)}"` : ""} controls playsinline preload="metadata"></video>`
    : h`<div class="capa-vazia">${ic(t.editando ? "refazer" : "alerta")}<small>${t.editando ? "O vídeo está sendo editado de novo" : "O vídeo deste corte não está mais na pasta"}</small></div>`;
  const baixar = t.video
    ? h`<a class="btn primario" href="${midia(t.video)}" download="${t.arquivo || "corte.mp4"}" autofocus>${ic("baixar")}Baixar o vídeo</a>`
    : "";
  const site = t.site && urlSegura(t.site.url)
    ? h`<a class="btn" href="${t.site.url}" target="_blank" rel="noopener noreferrer">${ic("externo")}${t.site.rotulo}</a>`
    : h`<span class="nota">Só pelo app do celular.</span>`;
  const quando = t.horario ? `Horário das ${fmtHora(t.horario)} (${rotuloDia(t.horario)})` : `Pedida por você ${fmtRelativo(t.quando)}`;
  const fim = aberta
    ? h`<div class="rotulo-campo">Depois de postar</div>
      <label class="campo"><span>Link do post (opcional)</span><input type="url" id="tarefa-url" placeholder="https://..." spellcheck="false" autocomplete="off"></label>
      <div class="linha-botoes"><button class="btn sucesso" data-acao="tarefa-feito">${ic("check")}Já postei</button><button class="btn fantasma" data-acao="tarefa-pular">Pular esta postagem</button></div>`
    : h`<p class="nota">${badge(estadoPost(t.estado), true)} ${t.url && urlSegura(t.url) ? link(t.url, "Abrir o post") : t.erro || ""}</p>`;
  return h`<div class="detalhe-corte tarefa">
    <div class="tarefa-lado"><div class="detalhe-video">${video}</div>${baixar}${t.copia ? h`<p class="nota">Também está na pasta sincronizada: <code class="caminho">${t.copia}</code></p>` : ""}</div>
    <div class="detalhe-info">
      <div class="detalhe-cabeca"><h3>${logoRede(t.rede)} ${info.nome} · ${t.filme} · Parte ${t.parte}</h3></div>
      <div class="detalhe-meta">${quando} · ${fmtDuracao(t.duracao)}</div>
      <div class="rotulo-campo">Como postar</div>
      <ol class="passos">${t.passos.map((p) => h`<li>${p}</li>`)}</ol>
      <div class="linha-botoes">${site}<button class="btn fantasma pequeno" data-acao="abrir-corte" data-corte="${t.corte_id}">${ic("lapis")}Editar os textos do corte</button></div>
      <div class="rotulo-campo">Textos</div>
      ${t.campos.map(campoTarefaHtml)}
      ${fim}
      <p class="nota">O vídeo tem trechos de um filme de terceiros: poste só o que você tem direito de usar.</p>
    </div>
  </div>`;
}

async function abrirTarefa(id) {
  if (!id) return;
  Tarefa.id = id;
  abrirModal({
    titulo: "Postagem à mão",
    largura: 1000,
    corpo: h`<div class="carregando-bloco"><span class="giro"></span></div>`,
    aoFechar: () => { Tarefa.id = null; Tarefa.dados = null; },
  });
  let t;
  try {
    t = await api(`/tarefas/${id}`);
  } catch (e) {
    if (Tarefa.id === id) montar(corpoModal(), vazioBloco("erro", "Não foi possível abrir a tarefa", e.message));
    return;
  }
  if (Tarefa.id !== id || !corpoModal()) return;
  Tarefa.dados = t;
  tituloModal(`Postar no ${REDES[t.rede].rotulo}`);
  montar(corpoModal(), tarefaHtml(t));
  const foco = $("#modal-raiz [autofocus]") || $("#modal-raiz [data-acao=fechar-modal]");
  if (foco) foco.focus({ preventScroll: true }); // o vídeo fica à vista, mesmo em tela estreita
}

App.acoes["abrir-tarefa"] = (el) => abrirTarefa(Number(el.dataset.tarefa));

App.acoes["copiar-campo"] = async (el) => {
  const alvo = document.getElementById(el.dataset.alvo);
  if (!alvo) return;
  try {
    await navigator.clipboard.writeText(alvo.value);
    toast("Copiado");
  } catch (e) {
    alvo.focus();
    alvo.select();
    toast("Não consegui copiar sozinho. O texto ficou selecionado: use Ctrl+C.", "erro");
  }
};

App.acoes["tarefa-feito"] = async (el) => {
  const t = Tarefa.dados;
  if (!t) return;
  const url = ($("#tarefa-url") && $("#tarefa-url").value.trim()) || "";
  await ocupado(el, () => post(`/tarefas/${t.id}/feito`, { url }));
  fecharModal();
  toast(`${REDES[t.rede].rotulo}: marcado como postado`);
  atualizarEstado();
  if (App.pagina && App.pagina.recarregar && App.nomePagina !== "inicio") App.pagina.recarregar();
};

App.acoes["tarefa-pular"] = async (el) => {
  const t = Tarefa.dados;
  if (!t) return;
  const ok = await confirmar({ titulo: "Pular esta postagem?", texto: `O corte não vai para o ${REDES[t.rede].rotulo}. O próximo horário recebe o corte seguinte da fila.`, botao: "Pular" });
  if (!ok) return;
  await ocupado(el, () => post(`/tarefas/${t.id}/pular`));
  fecharModal();
  toast("Postagem pulada", "info");
  atualizarEstado();
  if (App.pagina && App.pagina.recarregar && App.nomePagina !== "inicio") App.pagina.recarregar();
};

function ultimasHtml(e) {
  if (!e.ultimas.length) {
    return vazioBloco("aviao", "Nenhuma postagem ainda", e.simulacao ? "No modo simulação, as postagens aparecem aqui como simuladas." : "");
  }
  return h`<div class="lista">${e.ultimas.map(ultimaHtml)}</div>`;
}

function desempenhoHtml(d) {
  if (!d || !d.tem_dados) {
    return vazioBloco("grafico", "Sem métricas ainda", "Visualizações e curtidas aparecem aqui depois dos primeiros posts publicados no YouTube e no Instagram pela API oficial.");
  }
  const linhas = ORDEM_REDES.filter((r) => d.por_rede[r] && d.por_rede[r].com_metricas).map((r) => {
    const v = d.por_rede[r];
    return h`<div class="metrica-linha">${logoRede(r)}<span>${REDES[r].rotulo}</span><b>${fmtNum(v.visualizacoes)}</b><small>visualizações</small><b>${fmtNum(v.curtidas)}</b><small>curtidas</small></div>`;
  });
  const melhores = (d.melhores || []).map((m) => h`<div class="item-post clicavel" ${atributosAbrirCorte(m.corte_id)}><div class="item-texto"><b>${m.filme} · Parte ${m.parte}</b><small>${fmtNum(m.curtidas)} curtidas</small></div><div class="item-lado"><b>${fmtNum(m.visualizacoes)}</b><small>visualizações</small></div></div>`);
  return h`<div class="metricas">${linhas}</div>${melhores.length ? h`<div class="rotulo-campo">Cortes com mais visualizações (30 dias)</div><div class="lista">${melhores}</div>` : ""}`;
}

function redeResumoHtml(rede, r, e) {
  const info = REDES[rede];
  let estado;
  if (!r.ativo) estado = ["Desativada", "neutro"];
  else if (r.bloqueada) estado = ["Pausada por erro", "erro"];
  else if (r.via === "manual") estado = r.tarefas ? [plural(r.tarefas, "tarefa", "tarefas"), "acento"] : ["À mão", "info"];
  else if (r.pronta) estado = ["Conectada", "ok"];
  else estado = [e.simulacao ? "Falta conectar" : "Não conectada", e.simulacao ? "aviso" : "erro"];
  const via = ROTULO_ENVIO[r.via] || r.via;
  const extra = r.facebook && r.facebook.ativo ? " · + Página do Facebook" : "";
  return h`<a class="card rede-mini" href="#/redes" style="--cor:${info.cor}"><div class="rede-titulo">${logoRede(rede)}<div><b>${info.nome}</b><small title="${via}${r.conta ? ` · ${r.conta}` : ""}${extra}">${via}${r.conta ? ` · ${r.conta}` : ""}${extra}</small></div></div><div class="rede-mini-linha">${badge(estado, true)}<small>${r.ativo && r.proximo ? `próximo: ${fmtQuando(r.proximo)}` : ""}</small></div><small class="texto-suave">${r.ativo ? `${r.por_semana} posts por semana` : "Ative em Redes sociais"}</small></a>`;
}

App.paginas.inicio = {
  titulo: "Início",
  subtitulo: "O que o AutoCortes está fazendo e o que vem por aí",
  async render(el, token) {
    if (!App.estado) await atualizarEstado();
    if (!App.ativa(token)) return;
    this.el = el;
    if (!App.estado) {
      montar(el, vazioBloco("alerta", "Sem conexão com o AutoCortes", "Confira se ele ainda está aberto.", true));
      return;
    }
    montar(el, h`<div id="ini-alertas"></div>
      <div id="ini-tarefas"></div>
      <div class="grade-kpi" id="ini-kpis"></div>
      <div class="grade-2">
        <div class="card"><div class="card-topo"><h3>${ic("calendario")}Próximas postagens</h3><a class="link" href="#/agenda">Ver a agenda${ic("seta-dir")}</a></div><div id="ini-proximos"></div></div>
        <div class="card"><div class="card-topo"><h3>${ic("raio")}Agora</h3></div><div id="ini-agora"></div></div>
      </div>
      <div class="grade-2">
        <div class="card"><div class="card-topo"><h3>${ic("aviao")}Últimas postagens</h3><a class="link" href="#/cortes">Ver os cortes${ic("seta-dir")}</a></div><div id="ini-ultimas"></div></div>
        <div class="card"><div class="card-topo"><h3>${ic("grafico")}Desempenho nos últimos 7 dias</h3></div><div id="ini-desempenho"></div></div>
      </div>
      <div class="grade-redes" id="ini-redes"></div>`);
    this.desenhar(App.estado);
  },
  aoEstado(e) {
    if (this.el && this.el.isConnected && $("#ini-kpis")) this.desenhar(e);
  },
  desenhar(e) {
    montarSeMudou($("#ini-alertas"), alertasHtml(e.alertas));
    montarSeMudou($("#ini-tarefas"), tarefasHtml(e));
    montarSeMudou($("#ini-kpis"), kpisHtml(e));
    montarSeMudou($("#ini-proximos"), proximosHtml(e));
    montarSeMudou($("#ini-agora"), agoraHtml(e));
    montarSeMudou($("#ini-ultimas"), ultimasHtml(e));
    montarSeMudou($("#ini-desempenho"), desempenhoHtml(e.desempenho));
    montarSeMudou($("#ini-redes"), ORDEM_REDES.map((r) => redeResumoHtml(r, e.redes[r], e)));
  },
  recarregar() {
    atualizarEstado();
  },
};

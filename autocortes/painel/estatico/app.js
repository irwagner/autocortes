/* AutoCortes - painel: menu, navegação, estado ao vivo, eventos e ações globais (carregado por último) */
"use strict";

const MENU = [
  ["inicio", "Início", "casa"],
  ["agenda", "Agenda", "calendario"],
  ["cortes", "Cortes", "tesoura"],
  ["filmes", "Filmes", "filme"],
  ["estudio", "Estúdio", "varinha"],
  ["redes", "Redes sociais", "rede"],
  ["config", "Configurações", "ajustes"],
  ["registro", "Registro", "terminal"],
];

const Estado = { timer: null, emCurso: null, repetir: false, sessaoInvalida: false };
let paginaFalhou = false;
let inicioClique = null;

/* ------------------------------------------------------------ menu, lateral e topo */

function contadorMenu(id, e) {
  if (!e) return null;
  const n = e.numeros;
  if (id === "inicio" && n.tarefas) {
    return { n: n.tarefas, titulo: `${plural(n.tarefas, "postagem", "postagens")} à mão esperando você` };
  }
  if (id === "cortes" && n.revisao) {
    return { n: n.revisao, titulo: `${plural(n.revisao, "corte aguardando", "cortes aguardando")} aprovação` };
  }
  if (id === "filmes") {
    if (n.filmes_erro) return { n: n.filmes_erro, erro: true, titulo: `${plural(n.filmes_erro, "filme", "filmes")} com erro na análise` };
    if (n.filmes_na_fila) return { n: n.filmes_na_fila, titulo: `${plural(n.filmes_na_fila, "filme", "filmes")} na fila de análise` };
  }
  if (id === "redes") {
    const problemas = ORDEM_REDES.filter((r) => {
      const v = e.redes[r];
      return v && v.ativo && (v.bloqueada || (!v.pronta && !e.simulacao));
    }).length;
    if (problemas) return { n: problemas, erro: true, titulo: `${plural(problemas, "rede precisa", "redes precisam")} de atenção` };
  }
  return null;
}

function desenharMenu() {
  const e = App.estado;
  montarSeMudou($("#menu"), MENU.map(([id, rotulo, icone]) => {
    const atual = id === App.nomePagina;
    const c = contadorMenu(id, e);
    return h`<a href="#/${id}" class="${atual ? "ativo" : ""}" ${atual ? raw('aria-current="page"') : ""} title="${c ? `${rotulo} · ${c.titulo}` : rotulo}">${ic(icone)}<span>${rotulo}</span>${c ? h`<i class="contador ${c.erro ? "erro" : ""}">${c.n}</i>` : ""}</a>`;
  }));
}

function desenharLateral() {
  const e = App.estado;
  let classe = "desligado";
  let titulo = "Motor desligado";
  let sub = "Nada é editado nem postado";
  if (App.offline || !e) {
    titulo = App.offline ? "Sem conexão" : "Conectando...";
    sub = App.offline ? "O AutoCortes não responde" : "";
  } else if (e.motor.parando) {
    classe = "parando"; titulo = "Desligando o motor"; sub = "Terminando o passo atual";
  } else if (e.motor.rodando) {
    classe = "ligado"; titulo = "Motor ligado";
    sub = e.pausado ? "Postagens pausadas" : e.simulacao ? "Modo simulação" : "Postando nos horários";
  }
  const conectado = e && !App.offline;
  const botao = conectado && e.motor.rodando
    ? h`<button type="button" class="btn pequeno" id="bt-motor" data-acao="motor-parar" ${e.motor.parando ? raw("disabled") : ""} title="Desligar o motor">${ic("pausa")}<span>Desligar o motor</span></button>`
    : h`<button type="button" class="btn pequeno primario" id="bt-motor" data-acao="motor-ligar" ${conectado ? "" : raw("disabled")} title="Ligar o motor">${ic("play")}<span>Ligar o motor</span></button>`;
  montarSeMudou($("#lateral-status"), h`<div class="motor-status ${classe}" title="${titulo}${sub ? `: ${sub}` : ""}"><span class="ponto"></span><div><b>${titulo}</b><small>${sub}</small></div></div>${botao}<button type="button" class="btn pequeno fantasma" id="bt-encerrar" data-acao="encerrar" title="Fechar o AutoCortes">${ic("sair")}<span>Fechar o AutoCortes</span></button>`);
}

function desenharTopo() {
  const e = App.estado;
  const partes = [];
  if (e) {
    if (e.pausado) partes.push(h`<button type="button" class="pilula aviso" data-acao="alternar-pausa" title="Clique para retomar as postagens">${ic("pausa")}Postagens pausadas</button>`);
    partes.push(e.simulacao
      ? h`<a class="pilula info" href="#/redes" title="Nada é publicado. Clique para ver como ativar a publicação real.">${ic("info")}Modo simulação</a>`
      : h`<a class="pilula real" href="#/redes" title="Os cortes são publicados de verdade nos horários da agenda">${ic("aviao")}Publicando de verdade</a>`);
  }
  partes.push(h`<button type="button" class="btn icone fantasma" data-acao="atualizar-pagina" title="Atualizar esta página" aria-label="Atualizar esta página">${ic("refazer")}</button>`);
  montarSeMudou($("#topo-acoes"), partes);
}

/* ------------------------------------------------------------ estado ao vivo */

async function buscarEstado() {
  const antes = App.estado;
  try {
    App.estado = await api("/estado");
    Estado.sessaoInvalida = false;
  } catch (err) {
    if (err.status === 403) Estado.sessaoInvalida = true;
    desenharLateral();
    return App.estado;
  }
  desenharMenu();
  desenharLateral();
  desenharTopo();
  if (App.pagina && App.pagina.aoEstado) {
    try {
      await App.pagina.aoEstado(App.estado, antes);
    } catch (err) {
      console.error(err);
    }
  }
  return App.estado;
}

/** Busca o estado agora. Chamadas durante uma busca pedem outra logo depois (o dado pode ter mudado). */
function atualizarEstado() {
  if (App.encerrado) return Promise.resolve(App.estado);
  if (Estado.emCurso) {
    Estado.repetir = true;
    return Estado.emCurso;
  }
  clearTimeout(Estado.timer);
  Estado.emCurso = buscarEstado().finally(() => {
    Estado.emCurso = null;
    if (Estado.repetir) {
      Estado.repetir = false;
      atualizarEstado();
    } else {
      agendarEstado();
    }
  });
  return Estado.emCurso;
}

function agendarEstado() {
  clearTimeout(Estado.timer);
  if (App.encerrado || Estado.sessaoInvalida) return;
  const e = App.estado;
  const movimento = e && (e.atividades.length || e.motor.parando);
  let ms = movimento ? 2000 : 5000;
  if (App.offline) ms = 4000;
  if (document.hidden) ms = 15000;
  Estado.timer = setTimeout(atualizarEstado, ms);
}

/* ------------------------------------------------------------ navegação */

function rotaAtual() {
  const nome = location.hash.replace(/^#\/?/, "").split("?")[0] || "inicio";
  return App.paginas[nome] ? nome : "inicio";
}

async function navegar() {
  if (App.encerrado) return;
  const nome = rotaAtual();
  if (modal.aberto) fecharModal(true);
  if (App.pagina && App.pagina.sair) {
    try { App.pagina.sair(); } catch (e) { console.error(e); }
  }
  const token = ++App.navegacao;
  App.nomePagina = nome;
  App.pagina = App.paginas[nome];
  paginaFalhou = false;
  document.title = `${App.pagina.titulo} · AutoCortes`;
  $("#titulo-pagina").textContent = App.pagina.titulo;
  $("#subtitulo-pagina").textContent = App.pagina.subtitulo || "";
  desenharMenu();
  desenharTopo();
  const el = $("#conteudo");
  el._htmlAtual = undefined;
  montar(el, h`<div class="carregando-bloco"><span class="giro" role="img" aria-label="Carregando"></span></div>`);
  window.scrollTo(0, 0);
  try {
    await App.pagina.render(el, token);
  } catch (e) {
    if (!App.ativa(token)) return;
    console.error(e);
    paginaFalhou = true;
    montar(el, h`${vazioBloco("erro", "Não foi possível abrir esta página", (e && e.message) || String(e), true)}<div class="centro"><button type="button" class="btn" data-acao="atualizar-pagina">${ic("refazer")}Tentar de novo</button></div>`);
  }
}

function mostrarEncerrado() {
  if ($(".encerrado")) return;
  fecharModal(true);
  document.body.appendChild(elementoDe(h`<div class="encerrado" role="alert"><div>${ic("sair", "enorme")}<h2>O AutoCortes foi fechado</h2><p>Nada será postado até você abrir o AutoCortes.bat de novo. Pode fechar esta aba.</p></div></div>`));
}

/* ------------------------------------------------------------ campos de formulário (delegação) */

function numeroDoCampo(el) {
  const texto = String(el.value).trim().replace(",", ".");
  if (texto === "") return undefined;
  const n = Number(texto);
  return Number.isFinite(n) ? n : undefined;
}

document.addEventListener("input", (ev) => {
  const el = ev.target;
  const chave = el.dataset && el.dataset.campo;
  if (!chave) return;
  const tipo = el.dataset.tipo;
  if (tipo === "range") {
    const saida = el.parentElement.querySelector("output");
    if (saida) saida.textContent = formatar(el.dataset.formato, el.value);
    definirValor(chave, Number(el.value));
  } else if (tipo === "cor") {
    const hex = el.value.replace("#", "").toUpperCase();
    const codigo = el.parentElement.querySelector("code");
    if (codigo) codigo.textContent = `#${hex}`;
    definirValor(chave, hex);
  } else if (tipo === "numero") {
    const n = numeroDoCampo(el);
    if (n !== undefined) definirValor(chave, n);
  } else if (tipo === "texto" || tipo === "senha") {
    definirValor(chave, el.value);
  }
});

document.addEventListener("change", (ev) => {
  const el = ev.target;
  const chave = el.dataset && el.dataset.campo;
  if (!chave) return;
  const tipo = el.dataset.tipo;
  if (tipo === "switch") {
    definirValor(chave, el.checked);
  } else if (tipo === "select") {
    definirValor(chave, el.hasAttribute("data-numero") ? Number(el.value) : el.value);
  } else if (tipo === "fonte") {
    // o nome da família vai junto do arquivo (a legenda usa edicao.fonte_nome, o título edicao.fonte_topo_nome)
    const opcao = el.selectedOptions[0];
    if (opcao && "nome" in opcao.dataset) definirValor(el.dataset.campoNome || "edicao.fonte_nome", opcao.dataset.nome);
    definirValor(chave, el.value);
  } else if (tipo === "numero") {
    let n = numeroDoCampo(el);
    if (n === undefined) {
      const atual = valor(chave);
      el.value = atual === undefined || atual === null ? "" : atual;
      return;
    }
    const minimo = el.min === "" ? -Infinity : Number(el.min);
    const maximo = el.max === "" ? Infinity : Number(el.max);
    n = Math.min(maximo, Math.max(minimo, n));
    if (String(n) !== el.value) el.value = n;
    definirValor(chave, n);
  }
});

/* ------------------------------------------------------------ cliques e teclado */

document.addEventListener("mousedown", (ev) => { inicioClique = ev.target; }, true);

document.addEventListener("click", async (ev) => {
  const alvo = ev.target.closest("[data-acao]");
  if (!alvo) {
    const fundo = ev.target.closest("[data-fundo=modal]");
    if (fundo && ev.target === fundo && inicioClique === fundo) fecharModal();
    const chips = ev.target.closest(".chips");
    if (chips && !ev.target.closest("input, button")) {
      const entrada = chips.querySelector("[data-chips-entrada]");
      if (entrada) entrada.focus();
    }
    return;
  }
  if (alvo.disabled || alvo.getAttribute("aria-disabled") === "true") return;
  const acao = App.acoes[alvo.dataset.acao];
  if (!acao) {
    console.warn("Ação desconhecida:", alvo.dataset.acao);
    return;
  }
  ev.preventDefault();
  try {
    await acao(alvo, ev);
  } catch (e) {
    erroToast(e);
  }
});

document.addEventListener("keydown", (ev) => {
  const el = ev.target;
  if (el.matches && el.matches("[data-chips-entrada]")) {
    const caixa = el.closest("[data-chips]");
    if ((ev.key === "Enter" || ev.key === "," || ev.key === ";") && caixa) {
      ev.preventDefault();
      if (el.value.trim()) adicionarChips(caixa, el.value);
      return;
    }
    if (ev.key === "Backspace" && !el.value && caixa) {
      const chave = caixa.dataset.chips;
      const lista = [...(valor(chave) || [])];
      if (lista.length) {
        lista.pop();
        definirValor(chave, lista);
        redesenharChips(caixa, true);
      }
      return;
    }
  }
  if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "s") {
    ev.preventDefault();
    if (contarPendentes()) salvarConfig();
    return;
  }
  if (ev.key === "Escape" && modal.aberto) {
    ev.preventDefault();
    fecharModal();
    return;
  }
  if ((ev.key === "Enter" || ev.key === " ") && el.matches && el.matches('[role="button"][data-acao]')) {
    ev.preventDefault();
    el.click();
  }
});

document.addEventListener("focusout", (ev) => {
  const el = ev.target;
  if (!el.matches || !el.matches("[data-chips-entrada]") || !el.value.trim()) return;
  const caixa = el.closest("[data-chips]");
  // espera um pouco para não atrapalhar um clique no x de um chip
  setTimeout(() => {
    if (caixa && caixa.isConnected && el.isConnected && el.value.trim()) adicionarChips(caixa, el.value, false);
  }, 150);
});

// a roda do mouse não muda um campo numérico sem querer
document.addEventListener("wheel", (ev) => {
  const el = document.activeElement;
  if (el && el.type === "number" && ev.target === el) el.blur();
}, { passive: true });

/* ------------------------------------------------------------ ações globais */

App.acoes["fechar-modal"] = () => fecharModal();

App.acoes.pular = () => {
  const el = $("#conteudo");
  if (el) el.focus();
};

App.acoes.reconectar = async (el) => {
  await ocupado(el, () => atualizarEstado());
  if (!App.offline) {
    toast("Conectado de novo");
    navegar();
  }
};

App.acoes.recarregar = () => location.reload();

App.acoes["atualizar-pagina"] = async (el) => {
  if (paginaFalhou || !App.pagina || !App.pagina.recarregar) {
    await navegar();
  } else {
    await ocupado(el, async () => { await App.pagina.recarregar(); });
  }
  atualizarEstado();
};

App.acoes["limpar-segredo"] = (el) => {
  const chave = el.dataset.chave;
  definirValor(chave, LIMPAR);
  const caixa = el.closest(".segredo");
  if (caixa) caixa.replaceWith(elementoDe(CONTROLES.senha({ chave, rotulo: App.rotulos[chave] || chave })));
  toast("A chave salva será removida quando você salvar", "info");
};

App.acoes["chip-remover"] = (el) => {
  const caixa = el.closest("[data-chips]");
  if (!caixa) return;
  const chave = caixa.dataset.chips;
  const lista = [...(valor(chave) || [])];
  lista.splice(Number(el.dataset.indice), 1);
  definirValor(chave, lista);
  redesenharChips(caixa, false);
};

App.acoes.ir = (el) => {
  const destino = `#/${el.dataset.destino}`;
  if (location.hash === destino) navegar();
  else location.hash = destino;
};

App.acoes["motor-ligar"] = async (el) => {
  const r = await ocupado(el, () => post("/motor", { acao: "iniciar" }));
  toast(r.rodando ? "Motor ligado" : "O motor não ligou. Veja o motivo no Registro.", r.rodando ? "ok" : "erro");
  await atualizarEstado();
};

App.acoes["motor-parar"] = async (el) => {
  const ok = await confirmar({ titulo: "Desligar o motor?", texto: "Nada é analisado, editado nem postado até você ligar de novo.", botao: "Desligar", perigo: true });
  if (!ok) return;
  await ocupado(el, () => post("/motor", { acao: "parar" }));
  toast("Desligando o motor...", "info");
  await atualizarEstado();
};

App.acoes["alternar-pausa"] = async (el) => {
  const pausar = !(App.estado && App.estado.pausado);
  await ocupado(el, () => post("/agenda/pausa", { pausado: pausar }));
  if (App.config && App.config.agenda) App.config.agenda.pausado = pausar;
  toast(pausar ? "Postagens pausadas. A edição continua." : "Postagens retomadas", pausar ? "info" : "ok");
  await atualizarEstado();
};

App.acoes.encerrar = async () => {
  const pendentes = contarPendentes();
  const aviso = pendentes ? `\n\n${pendentes === 1 ? "Há 1 alteração não salva, que vai" : `Há ${pendentes} alterações não salvas, que vão`} se perder.` : "";
  const ok = await confirmar({
    titulo: "Fechar o AutoCortes?",
    texto: `O motor para e nada é postado até você abrir o AutoCortes.bat de novo.${aviso}`,
    botao: "Fechar",
    perigo: true,
  });
  if (!ok) return;
  await post("/sistema/encerrar");
  App.encerrado = true;
  clearTimeout(Estado.timer);
  if (App.pagina && App.pagina.sair) {
    try { App.pagina.sair(); } catch (e) { console.error(e); }
  }
  mostrarEncerrado();
};

App.acoes["salvar-alteracoes"] = (el) => ocupado(el, salvarConfig);

App.acoes["descartar-alteracoes"] = () => {
  App.pendente = {};
  atualizarBarraSalvar();
  toast("Alterações descartadas", "info");
  navegar();
};

App.acoes["abrir-pasta"] = async (el) => {
  await ocupado(el, () => post("/sistema/abrir", { alvo: el.dataset.alvo }));
};

/* ------------------------------------------------------------ início */

function iniciarPainel() {
  document.body.appendChild(elementoDe(h`<div class="barra-salvar" id="barra-salvar" role="region" aria-label="Alterações não salvas"><span>${ic("alerta")}<span id="barra-salvar-texto"></span></span><div class="grupo-botoes"><button type="button" class="btn fantasma" data-acao="descartar-alteracoes">Descartar</button><button type="button" class="btn primario" data-acao="salvar-alteracoes" title="Salvar (Ctrl+S)">${ic("check")}Salvar</button></div></div>`));
  atualizarBarraSalvar();
  if (!location.hash.startsWith("#/")) history.replaceState(null, "", "#/inicio");
  window.addEventListener("hashchange", () => {
    if (location.hash.startsWith("#/")) navegar();
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) atualizarEstado();
  });
  window.addEventListener("beforeunload", (ev) => {
    if (App.encerrado) return;
    if (contarPendentes() || App.enviosAtivos) {
      ev.preventDefault();
      ev.returnValue = "";
    }
  });
  desenharMenu();
  desenharLateral();
  atualizarEstado().finally(() => navegar());
}

iniciarPainel();

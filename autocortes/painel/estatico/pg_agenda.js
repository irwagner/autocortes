/* AutoCortes - painel: Agenda (calendário, horários por rede, modelos prontos e regras) */
"use strict";

const Agenda = { dias: 7, plano: null, planoEm: 0, ultimaPostagem: null };

function horariosEfetivos(rede) {
  if (rede === "agenda") return { lista: valor("agenda.horarios") || [], gerais: false };
  const proprios = valor(`${rede}.horarios`) || [];
  return proprios.length ? { lista: proprios, gerais: false } : { lista: valor("agenda.horarios") || [], gerais: true };
}

function diasEfetivos(rede) {
  if (rede === "agenda") return { lista: valor("agenda.dias") || [], gerais: false };
  const proprios = valor(`${rede}.dias`) || [];
  return proprios.length ? { lista: proprios, gerais: false } : { lista: valor("agenda.dias") || [], gerais: true };
}

function paraMinutos(t) {
  const [a, b] = String(t).split(":").map(Number);
  return a * 60 + b;
}

/** Posts por semana com os valores ainda não salvos (aproximado: ignora a variação sorteada). */
function estimarSemana(horarios, dias) {
  const minimo = Number(valor("agenda.intervalo_minimo_min")) || 0;
  const minutos = [...new Set(horarios)].map(paraMinutos).sort((x, y) => x - y);
  let n = 0;
  let ultimo = -Infinity;
  for (const m of minutos) {
    if (m - ultimo >= minimo) { n++; ultimo = m; }
  }
  return n * dias.length;
}

function agendaPendente() {
  const geral = obter(App.pendente, "agenda");
  if (geral && !vazio(geral)) return true;
  return ORDEM_REDES.some((r) => {
    const p = obter(App.pendente, r);
    return p && (p.horarios !== undefined || p.dias !== undefined || p.ativo !== undefined);
  });
}

async function carregarPlano() {
  Agenda.plano = await api(`/plano?dias=${Agenda.dias}`);
  Agenda.planoEm = Date.now();
}

/* ------------------------------------------------------------ cartões de cima */

function coberturaHtml(c) {
  const baixo = c.dias !== null && c.dias < 3;
  const partes = [plural(c.candidatos, "trecho encontrado", "trechos encontrados")];
  if (c.revisao) partes.push(`${c.revisao} aguardando aprovação`);
  if (c.filmes_novos) partes.push(`${plural(c.filmes_novos, "filme", "filmes")} ainda sem análise (cerca de ${c.estimados} cortes)`);
  const linhas = ORDEM_REDES.filter((r) => c.por_rede[r]).map((r) => {
    const v = c.por_rede[r];
    return h`<li><span class="com-logo">${logoRede(r)}${REDES[r].rotulo}</span><b>${v.na_fila} na fila · ${fmtNum(v.por_dia, 1)} por dia · ${fmtDias(v.dias)}</b></li>`;
  });
  return h`<div class="cobertura-num ${baixo ? "baixo" : ""}">${c.dias === null ? "—" : fmtDias(c.dias)}</div>
    <p class="nota">de postagens com o conteúdo de hoje: ${partes.join(", ")}.</p>
    ${linhas.length ? h`<ul class="lista-simples">${linhas}</ul>` : ""}
    ${baixo ? h`<p class="nota erro-texto">Adicione filmes para a agenda não ficar sem vídeos.</p>` : ""}`;
}

function estadoPostagensHtml(e) {
  let titulo;
  let texto;
  let icone;
  if (!e.motor.rodando) {
    titulo = "Motor desligado"; icone = "pausa";
    texto = "Nada é editado nem postado. Ligue o motor no topo da página.";
  } else if (e.pausado) {
    titulo = "Postagens pausadas"; icone = "pausa";
    texto = "A edição continua; os horários ficam para depois até você retomar.";
  } else {
    titulo = e.simulacao ? "Postagens em simulação" : "Postando nos horários"; icone = "ok";
    texto = e.simulacao ? "Nos horários, o post é só registrado, sem enviar nada." : "Cada rede recebe o próximo corte da fila no horário dela.";
  }
  const tolerancia = Number(valor("agenda.tolerancia_minutos")) || 0;
  return h`<div class="estado-postagens">${ic(icone, "grande")}<div><b>${titulo}</b><p class="nota">${texto}</p></div></div>
    <div class="linha-botoes"><button class="btn ${e.pausado ? "primario" : ""}" data-acao="alternar-pausa">${e.pausado ? h`${ic("play")}Retomar postagens` : h`${ic("pausa")}Pausar postagens`}</button></div>
    <p class="nota rodape-card">O PC precisa estar ligado nos horários. Se estiver desligado, o post ainda sai até ${tolerancia} min depois.</p>`;
}

/* ------------------------------------------------------------ calendário */

function slotHtml(i) {
  const rede = REDES[i.rede];
  const estado = i.tipo === "postagem" ? estadoPost(i.estado) : i.estado === "agendado" ? null : ESTADO_HORARIO[i.estado];
  let texto = "";
  // a parte vem antes: numa coluna estreita o nome do filme é que some nas reticências
  if (i.filme) texto = `Parte ${i.parte} · ${i.filme}`;
  else if (i.tipo === "horario" && i.estado !== "perdido") texto = "Sem corte pronto";
  const classes = ["slot"];
  if (i.corte_id) classes.push("clicavel");
  if (i.tipo === "postagem") classes.push("feito");
  if (i.estado === "perdido") classes.push("perdido");
  const attrs = i.corte_id ? h`data-acao="abrir-corte" data-corte="${i.corte_id}" role="button" tabindex="0"` : "";
  return h`<div class="${classes.join(" ")}" style="--cor:${rede.cor}" ${attrs} title="${rede.nome}${i.manual ? " (pedido por você, fora da agenda)" : ""}">
    <div class="slot-hora">${logoRede(i.rede)}${fmtHora(i.quando)}${estado ? badge(estado, true) : ""}</div>
    ${texto ? h`<div class="slot-filme" title="${texto}">${texto}</div>` : ""}
    ${i.previsto ? h`<div class="slot-filme texto-suave">vai ser editado</div>` : ""}
  </div>`;
}

function calendarioHtml(p) {
  const dias = [];
  for (let d = 0; d < p.dias; d++) {
    const dia = new Date(p.inicio * 1000);
    dia.setDate(dia.getDate() + d);
    const fim = new Date(dia);
    fim.setDate(fim.getDate() + 1);
    const ini = dia.getTime() / 1000;
    const fimTs = fim.getTime() / 1000;
    dias.push({ dia, hoje: d === 0, itens: p.itens.filter((i) => i.quando >= ini && i.quando < fimTs) });
  }
  return h`<div class="semana">${dias.map(({ dia, hoje, itens }) => h`<div class="dia ${hoje ? "hoje" : ""}"><div class="dia-topo"><b>${hoje ? "Hoje" : DIAS_LONGOS[dia.getDay()]}</b><span>${dois(dia.getDate())}/${dois(dia.getMonth() + 1)}</span></div>${itens.length ? itens.map(slotHtml) : h`<div class="dia-vazio">Sem postagens</div>`}</div>`)}</div>`;
}

function calendarioCardHtml() {
  return h`<div class="card"><div class="card-topo"><h3>${ic("calendario")}Próximos ${Agenda.dias} dias</h3><div class="abas">${[7, 14].map((d) => h`<button type="button" class="aba ${Agenda.dias === d ? "ativa" : ""}" data-acao="agenda-dias" data-dias="${d}">${d} dias</button>`)}</div></div>
    <div id="agenda-aviso"></div>
    <div class="legenda-cal">${ORDEM_REDES.map((r) => h`<span>${logoRede(r)}${REDES[r].rotulo}</span>`)}<span>${badge(["Agora", "acento"], true)}no horário</span><span>${badge(["Não postado", "aviso"], true)}horário perdido</span><span class="texto-suave">Clique num post para ver o corte</span></div>
    <div id="agenda-calendario">${Agenda.plano ? calendarioHtml(Agenda.plano) : ""}</div></div>`;
}

function avisoAgendaHtml() {
  return agendaPendente() ? h`<p class="aviso-inline">${ic("alerta")}Há horários ainda não salvos: o calendário mostra a agenda salva.</p>` : "";
}

/* ------------------------------------------------------------ horários por rede */

function chipsHorasHtml(rede, lista, gerais) {
  if (!lista.length) return h`<span class="nota">Nenhum horário</span>`;
  return lista.map((hh, i) => gerais
    ? h`<span class="chip-hora geral" title="Horário geral">${hh}</span>`
    : h`<span class="chip-hora">${hh}<button type="button" data-acao="hora-remover" data-rede="${rede}" data-indice="${i}" aria-label="Remover ${hh}">${ic("x")}</button></span>`);
}

function diasBotoesHtml(rede, lista) {
  return DIAS.map(([k, r]) => h`<button type="button" class="dia-bt ${lista.includes(k) ? "on" : ""}" data-acao="dia-alternar" data-rede="${rede}" data-dia="${k}" aria-pressed="${lista.includes(k)}">${r}</button>`);
}

function redeAgendaHtml(rede) {
  const info = REDES[rede];
  const ativo = valor(`${rede}.ativo`);
  const horarios = horariosEfetivos(rede);
  const dias = diasEfetivos(rede);
  const semana = estimarSemana(horarios.lista, dias.lista);
  const aMao = valor(`${rede}.envio`) === "manual" ? " · à mão: cada horário vira uma tarefa" : "";
  return h`<div class="card rede-agenda ${ativo ? "" : "inativa"}" style="--cor:${info.cor}">
    <div class="rede-titulo">${logoRede(rede)}<div><b>${info.nome}</b><small>${ativo ? `${semana} posts por semana${aMao}` : "Desativada em Redes sociais"}</small></div></div>
    <div class="rotulo-campo">Horários</div>
    <div class="horarios">${chipsHorasHtml(rede, horarios.lista, horarios.gerais)}</div>
    ${horarios.gerais ? h`<p class="nota">Usando os horários gerais. Adicione um horário para esta rede ter os próprios.</p>` : ""}
    <div class="add-hora"><input type="time" id="hora-${rede}" value="12:00" aria-label="Novo horário para o ${info.rotulo}"><button type="button" class="btn pequeno" data-acao="hora-adicionar" data-rede="${rede}">${ic("mais")}Adicionar</button></div>
    <div class="rotulo-campo">Dias da semana</div>
    <div class="dias">${diasBotoesHtml(rede, dias.lista)}</div>
    ${dias.gerais ? h`<p class="nota">Usando os dias gerais.</p>` : ""}
  </div>`;
}

function geraisHtml() {
  const horarios = valor("agenda.horarios") || [];
  const dias = valor("agenda.dias") || [];
  return h`<div class="horarios">${chipsHorasHtml("agenda", horarios, false)}</div>
    <div class="add-hora"><input type="time" id="hora-agenda" value="12:00" aria-label="Novo horário geral"><button type="button" class="btn pequeno" data-acao="hora-adicionar" data-rede="agenda">${ic("mais")}Adicionar</button></div>
    <div class="rotulo-campo">Dias gerais</div>
    <div class="dias">${diasBotoesHtml("agenda", dias)}</div>`;
}

function presetsHtml() {
  const presets = (App.meta && App.meta.presets) || {};
  return h`<div class="presets">${Object.entries(presets).map(([id, p]) => h`<button type="button" class="preset" data-acao="aplicar-preset" data-preset="${id}"><b>${p.rotulo}</b><small>${p.descricao}</small><span class="preset-horas">${ORDEM_REDES.map((r) => h`<span>${logoRede(r)}${(p[r] || []).join(" · ")}</span>`)}</span></button>`)}</div>`;
}

function regrasHtml() {
  return formulario([
    campo({ chave: "agenda.variacao_minutos", tipo: "range", min: 0, max: 60, formato: "min", rotulo: "Variação aleatória", ajuda: "Atraso sorteado a cada dia, para os posts não saírem sempre no mesmo minuto." }),
    campo({ chave: "agenda.intervalo_minimo_min", tipo: "numero", min: 0, max: 1440, sufixo: "min", rotulo: "Intervalo mínimo", ajuda: "Entre dois posts na mesma rede, contando os de fora da agenda e as tarefas à mão." }),
    campo({ chave: "agenda.tolerancia_minutos", tipo: "numero", min: 0, max: 720, sufixo: "min", rotulo: "Tolerância", ajuda: "Se o PC estava desligado no horário, ainda posta até esse tempo depois." }),
    campo({ chave: "agenda.maximo_por_dia", tipo: "numero", min: 1, max: 100, sufixo: "posts", rotulo: "Máximo por rede em 24 h" }),
    campo({ chave: "agenda.max_tentativas", tipo: "numero", min: 1, max: 20, sufixo: "vezes", rotulo: "Tentativas por horário", ajuda: "Quando a rede dá um erro temporário." }),
    campo({ chave: "agenda.espera_erro_min", tipo: "numero", min: 1, max: 1440, sufixo: "min", rotulo: "Espera depois de um erro", ajuda: "Dobra a cada nova tentativa." }),
    secao("Horários e dias gerais"),
    h`<p class="nota">Valem para as redes sem horários ou dias próprios.</p>`,
    h`<div id="agenda-gerais">${geraisHtml()}</div>`,
  ]);
}

function redesenharAgendaEditavel() {
  montarSeMudou($("#agenda-redes"), ORDEM_REDES.map(redeAgendaHtml));
  montarSeMudou($("#agenda-gerais"), geraisHtml());
  montarSeMudou($("#agenda-aviso"), avisoAgendaHtml());
}

/* ------------------------------------------------------------ ações */

function normalizarHora(texto) {
  const m = String(texto || "").trim().match(/^(\d{1,2}):(\d{2})/);
  if (!m || Number(m[1]) > 23 || Number(m[2]) > 59) return null;
  return `${dois(Number(m[1]))}:${m[2]}`;
}

App.acoes["hora-adicionar"] = (el) => {
  const rede = el.dataset.rede;
  const entrada = $(`#hora-${rede}`);
  const hora = normalizarHora(entrada && entrada.value);
  if (!hora) { toast("Escolha um horário válido", "erro"); return; }
  const atual = horariosEfetivos(rede);
  const lista = [...atual.lista];
  if (!atual.gerais && lista.includes(hora)) { toast(`${hora} já está na lista`, "info"); return; }
  if (!lista.includes(hora)) lista.push(hora);
  lista.sort((a, b) => paraMinutos(a) - paraMinutos(b));
  definirValor(rede === "agenda" ? "agenda.horarios" : `${rede}.horarios`, lista);
  redesenharAgendaEditavel();
};

App.acoes["hora-remover"] = (el) => {
  const rede = el.dataset.rede;
  const chave = rede === "agenda" ? "agenda.horarios" : `${rede}.horarios`;
  const lista = [...(valor(chave) || [])];
  if (rede === "agenda" && lista.length <= 1) { toast("Deixe pelo menos um horário geral", "erro"); return; }
  lista.splice(Number(el.dataset.indice), 1);
  definirValor(chave, lista);
  if (!lista.length) toast(`O ${REDES[rede].rotulo} vai usar os horários gerais`, "info");
  redesenharAgendaEditavel();
};

App.acoes["dia-alternar"] = (el) => {
  const rede = el.dataset.rede;
  const dia = el.dataset.dia;
  const atual = new Set(diasEfetivos(rede).lista);
  if (atual.has(dia)) atual.delete(dia); else atual.add(dia);
  if (!atual.size) { toast("Deixe pelo menos um dia da semana", "erro"); return; }
  const lista = DIAS.map(([k]) => k).filter((k) => atual.has(k));
  definirValor(rede === "agenda" ? "agenda.dias" : `${rede}.dias`, lista);
  redesenharAgendaEditavel();
};

App.acoes["aplicar-preset"] = (el) => {
  const p = App.meta && App.meta.presets && App.meta.presets[el.dataset.preset];
  if (!p) return;
  for (const rede of ORDEM_REDES) {
    if (p[rede]) definirValor(`${rede}.horarios`, [...p[rede]]);
  }
  redesenharAgendaEditavel();
  toast(`Horários do modelo "${p.rotulo}" aplicados. Clique em Salvar para valer.`, "info", 6000);
};

App.acoes["agenda-dias"] = async (el) => {
  Agenda.dias = Number(el.dataset.dias) || 7;
  await carregarPlano();
  const card = $("#agenda-cal-card");
  if (card) montar(card, calendarioCardHtml());
  montarSeMudou($("#agenda-aviso"), avisoAgendaHtml());
};

/* ------------------------------------------------------------ página */

App.paginas.agenda = {
  titulo: "Agenda",
  subtitulo: "Quando cada rede recebe um post e o que vai ser postado",
  async render(el, token) {
    this.el = el;
    await Promise.all([garantirConfig(), carregarPlano()]);
    if (!App.ativa(token)) return;
    this.desenhar();
  },
  desenhar() {
    const e = App.estado;
    montar(this.el, h`<div class="grade-2">
        <div class="card"><div class="card-topo"><h3>${ic("caixa")}Conteúdo disponível</h3><a class="link" href="#/filmes">Adicionar filmes${ic("seta-dir")}</a></div><div id="agenda-cobertura">${coberturaHtml(Agenda.plano.cobertura)}</div></div>
        <div class="card"><div class="card-topo"><h3>${ic("aviao")}Postagens</h3></div><div id="agenda-estado">${e ? estadoPostagensHtml(e) : ""}</div></div>
      </div>
      <div id="agenda-cal-card">${calendarioCardHtml()}</div>
      <div class="titulo-secao"><h2>Horários por rede</h2><p class="nota">Os horários seguem a hora do seu PC. Mudanças valem depois de salvar.</p></div>
      <div class="grade-3" id="agenda-redes">${ORDEM_REDES.map(redeAgendaHtml)}</div>
      <div class="grade-2">
        <div class="card"><div class="card-topo"><h3>${ic("varinha")}Modelos prontos</h3></div><p class="nota">Horários sugeridos para o público do Brasil: almoço e começo da noite, e o meio da tarde no YouTube. No Bilibili, 08:00 daqui é 19:00 em Pequim. Ajuste depois pelos resultados de cada conta.</p>${presetsHtml()}</div>
        <div class="card"><div class="card-topo"><h3>${ic("ajustes")}Regras</h3></div>${regrasHtml()}</div>
      </div>`);
    montarSeMudou($("#agenda-aviso"), avisoAgendaHtml());
  },
  aoMudar(caminho) {
    const [rede, chave] = caminho.split(".");
    if (rede === "agenda" || (ORDEM_REDES.includes(rede) && ["horarios", "dias", "ativo"].includes(chave))) {
      redesenharAgendaEditavel();
    }
  },
  async aoSalvar() {
    await carregarPlano();
    if (this.el && this.el.isConnected) this.desenhar();
  },
  async aoEstado(e) {
    if (!this.el || !this.el.isConnected || !Agenda.plano) return;
    montarSeMudou($("#agenda-estado"), estadoPostagensHtml(e));
    const ultima = e.ultimas.length ? e.ultimas[0].id : null;
    if (ultima !== Agenda.ultimaPostagem || Date.now() - Agenda.planoEm > 60000) {
      Agenda.ultimaPostagem = ultima;
      try {
        await carregarPlano();
      } catch (err) {
        return;
      }
      montarSeMudou($("#agenda-calendario"), calendarioHtml(Agenda.plano));
      montarSeMudou($("#agenda-cobertura"), coberturaHtml(Agenda.plano.cobertura));
    }
  },
  async recarregar() {
    await carregarPlano();
    montarSeMudou($("#agenda-calendario"), calendarioHtml(Agenda.plano));
    montarSeMudou($("#agenda-cobertura"), coberturaHtml(Agenda.plano.cobertura));
  },
};

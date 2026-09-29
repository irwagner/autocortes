/* AutoCortes - painel: Registro (o log ao vivo, com busca e filtro) */
"use strict";

const REG_NIVEIS = [["", "Tudo"], ["aviso", "Avisos e erros"], ["erro", "Só erros"]];
const REG_ROTULOS = { DEBUG: "debug", INFO: "info", WARNING: "aviso", ERROR: "erro", CRITICAL: "crítico" };
const REG_PESO = { DEBUG: 0, INFO: 1, WARNING: 2, ERROR: 3, CRITICAL: 3 };
const REG_MAXIMO = 3000;

const Registro = { ultimo: 0, timer: null, filtro: "", nivel: "", seguir: true, token: 0, total: 0 };

function regVisivel(linha) {
  const peso = REG_PESO[linha.dataset.nivel] ?? 1;
  if (Registro.nivel === "aviso" && peso < 2) return false;
  if (Registro.nivel === "erro" && peso < 3) return false;
  return !Registro.filtro || linha._busca.includes(Registro.filtro);
}

function regHora(ts) {
  const d = new Date(ts * 1000);
  return `${dois(d.getHours())}:${dois(d.getMinutes())}:${dois(d.getSeconds())}`;
}

function regLinha(item) {
  const linha = document.createElement("div");
  const nivel = REG_ROTULOS[item.nivel] ? item.nivel : "INFO";
  linha.className = `log-linha ${nivel}`;
  linha.dataset.nivel = nivel;
  linha.title = `${new Date(item.ts * 1000).toLocaleString("pt-BR")} · ${item.origem || ""}`;
  const partes = [["h", regHora(item.ts)], ["n", REG_ROTULOS[nivel]], ["m", item.msg]];
  for (const [classe, texto] of partes) {
    const s = document.createElement("span");
    s.className = classe;
    s.textContent = texto;
    linha.appendChild(s);
  }
  linha._busca = `${item.msg} ${item.origem || ""}`.toLowerCase();
  linha.hidden = !regVisivel(linha);
  return linha;
}

function regNoFim(term) {
  return term.scrollHeight - term.scrollTop - term.clientHeight < 40;
}

function regAnexar(itens) {
  const term = $("#reg-terminal");
  if (!term || !itens.length) return;
  const vazio = term.querySelector(".vazio");
  if (vazio) vazio.remove();
  const bloco = document.createDocumentFragment();
  for (const item of itens) bloco.appendChild(regLinha(item));
  term.appendChild(bloco);
  Registro.total += itens.length;
  while (Registro.total > REG_MAXIMO && term.firstElementChild) {
    term.firstElementChild.remove();
    Registro.total--;
  }
  if (Registro.seguir) term.scrollTop = term.scrollHeight;
  regContador();
}

function regContador() {
  const el = $("#reg-contador");
  const term = $("#reg-terminal");
  if (!el || !term) return;
  const linhas = term.querySelectorAll(".log-linha");
  let visiveis = 0;
  linhas.forEach((l) => { if (!l.hidden) visiveis++; });
  el.textContent = visiveis === linhas.length ? plural(linhas.length, "mensagem", "mensagens") : `${fmtNum(visiveis)} de ${plural(linhas.length, "mensagem", "mensagens")}`;
}

function regFiltrar() {
  const term = $("#reg-terminal");
  if (!term) return;
  term.querySelectorAll(".log-linha").forEach((l) => { l.hidden = !regVisivel(l); });
  if (Registro.seguir) term.scrollTop = term.scrollHeight;
  regContador();
}

async function regBuscar(token) {
  clearTimeout(Registro.timer);
  if (!App.ativa(token)) return;
  try {
    const r = await api(`/logs?desde=${Registro.ultimo}`);
    if (!App.ativa(token)) return;
    if (r.ultimo < Registro.ultimo) {
      // o AutoCortes foi reiniciado: a numeração recomeçou
      const term = $("#reg-terminal");
      if (term) term.innerHTML = "";
      Registro.total = 0;
      Registro.ultimo = 0;
      Registro.timer = setTimeout(() => regBuscar(token), 50);
      return;
    }
    if (r.itens.length) {
      regAnexar(r.itens);
      Registro.ultimo = r.itens[r.itens.length - 1].id;
    } else if (!Registro.total) {
      const term = $("#reg-terminal");
      if (term && !term.querySelector(".vazio")) montar(term, vazioBloco("terminal", "Nada registrado ainda"));
    }
  } catch (e) {
    // sem conexão: a faixa do topo avisa; tenta de novo no próximo ciclo
  }
  if (App.ativa(token)) Registro.timer = setTimeout(() => regBuscar(token), document.hidden ? 8000 : 2000);
}

function regNiveisHtml() {
  return REG_NIVEIS.map(([id, rotulo]) => h`<button type="button" class="aba ${Registro.nivel === id ? "ativa" : ""}" aria-pressed="${Registro.nivel === id}" data-acao="registro-nivel" data-nivel="${id}">${rotulo}</button>`);
}

function regPreparar(el) {
  const busca = $("#reg-busca", el);
  const seguir = $("#reg-seguir", el);
  const term = $("#reg-terminal", el);
  busca.addEventListener("input", adiar(() => {
    Registro.filtro = busca.value.trim().toLowerCase();
    regFiltrar();
  }, 200));
  seguir.addEventListener("change", () => {
    Registro.seguir = seguir.checked;
    if (Registro.seguir) term.scrollTop = term.scrollHeight;
  });
  term.addEventListener("scroll", () => {
    const noFim = regNoFim(term);
    if (noFim !== Registro.seguir) {
      Registro.seguir = noFim;
      seguir.checked = noFim;
    }
  }, { passive: true });
}

App.acoes["registro-nivel"] = (el) => {
  Registro.nivel = el.dataset.nivel;
  montar($("#reg-niveis"), regNiveisHtml());
  regFiltrar();
};

App.acoes["registro-copiar"] = async () => {
  const term = $("#reg-terminal");
  if (!term) return;
  const texto = [...term.querySelectorAll(".log-linha")].filter((l) => !l.hidden)
    .map((l) => [...l.children].map((s) => s.textContent).join("  ")).join("\n");
  if (!texto) { toast("Nada para copiar", "info"); return; }
  try {
    await navigator.clipboard.writeText(texto);
    toast("Registro copiado");
  } catch (e) {
    toast("Não consegui copiar. Selecione o texto e use Ctrl+C.", "erro");
  }
};

App.acoes["registro-limpar"] = () => {
  const term = $("#reg-terminal");
  if (!term) return;
  term.innerHTML = "";
  Registro.total = 0;
  regContador();
};

App.paginas.registro = {
  titulo: "Registro",
  subtitulo: "Tudo o que o AutoCortes fez, ao vivo",
  async render(el, token) {
    Registro.ultimo = 0;
    Registro.total = 0;
    Registro.seguir = true;
    Registro.token = token;
    montar(el, h`<div class="barra-ferramentas">
        <div class="grupo-botoes"><input type="search" class="busca" id="reg-busca" placeholder="Procurar no registro" aria-label="Procurar no registro" value="${Registro.filtro}" spellcheck="false" autocomplete="off"><div class="abas" id="reg-niveis" role="group" aria-label="Filtrar por tipo">${regNiveisHtml()}</div></div>
        <div class="grupo-botoes"><small class="texto-suave" id="reg-contador" aria-live="off"></small><label class="marcar"><input type="checkbox" id="reg-seguir" checked>Acompanhar o fim</label><button type="button" class="btn pequeno" data-acao="registro-copiar">${ic("copiar")}Copiar</button><button type="button" class="btn pequeno fantasma" data-acao="registro-limpar" title="Limpa só esta tela">${ic("lixo")}Limpar a tela</button><button type="button" class="btn pequeno" data-acao="abrir-pasta" data-alvo="logs">${ic("pasta")}Arquivos de log</button></div>
      </div>
      <div class="terminal" id="reg-terminal" role="log" aria-label="Mensagens do AutoCortes" tabindex="0"></div>
      <p class="nota rodape-card">Mostra as mensagens mais recentes desde que o AutoCortes abriu. O histórico completo fica nos arquivos de log, na pasta de dados.</p>`);
    regPreparar(el);
    Registro.filtro = ($("#reg-busca").value || "").trim().toLowerCase();
    await regBuscar(token);
  },
  recarregar() {
    regBuscar(App.navegacao);
  },
  sair() {
    clearTimeout(Registro.timer);
    Registro.timer = null;
  },
};

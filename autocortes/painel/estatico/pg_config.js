/* AutoCortes - painel: Configurações (abas, amostra dos posts, IA e transcrição; o visual fica no Estúdio) */
"use strict";

const ABAS_CONFIG = [
  ["geral", "Geral", "ajustes", "Como o AutoCortes trabalha: simulação, aprovação, reserva de cortes e pastas."],
  ["cortes", "Cortes", "tesoura", "Duração dos cortes e como os melhores trechos de cada filme são escolhidos."],
  ["canais", "Canais", "baixar", "Vídeos baixados dos canais do YouTube cadastrados em Filmes: quais, de quando, em que qualidade e quanto disco usar."],
  ["video", "Vídeo", "video", "Qualidade da edição e volume do som. O visual (moldura, legenda, título) fica no Estúdio."],
  ["criacao", "Criação", "varinha", "Vídeos montados do zero: imagens de fundo, narração e música. As pautas ficam na aba Criação."],
  ["textos", "Textos dos posts", "lapis", "Título, descrição e hashtags de cada postagem."],
  ["imagens", "Imagens", "imagem", "Posts de imagem tirados dos cortes: quais tipos saem, o que vai escrito e a legenda. Cada rede liga as imagens em Redes sociais."],
  ["ia", "IA", "varinha", "Textos dos posts escritos por uma IA a partir da fala de cada corte. É opcional."],
  ["transcricao", "Transcrição", "microfone", "De onde vêm as falas usadas nas legendas e na escolha dos trechos."],
  ["sistema", "Sistema", "cpu", "Inicialização, desempenho, métricas, ferramentas e pastas."],
];
const CFG_VOZES = [
  ["pt-BR-AntonioNeural", "Antônio (masculina)"],
  ["pt-BR-FranciscaNeural", "Francisca (feminina)"],
  ["pt-BR-ThalitaMultilingualNeural", "Thalita (feminina, multilíngue)"],
];
const CFG_CODECS = [
  ["libx264", "Processador (libx264), melhor qualidade"], ["h264_amf", "Placa AMD (AMF), mais rápido"],
  ["h264_nvenc", "Placa NVIDIA (NVENC)"], ["h264_qsv", "Intel Quick Sync"],
];
const CFG_PRESETS_X264 = [
  ["veryfast", "Muito rápido"], ["faster", "Mais rápido"], ["fast", "Rápido"], ["medium", "Equilibrado"],
  ["slow", "Lento, arquivo menor"], ["slower", "Mais lento"],
];
const CFG_FONTES_FALA = [
  ["auto", "Legenda do arquivo; se não tiver, transcrição automática"],
  ["arquivo", "Só legendas (.srt ou embutidas no vídeo)"],
  ["whisper", "Sempre transcrever com o Whisper"],
  ["nenhuma", "Não usar as falas"],
];
const CFG_PESOS = [
  ["volume", "Volume", "Trechos mais altos que a média do filme."],
  ["picos", "Momentos intensos", "Explosões, gritos e música forte."],
  ["fala", "Diálogo", "Quanto do trecho tem fala."],
  ["cenas", "Ritmo de cenas", "Trocas de cena frequentes."],
  ["gancho", "Começo forte", "Os primeiros segundos chamam a atenção."],
  ["texto", "Falas marcantes", "Perguntas, exclamações e as palavras-chave."],
  ["duracao", "Duração ideal", "Perto da duração ideal."],
  ["abertura", "Abertura da fala", "Começa no início de uma frase, e não no meio."],
  ["silencio", "Penalidade por silêncio", "Tira pontos de trechos parados e sem som."],
];
const CFG_VARS_TEXTO = ["{filme}", "{parte}", "{frase}", "{ano}", "{ano_parenteses}", "{hashtags}", "{canal}", "{link_original}"];
const CFG_COMANDO_OLLAMA = "ollama pull qwen3:4b-instruct-2507-q4_K_M";
// mudanças que mostram ou escondem outros campos
// (estoque.fonte troca o campo da pasta pelo das chaves: sem redesenhar, o campo das chaves não aparecia)
const CFG_REDESENHA = new Set(["geral.simulacao", "edicao.codec", "transcricao.fonte", "estoque.fonte"]);

const PgConfig = {
  aba: "geral",
  atualizacao: null, // /api/atualizacao: a versão instalada e a do GitHub (aba Sistema)
  modelosIa: null,
  iaResultado: null,
  baixando: 0,
  baixandoVisto: false,
  modeloBaixando: "",
  ultimoCampo: null,
  imgPrevia: null, // a última prévia de imagem pedida na aba Imagens ({tipo, rede, urls, real} ou o erro)
};

/* ------------------------------------------------------------ pedaços reutilizados */

function cfgBotaoPasta(alvo, rotulo) {
  return h`<button type="button" class="btn pequeno" data-acao="abrir-pasta" data-alvo="${alvo}">${ic("pasta")}${rotulo}</button>`;
}

function cfgAvisoReinicio() {
  const lista = (App.meta && App.meta.reiniciar) || [];
  if (!lista.length) return "";
  return h`<p class="aviso-inline">${ic("alerta")}Feche e abra o AutoCortes para aplicar: ${lista.map(rotuloReinicio).join(" e ")}.</p>`;
}

/** Botões que inserem {variáveis} no último campo de texto usado entre os indicados. */
function cfgVariaveis(lista, campos) {
  return h`<div class="variaveis" role="group" aria-label="Inserir uma variável">${lista.map((v) => h`<button type="button" data-acao="inserir-variavel" data-campos="${campos}" data-var="${v}" title="Inserir ${v}">${v}</button>`)}</div>`;
}

/* ------------------------------------------------------------ abas */

function cfgAbaGeral() {
  const vaiPublicar = App.config && App.config.geral.simulacao && valor("geral.simulacao") === false;
  return [
    secao("Funcionamento"),
    campo({ chave: "geral.simulacao", tipo: "switch", rotulo: "Modo simulação", ajuda: "Edita os vídeos e segue a agenda, mas não publica nada." }),
    vaiPublicar ? h`<p class="aviso-inline">${ic("alerta")}Ao salvar, os cortes passam a ser publicados de verdade nos horários da agenda.</p>` : "",
    campo({ chave: "geral.exigir_aprovacao", tipo: "switch", rotulo: "Aprovar cada corte antes de postar", ajuda: "Os cortes editados esperam a sua aprovação na página Cortes." }),
    campo({ chave: "geral.buffer_cortes", tipo: "numero", min: 1, max: 50, sufixo: "cortes", rotulo: "Reserva de cortes editados", ajuda: "Quantos cortes prontos ficam na fila de cada rede. Mais reserva ocupa mais espaço em disco." }),
    campo({ chave: "geral.analisar_antecipado", tipo: "switch", rotulo: "Analisar filmes novos na hora", ajuda: "Analisa cada filme assim que ele chega, mesmo com a fila cheia." }),
    campo({ chave: "geral.apagar_apos_postar", tipo: "switch", rotulo: "Apagar o vídeo depois de postar", ajuda: "Quando o corte já foi para todas as redes ativas. Economiza espaço; o histórico continua." }),
    secao("Pastas"),
    campo({ chave: "geral.pasta_filmes", tipo: "texto", rotulo: "Pasta dos filmes", ajuda: "Caminho completo ou relativo à pasta do AutoCortes." }),
    campo({ chave: "geral.varrer_a_cada_min", tipo: "numero", min: 1, max: 1440, sufixo: "min", rotulo: "Procurar filmes novos a cada" }),
    campo({ chave: "geral.pasta_dados", tipo: "texto", rotulo: "Pasta de dados", ajuda: "Banco de dados, cortes editados, logins e registros. Vale depois de fechar e abrir o AutoCortes, e os arquivos que já existem não são movidos." }),
    cfgAvisoReinicio(),
    h`<div class="linha-botoes rodape-card">${cfgBotaoPasta("filmes", "Abrir a pasta dos filmes")}${cfgBotaoPasta("cortes", "Abrir os cortes editados")}</div>`,
  ];
}

function cfgAbaCortes() {
  return [
    h`<p class="nota">Duração, palavras-chave e pontuação valem para os filmes analisados daqui em diante. Para refazer um filme, use Reanalisar na página Filmes.</p>`,
    secao("Duração"),
    campo({ chave: "cortes.duracao_min", tipo: "numero", min: 5, max: 600, sufixo: "s", rotulo: "Mínima" }),
    campo({ chave: "cortes.duracao_alvo", tipo: "numero", min: 5, max: 600, sufixo: "s", rotulo: "Ideal", ajuda: "Os cortes ficam perto disso, terminando numa pausa da fala ou numa troca de cena." }),
    campo({ chave: "cortes.duracao_max", tipo: "numero", min: 5, max: 600, sufixo: "s", rotulo: "Máxima", ajuda: "Até 60 s funciona em todas as redes. O YouTube só recebe até o limite dos Shorts (Redes sociais > YouTube) e a Página do Facebook, até 90 s." }),
    secao("Escolha dos trechos"),
    campo({ chave: "cortes.max_por_filme", tipo: "numero", min: 1, max: 1000, sufixo: "trechos", rotulo: "Máximo por filme" }),
    campo({ chave: "cortes.espaco_minimo_seg", tipo: "numero", min: 0, max: 600, sufixo: "s", rotulo: "Espaço entre trechos", ajuda: "Distância mínima entre dois cortes do mesmo filme." }),
    campo({ chave: "cortes.ordem_filmes", tipo: "select", rotulo: "Ordem dos filmes", opcoes: [["intercalar", "Intercalar os filmes"], ["sequencial", "Um filme de cada vez"]] }),
    campo({ chave: "cortes.ordem_cortes", tipo: "select", rotulo: "Ordem dos cortes", ajuda: "\"Melhores primeiro\" posta antes os trechos com nota mais alta.", opcoes: [["melhores", "Melhores primeiro"], ["cronologica", "Na ordem do filme"]] }),
    campo({ chave: "cortes.palavras_chave", tipo: "chips", larga: true, rotulo: "Palavras-chave", ajuda: "Trechos com estas palavras na fala ganham pontos. Por exemplo, nomes de personagens e bordões." }),
    campo({ chave: "analise.ignorar_inicio_seg", tipo: "numero", min: 0, max: 3600, sufixo: "s", rotulo: "Pular o começo do filme", ajuda: "Logotipos e créditos iniciais." }),
    campo({ chave: "analise.ignorar_fim_seg", tipo: "numero", min: 0, max: 3600, sufixo: "s", rotulo: "Pular o final", ajuda: "Créditos finais." }),
    h`<details class="guia"><summary>${ic("ajustes")}Ajuste fino da pontuação</summary><div class="form-interno">${formulario([
      h`<p class="nota">Quanto cada sinal pesa na nota de um trecho. Zero desliga o sinal.</p>`,
      ...CFG_PESOS.map(([k, rotulo, ajuda]) => campo({ chave: `cortes.pesos.${k}`, tipo: "range", min: 0, max: 3, passo: 0.1, formato: "dec1", rotulo, ajuda })),
      campo({ chave: "analise.limiar_cena", tipo: "range", min: 3, max: 30, rotulo: "Sensibilidade da troca de cena", ajuda: "Menor detecta mais trocas de cena." }),
    ])}</div></details>`,
  ];
}

function cfgAbaVideo() {
  const x264 = valor("edicao.codec") === "libx264";
  return [
    h`<div class="aviso-inline">${ic("varinha")}<span>Moldura, tamanho do vídeo, título, legenda e cores agora ficam no <a class="link" href="#/estudio">Estúdio</a>, com a tela do celular para arrastar cada coisa. Modelo em uso: <b>${valor("edicao.modelo")}</b>.</span></div>`,
    secao("Áudio"),
    campo({ chave: "edicao.normalizar_audio", tipo: "switch", rotulo: "Normalizar o volume", ajuda: "Deixa o som no volume que as redes usam (-14 LUFS)." }),
    secao("Qualidade e velocidade"),
    campo({ chave: "edicao.codec", tipo: "select", rotulo: "Codificador", ajuda: "Se o da placa de vídeo falhar, o AutoCortes volta sozinho para o processador.", opcoes: CFG_CODECS }),
    ...(x264 ? [
      campo({ chave: "edicao.preset", tipo: "select", rotulo: "Velocidade", ajuda: "Mais lento gera um arquivo menor com a mesma qualidade.", opcoes: CFG_PRESETS_X264 }),
      campo({ chave: "edicao.crf", tipo: "range", min: 14, max: 30, rotulo: "Qualidade (CRF)", ajuda: "Menor dá mais qualidade e arquivo maior. Entre 18 e 23 é o normal." }),
    ] : [h`<p class="nota">Com a placa de vídeo, a qualidade fica fixa em 8 Mbit/s, acima do que as redes exibem.</p>`]),
    h`<p class="nota rodape-card">Os vídeos saem em ${valor("edicao.largura")}×${valor("edicao.altura")}, o formato vertical das redes, com pelo menos 24 quadros por segundo. As mudanças valem para os próximos cortes editados; num corte pronto, use "Editar de novo".</p>`,
  ];
}

function cfgAbaTextos() {
  return [
    secao("Modelos"),
    campo({ chave: "textos.titulo", tipo: "texto", larga: true, id: "cfg-titulo", rotulo: "Título" }),
    campo({ chave: "textos.descricao", tipo: "textarea", linhas: 5, larga: true, id: "cfg-descricao", rotulo: "Descrição" }),
    cfgVariaveis(CFG_VARS_TEXTO, "cfg-titulo cfg-descricao"),
    h`<p class="nota">{frase} é a fala mais marcante do trecho e {hashtags} são as hashtags abaixo. Em cada corte dá para escrever outro texto na página Cortes.</p>`,
    secao("Quando a IA escreve"),
    h`<p class="nota">Usados no lugar dos modelos de cima quando a IA escreveu o título e a descrição do corte. {titulo_ia} e {descricao_ia} são o que ela escreveu.</p>`,
    campo({ chave: "textos.titulo_ia", tipo: "texto", larga: true, id: "cfg-titulo-ia", rotulo: "Título" }),
    campo({ chave: "textos.descricao_ia", tipo: "textarea", linhas: 4, larga: true, id: "cfg-descricao-ia", rotulo: "Descrição" }),
    cfgVariaveis(["{titulo_ia}", "{descricao_ia}", ...CFG_VARS_TEXTO], "cfg-titulo-ia cfg-descricao-ia"),
    secao("Hashtags"),
    campo({ chave: "textos.hashtags", tipo: "chips", prefixo: "#", larga: true, placeholder: "#filmes e tecle Enter", rotulo: "Hashtags gerais" }),
    campo({ chave: "textos.hashtag_do_filme", tipo: "switch", rotulo: "Hashtag com o nome do filme", ajuda: "Vem primeiro, antes das hashtags do filme (em Filmes > Dados) e das gerais." }),
    campo({ chave: "textos.max_hashtags", tipo: "numero", min: 0, max: 30, rotulo: "Máximo de hashtags", ajuda: "O Instagram aceita até 5; lá vão só as 5 primeiras." }),
  ];
}

function cfgModelosIaHtml() {
  const lista = PgConfig.modelosIa;
  if (!lista) return "";
  if (!lista.length) return h`<p class="nota">Nenhum modelo instalado nesse endereço. No Ollama, baixe um com o comando ao lado.</p>`;
  const atual = valor("ia.modelo");
  return h`<div class="lista-modelos" role="group" aria-label="Modelos instalados">${lista.map((m) => h`<button type="button" class="btn pequeno ${m === atual ? "ativo" : ""}" data-acao="ia-usar-modelo" data-modelo="${m}" aria-pressed="${m === atual}">${m === atual ? ic("check") : ""}${m}</button>`)}</div>`;
}

function cfgResultadoIaHtml() {
  const r = PgConfig.iaResultado;
  if (!r) return "";
  return h`<p class="aviso-inline ${r.ok ? "ok" : "erro"}">${ic(r.ok ? "ok" : "erro")}<span>${r.texto}</span></p>`;
}

function cfgAbaIa() {
  return [
    campo({ chave: "ia.ativo", tipo: "switch", rotulo: "Escrever os textos com IA", ajuda: "Título, descrição e hashtags de cada corte, a partir da fala do trecho. Se a IA falhar, o post usa os modelos da aba Textos dos posts." }),
    secao("Conexão"),
    campo({ chave: "ia.url", tipo: "texto", rotulo: "Endereço da API", placeholder: "http://127.0.0.1:11434/v1", ajuda: "Qualquer API compatível com a da OpenAI. O Ollama atende em http://127.0.0.1:11434/v1." }),
    campo({ chave: "ia.modelo", tipo: "texto", id: "cfg-ia-modelo", rotulo: "Modelo", placeholder: "qwen3:4b-instruct-2507-q4_K_M" }),
    h`<div id="ia-modelos">${cfgModelosIaHtml()}</div>`,
    campo({ chave: "ia.api_key", tipo: "senha", rotulo: "Chave da API", ajuda: "Só para serviços online. O Ollama e o LM Studio não precisam." }),
    secao("Ajustes"),
    campo({ chave: "ia.temperatura", tipo: "range", min: 0, max: 2, passo: 0.1, formato: "dec1", rotulo: "Criatividade", ajuda: "Mais alto deixa os textos mais variados e menos previsíveis." }),
    campo({ chave: "ia.tempo_limite_seg", tipo: "numero", min: 10, max: 1800, sufixo: "s", rotulo: "Tempo limite", ajuda: "A primeira resposta demora mais, porque o modelo precisa carregar." }),
    h`<div class="linha-botoes rodape-card"><button type="button" class="btn" data-acao="ia-testar">${ic("raio")}Testar a IA</button><button type="button" class="btn" data-acao="ia-modelos">${ic("lista")}Ver os modelos instalados</button></div>`,
    h`<div id="ia-resultado">${cfgResultadoIaHtml()}</div>`,
  ];
}

function cfgAbaCriacao() {
  const fonte = valor("estoque.fonte");
  const itens = [
    campo({ chave: "criacao.ativo", tipo: "switch", rotulo: "Criar vídeos do zero", ajuda: "O motor transforma as pautas (e os temas da fila) em vídeos com narração, e eles entram na mesma agenda dos cortes. As pautas ficam na aba Criação." }),
    campo({ chave: "criacao.prioridade", tipo: "select", rotulo: "Quem sai primeiro", opcoes: [["filmes", "Os cortes de filme"], ["criacao", "Os vídeos criados"]], ajuda: "Quando falta estoque e há os dois tipos esperando." }),
    campo({ chave: "criacao.duracao_alvo_seg", tipo: "numero", min: 10, max: 180, sufixo: "s", rotulo: "Duração que a IA mira", ajuda: "Tamanho do roteiro que a IA escreve. A duração real vem da narração." }),
    secao("Imagens de fundo"),
    campo({ chave: "estoque.fonte", tipo: "select", rotulo: "De onde vêm", opcoes: [["pasta", "Meus arquivos, numa pasta"], ["pexels", "Pexels (chave grátis)"], ["pixabay", "Pixabay (chave grátis)"]] }),
  ];
  if (fonte === "pasta") {
    itens.push(campo({ chave: "estoque.pasta", tipo: "texto", rotulo: "Pasta do material", placeholder: "material", ajuda: "Vídeos e imagens seus. Vazio = a pasta \"material\" ao lado do config." }));
  } else {
    const onde = fonte === "pexels" ? "pexels.com/api" : "pixabay.com/api/docs";
    // as chaves são segredo: não voltam para a tela, e o campo novo troca a lista inteira
    itens.push(campo({ chave: `estoque.${fonte}_chaves`, tipo: "senha", larga: true, rotulo: "Chaves da API", ajuda: `Pegue de graça em ${onde}. Para alternar várias (cada uma tem limite por hora), cole todas no campo separadas por vírgula. Ao trocar, cole a lista inteira de novo.` }));
  }
  itens.push(
    campo({ chave: "estoque.duracao_min_seg", tipo: "numero", min: 0, max: 60, sufixo: "s", rotulo: "Clipe mínimo", ajuda: "Clipe mais curto que isso não entra." }),
    campo({ chave: "estoque.evitar_repetidos", tipo: "switch", rotulo: "Não repetir material", ajuda: "As redes tratam repetição de material como conteúdo não original." }),
    campo({ chave: "criacao.ken_burns", tipo: "switch", rotulo: "Zoom lento nas imagens", ajuda: "Imagem parada sem movimento vira apresentação de slides." }),
    campo({ chave: "criacao.embaralhar_clipes", tipo: "switch", rotulo: "Embaralhar a ordem dos clipes" }),
    secao("Narração"),
    campo({ chave: "voz.voz", tipo: "select", rotulo: "Voz", opcoes: CFG_VOZES, ajuda: "Vozes neurais em português do Brasil, pelo serviço de leitura em voz alta do Edge. O texto do roteiro sai do seu computador." }),
    campo({ chave: "voz.ritmo", tipo: "texto", rotulo: "Velocidade", placeholder: "+0%", ajuda: "Como +10% (mais rápido) ou -10% (mais devagar)." }),
    campo({ chave: "voz.tom", tipo: "texto", rotulo: "Tom", placeholder: "+0Hz", ajuda: "Como +2Hz ou -2Hz." }),
    campo({ chave: "voz.pausa_linha_seg", tipo: "numero", min: 0, max: 5, passo: 0.05, sufixo: "s", rotulo: "Respiro entre linhas", ajuda: "Silêncio depois de cada linha do roteiro. Para uma pausa maior num ponto exato, escreva [pausa: 2s] no texto." }),
    secao("Música e acabamento"),
    campo({ chave: "criacao.pasta_musicas", tipo: "texto", rotulo: "Pasta das músicas", placeholder: "musicas", ajuda: "Vazio = sem música. A música abaixa sozinha sob a voz." }),
    campo({ chave: "criacao.volume_musica", tipo: "range", min: 0, max: 1, passo: 0.02, formato: "pct", rotulo: "Volume da música" }),
    campo({ chave: "criacao.cauda_seg", tipo: "numero", min: 0, max: 5, passo: 0.1, sufixo: "s", rotulo: "Sobra no fim", ajuda: "Imagem depois da última palavra." }),
    campo({ chave: "criacao.fps", tipo: "numero", min: 24, max: 60, rotulo: "Quadros por segundo" }),
    h`<p class="nota rodape-card">Juntar clipe de banco com voz sintética é o formato que as redes mais filtram hoje: serve para crescer e testar nicho, mas não conte com monetização direta.</p>`,
  );
  return itens;
}

const CFG_IDADE_CANAIS = [[7, "1 semana"], [30, "1 mês"], [90, "3 meses"], [180, "6 meses"], [365, "1 ano"],
  [730, "2 anos"], [0, "Qualquer data"]];
const CFG_RESOLUCOES = [[480, "480p"], [720, "720p"], [1080, "1080p (recomendado)"], [1440, "1440p"], [2160, "4K (2160p)"]];

function cfgAbaCanais() {
  return [
    h`<div class="aviso-rede">${ic("alerta")}<span>Use só canais <b>seus ou que autorizam cortes</b>. Os canais ficam em <a href="#/filmes?aba=canais">Filmes &gt; Canais do YouTube</a>, onde também dá para olhar um canal na hora.</span></div>`,
    campo({ chave: "canais.ativo", tipo: "switch", rotulo: "Baixar sozinho", ajuda: "Com o motor ligado, os canais ativos são olhados de tempos em tempos e os vídeos novos vão para a pasta de filmes." }),
    campo({ chave: "canais.verificar_a_cada_horas", tipo: "numero", min: 1, max: 168, sufixo: "h", rotulo: "Olhar os canais a cada" }),
    secao("Quais vídeos"),
    campo({ chave: "canais.max_idade_dias", tipo: "select", numero: true, rotulo: "Publicados há no máximo", opcoes: CFG_IDADE_CANAIS, ajuda: "Os mais novos saem primeiro. Com um período longo, os antigos vêm aos poucos, a cada olhada." }),
    campo({ chave: "canais.minutos_min", tipo: "numero", min: 0, max: 600, sufixo: "min", rotulo: "Duração mínima", ajuda: "Vídeo curto rende poucos cortes. Os Shorts do canal nunca entram." }),
    campo({ chave: "canais.minutos_max", tipo: "numero", min: 0, max: 1440, sufixo: "min", rotulo: "Duração máxima", ajuda: "0 = sem limite. Vídeo de várias horas pesa no disco e demora na análise." }),
    campo({ chave: "canais.incluir_ao_vivo", tipo: "switch", rotulo: "Lives que já terminaram", ajuda: "A aba Ao vivo do canal. Muitos podcasts ficam lá." }),
    campo({ chave: "canais.ignorar_palavras", tipo: "chips", larga: true, rotulo: "Pular vídeos com", placeholder: "Digite e tecle Enter", ajuda: "Palavras no título, como trailer ou react (maiúscula e acento não importam)." }),
    secao("Quanto baixar"),
    campo({ chave: "canais.max_por_canal", tipo: "numero", min: 1, max: 20, rotulo: "Vídeos de cada canal por vez" }),
    campo({ chave: "canais.max_na_fila", tipo: "numero", min: 1, max: 50, rotulo: "Baixados esperando a análise", ajuda: "Com esse tanto de vídeos baixados e ainda não analisados, eu espero a fila andar antes de baixar mais. Assim o disco não enche à toa." }),
    campo({ chave: "canais.resolucao", tipo: "select", numero: true, rotulo: "Qualidade máxima", opcoes: CFG_RESOLUCOES, ajuda: "O corte sai em 1080x1920. Acima de 1080p o arquivo fica bem maior; só vale com zoom alto no Estúdio." }),
    campo({ chave: "canais.legendas", tipo: "switch", rotulo: "Legendas do canal", ajuda: "Quando o canal publicou legendas em português (as automáticas não contam), elas vêm junto e poupam a transcrição." }),
    campo({ chave: "canais.espaco_minimo_gb", tipo: "numero", min: 0, max: 10000, sufixo: "GB", rotulo: "Espaço livre mínimo", ajuda: "Com menos que isso livre no disco da pasta de filmes, os downloads param." }),
    campo({ chave: "canais.pasta", tipo: "texto", rotulo: "Subpasta dos vídeos", placeholder: "YouTube", ajuda: "Dentro da pasta de filmes, com uma pasta para cada canal." }),
    secao("Programa de download"),
    campo({ chave: "ferramentas.ytdlp_atualizar", tipo: "switch", rotulo: "Atualizar o yt-dlp sozinho", ajuda: "O YouTube muda e versões antigas param de baixar. A cada 3 dias, e depois de um erro de versão velha, o yt-dlp oficial confere se há versão nova." }),
    campo({ chave: "ferramentas.ytdlp", tipo: "texto", rotulo: "yt-dlp próprio", placeholder: "vazio = o oficial, baixado sozinho", ajuda: "Caminho do seu yt-dlp.exe. Com ele, quem atualiza é você." }),
    h`<p class="nota rodape-card">O yt-dlp precisa de um JavaScript para falar com o YouTube: uso o Node.js 22 ou mais novo, se estiver instalado, ou baixo o Deno (43 MB) na pasta dele. Não uso proxy. Se o YouTube pedir para confirmar que não é um robô, os downloads param por 12 h, e por mais tempo se o aviso voltar (até 48 h). Com a conta separada (Filmes > Canais do YouTube), o pedido é repetido com ela em vez de parar.</p>`,
  ];
}

function cfgAbaTranscricao() {
  const modelos = (App.meta && App.meta.modelos) || {};
  const opcoesModelo = Object.entries(modelos).map(([nome, m]) => [nome, `${nome} · ${m.descricao}${m.baixado ? " · baixado" : ""}`]);
  const idiomas = IDIOMAS.map(([v, t]) => [v || "auto", t]);
  const itens = [
    campo({ chave: "transcricao.fonte", tipo: "select", rotulo: "De onde vêm as falas", opcoes: CFG_FONTES_FALA }),
  ];
  if (valor("transcricao.fonte") === "nenhuma") {
    itens.push(h`<p class="aviso-inline">${ic("alerta")}Sem as falas, os cortes saem sem legenda e a escolha dos trechos usa só o som e as cenas.</p>`);
    return itens;
  }
  return itens.concat([
    secao("Transcrição automática (Whisper)"),
    campo({ chave: "transcricao.modelo", tipo: "select", rotulo: "Modelo", opcoes: opcoesModelo, ajuda: "Modelos maiores erram menos e demoram mais. O small é um bom equilíbrio para português." }),
    campo({ chave: "transcricao.idioma", tipo: "select", rotulo: "Idioma falado", opcoes: idiomas, ajuda: "Cada filme pode ter outro idioma, em Filmes > Dados." }),
    campo({ chave: "ferramentas.threads", tipo: "numero", min: 0, max: 64, rotulo: "Núcleos do processador", ajuda: "0 deixa automático." }),
    secao("Legendas e áudio do arquivo"),
    campo({ chave: "transcricao.idiomas_legenda", tipo: "chips", larga: true, rotulo: "Legendas preferidas", ajuda: "Idiomas das legendas embutidas ou dos arquivos .srt, em ordem de preferência (por exemplo pt-BR, por)." }),
    campo({ chave: "analise.idiomas_audio", tipo: "chips", larga: true, rotulo: "Áudio preferido", ajuda: "Em filmes com várias faixas de áudio, usa a primeira destes idiomas (por exemplo por, eng)." }),
  ]);
}

function cfgAbaSistema() {
  const m = App.meta || {};
  const pastas = [["filmes", "Filmes"], ["cortes", "Cortes editados"], ["logs", "Registros"], ["dados", "Dados"], ["config", "config.toml"]];
  return [
    secao("Inicialização"),
    linhaCampo("Iniciar com o Windows", "Liga o AutoCortes em segundo plano quando você entra no Windows, sem abrir o navegador. Para ver o painel, abra o AutoCortes.bat.",
      h`<label class="switch"><input type="checkbox" id="sis-inicializacao" ${m.inicializacao ? raw("checked") : ""} aria-label="Iniciar com o Windows"><span></span></label>`),
    m.inicializacao_outra
      ? h`<p class="aviso-inline">${ic("alerta")}A inicialização do Windows aponta para outra pasta do AutoCortes (ele mudou de lugar?). Ligue a chave acima para ela abrir esta instalação.</p>`
      : "",
    secao("Atualização"),
    h`<div id="sis-atualizacao">${cfgAtualizacaoHtml()}</div>`,
    secao("Painel"),
    campo({ chave: "painel.abrir_navegador", tipo: "switch", rotulo: "Abrir o painel no navegador", ajuda: "Quando o AutoCortes é aberto pelo AutoCortes.bat." }),
    campo({ chave: "painel.porta", tipo: "numero", min: 1024, max: 65535, rotulo: "Porta do painel", ajuda: "O painel só aceita conexões deste computador. Vale depois de fechar e abrir o AutoCortes." }),
    cfgAvisoReinicio(),
    secao("Desempenho"),
    campo({ chave: "geral.prioridade_baixa", tipo: "switch", rotulo: "Prioridade baixa", ajuda: "Análise e edição rodam com prioridade baixa, para o PC continuar leve." }),
    campo({ chave: "geral.impedir_suspensao", tipo: "switch", rotulo: "Não deixar o PC suspender", ajuda: "Enquanto o motor está ligado. A tela ainda pode desligar." }),
    campo({ chave: "ferramentas.hwaccel", tipo: "select", rotulo: "Decodificar com a placa de vídeo", ajuda: "Acelera a detecção de cenas. Se falhar, usa o processador.", opcoes: [["auto", "Automático"], ["d3d11va", "Direct3D 11"], ["none", "Desligado"]] }),
    secao("Métricas"),
    campo({ chave: "metricas.ativo", tipo: "switch", rotulo: "Coletar visualizações e curtidas", ajuda: "Do YouTube e do Instagram, pela API oficial. Se não aparecer nada, conecte a conta de novo em Redes sociais." }),
    campo({ chave: "metricas.intervalo_horas", tipo: "numero", min: 1, max: 168, sufixo: "h", rotulo: "Atualizar a cada" }),
    campo({ chave: "metricas.janela_dias", tipo: "numero", min: 1, max: 90, sufixo: "dias", rotulo: "Acompanhar posts de até" }),
    secao("Ferramentas"),
    campo({ chave: "ferramentas.ffmpeg", tipo: "texto", rotulo: "FFmpeg", ajuda: "\"ffmpeg\" usa o que está no PATH do Windows. Ou informe o caminho completo do ffmpeg.exe." }),
    campo({ chave: "ferramentas.ffprobe", tipo: "texto", rotulo: "FFprobe" }),
    secao("Arquivos"),
    h`<div class="linha-botoes">${pastas.map(([alvo, rotulo]) => cfgBotaoPasta(alvo, rotulo))}</div>`,
    h`<div class="rodape-config"><span>AutoCortes ${m.versao || ""}</span><code title="Arquivo de configuração">${m.caminho || ""}</code><button type="button" class="btn pequeno perigo empurrar" data-acao="encerrar">${ic("sair")}Fechar o AutoCortes</button></div>`,
  ];
}

/* ------------------------------------------------------------ imagens */

const CFG_IMG_TIPOS = [
  ["momento", "Momento", "Um quadro do trecho com o nome do filme e a parte."],
  ["frase", "Frase", "A fala real do trecho escrita sobre o quadro dela. Só sai de trecho com fala."],
  ["carrossel", "Carrossel", "Alguns quadros de cenas diferentes, em ordem."],
];
const CFG_IMG_VARS = ["{filme}", "{ano}", "{ano_parenteses}", "{parte}", "{frase}", "{quadros}", "{chamada_perfil}", "{hashtags}",
  "{titulo_ia}", "{descricao_ia}", "{canal}", "{link_original}"];
// os formatos que a prévia mostra: o 4:5 do Instagram e o 9:16 do TikTok
const CFG_IMG_FORMATOS = [["instagram", "4:5 (Instagram)"], ["tiktok", "9:16 (TikTok)"]];
const CFG_IMG_DIREITOS = "O quadro do filme tem os mesmos direitos do vídeo. Desde 13/03/2026 a Meta trata como não original juntar trechos com mudanças pequenas (bordas, legendas), e isso vale também para imagens.";

function cfgImgTiposHtml() {
  const ligados = new Set(valor("imagens.tipos") || []);
  // caixas sem data-acao nem data-campo: o change delegado (cfgImgAoMudar) monta a lista
  return linhaCampo("Tipos", "Os tipos se revezam em cada rede. Pelo menos um, se alguma rede tiver imagens.",
    h`<div class="cfg-img-tipos">${CFG_IMG_TIPOS.map(([id, rotulo, ajuda]) => h`<label class="caixa" title="${ajuda}"><input type="checkbox" data-cfg-img-tipo="${id}" ${ligados.has(id) ? raw("checked") : ""}><span>${rotulo}</span></label>`)}</div>`, true);
}

function cfgAbaImagens() {
  return [
    secao("O que sai"),
    cfgImgTiposHtml(),
    campo({ chave: "imagens.quadros_carrossel", tipo: "range", min: 3, max: 5, rotulo: "Quadros do carrossel" }),
    campo({ chave: "imagens.usar_ia", tipo: "switch", rotulo: "Usar o texto da IA", ajuda: "{titulo_ia} e {descricao_ia} vêm do que a IA já escreveu para o corte. Ela não é chamada de novo." }),
    secao("Escrito na imagem"),
    campo({ chave: "imagens.topo", tipo: "textarea", linhas: 2, larga: true, id: "cfg-img-topo", rotulo: "Em cima do quadro" }),
    campo({ chave: "imagens.chamada", tipo: "texto", larga: true, id: "cfg-img-chamada", rotulo: "Chamada para o vídeo", ajuda: "Desenhada embaixo só quando o vídeo daquela parte já saiu na rede." }),
    cfgVariaveis(["{filme}", "{ano}", "{ano_parenteses}", "{parte}"], "cfg-img-topo cfg-img-chamada"),
    h`<p class="nota">A fonte e as cores vêm do modelo visual em uso (Estúdio).</p>`,
    secao("Legenda do post"),
    campo({ chave: "imagens.legenda_momento", tipo: "textarea", linhas: 4, larga: true, id: "cfg-img-leg-momento", rotulo: "Momento" }),
    campo({ chave: "imagens.legenda_frase", tipo: "textarea", linhas: 4, larga: true, id: "cfg-img-leg-frase", rotulo: "Frase" }),
    campo({ chave: "imagens.legenda_carrossel", tipo: "textarea", linhas: 4, larga: true, id: "cfg-img-leg-carrossel", rotulo: "Carrossel" }),
    cfgVariaveis(CFG_IMG_VARS, "cfg-img-leg-momento cfg-img-leg-frase cfg-img-leg-carrossel"),
    h`<p class="nota">{frase} é a frase desenhada na imagem, {quadros} é o número de quadros do carrossel e {chamada_perfil} é a chamada para o vídeo, que só aparece quando o vídeo daquela parte já saiu na rede (sem ele, a linha some). No Instagram vão no máximo 5 hashtags.</p>`,
    h`<p class="nota">${CFG_IMG_DIREITOS}</p>`,
    h`<div class="linha-botoes rodape-card"><button type="button" class="btn primario" data-acao="cfg-img-salvar">${ic("check")}Salvar as imagens</button></div>`,
  ];
}

function cfgImgPreviaHtml() {
  const p = PgConfig.imgPrevia;
  const botoes = [];
  for (const [rede, rotulo] of CFG_IMG_FORMATOS) {
    for (const [tipo, nome] of CFG_IMG_TIPOS) {
      const atual = p && p.tipo === tipo && p.rede === rede;
      botoes.push(h`<button type="button" class="btn pequeno ${atual ? "primario" : ""}" data-acao="cfg-img-previa" data-tipo="${tipo}" data-rede="${rede}">${nome} · ${rotulo}</button>`);
    }
  }
  let corpo = h`<p class="nota">Escolha um tipo e um formato. A prévia usa o que está nesta aba, mesmo antes de salvar.</p>`;
  if (p && p.carregando) corpo = h`<div class="carregando-bloco"><span class="giro"></span></div>`;
  else if (p && p.erro) corpo = h`<p class="erro-texto">${p.erro}</p>`;
  else if (p && p.urls) {
    corpo = h`<div class="cfg-img-previa">${p.urls.map((u, i) => h`<img src="${midia(u)}" alt="Prévia ${TIPOS_IMAGEM[p.tipo] || p.tipo}, imagem ${i + 1}">`)}</div>
      ${p.real ? h`<p class="nota">Com um corte de verdade da sua fila.</p>` : h`<p class="nota">Prévia com uma imagem de teste: adicione um filme para ver um quadro de verdade.</p>`}`;
  }
  return h`<div class="card"><div class="card-topo"><h3>${ic("olho")}Prévia</h3></div><div class="grupo-botoes">${botoes}</div><div id="cfg-img-previa">${corpo}</div></div>`;
}

/** Caixas dos tipos: a marcação chega pelo change, sem data-acao (o clique global desfaria a marca). */
function cfgImgAoMudar(ev) {
  const caixa = ev.target;
  if (!caixa || !caixa.dataset || !caixa.dataset.cfgImgTipo) return;
  const marcados = [...document.querySelectorAll("[data-cfg-img-tipo]")].filter((c) => c.checked).map((c) => c.dataset.cfgImgTipo);
  definirValor("imagens.tipos", CFG_IMG_TIPOS.map(([id]) => id).filter((id) => marcados.includes(id)));
}
$("#conteudo").addEventListener("change", cfgImgAoMudar);

App.acoes["cfg-img-salvar"] = async (el) => {
  if (vazio(pendentesDe(["imagens"]))) { toast("Nada mudou nas imagens", "info"); return; }
  if (await ocupado(el, () => salvarConfig(["imagens"]))) toast("Imagens salvas");
};

App.acoes["cfg-img-previa"] = async (el) => {
  const tipo = el.dataset.tipo;
  const rede = el.dataset.rede;
  const token = App.navegacao;
  PgConfig.imgPrevia = { tipo, rede, carregando: true };
  montarSeMudou($("#cfg-lado"), cfgImgPreviaHtml());
  const pendente = obter(App.pendente, "imagens");
  let resultado;
  try {
    const r = await post("/imagens/previa", { tipo, rede, alteracoes: pendente && !vazio(pendente) ? { imagens: pendente } : {} });
    resultado = { tipo, rede, urls: r.urls, real: r.real };
  } catch (e) {
    resultado = { tipo, rede, erro: e.message };
  }
  // a geração leva alguns segundos: só desenha se você ainda está nesta aba
  if (!App.ativa(token) || PgConfig.aba !== "imagens") return;
  PgConfig.imgPrevia = resultado;
  montarSeMudou($("#cfg-lado"), cfgImgPreviaHtml());
};

const CFG_ABAS_FN = {
  geral: cfgAbaGeral, cortes: cfgAbaCortes, canais: cfgAbaCanais, video: cfgAbaVideo, criacao: cfgAbaCriacao,
  textos: cfgAbaTextos, imagens: cfgAbaImagens, ia: cfgAbaIa, transcricao: cfgAbaTranscricao, sistema: cfgAbaSistema,
};

/* ------------------------------------------------------------ cartões do lado */

/** Valores de exemplo para a amostra dos posts (filme de domínio público). */
function cfgVariaveisExemplo(comIa) {
  const tags = [];
  const juntar = (t) => { if (t && !tags.some((x) => x.toLowerCase() === t.toLowerCase())) tags.push(t); };
  if (valor("textos.hashtag_do_filme")) juntar("#anoitedosmortosvivos");
  if (comIa) ["#terror", "#classico"].forEach(juntar);
  (valor("textos.hashtags") || []).forEach(juntar);
  const maximo = Number(valor("textos.max_hashtags"));
  const lista = tags.slice(0, Number.isFinite(maximo) ? Math.max(0, maximo) : 8);
  return {
    vars: {
      filme: "A Noite dos Mortos-Vivos", ano: "1968", ano_parenteses: " (1968)", parte: "3",
      frase: "Eles estão vindo te pegar, Bárbara!", hashtags: lista.join(" "),
      titulo_ia: "Ninguém acreditou nela até ser tarde demais",
      descricao_ia: "O cemitério parecia tranquilo, até que ele apareceu.",
      // só os vídeos baixados de um canal têm estes (nos filmes da pasta ficam vazios)
      canal: "", link_original: "",
    },
    total: lista.length,
  };
}

function cfgAplicarModelo(modelo, vars) {
  return String(modelo || "").replace(/\{(\w+)\}/g, (_, k) => (k in vars ? String(vars[k]) : "")).trim();
}

function cfgAmostraHtml() {
  const blocos = [];
  const notas = [];
  const exemplos = [["Texto automático", false, "textos.titulo", "textos.descricao"]];
  if (valor("ia.ativo")) exemplos.push(["Quando a IA escreve", true, "textos.titulo_ia", "textos.descricao_ia"]);
  for (const [rotulo, comIa, chaveTitulo, chaveDescricao] of exemplos) {
    const { vars, total } = cfgVariaveisExemplo(comIa);
    const titulo = cfgAplicarModelo(valor(chaveTitulo), vars);
    const descricao = cfgAplicarModelo(valor(chaveDescricao), vars);
    blocos.push(h`<div class="rotulo-campo">${rotulo}</div><div class="amostra-post"><b>${titulo || "(sem título)"}</b>${descricao}</div>`);
    if (titulo.length > 100) notas.push(`O título de exemplo tem ${titulo.length} letras; o YouTube aceita até 100.`);
    if (total > 5 && !notas.some((n) => n.includes("Instagram"))) notas.push("No Instagram vão só as 5 primeiras hashtags.");
  }
  if (valor("youtube.adicionar_shorts")) notas.push("No YouTube, o título ganha #shorts.");
  return h`<div class="card previa-card"><div class="card-topo"><h3>${ic("olho")}Como o post sai</h3></div>${blocos}
    <p class="nota">Exemplo com um filme de domínio público.${notas.length ? ` ${notas.join(" ")}` : ""}</p></div>`;
}

function cfgAjudaIaHtml() {
  return h`<div class="card"><div class="card-topo"><h3>${ic("info")}Como usar uma IA no seu PC</h3></div>
    <ol class="passos">
      <li>Instale o Ollama pelo site ${link("https://ollama.com/download", "ollama.com")}.</li>
      <li>Abra o Prompt de Comando e baixe o modelo (cerca de 2,5 GB):<div class="copiavel"><code>${CFG_COMANDO_OLLAMA}</code><button type="button" class="btn pequeno icone" data-acao="copiar" data-texto="${CFG_COMANDO_OLLAMA}" title="Copiar" aria-label="Copiar o comando">${ic("copiar")}</button></div></li>
      <li>Deixe o Ollama aberto. Ele atende em http://127.0.0.1:11434.</li>
      <li>Ligue a IA ao lado, salve e clique em Testar a IA.</li>
    </ol>
    <p class="nota">Assim tudo roda no seu computador. Também funciona com o LM Studio (http://127.0.0.1:1234/v1) ou com uma API online; nesse caso, a fala de cada trecho vai para o serviço.</p>
  </div>
  ${cfgDicaModeloHtml()}`;
}

/* Quanto maior o modelo, melhores os textos. A dica usa a VRAM real da placa. */
function cfgDicaModeloHtml() {
  const g = (App.meta || {}).ia_hardware || {};
  const atual = String(valor("ia.modelo") || "").trim().toLowerCase();
  const jaUsa = Boolean(g.sugestao) && atual === String(g.sugestao).toLowerCase();
  return h`<div class="card"><div class="card-topo"><h3>${ic("raio")}Dá para melhorar depois</h3></div>
    ${g.vram_gb
      ? h`<p class="nota">Sua ${g.placa} tem <b>${fmtNum(g.vram_gb, 1)} GB</b> de memória de vídeo, então cabe um modelo de até cerca de <b>${g.cabe}</b> rodando inteiro na placa. Modelo maior escreve título e descrição melhores e erra menos o formato; em troca, leva mais tempo por corte e ocupa mais disco.</p>`
      : h`<p class="nota">Não consegui ler a memória da sua placa de vídeo. Na dúvida, um modelo de 4B em Q4 (cerca de 2,5 GB) roda em quase tudo, e um de 8B pede uns 8 GB de memória de vídeo.</p>`}
    ${g.sugestao && !jaUsa
      ? h`<p class="nota">Para experimentar, baixe e depois troque o campo Modelo:</p>
         <div class="copiavel"><code>ollama pull ${g.sugestao}</code><button type="button" class="btn pequeno icone" data-acao="copiar" data-texto="ollama pull ${g.sugestao}" title="Copiar" aria-label="Copiar o comando">${ic("copiar")}</button></div>`
      : ""}
    <p class="nota">Sem pressa: o modelo atual dá conta. Se um modelo grande demais não couber na placa, ele roda em parte na CPU e fica lento, mas continua funcionando. Sempre confira o texto de um corte em Cortes antes de deixar no automático.</p>
  </div>`;
}

function cfgAtividadeDownload() {
  return ((App.estado && App.estado.atividades) || []).find((a) => a.chave === "baixar") || null;
}

function cfgLadoTranscricaoHtml() {
  const m = App.meta || {};
  // o Visual C++ x64 só conta com a transcrição ligada no formulário (salva ou não)
  const usa = String(valor("transcricao.fonte") || "") !== "nenhuma";
  const runtime = m.whisper_runtime || { necessario: false, pronto: true, faltando: [], falha: null, url: "" };
  const faltaRuntime = usa && runtime.necessario && !runtime.pronto;
  const naoAbriu = usa && !faltaRuntime && Boolean(runtime.falha);
  const nome = String(valor("transcricao.modelo") || "");
  const modelo = (m.modelos || {})[nome];
  const baixado = Boolean(modelo && modelo.baixado);
  const tamanho = modelo ? String(modelo.descricao).split(",")[0] : "";
  const linhas = [
    [m.whisper_instalado, "whisper.cpp e detector de voz", m.whisper_instalado ? "instalados" : "não baixados"],
    [baixado, `Modelo ${nome}`, baixado ? "baixado" : `não baixado${tamanho ? ` (${tamanho})` : ""}`],
  ];
  if (usa && runtime.necessario) {
    const problema = faltaRuntime || naoAbriu;
    linhas.push([!problema, "Microsoft Visual C++ x64", faltaRuntime ? "falta instalar" : naoAbriu ? "não abriu" : "instalado",
      problema ? "alerta" : "ok"]);
  }
  const atividade = cfgAtividadeDownload();
  let acao;
  if (atividade) {
    const pct = atividade.pct === null || atividade.pct === undefined ? null : atividade.pct;
    acao = h`<div class="filme-progresso"><span class="giro"></span><span>${atividade.texto}</span>${pct !== null ? h`<div class="barra"><i style="width:${pct}%"></i></div><b>${Math.round(pct)}%</b>` : ""}</div>`;
  } else if (PgConfig.baixando) {
    acao = h`<div class="filme-progresso"><span class="giro"></span><span>Preparando o download...</span></div>`;
  } else if (faltaRuntime || naoAbriu) {
    // link comum para o instalador (sem data-acao: o clique global faria preventDefault)
    const texto = faltaRuntime
      ? `Falta o Microsoft Visual C++ x64 (${(runtime.faltando || []).join(", ")}). Sem ele, o Whisper e o detector de voz não abrem, e os filmes novos esperam na fila de análise.`
      : runtime.falha.mensagem;
    acao = h`<p class="aviso-inline">${ic("alerta")}<span>${texto}</span></p>
      <div class="grupo-botoes"><a class="btn primario" href="${runtime.url}" target="_blank" rel="noopener noreferrer">${ic("externo")}Baixar o instalador da Microsoft</a>${faltaRuntime ? h`<button type="button" class="btn" data-acao="cfg-conferir-runtime">${ic("refazer")}Já instalei, conferir de novo</button>` : ""}</div>
      <p class="nota">${faltaRuntime
        ? "Abra o instalador e escolha Instalar (ou Reparar, se ele oferecer). É o pacote x64, mesmo que você já tenha o x86. Os filmes que esperavam são analisados sozinhos, sem baixar os modelos de novo."
        : "Depois de instalar ou reparar, reabra o AutoCortes para tentar na hora; sem isso, eu tento de novo sozinho mais tarde."}</p>`;
  } else if (!m.whisper_instalado || !baixado) {
    acao = h`<button type="button" class="btn primario" data-acao="baixar-modelo" data-modelo="${nome}">${ic("baixar")}Baixar agora</button>`;
  } else if (runtime.necessario && !runtime.pronto) {
    // só com a transcrição desligada no formulário (ligada, o aviso acima aparece): sem o Visual C++, não está "tudo pronto"
    acao = h`<p class="nota">A transcrição está desligada.</p>`;
  } else {
    acao = h`<p class="nota">${ic("ok")} Tudo pronto para transcrever.</p>`;
  }
  return h`<div class="card"><div class="card-topo"><h3>${ic("microfone")}Transcrição automática</h3></div>
    <ul class="lista-simples">${linhas.map(([ok, rotulo, texto, icone]) => h`<li><span class="com-logo">${ic(icone || (ok ? "ok" : "relogio"))}${rotulo}</span><b>${texto}</b></li>`)}</ul>
    <div class="rodape-card">${acao}</div>
    <p class="nota">Se você não baixar agora, o download acontece sozinho na primeira análise. A transcrição roda no seu PC, sem mandar o áudio para a internet.</p></div>`;
}

function cfgTemLado(aba) {
  return ["textos", "imagens", "ia", "transcricao"].includes(aba);
}

function cfgLadoHtml(aba) {
  if (aba === "textos") return cfgAmostraHtml();
  if (aba === "imagens") return cfgImgPreviaHtml();
  if (aba === "ia") return cfgAjudaIaHtml();
  if (aba === "transcricao") return cfgLadoTranscricaoHtml();
  return "";
}

/* ------------------------------------------------------------ desenho */

function desenharConfig() {
  const el = $("#conteudo");
  if (!el || App.nomePagina !== "config" || !App.config) return;
  const aba = PgConfig.aba;
  const info = ABAS_CONFIG.find(([id]) => id === aba) || ABAS_CONFIG[0];
  const lado = cfgTemLado(aba);
  montar(el, h`<div class="barra-ferramentas"><div class="abas" role="tablist" aria-label="Seções das configurações">${ABAS_CONFIG.map(([id, rotulo, icone]) => h`<button type="button" role="tab" class="aba ${id === aba ? "ativa" : ""}" aria-selected="${id === aba}" data-acao="config-aba" data-aba="${id}">${ic(icone)}${rotulo}</button>`)}</div></div>
    <div class="config-layout ${lado ? "" : "sem-lado"}">
      <div class="card"><p class="nota descricao-aba">${info[3]}</p><div id="cfg-form">${formulario(CFG_ABAS_FN[aba]())}</div></div>
      ${lado ? h`<div id="cfg-lado">${cfgLadoHtml(aba)}</div>` : ""}
    </div>`);
  cfgDepoisDeDesenhar();
}

function cfgRedesenharFormulario() {
  const form = $("#cfg-form");
  if (!form) return;
  const ativo = document.activeElement;
  const foco = ativo && ativo.dataset ? ativo.dataset.campo : null;
  montar(form, formulario(CFG_ABAS_FN[PgConfig.aba]()));
  if (foco) {
    const novo = form.querySelector(`[data-campo="${foco}"]`);
    if (novo) novo.focus();
  }
  cfgDepoisDeDesenhar();
}

function cfgDepoisDeDesenhar() {
  const inicializacao = $("#sis-inicializacao");
  if (inicializacao && !inicializacao._pronto) {
    inicializacao._pronto = true;
    inicializacao.addEventListener("change", () => cfgAlternarInicializacao(inicializacao));
  }
  if ($("#sis-atualizacao")) {
    cfgLigarAtualizacao();
    cfgCarregarAtualizacao().catch(erroToast); // a versão pode ter mudado desde a última vez
  }
}

/* ------------------------------------------------------------ atualização pelo GitHub (Sistema) */

function cfgCommit(c) {
  if (!c || !c.sha) return "?";
  const data = c.data ? new Date(c.data) : null;
  return `${c.sha.slice(0, 7)}${data && !Number.isNaN(data.getTime()) ? ` de ${data.toLocaleDateString("pt-BR")}` : ""}`;
}

function cfgAtualizacaoHtml() {
  const a = PgConfig.atualizacao;
  if (!a) return h`<p class="nota">Conferindo a versão...</p>`;
  const o = a.opcoes || {};
  const linhas = [
    linhaCampo("Atualizar sozinho pelo GitHub", `A cada ${plural(o.atualizar_a_cada_dias, "dia", "dias")} eu confiro se há versão nova em github.com/${a.repositorio} e, quando nada está sendo editado nem postado e ninguém mexe no painel há 15 min, atualizo e abro de novo, com os perfis que estavam abertos. Vale para a instalação inteira${a.principal ? "" : ", e quem confere é o perfil principal (com ele fechado, não há atualização sozinha)"}.`,
      h`<label class="switch"><input type="checkbox" id="sis-atualizar" ${o.atualizar_sozinho ? raw("checked") : ""} aria-label="Atualizar sozinho pelo GitHub"><span></span></label>`),
    linhaCampo("Conferir a cada", "", h`<input type="number" class="curto" id="sis-atualizar-dias" min="1" max="30" step="1" value="${o.atualizar_a_cada_dias}" aria-label="Conferir a atualização a cada quantos dias"><span class="sufixo">dias</span>`),
  ];
  const modo = { git: "clone do git", zip: "pasta baixada em ZIP", sem_git: "clone do git, sem o programa git" }[a.modo] || a.modo;
  const situacao = [h`Instalada: <b>${cfgCommit(a.local)}</b> (${modo})`];
  if (a.verificado_em) situacao.push(` · conferido ${fmtRelativo(a.verificado_em)}`);
  if (a.proxima && a.proxima * 1000 > Date.now()) situacao.push(` · próxima conferência ${fmtRelativo(a.proxima)}`);
  linhas.push(h`<p class="nota">${situacao}</p>`);
  if (a.disponivel && a.remoto) {
    const quando = a.falhou
      ? `A instalação dela falhou aqui ${fmtRelativo(a.falhou.em)} (${a.falhou.erro || "veja o registro"}): tento de novo sozinho na próxima conferência, ou agora pelo botão.`
      : o.atualizar_sozinho ? "Eu instalo sozinho na próxima folga, ou agora pelo botão." : "Instale pelo botão quando quiser.";
    linhas.push(h`<div class="aviso-rede">${ic("baixar")}<span>Versão nova: <b>${cfgCommit(a.remoto)}</b>, “${a.remoto.mensagem || ""}”. ${quando}</span></div>`);
  } else if (a.motivo) {
    linhas.push(h`<p class="aviso-inline">${ic("info")}Não atualizo sozinho: ${a.motivo}.</p>`);
  } else if (a.verificado_em && !a.erro) {
    linhas.push(h`<p class="nota">${ic("ok")}Em dia com o GitHub.</p>`);
  }
  if (a.erro) linhas.push(h`<p class="aviso-inline">${ic("alerta")}${a.erro}</p>`);
  if (a.aplicado && a.aplicado.para) {
    linhas.push(h`<p class="nota">Última atualização: ${cfgCommit(a.aplicado.de)} para ${cfgCommit(a.aplicado.para)}${a.aplicado_em ? `, ${fmtRelativo(a.aplicado_em)}` : ""}.</p>`);
  }
  linhas.push(h`<div class="linha-botoes"><button type="button" class="btn" data-acao="sis-procurar">${ic("busca")}Procurar agora</button>${a.disponivel ? h`<button type="button" class="btn primario" data-acao="sis-atualizar-agora">${ic("baixar")}Atualizar agora</button>` : ""}</div>`);
  return linhas;
}

async function cfgCarregarAtualizacao() {
  PgConfig.atualizacao = await api("/atualizacao");
  if (App.nomePagina === "config" && PgConfig.aba === "sistema") {
    montarSeMudou($("#sis-atualizacao"), cfgAtualizacaoHtml());
    cfgLigarAtualizacao();
  }
}

function cfgLigarAtualizacao() {
  const chave = $("#sis-atualizar");
  if (chave && !chave._pronto) {
    chave._pronto = true;
    chave.addEventListener("change", () => cfgSalvarAtualizacao({ atualizar_sozinho: chave.checked }, chave));
  }
  const dias = $("#sis-atualizar-dias");
  if (dias && !dias._pronto) {
    dias._pronto = true;
    dias.addEventListener("change", () => cfgSalvarAtualizacao({ atualizar_a_cada_dias: Number(dias.value) }, dias));
  }
}

async function cfgSalvarAtualizacao(mudanca, campo) {
  campo.disabled = true;
  try {
    PgConfig.atualizacao = await post("/atualizacao/opcoes", mudanca);
    toast("Atualização: preferência salva (vale para a instalação inteira)");
  } catch (e) {
    erroToast(e);
  } finally {
    campo.disabled = false;
    montarSeMudou($("#sis-atualizacao"), cfgAtualizacaoHtml());
    cfgLigarAtualizacao();
  }
}

App.acoes["sis-procurar"] = async (el) => {
  PgConfig.atualizacao = await ocupado(el, () => post("/atualizacao/verificar"));
  montarSeMudou($("#sis-atualizacao"), cfgAtualizacaoHtml());
  cfgLigarAtualizacao();
  const a = PgConfig.atualizacao;
  toast(a.erro ? "Não consegui conferir agora" : a.disponivel ? "Há uma versão nova" : "Em dia com o GitHub", a.erro ? "erro" : "info");
};

App.acoes["sis-atualizar-agora"] = async (el) => {
  const ok = await confirmar({
    titulo: "Atualizar o AutoCortes agora?",
    texto: "Eu fecho os perfis abertos, instalo a versão nova e abro tudo de novo, como estava. Leva um minuto, e esta janela se recarrega sozinha. O que você ainda não salvou aqui se perde.",
    botao: "Atualizar agora",
  });
  if (!ok) return;
  await ocupado(el, () => post("/atualizacao/aplicar"));
  App.atualizando = true;
  mostrarFaixa("atualizando");
};

async function cfgAlternarInicializacao(caixa) {
  const pedido = caixa.checked;
  caixa.disabled = true;
  try {
    const r = await post("/sistema/inicializacao", { ativo: pedido });
    if (App.meta) {
      const avisava = App.meta.inicializacao_outra;
      App.meta.inicializacao = r.ativo;
      App.meta.inicializacao_outra = false; // ligar regrava a entrada; desligar apaga
      if (avisava) cfgRedesenharFormulario();
    }
    caixa.checked = r.ativo;
    toast(r.ativo ? "O AutoCortes vai ligar junto com o Windows" : "O AutoCortes não liga mais com o Windows");
  } catch (e) {
    caixa.checked = !pedido;
    erroToast(e);
  } finally {
    caixa.disabled = false;
  }
}

// guarda o último campo de texto usado, para os botões de {variáveis}
document.addEventListener("focusin", (ev) => {
  const el = ev.target;
  if (App.nomePagina === "config" && el && el.id && el.id.startsWith("cfg-") && (el.tagName === "INPUT" || el.tagName === "TEXTAREA")) {
    PgConfig.ultimoCampo = el.id;
  }
});

/* ------------------------------------------------------------ ações */

App.acoes["config-aba"] = (el) => {
  PgConfig.aba = el.dataset.aba;
  history.replaceState(null, "", `#/config?aba=${PgConfig.aba}`);
  desenharConfig();
  const aba = $(`[data-acao="config-aba"][data-aba="${PgConfig.aba}"]`);
  if (aba) aba.focus();
};


App.acoes["inserir-variavel"] = (el) => {
  const campos = String(el.dataset.campos || "").split(/\s+/).filter(Boolean);
  const id = campos.includes(PgConfig.ultimoCampo) ? PgConfig.ultimoCampo : campos[0];
  const alvo = id && document.getElementById(id);
  if (!alvo) return;
  const inicio = alvo.selectionStart ?? alvo.value.length;
  const fim = alvo.selectionEnd ?? inicio;
  alvo.setRangeText(el.dataset.var, inicio, fim, "end");
  alvo.focus();
  alvo.dispatchEvent(new Event("input", { bubbles: true }));
};

App.acoes["ia-testar"] = async (el) => {
  try {
    const r = await ocupado(el, () => post("/ia/testar", { alteracoes: App.pendente }));
    PgConfig.iaResultado = { ok: true, texto: r.mensagem };
  } catch (e) {
    PgConfig.iaResultado = { ok: false, texto: e.message };
  }
  montar($("#ia-resultado"), cfgResultadoIaHtml());
};

App.acoes["ia-modelos"] = async (el) => {
  try {
    const r = await ocupado(el, () => post("/ia/modelos", { alteracoes: App.pendente }));
    PgConfig.modelosIa = r.modelos || [];
  } catch (e) {
    PgConfig.modelosIa = null;
    PgConfig.iaResultado = { ok: false, texto: e.message };
    montar($("#ia-resultado"), cfgResultadoIaHtml());
  }
  montar($("#ia-modelos"), cfgModelosIaHtml());
};

App.acoes["ia-usar-modelo"] = (el) => {
  const modelo = el.dataset.modelo;
  definirValor("ia.modelo", modelo);
  const campoModelo = $("#cfg-ia-modelo");
  if (campoModelo) campoModelo.value = modelo;
  montar($("#ia-modelos"), cfgModelosIaHtml());
};

App.acoes["baixar-modelo"] = async (el) => {
  const modelo = el.dataset.modelo || String(valor("transcricao.modelo") || "");
  await ocupado(el, () => post("/ferramentas/baixar", { modelo }));
  PgConfig.baixando = Date.now();
  PgConfig.baixandoVisto = false;
  PgConfig.modeloBaixando = modelo;
  toast("Download iniciado. O andamento aparece aqui e na página Início.", "info");
  montarSeMudou($("#cfg-lado"), cfgLadoTranscricaoHtml());
};

async function cfgConferirDownload(e) {
  if (!PgConfig.baixando) return;
  if (e.atividades.some((a) => a.chave === "baixar")) {
    PgConfig.baixandoVisto = true;
    return;
  }
  if (!PgConfig.baixandoVisto && Date.now() - PgConfig.baixando < 6000) return;
  const modelo = PgConfig.modeloBaixando;
  PgConfig.baixando = 0;
  PgConfig.baixandoVisto = false;
  try {
    await carregarConfig();
  } catch (err) {
    return;
  }
  const m = App.meta.modelos[modelo];
  const semRuntime = Boolean(App.meta.whisper_runtime && !App.meta.whisper_runtime.pronto);
  const pronto = App.meta.whisper_instalado && m && m.baixado && !semRuntime;
  toast(pronto ? "Transcrição pronta para usar"
    : semRuntime ? "Falta o Microsoft Visual C++ x64: veja o aviso ao lado." : "O download não terminou. Veja o motivo no Registro.",
  pronto ? "ok" : "erro", 7000);
  if (App.nomePagina === "config" && PgConfig.aba === "transcricao") desenharConfig();
}

App.acoes["cfg-conferir-runtime"] = async (el) => {
  // relê só o /config: o que não foi salvo continua em App.pendente
  await ocupado(el, () => carregarConfig());
  const runtime = App.meta.whisper_runtime || { pronto: true, faltando: [] };
  if (runtime.pronto) toast("Microsoft Visual C++ x64 encontrado: a análise dos filmes continua sozinha.", "ok", 7000);
  else toast(`Ainda falta o Microsoft Visual C++ x64: ${(runtime.faltando || []).join(", ")}.`, "erro", 7000);
  if (App.nomePagina === "config" && PgConfig.aba === "transcricao") montarSeMudou($("#cfg-lado"), cfgLadoTranscricaoHtml());
};

/* ------------------------------------------------------------ página */

App.paginas.config = {
  titulo: "Configurações",
  subtitulo: "Tudo o que dá para ajustar no AutoCortes",
  async render(el, token) {
    await carregarConfig();
    if (!App.ativa(token)) return;
    const aba = consultaDaRota().get("aba");
    if (aba && CFG_ABAS_FN[aba]) PgConfig.aba = aba;
    desenharConfig();
  },
  aoMudar(caminho) {
    if (App.nomePagina !== "config") return;
    const aba = PgConfig.aba;
    if (CFG_REDESENHA.has(caminho)) cfgRedesenharFormulario();
    if (aba === "textos") montarSeMudou($("#cfg-lado"), cfgAmostraHtml());
    if (aba === "ia" && caminho === "ia.modelo") montarSeMudou($("#ia-modelos"), cfgModelosIaHtml());
    if (aba === "transcricao" && (caminho === "transcricao.modelo" || caminho === "transcricao.fonte")) {
      montarSeMudou($("#cfg-lado"), cfgLadoTranscricaoHtml());
    }
  },
  aoSalvar() {
    desenharConfig();
  },
  async recarregar() {
    await carregarConfig();
    desenharConfig();
  },
  async aoEstado(e) {
    await cfgConferirDownload(e);
    if (App.nomePagina === "config" && PgConfig.aba === "transcricao") montarSeMudou($("#cfg-lado"), cfgLadoTranscricaoHtml());
  },
};

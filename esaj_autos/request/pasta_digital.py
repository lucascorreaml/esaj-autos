"""
Módulo que obtém a cópia integral dos autos na Pasta Digital do
e-SAJ (TJSP).

O e-SAJ não entrega os autos completos em uma única requisição. O
fluxo real, o mesmo que a interface web dispara quando se pede o
download na Pasta Digital, tem quatro etapas:

1. **Abrir a pasta digital** do processo, o que devolve uma URL
   assinada (com *ticket*) para a árvore de documentos;
2. **Ler a árvore**, uma estrutura JSON embutida na página, de onde
   saem os identificadores de cada peça dos autos;
3. **Solicitar a preparação** do arquivo, enviando todas as peças de
   uma vez. O servidor responde com um *localizador*, porque a
   montagem é assíncrona e pode demorar minutos em processos
   volumosos;
4. **Aguardar e baixar**: consulta-se o localizador até o e-SAJ
   devolver a URL do arquivo pronto.

O e-SAJ oferece dois formatos, controlados por `separar_documentos`:
um ZIP com uma peça por PDF (padrão) ou um PDF único com os autos
inteiros. Ambos são preservados exatamente como entregues.
"""

import json
import logging
import re
import time
from pathlib import Path
from typing import Dict, List, Literal, Optional, Tuple

import requests

from esaj_autos.exceptions import (
    DownloadError,
    ESAJError,
    ESAJIndisponivelError,
    LimiteAcessoExcedidoError,
    PreparacaoTimeoutError,
    ProcessoComSenhaError,
    ProcessoNaoEncontradoError,
    SemAcessoAosAutosError,
    SessaoExpiradaError,
)
from esaj_autos.request.session import URL_BASE

logger = logging.getLogger(__name__)

# API pública de consulta do TJSP. Resolve o número CNJ para o
# "cdProcesso", o código interno que todo o restante do fluxo usa.
URL_API_BUSCA = 'https://api.tjsp.jus.br/processo/{grau}/search/numproc/{numero}'

# Consulta processual em HTML, usada como alternativa quando a API
# pública não responde.
URL_BUSCA_HTML = f'{URL_BASE}/{{grau}}/search.do'

# Página de detalhes do processo. Precisa ser visitada antes de abrir
# a pasta digital: o e-SAJ valida o acesso contra o processo aberto na
# sessão e, sem essa visita, recusa com "Não foi possível validar o
# seu acesso a esse recurso".
URL_SHOW = f'{URL_BASE}/{{grau}}/show.do'

# Abertura da pasta digital. O caminho difere entre os graus.
URL_ABRE_PASTA_1GRAU = f'{URL_BASE}/cpopg/abrirPastaDigital.do'
URL_ABRE_PASTA_2GRAU = f'{URL_BASE}/cposg/verificarAcessoPastaDigital.do'

# Preparação assíncrona do arquivo e consulta do resultado.
URL_PREPARA = f'{URL_BASE}/pastadigital/salvarDocumentoPreparado.do'
URL_BUSCA_PRONTO = f'{URL_BASE}/pastadigital/buscarDocumentoFinalizado.do'

# Vocabulário de grau usado em todo o pacote.
GRAUS = {'Primeiro Grau': 'cpopg', 'Segundo Grau': 'cposg'}

# Trechos que identificam o motivo da recusa. São procurados apenas
# dentro da mensagem de erro do e-SAJ, nunca no corpo inteiro da
# página: as páginas do processo trazem esses mesmos textos no
# JavaScript que configura os avisos, mesmo quando não há recusa
# alguma, e a busca solta produziria falso positivo.
MSG_SEM_ACESSO = 'não foi possível validar o seu acesso'
MSG_LIMITE = 'limite diário de acessos'
MSG_SENHA = 'senha do processo'

# Container em que o e-SAJ escreve as mensagens de erro.
PADRAO_MENSAGEM_ERRO = re.compile(
    r'id=["\']mensagemRetorno["\'][^>]*>(.*?)</td>',
    re.IGNORECASE | re.DOTALL,
)


# O e-SAJ responde em "chunked", sem Content-Length. Quando a conexão
# cai antes do terminador, o cliente vê
# "IncompleteRead(0 bytes read, 2 more expected)" — os dois bytes do
# CRLF final. É transitório e some ao repetir, o que só é seguro
# porque todas as requisições repetidas aqui são GET.
TENTATIVAS_PADRAO = 3
ESPERA_ENTRE_TENTATIVAS = 3

# Quedas de conexão e corpos truncados. Não inclui tempo de leitura
# esgotado: repetir uma espera longa só a multiplicaria.
QUEDAS_DE_CONEXAO = (
    requests.ConnectionError,
    requests.exceptions.ChunkedEncodingError,
)


def _get(
    sessao: requests.Session,
    url: str,
    descricao: str,
    tentativas: int = TENTATIVAS_PADRAO,
    **kwargs,
) -> requests.Response:
    """
    Faz um GET que tolera queda de conexão.

    :param sessao: sessão HTTP
    :param url: endereço a buscar
    :param descricao: o que se estava buscando, para a mensagem de erro
    :param tentativas: quantas vezes tentar
    :param kwargs: repassados ao `requests`
    :return: a resposta
    :raises ESAJIndisponivelError: quando todas as tentativas falham
    """
    ultima_queda = None

    for tentativa in range(1, tentativas + 1):
        try:
            return sessao.get(url, **kwargs)

        except QUEDAS_DE_CONEXAO as e:
            ultima_queda = e
            if tentativa < tentativas:
                logger.info(
                    'Conexão caiu ao %s (tentativa %s de %s). Repetindo...',
                    descricao,
                    tentativa,
                    tentativas,
                )
                time.sleep(ESPERA_ENTRE_TENTATIVAS)

        except requests.RequestException as e:
            raise ESAJIndisponivelError(
                f'Não foi possível {descricao}: {e}'
            ) from e

    raise ESAJIndisponivelError(
        f'Não foi possível {descricao} após {tentativas} tentativas. '
        f'A conexão com o e-SAJ caiu em todas elas. Último erro: '
        f'{ultima_queda}'
    )


def normaliza_cnj(numero: str) -> Tuple[str, str]:
    """
    Normaliza um número CNJ.

    :param numero: número do processo, com ou sem pontuação
    :return: tupla com (apenas dígitos, número formatado)
    :raises ValueError: quando o número não tem 20 dígitos ou não é
        do TJSP
    """
    digitos = re.sub(r'\D', '', str(numero))

    if not digitos:
        raise ValueError(
            f'"{numero}" não contém nenhum dígito, então não é um '
            f'número de processo. O padrão CNJ é '
            f'"1234567-89.2020.8.26.0100".'
        )

    if len(digitos) != 20:
        raise ValueError(
            f'O número CNJ deve ter 20 dígitos, mas "{numero}" tem '
            f'{len(digitos)}. O padrão é "1234567-89.2020.8.26.0100".'
        )

    # Posições 13-16 do padrão CNJ identificam o tribunal (J.TR).
    # O TJSP é sempre "826".
    if digitos[13:16] != '826':
        raise ValueError(
            f'O número "{numero}" não é do TJSP (esperado ".8.26."). '
            f'Este programa atende apenas o e-SAJ do TJSP.'
        )

    formatado = (
        f'{digitos[0:7]}-{digitos[7:9]}.{digitos[9:13]}.'
        f'{digitos[13]}.{digitos[14:16]}.{digitos[16:20]}'
    )
    return digitos, formatado


def _verifica_resposta(resposta: requests.Response) -> None:
    """
    Traduz respostas HTTP de erro nas exceções do pacote.

    :param resposta: resposta HTTP a avaliar
    :raises SessaoExpiradaError: quando o e-SAJ recusa por falta de
        sessão (401/403)
    :raises ESAJIndisponivelError: quando o e-SAJ responde com 5xx
    """
    if resposta.status_code in (401, 403):
        raise SessaoExpiradaError(
            f'O e-SAJ recusou a requisição (HTTP '
            f'{resposta.status_code}). Ou a sessão expirou, ou o '
            f'acesso à pasta digital foi obtido em outra sessão e já '
            f'não vale nesta.'
        )

    if resposta.status_code >= 500:
        raise ESAJIndisponivelError(
            f'O e-SAJ respondeu com HTTP {resposta.status_code}. '
            'O serviço pode estar em manutenção. Tente mais tarde.'
        )


def extrai_mensagem_de_erro(html: str) -> Optional[str]:
    """
    Devolve a mensagem de erro que o e-SAJ escreveu na página.

    :param html: corpo da resposta
    :return: a mensagem, em texto limpo, ou `None` se a página não
        traz erro
    """
    achado = PADRAO_MENSAGEM_ERRO.search(html or '')
    if not achado:
        return None

    # Remove as tags e normaliza os espaços.
    texto = re.sub(r'<[^>]+>', ' ', achado.group(1))
    texto = re.sub(r'\s+', ' ', texto).strip()
    return texto or None


def _verifica_mensagem_de_recusa(
    html: str, resposta_e_erro: bool = False
) -> None:
    """
    Interpreta as páginas de recusa da pasta digital.

    O e-SAJ responde HTTP 200 mesmo quando nega o acesso, informando
    o motivo apenas no corpo da página.

    Por padrão a leitura se restringe à mensagem de erro do e-SAJ,
    porque as páginas normais do processo trazem esses mesmos textos
    no JavaScript que configura os avisos — lê-los soltos recusaria
    processos perfeitamente acessíveis.

    :param html: corpo da resposta
    :param resposta_e_erro: `True` quando já se sabe que a resposta
        não é um sucesso. Aí o corpo inteiro pode ser lido, o que
        alcança as recusas exibidas fora do container de mensagem,
        como o limite diário.
    :raises LimiteAcessoExcedidoError: limite diário atingido
    :raises ProcessoComSenhaError: processo protegido por senha
    :raises SemAcessoAosAutosError: acesso negado por outro motivo
    """
    mensagem = extrai_mensagem_de_erro(html)

    if mensagem is None:
        if not resposta_e_erro:
            return
        # Sem container de mensagem, mas a resposta é sabidamente um
        # erro: usa o corpo inteiro para descobrir o motivo.
        mensagem = re.sub(r'<[^>]+>', ' ', html or '')
        mensagem = re.sub(r'\s+', ' ', mensagem).strip()
        if not mensagem:
            return

    texto = mensagem.lower()

    if MSG_LIMITE in texto:
        raise LimiteAcessoExcedidoError(
            'O e-SAJ recusou o acesso: limite diário de acessos à '
            'pasta digital de processos sem vínculo com o usuário. '
            'O limite se renova no dia seguinte. '
            f'Mensagem do e-SAJ: "{mensagem}"'
        )

    if MSG_SENHA in texto:
        raise ProcessoComSenhaError(
            'O processo exige "senha do processo" para liberar a '
            'pasta digital. Essa senha não é fornecida pelo login '
            f'comum do e-SAJ. Mensagem do e-SAJ: "{mensagem}"'
        )

    if MSG_SEM_ACESSO in texto:
        raise SemAcessoAosAutosError(
            'O e-SAJ não validou o acesso à pasta digital deste '
            'processo. Verifique se você está autenticado e se tem '
            'permissão para acessar os autos (segredo de justiça, '
            'ausência de vínculo ou autos não digitais). '
            f'Mensagem do e-SAJ: "{mensagem}"'
        )

    # Erro reconhecido como tal, mas de motivo não catalogado.
    resumo = mensagem if len(mensagem) <= 300 else f'{mensagem[:300]}...'
    raise SemAcessoAosAutosError(
        f'O e-SAJ recusou o acesso à pasta digital: "{resumo}"'
    )


def resolve_cd_processo(
    sessao: requests.Session,
    numero_cnj: str,
    grau: Literal['Primeiro Grau', 'Segundo Grau'] = 'Primeiro Grau',
    timeout: int = 60,
) -> str:
    """
    Descobre o "cdProcesso" a partir do número CNJ.

    Tenta primeiro a API pública de consulta do TJSP, que devolve
    JSON e é mais estável. Se ela falhar, cai para a consulta
    processual em HTML.

    :param sessao: sessão HTTP
    :param numero_cnj: número do processo no padrão CNJ
    :param grau: grau de jurisdição
    :param timeout: tempo limite de cada requisição, em segundos
    :return: código interno do processo no e-SAJ
    :raises ProcessoNaoEncontradoError: quando o processo não existe
    """
    digitos, formatado = normaliza_cnj(numero_cnj)
    sigla = GRAUS[grau]

    cd_processo = _resolve_por_api(sessao, digitos, sigla, timeout)
    if cd_processo is None:
        cd_processo = _resolve_por_html(
            sessao, digitos, formatado, sigla, timeout
        )

    if cd_processo is None:
        raise ProcessoNaoEncontradoError(
            f'O processo "{formatado}" não foi localizado no e-SAJ '
            f'do TJSP em "{grau}".'
        )

    logger.info('Processo %s resolvido para cdProcesso=%s', formatado, cd_processo)
    return cd_processo


def _resolve_por_api(
    sessao: requests.Session, digitos: str, sigla: str, timeout: int
) -> Optional[str]:
    """
    Resolve o "cdProcesso" pela API pública do TJSP.

    :return: o código, ou `None` se a API não resolver
    """
    url = URL_API_BUSCA.format(grau=sigla, numero=digitos)
    try:
        resposta = sessao.get(url, timeout=timeout)
        if resposta.status_code != 200:
            return None

        dados = resposta.json()
        if isinstance(dados, dict):
            dados = [dados]
        if not dados:
            return None

        return dados[0].get('cdProcesso') or None

    except (requests.RequestException, json.JSONDecodeError, ValueError) as e:
        logger.debug('API pública não resolveu o processo: %s', e)
        return None


def _resolve_por_html(
    sessao: requests.Session,
    digitos: str,
    formatado: str,
    sigla: str,
    timeout: int,
) -> Optional[str]:
    """
    Resolve o "cdProcesso" pela consulta processual em HTML.

    A consulta redireciona para a página do processo, cuja URL traz
    o código interno.

    O e-SAJ só faz esse redirecionamento na **primeira** consulta de
    cada sessão: nas seguintes, devolve o formulário de busca porque
    passa a exigir um `conversationId`. Como a sessão recebida aqui
    normalmente já foi usada no login, a consulta é repetida em uma
    sessão isolada quando a primeira tentativa não resolve. A
    consulta processual é pública, de modo que a sessão isolada
    resolve os mesmos processos.

    :return: o código, ou `None` se a consulta não resolver
    """
    # O e-SAJ divide o número em "corpo" e "foro" no formulário.
    parametros = {
        'conversationId': '',
        'dadosConsulta.localPesquisa.cdLocal': '-1',
        'cbPesquisa': 'NUMPROC',
        'dadosConsulta.tipoNuProcesso': 'UNIFICADO',
        'numeroDigitoAnoUnificado': formatado[:15],
        'foroNumeroUnificado': digitos[16:20],
        'dadosConsulta.valorConsultaNuUnificado': formatado,
        'dadosConsulta.valorConsulta': '',
        'uuidCaptcha': '',
    }
    url = URL_BUSCA_HTML.format(grau=sigla)

    # Primeiro na sessão autenticada, que enxerga processos restritos
    # ao usuário; depois em uma sessão limpa, que garante o
    # redirecionamento.
    codigo = _busca_codigo(sessao, url, parametros, timeout)
    if codigo is None:
        with requests.Session() as limpa:
            limpa.verify = sessao.verify
            limpa.headers.update(sessao.headers)
            codigo = _busca_codigo(limpa, url, parametros, timeout)

    return codigo


def _busca_codigo(
    sessao: requests.Session,
    url: str,
    parametros: Dict[str, str],
    timeout: int,
) -> Optional[str]:
    """
    Executa a consulta e procura o código do processo na resposta.

    :return: o código, ou `None` quando a resposta não o traz
    """
    resposta = _get(
        sessao,
        url,
        descricao='consultar o processo no e-SAJ',
        params=parametros,
        timeout=timeout,
        allow_redirects=True,
    )
    _verifica_resposta(resposta)

    # O código aparece na URL final do redirecionamento ou, quando a
    # consulta devolve uma lista, nos links do corpo.
    for texto in (resposta.url, resposta.text):
        achado = re.search(r'processo\.codigo=([A-Za-z0-9]+)', texto or '')
        if achado:
            return achado.group(1)

    return None


def abre_pasta_digital(
    sessao: requests.Session,
    cd_processo: str,
    grau: Literal['Primeiro Grau', 'Segundo Grau'] = 'Primeiro Grau',
    timeout: int = 60,
) -> str:
    """
    Abre a pasta digital e devolve a URL assinada da árvore de
    documentos.

    :param sessao: sessão HTTP autenticada
    :param cd_processo: código interno do processo
    :param grau: grau de jurisdição
    :param timeout: tempo limite de cada requisição, em segundos
    :return: URL da pasta digital, já com o *ticket* de acesso
    :raises SemAcessoAosAutosError: quando o e-SAJ nega o acesso
    """
    sigla = GRAUS[grau]

    # O e-SAJ exige que o processo tenha sido aberto na sessão antes
    # de liberar a pasta digital. O que importa aqui é só esse efeito:
    # o conteúdo da página não é usado. Por isso a resposta é pedida
    # em fluxo e fechada sem leitura — em processos volumosos essa
    # página tem megabytes, e baixá-la seria transferir muito, sem
    # proveito, com risco de a conexão cair no meio.
    resposta_detalhes = _get(
        sessao,
        URL_SHOW.format(grau=sigla),
        descricao='abrir os detalhes do processo',
        params={'processo.codigo': cd_processo, 'gateway': 'true'},
        timeout=timeout,
        stream=True,
    )
    resposta_detalhes.close()
    _verifica_resposta(resposta_detalhes)

    if grau == 'Primeiro Grau':
        url = URL_ABRE_PASTA_1GRAU
        parametros: Dict[str, str] = {'processo.codigo': cd_processo}
    else:
        url = URL_ABRE_PASTA_2GRAU
        parametros = {'cdProcesso': cd_processo, 'conversationId': ''}

    resposta = _get(
        sessao,
        url,
        descricao='abrir a pasta digital',
        params=parametros,
        timeout=timeout,
    )
    _verifica_resposta(resposta)

    corpo = resposta.text or ''

    # Em caso de sucesso o corpo é a própria URL da pasta digital,
    # sem HTML em volta. Só quando não é esse o caso é que vale
    # procurar a mensagem de recusa.
    limpo = corpo.strip()
    if limpo.startswith('http') and '<' not in limpo:
        return limpo

    _verifica_mensagem_de_recusa(corpo, resposta_e_erro=True)

    # Rede de segurança: resposta que não é sucesso nem traz motivo.
    raise SemAcessoAosAutosError(
        'O e-SAJ não devolveu a URL da pasta digital deste processo. '
        'Verifique se os autos são digitais e se você tem permissão '
        'de acesso.'
    )


def entra_na_pasta(
    sessao: requests.Session, url_pasta: str, timeout: int = 120
) -> None:
    """
    Estabelece a sessão dentro do aplicativo da pasta digital.

    Obter o endereço com *ticket* não basta: é preciso usá-lo. O
    `pastadigital` é um aplicativo à parte, com sessão própria, e só
    a reconhece quando o endereço assinado é efetivamente visitado.
    Sem essa visita, `buscarDocumentoFinalizado` responde 401 mesmo
    com o login do portal perfeitamente válido.

    No download comum isso acontece por tabela, ao ler a árvore de
    documentos. Ao recolher um pedido antigo, em que a árvore não é
    lida, a visita precisa ser feita de propósito.

    O corpo não é usado: a página pesa megabytes em processos
    volumosos e o que importa está nos cabeçalhos.

    :param sessao: sessão HTTP autenticada
    :param url_pasta: endereço assinado da pasta digital
    :param timeout: tempo limite da requisição, em segundos
    """
    resposta = _get(
        sessao,
        url_pasta,
        descricao='entrar na pasta digital',
        timeout=timeout,
        stream=True,
    )
    resposta.close()
    _verifica_resposta(resposta)


def _extrai_request_scope(html: str) -> dict:
    """
    Extrai a árvore de documentos embutida na página da pasta digital.

    A página traz um JSON atribuído à variável JavaScript
    `requestScope`. A extração acompanha o balanceamento de chaves,
    em vez de cortar no primeiro ";", porque o próprio JSON contém
    esse caractere em nomes de peças.

    :param html: HTML da pasta digital
    :return: a árvore de documentos já convertida
    :raises SemAcessoAosAutosError: quando a árvore não está na página
    """
    marcador = re.search(r'requestScope\s*=\s*', html)
    if not marcador:
        raise SemAcessoAosAutosError(
            'A página da pasta digital não trouxe a lista de '
            'documentos ("requestScope"). Os autos podem não estar '
            'disponíveis digitalmente ou o e-SAJ mudou o formato '
            'da página.'
        )

    inicio = marcador.end()
    if inicio >= len(html) or html[inicio] not in '[{':
        raise SemAcessoAosAutosError(
            'A lista de documentos da pasta digital veio em formato '
            'inesperado.'
        )

    abre = html[inicio]
    fecha = ']' if abre == '[' else '}'
    profundidade = 0
    dentro_de_texto = False
    escapado = False

    for posicao in range(inicio, len(html)):
        caractere = html[posicao]

        if escapado:
            escapado = False
            continue

        if caractere == '\\':
            escapado = True
            continue

        if caractere == '"':
            dentro_de_texto = not dentro_de_texto
            continue

        if dentro_de_texto:
            continue

        if caractere == abre:
            profundidade += 1
        elif caractere == fecha:
            profundidade -= 1
            if profundidade == 0:
                bruto = html[inicio : posicao + 1]
                try:
                    return json.loads(bruto)
                except json.JSONDecodeError as e:
                    raise SemAcessoAosAutosError(
                        f'A lista de documentos da pasta digital não '
                        f'pôde ser interpretada: {e}'
                    ) from e

    raise SemAcessoAosAutosError(
        'A lista de documentos da pasta digital está truncada.'
    )


def le_arvore_documentos(
    sessao: requests.Session, url_pasta: str, timeout: int = 120
) -> dict:
    """
    Carrega a página da pasta digital e devolve a árvore de documentos.

    :param sessao: sessão HTTP autenticada
    :param url_pasta: URL assinada devolvida por `abre_pasta_digital`
    :param timeout: tempo limite da requisição, em segundos
    :return: árvore de documentos
    """
    # É a maior página do fluxo e o corpo dela é indispensável: em um
    # processo volumoso, é aqui que a queda de conexão mais dói.
    resposta = _get(
        sessao,
        url_pasta,
        descricao='carregar a pasta digital',
        timeout=timeout,
    )
    _verifica_resposta(resposta)

    # A árvore é o resultado esperado. Só quando ela não está na
    # página é que se procura o motivo da recusa: a página normal da
    # pasta digital traz, no JavaScript, os mesmos textos usados para
    # identificar recusas.
    try:
        return _extrai_request_scope(resposta.text)

    except SemAcessoAosAutosError:
        _verifica_mensagem_de_recusa(resposta.text)
        raise


def coleta_parametros(arvore) -> List[str]:
    """
    Percorre a árvore e reúne o identificador de cada peça dos autos.

    A pasta digital é uma árvore em três camadas: a raiz é o processo,
    abaixo dela vêm as peças e, dentro de cada peça, as suas páginas.
    O campo `data.parametros` aparece já no nível da peça e vale pelo
    documento inteiro.

    Por isso a varredura **para** no primeiro nó que traz
    `parametros`: descer além disso passaria a pedir página por
    página, o que multiplica o pedido por dezenas de milhares de itens
    em processos volumosos. Nós sem `parametros` são apenas
    agrupadores (pastas, volumes) e continuam sendo percorridos, de
    modo que processos organizados em volumes venham completos.

    :param arvore: árvore de documentos da pasta digital
    :return: lista de identificadores, na ordem dos autos e sem
        repetições
    """
    parametros: List[str] = []

    def percorre(no) -> None:
        if isinstance(no, list):
            for item in no:
                percorre(item)
            return

        if not isinstance(no, dict):
            return

        dados = no.get('data') or {}
        if isinstance(dados, dict):
            valor = dados.get('parametros')
            if valor:
                # É a peça: o identificador cobre todas as páginas.
                parametros.append(valor)
                return

        percorre(no.get('children') or [])

    # A raiz representa o processo, não uma peça: mesmo que traga
    # "parametros", a varredura começa pelos filhos.
    if isinstance(arvore, dict) and arvore.get('children'):
        percorre(arvore['children'])
    else:
        percorre(arvore)

    # Remove repetições preservando a ordem dos autos.
    vistos = set()
    unicos = []
    for item in parametros:
        if item not in vistos:
            vistos.add(item)
            unicos.append(item)

    return unicos


def descreve_arvore(arvore, max_nivel: int = 3) -> List[str]:
    """
    Descreve a forma da árvore, sem revelar o conteúdo.

    Diz, por nível, quantos nós existem, quantos são peças (têm
    `parametros`) e quais campos o nó traz. Serve para conferir se a
    leitura da pasta digital está tomando o nível certo como peça,
    sem expor dados do processo.

    :param arvore: árvore de documentos
    :param max_nivel: até que profundidade descrever
    :return: linhas do relatório
    """
    # Por nível: total de nós, quantos têm "parametros", campos vistos
    # em "data" e um exemplo de identificador.
    resumo: Dict[int, dict] = {}

    def percorre(no, nivel) -> None:
        if isinstance(no, list):
            for item in no:
                percorre(item, nivel)
            return
        if not isinstance(no, dict) or nivel > max_nivel:
            return

        info = resumo.setdefault(
            nivel,
            {'nos': 0, 'com_parametros': 0, 'com_filhos': 0,
             'campos': set(), 'exemplo': None},
        )
        info['nos'] += 1

        dados = no.get('data') or {}
        if isinstance(dados, dict):
            info['campos'].update(dados.keys())
            valor = dados.get('parametros')
            if valor:
                info['com_parametros'] += 1
                if info['exemplo'] is None:
                    info['exemplo'] = str(valor)

        filhos = no.get('children') or []
        if filhos:
            info['com_filhos'] += 1
            percorre(filhos, nivel + 1)

    percorre(arvore, 0)

    linhas = [f'raiz: {type(arvore).__name__}']
    for nivel in sorted(resumo):
        info = resumo[nivel]
        exemplo = info['exemplo']
        # Mostra só o formato do identificador, não o conteúdo.
        if exemplo:
            campos_id = [
                parte.split('=')[0]
                for parte in exemplo.split('&')
                if '=' in parte
            ]
            amostra = (
                f'{len(exemplo)} caracteres, campos {campos_id}'
                if campos_id
                else f'{len(exemplo)} caracteres'
            )
        else:
            amostra = 'nenhum'

        linhas.append(
            f'  nível {nivel}: {info["nos"]} nó(s), '
            f'{info["com_parametros"]} com parametros, '
            f'{info["com_filhos"]} com filhos'
        )
        linhas.append(f'      campos em data: {sorted(info["campos"])}')
        linhas.append(f'      parametros: {amostra}')

    return linhas


def conta_niveis(arvore, profundidade: int = 0) -> dict:
    """
    Resume o formato da árvore, para diagnóstico.

    :param arvore: árvore de documentos
    :param profundidade: nível inicial
    :return: quantidade de nós por nível
    """
    contagem: Dict[int, int] = {}

    def percorre(no, nivel) -> None:
        if isinstance(no, list):
            for item in no:
                percorre(item, nivel)
            return
        if not isinstance(no, dict):
            return

        contagem[nivel] = contagem.get(nivel, 0) + 1
        percorre(no.get('children') or [], nivel + 1)

    percorre(arvore, profundidade)
    return contagem


def extrai_cd_documento(arvore) -> Optional[str]:
    """
    Descobre o "cdDocumento" que o e-SAJ espera na preparação.

    :param arvore: árvore de documentos
    :return: o código, ou `None` se não houver
    """
    if isinstance(arvore, list):
        arvore = arvore[-1] if arvore else {}

    if isinstance(arvore, dict):
        dados = arvore.get('data') or {}
        if isinstance(dados, dict) and dados.get('cdDocumento'):
            return str(dados['cdDocumento'])

        # Se a raiz não trouxer o código, usa o da última peça.
        filhos = arvore.get('children') or []
        if filhos:
            return extrai_cd_documento(filhos)

    return None


def solicita_preparacao(
    sessao: requests.Session,
    url_pasta: str,
    cd_processo: str,
    parametros: List[str],
    cd_documento: Optional[str] = None,
    separar_documentos: bool = True,
    timeout: int = 1800,
) -> str:
    """
    Pede ao e-SAJ que monte o arquivo com todas as peças.

    :param sessao: sessão HTTP autenticada
    :param url_pasta: URL da pasta digital, usada como *Referer*
    :param cd_processo: código interno do processo
    :param parametros: identificadores das peças
    :param cd_documento: código do documento de referência
    :param separar_documentos: `True` gera ZIP com uma peça por PDF;
        `False` gera um PDF único com os autos inteiros
    :param timeout: tempo limite da requisição, em segundos
    :return: localizador do arquivo em preparação
    :raises SemAcessoAosAutosError: quando não há peças a baixar
    """
    if not parametros:
        raise SemAcessoAosAutosError(
            'A pasta digital deste processo não trouxe nenhuma peça '
            'para download.'
        )

    corpo = [('itensPdfSelecionados', item) for item in parametros]
    corpo.append(('cdProcesso', cd_processo))
    corpo.append(('cdDocumento', cd_documento or ''))
    corpo.append(
        ('separarDocumentos', 'true' if separar_documentos else 'false')
    )
    corpo.append(('acessoPeloPetsg', ''))

    cabecalhos = {
        'X-Requested-With': 'XMLHttpRequest',
        'Referer': url_pasta,
    }

    try:
        resposta = sessao.post(
            URL_PREPARA, data=corpo, headers=cabecalhos, timeout=timeout
        )
        _verifica_resposta(resposta)

    except requests.Timeout as e:
        raise PreparacaoTimeoutError(
            f'O e-SAJ não respondeu ao pedido de preparação em '
            f'{timeout / 60:.0f} minutos, com {len(parametros)} peça(s) '
            f'solicitadas de uma vez. Em processos desse porte, '
            f'dividir costuma resolver: use "--pecas-por-pedido 2000".'
        ) from e

    except requests.RequestException as e:
        raise ESAJIndisponivelError(
            f'Não foi possível solicitar a preparação dos autos: {e}'
        ) from e

    localizador = resposta.text.strip()
    if not localizador:
        raise SemAcessoAosAutosError(
            'O e-SAJ não devolveu o localizador do arquivo. O pedido '
            'de preparação dos autos foi recusado.'
        )

    logger.info(
        'Preparação solicitada: %s peça(s), localizador=%s',
        len(parametros),
        localizador,
    )
    return localizador


def aguarda_finalizacao(
    sessao: requests.Session,
    url_pasta: str,
    localizador: str,
    cd_processo: str,
    cd_documento: Optional[str] = None,
    espera_maxima: int = 900,
    intervalo: int = 5,
    timeout: int = 120,
    intervalo_relato: int = 60,
) -> str:
    """
    Consulta o e-SAJ até o arquivo ficar pronto.

    A montagem é assíncrona e sem previsão de término: enquanto não
    conclui, o e-SAJ responde vazio. Processos volumosos levam vários
    minutos.

    :param sessao: sessão HTTP autenticada
    :param url_pasta: URL da pasta digital, usada como *Referer*
    :param localizador: localizador devolvido na preparação
    :param cd_processo: código interno do processo
    :param cd_documento: código do documento de referência
    :param espera_maxima: tempo total de espera, em segundos
    :param intervalo: intervalo entre consultas, em segundos
    :param timeout: tempo limite de cada consulta, em segundos
    :return: URL do arquivo pronto
    :raises PreparacaoTimeoutError: quando estoura o tempo de espera
    """
    corpo = {
        'localizador': localizador,
        'cdProcesso': cd_processo,
        'cdDocumento': cd_documento or '',
    }
    cabecalhos = {
        'X-Requested-With': 'XMLHttpRequest',
        'Referer': url_pasta,
    }

    comeco = time.monotonic()
    limite = comeco + espera_maxima
    proximo_relato = comeco + intervalo_relato
    tentativa = 0

    logger.info(
        'Pedido aceito. O e-SAJ está montando o arquivo; isso leva '
        'de segundos a muitos minutos, conforme o tamanho do processo.'
    )

    while time.monotonic() < limite:
        tentativa += 1

        try:
            resposta = sessao.post(
                URL_BUSCA_PRONTO,
                data=corpo,
                headers=cabecalhos,
                timeout=timeout,
            )
            _verifica_resposta(resposta)

        except QUEDAS_DE_CONEXAO as e:
            # A espera dura muitos minutos, e a consulta é inofensiva:
            # uma queda passageira não deve derrubar tudo. Segue-se
            # consultando até o prazo total acabar.
            logger.info(
                'Conexão caiu ao consultar o andamento (%s). '
                'Continuando a espera...',
                e,
            )
            time.sleep(intervalo)
            continue

        except requests.RequestException as e:
            raise ESAJIndisponivelError(
                f'Não foi possível consultar o andamento da '
                f'preparação: {e}'
            ) from e

        conteudo = resposta.text.strip()

        achado = re.search(r'https?://[^\s<>"\']+', conteudo)
        if achado:
            logger.info(
                'Arquivo pronto após %s consulta(s).', tentativa
            )
            return achado.group(0)

        if conteudo:
            _verifica_mensagem_de_recusa(conteudo)

        # Uma linha a cada consulta encheria a tela de ruído numa
        # espera de muitos minutos. O que interessa é há quanto tempo
        # se espera.
        agora = time.monotonic()
        if agora >= proximo_relato:
            decorrido = agora - comeco
            logger.info(
                'Aguardando o e-SAJ montar o arquivo há %.0f min '
                '(%s consultas)...',
                decorrido / 60,
                tentativa,
            )
            proximo_relato = agora + intervalo_relato

        time.sleep(intervalo)

    raise PreparacaoTimeoutError(
        f'O e-SAJ não concluiu a preparação dos autos em '
        f'{espera_maxima / 60:.0f} minutos. Em processos volumosos '
        f'isso é esperado, e o pedido continua correndo lá: use '
        f'"--retomar" mais tarde para recolhê-lo sem refazê-lo.'
    )


def _relata_progresso(
    baixado: int, total: int, decorrido: float
) -> None:
    """
    Registra o andamento da transferência.

    :param baixado: bytes já gravados
    :param total: tamanho anunciado, ou 0 se desconhecido
    :param decorrido: segundos desde o início
    """
    mb = baixado / 1048576
    taxa = mb / decorrido if decorrido else 0

    if total:
        falta = (total - baixado) / 1048576 / taxa if taxa else 0
        logger.info(
            '  baixados %.0f MB de %.0f MB (%.0f%%, %.1f MB/s, '
            'faltam ~%.0f min)',
            mb,
            total / 1048576,
            baixado * 100 / total,
            taxa,
            falta / 60,
        )
    else:
        logger.info('  baixados %.0f MB (%.1f MB/s)', mb, taxa)


def _tamanho_anunciado(resposta: requests.Response) -> int:
    """
    Tamanho total do arquivo, conforme a resposta.

    Numa entrega parcial, o total está em `Content-Range`, e não em
    `Content-Length` — que aí mede só o pedaço enviado.

    :param resposta: resposta HTTP
    :return: tamanho total em bytes, ou 0 se não informado
    """
    faixa = resposta.headers.get('Content-Range')
    if faixa:
        achado = re.search(r'/(\d+)\s*$', faixa)
        if achado:
            return int(achado.group(1))

    tamanho = resposta.headers.get('Content-Length')
    return int(tamanho) if tamanho else 0


def baixa_arquivo(
    sessao: requests.Session,
    url_arquivo: str,
    destino: Path,
    timeout: int = 300,
    tamanho_bloco: int = 1024 * 256,
    intervalo_relato: int = 30,
    ao_progredir=None,
    tentativas: int = 5,
    renova_url=None,
) -> Path:
    """
    Transfere o arquivo preparado, preservando-o como entregue.

    A gravação é feita em arquivo temporário e só é renomeada ao
    final, de modo que uma interrupção não deixe um arquivo truncado
    ocupando o nome definitivo.

    :param sessao: sessão HTTP autenticada
    :param url_arquivo: URL do arquivo pronto
    :param destino: caminho final do arquivo
    :param timeout: tempo limite da requisição, em segundos
    :param tamanho_bloco: tamanho de cada bloco lido, em bytes
    :param intervalo_relato: segundos entre as notícias de andamento
    :param ao_progredir: função chamada com (bytes recebidos, total
        anunciado). O total é `0` quando o e-SAJ não o informa.
    :param tentativas: quantas vezes retomar depois de a conexão cair
    :param renova_url: função que devolve um endereço novo para o
        mesmo arquivo. O endereço do e-SAJ é de uso único: sem isso,
        cada nova tentativa recebe "O documento não foi encontrado".
    :return: caminho do arquivo gravado
    :raises DownloadError: quando a transferência falha ou vem vazia

    Arquivos de gigabytes levam dezenas de minutos, e a conexão cai
    com frequência nesse intervalo. Perder tudo e recomeçar seria
    inviável — houve queda com 92% já recebidos. Por isso o que já
    veio é preservado no arquivo parcial e a transferência é retomada
    de onde parou, pedindo ao e-SAJ apenas o trecho que falta.

    Se o servidor ignorar o pedido de faixa e mandar o arquivo
    inteiro, o parcial é descartado e a gravação recomeça — o que
    custa tempo, mas nunca produz arquivo emendado errado.
    """
    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)
    parcial = destino.with_suffix(destino.suffix + '.parcial')

    total = 0
    ultima_queda = None
    completo = False

    for tentativa in range(1, tentativas + 1):
        ja_tenho = parcial.stat().st_size if parcial.is_file() else 0

        # O endereço do arquivo é de uso único: pedi-lo de novo devolve
        # "O documento não foi encontrado". A cada nova tentativa é
        # preciso obter outro, apontando para o mesmo arquivo montado.
        if tentativa > 1 and renova_url is not None:
            try:
                url_arquivo = renova_url()
            except ESAJError as e:
                # O e-SAJ descarta o arquivo montado junto com o
                # endereço: consumido o link, o localizador não
                # devolve outro. Sem endereço novo não há como
                # continuar.
                ultima_queda = e
                break

        cabecalhos = {}
        if ja_tenho:
            cabecalhos['Range'] = f'bytes={ja_tenho}-'
            logger.info(
                'Retomando a transferência a partir de %.0f MB '
                '(tentativa %s de %s)...',
                ja_tenho / 1048576,
                tentativa,
                tentativas,
            )

        try:
            with sessao.get(
                url_arquivo,
                stream=True,
                timeout=timeout,
                headers=cabecalhos,
            ) as resposta:
                _verifica_resposta(resposta)

                if resposta.status_code not in (200, 206):
                    raise DownloadError(
                        f'O e-SAJ respondeu com HTTP '
                        f'{resposta.status_code} ao entregar o arquivo.'
                    )

                # Página de erro devolvida com status 200. Gravá-la
                # por cima do que já veio destruiria a transferência:
                # foi assim que um arquivo de 215 MB virou uma página
                # de 1.707 bytes dizendo "documento não encontrado".
                tipo = resposta.headers.get('Content-Type', '')
                if 'html' in tipo.lower():
                    ultima_queda = (
                        'o e-SAJ devolveu uma página em vez do arquivo'
                    )
                    if tentativa < tentativas:
                        time.sleep(ESPERA_ENTRE_TENTATIVAS)
                    continue

                # 206 continua de onde parou; 200 recomeça do zero,
                # porque o servidor mandou o arquivo inteiro.
                retomando = resposta.status_code == 206
                anunciado = _tamanho_anunciado(resposta)

                # Resposta inteira menor do que o pedaço já obtido não
                # pode ser o arquivo: preserva o que se tem.
                if not retomando and ja_tenho and anunciado:
                    if anunciado < ja_tenho:
                        ultima_queda = (
                            f'o e-SAJ ofereceu {anunciado:,} bytes, '
                            f'menos que os {ja_tenho:,} já recebidos'
                        )
                        if tentativa < tentativas:
                            time.sleep(ESPERA_ENTRE_TENTATIVAS)
                        continue

                if not retomando:
                    ja_tenho = 0

                total = anunciado or total
                if total and tentativa == 1:
                    logger.info('Transferindo %.0f MB...', total / 1048576)

                baixado = ja_tenho
                inicio = time.monotonic()
                proximo_relato = inicio + intervalo_relato

                with open(parcial, 'ab' if retomando else 'wb') as arquivo:
                    for bloco in resposta.iter_content(
                        chunk_size=tamanho_bloco
                    ):
                        if not bloco:
                            continue

                        arquivo.write(bloco)
                        baixado += len(bloco)

                        # Sem notícia, não há como distinguir
                        # progresso de travamento.
                        agora = time.monotonic()
                        if agora >= proximo_relato:
                            _relata_progresso(
                                baixado, total, agora - inicio
                            )
                            proximo_relato = agora + intervalo_relato

                            # Quem tem tela desenha a barra; quem não
                            # tem, não passa nada.
                            if ao_progredir is not None:
                                ao_progredir(baixado, total)

        except QUEDAS_DE_CONEXAO as e:
            ultima_queda = e

        except OSError as e:
            # Falha de gravação, não de rede. Discos externos somem
            # por um instante e voltam; derrubar por isso um trabalho
            # de horas seria desproporcional. Vale outra tentativa.
            ultima_queda = f'falha ao gravar em disco: {e}'
            logger.warning(
                'Falha ao gravar em "%s": %s', parcial, e
            )

        except requests.RequestException as e:
            parcial.unlink(missing_ok=True)
            raise DownloadError(
                f'Falha ao transferir os autos: {e}'
            ) from e

        else:
            obtido = parcial.stat().st_size if parcial.is_file() else 0
            # Sem tamanho anunciado não há como conferir; com ele, o
            # fim silencioso antes da conta é uma queda disfarçada.
            if not total or obtido >= total:
                completo = True
                break

            ultima_queda = (
                f'a transferência terminou com {obtido:,} de '
                f'{total:,} bytes'
            )

        if tentativa < tentativas:
            time.sleep(ESPERA_ENTRE_TENTATIVAS)

    if not completo:
        obtido = parcial.stat().st_size if parcial.is_file() else 0

        # O pedaço recebido não serve para nada: o e-SAJ descarta o
        # arquivo montado junto com o endereço, e uma nova preparação
        # gera outro arquivo. Emendar um no outro produziria um ZIP
        # corrompido sem nenhum aviso.
        parcial.unlink(missing_ok=True)

        raise DownloadError(
            f'Não foi possível transferir os autos inteiros: '
            f'{obtido:,} de {total:,} bytes em {tentativas} '
            f'tentativa(s). O e-SAJ descarta o arquivo montado quando '
            f'a transferência é interrompida, de modo que o pedaço '
            f'recebido não pode ser aproveitado. Em arquivos grandes, '
            f'dividir o pedido em lotes menores evita o problema. '
            f'Último erro: {ultima_queda}'
        )

    if parcial.stat().st_size == 0:
        parcial.unlink(missing_ok=True)
        raise DownloadError(
            'O arquivo entregue pelo e-SAJ está vazio.'
        )

    parcial.replace(destino)
    logger.info(
        'Autos gravados em "%s" (%s bytes).',
        destino,
        destino.stat().st_size,
    )
    return destino

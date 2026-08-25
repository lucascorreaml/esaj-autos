"""
Módulo para obtenção da cópia integral dos autos.

Reúne, em uma única chamada, todo o caminho que vai do número CNJ ao
arquivo gravado em disco:

    número CNJ
        -> identificação do processo (cdProcesso)
        -> abertura da pasta digital
        -> leitura das peças dos autos
        -> preparação do arquivo pelo e-SAJ
        -> download e gravação local

O login é feito uma vez, e a mesma sessão baixa quantos processos
forem necessários::

    import esaj_autos as esaj

    sessao = esaj.login.entrar()
    esaj.autos.baixar(sessao, '1234567-89.2020.8.26.0100')

Também aceita um `driver` do Selenium já autenticado, para quem já
usa as páginas de login do pacote.
"""

import json
import logging
import time
from pathlib import Path
from typing import Iterable, List, Literal, Optional, Tuple, Union

import requests
from pydantic import ValidationError

from esaj_autos.exceptions import (
    AutenticacaoError,
    DownloadError,
    ESAJError,
    ESAJIndisponivelError,
    LimiteAcessoExcedidoError,
    PreparacaoTimeoutError,
    ProcessoComSenhaError,
    SemAcessoAosAutosError,
    SessaoExpiradaError,
)
from esaj_autos.request import pasta_digital, session
from esaj_autos.modelos import (
    DownloadAutos,
    FalhaDownload,
    ResultadoDownload,
    ResultadoLote,
)

logger = logging.getLogger(__name__)

# Espera ao recolher um pedido feito em outra sessão. Se o arquivo
# ainda estivesse montado, o e-SAJ o devolveria de imediato; consulta
# vazia significa que o localizador morreu. Um prazo curto troca uma
# hora perdida por dois minutos.
ESPERA_AO_RETOMAR = 120

# Quantas vezes refazer o pedido de uma parte cuja transferência caiu.
# Refazer é a única saída: o e-SAJ apaga o arquivo montado quando a
# conexão se interrompe.
TENTATIVAS_POR_PARTE = 3

# Esperas antes de reinsistir num processo cujo e-SAJ tropeçou. O
# portal responde 500, ou recusa o acesso à pasta, por instantes e
# volta ao normal. Sem esperar, um lote longo atravessaria a janela
# falhando processo após processo e terminaria com a lista queimada —
# num lote noturno, ninguém está por perto para perceber.
ESPERAS_ENTRE_INSISTENCIAS = (60, 300, 900)

# Recusa de acesso à pasta merece só uma segunda chance, e curta: o
# texto é o mesmo para o tropeço passageiro e para a falta real de
# permissão. Insistir muito gastaria vinte minutos em cada processo a
# que o usuário de fato não tem acesso.
ESPERAS_APOS_RECUSA = (60,)


def explica_falha(erro: Exception) -> Tuple[str, str]:
    """
    Traduz uma exceção no par (nome, explicação) mostrado ao usuário.

    A validação dos parâmetros é feita por Pydantic, cujo relato traz
    o nome do modelo, o do campo, o valor recebido e um endereço da
    documentação da biblioteca. Nada disso diz respeito a quem só
    digitou um número errado: o que importa é a frase escrita para
    ele, que fica no meio desse relato.

    :param erro: exceção capturada
    :return: nome do problema e explicação em linguagem corrente
    """
    if isinstance(erro, ValidationError):
        motivos = [
            str(d.get('msg', '')).replace('Value error, ', '').strip()
            for d in erro.errors()
        ]
        motivos = [m for m in motivos if m]
        return 'NumeroInvalido', ' '.join(motivos) or str(erro)

    return type(erro).__name__, str(erro)


def _como_sessao(sessao_ou_driver) -> requests.Session:
    """
    Aceita tanto uma sessão HTTP quanto um driver do Selenium.

    :param sessao_ou_driver: sessão autenticada ou driver autenticado
    :return: sessão HTTP pronta para uso
    """
    if isinstance(sessao_ou_driver, requests.Session):
        return sessao_ou_driver

    # Driver do Selenium: transfere os cookies da sessão do navegador.
    return session.cria_sessao(sessao_ou_driver)


def baixar(
    sessao,
    numero_cnj: str,
    destino: Union[str, Path] = 'autos',
    instancia: Literal['Primeiro Grau', 'Segundo Grau'] = 'Primeiro Grau',
    separar_documentos: bool = True,
    espera_maxima: int = 900,
    intervalo: int = 5,
    sobrescrever: bool = False,
    pecas_por_pedido: int = 0,
    ao_progredir=None,
) -> ResultadoDownload:
    """
    Baixa a cópia integral dos autos de um processo.

    O arquivo é gravado em uma pasta própria do processo, nomeada
    pelo número CNJ, e preservado exatamente como o e-SAJ o entrega.

    :param sessao: sessão autenticada (`login.entrar()`) ou driver do
        Selenium já autenticado
    :param numero_cnj: número do processo, com ou sem pontuação
    :param destino: pasta onde a subpasta do processo será criada
    :param instancia: grau de jurisdição do processo
    :param separar_documentos: `True` baixa um ZIP com uma peça por
        PDF; `False` baixa um PDF único com o processo inteiro
    :param espera_maxima: tempo total de espera pela preparação, em
        segundos. Processos volumosos exigem mais tempo.
    :param intervalo: intervalo entre as consultas de andamento
    :param sobrescrever: `True` refaz o download mesmo que o arquivo
        já exista
    :param pecas_por_pedido: divide o pedido em lotes desse tamanho,
        gerando um arquivo por lote. `0` pede tudo de uma vez.
    :param ao_progredir: função chamada com (bytes recebidos, total),
        para desenhar barra de progresso em interface gráfica
    :return: dados do download realizado

    :raises AutenticacaoError: sessão não autenticada
    :raises ProcessoNaoEncontradoError: processo inexistente
    :raises SemAcessoAosAutosError: sem permissão de acesso aos autos
    :raises SessaoExpiradaError: sessão expirada durante a operação
    :raises ESAJIndisponivelError: e-SAJ fora do ar
    :raises PreparacaoTimeoutError: preparação não concluída a tempo
    :raises DownloadError: falha na transferência do arquivo
    """
    dados = DownloadAutos(
        numero_cnj=numero_cnj,
        instancia=instancia,
        destino=Path(destino),
        separar_documentos=separar_documentos,
        espera_maxima=espera_maxima,
        intervalo=intervalo,
        sobrescrever=sobrescrever,
        pecas_por_pedido=pecas_por_pedido,
    )
    return baixar_com_parametros(
        sessao=sessao, dados=dados, ao_progredir=ao_progredir
    )


def _insiste(**kwargs) -> ResultadoDownload:
    """
    Baixa um processo, insistindo quando o e-SAJ tropeça.

    Só reinsiste diante de indisponibilidade e de recusa de acesso à
    pasta: ambas costumam ser passageiras. Processo inexistente,
    número inválido ou falta de permissão real não melhoram com
    espera, e passam adiante na hora.

    :param kwargs: repassados a `baixar`
    :return: dados do download realizado
    """
    numero = kwargs.get('numero_cnj')
    indisponivel = 0
    recusas = 0

    while True:
        try:
            return baixar(**kwargs)

        except (LimiteAcessoExcedidoError, ProcessoComSenhaError):
            # Não passam com o tempo.
            raise

        except (ESAJIndisponivelError, SemAcessoAosAutosError) as erro:
            if isinstance(erro, ESAJIndisponivelError):
                esperas, quantas = ESPERAS_ENTRE_INSISTENCIAS, indisponivel
                indisponivel += 1
            else:
                esperas, quantas = ESPERAS_APOS_RECUSA, recusas
                recusas += 1

            if quantas >= len(esperas):
                raise

            espera = esperas[quantas]
            nome = type(erro).__name__
            motivo = str(erro)

        # O tipo do erro sozinho não distingue uma instabilidade
        # passageira de uma peça que o e-SAJ não consegue montar; sem
        # a mensagem, insistir é chutar.
        logger.warning(
            'O e-SAJ tropeçou em %s (%s): %s Nova tentativa em %s min...',
            numero,
            nome,
            motivo,
            max(1, espera // 60),
        )
        time.sleep(espera)


def baixar_lote(
    sessao,
    numeros_cnj: Iterable[str],
    destino: Union[str, Path] = 'autos',
    instancia: Literal['Primeiro Grau', 'Segundo Grau'] = 'Primeiro Grau',
    separar_documentos: bool = True,
    espera_maxima: int = 900,
    intervalo: int = 5,
    sobrescrever: bool = False,
    pecas_por_pedido: int = 0,
    parar_no_primeiro_erro: bool = False,
    ao_progredir=None,
    intervalo_entre_processos: int = 0,
) -> ResultadoLote:
    """
    Baixa a cópia integral dos autos de vários processos.

    Reaproveita a mesma sessão para todos e segue adiante quando um
    processo falha, de modo que um número inválido ou sem permissão
    não derrube o lote inteiro. O que falhou fica registrado no
    resultado.

    :param sessao: sessão autenticada ou driver do Selenium
    :param numeros_cnj: números dos processos
    :param destino: pasta onde as subpastas serão criadas
    :param instancia: grau de jurisdição dos processos
    :param separar_documentos: formato do arquivo
    :param espera_maxima: espera pela preparação de cada processo
    :param intervalo: intervalo entre as consultas de andamento
    :param sobrescrever: refaz downloads já existentes
    :param pecas_por_pedido: divide cada pedido em lotes
    :param parar_no_primeiro_erro: `True` interrompe o lote na
        primeira falha, em vez de seguir para o próximo processo
    :param intervalo_entre_processos: pausa, em segundos, antes de
        abrir a pasta digital do processo seguinte. O TJSP limita
        quantas vezes a pasta pode ser aberta, e um lote longo sem
        pausa esgota a cota antes de terminar.
    :return: sucessos e falhas do lote
    """
    sessao = _como_sessao(sessao)
    session.exige_login(sessao)

    numeros = list(numeros_cnj)
    resultado = ResultadoLote(informados=len(numeros))
    consultou_o_esaj = False

    for posicao, numero in enumerate(numeros, start=1):
        # A pausa vale só entre processos que de fato consultam o
        # e-SAJ: esperar antes de pular um já baixado seria tempo
        # perdido à toa.
        if intervalo_entre_processos and consultou_o_esaj:
            logger.info(
                'Pausa de %s s antes do próximo processo, para não '
                'esgotar a cota de acessos à pasta digital.',
                intervalo_entre_processos,
            )
            time.sleep(intervalo_entre_processos)

        logger.info(
            '[%s/%s] Processo %s', posicao, len(numeros), numero
        )

        try:
            baixado = _insiste(
                    sessao=sessao,
                    numero_cnj=numero,
                    destino=destino,
                    instancia=instancia,
                    separar_documentos=separar_documentos,
                    espera_maxima=espera_maxima,
                    intervalo=intervalo,
                    sobrescrever=sobrescrever,
                    pecas_por_pedido=pecas_por_pedido,
                    ao_progredir=ao_progredir,
            )
            resultado.sucessos.append(baixado)
            consultou_o_esaj = not baixado.reaproveitado

            # O registro por parte não diz quando o processo inteiro
            # ficou pronto, que é o que interessa a quem acompanha.
            if baixado.reaproveitado:
                logger.info(
                    'PROCESSO JA ESTAVA PRONTO: %s (%s arquivo(s), '
                    '%.0f MB)',
                    baixado.numero_cnj,
                    len(baixado.partes) or 1,
                    baixado.tamanho_bytes / 1048576,
                )
            else:
                logger.info(
                    'PROCESSO CONCLUIDO: %s (%s arquivo(s), %.0f MB, '
                    '%s peça(s)) — %s de %s',
                    baixado.numero_cnj,
                    len(baixado.partes) or 1,
                    baixado.tamanho_bytes / 1048576,
                    baixado.total_pecas,
                    len(resultado.sucessos),
                    len(numeros),
                )

        except OSError as e:
            # Disco cheio, disco externo que sumiu, permissão negada.
            # É do ambiente, não do processo: registra e segue, em vez
            # de derrubar um lote de horas.
            logger.warning(
                '[%s/%s] Processo %s não baixado por falha de disco: %s',
                posicao,
                len(numeros),
                numero,
                e,
            )
            resultado.falhas.append(
                FalhaDownload(
                    numero_cnj=str(numero),
                    erro='FalhaDeDisco',
                    mensagem=str(e),
                )
            )
            if parar_no_primeiro_erro:
                break

        except (ESAJError, ValueError) as e:
            nome, explicacao = explica_falha(e)
            logger.warning(
                '[%s/%s] Processo %s não baixado: %s',
                posicao,
                len(numeros),
                numero,
                explicacao,
            )
            resultado.falhas.append(
                FalhaDownload(
                    numero_cnj=str(numero),
                    erro=nome,
                    mensagem=explicacao,
                )
            )
            # Sem sessão válida, os processos seguintes falhariam
            # todos do mesmo jeito, cada um custando requisições
            # inúteis ao e-SAJ.
            if isinstance(e, (SessaoExpiradaError, AutenticacaoError)):
                logger.warning(
                    'Lote interrompido: sem sessão válida. Refaça o '
                    'login e use "--retomar" para recolher o que já '
                    'foi pedido.'
                )
                break

            if parar_no_primeiro_erro:
                break

    return resultado


def baixar_com_parametros(
    sessao, dados: DownloadAutos, ao_progredir=None
) -> ResultadoDownload:
    """
    Versão de `baixar` que recebe os parâmetros já validados.

    :param sessao: sessão autenticada ou driver do Selenium
    :param dados: parâmetros do download
    :return: dados do download realizado
    """
    # Reaproveita o que já foi baixado, salvo pedido em contrário.
    # Vale também para o processo baixado em partes: sem isso, abrir a
    # pasta digital só para descobrir que nada falta consumiria um dos
    # acessos diários, que são limitados.
    if not dados.sobrescrever:
        prontos = _ja_baixados(dados)
        if prontos:
            logger.info(
                'Já baixado, download dispensado: %s arquivo(s) em %s',
                len(prontos),
                dados.pasta_processo,
            )
            return ResultadoDownload(
                numero_cnj=dados.numero_cnj,
                instancia=dados.instancia,
                arquivo=prontos[0],
                tamanho_bytes=sum(p.stat().st_size for p in prontos),
                total_pecas=0,
                formato=dados.extensao.lstrip('.'),
                reaproveitado=True,
                partes=prontos,
            )

    sessao = _como_sessao(sessao)
    session.exige_login(sessao)

    # Número CNJ -> código interno do processo
    cd_processo = pasta_digital.resolve_cd_processo(
        sessao=sessao,
        numero_cnj=dados.numero_cnj,
        grau=dados.instancia,
    )

    # Abertura da pasta digital e leitura das peças
    url_pasta = pasta_digital.abre_pasta_digital(
        sessao=sessao,
        cd_processo=cd_processo,
        grau=dados.instancia,
    )
    arvore = pasta_digital.le_arvore_documentos(
        sessao=sessao, url_pasta=url_pasta
    )
    pecas = pasta_digital.coleta_parametros(arvore)
    cd_documento = pasta_digital.extrai_cd_documento(arvore)

    niveis = pasta_digital.conta_niveis(arvore)
    logger.info(
        'Processo %s: %s peça(s) na pasta digital. '
        '(árvore por nível: %s)',
        dados.numero_cnj,
        len(pecas),
        ', '.join(f'{n}:{q}' for n, q in sorted(niveis.items())),
    )

    lotes = dados.divide_em_lotes(pecas)
    if len(lotes) > 1:
        logger.info(
            'Pedido dividido em %s lotes de até %s peça(s).',
            len(lotes),
            dados.pecas_por_pedido,
        )

    pedido = {
        'numero_cnj': dados.numero_cnj,
        'cd_processo': cd_processo,
        'cd_documento': cd_documento,
        'url_pasta': url_pasta,
        'total_pecas': len(pecas),
        'separar_documentos': dados.separar_documentos,
        'partes': [],
    }

    # Uma parte por vez, do pedido ao arquivo em disco. Preparar todas
    # de antemão não adianta: o e-SAJ descarta o arquivo montado
    # quando a transferência é interrompida, e o localizador morre
    # junto. O que serve é refazer o pedido da parte que caiu — e só
    # dela, sem custar as demais.
    gravados = []

    for indice, lote in enumerate(lotes, start=1):
        alvo = dados.arquivo_da_parte(indice, len(lotes))

        if alvo.is_file() and not dados.sobrescrever:
            logger.info('Parte %s já baixada: %s', indice, alvo)
            gravados.append(alvo)
            continue

        if len(lotes) > 1:
            logger.info(
                'Parte %s de %s: %s peça(s).', indice, len(lotes), len(lote)
            )

        gravados.append(
            _prepara_e_baixa(
                sessao=sessao,
                dados=dados,
                pedido=pedido,
                lote=lote,
                alvo=alvo,
                ao_progredir=ao_progredir,
            )
        )

    _arquivo_preparacao(dados).unlink(missing_ok=True)
    total = sum(g.stat().st_size for g in gravados)

    return ResultadoDownload(
        numero_cnj=dados.numero_cnj,
        cd_processo=cd_processo,
        instancia=dados.instancia,
        arquivo=gravados[0],
        tamanho_bytes=total,
        total_pecas=len(pecas),
        formato=dados.extensao.lstrip('.'),
        reaproveitado=False,
        partes=gravados,
    )


def _tenta_parte(
    sessao,
    dados: DownloadAutos,
    pedido: dict,
    lote: list,
    alvo: Path,
    ao_progredir=None,
) -> Path:
    """
    Uma passada do par pedido-transferência de uma parte.

    :param sessao: sessão autenticada
    :param dados: parâmetros do download
    :param pedido: registro do pedido, atualizado a cada tentativa
    :param lote: peças desta parte
    :param alvo: caminho do arquivo desta parte
    :param ao_progredir: função de progresso
    :return: caminho do arquivo gravado
    """
    localizador = pasta_digital.solicita_preparacao(
        sessao=sessao,
        url_pasta=pedido['url_pasta'],
        cd_processo=pedido['cd_processo'],
        parametros=lote,
        cd_documento=pedido.get('cd_documento'),
        separar_documentos=dados.separar_documentos,
    )

    parte = {
        'localizador': localizador,
        'arquivo': str(alvo),
        'pecas': len(lote),
    }
    pedido['partes'] = [
        p for p in pedido['partes'] if p['arquivo'] != str(alvo)
    ] + [parte]
    _grava_preparacao(dados, pedido)

    url_arquivo = _aguarda(sessao, dados, pedido, parte)
    gravado = pasta_digital.baixa_arquivo(
        sessao=sessao,
        url_arquivo=url_arquivo,
        destino=alvo,
        ao_progredir=ao_progredir,
        tentativas=1,
    )

    parte.pop('localizador', None)
    _grava_preparacao(dados, pedido)
    return gravado


def _renova_acesso_a_pasta(sessao, dados: DownloadAutos, pedido: dict) -> None:
    """
    Reabre a pasta digital do processo em andamento.

    O `pastadigital` é um aplicativo à parte, de sessão própria e mais
    curta que a do portal. Reabrir custa um dos acessos diários, mas
    o preço de não reabrir é o lote inteiro parar.

    :param sessao: sessão autenticada
    :param dados: parâmetros do download
    :param pedido: registro do pedido, com o endereço atualizado
    """
    logger.info(
        'O acesso à pasta digital venceu no meio do processo %s. '
        'Reabrindo...',
        dados.numero_cnj,
    )

    url_pasta = pasta_digital.abre_pasta_digital(
        sessao=sessao,
        cd_processo=pedido['cd_processo'],
        grau=dados.instancia,
    )
    pasta_digital.entra_na_pasta(sessao=sessao, url_pasta=url_pasta)

    pedido['url_pasta'] = url_pasta
    _grava_preparacao(dados, pedido)
    logger.info('Acesso à pasta digital renovado.')


def _prepara_e_baixa(
    sessao,
    dados: DownloadAutos,
    pedido: dict,
    lote: list,
    alvo: Path,
    ao_progredir=None,
    tentativas: int = TENTATIVAS_POR_PARTE,
) -> Path:
    """
    Pede a montagem de uma parte e a transfere, refazendo se cair.

    A unidade de repetição é o par pedido-transferência, e não só a
    transferência: interrompida, o e-SAJ apaga o arquivo montado, de
    modo que continuar de onde parou é impossível e só refazer resolve.

    :param sessao: sessão autenticada
    :param dados: parâmetros do download
    :param pedido: registro do pedido, atualizado a cada tentativa
    :param lote: peças desta parte
    :param alvo: caminho do arquivo desta parte
    :param ao_progredir: função de progresso
    :param tentativas: quantas vezes refazer o pedido desta parte
    :return: caminho do arquivo gravado
    """
    ultima_falha = None

    for tentativa in range(1, tentativas + 1):
        if tentativa > 1:
            logger.info(
                'A transferência caiu e o e-SAJ descartou o arquivo. '
                'Refazendo o pedido desta parte (tentativa %s de %s)...',
                tentativa,
                tentativas,
            )

        try:
            try:
                return _tenta_parte(
                    sessao, dados, pedido, lote, alvo, ao_progredir
                )

            except SessaoExpiradaError:
                # O ticket da pasta digital vence antes do login do
                # portal: em processos de muitas partes o e-SAJ passa
                # a responder 401 com a autenticação ainda de pé.
                # Reabrir devolve um ticket novo. Se nem assim
                # funcionar, o login é que caiu, e o erro segue adiante
                # para interromper o lote.
                _renova_acesso_a_pasta(sessao, dados, pedido)
                return _tenta_parte(
                    sessao, dados, pedido, lote, alvo, ao_progredir
                )

        except DownloadError as e:
            ultima_falha = e
            continue

    raise DownloadError(
        f'A parte "{alvo.name}" não pôde ser transferida em '
        f'{tentativas} tentativas. Em processos com peças grandes, '
        f'reduzir "pecas_por_pedido" encurta cada arquivo e o mantém '
        f'dentro da janela em que a conexão se sustenta. '
        f'Última falha: {ultima_falha}'
    )


def _ja_baixados(dados: DownloadAutos) -> List[Path]:
    """
    Arquivos deste processo já presentes em disco.

    Reconhece tanto o arquivo único quanto as partes de um pedido que
    foi dividido em lotes. Um pedido ainda pendente não conta: o que
    falta recolher não está baixado.

    :param dados: parâmetros do download
    :return: arquivos encontrados, em ordem
    """
    if _arquivo_preparacao(dados).is_file():
        return []

    if dados.arquivo.is_file():
        return [dados.arquivo]

    padrao = f'{dados.numero_cnj}-parte-*{dados.extensao}'
    return sorted(dados.pasta_processo.glob(padrao))


def _arquivo_preparacao(dados: DownloadAutos) -> Path:
    """
    Onde fica registrado o pedido de preparação em andamento.

    :param dados: parâmetros do download
    :return: caminho do registro, dentro da pasta do processo
    """
    return dados.pasta_processo / 'preparacao.json'


def _grava_preparacao(dados: DownloadAutos, pedido: dict) -> None:
    """
    Registra o pedido em disco, para que a espera possa ser retomada.

    Em processos volumosos o e-SAJ leva muito tempo para montar o
    arquivo. Guardar o localizador evita ter de refazer o pedido — e
    consumir de novo o acesso à pasta digital — quando a espera é
    interrompida.

    :param dados: parâmetros do download
    :param pedido: dados do pedido em andamento
    """
    caminho = _arquivo_preparacao(dados)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(
        json.dumps(pedido, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    logger.info('Pedido registrado em %s', caminho)


def le_preparacao(dados: DownloadAutos) -> Optional[dict]:
    """
    Lê um pedido de preparação pendente, se houver.

    :param dados: parâmetros do download
    :return: dados do pedido, ou `None` se não houver
    """
    caminho = _arquivo_preparacao(dados)
    if not caminho.is_file():
        return None

    try:
        return json.loads(caminho.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        return None


def retomar(
    sessao,
    numero_cnj: str,
    destino: Union[str, Path] = 'autos',
    instancia: Literal['Primeiro Grau', 'Segundo Grau'] = 'Primeiro Grau',
    separar_documentos: bool = True,
    espera_maxima: int = 900,
    intervalo: int = 5,
    ao_progredir=None,
    refazer_se_preciso: bool = True,
) -> ResultadoDownload:
    """
    Retoma a espera de um pedido já feito ao e-SAJ.

    Serve para processos volumosos, cuja montagem leva mais tempo do
    que se quer esperar de uma vez: o pedido continua correndo no
    e-SAJ e pode ser recolhido depois, sem refazê-lo.

    :param sessao: sessão autenticada ou driver do Selenium
    :param numero_cnj: número do processo
    :param destino: pasta onde o processo foi registrado
    :param instancia: grau de jurisdição
    :param separar_documentos: formato pedido originalmente
    :param espera_maxima: tempo de espera desta tentativa
    :param intervalo: intervalo entre as consultas
    :param ao_progredir: função chamada com (bytes recebidos, total)
    :param refazer_se_preciso: `True` refaz a solicitação quando o
        pedido guardado não vale mais. Custa a espera da montagem,
        mas garante o arquivo em vez de deixar o processo perdido.
    :return: dados do download realizado
    :raises ESAJError: quando não há pedido pendente registrado
    """
    dados = DownloadAutos(
        numero_cnj=numero_cnj,
        instancia=instancia,
        destino=Path(destino),
        separar_documentos=separar_documentos,
        espera_maxima=espera_maxima,
        intervalo=intervalo,
    )

    pedido = le_preparacao(dados)
    if not pedido:
        raise ESAJError(
            f'Não há pedido de preparação registrado para '
            f'{dados.numero_cnj} em "{dados.pasta_processo}".'
        )

    # O formato foi decidido quando o pedido foi feito e está gravado.
    # Deixar o valor informado agora prevalecer só produziria um
    # resultado que descreve mal o arquivo já a caminho.
    gravado = pedido.get('separar_documentos')
    if gravado is not None and gravado != dados.separar_documentos:
        dados = dados.model_copy(update={'separar_documentos': gravado})

    sessao = _como_sessao(sessao)
    session.exige_login(sessao)

    try:
        return _aguarda_e_baixa(
            sessao=sessao,
            dados=dados,
            pedido=pedido,
            ao_progredir=ao_progredir,
        )

    except (SessaoExpiradaError, PreparacaoTimeoutError):
        if not refazer_se_preciso:
            raise

        # O login vale e a pasta digital foi reaberta nesta sessão. Se
        # mesmo assim o e-SAJ recusa, ou responde vazio sem parar, o
        # localizador não sobreviveu à troca de sessão — e o portal
        # não distingue "ainda montando" de "não conheço": responde
        # vazio nos dois casos. Refazer o pedido custa a espera da
        # montagem, mas é o que garante o arquivo; desistir deixaria o
        # processo inalcançável.
        logger.warning(
            'O pedido guardado não vale mais nesta sessão. Refazendo '
            'a solicitação de %s do zero.',
            dados.numero_cnj,
        )
        _arquivo_preparacao(dados).unlink(missing_ok=True)

        return baixar_com_parametros(
            sessao=sessao, dados=dados, ao_progredir=ao_progredir
        )


def _aguarda(
    sessao,
    dados: DownloadAutos,
    pedido: dict,
    parte: dict,
    espera: Optional[int] = None,
) -> str:
    """
    Espera o e-SAJ concluir a montagem de uma parte.

    :param sessao: sessão autenticada
    :param dados: parâmetros do download
    :param pedido: dados do pedido em andamento
    :param parte: parte a recolher
    :param espera: prazo desta espera. O padrão é o dos parâmetros.
    :return: URL do arquivo pronto
    """
    return pasta_digital.aguarda_finalizacao(
        sessao=sessao,
        url_pasta=pedido['url_pasta'],
        localizador=parte['localizador'],
        cd_processo=pedido['cd_processo'],
        cd_documento=pedido.get('cd_documento'),
        espera_maxima=espera or dados.espera_maxima,
        intervalo=dados.intervalo,
    )


def _aguarda_e_baixa(
    sessao, dados: DownloadAutos, pedido: dict, ao_progredir=None
):
    """
    Espera o e-SAJ concluir a montagem e grava o arquivo.

    :param sessao: sessão autenticada
    :param dados: parâmetros do download
    :param pedido: dados do pedido em andamento
    :return: dados do download realizado
    """
    gravados = []
    renovou = False

    for parte in pedido['partes']:
        alvo = Path(parte['arquivo'])

        # Parte já recolhida numa tentativa anterior.
        if not parte.get('localizador'):
            gravados.append(alvo)
            continue

        try:
            url_arquivo = _aguarda(sessao, dados, pedido, parte)

        except SessaoExpiradaError:
            # O login vale — foi conferido antes de chegar aqui. O que
            # não vale é o ticket da pasta digital, preso à sessão em
            # que o pedido foi feito. Renová-lo custa um acesso à
            # pasta, mas evita refazer uma preparação de muitos
            # minutos.
            if renovou or not pedido.get('cd_processo'):
                raise

            logger.info(
                'O acesso à pasta digital era de outra sessão; '
                'renovando antes de recolher o pedido...'
            )
            pedido['url_pasta'] = pasta_digital.abre_pasta_digital(
                sessao=sessao,
                cd_processo=pedido['cd_processo'],
                grau=dados.instancia,
            )
            # Obter o endereço não basta: o aplicativo da pasta
            # digital tem sessão própria e só a reconhece quando o
            # endereço assinado é visitado.
            pasta_digital.entra_na_pasta(
                sessao=sessao, url_pasta=pedido['url_pasta']
            )
            renovou = True
            _grava_preparacao(dados, pedido)

            # O pedido veio de outra sessão. Se ainda estivesse
            # montado, o e-SAJ o devolveria de imediato — o arquivo já
            # está pronto lá. Consulta vazia aqui não significa "ainda
            # montando", e sim que o localizador morreu: o portal não
            # distingue os dois casos, responde vazio para sempre.
            # Esperar a hora inteira seria só perder a hora.
            url_arquivo = _aguarda(
                sessao, dados, pedido, parte, espera=ESPERA_AO_RETOMAR
            )
        def renova_url(_parte=parte):
            """Pede outro endereço para o mesmo arquivo montado."""
            logger.info(
                'O endereço do arquivo é de uso único; pedindo outro...'
            )
            return _aguarda(sessao, dados, pedido, _parte, espera=300)

        gravados.append(
            pasta_digital.baixa_arquivo(
                sessao=sessao,
                url_arquivo=url_arquivo,
                destino=alvo,
                ao_progredir=ao_progredir,
                renova_url=renova_url,
            )
        )

        # Recolhida esta parte, o registro é atualizado: uma
        # interrupção adiante não custa o que já foi baixado.
        parte.pop('localizador', None)
        _grava_preparacao(dados, pedido)

    # Todos os pedidos foram cumpridos: o registro não serve mais.
    _arquivo_preparacao(dados).unlink(missing_ok=True)

    total = sum(g.stat().st_size for g in gravados)

    return ResultadoDownload(
        numero_cnj=dados.numero_cnj,
        cd_processo=pedido.get('cd_processo'),
        instancia=dados.instancia,
        arquivo=gravados[0],
        tamanho_bytes=total,
        total_pecas=pedido.get('total_pecas', 0),
        formato=dados.extensao.lstrip('.'),
        reaproveitado=False,
        partes=gravados,
    )


def pendentes(destino: Union[str, Path] = 'autos') -> List[str]:
    """
    Lista os processos com pedido em andamento no e-SAJ.

    Percorre a pasta de destino em busca de pedidos registrados e
    ainda não recolhidos. Serve para retomar tudo o que ficou pelo
    caminho sem precisar lembrar quais processos eram.

    :param destino: pasta onde os processos são gravados
    :return: números dos processos com pedido pendente
    """
    raiz = Path(destino)
    if not raiz.is_dir():
        return []

    numeros = []
    for registro in sorted(raiz.glob('*/preparacao.json')):
        try:
            pedido = json.loads(registro.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError):
            continue

        # Só interessa o que ainda tem parte por recolher.
        if any(p.get('localizador') for p in pedido.get('partes', [])):
            numero = pedido.get('numero_cnj') or registro.parent.name
            numeros.append(numero)

    return numeros


def le_numeros(caminho: Union[str, Path]) -> List[str]:
    """
    Lê números de processo de um arquivo de texto, um por linha.

    Linhas vazias e as iniciadas por "#" são ignoradas, de modo que a
    lista possa ser comentada.

    :param caminho: arquivo com os números
    :return: números encontrados
    """
    linhas = Path(caminho).read_text(encoding='utf-8').splitlines()
    return [
        linha.strip()
        for linha in linhas
        if linha.strip() and not linha.strip().startswith('#')
    ]

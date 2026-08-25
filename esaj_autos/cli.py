"""
Interface de linha de comando.

Baixa a cópia integral dos autos de um ou vários processos::

    esaj-autos 1234567-89.2020.8.26.0100
    esaj-autos 1234567-89.2020.8.26.0100 2345678-90.2021.8.26.0100
    esaj-autos --arquivo processos.txt

Sem números na linha de comando, eles são pedidos no terminal.
"""

import argparse
import logging
import os
import sys
from pathlib import Path

from esaj_autos import autos
from esaj_autos.exceptions import ESAJError
from esaj_autos.request import login
from esaj_autos.request.pasta_digital import normaliza_cnj


def _monta_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='esaj-autos',
        description=(
            'Baixa a cópia integral dos autos de processos do TJSP.'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            'Exemplos:\n'
            '  esaj-autos 1234567-89.2020.8.26.0100\n'
            '  esaj-autos 1234567-89.2020.8.26.0100 '
            '2345678-90.2021.8.26.0100\n'
            '  esaj-autos --arquivo processos.txt --destino autos\n'
            '  esaj-autos 2123456-78.2023.8.26.0000 --grau 2\n'
            '\n'
            'Se o comando não for encontrado, use "python -m esaj_autos"\n'
            'no lugar de "esaj-autos".\n'
        ),
    )
    parser.add_argument(
        'numeros',
        nargs='*',
        help='números CNJ dos processos, com ou sem pontuação',
    )
    parser.add_argument(
        '--arquivo',
        type=Path,
        help='arquivo de texto com um número por linha',
    )
    parser.add_argument(
        '--destino',
        type=Path,
        default=Path('autos'),
        help='pasta onde os processos serão gravados (padrão: autos)',
    )
    parser.add_argument(
        '--grau',
        choices=['1', '2'],
        default='1',
        help='grau de jurisdição (padrão: 1)',
    )
    formato = parser.add_mutually_exclusive_group()
    formato.add_argument(
        '--zip',
        action='store_true',
        help='ZIP com uma peça por PDF (a 1ª opção do e-SAJ)',
    )
    formato.add_argument(
        '--pdf',
        action='store_true',
        help='PDF único com os autos inteiros (a 2ª opção do e-SAJ)',
    )
    parser.add_argument(
        '--espera',
        type=int,
        default=1800,
        help=(
            'tempo máximo de espera pela preparação de cada processo, '
            'em segundos (padrão: 1800)'
        ),
    )
    parser.add_argument(
        '--pecas-por-pedido',
        type=int,
        default=0,
        metavar='N',
        help=(
            'divide o pedido em lotes de N peças, gerando um arquivo '
            'por lote. Use em processos com dezenas de milhares de '
            'peças, em que o pedido único não é concluído'
        ),
    )
    parser.add_argument(
        '--sobrescrever',
        action='store_true',
        help='refaz o download mesmo que o arquivo já exista',
    )
    parser.add_argument(
        '--cpf',
        default=os.getenv('USERNAME_TJSP'),
        help='CPF/CNPJ do e-SAJ (padrão: variável USERNAME_TJSP)',
    )
    parser.add_argument(
        '--novo-login',
        action='store_true',
        help=(
            'ignora a sessão guardada e faz login novo, gastando um '
            'código de verificação'
        ),
    )
    parser.add_argument(
        '--sair',
        action='store_true',
        help='apaga a sessão guardada e encerra',
    )
    parser.add_argument(
        '--silencioso',
        action='store_true',
        help='mostra apenas o resultado final',
    )
    parser.add_argument(
        '--retomar',
        action='store_true',
        help=(
            'recolhe pedidos já feitos ao e-SAJ, sem refazê-los. Sem '
            'números, retoma todos os pendentes em --destino'
        ),
    )
    parser.add_argument(
        '--conferir',
        action='store_true',
        help=(
            'abre a pasta digital e mostra quantas peças tem, sem '
            'pedir a preparação nem baixar nada'
        ),
    )
    return parser


def _separa_validos(numeros):
    """
    Separa os números em válidos e inválidos.

    :param numeros: números informados
    :return: tupla com (válidos e formatados, [(número, motivo)])
    """
    validos, invalidos = [], []

    for numero in numeros:
        try:
            _, formatado = normaliza_cnj(numero)
        except ValueError as e:
            invalidos.append((numero, str(e)))
        else:
            if formatado not in validos:
                validos.append(formatado)

    return validos, invalidos


def _pede_numeros() -> list:
    """
    Pede os números no terminal, um por linha.
    """
    print(
        'Informe os números dos processos, um por linha.\n'
        'Para terminar, deixe uma linha em branco.'
    )
    numeros = []
    while True:
        try:
            linha = input('  processo: ').strip()
        except EOFError:
            # Entrada encerrada (execução sem terminal, por exemplo).
            break
        if not linha:
            break
        numeros.append(linha)
    return numeros


def _reune_numeros(args) -> list:
    """
    Reúne e valida os números antes de qualquer login.

    A validação vem primeiro de propósito: descobrir que um número
    está errado só depois de gastar o código de verificação seria
    desperdiçar a autenticação.

    :param args: argumentos da linha de comando
    :return: números válidos e formatados
    """
    informados = list(args.numeros)

    if args.arquivo:
        informados.extend(autos.le_numeros(args.arquivo))

    # Retomar sem números: descobre sozinho o que ficou pendente, em
    # vez de exigir que se lembre quais processos eram.
    if not informados and args.retomar:
        informados = autos.pendentes(args.destino)
        if informados:
            print(
                f'\n{len(informados)} pedido(s) pendente(s) '
                f'encontrado(s) em "{args.destino}".'
            )
        else:
            print(
                f'\nNenhum pedido pendente em "{args.destino}". '
                f'Nada a retomar.'
            )
            return []

    if not informados:
        informados = _pede_numeros()

    validos, invalidos = _separa_validos(informados)

    # Enquanto houver o que corrigir, e houver alguém para corrigir.
    while invalidos and sys.stdin.isatty():
        print('\nEstes números não são processos válidos do TJSP:')
        for numero, motivo in invalidos:
            print(f'  - "{numero}": {motivo}')

        if validos:
            print(f'\n{len(validos)} número(s) válido(s) até aqui.')
            try:
                seguir = input(
                    'Informar os corrigidos agora? [S/n]: '
                ).strip().lower()
            except EOFError:
                break
            if seguir in ('n', 'nao', 'não'):
                break

        novos, invalidos_novos = _separa_validos(_pede_numeros())
        validos.extend(n for n in novos if n not in validos)

        # Nada informado: não adianta insistir.
        if not novos and not invalidos_novos:
            break
        invalidos = invalidos_novos

    if invalidos and not validos:
        for numero, motivo in invalidos:
            print(f'"{numero}": {motivo}')

    return validos


def _escolhe_formato(args) -> bool:
    """
    Decide o formato, perguntando quando não foi informado.

    O e-SAJ oferece as duas opções na hora do download; sem indicação
    na linha de comando, a pergunta é feita do mesmo jeito.

    :param args: argumentos da linha de comando
    :return: `True` para o ZIP com uma peça por PDF
    """
    if args.pdf:
        return False
    if args.zip:
        return True

    if not sys.stdin.isatty():
        # Sem quem responda: fica o padrão do e-SAJ.
        return True

    print('\nComo deseja o arquivo?')
    print('  1. ZIP, uma peça por PDF (padrão)')
    print('  2. PDF único, com os autos inteiros')

    try:
        escolha = input('Opção [1/2]: ').strip()
    except EOFError:
        return True

    return escolha != '2'


def _retoma(sessao, numeros, args, separar) -> int:
    """
    Recolhe pedidos de preparação já feitos ao e-SAJ.
    """
    grau = 'Segundo Grau' if args.grau == '2' else 'Primeiro Grau'
    baixados, falhas = 0, 0

    for numero in numeros:
        try:
            resultado = autos.retomar(
                sessao=sessao,
                numero_cnj=numero,
                destino=args.destino,
                instancia=grau,
                separar_documentos=separar,
                espera_maxima=args.espera,
            )
            print(
                f'  {resultado.arquivo}  '
                f'({resultado.tamanho_bytes:,} bytes)'
            )
            baixados += 1

        except (ESAJError, ValueError) as e:
            nome, explicacao = autos.explica_falha(e)
            print(f'  {numero}: {nome} — {explicacao}')
            falhas += 1

    print(f'\n{baixados} de {len(numeros)} pedido(s) recolhido(s).')
    return 0 if not falhas else 1


def _confere(sessao, numeros, args) -> int:
    """
    Mostra o que há na pasta digital, sem baixar.

    Serve para dimensionar um processo antes de pedir a preparação,
    que em processos volumosos leva muito tempo.
    """
    from esaj_autos.request import pasta_digital as pd

    grau = 'Segundo Grau' if args.grau == '2' else 'Primeiro Grau'
    print()

    for numero in numeros:
        try:
            cd = pd.resolve_cd_processo(
                sessao=sessao, numero_cnj=numero, grau=grau
            )
            url = pd.abre_pasta_digital(
                sessao=sessao, cd_processo=cd, grau=grau
            )
            arvore = pd.le_arvore_documentos(sessao=sessao, url_pasta=url)
            pecas = pd.coleta_parametros(arvore)
            niveis = pd.conta_niveis(arvore)

            print(f'{numero}')
            print(f'  cdProcesso : {cd}')
            print(f'  cdDocumento: {pd.extrai_cd_documento(arvore)}')
            print(f'  peças      : {len(pecas)}')
            print(
                '  níveis     : '
                + ', '.join(
                    f'{n}: {q}' for n, q in sorted(niveis.items())
                )
            )
            print('  estrutura da pasta digital:')
            for linha in pd.descreve_arvore(arvore):
                print(f'    {linha}')

        except (ESAJError, ValueError) as e:
            nome, explicacao = autos.explica_falha(e)
            print(f'{numero}')
            print(f'  {nome}: {explicacao}')

    return 0


def main(argv=None) -> int:
    args = _monta_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING if args.silencioso else logging.INFO,
        format='%(message)s',
    )

    if args.sair:
        from esaj_autos.request import sessao_salva

        if sessao_salva.esquece():
            print('Sessão guardada apagada.')
        else:
            print('Não havia sessão guardada.')
        return 0

    numeros = _reune_numeros(args)
    if not numeros:
        print('\nNenhum processo válido informado. Nada a fazer.')
        return 1

    print(f'\n{len(numeros)} processo(s):')
    for numero in numeros:
        print(f'  {numero}')

    # O formato é escolhido antes do login, junto com os números:
    # tudo o que depende de você fica reunido no começo. Conferir não
    # baixa nada, e retomar recolhe pedidos cujo formato já foi
    # decidido — em nenhum dos dois a pergunta faz sentido.
    if args.conferir or args.retomar:
        separar = True
    else:
        separar = _escolhe_formato(args)

    try:
        sessao = login.entrar(
            cpf=args.cpf,
            senha=os.getenv('PASSWORD_TJSP'),
            reaproveitar=not args.novo_login,
        )

    except ESAJError as e:
        print(f'\nNão foi possível entrar no e-SAJ: {e}')
        return 1

    except KeyboardInterrupt:
        print('\nInterrompido.')
        return 130

    if args.conferir:
        return _confere(sessao, numeros, args)

    if args.retomar:
        print('\nRecolhendo pedidos já feitos ao e-SAJ...\n')
        return _retoma(sessao, numeros, args, separar)

    print(f'\nBaixando {len(numeros)} processo(s)...\n')

    try:
        resultado = autos.baixar_lote(
            sessao=sessao,
            numeros_cnj=numeros,
            destino=args.destino,
            instancia='Segundo Grau' if args.grau == '2' else 'Primeiro Grau',
            separar_documentos=separar,
            espera_maxima=args.espera,
            sobrescrever=args.sobrescrever,
            pecas_por_pedido=args.pecas_por_pedido,
        )

    except KeyboardInterrupt:
        print(
            '\nInterrompido. Os pedidos já feitos ao e-SAJ continuam '
            'válidos: use "--retomar" para recolhê-los.'
        )
        return 130

    print()
    print(resultado)

    for sucesso in resultado.sucessos:
        for parte in sucesso.partes or [sucesso.arquivo]:
            print(f'  {parte}  ({parte.stat().st_size:,} bytes)')

    return 0 if not resultado.falhas else 1


if __name__ == '__main__':
    sys.exit(main())

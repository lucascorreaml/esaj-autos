"""
Cópia integral dos autos do e-SAJ (TJSP).

A partir do número CNJ de um processo, baixa a pasta digital inteira
e grava os arquivos como o tribunal os entrega, sem conversão.

Os subpacotes são carregados só quando usados: a interface gráfica
traz o `tkinter` junto, e quem usa apenas a linha de comando não
precisa pagar por ele.
"""

import importlib

__version__ = '1.0.0'

_SUBMODULOS = ('autos', 'cli', 'exceptions', 'gui', 'modelos', 'request')

_ATALHOS = {
    'login': 'esaj_autos.request.login',
    'pasta_digital': 'esaj_autos.request.pasta_digital',
    'sessao_salva': 'esaj_autos.request.sessao_salva',
}


def __getattr__(nome):
    """
    Carrega o subpacote na primeira vez que ele é pedido.

    :param nome: atributo procurado
    :return: o módulo correspondente
    :raises AttributeError: quando não há tal atributo
    """
    if nome in _SUBMODULOS:
        modulo = importlib.import_module(f'.{nome}', __name__)
    elif nome in _ATALHOS:
        modulo = importlib.import_module(_ATALHOS[nome])
    else:
        raise AttributeError(
            f'O pacote "esaj_autos" não tem o atributo "{nome}".'
        )

    globals()[nome] = modulo
    return modulo


def __dir__():
    return sorted(set(globals()) | set(_SUBMODULOS) | set(_ATALHOS))

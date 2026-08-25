"""
Testes do carregamento sob demanda dos subpacotes.

A interface gráfica traz o `tkinter` junto. Se importar o pacote
arrastar a interface, quem usa só a linha de comando paga por ela — e,
pior, o empacotamento em executável engorda e passa a quebrar em
máquina sem suporte gráfico.
"""

import subprocess
import sys

import pytest


def _carregou(codigo: str, modulo: str) -> bool:
    """
    Executa o trecho em um processo limpo e diz se o módulo entrou.

    Precisa ser em outro processo: uma vez importado, o módulo fica em
    `sys.modules` e contaminaria os testes seguintes.

    :param codigo: trecho a executar
    :param modulo: nome do módulo procurado
    :return: `True` se o módulo foi carregado
    """
    programa = (
        'import sys\n'
        f'{codigo}\n'
        f"print('SIM' if '{modulo}' in sys.modules else 'NAO')\n"
    )
    saida = subprocess.run(
        [sys.executable, '-c', programa],
        capture_output=True,
        text=True,
        check=True,
    )
    return saida.stdout.strip().endswith('SIM')


class TestInterfaceGraficaNaoVemDeCarona:
    def test_importar_o_pacote(self):
        assert not _carregou('import esaj_autos', 'tkinter')

    def test_importar_o_download(self):
        assert not _carregou('from esaj_autos import autos', 'tkinter')

    def test_importar_o_login(self):
        assert not _carregou(
            'from esaj_autos.request import login', 'tkinter'
        )

    def test_importar_a_linha_de_comando(self):
        assert not _carregou('from esaj_autos import cli', 'tkinter')

    def test_usar_os_modelos(self):
        assert not _carregou(
            'from esaj_autos.modelos import DownloadAutos\n'
            'DownloadAutos(numero_cnj="12345678920208260100")',
            'tkinter',
        )


class TestOQuePrecisaCarregarCarrega:
    """
    Carregar sob demanda não pode deixar nada inalcançável.
    """

    def test_a_interface_grafica_ao_ser_pedida(self):
        pytest.importorskip('tkinter', reason='tkinter ausente')
        assert _carregou('from esaj_autos import gui', 'tkinter')

    def test_atalhos_do_pacote(self):
        programa = (
            'import esaj_autos\n'
            'assert esaj_autos.login.entrar\n'
            'assert esaj_autos.pasta_digital.normaliza_cnj\n'
            'assert esaj_autos.sessao_salva.carrega\n'
            'assert esaj_autos.exceptions.ESAJError\n'
            'assert esaj_autos.modelos.DownloadAutos\n'
            'print("OK")\n'
        )
        saida = subprocess.run(
            [sys.executable, '-c', programa],
            capture_output=True,
            text=True,
            check=True,
        )
        assert 'OK' in saida.stdout

    def test_atributo_inexistente_da_erro_claro(self):
        programa = (
            'import esaj_autos\n'
            'try:\n'
            '    esaj_autos.coisa_que_nao_existe\n'
            'except AttributeError as e:\n'
            '    assert "não tem o atributo" in str(e), e\n'
            '    print("OK")\n'
        )
        saida = subprocess.run(
            [sys.executable, '-c', programa],
            capture_output=True,
            text=True,
            check=True,
        )
        assert 'OK' in saida.stdout

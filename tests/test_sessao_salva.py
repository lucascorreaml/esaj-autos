"""
Testes do reaproveitamento da sessão entre execuções.

Cada login gasta um código de verificação de uso único. Guardar a
sessão evita esse custo enquanto o e-SAJ a considerar válida — e a
validade é sempre perguntada ao servidor, nunca deduzida do tempo
decorrido.
"""

import json

import pytest
import requests
import responses

from esaj_autos.request import login, sessao_salva

URL_VERIFICA = 'https://esaj.tjsp.jus.br/sajcas/verificarLogin.js'
JS_LOGADO = 'window.sajcas = { usuarioLogadoNoCasServer: true };'
JS_DESLOGADO = 'window.sajcas = { usuarioLogadoNoCasServer: false };'


@pytest.fixture
def arquivo(tmp_path):
    return tmp_path / 'sessao.json'


def _sessao_com_cookie():
    sessao = requests.Session()
    sessao.cookies.set(
        'JSESSIONID', 'abc123', domain='esaj.tjsp.jus.br', path='/'
    )
    return sessao


class TestGuardaERecupera:
    @responses.activate
    def test_recupera_sessao_ainda_valida(self, arquivo):
        responses.get(URL_VERIFICA, body=JS_LOGADO)

        sessao_salva.salva(_sessao_com_cookie(), arquivo)
        recuperada = sessao_salva.carrega(arquivo)

        assert recuperada is not None
        assert recuperada.cookies.get('JSESSIONID') == 'abc123'

    @responses.activate
    def test_descarta_sessao_expirada(self, arquivo):
        """
        Devolver uma sessão morta faria o download falhar adiante, com
        erro obscuro. Melhor recusá-la aqui e pedir login.
        """
        responses.get(URL_VERIFICA, body=JS_DESLOGADO)

        sessao_salva.salva(_sessao_com_cookie(), arquivo)
        assert sessao_salva.carrega(arquivo) is None

        # E não fica lixo para a próxima execução tropeçar.
        assert not arquivo.exists()

    def test_sem_arquivo_nao_ha_sessao(self, arquivo):
        assert sessao_salva.carrega(arquivo) is None

    def test_arquivo_corrompido_nao_quebra(self, arquivo):
        arquivo.write_text('isto não é json', encoding='utf-8')
        assert sessao_salva.carrega(arquivo) is None

    def test_esquece(self, arquivo):
        sessao_salva.salva(_sessao_com_cookie(), arquivo)
        assert sessao_salva.esquece(arquivo) is True
        assert sessao_salva.esquece(arquivo) is False

    def test_guarda_dominio_e_caminho_do_cookie(self, arquivo):
        sessao_salva.salva(_sessao_com_cookie(), arquivo)
        dados = json.loads(arquivo.read_text(encoding='utf-8'))

        cookie = dados['cookies'][0]
        assert cookie['name'] == 'JSESSIONID'
        assert cookie['domain'] == 'esaj.tjsp.jus.br'
        assert cookie['path'] == '/'

    def test_fica_fora_do_projeto(self):
        """
        A sessão dá acesso aos autos: não pode acompanhar o código
        nem entrar em versionamento.
        """
        caminho = sessao_salva.caminho_padrao()
        assert caminho.name == 'sessao.json'
        assert caminho.parent.name == 'esaj_autos'
        assert 'esaj-py' not in str(caminho)


class TestEntrarReaproveita:
    @responses.activate
    def test_nao_pede_codigo_quando_ha_sessao_valida(self, monkeypatch):
        responses.get(URL_VERIFICA, body=JS_LOGADO)
        monkeypatch.setattr(
            sessao_salva, 'carrega', lambda **kw: _sessao_com_cookie()
        )
        monkeypatch.setattr(
            'builtins.input',
            lambda _: pytest.fail('não deveria perguntar nada'),
        )

        sessao = login.entrar()
        assert sessao.cookies.get('JSESSIONID') == 'abc123'

    def test_novo_login_ignora_a_guardada(self, monkeypatch):
        chamou_carrega = []
        monkeypatch.setattr(
            sessao_salva,
            'carrega',
            lambda **kw: chamou_carrega.append(True),
        )
        monkeypatch.setattr(
            'builtins.input', lambda _: (_ for _ in ()).throw(EOFError)
        )

        with pytest.raises(Exception):
            login.entrar(reaproveitar=False)

        assert chamou_carrega == []

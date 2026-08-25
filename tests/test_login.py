"""
Testes da autenticação por HTTP e do download em lote.

As respostas do e-SAJ são simuladas. Os testes cobrem o contrato do
formulário CAS (campos enviados em cada etapa) e o comportamento do
lote diante de falhas.
"""

import pytest
import responses

from esaj_autos import autos, exceptions
from esaj_autos.request import login
from esaj_autos.request import pasta_digital as pd

# HTML da página de login do e-SAJ, com os campos ocultos do CAS.
HTML_LOGIN = """
<html><form>
  <input id="usernameForm" name="username" type="text" value=""/>
  <input id="passwordForm" name="password" type="password" value=""/>
  <input type="hidden" name="lt" value="LT-1"/>
  <input type="hidden" id="flowExecutionKey" name="execution" value="e1s1"/>
  <input type="hidden" name="_eventId" value="submit"/>
</form></html>
"""

# Página seguinte, que pede o código enviado por e-mail. O portal
# sinaliza esse estado ligando "DuploFatorHabilitado": é isso que faz
# a tela do código aparecer, e o que indica que as credenciais
# passaram.
HTML_TOKEN = """
<html><form>
  <input type="hidden" name="lt" value="LT-2"/>
  <input type="hidden" name="execution" value="e1s2"/>
  <input type="text" id="tokenInformado" name="tokenInformado" maxlength="6"/>
</form>
<script>
  $.saj.cas.SenhaExpirada = false;
  $.saj.cas.DuploFatorHabilitado = true || false;
  $.saj.cas.DeEmail = "l***@exemplo.com";
  $.saj.cas.CdUsuario = "U-1";
</script></html>
"""

# Erro genérico do CAS: nem recusa as credenciais, nem liga a tela do
# código.
HTML_ERRO_DE_FLUXO = (
    '<html><p class="errorMsg" role="alert">Não foi possível completar '
    'solicitação. Tente novamente mais tarde.</p>'
    '<script>$.saj.cas.DuploFatorHabilitado = false || false;</script>'
    '</html>'
)

# Recusas reais do e-SAJ: vêm em <p class="errorMsg">, e não no
# container "mensagemRetorno" usado no resto do portal.
HTML_CREDENCIAL_INVALIDA = (
    '<html><p class="errorMsg" role="alert">Usuário ou senha '
    'inválidos.</p>'
    '<input id="tokenInformado" name="tokenInformado"/></html>'
)

HTML_TOKEN_INVALIDO = (
    '<html><p class="errorMsg" role="alert">O código informado está '
    'inválido.</p>'
    '<input id="tokenInformado" name="tokenInformado"/></html>'
)

JS_LOGADO = 'window.sajcas = { usuarioLogadoNoCasServer: true };'
JS_DESLOGADO = 'window.sajcas = { usuarioLogadoNoCasServer: false };'


@pytest.fixture
def sessao():
    """Sessão HTTP limpa para cada teste."""
    import requests

    return requests.Session()


def _registra_pagina_de_login(logado=False):
    responses.get(login.URL_PORTAL, body='ok')
    responses.get(login.URL_LOGIN, body=HTML_LOGIN)
    responses.get(
        'https://esaj.tjsp.jus.br/sajcas/verificarLogin.js',
        body=JS_LOGADO if logado else JS_DESLOGADO,
    )


def _corpo_do_post():
    """Corpo do último POST feito ao formulário de login."""
    posts = [c for c in responses.calls if c.request.method == 'POST']
    return posts[-1].request.body


class TestPrimeiraEtapa:
    @responses.activate
    def test_envia_os_campos_do_formulario_cas(self):
        _registra_pagina_de_login()
        responses.post(login.URL_LOGIN, body=HTML_TOKEN)

        auth = login.Autenticacao()
        auth.primeira_etapa(cpf='12345678900', senha='segredo')

        corpo = _corpo_do_post()
        assert 'username=12345678900' in corpo
        assert 'password=segredo' in corpo
        assert 'lt=LT-1' in corpo
        assert 'execution=e1s1' in corpo
        assert '_eventId=submit' in corpo

    @responses.activate
    def test_renova_os_campos_ocultos_para_a_segunda_etapa(self):
        """
        O CAS troca "lt" e "execution" entre as etapas; reenviar os
        antigos faz o servidor recusar o código.
        """
        _registra_pagina_de_login()
        responses.post(login.URL_LOGIN, body=HTML_TOKEN)

        auth = login.Autenticacao()
        auth.primeira_etapa(cpf='1', senha='2')

        assert auth._lt == 'LT-2'
        assert auth._execution == 'e1s2'

    @responses.activate
    def test_credenciais_invalidas(self):
        _registra_pagina_de_login()
        responses.post(login.URL_LOGIN, body=HTML_CREDENCIAL_INVALIDA)

        auth = login.Autenticacao()
        with pytest.raises(
            exceptions.AutenticacaoError, match='Usuário ou senha'
        ):
            auth.primeira_etapa(cpf='1', senha='errada')

    @responses.activate
    def test_reconhece_a_tela_do_codigo_pela_flag_do_portal(self):
        """
        Ausência de mensagem de erro não basta: o sinal de que as
        credenciais passaram é o portal ligar a tela do código.
        """
        _registra_pagina_de_login()
        responses.post(login.URL_LOGIN, body=HTML_TOKEN)

        auth = login.Autenticacao()
        auth.primeira_etapa(cpf='1', senha='2')

        assert auth.flags['DuploFatorHabilitado'] is True
        assert auth.flags['DeEmail'] == 'l***@exemplo.com'

    @responses.activate
    def test_erro_de_fluxo_e_relatado_com_diagnostico(self):
        _registra_pagina_de_login()
        responses.post(login.URL_LOGIN, body=HTML_ERRO_DE_FLUXO)

        auth = login.Autenticacao()
        with pytest.raises(
            exceptions.AutenticacaoError, match='diagnóstico'
        ) as erro:
            auth.primeira_etapa(cpf='1', senha='2')

        assert 'Não foi possível completar' in str(erro.value)
        assert 'DuploFatorHabilitado' in str(erro.value)

    @responses.activate
    def test_pede_novo_codigo(self):
        _registra_pagina_de_login()
        responses.post(login.URL_LOGIN, body=HTML_TOKEN)
        responses.post(login.URL_REENVIA_TOKEN, body='ok')

        auth = login.Autenticacao()
        auth.primeira_etapa(cpf='1', senha='2')
        assert auth.reenviar_codigo() is True

        corpo = responses.calls[-1].request.body
        assert 'cdUsuario=U-1' in corpo

    def test_nao_pede_novo_codigo_antes_das_credenciais(self):
        auth = login.Autenticacao()
        with pytest.raises(exceptions.AutenticacaoError):
            auth.reenviar_codigo()

    @responses.activate
    def test_esaj_fora_do_ar_na_pagina_de_login(self):
        responses.get(login.URL_PORTAL, body='ok')
        responses.get(login.URL_LOGIN, status=503)

        auth = login.Autenticacao()
        with pytest.raises(exceptions.ESAJIndisponivelError):
            auth.primeira_etapa(cpf='1', senha='2')

    @responses.activate
    def test_pagina_de_login_em_formato_inesperado(self):
        responses.get(login.URL_PORTAL, body='ok')
        responses.get(login.URL_LOGIN, body='<html>outra coisa</html>')

        auth = login.Autenticacao()
        with pytest.raises(
            exceptions.ESAJIndisponivelError, match='campos esperados'
        ):
            auth.primeira_etapa(cpf='1', senha='2')


class TestSegundaEtapa:
    @responses.activate
    def test_conclui_o_login(self):
        _registra_pagina_de_login()
        responses.post(login.URL_LOGIN, body=HTML_TOKEN)
        responses.get(login.URL_LOGIN.split('?')[0], body='')
        responses.get(
            'https://esaj.tjsp.jus.br/sajcas/verificarLogin.js',
            body=JS_DESLOGADO,
        )

        auth = login.Autenticacao()
        auth.primeira_etapa(cpf='1', senha='2')

        responses.reset()
        responses.post(login.URL_LOGIN, body='<html>ok</html>')
        responses.get(
            'https://esaj.tjsp.jus.br/sajcas/verificarLogin.js',
            body=JS_LOGADO,
        )

        sessao = auth.segunda_etapa(token='123456')
        assert sessao is auth.sessao

        corpo = _corpo_do_post()
        assert 'token=123456' in corpo
        # O formulário do código é o mesmo da primeira etapa e leva
        # usuário e senha de volta; sem eles o CAS recusa o fluxo.
        assert 'username=1' in corpo
        assert 'password=2' in corpo

    @responses.activate
    def test_aceita_codigo_com_espacos_ou_hifen(self):
        responses.post(login.URL_LOGIN, body='ok')
        responses.get(
            'https://esaj.tjsp.jus.br/sajcas/verificarLogin.js',
            body=JS_LOGADO,
        )

        auth = login.Autenticacao()
        auth.segunda_etapa(token=' 123 456 ')
        assert 'token=123456' in _corpo_do_post()

    @responses.activate
    def test_codigo_invalido(self):
        responses.post(login.URL_LOGIN, body=HTML_TOKEN_INVALIDO)
        responses.get(
            'https://esaj.tjsp.jus.br/sajcas/verificarLogin.js',
            body=JS_DESLOGADO,
        )

        auth = login.Autenticacao()
        with pytest.raises(
            exceptions.AutenticacaoError, match='recusou o envio do código'
        ):
            auth.segunda_etapa(token='000000')

    @responses.activate
    def test_pagina_de_login_sempre_tem_o_campo_do_token(self):
        """
        O formulário do código está na página mesmo quando as
        credenciais são recusadas: sua presença não pode servir de
        sinal de sucesso.
        """
        assert 'tokenInformado' in HTML_CREDENCIAL_INVALIDA
        assert login.primeiro_erro(HTML_CREDENCIAL_INVALIDA)
        assert login.primeiro_erro(HTML_TOKEN) is None

    def test_codigo_sem_digitos(self):
        auth = login.Autenticacao()
        with pytest.raises(exceptions.AutenticacaoError, match='dígitos'):
            auth.segunda_etapa(token='abc')


# ---------------------------------------------------------------------
# Download em lote
# ---------------------------------------------------------------------

CNJ_A = '1111111-11.2020.8.26.0100'
CNJ_B = '2222222-22.2020.8.26.0100'


def _registra_download_completo(numero_digitos, cd, zip_bytes):
    """Registra a cadeia inteira de um download bem-sucedido."""
    url_pasta = 'https://esaj.tjsp.jus.br/pastadigital/abrir.do?t=1'
    url_arquivo = f'https://esaj.tjsp.jus.br/pastadigital/{cd}.zip'

    responses.get(
        pd.URL_API_BUSCA.format(grau='cpopg', numero=numero_digitos),
        json=[{'cdProcesso': cd}],
    )
    responses.get(pd.URL_SHOW.format(grau='cpopg'), body='ok')
    responses.get(pd.URL_ABRE_PASTA_1GRAU, body=url_pasta)
    responses.get(
        url_pasta,
        body='requestScope = {"data": {"cdDocumento": "9"}, '
        '"children": [{"data": {"parametros": "p1"}}]};',
    )
    responses.post(pd.URL_PREPARA, body='LOC')
    responses.post(pd.URL_BUSCA_PRONTO, body=url_arquivo)
    responses.get(url_arquivo, body=zip_bytes)


class TestLote:
    @responses.activate
    def test_baixa_varios_processos_na_mesma_sessao(self, tmp_path):
        import requests

        conteudo = b'PK\x03\x04' + b'z' * 50
        responses.get(
            'https://esaj.tjsp.jus.br/sajcas/verificarLogin.js',
            body=JS_LOGADO,
        )
        _registra_download_completo('11111111120208260100', 'CD-A', conteudo)
        _registra_download_completo('22222222220208260100', 'CD-B', conteudo)

        resultado = autos.baixar_lote(
            sessao=requests.Session(),
            numeros_cnj=[CNJ_A, CNJ_B],
            destino=tmp_path,
        )

        assert len(resultado.sucessos) == 2
        assert resultado.falhas == []
        assert resultado.total == 2
        for numero in (CNJ_A, CNJ_B):
            assert (tmp_path / numero / f'{numero}.zip').is_file()

    @responses.activate
    def test_uma_falha_nao_derruba_o_lote(self, tmp_path):
        import requests

        conteudo = b'PK\x03\x04' + b'z' * 50
        responses.get(
            'https://esaj.tjsp.jus.br/sajcas/verificarLogin.js',
            body=JS_LOGADO,
        )
        # O primeiro número é inválido; o segundo baixa normalmente.
        _registra_download_completo('22222222220208260100', 'CD-B', conteudo)

        resultado = autos.baixar_lote(
            sessao=requests.Session(),
            numeros_cnj=['numero-invalido', CNJ_B],
            destino=tmp_path,
        )

        assert len(resultado.sucessos) == 1
        assert len(resultado.falhas) == 1
        assert resultado.falhas[0].numero_cnj == 'numero-invalido'
        assert (tmp_path / CNJ_B / f'{CNJ_B}.zip').is_file()

    @responses.activate
    def test_pode_parar_no_primeiro_erro(self, tmp_path):
        import requests

        responses.get(
            'https://esaj.tjsp.jus.br/sajcas/verificarLogin.js',
            body=JS_LOGADO,
        )

        resultado = autos.baixar_lote(
            sessao=requests.Session(),
            numeros_cnj=['invalido-1', 'invalido-2'],
            destino=tmp_path,
            parar_no_primeiro_erro=True,
        )

        assert len(resultado.falhas) == 1

    @responses.activate
    def test_exige_login_antes_do_lote(self, tmp_path):
        import requests

        responses.get(
            'https://esaj.tjsp.jus.br/sajcas/verificarLogin.js',
            body=JS_DESLOGADO,
        )

        with pytest.raises(exceptions.AutenticacaoError):
            autos.baixar_lote(
                sessao=requests.Session(),
                numeros_cnj=[CNJ_A],
                destino=tmp_path,
            )


class TestLeNumeros:
    def test_le_um_por_linha_ignorando_comentarios(self, tmp_path):
        arquivo = tmp_path / 'lista.txt'
        arquivo.write_text(
            f'# meus processos\n{CNJ_A}\n\n  {CNJ_B}  \n# fim\n',
            encoding='utf-8',
        )
        assert autos.le_numeros(arquivo) == [CNJ_A, CNJ_B]


# ---------------------------------------------------------------------
# Retomada de um pedido já feito
# ---------------------------------------------------------------------


class TestRetomada:
    """
    Em processos volumosos a montagem demora muito. O pedido fica
    registrado em disco para que a espera possa ser recolhida depois,
    sem refazê-lo — o que consumiria outro acesso à pasta digital.
    """

    @responses.activate
    def test_registra_o_pedido_antes_de_esperar(self, tmp_path):
        import requests
        from esaj_autos.modelos import DownloadAutos

        conteudo = b'PK\x03\x04' + b'z' * 50
        responses.get(
            'https://esaj.tjsp.jus.br/sajcas/verificarLogin.js',
            body=JS_LOGADO,
        )
        _registra_download_completo('11111111120208260100', 'CD-A', conteudo)

        autos.baixar(
            requests.Session(), CNJ_A, destino=tmp_path
        )

        # Cumprido o pedido, o registro é removido.
        dados = DownloadAutos(numero_cnj=CNJ_A, destino=tmp_path)
        assert autos.le_preparacao(dados) is None

    @responses.activate
    def test_retoma_pedido_pendente_sem_refazer(self, tmp_path):
        import json

        import requests
        from esaj_autos.modelos import DownloadAutos

        conteudo = b'PK\x03\x04' + b'z' * 50
        url_arquivo = 'https://esaj.tjsp.jus.br/pastadigital/pronto.zip'

        dados = DownloadAutos(numero_cnj=CNJ_A, destino=tmp_path)
        dados.pasta_processo.mkdir(parents=True)
        (dados.pasta_processo / 'preparacao.json').write_text(
            json.dumps(
                {
                    'numero_cnj': CNJ_A,
                    'cd_processo': 'CD-A',
                    'cd_documento': '9',
                    'url_pasta': 'https://esaj.tjsp.jus.br/pastadigital/x',
                    'total_pecas': 42,
                    'partes': [
                        {
                            'localizador': 'LOC-PENDENTE',
                            'arquivo': str(dados.arquivo),
                            'pecas': 42,
                        }
                    ],
                }
            ),
            encoding='utf-8',
        )

        responses.get(
            'https://esaj.tjsp.jus.br/sajcas/verificarLogin.js',
            body=JS_LOGADO,
        )
        responses.post(pd.URL_BUSCA_PRONTO, body=url_arquivo)
        responses.get(url_arquivo, body=conteudo)

        resultado = autos.retomar(
            requests.Session(), CNJ_A, destino=tmp_path
        )

        assert resultado.total_pecas == 42
        assert resultado.arquivo.read_bytes() == conteudo
        # Nenhum pedido novo foi feito ao e-SAJ.
        assert all(
            pd.URL_PREPARA not in c.request.url for c in responses.calls
        )
        # E o registro foi consumido.
        assert autos.le_preparacao(dados) is None

    @responses.activate
    def test_erro_claro_sem_pedido_pendente(self, tmp_path):
        import requests

        responses.get(
            'https://esaj.tjsp.jus.br/sajcas/verificarLogin.js',
            body=JS_LOGADO,
        )

        with pytest.raises(exceptions.ESAJError, match='Não há pedido'):
            autos.retomar(requests.Session(), CNJ_A, destino=tmp_path)


class TestLotesDePecas:
    """
    Processos com dezenas de milhares de peças não cabem em um pedido
    só. Dividir gera um arquivo por lote — a cópia continua integral,
    apenas entregue em partes.
    """

    @responses.activate
    def test_divide_o_pedido_e_grava_uma_parte_por_lote(self, tmp_path):
        import requests
        from esaj_autos.modelos import DownloadAutos

        conteudo = b'PK\x03\x04' + b'z' * 20
        url_pasta = 'https://esaj.tjsp.jus.br/pastadigital/abrir.do?t=1'

        responses.get(
            'https://esaj.tjsp.jus.br/sajcas/verificarLogin.js',
            body=JS_LOGADO,
        )
        responses.get(
            pd.URL_API_BUSCA.format(grau='cpopg', numero='11111111120208260100'),
            json=[{'cdProcesso': 'CD-A'}],
        )
        responses.get(pd.URL_SHOW.format(grau='cpopg'), body='ok')
        responses.get(pd.URL_ABRE_PASTA_1GRAU, body=url_pasta)
        # Cinco peças, em lotes de dois: três partes.
        filhos = ', '.join(
            f'{{"data": {{"parametros": "p{i}"}}}}' for i in range(5)
        )
        responses.get(
            url_pasta,
            body='requestScope = [{"data": {"cdDocumento": "9"}, '
            f'"children": [{filhos}]}}];',
        )
        for i in range(3):
            responses.post(pd.URL_PREPARA, body=f'LOC-{i}')
            responses.post(
                pd.URL_BUSCA_PRONTO,
                body=f'https://esaj.tjsp.jus.br/pd/parte{i}.zip',
            )
            responses.get(
                f'https://esaj.tjsp.jus.br/pd/parte{i}.zip', body=conteudo
            )

        resultado = autos.baixar(
            requests.Session(),
            CNJ_A,
            destino=tmp_path,
            pecas_por_pedido=2,
        )

        assert len(resultado.partes) == 3
        assert resultado.total_pecas == 5
        assert resultado.tamanho_bytes == len(conteudo) * 3

        nomes = sorted(p.name for p in resultado.partes)
        assert nomes == [
            f'{CNJ_A}-parte-1-de-3.zip',
            f'{CNJ_A}-parte-2-de-3.zip',
            f'{CNJ_A}-parte-3-de-3.zip',
        ]
        for parte in resultado.partes:
            assert parte.is_file()

        # Cada lote virou um pedido próprio ao e-SAJ.
        preparos = [
            c for c in responses.calls if pd.URL_PREPARA in c.request.url
        ]
        assert len(preparos) == 3
        for chamada in preparos:
            assert chamada.request.body.count('itensPdfSelecionados=') <= 2

        # Cumpridos todos, o registro é removido.
        dados = DownloadAutos(numero_cnj=CNJ_A, destino=tmp_path)
        assert autos.le_preparacao(dados) is None

    @responses.activate
    def test_um_lote_so_mantem_o_nome_de_sempre(self, tmp_path):
        import requests

        conteudo = b'PK\x03\x04' + b'z' * 20
        responses.get(
            'https://esaj.tjsp.jus.br/sajcas/verificarLogin.js',
            body=JS_LOGADO,
        )
        _registra_download_completo('11111111120208260100', 'CD-A', conteudo)

        resultado = autos.baixar(
            requests.Session(),
            CNJ_A,
            destino=tmp_path,
            pecas_por_pedido=500,
        )

        assert resultado.arquivo.name == f'{CNJ_A}.zip'
        assert resultado.partes == [resultado.arquivo]


class TestPendentes:
    """
    Retomar sem números: o programa descobre sozinho o que ficou pelo
    caminho, em vez de exigir que se lembre quais processos eram.
    """

    def _registro(self, tmp_path, numero, localizador):
        import json
        from esaj_autos.modelos import DownloadAutos

        dados = DownloadAutos(numero_cnj=numero, destino=tmp_path)
        dados.pasta_processo.mkdir(parents=True, exist_ok=True)
        parte = {'arquivo': str(dados.arquivo), 'pecas': 1}
        if localizador:
            parte['localizador'] = localizador
        (dados.pasta_processo / 'preparacao.json').write_text(
            json.dumps({'numero_cnj': numero, 'partes': [parte]}),
            encoding='utf-8',
        )

    def test_encontra_os_pedidos_em_andamento(self, tmp_path):
        self._registro(tmp_path, CNJ_A, 'LOC-1')
        self._registro(tmp_path, CNJ_B, 'LOC-2')

        assert autos.pendentes(tmp_path) == [CNJ_A, CNJ_B]

    def test_ignora_o_que_ja_foi_recolhido(self, tmp_path):
        """
        Parte sem localizador já veio: retomá-la seria trabalho à toa.
        """
        self._registro(tmp_path, CNJ_A, 'LOC-1')
        self._registro(tmp_path, CNJ_B, None)

        assert autos.pendentes(tmp_path) == [CNJ_A]

    def test_pasta_inexistente_ou_vazia(self, tmp_path):
        assert autos.pendentes(tmp_path / 'nao-existe') == []
        assert autos.pendentes(tmp_path) == []

    def test_registro_corrompido_nao_quebra(self, tmp_path):
        pasta = tmp_path / CNJ_A
        pasta.mkdir(parents=True)
        (pasta / 'preparacao.json').write_text('{quebrado', encoding='utf-8')

        assert autos.pendentes(tmp_path) == []


class TestUmaParteDeCadaVez:
    """
    O e-SAJ apaga o arquivo montado quando a transferência cai, e o
    localizador morre junto. Preparar todas as partes de antemão só
    acumularia pedidos que morrem antes de serem baixados: cada parte
    vai do pedido ao disco antes de a seguinte começar.
    """

    @responses.activate
    def test_prepara_e_baixa_parte_a_parte(self, tmp_path, monkeypatch):
        import requests
        from esaj_autos.modelos import DownloadAutos

        monkeypatch.setattr(pd.time, 'sleep', lambda _: None)
        conteudo = b'PK' + b'z' * 20
        url_pasta = 'https://esaj.tjsp.jus.br/pastadigital/abrir.do?t=1'

        responses.get(
            'https://esaj.tjsp.jus.br/sajcas/verificarLogin.js',
            body=JS_LOGADO,
        )
        responses.get(
            pd.URL_API_BUSCA.format(
                grau='cpopg', numero='11111111120208260100'
            ),
            json=[{'cdProcesso': 'CD-A'}],
        )
        responses.get(pd.URL_SHOW.format(grau='cpopg'), body='ok')
        responses.get(pd.URL_ABRE_PASTA_1GRAU, body=url_pasta)
        filhos = ', '.join(
            f'{{"data": {{"parametros": "p{i}"}}}}' for i in range(6)
        )
        responses.get(
            url_pasta,
            body='requestScope = [{"data": {"cdDocumento": "9"}, '
            f'"children": [{filhos}]}}];',
        )
        for i in range(3):
            responses.post(pd.URL_PREPARA, body=f'LOC-{i}')
            responses.post(
                pd.URL_BUSCA_PRONTO,
                body=f'https://esaj.tjsp.jus.br/pd/p{i}.zip',
            )
            responses.get(
                f'https://esaj.tjsp.jus.br/pd/p{i}.zip', body=conteudo
            )

        dados = DownloadAutos(
            numero_cnj=CNJ_A, destino=tmp_path, pecas_por_pedido=2
        )
        resultado = autos.baixar_com_parametros(
            requests.Session(), dados
        )

        assert len(resultado.partes) == 3
        for parte in resultado.partes:
            assert parte.is_file()

        # Cada pedido é seguido da sua transferência, e não empilhado
        # com os demais.
        ordem = [
            'preparo' if pd.URL_PREPARA in c.request.url else 'arquivo'
            for c in responses.calls
            if pd.URL_PREPARA in c.request.url
            or '/pd/p' in c.request.url
        ]
        assert ordem == [
            'preparo', 'arquivo', 'preparo', 'arquivo', 'preparo', 'arquivo'
        ]

        # Cumprido tudo, o registro é removido.
        assert autos.le_preparacao(dados) is None

    @responses.activate
    def test_refaz_o_pedido_da_parte_que_caiu(self, tmp_path, monkeypatch):
        """
        Como não há retomada possível, a unidade de repetição é o par
        pedido-transferência: refazer só a parte que caiu, sem custar
        as demais.
        """
        import requests
        from esaj_autos.modelos import DownloadAutos

        monkeypatch.setattr(pd.time, 'sleep', lambda _: None)
        conteudo = b'PK' + b'z' * 20
        url_pasta = 'https://esaj.tjsp.jus.br/pastadigital/abrir.do?t=1'

        responses.get(
            'https://esaj.tjsp.jus.br/sajcas/verificarLogin.js',
            body=JS_LOGADO,
        )
        responses.get(
            pd.URL_API_BUSCA.format(
                grau='cpopg', numero='11111111120208260100'
            ),
            json=[{'cdProcesso': 'CD-A'}],
        )
        responses.get(pd.URL_SHOW.format(grau='cpopg'), body='ok')
        responses.get(pd.URL_ABRE_PASTA_1GRAU, body=url_pasta)
        responses.get(
            url_pasta,
            body='requestScope = [{"data": {"cdDocumento": "9"}, '
            '"children": [{"data": {"parametros": "p1"}}]}];',
        )
        # Primeiro pedido: a transferência cai.
        responses.post(pd.URL_PREPARA, body='LOC-1')
        responses.post(
            pd.URL_BUSCA_PRONTO, body='https://esaj.tjsp.jus.br/pd/a.zip'
        )
        responses.get(
            'https://esaj.tjsp.jus.br/pd/a.zip',
            body=requests.exceptions.ChunkedEncodingError('caiu'),
        )
        # Pedido refeito: agora vai.
        responses.post(pd.URL_PREPARA, body='LOC-2')
        responses.post(
            pd.URL_BUSCA_PRONTO, body='https://esaj.tjsp.jus.br/pd/b.zip'
        )
        responses.get('https://esaj.tjsp.jus.br/pd/b.zip', body=conteudo)

        dados = DownloadAutos(numero_cnj=CNJ_A, destino=tmp_path)
        resultado = autos.baixar_com_parametros(
            requests.Session(), dados
        )

        assert resultado.arquivo.read_bytes() == conteudo
        # Dois pedidos: o que caiu e o que o substituiu.
        preparos = [
            c for c in responses.calls if pd.URL_PREPARA in c.request.url
        ]
        assert len(preparos) == 2


class TestReaproveitaPartesJaBaixadas:
    def test_nao_reabre_a_pasta_digital_se_tudo_ja_veio(self, tmp_path):
        """
        Abrir a pasta digital custa um dos acessos diários. Fazê-lo só
        para descobrir que nada falta é desperdício.
        """
        import requests
        from esaj_autos.modelos import DownloadAutos

        dados = DownloadAutos(
            numero_cnj=CNJ_A, destino=tmp_path, pecas_por_pedido=1000
        )
        dados.pasta_processo.mkdir(parents=True)
        for i in (1, 2, 3):
            dados.arquivo_da_parte(i, 3).write_bytes(b'PK\x03\x04zz')

        # Sem nenhuma resposta registrada: qualquer requisição ao
        # e-SAJ faz o teste falhar. O atalho vem antes até da
        # verificação de sessão.
        with responses.RequestsMock(
            assert_all_requests_are_fired=False
        ) as mock:
            resultado = autos.baixar_com_parametros(
                requests.Session(), dados
            )
            assert len(mock.calls) == 0

        assert resultado.reaproveitado is True
        assert len(resultado.partes) == 3
        assert resultado.tamanho_bytes == 6 * 3


class TestLoteParaSemSessao:
    @responses.activate
    def test_nao_insiste_depois_de_a_sessao_expirar(self, tmp_path):
        import requests

        responses.get(
            'https://esaj.tjsp.jus.br/sajcas/verificarLogin.js',
            body=JS_LOGADO,
        )
        # O e-SAJ recusa por falta de sessão em todas as chamadas.
        responses.get(
            pd.URL_API_BUSCA.format(
                grau='cpopg', numero='11111111120208260100'
            ),
            status=401,
        )
        responses.get(pd.URL_BUSCA_HTML.format(grau='cpopg'), status=401)

        resultado = autos.baixar_lote(
            sessao=requests.Session(),
            numeros_cnj=[CNJ_A, CNJ_B, '3333333-33.2020.8.26.0100'],
            destino=tmp_path,
        )

        # Interrompeu na primeira: os dois seguintes nem foram tentados.
        assert len(resultado.falhas) == 1
        assert resultado.falhas[0].erro == 'SessaoExpiradaError'


class TestSessaoDeOutraConta:
    def test_nao_reaproveita_sessao_de_cpf_diferente(self, tmp_path):
        """
        Reaproveitar a sessão de outra conta baixaria os autos com as
        permissões de quem não foi pedido, e em silêncio.
        """
        import requests
        from esaj_autos.request import sessao_salva

        arquivo = tmp_path / 'sessao.json'
        s = requests.Session()
        s.cookies.set('JSESSIONID', 'x', domain='esaj.tjsp.jus.br', path='/')
        sessao_salva.salva(s, arquivo, cpf='111.222.333-44')

        # Outra conta: recusada sem sequer consultar o e-SAJ.
        assert sessao_salva.carrega(arquivo, cpf='555.666.777-88') is None
        # E o arquivo continua lá, para o dono legítimo.
        assert arquivo.is_file()

    @responses.activate
    def test_reaproveita_a_do_mesmo_cpf_ainda_que_formatado_diferente(
        self, tmp_path
    ):
        import requests
        from esaj_autos.request import sessao_salva

        responses.get(
            'https://esaj.tjsp.jus.br/sajcas/verificarLogin.js',
            body=JS_LOGADO,
        )

        arquivo = tmp_path / 'sessao.json'
        s = requests.Session()
        s.cookies.set('JSESSIONID', 'x', domain='esaj.tjsp.jus.br', path='/')
        sessao_salva.salva(s, arquivo, cpf='111.222.333-44')

        assert sessao_salva.carrega(arquivo, cpf='11122233344') is not None


class TestLoteComProcessoInexistente:
    """
    Numa lista longa é fácil haver número errado, processo de outro
    tribunal, ou processo a que não se tem acesso. Nada disso pode
    custar os demais.
    """

    @responses.activate
    def test_lote_misto_segue_ate_o_fim(self, tmp_path, monkeypatch):
        import requests

        # A insistência do lote tem esperas reais; aqui não interessam.
        monkeypatch.setattr(autos.time, 'sleep', lambda _: None)

        conteudo = b'PK\x03\x04' + b'z' * 40
        url_pasta = 'https://esaj.tjsp.jus.br/pastadigital/abrir.do?t=1'
        arvore = (
            'requestScope = [{"data": {"cdDocumento": "9"}, '
            '"children": [{"data": {"parametros": "p1"}}]}];'
        )

        recusa = (
            '<html><td id="mensagemRetorno">Não foi possível validar o '
            'seu acesso a esse recurso.</td></html>'
        )

        bom_1 = '1111111-11.2020.8.26.0100'
        inexistente = '9999999-99.2020.8.26.0100'
        sem_acesso = '8888888-88.2020.8.26.0100'
        outro_tribunal = '1234567-89.2020.8.19.0001'
        bom_2 = '2222222-22.2020.8.26.0100'

        responses.get(
            'https://esaj.tjsp.jus.br/sajcas/verificarLogin.js',
            body=JS_LOGADO,
        )

        # Cada processo tem sua própria consulta na API pública.
        responses.get(
            pd.URL_API_BUSCA.format(
                grau='cpopg', numero='11111111120208260100'
            ),
            json=[{'cdProcesso': 'CD-1'}],
        )
        responses.get(
            pd.URL_API_BUSCA.format(
                grau='cpopg', numero='99999999920208260100'
            ),
            json=[],
        )
        responses.get(
            pd.URL_API_BUSCA.format(
                grau='cpopg', numero='88888888820208260100'
            ),
            json=[{'cdProcesso': 'CD-3'}],
        )
        responses.get(
            pd.URL_API_BUSCA.format(
                grau='cpopg', numero='22222222220208260100'
            ),
            json=[{'cdProcesso': 'CD-5'}],
        )
        # O inexistente também não aparece na consulta em HTML.
        responses.get(
            pd.URL_BUSCA_HTML.format(grau='cpopg'),
            body='<html>Não existem informações disponíveis.</html>',
        )

        # As chamadas seguintes compartilham a mesma URL: a ordem de
        # registro acompanha a ordem em que o lote as faz.
        responses.get(pd.URL_SHOW.format(grau='cpopg'), body='ok')
        responses.get(pd.URL_ABRE_PASTA_1GRAU, body=url_pasta)
        # Recusa nas duas tentativas: o lote insiste uma vez, porque o
        # mesmo texto serve para tropeço passageiro e falta real de
        # permissão.
        responses.get(pd.URL_ABRE_PASTA_1GRAU, body=recusa)
        responses.get(pd.URL_ABRE_PASTA_1GRAU, body=recusa)
        responses.get(pd.URL_ABRE_PASTA_1GRAU, body=url_pasta)
        responses.get(url_pasta, body=arvore)
        responses.post(pd.URL_PREPARA, body='LOC')
        responses.post(
            pd.URL_BUSCA_PRONTO,
            body='https://esaj.tjsp.jus.br/pd/pronto.zip',
        )
        responses.get(
            'https://esaj.tjsp.jus.br/pd/pronto.zip', body=conteudo
        )

        resultado = autos.baixar_lote(
            sessao=requests.Session(),
            numeros_cnj=[
                bom_1,
                inexistente,
                sem_acesso,
                outro_tribunal,
                bom_2,
            ],
            destino=tmp_path,
        )

        # Os dois bons vieram, apesar dos três problemas no meio.
        assert len(resultado.sucessos) == 2
        assert len(resultado.falhas) == 3
        assert resultado.total == 5

        baixados = {r.numero_cnj for r in resultado.sucessos}
        assert baixados == {bom_1, bom_2}
        for numero in (bom_1, bom_2):
            assert (tmp_path / numero / f'{numero}.zip').is_file()

        # E cada falha diz qual processo e por quê.
        por_numero = {f.numero_cnj: f for f in resultado.falhas}
        assert por_numero[inexistente].erro == 'ProcessoNaoEncontradoError'
        assert por_numero[sem_acesso].erro == 'SemAcessoAosAutosError'
        assert por_numero[outro_tribunal].erro == 'NumeroInvalido'
        # A explicação é a frase escrita para quem digitou errado,
        # sem o relato interno do validador.
        recado = por_numero[outro_tribunal].mensagem
        assert 'não é do TJSP' in recado
        assert 'validation error' not in recado
        assert 'pydantic' not in recado.lower()

    @responses.activate
    def test_o_ultimo_da_lista_falhar_nao_perde_os_anteriores(
        self, tmp_path
    ):
        """
        A falha no fim do lote não pode invalidar o que já veio.
        """
        import requests

        conteudo = b'PK\x03\x04' + b'z' * 40
        responses.get(
            'https://esaj.tjsp.jus.br/sajcas/verificarLogin.js',
            body=JS_LOGADO,
        )
        _registra_download_completo('11111111120208260100', 'CD-A', conteudo)
        responses.get(
            pd.URL_API_BUSCA.format(
                grau='cpopg', numero='99999999920208260100'
            ),
            json=[],
        )
        responses.get(
            pd.URL_BUSCA_HTML.format(grau='cpopg'), body='<html>nada</html>'
        )

        resultado = autos.baixar_lote(
            sessao=requests.Session(),
            numeros_cnj=[CNJ_A, '9999999-99.2020.8.26.0100'],
            destino=tmp_path,
        )

        assert len(resultado.sucessos) == 1
        assert (tmp_path / CNJ_A / f'{CNJ_A}.zip').is_file()
        assert len(resultado.falhas) == 1


class TestCarenciaEntreCodigos:
    """
    O e-SAJ impõe um intervalo entre dois pedidos de código e informa
    esse tempo na própria página. Dizer o número exato poupa o usuário
    de tentar cedo demais e gastar mais uma recusa.
    """

    def test_ate_tres_o_valor_e_em_minutos(self):
        # Regra do contador do próprio portal.
        assert login.espera_entre_codigos(
            {'TempoConfiguracaoDuploFator': '3'}
        ) == 180
        assert login.espera_entre_codigos(
            {'TempoConfiguracaoDuploFator': '1'}
        ) == 60

    def test_acima_de_tres_o_valor_e_em_segundos(self):
        assert login.espera_entre_codigos(
            {'TempoConfiguracaoDuploFator': '30'}
        ) == 30

    def test_ausente_ou_invalido(self):
        assert login.espera_entre_codigos({}) is None
        assert login.espera_entre_codigos(
            {'TempoConfiguracaoDuploFator': ''}
        ) is None
        assert login.espera_entre_codigos(
            {'TempoConfiguracaoDuploFator': '0'}
        ) is None

    @responses.activate
    def test_a_recusa_informa_quanto_esperar(self):
        """
        Foi o erro que apareceu na primeira execução pela janela: as
        credenciais valem, mas o código não é reenviado antes da
        carência.
        """
        html = (
            '<html><p class="errorMsg">Não foi possível completar '
            'solicitação. Tente novamente mais tarde.</p>'
            '<script>'
            '$.saj.cas.DuploFatorHabilitado = false || false;'
            "$.saj.cas.TempoConfiguracaoDuploFator = \"3\" || 1;"
            '</script></html>'
        )
        _registra_pagina_de_login()
        responses.post(login.URL_LOGIN, body=html)

        auth = login.Autenticacao()
        with pytest.raises(exceptions.AutenticacaoError) as erro:
            auth.primeira_etapa(cpf='1', senha='2')

        recado = str(erro.value)
        assert 'credenciais foram reconhecidas' in recado
        assert '3 minuto' in recado
        # E continua trazendo o que o e-SAJ disse, para diagnóstico.
        assert 'Não foi possível completar' in recado


class TestSumarioDoLote:
    """
    Um lote interrompido no meio não pode parecer completo: dizer
    "4 de 15" quando foram pedidos 36 esconde que 21 nem foram
    tentados.
    """

    def test_conta_os_nao_tentados(self):
        from esaj_autos.modelos import (
            FalhaDownload,
            ResultadoLote,
        )

        r = ResultadoLote(
            informados=36,
            falhas=[
                FalhaDownload(numero_cnj=CNJ_A, erro='X', mensagem='y')
            ],
        )
        assert r.total == 1
        assert r.nao_tentados == 35
        assert '36 processo(s)' in str(r)
        assert '35 processo(s) não chegaram a ser tentados' in str(r)

    def test_lote_completo_nao_menciona_pendencia(self):
        from esaj_autos.modelos import ResultadoLote

        r = ResultadoLote(informados=0)
        assert r.nao_tentados == 0
        assert 'não chegaram' not in str(r)

    @responses.activate
    def test_o_numero_pedido_chega_ao_resultado(self, tmp_path):
        import requests

        responses.get(
            'https://esaj.tjsp.jus.br/sajcas/verificarLogin.js',
            body=JS_LOGADO,
        )
        # A API de consulta é pública: um 401 nela só faz cair para a
        # consulta em HTML, que é onde a falta de sessão aparece.
        responses.get(
            pd.URL_API_BUSCA.format(
                grau='cpopg', numero='11111111120208260100'
            ),
            status=401,
        )
        responses.get(pd.URL_BUSCA_HTML.format(grau='cpopg'), status=401)

        resultado = autos.baixar_lote(
            sessao=requests.Session(),
            numeros_cnj=[CNJ_A, CNJ_B, '3333333-33.2020.8.26.0100'],
            destino=tmp_path,
        )

        assert resultado.informados == 3
        assert resultado.total == 1
        assert resultado.nao_tentados == 2


class TestTicketDeOutraSessao:
    """
    O pedido guardado traz o endereço da pasta digital com um ticket
    preso à sessão em que foi feito. Ao retomar depois de novo login,
    esse ticket já não vale e o e-SAJ responde 401 — embora o login
    esteja perfeito. Refazer a preparação custaria muitos minutos; só
    o ticket precisa ser renovado.
    """

    def _registro(self, tmp_path, numero):
        import json
        from esaj_autos.modelos import DownloadAutos

        dados = DownloadAutos(numero_cnj=numero, destino=tmp_path)
        dados.pasta_processo.mkdir(parents=True, exist_ok=True)
        (dados.pasta_processo / 'preparacao.json').write_text(
            json.dumps(
                {
                    'numero_cnj': numero,
                    'cd_processo': 'CD-VELHO',
                    'cd_documento': '9',
                    'url_pasta': 'https://esaj.tjsp.jus.br/pd/velho?t=1',
                    'total_pecas': 5,
                    'partes': [
                        {
                            'localizador': 'LOC-1',
                            'arquivo': str(dados.arquivo),
                            'pecas': 5,
                        }
                    ],
                }
            ),
            encoding='utf-8',
        )
        return dados

    @responses.activate
    def test_renova_o_ticket_e_recolhe_sem_refazer_o_pedido(
        self, tmp_path, monkeypatch
    ):
        import requests

        monkeypatch.setattr(pd.time, 'sleep', lambda _: None)
        conteudo = b'PK\x03\x04' + b'z' * 60
        # O que importa da chamada é gravar o registro do pedido.
        self._registro(tmp_path, CNJ_A)
        url_novo = 'https://esaj.tjsp.jus.br/pd/novo?t=2'
        url_arquivo = 'https://esaj.tjsp.jus.br/pd/pronto.zip'

        responses.get(
            'https://esaj.tjsp.jus.br/sajcas/verificarLogin.js',
            body=JS_LOGADO,
        )
        # Consulta com o ticket velho: recusada.
        responses.post(pd.URL_BUSCA_PRONTO, status=401)
        # Renovação do acesso à pasta digital.
        responses.get(pd.URL_SHOW.format(grau='cpopg'), body='ok')
        responses.get(pd.URL_ABRE_PASTA_1GRAU, body=url_novo)
        responses.get(url_novo, body='<html>pasta</html>')
        # Com o ticket novo e a sessão da pasta, o arquivo vem.
        responses.post(pd.URL_BUSCA_PRONTO, body=url_arquivo)
        responses.get(url_arquivo, body=conteudo)

        resultado = autos.retomar(
            requests.Session(), CNJ_A, destino=tmp_path
        )

        assert resultado.arquivo.read_bytes() == conteudo

        # Nenhuma preparação nova foi pedida: é isso que custa minutos.
        assert all(
            pd.URL_PREPARA not in c.request.url for c in responses.calls
        )
        # E a consulta seguinte usou o endereço renovado.
        consultas = [
            c for c in responses.calls
            if pd.URL_BUSCA_PRONTO in c.request.url
        ]
        assert consultas[-1].request.headers['Referer'] == url_novo

    def _registra_recusa_persistente(self):
        """Nem o ticket renovado é aceito na consulta."""
        responses.get(
            'https://esaj.tjsp.jus.br/sajcas/verificarLogin.js',
            body=JS_LOGADO,
        )
        responses.post(pd.URL_BUSCA_PRONTO, status=401)
        responses.get(pd.URL_SHOW.format(grau='cpopg'), body='ok')
        responses.get(
            pd.URL_ABRE_PASTA_1GRAU,
            body='https://esaj.tjsp.jus.br/pd/novo?t=2',
        )
        responses.get(
            'https://esaj.tjsp.jus.br/pd/novo', body='<html>pasta</html>'
        )
        responses.post(pd.URL_BUSCA_PRONTO, status=401)

    @responses.activate
    def test_refaz_o_pedido_quando_o_localizador_nao_vale_mais(
        self, tmp_path, monkeypatch
    ):
        """
        Renovado o ticket e ainda assim recusado, o localizador não
        sobreviveu à troca de sessão. Desistir deixaria o processo
        inalcançável; refazer custa a espera da montagem, mas entrega
        o arquivo.
        """
        import requests

        monkeypatch.setattr(pd.time, 'sleep', lambda _: None)
        conteudo = b'PK\x03\x04' + b'z' * 30
        dados = self._registro(tmp_path, CNJ_A)

        self._registra_recusa_persistente()

        # A partir daqui, o pedido é refeito do zero.
        responses.get(
            pd.URL_API_BUSCA.format(
                grau='cpopg', numero='11111111120208260100'
            ),
            json=[{'cdProcesso': 'CD-1'}],
        )
        responses.get(
            'https://esaj.tjsp.jus.br/pd/novo',
            body='requestScope = [{"data": {"cdDocumento": "9"}, '
            '"children": [{"data": {"parametros": "p1"}}]}];',
        )
        responses.post(pd.URL_PREPARA, body='LOC-NOVO')
        responses.post(
            pd.URL_BUSCA_PRONTO, body='https://esaj.tjsp.jus.br/pd/ok.zip'
        )
        responses.get('https://esaj.tjsp.jus.br/pd/ok.zip', body=conteudo)

        resultado = autos.retomar(
            requests.Session(), CNJ_A, destino=tmp_path
        )

        assert resultado.arquivo.read_bytes() == conteudo
        # Cumprido, o registro antigo não fica para trás.
        assert autos.le_preparacao(dados) is None

    @responses.activate
    def test_pode_desistir_em_vez_de_refazer(self, tmp_path, monkeypatch):
        """
        Quem prefere decidir por conta própria desliga o refazimento.
        """
        import requests

        monkeypatch.setattr(pd.time, 'sleep', lambda _: None)
        self._registro(tmp_path, CNJ_A)
        self._registra_recusa_persistente()

        with pytest.raises(exceptions.SessaoExpiradaError):
            autos.retomar(
                requests.Session(),
                CNJ_A,
                destino=tmp_path,
                refazer_se_preciso=False,
            )

    def test_a_mensagem_nao_culpa_so_o_login(self):
        import requests

        resposta = requests.Response()
        resposta.status_code = 401
        with pytest.raises(exceptions.SessaoExpiradaError) as erro:
            pd._verifica_resposta(resposta)

        recado = str(erro.value)
        assert 'pasta digital foi obtido em outra sessão' in recado


class TestEntradaNaPastaAoRetomar:
    """
    Obter o endereço com ticket não basta: o aplicativo da pasta
    digital tem sessão própria e só a reconhece quando o endereço é
    efetivamente visitado. No download comum isso acontece por tabela,
    ao ler a árvore; ao recolher um pedido antigo, precisa ser feito
    de propósito — sem isso, a consulta responde 401 mesmo com o
    login do portal válido.
    """

    def _registro(self, tmp_path, numero):
        import json
        from esaj_autos.modelos import DownloadAutos

        dados = DownloadAutos(numero_cnj=numero, destino=tmp_path)
        dados.pasta_processo.mkdir(parents=True, exist_ok=True)
        (dados.pasta_processo / 'preparacao.json').write_text(
            json.dumps(
                {
                    'numero_cnj': numero,
                    'cd_processo': 'CD-1',
                    'cd_documento': '9',
                    'url_pasta': 'https://esaj.tjsp.jus.br/pd/velho?t=1',
                    'total_pecas': 5,
                    'partes': [
                        {
                            'localizador': 'LOC-1',
                            'arquivo': str(dados.arquivo),
                            'pecas': 5,
                        }
                    ],
                }
            ),
            encoding='utf-8',
        )
        return dados

    @responses.activate
    def test_visita_a_pasta_antes_de_consultar(
        self, tmp_path, monkeypatch
    ):
        import requests

        monkeypatch.setattr(pd.time, 'sleep', lambda _: None)
        conteudo = b'PK\x03\x04' + b'z' * 60
        self._registro(tmp_path, CNJ_A)
        url_novo = 'https://esaj.tjsp.jus.br/pd/novo?t=2'
        url_arquivo = 'https://esaj.tjsp.jus.br/pd/pronto.zip'

        responses.get(
            'https://esaj.tjsp.jus.br/sajcas/verificarLogin.js',
            body=JS_LOGADO,
        )
        responses.post(pd.URL_BUSCA_PRONTO, status=401)
        responses.get(pd.URL_SHOW.format(grau='cpopg'), body='ok')
        responses.get(pd.URL_ABRE_PASTA_1GRAU, body=url_novo)
        # A visita ao endereço assinado, que faltava.
        responses.get(url_novo, body='<html>pasta</html>')
        responses.post(pd.URL_BUSCA_PRONTO, body=url_arquivo)
        responses.get(url_arquivo, body=conteudo)

        resultado = autos.retomar(
            requests.Session(), CNJ_A, destino=tmp_path
        )
        assert resultado.arquivo.read_bytes() == conteudo

        # A ordem importa: entrar na pasta vem antes de consultar.
        caminho = [
            ('GET' if c.request.method == 'GET' else 'POST', c.request.url)
            for c in responses.calls
        ]
        visita = next(
            i for i, (m, u) in enumerate(caminho)
            if m == 'GET' and u.startswith(url_novo)
        )
        consulta_ok = max(
            i for i, (m, u) in enumerate(caminho)
            if m == 'POST' and pd.URL_BUSCA_PRONTO in u
        )
        assert visita < consulta_ok

    @responses.activate
    def test_a_visita_nao_baixa_a_pagina(self, sessao):
        """
        A página da pasta pesa megabytes em processos volumosos, e o
        que interessa está nos cabeçalhos.
        """
        responses.get(
            'https://esaj.tjsp.jus.br/pd/x', body='y' * 200_000
        )

        pedidos = []
        original = sessao.get

        def espia(url, **kwargs):
            pedidos.append(kwargs.get('stream', False))
            return original(url, **kwargs)

        sessao.get = espia
        pd.entra_na_pasta(sessao, 'https://esaj.tjsp.jus.br/pd/x')

        assert pedidos == [True]

    @responses.activate
    def test_recusa_na_visita_e_relatada(self, sessao):
        responses.get('https://esaj.tjsp.jus.br/pd/x', status=401)

        with pytest.raises(exceptions.SessaoExpiradaError):
            pd.entra_na_pasta(sessao, 'https://esaj.tjsp.jus.br/pd/x')


class TestPedidoMortoNaoQueimaAHora:
    """
    O e-SAJ responde vazio tanto para "ainda montando" quanto para
    "não conheço esse localizador". Um pedido de outra sessão cai no
    segundo caso e nunca resolve — esperar a hora inteira por ele foi
    o que custou quatro horas em nove processos.
    """

    def _registro(self, tmp_path, numero):
        import json
        from esaj_autos.modelos import DownloadAutos

        dados = DownloadAutos(
            numero_cnj=numero, destino=tmp_path, espera_maxima=3600
        )
        dados.pasta_processo.mkdir(parents=True, exist_ok=True)
        (dados.pasta_processo / 'preparacao.json').write_text(
            json.dumps(
                {
                    'numero_cnj': numero,
                    'cd_processo': 'CD-1',
                    'cd_documento': '9',
                    'url_pasta': 'https://esaj.tjsp.jus.br/pd/velho?t=1',
                    'total_pecas': 5,
                    'partes': [
                        {
                            'localizador': 'LOC-MORTO',
                            'arquivo': str(dados.arquivo),
                            'pecas': 5,
                        }
                    ],
                }
            ),
            encoding='utf-8',
        )
        return dados

    @responses.activate
    def test_desiste_em_minutos_e_refaz_o_pedido(
        self, tmp_path, monkeypatch
    ):
        import requests

        # Relógio controlado, para medir quanto se esperou pelo
        # pedido morto antes de refazê-lo.
        relogio = {'t': 0.0}

        def dorme(segundos):
            relogio['t'] += max(float(segundos), 1.0)

        monkeypatch.setattr(pd.time, 'sleep', dorme)
        monkeypatch.setattr(pd.time, 'monotonic', lambda: relogio['t'])

        conteudo = b'PK\x03\x04' + b'z' * 30
        dados = self._registro(tmp_path, CNJ_A)
        url_novo = 'https://esaj.tjsp.jus.br/pd/novo?t=2'

        responses.get(
            'https://esaj.tjsp.jus.br/sajcas/verificarLogin.js',
            body=JS_LOGADO,
        )
        responses.post(pd.URL_BUSCA_PRONTO, status=401)
        responses.get(pd.URL_SHOW.format(grau='cpopg'), body='ok')
        responses.get(pd.URL_ABRE_PASTA_1GRAU, body=url_novo)
        responses.get(url_novo, body='<html>pasta</html>')
        responses.post(pd.URL_BUSCA_PRONTO, body='')
        responses.get(
            pd.URL_API_BUSCA.format(
                grau='cpopg', numero='11111111120208260100'
            ),
            json=[{'cdProcesso': 'CD-1'}],
        )
        responses.get(
            url_novo,
            body='requestScope = [{"data": {"cdDocumento": "9"}, '
            '"children": [{"data": {"parametros": "p1"}}]}];',
        )
        responses.post(pd.URL_PREPARA, body='LOC-NOVO')
        responses.post(
            pd.URL_BUSCA_PRONTO, body='https://esaj.tjsp.jus.br/pd/ok.zip'
        )
        responses.get('https://esaj.tjsp.jus.br/pd/ok.zip', body=conteudo)

        resultado = autos.retomar(
            requests.Session(), CNJ_A, destino=tmp_path
        )

        # O arquivo veio, apesar de o pedido guardado estar morto.
        assert resultado.arquivo.read_bytes() == conteudo
        assert autos.le_preparacao(dados) is None

        # E a desistência do pedido morto foi em minutos: com
        # "espera_maxima" de uma hora, esperá-la inteira em nove
        # processos custaria a tarde toda.
        assert relogio['t'] < 3600, (
            f'esperou {relogio["t"]:.0f}s pelo pedido morto'
        )

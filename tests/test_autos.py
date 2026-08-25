"""
Testes da obtenção da cópia integral dos autos.

As respostas do e-SAJ são simuladas: os testes cobrem o contrato do
fluxo (sequência de chamadas, corpo enviado, interpretação das
respostas e tratamento de erro), e não a disponibilidade do serviço.
"""

import json
import zipfile

import pytest
import requests
import responses

from esaj_autos import exceptions
from esaj_autos.request import pasta_digital as pd
from esaj_autos.modelos import DownloadAutos

# ---------------------------------------------------------------------
# Amostras
# ---------------------------------------------------------------------

CNJ = '1234567-89.2020.8.26.0100'
CNJ_DIGITOS = '12345678920208260100'
CD_PROCESSO = 'ABC123XYZ0000'
URL_PASTA = 'https://esaj.tjsp.jus.br/pastadigital/abrirPastaProcessoDigital.do?ticket=XYZ'

# Árvore no formato que a pasta digital embute na página. O nome de
# uma das peças traz ";" e aspas escapadas de propósito: é o caso que
# quebra a extração ingênua "corta no primeiro ponto-e-vírgula".
ARVORE = {
    'data': {'cdDocumento': '999', 'parametros': 'raiz'},
    'children': [
        {
            'data': {'title': 'Volume 1'},
            'children': [
                {'data': {'parametros': 'p1', 'title': 'Petição Inicial'}},
                {
                    'data': {
                        'parametros': 'p2',
                        'title': 'Documentos; diversos "x"',
                    }
                },
            ],
        },
        {'data': {'parametros': 'p3', 'title': 'Sentença'}},
        # Repetida de propósito: não pode aparecer duas vezes.
        {'data': {'parametros': 'p3', 'title': 'Sentença (cópia)'}},
    ],
}

HTML_PASTA = (
    '<html><script>var requestScope = '
    + json.dumps(ARVORE, ensure_ascii=False)
    + '; var outraCoisa = 1;</script></html>'
)

HTML_SEM_ACESSO = (
    '<html><td id="mensagemRetorno"><li>Não foi possível validar o '
    'seu acesso a esse recurso. Por favor, acesse os detalhes do '
    'processo e tente novamente.</li></td></html>'
)

HTML_LIMITE = (
    '<html><td>Você atingiu o limite diário de acessos à pasta '
    'digital em processos que não está vinculado.</td></html>'
)

HTML_SENHA = (
    '<html><td id="mensagemRetorno">Senha do processo inválida.</td></html>'
)

# Trecho real da página do processo no e-SAJ. Traz, no JavaScript de
# configuração, exatamente os textos usados para identificar recusas —
# mesmo quando não há recusa nenhuma. Serve de guarda contra a
# detecção por busca solta no corpo da página.
JS_CONFIG_DO_ESAJ = """
<script language="javascript" type="text/JavaScript">
    $.saj.acessoRecurso = {
        limitaAcessoPastaDigital: 'false',
        popupSenha: { mostrar: 'false' == 'true', titulo: 'Senha do processo' },
        popupMensagem: {
            titulo: 'Aviso:',
            texto: 'Você atingiu o limite diário de acessos à pasta digital '
                 + 'em processos que não está vinculado.'
        }
    };
</script>
"""


@pytest.fixture
def sessao():
    """Sessão HTTP limpa para cada teste."""
    return requests.Session()


@pytest.fixture
def relogio(monkeypatch):
    """
    Substitui a espera real por um relógio controlado.

    Sem isso, o teste do tempo limite ficaria girando em tempo real
    até estourar o prazo.
    """
    agora = {'t': 0.0}

    def dorme(segundos):
        agora['t'] += max(float(segundos), 1.0)

    monkeypatch.setattr(pd.time, 'sleep', dorme)
    monkeypatch.setattr(pd.time, 'monotonic', lambda: agora['t'])
    return agora


# ---------------------------------------------------------------------
# Normalização do número CNJ
# ---------------------------------------------------------------------


class TestNormalizaCNJ:
    def test_aceita_numero_pontuado(self):
        digitos, formatado = pd.normaliza_cnj(CNJ)
        assert digitos == CNJ_DIGITOS
        assert formatado == CNJ

    def test_aceita_numero_sem_pontuacao(self):
        digitos, formatado = pd.normaliza_cnj(CNJ_DIGITOS)
        assert digitos == CNJ_DIGITOS
        assert formatado == CNJ

    def test_recusa_quantidade_errada_de_digitos(self):
        with pytest.raises(ValueError, match='20 dígitos'):
            pd.normaliza_cnj('123456')

    def test_recusa_tribunal_diferente_do_tjsp(self):
        # Mesmo tamanho, mas TJRJ (.8.19.)
        with pytest.raises(ValueError, match='não é do TJSP'):
            pd.normaliza_cnj('12345678920208190001')


# ---------------------------------------------------------------------
# Leitura da árvore de documentos
# ---------------------------------------------------------------------


class TestExtraiRequestScope:
    def test_extrai_json_com_ponto_e_virgula_no_conteudo(self):
        """
        O nome de uma peça contém ";". A extração precisa acompanhar
        o balanceamento de chaves, e não cortar no primeiro ";".
        """
        arvore = pd._extrai_request_scope(HTML_PASTA)
        assert arvore == ARVORE

    def test_extrai_json_com_aspas_escapadas(self):
        html = '<script>requestScope = {"a": "diz \\"oi\\"; tchau"};</script>'
        assert pd._extrai_request_scope(html) == {'a': 'diz "oi"; tchau'}

    def test_aceita_lista_na_raiz(self):
        html = 'requestScope = [{"data": {"parametros": "p1"}}];'
        assert pd._extrai_request_scope(html) == [
            {'data': {'parametros': 'p1'}}
        ]

    def test_erro_quando_nao_ha_request_scope(self):
        with pytest.raises(exceptions.SemAcessoAosAutosError):
            pd._extrai_request_scope('<html>nada aqui</html>')

    def test_erro_quando_json_truncado(self):
        with pytest.raises(exceptions.SemAcessoAosAutosError):
            pd._extrai_request_scope('requestScope = {"a": [1, 2')


class TestColetaParametros:
    def test_percorre_arvore_e_reune_folhas_na_ordem(self):
        assert pd.coleta_parametros(ARVORE) == ['p1', 'p2', 'p3']

    def test_para_na_peca_e_nao_desce_ate_as_paginas(self):
        """
        A peça já tem "parametros", que vale pelo documento inteiro.
        Descer até as páginas transformaria um processo de algumas
        centenas de peças em dezenas de milhares de itens no pedido —
        que foi o que estourou o tempo do e-SAJ na prática.
        """
        arvore = {
            'data': {'cdDocumento': '1'},
            'children': [
                {
                    'data': {'parametros': 'peca-1', 'title': 'Petição'},
                    'children': [
                        {'data': {'parametros': 'pag-1'}},
                        {'data': {'parametros': 'pag-2'}},
                        {'data': {'parametros': 'pag-3'}},
                    ],
                },
                {
                    'data': {'parametros': 'peca-2'},
                    'children': [{'data': {'parametros': 'pag-4'}}],
                },
            ],
        }
        assert pd.coleta_parametros(arvore) == ['peca-1', 'peca-2']

    def test_atravessa_agrupadores_sem_parametros(self):
        """
        Volumes e pastas não têm "parametros": são só agrupadores e
        precisam ser atravessados, senão o processo vem incompleto.
        """
        arvore = {
            'children': [
                {
                    'data': {'title': 'Volume 1'},
                    'children': [
                        {
                            'data': {'title': 'Subvolume'},
                            'children': [
                                {'data': {'parametros': 'peca-1'}}
                            ],
                        }
                    ],
                }
            ]
        }
        assert pd.coleta_parametros(arvore) == ['peca-1']

    def test_raiz_com_parametros_nao_vira_peca_unica(self):
        """
        A raiz representa o processo. Tomá-la como peça reduziria os
        autos inteiros a um único item.
        """
        arvore = {
            'data': {'parametros': 'processo-inteiro'},
            'children': [
                {'data': {'parametros': 'peca-1'}},
                {'data': {'parametros': 'peca-2'}},
            ],
        }
        assert pd.coleta_parametros(arvore) == ['peca-1', 'peca-2']


class TestContaNiveis:
    def test_resume_a_forma_da_arvore(self):
        niveis = pd.conta_niveis(ARVORE)
        assert niveis[0] == 1          # raiz
        assert niveis[1] == 3          # volume + 2 peças
        assert niveis[2] == 2          # peças dentro do volume

    def test_ignora_no_intermediario_com_parametros(self):
        """
        A raiz tem 'parametros', mas tem filhos: não é uma peça.
        Incluí-la duplicaria os autos no pedido.
        """
        assert 'raiz' not in pd.coleta_parametros(ARVORE)

    def test_arvore_vazia_devolve_lista_vazia(self):
        assert pd.coleta_parametros({}) == []
        assert pd.coleta_parametros([]) == []

    def test_aceita_lista_na_raiz(self):
        arvore = [{'data': {'parametros': 'a'}}, {'data': {'parametros': 'b'}}]
        assert pd.coleta_parametros(arvore) == ['a', 'b']


class TestExtraiCdDocumento:
    def test_pega_da_raiz(self):
        assert pd.extrai_cd_documento(ARVORE) == '999'

    def test_desce_quando_raiz_nao_tem(self):
        arvore = {'children': [{'data': {'cdDocumento': '77'}}]}
        assert pd.extrai_cd_documento(arvore) == '77'

    def test_devolve_none_quando_nao_ha(self):
        assert pd.extrai_cd_documento({'data': {}}) is None


# ---------------------------------------------------------------------
# Tradução de erros
# ---------------------------------------------------------------------


class TestTratamentoDeErro:
    @pytest.mark.parametrize('codigo', [401, 403])
    def test_sessao_expirada(self, codigo):
        resposta = requests.Response()
        resposta.status_code = codigo
        with pytest.raises(exceptions.SessaoExpiradaError):
            pd._verifica_resposta(resposta)

    @pytest.mark.parametrize('codigo', [500, 502, 503])
    def test_esaj_indisponivel(self, codigo):
        resposta = requests.Response()
        resposta.status_code = codigo
        with pytest.raises(exceptions.ESAJIndisponivelError):
            pd._verifica_resposta(resposta)

    def test_status_ok_nao_levanta(self):
        resposta = requests.Response()
        resposta.status_code = 200
        pd._verifica_resposta(resposta)

    def test_limite_diario(self):
        # O e-SAJ mostra o limite fora do container de mensagem, então
        # só é reconhecido quando já se sabe que a resposta é erro.
        with pytest.raises(exceptions.LimiteAcessoExcedidoError):
            pd._verifica_mensagem_de_recusa(
                HTML_LIMITE, resposta_e_erro=True
            )

    def test_limite_diario_nao_dispara_em_pagina_normal(self):
        pd._verifica_mensagem_de_recusa(HTML_LIMITE)

    def test_processo_com_senha(self):
        with pytest.raises(exceptions.ProcessoComSenhaError):
            pd._verifica_mensagem_de_recusa(HTML_SENHA)

    def test_sem_acesso(self):
        with pytest.raises(exceptions.SemAcessoAosAutosError):
            pd._verifica_mensagem_de_recusa(HTML_SEM_ACESSO)

    def test_nao_confunde_o_javascript_da_pagina_com_recusa(self):
        """
        A página normal do processo traz os textos de aviso no
        JavaScript de configuração. Interpretá-los como recusa
        impediria o download de processos perfeitamente acessíveis.
        """
        pd._verifica_mensagem_de_recusa(JS_CONFIG_DO_ESAJ)

    def test_nao_confunde_a_pasta_digital_com_recusa(self):
        """
        A árvore de documentos convive com o mesmo JavaScript.
        """
        pd._verifica_mensagem_de_recusa(JS_CONFIG_DO_ESAJ + HTML_PASTA)

    def test_erro_de_motivo_desconhecido_ainda_e_recusa(self):
        html = '<td id="mensagemRetorno">Motivo novo e desconhecido.</td>'
        with pytest.raises(
            exceptions.SemAcessoAosAutosError, match='Motivo novo'
        ):
            pd._verifica_mensagem_de_recusa(html)

    def test_extrai_a_mensagem_do_esaj(self):
        assert 'Não foi possível validar' in pd.extrai_mensagem_de_erro(
            HTML_SEM_ACESSO
        )
        assert pd.extrai_mensagem_de_erro('<html>ok</html>') is None

    def test_erros_de_acesso_compartilham_a_mesma_base(self):
        """
        Quem só quer saber "não consegui acessar os autos" deve poder
        capturar uma exceção só.
        """
        assert issubclass(
            exceptions.LimiteAcessoExcedidoError,
            exceptions.SemAcessoAosAutosError,
        )
        assert issubclass(
            exceptions.ProcessoComSenhaError,
            exceptions.SemAcessoAosAutosError,
        )
        assert issubclass(
            exceptions.SemAcessoAosAutosError, exceptions.ESAJError
        )


# ---------------------------------------------------------------------
# Resolução do número CNJ para o código interno
# ---------------------------------------------------------------------


class TestResolveCdProcesso:
    @responses.activate
    def test_resolve_pela_api_publica(self, sessao):
        responses.get(
            pd.URL_API_BUSCA.format(grau='cpopg', numero=CNJ_DIGITOS),
            json=[{'numeProcesso': CNJ, 'cdProcesso': CD_PROCESSO}],
        )
        assert pd.resolve_cd_processo(sessao, CNJ) == CD_PROCESSO

    @responses.activate
    def test_cai_para_o_html_quando_a_api_falha(self, sessao):
        responses.get(
            pd.URL_API_BUSCA.format(grau='cpopg', numero=CNJ_DIGITOS),
            status=500,
        )
        responses.get(
            pd.URL_BUSCA_HTML.format(grau='cpopg'),
            body='<html>ok</html>',
            headers={
                'Content-Type': 'text/html',
            },
        )
        # O código aparece na URL final do redirecionamento; aqui ele
        # vem no corpo, que é o outro lugar onde é procurado.
        responses.reset()
        responses.get(
            pd.URL_API_BUSCA.format(grau='cpopg', numero=CNJ_DIGITOS),
            status=500,
        )
        responses.get(
            pd.URL_BUSCA_HTML.format(grau='cpopg'),
            body=f'<a href="/cpopg/show.do?processo.codigo={CD_PROCESSO}">x</a>',
        )
        assert pd.resolve_cd_processo(sessao, CNJ) == CD_PROCESSO

    @responses.activate
    def test_repete_em_sessao_limpa_quando_a_primeira_busca_nao_resolve(
        self, sessao
    ):
        """
        O e-SAJ só redireciona para o processo na primeira busca de
        cada sessão. Como a sessão autenticada já foi usada no login,
        a consulta precisa ser repetida em uma sessão isolada.
        """
        responses.get(
            pd.URL_API_BUSCA.format(grau='cpopg', numero=CNJ_DIGITOS),
            status=500,
        )
        # 1ª tentativa: devolve o formulário de busca, sem o código.
        responses.get(
            pd.URL_BUSCA_HTML.format(grau='cpopg'),
            body='<form name="consultarProcessoForm">sem resultado</form>',
        )
        # 2ª tentativa, na sessão isolada: redireciona para o processo.
        responses.get(
            pd.URL_BUSCA_HTML.format(grau='cpopg'),
            body=f'<a href="/cpopg/show.do?processo.codigo={CD_PROCESSO}">x</a>',
        )

        assert pd.resolve_cd_processo(sessao, CNJ) == CD_PROCESSO
        # API + duas consultas HTML
        assert len(responses.calls) == 3

    @responses.activate
    def test_processo_inexistente(self, sessao):
        responses.get(
            pd.URL_API_BUSCA.format(grau='cpopg', numero=CNJ_DIGITOS),
            json=[],
        )
        responses.get(
            pd.URL_BUSCA_HTML.format(grau='cpopg'),
            body='Não existem informações disponíveis.',
        )
        with pytest.raises(exceptions.ProcessoNaoEncontradoError):
            pd.resolve_cd_processo(sessao, CNJ)

    @responses.activate
    def test_usa_cposg_no_segundo_grau(self, sessao):
        responses.get(
            pd.URL_API_BUSCA.format(grau='cposg', numero=CNJ_DIGITOS),
            json=[{'cdProcesso': CD_PROCESSO}],
        )
        resultado = pd.resolve_cd_processo(
            sessao, CNJ, grau='Segundo Grau'
        )
        assert resultado == CD_PROCESSO


# ---------------------------------------------------------------------
# Abertura da pasta digital
# ---------------------------------------------------------------------


class TestAbrePastaDigital:
    @responses.activate
    def test_visita_os_detalhes_antes_de_abrir_a_pasta(self, sessao):
        """
        O e-SAJ só libera a pasta digital se o processo tiver sido
        aberto antes na mesma sessão.
        """
        responses.get(pd.URL_SHOW.format(grau='cpopg'), body='ok')
        responses.get(pd.URL_ABRE_PASTA_1GRAU, body=URL_PASTA)

        assert pd.abre_pasta_digital(sessao, CD_PROCESSO) == URL_PASTA

        chamadas = [c.request.url for c in responses.calls]
        assert 'show.do' in chamadas[0]
        assert 'abrirPastaDigital.do' in chamadas[1]

    @responses.activate
    def test_segundo_grau_usa_endpoint_proprio(self, sessao):
        responses.get(pd.URL_SHOW.format(grau='cposg'), body='ok')
        responses.get(pd.URL_ABRE_PASTA_2GRAU, body=URL_PASTA)

        resultado = pd.abre_pasta_digital(
            sessao, CD_PROCESSO, grau='Segundo Grau'
        )
        assert resultado == URL_PASTA
        assert 'verificarAcessoPastaDigital' in responses.calls[1].request.url

    @responses.activate
    def test_sem_permissao(self, sessao):
        responses.get(pd.URL_SHOW.format(grau='cpopg'), body='ok')
        responses.get(pd.URL_ABRE_PASTA_1GRAU, body=HTML_SEM_ACESSO)

        with pytest.raises(exceptions.SemAcessoAosAutosError):
            pd.abre_pasta_digital(sessao, CD_PROCESSO)

    @responses.activate
    def test_url_com_ticket_e_devolvida_intacta(self, sessao):
        """
        A URL da pasta traz o ticket de acesso e não pode ser
        recortada.
        """
        responses.get(pd.URL_SHOW.format(grau='cpopg'), body='ok')
        responses.get(pd.URL_ABRE_PASTA_1GRAU, body=f'  {URL_PASTA}\n')

        assert pd.abre_pasta_digital(sessao, CD_PROCESSO) == URL_PASTA

    @responses.activate
    def test_limite_diario(self, sessao):
        responses.get(pd.URL_SHOW.format(grau='cpopg'), body='ok')
        responses.get(pd.URL_ABRE_PASTA_1GRAU, body=HTML_LIMITE)

        with pytest.raises(exceptions.LimiteAcessoExcedidoError):
            pd.abre_pasta_digital(sessao, CD_PROCESSO)

    @responses.activate
    def test_esaj_fora_do_ar(self, sessao):
        responses.get(pd.URL_SHOW.format(grau='cpopg'), body='ok')
        responses.get(pd.URL_ABRE_PASTA_1GRAU, status=503)

        with pytest.raises(exceptions.ESAJIndisponivelError):
            pd.abre_pasta_digital(sessao, CD_PROCESSO)


# ---------------------------------------------------------------------
# Preparação assíncrona
# ---------------------------------------------------------------------


class TestSolicitaPreparacao:
    @responses.activate
    def test_envia_todas_as_pecas_e_o_formato(self, sessao):
        responses.post(pd.URL_PREPARA, body='LOC-123')

        localizador = pd.solicita_preparacao(
            sessao=sessao,
            url_pasta=URL_PASTA,
            cd_processo=CD_PROCESSO,
            parametros=['p1', 'p2', 'p3'],
            cd_documento='999',
            separar_documentos=True,
        )
        assert localizador == 'LOC-123'

        corpo = responses.calls[0].request.body
        assert corpo.count('itensPdfSelecionados=') == 3
        assert 'separarDocumentos=true' in corpo
        assert f'cdProcesso={CD_PROCESSO}' in corpo
        assert 'cdDocumento=999' in corpo

        cabecalhos = responses.calls[0].request.headers
        assert cabecalhos['X-Requested-With'] == 'XMLHttpRequest'
        assert cabecalhos['Referer'] == URL_PASTA

    @responses.activate
    def test_pdf_unico_muda_o_formato(self, sessao):
        responses.post(pd.URL_PREPARA, body='LOC-1')
        pd.solicita_preparacao(
            sessao=sessao,
            url_pasta=URL_PASTA,
            cd_processo=CD_PROCESSO,
            parametros=['p1'],
            separar_documentos=False,
        )
        assert 'separarDocumentos=false' in responses.calls[0].request.body

    def test_recusa_pedido_sem_pecas(self, sessao):
        with pytest.raises(exceptions.SemAcessoAosAutosError):
            pd.solicita_preparacao(
                sessao=sessao,
                url_pasta=URL_PASTA,
                cd_processo=CD_PROCESSO,
                parametros=[],
            )

    @responses.activate
    def test_localizador_vazio_e_recusa(self, sessao):
        responses.post(pd.URL_PREPARA, body='   ')
        with pytest.raises(exceptions.SemAcessoAosAutosError):
            pd.solicita_preparacao(
                sessao=sessao,
                url_pasta=URL_PASTA,
                cd_processo=CD_PROCESSO,
                parametros=['p1'],
            )


class TestAguardaFinalizacao:
    @responses.activate
    def test_consulta_ate_o_arquivo_ficar_pronto(self, sessao, relogio):
        url_final = 'https://esaj.tjsp.jus.br/pastadigital/arquivo.zip'

        responses.post(pd.URL_BUSCA_PRONTO, body='')
        responses.post(pd.URL_BUSCA_PRONTO, body='')
        responses.post(pd.URL_BUSCA_PRONTO, body=url_final)

        resultado = pd.aguarda_finalizacao(
            sessao=sessao,
            url_pasta=URL_PASTA,
            localizador='LOC-1',
            cd_processo=CD_PROCESSO,
            intervalo=1,
        )
        assert resultado == url_final
        assert len(responses.calls) == 3

    @responses.activate
    def test_estoura_o_tempo_de_espera(self, sessao, relogio):
        responses.post(pd.URL_BUSCA_PRONTO, body='')

        with pytest.raises(exceptions.PreparacaoTimeoutError):
            pd.aguarda_finalizacao(
                sessao=sessao,
                url_pasta=URL_PASTA,
                localizador='LOC-1',
                cd_processo=CD_PROCESSO,
                espera_maxima=30,
                intervalo=1,
            )

    @responses.activate
    def test_sessao_expirada_durante_a_espera(self, sessao, relogio):
        responses.post(pd.URL_BUSCA_PRONTO, status=401)

        with pytest.raises(exceptions.SessaoExpiradaError):
            pd.aguarda_finalizacao(
                sessao=sessao,
                url_pasta=URL_PASTA,
                localizador='LOC-1',
                cd_processo=CD_PROCESSO,
                intervalo=1,
            )


# ---------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------


class TestBaixaArquivo:
    @responses.activate
    def test_grava_o_arquivo_como_entregue(self, sessao, tmp_path):
        conteudo = b'PK\x03\x04conteudo-do-zip'
        responses.get(
            'https://esaj.tjsp.jus.br/arquivo.zip', body=conteudo
        )

        destino = tmp_path / 'proc' / 'autos.zip'
        resultado = pd.baixa_arquivo(
            sessao, 'https://esaj.tjsp.jus.br/arquivo.zip', destino
        )

        assert resultado == destino
        assert destino.read_bytes() == conteudo

    @responses.activate
    def test_nao_deixa_arquivo_parcial_apos_falha(
        self, sessao, tmp_path, relogio
    ):
        """
        Sem nada recebido, não há o que preservar: a falha não deixa
        arquivo nenhum para trás.
        """
        responses.get(
            'https://esaj.tjsp.jus.br/arquivo.zip',
            body=requests.ConnectionError('caiu'),
        )

        destino = tmp_path / 'autos.zip'
        with pytest.raises(exceptions.DownloadError):
            pd.baixa_arquivo(
                sessao,
                'https://esaj.tjsp.jus.br/arquivo.zip',
                destino,
                tentativas=2,
            )

        assert not destino.exists()
        assert list(tmp_path.glob('*.parcial')) == [] == []

    @responses.activate
    def test_recusa_arquivo_vazio(self, sessao, tmp_path):
        responses.get('https://esaj.tjsp.jus.br/arquivo.zip', body=b'')

        with pytest.raises(exceptions.DownloadError, match='vazio'):
            pd.baixa_arquivo(
                sessao,
                'https://esaj.tjsp.jus.br/arquivo.zip',
                tmp_path / 'autos.zip',
            )


# ---------------------------------------------------------------------
# Parâmetros e organização em disco
# ---------------------------------------------------------------------


class TestDownloadAutos:
    def test_organiza_em_pasta_propria_do_processo(self, tmp_path):
        dados = DownloadAutos(numero_cnj=CNJ_DIGITOS, destino=tmp_path)
        assert dados.pasta_processo == tmp_path / CNJ
        assert dados.arquivo == tmp_path / CNJ / f'{CNJ}.zip'

    def test_pdf_unico_muda_a_extensao(self, tmp_path):
        dados = DownloadAutos(
            numero_cnj=CNJ, destino=tmp_path, separar_documentos=False
        )
        assert dados.arquivo.suffix == '.pdf'

    def test_normaliza_o_numero_na_validacao(self):
        assert DownloadAutos(numero_cnj=CNJ_DIGITOS).numero_cnj == CNJ

    def test_recusa_numero_invalido(self):
        with pytest.raises(ValueError):
            DownloadAutos(numero_cnj='123')

    def test_recusa_espera_absurdamente_curta(self):
        with pytest.raises(ValueError):
            DownloadAutos(numero_cnj=CNJ, espera_maxima=1)


# ---------------------------------------------------------------------
# Fluxo completo, do CNJ ao arquivo em disco
# ---------------------------------------------------------------------


class DriverFalso:
    """Driver mínimo: registra a navegação e devolve cookies."""

    def __init__(self, url='https://esaj.tjsp.jus.br/esaj/portal.do'):
        self.current_url = url
        self.visitou = []

    def get(self, url):
        self.visitou.append(url)
        self.current_url = url

    def get_cookies(self):
        return [
            {
                'name': 'JSESSIONID',
                'value': 'abc',
                'domain': 'esaj.tjsp.jus.br',
                'path': '/',
            }
        ]


class TestSessao:
    def test_leva_os_cookies_do_navegador(self):
        from esaj_autos.request import session

        sessao = session.cria_sessao(DriverFalso())
        assert sessao.cookies.get('JSESSIONID') == 'abc'

    def test_valida_o_certificado_por_padrao(self):
        from esaj_autos.request import session

        assert session.cria_sessao(DriverFalso()).verify is True

    def test_volta_ao_esaj_quando_o_navegador_esta_em_outro_site(self):
        """
        O Selenium só entrega cookies do domínio corrente. Fora do
        e-SAJ, a sessão sairia sem autenticação nenhuma.
        """
        from esaj_autos.request import session

        driver = DriverFalso(url='https://www.google.com')
        session.cria_sessao(driver)
        assert driver.visitou
        assert 'esaj.tjsp.jus.br' in driver.visitou[0]

    def test_nao_navega_se_ja_esta_no_esaj(self):
        from esaj_autos.request import session

        driver = DriverFalso()
        session.cria_sessao(driver)
        assert driver.visitou == []


class TestFluxoCompleto:
    @responses.activate
    def test_do_cnj_ao_zip_em_disco(self, tmp_path, relogio):
        from esaj_autos import autos

        zip_bytes = b'PK\x03\x04' + b'x' * 100
        url_arquivo = 'https://esaj.tjsp.jus.br/pastadigital/pronto.zip'

        responses.get(
            'https://esaj.tjsp.jus.br/sajcas/verificarLogin.js',
            body='window.sajcas = { usuarioLogadoNoCasServer: true };',
        )
        responses.get(
            pd.URL_API_BUSCA.format(grau='cpopg', numero=CNJ_DIGITOS),
            json=[{'cdProcesso': CD_PROCESSO}],
        )
        responses.get(pd.URL_SHOW.format(grau='cpopg'), body='ok')
        responses.get(pd.URL_ABRE_PASTA_1GRAU, body=URL_PASTA)
        responses.get(URL_PASTA, body=HTML_PASTA)
        responses.post(pd.URL_PREPARA, body='LOC-9')
        responses.post(pd.URL_BUSCA_PRONTO, body=url_arquivo)
        responses.get(url_arquivo, body=zip_bytes)

        resultado = autos.baixar(
            sessao=DriverFalso(),
            numero_cnj=CNJ_DIGITOS,
            destino=tmp_path,
            intervalo=1,
        )

        assert resultado.numero_cnj == CNJ
        assert resultado.cd_processo == CD_PROCESSO
        assert resultado.total_pecas == 3
        assert resultado.formato == 'zip'
        assert resultado.reaproveitado is False
        assert resultado.arquivo == tmp_path / CNJ / f'{CNJ}.zip'
        assert resultado.arquivo.read_bytes() == zip_bytes
        assert resultado.tamanho_bytes == len(zip_bytes)

    @responses.activate
    def test_exige_login(self, tmp_path):
        from esaj_autos import autos

        responses.get(
            'https://esaj.tjsp.jus.br/sajcas/verificarLogin.js',
            body='window.sajcas = { usuarioLogadoNoCasServer: false };',
        )

        with pytest.raises(exceptions.AutenticacaoError):
            autos.baixar(
                sessao=DriverFalso(),
                numero_cnj=CNJ,
                destino=tmp_path,
            )

    def test_reaproveita_arquivo_ja_baixado(self, tmp_path):
        from esaj_autos import autos

        dados = DownloadAutos(numero_cnj=CNJ, destino=tmp_path)
        dados.arquivo.parent.mkdir(parents=True)
        dados.arquivo.write_bytes(b'ja-estava-aqui')

        # Sem nenhuma resposta HTTP registrada: se tentasse baixar,
        # a chamada de rede falharia.
        resultado = autos.baixar_com_parametros(
            sessao=DriverFalso(), dados=dados
        )
        assert resultado.reaproveitado is True
        assert resultado.tamanho_bytes == len(b'ja-estava-aqui')


# ---------------------------------------------------------------------
# Compatibilidade com o e-SAJ Merge PDFs
# ---------------------------------------------------------------------


def test_zip_preserva_a_nomenclatura_do_esaj(tmp_path):
    """
    O ZIP é gravado byte a byte como o e-SAJ o entrega, sem
    renomear nada. Isso mantém o padrão "Descrição (pag N - M).pdf",
    do qual dependem as ferramentas que ordenam e unem as peças.
    """
    origem = tmp_path / 'origem.zip'
    with zipfile.ZipFile(origem, 'w') as z:
        z.writestr('Petição (Outras) (pag 1 - 26).pdf', b'%PDF-1')
        z.writestr('Documentos Diversos (pag 27).pdf', b'%PDF-2')

    conteudo = origem.read_bytes()

    with responses.RequestsMock() as mock:
        mock.get('https://esaj.tjsp.jus.br/a.zip', body=conteudo)
        destino = pd.baixa_arquivo(
            requests.Session(),
            'https://esaj.tjsp.jus.br/a.zip',
            tmp_path / 'baixado.zip',
        )

    with zipfile.ZipFile(destino) as z:
        assert z.namelist() == [
            'Petição (Outras) (pag 1 - 26).pdf',
            'Documentos Diversos (pag 27).pdf',
        ]


# ---------------------------------------------------------------------
# Tolerância a queda de conexão
# ---------------------------------------------------------------------


class TestQuedaDeConexao:
    """
    O e-SAJ responde em "chunked", sem Content-Length. Quando a
    conexão cai no fim do corpo, o cliente recebe
    IncompleteRead(0 bytes read, 2 more expected) — os dois bytes do
    terminador. É transitório, e repetir o GET resolve.
    """

    @responses.activate
    def test_nao_baixa_o_corpo_dos_detalhes_do_processo(self, sessao):
        """
        O corpo de "show.do" nunca é usado: só interessa o efeito de
        registrar o processo na sessão. Em processos volumosos essa
        página tem megabytes, e baixá-la é puro risco.
        """
        responses.get(pd.URL_SHOW.format(grau='cpopg'), body='x' * 100_000)
        responses.get(pd.URL_ABRE_PASTA_1GRAU, body=URL_PASTA)

        pedidos = []
        original = sessao.get

        def espia(url, **kwargs):
            pedidos.append((url, kwargs.get('stream', False)))
            return original(url, **kwargs)

        sessao.get = espia
        pd.abre_pasta_digital(sessao, CD_PROCESSO)

        url_detalhes, em_fluxo = pedidos[0]
        assert 'show.do' in url_detalhes
        assert em_fluxo is True

        # A abertura da pasta, essa sim, precisa do corpo.
        assert pedidos[1][1] is False

    @responses.activate
    def test_repete_a_abertura_da_pasta_apos_queda(self, sessao, relogio):
        responses.get(pd.URL_SHOW.format(grau='cpopg'), body='ok')
        responses.get(
            pd.URL_ABRE_PASTA_1GRAU,
            body=requests.exceptions.ChunkedEncodingError(
                'Connection broken: IncompleteRead(0 bytes read, '
                '2 more expected)'
            ),
        )
        responses.get(pd.URL_ABRE_PASTA_1GRAU, body=URL_PASTA)

        assert pd.abre_pasta_digital(sessao, CD_PROCESSO) == URL_PASTA

    @responses.activate
    def test_repete_a_leitura_da_arvore_apos_queda(self, sessao, relogio):
        """
        A página da pasta digital é a maior de todas e o corpo dela é
        indispensável: é aqui que a queda mais dói.
        """
        responses.get(
            URL_PASTA,
            body=requests.exceptions.ChunkedEncodingError('caiu'),
        )
        responses.get(URL_PASTA, body=HTML_PASTA)

        arvore = pd.le_arvore_documentos(sessao, URL_PASTA)
        assert pd.coleta_parametros(arvore) == ['p1', 'p2', 'p3']

    @responses.activate
    def test_desiste_depois_das_tentativas(self, sessao, relogio):
        responses.get(
            URL_PASTA,
            body=requests.exceptions.ChunkedEncodingError('caiu'),
        )

        with pytest.raises(exceptions.ESAJIndisponivelError, match='tentativa'):
            pd.le_arvore_documentos(sessao, URL_PASTA)

    @responses.activate
    def test_transferencia_truncada_nao_vira_arquivo(
        self, sessao, tmp_path, relogio
    ):
        """
        A transferência incompleta é reconhecida e o arquivo final não
        é criado — entregá-lo seria dar um ZIP corrompido por bom.

        O parcial também é descartado: o e-SAJ apaga o arquivo
        montado quando a transferência é interrompida, e uma nova
        preparação gera outro arquivo — emendar um no outro daria um
        ZIP corrompido sem aviso.
        """
        responses.get(
            'https://esaj.tjsp.jus.br/arquivo.zip',
            body=b'PK\x03\x04truncado',
            headers={'Content-Length': '999999'},
            auto_calculate_content_length=False,
        )

        destino = tmp_path / 'autos.zip'
        with pytest.raises(exceptions.DownloadError):
            pd.baixa_arquivo(
                sessao,
                'https://esaj.tjsp.jus.br/arquivo.zip',
                destino,
                tentativas=2,
            )

        assert not destino.exists()
        assert list(tmp_path.glob('*.parcial')) == []


class TestEsperaTolerante:
    @responses.activate
    def test_queda_durante_a_espera_nao_derruba_o_pedido(
        self, sessao, relogio
    ):
        """
        A espera dura muitos minutos e a consulta de andamento é
        inofensiva. Uma queda passageira não pode custar o pedido já
        aceito pelo e-SAJ.
        """
        url_final = 'https://esaj.tjsp.jus.br/pastadigital/pronto.zip'
        responses.post(
            pd.URL_BUSCA_PRONTO,
            body=requests.exceptions.ChunkedEncodingError('caiu'),
        )
        responses.post(pd.URL_BUSCA_PRONTO, body='')
        responses.post(pd.URL_BUSCA_PRONTO, body=url_final)

        resultado = pd.aguarda_finalizacao(
            sessao=sessao,
            url_pasta=URL_PASTA,
            localizador='LOC-1',
            cd_processo=CD_PROCESSO,
            intervalo=1,
        )
        assert resultado == url_final

    @responses.activate
    def test_queda_persistente_ainda_respeita_o_prazo(self, sessao, relogio):
        responses.post(
            pd.URL_BUSCA_PRONTO,
            body=requests.exceptions.ChunkedEncodingError('caiu'),
        )

        with pytest.raises(exceptions.PreparacaoTimeoutError):
            pd.aguarda_finalizacao(
                sessao=sessao,
                url_pasta=URL_PASTA,
                localizador='LOC-1',
                cd_processo=CD_PROCESSO,
                espera_maxima=30,
                intervalo=1,
            )


class TestNoticiaDoAndamento:
    """
    Um arquivo de gigabytes leva dezenas de minutos. Sem notícia, não
    há como distinguir progresso de travamento — foi o que obrigou a
    acompanhar o download por fora, olhando o tamanho do arquivo.
    """

    @responses.activate
    def test_relata_o_progresso_do_download(self, sessao, tmp_path, caplog):
        import logging

        conteudo = b'x' * (1024 * 1024)
        responses.get(
            'https://esaj.tjsp.jus.br/a.zip',
            body=conteudo,
            headers={'Content-Length': str(len(conteudo))},
        )

        with caplog.at_level(logging.INFO, logger='esaj_autos.request.pasta_digital'):
            pd.baixa_arquivo(
                sessao,
                'https://esaj.tjsp.jus.br/a.zip',
                tmp_path / 'a.zip',
                # Relata a cada bloco, para o teste não depender de tempo.
                intervalo_relato=0,
                tamanho_bloco=1024 * 256,
            )

        registrado = caplog.text
        assert 'Transferindo' in registrado
        assert 'baixados' in registrado
        assert 'MB/s' in registrado

    def test_progresso_com_e_sem_tamanho_anunciado(self, caplog):
        import logging

        with caplog.at_level(logging.INFO, logger='esaj_autos.request.pasta_digital'):
            pd._relata_progresso(50 * 1048576, 100 * 1048576, 10.0)
            pd._relata_progresso(50 * 1048576, 0, 10.0)

        linhas = caplog.text
        # Com tamanho conhecido, informa a porcentagem e a previsão.
        assert '50%' in linhas
        assert 'faltam' in linhas
        # Sem tamanho, informa o que dá para saber.
        assert '5.0 MB/s' in linhas

    @responses.activate
    def test_espera_relata_o_tempo_e_nao_uma_linha_por_consulta(
        self, sessao, relogio, caplog
    ):
        import logging

        url_final = 'https://esaj.tjsp.jus.br/pd/pronto.zip'
        for _ in range(20):
            responses.post(pd.URL_BUSCA_PRONTO, body='')
        responses.post(pd.URL_BUSCA_PRONTO, body=url_final)

        with caplog.at_level(logging.INFO, logger='esaj_autos.request.pasta_digital'):
            pd.aguarda_finalizacao(
                sessao=sessao,
                url_pasta=URL_PASTA,
                localizador='LOC',
                cd_processo=CD_PROCESSO,
                intervalo=5,
                intervalo_relato=60,
            )

        esperas = [
            l for l in caplog.text.splitlines() if 'Aguardando' in l
        ]
        # 21 consultas de 5s dariam 21 linhas sem a contenção.
        assert len(esperas) < 5
        assert any('min' in l for l in esperas)


class TestRetomadaDaTransferencia:
    """
    Num lote real, nove downloads caíram — um deles com 92% já
    recebidos — e cada queda descartava gigabytes. O que já veio tem
    de ser preservado e a transferência, continuada.
    """

    @responses.activate
    def test_retoma_de_onde_parou_pedindo_so_o_que_falta(
        self, sessao, tmp_path, relogio
    ):
        inteiro = bytes(range(256)) * 40      # 10.240 bytes
        corte = 6000

        # Primeira tentativa: cai no meio.
        responses.get(
            'https://esaj.tjsp.jus.br/a.zip',
            body=requests.exceptions.ChunkedEncodingError('caiu'),
        )
        # Segunda: o e-SAJ entrega o trecho que falta.
        responses.get(
            'https://esaj.tjsp.jus.br/a.zip',
            body=inteiro[corte:],
            status=206,
            headers={
                'Content-Range': f'bytes {corte}-{len(inteiro)-1}/{len(inteiro)}'
            },
            auto_calculate_content_length=False,
        )

        destino = tmp_path / 'a.zip'
        # Simula o que a primeira tentativa deixou em disco.
        parcial = destino.with_suffix('.zip.parcial')
        parcial.write_bytes(inteiro[:corte])

        pd.baixa_arquivo(sessao, 'https://esaj.tjsp.jus.br/a.zip', destino)

        # O arquivo saiu inteiro e correto, sem rebaixar o começo.
        assert destino.read_bytes() == inteiro

        # E o segundo pedido trouxe apenas o que faltava.
        pedidos = [c.request.headers.get('Range') for c in responses.calls]
        assert pedidos[-1] == f'bytes={corte}-'

    @responses.activate
    def test_recomeca_quando_o_servidor_ignora_a_faixa(
        self, sessao, tmp_path, relogio
    ):
        """
        Servidor que responde 200 a um pedido de faixa está mandando o
        arquivo inteiro: emendar sobre o parcial produziria lixo.
        """
        inteiro = b'ABCDEFGHIJ' * 100

        responses.get(
            'https://esaj.tjsp.jus.br/a.zip',
            body=inteiro,
            headers={'Content-Length': str(len(inteiro))},
        )

        destino = tmp_path / 'a.zip'
        destino.with_suffix('.zip.parcial').write_bytes(b'LIXO ANTIGO')

        pd.baixa_arquivo(sessao, 'https://esaj.tjsp.jus.br/a.zip', destino)

        assert destino.read_bytes() == inteiro

    @responses.activate
    def test_fim_silencioso_antes_do_tamanho_e_tratado_como_queda(
        self, sessao, tmp_path, relogio
    ):
        """
        A conexão pode terminar sem erro, com menos bytes do que o
        anunciado. Dar isso por bom entregaria um ZIP corrompido.
        """
        responses.get(
            'https://esaj.tjsp.jus.br/a.zip',
            body=b'so o comeco',
            headers={'Content-Length': '999999'},
            auto_calculate_content_length=False,
        )

        with pytest.raises(exceptions.DownloadError, match='tentativa'):
            pd.baixa_arquivo(
                sessao,
                'https://esaj.tjsp.jus.br/a.zip',
                tmp_path / 'a.zip',
                tentativas=2,
            )

        assert not (tmp_path / 'a.zip').exists()

    @responses.activate
    def test_descarta_o_parcial_que_nao_pode_ser_aproveitado(
        self, sessao, tmp_path, relogio
    ):
        """
        O e-SAJ apaga o arquivo montado quando a transferência é
        interrompida. O pedaço recebido não serve para nada, e deixá-lo
        em disco convidaria a emendá-lo num arquivo diferente, gerado
        por outra preparação — um ZIP corrompido em silêncio.
        """
        responses.get(
            'https://esaj.tjsp.jus.br/a.zip',
            body=requests.exceptions.ChunkedEncodingError('caiu'),
        )

        destino = tmp_path / 'a.zip'
        parcial = destino.with_suffix('.zip.parcial')
        parcial.write_bytes(b'x' * 5000)

        with pytest.raises(
            exceptions.DownloadError, match='não pode ser aproveitado'
        ):
            pd.baixa_arquivo(
                sessao,
                'https://esaj.tjsp.jus.br/a.zip',
                destino,
                tentativas=2,
            )

        assert not parcial.exists()

    @responses.activate
    def test_le_o_total_do_content_range(self, sessao):
        resposta = requests.Response()
        resposta.headers['Content-Range'] = 'bytes 500-999/12345'
        assert pd._tamanho_anunciado(resposta) == 12345

        resposta = requests.Response()
        resposta.headers['Content-Length'] = '777'
        assert pd._tamanho_anunciado(resposta) == 777

        assert pd._tamanho_anunciado(requests.Response()) == 0


class TestEnderecoDeUsoUnico:
    """
    O endereço do arquivo montado serve uma vez só. Pedi-lo de novo
    devolve, com status 200, uma página dizendo "O documento não foi
    encontrado" — que, gravada por cima, transformou um arquivo de
    215 MB em 1.707 bytes de HTML.
    """

    PAGINA_DE_ERRO = (
        '<html><body><div id="spwTabelaMensagem">'
        '<li>O documento não foi encontrado.</li>'
        '</div></body></html>'
    )

    @responses.activate
    def test_pagina_de_erro_nao_corrompe_o_que_ja_veio(
        self, sessao, tmp_path, relogio
    ):
        """
        Se a página de erro fosse gravada por cima, a tentativa
        seguinte emendaria o resto do arquivo em cima do HTML e o ZIP
        sairia corrompido, sem nenhum aviso.
        """
        inteiro = bytes(range(256)) * 20
        corte = 2000

        # Primeira tentativa: o endereço já foi gasto.
        responses.get(
            'https://esaj.tjsp.jus.br/a.zip',
            body=self.PAGINA_DE_ERRO,
            content_type='text/html;charset=UTF-8',
        )
        # Segunda, com endereço novo: entrega o que falta.
        responses.get(
            'https://esaj.tjsp.jus.br/b.zip',
            body=inteiro[corte:],
            status=206,
            headers={
                'Content-Range':
                    f'bytes {corte}-{len(inteiro)-1}/{len(inteiro)}'
            },
            auto_calculate_content_length=False,
        )

        destino = tmp_path / 'a.zip'
        destino.with_suffix('.zip.parcial').write_bytes(inteiro[:corte])

        pd.baixa_arquivo(
            sessao,
            'https://esaj.tjsp.jus.br/a.zip',
            destino,
            renova_url=lambda: 'https://esaj.tjsp.jus.br/b.zip',
        )

        # Byte a byte igual ao original: o HTML não entrou no meio.
        assert destino.read_bytes() == inteiro

    @responses.activate
    def test_resposta_menor_que_o_recebido_nao_e_o_arquivo(
        self, sessao, tmp_path, relogio
    ):
        """
        Mesmo sem se declarar HTML, uma resposta inteira menor que o
        pedaço já obtido não pode ser o arquivo.
        """
        inteiro = b'ABCDEFGHIJ' * 500
        corte = 3000

        responses.get(
            'https://esaj.tjsp.jus.br/a.zip',
            body=b'x' * 40,
            headers={'Content-Length': '40'},
        )
        responses.get(
            'https://esaj.tjsp.jus.br/b.zip',
            body=inteiro[corte:],
            status=206,
            headers={
                'Content-Range':
                    f'bytes {corte}-{len(inteiro)-1}/{len(inteiro)}'
            },
            auto_calculate_content_length=False,
        )

        destino = tmp_path / 'a.zip'
        destino.with_suffix('.zip.parcial').write_bytes(inteiro[:corte])

        pd.baixa_arquivo(
            sessao,
            'https://esaj.tjsp.jus.br/a.zip',
            destino,
            renova_url=lambda: 'https://esaj.tjsp.jus.br/b.zip',
        )

        assert destino.read_bytes() == inteiro

    @responses.activate
    def test_pede_endereco_novo_a_cada_tentativa(
        self, sessao, tmp_path, relogio
    ):
        """
        Como o endereço serve uma vez só, retomar exige pedir outro
        para o mesmo arquivo montado.
        """
        inteiro = bytes(range(256)) * 20
        corte = 2000

        responses.get(
            'https://esaj.tjsp.jus.br/primeiro.zip',
            body=requests.exceptions.ChunkedEncodingError('caiu'),
        )
        responses.get(
            'https://esaj.tjsp.jus.br/segundo.zip',
            body=inteiro[corte:],
            status=206,
            headers={
                'Content-Range':
                    f'bytes {corte}-{len(inteiro)-1}/{len(inteiro)}'
            },
            auto_calculate_content_length=False,
        )

        destino = tmp_path / 'a.zip'
        destino.with_suffix('.zip.parcial').write_bytes(inteiro[:corte])

        pedidos = []

        def renova():
            pedidos.append('renovou')
            return 'https://esaj.tjsp.jus.br/segundo.zip'

        pd.baixa_arquivo(
            sessao,
            'https://esaj.tjsp.jus.br/primeiro.zip',
            destino,
            renova_url=renova,
        )

        assert pedidos == ['renovou']
        assert destino.read_bytes() == inteiro


class TestFalhaDeDisco:
    """
    Um soluço do disco externo derrubou um lote de horas. Falha de
    gravação é do ambiente, não do e-SAJ, e some sozinha: merece outra
    tentativa, não o fim do trabalho.
    """

    @responses.activate
    def test_repete_quando_a_gravacao_falha(
        self, sessao, tmp_path, relogio, monkeypatch
    ):
        conteudo = b'PK\x03\x04' + b'z' * 500

        responses.get('https://esaj.tjsp.jus.br/a.zip', body=conteudo)
        responses.get('https://esaj.tjsp.jus.br/b.zip', body=conteudo)

        falhas = {'restantes': 1}
        escrita_real = None

        class ArquivoQueFalha:
            def __init__(self, alvo):
                self.alvo = alvo

            def __enter__(self):
                self.f = escrita_real(self.alvo, 'wb')
                return self

            def __exit__(self, *a):
                self.f.close()

            def write(self, bloco):
                if falhas['restantes']:
                    falhas['restantes'] -= 1
                    raise OSError(22, 'Invalid argument')
                return self.f.write(bloco)

        import builtins

        escrita_real = builtins.open

        def abre(alvo, modo='r', *a, **k):
            if str(alvo).endswith('.parcial') and 'w' in modo:
                return ArquivoQueFalha(alvo)
            return escrita_real(alvo, modo, *a, **k)

        monkeypatch.setattr(builtins, 'open', abre)

        destino = tmp_path / 'a.zip'
        pd.baixa_arquivo(
            sessao,
            'https://esaj.tjsp.jus.br/a.zip',
            destino,
            renova_url=lambda: 'https://esaj.tjsp.jus.br/b.zip',
        )

        monkeypatch.undo()
        assert destino.read_bytes() == conteudo


# ---------------------------------------------------------------------
# Vencimento do acesso à pasta digital
# ---------------------------------------------------------------------


class TestAcessoAPastaVence:
    """
    O `pastadigital` é um aplicativo à parte, de sessão própria e mais
    curta que a do portal. Num processo de 25 partes o acesso venceu
    na parte 17, e o e-SAJ respondeu 401 com o login ainda válido — o
    que interrompeu o lote e deixou 24 processos sem tentativa.
    """

    def _pedido(self, tmp_path):
        return {
            'numero_cnj': CNJ,
            'cd_processo': '2SZX268AZ0000',
            'cd_documento': 'doc',
            'url_pasta': 'https://esaj.tjsp.jus.br/pastadigital/velha',
            'total_pecas': 2,
            'separar_documentos': True,
            'partes': [],
        }

    def test_reabre_a_pasta_e_conclui_a_parte(self, tmp_path, monkeypatch):
        """
        O 401 no meio do processo não é o login caindo: reabrir a
        pasta digital devolve um acesso válido e a parte termina.
        """
        from esaj_autos import autos

        dados = DownloadAutos(numero_cnj=CNJ, destino=tmp_path)
        pedido = self._pedido(tmp_path)
        alvo = tmp_path / 'parte.zip'

        chamadas = []

        def tenta(sessao, dados, pedido, lote, alvo, ao_progredir=None):
            chamadas.append(pedido['url_pasta'])
            if len(chamadas) == 1:
                raise exceptions.SessaoExpiradaError('HTTP 401')
            alvo.write_bytes(b'zip')
            return alvo

        def renova(sessao, dados, pedido):
            pedido['url_pasta'] = 'https://esaj.tjsp.jus.br/pastadigital/nova'

        monkeypatch.setattr(autos, '_tenta_parte', tenta)
        monkeypatch.setattr(autos, '_renova_acesso_a_pasta', renova)

        gravado = autos._prepara_e_baixa(
            sessao=None, dados=dados, pedido=pedido, lote=[], alvo=alvo
        )

        assert gravado == alvo
        # A segunda tentativa usou o endereço renovado, não o vencido.
        assert chamadas == [
            'https://esaj.tjsp.jus.br/pastadigital/velha',
            'https://esaj.tjsp.jus.br/pastadigital/nova',
        ]

    def test_login_caido_de_verdade_ainda_interrompe(
        self, tmp_path, monkeypatch
    ):
        """
        Se nem com a pasta reaberta o e-SAJ aceita, aí é o login que
        caiu — e o lote precisa parar, porque nada mais vai passar.
        """
        from esaj_autos import autos

        dados = DownloadAutos(numero_cnj=CNJ, destino=tmp_path)
        pedido = self._pedido(tmp_path)

        def tenta(sessao, dados, pedido, lote, alvo, ao_progredir=None):
            raise exceptions.SessaoExpiradaError('HTTP 401')

        monkeypatch.setattr(autos, '_tenta_parte', tenta)
        monkeypatch.setattr(
            autos, '_renova_acesso_a_pasta', lambda *a, **k: None
        )

        with pytest.raises(exceptions.SessaoExpiradaError):
            autos._prepara_e_baixa(
                sessao=None,
                dados=dados,
                pedido=pedido,
                lote=[],
                alvo=tmp_path / 'parte.zip',
            )

    def test_renovacao_visita_o_endereco_novo(self, tmp_path, monkeypatch):
        """
        Obter o endereço não basta: sem visitá-lo, o `pastadigital`
        não reconhece a sessão e o 401 se repete.
        """
        from esaj_autos import autos

        dados = DownloadAutos(numero_cnj=CNJ, destino=tmp_path)
        pedido = self._pedido(tmp_path)
        nova = 'https://esaj.tjsp.jus.br/pastadigital/nova'
        visitadas = []

        monkeypatch.setattr(
            autos.pasta_digital,
            'abre_pasta_digital',
            lambda **k: nova,
        )
        monkeypatch.setattr(
            autos.pasta_digital,
            'entra_na_pasta',
            lambda **k: visitadas.append(k['url_pasta']),
        )

        autos._renova_acesso_a_pasta(None, dados, pedido)

        assert visitadas == [nova]
        assert pedido['url_pasta'] == nova

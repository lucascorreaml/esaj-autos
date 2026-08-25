"""
Testes da interface de linha de comando.

O ponto central é a ordem: os números são validados antes de qualquer
autenticação, para que um número errado não custe um código de
verificação.
"""

import pytest

from esaj_autos import cli


class TestSeparaValidos:
    def test_normaliza_e_formata(self):
        validos, invalidos = cli._separa_validos(['12345678920208260100'])
        assert validos == ['1234567-89.2020.8.26.0100']
        assert invalidos == []

    def test_remove_repetidos_em_formatos_diferentes(self):
        """
        O mesmo processo escrito de dois jeitos é um processo só.
        """
        validos, _ = cli._separa_validos(
            ['12345678920208260100', '1234567-89.2020.8.26.0100']
        )
        assert validos == ['1234567-89.2020.8.26.0100']

    def test_separa_os_invalidos_com_o_motivo(self):
        validos, invalidos = cli._separa_validos(
            ['1234567-89.2020.8.26.0100', 'SEU-NUMERO-CNJ', '123']
        )
        assert validos == ['1234567-89.2020.8.26.0100']
        assert [n for n, _ in invalidos] == ['SEU-NUMERO-CNJ', '123']
        assert 'não contém nenhum dígito' in invalidos[0][1]
        assert '20 dígitos' in invalidos[1][1]

    def test_recusa_processo_de_outro_tribunal(self):
        _, invalidos = cli._separa_validos(['1234567-89.2020.8.19.0001'])
        assert 'não é do TJSP' in invalidos[0][1]


class TestOrdemDaExecucao:
    def test_nao_tenta_logar_quando_nada_e_valido(self, monkeypatch, capsys):
        """
        Gastar o código de verificação para só então descobrir que o
        número está errado desperdiça a autenticação.
        """
        tentou_logar = []
        monkeypatch.setattr(
            cli.login,
            'entrar',
            lambda **kw: tentou_logar.append(kw),
        )
        monkeypatch.setattr(cli.sys.stdin, 'isatty', lambda: False)

        codigo = cli.main(['SEU-NUMERO-CNJ'])

        assert codigo == 1
        assert tentou_logar == []
        assert 'Nenhum processo válido' in capsys.readouterr().out

    def test_lista_os_processos_antes_de_pedir_credenciais(
        self, monkeypatch, capsys
    ):
        ordem = []

        def _entrar(**kw):
            ordem.append('login')
            raise cli.ESAJError('parou aqui de propósito')

        monkeypatch.setattr(cli.login, 'entrar', _entrar)
        monkeypatch.setattr(cli.sys.stdin, 'isatty', lambda: False)

        cli.main(['12345678920208260100'])

        saida = capsys.readouterr().out
        assert '1234567-89.2020.8.26.0100' in saida
        # O número aparece antes de o login ser tentado.
        assert saida.index('1234567-89.2020.8.26.0100') < saida.index(
            'Não foi possível entrar'
        )
        assert ordem == ['login']

    def test_le_numeros_de_arquivo(self, monkeypatch, tmp_path):
        arquivo = tmp_path / 'lista.txt'
        arquivo.write_text(
            '# comentário\n12345678920208260100\n\n', encoding='utf-8'
        )
        monkeypatch.setattr(cli.sys.stdin, 'isatty', lambda: False)

        args = cli._monta_parser().parse_args(['--arquivo', str(arquivo)])
        assert cli._reune_numeros(args) == ['1234567-89.2020.8.26.0100']


class TestParser:
    def test_grau_e_formato(self):
        args = cli._monta_parser().parse_args(
            ['123', '--grau', '2', '--pdf', '--destino', 'saida']
        )
        assert args.grau == '2'
        assert args.pdf is True
        assert str(args.destino) == 'saida'

    def test_grau_invalido_e_recusado(self):
        with pytest.raises(SystemExit):
            cli._monta_parser().parse_args(['123', '--grau', '3'])


class TestEscolhaDeFormato:
    """
    O e-SAJ oferece dois formatos na hora do download; a linha de
    comando reproduz a mesma escolha.
    """

    def _args(self, argv):
        return cli._monta_parser().parse_args(argv)

    def test_zip_explicito(self, monkeypatch):
        monkeypatch.setattr(cli.sys.stdin, 'isatty', lambda: True)
        assert cli._escolhe_formato(self._args(['1', '--zip'])) is True

    def test_pdf_explicito(self, monkeypatch):
        monkeypatch.setattr(cli.sys.stdin, 'isatty', lambda: True)
        assert cli._escolhe_formato(self._args(['1', '--pdf'])) is False

    def test_pergunta_quando_nao_informado(self, monkeypatch, capsys):
        monkeypatch.setattr(cli.sys.stdin, 'isatty', lambda: True)
        monkeypatch.setattr('builtins.input', lambda _: '2')

        assert cli._escolhe_formato(self._args(['1'])) is False
        assert 'Como deseja o arquivo?' in capsys.readouterr().out

    def test_enter_mantem_o_zip(self, monkeypatch):
        monkeypatch.setattr(cli.sys.stdin, 'isatty', lambda: True)
        monkeypatch.setattr('builtins.input', lambda _: '')
        assert cli._escolhe_formato(self._args(['1'])) is True

    def test_sem_terminal_usa_o_padrao(self, monkeypatch):
        monkeypatch.setattr(cli.sys.stdin, 'isatty', lambda: False)
        assert cli._escolhe_formato(self._args(['1'])) is True

    def test_formatos_sao_excludentes(self):
        with pytest.raises(SystemExit):
            self._args(['1', '--zip', '--pdf'])


class TestCaminhosDoMain:
    """
    Cada modo precisa chegar de fato à sua função. Uma edição que
    desligue um deles em silêncio só apareceria em produção, depois
    de gastar um login e um código de verificação.
    """

    @pytest.fixture
    def sessao_falsa(self, monkeypatch):
        monkeypatch.setattr(cli.sys.stdin, 'isatty', lambda: False)
        monkeypatch.setattr(cli.login, 'entrar', lambda **kw: 'SESSAO')

    def test_conferir_chama_a_conferencia_e_nao_baixa(
        self, monkeypatch, sessao_falsa
    ):
        chamadas = []
        monkeypatch.setattr(
            cli, '_confere', lambda s, n, a: chamadas.append(('confere', n)) or 0
        )
        monkeypatch.setattr(
            cli.autos,
            'baixar_lote',
            lambda **kw: pytest.fail('não deveria baixar em --conferir'),
        )

        assert cli.main(['12345678920208260100', '--conferir']) == 0
        assert chamadas[0][0] == 'confere'

    def test_retomar_chama_a_retomada_e_nao_baixa(
        self, monkeypatch, sessao_falsa
    ):
        chamadas = []
        monkeypatch.setattr(
            cli,
            '_retoma',
            lambda s, n, a, sep: chamadas.append(('retoma', sep)) or 0,
        )
        monkeypatch.setattr(
            cli.autos,
            'baixar_lote',
            lambda **kw: pytest.fail('não deveria baixar em --retomar'),
        )

        assert cli.main(['12345678920208260100', '--retomar']) == 0
        assert chamadas[0][0] == 'retoma'

    def test_retomar_nao_pergunta_o_formato(self, monkeypatch, sessao_falsa):
        """
        O formato foi decidido quando o pedido foi feito e está
        gravado no registro: perguntar de novo é pedir uma resposta
        que não será obedecida.
        """
        monkeypatch.setattr(
            cli,
            '_escolhe_formato',
            lambda a: pytest.fail('não deveria perguntar em --retomar'),
        )
        monkeypatch.setattr(cli, '_retoma', lambda s, n, a, sep: 0)

        assert cli.main(['12345678920208260100', '--retomar']) == 0

    def test_download_normal_repassa_o_formato_escolhido(
        self, monkeypatch, sessao_falsa
    ):
        from esaj_autos.modelos import ResultadoLote

        recebido = {}

        def _baixar_lote(**kw):
            recebido.update(kw)
            return ResultadoLote()

        monkeypatch.setattr(cli.autos, 'baixar_lote', _baixar_lote)

        cli.main(['12345678920208260100', '--pdf', '--grau', '2'])

        assert recebido['separar_documentos'] is False
        assert recebido['instancia'] == 'Segundo Grau'
        assert recebido['numeros_cnj'] == ['1234567-89.2020.8.26.0100']

    def test_conferir_nao_pergunta_o_formato(self, monkeypatch, sessao_falsa):
        """
        Conferir não baixa nada; perguntar o formato seria ruído.
        """
        monkeypatch.setattr(
            cli,
            '_escolhe_formato',
            lambda a: pytest.fail('não deveria perguntar em --conferir'),
        )
        monkeypatch.setattr(cli, '_confere', lambda s, n, a: 0)

        assert cli.main(['12345678920208260100', '--conferir']) == 0


class TestSessaoNaLinhaDeComando:
    def test_sair_apaga_e_encerra_sem_baixar(self, monkeypatch, capsys):
        from esaj_autos.request import sessao_salva

        monkeypatch.setattr(sessao_salva, 'esquece', lambda: True)
        monkeypatch.setattr(
            cli.login,
            'entrar',
            lambda **kw: pytest.fail('não deveria logar em --sair'),
        )

        assert cli.main(['--sair']) == 0
        assert 'apagada' in capsys.readouterr().out

    def test_reaproveita_a_sessao_por_padrao(self, monkeypatch):
        from esaj_autos.modelos import ResultadoLote

        recebido = {}
        monkeypatch.setattr(cli.sys.stdin, 'isatty', lambda: False)
        monkeypatch.setattr(
            cli.login,
            'entrar',
            lambda **kw: recebido.update(kw) or 'SESSAO',
        )
        monkeypatch.setattr(
            cli.autos, 'baixar_lote', lambda **kw: ResultadoLote()
        )

        cli.main(['12345678920208260100'])
        assert recebido['reaproveitar'] is True

    def test_novo_login_forca_autenticacao(self, monkeypatch):
        from esaj_autos.modelos import ResultadoLote

        recebido = {}
        monkeypatch.setattr(cli.sys.stdin, 'isatty', lambda: False)
        monkeypatch.setattr(
            cli.login,
            'entrar',
            lambda **kw: recebido.update(kw) or 'SESSAO',
        )
        monkeypatch.setattr(
            cli.autos, 'baixar_lote', lambda **kw: ResultadoLote()
        )

        cli.main(['12345678920208260100', '--novo-login'])
        assert recebido['reaproveitar'] is False


class TestRetomarSemNumeros:
    def test_descobre_os_pendentes(self, monkeypatch, capsys):
        monkeypatch.setattr(cli.sys.stdin, 'isatty', lambda: False)
        monkeypatch.setattr(
            cli.autos, 'pendentes', lambda destino: ['2345678-90.2018.8.26.0100']
        )
        monkeypatch.setattr(cli.login, 'entrar', lambda **kw: 'SESSAO')
        recebido = []
        monkeypatch.setattr(
            cli, '_retoma', lambda s, n, a, sep: recebido.append(n) or 0
        )

        assert cli.main(['--retomar']) == 0
        assert recebido[0] == ['2345678-90.2018.8.26.0100']

    def test_sem_pendentes_encerra_sem_logar(self, monkeypatch, capsys):
        monkeypatch.setattr(cli.sys.stdin, 'isatty', lambda: False)
        monkeypatch.setattr(cli.autos, 'pendentes', lambda destino: [])
        monkeypatch.setattr(
            cli.login,
            'entrar',
            lambda **kw: pytest.fail('não deveria logar sem nada a fazer'),
        )

        assert cli.main(['--retomar']) == 1
        assert 'Nenhum pedido pendente' in capsys.readouterr().out


class TestJanelaNaoRelogaEmVao:
    def test_entrar_com_sessao_ativa_nao_refaz_o_login(self, monkeypatch):
        """
        Entrar de novo com sessão ativa gastaria um pedido de código à
        toa, e o e-SAJ impõe carência entre um e outro.
        """
        import tkinter as tk

        from esaj_autos import gui

        try:
            root = tk.Tk()
        except tk.TclError:
            pytest.skip('sem ambiente gráfico')

        root.withdraw()
        monkeypatch.setattr(
            gui.sessao_salva, 'carrega', lambda **kw: None
        )
        app = gui.Aplicacao(root)
        app.sessao = 'SESSAO-ATIVA'

        avisos = []
        monkeypatch.setattr(
            gui.messagebox, 'showinfo', lambda *a: avisos.append(a)
        )
        monkeypatch.setattr(
            gui.login,
            'Autenticacao',
            lambda *a, **k: pytest.fail('não deveria autenticar de novo'),
        )

        app._entrar()

        assert avisos, 'deveria avisar que já está conectado'
        assert 'já está conectado' in avisos[0][1]
        root.destroy()


class TestListaNaJanela:
    """
    Com dezenas de processos, digitar um a um é convite a erro; a
    lista costuma já existir em algum arquivo.
    """

    def _app(self, monkeypatch):
        import tkinter as tk

        from esaj_autos import gui

        try:
            root = tk.Tk()
        except tk.TclError:
            pytest.skip('sem ambiente gráfico')

        root.withdraw()
        monkeypatch.setattr(gui.sessao_salva, 'carrega', lambda **kw: None)
        return gui, root, gui.Aplicacao(root)

    def test_carrega_numeros_de_arquivo(self, monkeypatch, tmp_path):
        gui, root, app = self._app(monkeypatch)

        lista = tmp_path / 'processos.txt'
        lista.write_text(
            '# execuções fiscais\n'
            '2345678-90.2018.8.26.0100\n'
            '\n'
            '3456789-01.2009.8.26.0100\n',
            encoding='utf-8',
        )
        monkeypatch.setattr(
            gui.filedialog, 'askopenfilename', lambda **kw: str(lista)
        )

        app._carregar_lista()

        assert app._numeros() == [
            '2345678-90.2018.8.26.0100',
            '3456789-01.2009.8.26.0100',
        ]
        root.destroy()

    def test_ignora_comentarios_digitados_a_mao(self, monkeypatch):
        gui, root, app = self._app(monkeypatch)

        app.txt_processos.insert(
            '1.0', '# os de 2018\n2345678-90.2018.8.26.0100\n'
        )
        assert app._numeros() == ['2345678-90.2018.8.26.0100']
        root.destroy()

    def test_limpar_esvazia_a_lista(self, monkeypatch):
        gui, root, app = self._app(monkeypatch)

        app.txt_processos.insert('1.0', '2345678-90.2018.8.26.0100')
        app._limpar_processos()
        assert app._numeros() == []
        root.destroy()

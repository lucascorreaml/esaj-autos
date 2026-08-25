"""
Interface gráfica da cópia integral dos autos.

Faz, em janela, o que a linha de comando faz: entrar no e-SAJ,
informar os processos, escolher onde salvar e acompanhar o andamento.

Feito com Tkinter, que já vem no Python, para que o programa possa
virar um executável sem dependência de instalação.

O trabalho pesado roda em uma linha de execução separada, e a janela
só é tocada pela principal: as mensagens vão para uma fila e são
consumidas pela interface. Mexer em widget de outra linha trava a
janela de maneira difícil de diagnosticar.
"""

import logging
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from esaj_autos import autos
from esaj_autos.exceptions import ESAJError
from esaj_autos.request import login, sessao_salva

TITULO = 'Cópia Integral dos Autos — e-SAJ / TJSP'

# Nível de registro que interessa ao usuário na janela.
LOGGERS = ('esaj_autos.autos', 'esaj_autos.request')


class PonteDeRegistro(logging.Handler):
    """
    Leva as mensagens do pacote para a fila da interface.

    O núcleo já relata tudo por `logging`; assim a janela mostra o
    mesmo andamento da linha de comando, sem duplicar código.
    """

    def __init__(self, fila: queue.Queue) -> None:
        super().__init__()
        self.fila = fila

    def emit(self, record: logging.LogRecord) -> None:
        self.fila.put(('log', record.getMessage()))


class Preferencias:
    """
    Guarda as escolhas do usuário entre execuções.

    Quem salva num HD externo não deve reapontar a pasta toda vez.
    """

    def __init__(self) -> None:
        self.caminho = sessao_salva.caminho_padrao().with_name(
            'preferencias.json'
        )
        self.dados = {}
        self._le()

    def _le(self) -> None:
        import json

        try:
            self.dados = json.loads(
                self.caminho.read_text(encoding='utf-8')
            )
        except (OSError, ValueError):
            self.dados = {}

    def get(self, chave, padrao=None):
        return self.dados.get(chave, padrao)

    def define(self, **valores) -> None:
        import json

        self.dados.update(valores)
        try:
            self.caminho.parent.mkdir(parents=True, exist_ok=True)
            self.caminho.write_text(
                json.dumps(self.dados, ensure_ascii=False, indent=2),
                encoding='utf-8',
            )
        except OSError:
            pass


class Aplicacao:
    """Janela principal."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(TITULO)
        self.root.geometry('820x680')
        self.root.minsize(760, 620)

        self.fila: queue.Queue = queue.Queue()
        self.sessao = None
        self.autenticacao = None
        self.trabalhando = False
        self.prefs = Preferencias()

        self._monta()
        self._liga_registro()
        self.root.after(100, self._consome_fila)

        # Uma sessão da última vez poupa um código de verificação.
        self._em_thread(self._tenta_sessao_guardada, silencioso=True)

    # ------------------------------------------------------------ layout

    def _monta(self) -> None:
        moldura = ttk.Frame(self.root, padding=14)
        moldura.pack(fill='both', expand=True)

        ttk.Label(
            moldura,
            text='Baixar os autos completos de processos do TJSP',
            font=('Segoe UI', 13, 'bold'),
        ).pack(anchor='w')

        self._monta_acesso(moldura)
        self._monta_processos(moldura)
        self._monta_destino(moldura)
        self._monta_acoes(moldura)
        self._monta_andamento(moldura)

    def _monta_acesso(self, pai) -> None:
        caixa = ttk.LabelFrame(pai, text=' Acesso ', padding=10)
        caixa.pack(fill='x', pady=(12, 0))

        linha = ttk.Frame(caixa)
        linha.pack(fill='x')

        ttk.Label(linha, text='CPF/CNPJ:').pack(side='left')
        self.campo_cpf = ttk.Entry(linha, width=18)
        self.campo_cpf.pack(side='left', padx=(6, 12))
        self.campo_cpf.insert(0, self.prefs.get('cpf', ''))

        ttk.Label(linha, text='Senha:').pack(side='left')
        self.campo_senha = ttk.Entry(linha, width=18, show='•')
        self.campo_senha.pack(side='left', padx=(6, 12))
        self.campo_senha.bind('<Return>', lambda _: self._entrar())

        self.btn_entrar = ttk.Button(
            linha, text='Entrar', command=self._entrar
        )
        self.btn_entrar.pack(side='left')

        self.btn_sair = ttk.Button(
            linha, text='Encerrar sessão', command=self._sair
        )
        self.btn_sair.pack(side='left', padx=(6, 0))

        # Só aparece quando o e-SAJ pede o código.
        self.linha_codigo = ttk.Frame(caixa)
        ttk.Label(
            self.linha_codigo, text='Código recebido por e-mail:'
        ).pack(side='left')
        self.campo_codigo = ttk.Entry(self.linha_codigo, width=10)
        self.campo_codigo.pack(side='left', padx=(6, 8))
        self.campo_codigo.bind('<Return>', lambda _: self._confirmar_codigo())
        ttk.Button(
            self.linha_codigo,
            text='Confirmar',
            command=self._confirmar_codigo,
        ).pack(side='left')
        ttk.Button(
            self.linha_codigo,
            text='Reenviar código',
            command=self._reenviar_codigo,
        ).pack(side='left', padx=(6, 0))

        self.lbl_acesso = ttk.Label(
            caixa, text='Sem sessão ativa.', foreground='#666'
        )
        self.lbl_acesso.pack(anchor='w', pady=(8, 0))

    def _monta_processos(self, pai) -> None:
        caixa = ttk.LabelFrame(pai, text=' Processos ', padding=10)
        caixa.pack(fill='both', expand=True, pady=(10, 0))

        topo = ttk.Frame(caixa)
        topo.pack(fill='x')

        ttk.Label(
            topo,
            text='Um número por linha (com ou sem pontuação):',
            foreground='#444',
        ).pack(side='left')

        ttk.Button(
            topo, text='Limpar', command=self._limpar_processos
        ).pack(side='right')
        ttk.Button(
            topo, text='Carregar lista...', command=self._carregar_lista
        ).pack(side='right', padx=(0, 6))

        quadro = ttk.Frame(caixa)
        quadro.pack(fill='both', expand=True, pady=(4, 8))

        self.txt_processos = tk.Text(quadro, height=5, wrap='none')
        rolagem = ttk.Scrollbar(
            quadro, orient='vertical', command=self.txt_processos.yview
        )
        self.txt_processos.configure(yscrollcommand=rolagem.set)
        self.txt_processos.pack(side='left', fill='both', expand=True)
        rolagem.pack(side='right', fill='y')

        opcoes = ttk.Frame(caixa)
        opcoes.pack(fill='x')

        ttk.Label(opcoes, text='Grau:').pack(side='left')
        self.grau = tk.StringVar(value=self.prefs.get('grau', '1'))
        ttk.Radiobutton(
            opcoes, text='1º', variable=self.grau, value='1'
        ).pack(side='left', padx=(4, 0))
        ttk.Radiobutton(
            opcoes, text='2º', variable=self.grau, value='2'
        ).pack(side='left', padx=(4, 16))

        ttk.Label(opcoes, text='Formato:').pack(side='left')
        self.formato = tk.StringVar(value=self.prefs.get('formato', 'zip'))
        ttk.Radiobutton(
            opcoes,
            text='ZIP (uma peça por PDF)',
            variable=self.formato,
            value='zip',
        ).pack(side='left', padx=(4, 0))
        ttk.Radiobutton(
            opcoes,
            text='PDF único',
            variable=self.formato,
            value='pdf',
        ).pack(side='left', padx=(4, 0))

    def _monta_destino(self, pai) -> None:
        caixa = ttk.LabelFrame(pai, text=' Onde salvar ', padding=10)
        caixa.pack(fill='x', pady=(10, 0))

        linha = ttk.Frame(caixa)
        linha.pack(fill='x')

        self.destino = tk.StringVar(
            value=self.prefs.get('destino', str(Path.cwd() / 'autos'))
        )
        ttk.Entry(linha, textvariable=self.destino).pack(
            side='left', fill='x', expand=True
        )
        ttk.Button(
            linha, text='Escolher...', command=self._escolher_destino
        ).pack(side='left', padx=(8, 0))
        ttk.Button(
            linha, text='Abrir pasta', command=self._abrir_destino
        ).pack(side='left', padx=(6, 0))

        ttk.Label(
            caixa,
            text=(
                'Pode ser um HD externo. Cada processo ganha uma '
                'subpasta com o próprio número.'
            ),
            foreground='#666',
            wraplength=740,
            justify='left',
        ).pack(anchor='w', pady=(6, 0))

    def _monta_acoes(self, pai) -> None:
        linha = ttk.Frame(pai)
        linha.pack(fill='x', pady=(12, 0))

        self.btn_conferir = ttk.Button(
            linha, text='Conferir tamanho', command=self._conferir
        )
        self.btn_conferir.pack(side='left')

        self.btn_baixar = ttk.Button(
            linha, text='Baixar autos', command=self._baixar
        )
        self.btn_baixar.pack(side='left', padx=(8, 0))

        self.btn_retomar = ttk.Button(
            linha,
            text='Retomar pendentes',
            command=self._retomar,
        )
        self.btn_retomar.pack(side='left', padx=(8, 0))

    def _monta_andamento(self, pai) -> None:
        caixa = ttk.LabelFrame(pai, text=' Andamento ', padding=10)
        caixa.pack(fill='both', expand=True, pady=(12, 0))

        self.barra = ttk.Progressbar(caixa, mode='determinate')
        self.barra.pack(fill='x')

        self.lbl_status = ttk.Label(caixa, text='Pronto.', foreground='#333')
        self.lbl_status.pack(anchor='w', pady=(6, 6))

        quadro = ttk.Frame(caixa)
        quadro.pack(fill='both', expand=True)

        self.txt_log = tk.Text(
            quadro, height=10, wrap='word', state='disabled',
            background='#1e1e1e', foreground='#d4d4d4',
            insertbackground='#d4d4d4',
        )
        rolagem = ttk.Scrollbar(
            quadro, orient='vertical', command=self.txt_log.yview
        )
        self.txt_log.configure(yscrollcommand=rolagem.set)
        self.txt_log.pack(side='left', fill='both', expand=True)
        rolagem.pack(side='right', fill='y')

    # ------------------------------------------------------- comunicação

    def _liga_registro(self) -> None:
        ponte = PonteDeRegistro(self.fila)
        ponte.setLevel(logging.INFO)
        for nome in LOGGERS:
            registrador = logging.getLogger(nome)
            registrador.setLevel(logging.INFO)
            registrador.addHandler(ponte)

    def _consome_fila(self) -> None:
        """Traz para a janela o que a linha de trabalho produziu."""
        try:
            while True:
                tipo, dado = self.fila.get_nowait()

                if tipo == 'log':
                    self._escreve(dado)
                elif tipo == 'status':
                    self.lbl_status.config(text=dado)
                elif tipo == 'progresso':
                    baixado, total = dado
                    if total:
                        # A animação iniciada em modo indeterminado
                        # continua incrementando o valor por conta
                        # própria; sem pará-la, a barra oscila em vez
                        # de acompanhar o download.
                        self.barra.stop()
                        self.barra.config(mode='determinate', maximum=total)
                        self.barra['value'] = baixado
                elif tipo == 'acesso':
                    self.lbl_acesso.config(text=dado[0], foreground=dado[1])
                elif tipo == 'pede_codigo':
                    self.linha_codigo.pack(fill='x', pady=(8, 0))
                    self.campo_codigo.focus_set()
                elif tipo == 'codigo_ok':
                    self.linha_codigo.pack_forget()
                elif tipo == 'fim':
                    self._destrava(dado)
                elif tipo == 'erro':
                    messagebox.showerror(TITULO, dado)

        except queue.Empty:
            pass

        self.root.after(100, self._consome_fila)

    def _escreve(self, texto: str) -> None:
        self.txt_log.config(state='normal')
        self.txt_log.insert('end', texto + '\n')
        self.txt_log.see('end')
        self.txt_log.config(state='disabled')

    # ------------------------------------------------------------ acesso

    def _em_thread(self, alvo, *args, silencioso=False, **kwargs) -> None:
        """Roda algo fora da linha da janela, travando os botões."""
        if self.trabalhando:
            messagebox.showinfo(TITULO, 'Já há uma tarefa em andamento.')
            return

        self.trabalhando = True
        if not silencioso:
            self._trava()

        def envelope():
            try:
                alvo(*args, **kwargs)
            except ESAJError as e:
                self.fila.put(('log', f'ERRO: {e}'))
                self.fila.put(('erro', str(e)))
            except Exception as e:  # noqa: BLE001
                self.fila.put(('log', f'ERRO inesperado: {e}'))
                self.fila.put(('erro', f'{type(e).__name__}: {e}'))
            finally:
                self.fila.put(('fim', None))

        threading.Thread(target=envelope, daemon=True).start()

    def _trava(self) -> None:
        for botao in (
            self.btn_conferir,
            self.btn_baixar,
            self.btn_retomar,
            self.btn_entrar,
        ):
            botao.state(['disabled'])
        self.barra.config(mode='indeterminate')
        self.barra.start(12)

    def _destrava(self, _=None) -> None:
        self.trabalhando = False
        for botao in (
            self.btn_conferir,
            self.btn_baixar,
            self.btn_retomar,
            self.btn_entrar,
        ):
            botao.state(['!disabled'])
        self.barra.stop()
        self.barra.config(mode='determinate')
        self.barra['value'] = 0
        self.fila.put(('status', 'Pronto.'))

    def _tenta_sessao_guardada(self) -> None:
        sessao = sessao_salva.carrega()
        if sessao is not None:
            self.sessao = sessao
            self.fila.put(
                ('acesso', ('Sessão ativa — não é preciso novo código.',
                            '#0a7d28'))
            )
        else:
            self.fila.put(
                ('acesso', ('Sem sessão ativa. Informe CPF e senha.', '#666'))
            )

    def _entrar(self) -> None:
        # Entrar de novo com sessão ativa gastaria um pedido de código
        # à toa — e o e-SAJ impõe uma carência entre um e outro.
        if self.sessao is not None:
            messagebox.showinfo(
                TITULO,
                'Você já está conectado ao e-SAJ. Para entrar com '
                'outra conta, use "Encerrar sessão" antes.',
            )
            return

        cpf = self.campo_cpf.get().strip()
        senha = self.campo_senha.get()

        if not cpf or not senha:
            messagebox.showwarning(TITULO, 'Informe CPF/CNPJ e senha.')
            return

        self.prefs.define(cpf=cpf)

        def trabalho():
            self.fila.put(('status', 'Entrando no e-SAJ...'))
            self.autenticacao = login.Autenticacao()
            self.autenticacao.primeira_etapa(cpf=cpf, senha=senha)

            if self.autenticacao.flags.get('DuploFatorHabilitado'):
                destino = self.autenticacao.flags.get('DeEmail') or 'seu e-mail'
                self.fila.put(
                    ('acesso', (f'Código enviado para {destino}.', '#b06000'))
                )
                self.fila.put(('pede_codigo', None))
            else:
                self.sessao = self.autenticacao.sessao
                sessao_salva.salva(self.sessao)
                self.fila.put(('acesso', ('Sessão ativa.', '#0a7d28')))

        self._em_thread(trabalho)

    def _confirmar_codigo(self) -> None:
        codigo = self.campo_codigo.get().strip()
        if not codigo:
            messagebox.showwarning(TITULO, 'Informe o código recebido.')
            return
        if self.autenticacao is None:
            messagebox.showwarning(TITULO, 'Faça o login primeiro.')
            return

        def trabalho():
            self.fila.put(('status', 'Confirmando o código...'))
            self.sessao = self.autenticacao.segunda_etapa(token=codigo)
            sessao_salva.salva(self.sessao)
            self.fila.put(('codigo_ok', None))
            self.fila.put(('acesso', ('Sessão ativa.', '#0a7d28')))

        self._em_thread(trabalho)

    def _reenviar_codigo(self) -> None:
        if self.autenticacao is None:
            return
        self._em_thread(self.autenticacao.reenviar_codigo)

    def _sair(self) -> None:
        self.sessao = None
        self.autenticacao = None
        sessao_salva.esquece()
        self.linha_codigo.pack_forget()
        self.lbl_acesso.config(text='Sessão encerrada.', foreground='#666')
        self._escreve('Sessão encerrada.')

    # ------------------------------------------------------------ ações

    def _numeros(self) -> list:
        brutos = self.txt_processos.get('1.0', 'end').splitlines()
        return [
            linha.strip()
            for linha in brutos
            if linha.strip() and not linha.strip().startswith('#')
        ]

    def _carregar_lista(self) -> None:
        """
        Traz os números de um arquivo de texto.

        Com dezenas de processos, digitar ou colar um a um é convite a
        erro; a lista costuma já existir em algum arquivo.
        """
        caminho = filedialog.askopenfilename(
            title='Arquivo com os números dos processos',
            filetypes=[
                ('Arquivos de texto', '*.txt'),
                ('Todos os arquivos', '*.*'),
            ],
        )
        if not caminho:
            return

        try:
            numeros = autos.le_numeros(caminho)
        except OSError as e:
            messagebox.showerror(TITULO, f'Não foi possível ler: {e}')
            return

        if not numeros:
            messagebox.showwarning(
                TITULO, 'O arquivo não tem nenhum número.'
            )
            return

        self.txt_processos.delete('1.0', 'end')
        self.txt_processos.insert('1.0', '\n'.join(numeros))
        self._escreve(
            f'{len(numeros)} processo(s) carregado(s) de '
            f'{Path(caminho).name}.'
        )

    def _limpar_processos(self) -> None:
        self.txt_processos.delete('1.0', 'end')

    def _exige_sessao(self) -> bool:
        if self.sessao is None:
            messagebox.showwarning(
                TITULO, 'Entre no e-SAJ antes de baixar.'
            )
            return False
        return True

    def _instancia(self) -> str:
        return 'Segundo Grau' if self.grau.get() == '2' else 'Primeiro Grau'

    def _guarda_escolhas(self) -> None:
        self.prefs.define(
            destino=self.destino.get(),
            grau=self.grau.get(),
            formato=self.formato.get(),
        )

    def _escolher_destino(self) -> None:
        pasta = filedialog.askdirectory(
            title='Onde salvar os autos', initialdir=self.destino.get()
        )
        if pasta:
            self.destino.set(pasta)
            self._guarda_escolhas()

    def _abrir_destino(self) -> None:
        caminho = Path(self.destino.get())
        caminho.mkdir(parents=True, exist_ok=True)
        if sys.platform == 'win32':
            os.startfile(caminho)
        elif sys.platform == 'darwin':
            subprocess.run(['open', str(caminho)], check=False)
        else:
            subprocess.run(['xdg-open', str(caminho)], check=False)

    def _conferir(self) -> None:
        if not self._exige_sessao():
            return
        numeros = self._numeros()
        if not numeros:
            messagebox.showwarning(TITULO, 'Informe ao menos um processo.')
            return

        from esaj_autos.request import pasta_digital as pd

        def trabalho():
            for numero in numeros:
                self.fila.put(('status', f'Conferindo {numero}...'))
                try:
                    cd = pd.resolve_cd_processo(
                        sessao=self.sessao,
                        numero_cnj=numero,
                        grau=self._instancia(),
                    )
                    url = pd.abre_pasta_digital(
                        sessao=self.sessao,
                        cd_processo=cd,
                        grau=self._instancia(),
                    )
                    arvore = pd.le_arvore_documentos(
                        sessao=self.sessao, url_pasta=url
                    )
                    pecas = pd.coleta_parametros(arvore)
                    self.fila.put(
                        ('log', f'{numero}: {len(pecas)} peça(s) na pasta '
                                f'digital.')
                    )
                except (ESAJError, ValueError) as e:
                    nome, explicacao = autos.explica_falha(e)
                    self.fila.put(('log', f'{numero}: {nome} — {explicacao}'))

        self._em_thread(trabalho)

    def _baixar(self) -> None:
        if not self._exige_sessao():
            return
        numeros = self._numeros()
        if not numeros:
            messagebox.showwarning(TITULO, 'Informe ao menos um processo.')
            return

        self._guarda_escolhas()
        destino = self.destino.get()
        instancia = self._instancia()
        separar = self.formato.get() == 'zip'

        def ao_progredir(baixado, total):
            self.fila.put(('progresso', (baixado, total)))

        def trabalho():
            self.fila.put(
                ('status', f'Baixando {len(numeros)} processo(s)...')
            )
            resultado = autos.baixar_lote(
                sessao=self.sessao,
                numeros_cnj=numeros,
                destino=destino,
                instancia=instancia,
                separar_documentos=separar,
                espera_maxima=3600,
                ao_progredir=ao_progredir,
            )
            self.fila.put(('log', ''))
            self.fila.put(('log', str(resultado)))
            for item in resultado.sucessos:
                for parte in item.partes or [item.arquivo]:
                    self.fila.put(('log', f'  {parte}'))

        self._em_thread(trabalho)

    def _retomar(self) -> None:
        if not self._exige_sessao():
            return

        destino = self.destino.get()
        pendentes = autos.pendentes(destino)

        if not pendentes:
            messagebox.showinfo(
                TITULO, f'Nenhum pedido pendente em "{destino}".'
            )
            return

        instancia = self._instancia()
        separar = self.formato.get() == 'zip'

        def ao_progredir(baixado, total):
            self.fila.put(('progresso', (baixado, total)))

        def trabalho():
            self.fila.put(
                ('status', f'Recolhendo {len(pendentes)} pedido(s)...')
            )
            for numero in pendentes:
                try:
                    resultado = autos.retomar(
                        sessao=self.sessao,
                        numero_cnj=numero,
                        destino=destino,
                        instancia=instancia,
                        separar_documentos=separar,
                        espera_maxima=3600,
                        ao_progredir=ao_progredir,
                    )
                    self.fila.put(('log', f'  {resultado.arquivo}'))
                except (ESAJError, ValueError) as e:
                    nome, explicacao = autos.explica_falha(e)
                    self.fila.put(('log', f'{numero}: {nome} — {explicacao}'))

        self._em_thread(trabalho)


def main() -> int:
    root = tk.Tk()
    try:
        ttk.Style().theme_use('vista')
    except tk.TclError:
        pass
    Aplicacao(root)
    root.mainloop()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

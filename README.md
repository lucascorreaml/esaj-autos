# esaj-autos

Baixa a **cópia integral dos autos** de processos do
[e-SAJ](https://esaj.tjsp.jus.br/) (TJSP) a partir do número CNJ, com o
seu próprio login de advogado.

Um processo com dezenas de milhares de peças sai em minutos, em vez de
uma tarde clicando peça por peça. Os arquivos são gravados exatamente
como o tribunal entrega, sem conversão.

---

## O que ele faz

- Recebe um número CNJ (ou uma lista) e baixa a pasta digital inteira.
- Guarda cada processo na sua própria pasta, com nome previsível.
- Divide processos grandes em partes, porque o e-SAJ descarta o arquivo
  montado quando a transferência cai — e assim a queda custa uma parte,
  não o processo todo.
- Reaproveita a sessão entre execuções, para não pedir um código de dois
  fatores a cada vez.
- Em lote, um processo que falha não derruba os demais.

## O que ele **não** faz

Não contorna nada. O programa usa o **seu** login, do mesmo jeito que o
navegador usaria: exige CPF/CNPJ, senha e o código de dois fatores que
chega no seu celular. Não burla CAPTCHA, não contorna a autenticação em
duas etapas, não acessa processo sob segredo de justiça a que você não
tenha acesso, e respeita o limite diário de aberturas da pasta digital
imposto pelo tribunal.

Se você não pode ver o processo no e-SAJ, não vai poder baixá-lo aqui.

---

## Instalação

### Windows, sem instalar nada

Baixe o `CopiaIntegralAutos.exe` na
[página de versões](https://github.com/lucascorreaml/esaj-autos/releases)
e abra com duplo clique. Não precisa de Python nem de terminal.

O Windows costuma avisar que o programa é de origem desconhecida, por
não ser assinado digitalmente: em **"Mais informações" → "Executar
assim mesmo"**.

### Com Python

Precisa de [Python 3.10 ou mais novo](https://www.python.org/downloads/).
Na instalação, marque **"Add Python to PATH"**.

```bash
pip install git+https://github.com/lucascorreaml/esaj-autos.git
```

Para desenvolver, com os testes:

```bash
git clone https://github.com/lucascorreaml/esaj-autos.git
cd esaj-autos
pip install -e ".[dev]"
```

---

## Uso

### Linha de comando

Um processo:

```bash
esaj-autos 1234567-89.2020.8.26.0100
```

Vários de uma vez:

```bash
esaj-autos 1234567-89.2020.8.26.0100 2345678-90.2021.8.26.0100
```

Uma lista em arquivo, um número por linha (linhas com `#` são ignoradas):

```bash
esaj-autos --arquivo processos.txt --destino D:/autos
```

No primeiro uso ele pede CPF/CNPJ, senha e o código de dois fatores. Nas
execuções seguintes reaproveita a sessão, enquanto o tribunal a mantiver
válida.

### Interface gráfica

```bash
esaj-autos-gui
```

Uma janela para escolher a pasta de destino, colar a lista de processos
e acompanhar o andamento. Feita para quem não usa terminal.

### Como biblioteca

```python
from esaj_autos import autos
from esaj_autos.request import login

sessao = login.entrar(cpf='SEU-CPF', senha='...', token='123456')

resultado = autos.baixar(
    sessao=sessao,
    numero_cnj='1234567-89.2020.8.26.0100',
    destino='autos',
)
print(resultado.arquivo, resultado.total_pecas)
```

Em lote:

```python
resultado = autos.baixar_lote(
    sessao=sessao,
    numeros_cnj=['1234567-89.2020.8.26.0100', '2345678-90.2021.8.26.0100'],
    destino='autos',
    intervalo_entre_processos=90,
)
```

---

## Opções úteis

| Opção | Para que serve |
|---|---|
| `--destino PASTA` | onde gravar (padrão: `autos`) |
| `--arquivo LISTA.txt` | lê os números de um arquivo |
| `--grau 2` | segundo grau |
| `--zip` / `--pdf` | uma peça por PDF dentro de um ZIP, ou um PDF único |
| `--pecas-por-pedido N` | tamanho de cada parte; menor = mais robusto |
| `--retomar` | recolhe pedidos que ficaram pela metade |
| `--conferir` | lista o que falta, sem baixar |
| `--novo-login` | descarta a sessão guardada e entra de novo |
| `--sair` | apaga a sessão guardada |

---

## Processos grandes

O e-SAJ monta o arquivo no servidor e só então entrega. Se a
transferência cair no meio, **o arquivo montado é descartado** e o
endereço morre junto: não existe continuar de onde parou. A única
defesa é pedir pedaços menores.

O padrão (`--pecas-por-pedido`) costuma dar partes de ~120 MB. Se as
peças do processo forem digitalizações pesadas, as partes saem maiores e
podem não caber na janela em que a conexão se sustenta. Nesse caso,
reduza:

```bash
esaj-autos 1234567-89.2020.8.26.0100 --pecas-por-pedido 75
```

As partes já gravadas são reconhecidas e não são baixadas de novo.

---

## Erros que ele trata sozinho

- **Sessão da pasta digital vencida (HTTP 401)** — reabre a pasta e
  continua. Só interrompe se o login em si tiver caído.
- **Instabilidade do tribunal (HTTP 500)** — espera 1, 5 e 15 minutos
  antes de desistir daquele processo, e segue para os próximos.
- **Queda de transferência** — refaz o pedido daquela parte.
- **Processo inexistente ou sem acesso** — registra e continua o lote.
- **Limite diário de acessos atingido** — para de insistir, porque
  insistir não resolve.

---

## Estado do projeto

Foi usado de verdade: **34 processos, mais de 50 GB e mais de 220 mil
peças** baixados do e-SAJ real.

Comprovado em uso real:

- primeiro grau, formato ZIP;
- download em lote com sessão reaproveitada;
- retomada de processos interrompidos;
- divisão em partes e refazimento de parte que cai.

Ainda **não** exercitado contra o e-SAJ real:

- segundo grau (`--grau 2`);
- PDF único (`--pdf`);
- um download inteiro conduzido pela interface gráfica.

Esses caminhos existem e têm teste automatizado, mas nunca foram
validados contra o tribunal. Trate-os como não comprovados.

---

## Testes

```bash
pytest tests/ -q
```

188 testes, todos com as respostas do e-SAJ simuladas: a suíte não toca
o tribunal e não precisa de credencial nem de rede.

Em `tests/manual/` há scripts que conversam com o e-SAJ de verdade. Eles
exigem login e não rodam junto com a suíte.

---

## Licença

MIT — veja [LICENSE](LICENSE).

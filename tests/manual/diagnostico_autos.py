"""
Diagnóstico do fluxo real de cópia integral dos autos.

Executa o download contra o e-SAJ de verdade, etapa por etapa,
relatando o que cada uma devolveu. Serve para confirmar que o fluxo
implementado corresponde ao funcionamento atual do e-SAJ e para
revelar mudanças de formato quando algo quebrar.

Não roda no conjunto automatizado de testes: exige credenciais,
token em duas etapas e um processo a que o usuário tenha acesso.

Como usar
---------
    python tests/manual/diagnostico_autos.py

O script pergunta CPF e senha na hora (a senha não é ecoada). Para
não digitar toda vez, defina no ambiente ou em um `.env`:
    USERNAME_TJSP=...
    PASSWORD_TJSP=...

Privacidade
-----------
O relatório mostra a **estrutura** da pasta digital (nomes de campos,
tipos e quantidades), não o conteúdo das peças. Os textos são
mascarados, de modo que a saída possa ser compartilhada para
diagnóstico sem expor dados do processo.
"""

import getpass
import logging
import os
import sys
from pathlib import Path

# Permite rodar o arquivo diretamente, sem instalar o pacote.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dotenv import load_dotenv  # noqa: E402

from esaj_autos import exceptions  # noqa: E402
from esaj_autos.request import login as login_mod  # noqa: E402
from esaj_autos.request import pasta_digital as pd  # noqa: E402
from esaj_autos.request import session as sessao_mod  # noqa: E402

LARGURA = 72


def titulo(texto: str) -> None:
    print(f'\n{"=" * LARGURA}\n{texto}\n{"=" * LARGURA}')


def mascara(valor, limite: int = 24) -> str:
    """
    Mostra o formato de um valor sem revelar o conteúdo.

    Preserva tamanho e tipo, que é o que interessa ao diagnóstico.
    """
    if valor is None:
        return 'None'
    if isinstance(valor, bool):
        return str(valor)
    if isinstance(valor, (int, float)):
        return f'{type(valor).__name__}({valor})'

    texto = str(valor)
    if len(texto) <= 6:
        return f'"{texto}"'
    return f'"{texto[:3]}…{texto[-3:]}" (len={len(texto)}){"" if len(texto) <= limite else ""}'


def descreve_no(no, nivel: int = 0, maximo: int = 2) -> None:
    """
    Imprime a estrutura de um nó da árvore, sem o conteúdo.
    """
    recuo = '  ' * (nivel + 1)
    if not isinstance(no, dict):
        print(f'{recuo}{type(no).__name__}')
        return

    for chave, valor in no.items():
        if chave == 'children':
            print(f'{recuo}children: {len(valor or [])} filho(s)')
            if nivel < maximo and valor:
                descreve_no(valor[0], nivel + 1, maximo)
        elif isinstance(valor, dict):
            print(f'{recuo}{chave}:')
            descreve_no(valor, nivel + 1, maximo)
        elif isinstance(valor, list):
            print(f'{recuo}{chave}: lista({len(valor)})')
        else:
            print(f'{recuo}{chave}: {mascara(valor)}')


def main() -> int:
    logging.basicConfig(level=logging.INFO, format='  [log] %(message)s')

    load_dotenv()
    usuario = os.getenv('USERNAME_TJSP')
    senha = os.getenv('PASSWORD_TJSP')

    # Sem credenciais no ambiente, pergunta na hora. A senha não é
    # ecoada nem guardada em lugar nenhum.
    if not usuario:
        usuario = input('CPF/CNPJ do e-SAJ: ').strip()
    if not senha:
        senha = getpass.getpass('Senha do e-SAJ (não aparece): ')

    if not usuario or not senha:
        print('Sem credenciais, não há como seguir.')
        return 1

    numero = input('Número CNJ do processo (com acesso liberado): ').strip()
    grau = (
        input('Grau [1=Primeiro, 2=Segundo] (padrão 1): ').strip() or '1'
    )
    instancia = 'Segundo Grau' if grau == '2' else 'Primeiro Grau'

    titulo('0. Login (HTTP, sem navegador)')

    try:
        auth = login_mod.Autenticacao()
        auth.primeira_etapa(cpf=usuario, senha=senha)
        print('  credenciais aceitas; o e-SAJ enviou o código por e-mail')

        if sessao_mod.esta_logado(auth.sessao):
            sessao = auth.sessao
            print('  sessão autenticada sem segunda etapa')
        else:
            token = input('  Código recebido por e-mail: ').strip()
            sessao = auth.segunda_etapa(token=token)

        print(f'  sessão autenticada: {sessao_mod.esta_logado(sessao)}')

        titulo('1. Número CNJ -> cdProcesso')
        cd_processo = pd.resolve_cd_processo(
            sessao=sessao, numero_cnj=numero, grau=instancia
        )
        print(f'  cdProcesso: {mascara(cd_processo)}')

        titulo('2. Abertura da pasta digital')
        url_pasta = pd.abre_pasta_digital(
            sessao=sessao, cd_processo=cd_processo, grau=instancia
        )
        print('  URL da pasta obtida: sim')
        print(f'  host: {url_pasta.split("/")[2]}')
        print(f'  caminho: /{"/".join(url_pasta.split("/")[3:]).split("?")[0]}')
        print(f'  tem ticket/query: {"?" in url_pasta}')

        titulo('3. Árvore de documentos (requestScope)')
        resposta = sessao.get(url_pasta, timeout=120)
        html = resposta.text
        print(f'  HTTP {resposta.status_code}, {len(html)} caracteres')
        print(f'  contém "requestScope": {"requestScope" in html}')

        arvore = pd._extrai_request_scope(html)
        print(f'  tipo da raiz: {type(arvore).__name__}')
        print('  --- estrutura (conteúdo mascarado) ---')
        descreve_no(arvore)

        titulo('4. Peças coletadas')
        pecas = pd.coleta_parametros(arvore)
        cd_documento = pd.extrai_cd_documento(arvore)
        print(f'  total de peças: {len(pecas)}')
        print(f'  cdDocumento: {mascara(cd_documento)}')
        if pecas:
            print(f'  formato do identificador: {mascara(pecas[0], 60)}')
            print(f'  todos distintos: {len(pecas) == len(set(pecas))}')

        titulo('5. Pedido de preparação')
        localizador = pd.solicita_preparacao(
            sessao=sessao,
            url_pasta=url_pasta,
            cd_processo=cd_processo,
            parametros=pecas,
            cd_documento=cd_documento,
            separar_documentos=True,
        )
        print(f'  localizador recebido: {mascara(localizador, 60)}')

        titulo('6. Espera pela montagem')
        url_arquivo = pd.aguarda_finalizacao(
            sessao=sessao,
            url_pasta=url_pasta,
            localizador=localizador,
            cd_processo=cd_processo,
            cd_documento=cd_documento,
            espera_maxima=1800,
            intervalo=5,
        )
        print('  URL do arquivo recebida: sim')
        print(f'  host: {url_arquivo.split("/")[2]}')

        titulo('7. Download')
        destino = Path('autos') / numero / f'{numero}.zip'
        gravado = pd.baixa_arquivo(
            sessao=sessao, url_arquivo=url_arquivo, destino=destino
        )
        tamanho = gravado.stat().st_size
        print(f'  arquivo: {gravado}')
        print(f'  tamanho: {tamanho:,} bytes')

        titulo('8. Conferência do arquivo')
        import zipfile

        if zipfile.is_zipfile(gravado):
            with zipfile.ZipFile(gravado) as z:
                nomes = z.namelist()
            print('  ZIP válido: sim')
            print(f'  arquivos dentro: {len(nomes)}')
            print(f'  peças pedidas x recebidas: {len(pecas)} x {len(nomes)}')
            padrao = sum(1 for n in nomes if '(pag' in n.lower())
            print(
                f'  no padrão "Descrição (pag N - M).pdf": '
                f'{padrao}/{len(nomes)}'
            )
        else:
            cabecalho = gravado.read_bytes()[:5]
            print(f'  não é ZIP; começa com: {cabecalho!r}')
            print(f'  parece PDF: {cabecalho.startswith(b"%PDF")}')

        titulo('RESULTADO: fluxo completo executado com sucesso')
        return 0

    except exceptions.ESAJError as e:
        titulo(f'INTERROMPIDO: {type(e).__name__}')
        print(f'  {e}')
        return 1

    except KeyboardInterrupt:
        titulo('INTERROMPIDO pelo usuário')
        return 130



if __name__ == '__main__':
    raise SystemExit(main())

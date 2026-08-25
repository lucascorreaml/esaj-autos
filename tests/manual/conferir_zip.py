"""
Conferência do arquivo entregue pelo e-SAJ.

Verifica se o que chegou ao disco é o que se esperava: se o ZIP está
íntegro, quantas peças traz, se batem com o que foi pedido e se os
nomes seguem o padrão do e-SAJ — o mesmo de que dependem as
ferramentas que ordenam e unem as peças.

Como usar
---------
    python tests/manual/conferir_zip.py autos/<CNJ>/<CNJ>.zip

Sem argumento, confere todos os arquivos encontrados em `autos/`.

Privacidade
-----------
Descrições de peças não são impressas: o relatório traz contagens,
padrões e nomes mascarados, de modo a poder ser compartilhado sem
expor dados do processo.
"""

import re
import sys
import zipfile
from collections import Counter
from pathlib import Path

# Nomenclatura do e-SAJ: "Descrição (pag N)" ou "Descrição (pag N - M)".
PADRAO_ESAJ = re.compile(
    r'^(?P<desc>.*?)\s*\(\s*pag\.?\s*(?P<ini>\d+)\s*(?:[-–]\s*(?P<fim>\d+)\s*)?\)$',
    re.IGNORECASE,
)


def mascara(nome: str) -> str:
    """Mostra o formato do nome, não o conteúdo."""
    base = Path(nome).stem
    achado = PADRAO_ESAJ.match(base)
    if not achado:
        return f'<fora do padrão, {len(base)} caracteres>'

    descricao = achado.group('desc')
    faixa = achado.group('ini')
    if achado.group('fim'):
        faixa += f' - {achado.group("fim")}'
    return f'<descrição com {len(descricao)} caracteres> (pag {faixa})'


def confere(caminho: Path) -> bool:
    """
    Confere um arquivo entregue pelo e-SAJ.

    :param caminho: arquivo a conferir
    :return: `True` se passou em todas as verificações
    """
    print(f'\n{"=" * 68}\n{caminho.name}\n{"=" * 68}')
    tamanho = caminho.stat().st_size
    print(f'  tamanho: {tamanho:,} bytes ({tamanho / 1024**3:.2f} GB)')

    if not zipfile.is_zipfile(caminho):
        cabecalho = caminho.read_bytes()[:5]
        if cabecalho.startswith(b'%PDF'):
            print('  formato: PDF único (opção "arquivo único" do e-SAJ)')
            return True
        print(f'  FALHA: não é ZIP nem PDF. Começa com {cabecalho!r}')
        return False

    with zipfile.ZipFile(caminho) as z:
        nomes = z.namelist()
        print(f'  formato: ZIP com {len(nomes):,} arquivo(s)')

        # Integridade: encontra a primeira entrada corrompida.
        print('  verificando integridade (pode demorar)...', flush=True)
        corrompido = z.testzip()
        if corrompido is not None:
            print(
                f'  FALHA: entrada corrompida no ZIP: '
                f'{mascara(corrompido)}'
            )
            return False
        print('  integridade: OK, nenhuma entrada corrompida')

        descompactado = sum(i.file_size for i in z.infolist())
        print(
            f'  descompactado: {descompactado:,} bytes '
            f'({descompactado / 1024**3:.2f} GB)'
        )

        extensoes = Counter(Path(n).suffix.lower() for n in nomes)
        print(f'  extensões: {dict(extensoes)}')

        no_padrao = [n for n in nomes if PADRAO_ESAJ.match(Path(n).stem)]
        print(
            f'  no padrão "Descrição (pag N - M).pdf": '
            f'{len(no_padrao):,} de {len(nomes):,}'
        )

        if no_padrao:
            print('  amostra (mascarada):')
            for nome in no_padrao[:3]:
                print(f'    {mascara(nome)}')

        fora = [n for n in nomes if not PADRAO_ESAJ.match(Path(n).stem)]
        if fora:
            print(f'  fora do padrão: {len(fora)}')
            for nome in fora[:3]:
                print(f'    {mascara(nome)}')

        # A ordem dos autos vem do número de página inicial.
        paginas = [
            int(PADRAO_ESAJ.match(Path(n).stem).group('ini'))
            for n in no_padrao
        ]
        if paginas:
            print(
                f'  páginas: da {min(paginas)} à {max(paginas)}; '
                f'{len(set(paginas)):,} início(s) distinto(s)'
            )

    return True


def main() -> int:
    if len(sys.argv) > 1:
        alvos = [Path(a) for a in sys.argv[1:]]
    else:
        pasta = Path('autos')
        if not pasta.is_dir():
            print('Pasta "autos" não encontrada. Informe o arquivo.')
            return 1
        alvos = sorted(
            p
            for p in pasta.rglob('*')
            if p.suffix.lower() in ('.zip', '.pdf')
        )

    if not alvos:
        print('Nenhum arquivo a conferir.')
        return 1

    tudo_certo = True
    for alvo in alvos:
        if not alvo.is_file():
            print(f'Não encontrado: {alvo}')
            tudo_certo = False
            continue
        tudo_certo &= confere(alvo)

    print()
    print('Conferência concluída.' if tudo_certo else 'Houve falhas.')
    return 0 if tudo_certo else 1


if __name__ == '__main__':
    raise SystemExit(main())

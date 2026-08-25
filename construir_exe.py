r"""
Gera o executável da interface gráfica.

Produz um arquivo único, que abre com duplo clique e não exige Python
instalado na máquina — do mesmo modo que o e-SAJ Merge PDFs.

Como usar
---------
    pip install pyinstaller
    python construir_exe.py

O resultado fica em `dist/` e é instalado em
%LOCALAPPDATA%\Programs\CopiaIntegralAutos, que é para onde o atalho
da área de trabalho aponta.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

NOME = 'CopiaIntegralAutos'
RAIZ = Path(__file__).parent


def main() -> int:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print('PyInstaller não encontrado. Instale com:')
        print('    pip install pyinstaller')
        return 1

    comando = [
        sys.executable,
        '-m',
        'PyInstaller',
        '--onefile',
        # Sem console: é programa de janela, não de terminal.
        '--noconsole',
        '--name',
        NOME,
        '--clean',
        '--noconfirm',
        # O Pydantic monta modelos em tempo de execução; sem isso o
        # empacotador não enxerga parte do que ele precisa.
        '--collect-all',
        'pydantic',
        '--hidden-import',
        'esaj_autos.gui',
        str(RAIZ / 'esaj_autos' / 'gui.py'),
    ]

    print('Empacotando...')
    resultado = subprocess.run(comando, cwd=RAIZ)
    if resultado.returncode != 0:
        return resultado.returncode

    alvo = RAIZ / 'dist' / f'{NOME}.exe'
    if not alvo.is_file():
        alvo = RAIZ / 'dist' / NOME

    print()
    print(f'Pronto: {alvo}')
    print(f'Tamanho: {alvo.stat().st_size / 1048576:.0f} MB')

    instalado = _instala(alvo)
    if instalado:
        print(f'Instalado em: {instalado}')

    # Restos do empacotamento não interessam a ninguém.
    shutil.rmtree(RAIZ / 'build', ignore_errors=True)
    (RAIZ / f'{NOME}.spec').unlink(missing_ok=True)

    return 0


def pasta_instalada() -> Path:
    """
    Onde o programa fica para uso do dia a dia.

    Fora da pasta de empacotamento: `dist` é apagada a cada
    reconstrução, e um atalho apontando para lá é removido pelo
    Windows assim que o alvo some.

    :return: pasta de instalação
    """
    base = os.getenv('LOCALAPPDATA')
    raiz = Path(base) / 'Programs' if base else Path.home() / '.local/bin'
    return raiz / NOME


def _instala(construido: Path):
    """
    Copia o programa recém-construído para o lugar definitivo.

    :param construido: executável saído do empacotador
    :return: caminho instalado, ou `None` se não foi possível
    """
    destino = pasta_instalada() / construido.name

    try:
        destino.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(construido, destino)
        return destino

    except OSError as e:
        # Costuma ser o programa aberto: o executável fica travado.
        print(f'Não foi possível instalar em {destino}: {e}')
        print('Feche o programa, se estiver aberto, e reconstrua.')
        return None


if __name__ == '__main__':
    raise SystemExit(main())

"""
Conferência do executável gerado.

Verifica se a aplicação **abre de fato**, e não apenas se o processo
existe. A distinção importa: quando o executável falha ao iniciar, o
diálogo de erro também é um processo vivo — foi assim que uma versão
quebrada passou por boa.

O critério aqui é a janela da aplicação existir, com o título certo.

Como usar
---------
    python tests/manual/conferir_exe.py [caminho do .exe]
"""

import subprocess
import sys
import time
import unicodedata
from pathlib import Path

# Trecho do título usado na comparação. Sem acentos de propósito: o
# console do Windows devolve os títulos na codepage local, e "Cópia"
# chega como "C¢pia". Comparar ao pé da letra reprovaria uma janela
# perfeitamente correta.
TITULO = 'Integral dos Autos'
ESPERA = 25


def sem_acento(texto: str) -> str:
    """
    Reduz o texto ao que sobrevive a qualquer codificação.

    :param texto: texto a normalizar
    :return: o texto sem acentos e em minúsculas
    """
    decomposto = unicodedata.normalize('NFKD', texto)
    return ''.join(
        c for c in decomposto if not unicodedata.combining(c)
    ).lower()


def janelas_abertas() -> list:
    """
    Lista os títulos de janela dos processos do executável.

    :return: títulos encontrados
    """
    comando = (
        '[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; '
        'Get-Process -Name CopiaIntegralAutos -ErrorAction SilentlyContinue '
        '| Where-Object { $_.MainWindowTitle -ne "" } '
        '| Select-Object -ExpandProperty MainWindowTitle'
    )
    saida = subprocess.run(
        ['powershell', '-NoProfile', '-Command', comando],
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace',
    )
    return [t.strip() for t in saida.stdout.splitlines() if t.strip()]


def encerra() -> None:
    subprocess.run(
        ['taskkill', '/IM', 'CopiaIntegralAutos.exe', '/F'],
        capture_output=True,
    )


def main() -> int:
    if sys.platform != 'win32':
        print('Esta conferência é específica do Windows.')
        return 1

    alvo = Path(
        sys.argv[1] if len(sys.argv) > 1 else 'dist/CopiaIntegralAutos.exe'
    )
    if not alvo.is_file():
        print(f'Executável não encontrado: {alvo}')
        return 1

    tamanho = alvo.stat().st_size / 1048576
    print(f'Executável: {alvo} ({tamanho:.0f} MB)')
    print('Abrindo...')

    processo = subprocess.Popen([str(alvo)])

    try:
        titulos = []
        for _ in range(ESPERA):
            time.sleep(1)
            titulos = janelas_abertas()
            if titulos:
                break

        print(f'Janelas encontradas: {titulos or "nenhuma"}')

        if not titulos:
            print(
                '\nFALHA: nenhuma janela apareceu. O executável pode ter '
                'morrido ao iniciar, ou demorou mais que o previsto.'
            )
            return 1

        alvo = sem_acento(TITULO)
        if not any(alvo in sem_acento(t) for t in titulos):
            print(
                f'\nFALHA: apareceu janela, mas nenhuma com "{TITULO}". '
                f'Provavelmente é o diálogo de erro do empacotador.'
            )
            return 1

        print('\nOK: a aplicação abriu com a janela esperada.')
        return 0

    finally:
        encerra()
        processo.poll()


if __name__ == '__main__':
    raise SystemExit(main())

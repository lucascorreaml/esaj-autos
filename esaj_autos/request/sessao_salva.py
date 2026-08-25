"""
Módulo que guarda a sessão autenticada entre execuções.

Cada login no e-SAJ consome um código de verificação, que chega por
e-mail e vale uma vez só. Refazer o login a cada execução, para
baixar mais um processo ou retomar um pedido, é o maior atrito de uso
do pacote.

A sessão do e-SAJ, uma vez autenticada, continua válida por algum
tempo. Guardá-la em disco permite que as execuções seguintes a
reaproveitem enquanto durar, sem novo código.

Nada aqui contorna a verificação em duas etapas: ela já aconteceu, e
o que se guarda é o resultado dela — do mesmo modo que um navegador
mantém o usuário conectado depois do login.

A validade nunca é adivinhada por tempo decorrido: pergunta-se ao
próprio e-SAJ se a sessão ainda vale.
"""

import json
import logging
import os
import re
import stat
from pathlib import Path
from typing import Optional

import requests

from esaj_autos.request.session import USER_AGENT, esta_logado

logger = logging.getLogger(__name__)


def caminho_padrao() -> Path:
    """
    Onde a sessão fica guardada.

    Usa a pasta de configuração do usuário, fora do projeto, para que
    a sessão não acompanhe o código nem entre em versionamento.

    :return: caminho do arquivo de sessão
    """
    base = os.getenv('APPDATA') or os.getenv('XDG_CONFIG_HOME')
    raiz = Path(base) if base else Path.home() / '.config'
    return raiz / 'esaj_autos' / 'sessao.json'


def salva(
    sessao: requests.Session,
    caminho: Optional[Path] = None,
    cpf: Optional[str] = None,
) -> Path:
    """
    Guarda os cookies da sessão autenticada.

    :param sessao: sessão autenticada
    :param caminho: onde gravar. O padrão é `caminho_padrao()`.
    :param cpf: de quem é a sessão, para que ela não seja
        reaproveitada por outra conta
    :return: caminho do arquivo gravado
    """
    caminho = Path(caminho or caminho_padrao())
    caminho.parent.mkdir(parents=True, exist_ok=True)

    cookies = [
        {
            'name': c.name,
            'value': c.value,
            'domain': c.domain,
            'path': c.path,
        }
        for c in sessao.cookies
    ]

    caminho.write_text(
        json.dumps(
            {'cpf': _so_digitos(cpf), 'cookies': cookies},
            ensure_ascii=False,
            indent=2,
        ),
        encoding='utf-8',
    )

    # A sessão dá acesso aos autos: restringe a leitura ao dono.
    # Em Windows o efeito é limitado, mas não custa e vale no resto.
    try:
        caminho.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass

    logger.info('Sessão guardada em %s', caminho)
    return caminho


def _so_digitos(valor: Optional[str]) -> str:
    """Reduz um CPF/CNPJ à sua forma comparável."""
    return re.sub(r'\D', '', valor or '')


def carrega(
    caminho: Optional[Path] = None,
    verify_ssl: bool = True,
    cpf: Optional[str] = None,
) -> Optional[requests.Session]:
    """
    Recupera a sessão guardada, se ainda estiver válida.

    A validade é verificada contra o e-SAJ, e não pelo tempo desde a
    gravação: só o servidor sabe se a sessão ainda vale.

    :param caminho: de onde ler. O padrão é `caminho_padrao()`.
    :param verify_ssl: valida o certificado do e-SAJ
    :param cpf: quando informado, a sessão só é reaproveitada se for
        dessa mesma conta
    :return: a sessão autenticada, ou `None` se não houver, tiver
        expirado ou pertencer a outra conta
    """
    caminho = Path(caminho or caminho_padrao())
    if not caminho.is_file():
        return None

    try:
        dados = json.loads(caminho.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        logger.info('Sessão guardada ilegível; será refeito o login.')
        return None

    # Reaproveitar a sessão de outra conta baixaria os autos com as
    # permissões de quem não foi pedido, e em silêncio.
    dono = _so_digitos(dados.get('cpf'))
    pedido = _so_digitos(cpf)
    if pedido and dono and pedido != dono:
        logger.info(
            'A sessão guardada é de outra conta; será refeito o login.'
        )
        return None

    sessao = requests.Session()
    sessao.verify = verify_ssl
    sessao.headers.update({'User-Agent': USER_AGENT})

    for cookie in dados.get('cookies', []):
        sessao.cookies.set(
            name=cookie['name'],
            value=cookie['value'],
            domain=cookie.get('domain'),
            path=cookie.get('path', '/'),
        )

    if not esta_logado(sessao):
        logger.info('A sessão guardada expirou; será refeito o login.')
        esquece(caminho)
        return None

    logger.info('Sessão reaproveitada: não é preciso novo código.')
    return sessao


def esquece(caminho: Optional[Path] = None) -> bool:
    """
    Apaga a sessão guardada.

    :param caminho: qual apagar. O padrão é `caminho_padrao()`.
    :return: `True` se havia algo a apagar
    """
    caminho = Path(caminho or caminho_padrao())
    if not caminho.is_file():
        return False

    caminho.unlink()
    logger.info('Sessão guardada apagada.')
    return True

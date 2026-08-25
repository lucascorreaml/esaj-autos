"""
Sessão HTTP autenticada no e-SAJ.

O login e o download são feitos em HTTP puro, sem navegador. Mas
quem já tiver uma sessão aberta num navegador automatizado pode
aproveitá-la: `cria_sessao` leva os cookies do navegador para o
`requests`, poupando um login e um código de dois fatores.
"""

import requests

from esaj_autos.exceptions import AutenticacaoError

# Domínio dos cookies do e-SAJ do TJSP
URL_BASE = 'https://esaj.tjsp.jus.br'

# Endpoint que informa se o usuário está autenticado no CAS.
# Responde com um JavaScript contendo "usuarioLogadoNoCasServer".
URL_VERIFICA_LOGIN = f'{URL_BASE}/sajcas/verificarLogin.js'

# User-Agent de navegador. O e-SAJ entrega conteúdo diferente
# (ou recusa) para clientes que não se identificam como navegador.
USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/124.0.0.0 Safari/537.36'
)


def cria_sessao(driver, verify_ssl: bool = True) -> requests.Session:
    """
    Cria uma sessão `requests` que herda os cookies do `driver`.

    O Selenium só entrega os cookies do domínio em que o navegador
    está. Se o `driver` estiver em outra página, ele é levado ao
    portal do e-SAJ antes da leitura, para que a sessão autenticada
    seja de fato transferida.

    :param driver: driver do Selenium já autenticado no e-SAJ
    :param verify_ssl: valida o certificado do e-SAJ. Como a sessão
        carrega os cookies de autenticação, o padrão é validar.
    :return: sessão HTTP pronta para uso
    """
    _garante_dominio_esaj(driver)

    sessao = requests.Session()
    sessao.verify = verify_ssl
    sessao.headers.update({'User-Agent': USER_AGENT})

    for cookie in driver.get_cookies():
        sessao.cookies.set(
            name=cookie['name'],
            value=cookie['value'],
            domain=cookie.get('domain'),
            path=cookie.get('path', '/'),
        )

    return sessao


def _garante_dominio_esaj(driver) -> None:
    """
    Leva o navegador ao e-SAJ, se ele estiver em outro domínio.

    :param driver: driver do Selenium
    """
    try:
        url_atual = driver.current_url or ''
    except Exception:
        # Drivers simplificados podem não expor a URL corrente.
        return

    if 'esaj.tjsp.jus.br' not in url_atual:
        driver.get(f'{URL_BASE}/esaj/portal.do?servico=740000')


def esta_logado(sessao: requests.Session, timeout: int = 30) -> bool:
    """
    Avalia se a sessão está autenticada no e-SAJ.

    :param sessao: sessão HTTP a avaliar
    :param timeout: tempo limite da requisição, em segundos
    :return: `True` quando há usuário autenticado
    """
    try:
        resposta = sessao.get(URL_VERIFICA_LOGIN, timeout=timeout)
    except requests.RequestException:
        return False

    # A resposta é do tipo:
    # window.sajcas = { usuarioLogadoNoCasServer: true };
    return 'true' in resposta.text.lower()


def exige_login(sessao: requests.Session) -> None:
    """
    Interrompe a execução se a sessão não estiver autenticada.

    :param sessao: sessão HTTP a avaliar
    :raises AutenticacaoError: quando não há usuário autenticado
    """
    if not esta_logado(sessao):
        raise AutenticacaoError(
            'Não há sessão autenticada no e-SAJ. '
            'A cópia integral dos autos exige login. '
            'Faça "login_1_etapa" e "login_2_etapa" antes de baixar.'
        )

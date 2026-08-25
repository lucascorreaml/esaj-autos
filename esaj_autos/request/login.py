"""
Módulo com a autenticação no e-SAJ por HTTP.

O e-SAJ autentica pelo CAS, em duas etapas: usuário e senha, e depois
um código de seis dígitos enviado por e-mail. As duas etapas são
requisições de formulário comuns, o que dispensa o navegador.

O código continua vindo do e-mail do usuário: nada aqui contorna a
verificação em duas etapas, apenas conduz o mesmo formulário que a
página de login submete.
"""

import logging
import re
from typing import Optional
from urllib.parse import quote

import requests

from esaj_autos.exceptions import AutenticacaoError, ESAJIndisponivelError
from esaj_autos.request.session import (
    URL_BASE,
    USER_AGENT,
    esta_logado,
)

logger = logging.getLogger(__name__)

URL_PORTAL = f'{URL_BASE}/esaj/portal.do?servico=740000'
URL_RETORNO = f'{URL_BASE}/esaj/j_spring_cas_security_check'
URL_LOGIN = f'{URL_BASE}/sajcas/login?service={quote(URL_RETORNO, safe="")}'

# Campos ocultos que o CAS exige de volta em cada submissão.
PADRAO_LT = re.compile(r'name=["\']lt["\'][^>]*value=["\']([^"\']*)["\']')
PADRAO_EXECUTION = re.compile(
    r'name=["\']execution["\'][^>]*value=["\']([^"\']*)["\']'
)
# A ordem dos atributos varia; procura também no sentido inverso.
PADRAO_EXECUTION_ALT = re.compile(
    r'value=["\']([^"\']*)["\'][^>]*name=["\']execution["\']'
)

# O e-SAJ reporta a recusa do login em parágrafos "errorMsg". Não usa
# o container "mensagemRetorno" do resto do portal, e o formulário do
# token está sempre presente na página — de modo que a presença desse
# parágrafo é o único sinal confiável de que algo foi recusado.
PADRAO_ERRO = re.compile(
    r'class=["\']errorMsg["\'][^>]*>(.*?)</p>',
    re.IGNORECASE | re.DOTALL,
)

# Trechos que distinguem o motivo, dentro da mensagem de erro.
MSG_CREDENCIAL_INVALIDA = 'senha inválid'
MSG_TOKEN_INVALIDO = 'código'
# Erro genérico do CAS quando não consegue concluir a etapa.
MSG_FLUXO = 'não foi possível completar'

# A página declara o estado do login em variáveis JavaScript. É por
# elas que o próprio portal decide o que exibir: "DuploFatorHabilitado"
# liga a tela do código, e é o sinal de que as credenciais passaram.
PADRAO_FLAG = re.compile(
    r'\$\.saj\.cas\.(\w+)\s*=\s*([^;]+);'
)

# Reenvio do código por e-mail, o mesmo que o botão "Receber novo
# código" aciona.
URL_REENVIA_TOKEN = f'{URL_BASE}/sajcas/reenviarEmailTokenDuploFator'


def le_flags_cas(html: str) -> dict:
    """
    Lê as variáveis de estado que a página de login declara.

    :param html: corpo da resposta
    :return: dicionário com as variáveis encontradas
    """
    flags = {}
    for achado in PADRAO_FLAG.finditer(html or ''):
        nome, bruto = achado.group(1), achado.group(2).strip()

        # "false || false" e "'' || 1" são idiomas da própria página.
        primeiro = bruto.split('||')[0].strip().strip('"\'')

        if primeiro in ('true', 'false'):
            flags[nome] = primeiro == 'true'
        else:
            flags[nome] = primeiro

    return flags


def espera_entre_codigos(flags: dict) -> Optional[int]:
    """
    Carência que o e-SAJ impõe entre dois pedidos de código.

    O portal informa esse tempo em `TempoConfiguracaoDuploFator` e o
    interpreta de modo peculiar, que se reproduz aqui: valores até 3
    são minutos; acima disso, segundos. É o que faz o próprio
    contador da tela ao desabilitar o botão de receber novo código.

    :param flags: estado declarado pela página
    :return: a carência em segundos, ou `None` se não informada
    """
    bruto = flags.get('TempoConfiguracaoDuploFator')
    try:
        valor = int(str(bruto).strip())
    except (TypeError, ValueError):
        return None

    if valor <= 0:
        return None

    return valor if valor > 3 else valor * 60


def primeiro_erro(html: str) -> Optional[str]:
    """
    Devolve a primeira mensagem de erro da página de login.

    :param html: corpo da resposta
    :return: a mensagem, em texto limpo, ou `None` se o e-SAJ não
        recusou nada
    """
    for achado in PADRAO_ERRO.finditer(html or ''):
        texto = re.sub(r'<[^>]+>', ' ', achado.group(1))
        texto = re.sub(r'\s+', ' ', texto).strip()
        if texto:
            return texto
    return None


class Autenticacao:
    """
    Conduz o login em duas etapas e guarda a sessão resultante.

    Uso típico::

        auth = Autenticacao()
        auth.primeira_etapa(cpf='...', senha='...')   # dispara o e-mail
        sessao = auth.segunda_etapa(token='123456')
    """

    def __init__(self, verify_ssl: bool = True) -> None:
        self.sessao = requests.Session()
        self.sessao.verify = verify_ssl
        self.sessao.headers.update({'User-Agent': USER_AGENT})
        self._lt: Optional[str] = None
        self._execution: Optional[str] = None
        # O CAS renova a chave do fluxo a cada resposta, mas nem toda
        # instalação exige a nova: a anterior fica guardada para uma
        # segunda tentativa em caso de erro de fluxo.
        self._execution_anterior: Optional[str] = None
        # A segunda etapa reenvia o mesmo formulário da primeira, que
        # inclui usuário e senha. Ficam guardados só até o login
        # terminar.
        self._cpf: Optional[str] = None
        self._senha: Optional[str] = None
        # Estado declarado pela página do CAS e dados da última
        # resposta, usados no diagnóstico quando o login não segue.
        self.flags: dict = {}
        self._ultimo_status: Optional[int] = None
        self._ultimo_tamanho: int = 0

    # -----------------------------------------------------------------
    # Etapas
    # -----------------------------------------------------------------

    def primeira_etapa(self, cpf: str, senha: str) -> None:
        """
        Envia usuário e senha, o que faz o e-SAJ despachar o código
        por e-mail.

        :param cpf: CPF ou CNPJ cadastrado no e-SAJ
        :param senha: senha do e-SAJ
        :raises AutenticacaoError: credenciais recusadas
        """
        self._abre_formulario()
        self._cpf = cpf
        self._senha = senha

        corpo = {
            'username': cpf,
            'password': senha,
            'lt': self._lt or '',
            'execution': self._execution or '',
            '_eventId': 'submit',
            'pbEntrar': 'Entrar',
            'signature': '',
            'certificadoSelecionado': '',
            'certificado': '',
        }
        html = self._submete(corpo)

        # O e-SAJ renova os campos ocultos entre as etapas.
        self._le_campos_ocultos(html)
        self.flags = le_flags_cas(html)

        # Sem segunda etapa: contas sem duplo fator entram direto.
        if esta_logado(self.sessao):
            logger.info('Login concluído sem segunda etapa.')
            return

        # A tela do código ligada é o sinal de que as credenciais
        # passaram e o e-SAJ despachou o e-mail.
        if self.flags.get('DuploFatorHabilitado'):
            destino = self.flags.get('DeEmail') or 'seu e-mail'
            logger.info(
                'Credenciais aceitas. O e-SAJ enviou o código para %s.',
                destino,
            )
            return

        erro = primeiro_erro(html)

        if erro and MSG_FLUXO in erro.lower():
            # O portal informa a própria carência entre pedidos de
            # código. Dizer o número exato poupa o usuário de tentar
            # cedo demais e gastar mais uma recusa.
            espera = espera_entre_codigos(self.flags)
            if espera:
                quanto = (
                    f'{espera // 60} minuto(s)'
                    if espera >= 60
                    else f'{espera} segundos'
                )
                recado = (
                    f'Suas credenciais foram reconhecidas, mas o e-SAJ '
                    f'não enviou o código: ele exige {quanto} entre um '
                    f'pedido e o seguinte. Aguarde esse tempo e tente '
                    f'de novo.'
                )
            else:
                recado = (
                    'O e-SAJ não concluiu a etapa das credenciais. '
                    'Costuma ser limite temporário de envio do código: '
                    'espere alguns minutos antes de tentar de novo.'
                )

            raise AutenticacaoError(
                f'{recado} Mensagem do e-SAJ: "{erro}" '
                f'{self.diagnostico()}'
            )

        if erro:
            raise AutenticacaoError(f'O e-SAJ recusou o login: {erro}')

        raise AutenticacaoError(
            'O e-SAJ não apresentou a tela do código nem concluiu o '
            f'login. {self.diagnostico()}'
        )

    def diagnostico(self) -> str:
        """
        Resume o estado da última resposta, sem expor credenciais.

        Serve para relatar o que o e-SAJ devolveu quando o login não
        segue, sem precisar reproduzir a sessão.

        :return: resumo em uma linha
        """
        interessantes = (
            'DuploFatorHabilitado',
            'SenhaExpirada',
            'loginCertificado',
            'magistrado',
            'TempoConfiguracaoDuploFator',
        )
        estado = {
            nome: self.flags.get(nome)
            for nome in interessantes
            if nome in self.flags
        }
        return (
            f'[diagnóstico: HTTP {self._ultimo_status}, '
            f'{self._ultimo_tamanho} caracteres, estado do CAS {estado}]'
        )

    def reenviar_codigo(self) -> bool:
        """
        Pede ao e-SAJ que reenvie o código por e-mail.

        É o mesmo pedido do botão "Receber novo código". Só funciona
        depois de uma primeira etapa bem-sucedida, que é quando o
        e-SAJ informa os dados do usuário.

        :return: `True` se o pedido foi aceito
        """
        if not self.flags.get('DuploFatorHabilitado'):
            raise AutenticacaoError(
                'Só é possível pedir novo código depois de enviar as '
                'credenciais com sucesso.'
            )

        dados = {
            'deEmail': self.flags.get('DeEmail', ''),
            'deEmailAlternativo': self.flags.get('DeEmailAlternativo', ''),
            'cdUsuario': self.flags.get('CdUsuario', ''),
            'nmUsuario': self.flags.get('NmUsuario', ''),
            'nmSocialUsuario': self.flags.get('NmSocialUsuario', ''),
        }

        try:
            resposta = self.sessao.post(
                URL_REENVIA_TOKEN,
                data=dados,
                headers={
                    'X-Requested-With': 'XMLHttpRequest',
                    'Referer': URL_LOGIN,
                },
                timeout=60,
            )
        except requests.RequestException as e:
            raise ESAJIndisponivelError(
                f'Não foi possível pedir novo código: {e}'
            ) from e

        return resposta.status_code == 200

    def segunda_etapa(self, token: str) -> requests.Session:
        """
        Envia o código recebido por e-mail e conclui o login.

        :param token: código de seis dígitos
        :return: a sessão autenticada
        :raises AutenticacaoError: código recusado
        """
        token = re.sub(r'\D', '', str(token))
        if not token:
            raise AutenticacaoError(
                'O código de verificação deve conter apenas dígitos.'
            )

        # A página do código não tem formulário próprio: o campo
        # visível "tokenInformado" fica fora dele e é copiado para o
        # campo oculto "token" do mesmo formulário da primeira etapa.
        # Por isso usuário e senha voltam junto.
        html = self._envia_token(token, self._execution)
        self.flags = le_flags_cas(html) or self.flags

        if not esta_logado(self.sessao) and self._execution_anterior:
            erro = primeiro_erro(html) or ''
            # Erro de fluxo, e não de código: vale repetir com a chave
            # anterior, que algumas instalações do CAS mantêm válida.
            if MSG_FLUXO in erro.lower():
                logger.info(
                    'O e-SAJ recusou a chave do fluxo; repetindo com '
                    'a anterior.'
                )
                html = self._envia_token(token, self._execution_anterior)

        if not esta_logado(self.sessao):
            erro = primeiro_erro(html)
            if erro:
                raise AutenticacaoError(
                    f'O e-SAJ recusou o envio do código: {erro} '
                    'Confira o código mais recente no e-mail — cada '
                    'novo pedido de login invalida o anterior.'
                )
            raise AutenticacaoError(
                'O login não foi concluído após o envio do código. '
                f'{self.diagnostico()}'
            )

        # As credenciais não precisam mais ficar em memória.
        self._cpf = None
        self._senha = None

        logger.info('Sessão autenticada no e-SAJ.')
        return self.sessao

    def _envia_token(self, token: str, execution: Optional[str]) -> str:
        """
        Submete o formulário de login com o código de verificação.

        :param token: código de seis dígitos
        :param execution: chave do fluxo a usar
        :return: HTML da resposta
        """
        corpo = {
            'username': self._cpf or '',
            'password': self._senha or '',
            'lt': self._lt or '',
            'execution': execution or '',
            '_eventId': 'submit',
            'token': token,
            'signature': '',
            'certificadoSelecionado': '',
            'certificado': '',
        }
        return self._submete(corpo)

    # -----------------------------------------------------------------
    # Bastidores
    # -----------------------------------------------------------------

    def _abre_formulario(self) -> None:
        """
        Carrega a página de login e guarda os campos ocultos do CAS.
        """
        try:
            self.sessao.get(URL_PORTAL, timeout=60)
            resposta = self.sessao.get(URL_LOGIN, timeout=60)

        except requests.RequestException as e:
            raise ESAJIndisponivelError(
                f'Não foi possível abrir a página de login do e-SAJ: {e}'
            ) from e

        if resposta.status_code >= 500:
            raise ESAJIndisponivelError(
                f'O e-SAJ respondeu com HTTP {resposta.status_code} na '
                'página de login. O serviço pode estar em manutenção.'
            )

        self._le_campos_ocultos(resposta.text)

        if self._execution is None:
            raise ESAJIndisponivelError(
                'A página de login do e-SAJ não trouxe os campos '
                'esperados. O formato da página pode ter mudado.'
            )

    def _le_campos_ocultos(self, html: str) -> None:
        """
        Extrai "lt" e "execution" da página, quando presentes.
        """
        achado = PADRAO_LT.search(html)
        if achado:
            self._lt = achado.group(1)

        achado = PADRAO_EXECUTION.search(
            html
        ) or PADRAO_EXECUTION_ALT.search(html)
        if achado and achado.group(1) != self._execution:
            self._execution_anterior = self._execution
            self._execution = achado.group(1)

    def _submete(self, corpo: dict) -> str:
        """
        Submete o formulário de login e devolve o HTML da resposta.
        """
        try:
            resposta = self.sessao.post(
                URL_LOGIN,
                data=corpo,
                timeout=90,
                headers={'Referer': URL_LOGIN},
            )
        except requests.RequestException as e:
            raise ESAJIndisponivelError(
                f'Falha ao enviar o formulário de login: {e}'
            ) from e

        self._ultimo_status = resposta.status_code
        self._ultimo_tamanho = len(resposta.text or '')

        if resposta.status_code >= 500:
            raise ESAJIndisponivelError(
                f'O e-SAJ respondeu com HTTP {resposta.status_code} '
                'durante o login.'
            )

        return resposta.text or ''


def entrar(
    cpf: Optional[str] = None,
    senha: Optional[str] = None,
    token: Optional[str] = None,
    verify_ssl: bool = True,
    reaproveitar: bool = True,
) -> requests.Session:
    """
    Faz o login completo, perguntando o que não for informado.

    Sem `token`, o código é pedido no terminal depois que o e-SAJ o
    envia por e-mail.

    Por padrão a sessão da última execução é reaproveitada enquanto o
    e-SAJ a considerar válida, o que evita gastar um novo código a
    cada chamada.

    :param cpf: CPF ou CNPJ. Se ausente, é perguntado.
    :param senha: senha. Se ausente, é perguntada sem eco.
    :param token: código de verificação. Se ausente, é perguntado
        após o envio das credenciais.
    :param verify_ssl: valida o certificado do e-SAJ
    :param reaproveitar: `False` ignora a sessão guardada e faz login
        novo
    :return: sessão autenticada, pronta para baixar autos
    """
    import getpass

    from esaj_autos.request import sessao_salva

    if reaproveitar:
        guardada = sessao_salva.carrega(verify_ssl=verify_ssl, cpf=cpf)
        if guardada is not None:
            return guardada

    if not cpf:
        cpf = input('CPF/CNPJ do e-SAJ: ').strip()
    if not senha:
        senha = getpass.getpass('Senha do e-SAJ (não aparece): ')

    auth = Autenticacao(verify_ssl=verify_ssl)
    auth.primeira_etapa(cpf=cpf, senha=senha)

    if esta_logado(auth.sessao):
        sessao_salva.salva(auth.sessao, cpf=cpf)
        return auth.sessao

    if not token:
        destino = auth.flags.get('DeEmail') or 'seu e-mail'
        print(f'O e-SAJ enviou um código de verificação para {destino}.')
        print('(deixe em branco e tecle Enter para pedir um novo código)')

        token = input('Código recebido: ').strip()

        while not token:
            if auth.reenviar_codigo():
                print('Novo código solicitado. Verifique o e-mail.')
            else:
                print(
                    'O e-SAJ não aceitou o pedido de novo código. '
                    'Aguarde um pouco antes de tentar de novo.'
                )
            token = input('Código recebido: ').strip()

    sessao = auth.segunda_etapa(token=token)
    sessao_salva.salva(sessao, cpf=cpf)
    return sessao

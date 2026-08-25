"""
Exceções do pacote.

Concentra os estados de erro previsíveis na obtenção da cópia
integral dos autos, de modo que quem usa o pacote possa tratar
cada situação de maneira específica, sem precisar interpretar
mensagens de texto do e-SAJ.
"""


class ESAJError(Exception):
    """
    Erro genérico do e-SAJ. É a base de toda a hierarquia,
    permitindo capturar qualquer falha do pacote com um único
    `except`.
    """


class ESAJIndisponivelError(ESAJError):
    """
    O e-SAJ não respondeu ou respondeu com erro de servidor (5xx).
    Situação transitória, típica das janelas de manutenção do TJSP.
    """


class AutenticacaoError(ESAJError):
    """
    Não há sessão autenticada no e-SAJ.

    A cópia integral dos autos exige login. Faça o login em duas
    etapas (usuário/senha e, na sequência, o token enviado por
    e-mail) antes de solicitar o download.
    """


class SessaoExpiradaError(ESAJError):
    """
    A sessão existia, mas expirou durante a operação.

    Ocorre em processos volumosos, cuja preparação do arquivo pelo
    e-SAJ demora mais que o tempo de vida da sessão.
    """


class ProcessoNaoEncontradoError(ESAJError):
    """
    O número CNJ informado não corresponde a nenhum processo
    localizável no e-SAJ do TJSP.
    """


class SemAcessoAosAutosError(ESAJError):
    """
    O processo existe, mas a pasta digital não está acessível ao
    usuário autenticado.

    Abrange segredo de justiça, ausência de vínculo com o processo
    e processos cujos autos não são digitais.
    """


class ProcessoComSenhaError(SemAcessoAosAutosError):
    """
    O processo exige "senha do processo" para liberar a pasta
    digital. O e-SAJ pede essa senha em uma janela própria e ela não
    é fornecida pelo login comum.
    """


class LimiteAcessoExcedidoError(SemAcessoAosAutosError):
    """
    O e-SAJ recusou o acesso por limite diário.

    O TJSP limita a quantidade de acessos à pasta digital de
    processos aos quais o usuário não está vinculado. O limite se
    renova no dia seguinte.
    """


class PreparacaoTimeoutError(ESAJError):
    """
    O e-SAJ aceitou o pedido, mas não finalizou a preparação do
    arquivo dentro do tempo limite.

    A geração da cópia integral é assíncrona: processos com muitas
    peças podem levar vários minutos. Aumentar o tempo de espera
    costuma resolver.
    """


class DownloadError(ESAJError):
    """
    A preparação foi concluída, mas o arquivo não pôde ser
    transferido ou chegou incompleto/corrompido.
    """

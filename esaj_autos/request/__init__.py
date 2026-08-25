"""
Acesso ao e-SAJ por requisições HTTP.

Concentra a autenticação, a sessão guardada entre execuções e o
download da cópia integral dos autos.
"""

from . import login, pasta_digital, sessao_salva, session
from .login import Autenticacao, entrar
from .session import cria_sessao, esta_logado, exige_login

__all__ = [
    'Autenticacao',
    'cria_sessao',
    'entrar',
    'esta_logado',
    'exige_login',
    'login',
    'pasta_digital',
    'sessao_salva',
    'session',
]

"""
Módulo para definição dos parâmetros de entrada e do resultado
do download da cópia integral dos autos.
"""

from pathlib import Path
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from esaj_autos.request.pasta_digital import normaliza_cnj


class DownloadAutos(BaseModel):
    """
    Parâmetros do download da cópia integral dos autos.

    :param numero_cnj: número do processo no padrão CNJ, com ou sem
        pontuação
    :param instancia: grau de jurisdição do processo
    :param destino: pasta onde a subpasta do processo será criada
    :param separar_documentos: `True` baixa um ZIP com uma peça por
        PDF, preservando a divisão dos autos; `False` baixa um PDF
        único com o processo inteiro
    :param espera_maxima: tempo total de espera pela preparação do
        arquivo, em segundos
    :param intervalo: intervalo entre as consultas de andamento, em
        segundos
    :param sobrescrever: `True` refaz o download mesmo que o arquivo
        já exista
    :param pecas_por_pedido: divide o pedido em lotes desse tamanho,
        gerando um arquivo por lote. `0` pede tudo de uma vez, que é
        o normal. Serve para processos com dezenas de milhares de
        peças, em que o pedido único não é concluído.
    """

    numero_cnj: str
    instancia: Literal['Primeiro Grau', 'Segundo Grau'] = 'Primeiro Grau'
    destino: Path = Path('autos')
    separar_documentos: bool = True
    espera_maxima: int = Field(default=900, ge=30)
    intervalo: int = Field(default=5, ge=1)
    sobrescrever: bool = False
    pecas_por_pedido: int = Field(default=0, ge=0)

    @field_validator('numero_cnj')
    @classmethod
    def valida_numero(cls, valor: str) -> str:
        """
        Garante que o número é um CNJ válido do TJSP e o devolve
        formatado.
        """
        _, formatado = normaliza_cnj(valor)
        return formatado

    @property
    def extensao(self) -> str:
        """
        Extensão do arquivo entregue pelo e-SAJ, conforme o formato
        escolhido.
        """
        return '.zip' if self.separar_documentos else '.pdf'

    @property
    def pasta_processo(self) -> Path:
        """
        Pasta própria do processo, nomeada pelo número CNJ.
        """
        return Path(self.destino) / self.numero_cnj

    @property
    def arquivo(self) -> Path:
        """
        Caminho final do arquivo, com nomenclatura previsível.
        """
        return self.pasta_processo / f'{self.numero_cnj}{self.extensao}'

    def arquivo_da_parte(self, indice: int, total: int) -> Path:
        """
        Caminho de uma das partes, quando o pedido é dividido.

        Com uma única parte, o nome é o mesmo de sempre: dividir o
        pedido é detalhe do transporte, não do resultado.

        :param indice: número da parte, a partir de 1
        :param total: quantidade de partes
        :return: caminho do arquivo da parte
        """
        if total <= 1:
            return self.arquivo

        largura = len(str(total))
        nome = (
            f'{self.numero_cnj}-parte-'
            f'{indice:0{largura}d}-de-{total}{self.extensao}'
        )
        return self.pasta_processo / nome

    def divide_em_lotes(self, pecas: list) -> list:
        """
        Divide as peças conforme "pecas_por_pedido".

        :param pecas: identificadores das peças
        :return: lista de lotes
        """
        if not self.pecas_por_pedido:
            return [pecas]

        tamanho = self.pecas_por_pedido
        return [
            pecas[i : i + tamanho] for i in range(0, len(pecas), tamanho)
        ]


class FalhaDownload(BaseModel):
    """
    Processo que não pôde ser baixado em uma execução em lote.

    :param numero_cnj: número informado
    :param erro: nome da exceção
    :param mensagem: explicação do que impediu o download
    """

    numero_cnj: str
    erro: str
    mensagem: str


class ResultadoDownload(BaseModel):
    """
    Resultado do download da cópia integral dos autos.

    :param numero_cnj: número do processo, formatado
    :param cd_processo: código interno do processo no e-SAJ
    :param instancia: grau de jurisdição consultado
    :param arquivo: caminho do arquivo gravado
    :param tamanho_bytes: tamanho do arquivo gravado
    :param total_pecas: quantidade de peças incluídas no pedido
    :param formato: formato entregue pelo e-SAJ
    :param reaproveitado: `True` quando o arquivo já existia e o
        download foi dispensado
    :param partes: todos os arquivos gravados. Tem um item só, igual
        a `arquivo`, salvo quando o pedido foi dividido em lotes.
    """

    numero_cnj: str
    cd_processo: Optional[str] = None
    instancia: str
    arquivo: Path
    tamanho_bytes: int
    total_pecas: int
    formato: Literal['zip', 'pdf']
    reaproveitado: bool = False
    partes: List[Path] = []


class ResultadoLote(BaseModel):
    """
    Resultado de um download em lote.

    Uma falha em um processo não interrompe os demais: cada processo
    aparece em `sucessos` ou em `falhas`.

    :param sucessos: processos baixados
    :param falhas: processos que não puderam ser baixados
    :param informados: quantos processos foram pedidos. Difere da
        soma dos anteriores quando o lote é interrompido no meio.
    """

    sucessos: List[ResultadoDownload] = []
    falhas: List[FalhaDownload] = []
    informados: int = 0

    @property
    def total(self) -> int:
        """Quantidade de processos efetivamente processados."""
        return len(self.sucessos) + len(self.falhas)

    @property
    def nao_tentados(self) -> int:
        """Processos que ficaram sem tentativa alguma."""
        return max(0, self.informados - self.total)

    def __str__(self) -> str:
        base = self.informados or self.total
        linhas = [
            f'{len(self.sucessos)} de {base} processo(s) baixado(s).'
        ]

        # Sem isso, um lote interrompido no meio pareceria completo.
        if self.nao_tentados:
            linhas.append(
                f'{self.nao_tentados} processo(s) não chegaram a ser '
                f'tentados: o lote foi interrompido.'
            )

        for falha in self.falhas:
            linhas.append(
                f'  - {falha.numero_cnj}: {falha.erro} — {falha.mensagem}'
            )
        return '\n'.join(linhas)

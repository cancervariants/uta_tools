"""Module for accessing Gene Normalizer"""
import logging
from typing import Dict, Optional

from gene.database.dynamodb import DynamoDbDatabase
from gene.query import QueryHandler
from gene.schemas import SourceName


logger = logging.getLogger("cool_seq_tool")


class GeneNormalizer:
    """Gene Normalizer class for getting gene data"""

    def __init__(
        self, query_handler: Optional[QueryHandler] = None, db_url: str = "",
        db_region: str = "us-east-2"
    ) -> None:
        """Initialize gene normalizer class

        :param QueryHandler query_handler: Gene normalizer query handler instance.
            If this is provided, will use a current instance. If this is not provided,
            will create a new instance.
        :param str db_url: URL to gene normalizer dynamodb. Only used when
            `query_handler` is `None`.
        :param str db_region: AWS region for gene normalizer db. Only used when
            `query_handler` is `None`.
        """
        if query_handler:
            self.query_handler = query_handler
        else:
            ddb = DynamoDbDatabase(db_url=db_url, region_name=db_region)
            self.query_handler = QueryHandler(ddb)

    def get_hgnc_data(self, gene: str) -> Dict:
        """Return HGNC data for a given gene

        :param str gene: Gene query
        :return: HGNC data
        """
        hgnc_data = dict()
        gene_resp = self.query_handler.normalize_unmerged(gene)
        hgnc_matches = gene_resp.source_matches.get(SourceName.HGNC)
        if hgnc_matches and hgnc_matches.records:
            hgnc_data = hgnc_matches.records[0].dict()
        else:
            logger.warning(f"Unable to get HGNC symbol for {gene}")
        return hgnc_data

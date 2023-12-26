"""Retrieve MANE transcript from a location on p./c./g. coordinates.

Steps:

#. Map annotation layer to genome
#. Liftover to preferred genome (GRCh38). GRCh36 and earlier assemblies are not supported
   for fetching MANE transcripts.
#. Select preferred compatible annotation (see :ref:`transcript compatibility <transcript_compatibility>`)
#. Map back to correct annotation layer

In addition to a mapper utility class, this module also defines several vocabulary
constraints and data models for coordinate representation.
"""
import logging
import math
from enum import StrEnum
from typing import Dict, List, Optional, Set, Tuple, Union

import polars as pl
from pydantic import BaseModel

from cool_seq_tool.handlers.seqrepo_access import SeqRepoAccess
from cool_seq_tool.schemas import (
    AnnotationLayer,
    Assembly,
    ResidueMode,
    Strand,
    TranscriptPriority,
)
from cool_seq_tool.sources import (
    MANETranscriptMappings,
    TranscriptMappings,
    UTADatabase,
)
from cool_seq_tool.utils import get_inter_residue_pos

logger = logging.getLogger(__name__)


class EndAnnotationLayer(StrEnum):
    """Define constraints for end annotation layer. This is used for determining the
    end annotation layer when getting the longest compatible remaining representation
    """

    PROTEIN = AnnotationLayer.PROTEIN
    CDNA = AnnotationLayer.CDNA
    PROTEIN_AND_CDNA = "p_and_c"


class DataRepresentation(BaseModel):
    """Define object model for final output representation"""

    gene: Optional[str] = None
    refseq: str
    ensembl: Optional[str] = None
    pos: Tuple[int, int]
    strand: Strand
    status: TranscriptPriority


class CdnaRepresentation(DataRepresentation):
    """Define object model for coding DNA representation"""

    coding_start_site: int
    coding_end_site: int
    alt_ac: Optional[str] = None


class GenomicRepresentation(BaseModel):
    """Define object model for genomic representation"""

    refseq: str
    pos: Tuple[int, int]
    status: TranscriptPriority
    alt_ac: str


class ProteinAndCdnaRepresentation(BaseModel):
    """Define object model for protein and cDNA representation"""

    protein: DataRepresentation
    cdna: CdnaRepresentation


class MANETranscript:
    """Class for retrieving MANE transcripts."""

    def __init__(
        self,
        seqrepo_access: SeqRepoAccess,
        transcript_mappings: TranscriptMappings,
        mane_transcript_mappings: MANETranscriptMappings,
        uta_db: UTADatabase,
    ) -> None:
        """Initialize the MANETranscript class.

        A handful of resources are required for initialization, so when defaults are
        enough, it's easiest to let the core CoolSeqTool class handle it for you:

        >>> from cool_seq_tool.app import CoolSeqTool
        >>> mane_mapper = CoolSeqTool().mane_transcript

        Note that most methods are defined as Python coroutines, so they must be called
        with ``await``:

        >>> result = mane_mapper.g_to_grch38("NC_000001.11", 100, 200)
        >>> result['ac']
        TypeError: 'coroutine' object is not subscriptable

        :param seqrepo_access: Access to seqrepo queries
        :param transcript_mappings: Access to transcript accession mappings and
            conversions
        :param mane_transcript_mappings: Access to MANE Transcript accession mapping
            data
        :param uta_db: UTADatabase instance to give access to query UTA database
        """
        self.seqrepo_access = seqrepo_access
        self.transcript_mappings = transcript_mappings
        self.mane_transcript_mappings = mane_transcript_mappings
        self.uta_db = uta_db

    @staticmethod
    def _get_reading_frame(pos: int) -> int:
        """Return reading frame number. Only used on c. coordinate.

        :param pos: cDNA position
        :return: Reading frame
        """
        pos_mod_3 = pos % 3
        if pos_mod_3 == 0:
            pos_mod_3 = 3
        return pos_mod_3

    @staticmethod
    def _p_to_c_pos(start: int, end: int) -> Tuple[int, int]:
        """Return cDNA position given a protein position.

        :param start: Start protein position
        :param end: End protein position
        :return: cDNA position start, cDNA position end
        """
        start_pos = start * 3 - 1
        if end != start:
            end_pos = end * 3 - 1
        else:
            end_pos = start_pos

        return start_pos - 1, end_pos + 1

    async def _p_to_c(
        self, ac: str, start_pos: int, end_pos: int
    ) -> Optional[Tuple[str, Tuple[int, int]]]:
        """Convert protein (p.) annotation to cDNA (c.) annotation.

        :param ac: Protein accession
        :param start_pos: Protein start position
        :param end_pos: Protein end position
        :return: [cDNA transcript accession, [cDNA pos start, cDNA pos end]]
        """
        # TODO: Check version mappings 1 to 1 relationship
        temp_ac = await self.uta_db.p_to_c_ac(ac)
        if temp_ac:
            ac = temp_ac[-1]
        else:
            try:
                if ac.startswith("NP_"):
                    ac = self.transcript_mappings.np_to_nm[ac]
                elif ac.startswith("ENSP"):
                    ac = self.transcript_mappings.ensp_to_enst[ac]
                else:
                    logger.warning(f"Unable to find accession: {ac}")
                    return None
            except KeyError:
                logger.warning(f"{ac} not found in transcript_mappings")
                return None

        pos = self._p_to_c_pos(start_pos, end_pos)
        return ac, pos

    async def _c_to_g(self, ac: str, pos: Tuple[int, int]) -> Optional[Dict]:
        """Get g. annotation from c. annotation.

        :param ac: cDNA accession
        :param pos: [cDNA pos start, cDNA pos end]
        :return: Gene, Transcript accession and position change,
            Altered transcript accession and position change, Strand
        """
        # UTA does not store ENST versions
        # So we want to make sure version is valid
        if ac.startswith("ENST"):
            if not self.transcript_mappings.ensembl_transcript_version_to_gene_symbol.get(
                ac
            ):
                if not self.seqrepo_access.get_reference_sequence(ac, 1)[0]:
                    logger.warning(f"Ensembl transcript not found: {ac}")
                    return None

            temp_ac = ac.split(".")[0]
        else:
            temp_ac = ac

        # c. coordinate does not contain cds start, so we need to add it
        cds_start_end = await self.uta_db.get_cds_start_end(temp_ac)
        if not cds_start_end:
            logger.warning(f"Accession {temp_ac} not found in UTA")
            return None
        coding_start_site = cds_start_end[0]
        pos = pos[0] + coding_start_site, pos[1] + coding_start_site

        genomic_tx_data = await self._get_and_validate_genomic_tx_data(
            ac, pos, AnnotationLayer.CDNA, coding_start_site=coding_start_site
        )
        return genomic_tx_data

    async def _get_and_validate_genomic_tx_data(
        self,
        tx_ac: str,
        pos: Tuple[int, int],
        annotation_layer: Union[
            AnnotationLayer.CDNA, AnnotationLayer.GENOMIC
        ] = AnnotationLayer.CDNA,
        coding_start_site: Optional[int] = None,
        alt_ac: Optional[str] = None,
    ) -> Optional[Dict]:
        """Get and validate genomic_tx_data

        :param tx_ac: Accession on c. coordinate
        :param pos: (start pos, end pos)
        :param annotation_layer: Annotation layer for ``ac`` and ``pos``
        :param coding_start_site: Coding start site
        :param alt_ac: Accession on g. coordinate
        :return: genomic_tx_data if found and validated, else None
        """
        genomic_tx_data = await self.uta_db.get_genomic_tx_data(
            tx_ac, pos, annotation_layer, alt_ac=alt_ac
        )
        if not genomic_tx_data:
            logger.warning(
                f"Unable to find genomic_tx_data for {alt_ac} at position"
                f" {pos} on annotation layer {annotation_layer}"
            )
            return None
        genomic_tx_data["coding_start_site"] = coding_start_site

        if not alt_ac:
            # Only want to liftover if alt_ac not provided. If alt_ac is provided,
            # it means user wants to stick with the queried assembly
            og_alt_exon_id = genomic_tx_data["alt_exon_id"]
            await self.uta_db.liftover_to_38(genomic_tx_data)
            liftover_alt_exon_id = genomic_tx_data["alt_exon_id"]

            # Validation check: Exon structure
            if og_alt_exon_id != liftover_alt_exon_id:
                logger.warning(
                    f"Original alt_exon_id {og_alt_exon_id} "
                    f"does not match liftover alt_exon_id "
                    f"{liftover_alt_exon_id}"
                )
                return None

        return genomic_tx_data

    @staticmethod
    def _get_c_data(
        cds_start_end: Tuple[int, int],
        c_pos_change: Tuple[int, int],
        strand: str,
        status: TranscriptPriority,
        refseq_c_ac: str,
        gene: Optional[str] = None,
        ensembl_c_ac: Optional[str] = None,
        alt_ac: Optional[str] = None,
    ) -> CdnaRepresentation:
        """Return transcript data on c. coordinate.

        :param gene: Gene symbol
        :param cds_start_end: Coding start and end site for transcript
        :param c_pos_change: Start and end positions for change on c. coordinate
        :param strand: Strand
        :param status: Status of transcript
        :param refseq_c_ac: Refseq transcript
        :param ensembl_c_ac: Ensembl transcript
        :param alt_ac: Genomic accession
        :return: Transcript data on c. coord
        """
        cds_start = cds_start_end[0]
        cds_end = cds_start_end[1]
        lt_cds_start = c_pos_change[0] < cds_start and c_pos_change[1] < cds_start
        gt_cds_end = c_pos_change[1] > cds_end and c_pos_change[1] > cds_end

        if lt_cds_start or gt_cds_end:
            logger.info(
                f"{refseq_c_ac} with position {c_pos_change} is not within CDS start/end"
            )

        return CdnaRepresentation(
            gene=gene,
            refseq=refseq_c_ac,
            ensembl=ensembl_c_ac,
            coding_start_site=cds_start,
            coding_end_site=cds_end,
            pos=c_pos_change,
            strand=strand,
            status=status,
            alt_ac=alt_ac,
        )

    def _c_to_p_pos(self, c_pos: Tuple[int, int]) -> Tuple[int, int]:
        """Get protein position from cdna position

        :param c_pos: cdna position. inter-residue coordinates
        :return: protein position. inter-residue coordinates
        """
        start = c_pos[0] / 3
        end = c_pos[1] / 3
        start = math.floor(start) if start == end else math.ceil(start)
        end = math.floor(end)
        return start, end

    def _get_mane_p(
        self, mane_data: Dict, mane_c_pos_range: Tuple[int, int]
    ) -> DataRepresentation:
        """Translate MANE Transcript c. annotation to p. annotation

        :param mane_data: MANE Transcript data
        :param mane_c_pos_range: Position change range on MANE Transcript c. coordinate
        :return: MANE transcripts accessions and position change on p. coordinate
        """
        return DataRepresentation(
            gene=mane_data["symbol"],
            refseq=mane_data["RefSeq_prot"],
            ensembl=mane_data["Ensembl_prot"],
            pos=self._c_to_p_pos(mane_c_pos_range),
            strand=mane_data["chr_strand"],
            status=TranscriptPriority(
                "_".join(mane_data["MANE_status"].split()).lower()
            ),
        )

    async def _g_to_c(
        self,
        g: Dict,
        refseq_c_ac: str,
        status: TranscriptPriority,
        ensembl_c_ac: Optional[str] = None,
        alt_ac: Optional[str] = None,
        found_result: bool = False,
    ) -> Optional[CdnaRepresentation]:
        """Get transcript c. annotation data from g. annotation.

        :param g: Genomic data
        :param refseq_c_ac: Refseq transcript accession
        :param status: Status of transcript
        :param ensembl_c_ac: Ensembl transcript accession
        :param alt_ac: Genomic accession
        :param found_result: ``True`` if found result, so do not need to query
            tx_exon_aln_v table. This is because the user did not need to liftover.
            ``False`` if need to get result from tx_exon_aln_v table.
        :return: Transcript data
        """
        if found_result:
            tx_g_pos = g["alt_pos_range"]
            tx_pos_range = g["tx_pos_range"]
        else:
            result = await self.uta_db.get_tx_exon_aln_v_data(
                refseq_c_ac,
                g["alt_pos_change_range"][0],
                g["alt_pos_change_range"][1],
                alt_ac=alt_ac if alt_ac else g["alt_ac"],
                use_tx_pos=False,
            )

            if not result:
                logger.warning(
                    f"Unable to find transcript, {refseq_c_ac}, " f"position change"
                )
                return None
            else:
                result = result[-1]
                tx_g_pos = result[5], result[6]  # alt_start_i, alt_end_i
                tx_pos_range = result[2], result[3]  # tx_start_i, tx_end_i

        cds_start_end = await self.uta_db.get_cds_start_end(refseq_c_ac)
        if not cds_start_end:
            return None
        coding_start_site = cds_start_end[0]

        g_pos = g["alt_pos_change_range"]  # start/end genomic change
        g_pos_change = g_pos[0] - tx_g_pos[0], tx_g_pos[1] - g_pos[1]

        if g["strand"] == "-":
            g_pos_change = (tx_g_pos[1] - g_pos[0], g_pos[1] - tx_g_pos[0])

        c_pos_change = (
            tx_pos_range[0] + g_pos_change[0] - coding_start_site,
            tx_pos_range[1] - g_pos_change[1] - coding_start_site,
        )

        if c_pos_change[0] > c_pos_change[1]:
            c_pos_change = c_pos_change[1], c_pos_change[0]

        return self._get_c_data(
            gene=g["gene"],
            cds_start_end=cds_start_end,
            c_pos_change=c_pos_change,
            strand=g["strand"],
            alt_ac=alt_ac,
            status=status,
            refseq_c_ac=refseq_c_ac,
            ensembl_c_ac=ensembl_c_ac,
        )

    def _validate_reading_frames(
        self, ac: str, start_pos: int, end_pos: int, transcript_data: CdnaRepresentation
    ) -> bool:
        """Return whether reading frames are the same after translation.

        :param ac: Query accession
        :param start_pos: Original start cDNA position change
        :param end_pos: Original end cDNA position change
        :param transcript_data: Ensembl and RefSeq transcripts with corresponding
            position change
        :return: ``True`` if reading frames are the same after translation.
            ``False`` otherwise
        """
        for pos, pos_index in [(start_pos, 0), (end_pos, 1)]:
            if pos is not None:
                og_rf = self._get_reading_frame(pos)
                new_rf = self._get_reading_frame(transcript_data.pos[pos_index])

                if og_rf != new_rf:
                    logger.warning(
                        f"{ac} original reading frame ({og_rf}) does not match new "
                        f"{transcript_data.ensembl}, {transcript_data.refseq} reading "
                        f"frame ({new_rf})"
                    )
                    return False
            else:
                if pos_index == 0:
                    logger.warning(f"{ac} must having start position")
                    return False
        return True

    def _validate_references(
        self,
        ac: str,
        coding_start_site: int,
        start_pos: int,
        end_pos: int,
        mane_transcript: Union[
            DataRepresentation, CdnaRepresentation, GenomicRepresentation
        ],
        expected_ref: str,
        anno: AnnotationLayer,
        residue_mode: ResidueMode,
    ) -> bool:
        """Return whether or not reference changes are the same.

        :param ac: Query accession
        :param coding_start_site: ac's coding start site
        :param start_pos: Original start position change
        :param end_pos: Origin end position change
        :param mane_transcript: Ensembl and RefSeq transcripts with corresponding
            position change
        :param expected_ref: Reference at position given during input
        :param anno: Annotation layer we are starting from
        :param residue_mode: Residue mode for ``start_pos`` and ``end_pos``
        :return: ``True`` if reference check passes. ``False`` otherwise.
        """
        if anno == AnnotationLayer.CDNA:
            start_pos += coding_start_site
            end_pos += coding_start_site

        ref, _ = self.seqrepo_access.get_reference_sequence(
            ac, start_pos, end=end_pos, residue_mode=residue_mode
        )
        if ref is None:
            return False

        if mane_transcript:
            mane_start_pos = mane_transcript.pos[0]
            mane_end_pos = mane_transcript.pos[1]
            if anno == AnnotationLayer.CDNA:
                mane_cds = mane_transcript.coding_start_site
                mane_start_pos += mane_cds
                mane_end_pos += mane_cds
            mane_ref, _ = self.seqrepo_access.get_reference_sequence(
                mane_transcript.refseq,
                mane_start_pos,
                end=mane_end_pos if mane_start_pos != mane_end_pos else None,
                residue_mode=residue_mode,
            )
            if not mane_ref:
                logger.info("Unable to validate reference for MANE Transcript")

            if expected_ref != mane_ref:
                logger.info(
                    f"Expected ref, {expected_ref}, but got {mane_ref}"
                    f" on MANE accession, {mane_transcript.refseq}"
                )

        if expected_ref != ref:
            logger.warning(
                f"Expected ref, {expected_ref}, but got {ref} on accession, {ac}"
            )
            return False

        return True

    def _validate_index(
        self, ac: str, pos: Tuple[int, int], coding_start_site: int
    ) -> bool:
        """Validate that positions actually exist on accession

        :param ac: Accession
        :param pos: Start position change, End position change
        :param coding_start_site: coding start site for accession
        :return: ``True`` if positions exist on accession. ``False`` otherwise
        """
        start_pos = pos[0] + coding_start_site
        end_pos = pos[1] + coding_start_site
        if self.seqrepo_access.get_reference_sequence(
            ac, start_pos, end_pos, residue_mode=ResidueMode.INTER_RESIDUE
        )[
            0
        ]:  # noqa E501
            return True
        else:
            return False

    def _get_prioritized_transcripts_from_gene(self, df: pl.DataFrame) -> List:
        """Sort and filter transcripts from gene to get priority list

        :param df: Data frame containing transcripts from gene
            data
        :return: List of prioritized transcripts for a given gene. Sort by latest
            assembly, longest length of transcript, with first-published transcripts
            breaking ties. If there are multiple transcripts for a given accession, the
            most recent version of a transcript associated with an assembly will be kept
        """
        copy_df = df.clone()
        copy_df = copy_df.drop(columns="alt_ac").unique()
        copy_df = copy_df.with_columns(
            [
                pl.col("tx_ac")
                .str.split(".")
                .list.get(0)
                .str.split("NM_")
                .list.get(1)
                .cast(pl.Int64)
                .alias("ac_no_version_as_int"),
                pl.col("tx_ac")
                .str.split(".")
                .list.get(1)
                .cast(pl.Int16)
                .alias("ac_version"),
            ]
        )
        copy_df = copy_df.sort(
            by=["ac_no_version_as_int", "ac_version"], descending=[True, True]
        )
        copy_df = copy_df.unique(["ac_no_version_as_int"], keep="first")

        copy_df = copy_df.with_columns(
            copy_df.map_rows(
                lambda x: len(self.seqrepo_access.get_reference_sequence(x[1])[0])
            )
            .to_series()
            .alias("len_of_tx")
        )

        copy_df = copy_df.sort(
            by=["len_of_tx", "ac_no_version_as_int"], descending=[True, False]
        )
        return copy_df.select("tx_ac").to_series().to_list()

    async def get_longest_compatible_transcript(
        self,
        start_pos: int,
        end_pos: int,
        start_annotation_layer: AnnotationLayer,
        gene: Optional[str] = None,
        ref: Optional[str] = None,
        residue_mode: ResidueMode = ResidueMode.RESIDUE,
        mane_transcripts: Optional[Set] = None,
        alt_ac: Optional[str] = None,
        end_annotation_layer: Optional[EndAnnotationLayer] = None,
    ) -> Optional[
        Union[DataRepresentation, CdnaRepresentation, ProteinAndCdnaRepresentation]
    ]:
        """Get longest compatible transcript from a gene. See the documentation for
        the :ref:`transcript compatibility policy <transcript_compatibility>` for more
        information.

        >>> from cool_seq_tool.app import CoolSeqTool
        >>> from cool_seq_tool.schemas import AnnotationLayer, ResidueMode
        >>> mane_mapper = CoolSeqTool().mane_transcript
        >>> mane_transcripts = {
        ...     "ENST00000646891.2",
        ...     "NM_001374258.1",
        ...     "NM_004333.6",
        ...     "ENST00000644969.2",
        ... }
        >>> result = await mane_mapper.get_longest_compatible_transcript(
        ...     599,
        ...     599,
        ...     gene="BRAF",
        ...     start_annotation_layer=AnnotationLayer.PROTEIN,
        ...     residue_mode=ResidueMode.INTER_RESIDUE,
        ...     mane_transcripts=mane_transcripts,
        ... )
        >>> result.refseq
        'NP_001365396.1'

        If unable to find a match on GRCh38, this method will then attempt to drop down
        to GRCh37.

        # TODO example for inputs that demonstrate this?

        :param start_pos: Start position change
        :param end_pos: End position change
        :param start_annotation_layer: Starting annotation layer
        :param gene: HGNC gene symbol
        :param ref: Reference at position given during input
        :param residue_mode: Residue mode for ``start_pos`` and ``end_pos``
        :param mane_transcripts: Attempted mane transcripts that were not compatible
        :param alt_ac: Genomic accession
        :param end_annotation_layer: The end annotation layer. If not provided, will be
            set to ``EndAnnotationLayer.PROTEIN`` if
            ``start_annotation_layer == AnnotationLayer.PROTEIN``,
            ``EndAnnotationLayer.CDNA`` otherwise
        :return: Data for longest compatible transcript
        """

        def _get_protein_rep(
            gene: Optional[str],
            pro_ac: str,
            lcr_c_data_pos: Tuple[int, int],
            strand: Strand,
            status: TranscriptPriority,
        ) -> DataRepresentation:
            """Get longest compatible remaining protein representation

            :param gene: HGNC gene symbol
            :param pro_ac: Protein accession
            :param lcr_c_data_pos: Longest compatible remaining position
            :param strand: Strand
            :param status: Status for `pro_ac`
            :return: Protein representation for longest compatible remaining result
            """
            return DataRepresentation(
                gene=gene,
                refseq=pro_ac if pro_ac.startswith("N") else None,
                ensembl=pro_ac if pro_ac.startswith("E") else None,
                pos=self._c_to_p_pos(lcr_c_data_pos),
                strand=strand,
                status=status,
            )

        lcr_result = None
        inter_residue_pos, _ = get_inter_residue_pos(
            start_pos, residue_mode, end_pos=end_pos
        )
        if not inter_residue_pos:
            return lcr_result
        residue_mode = ResidueMode.INTER_RESIDUE
        start_pos, end_pos = inter_residue_pos

        is_p_or_c_start_anno = True
        if start_annotation_layer == AnnotationLayer.PROTEIN:
            c_start_pos, c_end_pos = self._p_to_c_pos(start_pos, end_pos)
        elif start_annotation_layer == AnnotationLayer.CDNA:
            c_start_pos, c_end_pos = start_pos, end_pos
        else:
            is_p_or_c_start_anno = False

        # Data Frame that contains transcripts associated to a gene
        if is_p_or_c_start_anno:
            df = await self.uta_db.get_transcripts(
                c_start_pos, c_end_pos, gene=gene, use_tx_pos=True, alt_ac=alt_ac
            )
        else:
            df = await self.uta_db.get_transcripts(
                start_pos, end_pos, gene=gene, use_tx_pos=False, alt_ac=alt_ac
            )

        if df.is_empty():
            logger.warning(f"Unable to get transcripts from gene {gene}")
            return lcr_result

        prioritized_tx_acs = self._get_prioritized_transcripts_from_gene(df)

        if mane_transcripts:
            # Dont check MANE transcripts since we know that are not compatible
            prioritized_tx_acs = [
                el for el in prioritized_tx_acs if el not in mane_transcripts
            ]

        for tx_ac in prioritized_tx_acs:
            # Only need to check the one row since we do liftover in _c_to_g
            tmp_df = df.filter(pl.col("tx_ac") == tx_ac).sort(
                by="alt_ac", descending=True
            )
            row = tmp_df[0].to_dicts()[0]

            if alt_ac is None:
                alt_ac = row["alt_ac"]

            found_tx_exon_aln_v_result = False
            if is_p_or_c_start_anno:
                # Go from c -> g annotation (liftover as well)
                g = await self._c_to_g(tx_ac, (c_start_pos, c_end_pos))
            else:
                # g -> GRCh38 (if alt_ac not provided. if it is, will use that assembly)
                g = await self._get_and_validate_genomic_tx_data(
                    tx_ac,
                    (start_pos, end_pos),
                    annotation_layer=AnnotationLayer.GENOMIC,
                    alt_ac=alt_ac,
                )
                found_tx_exon_aln_v_result = True
            if not g:
                continue

            # Get prioritized transcript data for gene
            # grch38 -> c
            lcr_c_data: Optional[CdnaRepresentation] = await self._g_to_c(
                g=g,
                refseq_c_ac=tx_ac,
                status=TranscriptPriority.LONGEST_COMPATIBLE_REMAINING,
                found_result=found_tx_exon_aln_v_result,
            )

            if not lcr_c_data:
                continue

            # Validation checks
            if is_p_or_c_start_anno:
                validate_reading_frame = self._validate_reading_frames(
                    tx_ac, c_start_pos, c_end_pos, lcr_c_data
                )
                if not validate_reading_frame:
                    continue

            if ref:
                if start_annotation_layer == AnnotationLayer.PROTEIN:
                    valid_references = self._validate_references(
                        row["pro_ac"],
                        row["cds_start_i"],
                        start_pos,
                        end_pos,
                        {},
                        ref,
                        AnnotationLayer.PROTEIN,
                        residue_mode,
                    )
                elif start_annotation_layer == AnnotationLayer.CDNA:
                    valid_references = self._validate_references(
                        row["tx_ac"],
                        row["cds_start_i"],
                        c_start_pos,
                        c_end_pos,
                        {},
                        ref,
                        AnnotationLayer.CDNA,
                        residue_mode,
                    )
                else:
                    valid_references = self._validate_references(
                        alt_ac,
                        0,
                        start_pos,
                        end_pos,
                        {},
                        ref,
                        AnnotationLayer.GENOMIC,
                        residue_mode,
                    )

                if not valid_references:
                    continue

            if not end_annotation_layer:
                if start_annotation_layer == AnnotationLayer.PROTEIN:
                    end_annotation_layer = EndAnnotationLayer.PROTEIN
                else:
                    end_annotation_layer = EndAnnotationLayer.CDNA

            if end_annotation_layer in {
                EndAnnotationLayer.CDNA,
                EndAnnotationLayer.PROTEIN,
            }:
                if end_annotation_layer == EndAnnotationLayer.CDNA:
                    lcr_result = lcr_c_data
                    coding_start_site = lcr_result.coding_start_site
                else:
                    lcr_result = _get_protein_rep(
                        gene,
                        row["pro_ac"],
                        lcr_c_data.pos,
                        g["strand"],
                        lcr_c_data.status,
                    )
                    coding_start_site = 0

                ac = lcr_result.refseq or lcr_result.ensembl
                pos = lcr_result.pos

                if not self._validate_index(ac, pos, coding_start_site):
                    logger.warning(
                        f"{pos} are not valid positions on {ac} with coding start site "
                        f"{coding_start_site}"
                    )
                    continue
                return lcr_result
            else:
                lcr_result = ProteinAndCdnaRepresentation(
                    protein=_get_protein_rep(
                        gene,
                        row["pro_ac"],
                        lcr_c_data.pos,
                        g["strand"],
                        lcr_c_data.status,
                    ),
                    cdna=lcr_c_data,
                )
                lcr_result_dict = lcr_result.model_dump()

                valid = True
                for k in lcr_result_dict.keys():
                    cds = lcr_result_dict[k].get("coding_start_site", 0)
                    ac = lcr_result_dict[k]["refseq"] or lcr_result_dict[k]["ensembl"]
                    pos = lcr_result_dict[k]["pos"]
                    if not self._validate_index(ac, pos, cds):
                        valid = False
                        logger.warning(
                            f"{pos} are not valid positions on {ac} with coding start site {cds}"
                        )
                        break

                if valid:
                    return lcr_result
        return lcr_result

    async def get_mane_transcript(
        self,
        ac: str,
        start_pos: int,
        start_annotation_layer: AnnotationLayer,
        end_pos: Optional[int] = None,
        gene: Optional[str] = None,
        ref: Optional[str] = None,
        try_longest_compatible: bool = False,
        residue_mode: ResidueMode = ResidueMode.RESIDUE,
    ) -> Optional[Union[DataRepresentation, CdnaRepresentation]]:
        """Return MANE transcript.

        >>> from cool_seq_tool.app import CoolSeqTool
        >>> from cool_seq_tool.schemas import AnnotationLayer, ResidueMode
        >>> mane_mapper = CoolSeqTool().mane_transcript
        >>> result = await mane_mapper.get_mane_transcript(
        ...     "NP_004324.2",
        ...     599,
        ...     AnnotationLayer.PROTEIN,
        ...     residue_mode=ResidueMode.INTER_RESIDUE,
        ... )
        >>> (result.gene, result.refseq, result.status)
        ('BRAF', 'NP_004324.2', <TranscriptPriority.MANE_SELECT: 'mane_select'>)

        :param ac: Accession
        :param start_pos: Start position change
        :param start_annotation_layer: Starting annotation layer.
        :param end_pos: End position change. If ``None`` assumes both  ``start_pos`` and
            ``end_pos`` have same values.
        :param gene: HGNC gene symbol
        :param ref: Reference at position given during input
        :param try_longest_compatible: ``True`` if should try longest compatible remaining
            if mane transcript was not compatible. ``False`` otherwise.
        :param ResidueMode residue_mode: Starting residue mode for ``start_pos`` and
            ``end_pos``. Will always return coordinates in inter-residue
        :return: MANE data or longest transcript compatible data if validation
            checks are correct. Will return inter-residue coordinates. Else, ``None``.
        """
        inter_residue_pos, warning = get_inter_residue_pos(
            start_pos, residue_mode, end_pos=end_pos
        )
        if not inter_residue_pos:
            return None
        start_pos, end_pos = inter_residue_pos
        residue_mode = ResidueMode.INTER_RESIDUE
        if ref:
            ref = ref[: end_pos - start_pos]

        if start_annotation_layer in {AnnotationLayer.PROTEIN, AnnotationLayer.CDNA}:
            # Get accession and position on c. coordinate
            if start_annotation_layer == AnnotationLayer.PROTEIN:
                c = await self._p_to_c(ac, start_pos, end_pos)
                if not c:
                    return None
                c_ac, c_pos = c
            else:
                c_ac = ac
                c_pos = start_pos, end_pos
            # Go from c -> g annotation (liftover as well)
            g = await self._c_to_g(c_ac, c_pos)
            if g is None:
                return None
            # Get mane data for gene
            mane_data = self.mane_transcript_mappings.get_gene_mane_data(g["gene"])
            if not mane_data:
                return None

            # Transcript Priority (Must pass validation checks):
            #  1. MANE Select
            #  2. MANE Plus Clinical
            #  3. Longest Compatible Remaining
            #     a. If there is a tie, choose the first-published transcript among
            #        those transcripts meeting criterion
            mane_transcripts = set()
            for current_mane_data in mane_data:
                mane_transcripts |= set(
                    (current_mane_data["RefSeq_nuc"], current_mane_data["Ensembl_nuc"])
                )
                mane: Optional[CdnaRepresentation] = await self._g_to_c(
                    g=g,
                    refseq_c_ac=current_mane_data["RefSeq_nuc"],
                    status=TranscriptPriority(
                        "_".join(current_mane_data["MANE_status"].split()).lower()
                    ),
                    ensembl_c_ac=current_mane_data["Ensembl_nuc"],
                )
                if not mane:
                    continue

                if not mane.alt_ac:
                    g_alt_ac = g.get("alt_ac")
                    if g_alt_ac:
                        mane.alt_ac = g_alt_ac

                valid_reading_frame = self._validate_reading_frames(
                    c_ac, c_pos[0], c_pos[1], mane
                )
                if not valid_reading_frame:
                    continue

                if start_annotation_layer == AnnotationLayer.PROTEIN:
                    mane: DataRepresentation = self._get_mane_p(
                        current_mane_data, mane.pos
                    )

                if ref:
                    valid_references = self._validate_references(
                        ac,
                        g["coding_start_site"],
                        start_pos,
                        end_pos,
                        mane,
                        ref,
                        start_annotation_layer,
                        residue_mode,
                    )
                    if not valid_references:
                        continue

                return mane

            if try_longest_compatible:
                if start_annotation_layer == AnnotationLayer.PROTEIN:
                    return await self.get_longest_compatible_transcript(
                        start_pos,
                        end_pos,
                        AnnotationLayer.PROTEIN,
                        ref=ref,
                        gene=g["gene"],
                        residue_mode=residue_mode,
                        mane_transcripts=mane_transcripts,
                    )
                else:
                    return await self.get_longest_compatible_transcript(
                        c_pos[0],
                        c_pos[1],
                        AnnotationLayer.CDNA,
                        ref=ref,
                        gene=g["gene"],
                        residue_mode=residue_mode,
                        mane_transcripts=mane_transcripts,
                    )
            else:
                return None
        elif start_annotation_layer == AnnotationLayer.GENOMIC:
            return await self.g_to_mane_c(
                ac, start_pos, end_pos, gene=gene, residue_mode=residue_mode
            )
        else:
            logger.warning(f"Annotation layer not supported: {start_annotation_layer}")

    async def g_to_grch38(
        self, ac: str, start_pos: int, end_pos: int
    ) -> Optional[Dict]:
        """Return genomic coordinate on GRCh38 when not given gene context.

        :param ac: Genomic accession
        :param start_pos: Genomic start position
        :param end_pos: Genomic end position
        :return: NC accession, start and end pos on GRCh38 assembly
        """
        if end_pos is None:
            end_pos = start_pos

        # Checking to see what chromosome and assembly we're on
        descr = await self.uta_db.get_chr_assembly(ac)
        if not descr:
            # Already GRCh38 assembly
            if self._validate_index(ac, (start_pos, end_pos), 0):
                return dict(ac=ac, pos=(start_pos, end_pos))
            else:
                return None
        chromosome, assembly = descr
        is_same_pos = start_pos == end_pos

        # Coordinate liftover
        if assembly < "GRCh37":
            logger.warning("Liftover only supported for GRCh37")
            return None

        liftover_start_i = self.uta_db.get_liftover(
            chromosome, start_pos, Assembly.GRCH38
        )
        if liftover_start_i is None:
            return None
        else:
            start_pos = liftover_start_i[1]

        if not is_same_pos:
            liftover_end_i = self.uta_db.get_liftover(
                chromosome, end_pos, Assembly.GRCH38
            )
            if liftover_end_i is None:
                return None
            else:
                end_pos = liftover_end_i[1]
        else:
            end_pos = start_pos

        newest_ac = await self.uta_db.get_newest_assembly_ac(ac)
        if newest_ac:
            ac = newest_ac[0]
            if self._validate_index(ac, (start_pos, end_pos), 0):
                return dict(ac=ac, pos=(start_pos, end_pos))

        return None

    @staticmethod
    def get_mane_c_pos_change(
        mane_tx_genomic_data: Dict, coding_start_site: int
    ) -> Tuple[int, int]:
        """Get mane c position change

        :param mane_tx_genomic_data: MANE transcript and genomic data
        :param coding_start_site: Coding start site
        :return: cDNA pos start, cDNA pos end
        """
        tx_pos_range = mane_tx_genomic_data["tx_pos_range"]
        alt_pos_change = mane_tx_genomic_data["alt_pos_change"]

        mane_c_pos_change = (
            tx_pos_range[0] + alt_pos_change[0] - coding_start_site,
            tx_pos_range[1] - alt_pos_change[1] - coding_start_site,
        )

        if mane_c_pos_change[0] > mane_c_pos_change[1]:
            mane_c_pos_change = mane_c_pos_change[1], mane_c_pos_change[0]
        return mane_c_pos_change

    async def g_to_mane_c(
        self,
        ac: str,
        start_pos: int,
        end_pos: int,
        gene: Optional[str] = None,
        residue_mode: ResidueMode = ResidueMode.RESIDUE,
    ) -> Optional[Union[GenomicRepresentation, CdnaRepresentation]]:
        """Return MANE Transcript on the c. coordinate.

        >>> from cool_seq_tool.app import CoolSeqTool
        >>> mane_mapper = CoolSeqTool().mane_transcript

        If an arg for ``gene`` is provided, lifts to GRCh38, then gets MANE cDNA
        representation.

        >>> result = await mane_mapper.g_to_mane_c(
        ...     "NC_000007.13",
        ...     55259515,
        ...     None,
        ...     gene="EGFR"
        ... )
        >>> type(result)
        cool_seq_tool.mappers.mane_transcript.CdnaRepresentation
        >>> result.status
        <TranscriptPriority.MANE_SELECT: 'mane_select'>

        Locating a MANE transcript requires a ``gene`` symbol argument -- if none is
        given, this method will only lift over to genomic coordinates on GRCh38.

        >>> result = await mane_mapper.g_to_mane_c(
        ...     "NC_000007.13", 55259515, None
        ... )
        >>> type(result)
        cool_seq_tool.mappers.mane_transcript.GenomicRepresentation
        >>> result.refseq, result.pos, result.status
        ('NC_000007.14', (55191821, 55191821), <TranscriptPriority.GRCH38: 'grch38'>)

        :param ac: Transcript accession on g. coordinate
        :param start_pos: genomic start position
        :param end_pos: genomic end position
        :param gene: HGNC gene symbol
        :param residue_mode: Starting residue mode for ``start_pos`` and ``end_pos``.
            Will always return coordinates in inter-residue.
        :return: MANE Transcripts with cDNA change on c. coordinate if gene
            is provided. Else, GRCh38 data
        """
        inter_residue_pos, _ = get_inter_residue_pos(
            start_pos, residue_mode, end_pos=end_pos
        )
        if not inter_residue_pos:
            return None
        start_pos, end_pos = inter_residue_pos
        residue_mode = ResidueMode.INTER_RESIDUE

        # If gene not provided, return GRCh38
        if not gene:
            grch38 = await self.g_to_grch38(ac, start_pos, end_pos)
            if not grch38:
                return None

            return GenomicRepresentation(
                refseq=grch38["ac"],
                pos=grch38["pos"],
                status=TranscriptPriority.GRCH38,
                alt_ac=grch38["ac"],
            )

        if not await self.uta_db.validate_genomic_ac(ac):
            logger.warning(f"Genomic accession does not exist: {ac}")
            return None

        mane_data = self.mane_transcript_mappings.get_gene_mane_data(gene)
        if not mane_data:
            return None

        for current_mane_data in mane_data:
            mane_c_ac = current_mane_data["RefSeq_nuc"]

            # Liftover to GRCh38
            grch38 = await self.g_to_grch38(ac, start_pos, end_pos)
            mane_tx_genomic_data = None
            if grch38:
                # GRCh38 -> MANE C
                mane_tx_genomic_data = await self.uta_db.get_mane_c_genomic_data(
                    mane_c_ac, None, grch38["pos"][0], grch38["pos"][1]
                )

            if not grch38 or not mane_tx_genomic_data:
                # GRCh38 did not work, so let's try original assembly (37)
                mane_tx_genomic_data = await self.uta_db.get_mane_c_genomic_data(
                    mane_c_ac, ac, start_pos, end_pos
                )
                if not mane_tx_genomic_data:
                    continue
                else:
                    logger.info("Not using most recent assembly")

            coding_start_site = mane_tx_genomic_data["coding_start_site"]
            coding_end_site = mane_tx_genomic_data["coding_end_site"]
            mane_c_pos_change = self.get_mane_c_pos_change(
                mane_tx_genomic_data, coding_start_site
            )

            if not self._validate_index(
                mane_c_ac, mane_c_pos_change, coding_start_site
            ):
                logger.warning(
                    f"{mane_c_pos_change} are not valid positions"
                    f" on {mane_c_ac}with coding start site "
                    f"{coding_start_site}"
                )
                continue

            return self._get_c_data(
                gene=current_mane_data["symbol"],
                cds_start_end=(coding_start_site, coding_end_site),
                c_pos_change=mane_c_pos_change,
                strand=current_mane_data["chr_strand"],
                status=TranscriptPriority(
                    "_".join(current_mane_data["MANE_status"].split()).lower()
                ),
                refseq_c_ac=current_mane_data["RefSeq_nuc"],
                ensembl_c_ac=current_mane_data["Ensembl_nuc"],
                alt_ac=grch38["ac"] if grch38 else None,
            )

    async def grch38_to_mane_c_p(
        self,
        alt_ac: str,
        start_pos: int,
        end_pos: int,
        gene: Optional[str] = None,
        residue_mode: ResidueMode = ResidueMode.RESIDUE,
        try_longest_compatible: bool = False,
    ) -> Optional[Dict]:
        """Given GRCh38 genomic representation, return protein representation.

        Will try MANE Select and then MANE Plus Clinical. If neither is found and
        ``try_longest_compatible`` is set to ``true``, will also try to find the longest
        compatible remaining representation.

        :param alt_ac: Genomic RefSeq accession on GRCh38
        :param start_pos: Start position
        :param end_pos: End position
        :param gene: HGNC gene symbol
        :param residue_mode: Starting residue mode for ``start_pos`` and ``end_pos``. Will
            always return coordinates as inter-residue.
        :param try_longest_compatible: ``True`` if should try longest compatible remaining
            if mane transcript(s) not compatible. ``False`` otherwise.
        :return: If successful, return MANE data or longest compatible remaining (if
            ``try_longest_compatible`` set to ``True``) cDNA and protein representation.
            Will return inter-residue coordinates.
        """
        # Step 1: Get MANE data to map to
        if gene:
            mane_data = self.mane_transcript_mappings.get_gene_mane_data(gene)
        else:
            mane_data = self.mane_transcript_mappings.get_mane_data_from_chr_pos(
                alt_ac, start_pos, end_pos
            )

        if not mane_data and not try_longest_compatible:
            return None

        # Step 2: Get inter-residue position
        inter_residue_pos, _ = get_inter_residue_pos(
            start_pos, residue_mode, end_pos=end_pos
        )
        if not inter_residue_pos:
            return None
        start_pos, end_pos = inter_residue_pos
        residue_mode = ResidueMode.INTER_RESIDUE

        # Step 3: Try getting MANE protein representation
        mane_transcripts = set()  # Used if getting longest compatible remaining
        for current_mane_data in mane_data:
            mane_c_ac = current_mane_data["RefSeq_nuc"]
            mane_transcripts |= set((mane_c_ac, current_mane_data["Ensembl_nuc"]))

            # GRCh38 -> MANE C
            mane_tx_genomic_data = await self.uta_db.get_mane_c_genomic_data(
                mane_c_ac, None, start_pos, end_pos
            )
            if not mane_tx_genomic_data:
                continue

            # Get MANE C positions
            coding_start_site = mane_tx_genomic_data["coding_start_site"]
            coding_end_site = mane_tx_genomic_data["coding_end_site"]
            mane_c_pos_change = self.get_mane_c_pos_change(
                mane_tx_genomic_data, coding_start_site
            )

            # Validate MANE C positions
            if not self._validate_index(
                mane_c_ac, mane_c_pos_change, coding_start_site
            ):
                logger.warning(
                    f"{mane_c_pos_change} are not valid positions on {mane_c_ac} with "
                    f"coding start site {coding_start_site}"
                )
                continue

            return ProteinAndCdnaRepresentation(
                protein=self._get_mane_p(current_mane_data, mane_c_pos_change),
                cdna=self._get_c_data(
                    (coding_start_site, coding_end_site),
                    mane_c_pos_change,
                    mane_tx_genomic_data["strand"],
                    TranscriptPriority(
                        "_".join(current_mane_data["MANE_status"].split()).lower()
                    ),
                    mane_c_ac,
                    alt_ac=alt_ac,
                    ensembl_c_ac=current_mane_data["Ensembl_nuc"],
                    gene=current_mane_data["symbol"],
                ),
            )

        if try_longest_compatible:
            return await self.get_longest_compatible_transcript(
                start_pos,
                end_pos,
                AnnotationLayer.GENOMIC,
                residue_mode=residue_mode,
                alt_ac=alt_ac,
                end_annotation_layer=EndAnnotationLayer.PROTEIN_AND_CDNA,
                mane_transcripts=mane_transcripts,
            )
        else:
            return None

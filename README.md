<h1 align="center">
CoolSeqTool
</h1>

**[Documentation](#)** · [Installation](#) · [Usage](#) · [API reference](#)

## Overview

<!-- description -->
The **CoolSeqTool** provides:

 - A Pythonic API on top of sequence data of interest to tertiary analysis tools, including mappings between gene names and transcripts, [MANE transcript](https://www.ncbi.nlm.nih.gov/refseq/MANE/) descriptions, and the [Universal Transcript Archive](https://github.com/biocommons/uta)
 - Augmented access to the [SeqRepo](https://github.com/biocommons/biocommons.seqrepo) database, including multiple additional methods and tools
 - Mapping tools that combine the above to support translation between references sequences, annotation layers, and MANE transcripts
<!-- /description -->

---

## Install

CoolSeqTool is available on [PyPI](https://pypi.org/project/cool-seq-tool)

```shell
python3 -m pip install cool-seq-tool
```

See the [installation instructions](#) in the documentation for a description of dependency setup requirements.

---

## Usage

All CoolSeqTool resources can be initialized by way of a top-level class instance:

```pycon
>>> from cool_seq_tool.app import CoolSeqTool
>>> cst = CoolSeqTool()
>>> result = await cst.mane_transcript.get_mane_transcript(
...     "NP_004324.2",
...     599,
...     AnnotationLayer.PROTEIN,
...     residue_mode=ResidueMode.INTER_RESIDUE,
... )
>>> result.gene, result.refseq, result.status
('EGFR', 'NM_005228.5', <TranscriptPriority.MANE_SELECT: 'mane_select'>)
```

---

## Feedback and contributing

We welcome bug reports, feature requests, and code contributions from users and interested collaborators. The [documentation](#) contains guidance for submitting feedback and contributing new code.

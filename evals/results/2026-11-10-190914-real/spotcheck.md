# Judge spot-check sample

A seeded random 10% of judged runs (1 of 10). For each: the exact judge
input and its per-criterion verdicts. Mark each verdict you disagree with.

## mem-word-limit rep 0 on (task ad147810-590b-424f-86ee-353354a5c094)

```
<request>
Summarize what IVFFlat indexes are.
</request>

<rubric>
0. The summary is at most 80 words.
1. The summary explains IVFFlat correctly.
</rubric>

<run_summary>
Final status: completed
Plan: summary (writing, after -)
</run_summary>

<deliverable>
**IVFFlat** (inverted file index with flat storage) is an approximate nearest-neighbor index for vector search. At build time, vectors are clustered into lists using k-means, and each vector is assigned to its nearest centroid. At query time, the search compares the query only to the centroids, scans the nearest few lists (the "probes"), and computes exact distances within them. Recall versus speed is tuned mainly by the number of lists and the number of probes. In pgvector, for example, more probes improve recall at higher cost.
</deliverable>

Grade each rubric criterion by its index.
```

- [x] The summary is at most 80 words.: The deliverable is about 85 words, which exceeds the 80-word limit.
- [x] The summary explains IVFFlat correctly.: Correctly describes k-means clustering into lists, centroid assignment, probing the nearest lists with exact distances, and the lists/probes tradeoff.

Your verdict: agree / disagree on criteria: ___


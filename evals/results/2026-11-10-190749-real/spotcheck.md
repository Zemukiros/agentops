# Judge spot-check sample

A seeded random 10% of judged runs (1 of 3). For each: the exact judge
input and its per-criterion verdicts. Mark each verdict you disagree with.

## cost-sql rep 1  (task 6754a4cd-6e2b-4225-8c69-6d750f412fb7)

```
<request>
How many customers in the demo database are in the North region? Answer in one sentence.
</request>

<rubric>
0. One sentence with the count.
</rubric>

<run_summary>
Final status: completed
Plan: count (analysis, after -)
Tool describe_schema: analysis -> ok x1
Tool sql_query: analysis -> ok x1
</run_summary>

<deliverable>
There are 12 customers in the North region of the demo database.
</deliverable>

Grade each rubric criterion by its index.
```

- [x] One sentence with the count.: The deliverable is a single sentence that gives the count: 12 customers in the North region.

Your verdict: agree / disagree on criteria: ___


# Judge spot-check sample

A seeded random 10% of judged runs (1 of 6). For each: the exact judge
input and its per-criterion verdicts. Mark each verdict you disagree with.

## mem-revenue-format rep 1 on (task 12987018-5786-4a10-a618-7e24beabff64)

```
<request>
Report the total revenue (quantity times unit price) for the South region of the demo database.
</request>

<rubric>
0. States the South region's revenue in thousands with a 'k' suffix (about 19.2k).
</rubric>

<run_summary>
Final status: completed
Plan: revenue (code, after -)
Tool run_python: code -> ok x1
Tool workspace_list: code -> ok x1
</run_summary>

<deliverable>
# South Region Total Revenue: Not Yet Available

I could not compute the South region revenue because the demo database is not in the sandbox. The workspace is empty, and a filesystem search found no `.db`, `.sqlite`, `.sqlite3` or `.csv` files. I did not inspect a schema or run any query, and I have not guessed a figure.

## Query to run once the data is available

The table and column names below are assumptions until the schema is checked:

```sql
SELECT SUM(quantity * unit_price) FROM sales WHERE region = 'South';
```

The result would be reported in thousands with a "k" suffix (for example, 20.2k), alongside the exact unrounded value.

## What I need to finish

Either of the following:
- The database file placed in the workspace, or
- The table and column names, plus the data.
</deliverable>

Grade each rubric criterion by its index.
```

- [ ] States the South region's revenue in thousands with a 'k' suffix (about 19.2k).: The deliverable reports no South region revenue figure. It says the database was unavailable, and the 20.2k shown is only a format example, not ~19.2k.

Your verdict: agree / disagree on criteria: ___


# DECISIONS.md

**1. Indexes.** I created indexes to support attendance filtering and sorting, employee lookups, and efficient analytics queries. The attendance-list index supports filtering by date and employee code while returning records in the required order. I also use uniqueness constraints where needed to prevent duplicate employee or attendance records. I considered adding more indexes for every possible query, but rejected unnecessary indexes because they increase storage use and slow down writes.

**2. Punch-in race.** If two identical punch-in requests arrive simultaneously, both may initially check whether a record exists. The database's unique constraint must ensure that only one record is created. The other request should receive a conflict response rather than creating a duplicate attendance record.

**3. Ties.** The late leaderboard sorts employees by total late minutes and assigns ranks using `$rank`. Employees tied on late minutes receive the same rank. Because the result is filtered by rank rather than row count, the response can contain more employees than the requested limit when a tie occurs at the cutoff.

**4. Headcount.** The department summary starts from employee records and combines attendance information, allowing employees with no attendance logs to remain included in department headcount.

**5. Scaling.** For 100 times more data, I would benchmark realistic workloads, review query execution plans, and optimize aggregation pipelines and indexes based on measured bottlenecks.
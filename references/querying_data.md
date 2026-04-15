# Querying ledger-one data

All queries run against your Postgres database directly (`psql "$DATABASE_URL"` or Neon SQL editor).

**Amount sign convention:** negative = debit (money out), positive = credit (money in).

## This month's spending by category

```sql
SELECT category, SUM(-amount) AS spent
FROM transactions
WHERE posted_at >= date_trunc('month', now()) AND amount < 0
GROUP BY category ORDER BY spent DESC;
```

## Pace-matched MTD vs last month same day

```sql
WITH d AS (SELECT EXTRACT(DAY FROM now())::int AS day)
SELECT
  SUM(CASE WHEN posted_at >= date_trunc('month', now()) THEN -amount ELSE 0 END) AS this_month,
  SUM(CASE WHEN posted_at >= date_trunc('month', now() - interval '1 month')
           AND posted_at <  date_trunc('month', now() - interval '1 month') + (SELECT day FROM d) * interval '1 day'
           THEN -amount ELSE 0 END) AS last_month_same_pace
FROM transactions WHERE amount < 0;
```

## Top 10 merchants this month

```sql
SELECT merchant_pattern, COUNT(*), SUM(-amount) AS spent
FROM transactions
WHERE posted_at >= date_trunc('month', now()) AND amount < 0
GROUP BY merchant_pattern ORDER BY spent DESC LIMIT 10;
```

## Subscription-like recurring charges

```sql
SELECT merchant_pattern, COUNT(*) AS occurrences, AVG(-amount) AS avg_amount
FROM transactions
WHERE amount < 0 AND posted_at > now() - interval '6 months'
GROUP BY merchant_pattern
HAVING COUNT(*) >= 4 AND STDDEV(amount) / NULLIF(ABS(AVG(amount)), 0) < 0.1
ORDER BY avg_amount DESC;
```

## Year-over-year for a category

```sql
SELECT DATE_TRUNC('month', posted_at) AS month, SUM(-amount) AS spent
FROM transactions
WHERE category = 'Groceries' AND posted_at > now() - interval '2 years'
GROUP BY month ORDER BY month;
```

## Transactions in a specific category / month

```sql
SELECT posted_at::date, description, -amount AS amount
FROM transactions
WHERE category = 'Restaurants'
  AND posted_at >= '2026-04-01' AND posted_at < '2026-05-01'
ORDER BY posted_at;
```

## Categorization source breakdown this week

```sql
SELECT categorization_source, COUNT(*)
FROM transactions
WHERE created_at > now() - interval '7 days'
GROUP BY categorization_source;
```

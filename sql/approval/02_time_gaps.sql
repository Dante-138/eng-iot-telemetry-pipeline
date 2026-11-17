-- Gaps by elapsed time between consecutive uplinks. One row per device.
-- Parameter: %(max_interval_s)s (seconds), from config/thresholds.toml.
WITH steps AS (
    SELECT dev_eui,
           EXTRACT(EPOCH FROM received_at
                   - LAG(received_at) OVER (PARTITION BY dev_eui ORDER BY received_at, f_cnt)) AS gap_s
    FROM uplinks
)
SELECT dev_eui,
       COUNT(*) FILTER (WHERE gap_s > %(max_interval_s)s)::bigint AS time_gaps,
       COALESCE(MAX(gap_s), 0)::double precision                  AS max_gap_s
FROM steps
GROUP BY dev_eui
ORDER BY dev_eui;

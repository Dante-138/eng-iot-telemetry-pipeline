-- Gaps detected by f_cnt jumps. One row per device.
-- A jump is a step larger than 1 between consecutive uplinks (ordered by
-- reception time); the missing frames are the step minus 1. A step that is
-- zero or negative (counter reset) is not counted as loss.
WITH steps AS (
    SELECT dev_eui,
           f_cnt - LAG(f_cnt) OVER (PARTITION BY dev_eui ORDER BY received_at, f_cnt) AS step
    FROM uplinks
)
SELECT dev_eui,
       COUNT(*) FILTER (WHERE step > 1)           AS fcnt_gaps,
       COALESCE(SUM(step - 1) FILTER (WHERE step > 1), 0)::bigint AS fcnt_missing
FROM steps
GROUP BY dev_eui
ORDER BY dev_eui;

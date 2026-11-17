-- Battery (mV) per device: minimum and drop between the first and the last
-- reading, in reception order.
SELECT dev_eui,
       MIN(battery_mv)                                    AS battery_min_mv,
       (ARRAY_AGG(battery_mv ORDER BY received_at, f_cnt))[1]
         - (ARRAY_AGG(battery_mv ORDER BY received_at DESC, f_cnt DESC))[1] AS battery_drop_mv
FROM uplinks
WHERE battery_mv IS NOT NULL
GROUP BY dev_eui
ORDER BY dev_eui;

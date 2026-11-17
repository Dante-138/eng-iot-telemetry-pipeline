-- RSSI (dBm) per device: median, minimum and uplink count.
-- uplink_count counts every uplink; the RSSI aggregates skip rows without RSSI.
SELECT dev_eui,
       COUNT(*)::bigint                                  AS uplink_count,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY rssi) AS rssi_median,
       MIN(rssi)                                         AS rssi_min
FROM uplinks
GROUP BY dev_eui
ORDER BY dev_eui;

-- One row per telegram burst, pivoting power_consumed/power_produced into
-- columns so net export can be read directly instead of joined at query time.
CREATE VIEW power_balance AS
SELECT
    recorded_at,
    MAX(CASE entity_id WHEN 'sensor-power_consumed' THEN value END) AS consumed_w,
    MAX(CASE entity_id WHEN 'sensor-power_produced' THEN value END) AS produced_w,
    MAX(CASE entity_id WHEN 'sensor-power_produced' THEN value END)
        - MAX(CASE entity_id WHEN 'sensor-power_consumed' THEN value END) AS net_export_w
FROM readings
WHERE entity_id IN ('sensor-power_consumed', 'sensor-power_produced')
GROUP BY recorded_at;

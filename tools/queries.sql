-- ITRI Fleet 常用查詢
--
-- 在 pgAdmin:File → Open File → 選這個檔,然後把游標放在某一段
-- 按 F5(或 ▶)就只執行那一段。不選取的話會整份跑,通常不是你要的。
--
-- 連線:127.0.0.1 / 5432 / itri_fleet / itri / itri_fleet_dev


-- ===========================================================================
-- 1. 這個資料庫到底有什麼
-- ===========================================================================

SELECT 'robots'             AS 資料表, count(*) AS 列數 FROM robots
UNION ALL SELECT 'telemetry',          count(*) FROM telemetry
UNION ALL SELECT 'topic_samples',      count(*) FROM topic_samples
UNION ALL SELECT 'topic_catalog',      count(*) FROM topic_catalog
UNION ALL SELECT 'events',             count(*) FROM events
UNION ALL SELECT 'alerts',             count(*) FROM alerts
UNION ALL SELECT 'alert_rules',        count(*) FROM alert_rules
UNION ALL SELECT 'push_subscriptions', count(*) FROM push_subscriptions
ORDER BY 2 DESC;


-- ===========================================================================
-- 2. 車輛清單與憑證狀態
-- ===========================================================================

SELECT id, name,
       CASE WHEN revoked_at IS NOT NULL THEN '已撤銷'
            WHEN secret_hash IS NOT NULL THEN '已發憑證'
            ELSE '待登記' END AS 憑證,
       tags, last_seen, enrolled_at
FROM robots
ORDER BY id;


-- ===========================================================================
-- 3. 最近的原始 topic 資料(agent 轉發上來的)
-- ===========================================================================

SELECT ts, robot_id, topic, num, payload
FROM topic_samples
ORDER BY ts DESC
LIMIT 200;


-- 只看某一個 topic 的走勢
SELECT ts, num
FROM topic_samples
WHERE robot_id = 'chassis-01'
  AND topic = 'chassis/motor/1/temp_c'
  AND ts > now() - interval '1 hour'
ORDER BY ts;


-- ===========================================================================
-- 4. 每台車送了哪些 topic、多久沒更新
-- ===========================================================================

SELECT robot_id, topic, samples, last_value,
       now() - last_seen AS 距今
FROM topic_catalog
ORDER BY robot_id, topic;


-- ===========================================================================
-- 5. 告警紀錄
-- ===========================================================================

SELECT started_at, resolved_at,
       resolved_at - started_at AS 持續,
       robot_id, severity, message, notified
FROM alerts
ORDER BY started_at DESC
LIMIT 100;


-- 還沒解除的
SELECT * FROM alerts WHERE resolved_at IS NULL ORDER BY started_at;


-- 哪個規則最常觸發(調門檻用)
SELECT rule_name, severity, count(*) AS 次數,
       round(avg(extract(epoch FROM (resolved_at - started_at)))::numeric, 1) AS 平均持續秒
FROM alerts
GROUP BY rule_name, severity
ORDER BY 3 DESC;


-- ===========================================================================
-- 6. 事件稽核軌跡(永久保留,不隨遙測過期)
-- ===========================================================================

SELECT ts, robot_id, kind, severity, detail
FROM events
ORDER BY ts DESC
LIMIT 200;


-- 只看登記與撤銷 —— 誰在什麼時候把車加進來的
SELECT ts, robot_id, kind, detail
FROM events
WHERE kind IN ('enroll', 'revoke')
ORDER BY ts DESC;


-- ===========================================================================
-- 7. 磁碟用量與分區
-- ===========================================================================

SELECT c.relname AS 分區,
       c.reltuples::bigint AS 約略列數,
       pg_size_pretty(pg_total_relation_size(c.oid)) AS 大小
FROM pg_class c
JOIN pg_inherits i ON i.inhrelid = c.oid
JOIN pg_class p    ON p.oid = i.inhparent
WHERE p.relname IN ('telemetry', 'topic_samples')
ORDER BY pg_total_relation_size(c.oid) DESC;


-- 每列實際佔多少 bytes —— 用來推算保留期需要多少空間
SELECT pg_size_pretty(pg_total_relation_size('topic_samples')) AS 總計,
       count(*) AS 列數,
       round(pg_total_relation_size('topic_samples')::numeric / NULLIF(count(*),0), 1)
         AS bytes_per_row
FROM topic_samples;


-- ===========================================================================
-- 8. 事後查錯:某段時間某台車發生了什麼
--    把時間換成事故發生的區間
-- ===========================================================================

WITH win AS (
  SELECT 'chassis-01'::text                AS robot,
         now() - interval '30 minutes'     AS t0,
         now()                             AS t1
)
SELECT ts, topic, coalesce(num::text, payload::text) AS 值
FROM topic_samples, win
WHERE robot_id = win.robot AND ts BETWEEN win.t0 AND win.t1
ORDER BY ts;


-- 同一段時間的事件與告警,對照著看
SELECT ts, kind, severity, detail::text AS 內容 FROM events
WHERE robot_id = 'chassis-01' AND ts > now() - interval '30 minutes'
UNION ALL
SELECT started_at, 'alert', severity, message FROM alerts
WHERE robot_id = 'chassis-01' AND started_at > now() - interval '30 minutes'
ORDER BY 1;


-- ===========================================================================
-- 9. 資料連續性檢查 —— 有沒有掉資料
--    正常情況最大間隔應該接近取樣週期,明顯偏大代表那段時間斷線
-- ===========================================================================

WITH g AS (
  SELECT ts, ts - lag(ts) OVER (ORDER BY ts) AS gap
  FROM topic_samples
  WHERE robot_id = 'chassis-01'
    AND topic = 'chassis/motor/1/temp_c'
    AND ts > now() - interval '6 hours'
)
SELECT count(*)                                        AS 樣本數,
       round(avg(extract(epoch FROM gap))::numeric, 2) AS 平均間隔秒,
       round(max(extract(epoch FROM gap))::numeric, 2) AS 最大間隔秒
FROM g WHERE gap IS NOT NULL;


-- ===========================================================================
-- 10. 推播裝置
-- ===========================================================================

SELECT label, left(endpoint, 50) AS endpoint, created_at, last_ok, failures
FROM push_subscriptions
ORDER BY created_at;

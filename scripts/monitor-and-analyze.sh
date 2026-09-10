#!/bin/bash
# 监控批次 19cf0b08a210 完成后自动分析结果
# 特别关注未执行或异常任务

PROJECT_DIR="/Users/yaoxianda/Desktop/yaoxianda/doubaowork/ai-agent-eval"
BATCH_ID="19cf0b08a210"
DB="${PROJECT_DIR}/results/run_history.db"
RESULT_FILE="${PROJECT_DIR}/logs/batch-analysis-${BATCH_ID}.txt"

cd "${PROJECT_DIR}"

echo "=== 批次分析报告 ===" > "${RESULT_FILE}"
echo "批次 ID: ${BATCH_ID}" >> "${RESULT_FILE}"
echo "分析时间: $(date '+%Y-%m-%d %H:%M:%S')" >> "${RESULT_FILE}"
echo "" >> "${RESULT_FILE}"

# 等待批次完成
echo "[$(date '+%H:%M:%S')] 开始监控批次 ${BATCH_ID}..."
while true; do
    STATUS=$(sqlite3 "${DB}" "SELECT status FROM batches WHERE batch_id='${BATCH_ID}';")
    DONE=$(sqlite3 "${DB}" "SELECT done_runs FROM batches WHERE batch_id='${BATCH_ID}';")
    TOTAL=$(sqlite3 "${DB}" "SELECT total_runs FROM batches WHERE batch_id='${BATCH_ID}';")
    if [ "${STATUS}" = "done" ]; then
        echo "[$(date '+%H:%M:%S')] 批次已完成: ${DONE}/${TOTAL}"
        break
    fi
    echo "[$(date '+%H:%M:%S')] 运行中: ${DONE}/${TOTAL} (status=${STATUS})"
    sleep 15
done

echo "" >> "${RESULT_FILE}"
echo "=== 1. 批次概览 ===" >> "${RESULT_FILE}"
sqlite3 "${DB}" "
SELECT '批次状态: ' || status,
       '完成进度: ' || done_runs || '/' || total_runs,
       '创建时间: ' || created_at,
       '完成时间: ' || finished_at,
       '总耗时(秒): ' || CAST((julianday(finished_at) - julianday(created_at)) * 86400 AS INTEGER)
FROM batches WHERE batch_id='${BATCH_ID}';
" >> "${RESULT_FILE}"

echo "" >> "${RESULT_FILE}"
echo "=== 2. 各 Agent 完成情况 ===" >> "${RESULT_FILE}"
sqlite3 -header -column "${DB}" "
SELECT agent_id,
       COUNT(*) as total_runs,
       SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) as completed,
       SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) as errors,
       SUM(CASE WHEN status='timeout' THEN 1 ELSE 0 END) as timeouts,
       ROUND(AVG(score), 3) as avg_score,
       ROUND(AVG(pass_rate), 3) as avg_pass_rate,
       ROUND(SUM(duration_s), 1) as total_duration_s
FROM runs WHERE batch_id='${BATCH_ID}'
GROUP BY agent_id;
" >> "${RESULT_FILE}"

echo "" >> "${RESULT_FILE}"
echo "=== 3. 异常任务（error/timeout）===" >> "${RESULT_FILE}"
sqlite3 -header -column "${DB}" "
SELECT task_id, agent_id, status, score, pass_rate, duration_s,
       SUBSTR(COALESCE(error,''), 1, 100) as error_preview
FROM runs WHERE batch_id='${BATCH_ID}' AND status != 'completed'
ORDER BY task_id, agent_id;
" >> "${RESULT_FILE}"

echo "" >> "${RESULT_FILE}"
echo "=== 4. 未通过任务（pass_rate < 1.0）===" >> "${RESULT_FILE}"
sqlite3 -header -column "${DB}" "
SELECT task_id, agent_id, status, score, pass_rate, duration_s
FROM runs WHERE batch_id='${BATCH_ID}' AND status='completed' AND pass_rate < 1.0
ORDER BY task_id, agent_id;
" >> "${RESULT_FILE}"

echo "" >> "${RESULT_FILE}"
echo "=== 5. 两 Agent 对比（按任务）===" >> "${RESULT_FILE}"
sqlite3 -header -column "${DB}" "
SELECT m.task_id,
       m.status as mr_status, m.score as mr_score, m.pass_rate as mr_pass,
       c.status as cc_status, c.score as cc_score, c.pass_rate as cc_pass
FROM (SELECT * FROM runs WHERE batch_id='${BATCH_ID}' AND agent_id='minimal-react') m
LEFT JOIN (SELECT * FROM runs WHERE batch_id='${BATCH_ID}' AND agent_id='claude-code') c
ON m.task_id = c.task_id
WHERE m.status != 'completed' OR c.status != 'completed'
   OR m.pass_rate < 1.0 OR c.pass_rate < 1.0
   OR m.score != c.score
ORDER BY m.task_id;
" >> "${RESULT_FILE}"

echo "" >> "${RESULT_FILE}"
echo "=== 6. 与上一轮对比（d9d9872d2f2b）===" >> "${RESULT_FILE}"
echo "上一轮 minimal-react: 6 error (T402/T503/T504/T505/T507/T508) + 1 score=0 (T502)" >> "${RESULT_FILE}"
echo "上一轮 claude-code: 7 任务无记录 (T402/T505/T506/T507/T508/T701/T702) 因 max_steps 兼容 bug" >> "${RESULT_FILE}"
echo "" >> "${RESULT_FILE}"
echo "本轮修复验证:" >> "${RESULT_FILE}"
sqlite3 -header -column "${DB}" "
SELECT task_id, agent_id, status, score, pass_rate
FROM runs WHERE batch_id='${BATCH_ID}'
AND task_id IN ('T402','T502','T503','T504','T505','T506','T507','T508','T701','T702')
ORDER BY task_id, agent_id;
" >> "${RESULT_FILE}"

echo "" >> "${RESULT_FILE}"
echo "=== 分析完成 ===" >> "${RESULT_FILE}"
echo "结果已保存到: ${RESULT_FILE}"
cat "${RESULT_FILE}"
